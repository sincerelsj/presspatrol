# Vision diagnostician

[Agent catalog](../README.md) · [Implementation](../../scripts/diagnose_site.py) · [Costs](../../COSTS.md)

## Contract

- **Input:** saved top/footer screenshots, rendered DOM text, and unresolved fields.
- **Output:** diagnostic cause and proposed evidence-supported corrections.
- **Artifacts:** `work/{id}.diag.json, per-record audit, and pipeline overrides`.
- **Implementation:** SYSTEM_PROMPT, build_tool(), verify_corrections(), and main() in [`diagnose_site.py`](../../scripts/diagnose_site.py). Prompt and schema definitions are preserved in executable source.

## Behavior and evidence

Reads saved evidence instead of visiting the live site. Corrections are validated against available evidence; image-only fields remain explicitly identified.

## Cost unit

Charges depend on actual input/cache/output tokens, not one fixed price per record. The response output cap is 4000 tokens. Text review's cap can be configured. See [the common estimator and its limitations](../../COSTS.md). Retries and multi-step processing can add calls.

## Plan against an existing work directory

```powershell
python ver_1/skill/scripts/diagnose_site.py --workdir ver_1/run_sample --dry-run
```

The work directory must already contain the required stage artifacts. Dry-run does not fabricate those inputs or perform a live inspection.
