"""Playability evaluator (src/unity_playability.py) — the compiler grading its
own output.

The two failures this exists to catch, both of which passed every automated
check the compiler had before it:
  * ToyRacerArcade — schema-fill kept the player and replaced every rival and
    pickup with scene dressing. Graded 0/10 by a human in the editor.
  * DropZone — four of five explicitly-named entities never reached the build.
"""
from __future__ import annotations

import pytest

from src.unity_layout import compute_layout
from src.unity_playability import (ERROR, WARN, evaluate, gate)
from src.unity_schema import (EntitySpec, MissionObjective, MissionSpec,
                              SceneSpec, StoryBeat, UnitySchema,
                              UnityScript, WorldRegion, WorldSpec)


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


class TestConceptFidelity:
    """The gap brief_entities cannot see: the NAME survived, the WORDS didn't.
    'purple hippos with six legs' -> an entity called Hippo with no colour and
    no leg count grades clean on entity coverage alone."""

    def test_a_modifier_dropped_from_every_field_is_a_warning(self):
        brief = "ENTITIES\n- Hippo: a purple hippo with six legs.\n"
        schema = _playable(user_prompt=brief)
        schema.entities.append(
            EntitySpec(name="Hippo", kind="character", model_hint="hippo"))
        rep, checks = _checks(schema)
        assert "concept_fidelity" in checks
        finding = next(f for f in rep.findings if f.check == "concept_fidelity")
        assert finding.severity == WARN
        assert rep.playable          # WARN never flips the verdict alone

    def test_a_build_that_kept_the_words_passes(self):
        brief = "ENTITIES\n- Hippo: a purple hippo with six legs.\n"
        schema = _playable(user_prompt=brief)
        schema.entities.append(EntitySpec(
            name="Hippo", kind="character",
            model_hint="purple hippo six legs"))
        _, checks = _checks(schema)
        assert "concept_fidelity" not in checks

    def test_an_empty_user_prompt_is_not_graded(self):
        rep = evaluate(_playable(user_prompt=""))
        assert "concept_fidelity" not in rep.checks
        assert "user_prompt" in rep.not_graded["concept_fidelity"]

    def test_a_vibe_prompt_with_no_concrete_words_is_not_graded(self):
        rep = evaluate(_playable(user_prompt="make me a game"))
        assert "concept_fidelity" not in rep.checks


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


# -- graded vs ungraded -------------------------------------------------------
# The evaluator's first version let a check `return` when its evidence was
# missing, which shrank the score's denominator silently. Missing evidence is
# now reported, and an ERROR-class check that never ran can fail the gate.

class TestUngradedChecks:
    def test_an_ungraded_check_is_neither_a_pass_nor_a_failure(self):
        rep = evaluate(_playable())
        assert "role_graph" not in rep.checks
        assert "role_graph" in rep.not_graded
        assert rep.playable

    def test_the_scorecard_names_what_it_could_not_grade(self):
        text = evaluate(_playable()).render()
        assert "not graded" in text
        assert "NOGRD role_graph" in text

    def test_an_empty_user_prompt_no_longer_passes_brief_coverage(self):
        """A replayed schema carries no prompt. Returning True here handed a
        full-weight ERROR-class pass to every schema graded through the CLI."""
        rep = evaluate(_playable(user_prompt=""))
        assert "brief_entities" not in rep.checks
        assert "user_prompt" in rep.not_graded["brief_entities"]

    def test_unbaked_positions_no_longer_pass_the_spatial_checks(self):
        rep = evaluate(_playable(locomotion="fly"))
        for check in ("spawn_clear", "content_reachable"):
            assert check not in rep.checks
            assert "compute_layout" in rep.not_graded[check]

    def test_gate_can_require_that_error_checks_actually_ran(self):
        rep = evaluate(_playable(user_prompt=""))
        assert rep.playable
        assert gate(rep, fail_on_error=True) is None
        reason = gate(rep, fail_on_error=True, require_graded=True)
        assert reason is not None and "could not be graded" in reason

    def test_ungraded_warn_class_checks_never_fail_the_gate(self):
        rep = evaluate(_playable())
        assert "scene_population" in rep.not_graded          # WARN class
        assert "scene_population" not in rep.ungraded_errors


class TestWorldTerrain:
    """schema.world_spec.regions -> src/unity_terraingen.py's terrain bake,
    graded by `_check_world_terrain`. WARN class: a world with no ground
    underfoot is degraded (like `audio` shipping silent), not the ERROR-class
    line between "a game" and "a scene"."""

    def test_no_world_spec_is_ungraded_not_failed(self):
        rep = evaluate(_playable())
        assert "world_terrain" not in rep.checks
        assert "world_terrain" in rep.not_graded
        assert rep.playable

    def test_a_ground_biome_region_passes(self):
        rep = evaluate(_playable(
            world_spec=WorldSpec(regions=[
                WorldRegion(name="Alpha", biome="forest", bounds=[0, 0, 100, 100]),
            ])))
        assert rep.checks["world_terrain"] is True
        assert not [f for f in rep.findings if f.check == "world_terrain"]

    def test_every_region_a_no_ground_biome_warns(self):
        rep = evaluate(_playable(
            world_spec=WorldSpec(regions=[
                WorldRegion(name="Alpha", biome="interior", bounds=[0, 0, 100, 100]),
            ])))
        assert rep.checks["world_terrain"] is False
        findings = [f for f in rep.findings if f.check == "world_terrain"]
        assert len(findings) == 1 and findings[0].severity == WARN
        assert rep.playable   # WARN never fails the gate

    def test_a_degenerate_bounds_region_warns(self):
        rep = evaluate(_playable(
            world_spec=WorldSpec(regions=[
                WorldRegion(name="Alpha", biome="forest", bounds=[5, 5, 5, 5]),
            ])))
        assert rep.checks["world_terrain"] is False

    def test_one_good_region_among_bad_ones_still_passes(self):
        rep = evaluate(_playable(
            world_spec=WorldSpec(regions=[
                WorldRegion(name="Inside", biome="interior", bounds=[0, 0, 10, 10]),
                WorldRegion(name="Outside", biome="forest", bounds=[0, 0, 100, 100]),
            ])))
        assert rep.checks["world_terrain"] is True


# -- objectives the fill expressed through tags and missions -------------------

class TestObjectiveIdentity:
    def _tagged_objective(self, tag, kind="mechanism"):
        s = _schema(
            [EntitySpec(name="Hero", kind="player", tag="Player"),
             EntitySpec(name="Gates", kind=kind, count=4, placement="scatter",
                        tag=tag)],
            scripts=[UnityScript(name="P", type="player", description="d")])
        s.meta["spatial_primitive"] = "ground_plane"
        return s

    def test_a_generic_kind_with_the_meaning_in_the_tag_is_an_objective(self):
        """Live schema-fill writes kind='mechanism' tag='Gate'. Reading kind
        alone made every objective check vacuous on real output."""
        rep, checks = _checks(self._tagged_objective("Gate"))
        assert "objective" not in checks          # it found the objective
        assert rep.checks["objective"]

    def test_an_untagged_generic_objective_is_still_not_an_objective(self):
        rep, checks = _checks(self._tagged_objective("Untagged"))
        assert "objective" in checks

    def test_tag_carried_objectives_are_graded_for_reach(self):
        s = self._tagged_objective("Target")
        gates = next(e for e in s.entities if e.name == "Gates")
        gates.instance_positions = [[2.0, 40.0, 2.0]]
        rep = evaluate(s)
        assert not rep.checks["objective_kinematics"]

    def test_a_mission_target_makes_its_entity_an_objective(self):
        s = _schema(
            [EntitySpec(name="Hero", kind="player", tag="Player"),
             EntitySpec(name="LandingPad", kind="platform", tag="Target",
                        instance_positions=[[0.0, 30.0, 6.0]])],
            scripts=[UnityScript(name="P", type="player", description="d")])
        s.meta["spatial_primitive"] = "ground_plane"
        s.missions = [MissionSpec(
            id="m1", title="Land", description="d",
            objectives=[MissionObjective(id="o1", description="touch down",
                                         type="reach",
                                         target_ref="LandingPad")])]
        rep = evaluate(s)
        assert not rep.checks["objective_kinematics"]

    def test_a_mission_targeting_absent_content_is_an_error(self):
        s = _playable()
        s.missions = [MissionSpec(
            id="m1", title="Summit", description="d",
            objectives=[MissionObjective(id="o1", description="reach it",
                                         type="reach",
                                         target_ref="SummitView")])]
        rep, checks = _checks(s)
        assert "objective_targets" in checks
        assert not rep.playable
        assert "SummitView" in next(
            f.summary for f in rep.findings if f.check == "objective_targets")

    def test_no_target_refs_is_ungraded_not_a_pass(self):
        rep = evaluate(_playable())
        assert "objective_targets" not in rep.checks
        assert "objective_targets" in rep.not_graded


class TestObjectiveSceneReachability:
    """A mission's objective target must actually spawn in the scene that
    mission runs in, or SEAM 8's wiring has nothing to bind to no matter how
    correctly it is scoped — confirmed live (GearCitadel): a hall's own
    entity subset omitted the exact entity its own mission's objective
    targeted. `schema.meta["mission_scenes"]` is what
    `_infer_mission_scenes` (src/unity_pipeline.py) would have stamped."""

    def _mission_schema(self, *, scene_entities, target_ref="GearGate"):
        s = _schema(
            [EntitySpec(name="Hero", kind="player", tag="Player"),
             EntitySpec(name="GearGate", kind="prop", count=1)])
        s.scenes = ["Main", "HallOne"]
        s.scene_specs = [SceneSpec(name="HallOne", entities=scene_entities)]
        s.missions = [MissionSpec(
            id="m1", title="Hall One Clearance", description="d",
            objectives=[MissionObjective(id="o1", description="reach it",
                                         type="reach", target_ref=target_ref)])]
        s.meta["mission_scenes"] = {"m1": ["HallOne"]}
        return s

    def test_ungraded_when_no_mission_scenes_were_ever_inferred(self):
        rep = evaluate(_playable())
        assert "objective_scene_reachability" not in rep.checks
        assert "objective_scene_reachability" in rep.not_graded

    def test_error_when_target_missing_from_its_own_missions_scene(self):
        s = self._mission_schema(scene_entities=["SteamJet"])   # GearGate absent
        rep, checks = _checks(s)
        assert "objective_scene_reachability" in checks
        assert not rep.checks["objective_scene_reachability"]
        f = next(f for f in rep.findings if f.check == "objective_scene_reachability")
        assert f.severity == ERROR
        assert "GearGate" in f.summary and "HallOne" in f.summary

    def test_passes_when_target_present_in_its_own_missions_scene(self):
        s = self._mission_schema(scene_entities=["SteamJet", "GearGate"])
        rep, checks = _checks(s)
        assert "objective_scene_reachability" not in checks
        assert rep.checks["objective_scene_reachability"]

    def test_unrestricted_scene_is_not_graded_against(self):
        # entities=None means "hosts everything" -- nothing to flag.
        s = self._mission_schema(scene_entities=["SteamJet"])
        s.scene_specs = [SceneSpec(name="HallOne", entities=None)]
        rep, checks = _checks(s)
        assert "objective_scene_reachability" not in checks
        assert rep.checks["objective_scene_reachability"]


# -- the avatar chain ---------------------------------------------------------

class TestAvatarBinding:
    def _script_only(self):
        """A player script but no Player-tagged entity — what
        _reserve_player_tag leaves behind when it demotes a kind='prop' avatar
        and nothing restores the tag."""
        return _schema(
            [EntitySpec(name="Statue", kind="prop", count=6, tag="Untagged")],
            scripts=[UnityScript(name="P", type="player", description="d")])

    def test_a_script_without_a_tagged_entity_is_a_warning(self):
        rep, checks = _checks(self._script_only())
        assert "avatar_binding" in checks
        assert next(f for f in rep.findings
                    if f.check == "avatar_binding").severity == WARN

    def test_a_tagged_avatar_binds(self):
        rep = evaluate(_playable())
        assert rep.checks["avatar_binding"]

    def test_a_plural_avatar_is_an_error(self):
        """count>1 spawns N Player-tagged objects and disables the generator's
        player-merge, which needs count==1."""
        s = _playable()
        hero = next(e for e in s.entities if e.tag == "Player")
        hero.count = 6
        rep, checks = _checks(s)
        assert "avatar_cardinality" in checks
        assert not rep.playable

    def test_cardinality_is_ungraded_with_no_tagged_avatar(self):
        rep = evaluate(self._script_only())
        assert "avatar_cardinality" not in rep.checks
        assert "avatar_cardinality" in rep.not_graded

    def test_a_volume_game_with_no_tagged_avatar_fails_volume_player(self):
        """The check used to pass here: `avatar is None` skipped it, so the
        more broken the schema, the likelier it scored."""
        s = self._script_only()
        s.locomotion = "fly"
        s.entities.append(EntitySpec(name="Gates", kind="collectible", count=4,
                                     placement="scatter", elevation=800.0))
        rep, checks = _checks(s)
        assert "volume_player" in checks
        assert not rep.playable


# -- spawn clearance is measured against the real spawn -----------------------

class TestSpawnPoint:
    def test_a_blocker_at_an_elevated_spawn_is_found(self):
        s = _playable()
        hero = next(e for e in s.entities if e.tag == "Player")
        hero.instance_positions = [[0.0, 40.0, 0.0]]
        crate = EntitySpec(name="Crate", kind="obstacle", count=1)
        crate.instance_positions = [[0.3, 40.2, 0.4]]
        s.entities.append(crate)
        _, checks = _checks(s)
        assert "spawn_clear" in checks

    def test_the_origin_is_not_assumed_to_be_the_spawn(self):
        """A climbing wall stands the player at z=4 facing holds at z=0.2.
        Measuring those holds against (0,0) reported the entire hold chain as
        trapping the player at t=0."""
        s = _playable()
        hero = next(e for e in s.entities if e.tag == "Player")
        hero.instance_positions = [[0.0, 0.0, 4.0]]
        holds = EntitySpec(name="Holds", kind="handhold", count=2,
                           tag="Interactable")
        holds.instance_positions = [[0.0, 0.0, 0.2], [0.0, 2.0, 0.2]]
        s.entities.append(holds)
        _, checks = _checks(s)
        assert "spawn_clear" not in checks

    def test_a_platform_at_the_spawn_is_the_floor_not_a_trap(self):
        """VerticalCorridor deliberately spawns the player attached to the exit
        platform."""
        s = _playable()
        hero = next(e for e in s.entities if e.tag == "Player")
        hero.instance_positions = [[0.0, 2000.0, 0.0]]
        exit_pad = EntitySpec(name="Exit", kind="structure", count=1)
        exit_pad.instance_positions = [[0.0, 2000.0, 0.0]]
        s.entities.append(exit_pad)
        _, checks = _checks(s)
        assert "spawn_clear" not in checks


# -- unbaked gameplay groups --------------------------------------------------

class TestUnbakedGameplay:
    def _volume(self):
        """Everything already baked, so each test isolates the ONE group it
        adds."""
        s = _playable(locomotion="fly")
        s.meta["spatial_primitive"] = "airspace_column"
        hero = next(e for e in s.entities if e.tag == "Player")
        hero.instance_positions = [[0.0, 500.0, 0.0]]
        coin = next(e for e in s.entities if e.name == "Coin")
        coin.instance_positions = [[2.0, 480.0, 2.0]]
        return s

    def test_a_gameplay_group_with_no_baked_position_is_flagged(self):
        s = self._volume()
        s.entities.append(EntitySpec(name="Rivals", kind="enemy", count=6,
                                     placement="scatter", tag="Enemy"))
        rep, checks = _checks(s)
        assert "unbaked_gameplay" in checks

    def test_a_path_group_with_points_is_legitimately_unbaked(self):
        s = self._volume()
        s.entities.append(EntitySpec(
            name="Rivals", kind="enemy", count=6, placement="path",
            tag="Enemy", path_points=[[0.0, 500.0, 0.0], [0.0, 500.0, 50.0]]))
        _, checks = _checks(s)
        assert "unbaked_gameplay" not in checks

    def test_the_flat_plane_lets_the_runtime_scatter_decide(self):
        s = _playable()
        s.meta["spatial_primitive"] = "ground_plane"
        s.entities.append(EntitySpec(name="Rivals", kind="enemy", count=6,
                                     placement="scatter", tag="Enemy"))
        _, checks = _checks(s)
        assert "unbaked_gameplay" not in checks


# -- a swimmer swims up -------------------------------------------------------

def test_a_swim_descent_never_fails_content_above_the_diver():
    """'You cannot fall upwards' is a statement about freefall. Applied to
    water it failed a reef survey on fish 18 m above the diver."""
    s = _schema(
        [EntitySpec(name="Diver", kind="character", tag="Player",
                    instance_positions=[[0.0, 100.0, 0.0]]),
         EntitySpec(name="ReefFish", kind="enemy", count=12, tag="Enemy",
                    instance_positions=[[3.0, 118.0, 3.0]])],
        scripts=[UnityScript(name="P", type="player", description="d")],
        locomotion="swim")
    s.meta["spatial_primitive"] = "depth_column"
    s.meta["layout_archetype"] = "VerticalCorridor"
    assert evaluate(s).checks["objective_kinematics"]

    s.locomotion = "fly"
    assert not evaluate(s).checks["objective_kinematics"]


# ── custom_script_body ───────────────────────────────────────────────────────
#
# The custom template renders NOTHING but the fields and bodies the schema
# carries, so a `type: custom` script with every body empty compiles to a class
# of [SerializeField] fields with no methods — and `attach_to` still puts it on
# a live GameObject. Not a compile error, not a missing entity, not a broken
# seam, and until now not reported by anything.
#
# Fixture provenance: the shape below is TEST DATA transcribed from
# C:\u\CodeBranchSim, where all four custom scripts had empty bodies.

def _custom(name="FileNodeController", **kw):
    return UnityScript(name=name, type="custom",
                       description="Handles dragging file nodes.", **kw)


def _with_custom(*custom):
    """`_playable` fixes its own script list, so build the same passing baseline
    and APPEND — the custom scripts are the subject, not a replacement."""
    return _schema(
        [EntitySpec(name="Hero", kind="player", count=1, tag="Player"),
         EntitySpec(name="Coin", kind="collectible", count=6,
                    placement="scatter", tag="Collectible")],
        scripts=[UnityScript(name="PlayerController", type="player", description="d"),
                 UnityScript(name="CamRig", type="camera", description="d"),
                 *custom])


def test_a_custom_script_with_no_body_is_reported():
    rep = evaluate(_with_custom(_custom()))
    finding = next(f for f in rep.findings if f.check == "custom_script_body")
    assert finding.severity == WARN
    assert "FileNodeController" in finding.summary
    assert rep.checks["custom_script_body"] is False


def test_declared_fields_are_not_behaviour():
    """The whole trap: Step 3 fills extra_fields for nearly every custom script
    and leaves the bodies empty, so grading on "has any code field" would pass
    exactly the builds this exists to catch."""
    from src.unity_schema import CodeField
    rep = evaluate(_with_custom(_custom(extra_usings=["System.Collections.Generic"],
                extra_fields=[CodeField(name="_nodes", type="int", default="0")])))
    assert rep.checks["custom_script_body"] is False


@pytest.mark.parametrize("kw", [
    {"update_extra": "transform.Rotate(0f, 1f, 0f);"},
    {"awake_extra": "_n = 0;"},
    {"fixed_update_extra": "// physics"},
])
def test_any_real_body_passes(kw):
    rep = evaluate(_with_custom(_custom(**kw)))
    assert rep.checks["custom_script_body"] is True


def test_an_authored_method_counts_as_a_body():
    from src.unity_schema import CodeMethod
    rep = evaluate(_with_custom(_custom(extra_methods=[
        CodeMethod(signature="public void Drop()", body="return;")])))
    assert rep.checks["custom_script_body"] is True


def test_a_method_with_a_blank_body_does_not_count_as_a_body():
    """Live: VectorCore's SurfaceModifier came back from Step 3 with
    `extra_methods=[{"body": ""}]` — a signature, no body. A plain
    truthiness check over `extra_methods` (a non-empty list) would grade
    this as having a body when the class does nothing."""
    from src.unity_schema import CodeMethod
    rep = evaluate(_with_custom(_custom(extra_methods=[
        CodeMethod(signature="void Foo()", body="")])))
    assert rep.checks["custom_script_body"] is False


def test_a_typed_script_is_never_graded():
    """A `player`/`camera`/`game_manager` script renders a proven baseline
    template, so an empty body list says nothing about it."""
    rep = evaluate(_playable())
    assert "custom_script_body" not in rep.checks
    assert "custom_script_body" in rep.not_graded


def test_only_the_empty_ones_are_named():
    rep = evaluate(_with_custom(
        _custom("Alive", update_extra="transform.Rotate(0f, 1f, 0f);"),
        _custom("Dead")))
    finding = next(f for f in rep.findings if f.check == "custom_script_body")
    assert "1 of 2" in finding.summary
    assert "Dead" in finding.summary and "Alive" not in finding.summary
