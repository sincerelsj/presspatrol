# Review workspace

Python 3.10+, SQLite, and plain HTML/CSS/JavaScript. No frontend build step is required. Run commands from the repository root.

```powershell
python ver_1/review_ui/manage.py
python ver_1/review_ui/server.py --port 8081 --source ver_1/run_demo
```

Open http://127.0.0.1:8081/login. Bootstrap creates an administrator and writes the generated password to the ignored `data/admin_initial_credentials.txt` file. Change the password after signing in.

This archive starts with no businesses or reviewer accounts. The source directory should contain `outputs/verify_*.csv` produced by the pipeline, with associated `shots/`, `audit/`, `work/`, and `evidence/` artifacts. Administrators preview and import a source file, create reviewers, and assign records. Keep each inspection round in a separate `--data-dir`.

## Implemented

- Administrator/reviewer roles and assignment-scoped reads, writes, evidence, and exports.
- Source preview, SHA-256 validation, import history, and conflict preservation.
- Staged review, save/hold/approval states, prerequisite checks, optimistic versions, and event history.
- Session authentication, password hashing, CSRF checks, and login throttling.
- Saved evidence display, zoom/pan, and original/annotated screenshot selection.

## Boundaries

Reprocessing requests can be stored and exported, but are not executed by a connected API worker. Reviewer CSV output is separate from the pipeline final workbook. Training assets and the operational prototype selection are not included; missing training links are expected. This archive does not claim a verified production deployment.

## Checks

```powershell
python -m unittest discover -s ver_1/review_ui -p "test_*.py" -v
```
