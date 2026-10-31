# Repository drift remediation

Completed: 2026-10-30

| Roadmap phase | Implemented evidence |
| --- | --- |
| 0 Stabilization | protected-main policy, generated-C# workflow, real Unity workflow, RFC-0001 |
| 1 Observability | append-only intent ledger, six pipeline snapshots, prompt truncation warning/report |
| 2 Intent authority | claim producers, one resolver, explicit exclusion claims |
| 3 Validation boundary | read-only projectile validator and mutation guard |
| 4 Decomposition | seven named stage classes and single-execution registry; one resolver and reconciliation pass |
| 5 Consolidation | Compose Micros default, `PrimitiveProvider`, `AssetBinder` |
| 6 Mechanic registry | Fire, Projectile, and Health pilot registrations |
| 7 Evaluation | 30 prompt goldens in three cohorts and dated `eval_runs` history |
| 8 Hygiene | shared policy generator, restored installer, archived obsolete verification scripts |
| 9 Authority architecture | RFC enforcement in code, tests, CI, and execution provenance |

## Trace answer

For any generated schema, inspect `meta.intent_ledger`:

1. `claims` names the value, source, confidence, and rationale.
2. `meta.intent_resolution.winners` names the canonical winner per field.
3. `snapshots` and `diffs` show when the schema changed.
4. `meta.mechanic_registry` maps enabled mechanics to runtime, generator,
   templates, and validators.
5. `meta.execution_report.warnings` exposes any prompt truncation.

This is the deterministic answer to “Why did the generator produce this
game?” and is persisted with the schema rather than reconstructed from logs.
