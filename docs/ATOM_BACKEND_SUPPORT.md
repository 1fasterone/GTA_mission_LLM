# Atom/backend support audit (S0)

This audit was integrated with main `c83646b240fb4222ac112777fee1761d656acca1`
after the #282 squash merge. The inventory is regenerated from this integrated tree.
No gameplay implementation or vocabulary is changed by this slice.

## Branch readiness

Integration status checked November 4, 2026:

| PR | Work | Audit dependency | Disposition |
| --- | --- | --- | --- |
| #279 | Persist composed controller/micro selection | Required for later replay integration, not S0 inventory | Leave separate draft open |
| #280 | Complete Step 3 brief and beta startup | Independent of dispatch/ownership inventory | Leave separate draft open |
| #281 | Phase 0 authority and compilation infrastructure | Independent of S0; overlaps #282 | Leave separate draft open |
| #282 | Intent authority, registry pilot and infrastructure | Integrated prerequisite for current main | Merged as `c83646b`; shared-policy generation preserved |

PR #282 is merged. Its validated tree passed the real Unity Editor compile gate
(run `41793840269`); earlier missing-license reports are historical. Unity
credentials are configured. This S0 integration still requires its own Python,
generated-C# and Unity CI results; the earlier run is not evidence for this head.
The generated shared policy remains owned by
`docs/policies/shared-agent-guidance.md` and `tools/sync_agent_guidance.py`.
This slice does not change review requirements, protection, secrets or gates.

## Authority and meaning

Run from the repository root:

```sh
python -m src.atoms.support_audit --check
```

The command inspects actual atom definitions, aliases, backend dispatch tables,
all registered micro variants, and the standalone/composed call paths. It emits
no pipeline schema and invokes no LLM. The checked-in machine-readable result is
[atom_backend_inventory.json](atom_backend_inventory.json). CI runs the command
and the mutation tests in `tests/test_atom_support_audit.py`.

| Classification | Meaning | What it does not establish |
| --- | --- | --- |
| executable | Registered backend lowerer emits deterministic statements for the emission fixture | C# binding, correct host, physics timing, or runtime behavior |
| descriptive | Definition advertises this backend but no lowerer exists | Execution through a different template is not execution of this atom |
| unsupported | Backend excluded by AtomDef.engines | No inference from another backend's implementation |
| internal | Dispatch-only helper without a public AtomDef | Not new public vocabulary or proof of schema reachability |

`runtime_acceptance` is **unverified** throughout. This is a structural and
emission audit, not semantic compilation or Unity/GTA runtime acceptance. The
six bound emission cases under `tests/fixtures/` are engineer-authored synthetic
fixtures for guarded seams. All other public lowerers use AtomDef defaults.
They are never presented as real Pipeline LLM output or actual scene bindings.

## Observed baseline

| Backend | Executable lowerers | Descriptive gaps | Unsupported definitions | Dispatch-only helpers |
| --- | ---: | ---: | ---: | ---: |
| Unity | 65 | 25 | 0 | 6 |
| GTA5 | 26 | 0 | 64 | 0 |

The Unity gaps are AimDownSights, Ascend, Block, Boost, Brake, CycleWeapon,
Dash, Descend, Dodge, Grab, Interact, MeleeAttack, Nitro, OpenInventory, OpenMap,
Pause, Pitch, PrimaryAttack, Reload, RollPlane, SecondaryAttack, Shoot,
Throttle, ThrowGrenade and UseItem. `fire` remains an alias of canonical `Shoot`.

The capability resolver also constructs six unregistered names with no lowerers:
BarkOnSight, EnableNavMesh, EnableParticles, EnablePostProcessing,
InitDialogSystem and SetAIBehavior. Their static source locations and those of
all discoverable atom invocations are in the inventory. They are separately
baselined as unregistered reference debt; they are not counted as public atoms.
Dynamic references still require per-build validation in S4.

The dispatch-only helpers are EndDialog, Gesture, LookAt, PlayDialogAudio,
SayLine and WireTriggerToEscortObjective. The escort helper is directly called
by MicroComposer. The dialog helpers have no public AtomDef; four appear in the
descriptive npc_dialog map. Public registration or removal needs an explicit
vocabulary compatibility decision. They are not automatically exposed.

Four identity collisions are recorded without silently changing precedence:

| Token | Competing definitions | Current lookup behavior |
| --- | --- | --- |
| pause / Pause | Wait, Pause | lowercase alias resolves Wait; exact canonical Pause resolves Pause |
| roll_evade | Dash, Dodge | first registered alias wins: Dash |
| thrust | Throttle, ApplyForce | first registered alias wins: Throttle |
| kick | SecondaryAttack, ApplyImpulse | first registered alias wins: SecondaryAttack |

## Ownership and reachability

- Schema-level atoms run once through GameSequencer.Start's coroutine. Entity
  atom scripts are attached through EntitySpawner and may restart their coroutine.
  Custom-script atoms are an additional generator path; this audit makes no
  claim that they replace micro gameplay.
- Unity wiring atoms are explicitly called by MicroComposer to emit subscriptions,
  Update bridges and event handlers into GameWiring. These are the existing
  executable composition seams, not a universal lifecycle binding system.
- Unity standalone `_after_write` can emit default atom companion scripts.
  CameraRig suppresses this hook to retain sole camera ownership. Composed
  generation calls `write_files` directly and does not invoke that hook.
  A written Unity companion is not proof of attachment.
- GTA5 standalone companions run from a Script.Tick guard, optionally entering
  a yielding loop. This does not prove inclusion in the minigame skeleton.
- The inventory enumerates every registered Unity/GTA micro and all its variants.
  Controller (including pointer/multi_mode), board, rules and projectile gameplay
  remain template-owned. ModelPipeline is an asset pipeline, not a gameplay
  provider. No movement, firing, physics or avatar ownership changes here.

## Migration queue and compatibility

[The reviewed debt baseline](../lookups/atoms/support_baseline.json) gives every
missing lowerer, dispatch-only helper, unregistered reference and identity collision a reason and next
slice. It is a ratchet, not a second public API or implementation table.

| Follow-up | Required change before claiming completion |
| --- | --- |
| S0 vocabulary/alias review | Decide compatible resolution for four collisions and five legacy dialog helpers |
| S1–S2 | Extract selected rendered C# APIs with Roslyn and remove copied API tables |
| S3 | Validate mechanic descriptor connection and ownership closure |
| S4 | Persist typed host/phase/trigger/provider bindings with versioned replay |
| S5 | Share one firing provider between input and Shoot; prove ammo/cooldown and failure behavior |
| S6 | Migrate controller behaviors one at a time, with per-variant advancing tests |
| S7–S8 | Migrate legal board operations and rules progression through one owner |

The audit does not alter schemas, atom names, aliases, emitted C#, GUIDs or scene
identities. Reverting this slice removes the audit and its documentation/CI check;
there is no second active runtime implementation to disable.

## Updating the inventory

```sh
python -m src.atoms.support_audit --write
python -m src.atoms.support_audit --check
python -m pytest -q tests/test_atom_support_audit.py
```

`--write` cannot approve new missing lowerers or identity conflicts. New debt,
unsupported backend handlers, duplicate definitions/aliases/dispatch keys,
comment-only or nondeterministic public lowerers, changed policy blocks and
unsupported runtime ownership claims fail. Closing a known gap requires removing
its stale baseline entry. Review the resulting inventory diff in the same PR.

Compilation and advancing runtime tests remain mandatory for later runtime
migrations. Missing tooling and skipped runtime tests remain **unverified**.
