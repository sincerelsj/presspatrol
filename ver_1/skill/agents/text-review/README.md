# Text reviewer

[Agent catalog](../README.md) · [Implementation](../../scripts/llm_extract.py) · [Costs](../../COSTS.md)

## Contract

- **Input:** saved top/footer text, rule findings, and inspection date.
- **Output:** structured disclosure findings, agreement/disagreement, verbatim evidence, and usage.
- **Artifacts:** `work/{id}.llm.json and comparison CSV`.
- **Implementation:** SYSTEM_PROMPT, build_tool(), build_user_turn(), call_sync(), and post-processing in [`llm_extract.py`](../../scripts/llm_extract.py). Prompt and schema definitions are preserved in executable source.

## Behavior and evidence

The engine validates verbatim evidence against the supplied text, rejects unsupported values, and can request a repair turn. Rules remain a comparison baseline.

## Cost unit

Charges depend on actual input/cache/output tokens, not one fixed price per record. The response output cap is 3000 tokens. Text review's cap can be configured. See [the common estimator and its limitations](../../COSTS.md). Retries and multi-step processing can add calls.

## Plan against an existing work directory

```powershell
python ver_1/skill/scripts/llm_extract.py --workdir ver_1/run_sample --sync --dry-run
```

The work directory must already contain the required stage artifacts. Dry-run does not fabricate those inputs or perform a live inspection.
