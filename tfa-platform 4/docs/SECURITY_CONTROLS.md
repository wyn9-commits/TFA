# Security Control Mapping — Enterprise Governance Review

Maps each requirement from the POC security review (RITM0607876) and the
Restricted data classification to its implementation in this codebase and
infrastructure. Intended as the primary technical evidence pack for the
**separate enterprise governance review** (the POC approval was explicitly
scoped and time-bound and does not carry over).

| # | Requirement (from POC review / Restricted controls) | Enterprise implementation | Where |
|---|---|---|---|
| 1 | POC was internet-facing via DMZ; enterprise must not be | **No public data plane.** Every PaaS service (`publicNetworkAccess: Disabled`, `networkAcls: Deny`) is reachable only through private endpoints in the platform VNet; the Function App is VNet-integrated. The Power Apps UI reaches data via Dataverse/connector paths, not a public web app. | `infra/modules/*.bicep` |
| 2 | Users authenticated via Azure AD | Entra ID everywhere, **and no other auth exists**: SQL is `azureADOnlyAuthentication: true` (no SQL logins), Storage is `allowSharedKeyAccess: false`, Cosmos is `disableLocalAuth: true`, Document Intelligence and Foundry are `disableLocalAuth: true`. Workload identity is a single UAMI with least-privilege data-plane roles. | `infra/modules/data.bicep`, `storage.bicep`, `ai.bicep`, `rbac.bicep` |
| 3 | Data encrypted at rest and in transit | TLS 1.2 minimum enforced on every service; platform-managed encryption at rest by default; **customer-managed keys** available via `enableCmk` + Key Vault key URI (enable for prod per Restricted controls). | `infra/main.bicep` (`enableCmk`), `storage.bicep` |
| 4 | Data classification: Restricted — all applicable controls | Immutable raw container (versioning + 30-day delete retention; SharePoint deletions are flagged, never propagated), audit logging on SQL, diagnostic settings to Log Analytics, redaction-by-default tracing, review-gated persistence of uncertain extractions. | `storage.bicep`, `data.bicep`, `tracing.py`, `validators.py` |
| 5 | Third party involved; SOC 2 Type II must be collected | Third parties in the enterprise design: LangSmith (only if used; self-hosted-in-VNet is the default recommendation, which removes the data flow entirely) and the model providers via Microsoft Foundry (covered by Microsoft's compliance inheritance; for Claude, use the **Azure-hosted** deployments so data stays on Azure end-to-end). Collect: Anthropic SOC 2 Type II if Anthropic-hosted Claude is ever enabled; LangChain Inc. SOC 2 Type II if LangSmith Cloud is ever enabled. | `docs/DEPLOYMENT.md`, `observability/tracing.py` |
| 6 | Application uses API calls to fetch required data | All API calls are Entra-token-authenticated over private endpoints: Microsoft Graph (`Sites.Selected`, single-site grant), Document Intelligence, Foundry, SQL, Cosmos, Storage. No API keys exist anywhere in code or configuration. | `ingestion/sharepoint.py`, `llm/provider.py`, `storage/repositories.py` |
| 7 | Solution involves changes/developments in SAP | Isolated behind the `RateContext` seam: the SAP negotiated-rate feed replaces the JSON seed without touching extraction code. SAP integration itself ships as its own reviewed change. | `extraction/pipeline.py` (`RateContext`) |
| 8 | Data types: Excel, PDF, PNG, JPEG | All four routed: PDF/PNG/JPEG through Document Intelligence + vision LLM; Excel through the workbook reader. Unsupported types fail closed with a status record. | `extraction/pipeline.py`, `excel_reader.py` |
| 9 | Data storage: folio fields (hotel, dates, guest, charges…) | Single-registry schema (`FOLIO_COLUMNS`) generates the SQL DDL, so the stored fields are exactly the reviewed fields; guest names are the only personal data stored, minimised to what reconciliation requires. | `schemas.py`, `scripts/generate_sql_ddl.py` |
| 10 | SIEM: Splunk | All resources emit diagnostic settings to Log Analytics; ingestion to Splunk uses the organisation's existing LAW → Event Hub → Splunk add-on path (no new pattern introduced). | `monitoring.bicep`, `docs/DEPLOYMENT.md` |
| 11 | AI/prompt governance (new for enterprise) | Prompts are version-controlled code; extraction is schema-constrained (no free-form generation); model output never does arithmetic; uncertain output is human-review-gated; eval harness (`evals/`) provides measurable accuracy evidence per model/config; model swaps are config-only and auditable. | `llm_extractor.py`, `evals/` |

## Residual items for the review to decide
1. **CMK scope** — storage CMK is wired; extending CMK to SQL (TDE with BYOK)
   and Cosmos needs a decision on key-rotation ownership.
2. **LangSmith hosting** — self-hosted in VNet (recommended) vs. cloud with
   SOC 2 evidence + DPA.
3. **Serrala/FS² AP documents** — process, skip, or use as cross-check
   against SAP-posted amounts (they are classified before extraction, so any
   of the three is a config choice).
4. **Retention** — raw-container retention for Restricted data beyond the
   30-day soft-delete window.
