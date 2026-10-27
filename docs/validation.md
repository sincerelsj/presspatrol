# Validation

Verified locally on Windows on 2026-10-27 using the curated repository, without live news collection or paid model calls.

- Offline disclosure fixture E2E: all expected values matched across 10 generated records, covering extraction, registry comparison, and workbook generation. Fixture expectations were unchanged.
- Review workspace: all 10 unittest tests passed, including local HTTP authentication, access scope, CSRF, import conflicts, approval order, persistence, and version conflicts.
- Python source compilation and frontend JavaScript syntax checks passed.
- Root documentation links, SVG XML, and the PowerShell entry point were checked locally.

The inherited HTTP account test originally relied on supplied training materials. In this archive it generates temporary manifest and file-signature fixtures instead. These exercise authenticated delivery and path restrictions; they do not test PDF rendering or image decoding. No supplied training content was copied.

The E2E harness requires an absolute `--out` path when one is supplied and retains its artifacts. Initial relative-path execution failed before extraction; the run with an absolute path passed. UI HTTP tests require localhost socket access and passed outside the restricted socket sandbox.

These checks validate the covered behavior, not real-world model accuracy, live browser collection, provider availability, production security, or Linux deployment. The CI workflow is configured separately; its first remote result must be checked on GitHub.

## Feature documentation and anonymized sample

The root README was shortened to 44 lines and detailed guides moved into their owning feature directories. The pipeline sketch was rendered and visually checked; its handwriting is outlined in SVG so it does not depend on a viewer having the font installed.

The public sample contains exactly 20 anonymized records in one sheet with 40 source-schema columns. Only the 20 primary and 9 available secondary homepage URLs are restored from the original selection; other original values and personal contact fields are excluded. URL syntax and credentials/parameters were checked without visiting sites. All non-URL values were verified unchanged during restoration, and the rendered preview was reviewed.

The configuration loader was restricted to this repository's `.env` and placeholder keys are rejected. Eight archive checks cover credential-pattern detection without echoing values, staged-blob detection even when a working file is clean, forbidden data/config paths, the 20-record sample boundary, and parent-config isolation, real-contact rejection, and fixture-scoped synthetic numbers. Extraction fixture expectations remain unchanged. The existing 10 review tests still pass.
