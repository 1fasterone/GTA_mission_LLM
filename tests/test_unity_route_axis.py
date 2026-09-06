"""
src/unity_route_axis.py -- step 1 of docs/COURSE_ARCHETYPE_LAYERS.md's
migration path.

Two concerns kept separate:
  1. `sample_n` must be BYTE-IDENTICAL to the inline arc-length sampler it
     replaced in `unity_layout._sample_polyline` -- proven here against a
     literal copy of the pre-refactor code, and end-to-end by
     TestArenaPlaneUnchanged.test_layout_matches_the_pre_change_recording
     in tests/test_unity_layout_archetype.py still passing untouched.
  2. `sample`/`project`/the zone vocabulary are NEW primitives with no prior
     behavior to match -- tested on their own mathematical properties
     (round-trip, sign convention, wraparound).
"""
from __future__ import annotations

import math
import random

from src.unity_route_axis import (
    RouteModifier,
    RouteZone,
    matching_zones,
    project,
    resolve_layer,
    resolve_offset,
    sample,
    sample_n,
    zone_offset,
)


# ── 1. Byte-identical to the code this replaced ─────────────────────────────

def _legacy_sample_polyline(pts, n):
    """Verbatim copy of `unity_layout._sample_polyline`'s pre-refactor body
    (the exact code being replaced) -- the regression oracle for `sample_n`."""
    seglens = [math.hypot(b[0] - a[0], b[2] - a[2]) for a, b in zip(pts, pts[1:])]
    total = sum(seglens)
    if total < 1e-9 or n <= 0:
        return [(pts[0][0], pts[0][2], -0.0, 1.0)] * max(0, n)
    out = []
    for k in range(n):
        target = total * (k + 0.5) / n
        acc = 0.0
        for (a, b), seg_len in zip(zip(pts, pts[1:]), seglens):
            if seg_len < 1e-9:
                continue
            if acc + seg_len >= target:
                t = (target - acc) / seg_len
                px, pz = a[0] + (b[0] - a[0]) * t, a[2] + (b[2] - a[2]) * t
                tx, tz = (b[0] - a[0]) / seg_len, (b[2] - a[2]) / seg_len
                out.append((px, pz, -tz, tx))
                break
            acc += seg_len
        else:
            a = pts[-1]
            out.append((a[0], a[2], -0.0, 1.0))
    return out


def _random_polyline(rng: random.Random, n_points: int) -> list[list[float]]:
    pts = [[0.0, 0.0, 0.0]]
    for _ in range(n_points - 1):
        last = pts[-1]
        pts.append([last[0] + rng.uniform(-20, 20), 0.0, last[2] + rng.uniform(-20, 20)])
    return pts


class TestSampleNMatchesLegacy:
    def test_simple_square_loop(self):
        pts = [[0, 0, 0], [100, 0, 0], [100, 0, 100], [0, 0, 100], [0, 0, 0]]
        assert sample_n(pts, 12) == _legacy_sample_polyline(pts, 12)

    def test_open_zigzag(self):
        pts = [[0, 0, 0], [10, 0, 5], [5, 0, 20], [30, 0, 25], [40, 0, 0]]
        assert sample_n(pts, 7) == _legacy_sample_polyline(pts, 7)

    def test_degenerate_zero_length(self):
        pts = [[5, 0, 5], [5, 0, 5], [5, 0, 5]]
        assert sample_n(pts, 4) == _legacy_sample_polyline(pts, 4)

    def test_n_zero(self):
        pts = [[0, 0, 0], [10, 0, 0]]
        assert sample_n(pts, 0) == _legacy_sample_polyline(pts, 0)

    def test_random_polylines_across_many_seeds(self):
        rng = random.Random(42)
        for _ in range(200):
            pts = _random_polyline(rng, rng.randint(2, 12))
            n = rng.randint(1, 20)
            assert sample_n(pts, n) == _legacy_sample_polyline(pts, n)


# ── 2. sample_n is exactly n calls to sample() at bucket centres ────────────

class TestSampleConsistentWithSampleN:
    def test_sample_n_matches_individual_sample_calls(self):
        """Same math, not necessarily the same float64 bits: `sample_n`
        computes its arc-length target as `total*(k+0.5)/n` (matching the
        legacy code it replaces, see its docstring) while `sample(pts, t)`
        computes `total*t` for an already-divided t -- `(a*b)/c` vs
        `a*(b/c)` can differ in the last ULP. Approximate equality is the
        right check here; exact equality is what TestSampleNMatchesLegacy
        holds `sample_n` to against its actual byte-identical contract."""
        pts = [[0, 0, 0], [50, 0, 0], [50, 0, 50], [0, 0, 50]]
        n = 9
        anchors = sample_n(pts, n)
        for k, (px, pz, nx, nz) in enumerate(anchors):
            x, z, tx, tz = sample(pts, (k + 0.5) / n)
            assert abs(px - x) < 1e-9 and abs(pz - z) < 1e-9
            assert abs(nx - (-tz)) < 1e-9 and abs(nz - tx) < 1e-9


# ── 3. project() -- new primitive, tested on its own properties ────────────

class TestProjectRoundTrip:
    def test_points_on_the_polyline_round_trip(self):
        pts = [[0, 0, 0], [40, 0, 0], [40, 0, 40], [0, 0, 40], [0, 0, 0]]
        for t in (0.0, 0.1, 0.25, 0.5, 0.63, 0.9, 0.999):
            x, z, _tx, _tz = sample(pts, t)
            t2, lateral = project(pts, x, z)
            assert abs(t2 - t) < 1e-6, f"t round-trip failed at t={t}: got {t2}"
            assert abs(lateral) < 1e-6, f"on-line point got nonzero lateral: {lateral}"

    def test_lateral_sign_matches_left_normal_convention(self):
        """Offsetting a sampled point along its own left normal by +d must
        report lateral == +d (and -d for the opposite side) -- the
        convention `sample_n`'s (nx, nz) is documented to mean."""
        pts = [[0, 0, 0], [100, 0, 0]]  # straight line along +X
        x, z, tx, tz = sample(pts, 0.5)
        nx, nz = -tz, tx
        for d in (3.0, -7.5):
            ox, oz = x + nx * d, z + nz * d
            _t, lateral = project(pts, ox, oz)
            assert abs(lateral - d) < 1e-6, f"expected lateral={d}, got {lateral}"

    def test_random_offsets_recover_distance_magnitude(self):
        """Near a polyline BEND, a laterally-offset point's true globally
        closest point can legitimately fall on the neighboring segment
        instead of the one it was offset from -- that's correct nearest-point
        behavior, not a bug, so this only asserts exact recovery on a single
        straight segment (no bend to be near) with offsets kept well inside
        that segment's own span, away from its endpoints."""
        rng = random.Random(7)
        pts = [[0, 0, 0], [100, 0, 0]]
        for _ in range(100):
            t = rng.uniform(0.2, 0.8)
            x, z, tx, tz = sample(pts, t)
            nx, nz = -tz, tx
            d = rng.uniform(-15, 15)
            ox, oz = x + nx * d, z + nz * d
            _t2, lateral = project(pts, ox, oz)
            assert abs(abs(lateral) - abs(d)) < 1e-4


# ── 4. RouteZone / matching_zones / resolve_layer ───────────────────────────

class TestRouteZones:
    def test_contains_simple_range(self):
        z = RouteZone(start_t=0.2, end_t=0.5)
        assert not z.contains(0.1)
        assert z.contains(0.2)
        assert z.contains(0.35)
        assert not z.contains(0.5)

    def test_contains_wraps_across_zero(self):
        z = RouteZone(start_t=0.9, end_t=0.1)
        assert z.contains(0.95)
        assert z.contains(0.05)
        assert not z.contains(0.5)

    def test_nested_zone_wins_layer_via_last_match(self):
        wide = RouteZone(start_t=0.0, end_t=1.0, modifier=RouteModifier.RIPPLE,
                          layer="sand", label="WhoopSection")
        narrow = RouteZone(start_t=0.4, end_t=0.5, modifier=RouteModifier.DIP,
                            layer="rock", label="RockyWash")
        zones = [wide, narrow]
        assert [z.label for z in matching_zones(zones, 0.45)] == ["WhoopSection", "RockyWash"]
        assert resolve_layer(zones, 0.45) == "rock"
        assert resolve_layer(zones, 0.6) == "sand"

    def test_resolve_layer_default_when_no_zone_matches(self):
        zones = [RouteZone(start_t=0.0, end_t=0.3, layer="sand")]
        assert resolve_layer(zones, 0.7, default="dune") == "dune"


# ── 5. zone_offset / resolve_offset ─────────────────────────────────────────

class TestZoneOffset:
    def test_flat_is_always_zero(self):
        z = RouteZone(start_t=0.0, end_t=1.0, modifier=RouteModifier.FLAT, amplitude=5.0)
        assert zone_offset(z, 0.0) == 0.0
        assert zone_offset(z, 0.37) == 0.0

    def test_dip_is_constant_negative_amplitude(self):
        z = RouteZone(start_t=0.0, end_t=1.0, modifier=RouteModifier.DIP, amplitude=1.32)
        assert zone_offset(z, 0.1) == -1.32
        assert zone_offset(z, 0.9) == -1.32

    def test_ripple_oscillates_with_frequency(self):
        z = RouteZone(start_t=0.0, end_t=1.0, modifier=RouteModifier.RIPPLE,
                      amplitude=2.0, frequency=1.0)
        assert abs(zone_offset(z, 0.0)) < 1e-9        # sin(0) = 0
        assert abs(zone_offset(z, 0.25) - 2.0) < 1e-9  # sin(pi/2) = 1 * amplitude

    def test_rough_is_deterministic_given_same_inputs(self):
        z = RouteZone(start_t=0.0, end_t=1.0, modifier=RouteModifier.ROUGH,
                      amplitude=1.0, label="TestRough")
        a = zone_offset(z, 0.42, seed="EntityA")
        b = zone_offset(z, 0.42, seed="EntityA")
        c = zone_offset(z, 0.42, seed="EntityB")
        assert a == b, "same (zone, phase, seed) must reproduce exactly"
        assert a != c, "different seeds should (almost certainly) differ"
        assert -1.0 <= a <= 1.0

    def test_resolve_offset_uses_last_matching_zone(self):
        wide = RouteZone(start_t=0.0, end_t=1.0, modifier=RouteModifier.DIP, amplitude=1.0)
        narrow = RouteZone(start_t=0.4, end_t=0.5, modifier=RouteModifier.DIP, amplitude=5.0)
        zones = [wide, narrow]
        assert resolve_offset(zones, 0.45) == -5.0   # narrow (last match) wins
        assert resolve_offset(zones, 0.7) == -1.0    # only wide matches here

    def test_resolve_offset_default_when_no_zone_matches(self):
        zones = [RouteZone(start_t=0.0, end_t=0.3, modifier=RouteModifier.DIP, amplitude=9.0)]
        assert resolve_offset(zones, 0.7, default=0.0) == 0.0
        assert resolve_offset(zones, 0.7, default=-2.5) == -2.5
