"""Playability-artifact checks for the visual-quality/objective-guidance fixes:
post-processing, the baseline lighting rig, primitive-material harmonization,
and the waypoint marker. Each of these facts only exists in emitted FILES, not
the schema — see unity_playability_artifact.py's own module docstring for why
that split exists and src/unity_pipeline.py:2037 for where it now actually
gates a real build (evaluate_project, not the schema-only evaluate).
"""
from __future__ import annotations

import pytest

from src.unity_playability_artifact import evaluate_project
from src.unity_schema import MissionObjective, MissionSpec, UnitySchema, UnityScript


def _schema(*, render_pipeline="URP", locomotion="walk", missions=None, **kw):
    return UnitySchema(
        project_name="VisualTest", company_name="C", description="",
        game_type="3D", platform="pc", render_pipeline=render_pipeline,
        unity_version="6000.0", color_space="Linear",
        features=[], scenes=["Main"], packages=[], locomotion=locomotion,
        entities=[], scripts=[UnityScript(name="PlayerController", type="player",
                                          description="d")],
        missions=missions or [], meta={}, **kw)


def _mission_with_objective():
    return MissionSpec(id="m1", title="Round One", objectives=[
        MissionObjective(id="defeat1", type="eliminate",
                         description="Defeat the guard", target_ref="Guard", count=1),
    ])


# ── post-processing ──────────────────────────────────────────────────────────

class TestPostprocessingPresent:
    def test_ungraded_for_non_urp(self):
        rep = evaluate_project(_schema(render_pipeline="HDRP"), {})
        assert "postprocessing_present" not in rep.checks
        assert "postprocessing_present" in rep.not_graded

    def test_passes_when_profile_present(self):
        files = {"VisualTest/Assets/Settings/PostProcessProfile.asset": "..."}
        rep = evaluate_project(_schema(), files)
        assert rep.checks["postprocessing_present"] is True

    def test_fails_when_profile_missing(self):
        rep = evaluate_project(_schema(), {})
        assert rep.checks["postprocessing_present"] is False
        assert any(f.check == "postprocessing_present" for f in rep.findings)


# ── lighting rig ─────────────────────────────────────────────────────────────

class TestLightingRigPresent:
    def test_passes_when_lighting_director_present(self):
        files = {"VisualTest/Assets/Scripts/LightingDirector.cs": "..."}
        rep = evaluate_project(_schema(), files)
        assert rep.checks["lighting_rig_present"] is True

    def test_fails_when_missing(self):
        rep = evaluate_project(_schema(), {})
        assert rep.checks["lighting_rig_present"] is False
        assert any(f.check == "lighting_rig_present" for f in rep.findings)


# ── primitive material smoothness ────────────────────────────────────────────

class TestPrimitiveMaterialSmoothness:
    def test_ungraded_with_no_spawner(self):
        rep = evaluate_project(_schema(), {})
        assert "primitive_material_smoothness" not in rep.checks
        assert "primitive_material_smoothness" in rep.not_graded

    def test_passes_when_smoothness_is_set(self):
        files = {"VisualTest/Assets/Scripts/EntitySpawner.cs":
                'm.SetFloat("_Smoothness", 0.20f);'}
        rep = evaluate_project(_schema(), files)
        assert rep.checks["primitive_material_smoothness"] is True

    def test_fails_when_only_color_is_set(self):
        files = {"VisualTest/Assets/Scripts/EntitySpawner.cs": "m.color = color;"}
        rep = evaluate_project(_schema(), files)
        assert rep.checks["primitive_material_smoothness"] is False
        assert any(f.check == "primitive_material_smoothness" for f in rep.findings)


# ── objective marker (waypoint) ──────────────────────────────────────────────

class TestObjectiveMarkerPresent:
    def test_ungraded_for_ui_first_board_game(self):
        rep = evaluate_project(
            _schema(locomotion="none", missions=[_mission_with_objective()]), {})
        assert "objective_marker_present" not in rep.checks
        assert "objective_marker_present" in rep.not_graded

    def test_ungraded_with_no_mission_objectives(self):
        rep = evaluate_project(_schema(missions=[]), {})
        assert "objective_marker_present" not in rep.checks
        assert "objective_marker_present" in rep.not_graded

    def test_ungraded_without_compose(self):
        """No GameWiring.cs means the build didn't --compose — the waypoint
        rides the same compose-only wiring as the HUD's objective text."""
        rep = evaluate_project(
            _schema(missions=[_mission_with_objective()]), {})
        assert "objective_marker_present" not in rep.checks
        assert "objective_marker_present" in rep.not_graded

    def test_fails_when_composed_but_no_waypoint(self):
        files = {"VisualTest/Assets/Micros/wiring/Scripts/GameWiring.cs": "..."}
        rep = evaluate_project(
            _schema(missions=[_mission_with_objective()]), files)
        assert rep.checks["objective_marker_present"] is False
        assert any(f.check == "objective_marker_present" for f in rep.findings)

    def test_passes_when_composed_with_waypoint(self):
        files = {
            "VisualTest/Assets/Micros/wiring/Scripts/GameWiring.cs": "...",
            "VisualTest/Assets/Micros/waypoint/Scripts/Waypoint.cs": "...",
        }
        rep = evaluate_project(
            _schema(missions=[_mission_with_objective()]), files)
        assert rep.checks["objective_marker_present"] is True
