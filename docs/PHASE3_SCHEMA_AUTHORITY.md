# Phase 3 schema authority closure

Status: implemented on `codex/phase3-schema-authority`; acceptance still requires
the pull-request CI and review gates.

## Scope answers

| Question | Answer | Evidence and limit |
| --- | --- | --- |
| Does every system have a documented authority boundary? | Not literally every concrete module. | RFC-0001 documents the roadmap's authority domains. Mechanic-specific ownership is also inventoried, but that is not an exhaustive module-by-module authority map. |
| Does every schema change have a visible origin? | No, not universally. | The intent ledger records claims, winners, snapshots, and diffs for intent-controlled fields. Deterministic fixer, layout, composition, and asset-provenance writes are not yet a universal per-write mutation journal. |
| Do explicit exclusions survive pipeline execution? | Yes, for the declared closed vocabulary. | Bike, vehicle, aircraft, helicopter, and projectile exclusions are first-class claims, resolver inputs, persisted exclusions, and composition tests. This is not an arbitrary-language exclusion engine. |

These limits are intentionally explicit. Phase 3 closes projectile sidecar
mutation and validation writes; it does not retroactively make every compiler
mutation an intent decision.

## Workstream 3.1 — projectile contract conversion

The canonical path is now:

```text
untouched prompt
  -> src.intent.projectile produces a closed claim value
  -> ResolveIntentStage chooses the winner without schema writes
  -> GenerateSchemaStage materializes the resolved contract and view model
  -> unity_projectile_contract validates only
  -> unity_projectile_assets returns detached asset-binding data
  -> composition consumes that detached binding
```

Authority is split by artifact:

| Artifact | Owner | Forbidden action |
| --- | --- | --- |
| projectile intent evidence | `src/intent/projectile.py` | receive or mutate `UnitySchema` |
| canonical projectile schema contract | `GenerateSchemaStage` | reinterpret prompt text |
| projectile findings | `src/unity_projectile_contract.py` | repair, enrich, or mutate schema |
| resolved visual path | `src/unity_projectile_assets.py` plus `UnityProjectGenerator` | write the visual path back into canonical schema |

`presentation.resource_path` remains empty in the canonical intent contract.
The final Resources path is compiler-owned binding output held by
`UnityProjectGenerator.last_projectile_presentation` and handed directly to the
controller composer. Saved-schema replay follows the same path.

Exit criterion: **the projectile contract module performs validation only**.

## Workstream 3.2 — validation boundary

`src/schema_validation.py` is the execution boundary for schema validators.

- The canonical schema is never passed to validator code.
- Each validator receives a deep isolated copy.
- A before/after comparison raises `SchemaValidationMutationError` if the
  validator attempts any write, including nested writes.
- Decorated validators retain this protection when called directly, outside
  `ValidateSchemaStage`.
- `ValidateSchemaStage` applies the same guard to undecorated ad-hoc validators.

Validators may return findings and callers may warn or fail on those findings.
They cannot mutate the canonical object, add mechanics, or override resolved
intent.

Exit criterion: **no validation pass can change gameplay through schema writes**.

## S2 run-lineage discovery

Accepted S2 commit: `c239ac826ba62f9cb39a981679085e8032a56e36`.

New `unity-create-run-log/v2` manifests record:

- repository root, branch, and exact `HEAD` commit;
- clean versus locally modified working tree;
- ancestry relative to accepted S2;
- accepted-S2 tree equivalence for squash/cherry-pick lineages.

Run discovery with:

```powershell
.\.venv\Scripts\python.exe main.py unity-run-lineage --config config.yaml
```

The 2026-11-06 discovery found 16 historical v1 manifests and zero exact
source revisions. Every manifest predates the accepted S2 commit, so none can
represent a run of that accepted commit. The legacy evidence cannot distinguish
pre-S2 main from an uncommitted S2 candidate; those rows are correctly labeled
`pre_accepted_s2_commit_unverified` rather than guessed into either bucket.

Future v2 runs are exact when their recorded tree is clean. Dirty runs remain
visible as `*_with_local_changes` and are never presented as reproducible commit
evidence.

## Focused acceptance

The focused Phase 3 suite covers:

- resolver purity and schema-stage materialization;
- projectile contract validation-only ownership;
- detached projectile visual binding and saved-schema replay;
- direct and staged validator mutation attempts;
- explicit exclusions through resolution and composition;
- v1 bounded lineage discovery and v2 exact source evidence;
- durable run-log behavior.

The focused result before PR delivery was `104 passed`.
