"""
Tests for the minigame skeleton: schema models (MinigameSetting/MinigameSpec/
skeleton flag), template selection, the menu-primary lifecycle C#, .ini
emission, and the MinigameServices hub companion.
"""
import json
from pathlib import Path

import pytest

from src.code_generator import CodeGenerator, _build_template_context, _render_minigame_ini
from src.mission_schema import (
    MinigameSetting,
    MinigameSpec,
    MissionSchema,
    MissionStep,
)


@pytest.fixture
def gen() -> CodeGenerator:
    return CodeGenerator("templates/mission.cs.j2")


def _minigame_schema(**kwargs) -> MissionSchema:
    defaults = dict(
        mission_name="Rooftop Rush",
        location_name="downtown",
        skeleton="minigame",
        minigame={
            "settings": [
                {"key": "round_time", "label": "Round Time", "kind": "cycle",
                 "options": ["60s", "120s", "180s"], "default": "120s"},
                {"key": "hardcore", "label": "Hardcore Mode", "kind": "toggle",
                 "default": "false"},
                {"key": "traffic", "label": "Traffic Density", "kind": "int",
                 "min_value": 0, "max_value": 10, "step": 2, "default": "4"},
            ],
            "score_metric": "seconds",
            "score_label": "TIME",
            "round_type": "timed",
            "round_params": {"seconds": 90},
        },
    )
    defaults.update(kwargs)
    return MissionSchema(**defaults)


# ══════════════════════════════════════════════════════════════════════════════
# Schema models
# ══════════════════════════════════════════════════════════════════════════════

class TestSkeletonFlag:
    def test_default_is_mission_with_no_spec(self):
        s = MissionSchema(mission_name="Legacy", steps=[])
        assert s.skeleton == "mission"
        assert s.minigame is None

    def test_unknown_skeleton_clamps_to_mission(self):
        s = MissionSchema(mission_name="X", steps=[], skeleton="arcade")
        assert s.skeleton == "mission"

    def test_minigame_skeleton_gets_default_spec(self):
        s = MissionSchema(mission_name="X", steps=[], skeleton="minigame")
        assert isinstance(s.minigame, MinigameSpec)
        assert s.minigame.round_type == "timed"
        assert s.minigame.round_params["seconds"] == 60

    def test_mission_skeleton_drops_spec(self):
        # A stray minigame block on a mission schema must not change output.
        s = MissionSchema(mission_name="X", steps=[], skeleton="mission",
                          minigame={"score_metric": "kills"})
        assert s.minigame is None


class TestMinigameSetting:
    def test_unknown_kind_clamps_to_int(self):
        assert MinigameSetting(key="a", label="A", kind="slider").kind == "int"

    def test_key_sanitized_to_ini_safe(self):
        assert MinigameSetting(key="Round Time!", label="X").key == "round_time"

    def test_empty_key_falls_back(self):
        assert MinigameSetting(key="!!!", label="X").key == "setting"

    def test_cycle_without_options_degrades_to_toggle(self):
        s = MinigameSetting(key="a", label="A", kind="cycle", options=["only"])
        assert s.kind == "toggle"
        assert s.default == "true"

    def test_cycle_default_snaps_to_first_option(self):
        s = MinigameSetting(key="a", label="A", kind="cycle",
                            options=["x", "y"], default="nope")
        assert s.default == "x"

    def test_int_default_clamped_into_range(self):
        s = MinigameSetting(key="a", label="A", kind="int",
                            min_value=5, max_value=10, default="99")
        assert s.default == "10"  # out-of-range clamps to the nearest bound

    def test_int_default_garbage_falls_back_to_min(self):
        s = MinigameSetting(key="a", label="A", kind="int",
                            min_value=5, max_value=10, default="lots")
        assert s.default == "5"

    def test_swapped_min_max_corrected(self):
        s = MinigameSetting(key="a", label="A", kind="int",
                            min_value=10, max_value=5)
        assert s.min_value == 5 and s.max_value == 10


class TestMinigameSpec:
    def test_duplicate_setting_keys_deduped(self):
        spec = MinigameSpec(settings=[
            {"key": "time", "label": "First"},
            {"key": "time", "label": "Second"},
        ])
        assert len(spec.settings) == 1
        assert spec.settings[0].label == "First"

    def test_unknown_metric_and_round_clamp(self):
        spec = MinigameSpec(score_metric="fame", round_type="endless")
        assert spec.score_metric == "points"
        assert spec.round_type == "timed"

    def test_timed_seconds_clamped(self):
        spec = MinigameSpec(round_params={"seconds": 9999})
        assert spec.round_params["seconds"] == 600


# ══════════════════════════════════════════════════════════════════════════════
# Codegen — template selection + minigame lifecycle C#
# ══════════════════════════════════════════════════════════════════════════════

class TestMinigameCodegen:
    def test_mission_schema_untouched_by_minigame_machinery(self, gen, gta_full_schema):
        cs = gen.generate(gta_full_schema)
        for tok in ("CfgRow", "_cfgValues", "State.Results", "ScriptSettings",
                    "SaveBestScore", ">> START <<"):
            assert tok not in cs, f"minigame leak into mission: {tok}"
        assert "CheckStart" in cs

    def test_minigame_renders_menu_primary_lifecycle(self, gen):
        cs = gen.generate(_minigame_schema())
        for tok in ("State.Idle", "State.Menu", "State.Playing", "State.Results",
                    "DrawMenu", "HandleMenuControls", "AdjustConfig", "MenuValueFor",
                    "ResetRound", "RunRound", "EndRound", "DrawResults",
                    ">> START <<", "Keys.F7"):
            assert tok in cs, f"minigame C# missing {tok}"

    def test_minigame_has_no_mission_trappings(self, gen):
        cs = gen.generate(_minigame_schema())
        for tok in ("CheckStart", "_startBlip", "RunStep", "Keys.F5",
                    "State.Inactive", "Complete()"):
            assert tok not in cs, f"mission trapping in minigame: {tok}"

    def test_rows_render_as_cfgrow_entries(self, gen):
        cs = gen.generate(_minigame_schema())
        assert 'Key = "round_time"' in cs
        assert "RowKind.Cycle" in cs
        assert "RowKind.Toggle" in cs
        assert "RowKind.Int" in cs
        assert '"60s", "120s", "180s"' in cs

    def test_ini_persistence_uses_explicit_path(self, gen):
        cs = gen.generate(_minigame_schema())
        # C# string literal escapes the backslash: scripts\\RooftopRush.ini
        assert 'ScriptSettings.Load("scripts\\\\RooftopRush.ini")' in cs
        assert "SaveIniSettings" in cs
        assert "SaveBestScore" in cs

    def test_round_params_reach_round_body(self, gen):
        cs = gen.generate(_minigame_schema())
        assert "90" in cs  # timed round seconds

    def test_score_label_reaches_hud_and_results(self, gen):
        cs = gen.generate(_minigame_schema())
        assert "TIME" in cs

    def test_extra_insertion_points_supported(self, gen):
        schema = _minigame_schema(
            extra_fields=[{"name": "_combo", "type": "int", "default": "0",
                           "tooltip": "combo multiplier"}],
            extra_init="_combo = 1;",
            extra_tick="if (_state == State.Playing) { /* combo decay */ }",
            extra_methods=[{"signature": "private void Bump()",
                            "body": "_combo++;"}],
        )
        cs = gen.generate(schema)
        assert "private int _combo = 0;" in cs
        assert "_combo = 1;" in cs
        assert "private void Bump()" in cs

    def test_no_settings_still_renders(self, gen):
        cs = gen.generate(MissionSchema(
            mission_name="Bare Loop", skeleton="minigame"))
        assert ">> START <<" in cs
        assert cs.count("{") == cs.count("}")

    def test_braces_and_parens_balanced(self, gen):
        cs = gen.generate(_minigame_schema())
        assert cs.count("{") == cs.count("}")
        assert cs.count("(") == cs.count(")")

    def test_output_is_deterministic(self, gen):
        schema = _minigame_schema()
        assert gen.generate(schema) == gen.generate(schema)


# ══════════════════════════════════════════════════════════════════════════════
# survive_waves hero ability layer (xmen archetype) — gated on a settings row
# keyed "hero", looked up by position at runtime
# ══════════════════════════════════════════════════════════════════════════════

def _xmen_schema(**kwargs) -> MissionSchema:
    defaults = dict(
        mission_name="Mutant Uprising",
        location_name="downtown",
        skeleton="minigame",
        minigame={
            "settings": [
                {"key": "hero", "label": "Hero", "kind": "cycle",
                 "options": ["Magneto", "Wolverine", "Mystique"], "default": "Magneto"},
            ],
            "score_metric": "kills",
            "score_label": "KILLS",
            "round_type": "survive_waves",
        },
    )
    defaults.update(kwargs)
    return MissionSchema(**defaults)


class TestHeroAbilityLayer:
    def test_hero_row_wires_ability_code(self, gen):
        cs = gen.generate(_xmen_schema())
        assert "RunHeroAbility" in cs
        assert "_cfgValues[0]" in cs           # hero is the only/first settings row
        assert "SetSuperJumpThisFrame" in cs   # Wolverine passive
        assert "GetNearbyVehicles" in cs       # Magneto push/pull
        assert "_mysticMark" in cs             # Mystique mark/warp

    def test_no_hero_row_omits_ability_code(self, gen):
        # A plain survival_waves schema (no "hero" settings key) must not emit
        # any of the hero-ability scaffolding — it's purely additive.
        cs = gen.generate(_minigame_schema(
            skeleton="minigame",
            minigame={
                "settings": [{"key": "wave_count", "label": "Waves", "kind": "int",
                             "min_value": 3, "max_value": 10, "default": "5"}],
                "score_metric": "kills", "score_label": "KILLS",
                "round_type": "survive_waves",
            },
        ))
        assert "RunHeroAbility" not in cs
        assert "_mysticMark" not in cs
        assert "SetSuperJumpThisFrame" not in cs

    def test_hero_row_index_follows_position(self, gen):
        # "hero" isn't required to be the first row — the index must track
        # wherever it actually lands in settings.
        cs = gen.generate(_xmen_schema(minigame={
            "settings": [
                {"key": "wave_count", "label": "Waves", "kind": "int",
                 "min_value": 3, "max_value": 10, "default": "5"},
                {"key": "hero", "label": "Hero", "kind": "cycle",
                 "options": ["Magneto", "Wolverine", "Mystique"], "default": "Magneto"},
            ],
            "score_metric": "kills", "score_label": "KILLS",
            "round_type": "survive_waves",
        }))
        assert "_cfgValues[1]" in cs

    def test_braces_and_parens_balanced(self, gen):
        cs = gen.generate(_xmen_schema())
        assert cs.count("{") == cs.count("}")
        assert cs.count("(") == cs.count(")")


# ══════════════════════════════════════════════════════════════════════════════
# theft_streak round type — jack a vehicle, deliver to the compiler-placed
# garage, secure it (+1); garage sits at a real offset from the location anchor
# ══════════════════════════════════════════════════════════════════════════════

def _theft_schema(**kwargs) -> MissionSchema:
    defaults = dict(
        mission_name="Chop Shop Rush",
        location_name="downtown",
        skeleton="minigame",
        minigame={
            "settings": [
                {"key": "time_limit", "label": "Time Limit", "kind": "cycle",
                 "options": ["60", "120", "180"], "default": "120"},
            ],
            "score_metric": "points",
            "score_label": "CARS SECURED",
            "round_type": "theft_streak",
            "round_params": {"time_limit_s": 120, "garage_radius": 8.0},
        },
    )
    defaults.update(kwargs)
    return MissionSchema(**defaults)


class TestTheftStreakRoundType:
    def test_garage_positioned_from_real_location_anchor(self, gen):
        # downtown's real spawn anchor is (250.0, -850.0, 29.7) — the garage
        # must be a fixed offset from it, not an invented/arbitrary point.
        cs = gen.generate(_theft_schema())
        assert "new Vector3(275.0f, -825.0f, 29.7f)" in cs

    def test_theft_loop_and_garage_blip_present(self, gen):
        cs = gen.generate(_theft_schema())
        assert "_stolenVehicle" in cs
        assert "_securedHandles" in cs
        assert "World.CreateBlip(_garagePos)" in cs
        assert "_garageBlip.Delete()" in cs

    def test_scoring_gated_on_garage_radius(self, gen):
        cs = gen.generate(_theft_schema())
        assert "_gdist <= 8.0f" in cs
        assert "_securedHandles.Add(_cur.Handle)" in cs
        assert "_score++" in cs

    def test_time_limit_reaches_round_and_hud(self, gen):
        cs = gen.generate(_theft_schema())
        assert "_theftElapsed >= 120" in cs
        assert "120 - (int)_rt.TotalSeconds" in cs

    def test_secured_vehicle_cannot_be_rescored(self, gen):
        cs = gen.generate(_theft_schema())
        assert "!_securedHandles.Contains(_cur.Handle)" in cs

    def test_other_round_types_omit_theft_state(self, gen):
        cs = gen.generate(_minigame_schema())   # timed round from earlier fixture
        assert "_stolenVehicle" not in cs
        assert "_garageBlip" not in cs

    def test_braces_and_parens_balanced(self, gen):
        cs = gen.generate(_theft_schema())
        assert cs.count("{") == cs.count("}")
        assert cs.count("(") == cs.count(")")


# ══════════════════════════════════════════════════════════════════════════════
# write() — multi-file emission (.3.cs + schema + .ini + services hub)
# ══════════════════════════════════════════════════════════════════════════════

class TestMinigameWrite:
    def test_minigame_emits_four_files(self, gen, tmp_path):
        gen.write(_minigame_schema(), str(tmp_path))
        names = sorted(p.name for p in tmp_path.iterdir())
        assert names == ["MinigameServices.3.cs", "RooftopRush.3.cs",
                         "RooftopRush.ini", "RooftopRush.schema.json"]

    def test_mission_still_emits_two_files(self, gen, gta_minimal_schema, tmp_path):
        gen.write(gta_minimal_schema, str(tmp_path))
        names = sorted(p.name for p in tmp_path.iterdir())
        assert names == ["TestMission.3.cs", "TestMission.schema.json"]

    def test_ini_contains_marker_settings_and_scores(self, gen, tmp_path):
        gen.write(_minigame_schema(), str(tmp_path))
        ini = (tmp_path / "RooftopRush.ini").read_text(encoding="utf-8")
        assert "[Minigame]" in ini
        assert "Name=Rooftop Rush" in ini
        assert "Metric=seconds" in ini
        assert "round_time=120s" in ini
        assert "hardcore=false" in ini
        assert "traffic=4" in ini
        assert "[Scores]" in ini and "Best=0" in ini

    def test_services_hub_is_self_contained_script(self, gen, tmp_path):
        gen.write(_minigame_schema(), str(tmp_path))
        svc = (tmp_path / "MinigameServices.3.cs").read_text(encoding="utf-8")
        assert ": Script" in svc
        assert "Keys.F9" in svc
        assert '"Minigame"' in svc          # scans for the ini marker section
        assert "Rooftop" not in svc          # never references a specific minigame
        assert svc.count("{") == svc.count("}")

    def test_repeated_writes_byte_identical(self, gen, tmp_path):
        schema = _minigame_schema()
        gen.write(schema, str(tmp_path))
        first = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
        gen.write(schema, str(tmp_path))
        second = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
        assert first == second

    def test_schema_sidecar_round_trips(self, gen, tmp_path):
        out = gen.write(_minigame_schema(), str(tmp_path))
        data = json.loads((tmp_path / "RooftopRush.schema.json").read_text(encoding="utf-8"))
        restored = MissionSchema.model_validate(data)
        assert restored.skeleton == "minigame"
        assert restored.minigame.score_metric == "seconds"
        assert gen.generate(restored) == out.read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# Pipeline integration — reserved fields + compile_from_schema
# ══════════════════════════════════════════════════════════════════════════════

class TestMinigamePipelineIntegration:
    def test_reserved_minigame_fields_dropped_from_extras(self):
        from src.pipeline import _lint_mission_extras
        schema = _minigame_schema(
            extra_fields=[{"name": "_score", "type": "int", "default": "0"},
                          {"name": "_myOwn", "type": "int", "default": "0"}])
        _lint_mission_extras(schema)
        names = [f.name for f in schema.extra_fields]
        assert "_score" not in names
        assert "_myOwn" in names

    def test_compile_from_schema_round_trip(self, tmp_path):
        from src.pipeline import compile_from_schema
        schema = _minigame_schema()
        schema_file = tmp_path / "in.schema.json"
        schema_file.write_text(schema.model_dump_json(indent=2), encoding="utf-8")
        out = compile_from_schema(schema_file, output_dir=str(tmp_path / "out"))
        assert out.name == "RooftopRush.3.cs"
        code = out.read_text(encoding="utf-8")
        assert "State.Menu" in code
        assert (tmp_path / "out" / "RooftopRush.ini").exists()
        assert (tmp_path / "out" / "MinigameServices.3.cs").exists()

    def test_render_minigame_ini_helper_deterministic(self):
        schema = _minigame_schema()
        ctx = _build_template_context(schema)
        assert _render_minigame_ini(schema, ctx) == _render_minigame_ini(schema, ctx)
