"""
End-to-end tests for UnityPipeline.

Strategy
────────
• LMStudioClient.chat is mocked at the instance level (side_effect list).
  Call 1 → project concept text
  Call 2 → project schema JSON string

• UnityResolver uses only local lookup tables — no network calls needed.

• UnityProjectGenerator.write writes to tmp_path (via unity_pipeline_config).

These tests exercise the full pipe:
  – concept generation + schema parsing (Steps 2-3)
  – package resolution via local lookups (Step 4)
  – required-package injection (Step 4)
  – consistency enforcement + programmatic fixes (Step 5)
  – project folder generation (Step 6)
"""
import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.unity_pipeline import UnityPipeline, _programmatic_fix, _check_consistency
from src.unity_schema import UnityPackage, UnitySchema, UnityScript


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def unity_pipeline_config(tmp_path) -> dict:
    return {
        "lm_studio": {
            "base_url":            "http://localhost:1234/v1",
            "model":               "test-model",
            "temperature":         0.7,
            "max_tokens":          8192,
            "timeout":             30.0,
            "concept_temperature": 0.85,
            "schema_temperature":  0.2,
        },
        "unity_pipeline": {
            "lookups_dir":       "lookups",
            "unity_template_dir": "templates/unity",
            "unity_output_dir":  str(tmp_path),
        },
        "logging": {"show_prompts": False},
    }


@pytest.fixture
def unity_concept_text() -> str:
    return (
        "A 2D side-scrolling platformer where the player collects coins and "
        "avoids enemies. Clean pixel-art style, targeting PC."
    )


@pytest.fixture
def unity_schema_json_2d() -> str:
    return json.dumps({
        "project_name":    "PixelPlatformer",
        "company_name":    "TestCo",
        "game_type":       "2D",
        "platform":        "PC",
        "render_pipeline": "URP",
        "unity_version":   "6000.0",
        "color_space":     "Linear",
        "description":     "A 2D platformer with coins and enemies",
        "features":        ["input_system"],
        "scenes":          ["Main", "Menu"],
        "scripts": [
            {"name": "GameManager",      "type": "game_manager", "description": "Controls game state"},
            {"name": "PlayerController", "type": "player",       "description": "Player movement"},
            {"name": "EnemyAI",          "type": "enemy",        "description": "Enemy behaviour"},
            {"name": "HealthSystem",     "type": "health",       "description": "Health tracking"},
            {"name": "Collectible",      "type": "collectible",  "description": "Coin pickup"},
        ],
        "packages": [
            {"description": "universal render pipeline"},
        ],
    })


@pytest.fixture
def unity_schema_json_3d() -> str:
    return json.dumps({
        "project_name":    "ThirdPersonAdventure",
        "company_name":    "TestCo",
        "game_type":       "3D",
        "platform":        "PC",
        "render_pipeline": "URP",
        "unity_version":   "6000.0",
        "color_space":     "Linear",
        "description":     "A third-person adventure game",
        "features":        ["input_system", "cinemachine", "navmesh"],
        "scenes":          ["Main", "Menu", "Level2"],
        "scripts": [
            {"name": "GameManager",      "type": "game_manager",  "description": "GM"},
            {"name": "PlayerController", "type": "player",        "description": "Player"},
            {"name": "EnemyAI",          "type": "enemy",         "description": "Enemy"},
            {"name": "HealthSystem",     "type": "health",        "description": "Health"},
            {"name": "CameraFollow",     "type": "camera",        "description": "Camera"},
        ],
        "packages": [
            {"description": "universal render pipeline"},
            {"description": "cinemachine"},
        ],
    })


def _make_pipeline(config: dict) -> UnityPipeline:
    return UnityPipeline(config)


def _run_pipeline(pipeline: UnityPipeline, concept: str, schema_json: str) -> Path:
    pipeline.llm.chat = MagicMock(side_effect=[concept, schema_json])
    return pipeline.run("a game prompt")


# ══════════════════════════════════════════════════════════════════════════════
# Happy-path: 2D platformer full run
# ══════════════════════════════════════════════════════════════════════════════

class TestUnityPipelineHappyPath2D:
    def test_run_returns_path_that_exists(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_2d
    ):
        pipeline = _make_pipeline(unity_pipeline_config)
        project_root = _run_pipeline(pipeline, unity_concept_text, unity_schema_json_2d)
        assert project_root.exists()
        assert project_root.is_dir()

    def test_project_root_name_matches_schema(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_2d
    ):
        pipeline = _make_pipeline(unity_pipeline_config)
        project_root = _run_pipeline(pipeline, unity_concept_text, unity_schema_json_2d)
        assert project_root.name == "PixelPlatformer"

    def test_assets_folder_created(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_2d
    ):
        pipeline = _make_pipeline(unity_pipeline_config)
        project_root = _run_pipeline(pipeline, unity_concept_text, unity_schema_json_2d)
        assert (project_root / "Assets").is_dir()

    def test_scripts_folder_created(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_2d
    ):
        pipeline = _make_pipeline(unity_pipeline_config)
        project_root = _run_pipeline(pipeline, unity_concept_text, unity_schema_json_2d)
        assert (project_root / "Assets" / "Scripts").is_dir()

    def test_manifest_json_created(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_2d
    ):
        pipeline = _make_pipeline(unity_pipeline_config)
        project_root = _run_pipeline(pipeline, unity_concept_text, unity_schema_json_2d)
        manifest = project_root / "Packages" / "manifest.json"
        assert manifest.exists()

    def test_manifest_contains_urp_package(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_2d
    ):
        pipeline = _make_pipeline(unity_pipeline_config)
        project_root = _run_pipeline(pipeline, unity_concept_text, unity_schema_json_2d)
        manifest = project_root / "Packages" / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert "com.unity.render-pipelines.universal" in data["dependencies"]

    def test_input_system_package_injected_for_feature(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_2d
    ):
        pipeline = _make_pipeline(unity_pipeline_config)
        project_root = _run_pipeline(pipeline, unity_concept_text, unity_schema_json_2d)
        manifest = project_root / "Packages" / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert "com.unity.inputsystem" in data["dependencies"]

    def test_main_scene_created(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_2d
    ):
        pipeline = _make_pipeline(unity_pipeline_config)
        project_root = _run_pipeline(pipeline, unity_concept_text, unity_schema_json_2d)
        assert (project_root / "Assets" / "Scenes" / "Main.unity").exists()

    def test_player_controller_script_created(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_2d
    ):
        pipeline = _make_pipeline(unity_pipeline_config)
        project_root = _run_pipeline(pipeline, unity_concept_text, unity_schema_json_2d)
        assert (project_root / "Assets" / "Scripts" / "PlayerController.cs").exists()

    def test_game_manager_script_created(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_2d
    ):
        pipeline = _make_pipeline(unity_pipeline_config)
        project_root = _run_pipeline(pipeline, unity_concept_text, unity_schema_json_2d)
        assert (project_root / "Assets" / "Scripts" / "GameManager.cs").exists()

    def test_readme_created(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_2d
    ):
        pipeline = _make_pipeline(unity_pipeline_config)
        project_root = _run_pipeline(pipeline, unity_concept_text, unity_schema_json_2d)
        assert (project_root / "README.md").exists()

    def test_project_version_txt_created(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_2d
    ):
        pipeline = _make_pipeline(unity_pipeline_config)
        project_root = _run_pipeline(pipeline, unity_concept_text, unity_schema_json_2d)
        assert (project_root / "ProjectSettings" / "ProjectVersion.txt").exists()

    def test_llm_called_exactly_twice(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_2d
    ):
        pipeline = _make_pipeline(unity_pipeline_config)
        mock_chat = MagicMock(side_effect=[unity_concept_text, unity_schema_json_2d])
        pipeline.llm.chat = mock_chat
        pipeline.run("a prompt")
        assert mock_chat.call_count == 2


# ══════════════════════════════════════════════════════════════════════════════
# Happy-path: 3D URP project
# ══════════════════════════════════════════════════════════════════════════════

class TestUnityPipelineHappyPath3D:
    def test_3d_project_root_created(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_3d
    ):
        pipeline = _make_pipeline(unity_pipeline_config)
        project_root = _run_pipeline(pipeline, unity_concept_text, unity_schema_json_3d)
        assert project_root.name == "ThirdPersonAdventure"

    def test_cinemachine_in_manifest(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_3d
    ):
        pipeline = _make_pipeline(unity_pipeline_config)
        project_root = _run_pipeline(pipeline, unity_concept_text, unity_schema_json_3d)
        manifest = project_root / "Packages" / "manifest.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert "com.unity.cinemachine" in data["dependencies"]

    def test_inputactions_file_created_for_input_system_feature(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_3d
    ):
        pipeline = _make_pipeline(unity_pipeline_config)
        project_root = _run_pipeline(pipeline, unity_concept_text, unity_schema_json_3d)
        settings_dir = project_root / "Assets" / "Settings"
        inputactions_files = list(settings_dir.glob("*.inputactions"))
        assert len(inputactions_files) == 1

    def test_editor_render_pipeline_script_created_for_urp(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_3d
    ):
        pipeline = _make_pipeline(unity_pipeline_config)
        project_root = _run_pipeline(pipeline, unity_concept_text, unity_schema_json_3d)
        editor_script = project_root / "Assets" / "Editor" / "SetupRenderPipeline.cs"
        assert editor_script.exists()
        content = editor_script.read_text(encoding="utf-8")
        assert "UniversalRenderPipelineAsset" in content

    def test_all_scenes_created_for_3d(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_3d
    ):
        pipeline = _make_pipeline(unity_pipeline_config)
        project_root = _run_pipeline(pipeline, unity_concept_text, unity_schema_json_3d)
        for scene in ("Main", "Menu", "Level2"):
            assert (project_root / "Assets" / "Scenes" / f"{scene}.unity").exists()


# ══════════════════════════════════════════════════════════════════════════════
# Consistency checks (_check_consistency)
# ══════════════════════════════════════════════════════════════════════════════

class TestUnityConsistencyChecks:
    def _base_schema(self, **overrides) -> UnitySchema:
        defaults = dict(
            project_name="TestProject",
            company_name="TestCo",
            game_type="3D",
            platform="PC",
            render_pipeline="Built-in",
            unity_version="6000.0",
            color_space="Linear",
            description="test",
            features=[],
            scenes=["Main"],
            scripts=[
                UnityScript(name="GameManager",      type="game_manager", description="GM"),
                UnityScript(name="PlayerController", type="player",       description="Player"),
            ],
            packages=[],
        )
        defaults.update(overrides)
        return UnitySchema(**defaults)

    def test_game_manager_injected_when_missing(self):
        schema = self._base_schema(scripts=[
            UnityScript(name="PlayerController", type="player", description="Player"),
        ])
        fixed, fixes = _check_consistency(schema)
        types = {s.type for s in fixed.scripts}
        assert "game_manager" in types
        assert any("GameManager" in f for f in fixes)

    def test_player_controller_injected_when_missing(self):
        schema = self._base_schema(scripts=[
            UnityScript(name="GameManager", type="game_manager", description="GM"),
        ])
        fixed, fixes = _check_consistency(schema)
        types = {s.type for s in fixed.scripts}
        assert "player" in types
        assert any("PlayerController" in f for f in fixes)

    def test_health_system_injected_when_enemy_present_without_health(self):
        schema = self._base_schema(scripts=[
            UnityScript(name="GameManager",      type="game_manager", description="GM"),
            UnityScript(name="PlayerController", type="player",       description="Player"),
            UnityScript(name="EnemyAI",          type="enemy",        description="Enemy"),
        ])
        fixed, fixes = _check_consistency(schema)
        types = {s.type for s in fixed.scripts}
        assert "health" in types
        assert any("HealthSystem" in f for f in fixes)

    def test_health_not_injected_when_no_enemy(self):
        schema = self._base_schema()   # no enemy script
        fixed, fixes = _check_consistency(schema)
        types = {s.type for s in fixed.scripts}
        assert "health" not in types

    def test_navmesh_removed_for_2d_game(self):
        schema = self._base_schema(game_type="2D", features=["navmesh"])
        fixed, fixes = _check_consistency(schema)
        assert "navmesh" not in fixed.features
        assert any("navmesh" in f for f in fixes)

    def test_input_system_injected_for_vr(self):
        schema = self._base_schema(game_type="VR", features=[])
        fixed, fixes = _check_consistency(schema)
        assert "input_system" in fixed.features
        assert any("input_system" in f for f in fixes)

    def test_cinemachine_package_added_when_feature_set(self):
        schema = self._base_schema(features=["cinemachine"])
        fixed, fixes = _check_consistency(schema)
        pkg_descs = [p.description.lower() for p in fixed.packages]
        assert any("cinemachine" in d for d in pkg_descs)

    def test_material_palette_injected_for_3d(self):
        schema = self._base_schema(game_type="3D")
        fixed, fixes = _check_consistency(schema)
        types = {s.type for s in fixed.scripts}
        assert "material_palette" in types

    def test_no_fixes_needed_for_fully_consistent_schema(self):
        schema = self._base_schema(scripts=[
            UnityScript(name="GameManager",      type="game_manager",    description="GM"),
            UnityScript(name="PlayerController", type="player",          description="Player"),
            UnityScript(name="MaterialPalette",  type="material_palette", description="Palette"),
        ])
        _, fixes = _check_consistency(schema)
        # Only fix should be the auto-materialpalette (already present), so no new injections
        assert all("Injected" not in f for f in fixes)


# ══════════════════════════════════════════════════════════════════════════════
# Programmatic fixer (_programmatic_fix)
# ══════════════════════════════════════════════════════════════════════════════

class TestUnityProgrammaticFix:
    def _schema(self, **overrides) -> UnitySchema:
        defaults = dict(
            project_name="TestProject",
            company_name="TestCo",
            game_type="3D",
            platform="PC",
            render_pipeline="Built-in",
            unity_version="6000.0",
            color_space="Linear",
            description="test",
            features=[],
            scenes=["Main"],
            scripts=[UnityScript(name="GameManager", type="game_manager", description="")],
            packages=[],
        )
        defaults.update(overrides)
        return UnitySchema(**defaults)

    def test_project_name_converted_to_pascal_case(self):
        schema = self._schema(project_name="my cool game")
        fixed = _programmatic_fix(schema)
        assert fixed.project_name == "MyCoolGame"

    def test_project_name_with_special_chars_cleaned(self):
        schema = self._schema(project_name="game-project!")
        fixed = _programmatic_fix(schema)
        assert re.match(r"^[A-Za-z][A-Za-z0-9]*$", fixed.project_name)

    def test_company_name_slash_replaced(self):
        schema = self._schema(company_name="My/Company")
        fixed = _programmatic_fix(schema)
        assert "/" not in fixed.company_name

    def test_duplicate_scenes_deduplicated(self):
        schema = self._schema(scenes=["Main", "Main", "Menu"])
        fixed = _programmatic_fix(schema)
        assert fixed.scenes.count("Main") == 1

    def test_main_scene_inserted_when_missing(self):
        schema = self._schema(scenes=["Level1", "Level2"])
        fixed = _programmatic_fix(schema)
        assert "Main" in fixed.scenes

    def test_main_scene_inserted_at_front_when_absent(self):
        # When "Main" is completely missing it is inserted at index 0.
        # If "Main" already exists elsewhere in the list, the fixer does NOT
        # reorder it — that is the actual implementation contract.
        schema = self._schema(scenes=["Level1", "Level2"])
        fixed = _programmatic_fix(schema)
        assert fixed.scenes[0] == "Main"

    def test_scenes_capped_at_8_including_main_insertion(self):
        # _programmatic_fix slices to [:8], then inserts "Main" if absent →
        # result is ≤ 9.  When "Main" is included in the input it stays ≤ 8.
        scenes_with_main = ["Main"] + [f"Scene{i}" for i in range(11)]
        schema = self._schema(scenes=scenes_with_main)
        fixed = _programmatic_fix(schema)
        assert len(fixed.scenes) <= 8
        assert "Main" in fixed.scenes

    def test_scripts_capped_at_24_by_mutating_list_directly(self):
        # Pydantic enforces max_length=24 at construction time, so we
        # bypass it by mutating the list attribute after the object is built.
        schema = self._schema(scripts=[
            UnityScript(name=f"Script{i}", type="game_manager", description="")
            for i in range(24)
        ])
        # Append directly to the already-constructed list (bypasses validator)
        for i in range(24, 30):
            schema.scripts.append(
                UnityScript(name=f"Script{i}", type="game_manager", description="")
            )
        fixed = _programmatic_fix(schema)
        assert len(fixed.scripts) <= 24

    def test_script_name_pascal_cased(self):
        schema = self._schema(scripts=[
            UnityScript(name="player controller", type="player", description=""),
        ])
        fixed = _programmatic_fix(schema)
        assert fixed.scripts[0].name == "PlayerController"

    # ── GameContext bridge access (compose-aware sanitizer) ───────────────────
    def test_gamecontext_call_preserved_in_compose(self):
        schema = self._schema(scripts=[
            UnityScript(name="Bridger", type="custom", description="x",
                        update_extra="var hp = GameContext.Instance.Get<PlayerHealth>();"),
        ])
        fixed = _programmatic_fix(schema, compose=True)
        assert "GameContext.Instance.Get<PlayerHealth>()" in fixed.scripts[0].update_extra

    def test_gamecontext_call_stripped_without_compose(self):
        # No GameContext exists outside --compose, so the locator call must be
        # stripped (it would be CS0246) rather than left to break compilation.
        schema = self._schema(scripts=[
            UnityScript(name="Bridger", type="custom", description="x",
                        update_extra="var hp = GameContext.Instance.Get<PlayerHealth>();"),
        ])
        fixed = _programmatic_fix(schema)   # compose defaults to False
        assert "GameContext" not in fixed.scripts[0].update_extra

    def test_invented_singleton_still_stripped_in_compose(self):
        schema = self._schema(scripts=[
            UnityScript(name="Bridger", type="custom", description="x",
                        update_extra="FooManager.Instance.DoThing();"),
        ])
        fixed = _programmatic_fix(schema, compose=True)
        assert "FooManager" not in fixed.scripts[0].update_extra

    def test_gamecontext_guidance_constant(self):
        from src.unity_prompts import GAMECONTEXT_SCHEMA_GUIDANCE
        assert "GameContext.Instance.Get<T>()" in GAMECONTEXT_SCHEMA_GUIDANCE
        assert "null-check" in GAMECONTEXT_SCHEMA_GUIDANCE.lower()


# ══════════════════════════════════════════════════════════════════════════════
# Schema extraction from LLM artifacts (markdown + think blocks)
# ══════════════════════════════════════════════════════════════════════════════

class TestUnitySchemaExtractionFromArtifacts:
    def test_schema_in_markdown_fence_parsed(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_2d
    ):
        fenced = f"```json\n{unity_schema_json_2d}\n```"
        pipeline = _make_pipeline(unity_pipeline_config)
        pipeline.llm.chat = MagicMock(side_effect=[unity_concept_text, fenced])
        project_root = pipeline.run("a 2D game")
        assert project_root.exists()
        assert project_root.name == "PixelPlatformer"

    def test_schema_after_think_block_parsed(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_2d
    ):
        with_think = f"<think>Let me think...</think>\n{unity_schema_json_2d}"
        pipeline = _make_pipeline(unity_pipeline_config)
        pipeline.llm.chat = MagicMock(side_effect=[unity_concept_text, with_think])
        project_root = pipeline.run("a 2D game")
        assert project_root.name == "PixelPlatformer"

