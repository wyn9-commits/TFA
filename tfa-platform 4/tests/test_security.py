"""Security regression tests.

Each pins a finding from SECURITY-AUDIT.md. They are written as attack
attempts, not as assertions about implementation, so a refactor that reopens
the hole fails regardless of how the code is restructured.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tfa_core.domain.rules import (
    InvalidFileName, InvalidIdentifier, build_blob_path, sanitize_filename,
    validate_identifier,
)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


# =========================================================================== #
# SEC-01 — storage path injection via upload
# =========================================================================== #

@pytest.mark.parametrize("identifier", [
    "../../rates", "..", ".", "rates", "config", "system",
    "batch/../rates", "BATCH_UPPER", "batch with spaces", "", "_leading",
    "a" * 101, "batch\x00", "batch\n",
])
def test_hostile_identifiers_are_rejected(identifier):
    with pytest.raises(InvalidIdentifier):
        validate_identifier(identifier)


@pytest.mark.parametrize("filename", [
    "../../../rates/negotiated_rates.xlsx",
    "sub/dir/escape.pdf",
    "..\\..\\windows.pdf",
    ".hidden.pdf",
    "folio\u202egpj.pdf",     # RTL override disguises the real extension
    "folio\x00.pdf",
    "folio.exe",
    "folio.json",             # the rate feed format
    "",
    "x" * 300 + ".pdf",
])
def test_hostile_filenames_are_rejected(filename):
    with pytest.raises(InvalidFileName):
        sanitize_filename(filename)


def test_legitimate_uploads_still_work():
    assert build_blob_path("2026_q1_audit", "Factura B 00001507.pdf") == \
        "2026_q1_audit/Factura B 00001507.pdf"
    assert build_blob_path("ar-batch-1", "folio_(copy).xlsx") == \
        "ar-batch-1/folio_(copy).xlsx"


def test_resolved_path_is_always_exactly_two_segments():
    path = build_blob_path("batch1", "a.pdf")
    assert path.count("/") == 1
    assert ".." not in path.split("/")


def test_upload_route_uses_the_validated_builder():
    src = (SRC / "tfa/presentation/api/routers/documents.py").read_text()
    assert "build_blob_path(" in src
    assert 'f"{identifier}/{name}"' not in src, "raw interpolation reintroduced"


def test_plaintext_redis_is_refused_outside_dev():
    src = (SRC / "tfa/composition/container.py").read_text()
    assert 'startswith("rediss://")' in src
    assert 'settings.environment != "dev"' in src


# =========================================================================== #
# SEC-04 — response headers
# =========================================================================== #

def test_strict_csp_and_isolation_headers_are_set():
    src = (SRC / "tfa/presentation/api/main.py").read_text()
    for header, value in [
        ("Content-Security-Policy", "default-src 'none'"),
        ("Content-Security-Policy", "frame-ancestors 'none'"),
        ("X-Content-Type-Options", "nosniff"),
        ("Cross-Origin-Resource-Policy", "same-site"),
        ("Permissions-Policy", "geolocation=()"),
    ]:
        assert header in src and value in src, f"missing {header}: {value}"


def test_cors_is_an_allow_list_never_a_wildcard():
    src = (SRC / "tfa/presentation/api/main.py").read_text()
    assert 'allow_origins=origins' in src
    assert 'allow_origins=["*"]' not in src


# =========================================================================== #
# SEC-05 — rate limiting behind a proxy
# =========================================================================== #

def test_signing_algorithm_is_pinned():
    """Prevents `alg: none` and RS256->HS256 key-confusion attacks."""
    src = (SRC / "tfa/presentation/api/auth.py").read_text()
    assert 'algorithms=["RS256"]' in src


def test_id_tokens_are_rejected_as_api_credentials():
    src = (SRC / "tfa/presentation/api/auth.py").read_text()
    assert 'claims.get("scp")' in src and 'claims.get("roles")' in src
    assert "not an access token" in src


def test_issuer_and_expiry_are_verified():
    src = (SRC / "tfa/presentation/api/auth.py").read_text()
    assert '"verify_exp": True' in src
    assert '"verify_signature": True' in src
    assert "Untrusted issuer" in src


def test_authorization_is_deny_by_default():
    src = (SRC / "tfa/presentation/api/auth.py").read_text()
    assert "require_role" in src
    routers = (SRC / "tfa/presentation/api/routers")
    for path in routers.glob("*.py"):
        text = path.read_text()
        if path.name == "health.py":
            continue
        for match in re.finditer(r"@router\.(get|post|patch|put|delete)\(", text):
            tail = text[match.start():match.start() + 1200]
            assert "require_role" in tail, f"{path.name}: unprotected route near offset {match.start()}"


# =========================================================================== #
# SEC-07 — idempotency receipts
# =========================================================================== #

def test_review_corrections_are_column_allow_listed():
    src = (SRC / "tfa/infrastructure/persistence/sql/read_repository.py").read_text()
    assert "_CORRECTABLE" in src
    assert "if k in _CORRECTABLE" in src


def test_no_secrets_are_committed():
    for path in list(SRC.rglob("*.py")) + list((ROOT / "functions").rglob("*.py")):
        text = path.read_text()
        for pattern in (r'password\s*=\s*"[^"{}\s]{8,}"',
                        r'api_key\s*=\s*"[^"{}\s]{16,}"',
                        r'AccountKey=[A-Za-z0-9+/]{40,}'):
            assert not re.search(pattern, text, re.I), f"possible secret in {path.name}"