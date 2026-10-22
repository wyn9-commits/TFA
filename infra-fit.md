# Infrastructure: what the app uses from the infra Terraform

The infrastructure lives in the infra repo, `infra-hds-ds-travel-folio-analysis`,
and its Azure DevOps pipeline. **This repo creates no Azure resources.** There is
no Bicep and no Terraform here. Everything below was checked against that
repo's tfvars and templates, and the app is built to run on exactly what the
infra repo already provides.

If something below has to change, for example a second model deployment,
change it through the infra repo's normal process: a tfvars change, a pull
request and the infra pipeline. Never change it by hand in the portal.

## 1. Resources the app uses (all already deployed)

| Resource (from tfvars) | Used for | App setting |
|---|---|---|
| App Service (Linux, P0v3), container from ACR `hdsdscmndevscuscr.azurecr.io`, image `travel-folio-analysis-app:latest-dev` | runs the web app, the processing loop and SharePoint sync (one gunicorn worker) | — |
| Storage account, container `travel-folios`, shared keys **off** | uploaded and SharePoint-synced folios | `AZURE_STORAGE_ACCOUNT_URL` |
| Azure OpenAI, custom subdomain `travel-res`, deployment **gpt-4o** (version 2024-11-20) | extraction: PDF text plus page images, strict JSON schema | `AZURE_OPENAI_STARTBURST_END_POINT`, `_MODEL_NAME`, `_API_VERSION`, optional `_API_KEY` |
| Azure SQL (GP_S_Gen5_4), admin `SQLAdmin` | folio lines (`tfa_folio_lines`), processing status (`tfa_processing_status`), SharePoint sync state (`tfa_kv`) | `SQL_SERVER`, `SQL_DATABASE`, `SQL_USERNAME`, `SQL_PASSWORD` |
| Key Vault: `mssql_server_username`, `mssql_server_password`, `app_insights_connection_string` | SQL login and telemetry | Key Vault references in app settings |
| User-assigned managed identity | blob access, and the model if no key is set | `USER_ASSIGNED_CLIENT_ID` |
| App Service Authentication (`auth_settings`: issuer, client ID, allowed audiences) | sign-in before a request reaches the app | `WEBSITE_AUTH_ENABLED` (set by App Service), `ALLOWED_EMAILS` |
| Front Door with WAF | public entry point | — |
| Application Insights web test (expects HTTP 200) | availability | the app's `/health` returns 200 |

### What the infra does not have, and what the app does instead

| Not in the infra | What the app does instead |
|---|---|
| Document Intelligence (no `FormRecognizer` kind in the template) | `extraction/pdf_layout.py` reads the PDF's own text layer on every page, and sends 200-dpi images of up to 8 pages to gpt-4o. Most of the invoices are born-digital e-invoices (DIAN, CFDI, AFIP, SRI), so their text layer is exact. If an endpoint is ever added, set `TFA_DOCINTEL_ENDPOINT` and the app uses it. |
| Storage queue, Event Grid, Function App | the web app processes in the background itself and syncs SharePoint every 5 minutes. `functions/` stays for the day these resources exist. |
| Cosmos DB (in the template, not enabled in dev) | status and sync state live in two SQL tables. Set `TFA_COSMOS_ENDPOINT` to use Cosmos instead. |
| A second model | cross-model verification stays off. See §5. |

## 2. App settings

The app reads the **legacy app's setting names**, so the settings Terraform
already applies keep working. The `TFA_` spelling of each name also works.

| Setting | Value |
|---|---|
| `AZURE_STORAGE_ACCOUNT_URL` | `https://<account>.blob.core.windows.net` |
| `AZURE_OPENAI_STARTBURST_END_POINT` | `https://travel-res.openai.azure.com/` |
| `AZURE_OPENAI_STARTBURST_MODEL_NAME` | `gpt-4o` (the deployment name) |
| `AZURE_OPENAI_STARTBURST_API_VERSION` | `2024-10-21` or later. Structured outputs need 2024-08-01-preview or later. |
| `AZURE_OPENAI_STARTBURST_API_KEY` | optional. Leave unset to use the managed identity, which needs *Cognitive Services OpenAI User*. |
| `SQL_SERVER`, `SQL_DATABASE` | from tfvars |
| `SQL_USERNAME`, `SQL_PASSWORD` | `@Microsoft.KeyVault(...)` references to `mssql_server_username` and `mssql_server_password` |
| `USER_ASSIGNED_CLIENT_ID` | the app's managed identity |
| `ALLOWED_EMAILS` | comma- or semicolon-separated list of who may use the app |
| `FLASK_SECRET_KEY` | Key Vault reference. The app refuses to start without it. |
| `TFA_SHAREPOINT_SITE_ID`, `TFA_SHAREPOINT_DRIVE_ID` | the library to sync. Optional: leave unset and SharePoint sync is off. |
| `TFA_ALLOWED_GROUP_ID` | optional. Admits an Entra group as well as, or instead of, the email list. |

`CLIENT_ID`, `CLIENT_SECRET` and `TENANT_ID` are needed **only** if App
Service Authentication is turned off. In that case the app signs users in
itself, as the legacy app did.

### Sign-in

App Service Authentication is on in the tfvars. With it on, App Service signs
the user in and passes the identity to the app in `X-MS-CLIENT-PRINCIPAL-*`
headers. The app checks that user against `ALLOWED_EMAILS` (or the group), so
a signed-in user who is not on the list sees *Access denied*, and every data
route returns 403.

The app trusts those headers **only** when `WEBSITE_AUTH_ENABLED=True`. App
Service sets that variable itself and strips any copies of the headers that a
client sends. On a laptop nobody strips them, so they are ignored there.

## 3. Roles

| Who | Needs | On |
|---|---|---|
| App managed identity | Storage Blob Data Contributor | the storage account |
| App managed identity | Cognitive Services OpenAI User (only if no API key is set) | `travel-res` |
| App managed identity | Key Vault Secrets User | the Key Vault, for the references |
| App managed identity | AcrPull | the ACR |
| Your Entra group, for laptop runs | Storage Blob Data Contributor and Cognitive Services OpenAI User | as above |

SQL access uses the `SQLAdmin` login from Key Vault, so the database needs no
contained users. If you later move to Entra-only SQL sign-in, the last block
of `scripts/generate_sql_ddl.py` creates the managed identity's user.

## 4. Database

The app creates `tfa_processing_status` and `tfa_kv` on first start if its
login may. `SQLAdmin` may. To create everything ahead of time:

```
PYTHONPATH=src python scripts/generate_sql_ddl.py > migration.sql
```

**Before trusting totals from a database that v2 wrote to**, run
`db/migrations/001_dedupe_incident_2025_08.sql`. Run it against a restored
copy first.

## 5. Model accuracy, and what would raise it

What the app does now with gpt-4o:

- the full text layer of every page, plus page images
- a strict JSON schema (`to_strict_schema`)
- `temperature` 0
- validators that check the arithmetic
- a repair loop that sends the validator's complaint back to the model
- the eval gate in CI

The two changes that would raise accuracy further are both tfvars changes in
the infra repo. Neither needs a code change here.

1. **A second deployment from a different model family.** Then set
   `TFA_LLM_VERIFY_DEPLOYMENT`. When the first model is unsure, the second
   reads the document cold, and the money fields must agree within 1% or the
   document goes to review.
2. **A newer primary model** (for example a gpt-5-class model). Change
   `AZURE_OPENAI_STARTBURST_MODEL_NAME`. The provider already switches to
   `max_completion_tokens` and drops `temperature` for reasoning models.

Measure first: label 20 to 50 real folios as described in
`evals/golden/README.txt`, then run `python -m evals.local_eval --azure`.

## 6. Deployment (no Terraform drift)

Terraform pins the App Service image to `travel-folio-analysis-app:latest-dev`.
`azure-pipelines.yml` builds the image, pushes `$(Build.BuildId)` **and**
`latest-dev`, then restarts the App Service so it pulls the new image. It
never sets a different image tag on the app. A different tag would drift from
Terraform, and the next infra apply would revert it.
