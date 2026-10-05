"""tfa_core.domain.identity — how a document is identified.

This is a DOMAIN rule, not a storage detail: "the same bytes for the same file
in the same batch are the same document" is a business invariant that governs
idempotency and, through it, the correctness of financial totals.

It lived in the SQL engine module, which meant a rule the whole system depends
on could only be imported by pulling in a database adapter. Pure and
dependency-free here.
"""
from __future__ import annotations

import hashlib


def content_hash(content: bytes) -> str:
    """Content-addressed hash of the document bytes.

    Never derive identity from storage metadata (ETag, content_md5): those
    change per write and are not always populated, which is what produced
    duplicate documents and inflated reconciliation totals (INCIDENT-2025-08).
    """
    return hashlib.sha256(content).hexdigest()


def compute_document_id(identifier: str, file_name: str, content_sha256: str) -> str:
    """THE single definition of a document's identity."""
    return hashlib.sha256(
        f"{identifier}/{file_name}/{content_sha256}".encode()
    ).hexdigest()[:32]
