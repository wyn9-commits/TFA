"""tfa_core.adapters.sql_engine — shared SQL access primitives.

Replaces the duplicated `_connect()` / `_rows()` implementations that lived in
both `tfa.presentation.api.repository` and `tfa.infrastructure.persistence.sql.repositories`, and removes the
per-query Entra token round-trip that was the dominant API latency cost.

Three things live here and nowhere else:

1. `TokenProvider`  — caches the Entra access token and refreshes at 80% of
   its lifetime. Previously every query called `credential.get_token()`, i.e.
   an AAD round-trip per SQL statement.
2. `ConnectionPool` — bounded pool of pyodbc connections, health-checked on
   lease. Previously every query opened a new TCP+TLS+login.
3. `rows_as_dicts` / `KeysetPage` — the cursor→dict mapping and the
   `TOP (n+1)` keyset pagination that were each written twice.

Thread-safe: workers and the API both run these under threadpools.
"""
from __future__ import annotations

import base64
import logging
import struct
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from queue import Empty, LifoQueue
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Generic,
    Iterator,
    Optional,
    Sequence,
    TypeVar,
)

if TYPE_CHECKING:  # pyodbc is an adapter concern; pure helpers import cleanly
    import pyodbc

logger = logging.getLogger(__name__)

SQL_SCOPE = "https://database.windows.net/.default"
_SQL_COPT_SS_ACCESS_TOKEN = 1256  # msodbcsql.h
_REFRESH_AT = 0.8                 # refresh once 80% of the token lifetime is gone

T = TypeVar("T")


# --------------------------------------------------------------------------- #
# Token caching
# --------------------------------------------------------------------------- #

class TokenProvider:
    """Caches an Entra token for a scope; refreshes shortly before expiry."""

    def __init__(self, credential: Any, scope: str = SQL_SCOPE):
        self._credential = credential
        self._scope = scope
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_on: float = 0.0
        self._acquired_at: float = 0.0

    def _needs_refresh(self) -> bool:
        if self._token is None:
            return True
        lifetime = self._expires_on - self._acquired_at
        if lifetime <= 0:
            return True
        return (time.time() - self._acquired_at) >= lifetime * _REFRESH_AT

    def get(self) -> str:
        if not self._needs_refresh():
            return self._token  # type: ignore[return-value]
        with self._lock:
            if not self._needs_refresh():
                return self._token  # type: ignore[return-value]
            token = self._credential.get_token(self._scope)
            self._token = token.token
            self._expires_on = float(token.expires_on)
            self._acquired_at = time.time()
            logger.debug("Refreshed SQL access token (expires_on=%s).", self._expires_on)
            return self._token

    def as_odbc_attrs(self) -> dict[int, bytes]:
        raw = self.get().encode("utf-16-le")
        return {_SQL_COPT_SS_ACCESS_TOKEN: struct.pack(f"<I{len(raw)}s", len(raw), raw)}


# --------------------------------------------------------------------------- #
# Connection pooling
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SqlConfig:
    server: str
    database: str
    pool_size: int = 10
    login_timeout_s: int = 30
    driver: str = "ODBC Driver 18 for SQL Server"

    def connection_string(self) -> str:
        return (
            f"Driver={{{self.driver}}};"
            f"Server=tcp:{self.server},1433;Database={self.database};"
            f"Encrypt=yes;TrustServerCertificate=no;"
            f"Connection Timeout={self.login_timeout_s};"
        )


class ConnectionPool:
    """Bounded LIFO pool. LIFO keeps a small hot set of connections warm rather
    than round-robining every connection into idle-timeout territory.

    Liveness is checked only for connections that have been idle longer than
    `validate_after_idle_s`. Probing every lease would add a network round trip
    to every query and give back much of the win the pool exists to deliver.
    """

    def __init__(self, config: SqlConfig, token_provider: TokenProvider,
                 validate_after_idle_s: float = 30.0):
        self._config = config
        self._tokens = token_provider
        self._pool: "LifoQueue[tuple[Any, float]]" = LifoQueue(maxsize=config.pool_size)
        self._validate_after_idle_s = validate_after_idle_s
        self._created = 0
        self._lock = threading.Lock()

    def _new_connection(self) -> "pyodbc.Connection":
        import pyodbc
        conn = pyodbc.connect(
            self._config.connection_string(),
            attrs_before=self._tokens.as_odbc_attrs(),
            timeout=self._config.login_timeout_s,
        )
        conn.autocommit = False
        with self._lock:
            self._created += 1
        logger.debug("Opened SQL connection (%d created).", self._created)
        return conn

    @staticmethod
    def _is_alive(conn: "pyodbc.Connection") -> bool:
        try:
            conn.cursor().execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False

    @contextmanager
    def lease(self, commit: bool = True) -> "Iterator[pyodbc.Connection]":
        """Borrow a connection.

        Contract (INCIDENT-2025-08 P3): the connection runs with
        ``autocommit=False``, so *any* statement — including a bare SELECT —
        opens an implicit transaction on SQL Server. On the success path this
        method therefore COMMITS (or rolls back when ``commit=False``) before
        returning the connection to the pool.

        Without this, a read-only caller that never commits hands an
        idle-in-transaction connection back to the pool: it keeps holding
        locks and pinning the version store, blocks schema changes, and the
        next borrower silently joins someone else's open transaction. Callers
        that commit explicitly are unaffected — the extra commit is a no-op.

        Use ``commit=False`` for read-only work to make the intent explicit.
        """
        try:
            conn, returned_at = self._pool.get_nowait()
            idle_for = time.monotonic() - returned_at
            if idle_for > self._validate_after_idle_s and not self._is_alive(conn):
                self._safe_close(conn)
                conn = self._new_connection()
        except Empty:
            conn = self._new_connection()

        try:
            yield conn
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            self._safe_close(conn)
            raise
        else:
            try:
                # Terminate the implicit transaction before pooling the
                # connection. Safe to call unconditionally.
                if commit:
                    conn.commit()
                else:
                    conn.rollback()
            except Exception:
                self._safe_close(conn)
                raise
            try:
                self._pool.put_nowait((conn, time.monotonic()))
            except Exception:
                self._safe_close(conn)

    @staticmethod
    def _safe_close(conn: "pyodbc.Connection") -> None:
        try:
            conn.close()
        except Exception:
            pass

    def close_all(self) -> None:
        while True:
            try:
                conn, _ = self._pool.get_nowait()
                self._safe_close(conn)
            except Empty:
                return


# --------------------------------------------------------------------------- #
# Result mapping
# --------------------------------------------------------------------------- #

def rows_as_dicts(cursor: Any) -> list[dict[str, Any]]:
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def coerce_bools(rows: Sequence[dict[str, Any]], fields: Sequence[str]) -> None:
    """SQL BIT arrives as 0/1; the wire contract says bool."""
    for row in rows:
        for f in fields:
            if f in row and row[f] is not None:
                row[f] = bool(row[f])


# --------------------------------------------------------------------------- #
# Keyset pagination
# --------------------------------------------------------------------------- #

def encode_cursor(value: int) -> str:
    return base64.urlsafe_b64encode(str(value).encode()).decode()


def decode_cursor(cursor: str) -> int:
    try:
        return int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception as exc:
        raise ValueError(f"Invalid cursor: {exc}") from exc


@dataclass
class KeysetPage(Generic[T]):
    items: list[T]
    next_cursor: Optional[str]


def paginate(rows: list[dict[str, Any]], limit: int, key_field: str,
             transform: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None
             ) -> KeysetPage[dict[str, Any]]:
    """Given `limit + 1` rows fetched with `TOP (limit+1)`, split off the
    lookahead row and derive the next cursor from the last kept row."""
    next_cursor: Optional[str] = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1][key_field])
    if transform:
        rows = [transform(r) for r in rows]
    return KeysetPage(items=rows, next_cursor=next_cursor)



# ---------------------------------------------------------------------------
# Identity moved to tfa_core.domain.identity: "same bytes = same document" is a
# business invariant, not a storage concern. Re-exported while callers migrate.
# ---------------------------------------------------------------------------
from tfa_core.domain.identity import compute_document_id, content_hash  # noqa: E402,F401
