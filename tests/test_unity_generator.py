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

    def test_models_emitted_with_humanoid_meta(self, gen):
        files = gen.generate(self._schema(), {})
        meta = files.get("MP/Assets/Resources/Models/Default/Player/Player.fbx.meta", "")
        assert "animationType: 3" in meta            # Humanoid rig
        assert "MP/Assets/Scripts/ModelSwap.cs" in files
        assert "MP/Assets/Editor/SetupDefaultModels.cs" in files
        s = scene(files, "MP", "Main")
        assert "modelPath: Models/Default/Player/Player" in s
        assert "controllerPath: Models/Default/DefaultCharacter" in s

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
        assert '"Models/Default/Enemy/Enemy"' in cs  # enemy model exists in seed
        assert '"", null, sh);' in cs                # crowd has no file yet → primitive
