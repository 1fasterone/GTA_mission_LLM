"""Waypoint micro-generator — the screen-edge marker for the active objective.

Before this, objective progress only ever surfaced as flat TEXT (the HUD
readout / rules hint bar); nothing gave the player a spatial sense of where
to go. Every test below checks either the standalone script (renders, parses
as real C#) or the compose-time wiring that points it at the same objective
the HUD text names.
"""

from pathlib import Path

import pytest

from src.csharp_syntax_gate import check_files
from src.micro import REGISTRY
from src.micro.waypoint import VARIANTS, WaypointPipeline, WaypointSchema
from src.unity_compose import MicroComposer, subsystems_for
from src.unity_schema import MissionObjective, MissionSpec

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg():
    return {
        "lm_studio":      {"base_url": "http://localhost:1234/v1", "model": "test"},
        "unity_pipeline": {
            "unity_output_dir":   str(REPO_ROOT / "output" / "unity"),
            "unity_template_dir": str(REPO_ROOT / "templates" / "unity"),
        },
    }


@pytest.fixture
def pipeline(cfg):
    return WaypointPipeline(cfg)


def _mission(**kw):
    kw.setdefault("id", "m1")
    kw.setdefault("title", "Round One")
    kw.setdefault("objectives", [
        MissionObjective(id="defeat1", type="eliminate",
                         description="Defeat the guard", target_ref="Guard", count=1),
    ])
    return MissionSpec(**kw)


# ── Registry / metadata ──────────────────────────────────────────────────────

def test_waypoint_is_registered():
    assert REGISTRY["waypoint"] is WaypointPipeline


def test_only_one_variant():
    assert VARIANTS == ["edge_arrow"]


# ── Standalone script ────────────────────────────────────────────────────────

def test_default_fix_names_the_class(pipeline):
    schema = pipeline.default_fix(WaypointSchema(name="my waypoint"), "edge_arrow")
    assert schema.name == "MyWaypoint"


def test_generated_script_parses_as_real_csharp(pipeline, tmp_path):
    schema = pipeline.default_fix(WaypointSchema(), "edge_arrow")
    pipeline.write_files(schema, "edge_arrow", tmp_path)
    cs_path = tmp_path / "Scripts" / f"{schema.name}.cs"
    assert cs_path.is_file()
    result = check_files([cs_path])
    assert not result.diagnostics, result.diagnostics


def test_generated_script_has_the_public_api(pipeline, tmp_path):
    schema = pipeline.default_fix(WaypointSchema(), "edge_arrow")
    pipeline.write_files(schema, "edge_arrow", tmp_path)
    cs = (tmp_path / "Scripts" / f"{schema.name}.cs").read_text(encoding="utf-8")
    assert "public void SetTarget(string entityName)" in cs
    # The established singleton-naming convention (SEAM 8b) — no new registry.
    assert '"_0"' in cs
    assert "private void LateUpdate()" in cs


def test_unknown_variant_clamps_to_default():
    schema = WaypointSchema(variant="bogus")
    assert schema.variant == "edge_arrow"


# ── subsystems_for gating ────────────────────────────────────────────────────

class TestSubsystemGating:
    def test_spatial_game_with_objectives_gets_a_waypoint(self):
        subs = subsystems_for("3D", [], missions=[_mission()])
        kinds = [s.kind for s in subs]
        assert kinds.count("waypoint") == 1

    def test_no_missions_means_no_waypoint(self):
        subs = subsystems_for("3D", [], missions=[])
        assert "waypoint" not in {s.kind for s in subs}

    def test_mission_with_no_objectives_means_no_waypoint(self):
        subs = subsystems_for("3D", [], missions=[_mission(objectives=[])])
        assert "waypoint" not in {s.kind for s in subs}

    def test_ui_first_board_game_gets_no_waypoint(self):
        """A board's objectives bind to board events, not a world Transform —
        nothing in 3D space to point at."""
        subs = subsystems_for("3D", [], locomotion="none", missions=[_mission()])
        assert "waypoint" not in {s.kind for s in subs}

    def test_multiple_missions_still_get_one_waypoint(self):
        subs = subsystems_for("3D", [], missions=[
            _mission(id="m1", title="Round One"),
            _mission(id="m2", title="Round Two",
                    objectives=[MissionObjective(id="survive1", type="survive",
                                                 description="Survive", count=60)]),
        ])
        assert [s.kind for s in subs].count("waypoint") == 1


# ── Full compose wiring ──────────────────────────────────────────────────────

class TestComposeWiring:
    def test_waypoint_script_is_emitted(self, tmp_path):
        subs = subsystems_for("3D", [], missions=[_mission()])
        units = MicroComposer("WPTest").emit(subs, tmp_path / "Assets")
        wp = next(u for u in units if u.kind == "waypoint")
        assert wp.class_name == "Waypoint"
        assert (tmp_path / "Assets" / "Micros" / "waypoint" / "Scripts" / "Waypoint.cs").is_file()

    def test_composed_tree_parses_as_real_csharp(self, tmp_path):
        subs = subsystems_for("3D", [], missions=[_mission()])
        MicroComposer("WPTest").emit(subs, tmp_path / "Assets")
        cs_files = list((tmp_path / "Assets").rglob("*.cs"))
        assert cs_files
        result = check_files(cs_files)
        assert not result.diagnostics, result.diagnostics

    def test_game_wiring_forwards_the_same_target_ref_the_hud_reads(self, tmp_path):
        subs = subsystems_for("3D", [], missions=[_mission()])
        MicroComposer("WPTest").emit(subs, tmp_path / "Assets")
        wiring_cs = (tmp_path / "Assets" / "Micros" / "wiring" / "Scripts" / "GameWiring.cs")
        text = wiring_cs.read_text(encoding="utf-8")
        assert "string targetRef" in text
        assert "targetRef = a0.targetRef" in text
        assert "_waypoint.SetTarget(targetRef)" in text
        # Guarded the same way every other auto-found reference is.
        assert "if (_waypoint != null) _waypoint.SetTarget(targetRef);" in text

    def test_no_waypoint_composed_means_no_settarget_call(self, tmp_path):
        """locomotion='none' drops the waypoint subsystem — GameWiring must not
        reference a `_waypoint` field that was never declared (CS0103)."""
        subs = subsystems_for("3D", [], locomotion="none", missions=[_mission()])
        MicroComposer("WPTest").emit(subs, tmp_path / "Assets")
        wiring_cs = (tmp_path / "Assets" / "Micros" / "wiring" / "Scripts" / "GameWiring.cs")
        text = wiring_cs.read_text(encoding="utf-8")
        assert "SetTarget" not in text
        assert "_waypoint" not in text

    def test_output_is_deterministic(self, tmp_path):
        subs = subsystems_for("3D", [], missions=[_mission()])
        first  = MicroComposer("WPTest").emit(subs, tmp_path / "Assets1")
        second = MicroComposer("WPTest").emit(subs, tmp_path / "Assets2")
        cs1 = (tmp_path / "Assets1" / "Micros" / "waypoint" / "Scripts" / "Waypoint.cs").read_text(encoding="utf-8")
        cs2 = (tmp_path / "Assets2" / "Micros" / "waypoint" / "Scripts" / "Waypoint.cs").read_text(encoding="utf-8")
        assert cs1 == cs2
