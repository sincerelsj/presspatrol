# Script index

Executable modules stay together so imports and existing entry points remain intact. Documentation is grouped by feature instead of introducing duplicate implementations.

## Main path

- [inspect_registry.py](inspect_registry.py): inspect input mapping before running.
- [orchestrate.py](orchestrate.py): phases, gates, subprocesses, and final output.
- [run_extract.py](run_extract.py): collect and apply the [rule engine](md_core/README.md).
- [capture_shots.py](capture_shots.py): original capture, saved HTML and post-judgment annotation.
- [run_capture_parallel.py](run_capture_parallel.py): capture sharding/retry utility.
- [merge_llm.py](merge_llm.py): merge automatic final findings while preserving the rule baseline.
- [compare_registry.py](compare_registry.py): compare findings with input registration data.
- [finalize.py](finalize.py): inspection workbook.
- [location_map.py](location_map.py): field evidence map.

## API agents

See the [agent catalog](../agents/README.md) for prompts, schemas, validation, artifacts, CLI examples, and [unit costs](../COSTS.md). The actual implementations are `llm_extract.py`, `diagnose_site.py`, `verify_pubdate.py`, `resolve_failed.py`, `classify_i3.py`, and `inspect_run.py` in this folder.

## Supporting utilities

`fingerprint_sites.py`, `cluster_sites.py`, and `report_clusters.py` support site grouping. `build_master_xlsx.py`, `build_share_xlsx.py`, and `build_review_sample.py` support report/sample packaging. `attach_shots.py`, `emit_overrides.py`, `recover_resolved.py`, and `repair_pubdate_audit.py` retain evidence/recovery tooling. `run_all.py` and `fetch_masthead.py` are inherited entry points; use the main pipeline guide for the recommended archive flow.
