"""Tests for src/gta5_compile_check.py — the real ScriptHookVDotNet3.dll
compile gate `unity_cli.py gta-verify`/`e2e-verify` prefer when the Roslyn
toolchain is available.

Roslyn's own diagnostics can't catch a Function.Call(Hash.X, ...) with the
wrong argument count (InputArgument[] is variadic — it compiles clean
regardless of count and only fails at runtime), so compile_check() folds in
its own "ARITY" findings mined from lookups/gta_native_arity.json. The
`_find_arity_findings` tests below run everywhere (pure text scan, no
toolchain). The full `compile_check()` integration tests need the real SDK +
.NET Framework + vendored DLL and are skipped without them.
"""

import os
import tempfile

import pytest

from src.code_generator import CodeGenerator
from src.gta5_compile_check import _find_arity_findings, compile_check, gate_available
from src.mission_schema import MissionPed, MissionSchema, MissionStep, MissionVehicle

_TOOLCHAIN_AVAILABLE, _TOOLCHAIN_INFO = gate_available()
_needs_toolchain = pytest.mark.skipif(
    not _TOOLCHAIN_AVAILABLE,
    reason=f"real SHVDN3 compile toolchain unavailable: {_TOOLCHAIN_INFO}")


def _write_cs(tmp_path, body: str) -> str:
    f = tmp_path / "Probe.cs"
    f.write_text(
        "using GTA;\nusing GTA.Native;\n\n"
        "public class ProbeScript : Script\n{\n"
        f"    private void DoThing()\n    {{\n{body}\n    }}\n}}\n",
        encoding="utf-8",
    )
    return str(f)


def _patrol_behavior_schema() -> MissionSchema:
    return MissionSchema(
        mission_name="Patrol Behavior Probe",
        location_name="docks",
        time_of_day=20,
        weather="CLEAR",
        steps=[MissionStep(type="go_to", label="Reach the route")],
        peds=[MissionPed(
            description="dock sentry", model="s_m_y_dockwork_01",
            role="patrol", weapon="rifle",
        )],
        vehicles=[],
        objects=[],
        audio=[],
        meta={"editor": {
            "triggers": [{"id": "dock_trigger", "pos": [-995.0, -2670.0, 13.8],
                           "radius": 5, "linked_step": None}],
            "paths": [{
            "id": "behavior_probe_route",
            "ped_idx": 0,
            "mode": "loop",
            "speed": 2.5,
            "posture": "cautious",
            "weapon_state": "holstered",
            "look_at_type": "trigger",
            "look_at": "dock_trigger",
            "scan_heading": 90,
            "scan_arc": 120,
            "scan_duration": 3,
            "points": [[-1030.0, -2710.0, 13.8], [-1000.0, -2685.0, 13.8], [-970.0, -2710.0, 13.8]],
            "waypoints": [
                {"behavior": "walk", "movement": "walk", "speed": 1.0,
                 "posture": "normal", "weapon_state": "drawn",
                 "look_at_type": "coord", "look_at": "-1030,-2710,13.8"},
                {"behavior": "scan", "movement": "jog", "wait_time": 2,
                 "posture": "cautious", "weapon_state": "holstered",
                 "look_at_type": "trigger", "look_at": "dock_trigger",
                 "scan_heading": 180, "scan_arc": 90, "scan_duration": 2},
                {"behavior": "guard", "movement": "run", "wait_time": 1,
                 "guard_until_alert": True, "posture": "aggressive",
                 "weapon_state": "drawn", "look_at_type": "coord",
                 "look_at": [-970.0, -2710.0, 13.8]},
            ],
        }]}}
    )


def _patrol_stress_schema() -> MissionSchema:
    return MissionSchema(
        mission_name="Patrol Stress Probe",
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


# ── _find_arity_findings — pure text scan, runs everywhere, real lookups ─────
# Uses the REAL lookups/gta_native_arity.json (via load_native_arity() inside
# the function), not a mock — PLAY_SOUND_FRONTEND mines to a consistent arity
# of 4 across 3,345 real decompiled call sites.

def test_find_arity_findings_catches_wrong_count(tmp_path):
    path = _write_cs(tmp_path, '        Function.Call(Hash.PLAY_SOUND_FRONTEND, -1, "X");')
    findings = _find_arity_findings(path)
    assert len(findings) == 1
    f = findings[0]
    assert f["code"] == "ARITY"
    assert f["sev"] == "error"
    assert "PLAY_SOUND_FRONTEND" in f["msg"]
    assert "passes 2 argument(s)" in f["msg"]
    assert "mines to 4" in f["msg"]


def test_find_arity_findings_ignores_correct_count(tmp_path):
    path = _write_cs(
        tmp_path,
        '        Function.Call(Hash.PLAY_SOUND_FRONTEND, -1, "X", "Y", true);')
    assert _find_arity_findings(path) == []


def test_find_arity_findings_ignores_unknown_native(tmp_path):
    path = _write_cs(tmp_path, "        Function.Call(Hash.SOME_INVENTED_NATIVE, 1, 2);")
    assert _find_arity_findings(path) == []


def test_find_arity_findings_returns_empty_for_missing_file(tmp_path):
    assert _find_arity_findings(str(tmp_path / "does_not_exist.cs")) == []


def test_generated_patrol_behavior_native_arities_are_clean(tmp_path):
    code = CodeGenerator("templates/mission.cs.j2").generate(_patrol_behavior_schema())
    path = tmp_path / "PatrolBehaviorProbe.3.cs"
    path.write_text(code, encoding="utf-8")
    assert _find_arity_findings(str(path)) == []


def test_generated_patrol_stress_native_arities_are_clean(tmp_path):
    code = CodeGenerator("templates/mission.cs.j2").generate(_patrol_stress_schema())
    path = tmp_path / "PatrolStressProbe.3.cs"
    path.write_text(code, encoding="utf-8")
    assert _find_arity_findings(str(path)) == []


# ── compile_check() — full Roslyn integration, needs the real toolchain ─────

@_needs_toolchain
def test_compile_check_fails_on_wrong_arity_despite_clean_syntax(tmp_path):
    """The live bug this closes: InputArgument[] is variadic, so csc alone
    reports 0 errors on a truncated Function.Call — this must still fail the
    gate via the synthesized ARITY finding."""
    path = _write_cs(tmp_path, '        Function.Call(Hash.PLAY_SOUND_FRONTEND, -1, "X");')
    ok, findings, info = compile_check(path)
    assert ok is False
    codes = [f["code"] for f in findings]
    assert "ARITY" in codes
    arity_finding = next(f for f in findings if f["code"] == "ARITY")
    assert arity_finding["guidance"]   # _enrich() fills this from _CS_GUIDANCE["ARITY"]
    assert arity_finding["fix"] is None


@_needs_toolchain
def test_compile_check_passes_correct_arity_call(tmp_path):
    path = _write_cs(
        tmp_path,
        '        Function.Call(Hash.PLAY_SOUND_FRONTEND, -1, "X", "Y", true);')
    ok, findings, info = compile_check(path)
    assert ok is True
    assert findings == []


@_needs_toolchain
def test_generated_patrol_movement_native_task_compiles(tmp_path):
    schema = MissionSchema(
        mission_name="Patrol Movement Probe",
        location_name="docks",
        time_of_day=20,
        weather="CLEAR",
        steps=[MissionStep(type="go_to", label="Reach the route")],
        peds=[MissionPed(description="dock sentry", model="s_m_y_dockwork_01", role="patrol")],
        vehicles=[],
        objects=[],
        audio=[],
        meta={"editor": {"paths": [{
            "id": "movement_probe_route",
            "ped_idx": 0,
            "mode": "ping_pong",
            "speed": 2.5,
            "points": [[-1030.0, -2710.0, 13.8], [-1000.0, -2685.0, 13.8], [-970.0, -2710.0, 13.8]],
            "waypoints": [{"speed": 1.0}, {"speed": 2.5}, {"speed": 3.5}],
        }]}},
    )
    code = CodeGenerator("templates/mission.cs.j2").generate(schema)
    path = tmp_path / "PatrolMovementProbe.3.cs"
    path.write_text(code, encoding="utf-8")
    ok, findings, info = compile_check(str(path))
    assert ok is True
    assert findings == []


@_needs_toolchain
def test_generated_patrol_behavior_native_tasks_compile(tmp_path):
    code = CodeGenerator("templates/mission.cs.j2").generate(_patrol_behavior_schema())
    path = tmp_path / "PatrolBehaviorProbe.3.cs"
    path.write_text(code, encoding="utf-8")
    ok, findings, info = compile_check(str(path))
    assert ok is True
    assert findings == []


@_needs_toolchain
def test_generated_patrol_stress_native_tasks_compile(tmp_path):
    code = CodeGenerator("templates/mission.cs.j2").generate(_patrol_stress_schema())
    path = tmp_path / "PatrolStressProbe.3.cs"
    path.write_text(code, encoding="utf-8")
    ok, findings, info = compile_check(str(path))
    assert ok is True
    assert findings == []
