"""
Microgen → atom mapping.

Two dictionaries live here:

_BEHAVIOR_MAP  — behavior keyword → canonical atom sequence.
    Used by the resolver's expand_behaviors() step to expand an entity's
    prose `behavior` string ("patrol between waypoints") into a concrete
    list of AtomInvocations.

MICROGEN_ATOM_MAP — microgen kind → default atom sequence.
    Documents how each existing microgen in src/micro/ maps to atoms.
    Used for documentation, RAG corpus generation, and future composite
    microgen assembly.

Resolution in expand_behavior():
    1. Exact key match (e.g. "patrol")
    2. Substring / keyword scan in _KEYWORD_ORDER priority
    3. [] — no match, entity keeps its raw behavior string for a custom script
"""

from __future__ import annotations

from .base import AtomInvocation


# ── Raw atom sequences (dicts for JSON-round-trip safety) ─────────────────────

_BEHAVIOR_MAP: dict[str, list[dict]] = {
    "patrol": [
        {"atom": "MoveTo", "args": {"x": 0,  "y": 0, "z": 10, "speed": 3.0}, "label": "go to A"},
        {"atom": "Wait",   "args": {"seconds": 1.5},                          "label": "pause at A"},
        {"atom": "MoveTo", "args": {"x": 0,  "y": 0, "z": -10, "speed": 3.0},"label": "return to B"},
        {"atom": "Wait",   "args": {"seconds": 1.5},                          "label": "pause at B"},
    ],
    "chase": [
        {"atom": "Follow", "args": {"target": "Player", "distance": 1.5, "speed": 5.0}, "label": "chase player"},
    ],
    "guard": [
        {"atom": "Wait",     "args": {"seconds": 4.0},                    "label": "watch"},
        {"atom": "RotateTo", "args": {"euler_y": 180, "speed": 60.0},     "label": "scan left"},
        {"atom": "Wait",     "args": {"seconds": 4.0},                    "label": "watch"},
        {"atom": "RotateTo", "args": {"euler_y": 0,   "speed": 60.0},     "label": "scan right"},
    ],
    "idle": [
        {"atom": "PlayAnimation", "args": {"clip": "Idle", "speed": 1.0}, "label": "idle"},
        {"atom": "Wait",          "args": {"seconds": 3.0}},
    ],
    "wander": [
        {"atom": "MoveTo",   "args": {"x":  5, "y": 0, "z":  0, "speed": 2.0}, "label": "wander A"},
        {"atom": "Wait",     "args": {"seconds": 2.0}},
        {"atom": "MoveTo",   "args": {"x": -5, "y": 0, "z":  5, "speed": 2.0}, "label": "wander B"},
        {"atom": "Wait",     "args": {"seconds": 2.0}},
        {"atom": "RotateTo", "args": {"euler_y": 90, "speed": 45.0}},
    ],
    "attack": [
        {"atom": "PlayAnimation", "args": {"clip": "Attack"},           "label": "attack"},
        {"atom": "Wait",          "args": {"seconds": 1.2}},
        {"atom": "PlayAnimation", "args": {"clip": "Idle"},             "label": "recover"},
    ],
    "die": [
        {"atom": "PlayAnimation", "args": {"clip": "Death"},            "label": "death anim"},
        {"atom": "Wait",          "args": {"seconds": 2.0}},
        {"atom": "Despawn",       "args": {"entity": "self"},           "label": "remove"},
    ],
    "collect": [
        {"atom": "PlaySFX",      "args": {"sound": "pickup", "volume": 0.8}, "label": "pickup sfx"},
        {"atom": "LightFlash",   "args": {"intensity": 3.0, "duration": 0.1}},
        {"atom": "TriggerEvent", "args": {"event": "OnCollected"}},
        {"atom": "Despawn",      "args": {"entity": "self"}},
    ],
    "pickup": [
        {"atom": "PlaySFX",      "args": {"sound": "pickup"},           "label": "pickup sound"},
        {"atom": "TriggerEvent", "args": {"event": "OnPickup"}},
        {"atom": "Despawn",      "args": {"entity": "self"}},
    ],
    "hurt": [
        {"atom": "CameraShake", "args": {"intensity": 0.5, "duration": 0.2}},
        {"atom": "PlaySFX",     "args": {"sound": "hurt", "pitch": 1.2}},
        {"atom": "LightFlash",  "args": {"intensity": 2.0, "duration": 0.05,
                                         "color_r": 1, "color_g": 0, "color_b": 0}},
    ],
    "respawn": [
        {"atom": "Teleport",      "args": {"x": 0, "y": 1, "z": 0},         "label": "reset pos"},
        {"atom": "SetHealth",     "args": {"entity": "Player", "value": 100},"label": "full health"},
        {"atom": "PlayAnimation", "args": {"clip": "Idle"},                  "label": "reset anim"},
    ],
    "intro_fade": [
        {"atom": "ScreenFade", "args": {"alpha": 1, "duration": 0},          "label": "start black"},
        {"atom": "Wait",       "args": {"seconds": 0.5}},
        {"atom": "ScreenFade", "args": {"alpha": 0, "duration": 1.5},        "label": "fade in"},
    ],
    "outro_fade": [
        {"atom": "ScreenFade", "args": {"alpha": 0, "duration": 0}},
        {"atom": "Wait",       "args": {"seconds": 0.3}},
        {"atom": "ScreenFade", "args": {"alpha": 1, "duration": 1.5},        "label": "fade out"},
    ],
    "screen_transition": [
        {"atom": "ScreenFade", "args": {"alpha": 1, "duration": 0.5},        "label": "fade to black"},
        {"atom": "Wait",       "args": {"seconds": 0.1}},
        {"atom": "ScreenFade", "args": {"alpha": 0, "duration": 0.5},        "label": "fade in scene"},
    ],
    "camera_orbit": [
        {"atom": "CameraLookAt", "args": {"target": "Player"},               "label": "look at player"},
        {"atom": "CameraFOV",    "args": {"fov": 50, "duration": 1.0},       "label": "cinematic zoom"},
    ],
}

# Priority order for substring matching — specific patterns before broad ones.
_KEYWORD_ORDER = [
    "intro_fade", "outro_fade", "screen_transition", "camera_orbit",
    "collect", "pickup", "die", "respawn", "hurt", "spawn_and_die",
    "patrol", "chase", "guard", "attack", "wander", "idle",
]


# ── Microgen → atom composition map ───────────────────────────────────────────
# Documents which atom sequences each microgen kind is built on.
# Used for RAG corpus + future composite microgen assembly (Phase 3+).

MICROGEN_ATOM_MAP: dict[str, list[dict]] = {
    # ── Controller: look → move → sprint → jump ───────────────────────────────
    "controller": [
        {"atom": "Look",   "args": {"pitch": 0.0, "yaw": 0.0, "sensitivity": 2.0}, "label": "look input"},
        {"atom": "Move",   "args": {"dir_x": 0.0, "dir_z": 1.0, "speed": 6.0},    "label": "move"},
        {"atom": "Sprint", "args": {"active": True, "multiplier": 1.7},             "label": "sprint"},
        {"atom": "Jump",   "args": {"force": 8.0},                                  "label": "jump"},
    ],
    # ── Camera: point at player ───────────────────────────────────────────────
    "camera": [
        {"atom": "CameraLookAt", "args": {"target": "Player"}},
    ],
    # ── Audio: start main music track ─────────────────────────────────────────
    "audio": [
        {"atom": "PlayMusic", "args": {"track": "main_theme", "loop": True, "volume": 0.8}},
    ],
    # ── Spawner: place an enemy ───────────────────────────────────────────────
    "spawner": [
        {"atom": "SpawnEntity", "args": {"entity_type": "Enemy", "x": 0, "y": 0, "z": 5}},
    ],
    # ── Transition: screen fade in/out ────────────────────────────────────────
    "transition": _BEHAVIOR_MAP["screen_transition"],
    # ── AI: default patrol loop ───────────────────────────────────────────────
    "ai": _BEHAVIOR_MAP["patrol"],
    # ── Interactable: pickup sequence ─────────────────────────────────────────
    "interactable": _BEHAVIOR_MAP["pickup"],
    # ── Health: damage feedback ───────────────────────────────────────────────
    "health": _BEHAVIOR_MAP["hurt"],
    # ── Dialogue: open dialogue event ────────────────────────────────────────
    "dialogue": [
        {"atom": "TriggerEvent", "args": {"event": "OnDialogueStart"}},
    ],
    # ── Inventory: open inventory event ──────────────────────────────────────
    "inventory": [
        {"atom": "TriggerEvent", "args": {"event": "OnInventoryOpen"}},
    ],
    # ── Menu: fade in ─────────────────────────────────────────────────────────
    "menu": [
        {"atom": "ScreenFade", "args": {"alpha": 0, "duration": 0.5}},
    ],
    # ── SaveLoad: write save flag ─────────────────────────────────────────────
    "saveload": [
        {"atom": "SetFlag", "args": {"flag": "game_saved", "value": True}},
    ],
    # ── HUD: show → update health → set objective ─────────────────────────────
    "hud": [
        {"atom": "ShowHUD",         "args": {"duration": 0.0},                                    "label": "show hud"},
        {"atom": "UpdateHealth",    "args": {"value": 100.0, "max_value": 100.0},                 "label": "health bar"},
        {"atom": "UpdateObjective", "args": {"text": "Reach the objective", "show_marker": True}, "label": "objective"},
    ],
    # ── Physics: enable gravity + apply forward force ─────────────────────────
    "physics": [
        {"atom": "SetGravity",  "args": {"enabled": True, "scale": 1.0}, "label": "enable gravity"},
        {"atom": "ApplyForce",  "args": {"x": 0.0, "y": 0.0, "z": 5.0}, "label": "push forward"},
    ],
    # ── Input: bind jump + fire actions ──────────────────────────────────────
    "input": [
        {"atom": "BindAction",      "args": {"action_name": "Jump", "event_name": "OnJump"},                      "label": "bind jump"},
        {"atom": "MapInputToEvent", "args": {"action_name": "Fire1", "event_name": "OnFire", "threshold": 0.5},   "label": "bind fire"},
    ],
    # ── Model: load mesh + set idle state ─────────────────────────────────────
    "model": [
        {"atom": "SetModel",          "args": {"model_path": "Models/Default"},                              "label": "load model"},
        {"atom": "SetAnimationState", "args": {"param": "Idle", "value": "true", "param_type": "trigger"},  "label": "play idle"},
    ],
    # ── State machine: transition to initial state ─────────────────────────────
    "statemachine": [
        {"atom": "TriggerEvent", "args": {"event": "OnStateEnter"}, "label": "enter initial state"},
        {"atom": "Wait",         "args": {"seconds": 0.1},          "label": "settle"},
    ],
    # ── Trigger: fire activation event ────────────────────────────────────────
    "trigger": [
        {"atom": "TriggerEvent", "args": {"event": "OnTriggerActivate"}, "label": "trigger event"},
    ],
    # ── Animator: set initial animation state ─────────────────────────────────
    "animator": [
        {"atom": "SetAnimationState", "args": {"param": "Idle", "value": "true", "param_type": "trigger"}, "label": "idle"},
        {"atom": "SetAnimationState", "args": {"param": "Speed","value": "0",    "param_type": "float"},    "label": "zero speed"},
    ],
    # ── NPC Dialog: look → speak → gesture → end ─────────────────────────────
    "npc_dialog": [
        {"atom": "LookAt",   "args": {"target": "Player", "duration": 0.0, "weight": 0.8}, "label": "face player"},
        {"atom": "SayLine",  "args": {"line_id": "greet", "blocking": True},                "label": "greeting line"},
        {"atom": "Gesture",  "args": {"type": "nod", "duration": 1.0},                      "label": "idle gesture"},
        {"atom": "EndDialog","args": {"return_to_idle": True},                              "label": "close dialog"},
    ],
    # ── VFX: spawn an impact burst then flash the light ───────────────────────
    "vfx": [
        {"atom": "SpawnParticle", "args": {"effect": "Explosion", "x": 0.0, "y": 0.0, "z": 0.0, "scale": 1.0}, "label": "spawn burst"},
        {"atom": "LightFlash",    "args": {"intensity": 5.0, "duration": 0.05},                                "label": "impact flash"},
    ],
    # ── Lighting: the rig itself is built in Awake (not an atom sequence); the
    #    only atom-expressible behavior is pulsing the key light. ─────────────
    "lighting": [
        {"atom": "LightFlash", "args": {"intensity": 2.0, "duration": 0.1},
         "label": "key light pulse"},
    ],
    # ── Shader: assign the default material slot ──────────────────────────────
    "shader": [
        {"atom": "SetMaterial", "args": {"material_name": "Materials/Default", "slot": 0}, "label": "assign material"},
    ],
}


# ── GTA5 microgen → atom composition map ─────────────────────────────────────
# The GTA micro layer's canonical sequences. Uses ONLY gta5-capable atoms
# (the 7 engine-agnostic core categories) — never controller/hud/input/model
# atoms, which are Unity-scoped. Kinds whose baseline is pure template C#
# (menu drawing, saveload ini I/O) are deliberately absent.

GTA5_MICROGEN_ATOM_MAP: dict[str, list[dict]] = {
    # ── Spawner: announce + place an enemy near the action ────────────────────
    "spawner": [
        {"atom": "SpawnEntity", "args": {"entity_type": "g_m_y_lost_01", "x": 0, "y": 5, "z": 0},
         "label": "spawn hostile"},
        {"atom": "PlaySFX",     "args": {"sound": "CHECKPOINT_NORMAL",
                                          "sfx_set": "HUD_MINI_GAME_SOUNDSET"},
         "label": "spawn cue"},
    ],
    # ── Waves: escalation heartbeat — flag the wave, breathe, signal ──────────
    "waves": [
        {"atom": "SetFlag",      "args": {"flag": "wave_active", "value": True}, "label": "arm wave"},
        {"atom": "TriggerEvent", "args": {"event": "OnWaveStart"},               "label": "signal start"},
        {"atom": "Wait",         "args": {"seconds": 1.0},                       "label": "settle"},
    ],
    # ── HUD: an audible tick when the display comes alive ────────────────────
    "hud": [
        {"atom": "PlaySFX", "args": {"sound": "TIMER_STOP",
                                      "sfx_set": "HUD_MINI_GAME_SOUNDSET"},
         "label": "hud ready cue"},
    ],
}


# ── Public API ─────────────────────────────────────────────────────────────────

def expand_behavior(behavior: str) -> list[AtomInvocation]:
    """Map a behavior description string to a list of AtomInvocations.

    Returns [] if no pattern matches — the entity keeps its raw behavior
    string for a hand-authored custom script, which is fine.
    """
    if not behavior:
        return []
    low = behavior.lower()
    # exact key first
    if low in _BEHAVIOR_MAP:
        return [AtomInvocation.model_validate(d) for d in _BEHAVIOR_MAP[low]]
    # priority substring scan
    for key in _KEYWORD_ORDER:
        needle = key.replace("_", " ")
        if needle in low or key in low:
            return [AtomInvocation.model_validate(d) for d in _BEHAVIOR_MAP[key]]
    return []


def microgen_atoms(kind: str, engine: str = "unity") -> list[AtomInvocation]:
    """Return the default atom sequence for a microgen kind (may be []).

    engine selects the map: "unity" (default, backward-compatible) uses
    MICROGEN_ATOM_MAP; "gta5" uses GTA5_MICROGEN_ATOM_MAP.
    """
    table = GTA5_MICROGEN_ATOM_MAP if engine == "gta5" else MICROGEN_ATOM_MAP
    raw = table.get(kind, [])
    return [AtomInvocation.model_validate(d) for d in raw]


def behavior_loops(atoms: list[AtomInvocation]) -> bool:
    """True when this atom sequence should loop (i.e. does NOT end with Despawn)."""
    return not (atoms and atoms[-1].atom == "Despawn")
