# Layout archetypes (Step 5)

The layout pass no longer assumes every game happens on a ground plane. It
selects a **spatial primitive** from the schema first, then lays the entity
groups out inside it.

```
schema ──► select_archetype() ──► (motion_axis[/direction], medium, goal_topology)
                                        │
    ┌──────────────┬───────────┬────────┴──────┬──────────────┬──────────────┐
 vertical       vertical   volumetric    planar+ground   static+screen   anything
 /descent       /ascent    +air/water          │        (declared, not      else
 +air/water        │            │              │          derived)           │
    │              │            │              │              │              │
VerticalCorridor VerticalSurface BoundedVolume ArenaPlane  UISurface  [layout.strict]
```

| file | role |
| --- | --- |
| `src/unity_layout_archetype.py` | derives the three axes, maps them to an archetype, abstains when nothing fits; resolves every primitive's height through one tiered rule; owns the UISurface role vocabulary (`ui_element_of` / `ui_board_layer` / `ui_board_rank`) |
| `src/unity_layout.py` | `compute_layout()` dispatches; `_layout_arena_plane()`, `_layout_vertical_corridor()`, `_layout_bounded_volume()`, `_layout_vertical_surface()` and `_layout_ui_board()` are the primitives |
| `src/unity_spatial_facts.py` | extracts the brief's numeric distances before schema-fill and carries them past it in `schema.meta` |
| `src/unity_schema.py` | `is_embodied()` — the one predicate every pass reads the `locomotion='none'` declaration through |

## The derived axes

| axis | values | derived from |
| --- | --- | --- |
| `motion_axis` | `static` / `vertical` / `volumetric` / `planar` / `path` | **`locomotion='none'` first**, then rail wording, descent vocabulary, **unambiguous climbing nouns or climbable entities**, entity altitude data, free-medium locomotion |
| `direction` | `ascent` / `descent` (vertical only) | `locomotion='climb'` → then strong vocabulary → then entity tags (`Holdable` → ascent, exit platform → descent) |
| `medium` | `screen` / `air` / `water` / `ground` | **`locomotion='none'` first**, then `locomotion`, then air/water vocabulary |
| `goal_topology` | `reach_point` / `survive_area` / `hit_target` / `traverse` | `MissionObjective.type` first, then goal vocabulary |

`goal_topology` is provenance only — the archetype is `(motion_axis, medium)`.

`static` + `screen` is the one pair with no evidence behind it. Both axes fall
out of a single declaration — `locomotion='none'`, the schema saying it has no
avatar — and that check runs **before every vocabulary tier**, the rail test
included. It has to: this is the one game shape whose prose is systematically
about something other than space. A game that teaches version control says
branch, tree, cloud, push, merge and drop in every paragraph, and none of them
is a place. Scored as evidence they hand a game with no body an air medium and
a descent axis, which is not a wrong number but the wrong primitive.

Vertical evidence is deliberately **strong-only**, and **ascent and descent
clear the same bar**. That is the settled rule: a climb is as much a vertical
game as a drop is, and holding climbing evidence to a higher standard is what
produced a flat arena for the living-wall brief. Any one of:

- one unambiguous **descent** term (`freefall`, `skydive`, `parachute`,
  `wingsuit`, `altimeter`, …);
- one unambiguous **climbing** term (`handhold`, `belay`, `piton`, `rock face`,
  `free solo`, …), or a group authored as something you grab (`Holdable` tag,
  `handhold`/`ledge`/`grip` kind) — structure, so stronger still;
- two weaker descent verbs in a scene already established as air/water;
- entity y-data above 50 m.

What clears it in **neither** direction is a single ambiguous verb. A lone
"descent into the catacombs" never flips a ground game, and neither does "climb
the leaderboard" or "sales are climbing": the ascent **verbs** choose a
direction once the axis is already vertical and never establish the axis
themselves. That noun/verb split is the whole guard against prose-only false
positives — misclassifying a game that packs fine today would hard-fail it.

`volumetric` is the free-medium axis: `locomotion` of `fly`/`swim` (or
hover/float vocabulary, which the `locomotion` enum cannot express) **and** a
medium of air or water **and** no descent evidence. Descent is checked first,
so a skydiver — also `locomotion: fly` — still gets a corridor. The medium gate
matters: a *walking* game merely described in sky words is not volumetric, and
floating it in a box would be the same wrong-primitive failure.

## Height evidence — one rule for every primitive

Three primitives need a vertical extent, and all three resolve it through the
same tiered rule (`_schema_height` in `src/unity_layout_archetype.py`):

| tier | source | note |
| --- | --- | --- |
| 1 | the **schema's own** figure — baked entity altitude, else an altitude/deck reading parsed from its prose | authored intent; unchanged behaviour |
| 2 | a **spatial fact from the brief** whose referent names this dimension | largest match wins; logged `[layout.facts]` |
| 3 | the primitive's **named default** | logged as a warning, unchanged |

Tier 2 exists because the LLM is the only transport between the brief and the
compiler, and it compresses. The living-wall brief says *"The summit is a nerve
cluster 900m up"*; the schema that came back carried no height at all, so a
60 m default wall was built for a 900 m climb. Layout behaved correctly with
what reached it — the number never got there.

So `src/unity_spatial_facts.py` reads the brief itself, **before** schema-fill
runs, with a deterministic regex pass: a number carrying a length unit (`m`,
`meter(s)`, `km`, `ft`, `feet`), normalized to metres, plus the few tokens of
noun phrase around it and the raw sentence as evidence. The unit is the whole
gate — `3 lives` and `5 enemies` are not distances. The facts ride in
`schema.meta.spatial_facts`, which is provenance rather than content: no LLM
step writes it, the entity rescue does not touch it, and it survives into the
generator's model-aware re-layout so the second pass resolves the same height
as the first.

A fact is only consulted for a dimension whose **referent vocabulary** it
matches, so one brief can state a wall height, an approach distance and a fog
radius without them being interchangeable:

| dimension | referents |
| --- | --- |
| `surface_height` | summit, wall, cliff, peak, top |
| `corridor_height` | altitude, jump, deck, drop |
| `volume_height` | depth, ceiling |

When tiers 1 and 2 **both** have an answer and they differ by more than 25 %,
tier 1 still wins and the gap is logged — that is a schema-fill fidelity
signal, not an error.

## VerticalCorridor

- Corridor height **H** comes from baked entity altitude, else the highest
  altitude/deck figure in the schema's prose, else a matching brief spatial
  fact, else the named default (`_CORRIDOR_HEIGHT_DEFAULT`, 4000 m) **with a
  warning**.
- The **exit platform** — the thing the player jumps from — takes **y = H**,
  and the player spawn is attached beside it, clear of its footprint. Without
  the role, footprint-size stratification parked the jump ship *below* its own
  jumper. Detected in priority order:
  1. **relation** — the schema states it ("the player exits a Cessna Caravan")
     and the phrase matches an entity's name or model hint
  2. **kind** — a `vehicle`/`aircraft` in a vertical+air layout
  3. **vocabulary** — plane / cessna / helicopter / balloon / platform / …

  At most one entity ever takes the role, and the winning signal is logged. No
  platform is a fine outcome: the player just spawns unattached at the top.
- Non-player groups are stratified across `[0.15, 0.85] * H` — below the exit
  platform — or keep an altitude their own text pins them to. Nothing lands at
  y = 0 by default.
- Groups are parked one golden-angle step apart, offset from the drop axis by
  their own footprint radius, so the player falls **past** content.
- The terminal plane is the ground (y = 0), or a water **surface** at y = 0
  when `medium == "water"` — in which case the things that live in the water
  hang below it.
- The goal/landing entity is pinned to the terminal plane under the drop axis.

## VerticalSurface

Ascent is not descent upside-down: a drop is a **volume** you fall through, a
climb is a **surface** you move along with everything attached to it. The wall
is the X–Y plane at z = 0, face normal +Z; the player stands in front of it.

- Height **H** from schema evidence, else a brief spatial fact naming the
  summit/wall/cliff/peak/top, else `_SURFACE_HEIGHT_DEFAULT` (60 m) **with a
  warning**.
- **Wall surface entity**: a `structure`/`creature`/`terrain` group tagged
  `Environment`, else the largest wall-like group by footprint. The winning
  signal is logged.
- Player spawns in a **base zone** on the ground, `_SURFACE_BASE_DEPTH` out
  from the face.
- **Holdable/Climbable** groups form ONE chain across the face from base to
  summit — round-robined across every hold group, so the reachable path is
  built from all of them.
- **Reachability is an invariant, not a warning.** Every successive pair of
  holds is within `reach_distance` (default 2.5 m, override via
  `schema.meta.reach_distance`). The chain zigzags, so the budget is spent on
  both axes: the vertical step takes `_REACH_VERTICAL_FRACTION` and the
  sideways swing takes what is left, keeping the 3-D gap inside reach rather
  than only its vertical component. `_verify_hold_chain()` runs on the final
  geometry **after** hazards and raises `[layout.strict]` if any gap exceeds
  reach. If the hold budget cannot chain the full height, the climb is
  shortened to the reachable summit and logged — never left with a gap.
- **Hazard/Enemy** groups attach to the face between hold rows, pushed clear
  of every hold by `_HAZARD_HOLD_CLEARANCE`; they never count toward the chain.
- A named goal entity goes to the summit; otherwise the top edge is the goal
  region, and that is logged.
- Everything else packs on the ground plane around the base with the existing
  footprint packer.

## BoundedVolume

A play box of height **H** for free movement through a medium — a glider, a
flight sim, a scuba survey. Sized like the corridor (schema evidence, else a
brief spatial fact naming a depth/ceiling, else `_VOLUME_HEIGHT_DEFAULT` =
200 m **with a warning** — a box is not a jump run).

- Interior content is cut into `_VOLUME_LAYERS` (3) altitude layers across
  `[0.2, 0.8] * H`, size-sorted and round-robined so no layer holds all the
  bulk. Each layer is packed by the **existing** 2D footprint packer
  (`_pack_groups`) — groups in different layers may share XZ, because they are
  separated vertically. That is what makes it a volume rather than a plane.
- `medium == "air"`: the floor at y = 0 is terrain. Only ground-dwelling
  content sits on it. An entity that says it floats/hovers/drifts is never a
  ground dweller, whatever it is made of — a "drifting rock island" is
  airborne content.
- `medium == "water"`: the **top** of the box is the water surface. Only
  surface-marked content (boat / buoy / dock kinds or wording) rides it;
  the player and every swimming group hang below.
- The player spawns mid-box, never on a boundary, and its spawn **column** is
  reserved through every layer — in a volume the player can be at any altitude
  a moment later, so a group directly overhead is as much of a trap as one on
  top of them.

## ArenaPlane

Unchanged. The original circle packer plus its path-route corridor variant. A
freely driven racing circuit is planar+ground, **not** a `path` axis — routes
stay an ArenaPlane concern until the spline primitive lands.
`tests/fixtures/arenaplane_baseline.json` locks its output byte-for-byte
against the pre-archetype recording.

## UISurface

The board. Every primitive above answers *where does the player's body go and
what does it move through*; a non-embodied game (`locomotion='none'` — see
`unity_schema.is_embodied`) has no body, so those are not hard questions but
the wrong ones. Its content is an **interface**, and the player points at it.

- The board is the **X–Y plane at z = 0**, face normal +Z — the same convention
  as VerticalSurface, and where every flat camera preset already looks from
  `(0, 0, -10)`. The block is centred on the origin so the camera frames it
  without the generator having to aim.
- Content flows in **reading order**: left to right, top to bottom, in aligned
  rows with one constant gutter. Ordered by ROLE (`ui_board_rank` — the
  workspace, then panels and lists, then cards and buttons, then icons), with
  schema order breaking ties. Deliberately **not** by size: `_pack_groups`
  sorts biggest-first so the largest footprints claim the centre, which is
  right for a world and wrong for an interface, where position carries meaning.
- Inside a cell, every group gets the **same** reading-order sub-grid, and the
  schema's `placement` is ignored. Twelve commits laid out as a jittered
  scatter read as debris, not as history.
- Instance pitch comes from the element's **own size**, not `_effective_spacing`
  — that floors everything at `EntitySpec.spacing` (2.0 m, a gap between props
  a player walks between) and made a 3-unit terminal window narrower than a
  nine-icon grid of 0.6-unit files.
- **Three z-planes** keep it legible: backdrops behind (z = +2), content on the
  board (z = 0), captions and legends in front (z = −1). `ui_board_layer`
  decides which.
- 2D is the one exception to `compute_layout`'s "2D projects build their own
  course at spawn time": both spawn-time builders arrange content around a
  PLAYER, so for a game with no avatar they arrange the board around something
  that is not there. Every **embodied** 2D project still returns `[]`,
  byte-identically.

The rest of the archetype lives outside this pass and shares one vocabulary
with it (`ui_element_of` in `src/unity_layout_archetype.py`), so a group laid
out as a panel is also built as one:

| pass | what changes |
| --- | --- |
| compose (`subsystems_for`) | `pointer` controller on the systems holder, `board_framing` camera, `pointer` input, `click_zone` trigger, `button_switch` + pointer-detection interactable, `score_panel` HUD. Every unit targeting `"player"` is dropped — that target is what makes the scene template build the avatar. |
| assets (`resolve_entity_assets`) | Tier U routes controls to `ui:<element>`, built by `EntitySpawner.BuildUIElement` rather than matched to a model. The library holds 15 HUD glyphs and no UI kit, so a "commit" can only resolve to the wrong object. Unmatched content becomes a labelled card instead of a strict hard-fail. |
| fixer (`_check_consistency`) | no `player`-type script injected — that script is what makes `scene.unity.j2` build the capsule. |
| Step 7 (`unity_playability`) | nine avatar checks abstain; `pointer_targets` (ERROR) takes their place, and `scene_population` stops reading a board of `kind="prop"` controls as scenery. |

## Abstain

Any other pair raises `LayoutStrictError` (`[layout.strict]`, a `ValueError`)
naming the unmatched axes, the evidence behind each one, and a concrete fix.
It is never caught and never falls back to a ground plane. Adding BoundedVolume
did not soften this, and neither did adding UISurface: `planar+air` /
`planar+water` (a walker in a world merely *described* as sky or sea),
`vertical+ground` (shafts) and every `path` pairing still abstain. Shaft and
spline primitives do not exist yet.

UISurface in particular widened nothing. `static` is returned by exactly one
branch and `screen` by exactly one branch, both reading the same declaration,
so the pair is either matched outright or the game is embodied and every rule
above applies to it unchanged — there is no partial match to abstain on.
