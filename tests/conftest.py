"""
Shared pytest fixtures for the GTA5 + Unity pipeline test suites.
"""
import json
import tempfile
from pathlib import Path

import pytest

from src.mission_schema import (
    MissionAudio, MissionObject, MissionPed, MissionSchema, MissionStep, MissionVehicle,
)
from src.unity_schema import UnityPackage, UnitySchema, UnityScript


# ══════════════════════════════════════════════════════════════════════════════
# GTA5 fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def gta_minimal_schema() -> MissionSchema:
    """Smallest valid GTA schema: one go_to step, no entities."""
    return MissionSchema(
        mission_name="Test Mission",
        location_name="docks",
        time_of_day=12,
        weather="CLEAR",
        steps=[MissionStep(type="go_to", label="Head to the marker")],
        peds=[],
        vehicles=[],
        objects=[],
        audio=[],
    )


@pytest.fixture
def gta_full_schema() -> MissionSchema:
    """
    Full GTA schema with every entity type and step variant.
    All model IDs already resolved so codegen and unresolved checks pass.
    """
    return MissionSchema(
        mission_name="The Docks Heist",
        location_name="docks",
        time_of_day=22,
        weather="OVERCAST",
        steps=[
            MissionStep(type="go_to",          label="Head to the docks"),
            MissionStep(type="kill_all",        label="Eliminate the guards"),
            MissionStep(type="pickup",          label="Grab the package"),
            MissionStep(type="get_in_vehicle",  label="Get in the van"),
            MissionStep(type="deliver",         label="Deliver the goods"),
            MissionStep(type="escape",          label="Lose the cops"),
        ],
        peds=[
            MissionPed(description="cop",   model="s_m_y_cop_01",  role="guard",  count=3, weapon="pistol"),
            MissionPed(description="swat",  model="s_m_y_swat_01", role="patrol", count=2, weapon="rifle"),
        ],
        vehicles=[
            MissionVehicle(description="police car", model="police",  role="patrol", count=2),
            MissionVehicle(description="getaway van", model="burrito3", role="escape",  count=1),
        ],
        objects=[
            MissionObject(description="drug package", model="prop_drug_package_01", role="pickup"),
        ],
        audio=[
            MissionAudio(type="mission_passed", context="mission complete", name="FRANKLIN_BIG_01"),
            MissionAudio(type="mission_failed",  context="mission failed",   name="FRANKLIN_LOSE_1"),
        ],
    )


@pytest.fixture
def gta_minigame_schema() -> MissionSchema:
    """Smallest valid minigame-skeleton schema (menu->round->results loop).

    Mirrors tests/test_minigame_codegen.py's local `_minigame_schema()` helper
    as a shared fixture, so other test files (e.g. the CI-coverage smoke
    tests in test_outputs_smoke.py) don't need their own copy.
    """
    return MissionSchema(
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


@pytest.fixture
def gta_pipeline_config(tmp_path) -> dict:
    """Minimal pipeline config — output goes to tmp_path, fake service URLs."""
    return {
        "lm_studio": {
            "base_url":             "http://localhost:1234/v1",
            "model":                "test-model",
            "temperature":          0.7,
            "max_tokens":           8192,
            "timeout":              30.0,
            "concept_temperature":  0.85,
            "schema_temperature":   0.2,
        },
        "rag": {
            "fallback_to_local": True,
        },
        "pipeline": {
            "lookups_dir":   "lookups",
            "template_file": "templates/mission.cs.j2",
            "output_dir":    str(tmp_path),
        },
        "logging": {"show_prompts": False},
    }


@pytest.fixture
def gta_concept_text() -> str:
    """Realistic LLM concept response for a docks heist mission."""
    return (
        "A tense night mission set at the docks. Armed cops guard a drug shipment "
        "near the shipping containers. The player must eliminate the guards, grab "
        "the package, then escape in a getaway van before reinforcements arrive."
    )


@pytest.fixture
def gta_schema_json(tmp_path) -> str:
    """
    LLM schema-fill response as a JSON string.
    All descriptions match local lookup synonyms so RAG is never needed.
    """
    return json.dumps({
        "mission_name":  "The Docks Heist",
        "location_name": "docks",
        "time_of_day":   22,
        "weather":       "OVERCAST",
        "steps": [
            {"type": "go_to",   "label": "Head to the docks"},
            {"type": "kill_all","label": "Eliminate the guards"},
            {"type": "pickup",  "label": "Grab the package"},
            {"type": "escape",  "label": "Get out of there"},
        ],
        "peds": [
            {"description": "cop",  "role": "guard",  "count": 3, "weapon": "pistol"},
            {"description": "swat", "role": "patrol", "count": 2, "weapon": "rifle"},
        ],
        "vehicles": [
            {"description": "police car", "role": "patrol", "count": 1},
        ],
        "objects": [
            {"description": "drug package", "role": "pickup"},
        ],
        "audio": [
            {"type": "mission_passed", "context": "mission complete"},
        ],
    })


# ── Minimal schema ─────────────────────────────────────────────────────────────

@pytest.fixture
def minimal_schema() -> UnitySchema:
    """Absolute minimum: just a GameManager — no enemy, no camera, no packages."""
    return UnitySchema(
        project_name="MinimalProject",
        company_name="TestCo",
        game_type="3D",
        platform="PC",
        render_pipeline="Built-in",
        unity_version="6000.0",
        color_space="Linear",
        description="Minimal test project",
        features=[],
        scenes=["Main"],
        scripts=[UnityScript(name="GameManager", type="game_manager", description="GM")],
        packages=[],
    )


# ── Full 3D URP schema (has every script type, input system, cinemachine) ──────

@pytest.fixture
def full_3d_schema() -> UnitySchema:
    return UnitySchema(
        project_name="FullProject",
        company_name="TestCo",
        game_type="3D",
        platform="PC",
        render_pipeline="URP",
        unity_version="6000.0",
        color_space="Linear",
        description="Full 3D URP project with all script types",
        features=["input_system", "cinemachine", "navmesh"],
        scenes=["Main", "Menu", "Level2"],
        scripts=[
            UnityScript(name="GameManager",      type="game_manager",  description="GM"),
            UnityScript(name="PlayerController", type="player",        description="Player"),
            UnityScript(name="EnemyAI",          type="enemy",         description="Enemy"),
            UnityScript(name="HealthSystem",     type="health",        description="Health"),
            UnityScript(name="CameraFollow",     type="camera",        description="Camera"),
            UnityScript(name="UIManager",        type="ui_manager",    description="UI"),
            UnityScript(name="AudioManager",     type="audio_manager", description="Audio"),
        ],
        packages=[
            UnityPackage(
                description="urp",
                package_id="com.unity.render-pipelines.universal",
                version="17.0.3",
            ),
        ],
    )


# ── 2D schema ──────────────────────────────────────────────────────────────────

@pytest.fixture
def schema_2d() -> UnitySchema:
    return UnitySchema(
        project_name="PlatformerGame",
        company_name="TestCo",
        game_type="2D",
        platform="PC",
        render_pipeline="URP",
        unity_version="6000.0",
        color_space="Linear",
        description="2D platformer",
        features=["input_system"],
        scenes=["Main", "Menu"],
        scripts=[
            UnityScript(name="GameManager",      type="game_manager", description="GM"),
            UnityScript(name="PlayerController", type="player",       description="Player"),
            UnityScript(name="EnemyAI",          type="enemy",        description="Enemy"),
            UnityScript(name="HealthSystem",     type="health",       description="Health"),
        ],
        packages=[],
    )


# ── VR schema ──────────────────────────────────────────────────────────────────

@pytest.fixture
def schema_vr() -> UnitySchema:
    return UnitySchema(
        project_name="VRProject",
        company_name="TestCo",
        game_type="VR",
        platform="PC",
        render_pipeline="URP",
        unity_version="6000.0",
        color_space="Linear",
        description="VR project",
        features=["input_system"],
        scenes=["Main"],
        scripts=[
            UnityScript(name="GameManager",      type="game_manager", description="GM"),
            UnityScript(name="PlayerController", type="player",       description="Player"),
        ],
        packages=[],
    )


# ── HDRP schema ────────────────────────────────────────────────────────────────

@pytest.fixture
def schema_hdrp() -> UnitySchema:
    return UnitySchema(
        project_name="HDRPProject",
        company_name="TestCo",
        game_type="3D",
        platform="PC",
        render_pipeline="HDRP",
        unity_version="6000.0",
        color_space="Linear",
        description="HDRP project",
        features=[],
        scenes=["Main"],
        scripts=[
            UnityScript(name="GameManager",      type="game_manager", description="GM"),
            UnityScript(name="PlayerController", type="player",       description="Player"),
        ],
        packages=[],
    )


@pytest.fixture(autouse=True)
def _no_live_package_cache(monkeypatch):
    """No test may reach packages.unity.com or the real global package cache.

    The pipeline's package-cache hook runs inside `UnityPipeline.run()`, so
    without this every pipeline test would download the dependency closure from
    the live registry and copy it into the developer's own
    global Unity package cache. That is slow, needs network in CI, mutates
    machine-global state from a test run, and — measured — fills a disk.

    tests/test_unity_package_cache.py exercises the real functions directly
    against a fake registry and tmp_path roots, which is where that behaviour
    belongs.
    """
    def _refuse(url, timeout=60):
        raise AssertionError(
            f"test tried to reach the network: {url} — fake the registry instead")

    monkeypatch.setattr("src.unity_package_cache._http_get", _refuse)
    monkeypatch.setenv("UPM_CACHE_ROOT", str(Path(tempfile.gettempdir()) /
                                             "pytest-upm-cache-must-not-be-used"))
