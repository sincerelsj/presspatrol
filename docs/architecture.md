# Architecture

## Collection and judgment

`run_full.ps1` invokes the canonical Python orchestration. Registry mapping is inspected before processing. `run_extract.py` collects HTML and computes a rule baseline. Browser capture stores rendered HTML and top/footer/article evidence, then extraction can use the rendered content.

Gates route records through failed-site resolution, LLM extraction, visual diagnosis, and publication-date verification as needed. These stages call the model API from scripts. Downstream stages reuse saved artifacts; publication-date refetching is explicitly controlled by `--allow-fetch`.

After judgment, annotation uses saved HTML. Merge, registry comparison, workbook generation, and location-map generation produce inspection artifacts. The rule baseline remains a separate comparison record.

## Artifact contract

- `work/`: per-record extraction and model-review results.
- `shots/`: original screenshots, rendered HTML, article evidence, and annotations.
- `audit/`: per-record judgment/call audit artifacts.
- `evidence/`: field-level selector, region, coordinates, matching text, and context.
- `outputs/`: intermediate CSV, inspection workbooks, and location maps.

These are runtime artifacts, excluded from Git. See the reference schemas under `ver_1/skill/references/` for field details.

## Human verification

The review server uses Python's HTTP server and SQLite with an HTML/CSS/JavaScript frontend. Administrators preview and import verify CSV files, with file hashes and import history. Each business retains a source snapshot. Changed existing imports are reported as conflicts instead of silently replacing reviewed data.

Assignments scope reviewer access. Staged approvals, prerequisite checks, optimistic versions, and transactional events protect review state. Review results are distinct from automatic results. Scoped CSV export is implemented; automatic reprocessing execution and final XLSX integration are not connected.

## Portfolio boundary

The copied source paths are preserved. The fixture harness still resolves `ver_1/skill/scripts/` as canonical. The input schema is illustrated by the [20-record synthetic registry sample](../examples/registry/README.md). Full datasets, operational databases, screenshots, prototype record IDs, and externally supplied training assets are excluded. Training links can return a missing-resource response in this archive.

Feature-level navigation lives in the [pipeline guide](../ver_1/skill/README.md), [rule engine](../ver_1/skill/scripts/md_core/README.md), [criteria](../ver_1/skill/references/README.md), and [agent catalog](../ver_1/skill/agents/README.md). The [hand-drawn flow](../assets/pipeline.svg) summarizes the normal path; failed-site resolution is a separate follow-up and can require a new pass for affected records.
