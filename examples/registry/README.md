# Synthetic registry sample: 20 records

[registry_sample.xlsx](registry_sample.xlsx) contains **20 fully synthetic records plus a header** using the original input schema's 40 column names and order. It contains no original record values. IDs, titles, registration labels and people labels are fictional. Phone, postal-address and printing-contact fields are blank. URLs use reserved `.invalid` domains.

The earlier private working sample is not part of this public snapshot. This is a format example, not an evaluation set or a list of real sites. No source formulas, hidden sheets, comments, external links, macros, or embedded originals are copied. See [provenance.json](provenance.json) for the generated sample's metadata and hash.

From the repository root:

```powershell
python ver_1/skill/scripts/inspect_registry.py examples/registry/registry_sample.xlsx
.\ver_1\run_full.ps1 -InputXlsx examples/registry/registry_sample.xlsx -WorkDir ver_1/run_sample -DryRun
```

These commands inspect mapping and plan a run. The sample URLs are intentionally non-operational; do not use them for live collection. Supply your own local registry for a real run, or use the [offline fixture harness](../../news-audit-skill/tests/README.md) to exercise extraction without API calls.
