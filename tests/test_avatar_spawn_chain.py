"""
The chain that puts the player in their own game, end to end.

Four separate stages have to agree before an avatar exists where the design put
it, and a live DropZone run (tests/fixtures/dropzone_arcade_llm.schema.json)
broke three of them at once while every automated check passed:

  1. `UnitySchema.enforce_role_graph` — the schema must END UP with exactly one
     singular Player-tagged entity. `EntitySpec._reserve_player_tag` strips the
     tag from never-player kinds, and it is the only code that removes it, so a
     fill that tags a `kind="prop"` avatar left the build with no tagged avatar
     at all (R1b) — and a `count=6` avatar spawns six of them (R1a).
  2. `unity_pipeline._check_consistency` — objective content must carry a tag,
     because EntitySpawner only attaches a collider to a tagged group.
  3. `unity_layout.compute_layout` — the archetype chosen on the first pass must
     survive the generator's model-aware second pass, which would otherwise
     re-derive one from the altitudes the first pass baked.
  4. `unity_generator` — the layout's spawn must reach the scene even when the
     player-merge cannot fire.

Each test below fails on exactly one of those stages.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.unity_eval import replay
from src.unity_layout import _ARCH_DECISION_KEY, compute_layout
from src.unity_layout_archetype import pick_player_entity
from src.unity_pipeline import _check_consistency
from src.unity_playability_artifact import (_player_gameobjects,
                                            _primary_scene,
                                            _transform_position)
from src.unity_schema import EntitySpec, UnitySchema, UnityScript

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _schema(entities, *, scripts=None, locomotion="walk", **kw):
    return UnitySchema(
        project_name="ChainTest", company_name="C", description="",
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.0", color_space="Linear",
        features=[], scenes=["Main"], packages=[], locomotion=locomotion,
        entities=entities,
        scripts=scripts or [UnityScript(name="PlayerController", type="player",
                                        description="d")],
        meta={}, **kw)


# ── 1. the schema ends up with one singular avatar ────────────────────────────

class TestRoleGraphRestoresTheAvatar:
    def test_a_demoted_player_tag_is_restored(self):
        """The fill tagged its avatar Player on a kind='prop' group;
        _reserve_player_tag demoted it and nothing put it back."""
        s = _schema([EntitySpec(name="Player", kind="prop", tag="Player",
                                count=1)])
        avatar = next(e for e in s.entities if e.name == "Player")
        assert avatar.tag == "Player", "R1b should have restored the tag"
        assert avatar.kind == "character", (
            "a never-player kind would be demoted again on the next validation")
        assert any("R1b" in r for r in s.meta["role_lint"]["repairs"])

    def test_the_restored_avatar_is_singular(self):
        s = _schema([EntitySpec(name="Player", kind="prop", tag="Player",
                                count=6)])
        assert next(e for e in s.entities if e.name == "Player").count == 1

    def test_an_already_tagged_avatar_is_left_alone(self):
        s = _schema([EntitySpec(name="Hero", kind="character", tag="Player"),
                     EntitySpec(name="Player", kind="prop", count=3)])
        assert [e.name for e in s.entities if e.tag == "Player"] == ["Hero"]
        assert next(e for e in s.entities if e.name == "Player").kind == "prop"

    def test_a_plural_tagged_avatar_is_clamped(self):
        """count>1 on a Player-tagged group spawns N tagged objects AND fails
        the generator's merge gate, which needs count==1."""
        s = _schema([EntitySpec(name="Hero", kind="character", tag="Player",
                                count=6)])
        hero = next(e for e in s.entities if e.name == "Hero")
        assert hero.count == 1
        assert any("R1a" in r for r in s.meta["role_lint"]["repairs"])

    def test_player_prefixed_tag_alias_is_canonicalized(self):
        s = _schema([EntitySpec(name="ForegroundAircraft", kind="vehicle",
                                tag="PlayerCraft")])
        avatar = next(e for e in s.entities if e.name == "ForegroundAircraft")
        assert avatar.tag == "Player"
        assert any("R1b" in r for r in s.meta["role_lint"]["repairs"])

    def test_player_character_name_restores_demoted_avatar(self):
        s = _schema([EntitySpec(name="PlayerCharacter", kind="pickup",
                                tag="Untagged", count=1)])
        avatar = next(e for e in s.entities if e.name == "PlayerCharacter")
        assert avatar.tag == "Player"
        assert avatar.kind == "character"

    def test_nothing_is_invented_when_no_entity_reads_as_the_player(self):
        s = _schema([EntitySpec(name="Trees", kind="prop", count=9)])
        assert not [e for e in s.entities if e.tag == "Player"]

    def test_a_scenery_group_named_playerstatues_is_not_promoted(self):
        s = _schema([EntitySpec(name="PlayerStatues", kind="prop", count=4)])
        assert not [e for e in s.entities if e.tag == "Player"]


# ── 2. objective content carries a load-bearing tag ───────────────────────────

class TestObjectiveTagsAreAssigned:
    @pytest.mark.parametrize("kind,tag", [
        ("collectible", "Collectible"),
        ("pickup", "Collectible"),
        ("enemy", "Enemy"),
        ("goal", "Interactable"),
        ("hazard", "Obstacle"),
    ])
    def test_an_untagged_objective_kind_gets_its_tag(self, kind, tag):
        s = _schema([EntitySpec(name="Hero", kind="character", tag="Player"),
                     EntitySpec(name="Thing", kind=kind, tag="Untagged")])
        s, fixes = _check_consistency(s)
        assert next(e for e in s.entities if e.name == "Thing").tag == tag
        assert any("Thing" in f for f in fixes), "the repair must be logged"

    def test_scenery_is_left_untagged(self):
        """A prop with a collider tag is a different bug."""
        s = _schema([EntitySpec(name="Hero", kind="character", tag="Player"),
                     EntitySpec(name="Trees", kind="prop", tag="Untagged")])
        s, _ = _check_consistency(s)
        assert next(e for e in s.entities if e.name == "Trees").tag == "Untagged"

    def test_an_authored_tag_is_never_overwritten(self):
        s = _schema([EntitySpec(name="Hero", kind="character", tag="Player"),
                     EntitySpec(name="Gems", kind="collectible", tag="Gem")])
        s, _ = _check_consistency(s)
        assert next(e for e in s.entities if e.name == "Gems").tag == "Gem"


# ── 3. the archetype survives the second layout pass ──────────────────────────

class TestArchetypeIsDecidedOnce:
    def _swimmer(self):
        s = _schema(
            [EntitySpec(name="Diver", kind="character", tag="Player"),
             EntitySpec(name="ReefFish", kind="enemy", count=12,
                        placement="scatter"),
             EntitySpec(name="Coral", kind="prop", count=7,
                        placement="scatter")],
            locomotion="swim")
        s.description = ("A calm underwater photography game. The player swims "
                         "freely around a tropical lagoon. There is no descent "
                         "objective.")
        return s

    def test_the_first_pass_records_its_whole_decision(self):
        s = self._swimmer()
        compute_layout(s)
        decision = s.meta[_ARCH_DECISION_KEY]
        assert decision["name"] == s.meta["layout_archetype"]
        assert decision["medium"] and decision["motion_axis"]

    def test_a_second_pass_replays_it_instead_of_re_deriving(self):
        """The generator re-runs compute_layout once models are resolved. Its
        second derivation reads the altitudes the FIRST pass baked, which
        re-labelled a swim BoundedVolume as a descent VerticalCorridor and
        re-packed a reef survey as a fall column."""
        s = self._swimmer()
        compute_layout(s)
        first = s.meta["layout_archetype"]
        for e in s.entities:
            if e.placement == "scatter":
                e.instance_positions = []
        compute_layout(s)
        assert s.meta["layout_archetype"] == first

    def test_a_malformed_stamp_falls_back_to_a_fresh_derivation(self):
        s = self._swimmer()
        s.meta[_ARCH_DECISION_KEY] = "not-a-dict"
        compute_layout(s)
        assert s.meta["layout_archetype"]

    def test_a_partial_stamp_falls_back_rather_than_raising(self):
        s = self._swimmer()
        s.meta[_ARCH_DECISION_KEY] = {"name": "BoundedVolume"}
        compute_layout(s)
        assert s.meta["layout_archetype"]


# ── 4. the layout's spawn reaches the scene ───────────────────────────────────

class TestSpawnReachesTheScene:
    def _emitted_avatar(self, schema):
        schema, files, _log = replay(schema)
        path, scene = _primary_scene(schema, files)
        objs = _player_gameobjects(scene)
        return schema, files, objs, _transform_position(scene, objs[0][1])

    def test_the_captured_dropzone_avatar_starts_at_the_corridor_top(self):
        """The regression in full: the layout put the player at y=2000 and the
        emitted scene spawned them at y=1, 2000 m below their own game."""
        data = json.loads(
            (FIXTURES / "dropzone_arcade_llm.schema.json").read_text(
                encoding="utf-8"))
        schema, _files, objs, pos = self._emitted_avatar(
            UnitySchema.model_validate(data))
        group, _signal = pick_player_entity(schema.entities)
        baked = group.instance_positions[0]
        assert baked[1] > 1000, "the layout should place the player up the corridor"
        assert len(objs) == 1, "exactly one Player-tagged GameObject"
        assert pos is not None and abs(pos[1] - (baked[1] + 1.0)) < 0.01

    def test_an_unmergeable_avatar_still_gets_the_layout_spawn(self):
        """kind='robot' is not in _PLAYER_MERGE_KINDS, so the merge cannot run.
        The START POSITION must transfer anyway — it only needs the layout to
        have chosen one."""
        s = _schema([EntitySpec(name="Mech", kind="robot", tag="Player"),
                     EntitySpec(name="Gates", kind="collectible", count=5,
                                placement="scatter")],
                    locomotion="fly")
        s.description = "Freefall descent through a shaft from 900 m altitude."
        _schema_out, _files, objs, pos = self._emitted_avatar(s)
        group, _sig = pick_player_entity(_schema_out.entities)
        assert group.instance_positions, "layout baked no spawn — test is moot"
        assert len(objs) == 1
        assert abs(pos[1] - (group.instance_positions[0][1] + 1.0)) < 0.01

    def test_a_ground_game_avatar_is_unaffected(self):
        s = _schema([EntitySpec(name="Hero", kind="character", tag="Player"),
                     EntitySpec(name="Coin", kind="collectible", count=6,
                                placement="scatter")])
        schema, _files, objs, pos = self._emitted_avatar(s)
        group, _sig = pick_player_entity(schema.entities)
        assert len(objs) == 1
        assert abs(pos[1] - (group.instance_positions[0][1] + 1.0)) < 0.01
