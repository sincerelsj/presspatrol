# PressPatrol contributor instructions

- Canonical pipeline code lives in `ver_1/skill/scripts/`.
- Judgment must be performed by scripts calling the model API, never by a conversational assistant inspecting live news sites or hand-writing overrides.
- Preserve audit evidence and uncertainty. AI results remain drafts until the required human review.
- Preserve the frozen rule baseline; distinguish collection failure from missing disclosures and registry mismatch from disclosure compliance.
- Collection workers must not exceed eight. Use sequential model calls, not batch execution.
- Keep live datasets, account databases, keys, evidence, and supplied guideline/training originals out of Git.
- The only approved sample is `examples/registry/registry_sample.xlsx`: exactly 20 anonymized records plus the header. Only the URL and 발행소 홈페이지 fields retain actual public homepage URLs at the owner's request; phone/address fields stay blank and people labels stay fictional. No URL credentials or query parameters, formulas, external workbook links, embedded source documents, or full datasets.
- Keep `.env.example` at `ANTHROPIC_API_KEY=your_API_key`; never read or import a parent project's `.env`. Run the staged credential/data scanner before every commit and push.
- After extraction-rule changes run `python news-audit-skill/tests/disclosure_fixture_e2e.py`; do not change expected values to hide regressions.
- Run review-workspace tests for changes to review behavior or permissions.
- Save PowerShell files as UTF-8 BOM and use ASCII flags. Do not repurpose reserved PowerShell variables.
- Model/provider documentation must match the actual SDK implementation. Do not claim a production deployment or measured accuracy without evidence.
- No reuse license has been selected. Do not add one without the owner's decision.
