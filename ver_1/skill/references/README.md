# Original implemented rules and schemas

This directory preserves the human-authored field-rule documentation used by the source engine. It is not a new interpretation made for the portfolio.

- [field_rules.md](field_rules.md): operating-status branches, eight required fields, date/name alternatives, and cross-field recognition rules implemented from the supplied 2026-08-13 guideline.
- [output_schema.md](output_schema.md): extraction, comparison, and workbook fields.
- [override_schema.md](override_schema.md): explicit override structure and evidence requirements. Overrides are produced by the pipeline; a conversational assistant must not hand-write inspection decisions.
- [Rule engine source guide](../scripts/md_core/README.md): where these rules are implemented.
- [Agent source guide](../agents/README.md): how model prompts and validation apply them.

The originally supplied guideline workbook is not redistributed. If implementation and that guideline differ, the guideline is authoritative for the original inspection task. These archived documents describe the project's implemented criteria, not a claim about current law or universal compliance requirements.

## Essential distinctions

The required fields are 제호, 등록번호, 등록연월일, 발행인, 편집인, 발행연월일, 발행소 주소, and 발행소 전화번호. The archive also retains optional metadata.

Operating status is decided before disclosure checks. A failed/parked/empty site is handled as `홈페이지없음`, not proof of missing disclosures. Registry differences are separately recorded. The date and name cross-rules are explicit, including the article-date exception for registration dates and exclusion of real-time current-date widgets.
