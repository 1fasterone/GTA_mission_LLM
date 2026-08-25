# Control Surface Stack — multi-mode controller

A runtime-switchable controller: one player GameObject that can switch
between several locomotion modes, each with its own physics/input backing
store, instead of the `controller` micro-generator's usual one-variant-per-
project rule. Emitted as `ControllerVariant = "multi_mode"`
(`src/micro/controller.py`), its own template
(`templates/unity/micro/controller/ControllerMultiMode.cs.j2`).

This is a multi-phase plan — see "Phasing" below. Phase 1 (`on_foot` +
`vehicle_car`) and Phase 1b's `flying` kind are shipped. Everything here is
strictly additive: a project that never sets `locomotion_modes` compiles and
composes exactly as it did before this feature existed.

## How to opt in

Set `UnitySchema.locomotion_modes` to 2+ distinct kinds (currently
`"on_foot"`, `"vehicle_car"`, `"flying"` — the full vocabulary, see below):

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
| 1b (shipped) | `flying` | `Rigidbody` pitch/roll/rudder/throttle rig ported from the single-variant `aircraft` section — **not** the single-variant `flying` variant. See "Why `flying` isn't `on_foot`'s `_cc`" below. Abilities: `flaps`, `landing_gear` (pitch/roll/rudder/throttle are always on, not ability-gated). |
| 1b (remaining, planned) | `swimming` | still shares `on_foot`'s `_cc` backing store in the single-variant template |
| 2 (planned) | `zero_g_eva`, `boating` | delta off `flying`/`vehicle_hover` |
| out of scope | `skateboarding`, riding a mount | no template precedent (trick physics / needs a live cross-entity reference the composer has no mechanism for) |

### Why `flying` isn't `on_foot`'s `_cc`

The original plan for Phase 1b's `flying` kind was a simple ascend/descend
free-fly sharing `on_foot`'s `CharacterController`, mirroring the
single-variant `flying` variant. That has no room for flaps, a rudder, or
landing gear — a drivable aircraft/VTOL entity's brief asks for fixed-wing
controls, not a hovering drone. Shipped `flying` is instead a `Rigidbody`
rig ported from the single-variant **`aircraft`** section (pitch/roll/yaw/
throttle via `AddForce`), extended with two optional per-mode abilities.
It shares its `Rigidbody` with `vehicle_car` when both are registered — see
"Shared physical backing store" below for how `Awake()`/`ApplyMode()`
reconcile one Rigidbody wanting ground gravity and WheelColliders while the
other wants free flight and thrust.

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
  `vehicle_car` and `flying` share ONE `Rigidbody` (there is only ever one on
  the GameObject) rather than each owning their own — `ApplyMode` computes
  `isKinematic` from whichever of the two is active, and additionally
  toggles `useGravity`/`linearDamping` off/on around `flying` specifically
  (ground driving wants normal gravity, free flight doesn't). Mass is set
  once: `flying_awake` only assigns its own 2000f when no `vehicle_car`
  entry is present, deferring to the car's tuned 1200f otherwise — the
  physics micro's own mass (`unity_compose.py`'s `phys_opts`) makes the same
  choice, or the two `Awake()`s race for `Rigidbody.mass` exactly like the
  vehicle-only case this pattern was already guarding against.
- **Macros, not a second copy.** `_controller_modes.j2` holds each mode's
  field/Awake/movement/build logic as Jinja macros, adapted from the matching
  block in the single-variant `Controller.cs.j2` (on_foot ≈ `third_person`,
  vehicle_car ≈ `vehicle_car`, flying ≈ `aircraft`). **`Controller.cs.j2`
  itself is NOT yet refactored to call these same macros** — that mechanical,
  output-preserving refactor (verified by the existing variant test suite) is
  deferred to a follow-up pass, since it adds no new capability on its own,
  only a drift-prevention guarantee. Until it lands, an edit to a mode's
  logic in `Controller.cs.j2` and an edit to its matching macro in
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
- ~~Explicit declaration only~~ — **now also deterministically inferred.**
  `locomotion_modes` was originally Step 3-only, confirmed empirically to not
  reliably get set even with explicit schema-prompt guidance (VectorCore
  stress prompt, 2 different local models, 4 straight runs, 0 detections).
  `unity_archetypes.classify()`/`apply_intent()` now also detect it
  deterministically — a runtime-switch-signal phrase table AND 2+ distinct
  `ControllerModeKind`s each independently clearing the same confidence bar
  every other axis uses (`_score_multi_mode`). FILL-only, never override: it
  only ever writes `schema.locomotion_modes` when Step 3 left it empty, so an
  author's explicit registry (even a single entry) always wins. See
  "Multi-mode detection" below.

## Multi-mode detection (`unity_archetypes.py`)

Mirrors the `game_type`/`locomotion`/`genre` keyword-classifier backstop
that already existed for every other schema axis, extended to the one
field this doc used to call LLM-only:

- `_MULTI_MODE_SWITCH_SIGNAL` — phrases like "switch between", "mode
  manager", "get in and out of" that signal RUNTIME switching specifically,
  not just a brief that happens to mention more than one body in passing.
- `_MULTI_MODE_KIND_TABLE` — per-kind keyword lists for `on_foot` (new),
  `vehicle_car`/`flying` (reused from `_LOCOMOTION`'s own `drive`/`fly`
  entries, no second copy to drift out of sync).
- `_score_multi_mode` gates on BOTH halves independently (conjunctive, not
  additive keyword volume) — a racing-game brief whose flavour text says
  "hop out and check the engine" once must not become a Control Surface
  Stack project on that alone.
- Reaches the LLM two ways before the deterministic fill ever runs:
  `concept_hint()` plants it in Step 2's prose (Step 3 only ever sees the
  concept, never the raw brief — same reasoning as `locomotion="none"`'s
  concept-hint line), and `structure_hint()` emits a Step-3 schema-prompt
  MUST line. `apply_intent()` is the guaranteed backstop regardless of
  whether the LLM acts on either hint.

## Phasing

- **Phase 1 (shipped):** `on_foot` + `vehicle_car`.
- **Phase 1b (`flying` shipped, `swimming` remaining):** `flying` is a
  `Rigidbody` pitch/roll/rudder/throttle rig ported from the single-variant
  `aircraft` section, not the CC-based free-fly originally sketched for this
  phase — see "Why `flying` isn't `on_foot`'s `_cc`" above. `swimming` still
  shares `on_foot`'s `_cc` backing store in the single-variant template, so
  that piece is mostly proving the macro pattern generalizes a second way.
- **Phase 2:** `zero_g_eva` (delta off `flying`) and `boating` (delta off
  `vehicle_hover`'s raycast-spring pattern — also needs the `hover_*` param
  clamp block `default_fix` is currently missing, fixed as a prerequisite).
- **Explicitly out of scope:** `skateboarding` (no template precedent, new
  trick/momentum physics), riding a mount (needs a live cross-entity
  reference — the composer has no dependency-declaration mechanism between
  micros at all today), true per-zone spatial layout, per-mode camera/
  animator retuning, contextual enter/exit. (Deterministic keyword
  classification of "this prompt wants multi-mode" — the other kind of
  auto-classification — shipped; see "Multi-mode detection" above.)
