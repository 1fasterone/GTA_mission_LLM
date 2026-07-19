"""
Deterministic post-schema layout pass (pipeline Step 5, Fixer stage) — assigns
every entity group a non-overlapping world-space region and bakes exact
per-instance positions, closing two gaps left by EntitySpawner's runtime
placement formulas (templates/unity/EntitySpawner.cs.j2):

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

import math
import random
from typing import Sequence

from .unity_schema import EntitySpec, UnitySchema, WorldRegion, WorldSpec

# Minimum world-space gap enforced between two packed groups' footprint
# circles, on top of their own radii.
_GROUP_MARGIN = 2.0
# Fraction of a scatter grid cell an instance may be jittered by — keeps the
# "scattered" look while guaranteeing no instance drifts closer than
# roughly (1 - 2*_SCATTER_JITTER) * spacing to its grid neighbor.
_SCATTER_JITTER = 0.3
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

_LAYOUT_PLACEMENTS = frozenset({"single", "line", "ring", "grid", "scatter"})


def _effective_spacing(entity: EntitySpec) -> float:
    """A primitive's own footprint is approximated by its authored `scale`
    (Unity's built-in cube/sphere/capsule primitives are ~1 unit before
    scaling — the same approximation EntitySpawner.cs.j2 uses for the
    `modelPrefab == null` fallback case). `spacing` is an authored MINIMUM;
    the footprint can only widen it, never shrink below what was authored."""
    return max(entity.spacing, entity.scale * 1.15)


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
                        cx: float, cz: float) -> list[list[float]]:
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
    pts: list[list[float]] = []
    for _, _row, _col, x, z in cells[:n]:
        jx = (rng.random() * 2 - 1) * _SCATTER_JITTER * eff_spacing
        jz = (rng.random() * 2 - 1) * _SCATTER_JITTER * eff_spacing
        pts.append([cx + x + jx, 0.0, cz + z + jz])
    return pts


def _bake_positions(entity: EntitySpec, eff_spacing: float,
                     cx: float, cz: float) -> list[list[float]]:
    n = max(1, entity.count)
    if entity.placement == "single":
        return [[cx, 0.0, cz]]
    if entity.placement == "line":
        return [[cx + (i - (n - 1) / 2.0) * eff_spacing, 0.0, cz] for i in range(n)]
    if entity.placement == "ring":
        r = entity.group_radius
        return [[cx + r * math.cos(i * 2.0 * math.pi / n),
                 0.0, cz + r * math.sin(i * 2.0 * math.pi / n)] for i in range(n)]
    if entity.placement == "grid":
        cols = math.ceil(math.sqrt(n))
        pts = []
        for i in range(n):
            row, col = divmod(i, cols)
            pts.append([cx + (col - (cols - 1) / 2.0) * eff_spacing,
                        0.0, cz + row * eff_spacing])
        return pts
    if entity.placement == "scatter":
        return _scatter_positions(entity, eff_spacing, cx, cz)
    return []


def compute_layout(schema: UnitySchema) -> list[str]:
    """Pack every {single,line,ring,grid,scatter} entity group into a
    non-overlapping world-space region and bake exact instance positions onto
    each EntitySpec (`origin_offset`, `group_radius`, `instance_positions`).

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
    skipped here.
    """
    if schema.game_type in ("2D", "2d_topdown"):
        return []

    layoutable = [e for e in schema.entities if e.placement in _LAYOUT_PLACEMENTS]
    if not layoutable:
        return []

    preplaced = [e for e in layoutable if e.instance_positions]
    packable = [e for e in layoutable if not e.instance_positions]

    seed_circles: list[tuple[float, float, float]] = []
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
        e.group_radius = _footprint_radius(e, _effective_spacing(e))

    # When the scene has an authored route, the route is the placement GUIDE:
    # prop groups line the corridor (offset outside the barriers, spread by arc
    # length) instead of spiral-packing from the origin — which, with a long
    # diagonal corridor blocking the center, exiled them to distant islands and
    # ballooned the ground plane (`terrain_bounds` covers whatever they hit).
    from .unity_pathnorm import _primary_route
    primary = _primary_route(schema.entities) if path_groups else None
    if primary is not None and packable:
        slots = _corridor_pack(packable, primary, seed_circles)
    else:
        slots = _pack_groups(packable, seed_circles)

    moved: list[str] = []
    for e in packable:
        cx, cz, _r = slots[e.name]
        e.origin_offset = [round(cx, 3), round(cz, 3)]
        e.instance_positions = _bake_positions(e, _effective_spacing(e), cx, cz)
        if abs(cx) > 0.01 or abs(cz) > 0.01:
            moved.append(e.name)

    msgs: list[str] = []
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
