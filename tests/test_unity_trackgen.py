"""Tests for the bpy-free procedural race-circuit generator
(src/unity_trackgen.py) and its hand-off into the existing --route seam."""

import pytest

from src.unity_racetrack import compose_racetrack
from src.unity_schema import UnitySchema
from src.unity_trackgen import (STYLES, generate_centerline, generate_track_route,
                                _catmull_rom_centripetal, _self_intersects)


PRESET_STYLES = ("flowing", "technical", "high_speed", "kart")


# ── route contract ────────────────────────────────────────────────────────────

class TestRouteContract:
    def test_route_dict_shape(self):
        r = generate_track_route(style="flowing", seed=1)
        assert set(r) >= {"path_points", "path_width", "closed", "source"}
        assert r["closed"] is True
        assert r["source"] == "trackgen:flowing"
        assert r["path_width"] == 10.0
        assert r["seed"] == 1 and r["style"] == "flowing"

    def test_width_propagates_to_path_width(self):
        assert generate_track_route(seed=1, width=14.0)["path_width"] == 14.0

    @pytest.mark.parametrize("style", PRESET_STYLES + ("random",))
    def test_every_style_produces_a_closed_capped_route(self, style):
        r = generate_track_route(style=style, seed=3)
        pts = r["path_points"]
        assert 3 < len(pts) <= 64                 # schema path_points cap
        assert pts[0][:3] == pts[-1][:3]          # closed loop

    def test_unknown_style_raises(self):
        with pytest.raises(ValueError):
            generate_track_route(style="hexagon", seed=1)

    def test_styles_export_lists_presets_plus_random(self):
        assert set(STYLES) == set(PRESET_STYLES) | {"random"}


# ── invariants ────────────────────────────────────────────────────────────────

class TestInvariants:
    def test_p1_normalized_to_origin(self):
        # build_route → normalize_route puts P1 at the world origin.
        p1 = generate_track_route(style="technical", seed=9)["path_points"][0]
        assert abs(p1[0]) < 1e-3 and abs(p1[2]) < 1e-3

    def test_coords_within_schema_clamp(self):
        for style in PRESET_STYLES:
            for p in generate_track_route(style=style, seed=5)["path_points"]:
                assert abs(p[0]) <= 2000.0 and abs(p[2]) <= 2000.0

    def test_banking_present_and_within_cap(self):
        pts = generate_track_route(style="high_speed", seed=2)["path_points"]
        banks = [p[3] for p in pts if len(p) > 3]
        assert banks, "a curvy circuit should carry banking on at least one point"
        assert all(abs(b) <= 45.0 for b in banks)   # schema bank clamp

    def test_left_and_right_banking(self):
        # A real circuit turns both ways: banking must take both signs.
        pts = generate_track_route(style="flowing", seed=42)["path_points"]
        banks = [p[3] for p in pts if len(p) > 3]
        assert any(b > 0 for b in banks) and any(b < 0 for b in banks)

    def test_generated_loop_does_not_self_intersect(self):
        # generate_centerline returns the pre-normalization XZ loop; the
        # validated layout must not cross itself.
        loop = generate_centerline(style="technical", seed=11)
        xz = [(p[0], p[2]) for p in loop[:-1]]
        assert not _self_intersects(xz)

    def test_centerline_closes(self):
        loop = generate_centerline(style="kart", seed=4)
        assert loop[0] == loop[-1]


# ── determinism ───────────────────────────────────────────────────────────────

class TestDeterminism:
    def test_same_seed_is_byte_identical(self):
        assert generate_track_route("flowing", 7) == generate_track_route("flowing", 7)

    def test_different_seed_differs(self):
        assert generate_track_route("flowing", 7) != generate_track_route("flowing", 8)

    def test_uses_no_global_rng(self):
        # A local random.Random must not perturb the global stream between calls.
        import random
        random.seed(0)
        a = random.random()
        generate_track_route("technical", 123)
        random.seed(0)
        assert random.random() == a


# ── elevation (Gap H: tested primitive only, default off — see module docstring) ─

class TestElevation:
    def test_default_amplitude_is_flat(self):
        pts = generate_centerline(style="flowing", seed=7)
        assert all(p[1] == 0.0 for p in pts)

    def test_explicit_zero_amplitude_matches_default(self):
        a = generate_centerline(style="technical", seed=3)
        b = generate_centerline(style="technical", seed=3, elevation_amplitude=0.0)
        assert a == b

    def test_positive_amplitude_is_deterministic(self):
        a = generate_centerline(style="flowing", seed=11, elevation_amplitude=5.0)
        b = generate_centerline(style="flowing", seed=11, elevation_amplitude=5.0)
        assert a == b

    def test_positive_amplitude_varies_and_is_bounded(self):
        amp = 4.0
        pts = generate_centerline(style="kart", seed=6, elevation_amplitude=amp)
        assert any(p[1] != 0.0 for p in pts)
        assert all(abs(p[1]) <= amp + 1e-6 for p in pts)


# ── spline helper ─────────────────────────────────────────────────────────────

class TestCatmull:
    def test_endpoints_pass_through_control_points(self):
        p0, p1, p2, p3 = (0, 0), (1, 0), (2, 1), (3, 1)
        start = _catmull_rom_centripetal(p0, p1, p2, p3, 0.0)
        end = _catmull_rom_centripetal(p0, p1, p2, p3, 1.0)
        assert start == pytest.approx(p1, abs=1e-6)
        assert end == pytest.approx(p2, abs=1e-6)


# ── hand-off into the existing composer ───────────────────────────────────────

class TestComposeHandoff:
    def _schema(self):
        return UnitySchema(project_name="R", company_name="C",
                           description="race 3d circuit", game_type="3D",
                           platform="pc", render_pipeline="URP",
                           unity_version="6000.2", scenes=["Main"], entities=[])

    def test_generated_route_composes_a_full_circuit(self):
        route = generate_track_route(style="flowing", seed=7)
        schema = self._schema()
        compose_racetrack(schema, centerline=route["path_points"],
                          track_width=route["path_width"])
        names = {e.name for e in schema.entities}
        assert {"TrackSurface", "TrackBarriersOuter", "PlayerKart"} <= names
        assert any(n.startswith("TracksideStands") for n in names)
        assert any(n.startswith("TracksideTrees") for n in names)
