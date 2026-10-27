# Setup and API-key configuration

## Install

Python 3.10+ and PowerShell are used for the documented entry point. From the repository root:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
$env:PYTHONIOENCODING = "utf-8"
```

The offline fixture and review tests do not require a model API key. Fixtures retain artifacts under the system temporary directory by default. A custom `--out` must be an absolute path to a dedicated disposable directory; the harness replaces its contents.

## Configure your own key

```powershell
Copy-Item .env.example .env
```

Edit the new local `.env`:

```dotenv
ANTHROPIC_API_KEY=your_API_key
DISCLOSURE_LLM_MODEL=claude-sonnet-5
```

Replace `your_API_key` locally with your own key. Select a model available to your account and check [cost assumptions](COSTS.md) if you change models. The current implementation uses `anthropic.Anthropic()`; it is not an OpenAI SDK integration.

Only `.env.example` belongs in Git. The loader reads `.env` at this repository root, never a parent project's file. Existing environment variables take precedence. Missing keys and placeholder keys are rejected before a model call. No key is printed by the validation error.

## Run a planned inspection

```powershell
python ver_1/skill/scripts/inspect_registry.py examples/registry/registry_sample.xlsx
.\ver_1\run_full.ps1 -InputXlsx examples/registry/registry_sample.xlsx -WorkDir ver_1/run_sample -DryRun
```

The sample retains 20 actual homepage URLs; its registration and person labels are fictional, so comparisons are not real inspection findings. For a meaningful live inspection, supply your own accurate local registry. Review mapping and the plan before removing `-DryRun`: live runs visit sites and can incur API charges. Use the offline fixture harness for local reproduction.

## Repository credential checks

```powershell
git config core.hooksPath .githooks
python tools/check_secrets.py --tracked
```

The commit hook scans staged blobs, not just working-tree files; the push hook checks committed blobs. CI also checks committed files. The scanner rejects credential patterns and forbidden data/config paths, permits only placeholder configuration, and enforces the sample's 20-row limit. It reports filenames and rule names, never matching key values. Pattern scanning cannot prove the absence of every possible secret.
