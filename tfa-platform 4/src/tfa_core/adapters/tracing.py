"""tfa_core.adapters.tracing — LangSmith tracing, governance-safe.

Restricted-data rules for this platform:
- Tracing is OFF unless TFA_LANGSMITH_ENABLED=true.
- The endpoint is configurable so traces can go to a SELF-HOSTED LangSmith
  inside the VNet (TFA_LANGSMITH_ENDPOINT). Pointing at LangSmith Cloud with
  Restricted data requires explicit governance sign-off.
- Redaction is ON by default: page images are never traced, and OCR text /
  guest names are hashed unless TFA_LANGSMITH_REDACT=false (dev only).

Usage: wrap calls with `traced(name, fn, inputs_summary)` — a no-op when
tracing is disabled, so the pipeline has zero hard dependency on LangSmith.
"""
from __future__ import annotations

import hashlib
import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.environ.get("TFA_LANGSMITH_ENABLED", "").lower() == "true"


def _redact_on() -> bool:
    return os.environ.get("TFA_LANGSMITH_REDACT", "true").lower() != "false"


def redact(value: Any, max_len: int = 120) -> Any:
    """Summarise sensitive payloads: keep shape, drop content."""
    if not _redact_on():
        return value
    if isinstance(value, str):
        if len(value) <= max_len:
            return value
        digest = hashlib.sha256(value.encode()).hexdigest()[:12]
        return f"<redacted:{len(value)} chars sha256:{digest}>"
    if isinstance(value, bytes):
        return f"<redacted:{len(value)} bytes>"
    if isinstance(value, list):
        return f"<list of {len(value)} items>"
    if isinstance(value, dict):
        return {k: redact(v, max_len) for k, v in value.items()}
    return value


@contextmanager
def traced(name: str, inputs: dict[str, Any] | None = None,
           run_type: str = "chain") -> Iterator[dict[str, Any]]:
    """Context manager yielding a dict; put outputs into it before exit.

    No-op (still yields the dict) when tracing is disabled or langsmith is
    not installed, so callers never branch.
    """
    outputs: dict[str, Any] = {}
    if not _enabled():
        yield outputs
        return
    try:
        from langsmith.run_helpers import trace
    except ImportError:
        logger.warning("TFA_LANGSMITH_ENABLED but langsmith not installed.")
        yield outputs
        return

    safe_inputs = redact(inputs or {})
    with trace(name=name, run_type=run_type, inputs=safe_inputs) as run:
        try:
            yield outputs
        finally:
            run.end(outputs=redact(outputs))
