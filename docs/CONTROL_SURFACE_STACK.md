# Control Surface Stack — multi-mode controller

A runtime-switchable controller: one player GameObject that can switch
between several locomotion modes, each with its own physics/input backing
store, instead of the `controller` micro-generator's usual one-variant-per-
project rule. Emitted as `ControllerVariant = "multi_mode"`
(`src/micro/controller.py`), its own template
(`templates/unity/micro/controller/ControllerMultiMode.cs.j2`).

This is Phase 1 of a multi-phase plan — see "Phasing" below. It ships exactly
two mode kinds. Everything here is strictly additive: a project that never
sets `locomotion_modes` compiles and composes exactly as it did before this
feature existed.

## How to opt in

Set `UnitySchema.locomotion_modes` to 2+ distinct kinds (currently
`"on_foot"` and `"vehicle_car"` — the full vocabulary, see below):

```json
"locomotion_modes": ["on_foot", "vehicle_car"]
```

Below 2 entries, `subsystems_for` (`src/unity_compose.py`) never enters the
multi_mode branch at all — a 0- or 1-entry list composes as an ordinary
single-variant project (see `_check_multi_mode_registry` in
`unity_playability.py`, which WARNs on a 1-entry registry rather than
silently no-opping). `UnitySchema.locomotion` (the scalar field) stays the
project's sole embodiment/layout signal, unchanged — set it to whichever mode
the player STARTS in (`"walk"` for an on_foot start, `"drive"` for a
vehicle_car start).

Standalone (no `--compose`): `python main.py unity-controller "..." --variant multi_mode`
also works, since `ControllerSchema.modes`/`default_mode` are ordinary schema
fields the Pipeline LLM can fill directly in Step 3 (see the
`CONCEPT_PROMPT`/`SCHEMA_PROMPT` few-shot in `src/micro/controller.py`).

## Mode vocabulary (`ControllerModeKind`)

| Phase | Kind | Notes |
|-------|------|-------|
| 1 (shipped) | `on_foot` | `CharacterController`-based; abilities: `jump`, `sprint` |
| 1 (shipped) | `vehicle_car` | 4-`WheelCollider` rig; abilities: `nitro` |
| 1b (planned) | `flying`, `swimming` | already share `on_foot`'s `_cc` backing store in the single-variant template |
| 2 (planned) | `zero_g_eva`, `boating` | delta off `flying`/`vehicle_hover` |
| out of scope | `skateboarding`, riding a mount | no template precedent (trick physics / needs a live cross-entity reference the composer has no mechanism for) |

Extending the vocabulary means updating, together: `ControllerModeKind` +
`_MODE_FAMILY` (`src/micro/controller.py`), `UnitySchema.
enforce_locomotion_modes`'s `valid` tuple (`src/unity_schema.py` — kept as a
literal there on purpose, not imported, to avoid a schema → micro coupling),
a new mode macro in `_controller_modes.j2`, and the `has_<kind>` gates in
`ControllerMultiMode.cs.j2`.

## Architecture

- **One `Subsystem`, not many.** `subsystems_for` still emits exactly one
  `Subsystem(kind="controller", variant="multi_mode", ...)` — never one per
  mode. Multiplying same-kind `Subsystem`s would collide in `_pick()` (SEAM
  wiring), in `MicrogensSpec`'s kind-keyed round-trip
  (`src/unity_micro_code.py`), and in the `target="player"` singleton
  contract with the scene template. Plurality lives *inside* the schema
  (`ControllerSchema.modes: list[ControllerMode]`) instead.
- **Shared physical backing store.** `Awake()` adds every component every
  registered mode needs up front (`CharacterController` + `Rigidbody` +
  `WheelCollider[]`), all but the starting mode's dormant. `SwitchMode`
  toggles `_cc.enabled` / `_rb.isKinematic` / the vehicle visual root's
  `SetActive` — never re-instantiates anything. **No velocity/momentum
  carries across a switch** — `SwitchMode` zeroes it before applying the new
  mode. Modeling carryover (e.g. exiting a moving car at speed) is a later
  refinement, not a Phase 1 correctness requirement.
- **Macros, not a second copy.** `_controller_modes.j2` holds each mode's
  field/Awake/movement/build logic as Jinja macros, adapted from the matching
  block in the single-variant `Controller.cs.j2` (on_foot ≈ `third_person`,
  vehicle_car ≈ `vehicle_car`). **`Controller.cs.j2` itself is NOT yet
  refactored to call these same macros** — that mechanical, output-preserving
  refactor (verified by the existing 14-variant test suite) is deferred to a
  follow-up pass, since it adds no new capability on its own, only a
  drift-prevention guarantee. Until it lands, an edit to `vehicle_car`'s
  logic in `Controller.cs.j2` and an edit to `vehicle_car_movement` in
  `_controller_modes.j2` are two places, not one — keep them in step by hand.

## Known v1 limitations (named, not silent)

- **One spatial layout for the whole project.** `select_archetype`
  (`src/unity_layout_archetype.py`) and `_primitive_of`
  (`src/unity_playability.py`) are untouched — they keep reading the scalar
  `UnitySchema.locomotion` and pick ONE primitive for the whole project (the
  *starting* mode's). A driving mode operates inside whatever generic space
  the starting mode's archetype produced, not a purpose-built layout of its
  own. True per-zone/multi-primitive layout is a separate, much larger
  rewrite of that module's own contract.
- **Camera and animator are tuned for the starting mode only.**
  `subsystems_for`'s `anim_variant`/`cam` are still derived from the scalar
  `locomotion`, not from `locomotion_modes` — an on_foot-start project gets a
  humanoid animator and a follow camera that do not retune when the player
  switches into the car.
- **Keypress cycling, not contextual enter/exit.** Phase 1 switches modes via
  a configurable key (`mode_switch_key`, default `Tab`) cycling through the
  registry — not "walk up to the car and press F to get in." That needs
  `interactable`/`trigger` micro coupling the controller doesn't have.
- **Explicit declaration only.** `locomotion_modes` is populated by Step 3
  schema-fill (or hand-authored test fixtures), never inferred from prompt
  keywords — `unity_archetypes.classify()`'s locomotion scorer stays
  single-winner, unchanged.

## Phasing

- **Phase 1 (shipped):** `on_foot` + `vehicle_car`.
- **Phase 1b:** fold in `flying`/`swimming` — both already share `on_foot`'s
  `_cc` backing store in the single-variant template, so this is mostly
  proving the macro pattern generalizes.
- **Phase 2:** `zero_g_eva` (delta off `flying`) and `boating` (delta off
  `vehicle_hover`'s raycast-spring pattern — also needs the `hover_*` param
  clamp block `default_fix` is currently missing, fixed as a prerequisite).
- **Explicitly out of scope:** `skateboarding` (no template precedent, new
  trick/momentum physics), riding a mount (needs a live cross-entity
  reference — the composer has no dependency-declaration mechanism between
  micros at all today), true per-zone spatial layout, per-mode camera/
  animator retuning, contextual enter/exit, LLM auto-classification of "this
  prompt wants multi-mode."
