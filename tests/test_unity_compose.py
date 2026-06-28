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
    for u in units:                      # every composed component wired by its GUID
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
        if u.kind not in ("wiring",):
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


def test_locomotion_schema_normalises_synonyms():
    from src.unity_schema import UnitySchema
    s = UnitySchema(project_name="L", game_type="3D", platform="pc",
                    render_pipeline="URP", locomotion="racing")
    assert s.locomotion == "drive"
    s2 = UnitySchema(project_name="L", game_type="3D", platform="pc",
                     render_pipeline="URP", locomotion="nonsense")
    assert s2.locomotion == "walk"
