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


def _i(v: Any, default: int) -> int:
    """Safe int coercion — mirrors _f's clamp-don't-crash contract (required
    AtomParams carry default=None, so raw int()/float() calls are landmines)."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _fv(v: Any, default: float) -> float:
    """Safe float VALUE coercion (unlike _f, which formats a C# literal)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


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
    clip  = _s(args.get("clip"),   "idle")
    dict_ = _s(args.get("dict"),   "amb@world_human_hang_out_street@male_b@base")
    speed = _f(args.get("speed",  1))
    return (
        f"GTA.Native.Function.Call(GTA.Native.Hash.REQUEST_ANIM_DICT, \"{dict_}\");\n"
        f"while (!GTA.Native.Function.Call<bool>(GTA.Native.Hash.HAS_ANIM_DICT_LOADED, \"{dict_}\"))\n"
        f"    Script.Wait(0);\n"
        f"GTA.Native.Function.Call(GTA.Native.Hash.TASK_PLAY_ANIM,\n"
        f"    Game.Player.Character, \"{dict_}\", \"{clip}\", {speed}, -1f, -1, 0, 0f, false, false, false);"
    )


def _play_sfx(args: dict) -> str:
    sound   = _s(args.get("sound"),   "CHECKPOINT_NORMAL")
    sfx_set = _s(args.get("sfx_set"), "HUD_MINI_GAME_SOUNDSET")
    return (
        f"GTA.Native.Function.Call(GTA.Native.Hash.PLAY_SOUND_FRONTEND,\n"
        f"    -1, \"{sound}\", \"{sfx_set}\", true);"
    )


def _spawn_particle(args: dict) -> str:
    effect = _s(args.get("effect"), "scr_exp_gen_explosion")
    bank   = _s(args.get("bank"),   "scr_explosions")
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
    entity = _s(args.get("entity"), "player")
    value  = _i(args.get("value"), 100)
    return (
        f"var _ped = \"{entity}\" == \"player\"\n"
        f"    ? (GTA.Ped)Game.Player.Character\n"
        f"    : GTA.World.GetNearestPed(new Vector3(0,0,0), 999f);\n"
        f"if (_ped != null) _ped.Health = {value};"
    )


def _spawn_entity(args: dict) -> str:
    model  = _s(args.get("entity_type"), "a_m_y_beach_01")
    x, y, z = _f(args.get("x", 0)), _f(args.get("y", 0)), _f(args.get("z", 0))
    return (
        f"var _eModel = new GTA.Model(\"{model}\");\n"
        f"_eModel.Request(5000);\n"
        f"if (_eModel.IsLoaded)\n"
        f"    GTA.World.CreatePed(_eModel, new Vector3({x}, {y}, {z}));\n"
        f"_eModel.MarkAsNoLongerNeeded();"
    )


def _wait(args: dict) -> str:
    ms = int(_fv(args.get("seconds"), 1.0) * 1000)
    return f"Script.Wait({ms});"


def _set_flag(args: dict) -> str:
    # `_flags` is declared by the gta5_tick_body() preamble — flag atoms are
    # meant to be assembled, not pasted standalone (mirrors Unity PlayerPrefs
    # semantics with a script-local session dict).
    flag  = _s(args.get("flag"), "flag")
    value = _b(args.get("value", True))
    return f"_flags[\"{flag}\"] = {value};"


def _trigger_event(args: dict) -> str:
    # GTA5 has no SendMessage bus; events surface as namespaced flags that
    # ConditionCheck (or mission-graph C#) can read.
    event = _s(args.get("event"), "OnEvent")
    return f"_flags[\"event:{event}\"] = true;"


def _condition_check(args: dict) -> str:
    # Mirrors the Unity backend's `yield break` semantics: a failed check
    # aborts the remaining sequence (returns out of the assembled method).
    flag   = _s(args.get("flag"), "flag")
    expect = _b(args.get("expect", True))
    return (
        f"// ConditionCheck: abort sequence if '{flag}' != {expect}\n"
        f"bool _cond = _flags.ContainsKey(\"{flag}\") && _flags[\"{flag}\"];\n"
        f"if (_cond != {expect}) return;"
    )


def _screen_fade(args: dict) -> str:
    duration = int(_fv(args.get("duration"), 1.0) * 1000)
    alpha    = _fv(args.get("alpha"), 1.0)
    if alpha >= 1:
        return f"GTA.Native.Function.Call(GTA.Native.Hash.DO_SCREEN_FADE_OUT, {duration});"
    else:
        return f"GTA.Native.Function.Call(GTA.Native.Hash.DO_SCREEN_FADE_IN, {duration});"


def _camera_look_at(args: dict) -> str:
    target = _s(args.get("target"), "Player")
    return (
        f"// GTA5 camera look at '{target}'\n"
        f"var _cam = GTA.Native.Function.Call<int>(GTA.Native.Hash.CREATE_CAM, \"DEFAULT_SCRIPTED_CAMERA\", true);\n"
        f"GTA.Native.Function.Call(GTA.Native.Hash.POINT_CAM_AT_ENTITY, _cam, Game.Player.Character, 0, 0, 0, true);\n"
        f"GTA.Native.Function.Call(GTA.Native.Hash.SET_CAM_ACTIVE, _cam, true);\n"
        f"GTA.Native.Function.Call(GTA.Native.Hash.RENDER_SCRIPT_CAMS, true, false, 3000, true, false);"
    )


def _equip_weapon(args: dict) -> str:
    # GTA.Native.Hash holds native FUNCTION names only — weapon hashes are not
    # members (CS0117). Resolve the weapon string at runtime via GET_HASH_KEY,
    # the same pattern the mission template uses (covers DLC weapons too).
    # Ped target resolves the same way _set_health does — the atom's own
    # contract declares `entity` required, but this used to hardcode the
    # player regardless of what was passed.
    entity = _s(args.get("entity"), "player")
    weapon = _s(args.get("weapon"), "weapon_pistol").lower()
    if not weapon.startswith("weapon_"):
        weapon = "weapon_" + weapon
    ammo = _i(args.get("ammo"), 100)
    return (
        f"var _ped = \"{entity}\" == \"player\"\n"
        f"    ? (GTA.Ped)Game.Player.Character\n"
        f"    : GTA.World.GetNearestPed(new Vector3(0,0,0), 999f);\n"
        f"int _wh = GTA.Native.Function.Call<int>(GTA.Native.Hash.GET_HASH_KEY, \"{weapon}\");\n"
        f"if (_ped != null) GTA.Native.Function.Call(GTA.Native.Hash.GIVE_WEAPON_TO_PED, _ped, _wh, {ammo}, false, true);"
    )


def _move_to_entity(args: dict) -> str:
    # entity_ref resolution follows the _set_health idiom: nearest ped stands in
    # for a named world entity (GTA entities are handles, not names).
    speed     = _f(args.get("speed", 5))
    stop_dist = _f(args.get("stop_dist", 1))
    return (
        f"var _target = GTA.World.GetNearestPed(Game.Player.Character.Position, 100f);\n"
        f"if (_target != null)\n"
        f"    GTA.Native.Function.Call(GTA.Native.Hash.TASK_GO_TO_ENTITY,\n"
        f"        Game.Player.Character, _target, -1, {stop_dist}, {speed}, 1073741824, 0);"
    )


def _rotate_to(args: dict) -> str:
    euler_y = _f(args.get("euler_y", 0))
    return f"Game.Player.Character.Heading = {euler_y};"


def _follow(args: dict) -> str:
    distance = _f(args.get("distance", 2))
    speed    = _f(args.get("speed", 4))
    return (
        f"var _followTarget = GTA.World.GetNearestPed(Game.Player.Character.Position, 100f);\n"
        f"if (_followTarget != null)\n"
        f"    GTA.Native.Function.Call(GTA.Native.Hash.TASK_FOLLOW_TO_OFFSET_OF_ENTITY,\n"
        f"        Game.Player.Character, _followTarget, 0f, -{distance}, 0f, {speed}, -1, {distance}, true);"
    )


def _stop_animation(args: dict) -> str:
    return "Game.Player.Character.Task.ClearAll();"


def _blend_animation(args: dict) -> str:
    clip_b   = _s(args.get("clip_b"), "base")
    dict_    = _s(args.get("dict"), "amb@world_human_hang_out_street@male_b@base")
    duration = max(0.05, _fv(args.get("duration"), 0.25))
    blend    = f"{1.0 / duration}f"
    return (
        f"GTA.Native.Function.Call(GTA.Native.Hash.REQUEST_ANIM_DICT, \"{dict_}\");\n"
        f"while (!GTA.Native.Function.Call<bool>(GTA.Native.Hash.HAS_ANIM_DICT_LOADED, \"{dict_}\"))\n"
        f"    Script.Wait(0);\n"
        f"// Cross-fade: blend-in/out speed derived from the requested duration.\n"
        f"GTA.Native.Function.Call(GTA.Native.Hash.TASK_PLAY_ANIM,\n"
        f"    Game.Player.Character, \"{dict_}\", \"{clip_b}\", {blend}, -{blend}, -1, 0, 0f, false, false, false);"
    )


def _camera_shake(args: dict) -> str:
    intensity = _f(args.get("intensity", 1))
    ms        = int(_fv(args.get("duration"), 0.3) * 1000)
    return (
        f"GTA.Native.Function.Call(GTA.Native.Hash.SHAKE_GAMEPLAY_CAM,\n"
        f"    \"SMALL_EXPLOSION_SHAKE\", {intensity});\n"
        f"Script.Wait({ms});\n"
        f"GTA.Native.Function.Call(GTA.Native.Hash.STOP_GAMEPLAY_CAM_SHAKING, true);"
    )


def _camera_offset(args: dict) -> str:
    x, y, z = _f(args.get("x", 0)), _f(args.get("y", 0)), _f(args.get("z", 0))
    return (
        f"var _cam = GTA.Native.Function.Call<int>(GTA.Native.Hash.CREATE_CAM, \"DEFAULT_SCRIPTED_CAMERA\", true);\n"
        f"var _cpos = Game.Player.Character.Position;\n"
        f"GTA.Native.Function.Call(GTA.Native.Hash.SET_CAM_COORD, _cam,\n"
        f"    _cpos.X + {x}, _cpos.Y + {y}, _cpos.Z + {z});\n"
        f"GTA.Native.Function.Call(GTA.Native.Hash.POINT_CAM_AT_ENTITY, _cam, Game.Player.Character, 0f, 0f, 0f, true);\n"
        f"GTA.Native.Function.Call(GTA.Native.Hash.SET_CAM_ACTIVE, _cam, true);\n"
        f"GTA.Native.Function.Call(GTA.Native.Hash.RENDER_SCRIPT_CAMS, true, true, 500, true, false);"
    )


def _camera_fov(args: dict) -> str:
    fov = _f(args.get("fov", 60))
    ms  = int(_fv(args.get("duration"), 0.0) * 1000)
    return (
        f"var _cam = GTA.Native.Function.Call<int>(GTA.Native.Hash.CREATE_CAM, \"DEFAULT_SCRIPTED_CAMERA\", true);\n"
        f"GTA.Native.Function.Call(GTA.Native.Hash.SET_CAM_COORD, _cam,\n"
        f"    GTA.GameplayCamera.Position.X, GTA.GameplayCamera.Position.Y, GTA.GameplayCamera.Position.Z);\n"
        f"GTA.Native.Function.Call(GTA.Native.Hash.SET_CAM_ROT, _cam,\n"
        f"    GTA.GameplayCamera.Rotation.X, GTA.GameplayCamera.Rotation.Y, GTA.GameplayCamera.Rotation.Z, 2);\n"
        f"GTA.Native.Function.Call(GTA.Native.Hash.SET_CAM_FOV, _cam, {fov});\n"
        f"GTA.Native.Function.Call(GTA.Native.Hash.SET_CAM_ACTIVE, _cam, true);\n"
        f"GTA.Native.Function.Call(GTA.Native.Hash.RENDER_SCRIPT_CAMS, true, {str(ms > 0).lower()}, {max(ms, 1)}, true, false);"
    )


def _play_music(args: dict) -> str:
    # GTA music is event-driven (interactive music system), not file playback.
    track = _s(args.get("track"), "MP_MC_ACTION_PREP")
    return f"GTA.Native.Function.Call(GTA.Native.Hash.TRIGGER_MUSIC_EVENT, \"{track}\");"


def _stop_music(args: dict) -> str:
    return "GTA.Native.Function.Call(GTA.Native.Hash.TRIGGER_MUSIC_EVENT, \"GLOBAL_KILL_MUSIC\");"


def _light_flash(args: dict) -> str:
    intensity = _f(args.get("intensity", 5))
    ms        = int(_fv(args.get("duration"), 0.05) * 1000)
    r = int(max(0.0, min(1.0, _fv(args.get("color_r"), 1.0))) * 255)
    g = int(max(0.0, min(1.0, _fv(args.get("color_g"), 1.0))) * 255)
    b = int(max(0.0, min(1.0, _fv(args.get("color_b"), 1.0))) * 255)
    return (
        f"var _lpos = Game.Player.Character.Position;\n"
        f"int _lightUntil = Game.GameTime + {max(ms, 1)};\n"
        f"while (Game.GameTime < _lightUntil)\n"
        f"{{\n"
        f"    GTA.Native.Function.Call(GTA.Native.Hash.DRAW_LIGHT_WITH_RANGE,\n"
        f"        _lpos.X, _lpos.Y, _lpos.Z + 1f, {r}, {g}, {b}, 10f, {intensity});\n"
        f"    Script.Wait(0);\n"
        f"}}"
    )


def _despawn(args: dict) -> str:
    return (
        "var _doomed = GTA.World.GetNearestPed(Game.Player.Character.Position, 50f);\n"
        "if (_doomed != null) _doomed.Delete();"
    )


# ── Dispatch table ────────────────────────────────────────────────────────────

_SNIPPETS: dict[str, Any] = {
    # movement
    "MoveTo":          _move_to,
    "MoveToEntity":    _move_to_entity,
    "RotateTo":        _rotate_to,
    "Teleport":        _teleport,
    "Follow":          _follow,
    # animation
    "PlayAnimation":   _play_animation,
    "StopAnimation":   _stop_animation,
    "BlendAnimation":  _blend_animation,
    # camera
    "CameraLookAt":    _camera_look_at,
    "CameraShake":     _camera_shake,
    "CameraOffset":    _camera_offset,
    "CameraFOV":       _camera_fov,
    # audio
    "PlaySFX":         _play_sfx,
    "PlayMusic":       _play_music,
    "StopMusic":       _stop_music,
    # fx
    "SpawnParticle":   _spawn_particle,
    "LightFlash":      _light_flash,
    "ScreenFade":      _screen_fade,
    # logic
    "Wait":            _wait,
    "SetFlag":         _set_flag,
    "TriggerEvent":    _trigger_event,
    "ConditionCheck":  _condition_check,
    # entity
    "SpawnEntity":     _spawn_entity,
    "Despawn":         _despawn,
    "SetHealth":       _set_health,
    "EquipWeapon":     _equip_weapon,
}

# Atoms whose snippets reference the `_flags` dict that gta5_tick_body()
# declares in its preamble — they must be assembled, not pasted standalone.
_FLAG_ATOMS: frozenset[str] = frozenset({"SetFlag", "TriggerEvent", "ConditionCheck"})


def gta5_atom_snippet(atom: str, args: dict | None = None) -> str:
    """Return a GTA5 SHVDN C# snippet for the given atom name + bound args.

    Raises ValueError for atoms with no GTA5 backend — FAIL-FAST doctrine:
    an atom either has a real snippet or its AtomDef excludes "gta5" from
    `engines`; comment stubs are never emitted.
    """
    fn = _SNIPPETS.get(atom)
    if fn is None:
        raise ValueError(
            f"atom {atom!r} has no GTA5 backend snippet — it is Unity-only "
            f"(check AtomDef.engines) or unknown"
        )
    return fn(args or {})


def gta5_tick_body(invocations: list[dict], *, loop: bool = False) -> str:
    """Assemble AtomInvocation dicts into a SHVDN statement sequence.

    Mirrors unity_coroutine_body: each multi-line snippet is wrapped in a
    `{ }` block so repeated atoms of the same type (two MoveTo, two Follow)
    don't redeclare the same local in one scope (CS0128). A `_flags` dict is
    declared once in the preamble when any flag atom (SetFlag/TriggerEvent/
    ConditionCheck) is present. Blocking `while + Script.Wait(0)` loops inside
    snippets are legal in SHVDN — Wait yields to the game.

    loop=True wraps the sequence in `while (true) { ...; Script.Wait(0); }`
    (behavior loops); the `_flags` preamble stays outside the loop so flag
    state persists across iterations. A failed ConditionCheck `return`s out
    of the enclosing method, ending the loop — same abort semantics as the
    Unity backend's `yield break`.
    """
    lines: list[str] = []
    if any(inv.get("atom") in _FLAG_ATOMS for inv in invocations):
        lines.append("var _flags = new Dictionary<string, bool>();")

    body: list[str] = []
    for inv in invocations:
        atom  = inv.get("atom", "")
        args  = inv.get("args", {})
        label = inv.get("label", "")
        if label:
            body.append(f"// {label}")
        snippet = gta5_atom_snippet(atom, args)
        if "\n" in snippet:
            body.append("{")
            for sl in snippet.split("\n"):
                body.append(("    " + sl) if sl.strip() else "")
            body.append("}")
        else:
            body.append(snippet)

    if loop:
        lines.append("while (true)")
        lines.append("{")
        lines.extend(("    " + bl) if bl.strip() else "" for bl in body)
        lines.append("    Script.Wait(0);")
        lines.append("}")
    else:
        lines.extend(body)

    return "\n".join(lines)
