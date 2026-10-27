# I-3 site classifier

[Agent catalog](../README.md) · [Implementation](../../scripts/classify_i3.py) · [Costs](../../COSTS.md)

## Contract

- **Input:** saved text for records already classified as non-news under I-3.
- **Output:** screen category, explanation, evidence, and suggested search value.
- **Artifacts:** `work/{id}.i3.json and audit/{id}.json`.
- **Implementation:** SYSTEM, build_tool(), build_request(), and main() in [`classify_i3.py`](../../scripts/classify_i3.py). Prompt and schema definitions are preserved in executable source.

## Behavior and evidence

This is a supplementary utility. It classifies the saved screen and does not itself establish missing disclosures or reopen a site visit.

## Cost unit

Charges depend on actual input/cache/output tokens, not one fixed price per record. The response output cap is 700 tokens. Text review's cap can be configured. See [the common estimator and its limitations](../../COSTS.md). Retries and multi-step processing can add calls.

## Plan against an existing work directory

```powershell
python ver_1/skill/scripts/classify_i3.py --workdir ver_1/run_sample --dry-run
```

The work directory must already contain the required stage artifacts. Dry-run does not fabricate those inputs or perform a live inspection.
