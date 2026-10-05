# Deployment Guide

## Order of operations (per environment)

1. **Prereqs (one-time, per env)**
   - Resource group exists (e.g. `asg-tfa-dev-scus-rg`).
   - Entra group for SQL admins exists; put its object ID in
     `infra/main.<env>.bicepparam`.
   - For prod: Key Vault + key for CMK; set `enableCmk` + `cmkKeyVaultKeyUri`.

2. **Infra pipeline** (`pipelines/infra-pipeline.yml`)
   - PR → bicep lint + `what-if`; merge to main → deploy behind the ADO
     environment approval gate.
   - Outputs: Function App name, UAMI client/principal IDs, SQL FQDN.

3. **SQL schema** (as a member of the SQL Entra admin group, via private
   endpoint / jump host):
   ```bash
   PYTHONPATH=src python scripts/generate_sql_ddl.py id-tfa-<env>-<region> > migration.sql
   # review, then execute migration.sql against the tfa database
   ```
   The DDL is generated from `schemas.FOLIO_COLUMNS`, so code and DB cannot
   drift silently. Re-run after any registry change (idempotence: wrap in
   IF NOT EXISTS or use your migration tool of choice).

4. **Graph permission for SharePoint** (one-time, Global/App admin):
   ```bash
   # a) Grant the UAMI the Sites.Selected application permission
   az ad app permission ... (or Graph API: appRoleAssignments on the UAMI SP,
   appRoleId for Sites.Selected on the Microsoft Graph SP)
   # b) Grant that permission access to ONLY the TFA site:
   POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions
   { "roles": ["read"],
     "grantedToIdentities": [ { "application": {
         "id": "<uami-client-id>", "displayName": "tfa-platform" } } ] }
   ```
   Then set `TFA_SHAREPOINT_SITE_ID` / `TFA_SHAREPOINT_DRIVE_ID` app settings.
   Find them: `GET /sites/{hostname}:/{site-path}` → id;
   `GET /sites/{site-id}/drives` → the document library's drive id.

5. **App pipeline** (`pipelines/app-pipeline.yml`)
   - Tests (36) → package (function_app + tfa_core + requirements, remote
     Oryx build) → deploy to Flex Consumption behind the same approval gate.

6. **Rates seed (DEV only)** — upload `rates/negotiated_rates.json` and
   `rates/fx.json` to the raw container. PROD replaces this with the SAP-fed
   tables behind `RateContext`.

7. **Smoke test** — drop a sample PDF into the SharePoint library; within
   ~5 min the sync ingests it, Event Grid fires, and either a SQL row
   appears or a `needs_review`/`failed` status shows in Cosmos with a reason.

## Observability

- App traces/metrics: Application Insights (workspace-based) → Log Analytics.
- Resource logs: diagnostic settings → same workspace.
- **Splunk**: connect the workspace to the org's existing LAW → Event Hub →
  Splunk ingestion path (Splunk Add-on for Microsoft Cloud Services). No new
  ingestion pattern is introduced by this platform.
- Optional LLM-step tracing: self-hosted LangSmith in the VNet;
  `TFA_LANGSMITH_ENABLED=true` + `LANGSMITH_ENDPOINT`. Redaction is on by
  default (`observability/tracing.py`).

## Operational runbook (day 2)

| Symptom | Where to look | Action |
|---|---|---|
| Document stuck "queued" | Cosmos `processing_status` item; poison queue depth | Check `handle_poison` logs; message in `folio-extract-poison` carries the payload |
| `needs_review` growing | `vw_needs_review` in SQL | Weekly triage; each correction is either a prompt/schema gap (fix in code + add a golden) or genuinely ambiguous (correctly routed) |
| Extraction accuracy question | LangSmith experiments | Re-run `evals/run_eval.py` against the golden set; compare `money_exact` |
| Model deprecation notice | Foundry model lifecycle page | Change `TFA_LLM_DEPLOYMENT` (+ Bicep `llmDeploymentName`), re-run evals before promoting |
| SharePoint sync silent | `sync_sharepoint` timer logs; `delta-link` item in Cosmos | Delete the `delta-link` item to force a full re-enumeration (idempotent — eTags prevent re-ingest) |
