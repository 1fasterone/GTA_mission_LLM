"""
Composition layer — the assembler for the combinatorial game-design tool.

A micro-generator produces one self-contained subsystem (controller, hud, …). The
composer assembles a SET of micros into a single Unity project and records how to
wire each one into a scene. Crucially it reuses each micro's *deterministic*
`write_files` (no LLM), so composition is a pure function of the chosen subsystems.

P1 scope: generate the chosen micros into `<Assets>/Micros/<kind>/`, emit
deterministic `.cs.meta` GUIDs (so a scene can reference the component), and return
a manifest + a Unity `MonoBehaviour` attach-block per unit. Splicing the blocks into
the real scene template is P2.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from .micro import REGISTRY
from .micro_pipeline import to_pascal
from .unity_generator import _make_guid, _meta_file, _default_meta


# Where each subsystem attaches in the scene (used by P2's scene wiring).
Target = str   # "player" | "camera" | "ui" | "systems" | "world"


class Subsystem(BaseModel):
    kind:    str                       # a key in micro.REGISTRY
    variant: str = ""                  # "" → the micro's default_variant
    target:  Target = "systems"
    name:    str = ""                  # optional class-name override
    options: dict = Field(default_factory=dict)   # extra schema fields (e.g. input_backend)


class ComposedUnit(BaseModel):
    kind:       str
    variant:    str
    class_name: str
    guid:       str                    # GUID of the main component script
    target:     Target
    file_id:    int = 0                # scene MonoBehaviour fileID (per-target wiring)
    scripts:    list[str] = Field(default_factory=list)   # asset-relative .cs paths


# Base scene fileID for composed MonoBehaviours (well above the template's range).
_COMPOSED_FILE_ID_BASE = 900000010

# Built-in UnityScript types the micro library now OWNS. In --compose mode these
# are dropped from the generated project so the composer is the single source of
# truth (no duplicate PlayerController/CameraFollow/etc.). The unique glue scripts
# (game_manager, mesh_builder, material_palette, procedural_mesh, level_generator)
# have no micro equivalent and are kept.
COVERED_SCRIPT_TYPES = {
    "player", "camera", "enemy", "audio_manager",
    "ui_manager", "collectible", "dialogue_manager", "health",
}


class MicroComposer:
    """Assemble micros into a project's Assets and report how to wire them."""

    def __init__(self, project_name: str, template_dir: str = "templates/unity"):
        self.project_name = project_name
        self._cfg = {
            # write_files / default_fix never touch the network; the client is
            # constructed but unused, exactly as in the micro unit tests.
            "lm_studio": {"base_url": "http://localhost:1234/v1", "model": "compose"},
            "unity_pipeline": {
                "unity_output_dir":  "output/unity",
                "unity_template_dir": template_dir,
            },
        }

    def _resolve(self, sub: Subsystem):
        """Build the micro pipeline + fixed schema + class name + GUID for a
        subsystem, without writing anything. Pure — drives both plan() and emit()."""
        if sub.kind not in REGISTRY:
            raise ValueError(f"unknown subsystem kind: {sub.kind!r}")
        pipeline_cls = REGISTRY[sub.kind]
        variant = sub.variant or pipeline_cls.default_variant
        pipeline = pipeline_cls(self._cfg)
        schema = pipeline_cls.schema_cls(
            name=sub.name or sub.kind.title(),
            variant=variant,
            **sub.options,
        )
        schema = pipeline.default_fix(schema, variant)
        # attach_class: the scene-attachable MonoBehaviour (≠ schema.name when a
        # micro's primary file is a ScriptableObject, e.g. the audio bank).
        class_name = pipeline.attach_class(schema)
        guid = _make_guid(f"{self.project_name}/Micros/{sub.kind}/{class_name}")
        return pipeline, schema, variant, class_name, guid

    def plan(self, subsystems: list[Subsystem]) -> list[ComposedUnit]:
        """Compute the manifest (class names + GUIDs + targets + scene fileIDs)
        WITHOUT writing files, INCLUDING the synthetic GameWiring unit. Feeds the
        scene wiring before the project dir exists; GUIDs/fileIDs match emit()."""
        units: list[ComposedUnit] = []
        for i, sub in enumerate(subsystems):
            _, _, variant, class_name, guid = self._resolve(sub)
            units.append(ComposedUnit(
                kind=sub.kind, variant=variant, class_name=class_name, guid=guid,
                target=sub.target, file_id=_COMPOSED_FILE_ID_BASE + 10 * i))
        # Synthetic GameWiring glue component (cross-micro event wiring), on _Systems.
        units.append(ComposedUnit(
            kind="wiring", variant="", class_name="GameWiring",
            guid=_make_guid(f"{self.project_name}/Micros/wiring/GameWiring"),
            target="systems", file_id=_COMPOSED_FILE_ID_BASE + 10 * len(subsystems)))
        return units

    def _folder_meta(self, path: Path) -> None:
        meta = path.with_name(path.name + ".meta")
        meta.write_text(_default_meta(_make_guid(f"{self.project_name}/{path.name}/dir"),
                                      folder=True), encoding="utf-8")

    def _wiring_template_dir(self) -> Path:
        return Path(self._cfg["unity_pipeline"]["unity_template_dir"]) / "compose"

    def compose(self, subsystems: list[Subsystem], assets_root: Path) -> list[ComposedUnit]:
        assets_root = Path(assets_root)
        micros_root = assets_root / "Micros"
        micros_root.mkdir(parents=True, exist_ok=True)
        self._folder_meta(micros_root)

        # Single source of truth for class names / GUIDs / fileIDs (incl. wiring).
        units = self.plan(subsystems)
        by_kind = {u.kind: u for u in units}

        for sub in subsystems:
            pipeline, schema, variant, class_name, _ = self._resolve(sub)
            kind_dir = micros_root / sub.kind
            pipeline.write_files(schema, variant, kind_dir)

            # Deterministic .cs.meta for every generated script so the scene can
            # reference the component by GUID. Non-.cs assets: Unity imports them.
            self._folder_meta(kind_dir)
            scripts_dir = kind_dir / "Scripts"
            rel_scripts: list[str] = []
            if scripts_dir.is_dir():
                self._folder_meta(scripts_dir)
                for cs in sorted(scripts_dir.glob("*.cs")):
                    guid = _make_guid(f"{self.project_name}/Micros/{sub.kind}/{cs.stem}")
                    (cs.parent / (cs.name + ".meta")).write_text(
                        _meta_file(guid), encoding="utf-8")
                    rel_scripts.append(str(cs.relative_to(assets_root)).replace("\\", "/"))
            by_kind[sub.kind].scripts = rel_scripts

        # GameWiring.cs — generated glue referencing the composed (non-wiring) classes.
        self._emit_wiring(units, micros_root)
        return units

    def _emit_wiring(self, units: list[ComposedUnit], micros_root: Path) -> None:
        from jinja2 import Environment, FileSystemLoader, StrictUndefined
        wiring = next(u for u in units if u.kind == "wiring")
        real_units = [u for u in units if u.kind != "wiring"]
        wdir = micros_root / "wiring"
        sdir = wdir / "Scripts"
        sdir.mkdir(parents=True, exist_ok=True)
        env = Environment(loader=FileSystemLoader(str(self._wiring_template_dir())),
                          undefined=StrictUndefined, keep_trailing_newline=True)
        hud = next((u for u in real_units if u.kind == "hud"), None)
        health = next((u for u in real_units if u.kind == "health"), None)
        # HUD.SetHealth(float,float) only exists on the health_bar variant.
        wire_health_hud = bool(health and hud and hud.variant == "health_bar")
        content = env.get_template("GameWiring.cs.j2").render(
            units=real_units,
            has_menu=any(u.kind == "menu" for u in real_units),
            wire_health_hud=wire_health_hud)
        (sdir / "GameWiring.cs").write_text(content, encoding="utf-8")
        (sdir / "GameWiring.cs.meta").write_text(_meta_file(wiring.guid), encoding="utf-8")
        self._folder_meta(wdir)
        self._folder_meta(sdir)

    # Built-in modules / packages each composed micro kind needs in manifest.json.
    # Merged into the project's deps so a composed project actually compiles.
    # UnityEngine.UI (Button/Slider/Image) comes from the UGUI package
    # `com.unity.ugui`, which a freshly-written manifest must list explicitly
    # (an already-opened project gets it auto-added). `com.unity.modules.ui` is
    # only the UIModule (Canvas/RectTransform) — not enough on its own.
    _UGUI = "com.unity.ugui"
    _KIND_MODULES: dict[str, list[str]] = {
        "hud":          ["com.unity.modules.ui", _UGUI],
        "menu":         ["com.unity.modules.ui", "com.unity.modules.uielements", _UGUI],
        "ai":           ["com.unity.modules.ai"],
        "audio":        ["com.unity.modules.audio"],
        "controller":   ["com.unity.modules.physics"],
        "physics":      ["com.unity.modules.physics"],
        "spawner":      ["com.unity.modules.physics"],
        "interactable": ["com.unity.modules.physics"],
        "transition":   ["com.unity.modules.ui", _UGUI],
    }

    @classmethod
    def module_deps(cls, units: list[ComposedUnit]) -> dict[str, str]:
        """Built-in module/package deps the composed micros require — merge into the
        project's manifest so HUD/menu (UnityEngine.UI), AI (NavMesh), etc. compile."""
        deps: dict[str, str] = {}
        for u in units:
            for mod in cls._KIND_MODULES.get(u.kind, []):
                deps[mod] = "2.0.0" if mod == cls._UGUI else "1.0.0"
        return deps

    @staticmethod
    def manifest(units: list[ComposedUnit]) -> str:
        return json.dumps([u.model_dump() for u in units], indent=2)

    @staticmethod
    def attach_block(unit: ComposedUnit, file_id: int) -> str:
        """A valid Unity MonoBehaviour YAML block attaching this component, keyed to
        the script's real .meta GUID. (P2 splices composed units into scene.unity.j2
        directly; this stays a handy standalone artifact.)"""
        return (
            f"--- !u!114 &{file_id}\n"
            "MonoBehaviour:\n"
            "  m_ObjectHideFlags: 0\n"
            "  m_CorrespondingSourceObject: {fileID: 0}\n"
            "  m_PrefabInstance: {fileID: 0}\n"
            "  m_PrefabAsset: {fileID: 0}\n"
            "  m_GameObject: {fileID: 0}\n"
            "  m_Enabled: 1\n"
            "  m_EditorHideFlags: 0\n"
            f"  m_Script: {{fileID: 11500000, guid: {unit.guid}, type: 3}}\n"
            f"  m_Name: \n"
            "  m_EditorClassIdentifier: \n"
        )

    # Alias: emit() writes files (same as compose); plan() does not.
    emit = compose


# ── Deterministic mapper: game concept → subsystem set ────────────────────────

def subsystems_for(game_type: str, features: list[str] | None = None,
                   locomotion: str = "walk") -> list[Subsystem]:
    """Map a project's game_type + features + locomotion to a default set of
    subsystems to compose. Deterministic — no LLM."""
    feats = set(features or [])
    gt = (game_type or "3D").lower()

    # Controller variant by game type.
    ctrl = {
        "2d": "2d_platformer", "2.5d": "2d_platformer", "top_down": "top_down",
        "isometric": "top_down", "vr": "first_person",
    }.get(gt, "third_person")
    cam = {
        "2d": "follow", "2.5d": "follow", "top_down": "cinemachine_topdown",
        "isometric": "cinemachine_topdown",
    }.get(gt, "follow")

    # Locomotion overrides the walker default for 3D-ish game types — a racing
    # prompt gets the real WheelCollider car, not a humanoid (2D/VR keep their
    # game-type controller; a "2D racing game" is still a 2D controller).
    if gt in ("3d", "top_down", "isometric"):
        loco_ctrl = {"drive": "vehicle_car", "fly": "flying", "swim": "swimming"}
        loco_cam  = {"drive": "vehicle_chase"}
        if locomotion in loco_ctrl:
            ctrl = loco_ctrl[locomotion]
            cam  = loco_cam.get(locomotion, "follow")

    subs: list[Subsystem] = [
        Subsystem(kind="controller", variant=ctrl, target="player",
                  name="PlayerController", options={"input_backend": "legacy"}),
        Subsystem(kind="camera", variant=cam, target="camera", name="PlayerCamera"),
        Subsystem(kind="health", variant="simple", target="player", name="PlayerHealth"),
        Subsystem(kind="hud", variant="health_bar", target="ui", name="GameHud"),
        Subsystem(kind="menu", variant="pause_menu", target="ui", name="PauseMenu",
                  options={"backend": "uGUI"}),
        Subsystem(kind="saveload", variant="json_file", target="systems", name="SaveSystem"),
    ]
    # Feature-driven additions.
    if "ai" in feats or "navmesh" in feats or "enemies" in feats:
        # Driving games want a racing-line follower (no NavMesh bake needed),
        # not a patrol guard — it consumes the EntitySpawner's "Waypoints".
        # target="asset": the class is emitted for per-enemy attachment (via
        # entities[].behavior) but NOT attached to _Systems — a mover script on
        # _Systems would literally drive the holder object around the track.
        if locomotion == "drive":
            subs.append(Subsystem(kind="ai", variant="waypoint_follow", target="asset", name="EnemyAI"))
        else:
            subs.append(Subsystem(kind="ai", variant="navmesh_patrol", target="world", name="EnemyAI"))
        subs.append(Subsystem(kind="spawner", variant="wave_spawner", target="world", name="EnemySpawner"))
    if "audio" in feats:
        subs.append(Subsystem(kind="audio", variant="categorical_mixer", target="systems", name="AudioDirector"))
    if "inventory" in feats:
        subs.append(Subsystem(kind="inventory", variant="slot_based", target="systems", name="PlayerInventory"))
    return subs
