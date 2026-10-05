"""Regression tests for INCIDENT-2025-08.

Each test fails against the pre-fix code. They pin the *behaviour*, not the
implementation, so a future refactor cannot quietly reintroduce the defect.
"""
from __future__ import annotations

import ast
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from tfa_core.adapters.sql_engine import (
    ConnectionPool,
    SqlConfig,
    TokenProvider,
    compute_document_id,
    content_hash,
)

ROOT = Path(__file__).resolve().parents[1]


# ===========================================================================
# P1 — unstable idempotency key caused duplicate financial rows
# ===========================================================================

def test_identity_is_content_addressed_not_metadata_addressed():
    """Identical bytes must always yield the same id, no matter how many times
    the blob is rewritten (each rewrite changes the ETag)."""
    content = b"%PDF-1.4 folio"
    ids = {
        compute_document_id("ar_batch", "factura.pdf", content_hash(content))
        for _ in range(5)
    }
    assert len(ids) == 1


def test_different_bytes_yield_different_identity():
    a = compute_document_id("b", "f.pdf", content_hash(b"one"))
    b = compute_document_id("b", "f.pdf", content_hash(b"two"))
    assert a != b


def test_reprocessing_the_same_file_does_not_duplicate_rows():
    """Simulates the delete-then-insert upsert keyed on document_id across
    three reprocesses. Pre-fix this produced 3 rows and 3x the overcharge."""
    table: dict[str, Decimal] = {}
    content, net = b"%PDF-1.4 folio", Decimal("78.48")

    for _ in range(3):
        doc_id = compute_document_id("ar_batch", "factura.pdf", content_hash(content))
        table.pop(doc_id, None)
        table[doc_id] = net

    assert len(table) == 1
    assert sum(table.values()) == net


def test_api_upload_and_worker_agree_on_identity():
    """The id the API hands back to an uploader must be the id that is stored."""
    content = b"%PDF-1.4 folio"
    api_id = compute_document_id("b1", "f.pdf", content_hash(content))
    worker_id = compute_document_id("b1", "f.pdf", content_hash(content))
    assert api_id == worker_id


def test_blob_download_does_not_hand_back_a_hash():
    """`download()` returning "md5-or-etag" is what let ETags become identity.
    The signature must not offer that footgun again."""
    src = (ROOT / "src/tfa/infrastructure/azure/storage.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "download":
            returns = ast.unparse(node.returns) if node.returns else ""
            assert "tuple" not in returns.lower(), (
                f"download() returns {returns}; it must return bytes only")
            return
    pytest.fail("BlobRepository.download not found")


def test_reconciliation_uses_the_shared_identity_function():
    src = (ROOT / "src/tfa/domain/services/reconciliation.py").read_text()
    assert "compute_document_id(" in src
    assert "hashlib.sha256(" not in src, "local identity implementation reintroduced"


# ===========================================================================
# P2 — status written under a different key than it is read by
# ===========================================================================

def test_all_status_writes_use_the_same_document_id():
    """In-flight and failed states were keyed on a blob ETag while the terminal
    write used document_id, so GET /status 404'd for exactly the documents an
    operator needed to see."""
    src = (ROOT / "src/tfa/application/use_cases/ingest_document.py").read_text()
    marks = [ln for ln in src.splitlines() if "_status.mark(" in ln]
    assert marks, "no status writes found"
    assert all("document_id=document_id" in ln for ln in marks), \
        f"status write not keyed on document_id: {marks}"


def test_identity_is_established_before_the_first_status_write():
    src = (ROOT / "src/tfa/application/use_cases/ingest_document.py").read_text()
    assert src.index("document_id = compute_document_id(") < src.index("_status.mark(")


# ===========================================================================
# P3 — pooled connections returned with an open implicit transaction
# ===========================================================================

class FakeCursor:
    def execute(self, *a, **kw):
        return self

    def fetchone(self):
        return (1,)


class FakeConn:
    def __init__(self, log):
        self.log = log
        self.autocommit = None
        self.closed = False

    def cursor(self):
        return FakeCursor()

    def commit(self):
        self.log.append("commit")

    def rollback(self):
        self.log.append("rollback")

    def close(self):
        self.closed = True
        self.log.append("close")


def _pool(log, **kw):
    class FakeCred:
        def get_token(self, scope):
            class T:
                token, expires_on = "t", 9e9
            return T()

    pool = ConnectionPool(SqlConfig(server="s", database="d", pool_size=2),
                          TokenProvider(FakeCred()), **kw)
    pool._new_connection = lambda: FakeConn(log)  # type: ignore[method-assign]
    return pool


def test_read_only_lease_terminates_the_transaction():
    """A SELECT under autocommit=False opens a transaction; returning that
    connection to the pool leaves it idle-in-transaction, holding locks."""
    log: list[str] = []
    pool = _pool(log)
    with pool.lease() as conn:
        conn.cursor().execute("SELECT 1")
    assert log[-1] in ("commit", "rollback"), f"transaction left open: {log}"


def test_explicit_read_only_lease_rolls_back():
    log: list[str] = []
    pool = _pool(log)
    with pool.lease(commit=False) as conn:
        conn.cursor().execute("SELECT 1")
    assert "rollback" in log


def test_failed_lease_rolls_back_and_discards_the_connection():
    log: list[str] = []
    pool = _pool(log)
    with pytest.raises(RuntimeError):
        with pool.lease():
            raise RuntimeError("boom")
    assert "rollback" in log and "close" in log


def test_hot_connection_is_not_revalidated_on_every_lease():
    """Probing liveness on every lease adds a round trip to every query."""
    log: list[str] = []
    pool = _pool(log, validate_after_idle_s=60.0)
    checks = {"n": 0}
    original = pool._is_alive

    def counting(conn):
        checks["n"] += 1
        return original(conn)

    pool._is_alive = counting  # type: ignore[method-assign]
    for _ in range(5):
        with pool.lease():
            pass
    assert checks["n"] == 0


# ===========================================================================
# P4 — configuration read at import time took down the whole Function App
# ===========================================================================

def test_function_app_reads_no_settings_at_module_scope():
    """A single bad TFA_* setting raised during import, which the Functions
    host reports as worker indexing failure — all four functions dead."""
    tree = ast.parse((ROOT / "functions/function_app.py").read_text())
    offenders: list[int] = []

    # Only statements that EXECUTE at import time count. Calls inside a
    # FunctionDef body run when the function is invoked, which is fine.
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and getattr(sub.func, "id", "") in {
                    "get_settings", "get_credential"}:
                offenders.append(sub.lineno)

    assert not offenders, f"config accessed at import time on line(s) {offenders}"


def test_rate_cache_is_built_lazily_and_once():
    calls = {"n": 0}
    lock = threading.Lock()
    cache = {"v": None}

    def build():
        with lock:
            if cache["v"] is None:
                calls["n"] += 1
                cache["v"] = object()
        return cache["v"]

    threads = [threading.Thread(target=build) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls["n"] == 1


# ===========================================================================
# P5 — the "cheap" cache probe downloaded both blobs in full
# ===========================================================================

def test_version_probe_does_not_download_payload():
    src = (ROOT / "functions/function_app.py").read_text()
    probe = src.split("def _rate_context_version")[1].split("\ndef ")[0]
    assert "blobs.download(" not in probe, "version probe transfers full payload"
    assert "blobs.version(" in probe


def test_blob_version_uses_properties_only():
    src = (ROOT / "src/tfa/infrastructure/azure/storage.py").read_text()
    body = src.split("def version(")[1].split("\n    def ")[0]
    assert "get_blob_properties" in body
    assert "download_blob" not in body
