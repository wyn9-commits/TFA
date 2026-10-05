# How to run this

**There are no API keys, passwords or connection strings in this system.**
Everything authenticates with your Azure identity — `az login` locally, a
Managed Identity in Azure. If you find yourself looking for a secret to paste,
you are on the wrong path.

What you need instead is **access**: your account must hold the right roles on
the right resources. The sections below say exactly which.

---

# Part 1 — Run the tests (5 minutes, no Azure)

Do this first. It confirms the code arrived intact, so a later failure is
clearly configuration rather than corruption.

```bash
python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

PYTHONPATH=src pytest tests -q
```

Expect **78 passed**. No Azure account, no network, no database.

---

# Part 2 — What you need access to

Seven resources. The first four are mandatory.

| # | Resource | What it does | Role you need |
|---|---|---|---|
| 1 | Storage account | holds the source documents | **Storage Blob Data Contributor** |
| 2 | Document Intelligence | reads the document (OCR + tables) | **Cognitive Services User** |
| 3 | Foundry / Azure OpenAI | extracts the fields | **Cognitive Services User** |
| 4 | Azure SQL | reconciliation results | member of the Entra admin group |
| 5 | Cosmos DB | per-document processing status | **Cosmos DB Built-in Data Contributor** |
| 6 | Storage queues | work queue between sync and worker | **Storage Queue Data Contributor** |
| 7 | SharePoint library | where folios arrive | Graph **Sites.Selected**, read |

## Who grants these

You almost certainly cannot grant them to yourself. Ask:

- **Roles 1-3, 5-6** — whoever owns the Azure subscription (Cloud Platform)
- **Role 4** — the SQL Entra admin group owner; ask to be added to the group
- **Role 7** — a Microsoft 365 / Entra administrator. This one takes longest;
  ask early

If the resources do not exist yet, see Part 6.

---

# Part 3 — Find your endpoint values

Sign in once:

```bash
az login
az account set --subscription "<your subscription>"
```

Then collect the five values. These are **endpoints, not secrets** — they are
safe to paste into a config file and into a ticket.

```bash
# 1. Storage — blob and queue endpoints
az storage account list --query "[].{name:name, blob:primaryEndpoints.blob, queue:primaryEndpoints.queue}" -o table

# 2. Document Intelligence
az cognitiveservices account list --query "[?kind=='FormRecognizer'||kind=='AIServices'].{name:name, endpoint:properties.endpoint}" -o table

# 3. Foundry / Azure OpenAI — the ACCOUNT
az cognitiveservices account list --query "[?kind=='AIServices'||kind=='OpenAI'].{name:name, endpoint:properties.endpoint}" -o table

#    …and the DEPLOYMENT name on it
az cognitiveservices account deployment list -n <account-name> -g <resource-group> --query "[].{deployment:name, model:properties.model.name}" -o table

# 4. SQL server
az sql server list --query "[].{name:name, fqdn:fullyQualifiedDomainName}" -o table

# 5. Cosmos
az cosmosdb list --query "[].{name:name, endpoint:documentEndpoint}" -o table
```

> **The single most common mistake:** `TFA_LLM_DEPLOYMENT` must be your
> **deployment** name, not the model name. They are often different. A 404 from
> extraction is almost always this.

---

# Part 4 — Write the config

```bash
cp .env.template .env
```

Fill in what you collected:

```bash
TFA_ENVIRONMENT=dev

# Storage
TFA_STORAGE_ACCOUNT_URL=https://<account>.blob.core.windows.net
TFA_QUEUE_ACCOUNT_URL=https://<account>.queue.core.windows.net

# Document Intelligence
TFA_DOCINTEL_ENDPOINT=https://<name>.cognitiveservices.azure.com

# Model
TFA_LLM_ENDPOINT=https://<name>.openai.azure.com
TFA_LLM_DEPLOYMENT=<DEPLOYMENT name, not the model name>
TFA_LLM_PROVIDER=azure_openai

# Database
TFA_SQL_SERVER=<server>.database.windows.net
TFA_SQL_DATABASE=tfa
TFA_COSMOS_ENDPOINT=https://<name>.documents.azure.com:443/
```

**`.env` is gitignored. Keep it that way** — it names internal infrastructure
even though it holds no secrets.

---

# Part 5 — Prepare the data layer

## 5a. Database schema

```bash
sqlcmd -S <server>.database.windows.net -d tfa -G -i db/migrations/001_initial_schema.sql
sqlcmd -S <server>.database.windows.net -d tfa -G -i db/migrations/002_incident_2025_08_dedupe.sql
```

`-G` means Entra authentication — no password. You must be in the SQL Entra
admin group (role 4 above).

**Read the header of `002` before running it. It deletes duplicate rows.** On
an existing database, run it against a restored copy first.

## 5b. Negotiated rates — without these nothing reconciles

```bash
az storage blob upload --auth-mode login --account-name <account> \
  -c travel-folios -n rates/negotiated_rates.json -f seed/negotiated_rates.json

az storage blob upload --auth-mode login --account-name <account> \
  -c travel-folios -n rates/fx.json -f seed/fx.json
```

`seed/` contains **examples**. Replace them with your contracted rates first —
see `seed/README.md`. Without real rates, extraction runs and every overcharge
column comes back empty, which reads as a bug rather than missing data.

## 5c. SharePoint

Ask your Entra admin to grant the application `Sites.Selected` on the TFA site
only:

```http
POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions
{ "roles": ["read"],
  "grantedToIdentities": [{ "application": { "id": "<client-id>", "displayName": "TFA" } }] }
```

`Sites.Read.All` also works and will not survive a governance review.

Then resolve the library:

```bash
python scripts/resolve_sharepoint.py "https://<tenant>.sharepoint.com/sites/<site>/Shared%20Documents/Forms/AllItems.aspx"
```

Paste the URL from your browser address bar. It returns `TFA_SHAREPOINT_SITE_ID`
and `TFA_SHAREPOINT_DRIVE_ID`, and previews which batch each folder will
produce (`Argentina/2025` → `argentina_2025`). Add `--write` to save them.

---

# Part 6 — If the Azure resources do not exist yet

```bash
# Fill in vnetAddressSpace and sqlEntraAdminGroupObjectId first
code infra/main.dev.bicepparam

az group create -n asg-tfa-dev-scus-rg -l southcentralus
az deployment group what-if -g asg-tfa-dev-scus-rg \
  --template-file infra/main.bicep --parameters infra/main.dev.bicepparam
az deployment group create -g asg-tfa-dev-scus-rg \
  --template-file infra/main.bicep --parameters infra/main.dev.bicepparam
```

Review the `what-if` output before applying. Private endpoints and DNS zones
make this take 10-15 minutes.

> **Private endpoints mean a laptop on the open internet cannot reach these
> resources.** You must be on the corporate VPN. Preflight failing on every
> connectivity check is that network posture working, not a defect.

---

# Part 7 — Verify

```bash
python scripts/preflight.py
```

Checks config, your credential, Blob, the SQL schema, seed data, Document
Intelligence and the model deployment — and names exactly what is missing.

Fix anything red before continuing.

---

# Part 8 — The first real document

```bash
python scripts/process_one.py /path/to/folio.pdf --identifier pilot_001 --dry-run
```

`--dry-run` writes nothing. It prints every stage: pages read, OCR confidence,
whether page images were needed, how many repair attempts the extraction took,
whether the line items sum to the printed total, the matched hotel, the FX rate
applied, and the net variance.

**Until this succeeds, every accuracy claim about this system — including the
ones in this repository — is theoretical.** Do it before scheduling any client
demonstration.

Drop `--dry-run` to persist.

---

# Part 9 — Deploy the worker

```bash
cd functions
func azure functionapp publish <function-app-name> --python
```

Set the same `TFA_*` values as application settings, and assign the Function
App's Managed Identity the roles in Part 2. The Function App uses its identity;
no secrets are deployed.

Confirm it is working:

```bash
az webapp log tail --name <function-app> --resource-group <rg>
```

Drop one file into `Argentina/2025/` in SharePoint. It should appear in SQL
within five minutes.

---

# Troubleshooting

| What you see | What it means |
|---|---|
| Preflight fails everything, credential OK | Not on the VPN — private endpoints |
| `404` from extraction | `TFA_LLM_DEPLOYMENT` is the model name, not the deployment name |
| `403` from Document Intelligence or the model | Missing **Cognitive Services User** |
| SQL login failed | Not in the Entra admin group, or `-G` omitted from sqlcmd |
| Extraction works, overcharge columns empty | Negotiated rates not uploaded (5b) |
| Graph returns `403` | `Sites.Selected` not granted on that site |
| Documents sync but nothing processes | Event Grid subscription missing — check `infra/modules/events.bicep` |
| Duplicate rows appearing | `002` migration not applied |

---

# What is still outstanding

**Accuracy is unmeasured.** `evals/golden/` holds one example. Label 20-50 real
documents, then:

```bash
python -m evals.run_eval
```

`money_exact` — every cent exact — is the metric that matters. Until it has
documents to score, this system's accuracy is an expectation, not a number.
That is the last thing standing between this and a defensible delivery.
