# Atom/backend support and mechanic ownership audits (S0-S3)

The S0 support audit was integrated with main `c83646b240fb4222ac112777fee1761d656acca1`
after the #282 squash merge. S2 later made selected rendered C# source authoritative
for consumer-visible APIs. S3 adds structural mechanic connection and ownership
closure in [MECHANIC_OWNERSHIP.md](MECHANIC_OWNERSHIP.md). No gameplay
implementation, public vocabulary, or runtime owner is changed by S3.

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
python -m src.atoms.ownership_audit --check
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
twelve bound emission cases under `tests/fixtures/` are engineer-authored synthetic
fixtures for guarded seams. All other public lowerers use AtomDef defaults.
They are never presented as real Pipeline LLM output or actual scene bindings.

## Observed baseline

| Backend | Executable lowerers | Descriptive gaps | Unsupported definitions | Dispatch-only helpers |
| --- | ---: | ---: | ---: | ---: |
| Unity | 71 | 25 | 0 | 0 |
| GTA5 | 26 | 0 | 70 | 0 |

The Unity gaps are AimDownSights, Ascend, Block, Boost, Brake, CycleWeapon,
Dash, Descend, Dodge, Grab, Interact, MeleeAttack, Nitro, OpenInventory, OpenMap,
Pause, Pitch, PrimaryAttack, Reload, RollPlane, SecondaryAttack, Shoot,
Throttle, ThrowGrenade and UseItem. `fire` remains an alias of canonical `Shoot`.

## R1 vocabulary decisions (S0 follow-up closed)

R1 closed all 16 `S0-followup-*` baseline entries. No entry was closed by
re-labelling: each one either got a single owner, a registration backed by a
real lowerer, or retirement.

**Identity policy: one owner per token.** A token is an atom's canonical name or
alias, compared case-insensitively. `src/atoms/__init__.py` builds the lookup
index with `_build_alias_index`, which raises at import when two atoms claim one
token. First-registered-wins, which made a lookup depend on module order, is
gone. The audit still reports any collision as an unreviewed identity conflict.

| Token | Owner | Dropped from | Why | Lookup change |
| --- | --- | --- | --- | --- |
| pause | Pause | Wait | Canonical name outranks an alias; matches the controller `pause` ability and the `Pause` input action | `pause` was Wait, now Pause |
| roll_evade | Dodge | Dash | Evade family (`dodge`, `evade`, `sidestep`); Dash keeps `dash`, `quick_step` | was Dash, now Dodge |
| thrust | Throttle | ApplyForce | The controller's Thrust section is throttle-driven; ApplyForce keeps `push`, `add_force` | none |
| kick | SecondaryAttack | ApplyImpulse | The fighting variant defines kick as secondary_attack; ApplyImpulse keeps `impulse`, `knockback` | none |
| look_at | CameraLookAt | LookAt (dialog) | Already the camera's; the NPC head-look keeps `npc_look_at`, `head_look` | none |

Unity code generation dispatches on the raw atom name (`_SNIPPETS.get`), never
through aliases, so no generated C# changes. Alias lookups feed
`atom_snippet()`, the mechanics playtest's category label and the CLI only.

**Dispatch-only helpers: registered.** SayLine, PlayDialogAudio, Gesture, LookAt
and EndDialog already had `AtomDef`s (`src/atoms/dialog.py`) and real Unity
lowerers, but were never added to `ALL_ATOMS`. They are now registered as
`engines=["unity"]` in a new `dialog` category. `dialog` is not in
`_SEQUENCE_ATOM_CATEGORIES`, so the Step 3 prompt vocabulary is unchanged; their
callers remain the npc_dialog micro and its companion sequence.
WireTriggerToEscortObjective joins its registered `wiring` siblings with the
same parameters `MicroComposer` already passes. No new token was invented: each
name existed with a lowerer before R1.

**Unregistered capability names: retired.** InitDialogSystem, BarkOnSight,
EnableNavMesh, SetAIBehavior, EnableParticles and EnablePostProcessing were
built by `capability_resolver` and folded into `schema.atoms`. None was
registered or had a lowerer, so each reached GameSequencer as a `// TODO`
comment that read like scene setup but did nothing. Registering them would have
invented public vocabulary with no executable meaning, so they were retired.
Their capabilities still activate the dialogue, statemachine and trigger micros.
Params no lowerer consumes (`dialog.system`, `dialog.bark_on_sight`,
`ai.pathfinding`, `ai.behavior`, `ai.detection_range`, `vfx.particles`,
`vfx.post_processing`) are named in a Step 5 fix message rather than emitted as
placeholders. A saved schema that already holds one of these names still lowers
to the same `// TODO` comment on replay; schemas are never rewritten.

## Ownership and reachability

- Schema-level atoms lower into `GameSequencer.Start`, but S3 found no generated
  scene or prefab component attachment for `GameSequencer`; emission is therefore
  `connected_but_not_attached`, not an invoked runtime claim.
- The representative behavior-map entity route resolves a behavior class, renders
  `EntityBehavior`, attaches through `EntitySpawner`, and may restart its coroutine.
  S3 does not generalize that proof to a raw `EntitySpec.atoms` value whose behavior
  class was not deterministically selected. Custom-script atoms remain a separate
  generator path and do not replace micro gameplay.
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
| S0 vocabulary/alias review | Done in R1: one owner per token, dialog/escort helpers registered, six capability placeholders retired |
| S1–S2 | Extract selected rendered C# APIs with Roslyn and remove copied API tables |
| S3 | Structural representative closure implemented; preserve explicit unattached/missing-provider abstentions in `mechanic_ownership_inventory.json` |
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
python -m src.atoms.ownership_audit --write
python -m src.atoms.ownership_audit --check
python -m pytest -q tests/test_mechanic_ownership_audit.py
```

`--write` cannot approve new missing lowerers or identity conflicts. New debt,
unsupported backend handlers, duplicate definitions/aliases/dispatch keys,
comment-only or nondeterministic public lowerers, changed policy blocks and
unsupported runtime ownership claims fail. Closing a known gap requires removing
its stale baseline entry. Review the resulting inventory diff in the same PR.

Compilation and advancing runtime tests remain mandatory for later runtime
migrations. Missing tooling and skipped runtime tests remain **unverified**.
