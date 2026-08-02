"""Playability evaluator (src/unity_playability.py) — the compiler grading its
own output.

The two failures this exists to catch, both of which passed every automated
check the compiler had before it:
  * ToyRacerArcade — schema-fill kept the player and replaced every rival and
    pickup with scene dressing. Graded 0/10 by a human in the editor.
  * DropZone — four of five explicitly-named entities never reached the build.
"""
from __future__ import annotations

from src.unity_layout import compute_layout
from src.unity_playability import (ERROR, WARN, evaluate, gate)
from src.unity_schema import (EntitySpec, SceneSpec, StoryBeat, UnitySchema,
                              UnityScript)


def _schema(entities=None, *, scripts=None, locomotion="walk",
            user_prompt="", **kw):
    return UnitySchema(
        project_name="PlayTest", company_name="C", description="",
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.0", color_space="Linear",
        features=[], scenes=["Main"], packages=[],
        locomotion=locomotion,
        entities=entities if entities is not None else [],
        scripts=scripts or [],
        meta={"user_prompt": user_prompt},
        **kw)


def _playable(**kw):
    """A minimal schema that passes every ERROR-class check."""
    return _schema(
        [EntitySpec(name="Hero", kind="player", count=1, tag="Player"),
         EntitySpec(name="Coin", kind="collectible", count=6,
                    placement="scatter", tag="Collectible")],
        scripts=[UnityScript(name="PlayerController", type="player", description="d"),
                 UnityScript(name="CamRig", type="camera", description="d")],
        **kw)


def _checks(schema):
    rep = evaluate(schema)
    return rep, {f.check for f in rep.findings}


# ── the headline verdict ──────────────────────────────────────────────────────

class TestVerdict:
    def test_a_complete_schema_is_playable(self):
        rep = evaluate(_playable())
        assert rep.playable
        assert rep.score >= 90

    def test_an_empty_schema_is_not_playable(self):
        rep = evaluate(_schema())
        assert not rep.playable
        assert rep.score < 60

    def test_score_is_a_weighted_pass_rate(self):
        assert evaluate(_schema()).score < evaluate(_playable()).score
        assert 0 <= evaluate(_schema()).score <= 100

    def test_evaluate_never_mutates_the_schema(self):
        schema = _playable()
        before = schema.model_dump_json()
        evaluate(schema)
        assert schema.model_dump_json() == before

    def test_render_names_the_spatial_primitive(self):
        assert "airspace_column" in evaluate(
            _playable(locomotion="fly")).render()


# ── the reported failures ─────────────────────────────────────────────────────

class TestReportedFailures:
    def test_dropped_brief_entities_are_an_error(self):
        """DropZone: the brief named five entities and the build shipped one."""
        brief = ("ENTITIES\n"
                 "- Player: lead skydiver in a jumpsuit\n"
                 "- Enemy: formation AI skydivers (3)\n"
                 "- Collectible: altitude gate ring\n")
        rep, checks = _checks(_playable(user_prompt=brief))
        assert "brief_entities" in checks
        finding = next(f for f in rep.findings if f.check == "brief_entities")
        assert finding.severity == ERROR
        assert not rep.playable

    def test_a_brief_whose_entities_all_survived_passes(self):
        brief = "ENTITIES\n- Coin: a spinning pickup (kind: collectible).\n"
        schema = _schema(
            [EntitySpec(name="Hero", kind="player", tag="Player"),
             EntitySpec(name="Coin", kind="collectible", tag="Collectible")],
            scripts=[UnityScript(name="P", type="player", description="d")],
            user_prompt=brief)
        rep, checks = _checks(schema)
        assert "brief_entities" not in checks

    def test_a_vibe_prompt_with_no_entities_section_is_not_penalised(self):
        rep, checks = _checks(_playable(user_prompt="make me a racing game"))
        assert "brief_entities" not in checks

    def test_no_opponents_and_no_pickups_is_an_error(self):
        """The ToyRacer signature: a player alone in a themed prop scatter."""
        schema = _schema(
            [EntitySpec(name="Hero", kind="player", tag="Player"),
             EntitySpec(name="Trees", kind="prop", count=20, placement="scatter")],
            scripts=[UnityScript(name="P", type="player", description="d")])
        rep, checks = _checks(schema)
        assert "objective" in checks
        assert not rep.playable

    def test_story_beats_alone_count_as_an_objective(self):
        schema = _schema(
            [EntitySpec(name="Hero", kind="player", tag="Player")],
            scripts=[UnityScript(name="P", type="player", description="d")],
            story_beats=[StoryBeat(id="b1", label="Arrive", description="d",
                                   trigger="on_start")])
        _, checks = _checks(schema)
        assert "objective" not in checks


# ── individual checks ─────────────────────────────────────────────────────────

class TestAvatar:
    def test_no_avatar_at_all(self):
        _, checks = _checks(_schema([EntitySpec(name="Trees", kind="prop")]))
        assert "avatar" in checks

    def test_two_player_tags_is_an_error(self):
        """Unity's FindGameObjectWithTag returns one arbitrary match."""
        schema = _playable()
        schema.entities.append(
            EntitySpec(name="Decoy", kind="character", tag="Player"))
        rep, checks = _checks(schema)
        assert "avatar" in checks
        assert next(f for f in rep.findings
                    if f.check == "avatar").severity == ERROR

    def test_no_controller_is_an_error(self):
        schema = _playable()
        schema.scripts = [s for s in schema.scripts if s.type != "player"]
        _, checks = _checks(schema)
        assert "controllable" in checks

    def test_missing_camera_is_only_a_warning(self):
        schema = _playable()
        schema.scripts = [s for s in schema.scripts if s.type != "camera"]
        rep, checks = _checks(schema)
        assert "camera" in checks
        assert next(f for f in rep.findings
                    if f.check == "camera").severity == WARN
        assert rep.playable


class TestSpatialChecks:
    def test_a_group_on_the_player_spawn_is_an_error(self):
        """A collider on world XZ (0,0) traps the player at t=0."""
        schema = _playable()
        blocker = EntitySpec(name="Crate", kind="obstacle", count=1)
        blocker.instance_positions = [[0.0, 0.0, 0.0]]
        schema.entities.append(blocker)
        _, checks = _checks(schema)
        assert "spawn_clear" in checks

    def test_the_layout_pass_keeps_the_spawn_clear(self):
        schema = _playable()
        schema.entities.append(
            EntitySpec(name="Crate", kind="obstacle", count=8,
                       placement="scatter"))
        compute_layout(schema)
        _, checks = _checks(schema)
        assert "spawn_clear" not in checks

    def test_content_outside_the_ground_bounds_is_an_error(self):
        schema = _playable()
        stray = EntitySpec(name="Island", kind="obstacle", count=1)
        stray.instance_positions = [[1900.0, 0.0, 1900.0]]
        stray.group_radius = 1.0
        stray.origin_offset = [0.0, 0.0]
        schema.entities.append(stray)
        _, checks = _checks(schema)
        assert "content_reachable" in checks

    def test_a_grounded_player_in_a_volume_game_is_an_error(self):
        """The avatar left on the floor while the game happens at altitude."""
        schema = _playable(locomotion="fly")
        schema.entities.append(
            EntitySpec(name="Gates", kind="collectible", count=4,
                       placement="scatter", elevation=120.0))
        _, checks = _checks(schema)
        assert "volume_player" in checks

    def test_the_spatial_primitive_resolves_that_by_itself(self):
        schema = _playable(locomotion="fly")
        schema.entities.append(
            EntitySpec(name="Gates", kind="collectible", count=4,
                       placement="scatter"))
        compute_layout(schema)
        rep, checks = _checks(schema)
        assert "volume_player" not in checks
        assert rep.playable

    def test_a_surface_game_is_never_flagged_for_a_grounded_player(self):
        _, checks = _checks(_playable(locomotion="walk"))
        assert "volume_player" not in checks


class TestContentChecks:
    def test_untagged_pickups_cannot_be_collected(self):
        """The spawner only attaches a collider to a tagged group."""
        schema = _playable()
        schema.entities.append(
            EntitySpec(name="Gem", kind="collectible", count=4, tag="Untagged"))
        rep, checks = _checks(schema)
        assert "interactables" in checks
        assert rep.playable   # degraded, not broken

    def test_a_scene_hosting_only_scenery_is_flagged(self):
        schema = _playable()
        schema.entities.append(EntitySpec(name="Trees", kind="prop", count=9))
        schema.scene_specs = [SceneSpec(name="Main", description="d",
                                        entities=["Trees"])]
        _, checks = _checks(schema)
        assert "scene_population" in checks

    def test_a_scene_with_no_declared_subset_hosts_everything(self):
        schema = _playable()
        schema.scene_specs = [SceneSpec(name="Main", description="d")]
        _, checks = _checks(schema)
        assert "scene_population" not in checks

    def test_an_enemy_script_with_nothing_tagged_enemy_is_flagged(self):
        schema = _playable()
        schema.scripts.append(
            UnityScript(name="EnemyAI", type="enemy", description="d"))
        _, checks = _checks(schema)
        assert "enemy_wiring" in checks


# ── the gate ──────────────────────────────────────────────────────────────────

class TestGate:
    def test_errors_fail_when_gating(self):
        assert gate(evaluate(_schema()), fail_on_error=True) is not None

    def test_errors_pass_when_advisory(self):
        assert gate(evaluate(_schema()), fail_on_error=False) is None

    def test_a_playable_build_always_passes(self):
        assert gate(evaluate(_playable()), fail_on_error=True) is None

    def test_min_score_can_fail_a_build_with_only_warnings(self):
        schema = _playable()
        schema.scripts = [s for s in schema.scripts if s.type != "camera"]
        rep = evaluate(schema)
        assert rep.playable
        assert gate(rep, min_score=100, fail_on_error=False) is not None
        assert gate(rep, min_score=0, fail_on_error=False) is None


# -- role graph lint consumption ----------------------------------------------

class TestRoleGraphLint:
    def test_clean_meta_is_vacuous_not_a_free_pass(self):
        rep = evaluate(_playable())
        assert "role_graph" not in rep.checks
        assert rep.playable

    def test_r3_violation_is_an_error(self):
        s = _playable()
        s.meta["role_lint"] = {"violations": [
            "R3: role graph needs exactly 1 player character, found 0: none"]}
        rep = evaluate(s)
        assert not rep.checks["role_graph"]
        assert not rep.playable
        assert any(f.check == "role_graph" and f.severity == ERROR
                   for f in rep.findings)

    def test_repairs_alone_stay_informational(self):
        s = _playable()
        s.meta["role_lint"] = {"repairs": [
            "R2: demoted TheAppraiser from player (tagged FinalBoss)"]}
        rep = evaluate(s)
        assert rep.checks["role_graph"]
        assert rep.playable

    def test_malformed_lint_never_crashes_the_gate(self):
        s = _playable()
        s.meta["role_lint"] = "not-a-dict"
        rep = evaluate(s)
        assert "role_graph" not in rep.checks
        assert rep.playable


# -- kinematic reachability ---------------------------------------------------

class TestObjectiveKinematics:
    def _ground(self, y):
        s = _playable()
        s.meta["spatial_primitive"] = "ground_plane"
        coin = next(e for e in s.entities if e.name == "Coin")
        coin.instance_positions = [[3.0, y, 4.0]]
        return s

    def test_ground_level_coin_is_reachable(self):
        rep = evaluate(self._ground(0.5))
        assert rep.checks["objective_kinematics"]

    def test_floating_coin_in_a_walking_game_is_not(self):
        rep = evaluate(self._ground(12.0))
        assert not rep.checks["objective_kinematics"]
        assert not rep.playable

    def test_unbaked_objectives_are_vacuous_not_a_free_pass(self):
        s = _playable()
        s.meta["spatial_primitive"] = "ground_plane"
        rep = evaluate(s)
        assert "objective_kinematics" not in rep.checks
        assert rep.playable

    def _descent(self, coin_y):
        s = _playable(locomotion="fly")
        s.meta["spatial_primitive"] = "airspace_column"
        s.meta["layout_archetype"] = "VerticalCorridor"
        hero = next(e for e in s.entities if e.tag == "Player")
        hero.instance_positions = [[0.0, 100.0, 0.0]]
        hero.elevation = 100.0
        coin = next(e for e in s.entities if e.name == "Coin")
        coin.instance_positions = [[0.0, coin_y, 5.0]]
        coin.elevation = coin_y
        return s

    def test_descent_objective_below_the_jump_point_passes(self):
        rep = evaluate(self._descent(40.0))
        assert rep.checks["objective_kinematics"]

    def test_descent_objective_above_the_jump_point_fails(self):
        rep = evaluate(self._descent(150.0))
        assert not rep.checks["objective_kinematics"]
        assert any("cannot fall upwards" in f.summary
                   for f in rep.findings if f.check == "objective_kinematics")

    def test_bounded_volume_reaches_altitude_freely(self):
        s = self._descent(150.0)
        s.meta["layout_archetype"] = "BoundedVolume"
        rep = evaluate(s)
        assert rep.checks["objective_kinematics"]

    def _wall(self, hold_ys, coin_y):
        s = _playable()
        s.meta["spatial_primitive"] = "wall_surface"
        s.entities.append(EntitySpec(
            name="Holds", kind="handhold", count=len(hold_ys),
            tag="Environment",
            instance_positions=[[0.0, y, 0.2] for y in hold_ys]))
        coin = next(e for e in s.entities if e.name == "Coin")
        coin.instance_positions = [[0.0, coin_y, 0.2]]
        return s

    def test_connected_hold_chain_reaches_the_coin(self):
        rep = evaluate(self._wall([2, 4, 6, 8, 10], 11.0))
        assert rep.checks["objective_kinematics"]

    def test_a_gap_in_the_chain_strands_the_summit_coin(self):
        rep = evaluate(self._wall([2, 4, 20, 22], 23.0))
        assert not rep.checks["objective_kinematics"]
        assert any("hold chain ends at" in f.summary
                   for f in rep.findings if f.check == "objective_kinematics")

    def test_reach_override_is_read_from_every_key_layout_accepts(self):
        """The gate must grade the wall against the reach the LAYOUT packed at.
        `_reach_distance` accepts three meta keys; reading only the first one
        graded a wall packed at 'hold_reach' against the 2.5 default, collapsed
        the ceiling to the base hold, and failed a climbable wall."""
        s = self._wall([2, 8, 14, 20], 21.0)
        assert not evaluate(s).checks["objective_kinematics"], "2.5 default"
        for key in ("reach_distance", "layout_reach_distance", "hold_reach"):
            s = self._wall([2, 8, 14, 20], 21.0)
            s.meta[key] = 6.0
            assert evaluate(s).checks["objective_kinematics"], key

    def test_objective_kind_is_matched_case_insensitively(self):
        """`kind` has no lowercasing validator. A capitalised kind counts as an
        objective for _check_objective, so it must be graded here too rather
        than making the check silently vacuous."""
        s = self._ground(12.0)
        coin = next(e for e in s.entities if e.name == "Coin")
        coin.kind = "Collectible"
        rep = evaluate(s)
        assert not rep.checks["objective_kinematics"]
