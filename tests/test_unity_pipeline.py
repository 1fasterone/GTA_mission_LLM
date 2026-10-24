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

from src.unity_pipeline import (UnityPipeline, _programmatic_fix, _check_consistency,
                                _infer_mission_scenes, _objective_scene_map,
                                _reconcile_scene_specs)
from src.unity_reference_image import ReferenceImageError
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
    def test_reference_image_reaches_concept_only(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_2d, tmp_path
    ):
        image = tmp_path / "racing-reference.png"
        image.write_bytes(b"png-bytes")
        pipeline = _make_pipeline(unity_pipeline_config)
        mock_chat = MagicMock(side_effect=[unity_concept_text, unity_schema_json_2d])
        pipeline.llm.chat = mock_chat

        pipeline.run("a racing game", reference_image=image)

        concept_call, schema_call = mock_chat.call_args_list
        assert concept_call.kwargs["images"] == [
            "data:image/png;base64,cG5nLWJ5dGVz"
        ]
        assert "REFERENCE IMAGE" in concept_call.kwargs["user"]
        assert schema_call.kwargs.get("images") is None
        assert "REFERENCE IMAGE" not in schema_call.kwargs["user"]

    def test_visual_context_guides_step_2_and_writes_project_context(
        self, unity_pipeline_config, unity_concept_text, unity_schema_json_2d, tmp_path
    ):
        image = tmp_path / "basement.png"
        image.write_bytes(b"png-bytes")
        visual_context = (
            "# Visual Scene Context\n\n"
            "- robot | category=character | image_query=industrial robot\n"
        )
        schema_data = json.loads(unity_schema_json_2d)
        schema_data["entities"] = [{
            "name": "RobotEnemies",
            "kind": "enemy",
            "model_hint": "industrial robot",
            "sketchfab_query": "industrial robot enemy",
        }]
        pipeline = _make_pipeline(unity_pipeline_config)
        mock_chat = MagicMock(side_effect=[unity_concept_text, json.dumps(schema_data)])
        pipeline.llm.chat = mock_chat

        project_root = pipeline.run(
            "an FPS with robot enemies in a bright basement",
            reference_image=image,
            visual_context=visual_context,
        )

        concept_call, schema_call = mock_chat.call_args_list
        assert "VISUAL ASSET CONTEXT" in concept_call.kwargs["user"]
        assert "industrial robot" in concept_call.kwargs["user"]
        assert "VISUAL ASSET CONTEXT" not in schema_call.kwargs["user"]
        written = (project_root / "context.txt").read_text(encoding="utf-8")
        assert "industrial robot" in written
        assert "query=industrial robot enemy" in written
        assert "## Step 3 Sketchfab searches" in written
        assert "## Frozen Unity model bindings" in written

    def test_reference_image_is_invalid_for_no_llm_mode(
        self, unity_pipeline_config, tmp_path
    ):
        image = tmp_path / "racing-reference.png"
        image.write_bytes(b"png-bytes")
        pipeline = _make_pipeline(unity_pipeline_config)

        with pytest.raises(ReferenceImageError, match="requires the Concept LLM stage"):
            pipeline.run("a structured brief", no_llm=True, reference_image=image)

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
# Racing-genre track scaffold (end-to-end): DriftArcade regression
# ══════════════════════════════════════════════════════════════════════════════

class TestUnityPipelineRacingScaffold:
    """A racing-genre prompt whose LLM-authored schema has NO vehicle/track
    entities (the exact DriftArcade failure mode) must come out of the
    pipeline with a real closed circuit, not a themed prop scatter or a
    strict-mode hard-fail."""

    @pytest.fixture
    def racing_schema_json_degenerate(self) -> str:
        return json.dumps({
            "project_name":    "DriftArcade",
            "company_name":    "TestCo",
            "game_type":       "3D",
            "platform":        "PC",
            "render_pipeline": "URP",
            "unity_version":   "6000.2",
            "color_space":     "Linear",
            "description":     "A neon drift racing arcade game",
            "features":        ["input_system"],
            "scenes":          ["Main"],
            "scripts": [
                {"name": "GameManager",      "type": "game_manager", "description": "GM"},
                {"name": "PlayerController", "type": "player",       "description": "Player"},
            ],
            "packages": [{"description": "universal render pipeline"}],
            # Degenerate on purpose: scenery only, no vehicle/road/track kind —
            # this is what the composer must rescue.
            "entities": [
                {"name": "Grandstands", "kind": "building", "count": 8,
                 "placement": "ring", "model_hint": "grandstand structure"},
            ],
        })

    def _run_racing(self, config, concept, schema_json):
        pipeline = _make_pipeline(config)
        pipeline.llm.chat = MagicMock(side_effect=[concept, schema_json])
        return pipeline.run("DriftArcade: a neon drift racing arcade game "
                            "with tight corners and rival racers")

    def test_track_and_vehicle_entities_injected(
        self, unity_pipeline_config, racing_schema_json_degenerate
    ):
        project_root = self._run_racing(
            unity_pipeline_config, "concept text", racing_schema_json_degenerate)
        cs = (project_root / "Assets" / "Scripts" / "EntitySpawner.cs").read_text(
            encoding="utf-8")
        for name in ("TrackSurface", "OpponentKarts",
                     "TrackBarriersOuter", "ApexCones", "CheckpointBanners"):
            assert f'"{name}"' in cs, f"{name} missing from generated EntitySpawner.cs"
        # PlayerKart is NOT spawned as a static prop: with a player script in
        # the schema it merges into the playable Player object, which takes
        # the pole position + race-direction facing. After P1 normalization
        # (src/unity_pathnorm.py) the start line IS the world origin heading
        # +Z, so the pole position is exactly (0, 0) at capsule height.
        assert 'SpawnGroup("PlayerKart"' not in cs
        scene_yaml = (project_root / "Assets" / "Scenes" / "Main.unity").read_text(
            encoding="utf-8")
        assert "m_LocalPosition: {x: 0.0, y: 1.0, z: 0.0}" in scene_yaml

    def test_existing_grandstands_entity_preserved(
        self, unity_pipeline_config, racing_schema_json_degenerate
    ):
        project_root = self._run_racing(
            unity_pipeline_config, "concept text", racing_schema_json_degenerate)
        cs = (project_root / "Assets" / "Scripts" / "EntitySpawner.cs").read_text(
            encoding="utf-8")
        assert '"Grandstands"' in cs

    def test_project_builds_without_strict_asset_failure(
        self, unity_pipeline_config, racing_schema_json_degenerate
    ):
        # The whole point: this must not raise — a racing project with no
        # real vehicle/track content used to either hard-fail (strict) or
        # silently resolve to unrelated assets (an airplane).
        project_root = self._run_racing(
            unity_pipeline_config, "concept text", racing_schema_json_degenerate)
        assert project_root.exists()

    def test_explicit_route_overrides_parametric_oval(
        self, unity_pipeline_config, racing_schema_json_degenerate
    ):
        # unity-create --route: the circuit is composed around the authored
        # course (already P1-normalized by unity-path) instead of the oval,
        # and the playable Player still merges to the start line = origin.
        from src.unity_pathinput import build_route
        route = build_route(
            [[0, 0, 0], [0, 0, 90], [70, 0, 110], [110, 0, 60],
             [60, 0, -10], [0, 0, -4]], width=12.0)
        pipeline = _make_pipeline(unity_pipeline_config)
        pipeline.llm.chat = MagicMock(
            side_effect=["concept text", racing_schema_json_degenerate])
        project_root = pipeline.run(
            "DriftArcade: a neon drift racing arcade game", route=route)
        cs = (project_root / "Assets" / "Scripts" / "EntitySpawner.cs").read_text(
            encoding="utf-8")
        assert '"TrackSurface"' in cs
        assert "12f" in cs or "12.0f" in cs   # custom path_width reached codegen
        scene_yaml = (project_root / "Assets" / "Scenes" / "Main.unity").read_text(
            encoding="utf-8")
        assert "m_LocalPosition: {x: 0.0, y: 1.0, z: 0.0}" in scene_yaml


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

    def test_scene_specs_reconciled_and_mood_inferred(self):
        from src.unity_schema import EntitySpec, SceneSpec
        schema = self._base_schema(
            scenes=["Main", "StormShelf", "FinalApproach"],
            entities=[EntitySpec(name="Drones", kind="enemy", count=4)],
            scene_specs=[
                SceneSpec(name="StormShelf",
                          description="thunderheads, lightning arcs, boss drone",
                          entities=["Drones", "GhostGroup"]),
                SceneSpec(name="FinalApproach",
                          description="low-altitude descent at dusk, wind shear"),
                SceneSpec(name="NoSuchScene", description="orphan"),
                SceneSpec(name="StormShelf", description="duplicate — dropped"),
            ])
        fixed, fixes = _check_consistency(schema)
        by_name = {sp.name: sp for sp in fixed.scene_specs}
        assert set(by_name) == {"StormShelf", "FinalApproach"}
        assert by_name["StormShelf"].entities == ["Drones"]      # unknown dropped
        assert by_name["StormShelf"].fog is True                 # "thunderheads"
        assert by_name["FinalApproach"].time_of_day == "sunset"  # "dusk"
        assert any("NoSuchScene" in f for f in fixes)
        assert any("GhostGroup" in f for f in fixes)

    def test_unique_gameplay_scene_restores_raw_projectile_orphans(self):
        from src.scene_graph import build_scene_graph
        from src.unity_projectile_contract import apply_physical_projectile_contract
        from src.unity_schema import EntitySpec, SceneSpec

        schema = self._base_schema(
            scenes=["Main", "MainMenu"],
            entities=[
                EntitySpec(name="Player", kind="player"),
                EntitySpec(name="Targets", kind="enemy", count=3),
                EntitySpec(name="Bullets", kind="prop", count=8),
            ],
            scene_specs=[
                SceneSpec(name="Main", entities=["Player", "Targets"]),
                SceneSpec(name="MainMenu", entities=[]),
            ],
        )
        apply_physical_projectile_contract(
            schema,
            "A 3D target range where the player fires visible physical bullets.",
        )

        fixed, fixes = _reconcile_scene_specs(schema)

        main = next(spec for spec in fixed.scene_specs if spec.name == "Main")
        assert main.entities == [
            "Player",
            "Targets",
            "PlayerWeapon",
            "Bullets",
            "ProjectileBulletVisual",
        ]
        assert next(spec for spec in fixed.scene_specs
                    if spec.name == "MainMenu").entities == []
        assert any("restored orphaned entity group(s)" in fix for fix in fixes)
        build_scene_graph(fixed)

    def test_multiple_gameplay_scenes_preserve_orphan_ambiguity(self):
        from src.scene_graph import SceneGraphError, build_scene_graph
        from src.unity_schema import EntitySpec, SceneSpec

        schema = self._base_schema(
            scenes=["RangeA", "RangeB"],
            entities=[
                EntitySpec(name="Player", kind="player"),
                EntitySpec(name="TargetsA", kind="enemy"),
                EntitySpec(name="TargetsB", kind="enemy"),
                EntitySpec(name="Bullets", kind="prop"),
            ],
            scene_specs=[
                SceneSpec(name="RangeA", entities=["Player", "TargetsA"]),
                SceneSpec(name="RangeB", entities=["Player", "TargetsB"]),
            ],
        )

        fixed, fixes = _reconcile_scene_specs(schema)

        assert not any("restored orphaned entity group(s)" in fix for fix in fixes)
        with pytest.raises(SceneGraphError, match="entity 'Bullets'.*excluded"):
            build_scene_graph(fixed)

    def test_empty_model_hint_backfilled_from_name(self):
        # The local LLM routinely omits model_hint (TerminalHorizon: all 6
        # empty) — the fixer must backfill deterministically from the name.
        from src.unity_schema import EntitySpec
        schema = self._base_schema(entities=[
            EntitySpec(name="AtmosphericClouds", kind="prop", count=20),
            EntitySpec(name="InterceptorDrones", kind="enemy", count=8),
            EntitySpec(name="Pickups", kind="pickup", count=3,
                       model_hint="glowing shard"),   # respected, not overwritten
        ])
        fixed, fixes = _check_consistency(schema)
        hints = {e.name: e.model_hint for e in fixed.entities}
        assert hints["AtmosphericClouds"] == "atmospheric clouds"
        assert hints["InterceptorDrones"] == "interceptor drones enemy"
        assert hints["Pickups"] == "glowing shard"
        assert any("model_hint" in f for f in fixes)
        # idempotent: second pass changes nothing
        _, fixes2 = _check_consistency(fixed)
        assert not any("model_hint" in f for f in fixes2)

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

    # ── placement="single" + count>1 auto-upgrade (stacking fix) ────────────

    def test_single_placement_with_small_count_upgrades_to_line(self):
        from src.unity_schema import EntitySpec
        schema = self._base_schema(
            entities=[EntitySpec(name="Buildings", kind="building", count=5)])
        fixed, fixes = _check_consistency(schema)
        e = next(e for e in fixed.entities if e.name == "Buildings")
        assert e.placement == "line"
        assert any("Buildings" in f and "line" in f for f in fixes)

    def test_single_placement_with_large_count_upgrades_to_grid(self):
        from src.unity_schema import EntitySpec
        schema = self._base_schema(
            entities=[EntitySpec(name="Crowd", kind="npc", count=20)])
        fixed, fixes = _check_consistency(schema)
        e = next(e for e in fixed.entities if e.name == "Crowd")
        assert e.placement == "grid"

    def test_single_placement_with_count_one_is_untouched(self):
        from src.unity_schema import EntitySpec
        schema = self._base_schema(
            entities=[EntitySpec(name="Boss", kind="enemy", count=1)])
        fixed, fixes = _check_consistency(schema)
        e = next(e for e in fixed.entities if e.name == "Boss")
        assert e.placement == "single"
        assert all("placement" not in f.lower() for f in fixes)

    def test_explicit_non_single_placement_is_never_overridden(self):
        from src.unity_schema import EntitySpec
        schema = self._base_schema(
            entities=[EntitySpec(name="Stands", kind="structure", count=6,
                                 placement="ring")])
        fixed, fixes = _check_consistency(schema)
        e = next(e for e in fixed.entities if e.name == "Stands")
        assert e.placement == "ring"

    def test_path_placement_is_never_overridden(self):
        from src.unity_schema import EntitySpec
        schema = self._base_schema(
            entities=[EntitySpec(name="Road", kind="road", count=10,
                                 placement="path",
                                 path_points=[[0, 0, 0], [10, 0, 10]])])
        fixed, fixes = _check_consistency(schema)
        e = next(e for e in fixed.entities if e.name == "Road")
        assert e.placement == "path"


# ══════════════════════════════════════════════════════════════════════════════
# Mission -> scene inference (_infer_mission_scenes) and the entity-
# restoration it feeds in _reconcile_scene_specs.
#
# Regression fixture: a live GearCitadel run (3 missions across 4 hall
# scenes) had "NOT PLAYABLE" playability — 2 of 5 required objectives could
# never advance (SEAM 8 bound only the first mission's `reach`/`collect`
# project-wide) — and even the correctly-wired first mission's own targets
# were scoped only to an unrelated "Main" scene, absent from the hall the
# player actually plays. These tests use the real mission titles/scene
# names/entity names from that run.
# ══════════════════════════════════════════════════════════════════════════════

class TestMissionSceneInference:
    def _schema(self, **overrides) -> UnitySchema:
        from src.unity_schema import EntitySpec, MissionObjective, MissionSpec, SceneSpec
        defaults = dict(
            project_name="GearCitadel", company_name="TestCo", game_type="2D",
            platform="PC", render_pipeline="URP", unity_version="6000.0",
            color_space="Linear", description="test", features=[], packages=[],
            scenes=["Main", "MainMenu", "HallOne", "HallTwo", "HallThree", "BossArena"],
            entities=[
                EntitySpec(name="GearGate", kind="prop", count=1),
                EntitySpec(name="SaveCogShrine", kind="prop", count=1),
                EntitySpec(name="WallClingPoint", kind="prop", count=1),
                EntitySpec(name="BossTinkerGhost", kind="enemy", count=1),
            ],
        )
        defaults.update(overrides)
        return UnitySchema(**defaults)

    def _mission(self, mission_id, title, objectives):
        from src.unity_schema import MissionObjective, MissionSpec
        return MissionSpec(id=mission_id, title=title, objectives=[
            MissionObjective(id=oid, description=desc, type=typ, target_ref=ref)
            for oid, desc, typ, ref in objectives
        ])

    def test_name_match_resolves_mission_to_scene(self):
        mission = self._mission("m1", "Hall One Clearance", [
            ("obj_reach_gate_1", "Reach Gear Gate 1", "reach", "GearGate"),
        ])
        schema = self._schema(missions=[mission])
        resolved, unresolved = _infer_mission_scenes(schema)
        assert resolved == {"m1": ["HallOne"]}
        assert unresolved == []

    def test_boss_mission_name_matches_boss_arena(self):
        mission = self._mission("m3", "Boss Battle: Tinker Ghost", [
            ("obj_defeat_boss", "Defeat the Tinker Ghost boss", "defeat", "BossTinkerGhost"),
        ])
        schema = self._schema(missions=[mission])
        resolved, unresolved = _infer_mission_scenes(schema)
        assert resolved == {"m3": ["BossArena"]}
        assert unresolved == []

    def test_sibling_agreement_resolves_mission_when_no_name_match(self):
        # "Vertical Ascent" shares no token with "HallTwo" — tier 1 (name
        # match) finds nothing. Tier 2 must fall back to the fact that
        # WallClingPoint (this mission's OTHER objective's target) is already
        # correctly scoped to HallTwo in scene_specs.
        from src.unity_schema import SceneSpec
        mission = self._mission("m2", "Vertical Ascent", [
            ("obj_reach_gate_2", "Reach Gear Gate 2 via wall-cling points",
             "reach", "WallClingPoint"),
            ("obj_collect_cogs_2", "Collect gear-cogs to unlock the gate",
             "collect", "SaveCogShrine"),
        ])
        schema = self._schema(
            missions=[mission],
            scene_specs=[
                SceneSpec(name="HallTwo", entities=["WallClingPoint", "BossTinkerGhost"]),
            ])
        resolved, unresolved = _infer_mission_scenes(schema)
        assert resolved == {"m2": ["HallTwo"]}
        assert unresolved == []

    def test_sibling_agreement_tie_is_abstained_not_guessed(self):
        # Both scenes have exactly ONE of this mission's two targets already
        # scoped to them -- a genuine tie, not a resolvable case.
        from src.unity_schema import SceneSpec
        mission = self._mission("m2", "Vertical Ascent", [
            ("obj_reach_gate_2", "Reach Gear Gate 2", "reach", "WallClingPoint"),
            ("obj_collect_cogs_2", "Collect gear-cogs", "collect", "SaveCogShrine"),
        ])
        schema = self._schema(
            missions=[mission],
            scene_specs=[
                SceneSpec(name="HallTwo", entities=["WallClingPoint"]),
                SceneSpec(name="Main", entities=["SaveCogShrine"]),
            ])
        resolved, unresolved = _infer_mission_scenes(schema)
        assert resolved == {}
        assert unresolved == ["m2"]

    def test_unresolvable_mission_is_abstained_not_guessed(self):
        mission = self._mission("m9", "Sideways Interlude", [
            ("obj_x", "Do the thing", "reach", "GearGate"),
        ])
        schema = self._schema(missions=[mission], scene_specs=[])
        resolved, unresolved = _infer_mission_scenes(schema)
        assert resolved == {}
        assert unresolved == ["m9"]

    def test_name_match_tie_falls_through_to_sibling_tier(self):
        # "Hall" is a substring of BOTH "HallOne" and "HallTwo" -- tier 1
        # must not guess between them; falls through to tier 2, which finds
        # GearGate already uniquely scoped to HallOne.
        from src.unity_schema import SceneSpec
        mission = self._mission("m1", "Hall", [
            ("o1", "Reach the gate", "reach", "GearGate"),
        ])
        schema = self._schema(
            missions=[mission],
            scenes=["HallOne", "HallTwo"],
            scene_specs=[SceneSpec(name="HallOne", entities=["GearGate"])])
        resolved, unresolved = _infer_mission_scenes(schema)
        assert resolved == {"m1": ["HallOne"]}

    def test_objective_scene_map_flattens_mission_scenes_per_objective(self):
        mission = self._mission("m1", "Hall One Clearance", [
            ("obj_reach_gate_1", "Reach Gear Gate 1", "reach", "GearGate"),
            ("obj_collect_cogs_1", "Collect gear-cogs", "collect", "SaveCogShrine"),
        ])
        schema = self._schema(missions=[mission])
        resolved, _ = _infer_mission_scenes(schema)
        obj_scenes = _objective_scene_map(schema, resolved)
        assert obj_scenes == {
            "HallOneClearanceObjectives.obj_reach_gate_1": "HallOne",
            "HallOneClearanceObjectives.obj_collect_cogs_1": "HallOne",
        }

    def test_objective_scene_map_keys_by_tracker_class_not_bare_id(self):
        # Two different missions whose objectives share the SAME local id —
        # this codebase's own "obj_1"/"obj_2" convention — must not collide
        # in the flattened map; each mission's own tracker class name
        # disambiguates. Live regression (DropZone): three missions each
        # authored "obj_1"/"obj_2", so a bare-id map only ever held the
        # LAST-processed mission's scene, and SEAM 8 then lumped every
        # mission's objectives into that one scene bucket — 3 of 6 required
        # objectives went permanently unbound though a trigger existed for
        # each mission's own scene.
        m1 = self._mission("m1", "Hall One Clearance", [
            ("obj_1", "Reach Gear Gate 1", "reach", "GearGate"),
        ])
        m2 = self._mission("m3", "Boss Battle: Tinker Ghost", [
            ("obj_1", "Defeat the Tinker Ghost boss", "defeat", "BossTinkerGhost"),
        ])
        schema = self._schema(missions=[m1, m2])
        resolved, unresolved = _infer_mission_scenes(schema)
        assert resolved == {"m1": ["HallOne"], "m3": ["BossArena"]}
        assert unresolved == []
        obj_scenes = _objective_scene_map(schema, resolved)
        assert obj_scenes == {
            "HallOneClearanceObjectives.obj_1": "HallOne",
            "BossBattleTinkerGhostObjectives.obj_1": "BossArena",
        }

    def test_objective_scene_map_refines_per_objective_by_own_description(self):
        # Regression fixture: a live DeadReckoningProtocol run. Mission
        # "Engineer Extraction" title-matches scene "ExtractionPad" alone
        # (tier 1), so the mission-level map has only that one scene for
        # every objective. Two of its three objectives are BOTH `reach` —
        # one for the Engineer (met in the depot yard), one for the pad
        # itself — and SEAM 8 can bind only ONE `reach` objective per scene,
        # so lumping both under "ExtractionPad" left one permanently
        # unadvanceable even though DepotYard had its own unused trigger.
        # Each objective's own description must be checked before falling
        # back to the mission's scene, so "in the depot yard" sends the
        # first objective to DepotYard while the second stays on
        # ExtractionPad, and the two no longer collide.
        mission = self._mission("m1", "Engineer Extraction", [
            ("obj_escort_engineer", "Reach the Engineer in the depot yard.",
             "reach", "Engineer"),
            ("obj_collect_fuel", "Collect all SupplyCrate fuel cells for the VTOL.",
             "collect", "SupplyCrate"),
            ("obj_reach_pad", "Reach the ExtractionPad with the Engineer.",
             "reach", "ExtractionPad"),
        ])
        schema = self._schema(
            missions=[mission],
            scenes=["Main", "ExtractionPad", "DepotYard", "HangarBay"])
        resolved, unresolved = _infer_mission_scenes(schema)
        assert resolved == {"m1": ["ExtractionPad"]}
        assert unresolved == []
        obj_scenes = _objective_scene_map(schema, resolved)
        assert obj_scenes == {
            "EngineerExtractionObjectives.obj_escort_engineer": "DepotYard",
            "EngineerExtractionObjectives.obj_collect_fuel":    "ExtractionPad",  # no scene named -> mission default
            "EngineerExtractionObjectives.obj_reach_pad":       "ExtractionPad",
        }

    def test_objective_scene_map_requires_the_full_scene_name_not_one_word(self):
        # A single generic shared word must NOT reroute an objective: only an
        # objective that names a scene IN FULL (every one of its tokens) is
        # strong enough evidence to override the mission's own resolved
        # scene. "the supply yard" shares "yard" with "DepotYard" but not
        # "depot" -- one word of a two-word scene name is exactly the false
        # -positive shape a generic noun (yard, hallway, gate...) could
        # trigger by coincidence, so it must fall back to the mission
        # default instead of misrouting to DepotYard.
        mission = self._mission("m1", "Engineer Extraction", [
            ("obj_collect_fuel", "Collect fuel cells from the supply yard.",
             "collect", "SupplyCrate"),
        ])
        schema = self._schema(
            missions=[mission],
            scenes=["Main", "ExtractionPad", "DepotYard", "HangarBay"])
        resolved, _ = _infer_mission_scenes(schema)
        assert resolved == {"m1": ["ExtractionPad"]}
        obj_scenes = _objective_scene_map(schema, resolved)
        assert obj_scenes == {"EngineerExtractionObjectives.obj_collect_fuel": "ExtractionPad"}

    def test_reconcile_scene_specs_restores_mission_target_entities(self):
        # The exact live GearCitadel shape: HallOne's own subset lists its
        # hazard flavor entities but not the mission's own targets.
        from src.unity_schema import SceneSpec
        mission = self._mission("m1", "Hall One Clearance", [
            ("obj_reach_gate_1", "Reach Gear Gate 1", "reach", "GearGate"),
            ("obj_collect_cogs_1", "Collect gear-cogs", "collect", "SaveCogShrine"),
        ])
        schema = self._schema(
            missions=[mission],
            scene_specs=[SceneSpec(name="HallOne", entities=["BossTinkerGhost"])])
        resolved, _ = _infer_mission_scenes(schema)
        schema.meta["mission_scenes"] = resolved
        fixed, fixes = _reconcile_scene_specs(schema)
        spec = next(s for s in fixed.scene_specs if s.name == "HallOne")
        assert "GearGate" in spec.entities
        assert "SaveCogShrine" in spec.entities
        assert "BossTinkerGhost" in spec.entities   # untouched, not dropped
        assert any("GearGate" in f and "Hall One Clearance" in f for f in fixes)

    def test_reconcile_scene_specs_is_a_noop_without_mission_scenes(self):
        # No meta["mission_scenes"] set at all -- must behave exactly as it
        # did before this feature existed (backward compatibility).
        from src.unity_schema import SceneSpec
        mission = self._mission("m1", "Hall One Clearance", [
            ("obj_reach_gate_1", "Reach Gear Gate 1", "reach", "GearGate"),
        ])
        schema = self._schema(
            missions=[mission],
            scene_specs=[SceneSpec(name="HallOne", entities=["BossTinkerGhost"])])
        fixed, fixes = _reconcile_scene_specs(schema)
        spec = next(s for s in fixed.scene_specs if s.name == "HallOne")
        assert spec.entities == ["BossTinkerGhost"]
        assert not any("GearGate" in f for f in fixes)

    def test_menu_scenes_are_never_matched(self):
        # The mission title textually contains "Menu", but that scene is
        # explicitly non-gameplay and must never be inferred as home to a
        # real mission, even when nothing else matches.
        mission = self._mission("m1", "Menu Interlude", [
            ("o1", "Reach the gate", "reach", "GearGate"),
        ])
        schema = self._schema(
            missions=[mission], scenes=["MainMenu", "HallOne"], scene_specs=[])
        resolved, unresolved = _infer_mission_scenes(schema)
        assert "m1" not in resolved
        assert unresolved == ["m1"]


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

    # ── Real-world catches: live E2E run of prompts/newprompt.txt (SiegePlatformer,
    #    2026-07-06) hit both of these verbatim in 2 different custom scripts. ──

    def test_bogus_inputsystem_guard_stripped(self):
        # InputSystem is a real static class (GetDevice<T>(), devices, ...) but
        # has no `.current`, and a type name can never be null-compared — this
        # exact guard is always CS0119 + CS0117, never a real check.
        schema = self._schema(scripts=[
            UnityScript(name="Reader", type="custom", description="x",
                        update_extra="if (InputSystem != null && InputSystem.current != null) "
                                     "{ DoThing(); }"),
        ])
        fixed = _programmatic_fix(schema, compose=True)
        assert "InputSystem" not in fixed.scripts[0].update_extra

    def test_valid_inputsystem_getdevice_call_preserved(self):
        # A real InputSystem API call must not be caught by the bogus-guard regex.
        schema = self._schema(scripts=[
            UnityScript(name="Reader", type="custom", description="x",
                        update_extra="var kb = InputSystem.GetDevice<Keyboard>();"),
        ])
        fixed = _programmatic_fix(schema, compose=True)
        assert "InputSystem.GetDevice<Keyboard>()" in fixed.scripts[0].update_extra

    def test_gamemanager_member_via_get_bound_variable_validated(self):
        # `var context = GameContext.Instance.Get<GameManager>();` then
        # `context.SiegeTimer` — SiegeTimer isn't part of _GAME_MANAGER_API, so
        # this must be caught the same way as GameManager.Instance.SiegeTimer.
        schema = self._schema(scripts=[
            UnityScript(name="Debris", type="custom", description="x",
                        update_extra="var context = GameContext.Instance.Get<GameManager>(); "
                                     "if (context != null && context.SiegeTimer > 0f) DoThing();"),
        ])
        fixed = _programmatic_fix(schema, compose=True)
        assert "SiegeTimer" not in fixed.scripts[0].update_extra

    def test_gamemanager_member_via_get_bound_variable_valid_member_kept(self):
        # context.AddScore(0) IS in _GAME_MANAGER_API — must survive untouched.
        schema = self._schema(scripts=[
            UnityScript(name="Inv", type="custom", description="x",
                        update_extra="var context = GameContext.Instance.Get<GameManager>(); "
                                     "if (context != null) context.AddScore(0);"),
        ])
        fixed = _programmatic_fix(schema, compose=True)
        assert "context.AddScore(0)" in fixed.scripts[0].update_extra

    def test_live_e2e_debris_physics_sync_bug_fully_sanitized(self):
        # Exact update_extra the LLM produced for DebrisPhysicsSync.cs — hit both
        # bugs at once (bogus InputSystem guard wrapping an invalid SiegeTimer
        # access) and failed to compile with 3 errors (CS0119, CS0117, CS1061).
        schema = self._schema(scripts=[
            UnityScript(name="DebrisPhysicsSync", type="custom", description="x",
                        update_extra=(
                            "if (InputSystem != null && InputSystem.current != null) "
                            "{ var context = GameContext.Instance.Get<GameManager>(); "
                            "if (context != null && context.SiegeTimer > 0f) { "
                            "siegeTimer -= Time.deltaTime; if (siegeTimer <= 0f) "
                            "foreach (var go in triggeredCollapses) { if (go != null) Destroy(go); } "
                            "} }"
                        )),
        ])
        fixed = _programmatic_fix(schema, compose=True)
        result = fixed.scripts[0].update_extra
        assert "InputSystem" not in result
        assert "SiegeTimer" not in result

    # ── InputSystem.current hallucination (live regression: StackMaster) ─────

    def test_input_system_current_mouse_rewritten(self):
        # Live bug: LLM emitted "InputSystem.current" (CS0117 — no such member)
        # instead of the real per-device accessor Mouse.current.
        schema = self._schema(scripts=[
            UnityScript(name="RaycastSpawner", type="custom", description="x",
                        update_extra="if (InputSystem.current != null && "
                                     "InputSystem.current.mouse.leftButton.wasPressedThisFrame) "
                                     "{ Fire(); }"),
        ])
        fixed = _programmatic_fix(schema)
        body = fixed.scripts[0].update_extra
        assert "InputSystem.current" not in body
        assert "Mouse.current != null" in body
        assert "Mouse.current.leftButton.wasPressedThisFrame" in body

    def test_input_system_current_keyboard_inferred_from_device_use(self):
        schema = self._schema(scripts=[
            UnityScript(name="Jumper", type="custom", description="x",
                        update_extra="if (InputSystem.current != null && "
                                     "InputSystem.current.keyboard.spaceKey.isPressed) Jump();"),
        ])
        fixed = _programmatic_fix(schema)
        body = fixed.scripts[0].update_extra
        assert "InputSystem.current" not in body
        assert "Keyboard.current != null" in body
        assert "Keyboard.current.spaceKey.isPressed" in body

    def test_no_input_system_current_left_untouched(self):
        schema = self._schema(scripts=[
            UnityScript(name="Jumper", type="custom", description="x",
                        update_extra="if (Keyboard.current != null) Jump();"),
        ])
        fixed = _programmatic_fix(schema)
        assert fixed.scripts[0].update_extra == "if (Keyboard.current != null) Jump();"

    # ── Physics.Raycast bool-vs-out-param confusion (same live regression) ───

    def test_raycast_bool_member_access_redirected_to_out_param(self):
        # Live bug: `hit` is Physics.Raycast's bool return; hit.collider/hit.point
        # don't exist on bool — the real members are on the out RaycastHit var.
        schema = self._schema(scripts=[
            UnityScript(name="RaycastSpawner", type="custom", description="x",
                        update_extra=(
                            "var hit = Physics.Raycast(transform.position, transform.forward, "
                            "out RaycastHit hitInfo, spawnDistance); "
                            "if (hit.collider != null) { "
                            "Instantiate(_prefabs[0], hit.point, Quaternion.LookRotation(hit.normal)); }"
                        )),
        ])
        fixed = _programmatic_fix(schema)
        body = fixed.scripts[0].update_extra
        assert "hit.collider" not in body
        assert "hit.point" not in body
        assert "hit.normal" not in body
        assert "hitInfo.collider" in body
        assert "hitInfo.point" in body
        assert "hitInfo.normal" in body

    def test_raycast_with_matching_var_names_left_untouched(self):
        schema = self._schema(scripts=[
            UnityScript(name="Shooter", type="custom", description="x",
                        update_extra=(
                            "var hit = Physics.Raycast(transform.position, transform.forward, "
                            "out RaycastHit hit2, spawnDistance); if (hit) Fire(hit2.point);"
                        )),
        ])
        fixed = _programmatic_fix(schema)
        assert "hit2.point" in fixed.scripts[0].update_extra


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



class TestBriefAndFeatureReconciliation:
    def test_input_system_feature_injected_when_package_present(self):
        from src.unity_schema import UnityPackage, UnityScript, UnitySchema
        schema = UnitySchema(
            project_name="T", company_name="C", description="d",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", scenes=["Main"], features=["physics"],
            scripts=[UnityScript(name="PlayerController", type="player", description="p")],
            packages=[UnityPackage(
                description="Input System for granular multi-axis adjustments",
                package_id="com.unity.inputsystem")])
        fixed, fixes = _check_consistency(schema)
        assert "input_system" in fixed.features          # → activeInputHandler: 2
        assert any("input_system" in f for f in fixes)

    def test_structured_brief_detection(self):
        from src.unity_pipeline import _is_structured_brief
        brief = ("TITLE: Terminal Horizon\nGENRE: survival\n"
                 "SCENES (8 total)\nENTITIES\nAUDIO — cues\n")
        assert _is_structured_brief(brief)
        assert not _is_structured_brief("a cozy skydiving game over the desert")
        assert not _is_structured_brief("")

    def test_structured_brief_detection_arbitrary_headers(self):
        """Confirmed live (2026-08-28): a real 10-section brief using headers
        outside the original fixed vocabulary (GOAL/CORE GAMEPLAY/LEVEL &
        PLATFORMS/...) matched only 2 markers under the old closed-list regex
        — below the 3-marker floor — so resolve_theme() ran its autodetect
        path on already-fully-themed content and grafted an unrelated
        "Superhero Flying Mechanics" theme onto a ground-based brawler brief.
        _BRIEF_MARKER_RE must recognize ANY all-caps section header, not just
        the specific words in the original list."""
        from src.unity_pipeline import _is_structured_brief
        brief = (
            "GOAL:\nGenerate a colourful 2D beat 'em up.\n\n"
            "CORE GAMEPLAY:\n- Punch and throw.\n\n"
            "LEVEL & PLATFORMS:\n- Moving platforms.\n\n"
            "PLAYER STATS & LIVES:\n- 3 lives.\n\n"
            "UI & HUD:\n- Score counter.\n\n"
            "AUDIO (Stable Audio 3):\nUse Stable Audio 3.\n\n"
            "UNITY IMPLEMENTATION:\n- 2D character controller.\n\n"
            "STYLE & TONE:\n- Cartoonish.\n"
        )
        assert _is_structured_brief(brief)

    def test_structured_brief_detection_still_rejects_prose(self):
        """The broadened regex must not start matching normal sentence-case
        text just because it drops the fixed vocabulary — Title Case and
        sentence-case words (only the first letter capitalized) still fail
        the per-word ALL-CAPS requirement."""
        from src.unity_pipeline import _is_structured_brief
        brief = (
            "Make A Cozy Skydiving Game Over The Desert.\n"
            "The Player Wears A Bright Red Jumpsuit.\n"
            "Enemies Are Friendly Clouds.\n"
        )
        assert not _is_structured_brief(brief)
