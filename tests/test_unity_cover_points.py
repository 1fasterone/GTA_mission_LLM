"""Cover-point sidecar (src/unity_cover_points.py) — Step 5d.

The `cover_shooter` AI variant shipped with `// Naive: pop out at attack
range, retreat at half-distance` — zero obstacle/LOS query anywhere. This
module is what finally gives it real geometry: candidate cover points baked
from BLOCKING_KINDS obstacle footprints already placed by compute_layout(),
the same non-fabrication discipline as unity_spatial_facts.py (always writes
the key, even {}, so nothing can be mistaken for "not computed yet").
"""
from __future__ import annotations

from src.unity_cover_points import compute_cover_points
from src.unity_schema import EntitySpec, UnitySchema
from src.unity_spatial import GROUND_PLANE, ROUTE_CORRIDOR


def _schema(entities: list[EntitySpec], primitive: str = GROUND_PLANE) -> UnitySchema:
    s = UnitySchema(project_name="CoverTest", game_type="3D",
                    platform="PC", render_pipeline="builtin")
    s.meta["spatial_primitive"] = primitive
    s.entities = entities
    return s


def _player(pos=(0.0, 0.0, 0.0)) -> EntitySpec:
    return EntitySpec(name="Player", kind="player", tag="Player",
                      instance_positions=[list(pos)])


class TestComputeCoverPoints:
    def test_obstacle_near_enemy_produces_cover_point(self):
        schema = _schema([
            _player(),
            EntitySpec(name="Wall1", kind="wall", scale=1.0, instance_positions=[[5.0, 0.0, 5.0]]),
            EntitySpec(name="Guard", kind="enemy", instance_positions=[[6.0, 0.0, 6.0]]),
        ])
        compute_cover_points(schema)
        points = schema.meta["cover_points"]
        assert "Guard" in points
        assert len(points["Guard"]) == 1

    def test_no_obstacles_bakes_empty_but_present(self):
        """Non-fabrication discipline: the key exists even when there is
        nothing to bake — never let a consumer mistake "not computed" for
        "no cover exists"."""
        schema = _schema([_player(), EntitySpec(name="Guard", kind="enemy",
                                                instance_positions=[[6.0, 0.0, 6.0]])])
        messages = compute_cover_points(schema)
        assert schema.meta["cover_points"] == {}
        assert messages == []

    def test_no_enemies_bakes_empty(self):
        schema = _schema([
            _player(),
            EntitySpec(name="Wall1", kind="wall", instance_positions=[[5.0, 0.0, 5.0]]),
        ])
        compute_cover_points(schema)
        assert schema.meta["cover_points"] == {}

    def test_obstacle_far_from_enemy_is_not_used(self):
        schema = _schema([
            _player(),
            EntitySpec(name="FarWall", kind="wall", instance_positions=[[500.0, 0.0, 500.0]]),
            EntitySpec(name="Guard", kind="enemy", instance_positions=[[6.0, 0.0, 6.0]]),
        ])
        compute_cover_points(schema)
        assert schema.meta["cover_points"] == {}

    def test_non_blocking_kind_is_not_used_as_cover(self):
        """A `prop`/`tree`/etc is not in BLOCKING_KINDS — same allowlist
        unity_reachability.py uses, so decorative scenery never reads as
        cover geometry."""
        schema = _schema([
            _player(),
            EntitySpec(name="Bush", kind="prop", instance_positions=[[5.0, 0.0, 5.0]]),
            EntitySpec(name="Guard", kind="enemy", instance_positions=[[6.0, 0.0, 6.0]]),
        ])
        compute_cover_points(schema)
        assert schema.meta["cover_points"] == {}

    def test_cover_direction_points_back_toward_spawn(self):
        """The enemy stands on the far side of the obstacle from spawn, and
        faces BACK toward spawn to peek and shoot — see AI.cs.j2's
        transform.forward assignment in the cover_shooter branch."""
        schema = _schema([
            _player(pos=(0.0, 0.0, 0.0)),
            EntitySpec(name="Wall1", kind="wall", instance_positions=[[10.0, 0.0, 0.0]]),
            EntitySpec(name="Guard", kind="enemy", instance_positions=[[11.0, 0.0, 0.0]]),
        ])
        compute_cover_points(schema)
        point = schema.meta["cover_points"]["Guard"][0]
        # Obstacle is due +X of spawn, so the cover position sits further +X
        # (away from spawn) and faces back -X (toward spawn).
        assert point["position"][0] > 10.0
        assert point["cover_direction"][0] < 0

    def test_route_corridor_primitive_also_computes_cover(self):
        schema = _schema([
            _player(),
            EntitySpec(name="Wall1", kind="wall", instance_positions=[[5.0, 0.0, 5.0]]),
            EntitySpec(name="Guard", kind="enemy", instance_positions=[[6.0, 0.0, 6.0]]),
        ], primitive=ROUTE_CORRIDOR)
        compute_cover_points(schema)
        assert "Guard" in schema.meta["cover_points"]

    def test_non_ground_primitive_abstains(self):
        """A flying/swimming/climbing game's enemies have no ground-plane
        geometry to hide behind — abstain rather than fabricate cover in a
        volume, even with real obstacle+enemy entities present."""
        schema = _schema([
            _player(),
            EntitySpec(name="Wall1", kind="wall", instance_positions=[[5.0, 0.0, 5.0]]),
            EntitySpec(name="Guard", kind="enemy", instance_positions=[[6.0, 0.0, 6.0]]),
        ], primitive="depth_column")
        compute_cover_points(schema)
        assert schema.meta["cover_points"] == {}

    def test_unbaked_entities_produce_no_points(self):
        """An enemy/obstacle with no instance_positions (layout hasn't run,
        or nothing landed there) has no position to derive a point from."""
        schema = _schema([
            _player(),
            EntitySpec(name="Wall1", kind="wall"),
            EntitySpec(name="Guard", kind="enemy"),
        ])
        compute_cover_points(schema)
        assert schema.meta["cover_points"] == {}
