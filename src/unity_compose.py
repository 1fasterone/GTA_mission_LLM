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
import re
from pathlib import Path

from pydantic import BaseModel, Field
from rich.console import Console

from .micro import REGISTRY
from .micro_pipeline import to_pascal
from .unity_generator import _make_guid, _meta_file, _default_meta

console = Console()


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
    widgets:    list[dict] = Field(default_factory=list)  # hud kind only: the readouts
                                                            # this HUD emits {id,kind,
                                                            # setter}, so SEAM 9/10 can
                                                            # call a setter that provably
                                                            # exists on the class.
    setters:    list[str]  = Field(default_factory=list)  # hud kind only: EVERY public
                                                            # setter the class declares
                                                            # (variant baseline + widgets).
                                                            # SEAM 1/9/10 gate on this
                                                            # instead of the variant name.
    states:     list[str]  = Field(default_factory=list)  # statemachine kind only: the
                                                            # enum members the class
                                                            # declares, so SEAM 4 renders
                                                            # a case per REAL state rather
                                                            # than a hardcoded four.
    objectives: list[dict] = Field(default_factory=list)  # objectives kind only: the
                                                            # authored ObjectiveEntry
                                                            # {id,type,count,...} rows, so
                                                            # SEAM 8 binds real ids rather
                                                            # than inventing them.
    cues:       list[dict] = Field(default_factory=list)  # audio kind only: resolved
                                                            # AudioCueSpec {id,category,
                                                            # descriptor} — lets seam
                                                            # codegen match real cues
                                                            # instead of guessing names.
    board:      dict       = Field(default_factory=dict)  # board kind only: the group ids
                                                            # and verbs the emitted class
                                                            # really carries
                                                            # {pieces,zones,commands}, so
                                                            # SEAM 8 filters on a key the
                                                            # board can actually raise.


# Base scene fileID for composed MonoBehaviours (well above the template's range).
_COMPOSED_FILE_ID_BASE = 900000010

# SEAM 8 unsubscription targets: the C# event each objective-seam atom subscribes
# to, so OnDestroy can detach the exact delegate it attached. Kept beside the
# atom names rather than inside the emitter because only the caller builds the
# cleanup block (same split the other seams use).
_OBJECTIVE_EVENTS = {
    "WireTriggerToObjectives":      "OnActivated",
    "WireInteractableToObjectives": "OnInteract",
    "WireSpawnerToObjectives":      "OnDespawned",
}

# Objective types the tracker advances on its own clock (Objectives.cs.j2's
# Update drains one count per second), so they need no seam and their absence
# from `objectives_bound` is not a hole. Named here because both the WARN below
# and Step 7's mission_loop check have to agree on what "needs a source" means —
# they disagreed once, and an all-`survive` mission was graded unwinnable.
_SELF_DRIVEN_OBJECTIVE_TYPES = frozenset({"survive"})

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
        # What the wiring pass actually decided, for Step 7 to grade against.
        # The playability evaluator reads the SCHEMA, but whether a mission can
        # be completed is a compose-time fact — which seams fired and which
        # objectives found an event source. Stamped onto schema.meta["wiring"]
        # by the pipeline after compose(), so the evaluator grades ground truth
        # rather than re-deriving a prediction. Empty until _emit_wiring runs.
        self.wiring_report: dict = {}
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
        # The compose path does not go through MicroPipeline.run, so it must run
        # the template reconciliation itself — this is where the TurboToys
        # PlayerHealth.cs duplicate members and SceneTrigger.cs missing
        # System.Collections.Generic came from.
        for msg in pipeline.harden(schema):
            console.print(f"  [magenta]FIX[/]  {msg}")
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
            _, schema, variant, class_name, guid = self._resolve(sub)
            cues = ([{"id": c.id, "category": c.category, "descriptor": c.descriptor}
                     for c in schema.cues] if sub.kind == "audio" else [])
            objs = ([{"id": o.id, "type": o.type, "count": o.count,
                      "optional": o.optional, "target_ref": o.target_ref}
                     for o in schema.objectives] if sub.kind == "objectives" else [])
            # Read back AFTER default_fix, so a widget the HUD's collision lint
            # dropped is never wired to a setter the class does not declare.
            widgets, setters = [], []
            if sub.kind == "hud":
                from .micro.hud import hud_setters
                widgets = [{"id": w.id, "kind": w.kind, "setter": w.setter}
                           for w in schema.widgets]
                setters = sorted(hud_setters(variant, schema.widgets))
            # Also read back AFTER default_fix, so SEAM 4 sees the SANITISED
            # enum members rather than whatever the model originally wrote.
            states = ([st.name for st in schema.states]
                      if sub.kind == "statemachine" else [])
            # Read back AFTER default_fix for the same reason as the HUD's
            # setters: a command whose verb the schema dropped is a key SEAM 8
            # would filter on and the board could never raise.
            board = ({"pieces":   [p.id for p in schema.pieces if p.id],
                      "zones":    [z.id for z in schema.zones if z.id],
                      "commands": [c.verb for c in schema.commands if c.verb],
                      # Intersected with the verbs that SURVIVED default_fix,
                      # for the same reason the list above is read back late: a
                      # verb the schema dropped is a key the board can never
                      # raise, and an objective bound to it would be a dead
                      # step reported as a live one.
                      "objective_commands": {
                          oid: verb for oid, verb
                          in (schema.meta.get("objective_commands") or {}).items()
                          if verb in {c.verb for c in schema.commands}}}
                     if sub.kind == "board" else {})
            units.append(ComposedUnit(
                kind=sub.kind, variant=variant, class_name=class_name, guid=guid,
                target=sub.target, file_id=_COMPOSED_FILE_ID_BASE + 10 * i, cues=cues,
                objectives=objs, widgets=widgets, setters=setters, states=states,
                board=board))
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
                has_game_manager: bool = False, wiring=None,
                objective_scenes: "dict[str, str] | None" = None) -> list[ComposedUnit]:
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
        self._emit_wiring(units, micros_root, wiring, objective_scenes)
        # GameContext.cs — the shared locator bridging micros ↔ prompt-authored scripts.
        self._emit_context(units, micros_root, has_game_manager)

        # ── Syntax gate ───────────────────────────────────────────────────────
        # Every composed script through a real C# parser before the project is
        # handed back. One invocation for the whole tree rather than one per
        # micro: the gate costs a process, and a parse failure aborts the build
        # either way. Raises CSharpSyntaxError, listing file/line/column and the
        # compiler's own message for each defect (src/csharp_syntax_gate.py).
        from .csharp_syntax_gate import enforce
        enforce(sorted(micros_root.rglob("*.cs")),
                enabled=bool(self._cfg.get("unity_pipeline", {})
                             .get("syntax_gate", True)),
                label="composed script(s)")
        return units

    def _emit_wiring(self, units: list[ComposedUnit], micros_root: Path, wiring_map=None,
                     objective_scenes: "dict[str, str] | None" = None) -> None:
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

        # Vehicle/aircraft controllers read their own input and have no ground
        # plane, so Controller.cs.j2 omits BOTH SetInputReader and IsGrounded on
        # them (guarded there by `not variant.startswith("vehicle_") and
        # variant != "aircraft"`). Wiring seams that call those methods must be
        # gated the same way or the composed GameWiring won't compile (CS1061).
        # The pointer controller belongs in the self-driven set for the same
        # reason a car does: it reads its own input device (the cursor) and
        # PointerController.cs.j2 declares neither SetInputReader nor
        # IsGrounded. Wiring SEAM 4b/5/6 to it would be a CS1061 on a method
        # that does not exist — and there is no ground under a board anyway.
        _ctrl_var = ctrl.variant if ctrl else ""
        _ctrl_self_driven  = (_ctrl_var.startswith("vehicle_")
                              or _ctrl_var in ("aircraft", "pointer"))
        _ctrl_has_input_rd = bool(ctrl) and not _ctrl_self_driven
        _ctrl_has_grounded = _ctrl_has_input_rd and _ctrl_var not in ("swimming", "flying")

        # SetMove/SetJump/SetGrounded are gated `{% if variant == "player_fsm" %}`
        # in StateMachine.cs.j2 — an enemy_fsm/game_fsm/object_fsm has none of
        # them. `_pick("statemachine", "player")` above still returns the SOLE
        # statemachine unit even when its target isn't "player" (the
        # `len(candidates) == 1` short-circuit), so a project whose only FSM is
        # an enemy/reviewer AI reaches `fsm` here as if it were the player's.
        # Hit live on the RepoQuest run, GameWiring.cs(136,137,260) CS1061 —
        # EnemyFSM has no SetJump/SetMove.
        _fsm_var           = fsm.variant if fsm else ""
        _fsm_has_move_jump = _fsm_var == "player_fsm"

        # ── Build seam lists (atom-driven) ────────────────────────────────────
        awake_seams:      list[dict] = []
        update_seams:     list[dict] = []
        stored_delegates: list[dict] = []
        destroy_cleanups: list[str]  = []
        helper_methods:   list[str]  = []
        has_trigger_save              = False

        # SEAM 1 — Health → HUD (hp bar reflects damage/heal live)
        # Gated on the HUD class actually declaring SetHealth, not on the variant
        # name. Three ways it can: the health_bar baseline, the full_hud_bundle
        # baseline (which the old variant=="health_bar" test silently skipped, so
        # a bundled HUD showed a permanently full bar), or a composer-fed Health
        # widget on any other variant — racing_hud included.
        if health and hud and "SetHealth" in hud.setters:
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

        # SEAM 3 — Health → Audio (hurt / death / heal sounds). Cue ids are
        # matched deterministically against this project's actually-resolved
        # audio bank (audio.cues) — a concept with no matching diegetic sound
        # for a role gets that line omitted, never a fabricated generic id.
        if health and audio:
            from .audio.resolver import match_cue_for_role
            cue_hurt  = match_cue_for_role("hurt",  audio.cues)
            cue_death = match_cue_for_role("death", audio.cues)
            cue_heal  = match_cue_for_role("heal",  audio.cues)
            if cue_hurt or cue_death or cue_heal:
                hv, av = _fv(health), _fv(audio)
                delegate_types = {
                    "hurt": ("Action<float>", "_onDamaged", "OnDamaged"),
                    "death": ("Action", "_onDeath", "OnDeath"),
                    "heal": ("Action<float>", "_onHealed", "OnHealed"),
                }
                matched = {"hurt": cue_hurt, "death": cue_death, "heal": cue_heal}
                stored_delegates += [
                    {"type": t, "name": name}
                    for role, (t, name, _evt) in delegate_types.items()
                    if matched[role]
                ]
                awake_seams.append({
                    "label":   "SEAM 3: Health → Audio",
                    "snippet": unity_atom_snippet("WireHealthToAudio", {
                        "health_var": hv, "audio_var": av,
                        "cue_hurt": cue_hurt, "cue_death": cue_death, "cue_heal": cue_heal,
                    }),
                })
                unsub_lines = [
                    f"    if ({name} != null) {hv}.{evt} -= {name};"
                    for role, (_t, name, evt) in delegate_types.items()
                    if matched[role]
                ]
                destroy_cleanups.append(
                    f"if ({hv} != null)\n" "{\n" + "\n".join(unsub_lines) + "\n}"
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
            # One case per state the FSM ACTUALLY declares. The hardcoded
            # Idle/Moving/Jumping/Dead this replaces was the same CS0117 defect
            # the ai micro carried: it was correct only for player_fsm's default
            # state set, and referenced members that do not exist the moment the
            # FSM is an enemy_fsm or the model names its own states.
            cases = "\n".join(
                f"            case {fsm_cls}.State.{st}: "
                f"{anv}.CrossFade(\"{_anim_clip_for_state(st)}\", 0.1f); break;"
                for st in (fsm.states or ["Idle"]))
            helper_methods.append(
                f"private void OnFSMStateChanged({fsm_cls}.State state)\n"
                f"    {{\n"
                f"        if ({anv} == null) return;\n"
                f"        switch (state)\n"
                f"        {{\n"
                f"{cases}\n"
                f"        }}\n"
                f"    }}"
            )
            destroy_cleanups.append(
                f"if ({fv} != null && {anv} != null)\n"
                f"    {fv}.OnStateChanged -= OnFSMStateChanged;"
            )

        # SEAM 4b — InputReader → Controller (only for controllers that expose
        # SetInputReader — a vehicle/aircraft reads its own input).
        if has_input_so and ctrl and _ctrl_has_input_rd:
            awake_seams.append({
                "label":   "SEAM 4b: InputReader → Controller",
                "snippet": unity_atom_snippet("WireInputToController", {
                    "input_var": "_inputSO", "controller_var": _fv(ctrl),
                }),
            })
            # Controller unsubscribes itself in its own OnDisable.

        # SEAM 5 — InputReader → FSM (typed move + jump events)
        if has_input_so and fsm and _fsm_has_move_jump:
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
        # Only for controllers that expose IsGrounded. Swimming/flying have no
        # ground; vehicle/aircraft read their own input and also omit IsGrounded —
        # emitting the bridge for any of them would be CS1061.
        _fsm_for_grounded = fsm if (fsm and _fsm_has_move_jump) else None
        if ctrl and (_fsm_for_grounded or anim) and _ctrl_has_grounded:
            update_seams.append({
                "label":   "Grounded bridge: controller → FSM + Animator",
                "snippet": unity_atom_snippet("WireGroundedBridge", {
                    "controller_var":   _fv(ctrl),
                    "statemachine_var": _fv(_fsm_for_grounded) if _fsm_for_grounded else "_statemachine",
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

        # SEAM 8 — gameplay events → mission objectives (closes the mission loop)
        # Without this the objectives tracker receives real authored objectives and
        # then sits inert: nothing in the project ever called ReportProgress, so
        # OnMissionComplete was unreachable and no generated game could be won.
        #
        # Each objective TYPE has exactly one honest event source in a composed
        # project. A source can only be bound to ONE objective: two `eliminate`
        # objectives sharing the single enemy spawner would both advance on every
        # kill, which is worse than not wiring the second at all. So the first
        # match (schema order) binds and every other objective is NAMED in the log
        # rather than silently mis-wired — abstain loudly, never default quietly.
        interactable = _pick("interactable", "world")
        spawner      = _pick("spawner",      "world")
        board        = _pick("board",        "systems")
        objective_units = [u for u in real_units if u.kind == "objectives"]

        #  types                    source unit    atom                            source arg-name     delegate field           delegate C# type
        _OBJECTIVE_SOURCES = (
            (("reach",),             trigger,      "WireTriggerToObjectives",      "trigger_var",      "_onReachObjective",     "Action"),
            (("collect", "interact"), interactable, "WireInteractableToObjectives", "interactable_var", "_onCollectObjective",   "Action"),
            (("eliminate",),         spawner,      "WireSpawnerToObjectives",      "spawner_var",      "_onEliminateObjective", "Action<GameObject>"),
        )
        # `survive` needs no seam — the tracker runs its own clock (Objectives.cs.j2).
        _SELF_DRIVEN_TYPES = _SELF_DRIVEN_OBJECTIVE_TYPES

        bound_ids: set[str] = set()

        # SEAM 8a — board → objectives, FIRST and per objective.
        #
        # The generic sources below bind ONE objective each, because a trigger
        # volume and an enemy spawner cannot tell which objective an event was
        # about. A board can: every event it raises carries the group, zone or
        # verb it happened to, so each objective gets its own filtered binding
        # and a six-step board does not have to settle for one advanceable step.
        #
        # It runs before them so a `collect` objective about a real piece group
        # binds to the piece being placed rather than to any click on any
        # interactable — the difference between the instruction being true and
        # the instruction merely being displayed.
        if board is not None and objective_units:
            bv = _fv(board)
            n = 0
            for unit in objective_units:
                for obj in unit.objectives:
                    if not obj.get("id"):
                        continue
                    bind = _board_objective_binding(board, obj)
                    if bind is None:
                        continue
                    event, key, amount = bind
                    delegate = f"_onBoardObjective{n}"
                    n += 1
                    stored_delegates.append({"type": "Action<string>", "name": delegate})
                    awake_seams.append({
                        "label": f"SEAM 8: board → objectives "
                                 f"({obj.get('type')}: {obj['id']} ← {event} {key})",
                        "snippet": unity_atom_snippet("WireBoardToObjectives", {
                            "board_var": bv, "objectives_var": _fv(unit),
                            "delegate_var": delegate, "event_name": event,
                            "match_key": key, "objective_id": obj["id"],
                            "amount": amount,
                        }),
                    })
                    destroy_cleanups.append(
                        f"if ({bv} != null && {delegate} != null)\n"
                        f"    {bv}.{event} -= {delegate};"
                    )
                    bound_ids.add(f"{unit.class_name}.{obj['id']}")

        # Bucket objectives by the scene their mission resolves to
        # (`objective_scenes`: {objective.id: scene_name}, flattened by the
        # caller from schema.meta["mission_scenes"] — see
        # `_infer_mission_scenes`/`_objective_scene_map`,
        # src/unity_pipeline.py). Each bucket runs the SAME one-source-per-
        # type-wins algorithm independently, so a project with N missions
        # across N scenes gets up to N bindings per type instead of one
        # project-wide — confirmed live (GearCitadel): a single global
        # trigger/interactable meant only the FIRST mission's `reach`/
        # `collect` objectives could ever bind; every other mission's were
        # permanently unadvanceable. Objectives whose mission could not be
        # resolved to a scene fall into the `None` bucket and keep today's
        # global, ungated behavior byte-for-byte — a project with no
        # `objective_scenes` (single mission/scene, or every project before
        # this feature) is unaffected.
        objective_scenes = objective_scenes or {}
        scene_keys = sorted({s for s in objective_scenes.values() if s}) + [None]
        needs_scene_var = False
        for scene_key in scene_keys:
            scoped = [(u, o) for u in objective_units for o in u.objectives
                     if o.get("id") and objective_scenes.get(str(o["id"])) == scene_key]
            if not scoped:
                continue
            for types, source, atom, source_arg, delegate, delegate_type in _OBJECTIVE_SOURCES:
                if source is None:
                    continue
                match = next(
                    ((u, o) for u, o in scoped
                     if o.get("type") in types and o.get("id")
                     and f"{u.class_name}.{o['id']}" not in bound_ids),
                    None,
                )
                if match is None:
                    continue
                unit, obj = match
                ov = _fv(unit)
                # The delegate FIELD is declared once per name — two scene
                # buckets binding the same objective TYPE (e.g. two `reach`
                # objectives, one per hall) would otherwise both try to
                # declare `_onReachObjective`, a duplicate member (CS0102).
                # Scene-qualify the name whenever this binding is scene-gated.
                delegate_name = delegate if scene_key is None else (
                    f"{delegate}_{re.sub(r'[^A-Za-z0-9]', '', scene_key)}")
                stored_delegates.append({"type": delegate_type, "name": delegate_name})
                snippet = unity_atom_snippet(atom, {
                    source_arg: _fv(source), "objectives_var": ov,
                    "delegate_var": delegate_name, "objective_id": obj["id"],
                })
                label = f"SEAM 8: {source.kind} → objectives ({obj['type']}: {obj['id']})"
                if scene_key is not None:
                    needs_scene_var = True
                    label += f" [scene: {scene_key}]"
                    indented = "\n".join(f"    {line}" if line else line
                                         for line in snippet.splitlines())
                    snippet = f'if (_scene == "{scene_key}")\n{{\n{indented}\n}}'
                awake_seams.append({"label": label, "snippet": snippet})
                destroy_cleanups.append(
                    f"if ({_fv(source)} != null && {delegate_name} != null)\n"
                    f"    {_fv(source)}.{_OBJECTIVE_EVENTS[atom]} -= {delegate_name};"
                )
                bound_ids.add(f"{unit.class_name}.{obj['id']}")

        # Abstain loudly: name every authored objective this build cannot advance.
        #
        # Split three ways rather than two, because "unbound" was doing the work
        # of three different facts and Step 7 could not tell them apart:
        #   self-driven   the tracker's own clock advances it — not a hole
        #   optional      unbound, but it does not block OnMissionComplete
        #   required      unbound AND blocking — the mission can never finish
        # The mission_loop check grades the third list only.
        self_driven, optional_unbound, unbound = [], [], []
        for u in objective_units:
            for o in u.objectives:
                if not o.get("id"):
                    continue
                label = f"{u.class_name}.{o['id']} ({o.get('type')})"
                if o.get("type") in _SELF_DRIVEN_TYPES:
                    self_driven.append(label)
                elif f"{u.class_name}.{o['id']}" in bound_ids:
                    continue
                elif o.get("optional"):
                    optional_unbound.append(label)
                else:
                    unbound.append(label)
        if objective_units and (unbound or optional_unbound):
            sources = sorted(
                {s.kind for _t, s, *_r in _OBJECTIVE_SOURCES if s is not None}
                | ({"board"} if board is not None else set()))
            if unbound:
                console.print(
                    "  [yellow]WARN[/]  objectives: no event source for "
                    + ", ".join(unbound)
                    + " — these cannot advance at runtime, so the mission can "
                      "never complete. Reachable sources this build: "
                    + (", ".join(sources) or "none"))
            if optional_unbound:
                console.print(
                    "  [yellow]WARN[/]  objectives: no event source for optional "
                    + ", ".join(optional_unbound)
                    + " — the mission still completes without them.")

        # SEAM 9 — objectives → HUD (the mission loop becomes visible)
        # SEAM 8 makes the mission finishable; without this the player has no way
        # to know it is progressing. Bound to OnObjectiveComplete as well as
        # OnObjectiveProgress deliberately: ReportProgress fires OnObjectiveProgress
        # BEFORE it sets o.complete (Objectives.cs.j2), so a progress-only binding
        # would show the just-finished objective as still active — the readout
        # would trail the game by one event.
        # EVERY tracker, not just the first. One mission per tracker, and the
        # rules micro now states every mission's objectives as steps, so a
        # readout bound to the first alone displayed a tally that stopped a
        # third of the way through a progression the player was still being
        # walked along — and froze on "2 / 2" while four live objectives
        # advanced behind it.
        if objective_units and hud and "SetObjective" in hud.setters:
            uv = _fv(hud)
            stored_delegates += [
                {"type": "Action<string, int, int>", "name": "_onObjectiveHudProgress"},
                {"type": "Action<string>",           "name": "_onObjectiveHudComplete"},
            ]
            for unit in objective_units:
                ov = _fv(unit)
                awake_seams.append({
                    "label": f"SEAM 9: objectives → HUD "
                             f"({unit.class_name} → current objective + tally)",
                    "snippet": unity_atom_snippet("WireObjectivesToHUD", {
                        "objectives_var": ov, "hud_var": uv,
                        "refresh_method": "RefreshObjectiveHud",
                    }),
                })
                destroy_cleanups.append(
                    f"if ({ov} != null)\n"
                    f"{{\n"
                    f"    if (_onObjectiveHudProgress != null) {ov}.OnObjectiveProgress -= _onObjectiveHudProgress;\n"
                    f"    if (_onObjectiveHudComplete != null) {ov}.OnObjectiveComplete -= _onObjectiveHudComplete;\n"
                    f"}}"
                )
            # The tally is the whole project's, summed in the order the steps
            # are stated. The label is the FIRST tracker still holding a live
            # objective, which is the step the rules panel is showing — the two
            # readouts name the same action or the player is told two things.
            _hud_reads = "\n".join(
                f"        if ({_fv(u)} != null)\n"
                f"        {{\n"
                f"            var a{i} = {_fv(u)}.ActiveObjective;\n"
                f"            if (label.Length == 0 && a{i} != null) label = a{i}.description;\n"
                f"            done  += {_fv(u)}.CompletedRequired;\n"
                f"            total += {_fv(u)}.TotalRequired;\n"
                f"        }}"
                for i, u in enumerate(objective_units))
            helper_methods.append(
                f"private void RefreshObjectiveHud()\n"
                f"    {{\n"
                f"        if ({uv} == null) return;\n"
                f"        string label = \"\";\n"
                f"        int done = 0, total = 0;\n"
                f"{_hud_reads}\n"
                f"        {uv}.SetObjective(label, done, total);\n"
                f"    }}"
            )

        # SEAM 10 — inventory → HUD (carried count)
        inventory = _pick("inventory", "systems")
        if inventory and hud and "SetCarried" in hud.setters:
            iv, uv = _fv(inventory), _fv(hud)
            stored_delegates.append({"type": "Action", "name": "_onInventoryHud"})
            awake_seams.append({
                "label": "SEAM 10: inventory → HUD (carried count)",
                "snippet": unity_atom_snippet("WireInventoryToHUD", {
                    "inventory_var": iv, "hud_var": uv, "setter": "SetCarried",
                }),
            })
            destroy_cleanups.append(
                f"if ({iv} != null && _onInventoryHud != null)\n"
                f"    {iv}.OnChanged -= _onInventoryHud;"
            )

        # ── schema.wiring (WiringMap/WiringDependency) — additive only ─────────
        # Every composed unit already gets its own auto-found scene ref (the
        # `units` loop above — a private field + FindFirstObjectByType in
        # Awake), so a generic (source, target, type) triple has nothing new
        # to ADD there; what it CAN honestly contribute is making the
        # author's declared intent visible in the generated file instead of
        # silently dropping it. Skipped entirely for pairs the named SEAMs
        # above already cover (avoids a duplicate/conflicting comment) — real
        # behavior wiring for a genuinely novel pair still needs a named SEAM,
        # same as today. NOT a rewrite of the SEAM system: this never touches
        # awake_seams/update_seams content for the 7 named pairs above.
        _NAMED_SEAM_PAIRS = {
            frozenset({"health", "hud"}), frozenset({"health", "statemachine"}),
            frozenset({"health", "audio"}), frozenset({"statemachine", "animator"}),
            frozenset({"input", "controller"}), frozenset({"input", "statemachine"}),
            frozenset({"trigger", "saveload"}),
            # SEAM 8 pairs
            frozenset({"trigger", "objectives"}), frozenset({"interactable", "objectives"}),
            frozenset({"spawner", "objectives"}), frozenset({"board", "objectives"}),
            # SEAM 9/10 pairs
            frozenset({"objectives", "hud"}), frozenset({"inventory", "hud"}),
        }
        if wiring_map is not None:
            for dep in wiring_map.dependencies:
                pair = frozenset({dep.source, dep.target})
                if pair in _NAMED_SEAM_PAIRS:
                    continue
                src_unit = next((u for u in real_units if u.kind == dep.source), None)
                tgt_unit = next((u for u in real_units if u.kind == dep.target), None)
                if src_unit is None or tgt_unit is None:
                    continue
                awake_seams.append({
                    "label": f"wiring: {dep.source} --{dep.type}--> {dep.target} (schema.wiring)",
                    "snippet": (
                        f"// {_fv(src_unit)} ({src_unit.class_name}) --{dep.type}--> "
                        f"{_fv(tgt_unit)} ({tgt_unit.class_name})\n"
                        f"// Both are already available above as auto-found scene refs —\n"
                        f"// wire the specific method call yourself; a generic (source, target,\n"
                        f"// type) triple can't infer which one to call."
                    ),
                })

        # ── Report what was wired (Step 7 grades this) ────────────────────────
        # `objectives_bound` is the mission loop: an objective with an event
        # source can advance, and one without is content the player can see and
        # never complete. `hud_objective` is whether that progress is visible.
        #
        # The three unbound lists are reported separately because completion is
        # AND-shaped: Objectives.cs.j2 sets MissionComplete only once every
        # non-optional objective is done, so ONE unbound required objective is
        # an unwinnable build no matter how many others bound. A single
        # `objectives_unbound` list could not say which — the reason Step 7
        # passed a mission with five dead objectives out of six.
        required_total = sum(
            1 for u in objective_units for o in u.objectives
            if o.get("id") and not o.get("optional"))
        self.wiring_report = {
            "seams": [s["label"].split(":")[0].strip()
                      for s in (awake_seams + update_seams)
                      if s["label"].startswith("SEAM ")],
            "objectives_bound":       sorted(bound_ids),
            "objectives_unbound":     sorted(unbound),            # required + blocking
            "objectives_optional_unbound": sorted(optional_unbound),
            "objectives_self_driven": sorted(self_driven),
            "objectives_total":       sum(len(u.objectives) for u in objective_units),
            "objectives_required":    required_total,
            "hud_objective": bool(hud and "SetObjective" in hud.setters
                                  and objective_units),
        }

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
            needs_scene_var=needs_scene_var,
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

# The 3D "golden archetype" — the always-on core subsystems_for() emits for any
# plain 3D project (no genre/locomotion override). Order matters: plan() assigns
# scene fileIDs positionally (_COMPOSED_FILE_ID_BASE + 10*i), so reordering this
# list silently reshuffles every downstream fileID. Locked + regression-tested in
# tests/test_golden_3d_archetype.py so future subsystems_for() changes can't
# silently drift the baseline every 3D project depends on.
GOLDEN_3D_CORE_SIGNATURE: tuple[tuple[str, str], ...] = (
    ("controller", "third_person"), ("camera", "follow"), ("health", "simple"),
    ("hud", "health_bar"), ("menu", "pause_menu"), ("saveload", "json_file"),
    ("physics", "rigidbody_3d"), ("input", "keyboard_mouse"),
    ("interactable", "pickup"), ("animator", "humanoid"),
    ("statemachine", "player_fsm"), ("trigger", "zone_enter"),
    ("lighting", "three_point"),
)

# The 2D golden archetype — same always-on kinds as 3D, dimension-appropriate
# variants (2d_platformer/follow_2d/rigidbody_2d). "animator" stays "humanoid"
# here: anim_variant is keyed off locomotion, not game_type, and Animator.cs.j2
# is itself dimension-agnostic (no hardcoded rig/bone names) — confirmed
# functionally correct for 2D, just a cosmetically misleading label.
GOLDEN_2D_CORE_SIGNATURE: tuple[tuple[str, str], ...] = (
    ("controller", "2d_platformer"), ("camera", "follow_2d"), ("health", "simple"),
    ("hud", "health_bar"), ("menu", "pause_menu"), ("saveload", "json_file"),
    ("physics", "rigidbody_2d"), ("input", "keyboard_mouse"),
    ("interactable", "pickup"), ("animator", "humanoid"),
    ("statemachine", "player_fsm"), ("trigger", "zone_enter"),
    ("lighting", "flat_2d"),
)

# The 2.5D golden archetype — DISTINCT from 2D. 2.5D means real 3D models/world
# (matches unity_asset_resolver's "2.5D" context: it CAN use 3D Kenney models,
# unlike pure 2D) viewed and played from a side-scroller-style constrained
# perspective. So the world/enemies/colliders/rig are all real 3D (same as
# GOLDEN_3D_CORE_SIGNATURE's "rigidbody_3d"/"humanoid"), but the PLAYER gets a
# CharacterController locked to a lateral (X) movement plane
# ("platformer_2_5d") and a tilted/elevated perspective follow camera
# ("follow_2_5d") instead of first/third-person free-look. Was previously
# aliased 1:1 onto GOLDEN_2D_CORE_SIGNATURE (Rigidbody2D + orthographic
# follow_2d) — that paired 3D-capable assets with a 2D physics world, which is
# why the asset resolver's own "2.5D can use 3D models" comment always looked
# contradictory (see unity_asset_resolver.py's _GAME_TYPE_CONTEXTS).
GOLDEN_2_5D_CORE_SIGNATURE: tuple[tuple[str, str], ...] = (
    ("controller", "platformer_2_5d"), ("camera", "follow_2_5d"), ("health", "simple"),
    ("hud", "health_bar"), ("menu", "pause_menu"), ("saveload", "json_file"),
    ("physics", "rigidbody_3d"), ("input", "keyboard_mouse"),
    ("interactable", "pickup"), ("animator", "humanoid"),
    ("statemachine", "player_fsm"), ("trigger", "zone_enter"),
    ("lighting", "three_point"),
)

# The 2D top-down golden archetype — DISTINCT from both 2D side-scroller and the
# (real-3D) "top_down" game_type. Flat Rigidbody2D physics like GOLDEN_2D, but
# free 2-axis movement (no gravity/jump) via the "2d_topdown" controller, which
# already existed in src/micro/controller.py but was unreachable until this
# game_type was added — the deterministic mapper only ever selected
# "2d_platformer" for any 2D-flavored game_type.
GOLDEN_2D_TOPDOWN_CORE_SIGNATURE: tuple[tuple[str, str], ...] = (
    ("controller", "2d_topdown"), ("camera", "follow_2d"), ("health", "simple"),
    ("hud", "health_bar"), ("menu", "pause_menu"), ("saveload", "json_file"),
    ("physics", "rigidbody_2d"), ("input", "keyboard_mouse"),
    ("interactable", "pickup"), ("animator", "humanoid"),
    ("statemachine", "player_fsm"), ("trigger", "zone_enter"),
    ("lighting", "flat_2d"),
)


# ── Multimodal-IR field mappers (Phase 1) ──────────────────────────────────────
# Pure schema-field → Subsystem.options mappers. No I/O, no LLM — deterministic
# passthrough of LLM-authored content (Step 3) into the micro that already knows
# how to render it (Step 6), instead of that micro falling back to its defaults.

# AudioSpec.generation_method (unity_schema.py) → AudioCueSpec.kind (micro/audio.py)
_AUDIO_GEN_TO_CUE_KIND = {
    "sfxr":         "synth",
    "procedural":   "synth",
    "stable_audio": "gen",
    "elevenlabs":   "tts",
    "recorded":     "library",
}


def _audio_cues_from_specs(specs) -> list[dict]:
    """schema.audio_specs (list[AudioSpec]) -> AudioCueSpec kwargs (micro/audio.py:31-43)."""
    cues: list[dict] = []
    for spec in specs or []:
        params = spec.parameters or {}
        cues.append({
            "id":         spec.id,
            "category":   spec.type,   # same Literal domain — direct passthrough
            "kind":       _AUDIO_GEN_TO_CUE_KIND.get(spec.generation_method, "auto"),
            "descriptor": spec.description,
            "loop":       spec.loop,
            "archetype":  params.get("archetype", ""),
            "gen_prompt": params.get("gen_prompt", spec.description),
            "text":       params.get("text", ""),
            "voice":      params.get("voice", "default"),
            "spatial":    bool(params.get("spatial", False)),
        })
    return cues


def _vfx_options_from_specs(specs) -> dict:
    """schema.vfx (list[VfxSpec]) -> VfxSchema options (micro/vfx.py:24-63)."""
    if not specs:
        return {}
    if len(specs) == 1:
        spec = specs[0]
        params = spec.parameters or {}
        return {
            "variant":       "particle_system",
            "effect_id":     spec.name,
            "loop":          bool(params.get("loop", False)),
            "duration":      float(params.get("duration", 2.0)),
            "play_on_start": bool(params.get("play_on_start", True)),
            "auto_destroy":  bool(params.get("auto_destroy", True)),
        }
    cues = [{
        "effect_id": spec.name,
        "x": 0.0, "y": 0.0, "z": 0.0,
        "scale": float((spec.parameters or {}).get("scale", 1.0)),
        "delay": i * 0.1,
    } for i, spec in enumerate(specs)]
    return {"variant": "vfx_sequence", "cues": cues}


_SHADER_PASSTHROUGH = (
    "loop", "duration", "auto_start", "reverse_on_repeat",
    "flash_color_r", "flash_color_g", "flash_color_b", "flash_duration",
    "speed_x", "speed_y", "min_value", "max_value", "pulse_speed",
)


def _shader_options_from_specs(spec) -> dict:
    """One ShaderSpec (schema.shaders) -> ShaderSchema options (micro/shader.py:60-95).
    ShaderSpec.parameters is a loose bag with no shape guarantee, so every key is
    read defensively; ShaderSchema.default_fix fills whatever is left with the
    variant's own sensible defaults (property_name, color_cycle stops, ...)."""
    params = spec.parameters or {}
    opts: dict = {
        "variant":       str(params.get("variant", "color_cycle")),
        "property_name": params.get("property_name", ""),
    }
    for key in _SHADER_PASSTHROUGH:
        if key in params:
            opts[key] = params[key]
    return opts


def _animator_clips_from_specs(animations, characters) -> dict:
    """schema.animations + CharacterSpec.animation_set -> AnimatorSchema.clips
    (state_name -> clip_name, micro/animator.py:36). animations is authoritative;
    character animation_set names fill any gaps."""
    clips: dict[str, str] = {}
    for spec in animations or []:
        clip_name = spec.clip or spec.id
        if clip_name:
            clips[spec.name or spec.id] = clip_name
    for char in characters or []:
        for clip_name in (char.animation_set or []):
            clips.setdefault(clip_name, clip_name)
    return clips


def _dialogue_options_for_character(char) -> dict:
    """CharacterSpec.dialog_graph (DialogGraph/DialogNode, unity_schema.py:558-561)
    -> DialogueSchema options (DialogueNode/DialogueChoice, micro/dialogue.py:27-58).
    Lossy by design: DialogNode.emotion/audio_ref have no DialogueNode equivalent."""
    graph = char.dialog_graph
    nodes = [{
        "id":      n.id,
        "speaker": n.speaker,
        "text":    n.text,
        "choices": [{"text": c.text, "next": c.next_node} for c in n.choices],
    } for n in graph.nodes]
    return {"start_node": graph.start_node or (nodes[0]["id"] if nodes else ""),
            "nodes": nodes}


def _has_voiced_lines(char) -> bool:
    """DialogNode.audio_ref is documented as "AudioSpec id — empty = no voiced
    line" (unity_schema.py:554) — that's the DialoguePipeline vs NpcDialogPipeline
    routing signal."""
    return any(n.audio_ref for n in char.dialog_graph.nodes)


def _npc_dialog_lines_for_character(char) -> list[dict]:
    """CharacterSpec.dialog_graph (DialogNode) -> NpcDialogSchema.lines
    (DialogLine, micro/npc_dialog.py:67-118). Pydantic coerces these dicts into
    DialogLine directly — no meta._pre_lines indirection needed since we're
    building the Subsystem's options at construction time, not patching them in
    after the fact (unlike unity_pipeline.py's concept-extraction path)."""
    return [{
        "line_id": n.id,
        "text":    n.text,
        "emotion": n.emotion,
    } for n in char.dialog_graph.nodes]


def _board_objective_binding(board, objective: dict) -> tuple[str, str, int] | None:
    """Which board event advances this objective — (event, match key, amount).

    None means the board has nothing to do with it, and the objective falls
    through to the generic sources. Deliberately keyed on `target_ref`, the
    field the mission uses to name the content it is about: a board can only
    honestly advance an objective that names something ON the board.

        collect / interact  a piece group   OnPiecePlaced, one count per piece
        reach               a zone          OnZoneFilled, the whole count at once
        interact            a command verb  OnCommand,     the whole count at once

    `amount` differs because the events do: OnPiecePlaced fires once per piece,
    so an objective of count 9 completes after nine placements. The other two
    fire ONCE, so a count of 9 bound at 1-per-event would never complete —
    silently, which is the failure mode this whole seam exists to end.
    """
    from .micro.board import norm_ref

    otype = str(objective.get("type") or "").lower()
    try:
        count = max(1, int(objective.get("count") or 1))
    except (TypeError, ValueError):
        count = 1

    # A verb derived FOR this objective outranks its target_ref, because it was
    # only recorded where a drag cannot do the job: the target is a readout —
    # `TryPlace` refuses a drag into one, so no amount of dragging could ever
    # satisfy it — or the objective names nothing on the board at all. Binding
    # such an objective to OnZoneFilled reports a live step whose only route is
    # the built-in `move` verb, which nothing ever tells the player about.
    verb = (board.board.get("objective_commands") or {}).get(objective.get("id"))
    if verb:
        return ("OnCommand", verb, count)

    ref = norm_ref(objective.get("target_ref") or "")
    if not ref:
        return None

    groups   = {norm_ref(g): g for g in (board.board.get("pieces") or [])}
    zones    = {norm_ref(z): z for z in (board.board.get("zones") or [])}
    commands = {norm_ref(c): c for c in (board.board.get("commands") or [])}

    if otype in ("collect", "interact") and ref in groups:
        return ("OnPiecePlaced", groups[ref], 1)
    if ref in zones:
        return ("OnZoneFilled", zones[ref], count)
    if ref in commands:
        return ("OnCommand", commands[ref], count)
    # A `reach`/`eliminate` objective naming a piece group still has an honest
    # board answer — the group being finished — even though the per-piece event
    # is not the right granularity for it.
    if ref in groups:
        return ("OnGroupComplete", groups[ref], count)
    return None


def _objectives_options_for_mission(mission) -> dict:
    """schema.missions (MissionSpec/MissionObjective, unity_schema.py:734-764) ->
    ObjectivesSchema options (ObjectiveEntry, micro/objectives.py).

    `triggers` now feeds the composed trigger zone's action list — see
    _trigger_options_for_missions. `story_beat_ref` still has no execution engine
    to hook into and stays inert metadata (documented in the rendered README),
    not silently dropped.

    Objective TYPE crosses a vocabulary boundary here: the IR's MissionObjType
    (reach|collect|defeat|survive|escort|timer) is not the micro's ObjectiveType
    (reach|collect|eliminate|escort|survive|interact). ObjectiveEntry._clamp_type
    translates the synonyms; before it did, every `defeat` objective silently
    became "reach" and no kill objective could ever be wired."""
    return {
        "title": mission.title,
        "objectives": [{
            "id":          o.id,
            "description": o.description,
            "type":        o.type,
            "target_ref":  o.target_ref,
            "count":       o.count,
            "optional":    o.optional,
        } for o in mission.objectives],
        "rewards": mission.rewards,
    }


def _hud_widgets_for_project(*, hud_variant: str, missions, has_inventory: bool) -> list[dict]:
    """The readouts this specific project can actually feed.

    Deliberately conservative: a widget is only added when a composed subsystem
    exists to publish into it (SEAM 9/1b/10 below). Adding an "oxygen gauge"
    nothing ever writes to would be the same inert content this whole change
    exists to remove — a HUD that displays nothing is worse than no widget.

    The Pipeline LLM can still author widgets of its own through HUDSchema.widgets;
    these are the ones the composer can guarantee.
    """
    widgets: list[dict] = []
    if any(m.objectives for m in (missions or [])):
        widgets.append({"id": "Objective", "kind": "objective",
                        "label": "OBJECTIVE", "source": "objectives"})
    # health_bar and full_hud_bundle already own SetHealth (SEAM 1). Every other
    # variant — racing_hud especially — shows no HP at all without this.
    if hud_variant not in ("health_bar", "full_hud_bundle"):
        widgets.append({"id": "Health", "kind": "bar",
                        "label": "HP", "source": "health", "max_value": 100})
    if has_inventory:
        widgets.append({"id": "Carried", "kind": "counter",
                        "label": "CARRIED", "source": "inventory"})
    return widgets


# FSM state name -> the Animator clip SEAM 4 cross-fades to. Substring match on
# the lowercased state name, first hit wins.
_ANIM_CLIP_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("idle", "Idle"), ("wait", "Idle"), ("dormant", "Idle"), ("inactive", "Idle"),
    ("jump", "Jump"), ("leap", "Jump"), ("fall", "Fall"),
    ("dead", "Death"), ("die", "Death"), ("death", "Death"),
    ("destroy", "Death"), ("expire", "Death"),
    ("attack", "Attack"), ("strike", "Attack"), ("lunge", "Attack"),
    ("shoot", "Attack"), ("fire", "Attack"), ("melee", "Attack"),
    # "mov", not "move" — the default player_fsm state is `Moving`, and the
    # hardcoded mapping this replaces sent it to the Run clip.
    ("mov", "Run"), ("walk", "Run"), ("run", "Run"), ("chase", "Run"),
    ("charge", "Run"), ("pursue", "Run"), ("patrol", "Run"), ("prowl", "Run"),
    ("hunt", "Run"), ("flee", "Run"), ("retreat", "Run"), ("escape", "Run"),
    ("alert", "Idle"), ("freeze", "Idle"), ("watch", "Idle"),
)


def _anim_clip_for_state(state: str) -> str:
    """The Animator clip a state cross-fades to.

    Falls back to the STATE'S OWN NAME rather than a generic clip: an invented
    state like `Prowl` keeps its vocabulary in the generated wiring, and
    Animator.CrossFade against a clip the controller does not have is a runtime
    no-op, never a compile error — so an unmatched name costs nothing.
    """
    low = str(state).lower()
    for keyword, clip in _ANIM_CLIP_KEYWORDS:
        if keyword in low:
            return clip
    return str(state)


def _ai_states_from_brief(entities, characters, scripts=None) -> list[dict]:
    """EntitySpec.behavior + CharacterSpec.npc_brain + the enemy script's own
    description -> AI FSM states.

    All three were dropped on the floor: the ai micro was composed with bare
    dimension metadata, so an enemy the brief described as "prowls the gantries,
    freezes when it hears you, then charges" produced the same stateless patroller
    as an enemy described not at all.

    The enemy SCRIPT is included because that is where a structured brief actually
    puts its behaviour prose. EntitySpec has no description field, so an entity
    bullet's wording never survives the parse and `behavior` frequently holds a
    script reference ("EnemyAI") rather than a description. Read before
    drop_owned_scripts runs (unity_pipeline.py), so the enemy script is still
    present even in --compose builds where the micro later replaces it.

    Each clause is mapped to a behaviour role by src/micro/ai.infer_role, and the
    STATE KEEPS THE BRIEF'S OWN WORD ("Prowl", "Freeze", "Charge") while the role
    is what the generator emits code for. One state per role — a second "chase"
    clause adds vocabulary, not a second identical case.
    """
    from .micro.ai import infer_role

    sources: list[str] = []
    for e in (entities or []):
        if str(getattr(e, "kind", "") or "").lower() in ("enemy", "npc", "creature"):
            sources.append(str(getattr(e, "behavior", "") or ""))
    for c in (characters or []):
        brain = getattr(c, "npc_brain", None)
        if brain is not None:
            sources += [str(g) for g in (getattr(brain, "goals", None) or [])]
            sources += [str(t) for t in (getattr(brain, "traits", None) or [])]
    for sc in (scripts or []):
        if str(getattr(sc, "type", "") or "").lower() in ("enemy", "ai", "npc"):
            sources.append(str(getattr(sc, "description", "") or ""))

    by_role: dict[str, str] = {}
    for text in sources:
        for clause in re.split(r"[,;.]| then | and | but ", text):
            clause = clause.strip()
            if not clause:
                continue
            role = infer_role(clause)
            # infer_role is total and falls back to "idle", so an unmatched
            # clause would claim the idle slot with unrelated prose. Only keep a
            # clause that genuinely names a behaviour.
            if role == "idle" and not re.search(r"idle|wait|rest|stand|hold|dormant",
                                                clause, re.I):
                continue
            by_role.setdefault(role, clause)

    # One role is not a state machine — leave the stateless variant alone.
    if len(by_role) < 2:
        return []
    return [{"name": _state_name_from_clause(clause, role),
             "role": role, "description": clause[:120]}
            for role, clause in by_role.items()]


def _state_name_from_clause(clause: str, role: str) -> str:
    """The brief's own word for this behaviour, as a C# identifier.

    Uses the matched KEYWORD rather than the inflected token from the brief:
    "prowls the gantries" -> Prowl, "charges at full thrust" -> Charge. The
    keyword is already the uninflected stem, so this needs no stemming rules to
    get wrong (an early cut of this stripped "es" and produced `Charg`).

    Falls back to the role name when no keyword can be located, so a state is
    always named something rather than dropped.
    """
    from .micro.ai import _ROLE_KEYWORDS, _KEYWORD_DISPLAY

    keywords = [k for k, r in _ROLE_KEYWORDS if r == role]
    for word in re.findall(r"[A-Za-z]+", clause):
        low = word.lower()
        for k in keywords:
            if k in low:
                name = _KEYWORD_DISPLAY.get(k, k)
                return name[:1].upper() + name[1:]
    return role.title()


def _trigger_options_for_missions(missions) -> dict:
    """schema.missions[].triggers (list[str], unity_schema.py) -> TriggerSchema.actions.

    Previously dropped on the floor: the trigger micro was composed with dimension
    metadata only, so its `actions` always fell back to the single defaulted
    OnTriggerActivate and the brief's own trigger language never reached the code.

    Each authored trigger phrase becomes a SendMessage target that a prompt-authored
    script can implement by name. OnTriggerActivate is ALWAYS kept first: SEAM 7's
    CheckpointSaveRelay listens for exactly that message, so replacing the action
    list rather than extending it would silently break checkpoint saving.
    """
    seen  = {"OnTriggerActivate"}
    names = ["OnTriggerActivate"]
    for mission in (missions or []):
        for phrase in (mission.triggers or []):
            name = to_pascal(str(phrase), "")
            if not name:
                continue
            name = name if name.startswith("On") else f"On{name}"
            # C# method names cap out well before this; a whole sentence as an
            # identifier is unreadable in the generated file either way.
            name = name[:64]
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
    return {"actions": [{"event_name": n, "delay": 0.0, "one_shot": True} for n in names]}


def _bt_options_for_entry(entry) -> dict:
    """schema.behavior_trees (AIBehaviorTree/BehaviorTree/BTNode,
    unity_schema.py:563-581,769-772) -> BehaviorTreeSchema options
    (BTNodeEntry, micro/behavior_tree.py). Direct field-for-field mapping —
    both sides share the same node shape (id/type/children/action)."""
    tree = entry.tree
    return {
        "root": tree.root,
        "nodes": [{
            "id":       n.id,
            "type":     n.type,
            "children": list(n.children),
            "action":   n.action,
        } for n in tree.nodes],
    }


# ── Lighting variant selection ────────────────────────────────────────────────
# The lighting micro owns its own variant selection (src/micro/lighting.py) so
# that unity_generator can pick the SAME variant when baking scene.unity.j2's
# RenderSettings — including for non-composed projects, where this module never
# runs. Re-exported here under the old private name so compose-side callers and
# tests keep a stable import site.

from .micro.lighting import select_variant as _lighting_variant  # noqa: E402
from .micro.rules import steps_from_missions as _rule_steps_for_missions  # noqa: E402
from .micro.board import (board_domain_for_missions as _board_domain_for_missions,  # noqa: E402
                          board_options_for_entities as _board_options_for_entities)


def _mission_goal(mission) -> str:
    """One sentence naming what winning is — across every mission given.

    Built from each mission's own description, or from its objectives when it
    has none — never left blank, because "what counts as winning" is the first
    thing a player needs and the last thing any build has ever stated.

    Takes one MissionSpec or a list. Winning a three-mission project means
    finishing all three, so a goal quoting only the first would name a third of
    the game as the whole of it — under a step list that walks the player
    through every one of them.
    """
    missions = ([m for m in mission if m is not None]
                if isinstance(mission, (list, tuple)) else
                ([mission] if mission is not None else []))
    parts: list[str] = []
    for m in missions:
        desc = str(getattr(m, "description", "") or "").strip()
        if desc:
            parts.append(desc)
            continue
        n = len(getattr(m, "objectives", None) or [])
        parts.append(f"Complete all {n} objective(s) of {m.title}." if n else
                     "Complete the mission.")
    if not parts:
        return "Complete the mission."
    goal = " ".join(parts)
    # One cap for one string, whether it came from one mission or four.
    return goal if len(goal) <= 240 else goal[:237] + "…"


def subsystems_for(
    game_type:    str,
    features:     list[str] | None = None,
    locomotion:   str               = "walk",
    locomotion_modes: list[str] | None = None,   # Control Surface Stack — see below
    flying_abilities: list[str] | None = None,   # schema.meta["flying_abilities"] — see unity_archetypes.py
    capabilities  = None,            # Optional[ProjectCapabilities] — avoids import cycle
    user_prompt:  str               = "",
    *,
    scenes:       list[str] | None  = None,
    entities      = None,            # list of schema entities (have .kind)
    scripts       = None,            # list of schema scripts  (have .type/.name)
    genre:        str               = "universal",   # schema.meta["genre"] — see unity_archetypes.py
    audio_specs   = None,            # schema.audio_specs: list[AudioSpec]
    vfx_specs     = None,            # schema.vfx: list[VfxSpec]
    shaders       = None,            # schema.shaders: list[ShaderSpec]
    animations    = None,            # schema.animations: list[AnimationSpec]
    characters    = None,            # schema.characters: list[CharacterSpec]
    missions      = None,            # schema.missions: list[MissionSpec]
    behavior_trees = None,           # schema.behavior_trees: list[AIBehaviorTree]
    environment    = None,           # schema.environment: Optional[EnvironmentSpec]
    visual_style:  str              = "stylized",   # schema.visual_style
    palette_rgb:   dict | None      = None,         # {slot: {r,g,b}} — see unity_generator
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
    # A game that declared it has no avatar (locomotion='none' — see
    # unity_schema.is_embodied). Read once, here, because it changes the CORE
    # set and not just an option: seven of the always-on subsystems below exist
    # to move, animate, collide with or keep alive a player object that this
    # project does not have.
    ui_first = locomotion == "none"

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
        "2d": "2d_platformer", "2.5d": "platformer_2_5d", "top_down": "top_down",
        "2d_topdown": "2d_topdown", "isometric": "top_down", "vr": "first_person",
    }.get(gt, ctrl_default)
    cam = {
        "2d": "follow_2d", "2.5d": "follow_2_5d", "top_down": "cinemachine_topdown",
        "2d_topdown": "follow_2d", "isometric": "cinemachine_topdown",
    }.get(gt, cam_default)

    # Locomotion overrides for 3D-ish game types. "vr" belongs here too — a VR
    # scuba/flight game is a legitimate combination; excluding it meant a
    # correctly-classified VR+swim project silently kept the "first_person"
    # walking controller instead of "swimming" (hit live: an underwater VR
    # project never got its swim controller even after game_type/locomotion
    # were both detected correctly).
    # A wheelless craft still has locomotion "drive" (it is a ground vehicle),
    # but a WheelCollider rig is wrong for it: there are no wheels to spin and
    # the model carries no wheel bones, so the car sinks to its axles and the
    # bone driver has nothing to drive. Content-gated on the project's own
    # words, same as the optional micros below.
    _hover_words = ("hover", "hovercar", "hovercraft", "wheelless", "wheel-less",
                    "speeder", "antigrav", "anti-grav", "repulsor", "maglev",
                    "levitat", "floats above", "floating above")
    is_hover = any(w in prompt_low for w in _hover_words)

    # A two-wheeled vehicle still has locomotion "drive", but the 4-WheelCollider
    # car rig is the wrong archetype for it — no lean, four contact patches
    # instead of two, a steering-wheel-shaped input model for something the
    # brief explicitly says has none. Content-gated the same way as is_hover:
    # a brief this specific about "motocross"/"lean angle" deserves the variant
    # that actually exists for it (src/micro/controller.py's vehicle_bike).
    _bike_words = ("motocross", "motorcycle", "dirt bike", "dirtbike",
                   "mx bike", "quad bike", "moped", "scooter", "chopper",
                   "sportbike", "sport bike", "two-wheeler", "two wheeler",
                   "lean angle", "leaning into", "countersteer")
    is_bike = any(w in prompt_low for w in _bike_words)

    if gt in ("3d", "top_down", "isometric", "vr"):
        loco_ctrl = {"drive": "vehicle_car", "fly": "flying", "swim": "swimming"}
        loco_cam  = {"drive": "vehicle_chase"}
        if locomotion in loco_ctrl:
            ctrl = loco_ctrl[locomotion]
            if locomotion == "drive" and is_hover:
                ctrl = "vehicle_hover"
            elif locomotion == "drive" and is_bike:
                ctrl = "vehicle_bike"
            cam  = loco_cam.get(locomotion, "follow")

    # Control Surface Stack — a runtime-switchable multi-mode controller
    # (docs/CONTROL_SURFACE_STACK.md). Below 2 distinct modes this whole
    # branch is skipped and every line above/below behaves exactly as it did
    # before this feature existed — that skip IS the backward-compatibility
    # guarantee, not just a claim about one. `_multi_mode_kinds` stays a
    # locals-only set the mass/kinematic logic below reads; it does not touch
    # `ctrl_opts["modes"]` here — ControllerSchema.modes wants
    # {"kind": ...} dicts, built below once ctrl_opts exists.
    _multi_mode_kinds: set[str] = set()
    if len(locomotion_modes or []) >= 2:
        _multi_mode_kinds = set(locomotion_modes or [])
        ctrl, cam = "multi_mode", "follow"

    # No avatar: the cursor is the controller and the camera frames the board.
    # Overrides the game_type table above rather than sitting inside it — a
    # non-embodied game can be declared 2D, top-down or 3D, and none of those
    # dimensions makes a walk controller correct for it. This is the whole
    # first half of the fix: CodeVault shipped a 2d_topdown walk rig, a follow
    # camera hunting a Player tag that was never going to exist, a Rigidbody2D
    # and a humanoid animator, for a game whose own concept said it excluded
    # locomotion.
    if ui_first:
        ctrl, cam = "pointer", "board_framing"

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

    # Control Surface Stack registry — ControllerSchema.modes wants
    # {"kind": ...} dicts; default_mode is the FIRST registered kind (the
    # order locomotion_modes was declared in). ControllerMode.default_fix
    # (src/micro/controller.py) is what actually validates/prunes this —
    # here it's just a deterministic {kind} list, no per-mode tuning.
    if ctrl == "multi_mode":
        # flying_abilities defaults onto the "flying" entry specifically —
        # src/unity_archetypes.py._score_flying_abilities already gated this
        # on "flying" having independently cleared the multi_mode kind bar,
        # so no further condition is needed here; every other kind's dict
        # stays the bare {"kind": ...} it always was.
        ctrl_opts["modes"] = [
            {"kind": k, "abilities": list(flying_abilities or [])}
            if k == "flying" else {"kind": k}
            for k in (locomotion_modes or [])
        ]
        ctrl_opts["default_mode"] = (locomotion_modes or [""])[0]

    # A shooter needs the shoot ability or the whole weapon half of the
    # controller (fire, ammo, reload, the aim raycast) is templated out and the
    # player ends up holding nothing — the gun becomes a loose world prop. The
    # capability block above only ever speaks jump/sprint/crouch, so nothing
    # else can turn this on. Content-gated the same way the optional micros are:
    # the detected genre, or an actual weapon entity in the schema.
    if genre == "fps" or "weapon" in entity_kinds:
        _abils = list(ctrl_opts.get("abilities", ["jump", "sprint"]))
        if "shoot" not in _abils:
            _abils.append("shoot")
        ctrl_opts["abilities"] = _abils

    # Animator variant from locomotion
    anim_variant = {
        "drive": "vehicle", "fly": "creature", "swim": "creature",
    }.get(locomotion, "humanoid")

    # Input variant must align with the CONTROLLER, not raw locomotion: a wheel
    # reader's OnMove is inert on a character (2d_platformer/etc.) controller, so
    # the player can't move. Only a vehicle controller gets the wheel.
    input_variant = ("pointer" if ctrl == "pointer"
                     else "gamepad" if gt == "vr"
                     else "racing_wheel" if ctrl.startswith("vehicle_")
                     else "keyboard_mouse")

    # Physics body must match the controller's dimension: a 2D game uses a
    # Rigidbody2D (the prior "rigidbody" was invalid and clamped to 3D, which
    # then fought the 2d_platformer controller's Rigidbody2D). 2.5D's
    # "platformer_2_5d" controller is CharacterController-based (real 3D), so
    # it takes the 3D physics body. "2d_topdown" is Rigidbody2D too (its
    # controller sets gravityScale=0 for free 2-axis movement) — only these
    # two flat-2D controllers use Rigidbody2D.
    phys_variant = "rigidbody_2d" if gt in ("2d", "2d_topdown") else "rigidbody_3d"

    # Physics body mass must match a vehicle controller's own mass, or the
    # two Awake()s race for the same Rigidbody.mass and whichever runs last
    # wins nondeterministically. Live-caught on RustbeltDepthTestbed: the
    # physics micro's generic mass=1.0 default sometimes won over the
    # vehicle_car controller's own 1200f, leaving a 1200kg-tuned suspension
    # (35000 N/m springs) acting on a 1kg body — the car launched itself off
    # the map on the very first ground contact. Matches Controller.cs.j2's
    # own `variant.startswith("vehicle_")` mass (1200f); not a general
    # cross-micro mass resolver, just this one already-known collision.
    # A multi_mode registry with a vehicle family present needs the same
    # 1200kg agreement as a plain vehicle_* controller (see the comment
    # above) — the multi_mode class builds and drives its own Rigidbody for
    # that mode exactly like vehicle_car does. A "flying" entry drives that
    # same shared Rigidbody too (AddForce, not WheelColliders) — its own
    # Awake() (ControllerMultiMode.cs.j2/flying_awake) only sets mass itself
    # when NO vehicle_car entry is present, deferring to the car's tuned
    # 1200kg otherwise, so the physics micro must mirror that same choice or
    # the two Awake()s race exactly like the vehicle-only case above.
    _multi_mode_has_vehicle = bool(_multi_mode_kinds & {"vehicle_car"})
    _multi_mode_has_flying  = bool(_multi_mode_kinds & {"flying"})
    if ctrl.startswith("vehicle_") or _multi_mode_has_vehicle:
        phys_opts: dict = {"mass": 1200.0}
    elif _multi_mode_has_flying:
        phys_opts = {"mass": 2000.0}
    else:
        phys_opts = {}

    # A CharacterController-based controller does its OWN swept-collision movement
    # via CharacterController.Move() and adds its own CharacterController in Awake
    # (Controller.cs.j2's `else` branch: everything that is not 2d_*/vehicle_*/
    # aircraft). Pairing that with the always-on NON-kinematic rigidbody_3d body
    # puts a dynamic Rigidbody on the same GameObject, so the physics solver ALSO
    # integrates gravity/forces every FixedUpdate and the two movers fight
    # (jitter / tunneling / unexpected shove). Keep the body — it still sets the
    # player's layer/tag and its Rigidbody is what makes the player fire
    # OnTriggerEnter — but make it KINEMATIC so the CharacterController is the sole
    # mover. vehicle_*/aircraft controllers instead DRIVE a shared non-kinematic
    # Rigidbody (they AddForce/WheelCollider it), so those must stay dynamic; the
    # 2D controllers use a Rigidbody2D (rigidbody_2d) and never a CharacterController.
    _ctrl_is_character_controller = not (
        ctrl.startswith("2d_") or ctrl.startswith("vehicle_") or ctrl == "aircraft")
    if ctrl == "multi_mode":
        # multi_mode's OWN Awake() re-asserts _rb.isKinematic on every switch
        # (ControllerMultiMode.cs.j2's ApplyMode) — only the INITIAL agreement
        # with the physics micro's own Awake() matters here, so this mirrors
        # whichever mode the registry actually starts in.
        _default_mode = (locomotion_modes or [""])[0]
        _ctrl_is_character_controller = _default_mode not in ("vehicle_car", "flying")
    if phys_variant == "rigidbody_3d" and _ctrl_is_character_controller:
        phys_opts["is_kinematic"] = True

    # 2.5D's tilted/elevated follow camera needs a non-default offset (matches
    # the static camera preset already used for non-composed 2.5D projects —
    # see _CAMERA_PRESETS["2.5D"] in unity_generator.py: y=3, z=-8, ~21 deg
    # down-tilt). Every other camera variant is fine with CameraRigSchema's
    # defaults.
    cam_opts: dict = {"offset_y": 3.0, "offset_z": -8.0} if cam == "follow_2_5d" else {}

    # trigger/interactable need to know the project's dimension so their
    # generated scripts author Collider2D + OnTrigger*2D callbacks instead of
    # the 3D Physics API — a Collider2D never raises OnTriggerEnter(Collider).
    #
    # A UI board reports 3D whatever the project's game_type says, and that is
    # not a lie about the project: the board IS a flat plane of 3D colliders,
    # laid out by _layout_ui_board and spawned by EntitySpawner's generic
    # branch, and the pointer picks it with Physics.Raycast. Collider2D has no
    # Raycast(Ray) overload at all, so a 2d_topdown teaching sim emitting the
    # 2D API here would compile and then never register a single click. Only
    # these two micros read dim_meta; everything else about the project's
    # dimension (orthographic camera, sprite loading) is untouched.
    dim_meta = {"meta": {"game_type": "3D" if ui_first else (game_type or "3D")}}

    # schema.animations + CharacterSpec.animation_set -> AnimatorSchema.clips.
    # Additive: empty dict when neither field is populated, so the always-on
    # "animator" core subsystem stays byte-identical to today's output and the
    # GOLDEN_*_CORE_SIGNATURE (kind, variant) checks are unaffected.
    anim_clips = _animator_clips_from_specs(animations, characters)
    anim_opts: dict = {"clips": anim_clips} if anim_clips else {}

    # Lighting: variant from the project's own interior/exterior + time-of-day
    # signals; palette passed through so the fill/rim lights and the ambient
    # gradient are the project's real colors, not a generic grey (the fixer
    # refuses to invent a look — see LightingPipeline._enforce_lit).
    _tod = str(getattr(environment, "time_of_day", "") or "")
    light_variant = _lighting_variant(
        gt, visual_style=visual_style, time_of_day=_tod,
        explicit_env=environment is not None, user_prompt=prompt_low, scenes=scenes)
    light_opts: dict = {"meta": {"game_type": game_type or "3D"}}
    if palette_rgb:
        light_opts["meta"]["palette_rgb"] = palette_rgb

    # A UI game has no player to take damage, so it gets no health micro (see
    # the core list below) — and health_bar's whole layout is the bar SEAM 1
    # feeds from it. score_panel is the honest default for a board game: a
    # readout the objectives seam (SEAM 9) can publish into.
    hud_variant = ("score_panel" if ui_first
                   else "racing_hud" if genre == "racing"
                   else "health_bar")
    # Evaluated HERE, not at the inventory append site below, because the HUD's
    # "Carried" readout may only exist if there is an inventory to publish into
    # it (SEAM 10). Same condition, one source.
    has_inventory = ("inventory" in feats or "inventory" in prompt_low
                     or any("inventory" in t for t in script_types))
    hud_opts: dict = {"widgets": _hud_widgets_for_project(
        hud_variant=hud_variant, missions=missions, has_inventory=has_inventory)}

    # ── The core set ─────────────────────────────────────────────────────────
    # `target="player"` is not a label: it is what makes the scene template
    # build a Player GameObject at all (scene.unity.j2's player_units block,
    # and the generator's _has_player_object). A non-embodied project must not
    # emit ONE unit targeting "player", or the compiler builds the avatar the
    # schema declared it does not have — which is the exact contradiction this
    # archetype exists to remove. Its controller rides the systems holder.
    _ctrl_target = "systems" if ui_first else "player"
    subs: list[Subsystem] = [
        # Core (always)
        Subsystem(kind="controller",  variant=ctrl,           target=_ctrl_target,
                  name="PointerController" if ui_first else "PlayerController", options=ctrl_opts),
        Subsystem(kind="camera",      variant=cam,            target="camera",  name="PlayerCamera", options=cam_opts),
        Subsystem(kind="health",      variant="simple",       target="player",  name="PlayerHealth"),
        # Racing games get the race-feedback HUD (speed/lap/position/coins/boost
        # meter/minimap + countdown banner) instead of the health-bar default.
        # Same genre signal the AI/controller/input lines already act on — a
        # racing prompt classified genre="racing" by unity_archetypes.py. The
        # health_bar HUD is coupled to the always-on health micro (see SEAM 1
        # below); racing_hud is event-driven, resolved via GameContext.Get<T>().
        # `widgets` is what makes either variant show THIS project's readouts
        # rather than only its prebuilt layout (SEAM 1/9/10 publish into them).
        Subsystem(kind="hud",         variant=hud_variant,
                  target="ui",      name="GameHud", options=hud_opts),
        Subsystem(kind="menu",        variant="pause_menu",   target="ui",      name="PauseMenu", options={"backend": "uGUI"}),
        Subsystem(kind="saveload",    variant="json_file",    target="systems", name="SaveSystem"),
        # Always-on: physics body, input reader, interactables, animator, triggers
        Subsystem(kind="physics",     variant=phys_variant,   target="player",  name="PhysicsBody", options=phys_opts),
        Subsystem(kind="input",       variant=input_variant,  target="systems", name="InputReader"),
        # A UI game's interactables are CLICKED, not walked into: `pickup` +
        # trigger detection waits for a Player-tagged collider to enter, so on a
        # board every one of them stays inert forever — and SEAM 8 would wire a
        # collect/interact objective to an event that can never fire.
        Subsystem(kind="interactable",
                  variant="button_switch" if ui_first else "pickup",
                  target="world",   name="WorldInteractable",
                  options={**dim_meta,
                           **({"detection": "pointer", "prompt_text": "Click",
                               "radius": 1.0} if ui_first else {})}),
        Subsystem(kind="animator",    variant=anim_variant,   target="player",  name="PlayerAnimator", options=anim_opts),
        Subsystem(kind="statemachine",variant="player_fsm",   target="player",  name="PlayerFSM"),
        # dim_meta (game_type, for the 2D/3D collider split) PLUS the brief's own
        # trigger phrases from schema.missions[].triggers — see
        # _trigger_options_for_missions. Merge order matters: the mission actions
        # must not clobber the dimension metadata the template branches on.
        # click_zone is the same trigger reached by a pointer instead of a body,
        # which is what keeps SEAM 7 (checkpoint save) and SEAM 8's `reach`
        # objective source alive in a game with nothing to walk in.
        Subsystem(kind="trigger",
                  variant="click_zone" if ui_first else "zone_enter",
                  target="world",   name="SceneTrigger",
                  options={**dim_meta, **_trigger_options_for_missions(missions)}),
        # Always-on: the light rig AND the environment-lighting guard. Not a
        # "nice to have" — scene.unity.j2 ships one directional light and an
        # AmbientMode.Skybox pointing at the built-in Default-Skybox, and the
        # only existing repair for that is gated behind an editor-only
        # EditorPrefs one-shot in SetupRenderPipeline.cs.j2. Runtime-built
        # materials (EntitySpawner, MaterialPalette) have no GI contribution,
        # so with ambient at zero they render black on Play. This micro is what
        # makes "the scene is lit" an invariant instead of an accident.
        Subsystem(kind="lighting",    variant=light_variant,  target="world",   name="LightingDirector",
                  options=light_opts),
    ]

    # A non-embodied game drops every core subsystem that rides the avatar.
    # `target == "player"` is exactly that test and needs no second list to
    # maintain: it is already the thing that makes the scene template build a
    # Player GameObject, so a unit targeting it in a game with no avatar is a
    # MonoBehaviour attached to an object that is never created. Health,
    # PhysicsBody, PlayerAnimator and PlayerFSM all qualify — taking damage,
    # colliding, playing a walk cycle, being in the Idle state.
    #
    # Filtered HERE, in one place, rather than gated at each line above, for
    # the two reasons the playability evaluator filters _EMBODIED_ONLY_CHECKS
    # the same way: the embodied list keeps its exact ORDER (plan() assigns
    # file ids by position, and the golden core-signature tests read them), and
    # a core subsystem added later cannot forget to opt out.
    #
    # It runs BEFORE the feature-driven additions below on purpose — those are
    # world-targeted (enemy AI, spawners, the enemy FSM), and an enemy in a
    # board game is a legitimate thing that never needed a player object.
    if ui_first:
        dropped = [s for s in subs if s.target == "player"]
        subs = [s for s in subs if s.target != "player"]
        console.print(
            "  [green]OK[/]    ui-first: locomotion='none' — composed a pointer "
            "controller and a board-framing camera; dropped "
            + ", ".join(sorted(s.kind for s in dropped))
            + " (each one rides an avatar this game declares it has none of)")

    # Feature- AND content-driven additions
    if has_enemies:
        # locomotion=="drive" alone is not "this is a racing game" — it only
        # means the PLAYER gets around by car. RustbeltDepthTestbed drives a
        # scrap cart in a survival-scavenging game (genre="universal") whose
        # concept explicitly wants sentry drones that patrol between two
        # points and chase within a detection radius; locomotion=="drive"
        # routed them to "waypoint_follow" (a race-rival AI that follows a
        # "Waypoints" container only path-placement entities ever emit) —
        # the drones had no waypoints to follow and just sat inert. Gate on
        # the actual racing-game signal (genre, from the keyword classifier
        # in unity_archetypes.py) instead.
        # navmesh_patrol drives a NavMeshAgent, which needs a baked NavMesh and
        # is 3D-ground-only. Enemies the project describes as FLYING — floating
        # heads adrift in space, drones, hovering wraiths — have no ground to
        # bake and no NavMesh to follow, so they spawn and sit there inert
        # (hit live on CromulonSkyfall: eight giant floating heads that never
        # moved). flying_swarm seeks the player kinematically in full 3D, which
        # is what "hovers in and rams you" actually needs.
        _flying_enemy_words = (
            "floating", "float ", "floats", "hover", "hovering", "drifting",
            "drifts", "flying", "airborne", "levitat", "adrift", "in space",
        )
        enemies_fly = (locomotion == "fly"
                       or any(w in prompt_low for w in _flying_enemy_words))
        if genre == "racing":
            ai_var, ai_tgt = "waypoint_follow", "asset"
        elif enemies_fly:
            ai_var, ai_tgt = "flying_swarm", "world"
        else:
            ai_var, ai_tgt = "navmesh_patrol", "world"
        # An enemy the brief actually DESCRIBES gets a state machine instead of a
        # stateless patroller: two or more distinct behaviour roles in
        # EntitySpec.behavior / CharacterSpec.npc_brain upgrade the variant to
        # fsm_guard and become its real, named states. Racing rivals and flying
        # swarms keep their own movement variants — their behaviour is the
        # movement, and fsm_guard has neither a waypoint follower nor a hover rig.
        ai_opts: dict = dict(dim_meta)
        ai_states = _ai_states_from_brief(entities, characters, scripts)
        if ai_states and ai_var == "navmesh_patrol":
            ai_var = "fsm_guard"
            ai_opts["states"] = ai_states
            console.print(
                "  [green]OK[/]    ai: brief describes "
                + ", ".join(f"{s['name']}({s['role']})" for s in ai_states)
                + " — composing fsm_guard instead of navmesh_patrol"
            )
        subs.append(Subsystem(kind="ai",           variant=ai_var,          target=ai_tgt,    name="EnemyAI", options=ai_opts))
        subs.append(Subsystem(kind="spawner",      variant="wave_spawner",  target="world",   name="EnemySpawner"))
        subs.append(Subsystem(kind="statemachine", variant="enemy_fsm",     target=ai_tgt,    name="EnemyFSM"))
    if "audio" in feats or audio_specs:
        # Pass user_prompt via meta so AudioPipeline.default_fix can derive the game
        # type from the original request (e.g. "horror maze" → horror required cues
        # with ambient+music that route to the generative backend, not DSP).
        audio_opts: dict = {}
        if user_prompt:
            audio_opts["meta"] = {"user_prompt": user_prompt}
        if audio_specs:
            # Real, LLM-authored cues. default_fix only fills GAPS in schema.cues
            # (src/micro/audio.py:264-284) so these are never overwritten.
            audio_opts["cues"] = _audio_cues_from_specs(audio_specs)
        subs.append(Subsystem(kind="audio", variant="categorical_mixer", target="systems",
                              name="AudioDirector", options=audio_opts))
    if has_inventory:   # hoisted above — the HUD's "Carried" widget shares it
        subs.append(Subsystem(kind="inventory",    variant="slot_based",    target="systems", name="PlayerInventory"))
    # Dialogue — one subsystem per character with real authored dialogue
    # (CharacterSpec.dialog_graph), following the same per-role duplication
    # pattern as EnemyAI/PlayerFSM above. Voiced lines (any DialogNode.audio_ref
    # set) route to NpcDialogPipeline for real Kokoro TTS clips instead of the
    # plain-text DialoguePipeline tree. Falls back to a single generic
    # DialogueSystem (requested, VR, an authored dialogue_manager, or NPCs
    # exist) only when no character supplies real dialogue — avoids a
    # redundant empty-default tree alongside real per-character ones.
    characters_with_dialogue = [
        c for c in (characters or []) if c.dialog_graph and c.dialog_graph.nodes
    ]
    for char in characters_with_dialogue:
        char_label = to_pascal(char.name, "Character")
        if _has_voiced_lines(char):
            subs.append(Subsystem(kind="npc_dialog", variant="quest_giver", target="world",
                                  name=f"{char_label}NpcDialog",
                                  options={"speaker_name": char.name,
                                           "lines": _npc_dialog_lines_for_character(char)}))
        else:
            subs.append(Subsystem(kind="dialogue", variant="linear", target="systems",
                                  name=f"{char_label}Dialogue",
                                  options=_dialogue_options_for_character(char)))
    if not characters_with_dialogue and (
            "dialogue" in feats or gt in ("vr",) or "dialogue_manager" in script_types or has_npcs):
        subs.append(Subsystem(kind="dialogue",     variant="linear",        target="systems", name="DialogueSystem"))
    # Decorative impact effects — object-pooled, event-driven (OnTrigger/OnCollision),
    # self-contained (no GameWiring seam). impact_fx null-guards an unassigned prefab.
    # Reachable whenever there's combat (enemies), effects are requested, or the
    # schema authored real vfx.VfxSpec entries — those drive the variant/options
    # instead of the bare impact_fx default.
    if {"vfx", "effects", "particles"} & feats or has_enemies or vfx_specs:
        vfx_opts = _vfx_options_from_specs(vfx_specs) if vfx_specs else {"variant": "impact_fx"}
        vfx_variant = vfx_opts.pop("variant", "impact_fx")
        subs.append(Subsystem(kind="vfx", variant=vfx_variant, target="world",
                              name="ImpactVFX", options=vfx_opts))
    # Runtime shader/material controllers — one per authored schema.shaders
    # entry (MaterialPropertyBlock property driver, never the shared asset).
    # Unlike vfx there's no "sequence" merge: each ShaderSpec drives its own
    # object's property independently, so it gets its own Subsystem, same
    # shape as the per-mission/per-behavior_tree loops below. No authored
    # specs but the feature is requested -> one default color_cycle controller.
    if shaders:
        for spec in shaders:
            shader_label = to_pascal(spec.name, "Shader")
            shader_opts = _shader_options_from_specs(spec)
            shader_variant = shader_opts.pop("variant", "color_cycle")
            subs.append(Subsystem(kind="shader", variant=shader_variant, target="world",
                                  name=f"{shader_label}Controller", options=shader_opts))
    elif {"shader", "shaders", "material", "materials"} & feats:
        subs.append(Subsystem(kind="shader", variant="color_cycle", target="world",
                              name="ShaderController"))
    # Spoken NPCs (Kokoro TTS). skip_audio is the DEFAULT (a bare MicroComposer.emit
    # stays TTS-free — no generic preset clips), but the main pipeline LIFTS it:
    # unity_pipeline.run pre-extracts concept-specific lines while LM Studio is
    # loaded, injects them via meta._pre_lines AND pops skip_audio before the SA3
    # unload, so `unity-create --compose` renders REAL, concept-driven Kokoro clips.
    # The speaker name is derived from the first NPC entity so clips/wiring aren't "NPC".
    # The <name>Player MonoBehaviour is the attach class.
    if {"npc_dialog", "voiced_npcs", "npcs"} & feats or has_npcs:
        npc_name = "NPC"
        for _e in (entities or []):
            if str(getattr(_e, "kind", "") or "").lower() == "npc":
                npc_name = str(getattr(_e, "name", "") or "NPC") or "NPC"
                break
        subs.append(Subsystem(kind="npc_dialog",   variant="quest_giver",   target="world",   name="NpcDialog",
                              options={"speaker_name": npc_name, "meta": {"skip_audio": True}}))
    # Scene transitions — meaningful for any multi-scene project (deterministic: > 1 scene).
    if {"levels", "multi_scene", "scene_transition"} & feats or scene_count > 1:
        subs.append(Subsystem(kind="transition",   variant="fade",          target="systems", name="SceneTransition"))
    # Mission objectives — one tracker subsystem per authored MissionSpec with
    # real objectives (schema.missions). No feature-flag gate: presence of
    # authored objectives is itself the signal, same as audio_specs/vfx above.
    for mission in (missions or []):
        if not mission.objectives:
            continue
        subs.append(Subsystem(kind="objectives", variant="tracker", target="systems",
                              name=f"{to_pascal(mission.title, 'Mission')}Objectives",
                              options=_objectives_options_for_mission(mission)))
    # ── The board layer ──────────────────────────────────────────────────────
    # The MECHANIC of a game with no avatar: the domain model (where every piece
    # is) and the legal-move logic (whether it may go where the player is
    # dragging it), plus the command parser that reaches the same move core.
    #
    # UI-first only, and not by preference: an embodied game's verbs are the
    # controller's, and this one binds to the built board plates that the
    # `ui:<element>` route produces. Composed BEFORE the rules layer so SEAM 8
    # can bind an objective to a real move rather than to any click on any
    # interactable — otherwise the rules micro states an instruction ("drag
    # every file icon into a folder container") that nothing in the build
    # implements, which is the whole defect it was written to close.
    #
    # The mission is read here as well as by the rules layer below, and that is
    # the point: the order the board ENFORCES and the progression the player is
    # SHOWN are then two readings of one authored list, so the hint bar can
    # never name a next action the move core would refuse.
    _missions_with_objectives = [m for m in (missions or []) if m.objectives]
    _first_mission = _missions_with_objectives[0] if _missions_with_objectives else None
    if ui_first:
        # Titled from the same mission the briefing is titled from — a board
        # captioned "Game Board" under a briefing that calls the game something
        # else is two surfaces disagreeing about what the player is playing.
        # The missions are passed as the FALLBACK classifier, not the primary
        # one: the UI vocabulary answers the ordinary cases, and the objectives
        # answer the ones no word list can ("drag the shoes onto the tower").
        #
        # ALL of them, in both calls. SEAM 8a binds an objective from any
        # mission, and the rules micro below now states every one of them as a
        # step, so a cast classified — or a rule derived — from the first
        # mission alone would be reading a third of the evidence for a game the
        # player is being walked through all of.
        _board_opts = _board_options_for_entities(
            entities or [], title=(_first_mission.title if _first_mission else ""),
            mission=_missions_with_objectives)
        if _board_opts:
            for _ok, _msg in _board_domain_for_missions(
                    _board_opts, _missions_with_objectives, brief=user_prompt):
                console.print(f"  [green]OK[/]    {_msg}" if _ok
                              else f"  [yellow]WARN[/]  {_msg}")
            _board_variant = _board_opts.pop("variant")
            subs.append(Subsystem(kind="board", variant=_board_variant,
                                  target="systems", name="GameBoard",
                                  options=_board_opts))
            console.print(
                f"  [green]OK[/]    board: {len(_board_opts['pieces'])} piece "
                f"group(s), {len(_board_opts['zones'])} place(s), "
                f"{_board_variant.replace('_', ' ')} — the moves are real")
        else:
            # Abstain loudly: this game has no avatar AND no board, so nothing
            # composed here owns its verbs.
            console.print(
                "  [yellow]WARN[/]  board: this cast has no movable pieces and "
                "places to move them to (icons/cards + panels), so no legal-move "
                "logic was composed — every objective must find another source")

    # ── The rules layer ──────────────────────────────────────────────────────
    # One per PROJECT, not per subsystem. Everything above is a mechanism; this
    # is the briefing, the step the player is on, and the hint bar that names
    # the next action — the three things whose absence is why a build could
    # compile, pass every structural check, and still leave a human with no
    # idea what to do.
    #
    # Gated on there being a mission with objectives, because the progression
    # IS the objective list: with nothing to sequence there is no game to
    # explain, and a briefing that says "achieve the goal" is the empty
    # scaffolding this exists to replace.
    if _first_mission is not None:
        _ms = _missions_with_objectives
        _steps = _rule_steps_for_missions(_ms)
        subs.append(Subsystem(
            kind="rules", variant="guided", target="ui", name="GameRules",
            options={
                # The project's own name for itself is the first mission's
                # title, which is also what the board is captioned with — two
                # player-facing surfaces that must not disagree.
                "game_title": _first_mission.title or "This Game",
                "goal": _mission_goal(_ms),
                "steps": _steps,
                # The ids a step is allowed to reference — from EVERY mission,
                # because the steps now come from every mission. `harden`
                # reports any step pointing at an id the project does not
                # declare: such a step can never clear and strands the player.
                "meta": {"objective_ids": [o.id for m in _ms
                                           for o in m.objectives if o.id]},
            }))
        console.print(
            f"  [green]OK[/]    rules: {len(_steps)} step(s) from "
            + (f"{len(_ms)} missions ("
               + ", ".join(repr(m.title) for m in _ms) + ")" if len(_ms) > 1
               else f"mission {_first_mission.title!r}")
            + " — briefing, step panel and next-action hint bar")

    # Behavior trees — one interpreter subsystem per authored AIBehaviorTree
    # entry (schema.behavior_trees). CharacterSpec.npc_brain.behavior_tree (a
    # second, per-character embedding of the same BehaviorTree type) is left
    # unwired for now — a small, separate follow-up.
    for bt_entry in (behavior_trees or []):
        if not bt_entry.tree or not bt_entry.tree.nodes:
            continue
        subs.append(Subsystem(kind="behavior_tree", variant="tree", target="world",
                              name=f"{to_pascal(bt_entry.name, 'BehaviorTree')}BT",
                              options=_bt_options_for_entry(bt_entry)))
    return subs
