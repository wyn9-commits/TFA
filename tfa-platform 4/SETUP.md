# VS Code Setup

```bash
git init                                   # if starting the repo here
python3.11 -m venv .venv
source .venv/bin/activate                  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.template .env                      # fill in your endpoints
cp functions/local.settings.json.template functions/local.settings.json
python -m pytest tests -q                  # expect: 36 passed
```

Open the folder in VS Code → install recommended extensions when prompted.
- **Run tests:** Testing sidebar, or Ctrl+Shift+P → "Tasks: Run Test Task".
- **Run Functions locally:** requires Azure Functions Core Tools v4;
  task "func: host start", then the "Attach to Azure Functions" debug config.
- **Evals:** put goldens in `evals/golden/` then use the launch configs.

Repo map is in README.md.
