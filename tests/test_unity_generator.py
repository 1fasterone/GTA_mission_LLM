"""
Tests for UnityProjectGenerator: file output, scene content, prefabs,
input actions, canvas/event system, and meta file formats.
"""
import json
import re
from unittest import mock

import pytest

from src.unity_generator import (
    UnityProjectGenerator,
    _UGUI_GUIDS,
    _generate_material,
    _make_guid,
    _make_uuid,
)
from src.unity_schema import EntitySpec, UnityScript

# Stale UGUI script GUIDs that were wrong on Unity 6 and caused missing-script
# components on Canvas/EventSystem. Kept here so regressions assert they never return.
_STALE_GRAPHIC_RAYCASTER_GUID = "dc42784cf147fd346b4c9064bdc43eb9"
_STALE_EVENT_SYSTEM_GUID = "76c392e42b5098d4f8003d6289d9e15e"
# Shader GUIDs (verified on Unity 6000.0).
_URP_LIT_GUID = "933532a4fcc9baf4fa0491de14d08ed7"
_HDRP_LIT_GUID = "6e4ae4064600d784cac1e41a9e6f2e59"
_BUILTIN_STANDARD_GUID = "0000000000000000f000000000000000"
# URP Volume override GUIDs (sourced from real installed Unity Editors,
# cross-checked against this repo's actual Unity 6 target — see
# unity_generator.py's own comment above these constants for provenance).
_COLOR_ADJUSTMENTS_GUID_PP = "66f335fb1ffd8684294ad653bf1c7564"
_TONEMAPPING_GUID_PP = "97c23e3b12dc18c42a140437e53d3951"
_VIGNETTE_GUID_PP = "899c54efeace73346a0a16faa3afe726"
_BLOOM_GUID_PP = "0b2db86121404754db890f4c8dfe81b2"
_UNIVERSAL_CAM_DATA_GUID = "a79441f348de89743a2939f4d699eac1"


@pytest.fixture
def gen() -> UnityProjectGenerator:
    return UnityProjectGenerator("templates/unity")


@pytest.fixture
def files_3d(gen, full_3d_schema):
    manifest = {"com.unity.render-pipelines.universal": "14.0.9"}
    return gen.generate(full_3d_schema, manifest)


@pytest.fixture
def files_2d(gen, schema_2d):
    return gen.generate(schema_2d, {})


@pytest.fixture
def files_minimal(gen, minimal_schema):
    return gen.generate(minimal_schema, {})


# ── Helper ─────────────────────────────────────────────────────────────────────

def scene(files: dict, project: str, scene_name: str) -> str:
    return files[f"{project}/Assets/Scenes/{scene_name}.unity"]


# ── File set completeness ──────────────────────────────────────────────────────

class TestFileSet:
    def test_project_version_file_present(self, files_3d):
        assert any("ProjectVersion.txt" in p for p in files_3d)

    def test_manifest_json_present(self, files_3d):
        assert any("manifest.json" in p for p in files_3d)

    def test_all_scenes_generated(self, files_3d, full_3d_schema):
        for s in full_3d_schema.scenes:
            assert f"FullProject/Assets/Scenes/{s}.unity" in files_3d

    def test_all_scene_metas_generated(self, files_3d, full_3d_schema):
        for s in full_3d_schema.scenes:
            assert f"FullProject/Assets/Scenes/{s}.unity.meta" in files_3d

    def test_all_scripts_generated(self, files_3d, full_3d_schema):
        for script in full_3d_schema.scripts:
            assert f"FullProject/Assets/Scripts/{script.name}.cs" in files_3d

    def test_all_script_metas_generated(self, files_3d, full_3d_schema):
        for script in full_3d_schema.scripts:
            assert f"FullProject/Assets/Scripts/{script.name}.cs.meta" in files_3d

    def test_readme_generated(self, files_3d):
        assert "FullProject/README.md" in files_3d

    def test_placeholder_folders_present(self, files_minimal):
        for folder in ("Materials", "Prefabs", "Audio", "Animations", "Textures"):
            assert any(f"Assets/{folder}" in p for p in files_minimal)

    def test_output_is_deterministic(self, gen, full_3d_schema):
        """Two calls with the same schema must produce identical file dicts."""
        manifest = {"com.unity.render-pipelines.universal": "14.0.9"}
        first  = gen.generate(full_3d_schema, manifest)
        second = gen.generate(full_3d_schema, manifest)
        assert first == second

    def test_native_asset_files_end_with_trailing_newline(self, files_3d):
        # Regression (live Unity, StickmanClimber run): the Jinja Environment
        # rendering these templates didn't set keep_trailing_newline=True, so
        # Jinja silently stripped the template source's own final newline.
        # Unity's native YAML-ish asset parser (.prefab/.unity/.asset) is not
        # lenient about a missing final newline — it reads past EOF looking for
        # the value after the last key and reports a confused "Parser Failure
        # ... Expect ':' between key and value" one line past the real content,
        # plus a spurious duplicate-fileID warning on next reimport.
        native_exts = (".unity", ".prefab", ".asset")
        checked = 0
        for path, content in files_3d.items():
            if path.endswith(native_exts) and content:
                checked += 1
                assert content.endswith("\n"), f"{path} is missing its trailing newline"
        assert checked > 0, "no native asset files were exercised by this fixture"


# ── Playable-player merge (single Player tag) ────────────────────────────────
# The scene template always builds a dedicated Player object (tag Player +
# CapsuleCollider + Rigidbody + PlayerController). A brief-named entity tagged
# Player must be MERGED into it, not spawned as a second Player-tagged object —
# otherwise FindWithTag("Player") picks between them arbitrarily and the
# interactable/camera/transition gates latch onto the inert one.

class TestPlayerMerge:
    MANIFEST = {"com.unity.render-pipelines.universal": "14.0.9"}

    def _gameplay_scene(self, files):
        # "Main" is the gameplay scene in full_3d_schema (Menu is excluded).
        return files["FullProject/Assets/Scenes/Main.unity"]

    def test_character_player_entity_does_not_create_a_second_player_tag(
            self, gen, full_3d_schema):
        full_3d_schema.entities = [
            EntitySpec(name="PlayerHero", kind="character", count=1,
                       tag="Player", model_hint="hero in a coat"),
            EntitySpec(name="Crate", kind="prop", count=3, placement="scatter",
                       tag="Untagged", model_hint="wooden crate"),
        ]
        files = gen.generate(full_3d_schema, self.MANIFEST)
        scene = self._gameplay_scene(files)
        # Exactly one object carries the Player tag: the dedicated scene Player.
        assert scene.count("m_TagString: Player") == 1

    def test_merged_character_player_is_skipped_by_the_spawner(
            self, gen, full_3d_schema):
        full_3d_schema.entities = [
            EntitySpec(name="PlayerHero", kind="character", count=1,
                       tag="Player", model_hint="hero in a coat"),
            EntitySpec(name="Crate", kind="prop", count=3, placement="scatter",
                       tag="Untagged", model_hint="wooden crate"),
        ]
        files = gen.generate(full_3d_schema, self.MANIFEST)
        spawner = files["FullProject/Assets/Scripts/EntitySpawner.cs"]
        # The player entity is merged into the scene Player — not spawned.
        assert 'SpawnGroup("PlayerHero"' not in spawner
        # A normal entity still spawns, proving the spawner itself runs.
        assert 'SpawnGroup("Crate"' in spawner

    def test_untagged_character_entity_still_spawns_normally(
            self, gen, full_3d_schema):
        """Only a Player-tagged entity is merged; a normal NPC is not."""
        full_3d_schema.entities = [
            EntitySpec(name="Villager", kind="character", count=1,
                       tag="Untagged", model_hint="villager"),
        ]
        files = gen.generate(full_3d_schema, self.MANIFEST)
        spawner = files["FullProject/Assets/Scripts/EntitySpawner.cs"]
        assert 'SpawnGroup("Villager"' in spawner
        # Still exactly one Player tag (the dedicated scene Player).
        assert self._gameplay_scene(files).count("m_TagString: Player") == 1

    def test_output_stays_deterministic_with_a_merged_player(
            self, gen, full_3d_schema):
        full_3d_schema.entities = [
            EntitySpec(name="PlayerHero", kind="character", count=1,
                       tag="Player", model_hint="hero"),
        ]
        first  = gen.generate(full_3d_schema, self.MANIFEST)
        second = gen.generate(full_3d_schema, self.MANIFEST)
        assert first == second

    def test_compose_mode_still_merges_the_player(self, gen, full_3d_schema):
        """--compose drops the 'player' script (the controller micro owns it),
        so the merge must key off a composed player-target unit too — otherwise
        the avatar spawns as a SECOND Player-tagged object beside the composed
        one. Regression: the gate checked only schema.scripts."""
        from src.unity_compose import ComposedUnit

        # Compose drops the schema 'player' script; the controller micro owns it.
        full_3d_schema.scripts = [
            s for s in full_3d_schema.scripts if s.type != "player"]
        full_3d_schema.entities = [
            EntitySpec(name="PlayerHero", kind="character", count=1,
                       tag="Player", model_hint="hero in a coat"),
            EntitySpec(name="Crate", kind="prop", count=3, placement="scatter",
                       tag="Untagged", model_hint="wooden crate"),
        ]
        units = [
            ComposedUnit(kind="controller", variant="third_person",
                         class_name="PlayerController", guid=_make_guid("pc"),
                         target="player", file_id=900000010),
            ComposedUnit(kind="physics", variant="rigidbody_3d",
                         class_name="PhysicsBody", guid=_make_guid("pb"),
                         target="player", file_id=900000030),
        ]
        files = gen.generate(full_3d_schema, self.MANIFEST, composed_units=units)
        scene = self._gameplay_scene(files)
        spawner = files["FullProject/Assets/Scripts/EntitySpawner.cs"]
        # Exactly one Player tag (the composed Player object), and the avatar
        # entity is merged/skipped rather than spawned as a duplicate.
        assert scene.count("m_TagString: Player") == 1
        assert 'SpawnGroup("PlayerHero"' not in spawner
        assert 'SpawnGroup("Crate"' in spawner


# ── Meta file format ───────────────────────────────────────────────────────────

class TestMetaFormats:
    def test_script_meta_uses_mono_importer(self, files_3d):
        meta = files_3d["FullProject/Assets/Scripts/GameManager.cs.meta"]
        assert "MonoImporter:" in meta
        assert "fileFormatVersion: 2" in meta

    def test_folder_meta_uses_default_importer_with_folder_flag(self, files_3d):
        meta = files_3d["FullProject/Assets/Scripts.meta"]
        assert "folderAsset: yes" in meta
        assert "DefaultImporter:" in meta

    def test_scene_meta_uses_default_importer_without_folder_flag(self, files_3d):
        meta = files_3d["FullProject/Assets/Scenes/Main.unity.meta"]
        assert "DefaultImporter:" in meta
        assert "folderAsset" not in meta

    def test_prefab_meta_uses_native_format_importer(self, files_3d):
        meta = files_3d["FullProject/Assets/Prefabs/PlayerController.prefab.meta"]
        assert "NativeFormatImporter:" in meta
        assert "mainObjectFileID: 100100000" in meta

    def test_inputactions_meta_uses_native_format_importer(self, files_3d):
        meta = files_3d["FullProject/Assets/Settings/FullProjectInputActions.inputactions.meta"]
        assert "NativeFormatImporter:" in meta
        assert "mainObjectFileID: 13400000" in meta


# ── Ground plane ───────────────────────────────────────────────────────────────

class TestGroundPlane:
    def test_ground_plane_in_3d_gameplay_scene(self, files_3d):
        s = scene(files_3d, "FullProject", "Main")
        assert "m_Name: Ground" in s
        assert "705507700" in s
        assert "705507704" in s  # BoxCollider fileID

    def test_ground_plane_absent_from_menu_scene(self, files_3d):
        s = scene(files_3d, "FullProject", "Menu")
        assert "705507700" not in s
        assert "m_Name: Ground" not in s

    def test_ground_plane_absent_from_2d_scene(self, files_2d):
        s = scene(files_2d, "PlatformerGame", "Main")
        assert "705507700" not in s

    def test_ground_plane_navigation_static(self, files_3d):
        s = scene(files_3d, "FullProject", "Main")
        # m_StaticEditorFlags: 15 (Occluder+Lightmap+Batching+Navigation)
        assert "m_StaticEditorFlags: 15" in s

    def test_ground_is_layer_10(self, files_3d):
        s = scene(files_3d, "FullProject", "Main")
        # Find the Ground GameObject block and verify layer
        idx = s.index("m_Name: Ground")
        block = s[max(0, idx - 400):idx]
        assert "m_Layer: 10" in block


# ── Player and Enemy in scene ──────────────────────────────────────────────────

class TestSceneObjects:
    def test_player_in_gameplay_scene(self, files_3d):
        s = scene(files_3d, "FullProject", "Main")
        assert "705507900" in s   # Player GO fileID

    def test_player_absent_from_menu(self, files_3d):
        s = scene(files_3d, "FullProject", "Menu")
        assert "705507900" not in s

    def test_enemy_in_gameplay_scene(self, files_3d):
        s = scene(files_3d, "FullProject", "Main")
        assert "705507800" in s   # Enemy GO fileID

    def test_player_root_order_is_4(self, files_3d):
        s = scene(files_3d, "FullProject", "Main")
        # Player Transform is the YAML document anchored at &705507901
        idx = s.index("&705507901")
        block = s[idx:idx + 400]
        assert "m_RootOrder: 4" in block

    def test_3d_player_uses_capsule_mesh(self, files_3d):
        s = scene(files_3d, "FullProject", "Main")
        # Built-in Capsule mesh fileID is 10208
        assert "fileID: 10208" in s

    def test_2d_player_uses_quad_mesh(self, files_2d):
        s = scene(files_2d, "PlatformerGame", "Main")
        # Built-in Quad mesh fileID is 10210
        assert "fileID: 10210" in s

    def test_2d_player_uses_rigidbody2d(self, files_2d):
        s = scene(files_2d, "PlatformerGame", "Main")
        assert "Rigidbody2D:" in s


# ── Canvas and EventSystem ─────────────────────────────────────────────────────

class TestCanvasAndEventSystem:
    def test_canvas_in_non_vr_scene(self, files_3d):
        s = scene(files_3d, "FullProject", "Main")
        assert "m_Name: Canvas" in s
        assert "705507600" in s   # Canvas GO fileID

    def test_canvas_scaler_component_present(self, files_3d):
        s = scene(files_3d, "FullProject", "Main")
        # CanvasScaler UGUI GUID
        assert "0cd44c1031e13a943bb63640046fad76" in s

    def test_graphic_raycaster_component_present(self, files_3d):
        # Regression: the GraphicRaycaster GUID changed on Unity 6 (com.unity.ugui).
        # The old value resolved to a missing-script component. Assert the corrected
        # GUID is present and the stale one never returns.
        s = scene(files_3d, "FullProject", "Main")
        assert _UGUI_GUIDS["GraphicRaycaster"] in s
        assert _UGUI_GUIDS["GraphicRaycaster"] == "dc42784cf147c0c48a680349fa168899"
        assert _STALE_GRAPHIC_RAYCASTER_GUID not in s

    def test_event_system_present(self, files_3d):
        # Regression: EventSystem GUID also changed on Unity 6 (was a missing script).
        s = scene(files_3d, "FullProject", "Main")
        assert "m_Name: EventSystem" in s
        assert _UGUI_GUIDS["EventSystem"] in s
        assert _UGUI_GUIDS["EventSystem"] == "76c392e42b5098c458856cdf6ecaaaa1"
        assert _STALE_EVENT_SYSTEM_GUID not in s

    def test_standalone_input_module_present(self, files_3d):
        s = scene(files_3d, "FullProject", "Main")
        assert "4f231c4fb786f3946a6b90b886c48677" in s

    def test_ui_manager_wired_to_canvas(self, files_3d):
        s = scene(files_3d, "FullProject", "Main")
        ui_guid = _make_guid("FullProject/Scripts/UIManager")
        assert ui_guid in s

    def test_canvas_absent_from_vr_scene(self, gen, schema_vr):
        files = gen.generate(schema_vr, {})
        s = scene(files, "VRProject", "Main")
        assert "705507600" not in s
        assert "m_Name: Canvas" not in s

    def test_canvas_uses_rect_transform(self, files_3d):
        s = scene(files_3d, "FullProject", "Main")
        # RectTransform type tag is !u!224
        assert "!u!224 &705507601" in s


# ── Editor setup script ────────────────────────────────────────────────────────

class TestEditorScript:
    def test_urp_generates_editor_script(self, files_3d):
        key = "FullProject/Assets/Editor/SetupRenderPipeline.cs"
        assert key in files_3d

    def test_urp_editor_script_contains_urp_class(self, files_3d):
        cs = files_3d["FullProject/Assets/Editor/SetupRenderPipeline.cs"]
        assert "UniversalRenderPipelineAsset" in cs
        assert "HDRenderPipeline" not in cs

    def test_hdrp_generates_hdrp_editor_script(self, gen, schema_hdrp):
        files = gen.generate(schema_hdrp, {})
        cs = files["HDRPProject/Assets/Editor/SetupRenderPipeline.cs"]
        assert "HDRenderPipelineAsset" in cs

    def test_builtin_no_editor_script(self, files_minimal):
        assert not any("SetupRenderPipeline" in p for p in files_minimal)

    def test_settings_folder_stub_generated_for_urp(self, files_3d):
        assert "FullProject/Assets/Settings/.gitkeep" in files_3d

    def test_urp_pipeline_switch_reimports_models(self, files_3d):
        # Models imported before SetupRenderPipeline's deferred URP switch keep
        # built-in Standard materials; the script must force a reimport so
        # material generation reruns under URP.
        cs = files_3d["FullProject/Assets/Editor/SetupRenderPipeline.cs"]
        assert 'FindAssets("t:Model")' in cs
        assert "ImportAssetOptions.ForceUpdate" in cs


class TestURPMaterialPostprocessor:
    KEY = "FullProject/Assets/Editor/URPMaterialPostprocessor.cs"

    def test_emitted_for_urp(self, files_3d):
        assert self.KEY in files_3d
        assert f"{self.KEY}.meta" in files_3d

    def test_remaps_standard_to_urp_lit(self, files_3d):
        cs = files_3d[self.KEY]
        assert "Universal Render Pipeline/Lit" in cs
        assert "_BaseMap" in cs and "_BaseColor" in cs

    def test_not_emitted_for_hdrp_or_builtin(self, gen, schema_hdrp, files_minimal):
        hdrp_files = gen.generate(schema_hdrp, {})
        assert not any("URPMaterialPostprocessor" in p for p in hdrp_files)
        assert not any("URPMaterialPostprocessor" in p for p in files_minimal)


class TestModelMetaMaterialImport:
    def test_model_metas_use_material_description_mode(self):
        # materialImportMode 1 (legacy ImportStandard) builds materials on the
        # built-in Standard shader, which URP renders magenta/black. Mode 2
        # (ImportViaMaterialDescription) routes through the active pipeline.
        from src.unity_generator import (
            _generic_model_meta, _humanoid_model_meta, _model_meta)
        for meta in (_humanoid_model_meta("a" * 32), _model_meta("b" * 32),
                     _generic_model_meta("c" * 32)):
            assert "materialImportMode: 2" in meta
            assert "materialImportMode: 1" not in meta


# ── Input Actions ──────────────────────────────────────────────────────────────

class TestInputActions:
    def test_input_actions_generated_when_feature_set(self, files_3d):
        assert any(".inputactions" in p for p in files_3d)

    def test_input_actions_not_generated_without_feature(self, files_minimal):
        assert not any(".inputactions" in p for p in files_minimal)

    def test_input_actions_is_valid_json(self, files_3d):
        ia_path = "FullProject/Assets/Settings/FullProjectInputActions.inputactions"
        data = json.loads(files_3d[ia_path])
        assert data["name"] == "FullProjectInputActions"

    def test_player_map_present(self, files_3d):
        ia_path = "FullProject/Assets/Settings/FullProjectInputActions.inputactions"
        data = json.loads(files_3d[ia_path])
        map_names = [m["name"] for m in data["maps"]]
        assert "Player" in map_names

    def test_ui_map_present(self, files_3d):
        ia_path = "FullProject/Assets/Settings/FullProjectInputActions.inputactions"
        data = json.loads(files_3d[ia_path])
        map_names = [m["name"] for m in data["maps"]]
        assert "UI" in map_names

    def test_move_action_in_player_map(self, files_3d):
        ia_path = "FullProject/Assets/Settings/FullProjectInputActions.inputactions"
        data = json.loads(files_3d[ia_path])
        player_map = next(m for m in data["maps"] if m["name"] == "Player")
        action_names = [a["name"] for a in player_map["actions"]]
        assert "Move" in action_names

    def test_look_action_in_3d_player_map(self, files_3d):
        ia_path = "FullProject/Assets/Settings/FullProjectInputActions.inputactions"
        data = json.loads(files_3d[ia_path])
        player_map = next(m for m in data["maps"] if m["name"] == "Player")
        action_names = [a["name"] for a in player_map["actions"]]
        assert "Look" in action_names   # 3D game

    def test_look_action_absent_from_2d(self, files_2d):
        ia_path = "PlatformerGame/Assets/Settings/PlatformerGameInputActions.inputactions"
        data = json.loads(files_2d[ia_path])
        player_map = next(m for m in data["maps"] if m["name"] == "Player")
        action_names = [a["name"] for a in player_map["actions"]]
        assert "Look" not in action_names

    def test_vr_has_trigger_not_look(self, gen, schema_vr):
        files = gen.generate(schema_vr, {})
        ia_path = "VRProject/Assets/Settings/VRProjectInputActions.inputactions"
        data = json.loads(files[ia_path])
        player_map = next(m for m in data["maps"] if m["name"] == "Player")
        action_names = [a["name"] for a in player_map["actions"]]
        assert "Trigger" in action_names
        assert "Look" not in action_names

    def test_control_schemes_include_keyboard_and_gamepad(self, files_3d):
        ia_path = "FullProject/Assets/Settings/FullProjectInputActions.inputactions"
        data = json.loads(files_3d[ia_path])
        scheme_names = [s["name"] for s in data["controlSchemes"]]
        assert "Keyboard&Mouse" in scheme_names
        assert "Gamepad" in scheme_names

    def test_mobile_adds_touch_scheme(self, gen, full_3d_schema):
        # "mobile" is the real Platform literal. Assigning "Android" here used
        # to pass only because assignment skips the field validator — a
        # validated schema can never hold a store name (see normalise_platform).
        full_3d_schema.platform = "mobile"
        files = gen.generate(full_3d_schema, {})
        ia_path = "FullProject/Assets/Settings/FullProjectInputActions.inputactions"
        data = json.loads(files[ia_path])
        scheme_names = [s["name"] for s in data["controlSchemes"]]
        assert "Touch" in scheme_names

    def test_all_action_ids_are_unique(self, files_3d):
        ia_path = "FullProject/Assets/Settings/FullProjectInputActions.inputactions"
        data = json.loads(files_3d[ia_path])
        ids: list[str] = []
        for m in data["maps"]:
            ids += [a["id"] for a in m["actions"]]
            ids += [b["id"] for b in m["bindings"]]
        assert len(ids) == len(set(ids)), "Duplicate UUIDs found in .inputactions"


# ── Prefabs ────────────────────────────────────────────────────────────────────

class TestPrefabs:
    def test_player_prefab_generated(self, files_3d):
        assert "FullProject/Assets/Prefabs/PlayerController.prefab" in files_3d

    def test_enemy_prefab_generated(self, files_3d):
        assert "FullProject/Assets/Prefabs/EnemyAI.prefab" in files_3d

    def test_no_player_prefab_when_no_player_script(self, files_minimal):
        assert not any("Player.prefab" in p for p in files_minimal)

    def test_prefab_starts_with_yaml_header(self, files_3d):
        content = files_3d["FullProject/Assets/Prefabs/PlayerController.prefab"]
        assert content.startswith("%YAML 1.1")

    def test_player_prefab_has_capsule_collider_in_3d(self, files_3d):
        content = files_3d["FullProject/Assets/Prefabs/PlayerController.prefab"]
        assert "CapsuleCollider:" in content

    def test_player_prefab_has_box_collider2d_in_2d(self, files_2d):
        # PlayerController is the player script name in 2D fixture
        content = files_2d["PlatformerGame/Assets/Prefabs/PlayerController.prefab"]
        assert "BoxCollider2D:" in content

    def test_player_prefab_references_correct_script_guid(self, files_3d):
        content = files_3d["FullProject/Assets/Prefabs/PlayerController.prefab"]
        expected_guid = _make_guid("FullProject/Scripts/PlayerController")
        assert expected_guid in content

    def test_enemy_prefab_includes_health_system_when_present(self, files_3d):
        content = files_3d["FullProject/Assets/Prefabs/EnemyAI.prefab"]
        health_guid = _make_guid("FullProject/Scripts/HealthSystem")
        assert health_guid in content

    def test_enemy_prefab_tag_is_enemy(self, files_3d):
        content = files_3d["FullProject/Assets/Prefabs/EnemyAI.prefab"]
        assert "m_TagString: Enemy" in content

    def test_enemy_prefab_layer_is_9(self, files_3d):
        content = files_3d["FullProject/Assets/Prefabs/EnemyAI.prefab"]
        assert "m_Layer: 9" in content


# ── GUID helpers ───────────────────────────────────────────────────────────────

class TestGuidHelpers:
    def test_make_guid_is_32_hex_chars(self):
        g = _make_guid("some/seed")
        assert len(g) == 32
        assert re.fullmatch(r"[0-9a-f]{32}", g)

    def test_make_guid_is_deterministic(self):
        assert _make_guid("same/seed") == _make_guid("same/seed")

    def test_make_guid_different_seeds_differ(self):
        assert _make_guid("seed/a") != _make_guid("seed/b")

    def test_make_uuid_is_uuid_v4_shaped(self):
        u = _make_uuid("proj", "test/seed")
        assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}", u)

    def test_make_uuid_deterministic(self):
        assert _make_uuid("proj", "seed") == _make_uuid("proj", "seed")


# ── Material shader per render pipeline (regression: HDRP magenta) ───────────────

class TestMaterialShaderPipeline:
    """Regression for the magenta bug: HDRP projects must use the HDRP/Lit shader,
    not the URP Lit shader (which falls back to InternalErrorShader → magenta)."""

    def test_hdrp_material_uses_hdrp_lit_shader(self):
        mat = _generate_material("Primary", "#3366CC", "HDRP", 0.4)
        assert _HDRP_LIT_GUID in mat
        assert _URP_LIT_GUID not in mat          # must NOT be URP Lit in HDRP
        assert "_BaseColor" in mat

    def test_urp_material_uses_urp_lit_shader(self):
        mat = _generate_material("Primary", "#3366CC", "URP", 0.4)
        assert _URP_LIT_GUID in mat
        assert _HDRP_LIT_GUID not in mat

    def test_builtin_material_uses_standard_shader(self):
        mat = _generate_material("Primary", "#3366CC", "builtin", 0.4)
        assert _BUILTIN_STANDARD_GUID in mat
        assert _URP_LIT_GUID not in mat
        assert _HDRP_LIT_GUID not in mat

    def test_hdrp_material_carries_base_color(self):
        # #3366CC -> (0.2, 0.4, 0.8); the color must survive into _BaseColor.
        mat = _generate_material("Primary", "#3366CC", "HDRP", 0.4)
        assert "_BaseColor: {r: 0.2, g: 0.4, b: 0.8, a: 1}" in mat

    def test_hdrp_project_materials_use_hdrp_lit(self, gen, schema_hdrp):
        files = gen.generate(schema_hdrp, {})
        mat = files["HDRPProject/Assets/Materials/Primary.mat"]
        assert _HDRP_LIT_GUID in mat
        assert _URP_LIT_GUID not in mat

    def test_urp_project_materials_use_urp_lit(self, files_3d):
        mat = files_3d["FullProject/Assets/Materials/Primary.mat"]
        assert _URP_LIT_GUID in mat
        assert _HDRP_LIT_GUID not in mat


# ── Primitive fallback material harmonization (regression: plasticky primitives) ─
# Baked palette materials (_generate_material) use smoothness 0.10-0.40 and
# Metallic 0. A spawned entity with no resolved catalog asset falls back to a
# runtime primitive colored only via `m.color = color`, leaving smoothness at
# the shader's own default (~0.5) — visibly glossier than everything else in
# the scene. EntitySpawner.cs.j2's runtime fallback must match that band.

class TestPrimitiveFallbackMaterialHarmonization:
    MANIFEST = {"com.unity.render-pipelines.universal": "14.0.9"}

    def test_primitive_fallback_sets_smoothness_and_metallic(
            self, gen, full_3d_schema):
        full_3d_schema.entities = [
            EntitySpec(name="Crate", kind="prop", count=3, placement="scatter",
                       tag="Untagged", model_hint="wooden crate"),
        ]
        files = gen.generate(full_3d_schema, self.MANIFEST)
        spawner = files["FullProject/Assets/Scripts/EntitySpawner.cs"]
        assert 'SpawnGroup("Crate"' in spawner
        assert 'm.SetFloat("_Smoothness", 0.20f)' in spawner
        assert 'm.SetFloat("_Metallic", 0f)' in spawner
        assert 'm.SetColor("_BaseColor", color)' in spawner


# ── Post-processing volume (regression: no tonemap/grade to unify a scene) ───────
# Nothing baked exposure/contrast/saturation across a scene's mismatched asset
# palettes before this — see _generate_postprocess_profile's own docstring.

class TestPostProcessVolume:
    def test_urp_project_gets_a_postprocess_profile(self, files_3d):
        assert "FullProject/Assets/Settings/PostProcessProfile.asset" in files_3d
        assert "FullProject/Assets/Settings/PostProcessProfile.asset.meta" in files_3d

    def test_profile_carries_the_expected_overrides(self, files_3d):
        prof = files_3d["FullProject/Assets/Settings/PostProcessProfile.asset"]
        assert _COLOR_ADJUSTMENTS_GUID_PP in prof
        assert _TONEMAPPING_GUID_PP in prof
        assert _VIGNETTE_GUID_PP in prof
        assert _BLOOM_GUID_PP in prof
        # Tonemapping mode 1 = Neutral (minimal hue/saturation impact, chosen
        # over ACES so it can't fight the "never black" ambient floor).
        assert "mode:\n    m_OverrideState: 1\n    m_Value: 1\n" in prof

    def test_scene_gets_a_global_volume_pointing_at_the_profile(self, gen, full_3d_schema):
        from src.unity_generator import _make_guid
        files = gen.generate(full_3d_schema, {})
        scene = files["FullProject/Assets/Scenes/Main.unity"]
        assert "PostProcessVolume" in scene
        assert "m_IsGlobal: 1" in scene
        profile_guid = _make_guid("FullProject/Settings/PostProcessProfile")
        assert f"sharedProfile: {{fileID: 11400000, guid: {profile_guid}, type: 2}}" in scene

    def test_main_camera_gets_post_processing_enabled(self, files_3d):
        scene = files_3d["FullProject/Assets/Scenes/Main.unity"]
        assert _UNIVERSAL_CAM_DATA_GUID in scene
        assert "m_RenderPostProcessing: 1" in scene

    def test_hdrp_project_gets_no_postprocess_profile(self, gen, schema_hdrp):
        files = gen.generate(schema_hdrp, {})
        pn = schema_hdrp.project_name
        assert f"{pn}/Assets/Settings/PostProcessProfile.asset" not in files
        scene = files[f"{pn}/Assets/Scenes/{schema_hdrp.scenes[0]}.unity"]
        assert "PostProcessVolume" not in scene
        assert _UNIVERSAL_CAM_DATA_GUID not in scene

    def test_output_is_deterministic(self, gen, full_3d_schema):
        first  = gen.generate(full_3d_schema, {})
        second = gen.generate(full_3d_schema, {})
        assert (first["FullProject/Assets/Settings/PostProcessProfile.asset"]
                == second["FullProject/Assets/Settings/PostProcessProfile.asset"])


# ── Player controller component guard (regression: CharacterController missing) ──

_CC_GUARD = "AddComponent<CharacterController>()"


class TestPlayerControllerGuard:
    """Regression: the 3D/VR controller calls GetComponent<CharacterController>() but
    the scene/prefab wires Rigidbody+CapsuleCollider, so the controller must self-heal
    by adding the component when it's missing (else MissingComponentException at play)."""

    def test_3d_player_controller_self_heals_character_controller(self, files_3d):
        cs = files_3d["FullProject/Assets/Scripts/PlayerController.cs"]
        assert "GetComponent<CharacterController>()" in cs
        assert _CC_GUARD in cs

    def test_vr_player_controller_self_heals_character_controller(self, gen, schema_vr):
        files = gen.generate(schema_vr, {})
        cs = files["VRProject/Assets/Scripts/PlayerController.cs"]
        assert "GetComponent<CharacterController>()" in cs
        assert _CC_GUARD in cs

    def test_2d_player_controller_has_no_character_controller(self, files_2d):
        # 2D uses Rigidbody2D — it must not reference CharacterController at all.
        cs = files_2d["PlatformerGame/Assets/Scripts/PlayerController.cs"]
        assert "CharacterController" not in cs

    def test_vr_player_controller_has_real_locomotion(self, gen, schema_vr):
        # Regression: the VR branch used to declare moveSpeed/smoothTurn/turnSpeed
        # and never read them — Update() only called ApplyGravity(). It must now
        # actually move and turn the rig off the XR thumbsticks.
        files = gen.generate(schema_vr, {})
        cs = files["VRProject/Assets/Scripts/PlayerController.cs"]
        assert "InputDevices.GetDeviceAtXRNode" in cs
        assert "CommonUsages.primary2DAxis" in cs
        assert "moveSpeed" in cs and "_cc.Move(moveDir" in cs
        assert "smoothTurn" in cs and "RotateAround" in cs


# ── Custom script channel (prompt-authored behaviors) ──────────────────────────

class TestCustomScripts:
    """type=custom renders LLM-authored fragments via CustomBehaviour.cs.j2 and
    attaches the component per attach_to — the prompt-generality channel."""

    @pytest.fixture
    def custom_schema(self):
        from src.unity_schema import UnitySchema
        return UnitySchema(
            project_name="CustomT", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main", "MainMenu"], packages=[],
            scripts=[
                UnityScript(name="PlayerController", type="player", description="m"),
                UnityScript(
                    name="DriftAssist", type="drift_system",  # unknown → custom
                    description="drift", attach_to="player",
                    extra_fields=[{"name": "grip", "type": "float", "default": "0.5f"}],
                    fixed_update_extra="grip = Mathf.Clamp01(grip);",
                ),
                UnityScript(
                    name="RaceDirector", type="custom", description="laps",
                    attach_to="systems",
                    update_extra="```\nDebug.Log(\"tick\");\n```",   # fences stripped
                    extra_methods=[{"signature": "public void Lap()", "body": "Debug.Log(\"lap\");"}],
                ),
            ],
        )

    @pytest.fixture
    def files_custom(self, gen, custom_schema):
        return gen.generate(custom_schema, {})

    def test_unknown_type_clamps_to_custom(self, custom_schema):
        assert custom_schema.scripts[1].type == "custom"

    def test_custom_script_renders_fragments(self, files_custom):
        cs = files_custom["CustomT/Assets/Scripts/DriftAssist.cs"]
        assert "public class DriftAssist : MonoBehaviour" in cs
        assert "[SerializeField] private float grip = 0.5f;" in cs
        assert "private void FixedUpdate()" in cs
        assert "Mathf.Clamp01(grip)" in cs
        assert "TODO: implement" not in cs

    def test_custom_script_lint_strips_md_fences(self, files_custom):
        cs = files_custom["CustomT/Assets/Scripts/RaceDirector.cs"]
        assert "```" not in cs
        assert 'Debug.Log("tick")' in cs
        assert "public void Lap()" in cs

    def test_stray_quote_glued_extra_method_body_still_passes_the_gate(self, gen, tmp_path):
        """Live regression, DropZone's AudioManager.cs: all three extra_methods
        on one custom script had a leading `""` glued onto the first statement
        (CS1002), and the third's body also mixed one real newline with five
        undecoded `\\n` escapes — which used to make `_code_mask`'s line-
        comment scan run to the end of the fragment (no REAL newline left to
        stop at), hiding everything after the first `//` from
        `_decode_escaped_whitespace` and from the brace counter, which then
        "balanced" a depth it could no longer see by appending a spurious `}`.
        Both defects together took the whole custom-script gate down; this
        reproduces the exact three bodies end to end through the real
        generator and the real Roslyn syntax gate."""
        from src.unity_schema import UnitySchema

        schema = UnitySchema(
            project_name="DZ", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[
                UnityScript(name="PlayerController", type="player", description="m"),
                UnityScript(
                    name="AudioManager", type="custom", attach_to="systems",
                    description="Spatial audio pool, crossfade between altitude layers.",
                    extra_fields=[
                        {"name": "_freefallAudioSource", "type": "AudioSource", "default": "null"},
                    ],
                    extra_methods=[
                        {"signature": "private void InitializeAudioSources()",
                         "body": ('""_freefallAudioSource = GetComponent<AudioSource>();\n'
                                  'if (_freefallAudioSource == null) '
                                  '_freefallAudioSource = gameObject.AddComponent<AudioSource>();\n'
                                  '_freefallAudioSource.volume = 0.5f;"')},
                        {"signature": "private void UpdateAltitudeLogic()",
                         "body": '""if (_freefallAudioSource == null) return;\n'},
                        {"signature": "private void ApplyDopplerEffect(float airspeedKmh)",
                         "body": (
                             '""if (_freefallAudioSource != null && airspeedKmh > 50f) {\n'
                             '            // Simulate Doppler shift by pitch shifting,'
                             '\\n    // otherwise rely on spatial audio properties.'
                             '\\n    _freefallAudioSource.pitch = 1f + (airspeedKmh / 200f);'
                             '\\n} else {\\n    _freefallAudioSource.pitch = 1f;\\n}"')},
                    ],
                ),
            ],
        )
        root, _ = gen.write(schema, {}, output_dir=str(tmp_path))
        cs = (root / "Assets" / "Scripts" / "AudioManager.cs").read_text(encoding="utf-8")
        assert '""' not in cs
        assert '\\n' not in cs
        assert cs.count("{") == cs.count("}")
        assert "// Simulate Doppler shift" in cs
        assert "} else {" in cs

    def test_custom_attaches_to_player_in_gameplay_scene(self, files_custom):
        s = scene(files_custom, "CustomT", "Main")
        # DriftAssist (first custom) gets fileID 900003010 hosted on Player GO.
        assert "component: {fileID: 900003010}" in s
        m = re.search(r"--- !u!114 &900003010\nMonoBehaviour:(.*?)---", s, re.S)
        assert m and "m_GameObject: {fileID: 705507900}" in m.group(1)

    def test_systems_custom_gets_holder_object(self, files_custom):
        s = scene(files_custom, "CustomT", "Main")
        assert "m_Name: _CustomScripts" in s
        m = re.search(r"--- !u!114 &900003011\nMonoBehaviour:(.*?)$", s, re.S)
        assert m and "m_GameObject: {fileID: 900003000}" in m.group(1)

    def test_menu_scene_has_no_custom_scripts(self, files_custom):
        s = scene(files_custom, "CustomT", "MainMenu")
        assert "9000030" not in s
        assert "_CustomScripts" not in s

    def test_rejects_body_left_unbalanced_by_a_later_sanitize_pass(self, gen):
        """Confirmed live (2026-08-28): ObjectThrow.cs, CS1022 — an LLM's
        update_extra was balanced going IN (lint_extensible's own
        reject-on-surplus-brace check passed it), but came out with a
        surplus `}` after the ~10 regex-rewrite _sanitize_* passes that run
        between there and here, none of which had re-verified balance.
        _reject_unbalanced_custom_bodies (the fix) must catch that and clear
        the body rather than let a syntax error reach the generated file."""
        from src.unity_schema import UnitySchema
        from src.unity_generator import _reject_unbalanced_custom_bodies

        script = UnityScript(
            name="ObjectThrow", type="custom", description="d", attach_to="systems",
            update_extra=(
                "if (hit != null) {\n"
                "    var objRb = held.GetComponent<Rigidbody2D>();\n"
                "    if (objRb != null) {\n"
                "    }\n"
                "    held.transform.SetParent(null);\n"
                "}\n"
                "held = null;\n"
                "}"   # the surplus closer — everything above this line is balanced on its own
            ),
        )
        _reject_unbalanced_custom_bodies(script)
        assert script.update_extra == ""

    def test_balanced_body_passes_through_unchanged(self, gen):
        from src.unity_generator import _reject_unbalanced_custom_bodies

        script = UnityScript(
            name="Fine", type="custom", description="d", attach_to="systems",
            update_extra="if (x != null) { x.Do(); }",
            extra_methods=[{"signature": "public void Lap()", "body": "count++;"}],
        )
        _reject_unbalanced_custom_bodies(script)
        assert script.update_extra == "if (x != null) { x.Do(); }"
        assert script.extra_methods[0].body == "count++;"

    def test_player_attach_falls_back_to_systems_without_player(self, gen):
        from src.unity_schema import UnitySchema
        schema = UnitySchema(
            project_name="NoP", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(name="Solo", type="custom", description="x",
                                 attach_to="player", update_extra="int a = 1; a++;")],
        )
        files = gen.generate(schema, {})
        s = scene(files, "NoP", "Main")
        assert "m_Name: _CustomScripts" in s

    def test_pythonic_fragment_rejected(self, gen):
        from src.unity_schema import UnitySchema
        schema = UnitySchema(
            project_name="PyR", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(name="Bad", type="custom", description="x",
                                 update_extra="def update(self): print('no')")],
        )
        files = gen.generate(schema, {})
        cs = files["PyR/Assets/Scripts/Bad.cs"]
        assert "def update" not in cs
        assert "private void Update()" not in cs   # rejected fragment → no Update emitted

    # ── Stub-typed field usage (invented OOP behavior on an empty stub class) ──
    def test_stub_type_method_calls_stripped(self, gen):
        # Regression (live Unity, AscentStickman run): the LLM invented a
        # polymorphic FSM "state object" field (ClimbStateType) with .Update()/
        # .ShouldTransition()/.NextState() methods it never defined anywhere.
        # _collect_custom_type_stubs backfills an EMPTY class for the invented
        # type (prevents CS0246), but calling methods on that empty stub is
        # guaranteed CS1061 x3, and the field's `= Idle` default (an enum-like
        # member that also doesn't exist) is CS0103. A 4th CS0103 (`SwitchState`,
        # a bare undefined method on the script itself) shared the same line and
        # must be swept up too. 2 CS0103 + 3 CS1061 observed live — this test
        # locks in that the generator neutralizes all five deterministically.
        from src.unity_schema import UnitySchema
        schema = UnitySchema(
            project_name="StubT", company_name="C", description="",
            game_type="2D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(
                name="ClimbStateFSM", type="custom", description="climb fsm",
                extra_fields=[
                    {"name": "currentState", "type": "ClimbStateType", "default": "Idle"},
                    {"name": "transitionTimer", "type": "float", "default": "0f"},
                ],
                update_extra=(
                    "if (currentState != null) { currentState.Update(this); "
                    "if (currentState.ShouldTransition()) { transitionTimer = 0.5f; } "
                    "else if (transitionTimer > 0) { transitionTimer -= Time.deltaTime; "
                    "if (transitionTimer <= 0) SwitchState(currentState.NextState()); } }"
                ),
            )],
        )
        files = gen.generate(schema, {})
        cs = files["StubT/Assets/Scripts/ClimbStateFSM.cs"]
        assert "= Idle" not in cs                    # CS0103 #1: undeclared enum-like member
        assert "SwitchState" not in cs                # CS0103 #2: undefined method
        assert ".Update(this)" not in cs              # CS1061 #1
        assert ".ShouldTransition()" not in cs         # CS1061 #2
        assert ".NextState()" not in cs                # CS1061 #3
        assert "private ClimbStateType currentState;" in cs   # field kept, defaults to null
        assert "[SerializeField] private float transitionTimer = 0f;" in cs   # unrelated field untouched

    def test_stub_type_null_conditional_call_stripped(self, gen):
        # Live bug (AutomatonCitadel run, TimelineController.cs CS1061): the
        # LLM reached a stub-typed field through the null-conditional operator
        # (`_activeTimeline?.Play()`) — the SerializeField-inspector-assigned
        # idiom — rather than a plain `.`. The old call_re required a bare
        # `\.` immediately after the field name and missed this shape entirely.
        from src.unity_schema import UnitySchema
        schema = UnitySchema(
            project_name="StubNC", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(
                name="TimelineController", type="custom", description="d",
                extra_fields=[
                    {"name": "_activeTimeline", "type": "Timeline", "default": None},
                ],
                awake_extra="_activeTimeline?.Play();",
            )],
        )
        files = gen.generate(schema, {})
        cs = files["StubNC/Assets/Scripts/TimelineController.cs"]
        assert "?.Play()" not in cs
        assert "public class Timeline" in cs   # stub still emitted (prevents CS0246)

    def test_dotted_invented_namespace_field_type_flattened(self, gen):
        # Live bug (this run, TimelineController.cs CS0246): the LLM typed a
        # field as `Timeline.TimelineClip` — dotted, so _stub_type_names
        # unconditionally treated it as "already qualified" and never stubbed
        # it. Unity's real namespace is `UnityEngine.Timeline`, not a bare
        # `Timeline`, so the field failed to resolve on the DECLARATION line
        # itself, before the CS1061 stub-call sanitizer above ever runs.
        from src.unity_schema import UnitySchema
        schema = UnitySchema(
            project_name="StubNS", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(
                name="TimelineController", type="custom", description="d",
                extra_fields=[
                    {"name": "_timelineClip", "type": "Timeline.TimelineClip", "default": None},
                ],
            )],
        )
        files = gen.generate(schema, {})
        cs = files["StubNS/Assets/Scripts/TimelineController.cs"]
        assert "Timeline.TimelineClip" not in cs                      # invented namespace gone
        assert "[SerializeField] private TimelineClip _timelineClip;" in cs  # flattened to bare leaf
        assert "public class TimelineClip" in cs                      # stub emitted (prevents CS0246)

    def test_dotted_trusted_namespace_field_type_survives(self, gen):
        # A genuinely qualified UnityEngine type must NOT be flattened or
        # stubbed — it already resolves via the real engine assembly.
        from src.unity_schema import UnitySchema
        schema = UnitySchema(
            project_name="StubTrust", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(
                name="AudioRouter", type="custom", description="d",
                extra_fields=[
                    {"name": "_mixerGroup", "type": "UnityEngine.Audio.AudioMixerGroup", "default": None},
                ],
            )],
        )
        files = gen.generate(schema, {})
        cs = files["StubTrust/Assets/Scripts/AudioRouter.cs"]
        assert "[SerializeField] private UnityEngine.Audio.AudioMixerGroup _mixerGroup;" in cs
        assert "public class AudioMixerGroup" not in cs   # never stubbed — already real

    def test_coroutine_field_wrong_type_retyped(self, gen):
        # Live bug (this run, TimelineController.cs CS0029): the field meant
        # to hold a cancel-able coroutine handle was declared as IEnumerator
        # (the routine's OWN return type) rather than Coroutine (what
        # StartCoroutine actually hands back) — assigning the call result
        # into it is an unconditional CS0029 regardless of engine version.
        from src.unity_schema import UnitySchema
        schema = UnitySchema(
            project_name="CoroT", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(
                name="TimelineController", type="custom", description="d",
                extra_fields=[
                    {"name": "_transitionCoroutine", "type": "System.Collections.IEnumerator", "default": None},
                ],
                extra_methods=[{
                    "signature": "public void TriggerSceneTransition()",
                    "body": "_transitionCoroutine = StartCoroutine(FadeToBlack(1.5f));",
                }],
            )],
        )
        files = gen.generate(schema, {})
        cs = files["CoroT/Assets/Scripts/TimelineController.cs"]
        assert "IEnumerator _transitionCoroutine" not in cs
        assert "[SerializeField] private Coroutine _transitionCoroutine;" in cs

    def test_stub_type_field_without_calls_is_untouched(self, gen):
        # A stub-typed field the LLM never calls methods on (pure data holder)
        # must not be mangled — only method-call statements are stripped.
        from src.unity_schema import UnitySchema
        schema = UnitySchema(
            project_name="StubQuiet", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(
                name="Loadout", type="custom", description="x",
                extra_fields=[{"name": "weapon", "type": "WeaponData", "default": None}],
                update_extra="transform.position += Vector3.up * Time.deltaTime;",
            )],
        )
        files = gen.generate(schema, {})
        cs = files["StubQuiet/Assets/Scripts/Loadout.cs"]
        assert "transform.position += Vector3.up * Time.deltaTime;" in cs
        assert "private WeaponData weapon;" in cs

    # ── Composed-class component calls (real class, invented member) ───────────
    def test_getcomponent_call_to_nonexistent_member_stripped(self, gen):
        # Regression (live Unity, DeepDive run): the LLM correctly used
        # GetComponent<PlayerController>() — PlayerController IS a real,
        # composed class in --compose mode — but assumed a GetSpeed() method
        # that Controller.cs.j2 never exposes (it has ForwardSpeed/SpeedKmh
        # instead), which is CS1061. Unlike the stub-type case, PlayerController
        # is a genuine class, so this needs the verified-API allowlist, not the
        # empty-stub detector.
        from src.unity_compose import MicroComposer, subsystems_for
        from src.unity_schema import UnitySchema
        schema = UnitySchema(
            project_name="O2T", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(
                name="O2Manager", type="custom", description="oxygen",
                extra_fields=[{"name": "_oxygenLevel", "type": "float", "default": "100f"}],
                update_extra=(
                    "if (_oxygenLevel > 0) _oxygenLevel -= Time.deltaTime * "
                    "(GetComponent<PlayerController>()?.GetSpeed() ?? 1f) * 0.5f; "
                    "if (_oxygenLevel < 0) _oxygenLevel = 0;"
                ),
            )],
        )
        units = MicroComposer("O2T").plan(subsystems_for("3D", []))
        files = gen.generate(schema, {}, composed_units=units)
        cs = files["O2T/Assets/Scripts/O2Manager.cs"]
        assert "GetSpeed" not in cs
        assert "[SerializeField] private float _oxygenLevel = 100f;" in cs   # field untouched

    def test_getcomponent_call_to_real_member_survives(self, gen):
        from src.unity_compose import MicroComposer, subsystems_for
        from src.unity_schema import UnitySchema
        schema = UnitySchema(
            project_name="O2Real", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(
                name="SpeedReader", type="custom", description="x",
                update_extra="float s = GetComponent<PlayerController>().ForwardSpeed;",
            )],
        )
        units = MicroComposer("O2Real").plan(subsystems_for("3D", []))
        files = gen.generate(schema, {}, composed_units=units)
        cs = files["O2Real/Assets/Scripts/SpeedReader.cs"]
        assert "ForwardSpeed" in cs

    def test_gamecontext_variable_call_to_private_member_stripped(self, gen):
        # Regression (live Unity, DeepDive run): OxygenManager.cs reached
        # PlayerHealth through the GameContext service locator (the --compose
        # bridge), not GetComponent<T>(), and stashed the result in a local
        # variable before calling the invented member on a LATER statement:
        #   var context = GameContext.Instance.Get<PlayerHealth>();
        #   if (context != null) { ...; if (_oxygenLevel <= 0f) context.Die(); }
        # Die() is `private void Die()` on Health.cs.j2 — CS0122. Neither the
        # GameContext.Instance.Get<T>() getter shape nor the assign-then-call
        # shape was covered by the original GetComponent<T>().Member() regex.
        from src.unity_compose import MicroComposer, subsystems_for
        from src.unity_schema import UnitySchema
        schema = UnitySchema(
            project_name="AbyssalDiver", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(
                name="OxygenManager", type="custom", description="oxygen",
                extra_fields=[{"name": "_oxygenLevel", "type": "float", "default": "100.0f"}],
                update_extra=(
                    "var context = GameContext.Instance.Get<PlayerHealth>(); "
                    "if (context != null) { _oxygenLevel -= _consumptionRate * "
                    "Time.deltaTime; if (_oxygenLevel <= 0f) context.Die(); }"
                ),
            )],
        )
        units = MicroComposer("AbyssalDiver").plan(subsystems_for("3D", []))
        files = gen.generate(schema, {}, composed_units=units)
        cs = files["AbyssalDiver/Assets/Scripts/OxygenManager.cs"]
        assert "Die" not in cs
        assert "[SerializeField] private float _oxygenLevel = 100.0f;" in cs

    def test_gamecontext_variable_call_to_real_member_survives(self, gen):
        from src.unity_compose import MicroComposer, subsystems_for
        from src.unity_schema import UnitySchema
        schema = UnitySchema(
            project_name="HealthReader", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(
                name="HudBridge", type="custom", description="x",
                update_extra=(
                    "var hp = GameContext.Instance.Get<PlayerHealth>(); "
                    "if (hp != null) hp.TakeDamage(1f);"
                ),
            )],
        )
        units = MicroComposer("HealthReader").plan(subsystems_for("3D", []))
        files = gen.generate(schema, {}, composed_units=units)
        cs = files["HealthReader/Assets/Scripts/HudBridge.cs"]
        assert "TakeDamage" in cs

    def test_getcomponent_call_without_compose_units_untouched(self, gen):
        # No composed_units (non --compose run) — nothing to validate against,
        # so the sanitizer must not touch anything.
        from src.unity_schema import UnitySchema
        schema = UnitySchema(
            project_name="NoCompose", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(
                name="Whatever", type="custom", description="x",
                update_extra="var x = GetComponent<PlayerController>()?.GetSpeed();",
            )],
        )
        files = gen.generate(schema, {})
        cs = files["NoCompose/Assets/Scripts/Whatever.cs"]
        assert "GetSpeed" in cs


# ── EntitySpawner path placement ────────────────────────────────────────────────

class TestEntityPath:
    def test_path_group_renders_polyline_and_waypoints(self, gen):
        from src.unity_schema import UnitySchema, EntitySpec
        schema = UnitySchema(
            project_name="PathT", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(name="PlayerController", type="player", description="m")],
            entities=[
                EntitySpec(name="Road", kind="road", count=20, shape="cube",
                           placement="path", color_role="secondary", scale=4,
                           path_points=[[0, 0, 0], [40, 2, 20], [60, 5, 60]]),
                EntitySpec(name="Trees", kind="prop", count=10, shape="cylinder",
                           placement="scatter", radius=30),
            ],
        )
        files = gen.generate(schema, {})
        cs = files["PathT/Assets/Scripts/EntitySpawner.cs"]
        assert "_path_0" in cs                                  # polyline baked
        assert "new Vector3(40.00f, 2.00f, 20.00f)" in cs       # elevation honored
        assert 'EmitWaypoints("Waypoints", _path_0)' in cs      # AI-consumable route
        assert "PointAlongPath" in cs
        assert ', _path_0, sh,' in cs                           # path passed to group
        assert ', null, sh,' in cs                              # non-path group gets null
        assert cs.count("{") == cs.count("}")


# ── EntitySpawner path tiling (path_width > 0) ──────────────────────────────

class TestTiledPath:
    def _schema(self, project="TileT", game_type="3D", path_width=10.0):
        from src.unity_schema import UnitySchema, EntitySpec
        return UnitySchema(
            project_name=project, company_name="C", description="",
            game_type=game_type, platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(name="PlayerController", type="player", description="m")],
            entities=[
                EntitySpec(name="Road", kind="road", count=20, shape="cube",
                           placement="path", color_role="secondary", scale=4,
                           path_points=[[0, 0, 0], [40, 0, 20], [60, 0, 60]],
                           path_width=path_width),
                EntitySpec(name="Trees", kind="prop", count=10, shape="cylinder",
                           placement="scatter", radius=30),
            ],
        )

    def test_tiled_group_emits_spawn_tiled_group(self, gen):
        files = gen.generate(self._schema(), {})
        cs = files["TileT/Assets/Scripts/EntitySpawner.cs"]
        assert 'SpawnTiledGroup("Road"' in cs
        assert ", _path_0, sh, 10.00f);" in cs          # path_width literal
        assert "private void SpawnTiledGroup(" in cs
        assert "ArcLength" in cs
        assert "TILE_OVERLAP = 1.02f" in cs
        assert "1, 256);" in cs                          # runtime tile-count cap
        # Waypoint emission (AI route) is untouched by tiling.
        assert 'EmitWaypoints("Waypoints", _path_0)' in cs
        # The legacy even-spread path branch must still exist for
        # path_width == 0 groups and the unmeasurable-model fallback.
        assert 'placement == "path"' in cs
        assert cs.count("{") == cs.count("}")

    def test_no_tiled_entities_no_tile_code(self, gen):
        # path_width == 0 everywhere → the entire tile branch is absent, so
        # projects without tiled paths stay byte-identical to pre-feature
        # output (same gating pattern as has_fx_entities).
        files = gen.generate(self._schema(path_width=0.0), {})
        cs = files["TileT/Assets/Scripts/EntitySpawner.cs"]
        assert "SpawnTiledGroup" not in cs
        assert "TILE_OVERLAP" not in cs
        assert "ArcLength" not in cs
        assert 'SpawnGroup("Road"' in cs

    def test_2d_never_tiles(self, gen):
        files = gen.generate(self._schema(project="TileT2D", game_type="2D",
                                          path_width=10.0), {})
        cs = files["TileT2D/Assets/Scripts/EntitySpawner.cs"]
        assert "SpawnTiledGroup" not in cs

    def test_tiled_generation_is_deterministic(self, gen):
        f1 = gen.generate(self._schema(), {})
        f2 = gen.generate(self._schema(), {})
        assert (f1["TileT/Assets/Scripts/EntitySpawner.cs"]
                == f2["TileT/Assets/Scripts/EntitySpawner.cs"])


# ── Banked path points ([x, y, z, bank]) ─────────────────────────────────────

class TestBankedPaths:
    def _schema(self, project="BankT", game_type="3D", banked=True,
                path_width=10.0):
        from src.unity_schema import UnitySchema, EntitySpec
        pts = [[0, 0, 0], [40, 2, 20, -12], [60, 5, 60, 30]] if banked else \
              [[0, 0, 0], [40, 2, 20], [60, 5, 60]]
        return UnitySchema(
            project_name=project, company_name="C", description="",
            game_type=game_type, platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(name="PlayerController", type="player", description="m")],
            entities=[
                EntitySpec(name="Road", kind="road", count=20, shape="cube",
                           placement="path", color_role="secondary", scale=4,
                           path_points=pts, path_width=path_width),
            ],
        )

    def test_bank_array_helper_and_roll_emitted(self, gen):
        files = gen.generate(self._schema(), {})
        cs = files["BankT/Assets/Scripts/EntitySpawner.cs"]
        assert "float[] _bank_0" in cs
        assert "-12.000f," in cs and "30.000f," in cs
        assert "0.000f," in cs                       # unbanked point reads 0
        assert "BankAlongPath" in cs
        assert "float[] banks = null" in cs
        assert ", _bank_0);" in cs                   # wired into the spawn call
        assert "Quaternion.Euler(0f, 0f, -bank)" in cs
        assert cs.count("{") == cs.count("}")

    def test_unbanked_projects_stay_clean(self, gen):
        # Same gating pattern as has_tiled_paths: no banked point anywhere →
        # no bank arrays, no param, no helper — byte-identical output.
        files = gen.generate(self._schema(banked=False), {})
        cs = files["BankT/Assets/Scripts/EntitySpawner.cs"]
        assert "_bank_" not in cs
        assert "BankAlongPath" not in cs
        assert "banks" not in cs

    def test_bank_ignored_without_tiling(self, gen):
        # Bank only means something to tiled surfaces; a path_width == 0
        # group keeps the even spread and emits no bank machinery.
        files = gen.generate(self._schema(path_width=0.0), {})
        cs = files["BankT/Assets/Scripts/EntitySpawner.cs"]
        assert "_bank_" not in cs
        assert "BankAlongPath" not in cs

    def test_2d_never_banks(self, gen):
        files = gen.generate(self._schema(project="BankT2D", game_type="2D"), {})
        cs = files["BankT2D/Assets/Scripts/EntitySpawner.cs"]
        assert "_bank_" not in cs

    def test_banked_generation_is_deterministic(self, gen):
        f1 = gen.generate(self._schema(), {})
        f2 = gen.generate(self._schema(), {})
        assert (f1["BankT/Assets/Scripts/EntitySpawner.cs"]
                == f2["BankT/Assets/Scripts/EntitySpawner.cs"])


# ── Baked yaws + racing player merge ─────────────────────────────────────────

class TestBakedYawsAndPlayerMerge:
    def _racing_schema(self, project="RaceT", with_player=True):
        from src.unity_racetrack import compose_racetrack
        from src.unity_schema import UnitySchema
        scripts = [UnityScript(name="GameManager", type="game_manager", description="GM")]
        if with_player:
            scripts.append(UnityScript(name="PlayerController", type="player", description="P"))
        schema = UnitySchema(
            project_name=project, company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=scripts, entities=[],
        )
        compose_racetrack(schema)
        return schema

    def test_yaw_arrays_and_param_emitted(self, gen):
        files = gen.generate(self._racing_schema(with_player=False), {})
        cs = files["RaceT/Assets/Scripts/EntitySpawner.cs"]
        assert "float[] _yaw_" in cs
        assert "float[] bakedYaws = null" in cs
        assert "modelForwardIsX" in cs
        assert cs.count("{") == cs.count("}")

    def test_no_yaws_no_param(self, gen):
        # Projects without baked facing stay byte-identical (same gating
        # pattern as has_tiled_paths / has_clamped_entities).
        from src.unity_schema import UnitySchema, EntitySpec
        schema = UnitySchema(
            project_name="NoYawT", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(name="PlayerController", type="player", description="m")],
            entities=[EntitySpec(name="Trees", kind="prop", count=3,
                                 placement="scatter", radius=10)],
        )
        files = gen.generate(schema, {})
        cs = files["NoYawT/Assets/Scripts/EntitySpawner.cs"]
        assert "bakedYaws" not in cs
        assert "_yaw_" not in cs
        assert "modelForwardIsX" not in cs

    def test_player_merge_moves_scene_player_to_pole_position(self, gen):
        files = gen.generate(self._racing_schema(), {})
        scene_yaml = scene(files, "RaceT", "Main")
        # Pole position (36, 0, 42) + capsule pivot 1 m up, facing yaw 0.
        assert "m_LocalPosition: {x: 36.0, y: 1.0, z: 42.0}" in scene_yaml
        assert "m_LocalRotation: {x: 0, y: 0.0, z: 0, w: 1.0}" in scene_yaml

    def test_player_merge_skips_static_kart_group(self, gen):
        files = gen.generate(self._racing_schema(), {})
        cs = files["RaceT/Assets/Scripts/EntitySpawner.cs"]
        # The static PlayerKart prop is merged INTO the playable Player —
        # spawning it too would leave two Player-tagged objects.
        assert 'SpawnGroup("PlayerKart"' not in cs
        assert 'SpawnGroup("OpponentKarts"' in cs

    def test_no_player_script_keeps_kart_group(self, gen):
        files = gen.generate(self._racing_schema(with_player=False), {})
        cs = files["RaceT/Assets/Scripts/EntitySpawner.cs"]
        assert 'SpawnGroup("PlayerKart"' in cs

    def test_non_racing_player_stays_at_origin(self, gen, files_3d):
        scene_yaml = scene(files_3d, "FullProject", "Main")
        assert "m_LocalPosition: {x: 0, y: 1, z: 0}" in scene_yaml

    def test_merge_is_deterministic(self, gen):
        f1 = gen.generate(self._racing_schema(), {})
        f2 = gen.generate(self._racing_schema(), {})
        assert f1["RaceT/Assets/Scenes/Main.unity"] == f2["RaceT/Assets/Scenes/Main.unity"]
        assert (f1["RaceT/Assets/Scripts/EntitySpawner.cs"]
                == f2["RaceT/Assets/Scripts/EntitySpawner.cs"])


# ── Scale consistency: bounds-fit wiring (3D only) ──────────────────────────

class TestBoundsFit:
    def test_compute_fit_factor_helper_emitted_for_3d(self, gen):
        from src.unity_schema import UnitySchema, EntitySpec
        schema = UnitySchema(
            project_name="FitT3D", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(name="PlayerController", type="player", description="m")],
            entities=[EntitySpec(name="Trees", kind="prop", count=3,
                                 placement="scatter", radius=10)],
        )
        files = gen.generate(schema, {})
        cs = files["FitT3D/Assets/Scripts/EntitySpawner.cs"]
        assert "TryMeasureBounds" in cs
        assert "fitSize" in cs
        # No Sketchfab pick in this schema -> fit_size defaults to 0.0000f.
        # The positions argument that follows is deliberately NOT asserted here:
        # the generator now re-runs the layout pass itself once models resolve
        # (model-aware group spacing, see _entity_footprints), so an entity that
        # resolves to a measured model gets baked positions even when the caller
        # skipped the pipeline. This test is about the bounds-fit argument.
        assert "0.0000f, " in cs

    def test_bounds_fit_not_emitted_for_2d(self, gen):
        from src.unity_schema import UnitySchema, EntitySpec
        schema = UnitySchema(
            project_name="FitT2D", company_name="C", description="",
            game_type="2D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(name="PlayerController", type="player", description="m")],
            entities=[EntitySpec(name="Trees", kind="prop", count=3,
                                 placement="scatter", radius=10)],
        )
        files = gen.generate(schema, {})
        cs = files["FitT2D/Assets/Scripts/EntitySpawner.cs"]
        assert "TryMeasureBounds" not in cs

    def _fitt3d_files(self, gen):
        from src.unity_schema import UnitySchema, EntitySpec
        schema = UnitySchema(
            project_name="FitT3D", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(name="PlayerController", type="player", description="m")],
            entities=[EntitySpec(name="Trees", kind="prop", count=3,
                                 placement="scatter", radius=10)],
        )
        return gen.generate(schema, {})

    def test_spacing_and_radius_are_bounds_derived_minimums(self, gen):
        # spacing/radius must flow through effectiveSpacing/effectiveRadius —
        # never used as literal PlaceAt args directly (that was the fixed
        # spacing-ignores-model-size bug behind overlapping entities).
        cs = self._fitt3d_files(gen)["FitT3D/Assets/Scripts/EntitySpawner.cs"]
        assert "effectiveSpacing" in cs and "effectiveRadius" in cs
        assert "PlaceAt(placement, i, count, effectiveSpacing, effectiveRadius)" in cs
        assert "Mathf.Max(spacing, footprintXZ" in cs

    def test_ground_snap_uses_measured_pivot_offset(self, gen):
        cs = self._fitt3d_files(gen)["FitT3D/Assets/Scripts/EntitySpawner.cs"]
        assert "pivotOffsetY" in cs
        assert "pos.y += pivotOffsetY * finalScale;" in cs
        # The old fixed-half-height heuristic must be gone from the 3D path.
        assert "pos.y += scale * 0.5f;" not in cs

    def test_clamped_entity_emits_negative_sentinel_and_clamp_branch(self, gen):
        # kind=vehicle resolves through the catalog with kind_sizes.json's
        # clamp_sizes {vehicle: 13.5} → fit arg -13.5000f + the shrink-only
        # clamp branch. Uses the real lookups dir on purpose (locks in the
        # shipped table, same as the other catalog-integration tests).
        from src.unity_schema import UnitySchema, EntitySpec
        schema = UnitySchema(
            project_name="ClampT", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(name="PlayerController", type="player", description="m")],
            entities=[EntitySpec(name="RivalKarts", kind="vehicle", count=3,
                                 placement="grid", model_hint="kart racer vehicle")],
        )
        cs = gen.generate(schema, {})["ClampT/Assets/Scripts/EntitySpawner.cs"]
        assert "-13.5000f" in cs
        assert "Mathf.Min(1f, -fitSize / maxDim)" in cs

    def test_no_clamped_entities_no_clamp_branch(self, gen):
        # Props have no clamp_sizes entry → the clamp branch is absent and
        # unclamped projects stay byte-identical to pre-feature output.
        cs = self._fitt3d_files(gen)["FitT3D/Assets/Scripts/EntitySpawner.cs"]
        assert "Mathf.Min(1f, -fitSize / maxDim)" not in cs
        assert "-13.5000f" not in cs


# ── Environment mood (sun + fog) ────────────────────────────────────────────────

class TestEnvironmentMood:
    def _schema(self, environment):
        from src.unity_schema import UnitySchema
        return UnitySchema(
            project_name="EnvT", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(name="GameManager", type="game_manager", description="g")],
            environment=environment,
        )

    def test_sunset_swings_and_tints_the_sun(self, gen):
        from src.unity_schema import EnvironmentSpec
        files = gen.generate(self._schema(EnvironmentSpec(time_of_day="sunset", fog=True)), {})
        s = files["EnvT/Assets/Scenes/Main.unity"]
        assert "m_Color: {r: 1.0, g: 0.45, b: 0.25, a: 1}" in s   # sunset orange
        assert "m_LocalEulerAnglesHint: {x: 8.0, y: -30.0, z: 0}" in s  # low sun
        assert "m_Fog: 1" in s
        assert "{x: 0.40821788" not in s                          # day quat replaced

    def test_no_environment_keeps_historical_lighting(self, gen):
        files = gen.generate(self._schema(None), {})
        s = files["EnvT/Assets/Scenes/Main.unity"]
        assert "m_LocalRotation: {x: 0.40821788, y: -0.23456968, z: 0.10938163, w: 0.8754261}" in s
        assert "m_LocalEulerAnglesHint: {x: 50, y: -30, z: 0}" in s


# ── Model Pack v1 (default models replace primitives) ──────────────────────────

class TestModelPack:
    def _schema(self, **kw):
        from src.unity_schema import UnitySchema
        base = dict(
            project_name="MP", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(name="PlayerController", type="player", description="m"),
                     UnityScript(name="EnemyAI", type="enemy", description="e")],
        )
        base.update(kw)
        return UnitySchema(**base)

    def test_static_pack_model_downgrades_to_generic_even_in_3d(self, gen):
        # Regression: the static default pack's Player.fbx/Enemy.fbx are
        # confirmed to carry NO real humanoid skeleton (verified: zero
        # Hips/Spine/UpperLeg/LowerLeg bone names in the actual FBX binaries)
        # despite the catalog labeling them rig="humanoid". Forcing Humanoid
        # on them throws "Required human bone 'LeftLowerLeg' not found" on
        # EVERY game_type that isn't 2D/2.5D (hit live on a VR project) — the
        # old fix only downgraded for 2D, which was too narrow. Without a
        # Blender-built override, this must downgrade to Generic regardless
        # of game_type, and SetupDefaultModels.cs (which force-reimports as
        # Humanoid) must not even be emitted.
        files = gen.generate(self._schema(game_type="3D"), {})
        meta = files.get("MP/Assets/Resources/Models/Default/Player/Player.fbx.meta", "")
        assert "animationType: 2" in meta             # Generic, not Humanoid
        assert "animationType: 3" not in meta
        assert "MP/Assets/Scripts/ModelSwap.cs" in files
        assert "MP/Assets/Editor/SetupDefaultModels.cs" not in files
        s = scene(files, "MP", "Main")
        assert "modelPath: Models/Default/Player/Player" in s
        assert "controllerPath: Models/Default/DefaultCharacter" in s

    def test_blender_built_override_keeps_humanoid_rig(self, gen, tmp_path):
        # A genuine Blender-built override (see _apply_model_overrides) DOES
        # ship a real named skeleton, so that specific kind is trusted to stay
        # Humanoid even outside 2D — only the static-pack fallback downgrades.
        fake_fbx = tmp_path / "BuiltPlayer.fbx"
        fake_fbx.write_bytes(b"fake")
        files = gen.generate(self._schema(game_type="3D"), {},
                             model_overrides={"player": str(fake_fbx)})
        # dest_rel/resource_path are inherited from the existing catalog entry
        # (_apply_model_overrides only swaps src+rig) — still Player/Player.fbx.
        meta = files.get("MP/Assets/Resources/Models/Default/Player/Player.fbx.meta", "")
        assert "animationType: 3" in meta
        assert "MP/Assets/Editor/SetupDefaultModels.cs" in files

    def test_missing_pack_degrades_to_primitives(self, gen):
        files = gen.generate(self._schema(), {}, model_source_dir="does/not/exist")
        assert not any("ModelSwap" in p for p in files)
        s = scene(files, "MP", "Main")
        assert "modelPath:" not in s                 # primitives untouched

    def test_drive_mode_skips_player_model_swap(self, gen):
        files = gen.generate(self._schema(locomotion="drive"), {})
        s = scene(files, "MP", "Main")
        # The car IS the player visual — no humanoid swap on the Player GO.
        assert "modelPath: Models/Default/Player/Player" not in s

    def test_entity_groups_get_model_paths(self, gen):
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(entities=[
            EntitySpec(name="Guards", kind="enemy", count=3, tag="Enemy"),
            EntitySpec(name="Crowd", kind="crowd", count=5),
        ]), {})
        cs = files["MP/Assets/Scripts/EntitySpawner.cs"]
        assert '"Models/Default/Enemy/Enemy"' in cs        # enemy model exists in seed
        assert '"Models/Default/Crowd/Spectator"' in cs    # crowd now resolves to a real model (no primitive)

    # ── 2.5D coherence + fail-fast (no-generic-defaults doctrine) ──────────────
    def test_2d_player_uses_generic_rig_not_humanoid(self, gen):
        # 2D games render in 2.5D — never a Humanoid avatar (LeftLowerLeg crash).
        files = gen.generate(self._schema(game_type="2D"), {})
        meta = files.get("MP/Assets/Resources/Models/Default/Player/Player.fbx.meta", "")
        assert "animationType: 2" in meta            # Generic
        assert "animationType: 3" not in meta        # never Humanoid

    def test_2d_entityspawner_uses_collider2d(self, gen):
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(game_type="2D", entities=[
            EntitySpec(name="Crates", kind="prop", count=4),
        ]), {})
        cs = files["MP/Assets/Scripts/EntitySpawner.cs"]
        assert "BoxCollider2D" in cs                  # 2D physics collider, not 3D
        assert "XY side-on plane" in cs              # placed in the side-on plane

    def test_2_5d_player_keeps_humanoid_rig(self, gen, tmp_path):
        """2.5D is a real 3D world/player (see GOLDEN_2_5D_CORE_SIGNATURE) — a
        genuine Blender-built humanoid override must NOT be downgraded like 2D."""
        fake_fbx = tmp_path / "BuiltPlayer.fbx"
        fake_fbx.write_bytes(b"fake")
        files = gen.generate(self._schema(game_type="2.5D"), {},
                             model_overrides={"player": str(fake_fbx)})
        meta = files.get("MP/Assets/Resources/Models/Default/Player/Player.fbx.meta", "")
        assert "animationType: 3" in meta             # Humanoid retained
        assert "MP/Assets/Editor/SetupDefaultModels.cs" in files

    def test_2_5d_entityspawner_uses_3d_colliders_not_collider2d(self, gen):
        """2.5D's player is a 3D CharacterController — world entities must use
        real 3D colliders (a Collider2D would never collide with it)."""
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(game_type="2.5D", entities=[
            EntitySpec(name="Crates", kind="prop", count=4),
        ]), {})
        cs = files["MP/Assets/Scripts/EntitySpawner.cs"]
        assert "BoxCollider2D" not in cs
        assert "Collider2D" not in cs
        assert "XY side-on plane" not in cs

    def test_2_5d_enemy_prefab_uses_3d_collider(self, gen):
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(
            game_type="2.5D",
            entities=[EntitySpec(name="Guards", kind="enemy", count=2, tag="Enemy")],
        ), {})
        prefab = files.get("MP/Assets/Prefabs/EnemyAI.prefab", "")
        assert prefab
        assert "CircleCollider2D" not in prefab
        assert "Rigidbody2D" not in prefab
        assert "SphereCollider" in prefab
        assert "\nRigidbody:" in prefab

    def test_scenesetup_has_no_genre_boilerplate(self, gen):
        files = gen.generate(self._schema(), {})
        ss = files["MP/Assets/Scripts/SceneSetup.cs"]
        for banned in ("SURVIVAL HORROR", "Warden", "YOU ESCAPED", "Level1", "BossRoom"):
            assert banned not in ss, f"SceneSetup still emits boilerplate: {banned}"

    def test_strict_unmatched_kind_hard_fails(self, gen):
        # FAIL-FAST doctrine: in strict mode an entity matching neither the
        # default pack nor the asset catalog nor any CONCEPT LAYER stops the
        # build with an actionable error — it must NOT silently share one
        # generic prop. (kind="gizmo"/name="Zzgrbl" is genuinely unmappable —
        # unlike e.g. "dragon", which the creature concept layer now resolves.)
        from src.unity_schema import EntitySpec
        with pytest.raises(ValueError, match="assets.strict"):
            gen.generate(self._schema(entities=[
                EntitySpec(name="Zzgrbl", kind="gizmo", count=1),
            ]), {}, strict_assets=True)

    def test_nonstrict_unmatched_kind_falls_back_to_real_prop(self, gen):
        # Non-strict keeps the old graceful path: a REAL default prop (never a
        # primitive), logged so a fitting model can be added later. Uses a
        # genuinely unmappable entity so no concept layer rescues it first.
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(entities=[
            EntitySpec(name="Zzgrbl", kind="gizmo", count=1),
        ]), {}, strict_assets=False)
        cs = files["MP/Assets/Scripts/EntitySpawner.cs"]
        assert '"Models/Default/Prop/Prop"' in cs        # real prop fallback, not a primitive

    def test_strict_assets_raises_when_library_missing(self, gen):
        # Strict hard-fails when there is nothing real to resolve to at all.
        from src.unity_schema import EntitySpec
        with pytest.raises(ValueError, match="assets.strict"):
            gen.generate(self._schema(entities=[EntitySpec(name="X", kind="prop", count=1)]),
                         {}, model_source_dir="does/not/exist", strict_assets=True)

    def test_2d_prop_fallback_still_downgrades_humanoid_rig(self, gen, monkeypatch):
        # Regression: the humanoid->generic downgrade for 2D games used to run
        # BEFORE the prop-fallback loop that can insert a fresh used_models entry
        # via setdefault("prop", ...). If that fallback resolves to a humanoid
        # model (e.g. no "prop" kind in the catalog, so the first model wins),
        # it bypassed the downgrade entirely and re-triggered the "Required human
        # bone 'LeftLowerLeg'" Avatar crash for a 2D project. The downgrade must
        # run AFTER every used_models insertion, including this late one.
        import src.unity_generator as ug
        from src.unity_schema import EntitySpec
        real_resolve = ug.resolve_default_models

        def no_prop_catalog(*a, **kw):
            models = dict(real_resolve(*a, **kw))
            models.pop("prop", None)
            return models
        monkeypatch.setattr(ug, "resolve_default_models", no_prop_catalog)

        files = gen.generate(self._schema(
            game_type="2D",
            entities=[EntitySpec(name="Weirdo", kind="totally_unmapped_kind", count=1)],
        ), {})
        metas = {p: c for p, c in files.items() if p.endswith(".fbx.meta")}
        assert metas, "expected at least one model .meta to be emitted"
        for path, meta in metas.items():
            assert "animationType: 3" not in meta, f"{path} still imports as Humanoid in a 2D project"
        assert not any("SetupDefaultModels" in p for p in files)


# ── Non-compose ("vanilla") 2.5D templates ──────────────────────────────────
# unity_cli.py's `new` command has no --compose flag, so it can only ever reach
# templates/unity/scripts/*.cs.j2 + Player.prefab.j2 + scene.unity.j2 — a
# SEPARATE template family from the --compose micro pipeline (unity_compose.py
# + templates/unity/micro/). Both used to bundle game_type in ("2D", "2.5D")
# identically; the compose path was fixed first (GOLDEN_2_5D_CORE_SIGNATURE),
# but this vanilla path was missed, so unity_cli.py runs still built a 2.5D
# player as Rigidbody2D even after the compose path was real 3D — a mismatch
# with the (already-fixed) 3D entity/enemy colliders that would leave the
# player unable to collide with the world at all.

class TestNonCompose25D:
    def _schema(self, **kw):
        from src.unity_schema import UnitySchema
        base = dict(
            project_name="Vanilla25D", company_name="C", description="",
            game_type="2.5D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=["navmesh"], scenes=["Main"], packages=[],
            scripts=[UnityScript(name="PlayerController", type="player", description="m"),
                     UnityScript(name="CameraFollow", type="camera", description="c"),
                     UnityScript(name="EnemyAI", type="enemy", description="e"),
                     UnityScript(name="Loot", type="collectible", description="l")],
        )
        base.update(kw)
        return UnitySchema(**base)

    def test_player_controller_is_character_controller_not_rigidbody2d(self, gen):
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(
            entities=[EntitySpec(name="Guards", kind="enemy", count=1, tag="Enemy")]), {})
        pc = files["Vanilla25D/Assets/Scripts/PlayerController.cs"]
        assert "CharacterController _cc" in pc
        assert "GetComponent<Rigidbody2D>" not in pc
        assert "_velocity.z = 0f;" in pc     # depth locked, lateral-only input

    def test_camera_follow_stays_perspective(self, gen):
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(
            entities=[EntitySpec(name="Guards", kind="enemy", count=1, tag="Enemy")]), {})
        cam = files["Vanilla25D/Assets/Scripts/CameraFollow.cs"]
        assert "GetComponent<Rigidbody2D>" not in cam
        assert "orthographic" not in cam.lower()

    def test_enemy_ai_uses_navmesh_not_rigidbody2d(self, gen):
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(
            entities=[EntitySpec(name="Guards", kind="enemy", count=1, tag="Enemy")]), {})
        enemy = files["Vanilla25D/Assets/Scripts/EnemyAI.cs"]
        assert "NavMeshAgent" in enemy
        assert "GetComponent<Rigidbody2D>" not in enemy

    def test_collectible_uses_3d_trigger_api(self, gen):
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(
            entities=[EntitySpec(name="Guards", kind="enemy", count=1, tag="Enemy")]), {})
        loot = files["Vanilla25D/Assets/Scripts/Loot.cs"]
        assert "OnTriggerEnter2D" not in loot
        assert "OnTriggerEnter(Collider other)" in loot

    def test_player_and_enemy_prefabs_use_3d_colliders(self, gen):
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(
            entities=[EntitySpec(name="Guards", kind="enemy", count=1, tag="Enemy")]), {})
        pprefab = files["Vanilla25D/Assets/Prefabs/PlayerController.prefab"]
        assert "BoxCollider2D" not in pprefab and "CapsuleCollider" in pprefab
        eprefab = files["Vanilla25D/Assets/Prefabs/EnemyAI.prefab"]
        assert "CircleCollider2D" not in eprefab and "SphereCollider" in eprefab

    def test_scene_unity_has_no_2d_collider_leaks(self, gen):
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(
            entities=[EntitySpec(name="Guards", kind="enemy", count=1, tag="Enemy")]), {})
        s = scene(files, "Vanilla25D", "Main")
        assert "BoxCollider2D" not in s
        assert "Rigidbody2D" not in s


# ── Non-compose ("vanilla") 2D side-scroller — parity audit ────────────────────
# game_type="2D" already IS the side-scroller archetype end-to-end (unlike the
# 2.5D gap above, this was never bundled with anything else) — these tests lock
# that in as a regression baseline with the same coverage 2.5D now has, rather
# than assuming it stays correct forever.

class TestNonCompose2DSideScroller:
    def _schema(self, **kw):
        from src.unity_schema import UnitySchema
        base = dict(
            project_name="Vanilla2D", company_name="C", description="",
            game_type="2D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=["navmesh"], scenes=["Main"], packages=[],
            scripts=[UnityScript(name="PlayerController", type="player", description="m"),
                     UnityScript(name="CameraFollow", type="camera", description="c"),
                     UnityScript(name="EnemyAI", type="enemy", description="e"),
                     UnityScript(name="Loot", type="collectible", description="l")],
        )
        base.update(kw)
        return UnitySchema(**base)

    def test_player_controller_uses_rigidbody2d(self, gen):
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(
            entities=[EntitySpec(name="Guards", kind="enemy", count=1, tag="Enemy")]), {})
        pc = files["Vanilla2D/Assets/Scripts/PlayerController.cs"]
        assert "GetComponent<Rigidbody2D>" in pc

    def test_enemy_ai_never_uses_navmesh_stays_rigidbody2d(self, gen):
        # "navmesh" feature present, but 2D is deliberately excluded from the
        # NavMesh-eligible game_type set — a 2D game has no meaningful NavMesh
        # surface, so it must always fall to the Rigidbody2D chase branch.
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(
            entities=[EntitySpec(name="Guards", kind="enemy", count=1, tag="Enemy")]), {})
        enemy = files["Vanilla2D/Assets/Scripts/EnemyAI.cs"]
        assert "NavMeshAgent" not in enemy
        assert "GetComponent<Rigidbody2D>" in enemy

    def test_collectible_uses_2d_trigger_api(self, gen):
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(
            entities=[EntitySpec(name="Guards", kind="enemy", count=1, tag="Enemy")]), {})
        loot = files["Vanilla2D/Assets/Scripts/Loot.cs"]
        assert "OnTriggerEnter2D" in loot

    def test_player_and_enemy_prefabs_use_2d_colliders(self, gen):
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(
            entities=[EntitySpec(name="Guards", kind="enemy", count=1, tag="Enemy")]), {})
        pprefab = files["Vanilla2D/Assets/Prefabs/PlayerController.prefab"]
        assert "BoxCollider2D" in pprefab
        eprefab = files["Vanilla2D/Assets/Prefabs/EnemyAI.prefab"]
        assert "CircleCollider2D" in eprefab

    def test_scene_unity_is_orthographic(self, gen):
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(
            entities=[EntitySpec(name="Guards", kind="enemy", count=1, tag="Enemy")]), {})
        s = scene(files, "Vanilla2D", "Main")
        assert "orthographic: 1" in s
        assert "BoxCollider2D" in s


# ── Non-compose ("vanilla") 2D top-down ─────────────────────────────────────────
# game_type="2d_topdown" is a new archetype: flat Rigidbody2D like "2D", but free
# 2-axis movement (no gravity/jump) instead of a side-scroller. Must NOT alias
# onto "2D"'s BuildSideScroller2D (ground-strip/course-lane layout — wrong shape
# for an open top-down arena) nor onto the existing (real-3D) "top_down" game_type.

class TestNonCompose2DTopdown:
    def _schema(self, **kw):
        from src.unity_schema import UnitySchema
        base = dict(
            project_name="Vanilla2DTop", company_name="C", description="",
            game_type="2d_topdown", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=["navmesh"], scenes=["Main"], packages=[],
            scripts=[UnityScript(name="PlayerController", type="player", description="m"),
                     UnityScript(name="CameraFollow", type="camera", description="c"),
                     UnityScript(name="EnemyAI", type="enemy", description="e"),
                     UnityScript(name="Loot", type="collectible", description="l")],
        )
        base.update(kw)
        return UnitySchema(**base)

    def test_player_controller_free_2_axis_no_gravity(self, gen):
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(
            entities=[EntitySpec(name="Guards", kind="enemy", count=1, tag="Enemy")]), {})
        pc = files["Vanilla2DTop/Assets/Scripts/PlayerController.cs"]
        assert "GetComponent<Rigidbody2D>" in pc
        assert "gravityScale = 0f" in pc
        assert "CharacterController" not in pc
        assert "jumpForce" not in pc     # no jump concept for free top-down movement

    def test_enemy_ai_never_uses_navmesh_stays_rigidbody2d(self, gen):
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(
            entities=[EntitySpec(name="Guards", kind="enemy", count=1, tag="Enemy")]), {})
        enemy = files["Vanilla2DTop/Assets/Scripts/EnemyAI.cs"]
        assert "NavMeshAgent" not in enemy
        assert "GetComponent<Rigidbody2D>" in enemy

    def test_collectible_uses_2d_trigger_api(self, gen):
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(
            entities=[EntitySpec(name="Guards", kind="enemy", count=1, tag="Enemy")]), {})
        loot = files["Vanilla2DTop/Assets/Scripts/Loot.cs"]
        assert "OnTriggerEnter2D" in loot

    def test_player_and_enemy_prefabs_use_2d_colliders(self, gen):
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(
            entities=[EntitySpec(name="Guards", kind="enemy", count=1, tag="Enemy")]), {})
        pprefab = files["Vanilla2DTop/Assets/Prefabs/PlayerController.prefab"]
        assert "BoxCollider2D" in pprefab
        eprefab = files["Vanilla2DTop/Assets/Prefabs/EnemyAI.prefab"]
        assert "CircleCollider2D" in eprefab

    def test_scene_unity_is_orthographic(self, gen):
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(
            entities=[EntitySpec(name="Guards", kind="enemy", count=1, tag="Enemy")]), {})
        s = scene(files, "Vanilla2DTop", "Main")
        assert "orthographic: 1" in s

    def test_entityspawner_uses_topdown_archetype_not_sidescroller(self, gen):
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(
            entities=[EntitySpec(name="Guards", kind="enemy", count=3, tag="Enemy", placement="scatter")]
        ), {})
        cs = files["Vanilla2DTop/Assets/Scripts/EntitySpawner.cs"]
        assert "BuildTopDown2D" in cs
        assert "BuildSideScroller2D" not in cs
        assert cs.count("{") == cs.count("}")

    def test_humanoid_rig_downgraded_like_2d(self, gen, tmp_path):
        # 2d_topdown is flat-sprite like "2D" — a genuine Blender-built humanoid
        # override must still downgrade to Generic here (unlike 2.5D, which keeps it).
        fake_fbx = tmp_path / "BuiltPlayer.fbx"
        fake_fbx.write_bytes(b"fake")
        files = gen.generate(self._schema(), {}, model_overrides={"player": str(fake_fbx)})
        meta = files.get("Vanilla2DTop/Assets/Resources/Models/Default/Player/Player.fbx.meta", "")
        assert "animationType: 2" in meta
        assert "animationType: 3" not in meta


# ── write() is atomic per file (torn-write / duplicate-fileID hardening) ───────
# Regression (live Unity): regenerating into a project the Editor already has
# open races its file watcher — a plain write_text() truncates the file to 0
# bytes before writing new content, and a reimport inside that window sees a
# truncated file ("Parser Failure ... Expect ':' between key and value" /
# "same file identifier (100100000) for multiple object types"). write() must
# stage each file and swap it into place so the Editor only ever sees a
# complete file.

class TestAtomicWrite:
    def _schema(self):
        from src.unity_schema import UnitySchema
        return UnitySchema(
            project_name="AtomicWrite", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(name="PlayerController", type="player", description="m")],
        )

    def test_no_tmp_files_left_behind(self, gen, tmp_path):
        gen.write(self._schema(), {}, output_dir=str(tmp_path))
        leftovers = list(tmp_path.rglob("*.tmp"))
        assert not leftovers, f"temp files not cleaned up: {leftovers}"

    def test_written_content_matches_generated_content(self, gen, tmp_path):
        root, _ = gen.write(self._schema(), {}, output_dir=str(tmp_path))
        prefab = root / "Assets" / "Prefabs" / "PlayerController.prefab"
        assert prefab.exists()
        content = prefab.read_text(encoding="utf-8")
        assert "PrefabAsset:" in content
        assert content.count("{") == content.count("}")

    def test_regenerating_over_existing_project_replaces_atomically(self, gen, tmp_path):
        # Second write over the same output_dir (the normal "regenerate and
        # look at it in Unity again" loop) must fully replace file content,
        # never interleave/append old + new.
        gen.write(self._schema(), {}, output_dir=str(tmp_path))
        root, _ = gen.write(self._schema(), {}, output_dir=str(tmp_path))
        prefab = root / "Assets" / "Prefabs" / "PlayerController.prefab"
        content = prefab.read_text(encoding="utf-8")
        # A torn/duplicated write would double the PrefabAsset document header.
        assert content.count("--- !u!1001 &1001000000") == 1


# ── Stale-output sweep ────────────────────────────────────────────────────────
# Regression (live Unity, Monaco monobike): write() renders into the project
# directory in place, and Unity compiles EVERY .cs under Assets/ — not just the
# ones in the current build. A 14:57 run produced MonobikeController/MonobikeAI;
# the 15:36 regeneration of the same directory produced PlayerMonobikeController/
# RivalAIController instead, and the Editor kept reporting CS0236 in two files
# the current schema does not contain and nothing references.

class TestStaleSweep:
    def _schema(self, script_name: str):
        from src.unity_schema import UnitySchema
        return UnitySchema(
            project_name="Sweep", company_name="C", description="",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(name=script_name, type="player", description="m")],
        )

    def test_renamed_script_does_not_survive_a_regeneration(self, gen, tmp_path):
        """The live defect, end to end."""
        root, _ = gen.write(self._schema("MonobikeController"), {},
                            output_dir=str(tmp_path))
        old = root / "Assets" / "Scripts" / "MonobikeController.cs"
        assert old.is_file()

        gen.write(self._schema("PlayerMonobikeController"), {},
                  output_dir=str(tmp_path))
        assert not old.exists()
        assert (root / "Assets" / "Scripts" / "PlayerMonobikeController.cs").is_file()

    def test_sweep_only_touches_files_it_generated_itself(self, gen, tmp_path):
        root = tmp_path
        def touch(rel: str):
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("// x", encoding="utf-8")
            return p

        orphan      = touch("Demo/Assets/Scripts/MonobikeAI.cs")
        orphan_meta = touch("Demo/Assets/Scripts/MonobikeAI.cs.meta")
        kept        = touch("Demo/Assets/Scripts/RivalAIController.cs")
        handmade    = touch("Demo/Assets/Scripts/MyOwnScript.cs")

        first = {"Demo/Assets/Scripts/MonobikeAI.cs",
                 "Demo/Assets/Scripts/RivalAIController.cs"}
        assert gen._sweep_stale_outputs(root, "Demo", first) == []
        assert orphan.is_file(), "nothing to sweep before a manifest exists"

        removed = gen._sweep_stale_outputs(
            root, "Demo", {"Demo/Assets/Scripts/RivalAIController.cs"})
        assert removed == ["Demo/Assets/Scripts/MonobikeAI.cs"]
        assert not orphan.exists()
        assert not orphan_meta.exists(), "a .meta outlives nothing"
        assert kept.is_file()
        assert handmade.is_file(), "never generated by us, never swept"

    def test_a_gate_failing_run_still_records_what_it_wrote(self, gen, tmp_path):
        """Live (Chromabreak): the sweep is manifest-driven and the manifest was
        written AFTER the syntax gate, so a run that raised at the gate left its
        files on disk having recorded nothing — invisible to every later sweep.
        A LATER, clean build of the same project then failed the real Unity
        compile on the aborted run's broken script."""
        from src.csharp_syntax_gate import CSharpSyntaxError

        root, _ = gen.write(self._schema("FirstController"), {},
                            output_dir=str(tmp_path))
        first = root / "Assets" / "Scripts" / "FirstController.cs"
        assert first.is_file()

        # A run whose gate raises: its files land, then it aborts.
        with mock.patch("src.csharp_syntax_gate.enforce",
                        side_effect=CSharpSyntaxError(["boom"])):
            with pytest.raises(CSharpSyntaxError):
                gen.write(self._schema("AbortedController"), {},
                          output_dir=str(tmp_path))
        aborted = root / "Assets" / "Scripts" / "AbortedController.cs"
        assert aborted.is_file(), "the aborted run's file is on disk"
        assert not first.exists(), "and it swept the run before it"

        # The next clean run must be able to sweep the wreckage.
        gen.write(self._schema("ThirdController"), {}, output_dir=str(tmp_path))
        assert not aborted.exists(), "the aborted run's orphan is gone"
        assert (root / "Assets" / "Scripts" / "ThirdController.cs").is_file()

    def test_manifest_entry_escaping_the_root_is_ignored(self, gen, tmp_path):
        """The manifest is our own output, but it is a file on disk — a path
        that walks out of the project must never be followed."""
        outsider = tmp_path / "outside.cs"
        outsider.write_text("keep me", encoding="utf-8")
        root = tmp_path / "out"
        (root / "Demo").mkdir(parents=True)
        (root / "Demo" / ".generated_files.json").write_text(
            json.dumps(["../outside.cs"]), encoding="utf-8")

        assert gen._sweep_stale_outputs(root, "Demo", set()) == []
        assert outsider.is_file()

    def test_unreadable_manifest_sweeps_nothing(self, gen, tmp_path):
        """Abstain loudly: a corrupt manifest means we do not know what we
        wrote last time, and guessing is how a real file gets deleted."""
        root = tmp_path
        (root / "Demo").mkdir(parents=True)
        (root / "Demo" / ".generated_files.json").write_text("{not json",
                                                             encoding="utf-8")
        survivor = root / "Demo" / "Assets" / "Scripts" / "Old.cs"
        survivor.parent.mkdir(parents=True)
        survivor.write_text("// x", encoding="utf-8")

        assert gen._sweep_stale_outputs(root, "Demo", set()) == []
        assert survivor.is_file()


# ── Per-scene content (SceneSpec) ───────────────────────────────────────────────
# Regression target (TerminalHorizon, live): 7 gameplay scenes rendered as
# byte-identical templates — per-scene mood and entity subsets must actually
# change what each scene file contains.

class TestSceneSpecs:
    def _schema(self, **kw):
        from src.unity_schema import EntitySpec, SceneSpec, UnitySchema
        base = dict(
            project_name="SS", company_name="C", description="d",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], packages=[],
            scenes=["Main", "StormShelf", "GearBay"],
            scripts=[UnityScript(name="PlayerController", type="player", description="p")],
            entities=[
                EntitySpec(name="Drones", kind="enemy", count=4, tag="Enemy"),
                EntitySpec(name="Wreckage", kind="obstacle", count=6),
            ],
            scene_specs=[
                SceneSpec(name="StormShelf", description="thunderhead interior",
                          time_of_day="night", fog=True,
                          entities=["Drones"]),
                SceneSpec(name="GearBay", description="equipment hub",
                          entities=[]),
            ],
        )
        base.update(kw)
        return UnitySchema(**base)

    def test_scenes_render_differently(self, gen):
        files = gen.generate(self._schema(), {})
        main  = scene(files, "SS", "Main")
        storm = scene(files, "SS", "StormShelf")
        assert main != storm
        # night sun pitch + fog land only in the spec'd scene
        assert "m_Fog: 1" in storm
        assert "m_Fog: 1" not in main

    def test_entity_subset_controls_spawner_presence(self, gen):
        files = gen.generate(self._schema(), {})
        assert "EntitySpawner" in scene(files, "SS", "Main")        # all groups
        assert "EntitySpawner" in scene(files, "SS", "StormShelf")  # subset
        assert "EntitySpawner" not in scene(files, "SS", "GearBay") # empty scene

    def test_spawner_emits_runtime_scene_filter(self, gen):
        files = gen.generate(self._schema(), {})
        cs = files["SS/Assets/Scripts/EntitySpawner.cs"]
        assert "SceneManagement" in cs
        assert "GroupActive" in cs
        assert '"StormShelf"' in cs and '"Drones"' in cs
        assert 'if (GroupActive(_scene, "Drones"))' in cs
        assert 'if (GroupActive(_scene, "Wreckage"))' in cs

    def test_no_scene_specs_keeps_legacy_output(self, gen):
        files = gen.generate(self._schema(scene_specs=[]), {})
        cs = files["SS/Assets/Scripts/EntitySpawner.cs"]
        assert "SceneManagement" not in cs
        assert "GroupActive" not in cs
        # all gameplay scenes identical as before
        assert scene(files, "SS", "Main") == scene(files, "SS", "StormShelf")


# ── 2D sprite pipeline (PNG picks flow to disk + spawner) ──────────────────────

class TestSpritePipeline:
    def _env(self, tmp_path):
        import json as _json
        lib = tmp_path / "sprites"
        lib.mkdir()
        (lib / "fish_blue.png").write_bytes(b"PNG-BYTES")
        lookups = tmp_path / "lookups"
        (lookups / "unity").mkdir(parents=True)
        (lookups / "unity" / "asset_catalog.json").write_text(_json.dumps({
            "version": 1, "vocabulary": {"categories": [], "tags": []},
            "assets": [{
                "filename": "fish_blue.png", "relpath": "fish_blue.png",
                "root": lib.as_posix(), "format": "png", "pack": "kenney_fishpack",
                "category": "creature", "keywords": ["fish", "swim", "creature"],
                "kind_hints": ["enemy"], "game_contexts": ["2d"],
                "generic_score": 0, "scale": 1.0, "curated": False,
            }],
        }), encoding="utf-8")
        return lookups

    def _schema(self):
        from src.unity_schema import EntitySpec, UnitySchema
        return UnitySchema(
            project_name="SP", company_name="C",
            description="underwater side-scroller", game_type="2D",
            platform="pc", render_pipeline="URP", unity_version="6000.0",
            color_space="Linear", features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(name="PlayerController", type="player", description="p")],
            entities=[EntitySpec(name="Fishes", kind="prop", count=5,
                                 model_hint="swimming fish")],
        )

    def test_sprite_pick_flows_to_spawner_and_disk(self, gen, tmp_path):
        lookups = self._env(tmp_path)
        root, _ = gen.write(self._schema(), {}, output_dir=str(tmp_path / "out"),
                            lookups_dir=str(lookups))
        cs = (root / "Assets" / "Scripts" / "EntitySpawner.cs").read_text(encoding="utf-8")
        assert '"Sprites/Kenney/Creature/fish_blue"' in cs
        assert "Resources.Load<Texture2D>" in cs
        assert "SpriteRenderer" in cs
        # binary copied to Resources + user-visible Assets/Sprites, no authored .meta
        primary = root / "Assets" / "Resources" / "Sprites" / "Kenney" / "Creature" / "fish_blue.png"
        assert primary.read_bytes() == b"PNG-BYTES"
        assert not primary.with_suffix(".png.meta").exists()
        assert (root / "Assets" / "Sprites" / "Creature" / "fish_blue.png").is_file()


# ── Vendored URP-Atmosphere shipping ────────────────────────────────────────────

class TestAtmosphere:
    def _schema(self, **kw):
        from src.unity_schema import UnitySchema
        base = dict(
            project_name="AT", company_name="C",
            description="a quiet dungeon crawler", game_type="3D",
            platform="pc", render_pipeline="URP", unity_version="6000.0",
            color_space="Linear", features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(name="PlayerController", type="player", description="p")],
        )
        base.update(kw)
        return UnitySchema(**base)

    def test_gate_requires_urp_3d_and_sky_concept(self):
        from src.unity_generator import wants_atmosphere
        assert not wants_atmosphere(self._schema())                       # no sky words
        assert wants_atmosphere(self._schema(
            description="high-altitude freefall above the clouds"))
        assert wants_atmosphere(self._schema(locomotion="fly"))
        assert not wants_atmosphere(self._schema(
            locomotion="fly", render_pipeline="builtin"))                 # URP only
        assert not wants_atmosphere(self._schema(
            description="skydiving through clouds", game_type="2D"))      # 3D/VR only

    def test_atmosphere_scripts_emitted_when_gated_on(self, gen):
        files = gen.generate(self._schema(locomotion="fly"), {})
        assert "AT/Assets/Editor/SetupAtmosphere.cs" in files
        assert "AT/Assets/Scripts/AtmosphereBootstrap.cs" in files
        cs = files["AT/Assets/Scripts/AtmosphereBootstrap.cs"]
        assert "AtmosphereEffect" in cs and "RuntimeInitializeOnLoadMethod" in cs
        off = gen.generate(self._schema(), {})
        assert not any("Atmosphere" in p for p in off)

    def test_write_ships_vendored_feature_and_profile(self, gen, tmp_path):
        root, _ = gen.write(self._schema(locomotion="fly"), {},
                            output_dir=str(tmp_path))
        assert (root / "Assets" / "Atmosphere" / "LICENSE.md").is_file()
        assert (root / "Assets" / "Atmosphere" / "Runtime" / "Scripts"
                     / "AtmosphereRenderFeature.cs").is_file()
        assert (root / "Assets" / "Resources" / "Atmosphere"
                     / "DefaultAtmosphereProfile.asset").is_file()

    # ── Version ⇄ RenderGraph-source invariant ──────────────────────────────────
    # The vendored render pass uses URP 17's RenderGraph API. A project must never
    # ship those sources on a URP-14 manifest (Unity 2022.3), which cannot compile
    # them. Driven through the real resolver → write() seam so the reconciliation
    # (bump 2022.3 → 6.x when atmosphere is active) is exercised end to end.

    _RG_MARKERS = ("RenderGraphModule", "RecordRenderGraph",
                   "CreateRenderGraphTexture")

    def _resolve_and_write(self, gen, schema, tmp_path):
        from src.unity_resolver import UnityResolver
        r = UnityResolver(lookups_dir="lookups", use_rag=False)
        r.resolve_schema(schema)                 # may bump unity_version
        r.inject_required_packages(schema)
        manifest = r.build_manifest_deps(schema)
        root, _ = gen.write(schema, manifest, output_dir=str(tmp_path))
        return root, manifest

    def test_atmosphere_2022_3_brief_is_bumped_and_ships_rendergraph_on_urp17(
            self, gen, tmp_path):
        # An atmosphere brief the LLM pinned to 2022.3: the resolver bumps it to
        # 6.x so the RenderGraph sources it ships land on a URP-17 manifest.
        schema = self._schema(
            description="high-altitude freefall above the clouds",
            unity_version="2022.3")
        root, manifest = self._resolve_and_write(gen, schema, tmp_path)

        assert schema.unity_version == "6000.0"                 # reconciled
        urp = manifest["com.unity.render-pipelines.universal"]
        assert int(urp.split(".")[0]) >= 17                     # RenderGraph-capable
        assert "6000." in (root / "ProjectSettings"
                           / "ProjectVersion.txt").read_text()  # editor matches

        pass_cs = (root / "Assets" / "Atmosphere" / "Runtime" / "Scripts"
                   / "AtmosphereRenderPass.cs").read_text(encoding="utf-8")
        assert any(m in pass_cs for m in self._RG_MARKERS)      # RenderGraph shipped

    def test_2022_3_project_without_atmosphere_ships_no_rendergraph_sources(
            self, gen, tmp_path):
        # Conversely: a grounded 2022.3 brief keeps its pin (URP 14) and must ship
        # NO RenderGraph-referencing sources — there is no atmosphere feature.
        schema = self._schema(
            description="a quiet dungeon crawler underground",
            unity_version="2022.3")
        root, manifest = self._resolve_and_write(gen, schema, tmp_path)

        assert schema.unity_version == "2022.3"                 # not bumped
        assert manifest["com.unity.render-pipelines.universal"].startswith("14.")
        assert not (root / "Assets" / "Atmosphere").exists()

        offenders = [
            cs for cs in root.rglob("*.cs")
            if any(m in cs.read_text(encoding="utf-8") for m in self._RG_MARKERS)
        ]
        assert offenders == []


# ── Service-locator + undeclared-identifier sanitizers (TerminalHorizon repro) ──

class TestFragmentSanitizers:
    def _script(self, update_extra):
        from src.unity_schema import UnityScript
        return UnityScript(
            name="VelocityManager", type="custom", description="d",
            update_extra=update_extra,
            extra_fields=[{"name": "safeVelocityLimit", "type": "float",
                           "default": "45.0f"}])

    def test_phantom_service_type_calls_stripped(self):
        # Get<PlayerFSM>() where PlayerFSM exists nowhere → CS1061 live.
        from src.unity_generator import _sanitize_project_service_calls
        s = self._script(
            'var playerFSM = GameContext.Instance.Get<PlayerFSM>(); '
            'if (playerFSM != null) { playerFSM.SetState("Blackout"); }')
        _sanitize_project_service_calls(s, {"VelocityManager": frozenset()}, set())
        assert "PlayerFSM" not in s.update_extra

    def test_known_custom_class_member_check(self):
        from src.unity_generator import _sanitize_project_service_calls
        api = {"DialogueManager": frozenset({"ShowLine"})}
        good = self._script('GameContext.Instance.Get<DialogueManager>().ShowLine();')
        bad  = self._script('GameContext.Instance.Get<DialogueManager>().TriggerBeat();')
        _sanitize_project_service_calls(good, api, set())
        _sanitize_project_service_calls(bad, api, set())
        assert "ShowLine" in good.update_extra          # declared → kept
        assert "TriggerBeat" not in bad.update_extra    # undeclared → stripped

    def test_opaque_types_left_alone(self):
        from src.unity_generator import _sanitize_project_service_calls
        s = self._script('GameContext.Instance.Get<GameHud>().SetObjective("x");')
        _sanitize_project_service_calls(s, {}, {"GameHud"})
        assert "SetObjective" in s.update_extra

    def test_undeclared_foreach_stripped_declared_kept(self):
        from src.unity_generator import _strip_undeclared_foreach
        s = self._script(
            'foreach (var bird in FindObjectsOfType<BirdSwarmFlocking>()) { '
            'foreach (var b in birds) b.Disperse(); }')
        _strip_undeclared_foreach(s)
        assert "birds" not in s.update_extra            # undeclared → line gone
        ok = self._script('var flock = GetFlock(); foreach (var b in flock) b.ToString();')
        _strip_undeclared_foreach(ok)
        assert "foreach" in ok.update_extra             # declared local → kept

    # ── _custom_script_api: extra_fields always private, methods need `public` ──

    def test_custom_script_api_excludes_fields(self):
        from src.unity_generator import _custom_script_api
        s = UnityScript(name="InventoryManager", type="custom", description="d",
                        extra_fields=[{"name": "moraleValue", "type": "int", "default": "100"}])
        api = _custom_script_api([s])
        assert "moraleValue" not in api["InventoryManager"]

    def test_custom_script_api_excludes_private_methods(self):
        from src.unity_generator import _custom_script_api
        s = UnityScript(name="InventoryManager", type="custom", description="d",
                        extra_methods=[
                            {"signature": "private void Restock()", "body": ""},
                            {"signature": "public void Refill()", "body": ""},
                        ])
        api = _custom_script_api([s])
        assert "Restock" not in api["InventoryManager"]
        assert "Refill" in api["InventoryManager"]

    def test_custom_script_field_access_stripped_always_private(self):
        # Live bug: MoraleSystem.cs did `inv.moraleValue` where inv = Get<
        # InventoryManager>() and moraleValue is one of InventoryManager's OWN
        # extra_fields — extra_fields always render private (CS0122).
        from src.unity_generator import _custom_script_api, _sanitize_project_service_calls
        inventory = UnityScript(name="InventoryManager", type="custom", description="d",
                                extra_fields=[{"name": "moraleValue", "type": "int", "default": "100"}])
        morale = self._script(
            'if (GameContext.Instance.Get<InventoryManager>() != null) { '
            'var inv = GameContext.Instance.Get<InventoryManager>(); '
            'if (inv.moraleValue > moraleLevel) { moraleLevel = inv.moraleValue; } }')
        morale.name = "MoraleSystem"
        custom_api = _custom_script_api([inventory, morale])
        _sanitize_project_service_calls(morale, custom_api, set())
        assert "moraleValue" not in morale.update_extra

    # ── Nullable value-type null-conditional chains (CS1503) ──────────────────

    def test_nullable_valuetype_fixed_at_use_site_not_declaration(self):
        """Root-caused live: AIManager.cs passed a GameContext.Instance.Get<T>()
        ?.transform.position (Vector3?) straight into Vector3.Distance (Vector3)
        -> CS1503. The declaration must stay nullable (the LLM's own
        `playerPos != null` check right after only compiles on Vector3?) —
        only the Distance() call needs .GetValueOrDefault()."""
        from src.unity_generator import _sanitize_nullable_valuetype_access
        s = self._script(
            'var playerPos = GameContext.Instance.Get<PlayerController>()'
            '?.transform.position; '
            'if (playerPos != null) { '
            'var dist = Vector3.Distance(playerPos, transform.position); }')
        _sanitize_nullable_valuetype_access(s)
        assert ("var playerPos = GameContext.Instance.Get<PlayerController>()"
                "?.transform.position;") in s.update_extra
        assert "playerPos != null" in s.update_extra
        assert "Vector3.Distance(playerPos.GetValueOrDefault(), transform.position)" \
            in s.update_extra

    def test_nullable_valuetype_noop_without_pattern(self):
        from src.unity_generator import _sanitize_nullable_valuetype_access
        s = self._script("transform.position += Vector3.forward * Time.deltaTime;")
        original = s.update_extra
        _sanitize_nullable_valuetype_access(s)
        assert s.update_extra == original

    # ── Vector3 * Vector3 has no operator* (CS0019) ────────────────────────────

    def test_vector_scale_misuse_rewritten_live_bug_string(self):
        """Live regression, DropZone's WindProfileManager.cs(13):
        `_windForce * -direction` — `_windForce` is the script's own
        extra_field (Vector3), `direction` a local `var direction =
        Vector3.up;`. UnityEngine has no `operator*(Vector3, Vector3)`;
        `Vector3.Scale` is the componentwise equivalent."""
        from src.unity_generator import _sanitize_vector_scale_misuse
        s = UnityScript(
            name="WindProfileManager", type="custom", description="d",
            extra_fields=[{"name": "_windForce", "type": "Vector3",
                          "default": "Vector3.zero"}],
            update_extra=(
                "var currentSpeed = GetComponent<Rigidbody>().linearVelocity.magnitude; "
                "if (currentSpeed > _terminalVelocityCap) { "
                "var direction = Vector3.up; "
                "GetComponent<Rigidbody>().AddForce(_windForce * -direction, "
                "ForceMode.VelocityChange); }"),
        )
        _sanitize_vector_scale_misuse(s)
        assert "Vector3.Scale(_windForce, -direction)" in s.update_extra
        assert "_windForce * -direction" not in s.update_extra

    def test_vector_scale_misuse_without_negation(self):
        from src.unity_generator import _sanitize_vector_scale_misuse
        s = UnityScript(
            name="Test", type="custom", description="d",
            extra_fields=[{"name": "_a", "type": "Vector3", "default": "Vector3.zero"},
                         {"name": "_b", "type": "Vector3", "default": "Vector3.zero"}],
            update_extra="var c = _a * _b;",
        )
        _sanitize_vector_scale_misuse(s)
        assert s.update_extra == "var c = Vector3.Scale(_a, _b);"

    def test_scalar_multiply_is_left_alone(self):
        """`Vector3 * float` is real, legal UnityEngine scaling — must not be
        rewritten just because one operand happens to be a known Vector3."""
        from src.unity_generator import _sanitize_vector_scale_misuse
        s = UnityScript(
            name="Test", type="custom", description="d",
            extra_fields=[{"name": "_windForce", "type": "Vector3",
                          "default": "Vector3.zero"}],
            update_extra="var f = _windForce * speed;",
        )
        original = s.update_extra
        _sanitize_vector_scale_misuse(s)
        assert s.update_extra == original

    def test_extra_method_body_is_also_fixed(self):
        from src.unity_generator import _sanitize_vector_scale_misuse
        s = UnityScript(
            name="Test", type="custom", description="d",
            extra_fields=[{"name": "_windForce", "type": "Vector3",
                          "default": "Vector3.zero"}],
            extra_methods=[{"signature": "private void ApplyWind()",
                           "body": "var direction = Vector3.up; "
                                   "rb.AddForce(_windForce * direction);"}],
        )
        _sanitize_vector_scale_misuse(s)
        assert "Vector3.Scale(_windForce, direction)" in s.extra_methods[0].body

    # ── Undeclared persisted scalars (auto-declare, not strip) ─────────────────

    def test_auto_declares_undeclared_persisted_scalar(self):
        """Root-caused live: DriftCamera.cs tracked frame delta via `lastX` but
        never declared `private float lastX;` — both the read and the write
        were CS0103. Must be auto-declared (preserves the behavior), not
        stripped (would delete the whole drift-FOV feature)."""
        from src.unity_generator import _auto_declare_undeclared_scalars
        s = self._script(
            'var lateralSpeed = transform.position.x - lastX; '
            'lastX = transform.position.x;')
        _auto_declare_undeclared_scalars(s)
        names = {f.name: f.type for f in s.extra_fields}
        assert names.get("lastX") == "float"
        assert "lateralSpeed" not in names   # var-declared local, not a field

    def test_auto_declare_skips_already_declared_field(self):
        from src.unity_generator import _auto_declare_undeclared_scalars
        s = self._script("safeVelocityLimit = 10f;")
        _auto_declare_undeclared_scalars(s)
        assert sum(f.name == "safeVelocityLimit" for f in s.extra_fields) == 1

    def test_auto_declare_skips_comparisons_and_compound_assign(self):
        from src.unity_generator import _auto_declare_undeclared_scalars
        s = self._script(
            'if (safeVelocityLimit >= 10f) { cooldown -= Time.deltaTime; }')
        _auto_declare_undeclared_scalars(s)
        names = {f.name for f in s.extra_fields}
        assert "cooldown" in names           # compound assign -> still caught
        assert "safeVelocityLimit" in names  # pre-existing field, not duplicated


# ── _sanitize_composed_component_calls: per-composed-class member validation ──

class TestComposedClassApiCoverage:
    def _script(self, update_extra):
        return UnityScript(name="Custom", type="custom", description="d",
                           update_extra=update_extra)

    def _unit(self, class_name):
        from src.unity_compose import ComposedUnit
        return ComposedUnit(kind="x", variant="x", class_name=class_name,
                            guid="g", target="world")

    def test_world_interactable_member_validated(self):
        # Live bug: InventoryManager.cs did interact.CanPickup()/interact.ItemName
        # where interact = Get<WorldInteractable>() — neither exists on the real
        # template (only OnPrompt/OnInteract/OnToggled events + Interact()).
        from src.unity_generator import _sanitize_composed_component_calls
        s = self._script(
            'if (GameContext.Instance.Get<WorldInteractable>() != null) { '
            'var interact = GameContext.Instance.Get<WorldInteractable>(); '
            'if (interact != null && interact.CanPickup()) { '
            'inventoryList.Add(interact.ItemName); moraleValue += 5; } }')
        _sanitize_composed_component_calls(s, [self._unit("WorldInteractable")])
        assert "CanPickup" not in s.update_extra
        assert "ItemName" not in s.update_extra

    def test_world_interactable_real_member_kept(self):
        from src.unity_generator import _sanitize_composed_component_calls
        s = self._script("GameContext.Instance.Get<WorldInteractable>()?.Interact();")
        _sanitize_composed_component_calls(s, [self._unit("WorldInteractable")])
        assert "Interact()" in s.update_extra

    def test_world_interactable_event_called_as_method_stripped(self):
        # OnToggled IS a real, listed member of WorldInteractable's verified
        # API (_COMPOSED_CLASS_API) — but it's a C# EVENT
        # (`public event Action<bool> OnToggled;`, Interactable.cs.j2), only
        # invocable with parens from WITHIN its own declaring class. A
        # membership-only check (the pre-fix `_composed_member_ok`) waved
        # this through as a real, real-arity call; live it is CS1061.
        from src.unity_generator import _sanitize_composed_component_calls
        s = self._script("GameContext.Instance.Get<WorldInteractable>().OnToggled();")
        _sanitize_composed_component_calls(s, [self._unit("WorldInteractable")])
        assert "OnToggled()" not in s.update_extra

    def test_world_interactable_event_subscription_kept(self):
        # The legal shape (`+=`) must survive — only a paren-CALL of an event
        # is illegal from outside the declaring class.
        from src.unity_generator import _sanitize_composed_component_calls
        s = self._script(
            "GameContext.Instance.Get<WorldInteractable>().OnToggled += "
            "on => Debug.Log(on);")
        _sanitize_composed_component_calls(s, [self._unit("WorldInteractable")])
        assert "OnToggled +=" in s.update_extra

    def test_player_camera_has_zero_public_members(self):
        from src.unity_generator import _sanitize_composed_component_calls
        s = self._script("GameContext.Instance.Get<PlayerCamera>()?.Shake(1f);")
        _sanitize_composed_component_calls(s, [self._unit("PlayerCamera")])
        assert "Shake" not in s.update_extra

    def test_player_camera_base_monobehaviour_member_kept(self):
        # Regression guard: base MonoBehaviour access must survive even for a
        # composed type with an empty verified API.
        from src.unity_generator import _sanitize_composed_component_calls
        s = self._script(
            "GameContext.Instance.Get<PlayerCamera>()?.gameObject.SetActive(false);")
        _sanitize_composed_component_calls(s, [self._unit("PlayerCamera")])
        assert "gameObject" in s.update_extra

    def test_player_animator_dynamic_setter_kept(self):
        from src.unity_generator import _sanitize_composed_component_calls
        s = self._script("GameContext.Instance.Get<PlayerAnimator>()?.SetOxygenLevel(0.5f);")
        _sanitize_composed_component_calls(s, [self._unit("PlayerAnimator")])
        assert "SetOxygenLevel" in s.update_extra

    def test_player_animator_invented_member_stripped(self):
        from src.unity_generator import _sanitize_composed_component_calls
        s = self._script("GameContext.Instance.Get<PlayerAnimator>()?.ForceIdlePose();")
        _sanitize_composed_component_calls(s, [self._unit("PlayerAnimator")])
        assert "ForceIdlePose" not in s.update_extra

    def test_var_bound_property_read_to_invented_member_stripped(self):
        # Live bug (SkyRace2D): EngineSound.cs did
        #   var pc = GameContext.Instance.Get<PlayerController>();
        #   if (pc != null) { float speed = pc.currentSpeed; ... }
        # currentSpeed is a bare PROPERTY READ (no trailing parens) through a
        # bound variable — the var-bound regex used to require a trailing `(`
        # so it only ever caught var-bound METHOD calls, never property reads,
        # and this line reached Unity untouched (CS1061: no such member).
        from src.unity_generator import _sanitize_composed_component_calls
        s = self._script(
            "var pc = GameContext.Instance.Get<PlayerController>(); "
            "if (pc != null) { float speed = pc.currentSpeed; }")
        _sanitize_composed_component_calls(s, [self._unit("PlayerController")])
        assert "currentSpeed" not in s.update_extra

    def test_var_bound_property_read_to_real_member_survives(self):
        from src.unity_generator import _sanitize_composed_component_calls
        s = self._script(
            "var pc = GameContext.Instance.Get<PlayerController>(); "
            "if (pc != null) { float speed = pc.CurrentSpeed; }")
        _sanitize_composed_component_calls(s, [self._unit("PlayerController")])
        assert "pc.CurrentSpeed" in s.update_extra

    def test_lighting_director_member_validated(self):
        # LightingDirector was advertised in GAMECONTEXT_SCHEMA_GUIDANCE as
        # Get<T>()-able with no entry in _COMPOSED_CLASS_API — any invented
        # member on it was invisible to every check.
        from src.unity_generator import _sanitize_composed_component_calls
        s = self._script("GameContext.Instance.Get<LightingDirector>()?.SetTimeOfDay(0.5f);")
        _sanitize_composed_component_calls(s, [self._unit("LightingDirector")])
        assert "SetTimeOfDay" not in s.update_extra

    def test_lighting_director_real_member_kept(self):
        from src.unity_generator import _sanitize_composed_component_calls
        s = self._script("GameContext.Instance.Get<LightingDirector>()?.Apply();")
        _sanitize_composed_component_calls(s, [self._unit("LightingDirector")])
        assert "Apply()" in s.update_extra

    def test_composed_class_api_covers_every_gamecontext_advertised_type(self):
        # Regression guard for exactly the drift LightingDirector hit: every
        # class name GAMECONTEXT_SCHEMA_GUIDANCE tells the LLM it may Get<T>()
        # must have a _COMPOSED_CLASS_API entry, or an invented member on it
        # silently reaches Unity untouched. GameManager is advertised via a
        # separate sentence/mechanism (GameManager.Instance, validated by
        # _sanitize_singleton_calls) and is deliberately excluded here.
        import re
        from src.unity_generator import _COMPOSED_CLASS_API
        from src.unity_prompts import GAMECONTEXT_SCHEMA_GUIDANCE
        m = re.search(
            r"Composed subsystem types you may Get<T>\(\):\s*(.+?)\.\s*\n",
            GAMECONTEXT_SCHEMA_GUIDANCE, re.DOTALL)
        assert m, "GAMECONTEXT_SCHEMA_GUIDANCE's advertised-types sentence moved or was reworded"
        listed = m.group(1).replace("\n", " ")
        listed = re.sub(r"\(plus\s+GameManager\)", "", listed)
        names = {n.strip() for n in listed.split(",") if n.strip()}
        missing = names - set(_COMPOSED_CLASS_API)
        assert not missing, f"advertised but unverified in _COMPOSED_CLASS_API: {missing}"

    def test_arity_mismatch_zero_args_stripped(self):
        # Live bug: PauseMenu.Activate() called with none of its 1 required
        # arg — a real member, wrong arity, invisible to a name-only check.
        from src.unity_generator import _sanitize_composed_component_calls
        s = self._script("GameContext.Instance.Get<PauseMenu>()?.Activate();")
        _sanitize_composed_component_calls(s, [self._unit("PauseMenu")])
        assert "Activate" not in s.update_extra

    def test_arity_match_survives(self):
        from src.unity_generator import _sanitize_composed_component_calls
        s = self._script('GameContext.Instance.Get<PauseMenu>()?.Activate("Play");')
        _sanitize_composed_component_calls(s, [self._unit("PauseMenu")])
        assert 'Activate("Play")' in s.update_extra

    def test_var_bound_arity_mismatch_stripped(self):
        from src.unity_generator import _sanitize_composed_component_calls
        s = self._script(
            "var menu = GameContext.Instance.Get<PauseMenu>(); "
            "if (menu != null) { menu.Activate(); }")
        _sanitize_composed_component_calls(s, [self._unit("PauseMenu")])
        assert "Activate" not in s.update_extra

    def test_var_bound_arity_match_survives(self):
        from src.unity_generator import _sanitize_composed_component_calls
        s = self._script(
            "var menu = GameContext.Instance.Get<PauseMenu>(); "
            'if (menu != null) { menu.Activate("Resume"); }')
        _sanitize_composed_component_calls(s, [self._unit("PauseMenu")])
        assert 'Activate("Resume")' in s.update_extra


# ── _sanitize_field_typed_member_calls: field-declared cross-reference type ──
# (binding and call site split across two methods; the per-body getter-chain
# passes above cannot see across that split)

class TestFieldTypedMemberCalls:
    def _unit(self, class_name):
        from src.unity_compose import ComposedUnit
        return ComposedUnit(kind="x", variant="x", class_name=class_name,
                            guid="g", target="world")

    def test_composed_field_bound_and_called_in_different_methods(self):
        # Same shape as the live AutomatonCitadel bug (SaveShrineTrigger.cs
        # bound `_saveSystem` in OnEnable(), then called a member on it from a
        # separate ActivateShrine() extra_method) but with an invented member
        # name that is NOT in _COMPOSED_CLASS_API's cross-variant union for
        # ANY SaveSystem variant — unlike SetCheckpoint (real on the
        # "checkpoint" variant, so the union table intentionally still lets it
        # through as an accepted false negative on other variants; see
        # _COMPOSED_CLASS_API's own docstring). Neither the GameContext.Get<T>
        # ()-chain regex nor the var-decl regex in the other sanitizers scans
        # across method bodies, so a genuinely invented member reached this
        # split-method shape untouched before this pass existed.
        from src.unity_generator import _sanitize_field_typed_member_calls, _custom_script_api
        s = UnityScript(
            name="SaveShrineTrigger", type="custom", description="d",
            extra_fields=[{"name": "_saveSystem", "type": "SaveSystem", "default": None}],
            awake_extra="_saveSystem = GameContext.Instance.Get<SaveSystem>();",
            extra_methods=[{
                "signature": "public void ActivateShrine()",
                "body": "if (_saveSystem != null) _saveSystem.ForceReload();",
            }],
        )
        custom_api = _custom_script_api([s])
        _sanitize_field_typed_member_calls(s, custom_api, [self._unit("SaveSystem")])
        assert "ForceReload" not in s.extra_methods[0].body

    def test_composed_field_real_member_survives(self):
        from src.unity_generator import _sanitize_field_typed_member_calls, _custom_script_api
        s = UnityScript(
            name="SaveShrineTrigger", type="custom", description="d",
            extra_fields=[{"name": "_saveSystem", "type": "SaveSystem", "default": None}],
            awake_extra="_saveSystem = GameContext.Instance.Get<SaveSystem>();",
            extra_methods=[{
                "signature": "public void ActivateShrine()",
                "body": "if (_saveSystem != null) _saveSystem.Save();",
            }],
        )
        custom_api = _custom_script_api([s])
        _sanitize_field_typed_member_calls(s, custom_api, [self._unit("SaveSystem")])
        assert "_saveSystem.Save()" in s.extra_methods[0].body

    def test_custom_script_field_public_method_validated_across_methods(self):
        # Same split-method gap, but the field's declared type is ANOTHER
        # generated custom script rather than a composed micro class.
        from src.unity_generator import _sanitize_field_typed_member_calls, _custom_script_api
        dialogue = UnityScript(
            name="NPCDialogueManager", type="custom", description="d",
            extra_methods=[{"signature": "public void PlayDialogue(string a, string b)", "body": ""}],
        )
        shrine = UnityScript(
            name="SaveShrineTrigger", type="custom", description="d",
            extra_fields=[{"name": "_npc", "type": "NPCDialogueManager", "default": None}],
            awake_extra="_npc = GameContext.Instance.Get<NPCDialogueManager>();",
            extra_methods=[{
                "signature": "public void ActivateShrine()",
                "body": "if (_npc != null) _npc.TriggerBeat();",
            }],
        )
        custom_api = _custom_script_api([dialogue, shrine])
        _sanitize_field_typed_member_calls(shrine, custom_api, [])
        assert "TriggerBeat" not in shrine.extra_methods[0].body

    def test_field_typed_arity_mismatch_stripped(self):
        # Same split-method shape as the SaveShrineTrigger cases above, but
        # the member IS real on PauseMenu (Activate) — only the argument
        # count is wrong, the defect _COMPOSED_METHOD_ARITY exists to catch.
        from src.unity_generator import _sanitize_field_typed_member_calls, _custom_script_api
        s = UnityScript(
            name="ShrineMenuTrigger", type="custom", description="d",
            extra_fields=[{"name": "_menu", "type": "PauseMenu", "default": None}],
            awake_extra="_menu = GameContext.Instance.Get<PauseMenu>();",
            extra_methods=[{
                "signature": "public void ActivateShrine()",
                "body": "if (_menu != null) _menu.Activate();",
            }],
        )
        custom_api = _custom_script_api([s])
        _sanitize_field_typed_member_calls(s, custom_api, [self._unit("PauseMenu")])
        assert "Activate" not in s.extra_methods[0].body

    def test_field_typed_arity_match_survives(self):
        from src.unity_generator import _sanitize_field_typed_member_calls, _custom_script_api
        s = UnityScript(
            name="ShrineMenuTrigger", type="custom", description="d",
            extra_fields=[{"name": "_menu", "type": "PauseMenu", "default": None}],
            awake_extra="_menu = GameContext.Instance.Get<PauseMenu>();",
            extra_methods=[{
                "signature": "public void ActivateShrine()",
                "body": '_menu.Activate("Resume");',
            }],
        )
        custom_api = _custom_script_api([s])
        _sanitize_field_typed_member_calls(s, custom_api, [self._unit("PauseMenu")])
        assert 'Activate("Resume")' in s.extra_methods[0].body

    def test_invented_constructor_call_is_stripped(self):
        # Live bug: Graphics.Blit(source, dest, new ShaderPass(profile), 0) —
        # ShaderPass is not a real Unity/URP type, and it never touches a
        # declared field so _sanitize_stub_type_usage never sees it.
        from src.unity_generator import _sanitize_invented_constructor_calls
        s = UnityScript(
            name="PostFxTrigger", type="custom", description="d",
            update_extra="Graphics.Blit(source, dest, new ShaderPass(profile), 0);",
        )
        _sanitize_invented_constructor_calls(s)
        assert "ShaderPass" not in s.update_extra

    def test_invented_constructor_survives_when_builtin(self):
        from src.unity_generator import _sanitize_invented_constructor_calls
        s = UnityScript(name="X", type="custom", description="d",
                        update_extra="transform.position = new Vector3(1f, 0f, 0f);")
        _sanitize_invented_constructor_calls(s)
        assert "new Vector3" in s.update_extra

    def test_invented_constructor_survives_when_known_project_type(self):
        from src.unity_generator import _sanitize_invented_constructor_calls
        s = UnityScript(name="X", type="custom", description="d",
                        update_extra="var log = new AuditLog();")
        _sanitize_invented_constructor_calls(s, known_types=frozenset({"AuditLog"}))
        assert "new AuditLog" in s.update_extra

    def test_invented_constructor_survives_manager_suffix(self):
        # Same exemption _stub_type_names applies: *Manager/*System/
        # *Controller/*Handler are expected separate scripts this pass
        # cannot see the full project roster of.
        from src.unity_generator import _sanitize_invented_constructor_calls
        s = UnityScript(name="X", type="custom", description="d",
                        update_extra="var mgr = new OxygenManager();")
        _sanitize_invented_constructor_calls(s)
        assert "new OxygenManager" in s.update_extra

    def test_invented_find_object_type_call_is_stripped(self):
        # Live bug: MirrorSpawning.cs (Grandma's Antique Shop) —
        # FindFirstObjectByType<AntiqueMirror>() where AntiqueMirror is the
        # brief's ENTITY name, not a script anywhere in the project. CS0246.
        from src.unity_generator import _sanitize_invented_find_object_type_calls
        s = UnityScript(
            name="MirrorSpawning", type="custom", description="d",
            extra_methods=[{
                "signature": "private void SpawnRaider()",
                "body": "var mirror = FindFirstObjectByType<AntiqueMirror>();",
            }],
        )
        _sanitize_invented_find_object_type_calls(s)
        assert "AntiqueMirror" not in s.extra_methods[0].body

    def test_invented_find_object_type_survives_when_builtin(self):
        from src.unity_generator import _sanitize_invented_find_object_type_calls
        s = UnityScript(name="X", type="custom", description="d",
                        update_extra="var cam = FindFirstObjectByType<Camera>();")
        _sanitize_invented_find_object_type_calls(s)
        assert "FindFirstObjectByType<Camera>" in s.update_extra

    def test_invented_find_object_type_survives_when_known_project_type(self):
        from src.unity_generator import _sanitize_invented_find_object_type_calls
        s = UnityScript(name="X", type="custom", description="d",
                        update_extra="var hub = FindObjectOfType<GossipHub>();")
        _sanitize_invented_find_object_type_calls(s, known_types=frozenset({"GossipHub"}))
        assert "FindObjectOfType<GossipHub>" in s.update_extra

    def test_invented_find_object_type_survives_manager_suffix(self):
        from src.unity_generator import _sanitize_invented_find_object_type_calls
        s = UnityScript(name="X", type="custom", description="d",
                        update_extra="var mgr = FindObjectOfType<OxygenManager>();")
        _sanitize_invented_find_object_type_calls(s)
        assert "FindObjectOfType<OxygenManager>" in s.update_extra

    def test_get_component_wrong_arity_stripped(self):
        # Live shape: GetComponent<T>() called with an extra argument that
        # overload doesn't have — CS1501, invisible to every other check
        # because GetComponent is always exempted as a legitimate name.
        from src.unity_generator import _sanitize_builtin_component_call_arity
        s = UnityScript(name="X", type="custom", description="d",
                        update_extra="var rb = GetComponent<Rigidbody>(gameObject, true);")
        _sanitize_builtin_component_call_arity(s)
        assert "GetComponent" not in s.update_extra

    def test_get_component_zero_args_survives(self):
        from src.unity_generator import _sanitize_builtin_component_call_arity
        s = UnityScript(name="X", type="custom", description="d",
                        update_extra="var rb = GetComponent<Rigidbody>();")
        _sanitize_builtin_component_call_arity(s)
        assert "GetComponent<Rigidbody>()" in s.update_extra

    def test_get_component_in_children_bool_arg_survives(self):
        from src.unity_generator import _sanitize_builtin_component_call_arity
        s = UnityScript(name="X", type="custom", description="d",
                        update_extra="var rb = GetComponentInChildren<Rigidbody>(true);")
        _sanitize_builtin_component_call_arity(s)
        assert "GetComponentInChildren<Rigidbody>(true)" in s.update_extra

    def test_try_get_component_out_param_survives(self):
        from src.unity_generator import _sanitize_builtin_component_call_arity
        s = UnityScript(name="X", type="custom", description="d",
                        update_extra="if (TryGetComponent<Rigidbody>(out var rb)) { }")
        _sanitize_builtin_component_call_arity(s)
        assert "TryGetComponent<Rigidbody>(out var rb)" in s.update_extra

    def test_field_typed_hud_arity_mismatch_stripped(self):
        # Same defect through the hud_api branch (per-project setter list
        # overrides the static membership table for hud kind, but arity
        # still comes from _COMPOSED_METHOD_ARITY either way).
        from src.unity_generator import _sanitize_field_typed_member_calls, _custom_script_api
        from src.unity_compose import ComposedUnit
        hud_unit = ComposedUnit(kind="hud", variant="health_bar", class_name="GameHud",
                                 guid="g", target="world", setters=["SetHealth"])
        s = UnityScript(
            name="ArenaHudTrigger", type="custom", description="d",
            extra_fields=[{"name": "_hud", "type": "GameHud", "default": None}],
            awake_extra="_hud = GameContext.Instance.Get<GameHud>();",
            extra_methods=[{
                "signature": "public void OnHit()",
                "body": "if (_hud != null) _hud.SetHealth(50f);",
            }],
        )
        custom_api = _custom_script_api([s])
        _sanitize_field_typed_member_calls(s, custom_api, [hud_unit])
        assert "SetHealth" not in s.extra_methods[0].body


# ── _sanitize_param_typed_member_calls: cross-reference type on a PARAMETER ──
# (not a field — a parameter's scope is its own method only)

class TestParamTypedMemberCalls:
    def _unit(self, class_name):
        from src.unity_compose import ComposedUnit
        return ComposedUnit(kind="x", variant="x", class_name=class_name,
                            guid="g", target="world")

    def test_param_typed_event_called_as_method_stripped(self):
        # Live bug: InventoryManager.cs — TryUnlockGate(WorldInteractable gate)
        # and TryUnlockGateWithCog(WorldInteractable gate) both called
        # gate.OnToggled() on the PARAMETER, never a field or a
        # GameContext.Get<T>() chain, so _sanitize_field_typed_member_calls
        # (fields only) and _sanitize_composed_component_calls (getter chains
        # only) both missed it entirely. OnToggled is a real, listed member
        # (_COMPOSED_CLASS_API) but an EVENT — CS1061 either way.
        from src.unity_generator import _sanitize_param_typed_member_calls, _custom_script_api
        s = UnityScript(
            name="InventoryManager", type="custom", description="d",
            extra_methods=[{
                "signature": "public bool TryUnlockGate(WorldInteractable gate)",
                "body": "if (gate == null) return false;\ngate.OnToggled();\nreturn true;",
            }],
        )
        custom_api = _custom_script_api([s])
        _sanitize_param_typed_member_calls(s, custom_api, [self._unit("WorldInteractable")])
        assert "OnToggled()" not in s.extra_methods[0].body
        assert "return false;" in s.extra_methods[0].body   # unrelated code untouched
        assert "return true;" in s.extra_methods[0].body

    def test_param_typed_real_method_survives(self):
        from src.unity_generator import _sanitize_param_typed_member_calls, _custom_script_api
        s = UnityScript(
            name="InventoryManager", type="custom", description="d",
            extra_methods=[{
                "signature": "public bool TryUnlockGate(WorldInteractable gate)",
                "body": "if (gate == null) return false; gate.Interact(); return true;",
            }],
        )
        custom_api = _custom_script_api([s])
        _sanitize_param_typed_member_calls(s, custom_api, [self._unit("WorldInteractable")])
        assert "gate.Interact();" in s.extra_methods[0].body

    def test_param_typed_invented_member_stripped(self):
        from src.unity_generator import _sanitize_param_typed_member_calls, _custom_script_api
        s = UnityScript(
            name="InventoryManager", type="custom", description="d",
            extra_methods=[{
                "signature": "public bool TryUnlockGate(WorldInteractable gate)",
                "body": "if (gate.CanPickup()) return true; return false;",
            }],
        )
        custom_api = _custom_script_api([s])
        _sanitize_param_typed_member_calls(s, custom_api, [self._unit("WorldInteractable")])
        assert "CanPickup" not in s.extra_methods[0].body

    def test_param_typed_call_in_other_method_untouched(self):
        # A parameter's scope is its OWN method only — a same-named
        # identifier in a DIFFERENT method (not typed the same way there)
        # must not be checked against WorldInteractable's API at all.
        from src.unity_generator import _sanitize_param_typed_member_calls, _custom_script_api
        s = UnityScript(
            name="InventoryManager", type="custom", description="d",
            extra_methods=[
                {"signature": "public bool TryUnlockGate(WorldInteractable gate)",
                 "body": "gate.Interact(); return true;"},
                {"signature": "public void Reset()",
                 "body": "var gate = 0; gate = gate + 1;"},
            ],
        )
        custom_api = _custom_script_api([s])
        _sanitize_param_typed_member_calls(s, custom_api, [self._unit("WorldInteractable")])
        assert "gate = gate + 1;" in s.extra_methods[1].body


# ── Undeclared self-reference: bare field/method the script never declared ────

class TestUndeclaredSelfReferenceSanitizers:
    def _script(self, update_extra, extra_fields=None, extra_methods=None):
        return UnityScript(
            name="PlatformTrigger", type="custom", description="d",
            update_extra=update_extra,
            extra_fields=extra_fields or [],
            extra_methods=extra_methods or [],
        )

    # ── regression: exact live bug string ──
    def test_platform_trigger_live_bug_stripped(self):
        from src.unity_generator import (_sanitize_undeclared_self_calls,
                                         _sanitize_undeclared_condition_reads)
        s = self._script(
            "var enemy = GameContext.Instance.Get<EnemyAI>(); "
            "if (enemy != null && healthThreshold < 0.2f) TriggerCollapse();")
        _sanitize_undeclared_self_calls(s)
        _sanitize_undeclared_condition_reads(s)
        assert "TriggerCollapse" not in s.update_extra
        assert "healthThreshold" not in s.update_extra

    # ── Part A: must strip ──
    def test_bare_undeclared_method_call_stripped(self):
        from src.unity_generator import _sanitize_undeclared_self_calls
        s = self._script("DoSomethingInvented();")
        _sanitize_undeclared_self_calls(s)
        assert "DoSomethingInvented" not in s.update_extra

    def test_declared_extra_method_call_kept(self):
        from src.unity_generator import _sanitize_undeclared_self_calls
        s = self._script("TriggerCollapse();",
                         extra_methods=[{"signature": "private void TriggerCollapse()", "body": ""}])
        _sanitize_undeclared_self_calls(s)
        assert "TriggerCollapse" in s.update_extra

    def test_delegate_field_bare_invocation_kept(self):
        from src.unity_generator import _sanitize_undeclared_self_calls
        s = self._script("onCollapse();",
                         extra_fields=[{"name": "onCollapse", "type": "Action"}])
        _sanitize_undeclared_self_calls(s)
        assert "onCollapse()" in s.update_extra

    # ── Part A: must NOT strip (legitimate Unity/C# shapes) ──
    def test_new_vector3_construction_kept(self):
        from src.unity_generator import _sanitize_undeclared_self_calls
        s = self._script("var v = new Vector3(1f, 2f, 3f);")
        _sanitize_undeclared_self_calls(s)
        assert "new Vector3" in s.update_extra

    def test_instantiate_call_kept(self):
        from src.unity_generator import _sanitize_undeclared_self_calls
        s = self._script("Instantiate(prefab);")
        _sanitize_undeclared_self_calls(s)
        assert "Instantiate(prefab)" in s.update_extra

    def test_get_component_generic_call_kept(self):
        from src.unity_generator import _sanitize_undeclared_self_calls
        s = self._script("var rb = GetComponent<Rigidbody>();")
        _sanitize_undeclared_self_calls(s)
        assert "GetComponent<Rigidbody>()" in s.update_extra

    def test_null_conditional_dotted_call_kept(self):
        from src.unity_generator import _sanitize_undeclared_self_calls
        s = self._script("interact?.Interact();")
        _sanitize_undeclared_self_calls(s)
        assert "interact?.Interact()" in s.update_extra

    def test_keyword_paren_constructs_kept(self):
        from src.unity_generator import _sanitize_undeclared_self_calls
        s = self._script("if (transform.position.y < 0f) { while (true) { break; } }")
        _sanitize_undeclared_self_calls(s)
        assert "if (transform.position.y" in s.update_extra
        assert "while (true)" in s.update_extra

    # ── Part B: must strip ──
    def test_bare_undeclared_condition_value_stripped(self):
        from src.unity_generator import _sanitize_undeclared_condition_reads
        s = self._script("if (healthThreshold < 0.2f) enabled = false;")
        _sanitize_undeclared_condition_reads(s)
        assert "healthThreshold" not in s.update_extra

    def test_declared_field_condition_kept(self):
        from src.unity_generator import _sanitize_undeclared_condition_reads
        s = self._script("if (healthThreshold < 0.2f) enabled = false;",
                         extra_fields=[{"name": "healthThreshold", "type": "float", "default": "0.3f"}])
        _sanitize_undeclared_condition_reads(s)
        assert "healthThreshold" in s.update_extra

    # ── Part B: must NOT strip (legitimate Unity/C# shapes) ──
    def test_transform_position_condition_kept(self):
        from src.unity_generator import _sanitize_undeclared_condition_reads
        s = self._script("if (transform.position.y < 0f) { enabled = false; }")
        _sanitize_undeclared_condition_reads(s)
        assert "transform.position.y" in s.update_extra

    def test_is_pattern_match_kept(self):
        # Real false-positive risk: `EnemyAI` (type) and `enemyAi` (pattern var)
        # are both bare inside the condition and declared nowhere else. `other`
        # is declared as the method's own parameter, isolating the is-pattern
        # behavior from an unrelated "other is undeclared" false failure.
        from src.unity_generator import (_sanitize_undeclared_self_calls,
                                         _sanitize_undeclared_condition_reads)
        s = self._script("", extra_methods=[
            {"signature": "private void OnHit(Collider other)",
             "body": "if (other is EnemyAI enemyAi) { enemyAi.Alert(); }"},
        ])
        _sanitize_undeclared_self_calls(s)
        _sanitize_undeclared_condition_reads(s)
        assert "is EnemyAI enemyAi" in s.extra_methods[0].body

    def test_local_declared_before_condition_kept(self):
        from src.unity_generator import _sanitize_undeclared_condition_reads
        s = self._script("var speed = 5f; if (speed > walkSpeed) { }",
                         extra_fields=[{"name": "walkSpeed", "type": "float", "default": "5f"}])
        _sanitize_undeclared_condition_reads(s)
        assert "speed > walkSpeed" in s.update_extra

    # ── regression: exact live bug string (VectorCore's InputMapper.cs) ──
    # A generic method call in a K&R-style `if (cond) {` condition was
    # misread as a bare undeclared VALUE read (the type-argument list
    # confused the "not a method call" exclusion), so the whole line —
    # including its opening `{` — was dropped, orphaning the block body and
    # its closer (CS1022/CS8803/CS0106 downstream).
    def test_generic_method_call_in_condition_kept(self):
        from src.unity_generator import _sanitize_undeclared_condition_reads
        s = self._script(
            "if (_groundInputAction.ReadValue<Vector2>().magnitude > 0.1f) { "
            "SetGrounded(); }",
            extra_fields=[{"name": "_groundInputAction", "type": "InputActionReference"}],
            extra_methods=[{"signature": "private void SetGrounded()", "body": ""}],
        )
        _sanitize_undeclared_condition_reads(s)
        assert "ReadValue<Vector2>()" in s.update_extra
        assert s.update_extra.count("{") == s.update_extra.count("}")

    # ── regression: string-literal content misread as a bare identifier ──
    # "Fire1" inside Input.GetButtonDown("Fire1") was extracted as if it
    # were an undeclared identifier `Fire1` and the whole condition line
    # dropped — Input.GetButtonDown("Jump")/GetAxis("Horizontal")-shaped
    # calls are common, real Unity API, not a value read.
    def test_string_literal_argument_in_condition_kept(self):
        from src.unity_generator import _sanitize_undeclared_condition_reads
        s = self._script('if (Input.GetButtonDown("Fire1")) { Shoot(); }',
                         extra_methods=[{"signature": "private void Shoot()", "body": ""}])
        _sanitize_undeclared_condition_reads(s)
        assert 'Input.GetButtonDown("Fire1")' in s.update_extra

    def test_string_literal_argument_does_not_hide_a_real_undeclared_name(self):
        # The string-literal fix must not become a blanket exemption — an
        # actually-undeclared bare identifier alongside a string literal in
        # the same condition still gets caught.
        from src.unity_generator import _sanitize_undeclared_condition_reads
        s = self._script('if (Input.GetButtonDown("Fire1") && healthThreshold < 0.2f) '
                         '{ enabled = false; }')
        _sanitize_undeclared_condition_reads(s)
        assert "healthThreshold" not in s.update_extra

    # ── regression: bare no-initializer declaration ahead of an out-param ──
    # (VectorCore's SurfaceTypeDetector.cs) — `RaycastHit hit;` followed by
    # `Physics.Raycast(..., out hit, ...)` never registered `hit` as declared
    # (the declared-locals scan required a trailing `=`), so a later
    # `if (hit.collider != null)` read `hit` as an undeclared bare value and
    # the whole line — including its opening `{` — was dropped.
    def test_bare_out_param_declaration_condition_kept(self):
        from src.unity_generator import _sanitize_undeclared_condition_reads
        s = self._script(
            "RaycastHit hit;\n"
            "Physics.Raycast(transform.position, Vector3.down, out hit, 1f);\n"
            "if (hit.collider != null) { enabled = false; }")
        _sanitize_undeclared_condition_reads(s)
        assert "if (hit.collider != null)" in s.update_extra
        assert s.update_extra.count("{") == s.update_extra.count("}")

    def test_bare_declaration_regex_ignores_statement_keywords(self):
        # `return x;`/`throw e;`/`yield return x;` must never be misread as
        # declaring a local named after the keyword's own value — none of
        # "return"/"throw"/"yield" is PascalCase or a primitive keyword.
        from src.unity_generator import _LOCAL_DECL_NO_INIT_RE
        for line in ("return someValue;", "throw myException;",
                     "yield return x;", "break;", "continue;"):
            assert not _LOCAL_DECL_NO_INIT_RE.search(line), line


# ── SceneAudio emission + data-driven README ────────────────────────────────────

class TestSceneAudioAndReadme:
    def _schema(self, **kw):
        from src.unity_schema import EntitySpec, SceneSpec, UnitySchema
        base = dict(
            project_name="SA", company_name="C", description="skydiving survival",
            game_type="3D", platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=["audio"], scenes=["Main", "StormShelf"], packages=[],
            scripts=[UnityScript(name="PlayerController", type="player", description="p")],
            entities=[EntitySpec(name="Wreckage", kind="prop", count=6,
                                 model_hint="debris wreckage")],
            scene_specs=[SceneSpec(name="StormShelf",
                                   description="thunderhead interior",
                                   time_of_day="night", fog=True)],
        )
        base.update(kw)
        return UnitySchema(**base)

    def test_scene_audio_emitted_with_audio_feature(self, gen):
        files = gen.generate(self._schema(), {})
        cs = files["SA/Assets/Scripts/SceneAudio.cs"]
        assert 'Resources.LoadAll<AudioClip>("Audio")' in cs
        assert "RuntimeInitializeOnLoadMethod" in cs
        assert "public static void Play(string cue" in cs

    def test_scene_audio_skipped_without_audio_content(self, gen):
        files = gen.generate(self._schema(features=[]), {})
        assert not any("SceneAudio" in p for p in files)

    def test_readme_reflects_this_build(self, gen):
        files = gen.generate(self._schema(), {"com.unity.inputsystem": "1.8.2"})
        rm = files["SA/README.md"]
        assert "| **StormShelf** | night, fog | thunderhead interior |" in rm
        assert "Models/Kenney/Obstacle/debris-" in rm      # entity→model table
        assert "SceneAudio.cs" in rm                       # audio section
        assert "placeholder GameObjects" not in rm         # stale line gone
        assert "unity-validate SA.schema.json" in rm       # regeneration loop

    def test_readme_procedural_and_atmosphere_sections(self, gen):
        from src.unity_schema import EntitySpec, ProceduralMesh
        schema = self._schema(
            locomotion="fly",
            entities=[EntitySpec(name="AtmosphericClouds", kind="prop", count=9,
                                 model_hint="atmospheric clouds")],
            procedural_meshes=[ProceduralMesh(name="CloudLayer",
                                              description="volumetric clouds",
                                              mesh_type="custom", resolution=64)])
        files = gen.generate(schema, {}, strict_assets=True)
        rm = files["SA/README.md"]
        assert "procedural mesh (`custom`, res 64)" in rm
        assert "URP-Atmosphere" in rm                      # locomotion=fly gate
        assert "Flight controller" in rm


# ── Engine-type shadowing guards (hit live: 'WheelCollider' stub broke Unity) ──

def test_stub_types_never_shadow_unity_engine_types():
    """An LLM extra_field like List<WheelCollider> must NOT emit a stub class —
    a stub named after a real UnityEngine type shadows it project-wide (global
    namespace beats `using UnityEngine`) and every micro using the real type
    fails with CS1061/CS0311."""
    from types import SimpleNamespace as NS
    from src.unity_generator import _stub_type_names
    fields = [NS(type="List<WheelCollider>"), NS(type="WheelHit"),
              NS(type="WheelFrictionCurve"), NS(type="CharacterController"),
              NS(type="RectTransform"), NS(type="InputAction"),
              NS(type="List<BountyContract>")]
    names = _stub_type_names(fields)
    assert names == ["BountyContract"]


def test_vector2int_field_never_gets_stubbed():
    # Regression (live Unity, RepoFlowSim run): a field typed Vector2Int
    # (a real UnityEngine struct, common on grid-based games) got stubbed as
    # an empty class — new Vector2Int(50, 50) then failed with CS1729 "does
    # not contain a constructor that takes 2 arguments" because the stub's
    # parameterless class shadowed UnityEngine.Vector2Int project-wide. The
    # *Int struct variants were simply missing from _CS_BUILTIN_TYPES.
    from types import SimpleNamespace as NS
    from src.unity_generator import _stub_type_names
    fields = [NS(type="Vector2Int"), NS(type="Vector3Int"),
              NS(type="BoundsInt"), NS(type="RangeInt"),
              NS(type="List<BountyContract>")]
    names = _stub_type_names(fields)
    assert names == ["BountyContract"]


def test_composed_class_name_never_gets_stubbed(gen):
    """Regression: `unity-validate` (no --compose) used to call generate()
    with composed_units=None unconditionally, so _opaque_types never included
    a composed micro's class name — a custom script field typed after one
    (e.g. TutorialIcon's `WorldInteractable _interactable`, live on a RepoFlow
    --compose run) got an empty stub class generated for it, colliding
    (CS0101) with the composed `interactable` micro's real WorldInteractable
    class. `unity-validate --compose` now reconstructs composed_units the same
    deterministic way (subsystems_for + MicroComposer.plan) and passes them
    through to generate() — this locks that reconstruction path in."""
    from src.unity_compose import MicroComposer, subsystems_for
    from src.unity_schema import UnitySchema

    subsystems = subsystems_for("3D", ["interactables"], locomotion="walk",
                                genre="universal")
    composer = MicroComposer("TutorialProj", project_config={"audio": {"backend": "dsp"}})
    composed_units = composer.plan(subsystems)
    assert any(u.class_name == "WorldInteractable" for u in composed_units)

    schema = UnitySchema(
        project_name="TutorialProj", company_name="C", description="",
        game_type="3D", platform="pc", render_pipeline="URP",
        unity_version="6000.0", color_space="Linear",
        features=[], scenes=["Main"], packages=[],
        scripts=[
            UnityScript(name="PlayerController", type="player", description="m"),
            UnityScript(
                name="TutorialIcon", type="custom", description="hint icon",
                attach_to="systems",
                extra_fields=[{"name": "_interactable", "type": "WorldInteractable"}],
            ),
        ],
    )

    # Without composed_units (the pre-fix `unity-validate` call shape): the
    # field type resolves nowhere the generator can see, so it gets stubbed.
    stale = gen.generate(schema, {})
    cs_stale = stale["TutorialProj/Assets/Scripts/TutorialIcon.cs"]
    assert "class WorldInteractable" in cs_stale

    # With composed_units (what `unity-validate --compose` now passes): the
    # generator knows WorldInteractable already exists as a real composed
    # class, so no colliding stub is emitted.
    fixed = gen.generate(schema, {}, composed_units=composed_units)
    cs_fixed = fixed["TutorialProj/Assets/Scripts/TutorialIcon.cs"]
    assert "class WorldInteractable" not in cs_fixed
    assert "WorldInteractable _interactable" in cs_fixed   # field itself stays


def test_programmatic_fix_renames_engine_type_script_names():
    from src.unity_pipeline import _programmatic_fix
    from src.unity_schema import UnitySchema
    sch = UnitySchema(project_name="T", game_type="3D", platform="all",
                      render_pipeline="URP", scenes=["Main"],
                      scripts=[{"name": "WheelCollider", "type": "custom", "description": "x"},
                               {"name": "VehicleRig", "type": "custom", "description": "y"}])
    sch = _programmatic_fix(sch)
    assert sch.scripts[0].name == "WheelColliderBehaviour"
    assert sch.scripts[1].name == "VehicleRig"


# ── Track 3: schema.materials -> real .mat assets, schema.shaders -> README ──

def test_material_spec_emits_real_mat_asset():
    from src.unity_schema import UnitySchema, MaterialSpec
    from src.unity_generator import UnityProjectGenerator
    schema = UnitySchema(project_name="MatProof", game_type="3D", platform="PC",
                         render_pipeline="URP",
                         materials=[MaterialSpec(id="m1", name="RustyMetal",
                                                 shader_model="Standard",
                                                 parameters={"color": "#8B4513"})])
    files = UnityProjectGenerator().generate(schema, {})
    mat_path = "MatProof/Assets/Materials/RustyMetal.mat"
    assert mat_path in files
    assert "Material:" in files[mat_path]
    assert "m_Name: RustyMetal" in files[mat_path]
    assert "8b4513" not in files[mat_path].lower()  # hex is converted to float RGB, not kept literal
    assert "guid:" in files[f"{mat_path}.meta"]


def test_material_spec_without_color_falls_back_to_neutral_gray():
    from src.unity_schema import UnitySchema, MaterialSpec
    from src.unity_generator import UnityProjectGenerator, _hex_to_rgb
    schema = UnitySchema(project_name="MatProof2", game_type="3D", platform="PC",
                         render_pipeline="builtin",
                         materials=[MaterialSpec(id="m1", name="Unspecified")])
    files = UnityProjectGenerator().generate(schema, {})
    mat = files["MatProof2/Assets/Materials/Unspecified.mat"]
    r, g, b = _hex_to_rgb("#B0B0B0")
    assert f"r: {round(r, 8)}" in mat


def test_material_guids_are_deterministic():
    from src.unity_schema import UnitySchema, MaterialSpec
    from src.unity_generator import UnityProjectGenerator
    def _build():
        schema = UnitySchema(project_name="MatDet", game_type="3D", platform="PC",
                             render_pipeline="URP",
                             materials=[MaterialSpec(id="m1", name="Foo")])
        return UnityProjectGenerator().generate(schema, {})
    f1, f2 = _build(), _build()
    key = "MatDet/Assets/Materials/Foo.mat.meta"
    assert f1[key] == f2[key]


def test_shader_specs_surface_in_readme_not_as_files():
    from src.unity_schema import UnitySchema, ShaderSpec
    from src.unity_generator import UnityProjectGenerator
    schema = UnitySchema(project_name="ShaderProof", game_type="3D", platform="PC",
                         render_pipeline="URP",
                         shaders=[ShaderSpec(id="s1", name="Dissolve", language="hlsl",
                                             parameters={"description": "dissolve on death"})])
    files = UnityProjectGenerator().generate(schema, {})
    readme = files["ShaderProof/README.md"]
    assert "Dissolve" in readme and "hlsl" in readme and "dissolve on death" in readme
    # No fabricated shader source files — deliberately not code-generated.
    assert not any("Dissolve" in p and p.endswith((".shader", ".shadergraph")) for p in files)


# ── Track 5: world_spec feeds environment/atmosphere context ─────────────────

import re


def _fog_and_intensity(scene: str) -> tuple:
    fog = re.search(r"m_Fog: (\d)", scene)
    intensity = re.search(r"m_Intensity: ([\d.]+)", scene)
    return (fog.group(1) if fog else None, intensity.group(1) if intensity else None)


def test_world_spec_weather_drives_fog_when_no_environment():
    from src.unity_schema import UnitySchema, WorldSpec
    from src.unity_generator import UnityProjectGenerator
    no_env = UnitySchema(project_name="NoEnv", game_type="3D", platform="PC",
                         render_pipeline="URP", scenes=["Main"])
    rain = UnitySchema(project_name="Rain", game_type="3D", platform="PC",
                       render_pipeline="URP", scenes=["Main"],
                       world_spec=WorldSpec(weather="rain"))
    clear = UnitySchema(project_name="Clear", game_type="3D", platform="PC",
                        render_pipeline="URP", scenes=["Main"],
                        world_spec=WorldSpec(weather="clear"))
    gen = UnityProjectGenerator()
    fog_none, _ = _fog_and_intensity(gen.generate(no_env, {})["NoEnv/Assets/Scenes/Main.unity"])
    fog_rain, _ = _fog_and_intensity(gen.generate(rain, {})["Rain/Assets/Scenes/Main.unity"])
    fog_clear, _ = _fog_and_intensity(gen.generate(clear, {})["Clear/Assets/Scenes/Main.unity"])
    assert fog_none == "0"
    assert fog_rain == "1"
    assert fog_clear == "0"


def test_world_spec_time_of_day_drives_sun_when_no_environment():
    from src.unity_schema import UnitySchema, WorldSpec
    from src.unity_generator import UnityProjectGenerator
    night = UnitySchema(project_name="NightProof", game_type="3D", platform="PC",
                        render_pipeline="URP", scenes=["Main"],
                        world_spec=WorldSpec(time_of_day="night"))
    _, intensity = _fog_and_intensity(
        UnityProjectGenerator().generate(night, {})["NightProof/Assets/Scenes/Main.unity"])
    assert intensity == "0.25"   # night preset intensity, unity_generator.py _TIME_OF_DAY_PRESETS


def test_explicit_environment_wins_over_world_spec():
    from src.unity_schema import UnitySchema, WorldSpec, EnvironmentSpec
    from src.unity_generator import UnityProjectGenerator
    schema = UnitySchema(project_name="EnvWins", game_type="3D", platform="PC",
                         render_pipeline="URP", scenes=["Main"],
                         environment=EnvironmentSpec(time_of_day="dawn"),
                         world_spec=WorldSpec(time_of_day="night", weather="storm"))
    _, intensity = _fog_and_intensity(
        UnityProjectGenerator().generate(schema, {})["EnvWins/Assets/Scenes/Main.unity"])
    assert intensity == "0.9"   # dawn preset, not night — environment takes priority


# ── Track 7: microgens -> folded into schema.atoms ────────────────────────────

def test_resolve_microgens_prepends_atoms():
    from src.unity_schema import UnitySchema, MicrogensSpec, AtomInvocation
    from src.capability_resolver import resolve_microgens
    schema = UnitySchema(project_name="MgProof", game_type="3D", platform="PC",
                         render_pipeline="URP",
                         atoms=[AtomInvocation(atom="Existing", args={})],
                         microgens=[MicrogensSpec(kind="cutscene", input={},
                                                  atoms=[AtomInvocation(atom="Wait", args={"seconds": 1.0})])])
    schema, msgs = resolve_microgens(schema)
    assert [a.atom for a in schema.atoms] == ["Wait", "Existing"]
    assert msgs and "1 atoms injected" in msgs[0]


def test_resolve_microgens_noop_when_empty():
    from src.unity_schema import UnitySchema
    from src.capability_resolver import resolve_microgens
    schema = UnitySchema(project_name="MgEmpty", game_type="3D", platform="PC",
                         render_pipeline="URP")
    schema2, msgs = resolve_microgens(schema)
    assert schema2 is schema
    assert msgs == []


def test_microgen_atoms_render_in_game_sequencer_end_to_end():
    from src.unity_schema import UnitySchema, MicrogensSpec, AtomInvocation
    from src.capability_resolver import resolve_microgens
    from src.unity_generator import UnityProjectGenerator
    schema = UnitySchema(project_name="MgRender", game_type="3D", platform="PC",
                         render_pipeline="URP",
                         microgens=[MicrogensSpec(kind="cutscene", input={},
                                                  atoms=[AtomInvocation(atom="Wait", args={"seconds": 1.0})])])
    schema, _ = resolve_microgens(schema)
    files = UnityProjectGenerator().generate(schema, {})
    seq_files = [v for k, v in files.items() if "GameSequencer" in k and k.endswith(".cs")]
    assert seq_files, "no GameSequencer.cs rendered"
    assert "Wait" in seq_files[0]


def test_game_sequencer_always_yields_even_with_non_yielding_atoms():
    """CS0161 regression: Sequence() is `IEnumerator`, but several atom snippets
    (Teleport, SetFlag, BindAction, SetModel, ...) emit plain statements with no
    `yield`. A sequence built entirely from those has no yield anywhere in the
    method body, so C# compiles it as a non-iterator that must explicitly
    `return` — which it never did. The template must guarantee at least one
    yield regardless of which atoms are present."""
    from src.unity_schema import UnitySchema, AtomInvocation
    from src.unity_generator import UnityProjectGenerator
    schema = UnitySchema(project_name="NoYieldProof", game_type="3D", platform="PC",
                         render_pipeline="URP",
                         atoms=[AtomInvocation(atom="Teleport", args={"x": 1, "y": 0, "z": 1}),
                                AtomInvocation(atom="SetFlag", args={"flag": "f", "value": True})])
    files = UnityProjectGenerator().generate(schema, {})
    seq_files = [v for k, v in files.items() if "GameSequencer" in k and k.endswith(".cs")]
    assert seq_files, "no GameSequencer.cs rendered"
    content = seq_files[0]
    assert "transform.position" in content and "PlayerPrefs" in content  # both atoms rendered
    assert "yield" in content.split("private IEnumerator Sequence()", 1)[1]


# ── EntitySpawner instance colliders ────────────────────────────────────────

class TestEntityInstanceColliders:
    """Every model instance with a behavior or a real tag used to get the same
    hardcoded CapsuleCollider (height 2, center (0,1,0)) regardless of what the
    model was. Live symptom (TurboToyRacer): a path-laid road segment became a
    2.4-unit vertical pill standing on the origin, the player spawned inside it,
    and the car could never drive away."""

    @staticmethod
    def _schema(entities, name="ColT", game_type="3D"):
        from src.unity_schema import UnitySchema
        return UnitySchema(
            project_name=name, company_name="C", description="",
            game_type=game_type, platform="pc", render_pipeline="URP",
            unity_version="6000.0", color_space="Linear",
            features=[], scenes=["Main"], packages=[],
            scripts=[UnityScript(name="PlayerController", type="player", description="m")],
            entities=entities,
        )

    @pytest.fixture
    def road_and_props(self, gen):
        from src.unity_schema import EntitySpec
        schema = self._schema([
            EntitySpec(name="RoadSegments", kind="road", count=20, shape="plane",
                       placement="path", tag="Track", scale=1.2,
                       path_points=[[0, 0, 0], [0, 0, 40], [20, 2, 80]]),
            EntitySpec(name="Trees", kind="prop", count=10, shape="cylinder",
                       placement="scatter", tag="Scenery"),
        ])
        return gen.generate(schema, {})["ColT/Assets/Scripts/EntitySpawner.cs"]

    def test_fixed_capsule_collider_is_gone(self, road_and_props):
        assert "CapsuleCollider" not in road_and_props
        assert "cc.height = 2f" not in road_and_props

    def test_instances_collide_as_their_measured_box(self, road_and_props):
        assert "AddComponent<BoxCollider>()" in road_and_props
        assert "bc.size   = colSize;" in road_and_props
        assert "colSize   = _mb.size;" in road_and_props

    def test_surface_kinds_collide_as_a_base_slab(self, road_and_props):
        assert "IsSurfaceKind(kind)" in road_and_props
        assert "Mathf.Min(_mb.size.y, 0.5f)" in road_and_props
        for kind in ("road", "track", "bridge", "ramp", "platform", "runway"):
            assert f'case "{kind}":' in road_and_props

    def test_entity_kind_is_passed_to_the_spawner(self, road_and_props):
        assert 'SpawnGroup("RoadSegments", "road",' in road_and_props
        assert 'SpawnGroup("Trees", "prop",' in road_and_props

    def test_tiled_path_group_forwards_its_kind(self, gen):
        from src.unity_schema import EntitySpec
        schema = self._schema([
            EntitySpec(name="Track", kind="track", count=30, shape="cube",
                       placement="path", tag="Track", path_width=12.0,
                       path_points=[[0, 0, 0], [0, 0, 60], [40, 0, 90]]),
        ], name="TileKind")
        cs = gen.generate(schema, {})["TileKind/Assets/Scripts/EntitySpawner.cs"]
        assert 'SpawnTiledGroup("Track", "track",' in cs
        assert "SpawnGroup(groupName, kind, count," in cs   # measure-fail fallback
        assert cs.count("{") == cs.count("}")

    def test_2d_topdown_still_compiles_without_measured_bounds(self, gen):
        """2d_topdown skips the bounds-measuring block entirely — the collider
        vars have to be declared on that branch too or the C# won't build."""
        from src.unity_schema import EntitySpec
        schema = self._schema([
            EntitySpec(name="Foes", kind="enemy", count=6, shape="capsule",
                       placement="scatter", tag="Enemy"),
        ], name="TopDownCol", game_type="2d_topdown")
        cs = gen.generate(schema, {})["TopDownCol/Assets/Scripts/EntitySpawner.cs"]
        assert cs.count("Vector3 colSize") == 1
        assert cs.count("{") == cs.count("}")

    def test_deterministic(self, gen):
        from src.unity_schema import EntitySpec
        def _build():
            schema = self._schema([
                EntitySpec(name="RoadSegments", kind="road", count=8, shape="plane",
                           placement="path", tag="Track",
                           path_points=[[0, 0, 0], [0, 0, 40]])])
            return gen.generate(schema, {})["ColT/Assets/Scripts/EntitySpawner.cs"]
        assert _build() == _build()


class TestSignatureTypeStubs:
    """A type named only in a method SIGNATURE still needs its stub.

    Live regression (BlockWell/GridLogic): `GridPiece` appeared as a field type
    and was stubbed; `GridCell` appeared only as the return type of
    `RotateGrid` and was not, so the script shipped one CS0246.
    """

    class _M:
        def __init__(self, sig): self.signature = sig

    class _F:
        def __init__(self, ty): self.type = ty

    def _names(self, fields, methods):
        from src.unity_generator import (_stub_type_names, _signature_type_names,
                                         _TypeCarrier)
        sig = [_TypeCarrier(t) for t in _signature_type_names(methods)]
        return _stub_type_names(list(fields) + sig, frozenset())

    def test_return_type_is_stubbed(self):
        names = self._names(
            [self._F("GridPiece")],
            [self._M("private GridCell[,] RotateGrid(GridPiece piece)")])
        assert "GridCell" in names and "GridPiece" in names

    def test_parameter_type_is_stubbed(self):
        assert "Tetromino" in self._names(
            [], [self._M("public void Place(Tetromino piece)")])

    def test_builtins_are_not_stubbed(self):
        assert self._names([], [self._M("public bool Try(int x, float y)")]) == []

    def test_multidim_array_suffix_is_stripped(self):
        from src.unity_generator import _extract_base_type
        # rstrip("[]") alone stopped at the comma and yielded "GridCell[,".
        assert _extract_base_type("GridCell[,]") == "GridCell"
        assert _extract_base_type("GridCell[,,]") == "GridCell"

    def test_optional_parameter_default_is_not_part_of_the_type(self):
        # Live regression (Chromabreak/Interactable.cs): a parameter with a
        # default value split into ["float","lockoutSeconds","=","1.5f"], and
        # everything-but-the-last-token was read as the type — so the stub
        # collector emitted `public class float lockoutSeconds =` and the
        # syntax gate stopped the build with five CS errors.
        from src.unity_generator import _signature_type_names
        assert _signature_type_names(
            [self._M("public void SetLockout(float lockoutSeconds = 1.5f)")]
        ) == ["void", "float"]
        # ...and a real invented type still survives alongside a default.
        assert "WaveProbe" in self._names(
            [], [self._M("private void Tick(float dt = 0.02f, WaveProbe probe = null)")])

    def test_parameter_modifiers_are_not_part_of_the_type(self):
        # `ref Foo bar` yielded the "type" `ref Foo` — same class of defect.
        assert "ShardBurst" in self._names(
            [], [self._M("private void Push(ref ShardBurst burst, int count)")])
        assert "SparkCue" in self._names(
            [], [self._M("public void Emit(params SparkCue[] cues)")])

    def test_a_name_that_is_not_an_identifier_is_never_stubbed(self):
        # The containment layer: whatever a producer hands over, a stub is
        # written as `public class <name>`, so a non-identifier must be dropped
        # rather than compiled into a syntax error.
        assert self._names([self._F("float lockoutSeconds =")], []) == []
