# Failed-site resolver

[Agent catalog](../README.md) · [Implementation](../../scripts/resolve_failed.py) · [Costs](../../COSTS.md)

## Contract

- **Input:** unavailable/non-news record and registry identity information.
- **Output:** candidate URL, search rationale, identity comparisons, and resolution status.
- **Artifacts:** `work/{id}.resolved.json, audit/{id}.json, and CSV search columns`.
- **Implementation:** SYSTEM, WEB_SEARCH_TOOL, comparison helpers, and main() in [`resolve_failed.py`](../../scripts/resolve_failed.py). Prompt and schema definitions are preserved in executable source.

## Behavior and evidence

Uses web_search_20260209 with max_uses=4. Similar names alone do not establish identity: candidate disclosure information is compared with registration data. Candidate validation is a controlled network exception. A changed URL needs another collection/judgment pass.

## Cost unit

Charges depend on actual input/cache/output tokens, not one fixed price per record. The response output cap is 3000 tokens. Text review's cap can be configured. See [the common estimator and its limitations](../../COSTS.md). Search tool fees are additional and excluded from the existing token estimator.

## Plan against an existing work directory

```powershell
python ver_1/skill/scripts/resolve_failed.py --workdir ver_1/run_sample --dry-run
```

The work directory must already contain the required stage artifacts. Dry-run does not fabricate those inputs or perform a live inspection.
