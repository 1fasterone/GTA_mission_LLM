"""
P1 tests for the composition layer (src/unity_compose.py).

Offline + deterministic: composing a micro into a project's Assets must produce the
script, a stable .cs.meta GUID, a correct manifest, and a scene attach-block whose
GUID matches the meta — i.e. the wiring metadata is internally consistent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.unity_compose import MicroComposer, Subsystem


def _compose(tmp_path, project="ProofProj", **sub_kw):
    sub = Subsystem(kind="controller", variant="third_person", target="player",
                    options={"input_backend": "legacy"}, **sub_kw)
    assets = Path(tmp_path) / "Assets"
    units = MicroComposer(project).compose([sub], assets)
    return assets, units


def test_compose_emits_script_and_meta(tmp_path):
    assets, units = _compose(tmp_path)
    u = units[0]
    cs = assets / "Micros" / "controller" / "Scripts" / f"{u.class_name}.cs"
    assert cs.exists(), "composed controller script not written"
    assert (cs.parent / f"{cs.name}.meta").exists(), "missing .cs.meta"
    assert "public class" in cs.read_text(encoding="utf-8")


def test_meta_guid_matches_manifest_and_attach_block(tmp_path):
    assets, units = _compose(tmp_path)
    u = units[0]
    meta = (assets / "Micros" / "controller" / "Scripts" / f"{u.class_name}.cs.meta").read_text(encoding="utf-8")
    assert f"guid: {u.guid}" in meta, "manifest GUID must match the written .cs.meta"
    # The scene attach-block must reference exactly that GUID (wiring consistency).
    block = MicroComposer.attach_block(u, file_id=1234567)
    assert f"guid: {u.guid}" in block
    assert "MonoBehaviour" in block and "m_Script" in block


def test_composition_is_deterministic(tmp_path):
    a1, u1 = _compose(tmp_path / "run1")
    a2, u2 = _compose(tmp_path / "run2")
    assert u1[0].guid == u2[0].guid, "GUIDs must be stable across runs (same project)"


def test_project_name_changes_guid(tmp_path):
    _, u1 = _compose(tmp_path / "a", project="ProjA")
    _, u2 = _compose(tmp_path / "b", project="ProjB")
    assert u1[0].guid != u2[0].guid, "different projects must not collide on GUID"


def test_legacy_controller_has_no_inputsystem_dependency(tmp_path):
    assets, units = _compose(tmp_path)
    cs = (assets / "Micros" / "controller" / "Scripts" / f"{units[0].class_name}.cs").read_text(encoding="utf-8")
    assert "UnityEngine.InputSystem" not in cs, "legacy backend must not need the Input System package"


def test_manifest_round_trips(tmp_path):
    _, units = _compose(tmp_path)
    js = MicroComposer.manifest(units)
    assert units[0].class_name in js and units[0].guid in js


def test_unknown_kind_raises(tmp_path):
    with pytest.raises(ValueError, match="unknown subsystem"):
        MicroComposer("P").compose([Subsystem(kind="not_a_micro")], Path(tmp_path) / "Assets")


# ── P2: mapper + scene splice ───────────────────────────────────────────────

from src.unity_compose import subsystems_for
from src.unity_schema import UnitySchema
from src.unity_generator import UnityProjectGenerator


def test_mapper_core_subsystems_present():
    subs = subsystems_for("3D", [])
    kinds = {s.kind for s in subs}
    assert {"controller", "camera", "hud", "menu", "saveload"} <= kinds


def test_mapper_feature_driven_additions():
    subs = subsystems_for("3D", ["ai", "audio", "inventory"])
    kinds = {s.kind for s in subs}
    assert {"ai", "spawner", "audio", "inventory"} <= kinds


def test_mapper_2d_picks_platformer_controller():
    ctrl = next(s for s in subsystems_for("2D", []) if s.kind == "controller")
    assert ctrl.variant == "2d_platformer"


def test_mapper_targets_are_valid():
    for s in subsystems_for("3D", ["ai", "audio"]):
        assert s.target in {"player", "camera", "ui", "systems", "world", "npc"}


def test_scene_splices_composed_units():
    schema = UnitySchema(project_name="ComposeProof", game_type="3D",
                         platform="PC", render_pipeline="builtin", scenes=["Main"])
    subs  = subsystems_for(schema.game_type, ["ai"])
    units = MicroComposer(schema.project_name).plan(subs)
    files = UnityProjectGenerator().generate(schema, {}, composed_units=units)
    scene = files["ComposeProof/Assets/Scenes/Main.unity"]
    for u in units:                      # every scene-attachable component wired by its GUID
        if u.kind == "input":            # ScriptableObject (GameWiring._inputSO) — not a scene component
            assert u.guid not in scene, "input SO must not be spliced as a scene component"
            continue
        assert u.guid in scene, f"{u.kind} guid missing from scene"


def _guid_context(scene: str, guid: str, before: int = 280) -> str:
    idx = scene.find("guid: " + guid)
    return scene[max(0, idx - before): idx]


def test_p3_per_target_attachment():
    """Composed components attach to the right GameObjects, not one dump object."""
    schema = UnitySchema(project_name="P3", game_type="3D",
                         platform="PC", render_pipeline="builtin", scenes=["Main"])
    units = MicroComposer("P3").plan(subsystems_for("3D", ["ai"]))
    scene = UnityProjectGenerator().generate(schema, {}, composed_units=units)["P3/Assets/Scenes/Main.unity"]
    assert "_GameSystems" not in scene          # old single dump is gone
    assert "m_Name: _Systems" in scene
    assert "m_Name: Player" in scene
    by_kind = {u.kind: u for u in units}
    # camera → Main Camera (705507993), hud → Canvas (705507600), controller → Player holder.
    assert "705507993" in _guid_context(scene, by_kind["camera"].guid)
    assert "705507600" in _guid_context(scene, by_kind["hud"].guid)
    assert "900001000" in _guid_context(scene, by_kind["controller"].guid)


def test_no_composed_units_leaves_scene_unchanged():
    schema = UnitySchema(project_name="Plain", game_type="3D",
                         platform="PC", render_pipeline="builtin", scenes=["Main"])
    scene = UnityProjectGenerator().generate(schema, {})["Plain/Assets/Scenes/Main.unity"]
    assert "_Systems" not in scene and "900002000" not in scene


def test_systems_gameobject_gets_collider_for_trigger_and_interactable():
    """Regression: SceneTrigger (OnTriggerEnter) and WorldInteractable
    ([RequireComponent(Collider)]) are bundled onto the shared _Systems
    GameObject, which previously carried zero Collider component — their
    OnTrigger* callbacks could never fire. _Systems must now author a real
    collider (2D vs 3D matching game_type) whenever either kind is composed."""
    schema3d = UnitySchema(project_name="Coll3D", game_type="3D",
                           platform="PC", render_pipeline="builtin", scenes=["Main"])
    units3d = MicroComposer("Coll3D").plan(subsystems_for("3D", []))
    scene3d = UnityProjectGenerator().generate(schema3d, {}, composed_units=units3d)["Coll3D/Assets/Scenes/Main.unity"]
    assert "&900002900\nBoxCollider:" in scene3d
    assert "BoxCollider2D:" not in scene3d.split("&900002900")[1][:200]

    schema2d = UnitySchema(project_name="Coll2D", game_type="2D",
                           platform="PC", render_pipeline="builtin", scenes=["Main"])
    units2d = MicroComposer("Coll2D").plan(subsystems_for("2D", []))
    scene2d = UnityProjectGenerator().generate(schema2d, {}, composed_units=units2d)["Coll2D/Assets/Scenes/Main.unity"]
    assert "&900002900\nBoxCollider2D:" in scene2d


def test_systems_gameobject_no_collider_without_trigger_or_interactable():
    schema = UnitySchema(project_name="NoColl", game_type="3D",
                         platform="PC", render_pipeline="builtin", scenes=["Main"])
    subs = [s for s in subsystems_for("3D", []) if s.kind not in ("trigger", "interactable")]
    units = MicroComposer("NoColl").plan(subs)
    scene = UnityProjectGenerator().generate(schema, {}, composed_units=units)["NoColl/Assets/Scenes/Main.unity"]
    assert "900002900" not in scene


def test_plan_includes_gamewiring_unit():
    units = MicroComposer("W").plan(subsystems_for("3D", []))
    wiring = [u for u in units if u.kind == "wiring"]
    assert len(wiring) == 1 and wiring[0].class_name == "GameWiring"
    file_ids = [u.file_id for u in units]
    assert len(file_ids) == len(set(file_ids)), "scene fileIDs must be unique"


def test_emit_writes_gamewiring_referencing_composed_classes(tmp_path):
    subs = subsystems_for("3D", [])
    assets = Path(tmp_path) / "Assets"
    units = MicroComposer("W").emit(subs, assets)
    gw = (assets / "Micros" / "wiring" / "Scripts" / "GameWiring.cs").read_text(encoding="utf-8")
    assert "public class GameWiring" in gw
    # references real composed class names + has the menu seam
    for u in units:
        if u.kind not in ("wiring", "context", "input"):
            assert u.class_name in gw, f"GameWiring missing reference to {u.class_name}"
    assert ".Show()" in gw   # fallback Escape handler — variable name is class_name.lower()
    assert gw.count("{") == gw.count("}")


def test_covered_script_types_keep_unique_glue():
    from src.unity_compose import COVERED_SCRIPT_TYPES
    # Micros own these built-in script types (dropped in --compose mode)...
    assert {"player", "camera", "health", "dialogue_manager"} <= COVERED_SCRIPT_TYPES
    # ...but the unique project glue must NOT be covered (kept in composed projects).
    for kept in ("game_manager", "mesh_builder", "material_palette",
                 "procedural_mesh", "level_generator"):
        assert kept not in COVERED_SCRIPT_TYPES


def test_gamewiring_connects_health_to_hud(tmp_path):
    # Default mapper includes health (target player) + hud health_bar → the seam wires.
    units = MicroComposer("HSeam").emit(subsystems_for("3D", []), Path(tmp_path) / "Assets")
    gw = (Path(tmp_path) / "Assets" / "Micros" / "wiring" / "Scripts" / "GameWiring.cs").read_text(encoding="utf-8")
    assert "OnHealthChanged += " in gw and "SetHealth" in gw   # seam 1 wired; var names are class_name.lower()
    assert {"health", "hud"} <= {u.kind for u in units}


def test_plan_and_emit_agree_on_guid(tmp_path):
    subs = subsystems_for("3D", [])
    c = MicroComposer("AgreeProj")
    planned = c.plan(subs)
    emitted = c.emit(subs, Path(tmp_path) / "Assets")
    pmap = {u.kind: u.guid for u in planned}
    emap = {u.kind: u.guid for u in emitted}
    assert pmap == emap, "plan() and emit() must produce identical GUIDs"


# ── Locomotion routing (drive/fly/swim override the walker default) ────────────

def test_subsystems_for_drive_selects_vehicle():
    subs = subsystems_for("3D", [], locomotion="drive")
    ctrl = next(s for s in subs if s.kind == "controller")
    cam  = next(s for s in subs if s.kind == "camera")
    assert ctrl.variant == "vehicle_car"
    assert cam.variant == "vehicle_chase"


def test_subsystems_for_fly_and_swim():
    assert next(s for s in subsystems_for("3D", [], locomotion="fly")
                if s.kind == "controller").variant == "flying"
    assert next(s for s in subsystems_for("3D", [], locomotion="swim")
                if s.kind == "controller").variant == "swimming"


def test_subsystems_for_2d_ignores_drive():
    ctrl = next(s for s in subsystems_for("2D", [], locomotion="drive")
                if s.kind == "controller")
    assert ctrl.variant == "2d_platformer"


def test_subsystems_for_vr_honors_swim_locomotion():
    # Regression (live Unity, DeepDive run): the locomotion-override block only
    # applied to gt in ("3d", "top_down", "isometric") — a correctly-classified
    # VR+swim project silently kept the "first_person" walking controller
    # instead of "swimming" because "vr" wasn't in that set. VR+swim (or
    # VR+fly/drive) is a legitimate combination and must get the same override.
    ctrl = next(s for s in subsystems_for("VR", [], locomotion="swim")
                if s.kind == "controller")
    assert ctrl.variant == "swimming"


def test_subsystems_for_vr_without_locomotion_override_stays_first_person():
    ctrl = next(s for s in subsystems_for("VR", [], locomotion="walk")
                if s.kind == "controller")
    assert ctrl.variant == "first_person"


# ── Genre-aware controller fallback (fps -> first_person, not third_person) ────

def test_subsystems_for_fps_genre_selects_first_person():
    subs = subsystems_for("3D", [], genre="fps")
    ctrl = next(s for s in subs if s.kind == "controller")
    cam  = next(s for s in subs if s.kind == "camera")
    assert ctrl.variant == "first_person"
    assert cam.variant == "first_person"


def test_subsystems_for_no_genre_still_defaults_third_person():
    ctrl = next(s for s in subsystems_for("3D", []) if s.kind == "controller")
    assert ctrl.variant == "third_person"


def test_subsystems_for_climbing_brief_selects_wall_controller_and_camera():
    prompt = ("A third-person mountain climbing game with a wall of rock holds "
              "and ledges leading to a summit flag; manage stamina while climbing.")
    subs = subsystems_for("3D", [], user_prompt=prompt)
    ctrl = next(s for s in subs if s.kind == "controller")
    cam = next(s for s in subs if s.kind == "camera")
    assert ctrl.variant == "climbing"
    assert cam.variant == "climbing_follow"
    assert cam.options["offset_x"] > 0.0
    assert cam.options["offset_z"] < 0.0
    assert cam.options["collision_avoidance"] is True


def test_subsystems_for_fps_genre_does_not_override_explicit_game_type():
    # An explicit dimension/perspective (2D/top_down/isometric/VR) wins over an
    # fps genre hint — only the "no opinion" 3D default defers to genre.
    ctrl_td = next(s for s in subsystems_for("top_down", [], genre="fps")
                   if s.kind == "controller")
    assert ctrl_td.variant == "top_down"
    ctrl_2d = next(s for s in subsystems_for("2D", [], genre="fps")
                   if s.kind == "controller")
    assert ctrl_2d.variant == "2d_platformer"


def test_racing_genre_selects_racing_hud():
    # The race-feedback HUD is the racing-genre counterpart to the AI line's
    # waypoint_follow branch — both key off genre, not locomotion.
    hud = next(s for s in subsystems_for("3D", [], genre="racing") if s.kind == "hud")
    assert hud.variant == "racing_hud"


def test_non_racing_genres_keep_health_bar_hud():
    for g in ("universal", "fps", "platformer"):
        hud = next(s for s in subsystems_for("3D", [], genre=g) if s.kind == "hud")
        assert hud.variant == "health_bar", g


def test_racing_hud_does_not_trigger_health_to_hud_seam():
    # SEAM 1 (Health -> HUD) is gated on hud.variant == "health_bar"; racing_hud
    # is event-driven, so a racing project must compose without that seam and
    # without error. Sanity: the emitted GameHud is racing_hud, not health_bar.
    subs = subsystems_for("3D", [], genre="racing", locomotion="drive")
    hud = next(s for s in subs if s.kind == "hud")
    assert hud.variant == "racing_hud"


def test_locomotion_schema_normalises_synonyms():
    from src.unity_schema import UnitySchema
    s = UnitySchema(project_name="L", game_type="3D", platform="pc",
                    render_pipeline="URP", locomotion="racing")
    assert s.locomotion == "drive"
    s2 = UnitySchema(project_name="L", game_type="3D", platform="pc",
                     render_pipeline="URP", locomotion="nonsense")
    assert s2.locomotion == "walk"


# ── Newly-wired optional micros: vfx / npc_dialog / transition ────────────────

def test_vfx_feature_composes_impact_fx():
    subs = subsystems_for("3D", ["vfx"])
    v = [s for s in subs if s.kind == "vfx"]
    assert len(v) == 1 and v[0].variant == "impact_fx" and v[0].name == "ImpactVFX"


def test_npc_dialog_feature_composes_with_skip_audio_default():
    # skip_audio is the DEFAULT so a bare MicroComposer.emit stays TTS-free.
    # The main pipeline (unity_pipeline.run) LIFTS it — popping skip_audio and
    # injecting concept-specific meta._pre_lines — so `unity-create --compose`
    # renders real Kokoro clips. See test_npc_dialog_compose_lifts_skip_with_pre_lines.
    subs = subsystems_for("3D", ["npcs"])
    npc = [s for s in subs if s.kind == "npc_dialog"]
    assert len(npc) == 1
    assert npc[0].options.get("meta", {}).get("skip_audio") is True


def test_npc_dialog_compose_lifts_skip_with_pre_lines(tmp_path):
    # When concept-specific lines are pre-injected (as unity_pipeline.run does)
    # and skip_audio is lifted, compose emits the real dialog data with those
    # exact lines — concept-driven, not the generic preset.
    subs = subsystems_for("3D", ["npcs"])
    npc = next(s for s in subs if s.kind == "npc_dialog")
    npc.options["meta"].pop("skip_audio", None)
    npc.options["meta"]["_pre_lines"] = [
        {"line_id": "greet", "text": "Rev those engines, rookie!", "emotion": "excited", "tags": []},
    ]
    # generate_clips off so the test stays fast (no Kokoro load) — we only assert
    # the concept line reaches the emitted DialogData, proving skip was lifted.
    assets = Path(tmp_path) / "Assets"
    MicroComposer("PreLineProof",
                  project_config={"audio": {"generate_clips": False}}).emit(subs, assets)
    data = (assets / "Micros" / "npc_dialog" / "Data" / "NpcDialogData.json").read_text(encoding="utf-8")
    assert "Rev those engines, rookie!" in data


def test_transition_feature_composes_fade():
    subs = subsystems_for("3D", ["levels"])
    assert any(s.kind == "transition" and s.variant == "fade" for s in subs)


def test_new_micros_absent_without_their_features():
    # Feature-gating must not leak the new micros into a plain project.
    kinds = {s.kind for s in subsystems_for("3D", [])}
    assert not ({"vfx", "npc_dialog", "transition"} & kinds)


def test_npc_dialog_attach_class_is_player_not_bare_name():
    """Regression: the default attach_class (schema.name) would make GameWiring emit
    FindFirstObjectByType<NpcDialog>() — a CS0246, since only NpcDialogPlayer exists."""
    sub = next(s for s in subsystems_for("3D", ["npcs"]) if s.kind == "npc_dialog")
    units = MicroComposer("NpcAttach").plan([sub])
    npc = next(u for u in units if u.kind == "npc_dialog")
    assert npc.class_name == "NpcDialogPlayer"


def test_compose_new_micros_gamewiring_references_real_classes(tmp_path):
    """All three newly-wired micros compose; GameWiring references each real
    MonoBehaviour class; braces balance; and the broken bare NpcDialog ref is gone."""
    subs = subsystems_for("3D", ["vfx", "npcs", "levels"])
    units = MicroComposer("WireProof").emit(subs, Path(tmp_path) / "Assets")
    gw = (Path(tmp_path) / "Assets" / "Micros" / "wiring" / "Scripts"
          / "GameWiring.cs").read_text(encoding="utf-8")
    assert gw.count("{") == gw.count("}"), "GameWiring braces unbalanced"
    assert {"vfx", "npc_dialog", "transition"} <= {u.kind for u in units}
    for u in units:
        if u.kind not in ("wiring", "context", "input"):
            assert u.class_name in gw, f"GameWiring missing reference to {u.class_name}"
    assert "FindFirstObjectByType<NpcDialogPlayer>()" in gw
    assert "FindFirstObjectByType<NpcDialog>()" not in gw   # the old CS0246


def test_compose_npc_dialog_skips_audio_and_emits_player(tmp_path):
    """Compose path stays Higgs-free even when config enables clip generation, and
    writes the real NpcDialogPlayer MonoBehaviour."""
    assets = Path(tmp_path) / "Assets"
    MicroComposer("NpcAudioProof",
                  project_config={"audio": {"generate_clips": True}}).emit(
        subsystems_for("3D", ["npcs"]), assets)
    player = assets / "Micros" / "npc_dialog" / "Scripts" / "NpcDialogPlayer.cs"
    assert player.exists()
    assert "class NpcDialogPlayer" in player.read_text(encoding="utf-8")
    assert not (assets / "Micros" / "npc_dialog" / "Resources").exists(), \
        "compose must not generate dialog audio clips"


def test_module_deps_for_new_micros():
    units = MicroComposer("Deps").plan(subsystems_for("3D", ["vfx", "npcs", "levels"]))
    deps = MicroComposer.module_deps(units)
    assert "com.unity.modules.audio" in deps      # npc_dialog AudioSource
    assert "com.unity.ugui" in deps               # vfx/transition UGUI


def test_swim_compose_skips_grounded_bridge(tmp_path):
    """Fix #5 coupling: a swimming controller exposes no IsGrounded, so the
    composed GameWiring must NOT emit the grounded bridge (would be CS1061)."""
    subs = subsystems_for("3D", ["audio"], locomotion="swim")
    assert next(s.variant for s in subs if s.kind == "controller") == "swimming"
    MicroComposer("SwimWire").emit(subs, Path(tmp_path) / "Assets")
    gw = (Path(tmp_path) / "Assets" / "Micros" / "wiring" / "Scripts"
          / "GameWiring.cs").read_text(encoding="utf-8")
    assert "IsGrounded" not in gw
    assert gw.count("{") == gw.count("}")


def test_vehicle_controller_wiring_omits_input_and_grounded(tmp_path):
    """A vehicle_car controller reads its own input and has no ground plane, so
    Controller.cs.j2 omits SetInputReader + IsGrounded. GameWiring must not call
    either (would be CS1061), even when a statemachine/animator is present."""
    from src.unity_compose import Subsystem
    subs = [
        Subsystem(kind="controller",   variant="vehicle_car", target="player", name="PlayerController"),
        Subsystem(kind="statemachine", variant="enemy_fsm",   target="world",  name="EnemyFSM"),
        Subsystem(kind="animator",     variant="vehicle",     target="player", name="CarAnimator"),
    ]
    MicroComposer("VehicleWire").emit(subs, Path(tmp_path) / "Assets")
    gw = (Path(tmp_path) / "Assets" / "Micros" / "wiring" / "Scripts"
          / "GameWiring.cs").read_text(encoding="utf-8")
    assert "SetInputReader" not in gw
    assert "IsGrounded" not in gw
    assert gw.count("{") == gw.count("}")
    pc = (Path(tmp_path) / "Assets" / "Micros" / "controller" / "Scripts"
          / "PlayerController.cs").read_text(encoding="utf-8")
    assert "Transform[] FindWheelBones" in pc        # CS0029 regression
    assert "Transform[] wheelBones = FindWheelBones" in pc


def test_enemy_only_fsm_wiring_omits_move_jump(tmp_path):
    """SetMove/SetJump/SetGrounded are gated `{% if variant == "player_fsm" %}`
    in StateMachine.cs.j2 -- an enemy_fsm has none of them. `_pick("statemachine",
    "player")` returns the SOLE statemachine unit even when its target isn't
    "player" (the single-candidate short-circuit), so a project whose only FSM
    is an enemy/reviewer AI must not get SEAM 5/6 wired to it. Hit live on the
    RepoQuest run, GameWiring.cs(136,137,260) CS1061 -- EnemyFSM has no
    SetJump/SetMove."""
    from src.unity_compose import Subsystem
    subs = [
        Subsystem(kind="controller",   variant="third_person", target="player", name="PlayerController"),
        Subsystem(kind="statemachine", variant="enemy_fsm",    target="world",  name="EnemyFSM"),
    ]
    MicroComposer("EnemyOnlyWire").emit(subs, Path(tmp_path) / "Assets")
    gw = (Path(tmp_path) / "Assets" / "Micros" / "wiring" / "Scripts"
          / "GameWiring.cs").read_text(encoding="utf-8")
    assert "SetJump" not in gw
    assert "SetMove" not in gw
    assert gw.count("{") == gw.count("}")


def test_walk_compose_keeps_grounded_bridge(tmp_path):
    MicroComposer("WalkWire").emit(subsystems_for("3D", []), Path(tmp_path) / "Assets")
    gw = (Path(tmp_path) / "Assets" / "Micros" / "wiring" / "Scripts"
          / "GameWiring.cs").read_text(encoding="utf-8")
    assert "IsGrounded" in gw   # default third_person controller still bridges grounded


# ── AI enemy-variant selection: cover_shooter ──────────────────────────────────

def test_cover_words_select_cover_shooter_variant():
    from src.unity_schema import EntitySpec
    enemy = EntitySpec(name="Guard", kind="enemy")
    subs = subsystems_for("3D", [], user_prompt="soldiers take cover behind crates and shoot",
                          entities=[enemy])
    ai = next(s for s in subs if s.kind == "ai")
    assert ai.variant == "cover_shooter"


def test_no_cover_words_keeps_navmesh_patrol_default():
    from src.unity_schema import EntitySpec
    enemy = EntitySpec(name="Guard", kind="enemy")
    subs = subsystems_for("3D", [], user_prompt="guards patrol the warehouse", entities=[enemy])
    ai = next(s for s in subs if s.kind == "ai")
    assert ai.variant == "navmesh_patrol"


def test_cover_words_do_not_override_racing_or_flying():
    from src.unity_schema import EntitySpec
    enemy = EntitySpec(name="Guard", kind="enemy")
    racing = subsystems_for("3D", [], genre="racing", entities=[enemy],
                            user_prompt="rivals take cover and race")
    assert next(s for s in racing if s.kind == "ai").variant == "waypoint_follow"
    flying = subsystems_for("3D", [], entities=[enemy],
                            user_prompt="hovering drones take cover and shoot")
    assert next(s for s in flying if s.kind == "ai").variant == "flying_swarm"


def test_cover_points_threaded_from_meta_by_entity_name():
    from src.unity_schema import EntitySpec
    enemy = EntitySpec(name="Guard", kind="enemy")
    baked = {"Guard": [{"position": [1.0, 0.0, 1.0], "cover_direction": [0.0, 0.0, -1.0]}]}
    subs = subsystems_for("3D", [], user_prompt="guards take cover and shoot",
                          entities=[enemy], cover_points=baked)
    ai = next(s for s in subs if s.kind == "ai")
    assert ai.options.get("cover_points") == baked["Guard"]


def test_cover_shooter_compose_produces_valid_syntax(tmp_path):
    """End-to-end: prompt -> cover_shooter selection -> cover_points baked
    into the composed AI script -> the composer's own real C# syntax gate."""
    from src.unity_schema import EntitySpec
    enemy = EntitySpec(name="Guard", kind="enemy", instance_positions=[[6.0, 0.0, 6.0]])
    obstacle = EntitySpec(name="Crate", kind="obstacle", scale=1.5, instance_positions=[[5.0, 0.0, 5.0]])
    baked = {"Guard": [{"position": [6.7, 0.0, 6.7], "cover_direction": [-0.7, 0.0, -0.7]}]}
    subs = subsystems_for("3D", [], user_prompt="guards take cover behind crates and shoot at the player",
                          entities=[enemy, obstacle], cover_points=baked)
    assert next(s for s in subs if s.kind == "ai").variant == "cover_shooter"
    units = MicroComposer("CoverProof").emit(subs, Path(tmp_path) / "Assets")
    ai_unit = next(u for u in units if u.kind == "ai")
    ai_cs = (Path(tmp_path) / "Assets" / "Micros" / "ai" / "Scripts"
            / f"{ai_unit.class_name}.cs").read_text(encoding="utf-8")
    assert "6.70f" in ai_cs
    assert "_coverClaimed[i]" in ai_cs
    assert ai_cs.count("{") == ai_cs.count("}")


# ── Content-based reachability: optional micros from schema content ───────────

def _ns(**kw):
    from types import SimpleNamespace
    return SimpleNamespace(**kw)


def test_transition_reachable_from_multi_scene():
    kinds = {s.kind for s in subsystems_for("3D", [], scenes=["Main", "Level2"])}
    assert "transition" in kinds


def test_no_transition_for_single_scene():
    kinds = {s.kind for s in subsystems_for("3D", [], scenes=["Main"])}
    assert "transition" not in kinds


def test_npc_dialog_and_dialogue_reachable_from_npc_entity():
    subs = subsystems_for("3D", [], entities=[_ns(kind="npc"), _ns(kind="pickup")])
    kinds = {s.kind for s in subs}
    assert "npc_dialog" in kinds and "dialogue" in kinds


def test_vfx_and_ai_reachable_from_enemy_entity():
    kinds = {s.kind for s in subsystems_for("3D", [], entities=[_ns(kind="enemy")])}
    assert {"vfx", "ai", "spawner"} <= kinds


def test_dialogue_reachable_from_dialogue_manager_script():
    kinds = {s.kind for s in subsystems_for("3D", [], scripts=[_ns(type="dialogue_manager")])}
    assert "dialogue" in kinds


def test_content_gating_backward_compatible_when_no_content():
    # No scenes/entities/scripts and no feature flags → only the core 12 (existing contract).
    kinds = {s.kind for s in subsystems_for("3D", [])}
    assert not ({"vfx", "npc_dialog", "transition", "inventory", "dialogue", "ai"} & kinds)


def test_gamewiring_fsm_to_animator_uses_crossfade(tmp_path):
    """Regression: OnFSMStateChanged must use CrossFade for all states (universal),
    not the humanoid-only PlayJump()/PlayDeath() which a creature animator lacks (CS1061)."""
    MicroComposer("FsmAnim").emit(subsystems_for("3D", ["navmesh"]), Path(tmp_path) / "Assets")
    gw = (Path(tmp_path) / "Assets" / "Micros" / "wiring" / "Scripts"
          / "GameWiring.cs").read_text(encoding="utf-8")
    assert "PlayJump" not in gw and "PlayDeath" not in gw
    assert 'CrossFade("Jump"' in gw and 'CrossFade("Death"' in gw


def test_gamewiring_does_not_findobject_input_scriptableobject(tmp_path):
    """Regression: the input micro is an IInputReader ScriptableObject (class
    `<name>InputReader`), assigned via the _inputSO SerializeField — NOT a scene
    MonoBehaviour. GameWiring must not FindFirstObjectByType<InputReader>() (CS0246)."""
    units = MicroComposer("InputWire").emit(subsystems_for("3D", []), Path(tmp_path) / "Assets")
    gw = (Path(tmp_path) / "Assets" / "Micros" / "wiring" / "Scripts"
          / "GameWiring.cs").read_text(encoding="utf-8")
    assert "input" in {u.kind for u in units}              # input IS composed...
    assert "FindFirstObjectByType<InputReader>" not in gw   # ...but never found in-scene
    assert "private InputReader " not in gw
    assert "_inputSO" in gw                                 # it's wired via the SerializeField


def test_compose_drops_scripts_that_collide_with_micros_by_name():
    """Regression (CS0101/CS0111): the LLM sometimes authors a `type: custom`
    script whose class name shadows a composed class — e.g. its own `PhysicsBody`
    or `GameContext`. Compose owns those names, so they must be dropped from
    schema.scripts; otherwise the base generator writes Assets/Scripts/<name>.cs
    next to the micro's Assets/Micros/.../<name>.cs → duplicate definition."""
    from src.unity_compose import drop_owned_scripts, subsystems_for, MicroComposer
    from src.unity_schema import UnityScript

    units = MicroComposer("DupGuard").plan(subsystems_for("2D", []))
    composed_names = {u.class_name for u in units}
    assert {"PhysicsBody", "GameContext"} <= composed_names   # both are composed

    scripts = [
        UnityScript(name="PhysicsBody", type="custom", description="x"),   # collides by name
        UnityScript(name="GameContext", type="custom", description="x"),   # collides by name
        UnityScript(name="ScoreTracker", type="custom", description="x"),  # unique → kept
    ]
    kept = {s.name for s in drop_owned_scripts(scripts, units)}
    assert kept == {"ScoreTracker"}


def test_2d_selects_2d_physics_and_camera():
    """2.5D coherence: a 2D game_type must compose a Rigidbody2D physics body and
    an orthographic follow_2d camera (the old setup paired a 3D Rigidbody + 3D
    follow with the 2d_platformer controller and could not be played)."""
    from src.unity_compose import subsystems_for
    subs2d = subsystems_for("2D", [])
    assert next(s.variant for s in subs2d if s.kind == "physics") == "rigidbody_2d"
    assert next(s.variant for s in subs2d if s.kind == "camera")  == "follow_2d"
    # 3D stays 3D.
    subs3d = subsystems_for("3D", [])
    assert next(s.variant for s in subs3d if s.kind == "physics") == "rigidbody_3d"


# ── Movement model coherence: CharacterController vs Rigidbody ────────────────
# A CharacterController-based controller (first/third_person, top_down,
# platformer_2_5d, flying, swimming, fighting) does its own swept-collision
# movement, so the always-on rigidbody_3d body on the SAME GameObject must be
# KINEMATIC — otherwise the physics solver also integrates gravity/forces and the
# two movers fight (jitter/tunneling/unexpected shove). Vehicle/aircraft
# controllers DRIVE a shared dynamic Rigidbody, so those stay non-kinematic.

def test_character_controller_physics_body_is_kinematic():
    subs = subsystems_for("3D", [])
    ctrl = next(s for s in subs if s.kind == "controller")
    phys = next(s for s in subs if s.kind == "physics")
    assert ctrl.variant == "third_person" and phys.variant == "rigidbody_3d"
    assert phys.options.get("is_kinematic") is True


def test_2_5d_character_controller_physics_body_is_kinematic():
    # 2.5D pairs a CharacterController (platformer_2_5d) with a real-3D
    # rigidbody_3d body — same conflict, same fix.
    subs = subsystems_for("2.5d", [])
    ctrl = next(s for s in subs if s.kind == "controller")
    phys = next(s for s in subs if s.kind == "physics")
    assert ctrl.variant == "platformer_2_5d" and phys.variant == "rigidbody_3d"
    assert phys.options.get("is_kinematic") is True


def test_vehicle_physics_body_stays_dynamic():
    # The vehicle_car controller AddForces/steers a shared Rigidbody, so the
    # physics body must NOT be made kinematic (that would freeze the car).
    subs = subsystems_for("3D", [], locomotion="drive")
    ctrl = next(s for s in subs if s.kind == "controller")
    phys = next(s for s in subs if s.kind == "physics")
    assert ctrl.variant == "vehicle_car" and phys.variant == "rigidbody_3d"
    assert not phys.options.get("is_kinematic")


def test_2d_physics_body_not_marked_kinematic():
    # 2D uses a (dynamic) Rigidbody2D driven by the 2d_platformer controller and
    # never a CharacterController — the kinematic flag doesn't apply.
    phys = next(s for s in subsystems_for("2D", []) if s.kind == "physics")
    assert phys.variant == "rigidbody_2d"
    assert not phys.options.get("is_kinematic")


def test_composed_3d_player_has_no_characcontroller_plus_dynamic_rigidbody(tmp_path):
    """Regression: the always-on rigidbody_3d body put a NON-kinematic Rigidbody
    on the same Player GameObject as the third_person controller's
    CharacterController — two movement/collision systems fighting each other. The
    controller owns movement, so the composed body sharing that object must be a
    KINEMATIC Rigidbody: still present (its Rigidbody is what fires OnTriggerEnter)
    but never integrated by the physics solver."""
    import re
    assets = Path(tmp_path) / "Assets"
    MicroComposer("PlayerMoveModel").emit(subsystems_for("3D", []), assets)
    phys = (assets / "Micros" / "physics" / "Scripts" / "PhysicsBody.cs").read_text(encoding="utf-8")
    ctrl = (assets / "Micros" / "controller" / "Scripts" / "PlayerController.cs").read_text(encoding="utf-8")
    # Both the controller and the physics body target the Player GameObject.
    assert "GetComponent<CharacterController>()" in ctrl      # controller's mover IS a CC
    assert "[RequireComponent(typeof(Rigidbody))]" in phys    # a Rigidbody is present (trigger detection)
    assert re.search(r"rb\.isKinematic\s*=\s*isKinematic;", phys)   # ...driven by the flag
    assert re.search(r"private bool isKinematic\s*=\s*true;", phys), \
        "the Player's Rigidbody must be kinematic so it can't fight the CharacterController"


# ── GameContext: bridging the two islands (micros ↔ prompt-authored scripts) ───

def _gamecontext_cs(tmp_path, project, has_gm):
    assets = Path(tmp_path) / "Assets"
    MicroComposer(project).emit(subsystems_for("3D", []), assets, has_game_manager=has_gm)
    return (assets / "Micros" / "context" / "Scripts"
            / "GameContext.cs").read_text(encoding="utf-8")


def _gamecontext_cs_with(tmp_path, project, features):
    assets = Path(tmp_path) / "Assets"
    MicroComposer(project).emit(subsystems_for("3D", features), assets, has_game_manager=False)
    return (assets / "Micros" / "context" / "Scripts"
            / "GameContext.cs").read_text(encoding="utf-8")


def test_gamecontext_emitted_with_universal_locator(tmp_path):
    ctx = _gamecontext_cs(tmp_path, "CtxProof", has_gm=False)
    assert "public class GameContext" in ctx
    assert "public static GameContext Instance" in ctx
    assert "public T Get<T>()" in ctx                 # universal access surface
    assert "FindFirstObjectByType<T>()" in ctx
    assert ctx.count("{") == ctx.count("}")
    assert "{{" not in ctx and "{%" not in ctx


def test_gamecontext_bridges_player_death_to_gamemanager(tmp_path):
    ctx = _gamecontext_cs(tmp_path, "CtxBridge", has_gm=True)
    assert "GameManager.Instance.LoseLife" in ctx      # micro death -> concept brain
    assert "Get<PlayerHealth>()" in ctx
    assert "OnDeath +=" in ctx
    assert ctx.count("{") == ctx.count("}")


def test_gamecontext_no_concept_bridge_when_no_gamemanager(tmp_path):
    ctx = _gamecontext_cs(tmp_path, "CtxNoGm", has_gm=False)
    # No GameManager in the project → no concept-side code referenced (still compiles).
    assert "GameManager.Instance" not in ctx


def test_gamecontext_bridges_inventory_heal_to_health(tmp_path):
    """Inventory.OnHealEffect (a Heal-tagged item's UseItem()) is wired straight
    to the composed player Health's Heal(float) — the one item-effect bridge
    with a ready receiver on another micro today (see _emit_context)."""
    assets = Path(tmp_path) / "Assets"
    MicroComposer("CtxInvHeal").emit(
        subsystems_for("3D", ["inventory"]), assets, has_game_manager=False)
    ctx = (assets / "Micros" / "context" / "Scripts"
          / "GameContext.cs").read_text(encoding="utf-8")
    assert "Get<PlayerInventory>()" in ctx or "__inventory = Get<" in ctx
    assert "__inventory.OnHealEffect += __health.Heal;" in ctx
    assert ctx.count("{") == ctx.count("}")


def test_gamecontext_bridges_inventory_restoreammo_to_controller(tmp_path):
    """Inventory.OnRestoreAmmoEffect (a RestoreAmmo-tagged item's UseItem())
    is wired to the composed player Controller's AddAmmo(int) — gated on the
    controller's own post-default_fix abilities (ComposedUnit.abilities),
    same reasoning as the locked-interactable Unlock bridge below."""
    assets = Path(tmp_path) / "Assets"
    subs = subsystems_for("3D", ["inventory"])
    controller = next(s for s in subs if s.kind == "controller")
    controller.options = {"abilities": ["shoot"]}
    MicroComposer("CtxInvAmmo").emit(subs, assets, has_game_manager=False)
    ctx = (assets / "Micros" / "context" / "Scripts"
          / "GameContext.cs").read_text(encoding="utf-8")
    assert "__inventory.OnRestoreAmmoEffect += __controller.AddAmmo;" in ctx
    assert ctx.count("{") == ctx.count("}")


def test_gamecontext_no_restoreammo_bridge_without_shoot_ability(tmp_path):
    """The default 3D controller (jump/sprint, no shoot) declares no AddAmmo —
    bridging to it would be a CS1061, so _emit_context must gate on abilities,
    not just kind+inventory presence."""
    ctx = _gamecontext_cs_with(tmp_path, "CtxInvAmmoNoShoot", features=["inventory"])
    assert "OnRestoreAmmoEffect" not in ctx


def test_gamecontext_no_inventory_bridge_without_inventory_feature(tmp_path):
    ctx = _gamecontext_cs(tmp_path, "CtxNoInv", has_gm=False)
    assert "OnHealEffect" not in ctx


def test_gamecontext_no_inventory_bridge_for_equipment_variant(tmp_path):
    """The equipment variant renders no UseItem()/OnHealEffect at all (see
    Inventory.cs.j2) — bridging it would reference a member that doesn't
    compile, so _emit_context excludes it by variant, not just by kind."""
    assets = Path(tmp_path) / "Assets"
    subs = subsystems_for("3D", ["inventory"])
    inv = next(s for s in subs if s.kind == "inventory")
    inv.variant = "equipment"
    MicroComposer("CtxInvEquip").emit(subs, assets, has_game_manager=False)
    ctx = (assets / "Micros" / "context" / "Scripts"
          / "GameContext.cs").read_text(encoding="utf-8")
    assert "OnHealEffect" not in ctx


def test_gamecontext_bridges_inventory_questflag_to_objectives(tmp_path):
    """Inventory.OnQuestFlagEffect is wired straight to the composed
    Objectives tracker's SetFlag(string) — Objectives is a per-project
    singleton (one tracker for the whole mission), so this uses the plain
    Get<T>() pattern, same shape as the Heal bridge."""
    mission = MissionSpec(id="m1", title="Vault Raid", objectives=[
        MissionObjective(id="crack_vault", description="Crack the vault door",
                         type="interact", count=1),
    ])
    assets = Path(tmp_path) / "Assets"
    MicroComposer("CtxInvFlag").emit(
        subsystems_for("3D", ["inventory"], missions=[mission]), assets, has_game_manager=False)
    ctx = (assets / "Micros" / "context" / "Scripts"
          / "GameContext.cs").read_text(encoding="utf-8")
    assert "__inventory.OnQuestFlagEffect += __objectives.SetFlag;" in ctx
    assert ctx.count("{") == ctx.count("}")


def test_gamecontext_no_questflag_bridge_without_objectives(tmp_path):
    """Inventory present, no mission objectives — no Objectives tracker
    subsystem exists to bind SetFlag to."""
    ctx = _gamecontext_cs_with(tmp_path, "CtxNoObj", features=["inventory"])
    assert "OnQuestFlagEffect" not in ctx


def test_gamecontext_bridges_inventory_unlock_to_locked_interactable(tmp_path):
    """Inventory.OnUnlockEffect is wired to the LOCKED Interactable's static
    UnlockAny broadcast, not Get<T>() — a scene can hold more than one
    locked instance, which a single FindFirstObjectByType could never reach
    correctly (see unity_compose.py's _emit_context and Interactable.cs.j2's
    _lockedInstances)."""
    assets = Path(tmp_path) / "Assets"
    subs = subsystems_for("3D", ["inventory"])
    interactable = next(s for s in subs if s.kind == "interactable")
    interactable.options = {"locked": True, "lock_id": "vault_key"}
    units = MicroComposer("CtxInvUnlock").emit(subs, assets, has_game_manager=False)
    interactable_unit = next(u for u in units if u.kind == "interactable")
    ctx = (assets / "Micros" / "context" / "Scripts"
          / "GameContext.cs").read_text(encoding="utf-8")
    assert (f"__inventory.OnUnlockEffect += {interactable_unit.class_name}.UnlockAny;"
            in ctx)
    assert ctx.count("{") == ctx.count("}")


def test_gamecontext_no_unlock_bridge_for_unlocked_interactable(tmp_path):
    """Both inventory AND interactable are present here — the golden
    archetype's default Interactable (locked=False, the pickup variant)
    still renders no UnlockAny at all, so bridging to it would reference a
    static method that doesn't compile. _emit_context must gate on the
    unit's own `locked` readback (plan()), not just kind presence."""
    ctx = _gamecontext_cs_with(tmp_path, "CtxInvNoLock", features=["inventory"])
    assert "OnUnlockEffect" not in ctx


def test_gamewiring_uses_unqualified_findfirstobjectbytype(tmp_path):
    """Hardening: GameWiring imports both System and UnityEngine, so a bare
    `Object.` prefix would be CS0104. The inherited unqualified call is safe."""
    MicroComposer("GwObj").emit(subsystems_for("3D", []), Path(tmp_path) / "Assets")
    gw = (Path(tmp_path) / "Assets" / "Micros" / "wiring" / "Scripts"
          / "GameWiring.cs").read_text(encoding="utf-8")
    assert "Object.FindFirstObjectByType" not in gw
    assert "FindFirstObjectByType<" in gw


def test_plan_includes_gamecontext_unit_with_unique_fileids():
    units = MicroComposer("CtxPlan").plan(subsystems_for("3D", []))
    ctx = [u for u in units if u.kind == "context"]
    assert len(ctx) == 1 and ctx[0].class_name == "GameContext"
    fids = [u.file_id for u in units]
    assert len(fids) == len(set(fids)), "scene fileIDs must stay unique with context unit"


def test_gamecontext_attached_in_scene():
    units = MicroComposer("CtxScene").plan(subsystems_for("3D", []))
    schema = UnitySchema(project_name="CtxScene", game_type="3D", platform="PC",
                         render_pipeline="builtin", scenes=["Main"])
    scene = UnityProjectGenerator().generate(
        schema, {}, composed_units=units)["CtxScene/Assets/Scenes/Main.unity"]
    ctx = next(u for u in units if u.kind == "context")
    assert ctx.guid in scene   # GameContext coexists in the scene with everything else


# ── Phase 1: Multimodal-IR field wiring ─────────────────────────────────────
# schema.audio_specs / schema.vfx / schema.animations / CharacterSpec.animation_set /
# CharacterSpec.dialog_graph used to be orphaned — Step 3 (LLM) could fill them but
# Steps 4-6 never read them. These tests prove real authored content (not the
# micro's own defaults) actually reaches the rendered output.

from src.unity_schema import (AudioSpec, VfxSpec, AnimationSpec, CharacterSpec,
                               DialogGraph, DialogNode, DialogChoice, EntitySpec)


def test_audio_specs_flow_into_composed_cues():
    specs = [AudioSpec(id="engine_hum", type="sfx", description="deep engine hum",
                       generation_method="sfxr", loop=True)]
    audio = next(s for s in subsystems_for("3D", [], audio_specs=specs) if s.kind == "audio")
    cues = audio.options["cues"]
    assert len(cues) == 1
    assert cues[0]["id"] == "engine_hum"
    assert cues[0]["kind"] == "synth"          # sfxr -> synth
    assert cues[0]["descriptor"] == "deep engine hum"
    assert cues[0]["loop"] is True


def test_audio_specs_alone_activate_the_audio_subsystem():
    # No "audio" feature flag — audio_specs content alone must be enough to compose it.
    specs = [AudioSpec(id="cue1", type="music", generation_method="stable_audio")]
    kinds = {s.kind for s in subsystems_for("3D", [], audio_specs=specs)}
    assert "audio" in kinds


def test_vfx_single_spec_flows_into_particle_system_options():
    specs = [VfxSpec(id="v1", name="MuzzleFlash", type="particle",
                     parameters={"duration": 0.5, "loop": False})]
    vfx = next(s for s in subsystems_for("3D", [], vfx_specs=specs) if s.kind == "vfx")
    assert vfx.variant == "particle_system"
    assert vfx.options["effect_id"] == "MuzzleFlash"
    assert vfx.options["duration"] == 0.5


def test_vfx_multiple_specs_flow_into_vfx_sequence_cues():
    specs = [VfxSpec(id="v1", name="Spark", type="particle"),
             VfxSpec(id="v2", name="Smoke", type="particle")]
    vfx = next(s for s in subsystems_for("3D", [], vfx_specs=specs) if s.kind == "vfx")
    assert vfx.variant == "vfx_sequence"
    ids = [c["effect_id"] for c in vfx.options["cues"]]
    assert ids == ["Spark", "Smoke"]


def test_shader_feature_composes_default_controller():
    subs = subsystems_for("3D", ["shader"])
    s = [x for x in subs if x.kind == "shader"]
    assert len(s) == 1 and s[0].variant == "color_cycle" and s[0].name == "ShaderController"


def test_shader_specs_alone_activate_the_shader_subsystem():
    # No "shader" feature flag — authored schema.shaders content alone is enough.
    from src.unity_schema import ShaderSpec
    specs = [ShaderSpec(id="sh1", name="LavaFlow")]
    kinds = {s.kind for s in subsystems_for("3D", [], shaders=specs)}
    assert "shader" in kinds


def test_shader_spec_flows_into_options():
    from src.unity_schema import ShaderSpec
    specs = [ShaderSpec(id="sh1", name="LavaFlow",
                        parameters={"variant": "uv_scroll", "speed_x": 1.5})]
    shader = next(s for s in subsystems_for("3D", [], shaders=specs) if s.kind == "shader")
    assert shader.variant == "uv_scroll"
    assert shader.name == "LavaFlowController"
    assert shader.options["speed_x"] == 1.5


def test_shader_multiple_specs_each_get_their_own_subsystem():
    # Unlike vfx, shader controllers each drive an independent object's
    # material — no "sequence" merge, one Subsystem per authored spec.
    from src.unity_schema import ShaderSpec
    specs = [ShaderSpec(id="sh1", name="LavaFlow"), ShaderSpec(id="sh2", name="ForceField")]
    shaders = [s for s in subsystems_for("3D", [], shaders=specs) if s.kind == "shader"]
    assert [s.name for s in shaders] == ["LavaFlowController", "ForceFieldController"]


def test_animator_clips_from_animations_and_character_animation_set():
    animations = [AnimationSpec(id="a1", name="Run", clip="Run_Clip")]
    characters = [CharacterSpec(id="c1", name="Hero", animation_set=["Idle"])]
    animator = next(s for s in subsystems_for("3D", [], animations=animations,
                                              characters=characters) if s.kind == "animator")
    assert animator.options["clips"]["Run"] == "Run_Clip"
    assert animator.options["clips"]["Idle"] == "Idle"


def test_animator_core_signature_unaffected_when_no_ir_data():
    # Byte-identical to today when the new fields are absent — additive-only change.
    subs = subsystems_for("3D", [])
    animator = next(s for s in subs if s.kind == "animator")
    assert animator.options == {}


def test_character_dialog_graph_becomes_its_own_dialogue_subsystem():
    graph = DialogGraph(start_node="n1", nodes=[
        DialogNode(id="n1", speaker="Guard", text="Halt! Who goes there?",
                  choices=[DialogChoice(text="A friend", next_node="n2")]),
    ])
    characters = [CharacterSpec(id="c1", name="Guard", dialog_graph=graph)]
    subs = subsystems_for("3D", [], characters=characters)
    dlg = next(s for s in subs if s.kind == "dialogue")
    assert dlg.name == "GuardDialogue"
    assert dlg.options["start_node"] == "n1"
    node = dlg.options["nodes"][0]
    assert node["text"] == "Halt! Who goes there?"
    assert node["choices"][0] == {"text": "A friend", "next": "n2"}
    # No redundant generic fallback alongside the real per-character one.
    assert not any(s.kind == "dialogue" and s.name == "DialogueSystem" for s in subs)


def test_generic_dialogue_fallback_still_fires_without_character_dialogue():
    subs = subsystems_for("3D", ["dialogue"])
    assert any(s.kind == "dialogue" and s.name == "DialogueSystem" for s in subs)


def test_character_dialogue_renders_authored_text_end_to_end(tmp_path):
    graph = DialogGraph(start_node="n1", nodes=[
        DialogNode(id="n1", speaker="Guard", text="Halt! Who goes there?",
                  choices=[DialogChoice(text="A friend", next_node="")]),
    ])
    characters = [CharacterSpec(id="c1", name="Guard", dialog_graph=graph)]
    subs = [s for s in subsystems_for("3D", [], characters=characters) if s.kind == "dialogue"]
    assets = Path(tmp_path) / "Assets"
    units = MicroComposer("DialogueProof").compose(subs, assets)
    u = units[0]
    cs = (assets / "Micros" / "dialogue" / "Scripts" / f"{u.class_name}.cs").read_text(encoding="utf-8")
    assert "Halt! Who goes there?" in cs


# ── Track 2: voiced-dialogue routing to NpcDialogPipeline ───────────────────
# DialogNode.audio_ref ("AudioSpec id — empty = no voiced line") decides whether a
# character's dialog_graph routes to plain-text DialoguePipeline or voiced
# NpcDialogPipeline (real Kokoro TTS clips).

def test_character_with_audio_ref_routes_to_npc_dialog():
    graph = DialogGraph(start_node="n1", nodes=[
        DialogNode(id="n1", speaker="Vendor", text="Welcome, traveler.",
                  emotion="happy", audio_ref="vendor_greeting"),
    ])
    characters = [CharacterSpec(id="c1", name="Vendor", dialog_graph=graph)]
    subs = subsystems_for("3D", [], characters=characters)
    kinds = [s.kind for s in subs if s.name.startswith("Vendor")]
    assert kinds == ["npc_dialog"]
    npc = next(s for s in subs if s.kind == "npc_dialog" and s.name == "VendorNpcDialog")
    assert npc.options["speaker_name"] == "Vendor"
    lines = npc.options["lines"]
    assert lines[0]["line_id"] == "n1"
    assert lines[0]["text"] == "Welcome, traveler."
    assert lines[0]["emotion"] == "happy"
    # No "skip_audio" meta — real authored voiced lines should render TTS clips,
    # not stay silent like the generic has_npcs fallback.
    assert "meta" not in npc.options or not npc.options["meta"].get("skip_audio")


def test_authored_voiced_character_does_not_get_generic_dialog_fallback():
    graph = DialogGraph(start_node="n1", nodes=[
        DialogNode(id="n1", speaker="Guard", text="Welcome to the checkpoint.",
                   audio_ref="guard_greeting"),
    ])
    characters = [CharacterSpec(id="g1", name="Guard", dialog_graph=graph)]
    entities = [EntitySpec(name="Guard", kind="npc")]
    subs = [s for s in subsystems_for("3D", [], entities=entities,
                                      characters=characters)
            if s.kind == "npc_dialog"]
    assert len(subs) == 1
    assert subs[0].target == "npc"
    assert subs[0].options["npc_entity"] == "Guard"


def test_npc_dialog_runtime_binding_targets_spawned_entity(tmp_path):
    graph = DialogGraph(start_node="n1", nodes=[
        DialogNode(id="n1", speaker="Guard", text="Welcome to the checkpoint.",
                   audio_ref="guard_greeting"),
    ])
    schema = UnitySchema(
        project_name="DialogBinding", game_type="3D", platform="PC",
        render_pipeline="builtin", scenes=["Main"],
        entities=[EntitySpec(name="Guard", kind="npc")],
        characters=[CharacterSpec(id="g1", name="Guard", dialog_graph=graph)],
    )
    subs = subsystems_for(schema.game_type, [], entities=schema.entities,
                          characters=schema.characters)
    assets = Path(tmp_path) / "Assets"
    units = MicroComposer(
        "DialogBinding", project_config={"audio": {"generate_clips": False}}
    ).emit(subs, assets)

    loader = assets / "Micros" / "context" / "Scripts" / "NpcDialogLoader.cs"
    assert loader.exists()
    loader_text = loader.read_text(encoding="utf-8")
    assert "DialogRegistry" in loader_text
    assert "Guard" in loader_text
    assert "CapsuleCollider" in loader_text
    state_machine = assets / "Micros" / "npc_dialog" / "Data" / "GuardNpcDialogStateMachine.json"
    assert state_machine.exists()
    assert '"dialog_role": "npc"' in state_machine.read_text(encoding="utf-8")

    generated = UnityProjectGenerator().generate(schema, {}, composed_units=units)
    spawner = generated["DialogBinding/Assets/Scripts/EntitySpawner.cs"]
    scene = generated["DialogBinding/Assets/Scenes/Main.unity"]
    assert "NpcDialogLoader.AttachIfBound" in spawner
    npc_unit = next(u for u in units if u.kind == "npc_dialog")
    assert npc_unit.guid not in scene


def test_character_without_audio_ref_still_routes_to_dialogue():
    graph = DialogGraph(start_node="n1", nodes=[
        DialogNode(id="n1", speaker="Guard", text="State your business.",
                  choices=[DialogChoice(text="Trading", next_node="")]),
    ])
    characters = [CharacterSpec(id="c1", name="Guard", dialog_graph=graph)]
    subs = subsystems_for("3D", [], characters=characters)
    kinds = [s.kind for s in subs if s.name.startswith("Guard")]
    assert kinds == ["dialogue"]


def test_voiced_character_dialogue_renders_authored_text_end_to_end(tmp_path):
    graph = DialogGraph(start_node="n1", nodes=[
        DialogNode(id="n1", speaker="Vendor", text="Fresh potions, half price today!",
                  emotion="happy", audio_ref="vendor_greeting"),
    ])
    characters = [CharacterSpec(id="c1", name="Vendor", dialog_graph=graph)]
    subs = [s for s in subsystems_for("3D", [], characters=characters) if s.kind == "npc_dialog"]
    assets = Path(tmp_path) / "Assets"
    MicroComposer("NpcDialogProof").compose(subs, assets)
    candidates = list((assets / "Micros" / "npc_dialog").rglob("*Data.json"))
    assert candidates, "no DialogData.json written for the npc_dialog unit"
    assert "Fresh potions, half price today!" in candidates[0].read_text(encoding="utf-8")


# ── Track 4: missions -> objective-tracker micro ─────────────────────────────

from src.unity_schema import MissionSpec, MissionObjective


def test_mission_with_objectives_gets_a_tracker_subsystem():
    mission = MissionSpec(id="m1", title="Vault Raid", objectives=[
        MissionObjective(id="crack_vault", description="Crack the vault door",
                         type="interact", count=1),
        MissionObjective(id="grab_cash", description="Grab bags of cash",
                         type="collect", count=5),
    ], rewards={"cash": 50000})
    subs = subsystems_for("3D", [], missions=[mission])
    obj = next(s for s in subs if s.kind == "objectives")
    assert obj.name == "VaultRaidObjectives"
    ids = [o["id"] for o in obj.options["objectives"]]
    assert ids == ["crack_vault", "grab_cash"]
    assert obj.options["rewards"] == {"cash": 50000}


def test_mission_without_objectives_produces_no_tracker():
    mission = MissionSpec(id="m1", title="Empty Mission")
    subs = subsystems_for("3D", [], missions=[mission])
    assert not any(s.kind == "objectives" for s in subs)


def test_mission_objectives_render_authored_content_end_to_end(tmp_path):
    mission = MissionSpec(id="m1", title="Vault Raid", objectives=[
        MissionObjective(id="crack_vault", description="Crack the vault door",
                         type="interact", count=1),
    ])
    subs = [s for s in subsystems_for("3D", [], missions=[mission]) if s.kind == "objectives"]
    assets = Path(tmp_path) / "Assets"
    units = MicroComposer("MissionProof").compose(subs, assets)
    u = units[0]
    cs = (assets / "Micros" / "objectives" / "Scripts" / f"{u.class_name}.cs").read_text(encoding="utf-8")
    assert "crack_vault" in cs
    assert "Crack the vault door" in cs
    assert "OnMissionComplete" in cs and "ReportProgress" in cs


# ── Track 6: behavior_trees -> minimal BT interpreter ────────────────────────

from src.unity_schema import AIBehaviorTree, BehaviorTree, BTNode


def _guard_bt_entry():
    return AIBehaviorTree(id="bt1", name="GuardBrain", tree=BehaviorTree(
        root="root", nodes=[
            BTNode(id="root", type="selector", children=["chase", "idle"]),
            BTNode(id="chase", type="action", action="ChasePlayer"),
            BTNode(id="idle", type="action", action="Idle"),
        ]))


def test_behavior_tree_entry_gets_a_subsystem():
    subs = subsystems_for("3D", [], behavior_trees=[_guard_bt_entry()])
    bt = next(s for s in subs if s.kind == "behavior_tree")
    assert bt.name == "GuardBrainBT"
    assert bt.options["root"] == "root"
    ids = [n["id"] for n in bt.options["nodes"]]
    assert ids == ["root", "chase", "idle"]


def test_behavior_tree_entry_without_nodes_produces_no_subsystem():
    empty = AIBehaviorTree(id="bt1", name="Empty", tree=BehaviorTree(root="", nodes=[]))
    subs = subsystems_for("3D", [], behavior_trees=[empty])
    assert not any(s.kind == "behavior_tree" for s in subs)


def test_behavior_tree_renders_authored_structure_end_to_end(tmp_path):
    subs = [s for s in subsystems_for("3D", [], behavior_trees=[_guard_bt_entry()])
            if s.kind == "behavior_tree"]
    assets = Path(tmp_path) / "Assets"
    units = MicroComposer("BTProof").compose(subs, assets)
    u = units[0]
    cs = (assets / "Micros" / "behavior_tree" / "Scripts" / f"{u.class_name}.cs").read_text(encoding="utf-8")
    assert 'id = "root", type = "selector"' in cs
    assert "ChasePlayer" in cs and "Idle" in cs
    assert "RegisterCondition" in cs and "BTStatus Evaluate" in cs


# ── Track 8: wiring map -> additive custom seams (not a SEAM rewrite) ────────

from src.unity_schema import WiringMap, WiringDependency


def test_novel_wiring_dependency_produces_a_custom_seam_comment(tmp_path):
    subs = subsystems_for("3D", ["ai", "audio"])
    wm = WiringMap(dependencies=[
        WiringDependency(source="audio", target="hud", type="references"),
    ])
    assets = Path(tmp_path) / "Assets"
    MicroComposer("WiringNovel").compose(subs, assets, wiring=wm)
    gw = (assets / "Micros" / "wiring" / "Scripts" / "GameWiring.cs").read_text(encoding="utf-8")
    assert "wiring: audio --references--> hud" in gw


def test_wiring_dependency_matching_named_seam_is_not_duplicated(tmp_path):
    subs = subsystems_for("3D", ["ai", "audio"])
    wm = WiringMap(dependencies=[
        WiringDependency(source="health", target="hud", type="references"),
    ])
    assets = Path(tmp_path) / "Assets"
    MicroComposer("WiringNamed").compose(subs, assets, wiring=wm)
    gw = (assets / "Micros" / "wiring" / "Scripts" / "GameWiring.cs").read_text(encoding="utf-8")
    assert "wiring: health --references--> hud" not in gw
    assert gw.count("SEAM 1: Health") == 1   # the named seam, unaffected — no duplicate


def test_wiring_dependency_with_missing_kind_is_silently_skipped(tmp_path):
    subs = subsystems_for("3D", [])
    wm = WiringMap(dependencies=[
        WiringDependency(source="audio", target="nonexistent_kind", type="uses"),
    ])
    assets = Path(tmp_path) / "Assets"
    MicroComposer("WiringMissing").compose(subs, assets, wiring=wm)
    gw = (assets / "Micros" / "wiring" / "Scripts" / "GameWiring.cs").read_text(encoding="utf-8")
    assert "nonexistent_kind" not in gw


def test_no_wiring_map_leaves_gamewiring_unchanged(tmp_path):
    """wiring=None (the default) must behave byte-identically to before this track."""
    subs = subsystems_for("3D", ["ai", "audio"])
    a1 = Path(tmp_path) / "a" / "Assets"
    a2 = Path(tmp_path) / "b" / "Assets"
    MicroComposer("WiringNone").compose(subs, a1)
    MicroComposer("WiringNone").compose(subs, a2, wiring=None)
    gw1 = (a1 / "Micros" / "wiring" / "Scripts" / "GameWiring.cs").read_text(encoding="utf-8")
    gw2 = (a2 / "Micros" / "wiring" / "Scripts" / "GameWiring.cs").read_text(encoding="utf-8")
    assert gw1 == gw2
    assert "schema.wiring" not in gw1   # no custom-seam markers when wiring is absent
