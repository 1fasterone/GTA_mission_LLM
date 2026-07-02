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
        assert ', _path_0, sh);' in cs                          # path passed to group
        assert ', null, sh);' in cs                             # non-path group gets null
        assert cs.count("{") == cs.count("}")


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

    def test_scenesetup_has_no_genre_boilerplate(self, gen):
        files = gen.generate(self._schema(), {})
        ss = files["MP/Assets/Scripts/SceneSetup.cs"]
        for banned in ("SURVIVAL HORROR", "Warden", "YOU ESCAPED", "Level1", "BossRoom"):
            assert banned not in ss, f"SceneSetup still emits boilerplate: {banned}"

    def test_unmapped_kind_falls_back_to_real_prop(self, gen):
        # No-generic-defaults: an unmapped kind uses a REAL default prop (never a
        # primitive), so even strict mode succeeds when a library exists.
        from src.unity_schema import EntitySpec
        files = gen.generate(self._schema(entities=[
            EntitySpec(name="Wyrm", kind="dragon", count=1),
        ]), {}, strict_assets=True)
        cs = files["MP/Assets/Scripts/EntitySpawner.cs"]
        assert '"Models/Default/Prop/Prop"' in cs        # real prop fallback, not a primitive

    def test_strict_assets_raises_when_library_missing(self, gen):
        # Strict only hard-fails when there is no real model AT ALL (empty library).
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
