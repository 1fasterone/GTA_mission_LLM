"""
Capability resolver — Step 5 helper.

Reads schema.capabilities and produces:
  1. A concrete AtomInvocation list seeded from typed capability params
  2. The list of microgen kinds that should run (for --compose)
  3. A migration function for v1 (data-first) → v2 (behavior-first) schema JSON

The graph this closes:
    Schema.capabilities  →  capability_resolver  →  AtomInvocations
    AtomInvocations      →  generator / microgen  →  Unity C#

This is deterministic (no LLM) and runs inside Step 5 of the Unity pipeline.
"""

from __future__ import annotations

from typing import Any, Callable

from .unity_schema import (
    UnitySchema, ProjectCapabilities,
    ControllerCapability, PhysicsCapability,
    HudCapability, ModelCapability, InputCapability,
    AnimationCapability, DialogCapability, AICapability, VfxCapability,
)
from .atoms.base import AtomInvocation


# ── Atoms from capabilities ────────────────────────────────────────────────────

def _controller_atoms(c: ControllerCapability) -> list[AtomInvocation]:
    atoms: list[AtomInvocation] = []
    # Look always comes first — sets up camera/mouse rotation before movement
    atoms.append(AtomInvocation(atom="Look", args={
        "pitch": 0.0, "yaw": 0.0, "sensitivity": c.look_sensitivity,
    }))
    if c.movement:
        atoms.append(AtomInvocation(atom="Move", args={
            "dir_x": 0.0, "dir_z": 1.0, "speed": c.speed,
        }))
    if c.sprint:
        atoms.append(AtomInvocation(atom="Sprint", args={
            "active": True, "multiplier": c.sprint_multiplier,
        }))
    if c.jump:
        atoms.append(AtomInvocation(atom="Jump", args={"force": c.jump_force}))
    if c.crouch:
        atoms.append(AtomInvocation(atom="Crouch", args={
            "active": False, "height_scale": 0.55,
        }))
    return atoms


def _physics_atoms(p: PhysicsCapability) -> list[AtomInvocation]:
    atoms: list[AtomInvocation] = [
        AtomInvocation(atom="SetGravity", args={"enabled": p.gravity, "scale": 1.0}),
    ]
    if p.collision in ("dynamic", "kinematic"):
        atoms.append(AtomInvocation(atom="ToggleCollision", args={"enabled": True}))
    elif p.collision == "none":
        atoms.append(AtomInvocation(atom="ToggleCollision", args={"enabled": False}))
    return atoms


def _hud_atoms(h: HudCapability) -> list[AtomInvocation]:
    atoms: list[AtomInvocation] = [
        AtomInvocation(atom="ShowHUD", args={"duration": 0.0}),
    ]
    if h.show_health:
        atoms.append(AtomInvocation(atom="UpdateHealth", args={
            "value": 100.0, "max_value": 100.0,
        }))
    if h.show_ammo:
        atoms.append(AtomInvocation(atom="UpdateAmmo", args={
            "current": 30, "reserve": 90,
        }))
    if h.show_objective:
        atoms.append(AtomInvocation(atom="UpdateObjective", args={
            "text": "Reach the objective", "show_marker": True,
        }))
    return atoms


def _input_atoms(inp: InputCapability) -> list[AtomInvocation]:
    atoms: list[AtomInvocation] = []
    for b in inp.bindings:
        event = b.event.strip() or f"On{b.action.strip().capitalize()}"
        atoms.append(AtomInvocation(atom="BindAction", args={
            "action_name": b.action,
            "event_name":  event,
        }))
    return atoms


def _animation_atoms(a: AnimationCapability) -> list[AtomInvocation]:
    atoms: list[AtomInvocation] = []
    for state, clip in a.clips.items():
        atoms.append(AtomInvocation(atom="SetAnimationState", args={
            "param": clip, "value": "true", "param_type": "trigger",
        }))
    return atoms


def _dialog_atoms(d: DialogCapability) -> list[AtomInvocation]:
    atoms: list[AtomInvocation] = []
    if d.system != "simple":
        atoms.append(AtomInvocation(atom="InitDialogSystem", args={"system": d.system}))
    if d.bark_on_sight:
        atoms.append(AtomInvocation(atom="BarkOnSight", args={"enabled": True}))
    return atoms


def _ai_atoms(a: AICapability) -> list[AtomInvocation]:
    atoms: list[AtomInvocation] = []
    if a.pathfinding:
        atoms.append(AtomInvocation(atom="EnableNavMesh", args={"enabled": True}))
    atoms.append(AtomInvocation(atom="SetAIBehavior", args={
        "behavior": a.behavior, "detection_range": a.detection_range,
    }))
    return atoms


def _vfx_atoms(v: VfxCapability) -> list[AtomInvocation]:
    atoms: list[AtomInvocation] = []
    if v.particles:
        atoms.append(AtomInvocation(atom="EnableParticles", args={"enabled": True}))
    if v.post_processing:
        atoms.append(AtomInvocation(atom="EnablePostProcessing", args={"enabled": True}))
    return atoms


def _model_atoms(m: ModelCapability) -> list[AtomInvocation]:
    atoms: list[AtomInvocation] = [
        AtomInvocation(atom="SetModel", args={"model_path": "Models/Default"}),
    ]
    if m.lod not in ("auto", "disabled"):
        try:
            level = int(m.lod)
        except ValueError:
            level = 0
        atoms.append(AtomInvocation(atom="SetLOD", args={"level": level}))
    for state, clip in m.animations.items():
        atoms.append(AtomInvocation(atom="SetAnimationState", args={
            "param":      clip,
            "value":      "true",
            "param_type": "trigger",
        }))
    return atoms


# ── Public API ─────────────────────────────────────────────────────────────────

# Capability → atom builder, in the order the atoms fire at scene start.
_CAP_ATOM_BUILDERS: tuple[tuple[str, Callable[[Any], list[AtomInvocation]]], ...] = (
    ("controller", _controller_atoms),
    ("physics",    _physics_atoms),
    ("hud",        _hud_atoms),
    ("input",      _input_atoms),
    ("model",      _model_atoms),
    ("animation",  _animation_atoms),
    ("dialog",     _dialog_atoms),
    ("ai",         _ai_atoms),
    ("vfx",        _vfx_atoms),
)

# Capabilities whose atoms must NOT be folded into schema.atoms.
#
# schema.atoms is a ONE-SHOT sequence (unity_generator's "Global atom sequencer"
# → GameSequencer.cs, a single Start() coroutine). That is the right shape for
# scene-init actions — ShowHUD, BindAction, EnableNavMesh, SetModel — but the
# wrong shape for controller and physics:
#
#   controller → Look/Move/Sprint/Jump/Crouch are PER-FRAME, input-driven
#     behaviours. Fired once at Start they translate their host by a single
#     frame's delta and then never run again.
#   physics    → SetGravity/ToggleCollision mutate the host's Rigidbody. A
#     capability declaring gravity:false or collision:"none" would disable
#     gravity/collision on whatever object carries the sequencer.
#
# Both are already the composed subsystems' job: subsystems_for(capabilities=…)
# in unity_compose.py feeds ControllerCapability into the controller micro's
# options (move_speed/sprint_speed/jump_force/abilities) and picks the physics
# body variant, and those micros implement the behaviour continuously in
# Update/FixedUpdate. Folding them a second time as one-shot atoms produces a
# sequence that reads like it defines player movement but cannot.
SUBSYSTEM_OWNED_CAPABILITIES: frozenset[str] = frozenset({"controller", "physics"})


def capabilities_to_atoms(
    caps: ProjectCapabilities,
    *,
    exclude: frozenset[str] = SUBSYSTEM_OWNED_CAPABILITIES,
) -> list[AtomInvocation]:
    """Build an ordered atom sequence from declared capability parameters.

    `exclude` defaults to the capabilities the composed subsystems own outright
    (see SUBSYSTEM_OWNED_CAPABILITIES). Pass `exclude=frozenset()` for the raw
    full mapping — every capability's atoms, whatever their shape.
    """
    atoms: list[AtomInvocation] = []
    for name, builder in _CAP_ATOM_BUILDERS:
        if name in exclude:
            continue
        sub = getattr(caps, name)
        if sub:
            atoms.extend(builder(sub))
    return atoms


def capabilities_to_active_microgens(caps: ProjectCapabilities) -> list[str]:
    """Return the microgen kinds that should be activated for these capabilities."""
    kinds: list[str] = []
    if caps.controller: kinds.append("controller")
    if caps.physics:    kinds.append("physics")
    if caps.hud:        kinds.append("hud")
    if caps.input:      kinds.append("input")
    if caps.model:      kinds.append("model")
    if caps.animation:  kinds.append("animator")
    if caps.dialog:     kinds.append("dialogue")
    if caps.ai:         kinds.append("statemachine")
    if caps.vfx:        kinds.append("trigger")
    return kinds


def resolve_capabilities(schema: UnitySchema) -> tuple[UnitySchema, list[str]]:
    """
    Step 5 hook: derive atoms from schema.capabilities and merge into schema.atoms.

    Only SCENE-INIT capabilities are folded. controller/physics are skipped —
    schema.atoms is a one-shot Start() sequence and those two describe continuous
    behaviour the composed subsystems already implement from the same typed
    params (see SUBSYSTEM_OWNED_CAPABILITIES). Capability-derived atoms are
    prepended so they run at scene start (GameSequencer fires them before any
    entity-level atom sequences). Existing schema.atoms are preserved as a suffix.

    Returns (updated_schema, list_of_fix_messages).
    """
    if not schema.capabilities:
        return schema, []

    msgs: list[str] = []
    cap_atoms = capabilities_to_atoms(schema.capabilities)
    if cap_atoms:
        # Prepend: capability atoms fire first (engine init order)
        schema.atoms = cap_atoms + list(schema.atoms)
        # capabilities_to_active_microgens still reports controller/physics —
        # they ARE activated, just by subsystems_for, not by an atom fold.
        kinds = [k for k in capabilities_to_active_microgens(schema.capabilities)
                 if k not in SUBSYSTEM_OWNED_CAPABILITIES]
        msgs.append(
            f"Capabilities resolved: {len(cap_atoms)} scene-init atoms injected "
            f"({', '.join(kinds)})"
        )

    owned = sorted(name for name in SUBSYSTEM_OWNED_CAPABILITIES
                   if getattr(schema.capabilities, name))
    if owned:
        msgs.append(
            f"Capabilities: {'/'.join(owned)} left out of schema.atoms — the "
            f"composed subsystems own that behaviour continuously; a one-shot "
            f"Start() sequence cannot drive it"
        )

    return schema, msgs


def resolve_microgens(schema: UnitySchema) -> tuple[UnitySchema, list[str]]:
    """
    Step 5 hook: fold schema.microgens[].atoms into schema.atoms.

    MicrogensSpec.atoms (unity_schema.py:777-781) is structurally identical to
    what resolve_capabilities() produces above — a list[AtomInvocation] meant
    for the same global atom-coroutine consumer (unity_generator.py's
    "Global atom sequencer" / GameSequencer.cs.j2). Building a second dispatch
    mechanism for MicrogensSpec.kind/.input (invoking micro.REGISTRY pipelines
    programmatically) would duplicate what Subsystem/subsystems_for() already
    does, with no consumer asking for that today — kind/input/output_files
    remain unconsumed metadata (documented, not silently dropped).

    Prepended alongside capability atoms (both fire before entity-level atom
    sequences); existing schema.atoms are preserved as a suffix.

    Returns (updated_schema, list_of_fix_messages).
    """
    if not schema.microgens:
        return schema, []

    msgs: list[str] = []
    mg_atoms = [a for m in schema.microgens for a in m.atoms]
    if mg_atoms:
        schema.atoms = mg_atoms + list(schema.atoms)
        msgs.append(
            f"Microgens resolved: {len(mg_atoms)} atoms injected "
            f"from {len(schema.microgens)} microgen entr"
            f"{'y' if len(schema.microgens) == 1 else 'ies'}"
        )

    return schema, msgs


# ── Migration: v1 (data-first) → v2 (capability-aware) ──────────────────────

# Default binding sets by locomotion style
_WALK_BINDINGS = [
    {"action": "Jump",     "key": "Space"},
    {"action": "Sprint",   "key": "LeftShift"},
    {"action": "Interact", "key": "E"},
    {"action": "Crouch",   "key": "LeftControl"},
]
_DRIVE_BINDINGS = [
    {"action": "Accelerate", "key": "W"},
    {"action": "Brake",      "key": "S"},
    {"action": "HandBrake",  "key": "Space"},
]
_FLY_BINDINGS = [
    {"action": "Throttle",  "key": "W"},
    {"action": "Pitch",     "key": "Mouse1"},
    {"action": "Roll",      "key": "Q"},
]
_SWIM_BINDINGS = [
    {"action": "Ascend",  "key": "Space"},
    {"action": "Descend", "key": "LeftControl"},
]
_DEFAULT_ANIMATIONS = {"idle": "Idle", "walk": "Walk", "run": "Run"}


def migrate_schema_v1_to_v2(schema_dict: dict) -> dict:
    """
    Upgrade a schema.json from data-first (v1) to behavior-first (v2).

    Reads features + locomotion → synthesises a capabilities block.
    Idempotent: if capabilities already exists, returns schema unchanged.
    """
    if "capabilities" in schema_dict:
        return schema_dict

    features   = set(schema_dict.get("features", []))
    locomotion = schema_dict.get("locomotion", "walk")

    caps: dict = {}

    # controller — derived from locomotion
    if locomotion == "walk":
        caps["controller"] = {
            "movement": True, "jump": True,
            "sprint": True,   "crouch": False,
            "look_sensitivity": 2.0, "speed": 6.0,
            "sprint_multiplier": 1.7, "jump_force": 8.0,
        }
    elif locomotion == "drive":
        caps["controller"] = {
            "movement": True,  "jump": False,
            "sprint": False,   "crouch": False,
            "look_sensitivity": 0.0, "speed": 15.0,
            "sprint_multiplier": 1.0, "jump_force": 0.0,
        }
    elif locomotion == "fly":
        caps["controller"] = {
            "movement": True,  "jump": False,
            "sprint": True,    "crouch": False,
            "look_sensitivity": 1.5, "speed": 20.0,
            "sprint_multiplier": 2.0, "jump_force": 0.0,
        }
    elif locomotion == "swim":
        caps["controller"] = {
            "movement": True,  "jump": False,
            "sprint": False,   "crouch": False,
            "look_sensitivity": 1.0, "speed": 4.0,
            "sprint_multiplier": 1.5, "jump_force": 0.0,
        }

    # physics — from "physics" feature flag
    if "physics" in features:
        caps["physics"] = {
            "gravity": True, "collision": "dynamic",
            "mass": 80.0,    "drag": 0.0,
        }

    # hud — from ui_toolkit / any ui feature
    if features & {"ui_toolkit", "ui"}:
        caps["hud"] = {
            "show_health": True, "show_ammo": False,
            "show_objective": True, "show_minimap": False,
        }

    # input — from input_system feature
    if "input_system" in features:
        binding_table = {
            "walk": _WALK_BINDINGS, "drive": _DRIVE_BINDINGS,
            "fly":  _FLY_BINDINGS,  "swim":  _SWIM_BINDINGS,
        }
        caps["input"] = {"bindings": binding_table.get(locomotion, _WALK_BINDINGS)}

    # model — always synthesise; drives SetModel/SetAnimationState stubs
    caps["model"] = {"lod": "auto", "animations": _DEFAULT_ANIMATIONS}

    # animation — from animation_rigging feature
    if "animation_rigging" in features:
        rig = "humanoid" if locomotion == "walk" else "generic"
        caps["animation"] = {"rig_type": rig, "clips": _DEFAULT_ANIMATIONS}

    # ai — from navmesh feature
    if "navmesh" in features:
        caps["ai"] = {"pathfinding": True, "detection_range": 15.0, "behavior": "patrol"}

    # vfx — no v1 equivalent; leave absent unless a future feature flag is added

    schema_dict["capabilities"] = caps
    return schema_dict
