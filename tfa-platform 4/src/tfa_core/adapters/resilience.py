"""tfa_core.adapters.resilience — caching, pooled HTTP, retry, and rate limiting.

Three production concerns that were promised in the architecture but absent
from the code:

1. `TtlCache` / `CachedProvider` — the worker reloaded the negotiated-rate and
   FX blobs (2 GETs + JSON parse + full RateDirectory rebuild) for *every
   queue message*. At 5,000 documents that is 10,000 avoidable round trips.
   Now refreshed only on TTL expiry, and skipped entirely when the source
   ETags are unchanged.

2. `http_client()` — every outbound call (LLM, JWKS, Graph) constructed a new
   `httpx.Client`, i.e. a fresh TCP + TLS handshake per request. Now a
   process-wide pooled client per base configuration.

3. `retry_request()` + `TokenBucket` — the design specifies exponential
   backoff with jitter on 429/5xx and a client-side limiter for Foundry
   quota; neither existed. Without them, 100 worker replicas self-inflict
   429 storms.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Callable, Generic, Optional, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


# --------------------------------------------------------------------------- #
# Caching
# --------------------------------------------------------------------------- #

@dataclass
class _Entry(Generic[T]):
    value: T
    version: Optional[str]
    loaded_at: float


class CachedProvider(Generic[T]):
    """Thread-safe TTL cache with an optional cheap version probe.

    `version_fn` should be something far cheaper than `load_fn` (blob ETags,
    a MAX(updated_at) query). When the version is unchanged the cached value
    is kept and the TTL clock resets, so a stable rate table costs one HEAD
    per TTL window instead of a full reload.
    """

    def __init__(self, load_fn: Callable[[], T], ttl_seconds: float = 300.0,
                 version_fn: Optional[Callable[[], str]] = None, name: str = "cache"):
        self._load = load_fn
        self._version = version_fn
        self._ttl = ttl_seconds
        self._name = name
        self._lock = threading.Lock()
        self._entry: Optional[_Entry[T]] = None

    def get(self) -> T:
        entry = self._entry
        if entry is not None and (time.monotonic() - entry.loaded_at) < self._ttl:
            return entry.value

        with self._lock:
            entry = self._entry
            if entry is not None and (time.monotonic() - entry.loaded_at) < self._ttl:
                return entry.value

            version: Optional[str] = None
            if self._version is not None:
                try:
                    version = self._version()
                except Exception:
                    logger.warning("%s: version probe failed; forcing reload.",
                                   self._name, exc_info=True)

            if entry is not None and version is not None and version == entry.version:
                entry.loaded_at = time.monotonic()   # unchanged upstream
                logger.debug("%s: version unchanged (%s); reusing cache.",
                             self._name, version)
                return entry.value

            value = self._load()
            self._entry = _Entry(value=value, version=version, loaded_at=time.monotonic())
            logger.info("%s: reloaded (version=%s).", self._name, version)
            return value

    def invalidate(self) -> None:
        with self._lock:
            self._entry = None


# --------------------------------------------------------------------------- #
# Pooled HTTP
# --------------------------------------------------------------------------- #

_clients: dict[str, httpx.Client] = {}
_clients_lock = threading.Lock()


def http_client(name: str, timeout_s: float = 300.0, max_connections: int = 32,
                follow_redirects: bool = False) -> httpx.Client:
    """Process-wide pooled client, keyed by purpose."""
    client = _clients.get(name)
    if client is not None and not client.is_closed:
        return client
    with _clients_lock:
        client = _clients.get(name)
        if client is None or client.is_closed:
            client = httpx.Client(
                timeout=httpx.Timeout(timeout_s, connect=10.0),
                limits=httpx.Limits(max_connections=max_connections,
                                    max_keepalive_connections=max_connections // 2),
                follow_redirects=follow_redirects,
            )
            _clients[name] = client
            logger.debug("Created pooled HTTP client '%s'.", name)
        return client


def close_http_clients() -> None:
    with _clients_lock:
        for client in _clients.values():
            try:
                client.close()
            except Exception:
                pass
        _clients.clear()


# --------------------------------------------------------------------------- #
# Retry
# --------------------------------------------------------------------------- #

@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    jitter: float = 0.3

    def delay_for(self, attempt: int, retry_after: Optional[float] = None) -> float:
        if retry_after is not None:
            return min(retry_after, self.max_delay_s)
        raw = min(self.base_delay_s * (2 ** (attempt - 1)), self.max_delay_s)
        # Full jitter: prevents 100 replicas retrying in lockstep after a 429.
        return raw * (1.0 - self.jitter + random.random() * self.jitter * 2)


def retry_request(send: Callable[[], httpx.Response],
                  policy: Optional[RetryPolicy] = None,
                  sleep: Callable[[float], None] = time.sleep) -> httpx.Response:
    """Sends with exponential backoff + jitter on retryable statuses and
    transport errors. Honours `Retry-After` when the service supplies it."""
    policy = policy or RetryPolicy()
    last_error: Optional[Exception] = None

    for attempt in range(1, policy.max_attempts + 1):
        try:
            response = send()
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_error = exc
            if attempt == policy.max_attempts:
                raise
            delay = policy.delay_for(attempt)
            logger.warning("Transport error (attempt %d/%d): %s; retrying in %.1fs.",
                           attempt, policy.max_attempts, exc, delay)
            sleep(delay)
            continue

        if response.status_code not in RETRYABLE_STATUS:
            return response
        if attempt == policy.max_attempts:
            return response

        retry_after = None
        header = response.headers.get("retry-after")
        if header:
            try:
                retry_after = float(header)
            except ValueError:
                retry_after = None
        delay = policy.delay_for(attempt, retry_after)
        logger.warning("HTTP %d (attempt %d/%d); retrying in %.1fs.",
                       response.status_code, attempt, policy.max_attempts, delay)
        sleep(delay)

    if last_error:
        raise last_error
    raise RuntimeError("retry_request exhausted without a response")


# --------------------------------------------------------------------------- #
# Client-side rate limiting
# --------------------------------------------------------------------------- #

class TokenBucket:
    """Smooths burst load against a provider quota (e.g. Foundry TPM/RPM).

    Cheaper than discovering the limit through 429s, and keeps a fleet of
    workers from collectively overrunning a shared deployment.
    """

    def __init__(self, rate_per_second: float, capacity: Optional[float] = None):
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be > 0")
        self._rate = rate_per_second
        self._capacity = capacity if capacity is not None else max(1.0, rate_per_second)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0,
                sleep: Callable[[float], None] = time.sleep) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._capacity,
                                   self._tokens + (now - self._updated) * self._rate)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait = (tokens - self._tokens) / self._rate
            sleep(wait)
