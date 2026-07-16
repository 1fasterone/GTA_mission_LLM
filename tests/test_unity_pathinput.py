"""Tests for the deterministic path-input front door (src/unity_pathinput.py)
and the custom-centerline mode of the racetrack composer."""

import math

import pytest

from src.unity_pathinput import (MAX_POINTS, build_route, close_if_near,
                                 parse_points_text, resample_polyline,
                                 simplify_to_cap)
from src.unity_racetrack import compose_racetrack
from src.unity_schema import UnitySchema


# ── text parsing ─────────────────────────────────────────────────────────────

class TestParsePointsText:
    def test_two_numbers_are_ground_plane_xz(self):
        assert parse_points_text("0,0; 10,20") == [[0, 0.0, 0], [10, 0.0, 20]]

    def test_three_and_four_numbers_pass_through(self):
        pts = parse_points_text("0,1,2; 3,4,5,15")
        assert pts == [[0, 1, 2], [3, 4, 5, 15]]

    def test_newlines_work_as_separators(self):
        assert len(parse_points_text("0,0\n10,0\n10,10")) == 3

    def test_malformed_number_fails_loudly(self):
        with pytest.raises(ValueError, match="unparseable"):
            parse_points_text("0,0; ten,0")

    def test_wrong_arity_fails_loudly(self):
        with pytest.raises(ValueError, match="numbers"):
            parse_points_text("0,0; 1,2,3,4,5")

    def test_single_point_fails(self):
        with pytest.raises(ValueError, match="at least 2"):
            parse_points_text("5,5")


# ── resampling / closing / simplification ────────────────────────────────────

class TestResample:
    def test_under_cap_passes_through(self):
        pts = [[float(i), 0.0, 0.0] for i in range(10)]
        assert resample_polyline(pts) == pts

    def test_over_cap_resamples_to_cap(self):
        pts = [[float(i), 0.0, 0.0] for i in range(200)]
        out = resample_polyline(pts)
        assert len(out) == MAX_POINTS
        assert out[0] == [0.0, 0.0, 0.0]
        assert out[-1] == [199.0, 0.0, 0.0]

    def test_closed_input_stays_closed(self):
        n = 100
        pts = [[math.cos(2 * math.pi * i / n) * 50,
                0.0,
                math.sin(2 * math.pi * i / n) * 50] for i in range(n)]
        pts.append(list(pts[0]))
        out = resample_polyline(pts)
        assert len(out) == MAX_POINTS
        assert out[0] == out[-1]

    def test_bank_lerps_through_resample(self):
        pts = ([[0.0, 0.0, 0.0, 20.0]]
               + [[float(i), 0.0, 0.0] for i in range(1, 199)]
               + [[199.0, 0.0, 0.0, 20.0]])
        out = resample_polyline(pts)
        assert out[0][3] == 20.0 and out[-1][3] == 20.0


class TestCloseIfNear:
    def test_far_endpoints_stay_open(self):
        pts, closed = close_if_near([[0, 0, 0], [100, 0, 0], [100, 0, 100]])
        assert not closed and pts[-1] == [100, 0, 100]

    def test_near_endpoints_get_closed(self):
        pts, closed = close_if_near(
            [[0, 0, 0], [100, 0, 0], [100, 0, 100], [0, 0, 100], [0, 0, 2]])
        assert closed and pts[-1][:3] == [0, 0, 0]


class TestSimplifyToCap:
    def test_dense_polyline_fits_cap_and_keeps_shape(self):
        n = 400
        pts = [[math.cos(2 * math.pi * i / n) * 80, 0.0,
                math.sin(2 * math.pi * i / n) * 80] for i in range(n + 1)]
        out = simplify_to_cap(pts)
        assert 3 < len(out) <= MAX_POINTS
        for p in out:  # simplified points still lie on the circle
            assert abs(math.hypot(p[0], p[2]) - 80) < 1.0


# ── route building ───────────────────────────────────────────────────────────

class TestBuildRoute:
    def test_route_is_p1_normalized(self):
        route = build_route([[50, 0, 50], [90, 0, 50], [90, 0, 90]], width=8.0)
        assert route["path_points"][0] == [0.0, 0.0, 0.0]
        # First leg heads +Z after normalization.
        assert route["path_points"][1][0] == 0.0
        assert route["path_points"][1][2] > 0.0
        assert route["path_width"] == 8.0

    def test_forced_close_appends_duplicate(self):
        route = build_route([[0, 0, 0], [100, 0, 0], [100, 0, 100]], close=True)
        assert route["closed"]
        assert route["path_points"][0] == route["path_points"][-1]

    def test_deterministic(self):
        pts = [[0, 0, 0], [60, 0, 0], [80, 0, 40], [40, 0, 80], [0, 0, 60], [0, 0, 4]]
        assert build_route(pts) == build_route(pts)


# ── image tracing ────────────────────────────────────────────────────────────

class TestTraceImage:
    def _draw_loop(self, tmp_path):
        Image = pytest.importorskip("PIL.Image")
        ImageDraw = pytest.importorskip("PIL.ImageDraw")
        img = Image.new("L", (256, 256), 255)
        draw = ImageDraw.Draw(img)
        draw.rectangle([40, 60, 216, 196], outline=0, width=3)
        path = tmp_path / "track.png"
        img.save(path)
        return path

    def test_traced_loop_fits_cap_and_closes(self, tmp_path):
        from src.unity_pathinput import trace_image
        pts = trace_image(str(self._draw_loop(tmp_path)), world_size=160.0)
        assert 4 <= len(pts) <= MAX_POINTS
        assert pts[0][:3] == pts[-1][:3]        # a drawn rectangle is a loop

    def test_traced_extent_matches_world_size(self, tmp_path):
        from src.unity_pathinput import trace_image
        pts = trace_image(str(self._draw_loop(tmp_path)), world_size=160.0)
        xs = [p[0] for p in pts]
        zs = [p[2] for p in pts]
        assert abs(max(max(xs) - min(xs), max(zs) - min(zs)) - 160.0) < 8.0

    def test_blank_image_fails_loudly(self, tmp_path):
        Image = pytest.importorskip("PIL.Image")
        from src.unity_pathinput import trace_image
        blank = tmp_path / "blank.png"
        Image.new("L", (64, 64), 255).save(blank)
        with pytest.raises(ValueError, match="no drawn line"):
            trace_image(str(blank))

    def test_trace_is_deterministic(self, tmp_path):
        from src.unity_pathinput import trace_image
        path = str(self._draw_loop(tmp_path))
        assert trace_image(path) == trace_image(path)


# ── custom-centerline circuit composition ────────────────────────────────────

def _schema():
    return UnitySchema(
        project_name="RouteT", company_name="C",
        description="a drift racing arcade game",
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.2", color_space="Linear", scenes=["Main"],
        entities=[],
    )


class TestComposeRacetrackCustomRoute:
    _SQUARE = [[0.0, 0.0, 0.0], [0.0, 0.0, 80.0], [80.0, 0.0, 80.0],
               [80.0, 0.0, 0.0], [0.0, 0.0, 0.0]]

    def test_custom_centerline_becomes_track_surface(self):
        schema = _schema()
        msgs = compose_racetrack(schema, centerline=self._SQUARE, track_width=12.0)
        assert msgs and "authored route" in msgs[0]
        track = next(e for e in schema.entities if e.name == "TrackSurface")
        assert track.path_points == self._SQUARE
        assert track.path_width == 12.0

    def test_barrier_rails_flank_the_centerline(self):
        schema = _schema()
        compose_racetrack(schema, centerline=self._SQUARE, track_width=12.0)
        outer = next(e for e in schema.entities if e.name == "TrackBarriersOuter")
        inner = next(e for e in schema.entities if e.name == "TrackBarriersInner")
        # Outer rail encloses the square; inner rail sits inside it. At a
        # sharp 90-degree corner the averaged-tangent offset projects to
        # rail / sqrt(2) per axis (a beveled corner), so assert against
        # that, not the full rail distance.
        corner = (12.0 / 2 + 1.5) / math.sqrt(2)
        assert min(p[0] for p in outer.path_points) <= -corner + 0.1
        assert min(p[0] for p in inner.path_points) >= corner - 0.1

    def test_open_course_grid_forms_behind_p1(self):
        # Open route heading +Z: grid must sit at z < 0, inside the width.
        schema = _schema()
        compose_racetrack(
            schema, centerline=[[0.0, 0.0, 0.0], [0.0, 0.0, 120.0]],
            track_width=10.0)
        opponents = next(e for e in schema.entities if e.name == "OpponentKarts")
        for x, _y, z in opponents.instance_positions:
            assert z < 0.0
            assert abs(x) <= 5.0
        kart = next(e for e in schema.entities if e.name == "PlayerKart")
        assert kart.instance_yaws == [0.0]

    def test_default_scaffold_unchanged_by_new_parameters(self):
        s1, s2 = _schema(), _schema()
        compose_racetrack(s1)
        compose_racetrack(s2, centerline=None, track_width=None)
        assert ([e.model_dump() for e in s1.entities]
                == [e.model_dump() for e in s2.entities])

    def test_custom_region_bounds_cover_the_route(self):
        schema = _schema()
        compose_racetrack(schema, centerline=self._SQUARE, track_width=12.0)
        xmin, zmin, xmax, zmax = schema.world_spec.regions[0].bounds
        assert xmin < 0 and zmin < 0 and xmax > 80 and zmax > 80
