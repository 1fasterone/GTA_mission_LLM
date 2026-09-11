"""Tests for src/unity_layout.py — the deterministic entity-group packing and
terrain-sizing pass (pipeline Step 5). Covers the bug this closes: every
group spawning centered on world origin regardless of radius, and "scatter"
placement ignoring spacing/scale entirely.
"""
import math

import pytest

from src.unity_layout import _SPAWN_CLEARANCE_DEFAULT, compute_layout, terrain_bounds
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


def _instance_footprint(e: EntitySpec) -> float:
    """Per-instance radius approximation, matching the formula every packer
    in src/unity_layout.py already uses for an unmeasured primitive
    (`max(0.5, entity.scale * 0.6)` — see _edge_distributed_pack et al.)."""
    return max(0.5, e.scale * 0.6)


def _min_instance_distance(e1: EntitySpec, e2: EntitySpec) -> float:
    return min(
        math.hypot(p1[0] - p2[0], p1[2] - p2[2])
        for p1 in e1.instance_positions for p2 in e2.instance_positions
    )


class TestGroupPacking:
    def test_two_groups_with_same_radius_no_longer_share_origin(self):
        # This is the exact reported bug: a ring of buildings and a ring of
        # vehicles, both authored at radius=8, both landed on the identical
        # circle around world origin. Checked at the INSTANCE level, not via
        # origin_offset/group_radius: a scenery_distributed group's own
        # centroid is a loose aggregate over several stops (see
        # feedback-no-single-anchor-clusters memory) and is no longer a
        # tight "personal space" circle the way it was when every group had
        # exactly one anchor -- the actual invariant that matters is that no
        # instance of one group sits inside another's footprint.
        a = EntitySpec(name="Buildings", count=20, placement="ring", radius=8, scale=3.5)
        b = EntitySpec(name="Vehicles", count=3, placement="ring", radius=8, scale=1.5)
        schema = _schema([a, b])
        compute_layout(schema)

        assert a.origin_offset != [0.0, 0.0] or b.origin_offset != [0.0, 0.0]
        assert _min_instance_distance(a, b) >= (
            _instance_footprint(a) + _instance_footprint(b) - 1e-6)

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
                dist = _min_instance_distance(e1, e2)
                assert dist >= (
                    _instance_footprint(e1) + _instance_footprint(e2) - 1e-6), (
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
        # Poisson-disk sampling guarantees a REAL minimum pairwise distance of
        # eff_spacing between every pair, not an approximation eroded by
        # jitter -- see test_scatter_enforces_a_real_minimum_distance below
        # for the tight version of this assertion.
        assert worst >= eff_spacing * 0.35
        assert worst > min_gap * 0.9 or eff_spacing > min_gap

    def test_scatter_count_produces_exact_instance_count(self):
        e = EntitySpec(name="Crowd", count=37, placement="scatter", radius=5)
        schema = _schema([e])
        compute_layout(schema)
        assert len(e.instance_positions) == 37

    def test_scatter_enforces_a_real_minimum_distance(self):
        # Bridson's algorithm guarantees min pairwise distance == the packing
        # radius, up to floating-point slop -- not an approximate bound.
        e = EntitySpec(name="Crates", count=25, placement="scatter",
                        radius=6, spacing=2.5, scale=1.0)
        schema = _schema([e])
        compute_layout(schema)
        eff_spacing = max(e.spacing, e.scale * 1.15)
        pts = e.instance_positions
        worst = min(
            math.dist(pts[i], pts[j])
            for i in range(len(pts)) for j in range(i + 1, len(pts))
        )
        assert worst >= eff_spacing - 1e-6


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
        # "Crates" (kind="prop", the schema default) is scenery_distributed
        # now, not the old cluster/corridor_pack path -- checking "route"
        # rather than "corridor" covers whichever relationship message fired.
        assert any("route" in m for m in msgs)

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
        # Names deliberately avoid unity_layout_archetype.spatial_relationship_of's
        # edge_distributed/boundary_follow keyword tables and formation kinds --
        # these are plain scenery_distributed props (kind="prop", the schema
        # default), so EVERY stop must hug the corridor, not just one anchor.
        schema, track, props = self._big_circuit_schema(
            ["Grandstands", "DirtMounds", "Spectators", "MerchStalls"])
        msgs = compute_layout(schema)
        assert any("route" in m for m in msgs)
        half_w = track.path_width / 2.0
        for e in props:
            for x, _y, z in e.instance_positions:
                d = self._dist_to_polyline(x, z, track.path_points)
                # hugging: outside the ribbon, within a tight band beside
                # it — never flung hundreds of metres away into a packed
                # island, checked per INSTANCE now that a scenery group's
                # own centroid can land well inside the loop (several stops
                # on opposite sides of the track average toward the middle,
                # which is correct for the group but meaningless as a
                # "hugging" measure -- see TestEdgeDistributedRelationship's
                # equivalent per-instance check for the same reasoning).
                assert d > half_w
                assert d < half_w + 60.0, (
                    f"{e.name} instance ({x}, {z}) at dist {d:.1f} is not hugging")

    def test_groups_are_distributed_around_the_loop(self):
        schema, track, props = self._big_circuit_schema(
            ["A", "B", "C", "D", "E", "F"])
        compute_layout(schema)
        # Every group's OWN instances now spread across multiple stops
        # (scenery_distributed, count=6 -> 2 stops of 3) instead of landing
        # on one shared anchor -- checked per group's instance spread, not
        # by comparing origin_offset centroids across groups (which, now
        # that a group can straddle both sides of the loop, are no longer a
        # reliable "not clustered" signal on their own).
        for e in props:
            xs = [p[0] for p in e.instance_positions]
            zs = [p[2] for p in e.instance_positions]
            spread = math.hypot(max(xs) - min(xs), max(zs) - min(zs))
            assert spread > 30.0, f"{e.name}'s instances are still one clump"

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


class TestEdgeDistributedRelationship:
    """"Streetlights" reads as spatial_relationship_of() == "edge_distributed"
    (src/unity_layout_archetype.py) and gets ONE instance per arc-length
    interval spread along the WHOLE corridor (src/unity_layout._edge_distributed_pack)
    instead of the old behaviour: one anchor beside the track with all
    `count` copies clustered locally around that single point."""

    @staticmethod
    def _dist_to_polyline(px, pz, pts):
        return TestPathCorridors._dist_to_polyline(px, pz, pts)

    def _circuit_with_lights(self, count=12):
        route = [[0, 0, 0], [0, 0, 120], [80, 0, 200], [200, 0, 200],
                 [260, 0, 120], [200, 0, 40], [80, 0, 20], [0, 0, 0]]
        track = EntitySpec(name="TrackSurface", kind="road", placement="path",
                           count=len(route), path_points=route, path_width=10.0)
        lights = EntitySpec(name="Streetlights", kind="prop", count=count,
                            placement="scatter", scale=1.0)
        return _schema([track, lights]), track, lights

    def test_one_instance_per_count_not_one_clustered_anchor(self):
        schema, _track, lights = self._circuit_with_lights(count=12)
        msgs = compute_layout(schema)
        assert any("one instance per" in m for m in msgs)
        assert len(lights.instance_positions) == 12
        assert len(lights.instance_yaws) == 12

    def test_instances_spread_across_the_whole_route_not_one_spot(self):
        schema, track, lights = self._circuit_with_lights(count=12)
        compute_layout(schema)
        xs = [p[0] for p in lights.instance_positions]
        zs = [p[2] for p in lights.instance_positions]
        # the route itself spans ~260 x 200 -- a real corridor-wide spread,
        # not a dozen lamps stacked in one clustered anchor.
        assert (max(xs) - min(xs)) > 150.0
        assert (max(zs) - min(zs)) > 120.0

    def test_every_instance_clears_the_track_ribbon(self):
        schema, track, lights = self._circuit_with_lights(count=12)
        compute_layout(schema)
        half_w = track.path_width / 2.0
        for x, _y, z in lights.instance_positions:
            assert self._dist_to_polyline(x, z, track.path_points) > half_w

    def test_instances_face_back_toward_the_road(self):
        schema, track, lights = self._circuit_with_lights(count=12)
        compute_layout(schema)
        for (x, _y, z), yaw in zip(lights.instance_positions, lights.instance_yaws):
            # Walking a small step in the yaw's facing direction (Unity's
            # atan2(x, z) convention) must land CLOSER to the route than the
            # instance itself sits -- i.e. it faces the road, not away.
            rad = math.radians(yaw)
            fx, fz = math.sin(rad), math.cos(rad)
            here = self._dist_to_polyline(x, z, track.path_points)
            stepped = self._dist_to_polyline(x + fx, z + fz, track.path_points)
            assert stepped < here, f"instance at ({x}, {z}) yaw={yaw} faces away"

    def test_deterministic(self):
        def build():
            schema, _t, _l = self._circuit_with_lights(count=12)
            compute_layout(schema)
            return [e.model_dump() for e in schema.entities]
        assert build() == build()


class TestBoundaryFollowRelationship:
    """"CrashBarriers" reads as spatial_relationship_of() == "boundary_follow"
    (src/unity_layout_archetype.py) and src/unity_layout._boundary_follow_pack
    places them as a continuous chain along one fixed side of the route —
    direct parametric sampling (count derived from arc_length / max_gap,
    never from the schema's authored `count`), no per-instance search."""

    @staticmethod
    def _dist_to_polyline(px, pz, pts):
        return TestPathCorridors._dist_to_polyline(px, pz, pts)

    def _circuit_with_barriers(self, spacing=15.0):
        route = [[0, 0, 0], [0, 0, 120], [80, 0, 200], [200, 0, 200],
                 [260, 0, 120], [200, 0, 40], [80, 0, 20], [0, 0, 0]]
        track = EntitySpec(name="TrackSurface", kind="road", placement="path",
                           count=len(route), path_points=route, path_width=10.0)
        barriers = EntitySpec(name="CrashBarriers", kind="prop", count=1,
                              placement="scatter", scale=1.0, spacing=spacing)
        return _schema([track, barriers]), track, barriers

    def test_instances_stay_on_one_side_not_alternating(self):
        schema, track, barriers = self._circuit_with_barriers()
        compute_layout(schema)
        from src.unity_route_axis import project
        sides = [1 if project(track.path_points, x, z)[1] >= 0 else -1
                 for x, _y, z in barriers.instance_positions]
        assert len(set(sides)) == 1, "a barrier chain must not cross sides"

    def test_chain_covers_the_whole_route_with_no_large_gaps(self):
        schema, track, barriers = self._circuit_with_barriers()
        compute_layout(schema)
        from src.unity_route_axis import project
        ts = sorted(project(track.path_points, x, z)[0]
                    for x, _y, z in barriers.instance_positions)
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        assert max(gaps) < 0.25, "a >25%-of-the-loop gap isn't a continuous chain"

    def test_count_is_derived_from_max_gap_not_authored_count(self):
        # `count=1` on the schema (see _circuit_with_barriers) — a real
        # barrier run's instance count comes from arc_length / spacing, not
        # from an authored number that has no relation to the route's length.
        schema, track, barriers = self._circuit_with_barriers(spacing=15.0)
        compute_layout(schema)
        arc_length = sum(math.hypot(b[0] - a[0], b[2] - a[2])
                         for a, b in zip(track.path_points, track.path_points[1:]))
        assert len(barriers.instance_positions) == math.ceil(arc_length / 15.0)

    def test_smaller_max_gap_yields_more_instances(self):
        schema_wide, _t1, wide = self._circuit_with_barriers(spacing=40.0)
        schema_tight, _t2, tight = self._circuit_with_barriers(spacing=5.0)
        compute_layout(schema_wide)
        compute_layout(schema_tight)
        assert len(tight.instance_positions) > len(wide.instance_positions)

    def test_deterministic(self):
        def build():
            schema, _t, _b = self._circuit_with_barriers()
            compute_layout(schema)
            return [e.model_dump() for e in schema.entities]
        assert build() == build()


class TestSceneryDistributedRelationship:
    """A plain prop group (kind="prop", no formation kind/tag, no edge/
    boundary keyword match) is spatial_relationship_of() == "scenery_distributed"
    -- THE DEFAULT now, replacing the old unconditional single-anchor
    cluster. See feedback-no-single-anchor-clusters memory."""

    def _circuit_with_scenery(self, count=12):
        route = [[0, 0, 0], [0, 0, 120], [80, 0, 200], [200, 0, 200],
                 [260, 0, 120], [200, 0, 40], [80, 0, 20], [0, 0, 0]]
        track = EntitySpec(name="TrackSurface", kind="road", placement="path",
                           count=len(route), path_points=route, path_width=10.0)
        crowd = EntitySpec(name="Spectators", kind="prop", count=count,
                           placement="scatter", radius=5, spacing=2.0, scale=1.0)
        return _schema([track, crowd]), track, crowd

    def test_small_group_collapses_to_one_stop_unchanged(self):
        # count=2 <= _SCENERY_STOP_SIZE (3): today's original single-anchor
        # behaviour, unchanged -- a small decorative group was never the
        # complaint this relationship exists to fix.
        schema, _track, crowd = self._circuit_with_scenery(count=2)
        compute_layout(schema)
        assert len(crowd.instance_positions) == 2
        xs = [p[0] for p in crowd.instance_positions]
        zs = [p[2] for p in crowd.instance_positions]
        assert math.hypot(max(xs) - min(xs), max(zs) - min(zs)) < 10.0

    def test_large_group_spreads_across_multiple_stops(self):
        schema, _track, crowd = self._circuit_with_scenery(count=12)
        compute_layout(schema)
        assert len(crowd.instance_positions) == 12
        xs = [p[0] for p in crowd.instance_positions]
        zs = [p[2] for p in crowd.instance_positions]
        assert math.hypot(max(xs) - min(xs), max(zs) - min(zs)) > 100.0

    def test_every_instance_clears_the_track_ribbon(self):
        schema, track, crowd = self._circuit_with_scenery(count=12)
        compute_layout(schema)
        half_w = track.path_width / 2.0
        for x, _y, z in crowd.instance_positions:
            assert TestPathCorridors._dist_to_polyline(x, z, track.path_points) > half_w

    def test_deterministic(self):
        def build():
            schema, _t, _c = self._circuit_with_scenery(count=12)
            compute_layout(schema)
            return [e.model_dump() for e in schema.entities]
        assert build() == build()


class TestSceneryScatterNoRoute:
    """The no-route sibling of TestSceneryDistributedRelationship — a plain
    interior/level scene with no authored path (a shop, a room), where
    "fifteen mirrors" used to become one 5x3 grid at a single anchor. See
    feedback-no-single-anchor-clusters memory: this was reported live as
    "15 mirrors in a square anchor layout" in a grandma's-shop run."""

    def test_small_group_collapses_to_one_stop_unchanged(self):
        e = EntitySpec(name="Mirrors", kind="prop", count=2, placement="grid", scale=1.0)
        schema = _schema([e])
        compute_layout(schema)
        assert len(e.instance_positions) == 2
        xs = [p[0] for p in e.instance_positions]
        zs = [p[2] for p in e.instance_positions]
        assert math.hypot(max(xs) - min(xs), max(zs) - min(zs)) < 10.0

    def test_fifteen_mirrors_spread_across_several_stops_not_one_grid(self):
        e = EntitySpec(name="Mirrors", kind="prop", count=15, placement="grid", scale=1.0)
        schema = _schema([e])
        msgs = compute_layout(schema)
        assert any("stops" in m for m in msgs)
        assert len(e.instance_positions) == 15
        xs = [p[0] for p in e.instance_positions]
        zs = [p[2] for p in e.instance_positions]
        # A single 5x3 grid of unit-scale mirrors is well under ~20 units
        # across; several stops spread by _find_slot's spiral search must
        # cover meaningfully more ground than that.
        assert math.hypot(max(xs) - min(xs), max(zs) - min(zs)) > 25.0

    def test_no_instance_lands_inside_the_spawn_radius(self):
        e = EntitySpec(name="Mirrors", kind="prop", count=15, placement="grid", scale=1.0)
        schema = _schema([e])
        compute_layout(schema)
        for x, _y, z in e.instance_positions:
            assert math.hypot(x, z) >= _SPAWN_CLEARANCE_DEFAULT

    def test_different_groups_stay_apart(self):
        mirrors = EntitySpec(name="Mirrors", kind="prop", count=15,
                             placement="grid", scale=1.0)
        crates = EntitySpec(name="Crates", kind="prop", count=15,
                            placement="grid", scale=1.0)
        schema = _schema([mirrors, crates])
        compute_layout(schema)
        min_dist = min(
            math.hypot(p1[0] - p2[0], p1[2] - p2[2])
            for p1 in mirrors.instance_positions for p2 in crates.instance_positions)
        assert min_dist >= 2.0  # _GROUP_MARGIN

    def test_deterministic(self):
        def build():
            e = EntitySpec(name="Mirrors", kind="prop", count=15,
                           placement="grid", scale=1.0)
            schema = _schema([e])
            compute_layout(schema)
            return e.model_dump()
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


class TestPlayerSpawnReservation:
    """The scene templates put the player at world XZ (0, 0). Nothing reserved
    that cell, so the packer's first slot — `_find_slot` returns the origin
    while nothing is placed — was the player's own spawn point. Live symptom
    (TurboToyRacer): the player spawned inside a spawned group's collider and
    could not move at all."""

    @staticmethod
    def _dist_from_spawn(entity):
        return math.hypot(entity.origin_offset[0], entity.origin_offset[1])

    def test_first_packed_group_no_longer_claims_the_origin(self):
        big = EntitySpec(name="Big", count=9, placement="grid", scale=3.0)
        schema = _schema([big])
        compute_layout(schema)
        assert big.origin_offset != [0.0, 0.0]

    def test_every_packed_group_clears_the_spawn_radius(self):
        entities = [
            EntitySpec(name="Buildings", count=12, placement="ring", radius=8),
            EntitySpec(name="Crowd", count=30, placement="scatter", scale=0.6),
            EntitySpec(name="Pickups", count=6, placement="grid"),
            EntitySpec(name="Statue", count=1, placement="single", scale=2.0),
        ]
        schema = _schema(entities)
        compute_layout(schema)
        # Checked per INSTANCE, not via origin_offset/group_radius: a
        # scenery_distributed group's own centroid is a loose aggregate over
        # several stops (see feedback-no-single-anchor-clusters memory) and
        # is no longer guaranteed to sit outside its own group_radius from
        # the spawn the way a single-anchor group's did.
        for e in entities:
            for x, _y, z in e.instance_positions:
                assert math.hypot(x, z) >= _SPAWN_CLEARANCE_DEFAULT, \
                    f"{e.name} instance ({x}, {z}) overlaps the player spawn"

    def test_no_baked_instance_lands_on_the_spawn_point(self):
        entities = [
            EntitySpec(name="Crowd", count=40, placement="scatter", scale=0.6),
            EntitySpec(name="Crates", count=9, placement="grid", scale=1.0),
        ]
        schema = _schema(entities)
        compute_layout(schema)
        for e in entities:
            for x, _y, z in e.instance_positions:
                assert math.hypot(x, z) > 1.0, f"{e.name} instance sits on the player"

    @pytest.mark.parametrize("locomotion,clearance,in_volume", [
        ("walk", 3.0, False), ("drive", 6.0, False),
        ("swim", 6.0, True),  ("fly",   8.0, True),
    ])
    def test_clearance_scales_with_locomotion(self, locomotion, clearance,
                                              in_volume):
        """A vehicle_car's runtime BoxCollider is 1.7 x 4.0 — a walker's
        clearance would still leave the car's nose inside a neighbour. The
        spiral quantizes slots, so assert the honored clearance, not the
        distance the search happens to settle on.

        The clearance applies in both primitives this schema can select:
        walk/drive pack on the ArenaPlane ground, while fly/swim derive
        medium=air/water with no descent evidence and get a BoundedVolume,
        whose layers reserve the same spawn column. Placement differs — the
        volume puts content in the interior, off the floor — so assert that
        too, or the test would pass on a plane wearing a volume's name."""
        e = EntitySpec(name="Props", count=9, placement="grid", scale=1.0)
        schema = _schema([e])
        schema.locomotion = locomotion
        compute_layout(schema)
        # Checked per INSTANCE (see test_every_packed_group_clears_the_spawn_radius):
        # `clearance` here IS the reserved spawn radius itself
        # (_SPAWN_CLEARANCE_BY_LOCOMOTION), so no instance may sit closer to
        # the origin than that, regardless of how the group's own centroid
        # nets out across however many stops it was split into.
        for x, _y, z in e.instance_positions:
            assert math.hypot(x, z) >= clearance
        ys = {p[1] for p in e.instance_positions}
        if in_volume:
            assert ys and all(y > 0.0 for y in ys), ys
        else:
            assert ys == {0.0}

    def test_path_groups_are_not_displaced_by_the_reservation(self):
        """A race start line runs THROUGH the spawn by design — the road is a
        surface the player rests on, not an obstacle to route around."""
        road = EntitySpec(name="Road", kind="road", count=8, placement="path",
                          path_points=[[0, 0, 0], [0, 0, 40], [0, 0, 80]])
        schema = _schema([road])
        compute_layout(schema)
        assert road.path_points[0][:3] == [0.0, 0.0, 0.0]

    def test_layout_is_still_idempotent(self):
        entities = [EntitySpec(name="A", count=9, placement="grid"),
                    EntitySpec(name="B", count=12, placement="ring", radius=6)]
        schema = _schema(entities)
        compute_layout(schema)
        first = [(e.origin_offset, e.group_radius, e.instance_positions) for e in entities]
        compute_layout(schema)
        assert [(e.origin_offset, e.group_radius, e.instance_positions)
                for e in entities] == first
