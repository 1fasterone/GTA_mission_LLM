"""
End-to-end tests for the two launch minigames: Survival Waves + Pursuit Escape.

Covers archetype detection (incl. the chase-keyword tie-break), the
always-enforced minigame scaffold, round-param clamps, template composition of
the gta5 micro partials, and full fake-LLM pipeline runs producing the 4-file
minigame bundle.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.archetypes import apply_scaffold, detect_archetype, get_archetype
from src.code_generator import CodeGenerator
from src.mission_schema import (
    MissionPed,
    MissionSchema,
    PursuitRoundParams,
    WaveRoundParams,
)


@pytest.fixture
def gen() -> CodeGenerator:
    return CodeGenerator("templates/mission.cs.j2")


def _waves_schema(**round_params) -> MissionSchema:
    params = {"wave_count": 6, "enemies_per_wave": 4,
              "wave_growth": 2.2, "arena_radius": 40}
    params.update(round_params)
    return MissionSchema(
        mission_name="Arena Onslaught", location_name="sandy_shores",
        skeleton="minigame",
        peds=[MissionPed(description="cult enforcer", model="a_m_o_acult_01",
                         role="target", weapon="shotgun")],
        minigame={"score_metric": "kills", "score_label": "KILLS",
                  "round_type": "survive_waves", "round_params": params},
    )


def _pursuit_schema(**round_params) -> MissionSchema:
    params = {"start_wanted": 3, "escalation_interval_s": 30,
              "max_wanted": 5, "spawn_heli_at_level": 4}
    params.update(round_params)
    return MissionSchema(
        mission_name="Most Wanted Marathon", location_name="downtown",
        skeleton="minigame",
        minigame={"score_metric": "seconds", "score_label": "SURVIVED",
                  "round_type": "pursuit_survive", "round_params": params},
    )


# ══════════════════════════════════════════════════════════════════════════════
# Round param models
# ══════════════════════════════════════════════════════════════════════════════

class TestRoundParams:
    def test_wave_params_clamp(self):
        p = WaveRoundParams(wave_count=99, enemies_per_wave=0,
                            wave_growth=9.0, arena_radius=5.0,
                            intermission_seconds="soon")
        assert p.wave_count == 10
        assert p.enemies_per_wave == 2
        assert p.wave_growth == 3.0
        assert p.arena_radius == 20.0
        assert p.intermission_seconds == 8

    def test_pursuit_params_clamp_and_order(self):
        p = PursuitRoundParams(start_wanted=5, max_wanted=3,
                               escalation_interval_s=1, spawn_heli_at_level=99)
        assert p.max_wanted == 3
        assert p.start_wanted == 3          # ordered under the cap
        assert p.escalation_interval_s == 15
        assert p.spawn_heli_at_level == 5

    def test_minigame_spec_normalizes_round_params(self):
        s = _waves_schema(wave_count=50)
        assert s.minigame.round_params["wave_count"] == 10
        s2 = _pursuit_schema(escalation_interval_s=999)
        assert s2.minigame.round_params["escalation_interval_s"] == 120


# ══════════════════════════════════════════════════════════════════════════════
# Archetype detection + scaffold
# ══════════════════════════════════════════════════════════════════════════════

class TestMinigameArchetypes:
    def test_survival_prompt_detected(self):
        arch = detect_archetype(
            "an arena survival minigame where waves of cultists attack")
        assert arch is not None and arch.name == "survival_waves"
        assert arch.wants_skeleton == "minigame"

    def test_pursuit_prompt_beats_chase(self):
        # Tie-break: multiword pursuit keywords must outscore the generic
        # chase archetype's single "chase" hit.
        arch = detect_archetype(
            "survive the heat as long as you can — a most wanted cop chase "
            "survival minigame")
        assert arch is not None and arch.name == "pursuit_escape"

    def test_classic_chase_prompt_still_chase(self):
        arch = detect_archetype("chase down the fleeing van before it escapes")
        assert arch is not None and arch.name == "chase"

    def test_scaffold_forces_minigame_on_mission_shaped_output(self):
        # LLM ignored the minigame block and emitted a story mission.
        schema = MissionSchema(
            mission_name="X", location_name="downtown",
            steps=[{"type": "go_to", "label": "Go somewhere"},
                   {"type": "kill_all", "label": "Fight"}],
        )
        arch = get_archetype("survival_waves")
        msgs = apply_scaffold(schema, arch)
        assert msgs
        assert schema.skeleton == "minigame"
        assert schema.steps == []
        assert schema.minigame.round_type == "survive_waves"
        assert schema.minigame.settings          # default menu rows seeded
        assert schema.peds                        # default wave enemy seeded

    def test_scaffold_preserves_llm_authored_config(self):
        schema = _waves_schema()
        schema.minigame.settings = []             # only settings are missing
        arch = get_archetype("survival_waves")
        apply_scaffold(schema, arch)
        assert schema.minigame.round_params["wave_count"] == 6   # LLM value kept
        assert schema.peds[0].model == "a_m_o_acult_01"          # ped kept
        assert schema.minigame.settings                          # rows seeded

    def test_pursuit_scaffold_sets_seconds_metric(self):
        schema = MissionSchema(mission_name="X", steps=[])
        apply_scaffold(schema, get_archetype("pursuit_escape"))
        assert schema.skeleton == "minigame"
        assert schema.minigame.round_type == "pursuit_survive"
        assert schema.minigame.score_metric == "seconds"


# ══════════════════════════════════════════════════════════════════════════════
# Codegen — round bodies composed from the gta5 micro partials
# ══════════════════════════════════════════════════════════════════════════════

class TestSurvivalWavesCodegen:
    def test_composes_spawner_and_waves_partials(self, gen):
        cs = gen.generate(_waves_schema())
        for tok in ("SpawnWave", "AliveCount", "CleanupWave",           # spawner partial
                    "WaveSizeFor", "BeginWave", "BeginIntermission",    # waves partial
                    "_prevAlive", "CleanupRound", "wave-clear bonus"):
            assert tok in cs, f"missing {tok}"

    def test_enemy_model_and_weapon_come_from_resolved_ped(self, gen):
        cs = gen.generate(_waves_schema())
        assert "a_m_o_acult_01" in cs
        assert "weapon_pumpshotgun" in cs

    def test_unresolved_ped_falls_back(self, gen):
        schema = _waves_schema()
        schema.peds = []
        cs = gen.generate(schema)
        assert "g_m_y_lost_01" in cs

    def test_steep_growth_selects_exponential_director(self, gen):
        assert "Math.Pow" in gen.generate(_waves_schema(wave_growth=2.5))
        assert "Math.Pow" not in gen.generate(_waves_schema(wave_growth=1.2))

    def test_balanced_and_deterministic(self, gen):
        schema = _waves_schema()
        cs = gen.generate(schema)
        assert cs.count("{") == cs.count("}")
        assert cs.count("(") == cs.count(")")
        assert gen.generate(schema) == cs


class TestPursuitEscapeCodegen:
    def test_wanted_escalation_machinery(self, gen):
        cs = gen.generate(_pursuit_schema())
        for tok in ("SET_PLAYER_WANTED_LEVEL", "GET_PLAYER_WANTED_LEVEL",
                    "_nextEscalation", "THE HEAT IS RISING", "CleanupRound"):
            assert tok in cs, f"missing {tok}"
        # Every exit path clears the heat.
        assert "SET_PLAYER_WANTED_LEVEL, Game.Player, 0" in cs

    def test_heli_at_level_emits_attack_heli(self, gen):
        cs = gen.generate(_pursuit_schema(spawn_heli_at_level=4))
        assert "TASK_HELI_MISSION" in cs
        assert "buzzard" in cs

    def test_heli_level_zero_omits_heli(self, gen):
        cs = gen.generate(_pursuit_schema(spawn_heli_at_level=0))
        assert "TASK_HELI_MISSION" not in cs

    def test_no_spawner_leak_between_round_types(self, gen):
        cs = gen.generate(_pursuit_schema())
        assert "SpawnWave" not in cs
        timed = gen.generate(MissionSchema(mission_name="T", skeleton="minigame"))
        assert "SET_PLAYER_WANTED_LEVEL" not in timed

    def test_balanced_and_deterministic(self, gen):
        schema = _pursuit_schema()
        cs = gen.generate(schema)
        assert cs.count("{") == cs.count("}")
        assert cs.count("(") == cs.count(")")
        assert gen.generate(schema) == cs


# ══════════════════════════════════════════════════════════════════════════════
# Full fake-LLM pipeline runs
# ══════════════════════════════════════════════════════════════════════════════

def _make_pipeline(config: dict):
    from src.pipeline import MissionPipeline
    return MissionPipeline(config)


class TestMinigamePipelineE2E:
    def _run(self, gta_pipeline_config, prompt, concept, schema_json) -> Path:
        pipeline = _make_pipeline(gta_pipeline_config)
        pipeline.llm.chat = MagicMock(side_effect=[concept, schema_json])
        return pipeline.run(prompt)

    def test_survival_waves_end_to_end(self, gta_pipeline_config):
        out = self._run(
            gta_pipeline_config,
            "an arena survival minigame where waves of gang enforcers attack",
            "An arena last-stand at Sandy Shores: escalating waves of gang "
            "enforcers with shotguns, kill score, breathers between waves, "
            "instant replay from the config menu.",
            json.dumps({
                "mission_name":  "Sandy Shores Onslaught",
                "location_name": "sandy_shores",
                "skeleton":      "minigame",
                "minigame": {
                    "score_metric": "kills", "score_label": "KILLS",
                    "round_type": "survive_waves",
                    # out-of-range on purpose — round model must clamp
                    "round_params": {"wave_count": 50, "enemies_per_wave": 4},
                },
                "peds": [
                    {"description": "gang member", "role": "target",
                     "count": 1, "weapon": "shotgun"},
                ],
                "steps": [],
            }),
        )
        code = out.read_text(encoding="utf-8")
        for tok in ("State.Menu", "SpawnWave", "WaveSizeFor", ">> START <<"):
            assert tok in code, f"missing {tok}"
        assert code.count("{") == code.count("}")
        out_dir = out.parent
        assert (out_dir / "SandyShoresOnslaught.ini").exists()
        assert (out_dir / "MinigameServices.3.cs").exists()
        sidecar = json.loads(
            (out_dir / "SandyShoresOnslaught.schema.json").read_text(encoding="utf-8"))
        assert sidecar["skeleton"] == "minigame"
        assert sidecar["minigame"]["round_params"]["wave_count"] == 10  # clamped

    def test_pursuit_escape_end_to_end(self, gta_pipeline_config):
        out = self._run(
            gta_pipeline_config,
            "survive the heat as long as you can — a most wanted police "
            "survival minigame",
            "A downtown heat marathon: start at two stars, the pursuit "
            "escalates on a timer to five stars with an attack helicopter; "
            "score is seconds survived, best time persists.",
            json.dumps({
                "mission_name":  "Heat Marathon",
                "location_name": "downtown",
                # LLM ignored the minigame block entirely — mission-shaped
                # output. The pursuit_escape scaffold must still force the
                # minigame skeleton deterministically.
                "steps": [
                    {"type": "escape", "label": "Lose the cops"},
                ],
            }),
        )
        code = out.read_text(encoding="utf-8")
        for tok in ("State.Menu", "GET_PLAYER_WANTED_LEVEL", "_nextEscalation",
                    ">> START <<"):
            assert tok in code, f"missing {tok}"
        assert code.count("{") == code.count("}")
        sidecar = json.loads(
            (out.parent / "HeatMarathon.schema.json").read_text(encoding="utf-8"))
        assert sidecar["skeleton"] == "minigame"
        assert sidecar["minigame"]["round_type"] == "pursuit_survive"
        assert (out.parent / "HeatMarathon.ini").exists()

    def test_minigame_sidecar_recompiles_identically(self, gta_pipeline_config, tmp_path):
        from src.pipeline import compile_from_schema
        schema = _waves_schema()
        schema_file = tmp_path / "in.schema.json"
        schema_file.write_text(schema.model_dump_json(indent=2), encoding="utf-8")
        out = compile_from_schema(schema_file, output_dir=str(tmp_path / "out"))
        gen = CodeGenerator()
        assert out.read_text(encoding="utf-8") == gen.generate(schema)
