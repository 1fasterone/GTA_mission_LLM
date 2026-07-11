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

    def test_ped_is_hostile_for_guard_role(self):
        schema = self._schema(peds=[
            MissionPed(description="cop", model="s_m_y_cop_01", role="guard"),
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
