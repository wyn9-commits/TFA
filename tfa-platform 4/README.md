# Travel Folio Analysis Platform — v2 Rebuild

Ground-up rebuild of `app-hds-ds-travel-folio-analysis`, designed for **extraction
accuracy first**, aligned to the End-State High Level Deployment Architecture
(Power Apps UI → Blob → Orchestration Logic (Azure Functions) → Azure AI Foundry
→ SQL DB, with Dataverse/Cosmos metadata, Managed Identity, Log Analytics /
App Insights, Splunk SIEM).

## Document types

The library holds more than hotel folios — e.g. Colombian DIAN e-invoices from
travel agencies for air tickets (validated against a real sample: fare + IVA
19% + tasa aeroportuaria + admin fee + its IVA = printed total). The model
classifies every document (`hotel_folio` | `air_ticket_invoice` |
`agency_fee_invoice` | `other_travel_invoice`) before extraction:

- Hotel folios: room nights → `nightly_charges`, per-night rate audit runs.
- Air/agency invoices: all lines → `ancillary_charges` with categories
  (`airfare`, `airport_tax`, `admin_fee`, `tax`); route, passenger, both NIT
  tax IDs, and the CUFE are captured; rate audit is skipped.
- When a total is also written in words ("SON: … PESOS"), the model converts
  it and a validator cross-checks it against the numeric total — mismatch
  triggers the repair loop.

Validated against real samples from four countries:
- **Colombia** — DIAN Factura Electrónica (air ticket, IVA 19%, CUFE).
- **Argentina** — Factura B with a *collapsed* 3-night line ("Habitación
  single durante tres noches", `nights=3` on one charge) and *included* IVA
  ("IVA Contenido", Ley 27.743) recorded in `informational_tax_amount`, never
  as an additive line.
- **Ecuador/US** — Wings TAX INVOICE in USD with English number format
  (1,056.00) and multi-leg routing.
- **Mexico** — Serrala/FS² internal AP cover pages classified as
  `ap_system_document` (G/L lines + additive tax; approval-history tables
  ignored).
The per-night rate audit expands collapsed lines (line total ÷ nights) so
negotiated-rate comparisons stay per-night.

**Unknown-sample strategy.** New document shapes will keep appearing, so the
design principle is: a never-seen-before document must degrade to *human
review*, never to silent bad data or a retry crash-loop. Escape hatches:
`credit_note` (negative amounts, links to the reversed invoice via
`references_invoice_number`; positive-total credit notes are flagged);
`other_travel_invoice` (generic-but-correct extraction: vendor, dates, tax
IDs, every amount line, the total — the prompt explicitly forbids forcing a
wrong specific type); `not_an_invoice` (boarding passes, email printouts,
blank scans — routed to review with a one-line `unreadable_reason`, no
arithmetic checks run); `contains_multiple_documents` (several invoices in
one scan: first one extracted, file flagged for splitting, amounts never
merged); symbol-only currencies inferred from country and confidence-flagged.
Excel workbooks (`.xlsx/.xlsm/.xls`) are rendered sheet-by-sheet to markdown
and flow through the same extraction, validators, and repair loop.

## Ingestion: SharePoint → Blob

All folios live in a SharePoint document library. A timer-triggered Function
(every 5 min, `TFA_SHAREPOINT_SYNC_CRON`) runs a **Microsoft Graph delta
query** and mirrors new/changed PDFs (+ PNG/JPG/XLSX) into the raw Blob
container — Blob stays the immutable processing source, and the rest of the
pipeline is unchanged (blob upload → Event Grid → queue → extraction).

- **Auth:** the Function App's Managed Identity gets the Graph `Sites.Selected`
  application permission, then read access to only the TFA site — least
  privilege, no Sites.Read.All, no secrets. Governance-review friendly.
- **Layout:** `<library root>/<identifier>/<file.pdf>`; the first-level folder
  name is the upload identifier. The SharePoint uploader (`createdBy`) is
  recorded at sync time and becomes `uploading_person_name` — the legacy
  `-<user>` folder-suffix convention is retired (still parsed during
  migration).
- **Incremental + idempotent:** the delta link and per-item eTags persist in
  Cosmos, so runs only touch changes; re-uploads overwrite the blob, which
  changes the content hash, which re-upserts the SQL rows.
- **Deletions:** SharePoint deletions are *flagged, never propagated* — the raw
  container is immutable per the architecture; operators decide retention
  (relevant under the Restricted data classification).

Config: `TFA_SHAREPOINT_SITE_ID`, `TFA_SHAREPOINT_DRIVE_ID`,
`TFA_SHAREPOINT_ENABLED`.

## Why the POC failed → what v2 does instead

| POC failure | v2 design |
|---|---|
| Only **page 1** of each PDF, rendered at **150 DPI**, sent to the LLM | **All pages** analysed by Azure Document Intelligence (`prebuilt-layout`, markdown + tables) **and** rendered at 300 DPI; the LLM gets exact OCR text anchored by images |
| Single LLM call, "no validation or retries" (its own docstring) | Strict **structured outputs** (JSON-schema-constrained), Pydantic type validation, **cross-field consistency validators** (line items must sum to printed total, checkout > checkin, night count vs stay length), and a **repair loop** that feeds validator errors back to the model (max 3 attempts) |
| Hallucinated / silently wrong values landed in SQL | Anti-hallucination contract: absent → `null`, uncertain → listed in `low_confidence_fields`; uncertain money/date fields route the document to **`needs_review`** instead of auto-approval |
| LLM implicitly trusted for derived numbers | All arithmetic (FX conversion, over/undercharge per night, quarters) is **deterministic Python**, unit-tested |
| APScheduler inside Flask + a shared `track_folios_file.txt` (read-modify-write races, one bad PDF stalls the batch) | **Event Grid → Storage Queue → Azure Functions**; per-document Cosmos status items; platform-managed retries and a **poison queue** for triage |
| `SQL_USERNAME`/`SQL_PASSWORD` auth | **Entra ID access tokens** via Managed Identity for SQL, Blob, Cosmos, Document Intelligence, and Foundry — zero secrets |
| Three drifting copies of the column schema (app.py / script.js / SQL) | **One registry** (`schemas.FOLIO_COLUMNS`) generates the SQL DDL, the Excel headers, and the Excel data-dictionary sheet |
| Duplicate rows on reprocessing | Deterministic `document_id` (identifier + filename + content hash); SQL write is transactional delete-then-insert on that key → **idempotent** under at-least-once queue delivery |

## Model strategy (Azure AI Foundry, August 2026)

- **Stage 1 — layout:** Azure Document Intelligence `prebuilt-layout`
  (markdown output). Deterministic, per-page word confidences, strong on tables.
- **Stage 2 — semantic extraction:** configurable via `TFA_LLM_PROVIDER`:
  - `azure_openai` → **GPT-5.6** deployment, native `json_schema` strict mode
  - `anthropic_foundry` → **Claude Sonnet 5 / Opus 5** (Azure-hosted
    deployments), forced tool-use for the same schema guarantee
  Both authenticate with the workload's Managed Identity. Swap models by
  changing two env vars; pipeline code is provider-agnostic.
- For Restricted-classified data, prefer the **Azure-hosted** Claude
  deployments (data stays on Azure end-to-end) — confirm in the governance
  review.

## Repository layout

```
tfa-platform/
├── src/tfa_core/
│   ├── schemas.py                  # SINGLE SOURCE OF TRUTH: models + column registry
│   ├── config.py                   # pydantic-settings; Managed Identity credential
│   ├── llm/provider.py             # GPT-5.6 & Claude-on-Foundry structured output
│   ├── extraction/
│   │   ├── document_intelligence.py# Stage 1: layout (all pages) + 300-DPI renders
│   │   ├── llm_extractor.py        # Stage 2: extraction + repair loop
│   │   ├── validators.py           # consistency gates (fatal / needs_review)
│   │   ├── reconciliation.py       # rate match, FX, over/undercharge — pure Python
│   │   └── pipeline.py             # end-to-end, idempotent
│   └── storage/repositories.py     # SQL (Entra token), Cosmos status, Blob
├── functions/
│   ├── function_app.py             # Event Grid → queue → process → poison handling
│   └── host.json                   # maxDequeueCount=5, batchSize=4, 10-min timeout
├── tests/                          # validators + reconciliation (11 tests, all green)
├── requirements.txt
└── pyproject.toml
```

## Enterprise infrastructure & delivery

```
infra/                     Bicep — everything private-endpoint-only
├── main.bicep             wires: identity, network, storage, data, ai,
│                          functions, rbac, event grid; CMK toggle for prod
├── main.dev.bicepparam
└── modules/               network (VNet + 6 private DNS zones), monitoring
                           (LAW + workspace App Insights), storage (ZRS,
                           Entra-only, versioned/immutable raw container),
                           data (SQL Entra-only-auth + Cosmos local-auth-off),
                           ai (DocIntel + Foundry, disableLocalAuth, LLM
                           deployment), functions (Flex Consumption, UAMI,
                           VNet-integrated), rbac (least-privilege data-plane
                           roles), events (BlobCreated → enqueue_folio)
pipelines/
├── infra-pipeline.yml     PR: bicep lint + what-if · main: deploy behind
│                          ADO environment approval gates
└── app-pipeline.yml       tests → package → Flex Consumption deploy
scripts/generate_sql_ddl.py  migration SQL from the column registry
                           (+ vw_needs_review + UAMI role grants)
docs/
├── DEPLOYMENT.md          ordered runbook incl. Graph Sites.Selected setup,
│                          Splunk path, day-2 operations table
└── SECURITY_CONTROLS.md   requirement-by-requirement mapping from
                           RITM0607876 / Restricted controls to code+infra —
                           the evidence pack for the enterprise governance
                           review
```

Security posture in one line: **no public data plane, no keys, no passwords**
— every service is private-endpoint-only with Entra-only authentication
(SQL `azureADOnlyAuthentication`, Storage `allowSharedKeyAccess=false`,
Cosmos/DocIntel/Foundry `disableLocalAuth`), one least-privilege UAMI, CMK
toggle for prod.

## Configuration (env vars, `TFA_` prefix)

Required: `TFA_STORAGE_ACCOUNT_URL`, `TFA_QUEUE_ACCOUNT_URL`,
`TFA_DOCINTEL_ENDPOINT`, `TFA_LLM_ENDPOINT`, `TFA_SQL_SERVER`,
`TFA_SQL_DATABASE`, `TFA_COSMOS_ENDPOINT`.
Key optionals: `TFA_LLM_PROVIDER` (`azure_openai` | `anthropic_foundry`),
`TFA_LLM_DEPLOYMENT` (default `gpt-5.6`), `TFA_USER_ASSIGNED_CLIENT_ID`.

`FolioSqlRepository(...).ddl()` prints the CREATE TABLE + indexes — run it once
per environment so DB schema always matches the code registry.

## What is intentionally NOT in this drop

- **Power Apps UI** — per the end-state diagram the custom Flask UI is retired;
  Power Apps talks to Blob/Dataverse directly and reads status from Cosmos.
- **SAP negotiated-rate feed** — `RateContext` is the seam; DEV seeds from
  `rates/negotiated_rates.json` + `rates/fx.json` blobs, PROD swaps in the
  SAP-fed SQL tables without touching extraction code.
- **Bicep/IaC + pipelines** — next deliverable: Function App (Flex Consumption),
  private endpoints for Blob/SQL/Cosmos/Foundry, Event Grid subscription,
  diagnostic settings → Log Analytics → Splunk forwarder, and the CI/CD
  pipelines (infra + app tracks per your Azure Artifacts setup).

## Evaluation (LangSmith) & graph engine (LangGraph)

`evals/` is the model-bake-off harness. Label goldens as
`evals/golden/<name>.{pdf|xlsx|png}` + `<name>.expected.json` (a Colombia
example is included), push once with `python -m evals.run_eval push
--dataset tfa-goldens-v1`, then run one experiment per configuration
(provider × deployment × engine) and compare in the LangSmith UI on five
metrics defined in `evals/evaluators.py`:
`money_exact` (every cent exact — the metric that failed the POC),
`sum_check`, `doc_type_correct`, `field_accuracy`, and `review_routing`
(missing a needed review flag scores 0; over-flagging scores 0.5 — a
confident wrong answer is worse than a flagged one). Ship whichever config
wins `money_exact`; break ties on `review_routing`.

`TFA_EXTRACTION_ENGINE=graph` swaps the extract→validate→repair loop for a
LangGraph StateGraph (`src/tfa_core/graph/`) — functionally identical (same
prompt, schema, validators; proven by a scripted-LLM repair test), but every
attempt, verdict, and repair shows as a distinct traced node in LangSmith.

**Governance:** tracing is opt-in (`TFA_LANGSMITH_ENABLED=true`) and
redacting by default (`src/tfa_core/observability/tracing.py` — page images
never traced, long text hashed). Goldens contain Restricted data: point
`LANGSMITH_ENDPOINT` at a self-hosted LangSmith inside the VNet, or get
governance sign-off before any cloud endpoint.

## Running the tests

```bash
pip install -r requirements.txt
PYTHONPATH=src pytest tests/ -q     # 11 passed
```
