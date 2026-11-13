# Failed-run diagnostics (`unity-failed-run/v1`)

When `UnityPipeline.run()` raises, the pipeline writes one structured report
before the exception propagates:

```text
<unity_pipeline.unity_output_dir>/_failed_runs/
  failed-run-<UTC stamp>-<prompt sha12>.json         # the report
  failed-run-<UTC stamp>-<prompt sha12>.schema.json  # live schema at failure, if any
```

Implementation: `src/unity_failed_run.py`. The Pydantic model
`FailedRunReport` is the schema (`extra="forbid"`);
`failed_run_json_schema()` exports it as JSON Schema.

## What it answers

| Question | Field |
| --- | --- |
| Where did it die? | `phase.failed_phase`, `phase.last_step`, `phase.last_checkpoint`, `phase.completed_stages` |
| Why? | `failure.category`, `failure.matched_exception`, `failure.remedy`, `failure.chain`, `failure.traceback` |
| LLM or compiler? | `llm_boundary.frozen_at_failure` — `true` means Step 3.5 had frozen the client, so the failure is in deterministic Steps 4-7 |
| Whose hand wrote the schema? | `authorship.schema_source` (`schema_llm` / `schema_deterministic`), `authorship.concept_source` (`llm` / `precomputed` / `none`) |
| What did the model actually return? | `fill_attempts[]` — every raw Step 3 response with its rejection reason (cut at 200 000 chars, `raw_truncated` records the cut) |
| Can I replay without the LLM? | `schema_artifact.path` + `replay_command` (only when `frozen_at_failure`) |
| Which code produced this? | `source_lineage` (same capture as the run log) |

Also carried: the exact prompt and its SHA-256, the call options, the concept,
every `WARN` line, and the full `intent-ledger/v1` export.

## Phases

The phase is the one entered after the last intent-ledger checkpoint:

| Last checkpoint | `failed_phase` |
| --- | --- |
| none | setup (before Step 2) |
| `Pre-Step3` | Steps 2-3 (concept + schema fill) |
| `Post-Step3` | Step 3 intent resolution |
| `Post-Resolution`, not frozen | Step 3.5 enrichment |
| `Post-Resolution`, frozen | Steps 4-5 (resolve + fixer) |
| `Post-Reconciliation` | Step 5 validation, asset binding and compose |
| `Post-Compose` | Step 5 pre-generation |
| `Pre-Generation` | Steps 6-7 (generation + playability) |

## Categories

Deterministic, table-ordered (`_CATEGORY_RULES`). Each link of the
`__cause__`/`__context__` chain is tried outermost first against every class in
its MRO, so `RuntimeError("spatial sidecar freeze failed")` raised from a
`SpatialPipelineError` is `spatial_layout`. A Step 3 fill whose every attempt
was rejected is `schema_fill`.

`llm_freeze_violation`, `csharp_syntax_gate`, `playability_gate`,
`spatial_layout`, `authored_scene`, `brief_parse`, `reference_image`,
`visual_compiler`, `schema_validation`, `llm_transport`, `timeout`,
`interrupted`, `resources`, `filesystem`, `schema_fill`.

No match is `unclassified` with a remedy that says so — the report abstains
loudly instead of guessing. Add a rule when a recurring failure's remedy is
known.

## Contract

- Review provenance only, like `unity-create-run-log/v2`
  (docs/UNITY_RUN_LOGS.md). Never placed in the schema, never supplied to the
  Pipeline LLM, never read by Steps 4-7. `RunDiagnostics` holds references and
  is written to only by observation hooks.
- Best-effort: a report that cannot be written prints
  `FAILED-RUN DIAGNOSTICS ERROR` and the original exception still propagates
  unchanged. A schema that cannot be dumped is recorded in
  `schema_artifact.dump_error`.
- The run-log manifest links the report as `failed_run_report` (parsed from the
  `Failed-run diagnostics: <path>` console line).
- Disable with `diagnostics.failed_runs: false` in config.yaml.
- Reports contain the full prompt; review before sharing.
- Eval Discipline: the saved schema is pipeline output only when
  `authorship.schema_source` says so. A `schema_deterministic` report proves
  nothing about Step 3.
