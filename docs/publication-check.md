# Publication privacy check

Checked 2026-10-27 against the complete file set, the sample workbook's XML, and the private predecessor's reachable history.

## Removed from the public snapshot

- Original sample records, real names, postal addresses and contact fields. The replacement is 20 anonymized records with the same 40-column input schema. At the owner's request, only the primary/secondary homepage URL fields retain actual public URLs; personal contact fields remain blank and people labels remain fictional.
- A non-example fixture email, a real-case name/contact example, and real contact details in source comments and report text.
- User-specific machine paths and real residential-address examples in prompts/docstrings.
- Private-history commit metadata containing a personal email. Public commits use GitHub's privacy-preserving author address.

## Retained deliberately

The offline regression fixture requires realistic phone formats to test extraction. Its explicitly enumerated synthetic numbers are allowed only inside that one fixture file. Their values are test data, not a directory of actual contacts. Test email addresses use a reserved example domain. Province names and generic extraction patterns remain part of the implementation.

## Publication method

The predecessor repository remains private as an archive. The public repository starts from a fresh Git root containing only the checked snapshot; the predecessor's branches, commit objects, account databases, original data and generated artifacts are not copied. Personal author-email metadata from private commits is not made public.

## Automated checks

The commit/push scanner rejects known key patterns, non-example email addresses, unapproved phone numbers, personal home paths, and unapproved data/evidence files. It enforces blank contact fields, synthetic people labels, homepage URLs without credentials, query parameters or fragments, and the 20-record limit. CI additionally scans all reachable commit versions and checks author/committer privacy addresses.

The extraction fixture expectations are unchanged. Eight archive safety tests and ten review tests passed locally; the offline extraction/comparison/workbook regression also passed. Pattern scanning and manual review reduce disclosure risk but cannot establish the absence of every possible identifying detail.
