"""
Tests for UnityProjectGenerator: file output, scene content, prefabs,
input actions, canvas/event system, and meta file formats.
"""
import json
import re

import pytest

from src.unity_generator import (
    UnityProjectGenerator,
    _UGUI_GUIDS,
    _generate_material,
    _make_guid,
    _make_uuid,
)
from src.unity_schema import UnityScript

# Stale UGUI script GUIDs that were wrong on Unity 6 and caused missing-script
# components on Canvas/EventSystem. Kept here so regressions assert they never return.
_STALE_GRAPHIC_RAYCASTER_GUID = "dc42784cf147fd346b4c9064bdc43eb9"
_STALE_EVENT_SYSTEM_GUID = "76c392e42b5098d4f8003d6289d9e15e"
# Shader GUIDs (verified on Unity 6000.0).
_URP_LIT_GUID = "933532a4fcc9baf4fa0491de14d08ed7"
_HDRP_LIT_GUID = "6e4ae4064600d784cac1e41a9e6f2e59"
_BUILTIN_STANDARD_GUID = "0000000000000000f000000000000000"


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
        full_3d_schema.platform = "Android"
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
        assert "0.0000f);" in cs

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
