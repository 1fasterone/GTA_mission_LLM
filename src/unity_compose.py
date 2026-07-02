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

# Kinds that GameWiring / GameContext must NOT FindFirstObjectByType:
#   wiring/context — the synthetic glue components themselves.
#   input          — a ScriptableObject (IInputReader) assigned via the _inputSO
#                    SerializeField, never a scene MonoBehaviour. The generated SO
#                    class is `<name>InputReader` (not `<name>`), so finding it is
#                    CS0246 and would never resolve at runtime anyway.
_NON_SCENE_KINDS = {"wiring", "context", "input"}

# Built-in UnityScript types the micro library now OWNS. In --compose mode these
# are dropped from the generated project so the composer is the single source of
# truth (no duplicate PlayerController/CameraFollow/etc.). The unique glue scripts
# (game_manager, mesh_builder, material_palette, procedural_mesh, level_generator)
# have no micro equivalent and are kept.
COVERED_SCRIPT_TYPES = {
    "player", "camera", "enemy", "audio_manager",
    "ui_manager", "collectible", "dialogue_manager", "health",
    # newly owned by compose micros
    "physics", "input_system",
}


def drop_owned_scripts(scripts: list, composed_units: list) -> list:
    """Return LLM-authored scripts minus the ones compose already owns.

    Compose is the single source of truth for composed subsystems, so a script
    is dropped when it collides either:
      • by TYPE  — a built-in kind the micro library covers (COVERED_SCRIPT_TYPES), or
      • by NAME  — the LLM authored a custom script whose class name matches a
                   composed class (e.g. its own `PhysicsBody` or `GameContext`).
    The name check is what prevents CS0101/CS0111 duplicate-definition errors when
    the model invents a `type: custom` script that shadows a micro.
    """
    reserved_names = {u.class_name for u in composed_units}
    return [s for s in scripts
            if s.type not in COVERED_SCRIPT_TYPES and s.name not in reserved_names]


class MicroComposer:
    """Assemble micros into a project's Assets and report how to wire them."""

    def __init__(self, project_name: str, template_dir: str = "templates/unity",
                 project_config: dict | None = None):
        self.project_name = project_name
        # Merge real project config (audio backend, paths, etc.) over the minimal
        # dummy so write_files respects backend: stable3 / generate_clips / etc.
        # The LM Studio client is still constructed but never called (compose is
        # LLM-free), so the dummy base_url is harmless.
        base_cfg: dict = {
            "lm_studio": {"base_url": "http://localhost:1234/v1", "model": "compose"},
            "unity_pipeline": {
                "unity_output_dir":  "output/unity",
                "unity_template_dir": template_dir,
            },
        }
        if project_config:
            for k, v in project_config.items():
                if k not in base_cfg:
                    base_cfg[k] = v
                elif isinstance(v, dict) and isinstance(base_cfg.get(k), dict):
                    base_cfg[k] = {**base_cfg[k], **v}
        self._cfg = base_cfg

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
        # Synthetic GameContext locator — the shared bridge surface that lets the
        # composed micros AND the prompt-authored scripts find each other.
        units.append(ComposedUnit(
            kind="context", variant="", class_name="GameContext",
            guid=_make_guid(f"{self.project_name}/Micros/context/GameContext"),
            target="systems", file_id=_COMPOSED_FILE_ID_BASE + 10 * (len(subsystems) + 1)))
        return units

    def _folder_meta(self, path: Path) -> None:
        meta = path.with_name(path.name + ".meta")
        meta.write_text(_default_meta(_make_guid(f"{self.project_name}/{path.name}/dir"),
                                      folder=True), encoding="utf-8")

    def _wiring_template_dir(self) -> Path:
        return Path(self._cfg["unity_pipeline"]["unity_template_dir"]) / "compose"

    def compose(self, subsystems: list[Subsystem], assets_root: Path,
                has_game_manager: bool = False) -> list[ComposedUnit]:
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
        # GameContext.cs — the shared locator bridging micros ↔ prompt-authored scripts.
        self._emit_context(units, micros_root, has_game_manager)
        return units

    def _emit_wiring(self, units: list[ComposedUnit], micros_root: Path) -> None:
        from jinja2 import Environment, FileSystemLoader, StrictUndefined
        from .atoms.backends.unity import unity_atom_snippet

        wiring     = next(u for u in units if u.kind == "wiring")
        real_units = [u for u in units if u.kind not in _NON_SCENE_KINDS]

        wdir = micros_root / "wiring"
        sdir = wdir / "Scripts"
        sdir.mkdir(parents=True, exist_ok=True)
        env = Environment(loader=FileSystemLoader(str(self._wiring_template_dir())),
                          undefined=StrictUndefined, keep_trailing_newline=True)

        # ── Subsystem resolution ──────────────────────────────────────────────
        # When multiple units share a kind (e.g. two statemachine units), pick
        # the one whose target matches the hint.  This is the single source of
        # truth for which unit wires into each seam; it also prevents the old
        # by_kind dict overwrite that wired EnemyFSM seams to the player.
        def _pick(kind: str, target: str | None = None) -> "ComposedUnit | None":
            candidates = [u for u in real_units if u.kind == kind]
            if not candidates:
                return None
            if len(candidates) == 1 or target is None:
                return candidates[0]
            preferred = [u for u in candidates if u.target == target]
            return preferred[0] if preferred else candidates[0]

        health   = _pick("health",       "player")
        hud      = _pick("hud",          "ui")
        fsm      = _pick("statemachine", "player")   # player FSM only — enemy FSM excluded
        anim     = _pick("animator",     "player")
        audio    = _pick("audio",        "systems")
        ctrl     = _pick("controller",   "player")
        trigger  = _pick("trigger",      "world")
        saveload = _pick("saveload",     "systems")
        menu     = _pick("menu",         "ui")

        # Field-name helper: derive a unique C# variable name from class_name so
        # two units of the same kind (PlayerFSM/EnemyFSM) never collide.
        def _fv(u: "ComposedUnit") -> str:
            return f"_{u.class_name.lower()}"

        # InputReader SO is needed whenever controller, FSM, or menu is present.
        has_input_so = bool(ctrl or fsm or menu)

        # ── Build seam lists (atom-driven) ────────────────────────────────────
        awake_seams:      list[dict] = []
        update_seams:     list[dict] = []
        stored_delegates: list[dict] = []
        destroy_cleanups: list[str]  = []
        helper_methods:   list[str]  = []
        has_trigger_save              = False

        # SEAM 1 — Health → HUD (hp bar reflects damage/heal live)
        if health and hud and hud.variant == "health_bar":
            hv, uv = _fv(health), _fv(hud)
            awake_seams.append({
                "label":   "SEAM 1: Health → HUD",
                "snippet": unity_atom_snippet("WireHealthToHUD", {
                    "health_var": hv, "hud_var": uv,
                }),
            })
            destroy_cleanups.append(
                f"if ({hv} != null && {uv} != null)\n"
                f"    {hv}.OnHealthChanged -= {uv}.SetHealth;"
            )

        # SEAM 2 — Health → FSM (death drives Dead state)
        if health and fsm:
            hv, fv = _fv(health), _fv(fsm)
            awake_seams.append({
                "label":   "SEAM 2: Health → FSM (death drives Dead state)",
                "snippet": unity_atom_snippet("WireHealthToFSM", {
                    "health_var": hv, "statemachine_var": fv,
                }),
            })
            destroy_cleanups.append(
                f"if ({hv} != null && {fv} != null)\n"
                f"    {hv}.OnDeath -= {fv}.Kill;"
            )

        # SEAM 3 — Health → Audio (hurt / death / heal sounds)
        if health and audio:
            hv, av = _fv(health), _fv(audio)
            stored_delegates += [
                {"type": "Action<float>", "name": "_onDamaged"},
                {"type": "Action",        "name": "_onDeath"},
                {"type": "Action<float>", "name": "_onHealed"},
            ]
            awake_seams.append({
                "label":   "SEAM 3: Health → Audio",
                "snippet": unity_atom_snippet("WireHealthToAudio", {
                    "health_var": hv, "audio_var": av,
                    "cue_hurt":  "player_hurt",
                    "cue_death": "player_death",
                    "cue_heal":  "checkpoint_save_led",
                }),
            })
            destroy_cleanups.append(
                f"if ({hv} != null)\n"
                "{\n"
                f"    if (_onDamaged != null) {hv}.OnDamaged -= _onDamaged;\n"
                f"    if (_onDeath   != null) {hv}.OnDeath   -= _onDeath;\n"
                f"    if (_onHealed  != null) {hv}.OnHealed  -= _onHealed;\n"
                "}"
            )

        # SEAM 4 — FSM → Animator (state changes drive CrossFade)
        if fsm and anim:
            fv, anv = _fv(fsm), _fv(anim)
            awake_seams.append({
                "label":   "SEAM 4: FSM → Animator",
                "snippet": unity_atom_snippet("WireFSMToAnimator", {
                    "statemachine_var": fv, "animator_var": anv,
                }),
            })
            fsm_cls = fsm.class_name
            helper_methods.append(
                f"private void OnFSMStateChanged({fsm_cls}.State state)\n"
                f"    {{\n"
                f"        if ({anv} == null) return;\n"
                f"        switch (state)\n"
                f"        {{\n"
                f"            case {fsm_cls}.State.Idle:    {anv}.CrossFade(\"Idle\",  0.1f); break;\n"
                f"            case {fsm_cls}.State.Moving:  {anv}.CrossFade(\"Run\",   0.1f); break;\n"
                f"            case {fsm_cls}.State.Jumping: {anv}.CrossFade(\"Jump\",  0.1f); break;\n"
                f"            case {fsm_cls}.State.Dead:    {anv}.CrossFade(\"Death\", 0.1f); break;\n"
                f"        }}\n"
                f"    }}"
            )
            destroy_cleanups.append(
                f"if ({fv} != null && {anv} != null)\n"
                f"    {fv}.OnStateChanged -= OnFSMStateChanged;"
            )

        # SEAM 4b — InputReader → Controller
        if has_input_so and ctrl:
            awake_seams.append({
                "label":   "SEAM 4b: InputReader → Controller",
                "snippet": unity_atom_snippet("WireInputToController", {
                    "input_var": "_inputSO", "controller_var": _fv(ctrl),
                }),
            })
            # Controller unsubscribes itself in its own OnDisable.

        # SEAM 5 — InputReader → FSM (typed move + jump events)
        if has_input_so and fsm:
            fv = _fv(fsm)
            stored_delegates.append({"type": "Action", "name": "_onFSMJump"})
            awake_seams.append({
                "label":   "SEAM 5: InputReader → FSM",
                "snippet": unity_atom_snippet("WireInputToFSM", {
                    "input_var": "_inputSO", "statemachine_var": fv,
                }),
            })
            destroy_cleanups.append(
                f"if (_inputSO != null && {fv} != null)\n"
                "{\n"
                f"    _inputSO.OnMove -= {fv}.SetMove;\n"
                "    if (_onFSMJump != null) _inputSO.OnJump -= _onFSMJump;\n"
                "}"
            )

        # SEAM 6 — Grounded bridge (Update, not Awake)
        # Skip for swimming/flying controllers — they have no ground, so the
        # controller intentionally exposes no IsGrounded (would be CS1061).
        if ctrl and (fsm or anim) and ctrl.variant not in ("swimming", "flying"):
            update_seams.append({
                "label":   "Grounded bridge: controller → FSM + Animator",
                "snippet": unity_atom_snippet("WireGroundedBridge", {
                    "controller_var":   _fv(ctrl),
                    "statemachine_var": _fv(fsm)  if fsm  else "_statemachine",
                    "animator_var":     _fv(anim) if anim else "_animator",
                }),
            })

        # SEAM 7 — Trigger → SaveSystem (checkpoint saves)
        if trigger and saveload:
            has_trigger_save = True
            awake_seams.append({
                "label":   "SEAM 7: Trigger → SaveSystem",
                "snippet": unity_atom_snippet("WireTriggerToSave", {
                    "trigger_var":  _fv(trigger),
                    "saveload_var": _fv(saveload),
                }),
            })

        # ── Render template ───────────────────────────────────────────────────
        content = env.get_template("GameWiring.cs.j2").render(
            units=real_units,
            has_menu=menu is not None,
            menu_var=_fv(menu) if menu else "_menu",
            has_input_so=has_input_so,
            stored_delegates=stored_delegates,
            awake_seams=awake_seams,
            update_seams=update_seams,
            helper_methods=helper_methods,
            destroy_cleanups=destroy_cleanups,
            has_trigger_save=has_trigger_save,
        )
        (sdir / "GameWiring.cs").write_text(content, encoding="utf-8")
        (sdir / "GameWiring.cs.meta").write_text(_meta_file(wiring.guid), encoding="utf-8")
        self._folder_meta(wdir)
        self._folder_meta(sdir)

    def _emit_context(self, units: list[ComposedUnit], micros_root: Path,
                      has_game_manager: bool) -> None:
        """Emit GameContext.cs — the shared service locator that bridges the
        composed micros (Assets/Micros) and the prompt-authored scripts
        (Assets/Scripts) so all generated assets coexist. `Get<T>()` is the
        universal, leak-free access surface; concrete micro↔GameManager seams are
        added only when a GameManager concept script is present (both ends are
        pipeline-owned templates, so the references always resolve)."""
        from jinja2 import Environment, FileSystemLoader, StrictUndefined

        context    = next(u for u in units if u.kind == "context")
        real_units = [u for u in units if u.kind not in _NON_SCENE_KINDS]

        def _pick(kind: str, target: str | None = None) -> "ComposedUnit | None":
            cands = [u for u in real_units if u.kind == kind]
            if not cands:
                return None
            if len(cands) == 1 or target is None:
                return cands[0]
            pref = [u for u in cands if u.target == target]
            return pref[0] if pref else cands[0]

        # Cross-layer bridge. Direction matters for lifetime safety: the publisher
        # (per-scene PlayerHealth) is subscribed to a persistent-singleton method
        # (GameManager.LoseLife) — when the scene unloads the subscription dies
        # with PlayerHealth, so there is no dangling-delegate leak.
        bridges: list[dict] = []
        if has_game_manager:
            health = _pick("health", "player")
            if health:
                bridges.append({
                    "label": "Player death -> GameManager.LoseLife (micro -> concept)",
                    "snippet": (
                        f"var __health = Get<{health.class_name}>();\n"
                        f"if (__health != null && GameManager.Instance != null)\n"
                        f"    __health.OnDeath += GameManager.Instance.LoseLife;"
                    ),
                })

        env = Environment(loader=FileSystemLoader(str(self._wiring_template_dir())),
                          undefined=StrictUndefined, keep_trailing_newline=True)
        content = env.get_template("GameContext.cs.j2").render(
            units=real_units, bridges=bridges)

        cdir = micros_root / "context"
        sdir = cdir / "Scripts"
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "GameContext.cs").write_text(content, encoding="utf-8")
        (sdir / "GameContext.cs.meta").write_text(_meta_file(context.guid), encoding="utf-8")
        self._folder_meta(cdir)
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
        "npc_dialog":   ["com.unity.modules.audio", "com.unity.modules.physics"],
        "vfx":          ["com.unity.modules.ui", _UGUI],
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

def subsystems_for(
    game_type:    str,
    features:     list[str] | None = None,
    locomotion:   str               = "walk",
    capabilities  = None,            # Optional[ProjectCapabilities] — avoids import cycle
    user_prompt:  str               = "",
    *,
    scenes:       list[str] | None  = None,
    entities      = None,            # list of schema entities (have .kind)
    scripts       = None,            # list of schema scripts  (have .type/.name)
    genre:        str               = "universal",   # schema.meta["genre"] — see unity_archetypes.py
) -> list[Subsystem]:
    """Map a project's game_type + features + locomotion + capabilities to subsystems.

    Deterministic — no LLM. Capabilities (when present) override controller
    parameters so the composed output matches the schema's typed declarations
    rather than generic defaults.

    Optional micros are gated on BOTH feature flags AND deterministic schema
    CONTENT (scene count, entity kinds, script types). The local LLM rarely emits
    the niche feature tokens, so content gating is what actually makes vfx /
    npc_dialog / transition / dialogue reachable for a fitting game.
    """
    feats = set(features or [])
    gt = (game_type or "3D").lower()
    genre = (genre or "universal").lower()

    # ── Deterministic content signals (what the game actually contains) ───────
    scene_count  = len(scenes or [])
    entity_kinds = {str(getattr(e, "kind", "") or "").lower() for e in (entities or [])}
    script_types = {str(getattr(s, "type", "") or "").lower() for s in (scripts or [])}
    prompt_low   = (user_prompt or "").lower()
    has_enemies  = bool({"ai", "navmesh", "enemies"} & feats) or "enemy" in entity_kinds
    has_npcs     = ("npc" in entity_kinds or "dialogue_manager" in script_types
                    or any(k in prompt_low for k in ("voiced npc", "npc dialog", "quest giver")))

    # Controller variant by game type. gt values with an explicit dimension/
    # perspective (2D/top-down/isometric/VR) always win — an fps prompt doesn't
    # turn a top-down game into a first-person one. Only the "no opinion" default
    # branch (plain 3D) defers to genre, so an unqualified "fps" prompt gets a
    # first-person rig instead of silently falling through to third_person.
    ctrl_default = "first_person" if genre == "fps" else "third_person"
    cam_default  = "first_person" if genre == "fps" else "follow"
    ctrl = {
        "2d": "2d_platformer", "2.5d": "2d_platformer", "top_down": "top_down",
        "isometric": "top_down", "vr": "first_person",
    }.get(gt, ctrl_default)
    cam = {
        "2d": "follow_2d", "2.5d": "follow_2d", "top_down": "cinemachine_topdown",
        "isometric": "cinemachine_topdown",
    }.get(gt, cam_default)

    # Locomotion overrides for 3D-ish game types. "vr" belongs here too — a VR
    # scuba/flight game is a legitimate combination; excluding it meant a
    # correctly-classified VR+swim project silently kept the "first_person"
    # walking controller instead of "swimming" (hit live: an underwater VR
    # project never got its swim controller even after game_type/locomotion
    # were both detected correctly).
    if gt in ("3d", "top_down", "isometric", "vr"):
        loco_ctrl = {"drive": "vehicle_car", "fly": "flying", "swim": "swimming"}
        loco_cam  = {"drive": "vehicle_chase"}
        if locomotion in loco_ctrl:
            ctrl = loco_ctrl[locomotion]
            cam  = loco_cam.get(locomotion, "follow")

    # Controller options — capabilities override defaults when declared
    ctrl_opts: dict = {"input_backend": "legacy"}
    if capabilities and capabilities.controller:
        c = capabilities.controller
        abilities = []
        if c.jump:   abilities.append("jump")
        if c.sprint: abilities.append("sprint")
        if c.crouch: abilities.append("crouch")
        ctrl_opts.update({
            "move_speed":   c.speed,
            "sprint_speed": c.speed * c.sprint_multiplier,
            "jump_force":   c.jump_force,
            "abilities":    abilities,
        })

    # Animator variant from locomotion
    anim_variant = {
        "drive": "vehicle", "fly": "creature", "swim": "creature",
    }.get(locomotion, "humanoid")

    # Input variant must align with the CONTROLLER, not raw locomotion: a wheel
    # reader's OnMove is inert on a character (2d_platformer/etc.) controller, so
    # the player can't move. Only a vehicle controller gets the wheel.
    input_variant = ("gamepad" if gt == "vr"
                     else "racing_wheel" if ctrl.startswith("vehicle_")
                     else "keyboard_mouse")

    # Physics body must match the controller's dimension: a 2D game uses a
    # Rigidbody2D (the prior "rigidbody" was invalid and clamped to 3D, which
    # then fought the 2d_platformer controller's Rigidbody2D).
    phys_variant = "rigidbody_2d" if gt in ("2d", "2.5d") else "rigidbody_3d"

    # trigger/interactable need to know the project's dimension so their
    # generated scripts author Collider2D + OnTrigger*2D callbacks instead of
    # the 3D Physics API — a Collider2D never raises OnTriggerEnter(Collider).
    dim_meta = {"meta": {"game_type": game_type or "3D"}}

    subs: list[Subsystem] = [
        # Core (always)
        Subsystem(kind="controller",  variant=ctrl,           target="player",  name="PlayerController", options=ctrl_opts),
        Subsystem(kind="camera",      variant=cam,            target="camera",  name="PlayerCamera"),
        Subsystem(kind="health",      variant="simple",       target="player",  name="PlayerHealth"),
        Subsystem(kind="hud",         variant="health_bar",   target="ui",      name="GameHud"),
        Subsystem(kind="menu",        variant="pause_menu",   target="ui",      name="PauseMenu", options={"backend": "uGUI"}),
        Subsystem(kind="saveload",    variant="json_file",    target="systems", name="SaveSystem"),
        # Always-on: physics body, input reader, interactables, animator, triggers
        Subsystem(kind="physics",     variant=phys_variant,   target="player",  name="PhysicsBody"),
        Subsystem(kind="input",       variant=input_variant,  target="systems", name="InputReader"),
        Subsystem(kind="interactable",variant="pickup",       target="world",   name="WorldInteractable", options=dim_meta),
        Subsystem(kind="animator",    variant=anim_variant,   target="player",  name="PlayerAnimator"),
        Subsystem(kind="statemachine",variant="player_fsm",   target="player",  name="PlayerFSM"),
        Subsystem(kind="trigger",     variant="zone_enter",   target="world",   name="SceneTrigger", options=dim_meta),
    ]

    # Feature- AND content-driven additions
    if has_enemies:
        ai_var = "waypoint_follow" if locomotion == "drive" else "navmesh_patrol"
        ai_tgt = "asset"           if locomotion == "drive" else "world"
        subs.append(Subsystem(kind="ai",           variant=ai_var,          target=ai_tgt,    name="EnemyAI"))
        subs.append(Subsystem(kind="spawner",      variant="wave_spawner",  target="world",   name="EnemySpawner"))
        subs.append(Subsystem(kind="statemachine", variant="enemy_fsm",     target=ai_tgt,    name="EnemyFSM"))
    if "audio" in feats:
        # Pass user_prompt via meta so AudioPipeline.default_fix can derive the game
        # type from the original request (e.g. "horror maze" → horror required cues
        # with ambient+music that route to the generative backend, not DSP).
        audio_opts: dict = {}
        if user_prompt:
            audio_opts["meta"] = {"user_prompt": user_prompt}
        subs.append(Subsystem(kind="audio", variant="categorical_mixer", target="systems",
                              name="AudioDirector", options=audio_opts))
    if "inventory" in feats or "inventory" in prompt_low \
            or any("inventory" in t for t in script_types):
        subs.append(Subsystem(kind="inventory",    variant="slot_based",    target="systems", name="PlayerInventory"))
    # Dialogue (linear text) — requested, VR, an authored dialogue_manager, or NPCs exist.
    if "dialogue" in feats or gt in ("vr",) or "dialogue_manager" in script_types or has_npcs:
        subs.append(Subsystem(kind="dialogue",     variant="linear",        target="systems", name="DialogueSystem"))
    # Decorative impact effects — object-pooled, event-driven (OnTrigger/OnCollision),
    # self-contained (no GameWiring seam). impact_fx null-guards an unassigned prefab.
    # Reachable whenever there's combat (enemies), since impacts need effects.
    if {"vfx", "effects", "particles"} & feats or has_enemies:
        subs.append(Subsystem(kind="vfx",          variant="impact_fx",     target="world",   name="ImpactVFX"))
    # Spoken NPCs (Higgs TTS). Composed unit skips clip generation (meta.skip_audio) so
    # codegen stays LLM/Higgs-free; the <name>Player MonoBehaviour is the attach class.
    # Reachable whenever the world actually has NPCs (entity kind / dialogue script).
    if {"npc_dialog", "voiced_npcs", "npcs"} & feats or has_npcs:
        subs.append(Subsystem(kind="npc_dialog",   variant="quest_giver",   target="world",   name="NpcDialog",
                              options={"speaker_name": "NPC", "meta": {"skip_audio": True}}))
    # Scene transitions — meaningful for any multi-scene project (deterministic: > 1 scene).
    if {"levels", "multi_scene", "scene_transition"} & feats or scene_count > 1:
        subs.append(Subsystem(kind="transition",   variant="fade",          target="systems", name="SceneTransition"))
    return subs
