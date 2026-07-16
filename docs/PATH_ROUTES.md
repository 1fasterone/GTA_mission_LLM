# Path Routes (P1–P64): Layout Plan

Status: **P1 normalization + corridor packing + route authoring implemented**
(`src/unity_pathnorm.py`, `src/unity_layout.py`, `src/unity_pathinput.py`,
`unity-path` / `unity-create --route` in `main.py`); everything under
"Planned" below is design, not code.

A path route is `EntitySpec.placement == "path"` with up to 64 points
(`path_points`, each `[x, y, z]` or `[x, y, z, bank°]`). This document fixes the
conventions every consumer must share, and plans how routes drive AI/player
movement, prop placement, checkpoints, triggers, audio cues and spawn points —
from text prompts and (later) images — without re-creating the physical
pile-ups the layout pass now prevents.

---

## 1. Invariants (implemented)

These hold after pipeline Step 5 (Fixer) for every 3D schema with routes:

1. **P1 is the start line.** `path_points[0]` of the *primary route* is the
   start/finish line. Closed loops (`first == last`) race through it; open
   routes begin at it.
2. **P1 is at the world origin, heading +Z.** `normalize_paths()` applies one
   scene-level rigid transform (XZ translation + yaw about Y) anchored to the
   primary route. Primary = widest `path_width` first (the drivable ribbon —
   barrier rails are tiled too but only ~1 m wide), then longest XZ arc,
   name tie-break. Everything world-space moves together:
   all `path_points`, pre-baked `instance_positions`/`instance_yaws`,
   measured `origin_offset`s, `WorldRegion.bounds`. Relative geometry is
   untouched; only the anchor changes. Idempotent; 2D skipped.
3. **Direction at P1**: closed loops use the tangent *arriving* at P1 (the
   final straight — same convention as the start grid); open paths use
   P1→P2. After normalization this direction IS +Z, so "behind the start
   line" is simply `z < 0` and "distance into the lap" grows along +Z.
4. **Routes are occupied ground.** `compute_layout()` seeds the group packer
   with corridor circles traced along every route (`path_width/2` half-width
   for ribbons, instance footprint for waypoint routes, + 2 m clearance).
   Packed groups (`single/line/ring/grid/scatter`) can never be slotted onto
   a ribbon again — this was the pile-up: the spiral slot search started at
   the origin (the track infield) and had no idea the ribbon existed.

Downstream effects, free of charge:

- The racing player merge (`unity_generator.py`) puts the playable Player at
  exactly `(0, capsule, 0)` yaw 0 — cameras, minimaps and HUDs can assume it.
- Terrain sizing (`terrain_bounds`) measures normalized geometry.
- `EmitWaypoints` containers inherit the convention: waypoint index 0 = start.

## 2. Route roles (planned)

One schema, many routes. The route an entity group carries already implies its
role today (road ribbon vs. cone line). Make the role explicit so deterministic
consumers stop guessing from `kind`:

- `route_role: "circuit" | "lane" | "patrol" | "flight" | "decoration"`
  (new optional EntitySpec field, default derived: `path_width > 0` → circuit).
- **Player/AI racing lines**: a `lane` route with the same P1 convention,
  offset from the circuit centerline. AI difficulty = how tightly the
  follower tracks the lane + speed scalar. The existing `AI.cs.j2` waypoint
  follower consumes the emitted `Waypoints` container as-is.
- **Patrol routes** (non-racing genres): open or closed `patrol` routes for
  guards/creatures; P1 = post/spawn anchor. Same 64-point IR, no new atoms.
- **2D**: same IR with y ignored (side-scroller: x/altitude from y; top-down:
  XZ read as XY). The 2D archetype builders can consume routes instead of
  their hardcoded course tables — that is the path to parity, not a new
  schema shape.

## 3. Derived placements (planned)

All of these are deterministic functions of a route — computed in the Fixer
(Python), never at runtime, never by the LLM:

- **Arc-length parameterization** `s ∈ [0, L)` with helpers
  `point_at(s)`, `tangent_at(s)`, `normal_at(s)` (XZ), `bank_at(s)` (lerped
  like the runtime). This is the single primitive everything below uses.
- **Prop placement**: `along(route, every=Nm | at=[s…], lateral=±d, jitter=0)`
  → baked `instance_positions` + `instance_yaws` facing the tangent (or
  normal). Cones, banners, streetlights, grandstands-outside-the-barriers all
  become one declarative rule instead of hand-authored point lists. Lateral
  offsets beyond `path_width/2 + clearance` keep props out of the corridor by
  construction, so the packer and the ribbon never fight.
- **Checkpoints**: today `CheckpointBanners` samples 4 fixed indices. Plan:
  `checkpoints = quarter points by arc length` (not by index — index spacing
  is distorted by corner density), each carrying `(position, tangent yaw,
  gate width = path_width + margin)`. Emitted both as visual banners and as
  trigger volumes (below). CP0 is always P1 — the start/finish gate.
- **Spawn points**: the start grid generalizes to `grid(route, rows, cols,
  row_spacing, lane_fraction)` behind P1 — already implemented for racing;
  reuse for wave-spawns (spawn at `s` offsets along a patrol route) and
  respawn-at-last-checkpoint.

## 4. Runtime seams (planned)

Emitted by the generator from the baked data — thin, deterministic C#:

- **Trigger volumes**: a `RouteTriggers` container next to `Waypoints`; each
  checkpoint becomes a BoxCollider (isTrigger) sized `gate width ×
  path_width` oriented to the tangent yaw. A `CheckpointTracker` script
  (new template) counts gate order per Player/Enemy tag: lap validation,
  wrong-way detection (dot(velocity, tangent) < 0), race position = laps +
  `s` progress. No physics piling: triggers don't collide.
- **Audio cues**: route-anchored audio is a list of `(s, cue)` pairs baked
  into SceneSetup — crowd loop at the grandstand arc, tire-screech zones at
  high-|bank| spans, countdown + start horn at P1 (CP0 crossing → lap audio).
  Uses the existing SA3/audio pipeline for the clips; the route only supplies
  *where*, keeping the audio micro engine-agnostic.
- **AI enemy routes**: opponents follow the `lane` route with per-opponent
  lateral offset (deterministic rotation over the variety index, like model
  variety) so they don't stack — plus rubber-banding as a speed scalar only
  (never position teleports).

## 5. Authoring sources (implemented: `unity-path` → `unity-create --route`)

`unity-path` (main.py, `src/unity_pathinput.py`) authors a canonical route
file — ≤64 points, closed-loop auto-detected, P1-normalized on emission — and
`unity-create --route routes.json` composes the circuit around it (overriding
the racing-degeneracy gate: an explicit route always wins). Custom
centerlines get barrier rails by perpendicular polyline offset; open
point-to-point courses are supported (grid still forms behind P1 along the
travel direction).

- **Text — explicit points**: `--points "x,z; x,z; ..."` or `--file pts.txt`
  (2 numbers = ground-plane x,z; 3 = x,y,z; 4 = x,y,z,bank°; meters). Over
  64 points are resampled arc-length-uniform (elevation/bank lerped).
- **Text — prompt (Steps 2–3, the only LLM steps)**: the schema-fill LLM can
  author `path_points` directly in entities; the Fixer clamps and
  normalization anchors it. Degenerate racing output still falls back to
  the parametric oval (`unity_racetrack.py`).
- **Image**: `--image track.png` — threshold (dark-on-light or inverted,
  auto) → Zhang-Suen thinning → ordered skeleton walk → Douglas-Peucker to
  ≤64 points → scale (`--world-size` meters across the drawing's larger
  extent, image y-down flipped to world +Z). Pure Pillow + stdlib,
  deterministic, no LLM. Bank/elevation can't come from a top-down image
  (points carry y=0); a second grayscale heightmap is the planned extension.

## 6. Sequencing

| PR | Scope |
|----|-------|
| done | P1 normalization + corridor packing + `unity-path` text/image authoring + `unity-create --route` (this PR) |
| next | Arc-length param + `along()` prop placement + arc-length checkpoints |
| then | CheckpointTracker triggers + wrong-way/lap logic + route-anchored audio cues |
| then | `route_role` + AI lane offsets + patrol routes in non-racing genres |
| then | Heightmap elevation/bank for image routes; 2D route consumption |

Every stage stays inside the compiler contract: LLM writes shape intent in
Steps 2–3; Steps 4–6 turn routes into placements deterministically.
