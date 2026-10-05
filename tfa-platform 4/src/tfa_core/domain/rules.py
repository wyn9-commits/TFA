"""tfa_core.domain.rules — constraints on untrusted input that reaches storage.

These are DOMAIN rules, not formatting helpers: an identifier names a
reconciliation batch whose totals feed recovery claims against hotels, so who
may write into which batch is a business boundary, not a string-cleaning
concern.

FINDING SEC-01 (High). The upload route built its blob path as
``f"{identifier}/{name}"`` from a query parameter constrained only by length,
and a filename taken verbatim from the multipart body. An authenticated
Reviewer could therefore write to any path in the raw container:

    identifier="../../rates"  file="fx.xlsx"   -> ../../rates/fx.xlsx
    identifier="2026_q1_audit" file="forged.pdf" -> another team's batch

The second is the damaging one. Blob is a flat namespace, so ``..`` is not
filesystem traversal — but the *first path segment is the batch identity*, and
the ingestion pipeline trusts it. Writing into someone else's batch injects a
forged folio into their reconciliation totals, which is financial fraud with
an audit trail pointing at the wrong person.

Overwriting the rate feed itself (``rates/negotiated_rates.json``) was blocked
only because ``.json`` is absent from the upload extension allow-list — a
single control away from letting a user rewrite the negotiated rates that every
overcharge calculation depends on. That is too thin a margin for the blast
radius, so reserved prefixes are now rejected explicitly.
"""
from __future__ import annotations

import re
import unicodedata

from tfa_core.domain.errors import TerminalError

#: Batch identifiers: lowercase alphanumerics, underscore, hyphen. Matches the
#: convention the legacy application enforced and the SharePoint folder naming.
IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")

#: Filenames: no path separators, no control characters, no leading dot.
FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,199}$")

#: Prefixes the platform reserves for its own data. A caller writing here could
#: replace reference data the reconciliation engine trusts.
RESERVED_PREFIXES = frozenset({"rates", "config", "system", "deployments", "logs"})

ALLOWED_UPLOAD_SUFFIXES = (
    ".pdf", ".png", ".jpg", ".jpeg", ".xlsx", ".xlsm", ".xls", ".csv",
)

#: Bidirectional and zero-width characters. A filename containing U+202E renders
#: right-to-left, so "folio\u202egpj.exe" displays as "folio exe.jpg" to a
#: reviewer deciding whether to open it.
_DANGEROUS_CODEPOINTS = re.compile(r"[\u0000-\u001f\u007f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


class InvalidIdentifier(TerminalError):
    """The batch identifier is not a well-formed, permitted value."""


class InvalidFileName(TerminalError):
    """The filename is unsafe to use as part of a storage path."""


def validate_identifier(value: str) -> str:
    """Returns the identifier if it is safe to use as the first path segment."""
    raw = value or ""
    # Check BEFORE stripping: `.strip()` silently deletes control characters,
    # so "batch\n" would have been normalised into the valid "batch". Anything
    # carrying a control or bidirectional character is rejected outright rather
    # than quietly repaired into something the caller did not send.
    if _DANGEROUS_CODEPOINTS.search(raw):
        raise InvalidIdentifier("Identifier contains control characters.")
    candidate = raw.strip()
    if not IDENTIFIER_PATTERN.match(candidate):
        raise InvalidIdentifier(
            "Identifier must be 1-100 characters of lowercase letters, numbers, "
            "underscores or hyphens, and start with a letter or number."
        )
    if candidate in RESERVED_PREFIXES:
        raise InvalidIdentifier(f"'{candidate}' is reserved for platform data.")
    return candidate


def sanitize_filename(value: str) -> str:
    """Returns a filename safe to append to a storage path.

    Rejects rather than silently rewrites: quietly turning ``../../x.pdf`` into
    ``x.pdf`` would store a file under a name the uploader never chose, and the
    reviewer would later see a document whose name does not match what was sent.
    An explicit 400 tells the caller exactly what to fix.
    """
    candidate = (value or "").strip()
    if not candidate:
        raise InvalidFileName("A filename is required.")

    # Normalise first: composed and decomposed forms of the same name must not
    # produce two different blobs.
    candidate = unicodedata.normalize("NFC", candidate)

    if _DANGEROUS_CODEPOINTS.search(candidate):
        raise InvalidFileName("Filename contains control or bidirectional characters.")
    if "/" in candidate or "\\" in candidate:
        raise InvalidFileName("Filename must not contain path separators.")
    if candidate in {".", ".."} or candidate.startswith("."):
        raise InvalidFileName("Filename must not start with a dot.")
    if not candidate.lower().endswith(ALLOWED_UPLOAD_SUFFIXES):
        raise InvalidFileName(
            "Unsupported file type. Allowed: " + ", ".join(ALLOWED_UPLOAD_SUFFIXES))
    if not FILENAME_PATTERN.match(candidate):
        raise InvalidFileName(
            "Filename may contain letters, numbers, spaces, dots, hyphens, "
            "underscores and parentheses only.")
    return candidate


def build_blob_path(identifier: str, filename: str) -> str:
    """The ONLY supported way to turn caller input into a storage path."""
    safe_identifier = validate_identifier(identifier)
    safe_name = sanitize_filename(filename)
    path = f"{safe_identifier}/{safe_name}"

    # Belt and braces: assert the result is exactly two segments and contains no
    # traversal, so a future edit to either pattern cannot silently widen this.
    segments = path.split("/")
    if len(segments) != 2 or any(s in {"", ".", ".."} for s in segments):
        raise InvalidFileName("Resolved storage path is not a simple batch/file path.")
    return path
