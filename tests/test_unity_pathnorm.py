"""Tests for the P1-at-origin / P1-is-start-line normalization
(src/unity_pathnorm.py)."""

import math

from src.unity_pathnorm import normalize_paths, _primary_route, _start_direction
from src.unity_racetrack import compose_racetrack
from src.unity_schema import EntitySpec, UnitySchema, WorldRegion, WorldSpec


def _schema(entities=None, **overrides):
    defaults = dict(
        project_name="PathNormT", company_name="C",
        description="a drift racing arcade game",
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.2", color_space="Linear", scenes=["Main"],
        entities=entities or [],
    )
    defaults.update(overrides)
    return UnitySchema(**defaults)


def _racing_schema():
    schema = _schema()
    compose_racetrack(schema)
    return schema


# ── primary route pick ───────────────────────────────────────────────────────

class TestPrimaryRoute:
    def test_no_paths_returns_none(self):
        assert _primary_route([EntitySpec(name="Trees", kind="prop", count=3)]) is None

    def test_tiled_route_outranks_longer_waypoint_route(self):
        short_ribbon = EntitySpec(
            name="Road", kind="road", count=2, placement="path",
            path_points=[[0, 0, 0], [10, 0, 0]], path_width=8.0)
        long_waypoints = EntitySpec(
            name="Patrol", kind="npc", count=2, placement="path",
            path_points=[[0, 0, 0], [500, 0, 0]])
        assert _primary_route([long_waypoints, short_ribbon]).name == "Road"

    def test_longest_arc_wins_within_pool(self):
        a = EntitySpec(name="A", kind="road", count=2, placement="path",
                       path_points=[[0, 0, 0], [10, 0, 0]], path_width=8.0)
        b = EntitySpec(name="B", kind="road", count=2, placement="path",
                       path_points=[[0, 0, 0], [20, 0, 0]], path_width=8.0)
        assert _primary_route([a, b]).name == "B"


# ── start direction ──────────────────────────────────────────────────────────

class TestStartDirection:
    def test_open_path_uses_outgoing_tangent(self):
        assert _start_direction([[0, 0, 0], [5, 0, 0]]) == (1.0, 0.0)

    def test_closed_loop_uses_arriving_tangent(self):
        # Square loop: the segment arriving at P1 comes up from (0,0,-10).
        loop = [[0, 0, 0], [10, 0, 0], [10, 0, -10], [0, 0, -10], [0, 0, 0]]
        fx, fz = _start_direction(loop)
        assert (round(fx, 6), round(fz, 6)) == (0.0, 1.0)

    def test_single_point_defaults_to_plus_z(self):
        assert _start_direction([[3, 0, 4]]) == (0.0, 1.0)


# ── normalization ────────────────────────────────────────────────────────────

class TestNormalizePaths:
    def test_no_path_groups_is_a_noop(self):
        schema = _schema([EntitySpec(name="Trees", kind="prop", count=3)])
        assert normalize_paths(schema) == []

    def test_2d_schemas_are_skipped(self):
        schema = _schema(
            [EntitySpec(name="Road", kind="road", count=2, placement="path",
                        path_points=[[5, 0, 5], [15, 0, 5]], path_width=8.0)],
            game_type="2D")
        assert normalize_paths(schema) == []

    def test_racetrack_p1_lands_at_origin(self):
        schema = _racing_schema()
        msgs = normalize_paths(schema)
        assert msgs and "P1" in msgs[0]
        track = next(e for e in schema.entities if e.name == "TrackSurface")
        assert track.path_points[0][:3] == [0.0, 0.0, 0.0]
        # Closed-loop convention survives the transform.
        assert track.path_points[0] == track.path_points[-1]

    def test_racetrack_race_direction_is_plus_z(self):
        schema = _racing_schema()
        normalize_paths(schema)
        track = next(e for e in schema.entities if e.name == "TrackSurface")
        fx, fz = _start_direction(track.path_points)
        assert (round(fx, 6), round(fz, 6)) == (0.0, 1.0)

    def test_pole_kart_lands_at_origin_facing_plus_z(self):
        schema = _racing_schema()
        normalize_paths(schema)
        kart = next(e for e in schema.entities if e.name == "PlayerKart")
        assert kart.instance_positions == [[0.0, 0.0, 0.0]]
        assert kart.instance_yaws == [0.0]

    def test_start_grid_stays_behind_the_line(self):
        schema = _racing_schema()
        normalize_paths(schema)
        opponents = next(e for e in schema.entities if e.name == "OpponentKarts")
        for x, _y, z in opponents.instance_positions:
            assert z < 0.0                      # strictly behind the start line
            assert abs(x) <= 5.0 - 1e-6         # inside the 10 m track width
        assert opponents.instance_yaws == [0.0, 0.0, 0.0]

    def test_relative_geometry_is_preserved(self):
        before = _racing_schema()
        after = _racing_schema()
        normalize_paths(after)
        track_b = next(e for e in before.entities if e.name == "TrackSurface")
        track_a = next(e for e in after.entities if e.name == "TrackSurface")
        # Pairwise distances between path points are invariant under a rigid
        # transform (up to the 3-decimal rounding).
        for (b1, a1), (b2, a2) in zip(
                zip(track_b.path_points, track_a.path_points),
                zip(track_b.path_points[1:], track_a.path_points[1:])):
            d_before = math.hypot(b2[0] - b1[0], b2[2] - b1[2])
            d_after = math.hypot(a2[0] - a1[0], a2[2] - a1[2])
            assert abs(d_before - d_after) < 5e-3

    def test_world_region_bounds_are_transformed(self):
        schema = _racing_schema()
        bounds_before = list(schema.world_spec.regions[0].bounds)
        normalize_paths(schema)
        bounds_after = schema.world_spec.regions[0].bounds
        assert bounds_after != bounds_before
        # The normalized track extends across the origin; its region must too.
        xmin, zmin, xmax, zmax = bounds_after
        assert xmin < 0.0 < xmax or zmin < 0.0 < zmax

    def test_idempotent(self):
        schema = _racing_schema()
        assert normalize_paths(schema)          # first run does work
        snapshot = [e.model_dump() for e in schema.entities]
        assert normalize_paths(schema) == []    # second run is a no-op
        assert [e.model_dump() for e in schema.entities] == snapshot

    def test_deterministic(self):
        s1, s2 = _racing_schema(), _racing_schema()
        normalize_paths(s1)
        normalize_paths(s2)
        assert ([e.model_dump() for e in s1.entities]
                == [e.model_dump() for e in s2.entities])

    def test_rotation_aligns_heading_and_yaws(self):
        # An open route heading +X must be rotated -90° so it heads +Z; a
        # baked instance riding it keeps its relative facing.
        schema = _schema([
            EntitySpec(name="Road", kind="road", count=2, placement="path",
                       path_points=[[10, 0, 20], [30, 0, 20]], path_width=6.0),
            EntitySpec(name="Car", kind="vehicle", count=1, placement="single",
                       instance_positions=[[10, 0, 20]], instance_yaws=[90.0]),
        ])
        normalize_paths(schema)
        road = next(e for e in schema.entities if e.name == "Road")
        assert road.path_points[0] == [0.0, 0.0, 0.0]
        assert road.path_points[1] == [0.0, 0.0, 20.0]   # +X mapped onto +Z
        car = next(e for e in schema.entities if e.name == "Car")
        assert car.instance_positions == [[0.0, 0.0, 0.0]]
        assert car.instance_yaws == [0.0]                # 90° - 90°

    def test_bank_and_elevation_survive(self):
        schema = _schema([
            EntitySpec(name="Road", kind="road", count=2, placement="path",
                       path_points=[[10, 2.0, 20, 15.0], [30, 4.0, 20]],
                       path_width=6.0),
        ])
        normalize_paths(schema)
        road = next(e for e in schema.entities if e.name == "Road")
        assert road.path_points[0] == [0.0, 2.0, 0.0, 15.0]
        assert road.path_points[1] == [0.0, 4.0, 20.0]

    def test_secondary_routes_share_the_same_transform(self):
        schema = _racing_schema()
        normalize_paths(schema)
        track = next(e for e in schema.entities if e.name == "TrackSurface")
        outer = next(e for e in schema.entities if e.name == "TrackBarriersOuter")
        # The outer barrier's own first point is NOT forced to the origin —
        # only the primary route's P1 is; the barrier moved rigidly with it.
        assert outer.path_points[0][:3] != [0.0, 0.0, 0.0]
        # Outer ring still encloses the centerline start point.
        xs = [p[0] for p in outer.path_points]
        zs = [p[2] for p in outer.path_points]
        assert min(xs) < track.path_points[0][0] < max(xs)
        assert min(zs) < track.path_points[0][2] < max(zs)
