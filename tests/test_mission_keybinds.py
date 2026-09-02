"""
Tests for keybind-gated mid-mission actions (MissionKeybind/TeleportPoint):
schema validation (abstain-loudly on duplicate/reserved keys and dangling
targets) and the OnKeyDown/SpawnKeybind* codegen in templates/mission.cs.j2.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.code_generator import CodeGenerator
from src.gta5_compile_check import _find_arity_findings
from src.mission_schema import (
    MissionKeybind, MissionPed, MissionSchema, MissionStep, MissionVehicle, TeleportPoint,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _base_schema(**kwargs) -> MissionSchema:
    defaults = dict(
        mission_name="Rooftop Ambush",
        location_name="downtown",
        steps=[MissionStep(type="go_to", label="Head to the roof")],
        peds=[
            MissionPed(description="backup guard", model="s_m_y_cop_01",
                       role="ally", weapon="pistol"),
            MissionPed(description="hostile enforcer", model="g_m_y_lost_01",
                       role="target", weapon="rifle"),
        ],
        vehicles=[MissionVehicle(description="getaway car", model="blista", role="escape")],
        teleport_points=[TeleportPoint(name="rooftop", location_name="downtown",
                                       offset_x=15, offset_y=-10)],
    )
    defaults.update(kwargs)
    return MissionSchema(**defaults)


# ══════════════════════════════════════════════════════════════════════════════
# Schema validation — abstain loudly, never silently default
# ══════════════════════════════════════════════════════════════════════════════

class TestKeybindValidation:
    def test_valid_keybinds_all_kept(self):
        s = _base_schema(keybinds=[
            MissionKeybind(key="F6", action="spawn_ped_group", target="hostile enforcer"),
            MissionKeybind(key="F7", action="spawn_ped_group", target="backup guard"),
            MissionKeybind(key="F8", action="spawn_vehicle", target="getaway car"),
            MissionKeybind(key="F9", action="teleport_player", target="rooftop"),
        ])
        assert len(s.keybinds) == 4
        assert not s.meta.get("keybind_warnings")

    def test_duplicate_key_drops_the_second_and_names_both(self):
        s = _base_schema(keybinds=[
            MissionKeybind(key="F6", action="spawn_ped_group", target="hostile enforcer"),
            MissionKeybind(key="F6", action="spawn_vehicle", target="getaway car"),
        ])
        assert [kb.action for kb in s.keybinds] == ["spawn_ped_group"]
        warnings = s.meta.get("keybind_warnings") or []
        assert len(warnings) == 1
        assert "F6" in warnings[0] and "spawn_vehicle" in warnings[0]

    def test_reserved_keys_are_dropped(self):
        for reserved in ("F5", "F10"):
            s = _base_schema(keybinds=[
                MissionKeybind(key=reserved, action="spawn_ped_group", target="hostile enforcer"),
            ])
            assert s.keybinds == []
            assert reserved in (s.meta.get("keybind_warnings") or [""])[0]

    def test_dangling_ped_target_abstains(self):
        s = _base_schema(keybinds=[
            MissionKeybind(key="F6", action="spawn_ped_group", target="a ped that was never declared"),
        ])
        assert s.keybinds == []
        assert "spawn_ped_group" in s.meta["keybind_warnings"][0]

    def test_dangling_vehicle_target_abstains(self):
        s = _base_schema(keybinds=[
            MissionKeybind(key="F6", action="spawn_vehicle", target="a car that was never declared"),
        ])
        assert s.keybinds == []
        assert "spawn_vehicle" in s.meta["keybind_warnings"][0]

    def test_dangling_teleport_target_abstains(self):
        s = _base_schema(keybinds=[
            MissionKeybind(key="F6", action="teleport_player", target="nowhere"),
        ])
        assert s.keybinds == []
        assert "teleport_player" in s.meta["keybind_warnings"][0]

    def test_valid_and_invalid_keybinds_coexist(self):
        """One bad keybind must not discard the rest of an otherwise valid schema."""
        s = _base_schema(keybinds=[
            MissionKeybind(key="F6", action="spawn_ped_group", target="hostile enforcer"),
            MissionKeybind(key="F7", action="spawn_ped_group", target="nonexistent"),
        ])
        assert len(s.keybinds) == 1
        assert s.keybinds[0].key == "F6"
        assert len(s.meta["keybind_warnings"]) == 1

    def test_key_normalises_case_and_clamps_garbage(self):
        assert MissionKeybind(key="f6", action="teleport_player").key == "F6"
        assert MissionKeybind(key="not-a-key", action="teleport_player").key == "F6"
        assert MissionKeybind(key="F13", action="teleport_player").key == "F6"

    def test_teleport_point_never_carries_raw_world_coords(self):
        """TeleportPoint only exposes location_name + a bounded offset — the LLM
        has no field through which it could invent an absolute position."""
        assert set(TeleportPoint.model_fields) == {"name", "location_name", "offset_x", "offset_y"}
        assert TeleportPoint(name="x", location_name="not_a_real_place").location_name == "docks"
        assert TeleportPoint(name="x", offset_x=99999).offset_x == 300.0

    def test_round_trips_through_json(self):
        s = _base_schema(keybinds=[
            MissionKeybind(key="F6", action="spawn_ped_group", target="hostile enforcer"),
        ])
        s2 = MissionSchema.model_validate(s.model_dump())
        assert len(s2.keybinds) == 1


# ══════════════════════════════════════════════════════════════════════════════
# Codegen — OnKeyDown wiring + SpawnKeybindPeds_N/SpawnKeybindVehicle_N
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def gen() -> CodeGenerator:
    return CodeGenerator()


class TestKeybindCodegen:
    def test_no_keybinds_omits_onkeydown_entirely(self, gen):
        """Backward-compat: a schema with no keybinds and no config_menu must not
        emit OnKeyDown/KeyDown wiring at all."""
        code = gen.generate(_base_schema())
        assert "OnKeyDown" not in code
        assert "KeyDown +=" not in code

    def test_config_menu_alone_still_works_unchanged(self, gen):
        """The pre-existing config_menu-only OnKeyDown path must be untouched."""
        code = gen.generate(_base_schema(config_menu=True))
        assert "KeyDown += OnKeyDown;" in code
        assert "e.KeyCode == Keys.F10" in code
        assert "SpawnKeybindPeds" not in code

    def test_teleport_keybind_emits_position_assignment(self, gen):
        code = gen.generate(_base_schema(keybinds=[
            MissionKeybind(key="F9", action="teleport_player", target="rooftop", label="Blink"),
        ]))
        assert "e.KeyCode == Keys.F9" in code
        assert "Game.Player.Character.Position = new Vector3(" in code
        assert "~y~Blink" in code

    def test_spawn_ped_group_keybind_emits_dedicated_method(self, gen):
        code = gen.generate(_base_schema(keybinds=[
            MissionKeybind(key="F6", action="spawn_ped_group", target="hostile enforcer"),
        ]))
        assert "SpawnKeybindPeds_0();" in code
        assert "private void SpawnKeybindPeds_0()" in code
        assert 'World.CreatePed(new Model("g_m_y_lost_01")' in code
        # role="target" -> hostile combat task, not the ally follow task
        assert "p.Task.Combat(Game.Player.Character);" in code

    def test_spawn_ally_ped_keybind_uses_follow_task(self, gen):
        code = gen.generate(_base_schema(keybinds=[
            MissionKeybind(key="F7", action="spawn_ped_group", target="backup guard"),
        ]))
        assert "TASK_FOLLOW_TO_OFFSET_OF_ENTITY" in code

    def test_spawn_vehicle_keybind_emits_dedicated_method(self, gen):
        code = gen.generate(_base_schema(keybinds=[
            MissionKeybind(key="F8", action="spawn_vehicle", target="getaway car"),
        ]))
        assert "SpawnKeybindVehicle_0();" in code
        assert "private void SpawnKeybindVehicle_0()" in code
        assert 'World.CreateVehicle(new Model("blista")' in code

    def test_multiple_keybinds_get_distinct_indices(self, gen):
        code = gen.generate(_base_schema(keybinds=[
            MissionKeybind(key="F6", action="spawn_ped_group", target="hostile enforcer"),
            MissionKeybind(key="F7", action="spawn_ped_group", target="backup guard"),
            MissionKeybind(key="F8", action="spawn_vehicle", target="getaway car"),
            MissionKeybind(key="F9", action="teleport_player", target="rooftop"),
        ]))
        assert "SpawnKeybindPeds_0" in code
        assert "SpawnKeybindPeds_1" in code
        assert "SpawnKeybindVehicle_2" in code
        assert code.count("{") == code.count("}")

    def test_keybind_actions_gated_on_mission_active(self, gen):
        code = gen.generate(_base_schema(keybinds=[
            MissionKeybind(key="F6", action="spawn_ped_group", target="hostile enforcer"),
        ]))
        onkeydown = code.split("private void OnKeyDown", 1)[1].split("private void OnTick", 1)[0]
        assert "if (_state != State.Active) return;" in onkeydown

    def test_unresolved_ped_model_keybind_omits_dedicated_method(self, gen):
        """A ped whose model never resolved (RAG failure) must not get a spawn
        method that would emit `new Model("UNRESOLVED")` into real C#."""
        s = _base_schema(
            peds=[MissionPed(description="hostile enforcer", role="target")],  # model=None
            keybinds=[MissionKeybind(key="F6", action="spawn_ped_group", target="hostile enforcer")],
        )
        assert s.peds[0].model is None
        code = gen.generate(s)
        assert "SpawnKeybindPeds_0" not in code

    def test_generated_code_passes_native_arity_check(self, gen, tmp_path):
        code = gen.generate(_base_schema(keybinds=[
            MissionKeybind(key="F6", action="spawn_ped_group", target="hostile enforcer"),
            MissionKeybind(key="F7", action="spawn_ped_group", target="backup guard"),
            MissionKeybind(key="F8", action="spawn_vehicle", target="getaway car"),
            MissionKeybind(key="F9", action="teleport_player", target="rooftop"),
        ]))
        cs_path = tmp_path / "Probe.3.cs"
        cs_path.write_text(code, encoding="utf-8")
        assert _find_arity_findings(str(cs_path)) == []

    def test_output_is_deterministic(self, gen):
        s = _base_schema(keybinds=[
            MissionKeybind(key="F6", action="spawn_ped_group", target="hostile enforcer"),
            MissionKeybind(key="F9", action="teleport_player", target="rooftop"),
        ])
        assert gen.generate(s) == gen.generate(s)


# ══════════════════════════════════════════════════════════════════════════════
# Captured-LLM regression fixture — real held-out-brief replay, never hand-edited
# ══════════════════════════════════════════════════════════════════════════════
#
# tests/fixtures/vinewood_rooftop_infiltration_llm.schema.json is Step-3
# schema-fill output captured VERBATIM from a live `generate` run against
# prompts/vinewood_rooftop_infiltration.txt (a held-out natural-language
# brief — never hand-formatted, per CLAUDE.md's Eval Discipline). It is the
# first brief in the gauntlet to reach a fully wired keybind: the LLM paired
# a teleport_points entry with a keybinds entry whose target matches it
# exactly. Two earlier live runs against different gauntlet briefs did NOT
# populate keybinds at all (one drifted into an unrelated race archetype;
# the other added a teleport_points entry but no keybind to fire it) — that
# gap is exactly what motivated the "REQUIRED whenever the concept
# describes a one-press/on-demand ability" line src/prompts.py's keybinds
# field now carries. This fixture replays the DETERMINISTIC tail only
# (resolver -> mission_schema repair pass -> code_generator); it never
# re-invokes the LLM, so it stays a fast, offline regression case.

class TestVinewoodRooftopLiveCapture:
    def _schema(self) -> MissionSchema:
        data = json.loads(
            (FIXTURES / "vinewood_rooftop_infiltration_llm.schema.json").read_text(encoding="utf-8"))
        return MissionSchema.model_validate(data)

    def test_fixture_still_validates_against_the_current_schema(self):
        """Catches schema drift — the captured JSON must keep parsing as the
        schema evolves, the same guarantee test_outputs_smoke.py's
        test_gta_schema_round_trip gives real generated schemas."""
        s = self._schema()
        assert s.mission_name
        assert not s.meta.get("keybind_warnings"), "a previously-valid capture started abstaining"

    def test_fixture_keeps_its_paired_teleport_keybind(self):
        s = self._schema()
        assert len(s.keybinds) == 1
        kb = s.keybinds[0]
        assert kb.action == "teleport_player"
        assert kb.key == "F6"
        assert len(s.teleport_points) == 1
        assert s.teleport_points[0].name.strip().lower() == kb.target.strip().lower()

    def test_fixture_replays_through_the_deterministic_tail(self):
        code = CodeGenerator().generate(self._schema())
        assert "e.KeyCode == Keys.F6" in code
        assert "Game.Player.Character.Position = new Vector3(604.00f, 108.00f, 100.00f);" in code
        assert "UNRESOLVED" not in code
        assert code.count("{") == code.count("}")

    def test_fixture_passes_native_arity_check(self, tmp_path):
        code = CodeGenerator().generate(self._schema())
        cs_path = tmp_path / "Probe.3.cs"
        cs_path.write_text(code, encoding="utf-8")
        assert _find_arity_findings(str(cs_path)) == []
