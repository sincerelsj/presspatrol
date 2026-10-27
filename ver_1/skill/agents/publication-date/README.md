# Publication-date verifier

[Agent catalog](../README.md) · [Implementation](../../scripts/verify_pubdate.py) · [Costs](../../COSTS.md)

## Contract

- **Input:** saved main-page and article evidence, existing date finding, and inspection date.
- **Output:** publication-date evidence, provenance, and usage.
- **Artifacts:** `publication-date findings in work/ and audit/ plus output updates`.
- **Implementation:** prepare_main(), call(), tool schemas, and main() in [`verify_pubdate.py`](../../scripts/verify_pubdate.py). Prompt and schema definitions are preserved in executable source.

## Behavior and evidence

Reuses saved evidence. --allow-fetch is the explicit exception for network retrieval. Article dates and registration dates follow different criteria; current-date widgets are excluded.

## Cost unit

Charges depend on actual input/cache/output tokens, not one fixed price per record. The response output cap is 2000 tokens. Text review's cap can be configured. See [the common estimator and its limitations](../../COSTS.md). Retries and multi-step processing can add calls.

## Plan against an existing work directory

```powershell
python ver_1/skill/scripts/verify_pubdate.py --workdir ver_1/run_sample --dry-run
```

The work directory must already contain the required stage artifacts. Dry-run does not fabricate those inputs or perform a live inspection.
