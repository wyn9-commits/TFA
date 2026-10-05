#!/usr/bin/env python3
"""Preflight — verify the real (non-demo) system is ready.

Checks configuration, credentials, connectivity, schema and reference data,
then says exactly what is missing and how to fix it. Run it after deploying and
before every demo.

    python scripts/preflight.py

Exit code 0 = ready. 1 = something is missing (details printed).

Deliberately independent of the application's own startup: a check that shares
code with the thing it validates fails for the same reasons and tells you
nothing new.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

results: list[tuple[str, str, str]] = []   # (status, label, detail)


def ok(label: str, detail: str = "") -> None:
    results.append(("ok", label, detail))


def fail(label: str, detail: str) -> None:
    results.append(("fail", label, detail))


def warn(label: str, detail: str) -> None:
    results.append(("warn", label, detail))


def load_env() -> None:
    """Read .env without adding a dependency on python-dotenv."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        fail(".env present", "Run scripts/configure.sh, or copy .env.template")
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())
    ok(".env present")


REQUIRED = [
    ("TFA_STORAGE_ACCOUNT_URL", "blob endpoint"),
    ("TFA_QUEUE_ACCOUNT_URL", "queue endpoint"),
    ("TFA_DOCINTEL_ENDPOINT", "Document Intelligence"),
    ("TFA_LLM_ENDPOINT", "Foundry / Azure OpenAI"),
    ("TFA_SQL_SERVER", "SQL server FQDN"),
    ("TFA_SQL_DATABASE", "SQL database"),
    ("TFA_COSMOS_ENDPOINT", "Cosmos endpoint"),
]

API_ONLY = [("TFA_TENANT_ID", "tenant id"), ("TFA_API_AUDIENCE", "API audience")]


def check_config() -> None:
    for key, what in REQUIRED:
        value = os.environ.get(key, "")
        if value:
            ok(f"{key}", value[:56])
        else:
            fail(f"{key}", f"missing — {what}")

    for key, what in API_ONLY:
        if os.environ.get(key):
            ok(key)
        else:
            warn(key, f"missing — the API will not start ({what}); workers are unaffected")

    audience = os.environ.get("TFA_API_AUDIENCE", "")
    if audience and not audience.startswith("api://"):
        fail("TFA_API_AUDIENCE format",
             "must be api://<client-id>. A bare client id lets an ID token "
             "validate as an access token (SEC-06).")

    redis = os.environ.get("TFA_REDIS_URL", "")
    env = os.environ.get("TFA_ENVIRONMENT", "dev")
    if redis and env != "dev" and not redis.startswith("rediss://"):
        fail("TFA_REDIS_URL", "must be rediss:// outside dev — the app refuses plaintext")
    elif redis:
        ok("TFA_REDIS_URL", "TLS" if redis.startswith("rediss://") else "dev plaintext")
    else:
        warn("TFA_REDIS_URL", "not set — L1-only cache (correct for a single replica)")

    for key in ("TFA_SHAREPOINT_SITE_ID", "TFA_SHAREPOINT_DRIVE_ID"):
        if os.environ.get(key):
            ok(key)
        else:
            warn(key, "missing — SharePoint sync disabled; direct upload still works")


def check_credential():
    try:
        from azure.identity import DefaultAzureCredential
        cred = DefaultAzureCredential()
        cred.get_token("https://management.azure.com/.default")
        ok("Azure credential", "DefaultAzureCredential resolved")
        return cred
    except Exception as exc:
        fail("Azure credential",
             f"{type(exc).__name__}: run `az login` (or check the managed identity). {exc}"[:150])
        return None


def check_blob(cred) -> None:
    url = os.environ.get("TFA_STORAGE_ACCOUNT_URL")
    if not (cred and url):
        return
    try:
        from azure.storage.blob import BlobServiceClient
        svc = BlobServiceClient(account_url=url, credential=cred)
        container = svc.get_container_client("travel-folios")
        container.get_container_properties()
        ok("Blob container", "travel-folios reachable")

        names = {b.name for b in container.list_blobs(name_starts_with="rates/")}
        for required in ("rates/negotiated_rates.json", "rates/fx.json"):
            if required in names:
                ok(f"Seed data {required}")
            else:
                fail(f"Seed data {required}",
                     "missing — extraction runs but every overcharge column stays empty. "
                     "See seed/README.md")
    except Exception as exc:
        fail("Blob container",
             f"{type(exc).__name__}: {exc}"[:150] +
             "  (private endpoint? you must be on the VNet or VPN)")


def check_sql(cred) -> None:
    server = os.environ.get("TFA_SQL_SERVER")
    database = os.environ.get("TFA_SQL_DATABASE")
    if not (cred and server and database):
        return
    try:
        import struct

        import pyodbc
        token = cred.get_token("https://database.windows.net/.default").token.encode("utf-16-le")
        packed = struct.pack(f"<I{len(token)}s", len(token), token)
        conn = pyodbc.connect(
            "Driver={ODBC Driver 18 for SQL Server};"
            f"Server=tcp:{server},1433;Database={database};Encrypt=yes;Connection Timeout=15;",
            attrs_before={1256: packed})
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM sys.tables WHERE name IN "
                    "('documents','folio_lines','negotiated_rates','fx_rates','audit_log')")
        found = cur.fetchone()[0]
        if found == 5:
            cur.execute("SELECT COUNT(*) FROM dbo.documents")
            ok("SQL schema", f"5 tables present, {cur.fetchone()[0]} documents")
        else:
            fail("SQL schema",
                 f"only {found}/5 tables — run db/migrations/001_initial_schema.sql")
        conn.close()
    except ImportError:
        warn("SQL", "pyodbc not installed — install the ODBC Driver 18 for SQL Server")
    except Exception as exc:
        fail("SQL connection", f"{type(exc).__name__}: {exc}"[:160])


def check_docintel(cred) -> None:
    endpoint = os.environ.get("TFA_DOCINTEL_ENDPOINT")
    if not (cred and endpoint):
        return
    try:
        import httpx
        token = cred.get_token("https://cognitiveservices.azure.com/.default").token
        r = httpx.get(f"{endpoint.rstrip('/')}/documentintelligence/info",
                      params={"api-version": "2024-11-30"},
                      headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if r.status_code == 200:
            ok("Document Intelligence", "reachable and authorised")
        elif r.status_code in (401, 403):
            fail("Document Intelligence",
                 f"HTTP {r.status_code} — identity lacks the Cognitive Services User role")
        else:
            warn("Document Intelligence", f"HTTP {r.status_code}")
    except Exception as exc:
        fail("Document Intelligence", f"{type(exc).__name__}: {exc}"[:140])


def check_llm(cred) -> None:
    endpoint = os.environ.get("TFA_LLM_ENDPOINT")
    deployment = os.environ.get("TFA_LLM_DEPLOYMENT", "")
    if not (cred and endpoint and deployment):
        return
    try:
        import httpx
        token = cred.get_token("https://cognitiveservices.azure.com/.default").token
        r = httpx.post(
            f"{endpoint.rstrip('/')}/openai/deployments/{deployment}/chat/completions",
            params={"api-version": os.environ.get("TFA_LLM_API_VERSION", "2025-04-01-preview")},
            headers={"Authorization": f"Bearer {token}"},
            json={"messages": [{"role": "user", "content": "ping"}], "max_completion_tokens": 5},
            timeout=30)
        if r.status_code == 200:
            ok("LLM deployment", f"'{deployment}' responded")
        elif r.status_code == 404:
            fail("LLM deployment",
                 f"'{deployment}' not found — TFA_LLM_DEPLOYMENT must be your "
                 "DEPLOYMENT name, not the model name")
        elif r.status_code in (401, 403):
            fail("LLM deployment", f"HTTP {r.status_code} — missing Cognitive Services User role")
        else:
            warn("LLM deployment", f"HTTP {r.status_code}: {r.text[:90]}")
    except Exception as exc:
        fail("LLM deployment", f"{type(exc).__name__}: {exc}"[:140])


def main() -> int:
    print("\n" + "=" * 68)
    print("  TFA PREFLIGHT — is the real system ready?")
    print("=" * 68)

    load_env()
    check_config()
    cred = check_credential()
    check_blob(cred)
    check_sql(cred)
    check_docintel(cred)
    check_llm(cred)

    print()
    for status, label, detail in results:
        mark = {"ok": f"{GREEN}  OK  {RESET}", "fail": f"{RED} FAIL {RESET}",
                "warn": f"{YELLOW} WARN {RESET}"}[status]
        print(f"{mark} {label:<34} {DIM}{detail}{RESET}")

    failures = [r for r in results if r[0] == "fail"]
    warnings = [r for r in results if r[0] == "warn"]
    print("\n" + "-" * 68)
    if failures:
        print(f"{RED}NOT READY{RESET} — {len(failures)} blocking issue(s), "
              f"{len(warnings)} warning(s).")
        print("\nFix these first:")
        for _, label, detail in failures:
            print(f"  · {label}: {detail}")
        print("\nSee CONFIGURATION.md for each value.")
        return 1
    print(f"{GREEN}READY{RESET} — {len(warnings)} warning(s), nothing blocking.")
    print("\nNext:  python scripts/process_one.py <path-to-a-real-folio.pdf>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
