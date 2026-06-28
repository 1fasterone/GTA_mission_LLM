"""
GTA5 backend for the Atom IR — separate domain from Unity.

`gta5_atom_snippet(atom, args)` returns a GTA5 native-script body string for
the given atom name + bound args.

GTA5 scripting model:
  - Scripthook V / SHVDN (C#) for .NET native scripts
  - Native hash calls (e.g. ENTITY::SET_ENTITY_COORDS)
  - No MonoBehaviours, no Update() loop — use a while(true)+Wait(0) game loop
  - Entities referenced by handle (int), not by class reference

Each snippet is:
  - A C# SHVDN statement block (usable inside a Script subclass OnTick)
  - Deterministic for the same args
  - Self-contained — no dependencies on other snippets

GTA5 wiring atoms are NOT in WIRING_ATOMS (those are Unity-only).
GTA5 cross-system connections are handled by the native script mission graph.
"""

from __future__ import annotations
from typing import Any


def _v3(args: dict, x: str = "x", y: str = "y", z: str = "z") -> str:
    xv, yv, zv = _f(args.get(x, 0.0)), _f(args.get(y, 0.0)), _f(args.get(z, 0.0))
    return f"new Vector3({xv}, {yv}, {zv})"


def _f(v: Any, default: float = 0.0) -> str:
    try:
        return f"{float(v)}f"
    except (TypeError, ValueError):
        return f"{default}f"


def _s(v: Any, default: str = "") -> str:
    return str(v) if v is not None else default


def _b(v: Any, default: bool = True) -> str:
    return "true" if v else "false"


# ── Snippet generators (one per atom) ─────────────────────────────────────────

def _move_to(args: dict) -> str:
    x, y, z   = _f(args.get("x", 0)), _f(args.get("y", 0)), _f(args.get("z", 0))
    speed     = _f(args.get("speed", 5))
    tolerance = _f(args.get("tolerance", 0.1))
    return (
        f"var _target = new Vector3({x}, {y}, {z});\n"
        f"while (Vector3.Distance(Game.Player.Character.Position, _target) > {tolerance})\n"
        f"{{\n"
        f"    var _ped = Game.Player.Character;\n"
        f"    GTA.Native.Function.Call(GTA.Native.Hash.TASK_GO_STRAIGHT_TO_COORD,\n"
        f"        _ped, {x}, {y}, {z}, {speed}, -1, 0.0f, 0);\n"
        f"    Script.Wait(0);\n"
        f"}}"
    )


def _teleport(args: dict) -> str:
    x, y, z = _f(args.get("x", 0)), _f(args.get("y", 0)), _f(args.get("z", 0))
    return (
        f"Game.Player.Character.Position = new Vector3({x}, {y}, {z});"
    )


def _play_animation(args: dict) -> str:
    clip  = _s(args.get("clip",   "idle"))
    dict_ = _s(args.get("dict",   "amb@world_human_hang_out_street@male_b@base"))
    speed = _f(args.get("speed",  1))
    return (
        f"GTA.Native.Function.Call(GTA.Native.Hash.REQUEST_ANIM_DICT, \"{dict_}\");\n"
        f"while (!GTA.Native.Function.Call<bool>(GTA.Native.Hash.HAS_ANIM_DICT_LOADED, \"{dict_}\"))\n"
        f"    Script.Wait(0);\n"
        f"GTA.Native.Function.Call(GTA.Native.Hash.TASK_PLAY_ANIM,\n"
        f"    Game.Player.Character, \"{dict_}\", \"{clip}\", {speed}, -1f, -1, 0, 0f, false, false, false);"
    )


def _play_sfx(args: dict) -> str:
    sound   = _s(args.get("sound",   "CHECKPOINT_NORMAL"))
    sfx_set = _s(args.get("sfx_set", "HUD_MINI_GAME_SOUNDSET"))
    return (
        f"GTA.Native.Function.Call(GTA.Native.Hash.PLAY_SOUND_FRONTEND,\n"
        f"    -1, \"{sound}\", \"{sfx_set}\", true);"
    )


def _spawn_particle(args: dict) -> str:
    effect = _s(args.get("effect", "scr_exp_gen_explosion"))
    bank   = _s(args.get("bank",   "scr_explosions"))
    x, y, z = _f(args.get("x", 0)), _f(args.get("y", 0)), _f(args.get("z", 0))
    scale   = _f(args.get("scale", 1))
    return (
        f"GTA.Native.Function.Call(GTA.Native.Hash.REQUEST_NAMED_PTFX_ASSET, \"{bank}\");\n"
        f"while (!GTA.Native.Function.Call<bool>(GTA.Native.Hash.HAS_NAMED_PTFX_ASSET_LOADED, \"{bank}\"))\n"
        f"    Script.Wait(0);\n"
        f"GTA.Native.Function.Call(GTA.Native.Hash.USE_PARTICLE_FX_ASSET, \"{bank}\");\n"
        f"GTA.Native.Function.Call<int>(GTA.Native.Hash.START_PARTICLE_FX_NON_LOOPED_AT_COORD,\n"
        f"    \"{effect}\", {x}, {y}, {z}, 0f, 0f, 0f, {scale}, false, false, false);"
    )


def _set_health(args: dict) -> str:
    entity = _s(args.get("entity", "player"))
    value  = int(args.get("value", 100))
    return (
        f"var _ped = \"{entity}\" == \"player\"\n"
        f"    ? (GTA.Ped)Game.Player.Character\n"
        f"    : GTA.World.GetNearestPed(new Vector3(0,0,0), 999f);\n"
        f"if (_ped != null) _ped.Health = {value};"
    )


def _spawn_entity(args: dict) -> str:
    model  = _s(args.get("entity_type", "a_m_y_beach_01"))
    x, y, z = _f(args.get("x", 0)), _f(args.get("y", 0)), _f(args.get("z", 0))
    return (
        f"var _eModel = new GTA.Model(\"{model}\");\n"
        f"_eModel.Request(5000);\n"
        f"if (_eModel.IsLoaded)\n"
        f"    GTA.World.CreatePed(_eModel, new Vector3({x}, {y}, {z}));\n"
        f"_eModel.MarkAsNoLongerNeeded();"
    )


def _wait(args: dict) -> str:
    ms = int(float(args.get("seconds", 1)) * 1000)
    return f"Script.Wait({ms});"


def _set_flag(args: dict) -> str:
    flag  = _s(args.get("flag", "flag"))
    value = _b(args.get("value", True))
    return f"// GTA5 flag '{flag}' = {value}\n// Store in script-local dict or PlayerData"


def _trigger_event(args: dict) -> str:
    event = _s(args.get("event", "OnEvent"))
    return f"// GTA5 event '{event}' — raise via mission script message bus or native TRIGGER_SCRIPT_EVENT"


def _screen_fade(args: dict) -> str:
    duration = int(float(args.get("duration", 1)) * 1000)
    alpha    = float(args.get("alpha", 1))
    if alpha >= 1:
        return f"GTA.Native.Function.Call(GTA.Native.Hash.DO_SCREEN_FADE_OUT, {duration});"
    else:
        return f"GTA.Native.Function.Call(GTA.Native.Hash.DO_SCREEN_FADE_IN, {duration});"


def _camera_look_at(args: dict) -> str:
    target = _s(args.get("target", "Player"))
    return (
        f"// GTA5 camera look at '{target}'\n"
        f"var _cam = GTA.Native.Function.Call<int>(GTA.Native.Hash.CREATE_CAM, \"DEFAULT_SCRIPTED_CAMERA\", true);\n"
        f"GTA.Native.Function.Call(GTA.Native.Hash.POINT_CAM_AT_ENTITY, _cam, Game.Player.Character, 0, 0, 0, true);\n"
        f"GTA.Native.Function.Call(GTA.Native.Hash.SET_CAM_ACTIVE, _cam, true);\n"
        f"GTA.Native.Function.Call(GTA.Native.Hash.RENDER_SCRIPT_CAMS, true, false, 3000, true, false);"
    )


def _equip_weapon(args: dict) -> str:
    weapon = _s(args.get("weapon", "WEAPON_PISTOL")).upper()
    if not weapon.startswith("WEAPON_"):
        weapon = "WEAPON_" + weapon
    return (
        f"GTA.Native.Function.Call(GTA.Native.Hash.GIVE_WEAPON_TO_PED,\n"
        f"    Game.Player.Character, (uint)GTA.Native.Hash.{weapon}, 100, false, true);"
    )


# ── Dispatch table ────────────────────────────────────────────────────────────

_SNIPPETS: dict[str, Any] = {
    "MoveTo":          _move_to,
    "Teleport":        _teleport,
    "PlayAnimation":   _play_animation,
    "PlaySFX":         _play_sfx,
    "SpawnParticle":   _spawn_particle,
    "SetHealth":       _set_health,
    "SpawnEntity":     _spawn_entity,
    "Wait":            _wait,
    "SetFlag":         _set_flag,
    "TriggerEvent":    _trigger_event,
    "ScreenFade":      _screen_fade,
    "CameraLookAt":    _camera_look_at,
    "EquipWeapon":     _equip_weapon,
}


def gta5_atom_snippet(atom: str, args: dict | None = None) -> str:
    """Return a GTA5 SHVDN C# snippet for the given atom name + bound args.

    Returns a comment stub if the atom has no GTA5 backend snippet yet.
    """
    fn = _SNIPPETS.get(atom)
    if fn is None:
        return f"// TODO: atom '{atom}' has no GTA5 backend snippet"
    return fn(args or {})
