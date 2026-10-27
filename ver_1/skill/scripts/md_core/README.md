# Deterministic rule engine

[Criteria](../../references/README.md) · [Pipeline](../../README.md) · [Script index](../README.md)

## Modules

- [http.py](http.py): URL normalization and collection, including file fixtures.
- [zones.py](zones.py): HTML parsing and evidence text zones.
- [fields.py](fields.py): labels, extraction, names/dates, placeholder rejection, and `apply_cross_rules()`.
- [operation.py](operation.py): operating-status branches before disclosure inspection.
- [registry.py](registry.py): flexible input-header mapping and registry normalization/comparison.
- [gate.py](gate.py): Gate A after rules and Gate B after model review, controlling escalation.
- [schema.py](schema.py): output columns, flags, and spreadsheet-safe values.
- [evidence.py](evidence.py): validate that extracted findings are supported by source text.
- [audit.py](audit.py): per-record stages and evidence paths.
- [identity.py](identity.py): site identity/fingerprint helpers.
- [i3_reason.py](i3_reason.py): non-news page classification rules.
- [batch.py](batch.py): inherited batch helper; sequential execution is the intended archive workflow.

## Gates are routing decisions

Gate A distinguishes passed rules, text review, and diagnosis. Missing text or suspected client rendering can require diagnosis. Otherwise text review precedes expensive image diagnosis. Pass levels control unresolved-field and location checks.

Gate B asks whether images can add evidence: rejected model evidence, unresolved logo/title location, missing title, image hints, or incomplete JS text. A missing field alone does not prove that another model call is useful.

## Verify changes

From the repository root:

```powershell
python news-audit-skill/tests/disclosure_fixture_e2e.py
```

The fixture harness generates ten local records and asserts fixed outputs through extraction, comparison and final XLSX. Do not alter expected values just to make a regression pass. Its synthetic cases are separate from the included 20-record anonymized input sample.
