# Repository drift remediation

Status reviewed: 2026-11-07

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

## Mechanics realignment status

Slices S0-S8 are defined in `docs/ATOM_BACKEND_SUPPORT.md` (migration queue).
Reviewed on main `1d75000`.

| Slice | State | Evidence |
| --- | --- | --- |
| S0 atom/backend support audit | Done | #283; `support_audit --check` passes (Unity 65 executable / 25 descriptive / 6 internal) |
| S0 vocabulary/alias review | Open | 16 baseline entries tagged `S0-followup-*` in `lookups/atoms/support_baseline.json` |
| S1 Roslyn API extraction | Done | #284, #285 |
| S2 consumer cutover | Done, closeout in R0 | `c239ac8`; copied API tables survived as dead code (see R0) |
| S3 ownership closure | Done | #289; 3 `connected_and_owned`, 2 `connected_but_not_attached`, 1 `missing_provider` |
| Phase 3 schema authority | Done | #290 |
| Untouched-brief preflight | Done | #291 |
| S4-S8 | Not started | 24 baseline entries name S6, 1 names S5 |

## Remaining plan

```mermaid
flowchart LR
  R0[R0 S2 closeout] --> R1[R1 S0 vocabulary decisions]
  R0 --> R2[R2 S4 typed bindings]
  R1 --> R2
  R2 --> R3[R3 S5 shared firing provider]
  R3 --> R4[R4 S6 controller migration]
  R2 --> R5[R5 S7 board]
  R5 --> R6[R6 S8 rules]
  R4 --> R7[R7 Acceptance and doc refresh]
  R6 --> R7
```

| Step | Slice | Scope | Exit criterion |
| --- | --- | --- | --- |
| R0 | S2 closeout | Remove the copied project API tables and every uncalled consumer built on them. Move the Step 3.5 correction checks (`_would_not_bind`, `_script_would_not_bind`) off the mutating template binder onto the extracted contract. Derive HUD setter availability (`hud_setters` / `_VARIANT_SETTERS`) from selected writer output. Replace the live generator collaborator erasers (invented namespace/stub/constructor/find filters, undeclared self-call/condition/foreach filters) with preserved source plus structured diagnostics. Decide `_MB_METHOD_ARITY` / `_MONO_INHERITED`: real Unity references or an explicit framework-only allowlist. | No copied project API table remains; S2 regression matrix and real Editor compile gate pass. |
| R1 | S0 vocabulary review | Decide the four identity collisions (pause/Pause, roll_evade, thrust, kick); register or retire the six dispatch-only dialog helpers and six unregistered capability names. | Every `S0-followup-*` baseline entry closed or carries a recorded decision. |
| R2 | S4 | Persist typed host/phase/trigger/provider bindings in schema meta with versioned replay. Attach or retire the two unattached routes (`GameSequencer`, `ControllerDefaultBehavior` companion). Validate dynamic atom references per build. | Ownership audit reports zero `connected_but_not_attached`; saved-schema replay reproduces bindings twice. |
| R3 | S5 | Unity `Shoot` lowerer through one firing provider shared by input and atom; ammo, cooldown and failure behavior proven. Rebase or retire `codex/projectile-slice4-firearm-adapter` here. | Zero `missing_provider`; Shoot leaves the baseline; advancing Editor runtime test. |
| R4 | S6 | Migrate the 24 baselined controller atoms one behavior per PR with per-variant advancing tests. Controller leaves `protected_template_owners` only when every variant is covered. | All S6 baseline entries closed. |
| R5 | S7 | Legal board operations (`TryPlace`, command parser) behind one atom-founded owner. | Board leaves `protected_template_owners`; SEAM 8a binds to the new owner. |
| R6 | S8 | Rules progression (`steps_from_missions`) behind the same single owner. | Rules leaves `protected_template_owners`. |
| R7 | Acceptance | Universal per-write schema mutation journal, or an explicitly documented scope limit; refresh this file and `ATOM_BACKEND_SUPPORT.md`; 20-30 case untouched-brief evaluation from a clean `unity-create-run-log/v2` run. | Exact clean-commit lineage plus Editor compile evidence. |

Every step branches from current `origin/main` and must pass ruff, mypy, full
pytest, `support_audit --check`, `ownership_audit --check`, generated C# and the
real Unity Editor compile gate. Runtime acceptance stays **unverified** until an
advancing Editor test exists for that slice.

Open decisions:

- `codex/recover-mechanics-playtest` (9 commits, September) applies
  deterministic schema and atom patches outside the resolver. Port it as
  resolver claims under the Phase 3 authority model, or archive it.
- Framework member tables in R0 (see above).
- R1 collision policy: canonical meaning per token, or document the current
  first-registered-wins behavior.

### R0 progress

First pass (branch `codex/r0-s2-closeout`):

- Removed `_COMPOSED_CLASS_API`, `_COMPOSED_METHOD_ARITY`,
  `_COMPOSED_CLASS_EVENTS`, `_ANIMATOR_DYNAMIC_MEMBER_RE` and the uncalled
  consumers built on them (`_make_composed_call_stripper`,
  `_sanitize_composed_component_calls`, `_sanitize_project_service_calls`,
  `_sanitize_field_typed_member_calls`, `_sanitize_param_typed_member_calls`,
  `_custom_script_api`, `sanitize_subsystem_composed_calls`). None had a
  production caller after `c239ac8`; only tests exercised them.
- Removed the other consumers S2 left uncalled: `_drop_unresolvable_types`,
  `_bind_script`, `sanitize_subsystem_invented_constructors`,
  `sanitize_subsystem_builtin_call_arity`, and the ignored `known_types`
  parameter of `unity_micro_code._apply` with its helper chain.
- Removed a duplicate `_PARAM_MODIFIERS` binding that silently shadowed the
  fuller set used by `_signature_type_names`; `readonly`/`scoped` parameter
  modifiers are now stripped as that function's own definition intended.
- Isolated the four token-less Sketchfab client tests from a developer
  `SKETCHFAB_API_TOKEN` (two had been passing only via a live network call).

Remaining R0 items: correction-check contract migration, HUD setter
derivation, live collaborator erasers, framework table decision.

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
