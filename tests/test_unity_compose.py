"""
P1 tests for the composition layer (src/unity_compose.py).

Offline + deterministic: composing a micro into a project's Assets must produce the
script, a stable .cs.meta GUID, a correct manifest, and a scene attach-block whose
GUID matches the meta — i.e. the wiring metadata is internally consistent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.unity_compose import MicroComposer, Subsystem, ComposedUnit


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
        assert s.target in {"player", "camera", "ui", "systems", "world"}


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


def test_subsystems_for_fps_genre_does_not_override_explicit_game_type():
    # An explicit dimension/perspective (2D/top_down/isometric/VR) wins over an
    # fps genre hint — only the "no opinion" 3D default defers to genre.
    ctrl_td = next(s for s in subsystems_for("top_down", [], genre="fps")
                   if s.kind == "controller")
    assert ctrl_td.variant == "top_down"
    ctrl_2d = next(s for s in subsystems_for("2D", [], genre="fps")
                   if s.kind == "controller")
    assert ctrl_2d.variant == "2d_platformer"


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


def test_npc_dialog_feature_composes_with_skip_audio():
    subs = subsystems_for("3D", ["npcs"])
    npc = [s for s in subs if s.kind == "npc_dialog"]
    assert len(npc) == 1
    assert npc[0].options.get("meta", {}).get("skip_audio") is True


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


def test_walk_compose_keeps_grounded_bridge(tmp_path):
    MicroComposer("WalkWire").emit(subsystems_for("3D", []), Path(tmp_path) / "Assets")
    gw = (Path(tmp_path) / "Assets" / "Micros" / "wiring" / "Scripts"
          / "GameWiring.cs").read_text(encoding="utf-8")
    assert "IsGrounded" in gw   # default third_person controller still bridges grounded


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


# ── GameContext: bridging the two islands (micros ↔ prompt-authored scripts) ───

def _gamecontext_cs(tmp_path, project, has_gm):
    assets = Path(tmp_path) / "Assets"
    MicroComposer(project).emit(subsystems_for("3D", []), assets, has_game_manager=has_gm)
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
