# Repository checks

Run from the repository root:

```powershell
python -m unittest discover -s tools -p "test_*.py" -v
python tools/check_secrets.py --tracked
python tools/check_public_history.py
git config core.hooksPath .githooks
```

`check_secrets.py --staged` checks the complete Git index before commit. The push hook and `--tracked` check the committed HEAD used by CI. It reads Git blobs only, never local `.env` files. It prints file/rule names without matching values.

The checks reject known provider-token/private-key patterns, operational data paths, and unapproved spreadsheet/evidence files. The one approved anonymized workbook must have a single sheet, 20 data rows plus a header, no formulas, and no embedded/external content. Compressed worksheet XML is also scanned. Real email addresses, phone numbers and personal home paths are rejected. Only the finite synthetic-phone list in the designated regression fixture and reserved example email domains are allowed. The sample contact fields must be blank and its people labels must be fictional; actual homepage URLs are permitted only without credentials or query parameters. Pattern-based checks have coverage limits.

The safety tests cover key detection without echoing values, placeholder-only configuration, parent `.env` isolation, and the sample boundary. No paid API call or live-site judgment is performed.

`check_public_history.py` scans every reachable commit version and checks privacy-preserving author/committer addresses. It is intended for the clean public repository; the private predecessor deliberately retains its original history. CI fetches the complete public history before running it.
