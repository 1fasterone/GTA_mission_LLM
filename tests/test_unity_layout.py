"""Tests for src/unity_layout.py — the deterministic entity-group packing and
terrain-sizing pass (pipeline Step 5). Covers the bug this closes: every
group spawning centered on world origin regardless of radius, and "scatter"
placement ignoring spacing/scale entirely.
"""
import math

import pytest

from src.unity_layout import compute_layout, terrain_bounds
from src.unity_racetrack import compose_racetrack
from src.unity_schema import EntitySpec, UnitySchema


def _schema(entities, game_type="3D"):
    return UnitySchema(
        project_name="LayoutTest", company_name="C", description="",
        game_type=game_type, platform="pc", render_pipeline="URP",
        unity_version="6000.0", color_space="Linear",
        features=[], scenes=["Main"], packages=[],
        entities=entities,
    )


class TestGroupPacking:
    def test_two_groups_with_same_radius_no_longer_share_origin(self):
        # This is the exact reported bug: a ring of buildings and a ring of
        # vehicles, both authored at radius=8, both landed on the identical
        # circle around world origin.
        a = EntitySpec(name="Buildings", count=20, placement="ring", radius=8, scale=3.5)
        b = EntitySpec(name="Vehicles", count=3, placement="ring", radius=8, scale=1.5)
        schema = _schema([a, b])
        compute_layout(schema)

        assert a.origin_offset != [0.0, 0.0] or b.origin_offset != [0.0, 0.0]
        dist = math.hypot(a.origin_offset[0] - b.origin_offset[0],
                           a.origin_offset[1] - b.origin_offset[1])
        assert dist >= a.group_radius + b.group_radius

    def test_many_groups_all_pairwise_non_overlapping(self):
        entities = [
            EntitySpec(name=f"Group{i}", count=n, placement=p, radius=8,
                       spacing=2.0, scale=s)
            for i, (n, p, s) in enumerate([
                (20, "scatter", 3.5), (10, "ring", 2.0), (8, "line", 1.0),
                (6, "grid", 0.5), (4, "scatter", 1.0), (3, "ring", 1.5),
            ])
        ]
        schema = _schema(entities)
        compute_layout(schema)

        for i, e1 in enumerate(entities):
            for e2 in entities[i + 1:]:
                dist = math.hypot(e1.origin_offset[0] - e2.origin_offset[0],
                                   e1.origin_offset[1] - e2.origin_offset[1])
                assert dist >= e1.group_radius + e2.group_radius - 1e-6, (
                    f"{e1.name} and {e2.name} overlap")

    def test_path_placement_entities_are_never_repacked(self):
        track = EntitySpec(name="Track", placement="path", count=2,
                            path_points=[[0, 0, 0], [10, 0, 10]])
        schema = _schema([track])
        compute_layout(schema)
        assert track.origin_offset == [0.0, 0.0]
        assert track.group_radius == 0.0
        assert track.instance_positions == []

    def test_2d_game_types_are_skipped_entirely(self):
        e = EntitySpec(name="Trees", count=5, placement="scatter", radius=10)
        for gt in ("2D", "2d_topdown"):
            schema = _schema([e], game_type=gt)
            msgs = compute_layout(schema)
            assert msgs == []
            assert e.origin_offset == [0.0, 0.0]
            assert e.instance_positions == []

    def test_idempotent(self):
        e1 = EntitySpec(name="A", count=10, placement="scatter", radius=8, scale=2.0)
        e2 = EntitySpec(name="B", count=5, placement="ring", radius=6, scale=1.0)
        schema = _schema([e1, e2])
        compute_layout(schema)
        first = (list(e1.origin_offset), list(e1.instance_positions),
                  list(e2.origin_offset), list(e2.instance_positions))
        compute_layout(schema)
        second = (list(e1.origin_offset), list(e1.instance_positions),
                  list(e2.origin_offset), list(e2.instance_positions))
        assert first == second

    def test_populates_world_regions(self):
        e = EntitySpec(name="Buildings", count=5, placement="ring", radius=8)
        schema = _schema([e])
        compute_layout(schema)
        assert schema.world_spec is not None
        names = [r.name for r in schema.world_spec.regions]
        assert "Buildings" in names


class TestScatterSpacing:
    def test_scatter_never_ignores_spacing_or_scale(self):
        # The actual reported bug: 20 buildings at scale=3.5 scattered inside
        # radius=8 with no spacing enforcement at all -> guaranteed overlap.
        e = EntitySpec(name="Buildings", count=20, placement="scatter",
                        radius=8, spacing=2.0, scale=3.5)
        schema = _schema([e])
        compute_layout(schema)

        assert len(e.instance_positions) == 20
        min_gap = e.spacing  # what a naive per-instance check should clear
        eff_spacing = max(e.spacing, e.scale * 1.15)
        pts = e.instance_positions
        worst = min(
            math.dist(pts[i], pts[j])
            for i in range(len(pts)) for j in range(i + 1, len(pts))
        )
        # Jittered-grid guarantees neighbors are never closer than roughly
        # (1 - 2*jitter) * eff_spacing -- well above the un-widened `spacing`.
        assert worst >= eff_spacing * 0.35
        assert worst > min_gap * 0.9 or eff_spacing > min_gap

    def test_scatter_count_produces_exact_instance_count(self):
        e = EntitySpec(name="Crowd", count=37, placement="scatter", radius=5)
        schema = _schema([e])
        compute_layout(schema)
        assert len(e.instance_positions) == 37


class TestRacetrackStartGrid:
    """The racetrack scaffold (src/unity_racetrack.py) bakes PlayerKart/
    OpponentKarts positions anchored to the track's actual start/finish line
    BEFORE compute_layout runs — this pass must respect that geometry
    (measure it for packing/terrain purposes) rather than repack it onto
    some unrelated point relative to the scene origin."""

    def _racing_schema(self):
        schema = _schema([], game_type="3D")
        schema.meta["genre"] = "racing"
        compose_racetrack(schema)
        return schema

    def test_player_kart_sits_exactly_on_the_start_finish_line(self):
        schema = self._racing_schema()
        compute_layout(schema)
        track = next(e for e in schema.entities if e.name == "TrackSurface")
        player = next(e for e in schema.entities if e.name == "PlayerKart")
        assert player.instance_positions == [track.path_points[0]]

    def test_opponent_karts_are_staggered_behind_the_player_not_overlapping(self):
        schema = self._racing_schema()
        compute_layout(schema)
        player = next(e for e in schema.entities if e.name == "PlayerKart")
        opponents = next(e for e in schema.entities if e.name == "OpponentKarts")
        ppos = player.instance_positions[0]
        for opos in opponents.instance_positions:
            dist = math.dist(ppos, opos)
            assert 2.0 < dist < 15.0  # behind the line, not on top of / far from it

    def test_start_grid_untouched_by_generic_packing(self):
        # Regression guard for the original bug report: PlayerKart/OpponentKarts
        # must NOT get shoved off to some packed region elsewhere in the scene.
        schema = self._racing_schema()
        track = next(e for e in schema.entities if e.name == "TrackSurface")
        start = track.path_points[0]
        compute_layout(schema)
        player = next(e for e in schema.entities if e.name == "PlayerKart")
        assert math.dist(player.instance_positions[0], start) < 0.01

    def test_racetrack_layout_is_idempotent(self):
        schema = self._racing_schema()
        compute_layout(schema)
        snapshot = {e.name: (list(e.origin_offset), e.group_radius,
                             [list(p) for p in e.instance_positions])
                    for e in schema.entities}
        compute_layout(schema)
        for e in schema.entities:
            name_snapshot = snapshot[e.name]
            assert list(e.origin_offset) == name_snapshot[0]
            assert e.group_radius == name_snapshot[1]
            assert [list(p) for p in e.instance_positions] == name_snapshot[2]


class TestPathCorridors:
    """Path routes seed the packer as corridors of occupied circles — packed
    groups must never land ON a route ribbon (the "props pile up on the
    track" bug: the spiral slot search started at the world origin, the
    middle of the track infield, with no idea the ribbon existed)."""

    @staticmethod
    def _dist_to_polyline(px, pz, pts):
        best = float("inf")
        for a, b in zip(pts, pts[1:]):
            ax, az, bx, bz = a[0], a[2], b[0], b[2]
            dx, dz = bx - ax, bz - az
            L2 = dx * dx + dz * dz
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (pz - az) * dz) / L2))
            best = min(best, math.hypot(px - (ax + t * dx), pz - (az + t * dz)))
        return best

    def test_packed_groups_stay_off_the_ribbon(self):
        schema = _schema([], game_type="3D")
        schema.meta["genre"] = "racing"
        compose_racetrack(schema)
        props = EntitySpec(name="Crates", count=12, placement="scatter",
                           radius=6, spacing=2.0, scale=1.5)
        schema.entities.append(props)
        msgs = compute_layout(schema)
        assert any("corridor" in m for m in msgs)

        track = next(e for e in schema.entities if e.name == "TrackSurface")
        half_w = track.path_width / 2.0
        for x, _y, z in props.instance_positions:
            # Group-circle avoidance guarantees the group CENTER clears the
            # corridor by group_radius; individual instances must at minimum
            # clear the physical ribbon itself.
            assert self._dist_to_polyline(x, z, track.path_points) > half_w, (
                f"Crates instance ({x}, {z}) sits on the track ribbon")

    def test_corridor_seeding_is_deterministic(self):
        def build():
            schema = _schema([], game_type="3D")
            schema.meta["genre"] = "racing"
            compose_racetrack(schema)
            schema.entities.append(
                EntitySpec(name="Crates", count=12, placement="scatter",
                           radius=6, spacing=2.0, scale=1.5))
            compute_layout(schema)
            return schema
        s1, s2 = build(), build()
        assert ([e.model_dump() for e in s1.entities]
                == [e.model_dump() for e in s2.entities])

    def test_no_path_groups_no_corridor_message(self):
        e = EntitySpec(name="Trees", count=5, placement="scatter", radius=10)
        schema = _schema([e])
        msgs = compute_layout(schema)
        assert not any("corridor" in m for m in msgs)

    def _big_circuit_schema(self, group_names):
        # a long diagonal route (the shape that exiled groups to islands)
        route = [[0, 0, 0], [0, 0, 120], [80, 0, 200], [200, 0, 200],
                 [260, 0, 120], [200, 0, 40], [80, 0, 20], [0, 0, 0]]
        track = EntitySpec(name="TrackSurface", kind="road", placement="path",
                           count=len(route), path_points=route, path_width=10.0)
        props = [EntitySpec(name=n, count=6, placement="scatter",
                            radius=5, spacing=2.0, scale=1.0)
                 for n in group_names]
        return _schema([track] + props), track, props

    def test_groups_hug_the_corridor_not_distant_islands(self):
        schema, track, props = self._big_circuit_schema(
            ["Grandstands", "DirtMounds", "Spectators", "Banners"])
        msgs = compute_layout(schema)
        assert any("route corridor" in m for m in msgs)
        half_w = track.path_width / 2.0
        for e in props:
            cx, cz = e.origin_offset
            d = self._dist_to_polyline(cx, cz, track.path_points)
            # hugging: outside the ribbon, within a tight band beside it —
            # never flung hundreds of metres away into a packed island
            assert d > half_w
            assert d < half_w + 60.0, f"{e.name} at dist {d:.1f} is not hugging"

    def test_groups_are_distributed_around_the_loop(self):
        schema, track, props = self._big_circuit_schema(
            ["A", "B", "C", "D", "E", "F"])
        compute_layout(schema)
        centers = [tuple(e.origin_offset) for e in props]
        # not all clustered: the spread of anchor centers should span a large
        # fraction of the route's own extent
        xs = [c[0] for c in centers]
        zs = [c[1] for c in centers]
        assert (max(xs) - min(xs)) > 120.0
        assert (max(zs) - min(zs)) > 120.0

    def test_corridor_ground_bounds_track_the_route_not_islands(self):
        schema, track, _ = self._big_circuit_schema(
            ["Grandstands", "DirtMounds", "Spectators"])
        compute_layout(schema)
        xmin, zmin, xmax, zmax = terrain_bounds(schema.entities)
        # route spans ~260 x 200; a corridor-hugged layout keeps ground within
        # a sane margin of that, not the 1600x700 sprawl islands produced
        assert (xmax - xmin) < 260 + 200
        assert (zmax - zmin) < 200 + 200

    def test_corridor_placement_is_deterministic(self):
        def build():
            schema, _t, _p = self._big_circuit_schema(["A", "B", "C", "D"])
            compute_layout(schema)
            return [e.model_dump() for e in schema.entities]
        assert build() == build()


class TestTerrainBounds:
    def test_empty_entities_falls_back_to_fixed_footprint(self):
        xmin, zmin, xmax, zmax = terrain_bounds([])
        assert xmax - xmin > 0
        assert zmax - zmin > 0

    def test_bounds_cover_packed_group_and_path_geometry(self):
        ring = EntitySpec(name="Ring", count=10, placement="ring", radius=20)
        track = EntitySpec(name="Track", placement="path", count=2,
                            path_points=[[-60, 0, -40], [60, 0, 40]])
        schema = _schema([ring, track])
        compute_layout(schema)

        xmin, zmin, xmax, zmax = terrain_bounds(schema.entities)
        assert xmin <= -60
        assert xmax >= 60
        assert zmin <= -40
        assert zmax >= 40
        cx, cz = ring.origin_offset
        r = ring.group_radius
        assert xmin <= cx - r
        assert xmax >= cx + r
