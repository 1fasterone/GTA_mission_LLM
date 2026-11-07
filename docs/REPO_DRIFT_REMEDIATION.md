# Repository drift remediation

Status reviewed: 2026-11-06

This table tracks implemented roadmap mechanisms. It does not mean every broad
roadmap exit criterion is universally proven. In particular, authority is
documented by domain rather than for every concrete module, and intent-ledger
diffs do not yet provide a per-write origin for every deterministic schema
mutation. See `docs/PHASE3_SCHEMA_AUTHORITY.md`.

| Roadmap phase | Implemented evidence |
| --- | --- |
| 0 Stabilization | protected-main policy, generated-C# workflow, real Unity workflow, RFC-0001 |
| 1 Observability | append-only intent ledger, six pipeline snapshots, prompt truncation warning/report |
| 2 Intent authority | claim producers, one resolver, explicit exclusion claims |
| 3 Schema-mutating sidecars | projectile intent moved to claims; schema materialization isolated; asset binding detached; direct and staged validators guarded |
| 4 Decomposition | seven named stage classes and single-execution registry; one resolver and reconciliation pass |
| 5 Consolidation | Compose Micros default, `PrimitiveProvider`, `AssetBinder` |
| 6 Mechanic registry | Fire, Projectile, and Health pilot registrations |
| 7 Evaluation | 30 prompt goldens in three cohorts and dated `eval_runs` history |
| 8 Hygiene | shared policy generator, restored installer, archived obsolete verification scripts |
| 9 Authority architecture | RFC enforcement in code, tests, CI, and execution provenance |

## Trace answer

For intent-controlled decisions in a generated schema, inspect
`meta.intent_ledger`:

1. `claims` names the value, source, confidence, and rationale.
2. `meta.intent_resolution.winners` names the canonical winner per field.
3. `snapshots` and `diffs` show when the schema changed.
4. `meta.mechanic_registry` maps enabled mechanics to runtime, generator,
   templates, and validators.
5. `meta.execution_report.warnings` exposes any prompt truncation.

This is the deterministic answer for resolver-owned intent. Other deterministic
compiler writes still require their stage-specific provenance until a universal
schema mutation journal exists.

Ledger checkpoints capture nested state by value. Registered claims, returned
resolutions and applied schema metadata have separate ownership, so later
asset binding can update the working schema without rewriting the evidence or
prior checkpoints. Ledger reports and diffs are detached exports; editing them
does not change recorded history. The persisted formats remain
`intent-ledger/v1` and `intent-resolution/v1`.
