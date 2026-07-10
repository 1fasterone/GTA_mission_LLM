"""Tests for the deterministic race-track scaffold (src/unity_racetrack.py)."""

import pytest

from src.unity_racetrack import (compose_racetrack, is_racing_degenerate,
                                 _rounded_rect_loop, _every_nth)
from src.unity_schema import EntitySpec, UnitySchema


def _schema(entities=None, **overrides):
    defaults = dict(
        project_name="DriftArcade", company_name="C",
        description="a drift racing arcade game",
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.2", color_space="Linear", scenes=["Main"],
        entities=entities or [],
    )
    defaults.update(overrides)
    return UnitySchema(**defaults)


# ── geometry helpers ─────────────────────────────────────────────────────────

class TestRoundedRectLoop:
    def test_loop_is_closed(self):
        pts = _rounded_rect_loop(30.0, 50.0, 15.0)
        assert pts[0] == pts[-1]

    def test_loop_stays_under_path_points_cap(self):
        pts = _rounded_rect_loop(30.0, 50.0, 15.0)
        assert len(pts) <= 64

    def test_radius_clamped_to_half_extents(self):
        # A radius larger than either half-extent must not invert the shape.
        pts = _rounded_rect_loop(5.0, 5.0, 500.0)
        xs = [p[0] for p in pts]
        zs = [p[2] for p in pts]
        assert max(xs) <= 5.0 + 1e-6 and min(xs) >= -5.0 - 1e-6
        assert max(zs) <= 5.0 + 1e-6 and min(zs) >= -5.0 - 1e-6

    def test_loop_is_deterministic(self):
        assert _rounded_rect_loop(30.0, 50.0, 15.0) == _rounded_rect_loop(30.0, 50.0, 15.0)

    def test_all_points_are_xz_plane(self):
        pts = _rounded_rect_loop(30.0, 50.0, 15.0)
        assert all(p[1] == 0.0 for p in pts)


class TestEveryNth:
    def test_subsamples_and_keeps_closing_point(self):
        loop = _rounded_rect_loop(30.0, 50.0, 15.0)
        sub = _every_nth(loop, 3)
        assert sub[-1] == loop[0]
        assert len(sub) < len(loop)

    def test_n_le_1_returns_full_copy(self):
        loop = _rounded_rect_loop(30.0, 50.0, 15.0)
        assert _every_nth(loop, 1) == loop
        assert _every_nth(loop, 1) is not loop


# ── degeneracy check ─────────────────────────────────────────────────────────

class TestIsRacingDegenerate:
    def test_empty_entities_is_degenerate(self):
        assert is_racing_degenerate(_schema())

    def test_only_scenery_entities_is_degenerate(self):
        schema = _schema([EntitySpec(name="Trees", kind="prop", count=5)])
        assert is_racing_degenerate(schema)

    def test_vehicle_entity_is_not_degenerate(self):
        schema = _schema([EntitySpec(name="RaceCars", kind="vehicle", count=6)])
        assert not is_racing_degenerate(schema)

    def test_road_entity_is_not_degenerate(self):
        schema = _schema([EntitySpec(name="Track", kind="road", count=1)])
        assert not is_racing_degenerate(schema)


# ── composer ─────────────────────────────────────────────────────────────────

class TestComposeRacetrack:
    def test_injects_vehicle_and_track_entities(self):
        schema = _schema()
        compose_racetrack(schema)
        kinds = {e.kind for e in schema.entities}
        assert "vehicle" in kinds
        assert "road" in kinds

    def test_appends_never_removes_existing_entities(self):
        existing = EntitySpec(name="Grandstands", kind="building", count=4)
        schema = _schema([existing])
        compose_racetrack(schema)
        names = [e.name for e in schema.entities]
        assert "Grandstands" in names
        assert names.index("Grandstands") == 0   # existing entity untouched at front

    def test_all_path_entities_stay_within_64_points(self):
        schema = _schema()
        compose_racetrack(schema)
        for e in schema.entities:
            if e.placement == "path":
                assert len(e.path_points) <= 64

    def test_path_entity_count_matches_point_count(self):
        # EntitySpawner spawns `count` instances along `path_points` — a
        # mismatch would either skip real track geometry or waste instances.
        schema = _schema()
        compose_racetrack(schema)
        for e in schema.entities:
            if e.placement == "path":
                assert e.count == len(e.path_points)

    def test_player_kart_is_single_and_tagged(self):
        schema = _schema()
        compose_racetrack(schema)
        kart = next(e for e in schema.entities if e.name == "PlayerKart")
        assert kart.placement == "single"
        assert kart.count == 1
        assert kart.tag == "Player"

    def test_every_new_entity_carries_a_model_hint(self):
        # No entity should fall through to the generic prop fallback for lack
        # of a hint — the whole point is genre-correct, on-theme content.
        schema = _schema()
        compose_racetrack(schema)
        for e in schema.entities:
            assert e.model_hint.strip(), f"{e.name} has no model_hint"

    def test_world_region_created_when_absent(self):
        schema = _schema()
        assert schema.world_spec is None
        compose_racetrack(schema)
        assert schema.world_spec is not None
        assert schema.world_spec.regions[0].name == "Racetrack"
        assert len(schema.world_spec.regions[0].bounds) == 4

    def test_world_region_appended_when_already_present(self):
        from src.unity_schema import WorldRegion, WorldSpec
        schema = _schema(world_spec=WorldSpec(
            regions=[WorldRegion(name="Existing", bounds=[0, 0, 1, 1])]))
        compose_racetrack(schema)
        names = [r.name for r in schema.world_spec.regions]
        assert names == ["Existing", "Racetrack"]

    def test_returns_a_fix_message(self):
        schema = _schema()
        msgs = compose_racetrack(schema)
        assert msgs and "racing" in msgs[0].lower()

    def test_deterministic_across_calls(self):
        s1, s2 = _schema(), _schema()
        compose_racetrack(s1)
        compose_racetrack(s2)
        assert [e.model_dump() for e in s1.entities] == [e.model_dump() for e in s2.entities]
