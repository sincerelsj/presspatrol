# Run inspector

[Agent catalog](../README.md) · [Implementation](../../scripts/inspect_run.py) · [Costs](../../COSTS.md)

## Contract

- **Input:** computed aggregate metrics and diagnostic samples from an existing run.
- **Output:** dimension-level inspection notes, usage, and reports.
- **Artifacts:** `run inspection JSON/Markdown reports`.
- **Implementation:** DIMENSIONS, SYSTEM, metrics collection, and model call in [`inspect_run.py`](../../scripts/inspect_run.py). Prompt and schema definitions are preserved in executable source.

## Behavior and evidence

--no-llm computes diagnostic metrics without model interpretation. This is a run-quality review, not a replacement for record-level human verification.

## Cost unit

Charges depend on actual input/cache/output tokens, not one fixed price per record. The response output cap is 3000 tokens. Text review's cap can be configured. See [the common estimator and its limitations](../../COSTS.md). Retries and multi-step processing can add calls.

## Plan against an existing work directory

```powershell
python ver_1/skill/scripts/inspect_run.py --workdir ver_1/run_sample --dry-run
```

The work directory must already contain the required stage artifacts. Dry-run does not fabricate those inputs or perform a live inspection.
