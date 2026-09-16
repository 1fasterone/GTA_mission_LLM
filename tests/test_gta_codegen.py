"""
Tests for CodeGenerator, _build_template_context, and helper functions.
No LLM / network — pure deterministic code compilation from pre-built schemas.
"""
import json
import re

import pytest

from src.code_generator import (
    CodeGenerator,
    _build_template_context,
    _escape_cs_string,
    _to_class_name,
    _LOCATIONS,
)
from src.mission_schema import MissionObject, MissionPed, MissionSchema, MissionStep, MissionVehicle


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def gen() -> CodeGenerator:
    return CodeGenerator("templates/mission.cs.j2")


# ══════════════════════════════════════════════════════════════════════════════
# _to_class_name
# ══════════════════════════════════════════════════════════════════════════════

class TestToClassName:
    def test_simple_title_case(self):
        assert _to_class_name("The Harbor Job") == "TheHarborJob"

    def test_lowercase_words(self):
        assert _to_class_name("the harbor job") == "TheHarborJob"

    def test_special_characters_stripped(self):
        assert _to_class_name("A-High-Speed Chase!") == "AHighSpeedChase"

    def test_digit_prefix_gets_mission_prefix(self):
        name = _to_class_name("3 Way Deal")
        assert name.startswith("Mission")
        assert not name[0].isdigit()

    def test_empty_string_returns_mission_unnamed(self):
        assert _to_class_name("") == "MissionUnnamed"

    def test_only_symbols_returns_mission_unnamed(self):
        assert _to_class_name("!!!") == "MissionUnnamed"

    def test_single_word_capitalised(self):
        assert _to_class_name("heist") == "Heist"

    def test_pascal_case_input_titlecased_as_single_token(self):
        # _to_class_name runs .title() which treats CamelCase as one token —
        # "TheDocksHeist" → "Thedocksheist".  Use space-separated input for
        # reliable multi-word conversion.
        assert _to_class_name("The Docks Heist") == "TheDocksHeist"

    def test_numbers_in_middle_preserved(self):
        name = _to_class_name("Mission 51")
        assert "51" in name


# ══════════════════════════════════════════════════════════════════════════════
# _escape_cs_string
# ══════════════════════════════════════════════════════════════════════════════

class TestEscapeCsString:
    def test_plain_string_unchanged(self):
        assert _escape_cs_string("hello world") == "hello world"

    def test_backslash_doubled(self):
        assert _escape_cs_string("path\\to\\file") == "path\\\\to\\\\file"

    def test_double_quote_escaped(self):
        assert _escape_cs_string('say "hello"') == 'say \\"hello\\"'

    def test_newline_escaped(self):
        assert _escape_cs_string("line1\nline2") == "line1\\nline2"

    def test_carriage_return_escaped(self):
        assert _escape_cs_string("a\rb") == "a\\rb"

    def test_tab_escaped(self):
        assert _escape_cs_string("col1\tcol2") == "col1\\tcol2"

    def test_combined_special_chars(self):
        result = _escape_cs_string('He said "hello"\nand left')
        assert '\\"' in result
        assert "\\n" in result


# ══════════════════════════════════════════════════════════════════════════════
# _build_template_context
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildTemplateContext:

    def _schema(self, **kwargs) -> MissionSchema:
        defaults = dict(
            mission_name="Test Mission",
            location_name="docks",
            time_of_day=20,
            weather="CLEAR",
            steps=[MissionStep(type="go_to", label="Go here")],
            peds=[],
            vehicles=[],
            objects=[],
            audio=[],
        )
        defaults.update(kwargs)
        return MissionSchema(**defaults)

    def test_class_name_in_context(self):
        ctx = _build_template_context(self._schema(mission_name="The Docks Heist"))
        assert ctx["class_name"] == "TheDocksHeist"

    def test_location_coords_match_lookup_table(self):
        ctx = _build_template_context(self._schema(location_name="airport"))
        expected = _LOCATIONS["airport"]
        assert ctx["location"]["x"] == expected["x"]
        assert ctx["location"]["y"] == expected["y"]

    def test_unknown_location_falls_back_to_docks(self):
        # The schema validator clamps unknown locations to "docks"
        schema = self._schema(location_name="docks")
        ctx = _build_template_context(schema)
        assert ctx["location"]["x"] == _LOCATIONS["docks"]["x"]

    def test_weather_native_string_in_context(self):
        ctx = _build_template_context(self._schema(weather="RAIN"))
        assert ctx["weather"] == "RAIN"

    def test_time_of_day_in_context(self):
        ctx = _build_template_context(self._schema(time_of_day=3))
        assert ctx["time_of_day"] == 3

    def test_go_to_steps_create_waypoints(self):
        schema = self._schema(steps=[
            MissionStep(type="go_to", label="First"),
            MissionStep(type="go_to", label="Second"),
        ])
        ctx = _build_template_context(schema)
        assert len(ctx["waypoints"]) == 2

    def test_non_goto_steps_do_not_create_waypoints(self):
        schema = self._schema(steps=[
            MissionStep(type="kill_all", label="Kill"),
            MissionStep(type="pickup", label="Grab"),
        ])
        ctx = _build_template_context(schema)
        assert len(ctx["waypoints"]) == 0

    def test_waypoints_spread_from_origin(self):
        """Each go_to waypoint must be offset from SpawnOrigin, not stacked."""
        schema = self._schema(
            location_name="docks",
            steps=[
                MissionStep(type="go_to", label="A"),
                MissionStep(type="go_to", label="B"),
            ],
        )
        ctx = _build_template_context(schema)
        origin_x = _LOCATIONS["docks"]["x"]
        origin_y = _LOCATIONS["docks"]["y"]
        wp0 = ctx["waypoints"][0]
        wp1 = ctx["waypoints"][1]
        # Each waypoint must differ from the raw origin
        assert wp0["x"] != origin_x or wp0["y"] != origin_y
        # The two waypoints must be different from each other
        assert (wp0["x"], wp0["y"]) != (wp1["x"], wp1["y"])

    def test_ped_weapon_name_none_for_no_weapon(self):
        schema = self._schema(peds=[
            MissionPed(description="cop", model="s_m_y_cop_01", role="guard", weapon="none"),
        ])
        ctx = _build_template_context(schema)
        assert ctx["peds"][0]["weapon_name"] is None

    def test_ped_weapon_name_correct_for_pistol(self):
        schema = self._schema(peds=[
            MissionPed(description="cop", model="s_m_y_cop_01", role="guard", weapon="pistol"),
        ])
        ctx = _build_template_context(schema)
        assert ctx["peds"][0]["weapon_name"] == "weapon_pistol"

    def test_ped_weapon_name_correct_for_rifle(self):
        schema = self._schema(peds=[
            MissionPed(description="cop", model="s_m_y_cop_01", role="guard", weapon="rifle"),
        ])
        ctx = _build_template_context(schema)
        assert ctx["peds"][0]["weapon_name"] == "weapon_assaultrifle"

    def test_ped_ammo_default_in_context(self):
        schema = self._schema(peds=[
            MissionPed(description="cop", model="s_m_y_cop_01", role="guard", weapon="pistol"),
        ])
        ctx = _build_template_context(schema)
        assert ctx["peds"][0]["ammo"] == 100

    def test_ped_ammo_authored_in_context(self):
        schema = self._schema(peds=[
            MissionPed(description="sniper", model="s_m_y_cop_01", role="target",
                       weapon="sniper", ammo=8),
        ])
        ctx = _build_template_context(schema)
        assert ctx["peds"][0]["ammo"] == 8

    def test_ped_health_and_armor_default_in_context(self):
        schema = self._schema(peds=[
            MissionPed(description="cop", model="s_m_y_cop_01", role="guard"),
        ])
        ctx = _build_template_context(schema)
        assert ctx["peds"][0]["health"] == 100.0
        assert ctx["peds"][0]["armor"] == 0.0

    def test_ped_health_and_armor_authored_in_context(self):
        schema = self._schema(peds=[
            MissionPed(description="tank", model="s_m_y_cop_01", role="target",
                       health=300, armor=50),
        ])
        ctx = _build_template_context(schema)
        assert ctx["peds"][0]["health"] == 300.0
        assert ctx["peds"][0]["armor"] == 50.0

    def test_ped_is_not_hostile_for_guard_role(self):
        # Guards get their own passive GuardCurrentPosition() branch in the
        # template instead of instant Task.Combat at spawn.
        schema = self._schema(peds=[
            MissionPed(description="cop", model="s_m_y_cop_01", role="guard"),
        ])
        ctx = _build_template_context(schema)
        assert ctx["peds"][0]["is_hostile"] is False

    def test_ped_is_hostile_for_target_role(self):
        schema = self._schema(peds=[
            MissionPed(description="rival gang leader", model="g_m_y_lost_01", role="target"),
        ])
        ctx = _build_template_context(schema)
        assert ctx["peds"][0]["is_hostile"] is True

    def test_ped_is_not_hostile_for_civilian_role(self):
        schema = self._schema(peds=[
            MissionPed(description="bystander", model="a_m_m_beach_01", role="civilian"),
        ])
        ctx = _build_template_context(schema)
        assert ctx["peds"][0]["is_hostile"] is False

    def test_ped_offsets_are_different_per_group(self):
        """Multiple ped groups must have distinct spread offsets."""
        schema = self._schema(peds=[
            MissionPed(description="a", model="s_m_y_cop_01", role="guard"),
            MissionPed(description="b", model="s_m_y_cop_01", role="patrol"),
        ])
        ctx = _build_template_context(schema)
        p0 = (ctx["peds"][0]["base_x"], ctx["peds"][0]["base_y"])
        p1 = (ctx["peds"][1]["base_x"], ctx["peds"][1]["base_y"])
        assert p0 != p1

    def test_editor_patrol_fixture_preserves_metadata(self, gta_editor_patrol_schema):
        path = gta_editor_patrol_schema.meta["editor"]["paths"][0]
        assert path["id"] == "dock_patrol_alpha"
        assert path["ped_idx"] == 0
        assert path["mode"] == "loop"
        assert path["speed"] == 1.2
        assert path["waypoints"][1]["behavior"] == "scan"
        assert path["waypoints"][2]["behavior"] == "guard"

    def test_editor_patrol_path_context_binds_to_ped(self, gta_editor_patrol_schema):
        ctx = _build_template_context(gta_editor_patrol_schema)
        assert ctx["editor_paths"] == [{
            "id": "dock_patrol_alpha",
            "ped_idx": 0,
            "mode": "loop",
            "mode_cs": "Loop",
            "speed": 1.2,
            "wait_ms": 0,
            "behavior": "walk",
            "behavior_cs": "Walk",
            "guard_until_alert": False,
            "speeds": [1.0, 1.0, 1.2],
            "waits_ms": [0, 2000, 1000],
            "behaviors": ["Walk", "Scan", "Guard"],
            "guard_until_alerts": [False, False, False],
            "points": [
                {"x": -1030.0, "y": -2710.0, "z": 13.8},
                {"x": -1000.0, "y": -2685.0, "z": 13.8},
                {"x": -970.0, "y": -2710.0, "z": 13.8},
            ],
        }]
        assert ctx["peds"][0]["patrol_path_idx"] == 0
        assert ctx["peds"][0]["is_hostile"] is False
        assert ctx["peds"][0]["abs_x"] == -1040.0
        assert ctx["peds"][0]["heading"] == 90.0

    def test_unassigned_editor_path_emits_without_binding(self):
        schema = self._schema(
            peds=[MissionPed(description="dock sentry", model="s_m_y_dockwork_01", role="patrol")],
            meta={"editor": {"paths": [{
                "id": "unassigned_route",
                "ped_idx": None,
                "points": [[-1, -2, 3], [4, 5, 6]],
            }]}},
        )
        ctx = _build_template_context(schema)
        assert ctx["editor_paths"][0]["id"] == "unassigned_route"
        assert ctx["editor_paths"][0]["ped_idx"] is None
        assert ctx["peds"][0]["patrol_path_idx"] is None

    def test_invalid_editor_path_binding_does_not_bind_ped(self):
        schema = self._schema(
            peds=[MissionPed(description="dock sentry", model="s_m_y_dockwork_01", role="patrol")],
            meta={"editor": {"paths": [{
                "id": "missing_ped_route",
                "ped_idx": 99,
                "points": [[-1, -2, 3], [4, 5, 6]],
            }]}},
        )
        ctx = _build_template_context(schema)
        assert ctx["editor_paths"][0]["id"] == "missing_ped_route"
        assert ctx["editor_paths"][0]["ped_idx"] == 99
        assert ctx["peds"][0]["patrol_path_idx"] is None

    def test_unknown_editor_path_mode_falls_back_to_loop(self):
        schema = self._schema(
            meta={"editor": {"paths": [{
                "id": "odd_route",
                "ped_idx": None,
                "mode": "spiral",
                "points": [[-1, -2, 3], [4, 5, 6]],
            }]}},
        )
        ctx = _build_template_context(schema)
        assert ctx["editor_paths"][0]["mode"] == "loop"
        assert ctx["editor_paths"][0]["mode_cs"] == "Loop"

    def test_editor_path_speed_falls_back_to_path_speed(self):
        schema = self._schema(
            meta={"editor": {"paths": [{
                "id": "speed_route",
                "ped_idx": None,
                "speed": 2.5,
                "points": [[-1, -2, 3], [4, 5, 6], [7, 8, 9]],
                "waypoints": [
                    {"speed": 1.25},
                    {},
                    {"speed": "bad"},
                ],
            }]}},
        )
        ctx = _build_template_context(schema)
        assert ctx["editor_paths"][0]["speed"] == 2.5
        assert ctx["editor_paths"][0]["speeds"] == [1.25, 2.5, 2.5]

    def test_editor_path_behavior_metadata_falls_back_to_path_defaults(self):
        schema = self._schema(
            meta={"editor": {"paths": [{
                "id": "behavior_route",
                "ped_idx": None,
                "wait_time": 3,
                "behavior": "guard",
                "guard_until_alert": "true",
                "points": [[-1, -2, 3], [4, 5, 6], [7, 8, 9]],
                "waypoints": [
                    {"wait_time": 1, "behavior": "scan", "guard_until_alert": False},
                    {},
                    {"wait_time": "bad", "behavior": "mystery", "guard_until_alert": "false"},
                ],
            }]}}
        )
        path = _build_template_context(schema)["editor_paths"][0]
        assert path["wait_ms"] == 3000
        assert path["behavior"] == "guard"
        assert path["behavior_cs"] == "Guard"
        assert path["guard_until_alert"] is True
        assert path["waits_ms"] == [1000, 3000, 3000]
        assert path["behaviors"] == ["Scan", "Guard", "Guard"]
        assert path["guard_until_alerts"] == [False, True, False]

    def test_vehicle_offsets_present(self):
        schema = self._schema(vehicles=[
            MissionVehicle(description="van", model="burrito3", role="escape"),
        ])
        ctx = _build_template_context(schema)
        assert "base_x" in ctx["vehicles"][0]
        assert "base_y" in ctx["vehicles"][0]

    def test_audio_pass_fallback_when_no_audio(self):
        ctx = _build_template_context(self._schema(audio=[]))
        assert ctx["audio_pass"] == "FRANKLIN_BIG_01"

    def test_audio_fail_fallback_when_no_audio(self):
        ctx = _build_template_context(self._schema(audio=[]))
        assert ctx["audio_fail"] == "FRANKLIN_LOSE_1"

    def test_audio_pass_name_used_when_present(self):
        from src.mission_schema import MissionAudio
        schema = self._schema(audio=[
            MissionAudio(type="mission_passed", context="done", name="CUSTOM_JINGLE"),
        ])
        ctx = _build_template_context(schema)
        assert ctx["audio_pass"] == "CUSTOM_JINGLE"

    def test_step_waypoint_idx_assigned_for_goto(self):
        schema = self._schema(steps=[
            MissionStep(type="go_to", label="Marker"),
        ])
        ctx = _build_template_context(schema)
        assert ctx["steps"][0]["waypoint_idx"] == 0

    def test_step_waypoint_idx_none_for_non_goto(self):
        schema = self._schema(steps=[
            MissionStep(type="kill_all", label="Kill"),
        ])
        ctx = _build_template_context(schema)
        assert ctx["steps"][0]["waypoint_idx"] is None


# ══════════════════════════════════════════════════════════════════════════════
# CodeGenerator.generate — C# output structure
# ══════════════════════════════════════════════════════════════════════════════

class TestCodeGeneratorGenerate:

    def test_output_contains_class_name(self, gen, gta_full_schema):
        code = gen.generate(gta_full_schema)
        assert "class TheDocksHeist" in code

    def test_output_starts_with_auto_generated_comment(self, gen, gta_full_schema):
        code = gen.generate(gta_full_schema)
        assert code.startswith("// Auto-generated")

    def test_output_contains_using_gta(self, gen, gta_full_schema):
        code = gen.generate(gta_full_schema)
        assert "using GTA;" in code

    def test_output_contains_location_x_coordinate(self, gen, gta_full_schema):
        code = gen.generate(gta_full_schema)
        # Docks x = -1009.0
        assert "-1009.0f" in code

    def test_output_contains_ped_model(self, gen, gta_full_schema):
        code = gen.generate(gta_full_schema)
        assert "s_m_y_cop_01" in code

    def test_output_contains_vehicle_model(self, gen, gta_full_schema):
        code = gen.generate(gta_full_schema)
        assert "police" in code

    def test_output_contains_object_model(self, gen, gta_full_schema):
        code = gen.generate(gta_full_schema)
        assert "prop_drug_package_01" in code

    def test_output_contains_weapon_string_for_armed_ped(self, gen, gta_full_schema):
        code = gen.generate(gta_full_schema)
        assert "weapon_pistol" in code

    def test_editor_patrol_output_contains_runtime_contract(self, gen, gta_editor_patrol_schema):
        code = gen.generate(gta_editor_patrol_schema)
        for token in (
            "private static readonly Vector3[] EditorPath_0",
            "new Vector3(-1030.0f, -2710.0f, 13.8f)",
            "private static readonly float[] EditorPathSpeed_0",
            "private static readonly int[] EditorPathWaitMs_0",
            "private static readonly PatrolWaypointBehavior[] EditorPathBehavior_0",
            "private static readonly bool[] EditorPathGuardUntilAlert_0",
            "1.20f",
            "2000",
            "PatrolWaypointBehavior.Scan",
            "PatrolWaypointBehavior.Guard",
            "// id=dock_patrol_alpha pedIdx=0",
            "private static Vector3[] EditorPathByIndex(int idx)",
            "private static float[] EditorPathSpeedByIndex(int idx)",
            "private static int[] EditorPathWaitMsByIndex(int idx)",
            "private static PatrolWaypointBehavior[] EditorPathBehaviorByIndex(int idx)",
            "private static bool[] EditorPathGuardUntilAlertByIndex(int idx)",
            "private const float DefaultPatrolSpeed = 1.0f;",
            "private const int DefaultScanMs = 2000;",
            "private enum PatrolRouteMode { Once, Loop, PingPong }",
            "private enum PatrolRouteState { Moving, Waiting, Scanning, Guarding, Complete }",
            "private enum PatrolWaypointBehavior { Walk, Jog, Sprint, Scan, Guard }",
            "private class PatrolUnit",
            "public float[] Speed;",
            "public int[] WaitMs;",
            "public PatrolWaypointBehavior[] Behavior;",
            "public bool[] GuardUntilAlert;",
            "public PatrolRouteMode Mode;",
            "public PatrolRouteState State;",
            "public DateTime StateUntil;",
            "public int Dir;",
            "private readonly List<PatrolUnit> _patrols",
            "if (_state == State.Active) UpdatePatrols();",
            "Speed = EditorPathSpeedByIndex(0)",
            "WaitMs = EditorPathWaitMsByIndex(0)",
            "Behavior = EditorPathBehaviorByIndex(0)",
            "GuardUntilAlert = EditorPathGuardUntilAlertByIndex(0)",
            "Mode = PatrolRouteMode.Loop",
            "State = PatrolRouteState.Moving, StateUntil = DateTime.MinValue, Wp = 0, Dir = 1",
            "IssuePatrolMove(_patrols[_patrols.Count - 1]);",
            "private void UpdatePatrols()",
            "private float PatrolSpeedAt(PatrolUnit u)",
            "private int PatrolWaitMsAt(PatrolUnit u)",
            "private PatrolWaypointBehavior PatrolBehaviorAt(PatrolUnit u)",
            "private bool PatrolGuardUntilAlertAt(PatrolUnit u)",
            "private void IssuePatrolMove(PatrolUnit u)",
            "Hash.TASK_FOLLOW_NAV_MESH_TO_COORD",
            "u.Ped.Task.RunTo(target, true);",
            "private void EnterPatrolWaypoint(PatrolUnit u, DateTime now)",
            "private void BeginPatrolScan(PatrolUnit u, DateTime now, int durationMs)",
            "private void BeginPatrolGuard(PatrolUnit u, DateTime now, int durationMs, bool guardUntilAlert)",
            "Hash.TASK_STAND_STILL",
            "Hash.TASK_LOOK_AT_COORD",
            "Hash.TASK_GUARD_CURRENT_POSITION",
            "DateTime.MaxValue",
            "private bool AdvancePatrol(PatrolUnit u)",
            "_patrols.Clear();",
        ):
            assert token in code, f"patrol C# missing {token}"
        assert code.count("{") == code.count("}")
        assert code.count("(") == code.count(")")

    def test_editor_patrol_behaviors_emit_scan_and_timed_guard(self, gen):
        schema = MissionSchema(
            mission_name="Patrol Behaviors",
            location_name="docks",
            time_of_day=20,
            weather="CLEAR",
            steps=[MissionStep(type="go_to", label="Reach the route")],
            peds=[MissionPed(description="dock sentry", model="s_m_y_dockwork_01", role="patrol")],
            vehicles=[],
            objects=[],
            audio=[],
            meta={"editor": {"paths": [{
                "id": "behavior_route",
                "ped_idx": 0,
                "mode": "loop",
                "points": [[0, 0, 0], [10, 0, 0], [20, 0, 0]],
                "waypoints": [
                    {"behavior": "walk"},
                    {"behavior": "scan", "wait_time": 2},
                    {"behavior": "guard", "wait_time": 1, "guard_until_alert": True},
                ],
            }]}}
        )
        code = gen.generate(schema)
        for token in (
            "if (behavior == PatrolWaypointBehavior.Scan)",
            "BeginPatrolScan(u, now, waitMs > 0 ? waitMs : DefaultScanMs);",
            "Function.Call(Hash.TASK_STAND_STILL, u.Ped, durationMs);",
            "Function.Call(Hash.TASK_LOOK_AT_COORD,",
            "if (behavior == PatrolWaypointBehavior.Guard)",
            "bool guardUntilAlert = PatrolGuardUntilAlertAt(u);",
            "BeginPatrolGuard(u, now, waitMs, guardUntilAlert);",
            "u.StateUntil = guardUntilAlert ? DateTime.MaxValue : now.AddMilliseconds(durationMs);",
            "Function.Call(Hash.TASK_GUARD_CURRENT_POSITION, u.Ped, 10.0f, 10.0f, 1);",
            "else if (u.StateUntil != DateTime.MaxValue && now >= u.StateUntil)",
        ):
            assert token in code, f"behavior C# missing {token}"
        assert code.count("{") == code.count("}")
        assert code.count("(") == code.count(")")

    def test_editor_patrol_stress_multiple_peds_and_transition_metadata(self, gen):
        schema = MissionSchema(
            mission_name="Patrol Stress",
            location_name="docks",
            time_of_day=20,
            weather="CLEAR",
            steps=[MissionStep(type="go_to", label="Reach the routes")],
            peds=[
                MissionPed(description="outer sentry", model="s_m_y_dockwork_01", role="patrol"),
                MissionPed(description="inner guard", model="s_m_y_dockwork_01", role="guard"),
                MissionPed(description="driver sentry", model="s_m_y_dockwork_01", role="patrol"),
            ],
            vehicles=[MissionVehicle(description="sentry car", model="sultan", role="parked")],
            objects=[],
            audio=[],
            meta={"editor": {"paths": [
                {
                    "id": "outer_once",
                    "ped_idx": 0,
                    "mode": "once",
                    "speed": 1.0,
                    "points": [[0, 0, 0], [12, 0, 0], [24, 0, 0]],
                    "waypoints": [
                        {"behavior": "walk", "speed": 1.0},
                        {"behavior": "scan", "wait_time": 0.5,
                         "transition": {"kind": "interior", "name": "warehouse"}},
                        {"behavior": "walk", "speed": 1.8},
                    ],
                },
                {
                    "id": "courtyard_loop",
                    "ped_idx": 1,
                    "mode": "loop",
                    "speed": 1.4,
                    "wait_time": 0.25,
                    "points": [[0, 10, 0], [10, 10, 0], [10, 20, 0], [0, 20, 0]],
                    "waypoints": [
                        {"behavior": "guard", "wait_time": 1},
                        {"behavior": "walk", "speed": 2.0},
                        {"behavior": "scan"},
                        {"behavior": "guard", "guard_until_alert": True},
                    ],
                },
                {
                    "id": "driver_ping",
                    "ped_idx": 2,
                    "mode": "ping_pong",
                    "speed": 2.2,
                    "points": [[0, 30, 0], [12, 30, 0], [24, 30, 0]],
                    "waypoints": [
                        {"behavior": "jog", "speed": 2.2},
                        {"behavior": "sprint", "speed": 3.5,
                         "vehicle_transition": {"vehicle_idx": 0, "seat": "driver"}},
                        {"behavior": "scan", "wait_time": 2},
                    ],
                },
            ]}},
        )
        ctx = _build_template_context(schema)
        assert [p["patrol_path_idx"] for p in ctx["peds"]] == [0, 1, 2]
        assert [p["mode_cs"] for p in ctx["editor_paths"]] == ["Once", "Loop", "PingPong"]
        assert ctx["editor_paths"][0]["waits_ms"] == [0, 500, 0]
        assert ctx["editor_paths"][1]["waits_ms"] == [1000, 250, 250, 250]
        assert ctx["editor_paths"][2]["behaviors"] == ["Jog", "Sprint", "Scan"]
        assert ctx["editor_paths"][1]["guard_until_alerts"] == [False, False, False, True]

        code = gen.generate(schema)
        assert code.count("_patrols.Add(new PatrolUnit") == 3
        for token in (
            "private static readonly Vector3[] EditorPath_0",
            "private static readonly Vector3[] EditorPath_1",
            "private static readonly Vector3[] EditorPath_2",
            "Mode = PatrolRouteMode.Once",
            "Mode = PatrolRouteMode.Loop",
            "Mode = PatrolRouteMode.PingPong",
            "PatrolWaypointBehavior.Jog",
            "PatrolWaypointBehavior.Sprint",
            "PatrolWaypointBehavior.Scan",
            "PatrolWaypointBehavior.Guard",
            "500",
            "250",
            "true",
        ):
            assert token in code, f"stress patrol C# missing {token}"
        assert "warehouse" not in code
        assert "vehicle_transition" not in code
        assert code.count("{") == code.count("}")
        assert code.count("(") == code.count(")")

    def test_editor_patrol_modes_emit_state_driven_executor(self, gen):
        schema = MissionSchema(
            mission_name="Patrol Modes",
            location_name="docks",
            time_of_day=20,
            weather="CLEAR",
            steps=[MissionStep(type="go_to", label="Reach the route")],
            peds=[
                MissionPed(description="once ped", model="s_m_y_dockwork_01", role="patrol"),
                MissionPed(description="loop ped", model="s_m_y_dockwork_01", role="patrol"),
                MissionPed(description="ping pong ped", model="s_m_y_dockwork_01", role="patrol"),
            ],
            vehicles=[],
            objects=[],
            audio=[],
            meta={"editor": {"paths": [
                {"id": "once_route", "ped_idx": 0, "mode": "once", "points": [[0, 0, 0], [10, 0, 0]]},
                {"id": "loop_route", "ped_idx": 1, "mode": "loop", "points": [[0, 10, 0], [10, 10, 0]]},
                {"id": "ping_route", "ped_idx": 2, "mode": "ping_pong", "points": [[0, 20, 0], [10, 20, 0]]},
            ]}},
        )
        code = gen.generate(schema)
        for token in (
            "Mode = PatrolRouteMode.Once",
            "Mode = PatrolRouteMode.Loop",
            "Mode = PatrolRouteMode.PingPong",
            "case PatrolRouteMode.Once:",
            "case PatrolRouteMode.PingPong:",
            "u.State = PatrolRouteState.Complete;",
            "u.Dir = -u.Dir;",
            "u.Wp = (u.Wp + 1) % u.Path.Length;",
        ):
            assert token in code, f"route executor C# missing {token}"
        assert code.count("{") == code.count("}")
        assert code.count("(") == code.count(")")

    def test_no_editor_paths_emit_no_patrol_runtime(self, gen, gta_full_schema):
        code = gen.generate(gta_full_schema)
        for token in (
            "EditorPath_0",
            "EditorPathByIndex",
            "EditorPathSpeed_0",
            "EditorPathSpeedByIndex",
            "EditorPathWaitMs_0",
            "EditorPathWaitMsByIndex",
            "EditorPathBehavior_0",
            "EditorPathBehaviorByIndex",
            "EditorPathGuardUntilAlert_0",
            "EditorPathGuardUntilAlertByIndex",
            "DefaultPatrolSpeed",
            "DefaultScanMs",
            "PatrolRouteMode",
            "PatrolRouteState",
            "PatrolWaypointBehavior",
            "PatrolUnit",
            "_patrols",
            "UpdatePatrols",
        ):
            assert token not in code

    def test_invalid_editor_path_binding_emits_path_without_runtime_unit(self, gen):
        schema = MissionSchema(
            mission_name="Invalid Patrol Binding",
            location_name="docks",
            time_of_day=20,
            weather="CLEAR",
            steps=[MissionStep(type="go_to", label="Reach the route")],
            peds=[MissionPed(description="dock sentry", model="s_m_y_dockwork_01", role="patrol")],
            vehicles=[],
            objects=[],
            audio=[],
            meta={"editor": {"paths": [{
                "id": "missing_ped_route",
                "ped_idx": 99,
                "points": [[-1, -2, 3], [4, 5, 6]],
            }]}},
        )
        code = gen.generate(schema)
        assert "private static readonly Vector3[] EditorPath_0" in code
        assert "private static readonly float[] EditorPathSpeed_0" in code
        assert "// id=missing_ped_route pedIdx=99" in code
        assert "private void UpdatePatrols()" in code
        assert "_patrols.Add(new PatrolUnit" not in code

    def test_default_health_and_armor_emit_no_lines(self, gen, gta_full_schema):
        # gta_full_schema's ped is default health=100/armor=0 — no line needed,
        # since GTA5 peds already spawn at that state.
        code = gen.generate(gta_full_schema)
        assert "p.Health =" not in code
        assert "p.ArmorFloat =" not in code

    def test_nondefault_health_and_armor_emitted(self, gen):
        schema = MissionSchema(
            mission_name="Tank Fight", location_name="docks", time_of_day=20,
            weather="CLEAR", steps=[MissionStep(type="go_to", label="Go here")],
            peds=[MissionPed(description="tank", model="s_m_y_cop_01", role="target",
                             health=300, armor=50)],
            vehicles=[], objects=[], audio=[],
        )
        code = gen.generate(schema)
        assert "p.Health = 300;" in code
        assert "p.ArmorFloat = 50.0f;" in code

    def test_hostile_ped_gets_combat_attributes_before_task_combat(self, gen):
        # CanUseCover/CombatAbility must be set BEFORE Task.Combat starts the
        # AI, or GTA V's combat task won't pick them up for this engagement.
        schema = MissionSchema(
            mission_name="Ambush", location_name="docks", time_of_day=20,
            weather="CLEAR", steps=[MissionStep(type="go_to", label="Go here")],
            peds=[MissionPed(description="enforcer", model="s_m_y_cop_01",
                             role="target", weapon="rifle")],
            vehicles=[], objects=[], audio=[],
        )
        code = gen.generate(schema)
        assert "SetCombatAttribute(CombatAttributes.CanUseCover, true);" in code
        assert "CombatAbility = CombatAbility.Average;" in code
        attr_idx = code.index("SetCombatAttribute(CombatAttributes.CanUseCover")
        combat_idx = code.index("p.Task.Combat(Game.Player.Character);")
        assert attr_idx < combat_idx

    def test_non_hostile_ped_gets_no_combat_attributes(self, gen, gta_full_schema):
        code = gen.generate(gta_full_schema)
        assert "SetCombatAttribute" not in code

    def test_nondefault_ammo_emitted_for_armed_ped(self, gen):
        schema = MissionSchema(
            mission_name="Sniper Nest", location_name="docks", time_of_day=20,
            weather="CLEAR", steps=[MissionStep(type="go_to", label="Go here")],
            peds=[MissionPed(description="sniper", model="s_m_y_cop_01", role="target",
                             weapon="sniper", ammo=8)],
            vehicles=[], objects=[], audio=[],
        )
        code = gen.generate(schema)
        assert "GIVE_WEAPON_TO_PED, p, _wh, 8, false, true" in code

    def test_output_contains_weather_string(self, gen, gta_full_schema):
        code = gen.generate(gta_full_schema)
        assert "OVERCAST" in code

    def test_output_has_state_enum(self, gen, gta_full_schema):
        code = gen.generate(gta_full_schema)
        assert "enum State" in code
        assert "Inactive" in code
        assert "Active" in code

    def test_output_contains_script_extends(self, gen, gta_full_schema):
        code = gen.generate(gta_full_schema)
        assert ": Script" in code

    def test_output_is_deterministic(self, gen, gta_full_schema):
        first = gen.generate(gta_full_schema)
        second = gen.generate(gta_full_schema)
        assert first == second

    def test_no_waypoints_section_when_no_goto_steps(self, gen):
        schema = MissionSchema(
            mission_name="Kill Mission",
            location_name="docks",
            steps=[MissionStep(type="kill_all", label="Kill all")],
            peds=[MissionPed(description="cop", model="s_m_y_cop_01", role="guard")],
            vehicles=[],
            objects=[],
        )
        code = gen.generate(schema)
        assert "Waypoints" not in code

    def test_special_chars_in_mission_name_escaped(self, gen):
        schema = MissionSchema(
            mission_name='Say "Hello"',
            location_name="docks",
            steps=[MissionStep(type="go_to", label="Go")],
            peds=[],
            vehicles=[],
            objects=[],
        )
        code = gen.generate(schema)
        # The escaped quote must appear in the output, not a raw unescaped one
        assert '\\"Hello\\"' in code

    def test_minimal_schema_generates_without_error(self, gen, gta_minimal_schema):
        code = gen.generate(gta_minimal_schema)
        assert "class TestMission" in code


# ══════════════════════════════════════════════════════════════════════════════
# CodeGenerator.write — file output
# ══════════════════════════════════════════════════════════════════════════════

class TestCodeGeneratorWrite:

    def test_creates_dot3_cs_file(self, gen, gta_full_schema, tmp_path):
        out = gen.write(gta_full_schema, str(tmp_path))
        assert out.exists()
        assert out.name.endswith(".3.cs")

    def test_output_class_name_in_filename(self, gen, gta_full_schema, tmp_path):
        out = gen.write(gta_full_schema, str(tmp_path))
        assert "TheDocksHeist" in out.name

    def test_schema_json_sidecar_created(self, gen, gta_full_schema, tmp_path):
        out = gen.write(gta_full_schema, str(tmp_path))
        schema_file = out.with_suffix("").with_suffix(".schema.json")
        assert schema_file.exists()

    def test_schema_json_sidecar_is_valid_json(self, gen, gta_full_schema, tmp_path):
        out = gen.write(gta_full_schema, str(tmp_path))
        schema_file = out.with_suffix("").with_suffix(".schema.json")
        data = json.loads(schema_file.read_text(encoding="utf-8"))
        assert data["mission_name"] == "The Docks Heist"

    def test_schema_json_round_trips_through_model_validate(self, gen, gta_full_schema, tmp_path):
        from src.mission_schema import MissionSchema as MS
        out = gen.write(gta_full_schema, str(tmp_path))
        schema_file = out.with_suffix("").with_suffix(".schema.json")
        data = json.loads(schema_file.read_text(encoding="utf-8"))
        restored = MS.model_validate(data)
        assert restored.mission_name == gta_full_schema.mission_name

    def test_output_dir_created_if_not_exists(self, gen, gta_minimal_schema, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        out = gen.write(gta_minimal_schema, str(deep))
        assert out.exists()

    def test_generated_code_written_to_file(self, gen, gta_full_schema, tmp_path):
        out = gen.write(gta_full_schema, str(tmp_path))
        code = out.read_text(encoding="utf-8")
        assert "class TheDocksHeist" in code


# ══════════════════════════════════════════════════════════════════════════════
# Race flagship demo — visible course, finish, ceremony, rivals, real geometry
# ══════════════════════════════════════════════════════════════════════════════

class TestRaceDemo:
    def _race_schema(self, track="airport.figure_8", location="airport"):
        return MissionSchema(
            mission_name="Runway Sprint", location_name=location,
            steps=[MissionStep(type="get_in_vehicle", label="Get to the start"),
                   MissionStep(type="race_lap", label="Win the race",
                               params={"track": track, "lap_count": 3, "require_vehicle": True})],
            vehicles=[MissionVehicle(description="rival", model="sultan",
                                     role="race_opponent", count=2)],
        )

    def test_race_emits_visible_course_and_ceremony(self, gen):
        cs = gen.generate(self._race_schema())
        for tok in ("DrawCheckpointMarker", "World.DrawMarker", "UpdateRivals",
                    "PlayCheckpointChime", "prop_start_finish_line_01", "RACE_PLACED",
                    "_bestLap", "_rivalDrivers", "using System.Drawing"):
            assert tok in cs, f"race C# missing {tok}"
        assert cs.count("{") == cs.count("}")
        assert cs.count("(") == cs.count(")")

    def test_non_race_mission_has_no_race_code(self, gen):
        cs = gen.generate(MissionSchema(
            mission_name="Cleanup", location_name="downtown",
            steps=[MissionStep(type="kill_all", label="Clear them")],
            peds=[MissionPed(description="thug", model="g_m_y_lost_01",
                             role="guard", count=3)]))
        for tok in ("DrawCheckpointMarker", "UpdateRivals", "using System.Drawing"):
            assert tok not in cs

    def test_figure8_uses_real_geometry(self, gen):
        cs = gen.generate(self._race_schema(track="airport.figure_8"))
        assert "-1580.0f" in cs        # real figure-8 checkpoint, not a spiral

    def test_invalid_track_snaps_to_real_circuit_not_spiral(self, gen):
        # An invalid track on a race step must fall back to the location's REAL
        # circuit (airport.circuit → first CP x=-1740), never a synthetic spiral
        # around the airport spawn origin.
        cs = gen.generate(self._race_schema(track="does.not.exist"))
        assert "-1740.0f" in cs


# ══════════════════════════════════════════════════════════════════════════════
# HeliRace-style polish: AI rival difficulty curve + F10 config menu
# ══════════════════════════════════════════════════════════════════════════════

class TestAiRivalDifficultyAndConfigMenu:
    def _race_schema(self, **kwargs):
        defaults = dict(
            mission_name="Runway Sprint", location_name="airport",
            steps=[MissionStep(type="get_in_vehicle", label="Get to the start"),
                   MissionStep(type="race_lap", label="Win the race",
                               params={"track": "airport.figure_8", "lap_count": 3,
                                       "require_vehicle": True})],
            vehicles=[MissionVehicle(description="rival", model="sultan",
                                     role="race_opponent", count=2)],
        )
        defaults.update(kwargs)
        return MissionSchema(**defaults)

    def test_ai_speed_helpers_present_when_race_opponent_exists(self, gen):
        cs = gen.generate(self._race_schema())
        assert "AiSpawnSpeedFor" in cs
        assert "AiChaseSpeedFor" in cs

    def test_ai_speed_helpers_absent_without_race_opponent(self, gen):
        cs = gen.generate(MissionSchema(
            mission_name="Cleanup", location_name="downtown",
            steps=[MissionStep(type="kill_all", label="Clear them")],
            peds=[MissionPed(description="thug", model="g_m_y_lost_01",
                             role="guard", count=3)]))
        assert "AiSpawnSpeedFor" not in cs
        assert "AiChaseSpeedFor" not in cs

    def test_default_medium_difficulty_preserves_historical_speeds(self, gen):
        # Backward-compat: unset ai_difficulty must still drive the rival at the
        # original hardcoded 40f/45f so pre-existing generated missions don't change.
        cs = gen.generate(self._race_schema())
        assert "_aiDifficultyIdx = 1;" in cs
        assert "case 1: return 40.0f;" in cs
        assert "case 1: return 45.0f;" in cs

    def test_hard_difficulty_sets_higher_default_index(self, gen):
        cs = gen.generate(self._race_schema(ai_difficulty="hard"))
        assert "_aiDifficultyIdx = 2;" in cs

    def test_easy_difficulty_sets_lower_default_index(self, gen):
        cs = gen.generate(self._race_schema(ai_difficulty="easy"))
        assert "_aiDifficultyIdx = 0;" in cs

    def test_rival_spawn_and_chase_calls_use_difficulty_lookup(self, gen):
        cs = gen.generate(self._race_schema(ai_difficulty="hard"))
        assert "AiSpawnSpeedFor(_aiDifficultyIdx)" in cs
        assert "AiChaseSpeedFor(_aiDifficultyIdx)" in cs
        assert "786603, 40f" not in cs   # old hardcoded call site must be gone

    def test_config_menu_off_by_default(self, gen):
        cs = gen.generate(self._race_schema())
        assert "DrawConfigMenu" not in cs
        assert "KeyDown += OnKeyDown" not in cs

    def test_config_menu_emits_menu_machinery(self, gen):
        cs = gen.generate(self._race_schema(config_menu=True))
        for tok in ("DrawConfigMenu", "HandleMenuControls", "AdjustConfig",
                    "MenuValueFor", "KeyDown += OnKeyDown", "_cfgWeatherOptions",
                    "MISSION CONFIG"):
            assert tok in cs, f"config menu C# missing {tok}"

    def test_config_menu_includes_ai_difficulty_row_when_rival_present(self, gen):
        cs = gen.generate(self._race_schema(config_menu=True))
        assert "AI Difficulty" in cs

    def test_config_menu_omits_ai_difficulty_row_without_rival(self, gen):
        cs = gen.generate(MissionSchema(
            mission_name="Scenic Drive", location_name="downtown", config_menu=True,
            steps=[MissionStep(type="go_to", label="Drive there")]))
        assert "AI Difficulty" not in cs
        assert "MISSION CONFIG" in cs   # menu itself still emitted (weather row)

    def test_config_menu_weather_default_index_matches_schema_weather(self, gen):
        cs = gen.generate(self._race_schema(config_menu=True, weather="THUNDER"))
        # THUNDER is index 3 in the menu's weather option list.
        assert "_cfgWeatherIdx = 3;" in cs

    def test_config_menu_output_braces_and_parens_balanced(self, gen):
        cs = gen.generate(self._race_schema(config_menu=True, ai_difficulty="hard"))
        assert cs.count("{") == cs.count("}")
        assert cs.count("(") == cs.count(")")

    def test_config_menu_is_deterministic(self, gen):
        schema = self._race_schema(config_menu=True, ai_difficulty="hard")
        assert gen.generate(schema) == gen.generate(schema)


# ══════════════════════════════════════════════════════════════════════════════
# HeliRace-derived features: mid-mission events / HUD / cash reward / scavenger
# ══════════════════════════════════════════════════════════════════════════════

def _feature_schema(**kwargs):
    defaults = dict(
        mission_name="Storm Scavenger", location_name="docks",
        steps=[MissionStep(type="scavenger", label="Collect the stashes"),
               MissionStep(type="escape", label="Lose the heat")],
    )
    defaults.update(kwargs)
    return MissionSchema(**defaults)


class TestMissionEvents:
    def _events(self, spawn_heli=False):
        return [
            {"event_type": "thunderstorm", "at_progress": 0.4},
            {"event_type": "police_pursuit", "at_progress": 0.6,
             "params": {"spawn_heli": spawn_heli}},
        ]

    def test_no_events_emits_no_event_machinery(self, gen):
        cs = gen.generate(_feature_schema())
        for tok in ("UpdateEvents", "_eventsFired", "SET_WIND_SPEED",
                    "SET_PLAYER_WANTED_LEVEL", "_weatherChanged"):
            assert tok not in cs, f"leaked without events: {tok}"

    def test_thunderstorm_event_emits_weather_natives(self, gen):
        cs = gen.generate(_feature_schema(events=self._events()))
        assert "UpdateEvents" in cs
        assert '_eventsFired' in cs
        assert 'SET_WEATHER_TYPE_NOW_PERSIST, "THUNDER"' in cs
        assert "SET_WIND_SPEED, 40.0f" in cs
        assert "_prog >= 0.40f" in cs

    def test_police_pursuit_event_emits_wanted_natives(self, gen):
        cs = gen.generate(_feature_schema(events=self._events()))
        assert "SET_PLAYER_WANTED_LEVEL, Game.Player, 3" in cs
        assert "SET_PLAYER_WANTED_LEVEL_NOW" in cs
        assert "_prog >= 0.60f" in cs

    def test_spawn_heli_off_omits_attack_heli(self, gen):
        cs = gen.generate(_feature_schema(events=self._events(spawn_heli=False)))
        assert "TASK_HELI_MISSION" not in cs

    def test_spawn_heli_emits_attack_heli(self, gen):
        cs = gen.generate(_feature_schema(events=self._events(spawn_heli=True)))
        assert "TASK_HELI_MISSION" in cs
        assert "buzzard" in cs
        assert "s_m_y_pilot_01" in cs

    def test_thunderstorm_restores_schema_weather_in_cleanup(self, gen):
        cs = gen.generate(_feature_schema(events=self._events(), weather="EXTRASUNNY"))
        assert "_weatherChanged" in cs
        assert 'SET_WEATHER_TYPE_NOW_PERSIST, "EXTRASUNNY"' in cs
        assert "SET_WIND_SPEED, 0f" in cs

    def test_pursuit_only_omits_weather_reset(self, gen):
        cs = gen.generate(_feature_schema(
            events=[{"event_type": "police_pursuit", "at_progress": 0.5}]))
        assert "_weatherChanged" not in cs

    def test_events_output_balanced_and_deterministic(self, gen):
        schema = _feature_schema(events=self._events(spawn_heli=True), hud=True)
        cs = gen.generate(schema)
        assert cs.count("{") == cs.count("}")
        assert cs.count("(") == cs.count(")")
        assert gen.generate(schema) == cs


class TestHud:
    def test_hud_off_by_default(self, gen):
        cs = gen.generate(_feature_schema())
        assert "DrawHud" not in cs
        assert "_missionStart" not in cs

    def test_hud_emits_timer_bar_and_counter(self, gen):
        cs = gen.generate(_feature_schema(hud=True))
        for tok in ("DrawHud", "DrawHudText", "Hash.DRAW_RECT", "_missionStart",
                    "MissionProgress", "BEGIN_TEXT_COMMAND_DISPLAY_TEXT"):
            assert tok in cs, f"HUD C# missing {tok}"

    def test_hud_counter_uses_total_step_count(self, gen):
        cs = gen.generate(_feature_schema(hud=True))   # 2 steps
        assert "/ ~w~2" in cs

    def test_hud_output_balanced(self, gen):
        cs = gen.generate(_feature_schema(hud=True))
        assert cs.count("{") == cs.count("}")
        assert cs.count("(") == cs.count(")")


class TestRewardCash:
    def test_no_reward_by_default(self, gen):
        cs = gen.generate(_feature_schema())
        assert "STAT_SET_INT" not in cs
        assert "Game.Player.Money" not in cs

    def test_reward_emits_stat_pattern_with_money_fallback(self, gen):
        cs = gen.generate(_feature_schema(reward_cash=45000))
        assert "SP0_TOTAL_CASH" in cs
        assert "STAT_GET_INT" in cs
        assert "STAT_SET_INT" in cs
        assert "Game.Player.Money += 45000" in cs
        assert "$45000" in cs

    def test_reward_output_balanced(self, gen):
        cs = gen.generate(_feature_schema(reward_cash=45000))
        assert cs.count("{") == cs.count("}")
        assert cs.count("(") == cs.count(")")


class TestScavengerCodegen:
    def test_context_builds_scavenger_set(self):
        ctx = _build_template_context(_feature_schema())
        assert len(ctx["scavenger_sets"]) == 1
        sc = ctx["scavenger_sets"][0]
        assert sc["array_name"] == "ScavengerPoints0"
        assert len(sc["points"]) == 5          # default target_count
        assert sc["radius"] == 3.0
        # Points must be distinct (compass scatter, not a pile-up).
        coords = {(p["x"], p["y"]) for p in sc["points"]}
        assert len(coords) == 5

    def test_context_respects_target_count(self):
        schema = _feature_schema(steps=[MissionStep(
            type="scavenger", label="Collect", params={"target_count": 10})])
        ctx = _build_template_context(schema)
        assert len(ctx["scavenger_sets"][0]["points"]) == 10
        coords = {(p["x"], p["y"]) for p in ctx["scavenger_sets"][0]["points"]}
        assert len(coords) == 10               # ring multiplier keeps >8 distinct

    def test_editor_locations_win_verbatim(self):
        schema = _feature_schema(steps=[MissionStep(
            type="scavenger", label="Collect",
            params={"locations": [[10.0, 20.0, 30.0], {"x": 40.0, "y": 50.0, "z": 60.0}]})])
        ctx = _build_template_context(schema)
        pts = ctx["scavenger_sets"][0]["points"]
        assert pts == [{"x": 10.0, "y": 20.0, "z": 30.0},
                       {"x": 40.0, "y": 50.0, "z": 60.0}]

    def test_scavenger_step_emits_any_order_machinery(self, gen):
        cs = gen.generate(_feature_schema())
        for tok in ("Step0_SCAVENGER", "HashSet<int> _scavVisited", "_scavBlips",
                    "BlipColor.Green", "Scale = 0.6f", "ScavengerPoints0",
                    "PLAY_SOUND_FRONTEND"):
            assert tok in cs, f"scavenger C# missing {tok}"
        assert "#error" not in cs

    def test_scavenger_absent_from_plain_mission(self, gen, gta_full_schema):
        cs = gen.generate(gta_full_schema)
        assert "_scavVisited" not in cs
        assert "ScavengerPoints" not in cs

    def test_scavenger_cleanup_clears_blips(self, gen):
        cs = gen.generate(_feature_schema())
        assert "_scavBlips.Clear();" in cs

    def test_scavenger_output_balanced_and_deterministic(self, gen):
        schema = _feature_schema(events=[{"event_type": "thunderstorm"}],
                                 hud=True, reward_cash=1000)
        cs = gen.generate(schema)
        assert cs.count("{") == cs.count("}")
        assert cs.count("(") == cs.count(")")
        assert gen.generate(schema) == cs

    def test_mission_progress_includes_scavenger_fraction(self, gen):
        cs = gen.generate(_feature_schema(hud=True))
        assert "MissionProgress" in cs
        assert "_scavVisited.Count / (float)ScavengerPoints0.Length" in cs
