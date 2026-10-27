# Offline disclosure regression fixtures

Run from the repository root:

```powershell
$env:PYTHONIOENCODING = "utf-8"
python news-audit-skill/tests/disclosure_fixture_e2e.py
```

The harness creates ten example registry records and local HTML files, executes the canonical extraction → comparison → workbook path, and compares fixed expected values. It automatically chooses `ver_1/skill/scripts/`. The synthetic fixture data is separate from the 20-record synthetic registry sample.

No news-site access or paid model call is required. Output remains under the system temporary directory. `--out` accepts a dedicated absolute output path; existing contents are replaced. Do not point it at a source/data directory. Do not change expected values to hide regressions.
