# Inspection pipeline

[Setup](SETUP.md) · [Rules](references/README.md) · [Agents](agents/README.md) · [Costs](COSTS.md) · [Source index](scripts/README.md)

## Execution order

1. **R0 — collect and extract:** `run_extract.py` reads the registry, collects HTML, checks operating status, and freezes the original rule result.
2. **R0b — browser evidence:** `capture_shots.py` saves top/footer screenshots, unannotated rendered HTML, and article evidence within the capture session.
3. **R0c — rendered extraction:** rerun extraction against saved rendered HTML and reevaluate Gate A.
4. **R1 — text review:** `llm_extract.py` examines uncertain text and rule findings. Gate B decides whether visual diagnosis adds value.
5. **R2/R3 — specialized review:** `diagnose_site.py` uses saved images/DOM; `verify_pubdate.py` checks saved date/article evidence. No new visit is intended; publication-date fetching requires `--allow-fetch`.
6. **R4 — annotate and export:** saved HTML produces annotated evidence, then merge, registry comparison, final XLSX, and field-location maps run. `inspect_run.py` can inspect the resulting run.

Failed-site resolution is an additional API search path. In the default `--phase all` implementation it runs as R1b after capture/re-extraction and before R1 text review. A confirmed replacement URL still requires collecting and judging the affected site; a search result does not imply a completed inspection. The inherited `--phase` help text describes a different intended scheduling order, so this guide follows the executable default path rather than treating that help text as a guarantee.

## Plan with the included sample

```powershell
python ver_1/skill/scripts/inspect_registry.py examples/registry/registry_sample.xlsx
.\ver_1\run_full.ps1 -InputXlsx examples/registry/registry_sample.xlsx -WorkDir ver_1/run_sample -DryRun
```

The mapping is a human gate. Review it before a real run. The sample contains actual homepage URLs and fictional registration/person labels. `-SkipLlm` collects without model calls; browser dependencies are still required and sites will be visited. Use your own accurate local registry for meaningful registration comparisons. Paid execution requires [configuration](SETUP.md).

Use `-SkipCapture` only with an existing evidence directory. Keep `-Workers` at eight or fewer. Sequential calls are the intended mode; inherited batch code is preserved for source history but is not the recommended or tested archive workflow.

## Data and result boundaries

- Operating-status failures are not missing disclosures and must not become noncompliance counts.
- `준수여부_규칙` is the frozen baseline. `준수여부_LLM` is the automatic final draft used for review.
- Registry mismatches are separate from disclosure presence.
- `work/`, `audit/`, `shots/`, `evidence/`, `overrides/`, and `outputs/` are local runtime artifacts.
- The [workspace](../review_ui/README.md) imports verify CSV output and reads saved evidence. Its human-review exports are separate from `finalize.py` workbooks.

The canonical scripts remain in `ver_1/skill/scripts/`. Feature documentation is separated without moving modules or changing imports, PowerShell entry points, or test discovery.
