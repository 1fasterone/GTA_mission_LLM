"""
End-to-end tests for MissionPipeline.

Strategy
────────
• LMStudioClient.chat is mocked at the instance level (side_effect list).
  Call 1 → concept story text
  Call 2 → schema JSON string

• AnythingLLMClient.resolve_model is mocked to return None, forcing the
  local lookup fallback.  All schema descriptions use synonyms present in
  the local lookup tables so every model resolves without a real RAG server.

• CodeGenerator.write uses tmp_path (from gta_pipeline_config fixture).

These tests exercise:
  – schema parsing and validation (Steps 2-3)
  – location sanity check (Step 3)
  – model resolution via local lookups (Step 4)
  – consistency enforcement (Step 4b)
  – programmatic fixes (Step 5)
  – C# file generation (Step 6)
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.pipeline import MissionPipeline, _programmatic_fix, _llm_pick_model
from src.mission_schema import MissionObject, MissionPed, MissionSchema, MissionStep, MissionVehicle


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_pipeline(config: dict) -> MissionPipeline:
    """Instantiate pipeline; silence the RAG and LLM network calls."""
    pipeline = MissionPipeline(config)
    # Disable real network calls: RAG always returns None → local fallback
    pipeline.rag.resolve_model = MagicMock(return_value=None)
    pipeline.rag.resolve_audio = MagicMock(return_value=None)
    return pipeline


def _run_pipeline(pipeline: MissionPipeline, concept_text: str, schema_json: str) -> Path:
    """Run pipeline with mocked LLM returning concept then schema."""
    pipeline.llm.chat = MagicMock(side_effect=[concept_text, schema_json])
    return pipeline.run("A heist at the docks")


# ══════════════════════════════════════════════════════════════════════════════
# Happy-path: full run produces a valid .3.cs file
# ══════════════════════════════════════════════════════════════════════════════

class TestPipelineHappyPath:
    def test_run_returns_path_that_exists(
        self, gta_pipeline_config, gta_concept_text, gta_schema_json
    ):
        pipeline = _make_pipeline(gta_pipeline_config)
        out = _run_pipeline(pipeline, gta_concept_text, gta_schema_json)
        assert out.exists()

    def test_output_has_dot3_cs_extension(
        self, gta_pipeline_config, gta_concept_text, gta_schema_json
    ):
        pipeline = _make_pipeline(gta_pipeline_config)
        out = _run_pipeline(pipeline, gta_concept_text, gta_schema_json)
        assert out.name.endswith(".3.cs")

    def test_output_class_name_derived_from_mission_name(
        self, gta_pipeline_config, gta_concept_text, gta_schema_json
    ):
        pipeline = _make_pipeline(gta_pipeline_config)
        out = _run_pipeline(pipeline, gta_concept_text, gta_schema_json)
        assert "TheDocksHeist" in out.name

    def test_generated_code_contains_class_definition(
        self, gta_pipeline_config, gta_concept_text, gta_schema_json
    ):
        pipeline = _make_pipeline(gta_pipeline_config)
        out = _run_pipeline(pipeline, gta_concept_text, gta_schema_json)
        code = out.read_text(encoding="utf-8")
        assert "class TheDocksHeist" in code

    def test_generated_code_contains_spawn_origin(
        self, gta_pipeline_config, gta_concept_text, gta_schema_json
    ):
        pipeline = _make_pipeline(gta_pipeline_config)
        out = _run_pipeline(pipeline, gta_concept_text, gta_schema_json)
        code = out.read_text(encoding="utf-8")
        # Docks x coordinate
        assert "-1009.0f" in code

    def test_generated_code_contains_ped_model(
        self, gta_pipeline_config, gta_concept_text, gta_schema_json
    ):
        pipeline = _make_pipeline(gta_pipeline_config)
        out = _run_pipeline(pipeline, gta_concept_text, gta_schema_json)
        code = out.read_text(encoding="utf-8")
        # "cop" synonym → s_m_y_cop_01
        assert "s_m_y_cop_01" in code

    def test_generated_code_contains_vehicle_model(
        self, gta_pipeline_config, gta_concept_text, gta_schema_json
    ):
        pipeline = _make_pipeline(gta_pipeline_config)
        out = _run_pipeline(pipeline, gta_concept_text, gta_schema_json)
        code = out.read_text(encoding="utf-8")
        # "police car" synonym → police
        assert '"police"' in code

    def test_generated_code_contains_object_model(
        self, gta_pipeline_config, gta_concept_text, gta_schema_json
    ):
        pipeline = _make_pipeline(gta_pipeline_config)
        out = _run_pipeline(pipeline, gta_concept_text, gta_schema_json)
        code = out.read_text(encoding="utf-8")
        assert "prop_drug_package_01" in code

    def test_schema_json_sidecar_created(
        self, gta_pipeline_config, gta_concept_text, gta_schema_json
    ):
        pipeline = _make_pipeline(gta_pipeline_config)
        out = _run_pipeline(pipeline, gta_concept_text, gta_schema_json)
        sidecar = out.with_suffix("").with_suffix(".schema.json")
        assert sidecar.exists()

    def test_schema_sidecar_round_trips(
        self, gta_pipeline_config, gta_concept_text, gta_schema_json
    ):
        pipeline = _make_pipeline(gta_pipeline_config)
        out = _run_pipeline(pipeline, gta_concept_text, gta_schema_json)
        sidecar = out.with_suffix("").with_suffix(".schema.json")
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        restored = MissionSchema.model_validate(data)
        assert restored.mission_name == "The Docks Heist"

    def test_llm_called_exactly_twice(
        self, gta_pipeline_config, gta_concept_text, gta_schema_json
    ):
        """Pipeline must call LLM exactly twice: concept + schema fill."""
        pipeline = _make_pipeline(gta_pipeline_config)
        mock_chat = MagicMock(side_effect=[gta_concept_text, gta_schema_json])
        pipeline.llm.chat = mock_chat
        pipeline.run("A heist at the docks")
        assert mock_chat.call_count == 2


# ══════════════════════════════════════════════════════════════════════════════
# Location sanity check (Step 3 / FIX 3)
# ══════════════════════════════════════════════════════════════════════════════

class TestLocationSanityCheck:
    def test_location_corrected_when_concept_signals_airport(
        self, gta_pipeline_config
    ):
        """If the concept mentions 'runway' and 'hangar' but schema says 'docks',
        the pipeline must auto-correct to 'airport'."""
        concept = "The player must infiltrate the airport runway near the hangar."
        schema_json = json.dumps({
            "mission_name":  "Airport Run",
            "location_name": "docks",   # wrong — concept says airport
            "time_of_day":   14,
            "weather":       "CLEAR",
            "steps":         [{"type": "go_to", "label": "Go to airport"}],
            "peds":          [],
            "vehicles":      [],
            "objects":       [],
            "audio":         [],
        })
        pipeline = _make_pipeline(gta_pipeline_config)
        pipeline.llm.chat = MagicMock(side_effect=[concept, schema_json])
        out = pipeline.run("infiltrate the airport")
        code = out.read_text(encoding="utf-8")
        # Airport x = -1336.0
        assert "-1336.0f" in code

    def test_location_unchanged_when_concept_matches_schema(
        self, gta_pipeline_config, gta_concept_text, gta_schema_json
    ):
        """Concept mentions 'docks' → schema 'docks' → no correction."""
        pipeline = _make_pipeline(gta_pipeline_config)
        out = _run_pipeline(pipeline, gta_concept_text, gta_schema_json)
        code = out.read_text(encoding="utf-8")
        assert "-1009.0f" in code   # docks coords unchanged


# ══════════════════════════════════════════════════════════════════════════════
# Step / entity consistency enforcement (FIX 2)
# ══════════════════════════════════════════════════════════════════════════════

class TestConsistencyChecks:
    def test_pickup_step_without_pickup_prop_auto_adds_prop(
        self, gta_pipeline_config
    ):
        """PICKUP step but no pickup prop → pipeline injects prop_drug_package_01."""
        schema_json = json.dumps({
            "mission_name":  "Package Run",
            "location_name": "docks",
            "time_of_day":   12,
            "weather":       "CLEAR",
            "steps":         [
                {"type": "go_to",  "label": "Go"},
                {"type": "pickup", "label": "Grab"},
            ],
            "peds":     [],
            "vehicles": [],
            "objects":  [],   # <-- no pickup prop
            "audio":    [],
        })
        pipeline = _make_pipeline(gta_pipeline_config)
        pipeline.llm.chat = MagicMock(side_effect=["story", schema_json])
        out = pipeline.run("grab a package")
        code = out.read_text(encoding="utf-8")
        assert "prop_drug_package_01" in code

    def test_get_in_vehicle_without_vehicle_auto_adds_burrito(
        self, gta_pipeline_config
    ):
        """GET_IN_VEHICLE step but no vehicles → pipeline injects a burrito."""
        schema_json = json.dumps({
            "mission_name":  "Car Mission",
            "location_name": "docks",
            "time_of_day":   12,
            "weather":       "CLEAR",
            "steps":         [{"type": "get_in_vehicle", "label": "Get in"}],
            "peds":          [],
            "vehicles":      [],   # <-- no vehicle
            "objects":       [],
            "audio":         [],
        })
        pipeline = _make_pipeline(gta_pipeline_config)
        pipeline.llm.chat = MagicMock(side_effect=["story", schema_json])
        out = pipeline.run("get in a car")
        code = out.read_text(encoding="utf-8")
        assert "burrito" in code

    def test_deliver_without_goto_inserts_goto(
        self, gta_pipeline_config
    ):
        """DELIVER with no preceding go_to → pipeline inserts a go_to before it."""
        schema_json = json.dumps({
            "mission_name":  "Deliver Run",
            "location_name": "docks",
            "time_of_day":   12,
            "weather":       "CLEAR",
            "steps":         [{"type": "deliver", "label": "Drop off"}],
            "peds":          [],
            "vehicles":      [],
            "objects":       [],
            "audio":         [],
        })
        pipeline = _make_pipeline(gta_pipeline_config)
        pipeline.llm.chat = MagicMock(side_effect=["story", schema_json])
        out = pipeline.run("deliver goods")
        # A go_to step was injected → there should be a waypoint in the output
        code = out.read_text(encoding="utf-8")
        assert "Waypoints" in code


# ══════════════════════════════════════════════════════════════════════════════
# Programmatic fixer (_programmatic_fix)
# ══════════════════════════════════════════════════════════════════════════════

class TestProgrammaticFix:
    def _schema_with(self, **kwargs) -> MissionSchema:
        defaults = dict(
            mission_name="X",
            location_name="docks",
            steps=[MissionStep(type="go_to", label="Go")],
            peds=[],
            vehicles=[],
            objects=[],
        )
        defaults.update(kwargs)
        return MissionSchema(**defaults)

    def test_weather_uppercased(self):
        schema = self._schema_with(weather="rain")  # validator normalises; fixer re-applies
        fixed = _programmatic_fix(schema)
        assert fixed.weather == fixed.weather.upper()

    def test_ped_count_clamped_to_minimum_1(self):
        """_programmatic_fix clamps count to at least 1."""
        schema = self._schema_with(peds=[
            MissionPed(description="cop", model="s_m_y_cop_01", role="guard", count=1),
        ])
        # Manually force count below 1 (bypass validator for white-box test)
        schema.peds[0].count = 0
        fixed = _programmatic_fix(schema)
        assert fixed.peds[0].count >= 1

    def test_ped_count_clamped_to_maximum_8(self):
        schema = self._schema_with(peds=[
            MissionPed(description="cop", model="s_m_y_cop_01", role="guard", count=3),
        ])
        schema.peds[0].count = 99
        fixed = _programmatic_fix(schema)
        assert fixed.peds[0].count <= 8

    def test_vehicle_count_clamped_to_maximum_4(self):
        schema = self._schema_with(vehicles=[
            MissionVehicle(description="van", model="burrito3", role="escape", count=1),
        ])
        schema.vehicles[0].count = 10
        fixed = _programmatic_fix(schema)
        assert fixed.vehicles[0].count <= 4

    def test_ped_description_whitespace_stripped(self):
        schema = self._schema_with(peds=[
            MissionPed(description="  armed cop  ", model="s_m_y_cop_01", role="guard"),
        ])
        fixed = _programmatic_fix(schema)
        assert fixed.peds[0].description == "armed cop"


# ══════════════════════════════════════════════════════════════════════════════
# LLM-pick fallback for UNRESOLVED models
# ══════════════════════════════════════════════════════════════════════════════

class TestLlmPickFallback:
    def test_llm_called_for_unresolved_ped_when_triggered(
        self, gta_pipeline_config
    ):
        """If a ped is still UNRESOLVED after RAG+local, pipeline calls llm.chat
        a third time (the LLM picker) and uses its result."""
        schema_with_unresolvable = json.dumps({
            "mission_name":  "Alien Mission",
            "location_name": "docks",
            "time_of_day":   12,
            "weather":       "CLEAR",
            "steps":         [{"type": "go_to", "label": "Go"}],
            "peds": [{"description": "xyzzy_martian_alien_9999", "role": "guard", "count": 1, "weapon": "none"}],
            "vehicles":      [],
            "objects":       [],
            "audio":         [],
        })
        pipeline = _make_pipeline(gta_pipeline_config)
        # Third call (LLM picker) returns a valid-looking model ID
        pipeline.llm.chat = MagicMock(
            side_effect=["concept story", schema_with_unresolvable, "s_m_y_cop_01"]
        )
        out = pipeline.run("alien mission")
        code = out.read_text(encoding="utf-8")
        # LLM pick should have resolved to s_m_y_cop_01
        assert "s_m_y_cop_01" in code

    def test_pipeline_still_generates_when_model_stays_unresolved(
        self, gta_pipeline_config
    ):
        """Even if a model stays UNRESOLVED, the pipeline should not crash —
        it should emit a warning and generate code that skips that spawn."""
        schema_json = json.dumps({
            "mission_name":  "Broken Mission",
            "location_name": "docks",
            "time_of_day":   12,
            "weather":       "CLEAR",
            "steps":         [{"type": "go_to", "label": "Go"}],
            "peds": [{"description": "xyzzy_alien", "role": "guard", "count": 1, "weapon": "none"}],
            "vehicles": [],
            "objects":  [],
            "audio":    [],
        })
        pipeline = _make_pipeline(gta_pipeline_config)
        # LLM picker returns garbage → UNRESOLVED persists
        pipeline.llm.chat = MagicMock(
            side_effect=["story", schema_json, "!!!not_a_valid_id!!!"]
        )
        # Must not raise
        out = pipeline.run("broken")
        assert out.exists()


# ══════════════════════════════════════════════════════════════════════════════
# Schema fed with JSON embedded in markdown fence (extract_json path)
# ══════════════════════════════════════════════════════════════════════════════

class TestSchemaExtractionFromFence:
    def test_schema_in_markdown_fence_is_parsed(
        self, gta_pipeline_config, gta_concept_text, gta_schema_json
    ):
        """LLM wraps schema in ```json ... ``` — pipeline must still parse it."""
        fenced = f"```json\n{gta_schema_json}\n```"
        pipeline = _make_pipeline(gta_pipeline_config)
        pipeline.llm.chat = MagicMock(side_effect=[gta_concept_text, fenced])
        out = pipeline.run("heist")
        assert out.exists()
        assert "TheDocksHeist" in out.name

    def test_schema_after_think_block_is_parsed(
        self, gta_pipeline_config, gta_concept_text, gta_schema_json
    ):
        """LLM includes <think>...</think> before JSON — must be stripped."""
        with_think = f"<think>Reasoning...</think>\n{gta_schema_json}"
        pipeline = _make_pipeline(gta_pipeline_config)
        pipeline.llm.chat = MagicMock(side_effect=[gta_concept_text, with_think])
        out = pipeline.run("heist")
        assert out.exists()


# ══════════════════════════════════════════════════════════════════════════════
# End-to-end: HeliRace-style AI rival difficulty + F10 config menu
#
# Proves the full wire-up (race archetype hint in archetypes.py → SCHEMA_FILL_USER
# output keys in prompts.py → MissionSchema fields → CodeGenerator) survives a
# realistic race-flavored prompt/concept/schema all the way to compiled C#,
# without needing a live LM Studio instance.
# ══════════════════════════════════════════════════════════════════════════════

class TestPipelineAiDifficultyAndConfigMenu:
    def _race_concept_text(self) -> str:
        return (
            "A high-stakes illegal street race at Los Santos International Airport. "
            "The player challenges Vice, a ruthless, unbeatable ace racer who has "
            "never lost a circuit. Three punishing laps of the airport's figure-8 "
            "circuit at night in a thunderstorm, with two more rivals on the grid. "
            "Before the race, the player can open a settings menu to adjust the "
            "weather and the rival's difficulty."
        )

    def _race_schema_json(self) -> str:
        return json.dumps({
            "mission_name":  "Apex Circuit Grand Prix",
            "location_name": "airport",
            "time_of_day":   21,
            "weather":       "THUNDER",
            "ai_difficulty": "hard",
            "config_menu":   True,
            "steps": [
                {"type": "get_in_vehicle", "label": "Get to the start line"},
                {"type": "race_lap", "label": "Beat Vice to the finish",
                 "params": {"track": "airport.figure_8", "lap_count": 3,
                            "require_vehicle": True}},
            ],
            "vehicles": [
                {"description": "sports car", "role": "race_opponent", "count": 3},
            ],
            "audio": [
                {"type": "mission_passed", "context": "crossing the finish line"},
            ],
        })

    def _run_race(self, gta_pipeline_config) -> Path:
        pipeline = _make_pipeline(gta_pipeline_config)
        pipeline.llm.chat = MagicMock(
            side_effect=[self._race_concept_text(), self._race_schema_json()]
        )
        return pipeline.run(
            "A brutal airport street race against an unbeatable ace rival, with a "
            "pre-race settings menu to adjust weather and rival difficulty"
        )

    def test_hard_ai_difficulty_reaches_generated_code(self, gta_pipeline_config):
        out = self._run_race(gta_pipeline_config)
        code = out.read_text(encoding="utf-8")
        assert "_aiDifficultyIdx = 2;" in code   # 0=Easy 1=Medium 2=Hard
        assert "AiSpawnSpeedFor(_aiDifficultyIdx)" in code

    def test_config_menu_reaches_generated_code(self, gta_pipeline_config):
        out = self._run_race(gta_pipeline_config)
        code = out.read_text(encoding="utf-8")
        for tok in ("DrawConfigMenu", "HandleMenuControls", "KeyDown += OnKeyDown",
                    "AI Difficulty", "MISSION CONFIG"):
            assert tok in code, f"missing {tok}"

    def test_schema_json_sidecar_round_trips_new_fields(self, gta_pipeline_config):
        out = self._run_race(gta_pipeline_config)
        schema_file = out.with_suffix("").with_suffix(".schema.json")
        data = json.loads(schema_file.read_text(encoding="utf-8"))
        assert data["ai_difficulty"] == "hard"
        assert data["config_menu"] is True

    def test_generated_code_compiles_balanced(self, gta_pipeline_config):
        out = self._run_race(gta_pipeline_config)
        code = out.read_text(encoding="utf-8")
        assert code.count("{") == code.count("}")
        assert code.count("(") == code.count(")")


# ══════════════════════════════════════════════════════════════════════════════
# End-to-end: HeliRace-derived events / HUD / cash reward / scavenger step
#
# Proves the wire-up (SCHEMA_FILL_USER output keys in prompts.py → MissionSchema
# fields → _programmatic_fix clamps → CodeGenerator) survives a realistic
# scavenger-hunt prompt/concept/schema all the way to compiled C#, without a
# live LM Studio instance. Deliberately includes out-of-range values so the
# fixer clamps are exercised on the real path.
# ══════════════════════════════════════════════════════════════════════════════

class TestPipelineHeliRaceFeatures:
    def _concept_text(self) -> str:
        return (
            "A frantic scavenger hunt across the docks: five hidden stashes must "
            "be collected in any order before a rival crew gets wise. Halfway "
            "through, a thunderstorm rolls in; near the end the police scramble "
            "an attack helicopter. An on-screen timer and progress bar track the "
            "hunt, and the fixer pays $45,000 on completion."
        )

    def _schema_json(self) -> str:
        return json.dumps({
            "mission_name":  "Dockside Dead Drops",
            "location_name": "docks",
            "time_of_day":   21,
            "weather":       "OVERCAST",
            "hud":           True,
            "reward_cash":   45000,
            "events": [
                # at_progress out of the fixer's 0.1-0.9 band on purpose
                {"event_type": "thunderstorm",   "at_progress": 0.05},
                {"event_type": "police_pursuit", "at_progress": 0.95,
                 "params": {"wanted_level": 3, "spawn_heli": True}},
            ],
            "steps": [
                # target_count above the fixer's 3-10 clamp on purpose
                {"type": "scavenger", "label": "Collect the dead drops",
                 "params": {"target_count": 50}},
                {"type": "escape", "label": "Lose the heat"},
            ],
            "audio": [
                {"type": "mission_passed", "context": "all drops collected"},
            ],
        })

    def _run(self, gta_pipeline_config) -> Path:
        pipeline = _make_pipeline(gta_pipeline_config)
        pipeline.llm.chat = MagicMock(
            side_effect=[self._concept_text(), self._schema_json()]
        )
        return pipeline.run(
            "A dockside scavenger hunt with a mid-mission storm, a police attack "
            "chopper, an on-screen progress HUD, and a cash payout"
        )

    def test_feature_tokens_reach_generated_code(self, gta_pipeline_config):
        out = self._run(gta_pipeline_config)
        code = out.read_text(encoding="utf-8")
        for tok in ("Step0_SCAVENGER", "_scavVisited", "BlipColor.Green",
                    "UpdateEvents", 'SET_WEATHER_TYPE_NOW_PERSIST, "THUNDER"',
                    "TASK_HELI_MISSION", "DrawHud", "STAT_SET_INT",
                    "Game.Player.Money += 45000"):
            assert tok in code, f"missing {tok}"

    def test_fixer_clamps_survive_to_sidecar(self, gta_pipeline_config):
        out = self._run(gta_pipeline_config)
        schema_file = out.with_suffix("").with_suffix(".schema.json")
        data = json.loads(schema_file.read_text(encoding="utf-8"))
        assert data["hud"] is True
        assert data["reward_cash"] == 45000
        progresses = [e["at_progress"] for e in data["events"]]
        assert progresses == [0.1, 0.9]        # clamped from 0.05 / 0.95
        scav = next(s for s in data["steps"] if s["type"] == "scavenger")
        assert scav["params"]["target_count"] == 10   # clamped from 50

    def test_generated_code_compiles_balanced(self, gta_pipeline_config):
        out = self._run(gta_pipeline_config)
        code = out.read_text(encoding="utf-8")
        assert code.count("{") == code.count("}")
        assert code.count("(") == code.count(")")


class TestProgrammaticFixHeliRaceFeatures:
    def test_scavenger_target_count_clamped(self):
        schema = MissionSchema(mission_name="X", steps=[
            MissionStep(type="scavenger", label="Collect",
                        params={"target_count": 50, "radius": 99.0})])
        fixed = _programmatic_fix(schema)
        assert fixed.steps[0].params["target_count"] == 10
        assert fixed.steps[0].params["radius"] == 10.0

    def test_scavenger_garbage_params_fall_back(self):
        schema = MissionSchema(mission_name="X", steps=[
            MissionStep(type="scavenger", label="Collect",
                        params={"target_count": "many", "radius": "wide"})])
        fixed = _programmatic_fix(schema)
        assert fixed.steps[0].params["target_count"] == 5
        assert fixed.steps[0].params["radius"] == 3.0

    def test_event_progress_clamped_and_sorted(self):
        schema = MissionSchema(mission_name="X", steps=[], events=[
            {"event_type": "police_pursuit", "at_progress": 0.95},
            {"event_type": "thunderstorm",   "at_progress": 0.02},
        ])
        fixed = _programmatic_fix(schema)
        assert [(e.event_type, e.at_progress) for e in fixed.events] == [
            ("thunderstorm", 0.1), ("police_pursuit", 0.9)]

    def test_reward_cash_clamped(self):
        schema = MissionSchema(mission_name="X", steps=[])
        schema.reward_cash = 5_000_000        # bypass validator, fixer must clamp
        fixed = _programmatic_fix(schema)
        assert fixed.reward_cash == 1_000_000
