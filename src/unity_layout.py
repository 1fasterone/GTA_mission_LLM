"""
Deterministic post-schema layout pass (pipeline Step 5, Fixer stage) — selects
a spatial ARCHETYPE for the scene, then assigns every entity group a
world-space region inside it and bakes exact per-instance positions.

Three primitives ship, chosen by src/unity_layout_archetype.py from the
schema's (motion_axis, medium):

  ArenaPlane        planar + ground. The original packer, unchanged: circle
                    packing on the ground plane around the scene origin
                    (plus the path-route corridor variant for authored
                    circuits). Everything below this docstring's numbered
                    list describes it.
  VerticalCorridor  vertical + air, or vertical + water. A drop corridor of
                    height H: the exit platform and the player spawn take the
                    top, content is stratified down the corridor, and the goal
                    sits on the terminal plane (ground at y=0, or a water
                    surface with the things that live in the water hanging
                    below it).
  BoundedVolume     volumetric + air, or volumetric + water. A play box of
                    height H for free movement through a medium with no
                    descent framing (a glider, a flight sim, a scuba swim):
                    content is spread across altitude LAYERS inside the box,
                    each packed by the same 2D footprint packer, with only
                    ground-dwelling (air) or surface-marked (water) groups
                    allowed to touch a boundary plane.

Anything else ABSTAINS — `[layout.strict]`, hard-fail, never a silent
fall-through to ArenaPlane. A skydiving schema flattened onto a ground plane
(every altitude-banded entity at y≈0, the player standing on the landing pad
at t=0) is the exact failure that motivated the split.

ArenaPlane closes two gaps left by EntitySpawner's runtime placement formulas
(templates/unity/EntitySpawner.cs.j2):

  1. Every group spawned centered on the SAME world origin — its own
     `radius`/`spacing` only shaped the pattern WITHIN one group, nothing
     coordinated where different groups sat relative to each other, so a
     ring of buildings and a ring of vehicles authored at the same radius
     landed on the identical circle and stacked.
  2. "scatter" placement drew each instance uniformly at random inside
     `radius` with no reference to `spacing`/`scale` at all, so any scatter
     group with more than a handful of instances (or a large `scale`)
     guaranteed overlap — the "everything is spaced on top of each other"
     bug regardless of how the schema's spacing/scale fields were authored.

Both are geometry problems with one deterministic answer per schema, computed
once here in Python — never re-derived at runtime, never LLM-touched.
"""

from __future__ import annotations

import logging
import math
import random
from typing import Optional, Sequence

from .unity_layout_archetype import (
    LayoutArchetype,
    LayoutStrictError,
    corridor_height,
    entity_altitude_hint,
    is_climbable_entity,
    is_ground_dweller,
    is_hazard_entity,
    is_surface_dweller,
    is_terminal_entity,
    is_water_dweller,
    pick_exit_platform,
    pick_goal_entity,
    pick_player_entity,
    pick_wall_surface,
    select_archetype,
    surface_height,
    ui_board_layer,
    ui_board_rank,
    ui_element_of,
    volume_height,
)
from .unity_route_axis import RouteModifier, RouteZone
from .unity_route_axis import project as _route_project
from .unity_route_axis import resolve_offset as _route_resolve_offset
from .unity_route_axis import sample_n as _route_sample_n
from .unity_schema import EntitySpec, UnitySchema, WorldRegion, WorldSpec, is_embodied
from .unity_spatial import (VOLUME_PRIMITIVES, assign_elevations, band_key,
                            describe, select_primitive, tier_of)

_log = logging.getLogger(__name__)

# Minimum world-space gap enforced between two packed groups' footprint
# circles, on top of their own radii.
_GROUP_MARGIN = 2.0
# Clearance multiplier applied to a MEASURED model footprint (see
# _effective_spacing), on top of which the scatter jitter is clamped to the
# leftover slack so the cell spacing is actually honoured. 1.25 was enough to
# stop instances interpenetrating but still read as a solid mass of touching
# models; the gap has to be a real fraction of the model to look deliberate.
_MODEL_CLEARANCE = 1.9
# Ground-plane fallback half-extent for scenes with nothing to measure (e.g.
# MainMenu), and the margin added around every scene's measured content.
_DEFAULT_GROUND_HALF = 10.0
_GROUND_MARGIN = 8.0
# Live-caught on RustbeltDepthTestbed (locomotion="drive"): an 8-unit margin
# is sized for a walking player wandering near the props it was measured
# from, not a vehicle. A WheelCollider car reaches highway speeds within
# seconds of any throttle (observed 137 km/h after 6s of sustained torque)
# and crossed the entire 8-unit margin, and kept going into the void past
# the ground plane's edge, well within normal play. "fly" locomotion covers
# ground faster still. Both get a much larger margin; "walk"/"swim" keep the
# original value byte-identical.
_GROUND_MARGIN_BY_LOCOMOTION = {"drive": 60.0, "fly": 100.0}
# Extra radius added to a path route's cross-track half-width when building
# the corridor of seed circles the packer must keep other groups out of.
_PATH_CLEARANCE = 2.0

# The scene templates put the player at world XZ (0, 0) — see scene.unity.j2's
# Player GameObject. Nothing was reserving that cell, so the packer's very
# first slot (_find_slot returns the origin when nothing is placed yet) was
# the player's own spawn: whichever group had the largest footprint spawned
# on top of the player, and its collider trapped the player at t=0.
# Reserved as a seed circle, so the packer routes around it and never moves it.
_PLAYER_SPAWN_XZ = (0.0, 0.0)
# Radius of that reservation, by locomotion — clearance has to cover the body
# the controller builds at runtime plus room to pull away from a standing
# start. A vehicle_car's runtime BoxCollider alone is 1.7 x 4.0
# (micro/controller vehicle_car BuildCar), so a walker's 3 units would still
# leave a car's nose inside a neighbouring group.
_SPAWN_CLEARANCE_BY_LOCOMOTION = {"drive": 6.0, "fly": 8.0, "swim": 6.0}
_SPAWN_CLEARANCE_DEFAULT = 3.0

_LAYOUT_PLACEMENTS = frozenset({"single", "line", "ring", "grid", "scatter"})

# ── VerticalCorridor ─────────────────────────────────────────────────────────
# Stratification band, as a fraction of corridor height H. The player spawns AT
# H, so the top band sits below them (something to fall toward, not through at
# t=0) and the floor band sits well clear of the terminal plane (content the
# player is still falling past when the chute/goal comes into play).
_CORRIDOR_BAND_TOP   = 0.85
_CORRIDOR_BAND_FLOOR = 0.15
# Groups are parked beside the fall line rather than on it: the drop axis is
# the player's own path down, and a group centered on it spawns inside them.
# Each group's center sits its own footprint radius plus this clearance out.
_CORRIDOR_LANE_CLEARANCE = 6.0
# Successive groups are rotated by the golden angle around the drop axis —
# deterministic, and never repeats a bearing for any practical group count
# (a plain 2*pi/n would stack every group on one line when n is small).
_GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))
# How deep below a water surface plane the medium's own inhabitants hang, when
# the schema carries no depth geometry of its own.
_WATER_DEPTH_DEFAULT = 30.0
# Gap between the exit platform's footprint and the player standing in its
# doorway. Small on purpose: "attached to the platform", not "near it".
_EXIT_PLATFORM_CLEARANCE = 1.5
# Which entity is the player is decided by unity_layout_archetype's
# `pick_player_entity` (tag, then kind, then name) — see `player_rank` for why
# a single strict gate was not enough.

# ── BoundedVolume ────────────────────────────────────────────────────────────
# Interior band, as a fraction of box height H. Content lives strictly inside:
# the floor (or seabed) and the ceiling (or water surface) are boundaries, not
# shelves to stack things on.
_VOLUME_BAND_FLOOR = 0.2
_VOLUME_BAND_CEIL  = 0.8

# ── VerticalSurface ──────────────────────────────────────────────────────────
# The wall is the X-Y plane at z = 0, face normal +Z. The player stands in
# front of it (z > 0) and climbs in +Y; scenery packs behind and around on the
# ground plane. One convention, so "on the wall" is a z-coordinate question.
_SURFACE_BASE_DEPTH   = 4.0    # how far out from the face the base zone sits
_HOLD_SURFACE_OFFSET  = 0.35   # holds stand just proud of the face
_HAZARD_SURFACE_OFFSET = 0.6   # hazards sit proud of the holds
_HOLD_FIRST_Y         = 1.0    # first hold, reachable from standing
_WALL_HALF_WIDTH_MIN  = 6.0
# Maximum distance between two successive holds. A climbing wall whose holds
# are further apart than the player can reach is not a hard level, it is a
# broken one — so this is an INVARIANT the placement must satisfy, not a
# warning it may emit. Overridable per project via schema.meta.
_DEFAULT_REACH_DISTANCE = 2.5
_REACH_META_KEYS = ("reach_distance", "layout_reach_distance", "hold_reach")
# Share of the reach budget spent climbing rather than traversing, leaving the
# rest for the sideways zigzag that stops the chain being a vertical ladder.
_REACH_VERTICAL_FRACTION = 0.8
_ZIGZAG_FRACTION = 0.9         # of the leftover horizontal budget, per step
# Hazards must not sit on top of a hold and make it unusable.
_HAZARD_HOLD_CLEARANCE = 1.0
# EntitySpec.instance_positions is validator-clamped to this many points.
_MAX_BAKED_POSITIONS = 200

# ── UISurface (the board) ────────────────────────────────────────────────────
# Same plane convention as VerticalSurface: the board is the X-Y plane at z = 0
# with its face normal +Z, which is where every camera preset for a flat game
# already looks from (0, 0, -10). y grows UP, so reading order runs from +y
# down — the one place this primitive inverts a world-space habit, because a
# board is read from the top like a page and not from the origin outward.
_UI_BOARD_Z     = 0.0
_UI_BACKDROP_Z  = 2.0    # behind the content, from the camera's side
_UI_OVERLAY_Z   = -1.0   # chrome in front of it
# Gap between two cells, and between two instances inside one cell. An
# interface reads by ALIGNMENT, so both are constants rather than functions of
# footprint: unequal gutters look like a bug even when the geometry is correct.
_UI_CELL_GUTTER     = 1.6
_UI_INSTANCE_GUTTER = 0.45
# Target shape of the whole board. Chosen so the content fills a 16:9 frame,
# which is what the board-framing camera (src/micro/camera.py) fits to.
_UI_BOARD_ASPECT = 16.0 / 9.0
# A cell never gets narrower than this, whatever its footprint says: a group of
# one small icon still needs a hit target a pointer can land on.
_UI_MIN_CELL = 1.2
# Backdrops are sized to cover the content with a margin rather than to their
# own authored radius — a backdrop that does not reach the edges is a rectangle
# floating behind the game.
_UI_BACKDROP_MARGIN = 2.0


def _effective_spacing(entity: EntitySpec,
                       footprints: Optional[dict[str, float]] = None) -> float:
    """A primitive's own footprint is approximated by its authored `scale`
    (Unity's built-in cube/sphere/capsule primitives are ~1 unit before
    scaling — the same approximation EntitySpawner.cs.j2 uses for the
    `modelPrefab == null` fallback case). `spacing` is an authored MINIMUM;
    the footprint can only widen it, never shrink below what was authored.

    When `footprints` carries this entity's REAL model footprint (metres,
    measured by Unity and cached in the asset catalog — see
    src/unity_model_measure.py), that wins over the primitive approximation.
    Without it a 3 m model and a 0.2 m model are spaced identically, which is
    what packed fifteen giant heads into a single overlapping clump.
    """
    base = max(entity.spacing, entity.scale * 1.15)
    fp   = (footprints or {}).get(entity.name)
    if fp and fp > 0:
        # 1.15 is the clearance used for the primitive approximation. Measured
        # against the real layout, a bare 1.15 margin left neighbours
        # overlapping by about a quarter of their own width; _MODEL_CLEARANCE
        # is sized so the tightest pair still clears the model. (Scatter
        # placement itself now guarantees this as a real minimum distance via
        # Poisson-disk sampling — see _scatter_positions — rather than the
        # jittered-grid approximation this comment used to describe.)
        return max(base, fp * entity.scale * _MODEL_CLEARANCE)
    return base


def _footprint_radius(entity: EntitySpec, eff_spacing: float) -> float:
    """Bounding radius of this group's own layout pattern; `radius` is an
    authored minimum the computed footprint can only widen."""
    n = max(1, entity.count)
    if entity.placement == "ring":
        return max(entity.radius, n * eff_spacing / (2.0 * math.pi))
    if entity.placement == "line":
        return max(entity.radius, 0.5 * eff_spacing * (n - 1))
    if entity.placement == "grid":
        cols = math.ceil(math.sqrt(n))
        rows = math.ceil(n / cols)
        return max(entity.radius, 0.5 * eff_spacing * math.hypot(cols, rows))
    if entity.placement == "scatter":
        # n non-overlapping spacing x spacing cells, packed as a circle:
        # n * spacing^2 = pi * r^2.
        return max(entity.radius, eff_spacing * math.sqrt(n / math.pi))
    # "single" — a modest footprint so it still participates in packing.
    return max(0.5, entity.scale * 0.6)


def _find_slot(r: float, placed: list[tuple[float, float, float]]) -> tuple[float, float]:
    """Archimedean-spiral search for the first center at least
    `r + other_r + _GROUP_MARGIN` from every already-placed circle.
    Deterministic (fixed step sizes, no RNG) — terminates because the spiral
    radius grows monotonically and eventually clears any finite set of
    circles."""
    if not placed:
        return 0.0, 0.0
    step = max(1.0, r * 0.5)
    angle_step = math.pi / 8.0
    radius, angle = 0.0, 0.0
    for _ in range(100_000):
        cx, cz = radius * math.cos(angle), radius * math.sin(angle)
        if all(math.hypot(cx - ox, cz - oz) >= r + orad + _GROUP_MARGIN
               for ox, oz, orad in placed):
            return cx, cz
        angle += angle_step
        if angle >= 2 * math.pi:
            angle, radius = 0.0, radius + step
    return radius + r, 0.0  # unreachable in practice


def _pack_groups(entities: list[EntitySpec],
                  seed_circles: Sequence[tuple[float, float, float]] = ()
                  ) -> dict[str, tuple[float, float, float]]:
    """Deterministic non-overlapping circle packing over each group's
    `group_radius`. Biggest footprints are placed first (stable tie-break by
    name) so they claim central space — pure geometry, no RNG. `seed_circles`
    are treated as already-occupied space (e.g. a racetrack's pre-baked start
    grid) that these groups must avoid but which this pass never moves.
    Returns name -> (cx, cz, r)."""
    order = sorted(entities, key=lambda e: (-e.group_radius, e.name))
    placed: list[tuple[float, float, float]] = list(seed_circles)
    result: dict[str, tuple[float, float, float]] = {}
    for e in order:
        cx, cz = _find_slot(e.group_radius, placed)
        placed.append((cx, cz, e.group_radius))
        result[e.name] = (cx, cz, e.group_radius)
    return result


def _sample_polyline(pts: list, n: int) -> list[tuple[float, float, float, float]]:
    """`n` anchors evenly spaced by XZ arc length along the polyline, each as
    (px, pz, nx, nz) where (nx, nz) is the unit LEFT normal to the travel
    tangent. Anchors are centered in their arc-length bucket ((k+0.5)/n) so a
    closed loop's first and last don't coincide. Deterministic.

    REFACTORED to delegate to src.unity_route_axis.sample_n -- the shared
    primitive step 1 of docs/COURSE_ARCHETYPE_LAYERS.md's migration path
    lands. Byte-identical to the inline implementation this replaced; see
    that module's docstring and tests/test_unity_route_axis.py for the proof,
    plus TestArenaPlaneUnchanged.test_layout_matches_the_pre_change_recording
    (tests/test_unity_layout_archetype.py) for the end-to-end golden-output
    guarantee that this call site specifically still produces identical
    racetrack-corridor layouts."""
    return _route_sample_n(pts, n)


# Archetypes that actually consume schema.world_spec.course.zones today --
# see compute_layout()'s loud-not-silent warning for every other archetype.
# All five real archetypes are wired as of docs/COURSE_ARCHETYPE_LAYERS.md
# step 4; this set (and the warning mechanism it drives) stays in place for
# whatever spatial primitive gets added next (a spline/path primitive,
# per LAYOUT_ARCHETYPES.md's "Abstain" section) so that one's own gap is
# loud too, not silently reintroduced.
_ZONE_AWARE_ARCHETYPES = {"ArenaPlane", "BoundedVolume", "VerticalCorridor",
                         "VerticalSurface", "UISurface"}


def _course_zones(schema: UnitySchema) -> list[RouteZone]:
    """schema.world_spec.course.zones (Pydantic RouteZoneSpec, step 2 of
    docs/COURSE_ARCHETYPE_LAYERS.md's migration path) converted to
    src.unity_route_axis.RouteZone -- the schema model and the geometry
    primitive are kept independent (unity_route_axis has no schema/pydantic
    dependency), so this is the one place they're bridged. [] when no
    world_spec exists yet or none were authored -- the empty-means-unchanged
    contract every zone-aware interpreter relies on."""
    if schema.world_spec is None:
        return []
    return [
        RouteZone(start_t=z.start_t, end_t=z.end_t,
                 modifier=RouteModifier(z.modifier), amplitude=z.amplitude,
                 frequency=z.frequency, layer=z.layer, label=z.label)
        for z in schema.world_spec.course.zones
    ]


def _corridor_pack(packable: list[EntitySpec], primary: EntitySpec,
                   seed_circles: list[tuple[float, float, float]]
                   ) -> dict[str, tuple[float, float, float]]:
    """Place groups in bands hugging the primary route corridor — the path is
    the GUIDE for where props go (the user's ask: props line the track, they
    aren't dumped in packed islands off in the distance). Groups are spread
    evenly by arc length around the route and offset perpendicular to the
    OUTSIDE of the track, alternating sides; each is then pushed further out
    only as far as needed to clear the corridor and already-placed groups.
    Deterministic; same (cx, cz, r) contract as `_pack_groups`."""
    order = sorted(packable, key=lambda e: (-e.group_radius, e.name))
    anchors = _sample_polyline(primary.path_points, len(order))
    half_w = (primary.path_width / 2.0 if primary.path_width > 0.0 else 4.0)
    edge = half_w + _PATH_CLEARANCE + _GROUP_MARGIN
    placed = list(seed_circles)
    result: dict[str, tuple[float, float, float]] = {}
    for i, e in enumerate(order):
        px, pz, nx, nz = anchors[i]
        side = 1.0 if i % 2 == 0 else -1.0
        d = edge + e.group_radius
        cx = cz = 0.0
        for _ in range(400):
            cx, cz = px + nx * side * d, pz + nz * side * d
            if all(math.hypot(cx - ox, cz - oz) >= e.group_radius + orad + _GROUP_MARGIN
                   for ox, oz, orad in placed):
                break
            d += max(1.0, e.group_radius * 0.5)
        placed.append((cx, cz, e.group_radius))
        result[e.name] = (round(cx, 3), round(cz, 3), e.group_radius)
    return result


def _corridor_circles(entity: EntitySpec) -> list[tuple[float, float, float]]:
    """Chain of occupied circles tracing a path group's polyline — the seed
    geometry that keeps packed groups OFF the route itself (the "everything
    piles up on the track ribbon" bug: path groups never told the packer
    which ground they occupied, so the spiral search happily dropped scatter
    groups onto the road). Circle radius is the route's cross-track
    half-width (`path_width / 2` for tiled ribbons, the instance footprint
    for waypoint-spread groups) plus `_PATH_CLEARANCE`; centers are spaced by
    the chord at which consecutive circles still cover the full corridor
    width, so there are no gaps a group could slip through. Infield and
    outside space stays free. Deterministic pure geometry."""
    pts = entity.path_points
    half_w = (entity.path_width / 2.0 if entity.path_width > 0.0
              else max(0.5, entity.scale * 0.6))
    rc = half_w + _PATH_CLEARANCE
    step = 2.0 * math.sqrt(rc * rc - half_w * half_w)
    circles = [(pts[0][0], pts[0][2], rc)]
    since = 0.0  # arc distance walked since the last circle
    for a, b in zip(pts, pts[1:]):
        seg = math.hypot(b[0] - a[0], b[2] - a[2])
        if seg < 1e-9:
            continue
        pos = 0.0
        while since + (seg - pos) >= step:
            pos += step - since
            t = pos / seg
            circles.append((round(a[0] + (b[0] - a[0]) * t, 3),
                            round(a[2] + (b[2] - a[2]) * t, 3), rc))
            since = 0.0
        since += seg - pos
    return circles


def _measure_baked(entity: EntitySpec) -> tuple[float, float, float]:
    """Centroid + bounding radius of an entity's own pre-baked
    `instance_positions` (e.g. unity_racetrack.py's start-grid karts) — used
    to seed the packer and size terrain without touching positions a earlier
    deterministic scaffold already computed."""
    xs = [p[0] for p in entity.instance_positions]
    zs = [p[2] for p in entity.instance_positions]
    cx, cz = sum(xs) / len(xs), sum(zs) / len(zs)
    spread = max((math.hypot(x - cx, z - cz) for x, z in zip(xs, zs)), default=0.0)
    return cx, cz, spread + max(0.5, entity.scale * 0.6)


_BRIDSON_K = 30   # candidate points tried per active sample (standard Bridson default)


def _bridson_sample(rng: random.Random, radius: float,
                    region_radius: float) -> list[tuple[float, float]]:
    """Grid-accelerated Poisson-disk sampling (Bridson's algorithm) over a
    disk of `region_radius` centered at the origin, with a REAL minimum
    pairwise distance of `radius` between every accepted point — unlike a
    jittered grid, which only approximates a minimum spacing.

    Deterministic given `rng`'s state. The active list and `samples` are
    plain lists, iterated/indexed by position — never by dict/set iteration
    order, which Python does not guarantee stable across runs the way list
    order is (this pipeline's determinism tests replay a build twice and
    require byte-identical output). `grid` is a dict, but only ever looked up
    by a key computed deterministically from a point's own coordinates —
    never iterated — so it carries none of that risk."""
    if radius <= 0.0:
        radius = 1e-3
    cell = radius / math.sqrt(2.0)
    grid: dict[tuple[int, int], int] = {}   # (gx, gy) -> index into `samples`
    samples: list[tuple[float, float]] = []
    active: list[int] = []

    def gkey(p: tuple[float, float]) -> tuple[int, int]:
        return (int(math.floor(p[0] / cell)), int(math.floor(p[1] / cell)))

    def fits(p: tuple[float, float]) -> bool:
        if math.hypot(p[0], p[1]) > region_radius:
            return False
        gx, gy = gkey(p)
        for dy in (-2, -1, 0, 1, 2):
            for dx in (-2, -1, 0, 1, 2):
                idx = grid.get((gx + dx, gy + dy))
                if idx is not None and math.hypot(
                        p[0] - samples[idx][0], p[1] - samples[idx][1]) < radius:
                    return False
        return True

    origin = (0.0, 0.0)
    samples.append(origin)
    grid[gkey(origin)] = 0
    active.append(0)

    while active:
        i = rng.randrange(len(active))
        base = samples[active[i]]
        placed = False
        for _ in range(_BRIDSON_K):
            ang = rng.uniform(0.0, 2.0 * math.pi)
            dist = rng.uniform(radius, 2.0 * radius)
            cand = (base[0] + dist * math.cos(ang), base[1] + dist * math.sin(ang))
            if fits(cand):
                idx = len(samples)
                samples.append(cand)
                grid[gkey(cand)] = idx
                active.append(idx)
                placed = True
                break
        if not placed:
            active.pop(i)   # exhausted this sample's candidates; drop from active only

    return samples


def _scatter_positions(entity: EntitySpec, eff_spacing: float,
                        cx: float, cz: float,
                        footprint: float = 0.0,
                        y: float = 0.0) -> list[list[float]]:
    """Deterministic Poisson-disk (Bridson's algorithm) scatter: sample a disk
    around the group center with a real minimum-pairwise-distance guarantee,
    then keep the `count` samples closest to center (keeps the footprint
    round, same intent as the old grid's distance-sort). Trimming a valid
    Poisson-disk set to its `n` closest-to-center points cannot violate the
    minimum-distance guarantee among what remains — removing points can only
    preserve or increase the remaining minimum pairwise distance, never
    decrease it.

    Replaces the old jittered-grid approach, which only approximated the
    requested spacing (and, when a model's `footprint` exceeded `eff_spacing`,
    could still let two instances interpenetrate — the grid cells sat at
    `eff_spacing` regardless of a larger real footprint). Using `footprint`
    directly as the packing radius here fixes that case rather than just
    reproducing it."""
    n = max(1, entity.count)
    rng = random.Random(f"{entity.name}:{n}:{eff_spacing:.3f}")
    r = eff_spacing if footprint <= 0.0 else max(footprint, eff_spacing * 0.5)
    region_r = math.sqrt(n) * r * 1.6   # generous, same spirit as the old half_cols+2
    candidates = _bridson_sample(rng, radius=r, region_radius=region_r)
    if len(candidates) < n:
        _log.warning("scatter: Bridson under-produced for %r (need %d, got %d "
                     "at region_r=%.2f) — widening once, same seed",
                     entity.name, n, len(candidates), region_r)
        candidates = _bridson_sample(rng, radius=r, region_radius=region_r * 1.5)
    assert len(candidates) >= n, (
        f"[scatter.strict] Bridson under-produced for entity {entity.name!r} "
        f"(need {n}, got {len(candidates)}) even after widening the sampling "
        f"region — spacing={eff_spacing:.3f}/footprint={footprint:.3f} may be "
        f"too large for this count.")
    candidates.sort(key=lambda p: math.hypot(p[0], p[1]))
    return [[cx + x, y, cz + z] for x, z in candidates[:n]]


def _bake_positions(entity: EntitySpec, eff_spacing: float,
                     cx: float, cz: float,
                     footprint: float = 0.0,
                     y: float = 0.0) -> list[list[float]]:
    """`y` is the group's world-space elevation, chosen by the scene's spatial
    primitive (src/unity_spatial.py). It is 0.0 for every surface game, which
    is the only value this baker could produce before volume primitives
    existed."""
    n = max(1, entity.count)
    if entity.placement == "single":
        return [[cx, y, cz]]
    if entity.placement == "line":
        return [[cx + (i - (n - 1) / 2.0) * eff_spacing, y, cz] for i in range(n)]
    if entity.placement == "ring":
        r = entity.group_radius
        return [[cx + r * math.cos(i * 2.0 * math.pi / n),
                 y, cz + r * math.sin(i * 2.0 * math.pi / n)] for i in range(n)]
    if entity.placement == "grid":
        cols = math.ceil(math.sqrt(n))
        pts = []
        for i in range(n):
            row, col = divmod(i, cols)
            pts.append([cx + (col - (cols - 1) / 2.0) * eff_spacing,
                        y, cz + row * eff_spacing])
        return pts
    if entity.placement == "scatter":
        return _scatter_positions(entity, eff_spacing, cx, cz, footprint, y)
    return []


def _authored_elevation(entity: EntitySpec) -> Optional[float]:
    """An `elevation` the schema authored explicitly, or None.

    src/unity_spatial.py introduced this field as the intent channel and gave
    it absolute priority over anything derived; every primitive here honours
    that, so a deliberately placed group is never re-derived out from under
    the author.
    """
    return entity.elevation if abs(entity.elevation) > 1e-6 else None


def _band_order(entities: list[EntitySpec]) -> list[EntitySpec]:
    """Deterministic high-to-low ordering by what a KIND of thing is, not by
    how big it is.

    unity_spatial.tier_of encodes the ordering that actually matters in a
    vertical space — the carrier above what it drops toward, pickups strung
    on the route between them. Sorting by footprint radius (the first cut
    here) is arbitrary with respect to altitude: it put a jump plane below
    its own jumper, and kelp near the surface. Radius only breaks ties.
    """
    return sorted(entities, key=lambda e: (-tier_of(e), -e.group_radius, e.name))


def _set_elevation(entity: EntitySpec, y: float) -> None:
    """Record the chosen altitude on the entity as well as in its baked
    positions. Step 7 (src/unity_playability.py) reads `elevation` to tell a
    volume game whose avatar was left on the floor from one that was laid out
    properly, and can only do that if every primitive writes it."""
    entity.elevation = round(y, 3)


def _measure_and_size(layoutable: list[EntitySpec],
                      footprints: Optional[dict[str, float]]
                      ) -> tuple[list[EntitySpec], list[EntitySpec]]:
    """Split layoutable groups into (preplaced, packable) and give every group
    a `group_radius` — measured from already-baked positions for the former,
    computed from spacing/scale/count for the latter. A group's own footprint
    is the same geometry whether it lands on a ground plane or in a corridor.

    `_layout_arena_plane` deliberately keeps its own inline copy of this rather
    than calling in: it interleaves the measurement with building its seed
    circles, and its output is locked byte-for-byte against a pre-archetype
    recording (tests/fixtures/arenaplane_baseline.json). Fold the two together
    only under that test.
    """
    preplaced = [e for e in layoutable if e.instance_positions]
    packable  = [e for e in layoutable if not e.instance_positions]
    for e in preplaced:
        # group_radius <= 0.0 (the schema default) means these positions were
        # authored by something other than this function — e.g.
        # unity_racetrack.py's start grid — and haven't been measured yet.
        # Once measured (or if this is our OWN prior output being re-run),
        # trust the existing origin_offset/group_radius as-is so repeated
        # calls stay idempotent instead of re-measuring a jittered scatter's
        # empirical centroid slightly differently each time.
        if e.group_radius <= 0.0:
            cx, cz, r = _measure_baked(e)
            e.origin_offset, e.group_radius = [round(cx, 3), round(cz, 3)], round(r, 3)
    for e in packable:
        e.group_radius = _footprint_radius(e, _effective_spacing(e, footprints))
    return preplaced, packable


def _regions_for(packable: list[EntitySpec],
                 preplaced: list[EntitySpec]) -> list[WorldRegion]:
    """One axis-aligned XZ region per group, from its packed center + radius.
    Altitude is deliberately absent: WorldRegion.bounds is a ground-plane
    rectangle (and terrain sizing consumes it as one)."""
    return [
        WorldRegion(name=e.name, entities=[e.name],
                    bounds=[e.origin_offset[0] - e.group_radius,
                            e.origin_offset[1] - e.group_radius,
                            e.origin_offset[0] + e.group_radius,
                            e.origin_offset[1] + e.group_radius])
        for e in list(packable) + list(preplaced)
    ]


def _attach_regions(schema: UnitySchema, regions: list[WorldRegion]) -> None:
    if schema.world_spec is None:
        schema.world_spec = WorldSpec(regions=regions)
    else:
        schema.world_spec.regions = list(schema.world_spec.regions) + regions


def _lift(pts: list[list[float]], y: float) -> list[list[float]]:
    """Raise a ground-plane bake (`_bake_positions` always emits y = 0) to the
    corridor band it belongs in. The XZ pattern — ring, grid, jittered scatter
    — is identical; only the altitude changes."""
    return [[p[0], y, p[2]] for p in pts]


def _layout_vertical_corridor(schema: UnitySchema, arch: LayoutArchetype,
                              footprints: Optional[dict[str, float]]
                              ) -> list[str]:
    """Lay the scene out as a drop corridor of height H.

      - The EXIT PLATFORM — the thing the player jumps from — takes the top of
        the corridor (y = H) and the player spawn is attached beside it. Left
        to footprint-size stratification the jump ship sank below its own
        jumper; the role overrides that ordering for both.
      - Player spawns at the TOP (y = H), on the drop axis when there is no
        exit platform, or in the platform's doorway when there is.
      - Non-player groups are stratified down the corridor BELOW the exit
        platform: any group carrying its own altitude data keeps it, the rest
        are spread evenly across
        [_CORRIDOR_BAND_FLOOR, _CORRIDOR_BAND_TOP] * H. Nothing lands at y = 0
        by default — that flattening is the bug this primitive exists to fix.
      - The terminal plane is the ground (y = 0), or a water SURFACE at y = 0
        when medium == "water", in which case the things that live in the
        water hang below it at negative y.
      - The goal/landing entity is pinned to the terminal plane on the drop
        axis, directly under the player's exit point; other surface groups
        pack around it with the existing circle packer.

    Deterministic: fixed sort order, fixed golden-angle bearings, no RNG
    beyond the existing seeded scatter jitter. Same contract as ArenaPlane —
    writes `origin_offset`, `group_radius`, `instance_positions`.
    """
    msgs: list[str] = []
    height, why_height, defaulted = corridor_height(schema)
    if defaulted:
        _log.warning(
            "[layout] %r selected VerticalCorridor but carries no altitude or "
            "deck figure — falling back to the named default corridor height "
            "%.0f m. Add an exit altitude / hard deck to the description, or "
            "altitude-banded entity positions, to size the drop deliberately.",
            schema.project_name, height)
        msgs.append(
            f"VerticalCorridor height defaulted to {height:g} m ({why_height})")
    else:
        msgs.append(f"VerticalCorridor height H={height:g} m ({why_height})")

    layoutable = [e for e in schema.entities if e.placement in _LAYOUT_PLACEMENTS]
    preplaced, packable = _measure_and_size(layoutable, footprints)

    # ── Roles ────────────────────────────────────────────────────────────────
    player, why_player = pick_player_entity(packable)
    if player is not None:
        msgs.append(f"Player group resolved to '{player.name}' "
                    f"(signal: {why_player})")
    rest     = [e for e in packable if e is not player]
    terminal = [e for e in rest if is_terminal_entity(e)]
    corridor = [e for e in rest if not is_terminal_entity(e)]

    surface_y = 0.0
    is_water  = arch.medium == "water"
    top_y     = round(height, 3)

    # ── Exit platform: the top of the corridor belongs to it ─────────────────
    platform, why_platform = pick_exit_platform(schema, player, corridor,
                                                arch.medium)
    spawn_x = 0.0
    if platform is not None:
        corridor = [e for e in corridor if e is not platform]
        platform.origin_offset = [0.0, 0.0]
        platform.instance_positions = _lift(
            _bake_positions(platform, _effective_spacing(platform, footprints),
                            0.0, 0.0,
                            (footprints or {}).get(platform.name, 0.0) * platform.scale),
            top_y)
        _set_elevation(platform, top_y)
        # The player stands in its doorway: clear of the platform's own
        # footprint, but attached to it rather than parked somewhere else.
        spawn_x = round(platform.group_radius
                        + (player.group_radius if player is not None else 0.0)
                        + _EXIT_PLATFORM_CLEARANCE, 3)
        msgs.append(
            f"Exit platform '{platform.name}' takes the corridor top "
            f"(y={top_y:g}) — signal: {why_platform}")

    # ── Player: top of the corridor, attached to the platform if there is one ─
    if player is not None:
        player.origin_offset = [spawn_x, 0.0]
        player.instance_positions = _lift(
            _bake_positions(player, _effective_spacing(player, footprints),
                            spawn_x, 0.0,
                            (footprints or {}).get(player.name, 0.0) * player.scale),
            top_y)
        _set_elevation(player, top_y)
        where = (f"attached to '{platform.name}' at x={spawn_x:g}"
                 if platform is not None else "on the drop axis")
        msgs.append(
            f"Player group '{player.name}' spawns at the top of the corridor "
            f"(y={top_y:g}, {where}) instead of standing on the terminal plane")

    # ── Corridor content: stratified below the top, never all at y = 0 ───────
    # Authored course.zones (docs/COURSE_ARCHETYPE_LAYERS.md), interpreted
    # HERE along the DROP axis: t = this group's normalized position within
    # the stratification band [_CORRIDOR_BAND_FLOOR, _CORRIDOR_BAND_TOP] --
    # so a "dip" zone near the bottom of the fall (a downdraft pocket) pulls
    # affected content further down, "ripple" oscillates it, "rough" jitters
    # it. Only applied to the DEFAULT case -- an authored elevation, a schema
    # altitude hint, or a water-dweller's submerged depth are all explicit
    # intent and keep winning outright, same rule as before zones existed.
    zones = _course_zones(schema)
    ordered = _band_order(corridor)
    span = _CORRIDOR_BAND_TOP - _CORRIDOR_BAND_FLOOR
    banded: list[str] = []
    zoned: list[str] = []
    for i, e in enumerate(ordered):
        # Evenly spaced band centers, largest group highest. A single group
        # takes the top band — near the player, where it can be reached.
        frac = _CORRIDOR_BAND_TOP - span * (i / max(1, len(ordered) - 1))
        authored = _authored_elevation(e)
        hint = entity_altitude_hint(e)
        if authored is not None:
            y = round(authored, 3)
            band_why = f"{e.name}@{y:g}(authored)"
        elif hint is not None:
            y = round(max(0.0, min(height, hint)), 3)
            band_why = f"{e.name}@{y:g}(schema)"
        elif is_water and is_water_dweller(e):
            # Below the surface plane: this group's home is the medium
            # itself -- e.g. a "dip" zone here is a real thermocline/trench
            # pocket pulling a school of fish deeper, the underwater example
            # docs/COURSE_ARCHETYPE_LAYERS.md's multi-layer section names.
            y = round(-_WATER_DEPTH_DEFAULT * frac, 3)
            band_why = f"{e.name}@{y:g}(submerged)"
            if zones:
                t_norm = (frac - _CORRIDOR_BAND_FLOOR) / span if span > 1e-9 else 0.0
                offset = _route_resolve_offset(zones, t_norm, seed=e.name)
                if offset != 0.0:
                    # Additive, same convention as the default branch below --
                    # a "dip" zone's offset is already negative (zone_offset),
                    # so adding it here makes y MORE negative (deeper), correct
                    # for a thermocline/trench pocket.
                    y = round(y + offset * _WATER_DEPTH_DEFAULT, 3)
                    band_why = f"{e.name}@{y:g}(submerged, zoned)"
                    zoned.append(e.name)
        else:
            y = round(height * frac, 3)
            band_why = f"{e.name}@{y:g}"
            if zones:
                t_norm = (frac - _CORRIDOR_BAND_FLOOR) / span if span > 1e-9 else 0.0
                offset = _route_resolve_offset(zones, t_norm, seed=e.name)
                if offset != 0.0:
                    y = round(y + offset * height, 3)
                    band_why = f"{e.name}@{y:g}(zoned)"
                    zoned.append(e.name)
        banded.append(band_why)

        # Parked beside the drop axis, one golden-angle step per group, so the
        # player falls PAST content instead of spawning inside it.
        theta  = i * _GOLDEN_ANGLE
        lane_r = e.group_radius + _CORRIDOR_LANE_CLEARANCE
        cx, cz = lane_r * math.cos(theta), lane_r * math.sin(theta)
        e.origin_offset = [round(cx, 3), round(cz, 3)]
        e.instance_positions = _lift(
            _bake_positions(e, _effective_spacing(e, footprints), cx, cz,
                            (footprints or {}).get(e.name, 0.0) * e.scale),
            y)
        _set_elevation(e, y)
    if banded:
        msgs.append(
            f"Stratified {len(banded)} group(s) down the corridor "
            f"({', '.join(banded)})")
    if zoned:
        msgs.append(
            f"Applied {len(zones)} authored course zone(s) along the drop "
            f"axis to {len(zoned)} group's elevation ({', '.join(zoned)})")

    # ── Terminal plane ───────────────────────────────────────────────────────
    goal = pick_goal_entity(terminal)
    seed_circles: list[tuple[float, float, float]] = [
        (e.origin_offset[0], e.origin_offset[1], e.group_radius) for e in preplaced
    ]
    if goal is not None:
        goal.origin_offset = [0.0, 0.0]
        goal.instance_positions = _lift(
            _bake_positions(goal, _effective_spacing(goal, footprints), 0.0, 0.0,
                            (footprints or {}).get(goal.name, 0.0) * goal.scale),
            surface_y)
        _set_elevation(goal, surface_y)
        seed_circles.append((0.0, 0.0, goal.group_radius))
    elif player is None:
        # No airborne player and no goal: the scene template's own Player still
        # stands at the terminal-plane origin, so keep the ArenaPlane spawn
        # reservation rather than packing a group on top of them.
        seed_circles.append((_PLAYER_SPAWN_XZ[0], _PLAYER_SPAWN_XZ[1],
                             _SPAWN_CLEARANCE_BY_LOCOMOTION.get(
                                 schema.locomotion, _SPAWN_CLEARANCE_DEFAULT)))

    others = [e for e in terminal if e is not goal]
    if others:
        slots = _pack_groups(others, seed_circles)
        for e in others:
            cx, cz, _r = slots[e.name]
            e.origin_offset = [round(cx, 3), round(cz, 3)]
            e.instance_positions = _lift(
                _bake_positions(e, _effective_spacing(e, footprints), cx, cz,
                                (footprints or {}).get(e.name, 0.0) * e.scale),
                surface_y)
            _set_elevation(e, surface_y)
    if terminal:
        plane = "water surface" if is_water else "ground"
        msgs.append(
            f"Placed {len(terminal)} group(s) on the {plane} terminal plane "
            f"(y={surface_y:g}"
            + (f"; goal '{goal.name}' pinned under the drop axis" if goal else "")
            + ")")
    if is_water:
        msgs.append(
            f"Water surface plane at y={surface_y:g}; medium inhabitants hang "
            f"below it (down to y=-{_WATER_DEPTH_DEFAULT:g})")

    _attach_regions(schema, _regions_for(packable, preplaced))
    return msgs


def _reach_distance(schema: UnitySchema) -> tuple[float, str]:
    """Maximum climbable gap between successive holds, and where it came from.
    Schema override first (`meta.reach_distance`), else the named default."""
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    for key in _REACH_META_KEYS:
        if key in meta:
            try:
                val = float(meta[key])
            except (TypeError, ValueError):
                continue
            if val > 0.0:
                return val, f"schema.meta[{key!r}]"
    return _DEFAULT_REACH_DISTANCE, "named default _DEFAULT_REACH_DISTANCE"


def _hold_chain(n: int, first_y: float, top_y: float, reach: float,
                half_width: float) -> list[tuple[float, float]]:
    """`n` (x, y) hold positions on the wall face, base to summit, such that
    EVERY successive pair is within `reach` of the other.

    A plain ladder would satisfy reach trivially and read as a ladder, so the
    chain zigzags: the vertical step spends `_REACH_VERTICAL_FRACTION` of the
    budget and the sideways swing spends what is left, which is why the
    3-D gap stays inside `reach` instead of only the vertical component.
    """
    if n <= 1:
        return [(0.0, first_y)]
    dy = (top_y - first_y) / (n - 1)
    # Whatever the vertical step actually is, the horizontal swing gets the
    # remaining budget and no more.
    dx_budget = math.sqrt(max(0.0, reach * reach - dy * dy))
    amp = min(half_width, dx_budget * 0.5 * _ZIGZAG_FRACTION)
    return [((-1.0 if i % 2 else 1.0) * amp, first_y + dy * i) for i in range(n)]


def _max_chain_gap(pts: list[tuple[float, float]]) -> float:
    """Largest distance between successive holds — the number the reachability
    invariant is checked against, and the one logged in provenance."""
    return max((math.dist(a, b) for a, b in zip(pts, pts[1:])), default=0.0)


def _verify_hold_chain(pts: list[tuple[float, float]], reach: float,
                       project: str) -> float:
    """Enforce the reachability invariant; return the max observed gap.

    Separate from the generator on purpose. `_hold_chain` satisfies reach by
    construction, so a check folded into it would be unreachable code that
    only *looks* like a guarantee — this one is called on the final geometry,
    after hazards, and is directly testable with a chain that violates it.
    """
    worst = _max_chain_gap(pts)
    if worst > reach + 1e-6:
        raise LayoutStrictError(
            f"[layout.strict] VerticalSurface produced an unclimbable wall "
            f"for project {project!r}: the largest gap between successive "
            f"holds is {worst:.3f} m but the reach is {reach:.3f} m. This is "
            "a placement invariant — the layout refuses to emit a wall the "
            "player cannot climb."
        )
    return worst


def _layout_vertical_surface(schema: UnitySchema, arch: LayoutArchetype,
                             footprints: Optional[dict[str, float]]
                             ) -> list[str]:
    """Lay the scene out as a climbable wall of height H.

    Ascent is not descent upside-down: a drop is a volume the player falls
    through, a climb is a SURFACE the player moves along with everything
    attached to it. So:

      - one entity IS the wall (see `pick_wall_surface`), oriented as the X-Y
        plane at z = 0
      - the player spawns in a base zone on the ground in front of it
      - Holdable/Climbable groups are distributed across the FACE — never in
        free space, never at y = 0 — as a single chain from base to summit
        that satisfies the reachability invariant (see `_hold_chain`)
      - Hazard/Enemy groups attach to the face between the hold rows; they
        never count toward the chain and are kept clear of it
      - everything else packs on the ground plane around the base

    Deterministic: fixed ordering, fixed zigzag, no RNG beyond the existing
    seeded scatter jitter (which this primitive does not use).
    """
    msgs: list[str] = []
    zones = _course_zones(schema)
    height, why_height, defaulted = surface_height(schema)
    if defaulted:
        _log.warning(
            "[layout] %r selected VerticalSurface but carries no height "
            "figure — falling back to the named default wall height %.0f m. "
            "Give the brief a summit height to size the climb deliberately.",
            schema.project_name, height)
        msgs.append(f"VerticalSurface height defaulted to {height:g} m ({why_height})")
    else:
        msgs.append(f"VerticalSurface height H={height:g} m ({why_height})")

    reach, why_reach = _reach_distance(schema)
    msgs.append(f"Reach distance {reach:g} m ({why_reach}) — maximum gap "
                "between successive holds")

    layoutable = [e for e in schema.entities if e.placement in _LAYOUT_PLACEMENTS]
    preplaced, packable = _measure_and_size(layoutable, footprints)

    # ── Roles ────────────────────────────────────────────────────────────────
    player, why_player = pick_player_entity(packable)
    if player is not None:
        msgs.append(f"Player group resolved to '{player.name}' (signal: {why_player})")
    rest = [e for e in packable if e is not player]

    wall, why_wall = pick_wall_surface(rest)
    if wall is not None:
        rest = [e for e in rest if e is not wall]
        msgs.append(f"Wall surface is '{wall.name}' (signal: {why_wall})")
    else:
        msgs.append("No wall-surface entity in the schema — the face is bare "
                    "geometry; holds and hazards still define it")

    holds   = [e for e in rest if is_climbable_entity(e)]
    hazards = [e for e in rest if e not in holds and is_hazard_entity(e)]
    ground  = [e for e in rest if e not in holds and e not in hazards]

    half_width = max(_WALL_HALF_WIDTH_MIN,
                     wall.group_radius if wall is not None else 0.0)

    # ── Hold chain: the invariant ────────────────────────────────────────────
    top_y = round(height, 3)
    chain: list[tuple[float, float]] = []
    if holds:
        step_y = reach * _REACH_VERTICAL_FRACTION
        span = max(0.0, top_y - _HOLD_FIRST_Y)
        required = int(math.ceil(span / step_y)) + 1 if span > 0 else 1
        authored = sum(max(1, e.count) for e in holds)
        capacity = _MAX_BAKED_POSITIONS * len(holds)
        n = min(capacity, max(authored, required))
        if n < required:
            # More wall than holds can ever chain. A summit the player cannot
            # reach is a broken level, so the CLIMB is shortened to what the
            # holds actually connect, loudly, rather than leaving a gap.
            top_y = round(_HOLD_FIRST_Y + (n - 1) * step_y, 3)
            _log.warning(
                "[layout] %r wants a %.0f m wall but %d hold slot(s) at a "
                "%.2f m reach can only chain %.0f m — climb shortened to the "
                "reachable summit. Raise the hold count or the reach.",
                schema.project_name, height, n, reach, top_y)
            msgs.append(
                f"Climb shortened to y={top_y:g} — {n} hold slot(s) at reach "
                f"{reach:g} cannot chain the full {height:g} m")
        chain = _hold_chain(n, _HOLD_FIRST_Y, top_y, reach, half_width)

        # Round-robin the chain across the hold groups so the reachable path is
        # built from ALL of them, not one group climbing past the others.
        ordered_holds = sorted(holds, key=lambda e: e.name)
        buckets: dict[str, list[list[float]]] = {e.name: [] for e in ordered_holds}
        for i, (hx, hy) in enumerate(chain):
            owner = ordered_holds[i % len(ordered_holds)]
            buckets[owner.name].append(
                [round(hx, 3), round(hy, 3), _HOLD_SURFACE_OFFSET])
        for e in ordered_holds:
            mine = buckets[e.name]
            e.instance_positions = mine
            e.count = len(mine)         # keep count honest with what was baked
            xs = [p[0] for p in mine]
            e.origin_offset = [round(sum(xs) / len(xs), 3) if xs else 0.0, 0.0]
            ys = [q[1] for q in mine]
            _set_elevation(e, sum(ys) / len(ys) if ys else 0.0)
        msgs.append(
            f"Distributed {len(chain)} hold(s) across the wall face from "
            f"y={_HOLD_FIRST_Y:g} to y={top_y:g} "
            f"({', '.join(f'{e.name}x{e.count}' for e in ordered_holds)})")

    # ── Hazards: on the face, between the holds, never blocking them ─────────
    # Authored course.zones interpreted along the CLIMB axis (t = this
    # hazard instance's own fraction of the base->summit span, already
    # computed below for its nominal position) -- e.g. "rough" (crumbling
    # holds -> more slip hazard nearby) or "dip" (a section with no holds,
    # widening the hazard gap) shift a hazard's height within the climb.
    # Never applied to the hold chain itself: perturbing a hold's position
    # risks the `_verify_hold_chain` reachability invariant, which hazards
    # (never part of the chain) cannot violate.
    climb_span = max(1e-6, top_y - _HOLD_FIRST_Y)
    if hazards:
        placed_haz = 0
        zoned_haz: list[str] = []
        for hi, e in enumerate(sorted(hazards, key=lambda x: x.name)):
            n_h = max(1, e.count)
            pts: list[list[float]] = []
            for k in range(n_h):
                # Sit between hold rows, on the opposite swing from the chain.
                t = (k + 0.5 + hi) / max(1, n_h + hi)
                hy = _HOLD_FIRST_Y + climb_span * min(1.0, t)
                if zones:
                    offset = _route_resolve_offset(zones, min(1.0, t), seed=f"{e.name}:{k}")
                    if offset != 0.0:
                        hy = max(0.0, min(top_y, hy + offset * climb_span))
                        if e.name not in zoned_haz:
                            zoned_haz.append(e.name)
                hx = (1.0 if (k + hi) % 2 else -1.0) * half_width * 0.55
                # Push outward until it clears every hold it could smother.
                for _ in range(24):
                    if all(math.dist((hx, hy), c) >= _HAZARD_HOLD_CLEARANCE
                           for c in chain):
                        break
                    hx += math.copysign(_HAZARD_HOLD_CLEARANCE, hx or 1.0)
                    if abs(hx) > half_width:
                        hx = math.copysign(half_width, hx)
                        break
                pts.append([round(hx, 3), round(hy, 3), _HAZARD_SURFACE_OFFSET])
            e.instance_positions = pts
            e.origin_offset = [round(sum(p[0] for p in pts) / len(pts), 3), 0.0]
            _set_elevation(e, sum(q[1] for q in pts) / len(pts))
            placed_haz += len(pts)
        msgs.append(f"Attached {placed_haz} hazard/enemy instance(s) to the "
                    f"face between the hold rows, cleared by "
                    f"{_HAZARD_HOLD_CLEARANCE:g} m from every hold")
        if zoned_haz:
            msgs.append(
                f"Applied {len(zones)} authored course zone(s) along the "
                f"climb axis to {len(zoned_haz)} hazard group's height "
                f"({', '.join(zoned_haz)})")

    # ── Re-verify the chain AFTER hazards, as an invariant ───────────────────
    if chain:
        worst = _verify_hold_chain(chain, reach, schema.project_name)
        msgs.append(
            f"Hold chain verified base->summit after hazard placement: "
            f"{len(chain)} holds, max gap {worst:.3f} m <= reach {reach:g} m")

    # ── Player: base zone, on the ground in front of the face ────────────────
    if player is not None:
        player.origin_offset = [0.0, round(_SURFACE_BASE_DEPTH, 3)]
        player.instance_positions = [[0.0, 0.0, round(_SURFACE_BASE_DEPTH, 3)]]
        player.count = 1
        _set_elevation(player, 0.0)
        msgs.append(f"Player spawns in the base zone at the foot of the wall "
                    f"(y=0, {_SURFACE_BASE_DEPTH:g} m out from the face)")

    # ── Wall geometry ────────────────────────────────────────────────────────
    if wall is not None:
        n_w = max(1, wall.count)
        if n_w == 1:
            wall.instance_positions = [[0.0, round(top_y / 2.0, 3), 0.0]]
        else:
            cols = math.ceil(math.sqrt(n_w))
            rows = math.ceil(n_w / cols)
            wall.instance_positions = [
                [round(-half_width + 2 * half_width * ((i % cols) / max(1, cols - 1)), 3),
                 round(top_y * ((i // cols) / max(1, rows - 1)), 3), 0.0]
                for i in range(n_w)
            ]
        wall.origin_offset = [0.0, 0.0]
        wall.group_radius = max(wall.group_radius, half_width)
        _set_elevation(wall, sum(q[1] for q in wall.instance_positions)
                       / len(wall.instance_positions))

    # ── Summit goal ──────────────────────────────────────────────────────────
    goal = pick_goal_entity(ground)
    if goal is not None:
        ground = [e for e in ground if e is not goal]
        goal.origin_offset = [0.0, 0.0]
        goal.instance_positions = [[0.0, top_y, round(_HOLD_SURFACE_OFFSET, 3)]]
        goal.count = 1
        _set_elevation(goal, top_y)
        msgs.append(f"Summit goal '{goal.name}' placed at the top of the wall "
                    f"(y={top_y:g})")
    else:
        msgs.append(f"No goal entity in the schema — the wall's top edge "
                    f"(y={top_y:g}) is the summit goal region")

    # ── Everything else: ground plane around the base ────────────────────────
    if ground:
        seed_circles: list[tuple[float, float, float]] = [
            (0.0, _SURFACE_BASE_DEPTH,
             _SPAWN_CLEARANCE_BY_LOCOMOTION.get(schema.locomotion,
                                                _SPAWN_CLEARANCE_DEFAULT)),
            (0.0, 0.0, half_width),      # the wall's own ground footprint
        ]
        seed_circles += [(e.origin_offset[0], e.origin_offset[1], e.group_radius)
                         for e in preplaced]
        slots = _pack_groups(ground, seed_circles)
        for e in ground:
            cx, cz, _r = slots[e.name]
            e.origin_offset = [round(cx, 3), round(cz, 3)]
            e.instance_positions = _bake_positions(
                e, _effective_spacing(e, footprints), cx, cz,
                (footprints or {}).get(e.name, 0.0) * e.scale)
        msgs.append(f"Packed {len(ground)} non-wall group(s) on the ground "
                    f"plane around the base ({', '.join(e.name for e in ground)})")

    _attach_regions(schema, _regions_for(packable, preplaced))
    return msgs


def _layout_bounded_volume(schema: UnitySchema, arch: LayoutArchetype,
                           footprints: Optional[dict[str, float]]) -> list[str]:
    """Lay the scene out as a play box of height H — free movement through a
    medium, with no descent framing.

      - medium == "air": the floor at y = 0 is terrain. Only ground-dwelling
        content (trees, rocks, buildings, a landing pad) sits on it; the player
        and everything else live in the interior.
      - medium == "water": the top of the box at y = H is the water SURFACE.
        Only surface-marked content (boats, buoys, docks) rides on it; the
        player and every swimming group hang below it.
      - Interior content is cut into `_VOLUME_LAYERS` altitude layers, and each
        layer is packed by the EXISTING footprint packer (`_pack_groups`).
        Groups in different layers may share XZ — they are separated
        vertically, which is what makes this a volume rather than a plane.

    Deterministic: fixed sort order, fixed layer count, no RNG beyond the
    existing seeded scatter jitter.
    """
    msgs: list[str] = []
    height, why_height, defaulted = volume_height(schema)
    if defaulted:
        _log.warning(
            "[layout] %r selected BoundedVolume but carries no altitude or "
            "depth figure — falling back to the named default box height "
            "%.0f m. Add a ceiling / operating altitude / dive depth to the "
            "description, or altitude-banded entity positions, to size the "
            "play box deliberately.", schema.project_name, height)
        msgs.append(
            f"BoundedVolume height defaulted to {height:g} m ({why_height})")
    else:
        msgs.append(f"BoundedVolume height H={height:g} m ({why_height})")

    layoutable = [e for e in schema.entities if e.placement in _LAYOUT_PLACEMENTS]
    preplaced, packable = _measure_and_size(layoutable, footprints)

    is_water   = arch.medium == "water"
    floor_y    = 0.0
    ceiling_y  = round(height, 3)
    boundary_y = ceiling_y if is_water else floor_y
    # Water mirrors the tiers below the surface, so tier 1.0 is always
    # 'furthest from where the player ends up' in both media.
    sign = 1.0

    player, why_player = pick_player_entity(packable)
    if player is not None:
        msgs.append(f"Player group resolved to '{player.name}' "
                    f"(signal: {why_player})")
    rest = [e for e in packable if e is not player]
    # The one group allowed to touch a boundary plane. Everything else is
    # interior content — defaulting it to the floor is the flattening these
    # primitives exist to remove.
    on_boundary = [e for e in rest
                   if (is_surface_dweller(e) if is_water else is_ground_dweller(e))]
    interior = [e for e in rest if e not in on_boundary]

    # The player's spawn column stays clear through the WHOLE box: in a volume
    # the player can be at any altitude a moment later, so a group directly
    # above them is as much of a trap as one on top of them.
    spawn_r = _SPAWN_CLEARANCE_BY_LOCOMOTION.get(
        schema.locomotion, _SPAWN_CLEARANCE_DEFAULT)
    if player is not None:
        spawn_r += player.group_radius
    reserved: list[tuple[float, float, float]] = [
        (_PLAYER_SPAWN_XZ[0], _PLAYER_SPAWN_XZ[1], spawn_r)
    ]
    reserved += [(e.origin_offset[0], e.origin_offset[1], e.group_radius)
                 for e in preplaced]

    # ── Player: mid-box, never on a boundary ─────────────────────────────────
    player_y = round(height * (_VOLUME_BAND_FLOOR + _VOLUME_BAND_CEIL) / 2.0, 3)
    if player is not None:
        player.origin_offset = [0.0, 0.0]
        player.instance_positions = _lift(
            _bake_positions(player, _effective_spacing(player, footprints),
                            0.0, 0.0,
                            (footprints or {}).get(player.name, 0.0) * player.scale),
            player_y)
        _set_elevation(player, player_y)
        msgs.append(
            f"Player group '{player.name}' spawns inside the volume "
            f"(y={player_y:g})"
            + (f", below the water surface at y={ceiling_y:g}" if is_water
               else ", not on the terrain floor"))

    # ── Interior: altitude by KIND, then pack each band in plan view ─────────
    # Altitude comes from src/unity_spatial.py's tier table, scaled into this
    # box: an authored `elevation` wins outright, otherwise the kind decides.
    # An earlier cut round-robined size-sorted groups across three fixed
    # layers, which was wrong twice over — it put the carrier at the BOTTOM of
    # its own volume, and it split two groups of the same kind across
    # different altitudes for no reason. Tier ordering says a jump plane is
    # above what it drops, and two gate groups belong at the same height and
    # apart in PLAN view, which is what band packing then does.
    # Authored course.zones (docs/COURSE_ARCHETYPE_LAYERS.md), interpreted
    # HERE along the ALTITUDE axis: t = tier_of(e), the same [0,1] "where
    # this kind of thing lives vertically" value the band split already
    # used -- so a "dip" zone pulls affected kinds toward one boundary (a
    # thermocline pocket, a downdraft), "ripple" oscillates by tier, "rough"
    # adds small per-group jitter. Skipped for a group with an AUTHORED
    # elevation -- that is the schema's explicit intent and always wins
    # outright, same rule this function already applied before zones existed.
    zones = _course_zones(schema)
    span = _VOLUME_BAND_CEIL - _VOLUME_BAND_FLOOR
    elevations: dict[str, float] = {}
    zoned: list[str] = []
    for e in interior:
        authored = _authored_elevation(e)
        if authored is not None:
            elevations[e.name] = round(authored, 3)
            continue
        tier = tier_of(e)
        base = height * (_VOLUME_BAND_FLOOR + span * tier) * sign
        if zones:
            offset = _route_resolve_offset(zones, tier, seed=e.name)
            if offset != 0.0:
                base += offset * height * sign
                zoned.append(e.name)
        elevations[e.name] = round(base, 3)

    placed_layers: list[str] = []
    for band in sorted({band_key(y) for y in elevations.values()}, reverse=True):
        in_band = [e for e in interior if band_key(elevations[e.name]) == band]
        # Same band -> must not overlap in XZ. Different bands may share it:
        # they are already separated vertically, which is the whole point.
        slots = _pack_groups(in_band, reserved)
        for e in in_band:
            y = elevations[e.name]
            cx, cz, _r = slots[e.name]
            e.origin_offset = [round(cx, 3), round(cz, 3)]
            e.instance_positions = _lift(
                _bake_positions(e, _effective_spacing(e, footprints), cx, cz,
                                (footprints or {}).get(e.name, 0.0) * e.scale),
                y)
            _set_elevation(e, y)
        placed_layers.append(
            f"y={elevations[in_band[0].name]:g}: "
            + ", ".join(e.name for e in in_band))
    if placed_layers:
        msgs.append("Distributed interior content through the volume ("
                    + "; ".join(placed_layers) + ")")
    if zoned:
        msgs.append(
            f"Applied {len(zones)} authored course zone(s) along the "
            f"altitude axis to {len(zoned)} group's elevation ({', '.join(zoned)})")

    # ── Boundary plane ───────────────────────────────────────────────────────
    if on_boundary:
        slots = _pack_groups(on_boundary, reserved)
        for e in on_boundary:
            cx, cz, _r = slots[e.name]
            e.origin_offset = [round(cx, 3), round(cz, 3)]
            e.instance_positions = _lift(
                _bake_positions(e, _effective_spacing(e, footprints), cx, cz,
                                (footprints or {}).get(e.name, 0.0) * e.scale),
                boundary_y)
            _set_elevation(e, boundary_y)
        plane = "water surface" if is_water else "terrain floor"
        msgs.append(
            f"Placed {len(on_boundary)} group(s) on the {plane} "
            f"(y={boundary_y:g}): "
            + ", ".join(e.name for e in on_boundary))
    if is_water:
        msgs.append(
            f"Water surface plane at y={ceiling_y:g}; everything not marked "
            "surface/boat/buoy is below it")

    _attach_regions(schema, _regions_for(packable, preplaced))
    return msgs


def _ui_pitch(entity: EntitySpec,
              footprints: Optional[dict[str, float]]) -> float:
    """Distance between two neighbouring instances of this group on the board.

    An element's OWN size sets it, plus one constant gutter — deliberately not
    `_effective_spacing`, which floors everything at `EntitySpec.spacing`
    (default 2.0 m) and widens it by `_MODEL_CLEARANCE`. Those are ground-plane
    numbers: two metres is a sensible gap between props a player walks between,
    and it is a chasm between two 0.6-unit icons in a file list. Fed through
    that rule a 3-unit terminal window came out narrower than a nine-icon grid
    of 0.6-unit files, because the grid was being spaced by a default that had
    nothing to do with the icons.

    A measured model footprint still wins when there is one, for the same
    reason it does everywhere else: it is the real size of the thing.
    """
    fp = (footprints or {}).get(entity.name) or 0.0
    size = max(entity.scale, fp * entity.scale)
    return max(_UI_MIN_CELL, size) + _UI_INSTANCE_GUTTER


def _ui_cell_size(entity: EntitySpec, pitch: float) -> tuple[float, float]:
    """Width and height of the cell this group occupies on the board.

    Derived from the group's own instance grid rather than from its packing
    radius: a cell is a RECTANGLE holding `count` instances in reading order,
    and the circle radius `_footprint_radius` computes would make a row of ten
    icons as tall as it is wide. Square-ish by construction (`cols` is the
    ceiling of the square root), which is what keeps a board of mixed groups
    from stair-stepping.
    """
    n = max(1, entity.count)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    return (max(_UI_MIN_CELL, cols * pitch),
            max(_UI_MIN_CELL, rows * pitch))


def _ui_flow_rows(cells: list[tuple[EntitySpec, float, float]], board_w: float
                  ) -> list[list[tuple[EntitySpec, float, float]]]:
    """Break the cell sequence into rows that fit `board_w`, in order.

    Order is never rearranged to pack tighter. A row that ends early leaves a
    ragged right edge, which is what a reading flow looks like; reordering to
    fill it would move a group away from the one it belongs beside, and on a
    board adjacency is meaning.
    """
    rows: list[list[tuple[EntitySpec, float, float]]] = []
    row: list[tuple[EntitySpec, float, float]] = []
    used = 0.0
    for cell in cells:
        w = cell[1]
        need = w if not row else used + _UI_CELL_GUTTER + w
        if row and need > board_w:
            rows.append(row)
            row, used = [cell], w
        else:
            row.append(cell)
            used = need
    if row:
        rows.append(row)
    return rows


def _ui_bake_cell(entity: EntitySpec, pitch: float,
                  cx: float, cy: float, z: float) -> list[list[float]]:
    """Instances inside one cell: a reading-order sub-grid, left to right and
    top to bottom, centred on the cell.

    This is the whole difference between a board and a world. `_bake_positions`
    offers ring/scatter/line patterns because a world is looked at from any
    angle; a board is looked at from one, so every group gets the same grid and
    the schema's `placement` is deliberately ignored here. Twelve commits laid
    out as a jittered scatter read as debris, not as history.
    """
    n = max(1, entity.count)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    pts: list[list[float]] = []
    for i in range(n):
        row, col = divmod(i, cols)
        # Rows count DOWN from the top of the cell — reading order.
        pts.append([round(cx + (col - (cols - 1) / 2.0) * pitch, 3),
                    round(cy - (row - (rows - 1) / 2.0) * pitch, 3),
                    round(z, 3)])
    return pts


def _layout_ui_board(schema: UnitySchema, arch: LayoutArchetype,
                     footprints: Optional[dict[str, float]]) -> list[str]:
    """Lay the scene out as a board of panels facing the camera.

    Every other primitive here answers "where does the player's body go, and
    what does it move through". A non-embodied game (locomotion='none', a
    positive declaration — see unity_schema.is_embodied) has no body, so those
    are not hard questions to answer, they are the wrong questions. Its content
    is an INTERFACE: the player points at it.

    So the board is the X-Y plane at z = 0, and content flows across it in
    reading order — left to right, top to bottom, in aligned rows with one
    constant gutter — rather than being packed as circles around an origin.
    Three z-planes keep it legible: backdrops behind, content on the board,
    captions and legends in front.

    The ordering is by ROLE (`ui_board_rank`: the workspace, then the panels
    and lists, then the cards and buttons, then the icons), with schema order
    breaking ties. It is deliberately not by size — `_pack_groups` sorts
    biggest-first so the largest footprints claim the centre, which is right
    for a world and wrong for an interface, where where-a-thing-is is what it
    means.

    Deterministic: fixed ordering, fixed gutters, no RNG at all — this
    primitive never calls the seeded scatter jitter.
    """
    msgs: list[str] = []
    zones = _course_zones(schema)
    layoutable = [e for e in schema.entities if e.placement in _LAYOUT_PLACEMENTS]
    preplaced, packable = _measure_and_size(layoutable, footprints)

    order = {e.name: i for i, e in enumerate(schema.entities)}
    by_layer: dict[str, list[EntitySpec]] = {"backdrop": [], "board": [],
                                             "overlay": []}
    for e in packable:
        by_layer[ui_board_layer(e)].append(e)

    content = sorted(by_layer["board"],
                     key=lambda e: (ui_board_rank(e), order.get(e.name, 0)))
    overlay = sorted(by_layer["overlay"], key=lambda e: order.get(e.name, 0))

    # ── The board: cells in reading order ────────────────────────────────────
    pitches = {e.name: _ui_pitch(e, footprints) for e in packable}
    cells = [(e, *_ui_cell_size(e, pitches[e.name])) for e in content]
    if cells:
        # Board width from the content's own area at the target aspect, but
        # never narrower than its widest single cell — a cell that does not fit
        # its own row would start a row it then overflows anyway.
        area = sum((w + _UI_CELL_GUTTER) * (h + _UI_CELL_GUTTER)
                   for _e, w, h in cells)
        board_w = max(max(w for _e, w, _h in cells),
                      math.sqrt(max(area, 1e-6) * _UI_BOARD_ASPECT))
        rows = _ui_flow_rows(cells, board_w)

        row_heights = [max(h for _e, _w, h in row) for row in rows]
        total_h = sum(row_heights) + _UI_CELL_GUTTER * (len(rows) - 1)
        # The block is centred on the origin so the flat camera preset at
        # (0, 0, -10) frames it without the generator having to aim.
        cursor_y = total_h / 2.0
        # Authored course.zones interpreted along the READING-ORDER axis
        # (t = this row's position through the board, top to bottom) as a
        # z-depth offset -- NOT x/y: repositioning cells in the reading
        # plane would break "position carries meaning," the reason this
        # primitive doesn't pack by size in the first place. A "ripple"
        # zone gently staggers row depth for visual interest, "dip" pushes
        # a whole section further back, "rough" jitters depth per row.
        # Unlike the other three archetypes (which have a natural H to
        # scale a fraction by), amplitude here is raw world units of depth
        # -- there is no "height" for a flat board to make it a fraction of.
        zoned_rows: list[str] = []
        for ri, (row, row_h) in enumerate(zip(rows, row_heights)):
            row_w = (sum(w for _e, w, _h in row)
                     + _UI_CELL_GUTTER * (len(row) - 1))
            cursor_x = -row_w / 2.0
            cy = cursor_y - row_h / 2.0
            z = _UI_BOARD_Z
            if zones:
                t = ri / max(1, len(rows) - 1)
                offset = _route_resolve_offset(zones, t, seed=f"row{ri}")
                if offset != 0.0:
                    z = round(_UI_BOARD_Z + offset, 3)
                    zoned_rows.append(str(ri))
            for e, w, _h in row:
                cx = cursor_x + w / 2.0
                e.origin_offset = [round(cx, 3), z]
                e.instance_positions = _ui_bake_cell(
                    e, pitches[e.name], cx, cy, z)
                e.group_radius = round(max(w, _h) / 2.0, 3)
                _set_elevation(e, cy)
                cursor_x += w + _UI_CELL_GUTTER
            cursor_y -= row_h + _UI_CELL_GUTTER

        widest = max(
            (sum(w for _e, w, _h in row) + _UI_CELL_GUTTER * (len(row) - 1))
            for row in rows)
        msgs.append(
            f"UISurface board {widest:.1f} x {total_h:.1f} — {len(cells)} "
            f"group(s) in {len(rows)} reading-order row(s): "
            + " | ".join(", ".join(f"{e.name}({ui_element_of(e) or 'content'})"
                                   for e, _w, _h in row) for row in rows))
        if zoned_rows:
            msgs.append(
                f"Applied {len(zones)} authored course zone(s) along the "
                f"reading-order axis to {len(zoned_rows)} row's depth "
                f"(rows {', '.join(zoned_rows)})")
    else:
        board_w = total_h = 0.0
        msgs.append("UISurface board is empty — no content groups to arrange")

    # ── Overlay chrome: a strip above the board, in front of it ──────────────
    if overlay:
        top = total_h / 2.0 + _UI_CELL_GUTTER
        strip = [(e, *_ui_cell_size(e, pitches[e.name])) for e in overlay]
        strip_w = (sum(w for _e, w, _h in strip)
                   + _UI_CELL_GUTTER * (len(strip) - 1))
        cursor_x = -strip_w / 2.0
        for e, w, h in strip:
            cx = cursor_x + w / 2.0
            cy = top + h / 2.0
            e.origin_offset = [round(cx, 3), _UI_OVERLAY_Z]
            e.instance_positions = _ui_bake_cell(
                e, pitches[e.name], cx, cy, _UI_OVERLAY_Z)
            e.group_radius = round(max(w, h) / 2.0, 3)
            _set_elevation(e, cy)
            cursor_x += w + _UI_CELL_GUTTER
        msgs.append(
            f"Overlay strip above the board at z={_UI_OVERLAY_Z:g}: "
            + ", ".join(e.name for e in overlay))

    # ── Backdrops: sized to cover, parked behind ─────────────────────────────
    # A group whose KIND is literally background/skybox/sky is claimed earlier
    # by the asset resolver's _SKIP_KINDS (owned by SceneSetup and the sky
    # pipeline) and never spawns — this still records where it would sit, which
    # is what world_spec.regions and the camera fit read. The layer earns its
    # keep on the common case instead: a kind="prop" group called
    # "GridBackdrop", which does spawn and must not land on top of the game.
    for e in sorted(by_layer["backdrop"], key=lambda x: order.get(x.name, 0)):
        e.origin_offset = [0.0, _UI_BACKDROP_Z]
        e.instance_positions = [[0.0, 0.0, _UI_BACKDROP_Z]]
        e.count = 1
        e.group_radius = round(
            max(board_w, total_h) / 2.0 + _UI_BACKDROP_MARGIN, 3)
        _set_elevation(e, 0.0)
    if by_layer["backdrop"]:
        msgs.append(
            f"Backdrop group(s) behind the board at z={_UI_BACKDROP_Z:g}, "
            f"sized to cover: "
            + ", ".join(e.name for e in by_layer["backdrop"]))

    _attach_regions(schema, _regions_for(packable, preplaced))
    return msgs


# The full archetype decision, stamped by the first layout pass so later passes
# replay it rather than re-deriving one from the positions the first pass baked.
_ARCH_DECISION_KEY = "layout_archetype_decision"


def _replay_archetype(schema: UnitySchema) -> Optional[LayoutArchetype]:
    """The archetype an earlier pass on this schema already committed to, or
    None when there is none to replay. Deliberately tolerant of a malformed or
    partial stamp: a bad stamp falls back to a fresh derivation rather than
    failing a build, in the same spirit as WiringMap._accept_list."""
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    stamped = meta.get(_ARCH_DECISION_KEY)
    if not isinstance(stamped, dict):
        return None
    try:
        return LayoutArchetype(
            name=str(stamped["name"]),
            motion_axis=str(stamped["motion_axis"]),
            medium=str(stamped["medium"]),
            goal_topology=str(stamped["goal_topology"]),
            evidence=dict(stamped.get("evidence") or {}),
            direction=str(stamped.get("direction") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


def compute_layout(schema: UnitySchema,
                   footprints: Optional[dict[str, float]] = None) -> list[str]:
    """Select a spatial archetype for the scene, then lay every
    {single,line,ring,grid,scatter} entity group out inside it, baking
    `origin_offset`, `group_radius` and `instance_positions` onto each
    EntitySpec.

    ArenaPlane (planar + ground) is the original behaviour, described below and
    unchanged. VerticalCorridor (vertical + air/water) drops the scene down a
    corridor instead — see `_layout_vertical_corridor`; BoundedVolume
    (volumetric + air/water) spreads it through a play box — see
    `_layout_bounded_volume`; UISurface (static + screen, a game that declared
    it has no avatar) arranges it as a board of panels — see
    `_layout_ui_board`. Any other pair of axes raises `[layout.strict]` rather
    than being flattened onto a ground plane.

    ── ArenaPlane ───────────────────────────────────────────────────────────
    Pack every group into a non-overlapping world-space region and bake exact
    instance positions.

    Two categories are left untouched, only treated as ALREADY-OCCUPIED space
    the packer must avoid (and terrain sizing must cover):
      - "path"-placement groups (explicit authored geometry, e.g. a racetrack
        loop from src/unity_racetrack.py) — fully determined by `path_points`.
        Their polylines seed the packer as corridors of occupied circles
        (`_corridor_circles`), so packed groups can no longer land ON the
        route ribbon — the "props pile up on the track" bug.
      - Any {single,line,ring,grid,scatter} group that ALREADY carries
        `instance_positions` — an earlier deterministic scaffold (e.g. the
        racetrack's start-grid karts, anchored to the track's actual
        start/finish line rather than the scene origin) has already computed
        exact positions this pass must not override, only avoid.

    Idempotent: recomputed from spacing/scale/radius/count on every call for
    everything else, so calling this twice on the same schema yields the
    same layout.

    2D/2D-topdown archetypes build their own course/arena layout at spawn
    time (EntitySpawner.cs.j2's BuildSideScroller2D/BuildTopDown2D) and are
    skipped here — before archetype selection, so a 2D project is never asked
    to justify a 3D spatial primitive it will not use.

    Except when the 2D game has no avatar. Those spawn-time builders lay a
    course out AROUND A PLAYER — a side-scroller's single lane, a top-down
    arena scattered about the spawn — so for a non-embodied game they arrange
    the content around something that is not there. That is how the live
    CodeVault run reached a 2D top-down teaching sim with its file icons
    scattered like debris: the layout pass had declined to run, and the only
    other opinion in the build assumed an avatar. A declared non-embodied
    schema gets the UISurface board whatever its dimension, and every embodied
    2D project keeps returning [] here, byte-identically.
    """
    if schema.game_type in ("2D", "2d_topdown") and is_embodied(schema):
        return []

    layoutable = [e for e in schema.entities if e.placement in _LAYOUT_PLACEMENTS]
    if not layoutable:
        # Nothing to place (e.g. a MainMenu-only schema). There is no layout to
        # get wrong, so there is nothing for an archetype to decide either.
        return []

    # Raises [layout.strict] when no Stage-1 primitive covers the derived axes.
    # Deliberately NOT caught here: a silent fall-through to ArenaPlane is the
    # failure mode this selection exists to remove.
    #
    # The FIRST pass's decision is authoritative for every later pass. The
    # generator re-runs this function once models are resolved, to re-pack with
    # real measured footprints — and re-deriving the archetype there reads
    # evidence THIS pass created: the altitudes it just baked. A lagoon survey
    # (locomotion='swim', explicitly "no descent", selected BoundedVolume) came
    # back from the second pass as a VerticalCorridor purely because the first
    # pass had banded its content by altitude, so a game about swimming around
    # a reef was re-packed as a fall column — silently, since the generator
    # discards the messages this returns.
    arch = _replay_archetype(schema) or select_archetype(schema)
    # Step 7 (src/unity_playability.py) grades the scene against a spatial
    # primitive. Stamp the one the layout actually used, so the evaluator
    # judges what was built rather than re-deriving a second opinion from
    # locomotion alone.
    _has_route = any(e.placement == "path" and e.path_points
                     for e in schema.entities)
    if isinstance(schema.meta, dict):
        schema.meta["spatial_primitive"] = arch.spatial_primitive(_has_route)
        schema.meta["layout_archetype"] = arch.name
        # The whole decision, not just its name, so a second pass can replay it
        # instead of re-deriving one from this pass's own output.
        schema.meta[_ARCH_DECISION_KEY] = {
            "name": arch.name, "motion_axis": arch.motion_axis,
            "medium": arch.medium, "goal_topology": arch.goal_topology,
            "direction": arch.direction, "evidence": dict(arch.evidence),
        }
    msgs: list[str] = [arch.provenance()]
    # Abstain LOUDLY, never silently default (see CLAUDE.md): an authored
    # course.zones that lands on an archetype whose interpreter doesn't
    # consume it yet must say so, not quietly do nothing.
    _pre_zones = _course_zones(schema)
    if _pre_zones and arch.name not in _ZONE_AWARE_ARCHETYPES:
        _log.warning(
            "[layout] %r authored %d course zone(s) but the %r archetype "
            "interpreter doesn't consume them yet (wired: %s) -- they will "
            "have NO effect on this build.", schema.project_name,
            len(_pre_zones), arch.name,
            ", ".join(sorted(_ZONE_AWARE_ARCHETYPES)))
        msgs.append(
            f"WARNING: {len(_pre_zones)} authored course "
            f"zone(s) have no effect -- {arch.name} doesn't interpret them yet")
    if arch.name == "VerticalCorridor":
        return msgs + _layout_vertical_corridor(schema, arch, footprints)
    if arch.name == "BoundedVolume":
        return msgs + _layout_bounded_volume(schema, arch, footprints)
    if arch.name == "VerticalSurface":
        return msgs + _layout_vertical_surface(schema, arch, footprints)
    if arch.name == "UISurface":
        return msgs + _layout_ui_board(schema, arch, footprints)
    return msgs + _layout_arena_plane(schema, footprints)


def _layout_arena_plane(schema: UnitySchema,
                        footprints: Optional[dict[str, float]] = None) -> list[str]:
    """The original ground-plane packer — see `compute_layout`'s docstring.
    Byte-for-byte the behaviour that shipped before archetype selection."""
    layoutable = [e for e in schema.entities if e.placement in _LAYOUT_PLACEMENTS]
    preplaced = [e for e in layoutable if e.instance_positions]
    packable = [e for e in layoutable if not e.instance_positions]

    # The player's own spawn cell is occupied space, reserved before anything
    # else so no packed group can claim it. "path" groups are deliberately NOT
    # displaced by it — a race start line runs THROUGH the spawn by design, and
    # a road is a surface the player rests on rather than an obstacle (see
    # EntitySpawner.cs.j2's IsSurfaceKind slab collider).
    spawn_r = _SPAWN_CLEARANCE_BY_LOCOMOTION.get(
        schema.locomotion, _SPAWN_CLEARANCE_DEFAULT)
    seed_circles: list[tuple[float, float, float]] = [
        (_PLAYER_SPAWN_XZ[0], _PLAYER_SPAWN_XZ[1], spawn_r)
    ]
    path_groups = [e for e in schema.entities
                   if e.placement == "path" and e.path_points]
    for e in path_groups:
        seed_circles.extend(_corridor_circles(e))
    for e in preplaced:
        # group_radius <= 0.0 (the schema default) means these positions were
        # authored by something other than this function — e.g.
        # unity_racetrack.py's start grid — and haven't been measured yet.
        # Once measured (or if this is our OWN prior output being re-run),
        # trust the existing origin_offset/group_radius as-is so repeated
        # calls stay idempotent instead of re-measuring a jittered scatter's
        # empirical centroid slightly differently each time.
        if e.group_radius <= 0.0:
            cx, cz, r = _measure_baked(e)
            e.origin_offset, e.group_radius = [round(cx, 3), round(cz, 3)], round(r, 3)
        seed_circles.append((e.origin_offset[0], e.origin_offset[1], e.group_radius))

    for e in packable:
        e.group_radius = _footprint_radius(e, _effective_spacing(e, footprints))

    # When the scene has an authored route, the route is the placement GUIDE:
    # prop groups line the corridor (offset outside the barriers, spread by arc
    # length) instead of spiral-packing from the origin — which, with a long
    # diagonal corridor blocking the center, exiled them to distant islands and
    # ballooned the ground plane (`terrain_bounds` covers whatever they hit).
    from .unity_pathnorm import _primary_route
    primary = _primary_route(schema.entities) if path_groups else None

    # Which SHAPE of space is this game played in (src/unity_spatial.py)?
    # Surface games (ground_plane / route_corridor) get an empty elevation map
    # and are laid out exactly as before this selection existed.
    primitive = select_primitive(schema.game_type, schema.locomotion,
                                 has_route=primary is not None)
    elevations = assign_elevations(packable, primitive)

    if primary is not None and packable:
        slots = _corridor_pack(packable, primary, seed_circles)
    elif primitive in VOLUME_PRIMITIVES and packable:
        # A volume is packed one ALTITUDE BAND at a time: groups in different
        # bands are already separated in Y, so forcing them apart in XZ too
        # would spread a vertical game back out across a floor — the exact
        # failure this primitive exists to fix. Bands are keyed by quantized
        # elevation, so the partition doesn't depend on entity order.
        slots = {}
        for band in sorted({band_key(elevations.get(e.name, 0.0))
                            for e in packable}):
            in_band = [e for e in packable
                       if band_key(elevations.get(e.name, 0.0)) == band]
            slots.update(_pack_groups(in_band, seed_circles))
    else:
        slots = _pack_groups(packable, seed_circles)

    # Authored course.zones (docs/COURSE_ARCHETYPE_LAYERS.md), interpreted
    # HERE as elevation offsets along the route's arc length -- a group
    # packed near a "dip" zone (e.g. a wash crossing) sits lower, near a
    # "ripple" zone rides a sine wave by its position along the corridor,
    # near a "rough" zone gets small deterministic per-group jitter. Only
    # meaningful when there's a route to project onto; compute_layout()
    # already warned loudly if zones were authored without one reaching an
    # archetype that can use them.
    zones = _course_zones(schema) if primary is not None else []
    zoned: list[str] = []
    moved: list[str] = []
    for e in packable:
        cx, cz, _r = slots[e.name]
        y = elevations.get(e.name, 0.0)
        if zones:
            assert primary is not None  # zones is only ever non-empty when primary was found above
            t, _lateral = _route_project(primary.path_points, cx, cz)
            offset = _route_resolve_offset(zones, t, seed=e.name)
            if offset != 0.0:
                y = round(y + offset, 3)
                zoned.append(e.name)
        e.elevation = y
        e.origin_offset = [round(cx, 3), round(cz, 3)]
        e.instance_positions = _bake_positions(
            e, _effective_spacing(e, footprints), cx, cz,
            (footprints or {}).get(e.name, 0.0) * e.scale, y)
        if abs(cx) > 0.01 or abs(cz) > 0.01:
            moved.append(e.name)

    msgs: list[str] = []
    _volume_msg = describe(primitive, elevations)
    if _volume_msg:
        msgs.append(_volume_msg)
    if zoned:
        assert primary is not None  # zoned is only ever non-empty when primary was found above
        msgs.append(
            f"Applied {len(zones)} authored course zone(s) along the "
            f"'{primary.name}' route to {len(zoned)} group's elevation "
            f"({', '.join(zoned)})")
    if moved and primary is not None:
        msgs.append(
            f"Layout pass placed {len(packable)} entity group(s) along the "
            f"'{primary.name}' route corridor ({', '.join(moved)} spread by "
            "arc length and offset outside the track, alternating sides)"
        )
    elif moved:
        msgs.append(
            f"Layout pass packed {len(packable)} entity group(s) into "
            f"non-overlapping regions ({', '.join(moved)} offset from the "
            "scene origin to avoid stacking on other groups)"
        )
    if path_groups and packable and primary is None:
        msgs.append(
            f"Seeded {len(path_groups)} path route(s) as occupied corridors "
            "so packed groups stay off the route ribbons"
        )
    if any(e.placement == "scatter" for e in packable):
        msgs.append(
            "Replaced runtime random scatter with deterministic jittered-grid "
            "positions honoring spacing/scale"
        )

    regions = [
        WorldRegion(name=e.name, entities=[e.name],
                    bounds=[slots[e.name][0] - slots[e.name][2],
                            slots[e.name][1] - slots[e.name][2],
                            slots[e.name][0] + slots[e.name][2],
                            slots[e.name][1] + slots[e.name][2]])
        for e in packable
    ] + [
        WorldRegion(name=e.name, entities=[e.name],
                    bounds=[e.origin_offset[0] - e.group_radius,
                            e.origin_offset[1] - e.group_radius,
                            e.origin_offset[0] + e.group_radius,
                            e.origin_offset[1] + e.group_radius])
        for e in preplaced
    ]
    if schema.world_spec is None:
        schema.world_spec = WorldSpec(regions=regions)
    else:
        schema.world_spec.regions = list(schema.world_spec.regions) + regions

    return msgs


def terrain_bounds(entities: list[EntitySpec],
                   locomotion: str = "walk") -> tuple[float, float, float, float]:
    """Axis-aligned world-space bounds (xmin, zmin, xmax, zmax) a scene's
    ground plane must cover to contain every entity group actually hosted by
    that scene, plus a margin. Falls back to a modest fixed footprint when
    there's nothing to measure (e.g. MainMenu, which hosts no gameplay
    entities) — replaces the previous unconditional 20x20 plane.

    `locomotion` widens the margin for vehicle/aircraft-speed players (see
    _GROUND_MARGIN_BY_LOCOMOTION) — a fixed pedestrian-scale margin left a
    drivable car able to cross it and drive off the ground plane within
    normal play. Defaults to "walk" so existing callers are unaffected."""
    margin = _GROUND_MARGIN_BY_LOCOMOTION.get(locomotion, _GROUND_MARGIN)
    pts: list[tuple[float, float]] = []
    for e in entities:
        if e.placement == "path" and e.path_points:
            pts.extend((p[0], p[2]) for p in e.path_points)
        elif e.group_radius > 0.0:
            cx, cz = e.origin_offset
            r = e.group_radius
            pts.append((cx - r, cz - r))
            pts.append((cx + r, cz + r))
    if not pts:
        half = max(_DEFAULT_GROUND_HALF, margin)
        return (-half, -half, half, half)
    xs = [p[0] for p in pts]
    zs = [p[1] for p in pts]
    return (min(xs) - margin, min(zs) - margin,
            max(xs) + margin, max(zs) + margin)
