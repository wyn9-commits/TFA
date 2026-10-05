# Step-by-Step: TFA Platform in VS Code (Windows)

## Step 0 — Prerequisites (one-time installs)

| Tool | Why | Get it |
|---|---|---|
| Python 3.11 x64 | runtime | python.org (check "Add to PATH") |
| VS Code | editor | code.visualstudio.com |
| Git | source control | git-scm.com |
| Azure Functions Core Tools v4 | run Functions locally | `winget install Microsoft.Azure.FunctionsCoreTools` |
| ODBC Driver 18 for SQL Server | SQL access | Microsoft download (only needed for live SQL, not tests) |
| Azure CLI | auth for live Azure calls | `winget install Microsoft.AzureCLI` |

## Step 1 — Unzip and open

1. Unzip `tfa-platform.zip` somewhere like `C:\dev\tfa-platform`.
2. In VS Code: **File → Open Folder…** → select `tfa-platform`.
3. A popup offers the **recommended extensions** (Python, Azure Functions,
   Bicep, Ruff) — click **Install All**.

## Step 2 — Virtual environment + dependencies

Open the integrated terminal (`` Ctrl+` ``) and run:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks activation: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, reopen the terminal.

When VS Code asks "select this environment for the workspace?" → **Yes**
(or Ctrl+Shift+P → *Python: Select Interpreter* → `.venv`).

## Step 3 — Run the tests (no Azure needed)

```powershell
python -m pytest tests -q
```

Expected: **36 passed**. Or use the Testing sidebar (flask icon) — all tests
appear there because `.vscode/settings.json` already wires pytest.

Everything covered offline: validators, reconciliation math, country samples
(Colombia/Argentina/Ecuador/Mexico), escape hatches, Excel reader,
evaluators, and the LangGraph repair loop (scripted fake LLM).

## Step 4 — Configure endpoints (when Azure resources exist)

1. `copy .env.template .env` → fill in your storage, Document Intelligence,
   Foundry, SQL, Cosmos, and SharePoint values.
2. `copy functions\local.settings.json.template functions\local.settings.json`
   → same for the Functions host.
3. Sign in so DefaultAzureCredential works locally: `az login`
   (your user needs the same RBAC roles the Managed Identity will have:
   Storage Blob/Queue Data Contributor, Cognitive Services User, Cosmos DB
   Built-in Data Contributor, and a SQL user created FROM EXTERNAL PROVIDER).

## Step 5 — Create the SQL table

The DDL is generated from the column registry so it can never drift:

```powershell
python -c "from tfa_core.adapters.blob_cosmos import FolioSqlRepository; from tfa_core.config import get_settings, get_credential; s=get_settings(); print(FolioSqlRepository(s.sql_server, s.sql_database, s.sql_table, get_credential()).ddl())"
```

Paste the output into the Azure portal Query editor (or `sqlcmd`) once per
environment.

## Step 6 — Run the Functions host locally

- Ctrl+Shift+P → **Tasks: Run Task** → **func: host start**
  (starts `func start --python` inside `functions/` with PYTHONPATH set), or
- Run & Debug sidebar → **Attach to Azure Functions** to debug with
  breakpoints.

The timer function `sync_sharepoint` will no-op politely until
`TFA_SHAREPOINT_SITE_ID`/`DRIVE_ID` are set.

## Step 7 — Git + Azure DevOps

```powershell
git init
git add .
git commit -m "TFA platform v2 initial commit"
git remote add origin https://dev.azure.com/<org>/<project>/_git/tfa-platform
git push -u origin main
```

`.gitignore` already blocks `.env`, `local.settings.json`, and any sample
documents dropped into `evals/golden/` — Restricted data stays out of the repo.

## Step 8 — Label goldens & run the model bake-off

1. Copy 5–10 sample PDFs from SharePoint into `evals/golden/`.
2. For each, write `<name>.expected.json` next to it —
   `evals/golden/colombia_wgt000032.expected.json` is the worked example.
3. Push the dataset (needs LangSmith env vars from `.env`):
   Run & Debug → **Eval: push goldens**, or
   `python -m evals.run_eval push --dataset tfa-goldens-v1`
4. Run one experiment per model config:
   ```powershell
   $env:TFA_LLM_DEPLOYMENT="gpt-5.6";          python -m evals.run_eval run --dataset tfa-goldens-v1 --name gpt56
   $env:TFA_LLM_PROVIDER="anthropic_foundry"; $env:TFA_LLM_DEPLOYMENT="claude-sonnet-5"; python -m evals.run_eval run --dataset tfa-goldens-v1 --name sonnet5
   ```
5. Compare in the LangSmith UI; ship the winner on `money_exact`,
   tie-break on `review_routing`.

## Where things live

```
src/tfa_core/schemas.py            ← the data model & column registry (start here)
src/tfa_core/extraction/           ← DI layout, LLM extractor, validators, reconciliation, pipeline
src/tfa_core/graph/                ← LangGraph engine (TFA_EXTRACTION_ENGINE=graph)
src/tfa_core/ingestion/sharepoint.py ← Graph delta sync
src/tfa_core/storage/repositories.py ← SQL (Entra token), Cosmos, Blob
functions/function_app.py          ← the 4 Azure Functions
evals/                             ← LangSmith bake-off harness + goldens
tests/                             ← 36 offline tests
infra/                             ← empty; Bicep goes here next
```

## Common issues

- **`ModuleNotFoundError: tfa_core` in terminal** → the venv terminal was
  opened before settings loaded; open a new terminal (PYTHONPATH is injected
  per-terminal).
- **pyodbc install fails** → install "ODBC Driver 18 for SQL Server" first;
  on dev machines without it, tests still pass (SQL is only touched live).
- **`func` not found** → reinstall Core Tools v4 and reopen VS Code.
- **DefaultAzureCredential errors locally** → run `az login`, and set
  `TFA_USER_ASSIGNED_CLIENT_ID` empty locally (it's for the deployed identity).
