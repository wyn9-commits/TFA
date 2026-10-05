"""Tracing facade.

The orchestrator needs to mark spans; it should not need to know which vendor
records them. This module provides `traced` as a no-op by default and resolves
the real adapter lazily, at first use, only when tracing is switched on.

That lazy resolve is the point: importing `pipeline` must not drag in a tracing
SDK, or the ingestion path stops being testable without one.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

_resolved: Optional[Any] = None
_checked = False


def _backend():
    """Resolves the adapter once. Returns None when tracing is disabled or the
    SDK is absent — a missing tracer must never stop a document processing."""
    global _resolved, _checked
    if _checked:
        return _resolved
    _checked = True
    if os.environ.get("TFA_LANGSMITH_ENABLED", "").lower() != "true":
        return None
    try:
        from tfa_core.adapters import tracing as adapter
        _resolved = adapter
        logger.info("Tracing enabled.")
    except Exception as exc:
        logger.warning("Tracing unavailable, continuing without it: %s", exc)
        _resolved = None
    return _resolved


@contextmanager
def traced(name: str, metadata: Optional[dict] = None) -> Iterator[None]:
    backend = _backend()
    if backend is None:
        yield
        return
    with backend.traced(name, metadata):
        yield
