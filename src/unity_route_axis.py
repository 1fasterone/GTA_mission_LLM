"""
Generic route-axis primitive, shared across archetypes/interpreters: given an
arbitrary OPEN polyline (world-space points, XZ used for arc length; a Y or
bank-angle 4th component is ignored here), sample points evenly by arc
length, or invert a world point back to its arc-length position plus signed
lateral offset. Also a small, medium-agnostic zone vocabulary
(RouteModifier / RouteZone) for authoring "this stretch of the route behaves
differently" as data instead of one hardcoded branch per named feature.

Steps 1-4 of docs/COURSE_ARCHETYPE_LAYERS.md's migration path:

  * `sample_n` reproduces `unity_layout._sample_polyline`'s existing
    bucket-centered ((k+0.5)/n) arc-length sampling byte-for-byte -- proven
    both by `tests/test_unity_route_axis.py` and by
    `TestArenaPlaneUnchanged.test_layout_matches_the_pre_change_recording`
    (tests/test_unity_layout_archetype.py) still passing unchanged after
    `unity_layout.py` was refactored to call this module instead of keeping
    its own private copy of the same math.
  * `project`, `RouteZone`/`matching_zones`/`resolve_layer`/`zone_offset`/
    `resolve_offset` are consumed by real pipeline code now:
    `schema.world_spec.course.zones` (`unity_schema.RouteZoneSpec`/
    `CourseSpec`) reaches `unity_layout._layout_arena_plane` (interprets `t`
    as arc-length fraction along a path-route corridor) and
    `_layout_bounded_volume` (interprets `t` as `tier_of(e)`, the existing
    altitude-fraction axis) via `unity_layout._course_zones`. Any OTHER
    archetype an authored zone reaches instead logs a loud warning
    (`compute_layout`'s `_ZONE_AWARE_ARCHETYPES` check) rather than silently
    doing nothing -- see `TestCourseZonesWiring` in
    tests/test_unity_layout_archetype.py for the proof of both halves (real
    effect where wired, loud non-effect where not, byte-identical when
    `zones` is empty).

This module intentionally does NOT do noise/heightmap synthesis (Perlin fbm,
etc.) -- that is a Step 6/7 (Unity-side) rendering concern once an interpreter
for a specific archetype/medium exists. What Step 5 (layout, this module's
actual caller) needs is POSITION and CLASSIFICATION: where is t=0.3 along
this route, and which zone/layer claims it -- not what a heightmap pixel
looks like there.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

Point = Sequence[float]  # [x, y, z] or [x, y, z, bank] -- only [0] and [2] (XZ) are read here


def _segment_lengths(pts: Sequence[Point]) -> tuple[list[float], float]:
    """Per-segment XZ lengths and their sum, for consecutive points in `pts`
    (an OPEN polyline -- no implicit closing segment from last back to
    first; a closed loop must already repeat its start point at the end if
    it wants continuity there, exactly as every existing caller assumes)."""
    seglens = [math.hypot(b[0] - a[0], b[2] - a[2]) for a, b in zip(pts, pts[1:])]
    return seglens, sum(seglens)


def _walk_to_arc_length(pts: Sequence[Point], seglens: list[float],
                        target: float) -> tuple[float, float, float, float]:
    """Shared inner walk: the point (and tangent) at absolute arc-length
    `target` along `pts`. Both `sample` and `sample_n` reduce to this, but
    each computes `target` from `t` using its OWN expression (see their
    docstrings) -- floating-point operation order matters for `sample_n`'s
    byte-identical guarantee, so that reduction happens at the caller, not
    here."""
    acc = 0.0
    for (a, b), seg_len in zip(zip(pts, pts[1:]), seglens):
        if seg_len < 1e-9:
            continue
        if acc + seg_len >= target:
            u = (target - acc) / seg_len
            x, z = a[0] + (b[0] - a[0]) * u, a[2] + (b[2] - a[2]) * u
            tx, tz = (b[0] - a[0]) / seg_len, (b[2] - a[2]) / seg_len
            return (x, z, tx, tz)
        acc += seg_len
    last = pts[-1]
    return (last[0], last[2], 0.0, 1.0)


def sample(pts: Sequence[Point], t: float) -> tuple[float, float, float, float]:
    """Point on the polyline at normalized arc-length position t (clamped to
    [0, 1]), as (x, z, tangent_x, tangent_z). A single-point or
    effectively-zero-length polyline returns that point with a default
    +Z-ish tangent, matching `sample_n`'s existing degenerate-case behavior."""
    if not pts:
        raise ValueError("sample() needs at least one point")
    if len(pts) == 1:
        return (pts[0][0], pts[0][2], 0.0, 1.0)
    seglens, total = _segment_lengths(pts)
    if total < 1e-9:
        return (pts[0][0], pts[0][2], 0.0, 1.0)
    return _walk_to_arc_length(pts, seglens, total * max(0.0, min(1.0, t)))


def sample_n(pts: Sequence[Point], n: int) -> list[tuple[float, float, float, float]]:
    """`n` anchors evenly spaced by XZ arc length along `pts`, each as
    (px, pz, nx, nz) where (nx, nz) is the unit LEFT normal to the travel
    tangent. Anchors are centered in their arc-length bucket ((k+0.5)/n) so a
    closed loop's first and last don't coincide.

    IDENTICAL contract and output to the function this replaces
    (`unity_layout._sample_polyline`) -- see this module's docstring for the
    tests that hold that guarantee. `target = total * (k + 0.5) / n` is
    computed with that EXACT operation order (matching the legacy code
    verbatim) rather than via `total * t` for a separately-divided t --
    `(a*b)/c` and `a*(b/c)` can differ in the last float64 ULP, which the
    byte-identical regression test catches."""
    if not pts:
        return []
    seglens, total = _segment_lengths(pts)
    if total < 1e-9 or n <= 0:
        return [(pts[0][0], pts[0][2], -0.0, 1.0)] * max(0, n)
    out: list[tuple[float, float, float, float]] = []
    for k in range(n):
        target = total * (k + 0.5) / n
        x, z, tx, tz = _walk_to_arc_length(pts, seglens, target)
        out.append((x, z, -tz, tx))  # left normal = tangent rotated +90 deg in XZ
    return out


def project(pts: Sequence[Point], x: float, z: float) -> tuple[float, float]:
    """Inverse of `sample`: the arc-length fraction t of the closest point on
    the polyline to (x, z), plus the SIGNED lateral offset from the
    centreline there (positive = the side `sample_n`'s left-normal points
    toward -- i.e. offsetting a route point by `+lateral * (nx, nz)`
    reconstructs the query point when the closest point is a true
    perpendicular foot). Each segment is projected onto with its parameter
    clamped to [0, 1] (closest point ON the segment, not the infinite line);
    the globally closest segment wins.

    NEW primitive -- nothing in the pipeline calls this yet (see module
    docstring)."""
    if not pts:
        raise ValueError("project() needs at least one point")
    if len(pts) == 1:
        return 0.0, math.hypot(x - pts[0][0], z - pts[0][2])
    seglens, total = _segment_lengths(pts)
    if total < 1e-9:
        return 0.0, math.hypot(x - pts[0][0], z - pts[0][2])

    best_dist = None
    best_t = 0.0
    best_lateral = 0.0
    acc = 0.0
    for (a, b), seg_len in zip(zip(pts, pts[1:]), seglens):
        if seg_len < 1e-9:
            continue
        ax, az, bx, bz = a[0], a[2], b[0], b[2]
        dx, dz = bx - ax, bz - az
        u = ((x - ax) * dx + (z - az) * dz) / (seg_len * seg_len)
        u = max(0.0, min(1.0, u))
        px, pz = ax + dx * u, az + dz * u
        dist = math.hypot(x - px, z - pz)
        if best_dist is None or dist < best_dist:
            tx, tz = dx / seg_len, dz / seg_len
            # Signed lateral = (tangent) x (query - closest-point), in 2-D
            # cross-product form. Equals +/-dist exactly when u is interior
            # (a true perpendicular foot); at a clamped segment end it's the
            # normal-direction component of the offset, still a well-defined
            # "how far left/right of this segment's own heading" value.
            lateral = tx * (z - pz) - tz * (x - px)
            best_dist, best_t, best_lateral = dist, (acc + seg_len * u) / total, lateral
        acc += seg_len
    return best_t, best_lateral


# ── Generic corridor-zone modifier vocabulary ─────────────────────────────
# Small and medium-agnostic on purpose: "ripple"/"dip"/"rough" are shape
# verbs, not desert nouns. Ported from the proof-of-concept
# (GENREOffRoadDesertRacing/Assets/Scripts/BajaLoopRoute.cs) that motivated
# this module -- see docs/COURSE_ARCHETYPE_LAYERS.md for how a desert, an
# underwater channel, a fall corridor, and a cliff face each interpret the
# same verbs differently.
class RouteModifier(str, Enum):
    FLAT = "flat"
    RIPPLE = "ripple"
    DIP = "dip"
    ROUGH = "rough"


@dataclass(frozen=True)
class RouteZone:
    start_t: float
    end_t: float
    modifier: RouteModifier = RouteModifier.FLAT
    amplitude: float = 0.0
    frequency: float = 0.0
    layer: str = ""     # which surface/material/hazard layer this zone claims
    label: str = ""      # human-readable only, never branched on

    def contains(self, t: float) -> bool:
        t = t % 1.0
        if self.start_t <= self.end_t:
            return self.start_t <= t < self.end_t
        return t >= self.start_t or t < self.end_t  # wraps across t=0


def matching_zones(zones: Sequence[RouteZone], t: float) -> list[RouteZone]:
    """Every zone covering arc position t, in the order given -- a later
    entry nested inside an earlier one's range means both are returned, so a
    caller that wants "the narrower zone overrides" semantics (as the Unity
    proof-of-concept does for its RockyWash-inside-WhoopSection nesting)
    takes the LAST one; a caller that wants additive semantics sums all of
    them. Kept as separate concerns rather than baking one policy in here."""
    return [z for z in zones if z.contains(t)]


def resolve_layer(zones: Sequence[RouteZone], t: float, default: str = "") -> str:
    """The layer claimed at arc position t: the LAST matching zone wins (a
    narrower, later-authored zone overriding a wider one it's nested inside),
    or `default` if no zone covers this t at all."""
    matches = matching_zones(zones, t)
    return matches[-1].layer or default if matches else default


def zone_offset(zone: RouteZone, phase: float, seed: str = "") -> float:
    """The numeric offset ONE zone contributes at `phase` -- whatever the
    caller's axis means there (arc-length fraction, a per-group index, a
    tier fraction). Units are the zone's own `amplitude`; what the caller
    ADDS the result to (an elevation, a heightmap pixel, a hold spacing) is
    entirely archetype-owned -- see docs/COURSE_ARCHETYPE_LAYERS.md's table
    of what each verb means per medium. Deterministic: FLAT/RIPPLE/DIP are
    pure functions of `phase`; ROUGH is a seeded `random.Random` draw (this
    repo's existing convention for reproducible jitter, e.g.
    `unity_layout._bake_positions`' `random.Random(f"{entity.name}:...")`),
    so the same (zone, phase, seed) always yields the same offset.
    """
    if zone.modifier == RouteModifier.FLAT:
        return 0.0
    if zone.modifier == RouteModifier.RIPPLE:
        freq = zone.frequency if zone.frequency > 0.0 else 1.0
        return math.sin(phase * 2.0 * math.pi * freq) * zone.amplitude
    if zone.modifier == RouteModifier.DIP:
        return -zone.amplitude
    if zone.modifier == RouteModifier.ROUGH:
        import random as _random
        rng = _random.Random(f"{seed}:{zone.label}:{phase:.6f}")
        return (rng.random() * 2.0 - 1.0) * zone.amplitude
    return 0.0  # pragma: no cover -- exhaustive over RouteModifier today


def resolve_offset(zones: Sequence[RouteZone], t: float, *,
                   phase: float | None = None, seed: str = "",
                   default: float = 0.0) -> float:
    """The offset in effect at arc position t: the LAST matching zone's
    `zone_offset` (consistent with `resolve_layer`'s "narrower/later zone
    overrides" rule), or `default` if no zone covers this t at all.
    `phase` defaults to `t` itself when not given separately."""
    matches = matching_zones(zones, t)
    if not matches:
        return default
    return zone_offset(matches[-1], phase if phase is not None else t, seed=seed)
