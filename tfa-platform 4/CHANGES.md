# What changed in this drop

Your repository is the base. Nothing you had was removed — `.vscode/`,
`SETUP.md`, `WALKTHROUGH.md`, `README.md`, your pipelines and `.env.template`
are untouched.

---

## 1. The bug — `src/tfa_core/storage/repositories.py`

This is the only change that alters behaviour you already had. Everything else
is additive.

**Before:**

```python
def download(self, container, blob_path) -> tuple[bytes, str]:
    """Returns (content, md5-or-etag) for idempotency keys."""
    md5 = (props.content_settings.content_md5 or b"").hex() or props.etag.strip('"')
```

The Azure SDK only populates `content_md5` when the uploader supplies it, and
ours does not — so **the ETag branch was the only branch ever taken**. An ETag
changes on every write, including an overwrite with byte-identical content.

`document_id` is the idempotency key for a delete-then-insert. A changing key
means re-processing inserted an **additional** row instead of replacing one.
Three reprocesses produced three rows and three times the reported overcharge
— against a hotel, in a recovery claim.

**After:** `download()` returns bytes only. Identity comes from
`tfa_core.domain.identity.content_hash(content)`. The two-value return is gone so the
footgun cannot be reloaded. `version()` is available separately for cache
invalidation, never for identity.

Callers updated: `extraction/pipeline.py`, `functions/function_app.py`.

Your README describes this as fixed; the code did not match. Pinned now by
`tests/test_incident_2025_08.py`.

---

## 2. Schema migrations — you had none

`db/migrations/001_initial_schema.sql`, `002_incident_2025_08_dedupe.sql`

`scripts/generate_sql_ddl.py` prints DDL from the column registry, which keeps
code and schema aligned but leaves no history — no record of what was applied,
no way to review a change, no rollback point.

`002` adds `UQ_documents_identity (identifier_name, file_name)`. With the
constraint in place, a duplicate insert **fails loudly** rather than silently
inflating a total. Read its header before running; it deletes rows.

---

## 3. New modules

| | |
|---|---|
| `identity.py` | `content_hash`, `compute_document_id` — one definition, used everywhere |
| `validation_rules.py` | SEC-01. A batch identifier reaches a storage path; `../../rates` would write outside its container, letting a reviewer overwrite another team's batch |
| `ports.py` | protocols the adapters satisfy — makes collaborators substitutable in tests |
| `errors.py`, `values.py` | terminal vs retryable errors; `DocumentLayout` |
| `db.py` | connection pool + cached Entra token (50 queries → 1 AAD call) |
| `resilience.py` | retry policy, token bucket, cached provider |
| `mapping/columns.py`, `mapping/folio_rows.py` | domain → SQL rows, pure and testable |
| `storage/folio_store.py` | SQL write side against the normalized schema |
| `extraction/prompting.py` | prompt + `PROMPT_VERSION`, recorded per document so a historical figure stays explainable |
| `extraction/analyzer.py`, `csv_reader.py` | type routing; CSV with delimiter and encoding auto-detection — LATAM exports are usually semicolon-delimited cp1252 |
| `ingestion/sharepoint_locator.py` | paste a library URL; `Argentina/2025` → `argentina_2025`, every folder level contributing |

---

## 4. Tests: 36 → 72

| | |
|---|---|
| `test_incident_2025_08.py` | the duplicate-row regression |
| `test_security.py` | path injection, upload validation |
| `test_folio_store_mapping.py` | domain → SQL mapping |

```bash
pip install -r requirements.txt
PYTHONPATH=src pytest tests -q
```

---

## 5. Operator scripts

| | |
|---|---|
| `scripts/process_one.py` | **one real folio, every stage printed.** Self-contained — no composition root |
| `scripts/preflight.py` | verifies config, credentials, Blob, SQL schema, seed data, DocIntel, the LLM deployment |
| `scripts/resolve_sharepoint.py` | URL → Graph ids, previews the folder-to-batch mapping before ingestion |
| `scripts/estimate_cost.py` | cost model from the code paths |

`scripts/ingest.py` was **not** included — it depends on a batch use case that
only exists in the layered refactor.

---

## 6. Reference data and config

`seed/negotiated_rates.json`, `seed/fx.json` — the files the pipeline reads.
Without them extraction runs and every overcharge column stays empty, which
reads as a bug rather than missing configuration.

Also: `infra/main.{test,prod}.bicepparam`, `functions/requirements.txt`,
`functions/.funcignore`, `evals/gate.py`.

---

## What was deliberately left out

**`src/tfa_api/`** — a FastAPI read tier. Your README retires the custom UI in
favour of Power Apps per the approved diagram, so adding an API tier would
diverge from it. If Power Apps is later rejected, it exists in the layered
build.

**Caching, demo and local modes** — useful in isolation, not worth the surface
area here.

**Your Event Grid module is correct.** The subscription is present; I only
added two `output` lines.

---

## Order of work

1. **`pytest tests -q`** — confirms the drop is intact
2. **Run `db/migrations/002`** on a copy first; check `vw_folio_report` after
3. **`python scripts/preflight.py`** — tells you exactly what is not configured
4. **`python scripts/process_one.py <folio.pdf> --dry-run`** — the first real
   document through the real pipeline
5. **Label 20 goldens, run `python -m evals.run_eval`**

Step 5 is the one that still decides whether any of this is accurate enough to
deliver. Until `money_exact` has documents to score, accuracy is a claim.


---

## Added for demo and delivery

| | |
|---|---|
| `requirements-dev.txt` | `pytest` was absent — 78 tests could not be run from a clean checkout |
| `STRUCTURE.md` | the three-layer rule and why it is enforced by tests |
| `tests/test_structure.py` | fails the build when a dependency points the wrong way |
