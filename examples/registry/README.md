# Anonymized registry sample: 20 records

[registry_sample.xlsx](registry_sample.xlsx) preserves the original input schema's 40 column names and order. It includes **20 actual primary homepage URLs** and **9 available secondary homepage URLs** from the original 20-record selection. Only the `URL` and `발행소 홈페이지` values are restored. Names, IDs, registration labels and business labels are fictional; phone, postal-address and printing-contact fields are blank.

URLs were checked for HTTP/HTTPS syntax and absence of embedded authentication, query parameters and fragments. No live site was fetched or judged during preparation. No source formulas, hidden sheets, comments, external workbook links, macros, or embedded originals are copied. See [provenance.json](provenance.json) for the field policy and sample hash.

From the repository root:

```powershell
python ver_1/skill/scripts/inspect_registry.py examples/registry/registry_sample.xlsx
.\ver_1\run_full.ps1 -InputXlsx examples/registry/registry_sample.xlsx -WorkDir ver_1/run_sample -DryRun
```

These commands inspect mapping and plan a run without visiting sites. A live run against this sample visits actual sites, but registration comparisons against fictional labels are demonstration output, not real compliance findings. Use your own accurate local registry for an inspection, or the [offline fixture harness](../../news-audit-skill/tests/README.md) for reproduction without live collection or model calls.
