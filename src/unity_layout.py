"""
Deterministic post-schema layout pass (pipeline Step 5, Fixer stage) — selects
a spatial ARCHETYPE for the scene, then assigns every entity group a
world-space region inside it and bakes exact per-instance positions.

Two primitives ship, chosen by src/unity_layout_archetype.py from the
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
    corridor_height,
    entity_altitude_hint,
    is_terminal_entity,
    is_water_dweller,
    pick_exit_platform,
    pick_goal_entity,
    pick_player_entity,
    select_archetype,
)
from .unity_schema import EntitySpec, UnitySchema, WorldRegion, WorldSpec
from .unity_spatial import (VOLUME_PRIMITIVES, assign_elevations, band_key,
                            describe, select_primitive)

_log = logging.getLogger(__name__)

# Minimum world-space gap enforced between two packed groups' footprint
# circles, on top of their own radii.
_GROUP_MARGIN = 2.0
# Fraction of a scatter grid cell an instance may be jittered by — keeps the
# "scattered" look while guaranteeing no instance drifts closer than
# roughly (1 - 2*_SCATTER_JITTER) * spacing to its grid neighbor.
_SCATTER_JITTER = 0.3
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
        # 1.15 is the clearance used for the primitive approximation, but a
        # scatter cell is then jittered by up to _SCATTER_JITTER in each axis,
        # which pulls the closest pair well inside the nominal cell. Measured
        # against the real layout, a 1.15 margin left neighbours overlapping by
        # about a quarter of their own width; _MODEL_CLEARANCE is sized so the
        # tightest pair still clears the model.
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
    closed loop's first and last don't coincide. Deterministic."""
    seglens = [math.hypot(b[0] - a[0], b[2] - a[2]) for a, b in zip(pts, pts[1:])]
    total = sum(seglens)
    if total < 1e-9 or n <= 0:
        return [(pts[0][0], pts[0][2], -0.0, 1.0)] * max(0, n)
    out: list[tuple[float, float, float, float]] = []
    for k in range(n):
        target = total * (k + 0.5) / n
        acc = 0.0
        for (a, b), L in zip(zip(pts, pts[1:]), seglens):
            if L < 1e-9:
                continue
            if acc + L >= target:
                t = (target - acc) / L
                px, pz = a[0] + (b[0] - a[0]) * t, a[2] + (b[2] - a[2]) * t
                tx, tz = (b[0] - a[0]) / L, (b[2] - a[2]) / L
                out.append((px, pz, -tz, tx))   # left normal = tangent +90° in XZ
                break
            acc += L
        else:
            a = pts[-1]
            out.append((a[0], a[2], -0.0, 1.0))
    return out


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


def _scatter_positions(entity: EntitySpec, eff_spacing: float,
                        cx: float, cz: float,
                        footprint: float = 0.0,
                        y: float = 0.0) -> list[list[float]]:
    """Deterministic jittered-grid scatter: lay a grid of `eff_spacing` cells
    around the group center, take the `count` cells closest to it (keeps the
    footprint round rather than a filled square), and jitter each by up to
    `_SCATTER_JITTER * eff_spacing` — replaces the old runtime
    `System.Random` uniform draw, which ignored `spacing`/scale entirely and
    guaranteed overlap at any real count or scale."""
    n = max(1, entity.count)
    half_cols = math.ceil(math.sqrt(n)) + 2  # generous — always >= n cells
    cells = []
    for row in range(-half_cols, half_cols + 1):
        for col in range(-half_cols, half_cols + 1):
            x, z = col * eff_spacing, row * eff_spacing
            cells.append((math.hypot(x, z), row, col, x, z))
    cells.sort(key=lambda c: (c[0], c[1], c[2]))  # deterministic tie-break
    rng = random.Random(f"{entity.name}:{n}:{eff_spacing:.3f}")
    # Jitter is what makes a grid read as "scattered", but two adjacent cells
    # jittered toward each other close the gap by up to 2x the amplitude. When
    # we know the model's real footprint, clamp the amplitude to the slack the
    # cell actually has, so the closest pair still clears the model instead of
    # interpenetrating. Unknown footprint keeps the original amplitude, so
    # unmeasured projects stay byte-identical.
    amp = _SCATTER_JITTER * eff_spacing
    if footprint > 0.0:
        slack = max(0.0, eff_spacing - footprint)
        amp = min(amp, slack * 0.5)
    pts: list[list[float]] = []
    for _, _row, _col, x, z in cells[:n]:
        jx = (rng.random() * 2 - 1) * amp
        jz = (rng.random() * 2 - 1) * amp
        pts.append([cx + x + jx, y, cz + z + jz])
    return pts


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
        where = (f"attached to '{platform.name}' at x={spawn_x:g}"
                 if platform is not None else "on the drop axis")
        msgs.append(
            f"Player group '{player.name}' spawns at the top of the corridor "
            f"(y={top_y:g}, {where}) instead of standing on the terminal plane")

    # ── Corridor content: stratified below the top, never all at y = 0 ───────
    ordered = sorted(corridor, key=lambda e: (-e.group_radius, e.name))
    span = _CORRIDOR_BAND_TOP - _CORRIDOR_BAND_FLOOR
    banded: list[str] = []
    for i, e in enumerate(ordered):
        # Evenly spaced band centers, largest group highest. A single group
        # takes the top band — near the player, where it can be reached.
        frac = _CORRIDOR_BAND_TOP - span * (i / max(1, len(ordered) - 1))
        hint = entity_altitude_hint(e)
        if hint is not None:
            y = round(max(0.0, min(height, hint)), 3)
            band_why = f"{e.name}@{y:g}(schema)"
        elif is_water and is_water_dweller(e):
            # Below the surface plane: this group's home is the medium itself.
            y = round(-_WATER_DEPTH_DEFAULT * frac, 3)
            band_why = f"{e.name}@{y:g}(submerged)"
        else:
            y = round(height * frac, 3)
            band_why = f"{e.name}@{y:g}"
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
    if banded:
        msgs.append(
            f"Stratified {len(banded)} group(s) down the corridor "
            f"({', '.join(banded)})")

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


def compute_layout(schema: UnitySchema,
                   footprints: Optional[dict[str, float]] = None) -> list[str]:
    """Select a spatial archetype for the scene, then lay every
    {single,line,ring,grid,scatter} entity group out inside it, baking
    `origin_offset`, `group_radius` and `instance_positions` onto each
    EntitySpec.

    ArenaPlane (planar + ground) is the original behaviour, described below and
    unchanged. VerticalCorridor (vertical + air/water) drops the scene down a
    corridor instead — see `_layout_vertical_corridor`. Any other pair of
    axes raises `[layout.strict]` rather than being flattened onto a ground
    plane.

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
    """
    if schema.game_type in ("2D", "2d_topdown"):
        return []

    layoutable = [e for e in schema.entities if e.placement in _LAYOUT_PLACEMENTS]
    if not layoutable:
        # Nothing to place (e.g. a MainMenu-only schema). There is no layout to
        # get wrong, so there is nothing for an archetype to decide either.
        return []

    # Raises [layout.strict] when no Stage-1 primitive covers the derived axes.
    # Deliberately NOT caught here: a silent fall-through to ArenaPlane is the
    # failure mode this selection exists to remove.
    arch = select_archetype(schema)
    msgs: list[str] = [arch.provenance()]
    if arch.name == "VerticalCorridor":
        return msgs + _layout_vertical_corridor(schema, arch, footprints)
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

    moved: list[str] = []
    for e in packable:
        cx, cz, _r = slots[e.name]
        y = elevations.get(e.name, 0.0)
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
