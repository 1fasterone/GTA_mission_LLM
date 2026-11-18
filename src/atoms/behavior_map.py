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
    2. Whole-token word-form scan (_KEYWORD_FORMS) in _KEYWORD_ORDER priority
    3. [] — no match, entity keeps its raw behavior string for a custom script
"""

from __future__ import annotations

import re

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

# Priority order for keyword matching — specific patterns before broad ones.
_KEYWORD_ORDER = [
    "intro_fade", "outro_fade", "screen_transition", "camera_orbit",
    "collect", "pickup", "die", "respawn", "hurt",
    "patrol", "chase", "guard", "attack", "wander", "idle",
]

# The word forms that select each key, matched against WHOLE tokens of the
# behavior string (camelCase and non-alphanumeric boundaries split it). A tuple
# of several words is a phrase: consecutive tokens. Substring matching read
# "sol-DIE-r" as die, "pur-CHASE" as chase and "DIE-sel" as die. Every form is
# listed: a form missing here is a behavior that matches nothing, which keeps
# its raw string for a custom script, never a wrong canned sequence.
_KEYWORD_FORMS: dict[str, tuple[tuple[str, ...], ...]] = {
    "intro_fade":        (("intro", "fade"),),
    "outro_fade":        (("outro", "fade"),),
    "screen_transition": (("screen", "transition"),),
    "camera_orbit":      (("camera", "orbit"),),
    "collect":           (("collect",), ("collects",), ("collected",), ("collecting",),
                          ("collectible",), ("collectibles",), ("collectable",),
                          ("collectables",)),
    "pickup":            (("pickup",), ("pickups",), ("pick", "up")),
    "die":               (("die",), ("dies",), ("died",), ("dying",)),
    "respawn":           (("respawn",), ("respawns",), ("respawned",), ("respawning",)),
    "hurt":              (("hurt",), ("hurts",), ("hurting",)),
    "patrol":            (("patrol",), ("patrols",), ("patrolled",), ("patrolling",),
                          ("patroling",)),
    "chase":             (("chase",), ("chases",), ("chased",), ("chasing",)),
    "guard":             (("guard",), ("guards",), ("guarded",), ("guarding",)),
    "attack":            (("attack",), ("attacks",), ("attacked",), ("attacking",)),
    "wander":            (("wander",), ("wanders",), ("wandered",), ("wandering",)),
    "idle":              (("idle",), ("idles",), ("idling",)),
}

_TOKEN_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")

# When a canned sequence starts. EntityBehavior.cs.j2 renders one entry point
# per trigger: "spawn" runs at Start, "player_contact" on a Player-tagged
# collider entering the entity, "death" when OnDeath() is called. A pickup that
# ran at Start would destroy itself the frame it spawned.
TRIGGER_SPAWN = "spawn"
TRIGGER_PLAYER_CONTACT = "player_contact"
TRIGGER_DEATH = "death"
_BEHAVIOR_TRIGGERS: dict[str, str] = {
    "collect": TRIGGER_PLAYER_CONTACT,
    "pickup":  TRIGGER_PLAYER_CONTACT,
    "die":     TRIGGER_DEATH,
}


# ── Microgen → atom composition map ───────────────────────────────────────────
# Descriptive defaults; these sequences do not own primary micro gameplay.
# Since R2 no Unity hook emits them (the <Kind>DefaultBehavior companions were
# never attached and are retired); the support audit inventories them.

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

def behavior_tokens(text: str) -> list[str]:
    """Lowercase whole words of a behavior string: ``"PlayerCollectible"`` ->
    ``["player", "collectible"]``, ``"intro_fade"`` -> ``["intro", "fade"]``."""
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _has_phrase(tokens: list[str], phrase: tuple[str, ...]) -> bool:
    n = len(phrase)
    return any(tuple(tokens[i:i + n]) == phrase for i in range(len(tokens) - n + 1))


def behavior_keyword(behavior: str) -> str | None:
    """The _BEHAVIOR_MAP key a behavior string selects, or None.

    An exact key wins. Otherwise the first key in _KEYWORD_ORDER with a word
    form among the string's whole tokens."""
    if not behavior:
        return None
    low = behavior.strip().lower()
    if low in _BEHAVIOR_MAP:
        return low
    tokens = behavior_tokens(behavior)
    for key in _KEYWORD_ORDER:
        if any(_has_phrase(tokens, form) for form in _KEYWORD_FORMS[key]):
            return key
    return None


def expand_behavior(behavior: str) -> list[AtomInvocation]:
    """Map a behavior description string to a list of AtomInvocations.

    Returns [] if no pattern matches — the entity keeps its raw behavior
    string for a hand-authored custom script, which is fine.
    """
    key = behavior_keyword(behavior)
    if key is None:
        return []
    return [AtomInvocation.model_validate(d) for d in _BEHAVIOR_MAP[key]]


def _calls(atoms: list) -> list[tuple[str, dict]]:
    """(atom, args) per step: what a sequence does. Labels are comments."""
    out: list[tuple[str, dict]] = []
    for a in atoms or []:
        inv = a if isinstance(a, AtomInvocation) else AtomInvocation.model_validate(a)
        out.append((inv.atom, dict(inv.args or {})))
    return out


def behavior_trigger(atoms: list) -> str:
    """When ``atoms`` starts: the trigger of the canned sequence it exactly
    equals (atom and args, step by step), else TRIGGER_SPAWN.

    Only canned sequences carry a non-spawn trigger. ``expand_behaviors`` and
    ``unity_escort_follow`` write ``entity.atoms`` from them verbatim, so a
    saved schema keeps its trigger, and a sequence the Step 3 fill wrote
    itself, or one edited after expansion, starts at spawn as before.
    """
    calls = _calls(atoms)
    for key, trigger in _BEHAVIOR_TRIGGERS.items():
        if calls == _calls(_BEHAVIOR_MAP[key]):
            return trigger
    return TRIGGER_SPAWN


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
