"""Spatial primitives — one per game shape (src/unity_spatial.py) and their
effect on the layout pass.

Regression cover for the reported failure: the compiler had a single spatial
primitive, a ground plane, so a game played in a VOLUME (a skydive) compiled
to its aircraft, its divers, its altitude gates and its landing pad all packed
side by side on the floor at y=0.
"""
from __future__ import annotations

from src.unity_layout import compute_layout
from src.unity_schema import EntitySpec, UnitySchema
from src.unity_spatial import (AIRSPACE_COLUMN, DEPTH_COLUMN, GROUND_PLANE,
                               ROUTE_CORRIDOR, assign_elevations, band_key,
                               is_grounded, select_primitive)


def _schema(entities, *, game_type="3D", locomotion="walk"):
    return UnitySchema(
        project_name="SpatialTest", company_name="C", description="",
        game_type=game_type, platform="pc", render_pipeline="URP",
        unity_version="6000.0", color_space="Linear",
        features=[], scenes=["Main"], packages=[],
        locomotion=locomotion, entities=entities,
    )


def _skydive_entities():
    return [
        EntitySpec(name="Cessna", kind="npc", count=1, placement="single"),
        EntitySpec(name="Diver", kind="player", count=1, placement="single",
                   tag="Player"),
        EntitySpec(name="Formation", kind="enemy", count=3, placement="scatter",
                   tag="Enemy"),
        EntitySpec(name="Gates", kind="collectible", count=8, placement="scatter"),
        EntitySpec(name="LandingPad", kind="terrain", count=1, placement="single"),
    ]


# ── selection ─────────────────────────────────────────────────────────────────

class TestSelection:
    def test_walk_is_a_surface_game(self):
        assert select_primitive("3D", "walk") == GROUND_PLANE

    def test_drive_is_a_surface_game(self):
        assert select_primitive("3D", "drive") == GROUND_PLANE

    def test_fly_is_a_volume(self):
        assert select_primitive("3D", "fly") == AIRSPACE_COLUMN

    def test_swim_is_a_volume(self):
        assert select_primitive("3D", "swim") == DEPTH_COLUMN

    def test_an_authored_route_outranks_locomotion(self):
        """A path is an explicit statement about where the play space goes;
        locomotion only says how the player moves along it."""
        assert select_primitive("3D", "fly", has_route=True) == ROUTE_CORRIDOR
        assert select_primitive("3D", "walk", has_route=True) == ROUTE_CORRIDOR

    def test_unknown_locomotion_falls_back_to_the_ground_plane(self):
        assert select_primitive("3D", "") == GROUND_PLANE
        assert select_primitive("3D", "teleport") == GROUND_PLANE


# ── elevation assignment ──────────────────────────────────────────────────────

class TestElevations:
    def test_surface_primitives_derive_nothing(self):
        """An empty map is what keeps every existing project byte-identical:
        no entity in _skydive_entities() authors an elevation."""
        assert assign_elevations(_skydive_entities(), GROUND_PLANE) == {}
        assert assign_elevations(_skydive_entities(), ROUTE_CORRIDOR) == {}

    def test_floor_kinds_stay_on_the_floor(self):
        ys = assign_elevations(_skydive_entities(), AIRSPACE_COLUMN)
        assert ys["LandingPad"] == 0.0

    def test_airborne_groups_are_spread_across_altitudes(self):
        ys = assign_elevations(_skydive_entities(), AIRSPACE_COLUMN)
        airborne = {k: v for k, v in ys.items() if k != "LandingPad"}
        assert all(v > 0 for v in airborne.values())
        assert len(set(airborne.values())) > 1, "everything landed in one band"

    def test_the_carrier_sits_above_the_player_above_the_pickups(self):
        """The ordering that makes a descent read as a descent."""
        ys = assign_elevations(_skydive_entities(), AIRSPACE_COLUMN)
        assert ys["Cessna"] > ys["Diver"] > ys["Gates"] > ys["LandingPad"]

    def test_authored_elevation_always_wins(self):
        ents = [EntitySpec(name="Balloon", kind="prop", elevation=77.5)]
        assert assign_elevations(ents, AIRSPACE_COLUMN)["Balloon"] == 77.5

    def test_authored_elevation_survives_a_surface_game(self):
        """A deliberate floating platform in a walking game must not be
        flattened back onto the floor — the surface primitive derives nothing
        but still honours what the schema stated."""
        ents = [EntitySpec(name="Balloon", kind="prop", elevation=77.5)]
        assert assign_elevations(ents, GROUND_PLANE) == {"Balloon": 77.5}

    def test_a_surface_game_derives_no_elevation_of_its_own(self):
        ents = [EntitySpec(name="Crate", kind="prop")]
        assert assign_elevations(ents, GROUND_PLANE) == {}

    def test_depth_column_descends_below_the_surface(self):
        ys = assign_elevations(_skydive_entities(), DEPTH_COLUMN)
        assert ys["LandingPad"] == 0.0
        assert all(v < 0 for k, v in ys.items() if k != "LandingPad")

    def test_grounded_kinds_are_recognized(self):
        assert is_grounded(EntitySpec(name="T", kind="terrain"))
        assert is_grounded(EntitySpec(name="B", kind="building"))
        assert not is_grounded(EntitySpec(name="E", kind="enemy"))
        assert not is_grounded(EntitySpec(name="X", kind="something_new"))

    def test_band_key_is_order_independent(self):
        a = assign_elevations(_skydive_entities(), AIRSPACE_COLUMN)
        b = assign_elevations(list(reversed(_skydive_entities())), AIRSPACE_COLUMN)
        assert a == b
        assert band_key(a["Cessna"]) != band_key(a["Gates"])


# ── layout integration ────────────────────────────────────────────────────────

class TestVolumeLayout:
    def test_a_skydive_is_not_packed_onto_the_floor(self):
        """The reported bug, end to end."""
        schema = _schema(_skydive_entities(), locomotion="fly")
        compute_layout(schema)
        ys = {e.name: e.instance_positions[0][1] for e in schema.entities}
        assert ys["LandingPad"] == 0.0
        assert ys["Cessna"] > 100.0
        assert len({round(v) for v in ys.values()}) >= 4

    def test_every_instance_of_a_group_shares_its_altitude(self):
        schema = _schema(_skydive_entities(), locomotion="fly")
        compute_layout(schema)
        gates = next(e for e in schema.entities if e.name == "Gates")
        assert len(gates.instance_positions) == 8
        assert len({p[1] for p in gates.instance_positions}) == 1

    def test_the_same_scene_on_foot_still_lies_flat(self):
        schema = _schema(_skydive_entities(), locomotion="walk")
        compute_layout(schema)
        for e in schema.entities:
            assert all(p[1] == 0.0 for p in e.instance_positions)

    def test_groups_sharing_a_band_are_still_packed_apart(self):
        ents = [
            EntitySpec(name="GateA", kind="collectible", count=6, placement="scatter"),
            EntitySpec(name="GateB", kind="collectible", count=6, placement="scatter"),
        ]
        schema = _schema(ents, locomotion="fly")
        compute_layout(schema)
        a, b = schema.entities
        assert band_key(a.elevation) == band_key(b.elevation)
        assert a.origin_offset != b.origin_offset

    def test_different_bands_may_reuse_plan_view_space(self):
        """The point of the primitive: vertical separation is separation, so a
        volume game is not spread back out across a floor.

        Measured on the packed footprints themselves, not terrain_bounds —
        that adds a margin keyed to locomotion (100 m for a flyer vs 8 m for a
        walker), which would swamp the comparison being made here."""
        flat = _schema(_skydive_entities(), locomotion="walk")
        compute_layout(flat)
        volume = _schema(_skydive_entities(), locomotion="fly")
        compute_layout(volume)

        def _plan_extent(s):
            xs, zs = [], []
            for e in s.entities:
                (cx, cz), r = e.origin_offset, e.group_radius
                xs += [cx - r, cx + r]
                zs += [cz - r, cz + r]
            return max(max(xs) - min(xs), max(zs) - min(zs))

        assert _plan_extent(volume) < _plan_extent(flat)

    def test_layout_is_idempotent_in_a_volume(self):
        schema = _schema(_skydive_entities(), locomotion="fly")
        compute_layout(schema)
        first = [list(e.instance_positions) for e in schema.entities]
        compute_layout(schema)
        assert [list(e.instance_positions) for e in schema.entities] == first

    def test_elevation_is_recorded_on_the_spec(self):
        schema = _schema(_skydive_entities(), locomotion="fly")
        compute_layout(schema)
        cessna = next(e for e in schema.entities if e.name == "Cessna")
        assert cessna.elevation == cessna.instance_positions[0][1] > 0
