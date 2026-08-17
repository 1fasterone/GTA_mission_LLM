"""
Unity C# backend for the Atom IR.

`unity_atom_snippet(atom, args)` returns a C# coroutine body string for
the given atom name + bound args.  These snippets are yielding coroutines
(IEnumerator-compatible) so they compose cleanly inside a `StartCoroutine`.

Each snippet is self-contained: it reads from `transform`, `GetComponent<>`,
`Resources.Load`, `Camera.main`, and standard Unity APIs only — no engine
setup beyond what a generated scene provides by default.
"""

from __future__ import annotations

from typing import Any


def _v3(args: dict, x: str = "x", y: str = "y", z: str = "z") -> str:
    xv = _f(args.get(x, 0.0))
    yv = _f(args.get(y, 0.0))
    zv = _f(args.get(z, 0.0))
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
    x, y, z    = _f(args.get("x", 0)), _f(args.get("y", 0)), _f(args.get("z", 0))
    speed      = _f(args.get("speed", 5))
    tolerance  = _f(args.get("tolerance", 0.1))
    return (
        f"var _target = new Vector3({x}, {y}, {z});\n"
        f"while (Vector3.Distance(transform.position, _target) > {tolerance})\n"
        f"{{\n"
        f"    transform.position = Vector3.MoveTowards(transform.position, _target, {speed} * Time.deltaTime);\n"
        f"    yield return null;\n"
        f"}}"
    )


def _move_to_entity(args: dict) -> str:
    entity    = _s(args.get("entity", "Target"))
    speed     = _f(args.get("speed", 5))
    stop_dist = _f(args.get("stop_dist", 1))
    return (
        f"var _et = GameObject.Find(\"{entity}\");\n"
        f"if (_et != null) {{\n"
        f"    while (Vector3.Distance(transform.position, _et.transform.position) > {stop_dist})\n"
        f"    {{\n"
        f"        transform.position = Vector3.MoveTowards(transform.position, _et.transform.position, {speed} * Time.deltaTime);\n"
        f"        yield return null;\n"
        f"    }}\n"
        f"}}"
    )


def _rotate_to(args: dict) -> str:
    euler_y = _f(args.get("euler_y", 0))
    speed   = _f(args.get("speed", 90))
    return (
        f"var _rot = Quaternion.Euler(0f, {euler_y}, 0f);\n"
        f"while (Quaternion.Angle(transform.rotation, _rot) > 0.5f)\n"
        f"{{\n"
        f"    transform.rotation = Quaternion.RotateTowards(transform.rotation, _rot, {speed} * Time.deltaTime);\n"
        f"    yield return null;\n"
        f"}}"
    )


def _teleport(args: dict) -> str:
    x, y, z = _f(args.get("x", 0)), _f(args.get("y", 0)), _f(args.get("z", 0))
    return f"transform.position = new Vector3({x}, {y}, {z});"


def _follow(args: dict) -> str:
    target   = _s(args.get("target", "Player"))
    distance = _f(args.get("distance", 2))
    speed    = _f(args.get("speed", 4))
    return (
        f"var _ft = GameObject.Find(\"{target}\");\n"
        f"if (_ft != null) {{\n"
        f"    var _fpos = _ft.transform.position - _ft.transform.forward * {distance};\n"
        f"    transform.position = Vector3.MoveTowards(transform.position, _fpos, {speed} * Time.deltaTime);\n"
        f"}}\n"
        f"yield return null;"
    )


def _play_animation(args: dict) -> str:
    clip  = _s(args.get("clip", "Idle"))
    layer = int(args.get("layer", 0))
    speed = _f(args.get("speed", 1))
    return (
        f"var _anim = GetComponent<Animator>();\n"
        f"if (_anim != null) {{\n"
        f"    _anim.speed = {speed};\n"
        f"    _anim.Play(\"{clip}\", {layer});\n"
        f"}}"
    )


def _stop_animation(args: dict) -> str:
    layer = int(args.get("layer", 0))
    return (
        f"var _anim = GetComponent<Animator>();\n"
        f"if (_anim != null) _anim.Play(\"Idle\", {layer});"
    )


def _blend_animation(args: dict) -> str:
    clip_a   = _s(args.get("clip_a", "Idle"))
    clip_b   = _s(args.get("clip_b", "Walk"))
    weight   = _f(args.get("weight", 0.5))
    duration = _f(args.get("duration", 0.25))
    return (
        f"var _anim = GetComponent<Animator>();\n"
        f"if (_anim != null) {{\n"
        f"    _anim.CrossFadeInFixedTime(\"{clip_a}\", {duration}, 0);\n"
        f"    _anim.CrossFadeInFixedTime(\"{clip_b}\", {duration}, 1, 0f, {weight});\n"
        f"}}"
    )


def _camera_look_at(args: dict) -> str:
    target = _s(args.get("target", "Player"))
    return (
        f"var _ct = GameObject.Find(\"{target}\");\n"
        f"if (_ct != null && Camera.main != null)\n"
        f"    Camera.main.transform.LookAt(_ct.transform);"
    )


def _camera_shake(args: dict) -> str:
    intensity = _f(args.get("intensity", 1))
    duration  = _f(args.get("duration", 0.3))
    return (
        f"// CameraShake — requires CinemachineImpulseSource on Camera or a shake script\n"
        f"var _impulse = Camera.main?.GetComponent<UnityEngine.Rendering.Universal.CinemachineImpulseSource>();\n"
        f"if (_impulse != null) _impulse.GenerateImpulse({intensity});\n"
        f"yield return new WaitForSeconds({duration});"
    )


def _camera_offset(args: dict) -> str:
    x, y, z = _f(args.get("x", 0)), _f(args.get("y", 0)), _f(args.get("z", 0))
    return (
        f"if (Camera.main != null)\n"
        f"    Camera.main.transform.position += new Vector3({x}, {y}, {z});"
    )


def _camera_fov(args: dict) -> str:
    fov      = _f(args.get("fov", 60))
    duration = float(args.get("duration", 0))
    if duration <= 0:
        return f"if (Camera.main != null) Camera.main.fieldOfView = {fov};"
    dur = _f(duration)
    return (
        f"if (Camera.main != null) {{\n"
        f"    float _startFov = Camera.main.fieldOfView;\n"
        f"    float _t = 0f;\n"
        f"    while (_t < {dur}) {{\n"
        f"        Camera.main.fieldOfView = Mathf.Lerp(_startFov, {fov}, _t / {dur});\n"
        f"        _t += Time.deltaTime;\n"
        f"        yield return null;\n"
        f"    }}\n"
        f"    Camera.main.fieldOfView = {fov};\n"
        f"}}"
    )


def _play_sfx(args: dict) -> str:
    sound  = _s(args.get("sound", "sfx"))
    volume = _f(args.get("volume", 1))
    pitch  = _f(args.get("pitch", 1))
    return (
        f"var _clip = Resources.Load<AudioClip>(\"Audio/{sound}\");\n"
        f"if (_clip != null) {{\n"
        f"    var _src = gameObject.AddComponent<AudioSource>();\n"
        f"    _src.volume = {volume};\n"
        f"    _src.pitch  = {pitch};\n"
        f"    _src.PlayOneShot(_clip);\n"
        f"    Destroy(_src, _clip.length + 0.1f);\n"
        f"}}"
    )


def _play_music(args: dict) -> str:
    track  = _s(args.get("track", "music"))
    volume = _f(args.get("volume", 0.8))
    loop   = _b(args.get("loop", True))
    return (
        f"var _mus = Resources.Load<AudioClip>(\"Audio/Music/{track}\");\n"
        f"if (_mus != null) {{\n"
        f"    var _msrc = Camera.main?.GetComponent<AudioSource>() ?? Camera.main?.gameObject.AddComponent<AudioSource>();\n"
        f"    if (_msrc != null) {{ _msrc.clip = _mus; _msrc.volume = {volume}; _msrc.loop = {loop}; _msrc.Play(); }}\n"
        f"}}"
    )


def _stop_music(args: dict) -> str:
    duration = _f(args.get("duration", 0.5))
    return (
        f"var _ms = Camera.main?.GetComponent<AudioSource>();\n"
        f"if (_ms != null) {{\n"
        f"    float _t = _ms.volume;\n"
        f"    float _el = 0f;\n"
        f"    while (_el < {duration}) {{\n"
        f"        _ms.volume = Mathf.Lerp(_t, 0f, _el / {duration});\n"
        f"        _el += Time.deltaTime;\n"
        f"        yield return null;\n"
        f"    }}\n"
        f"    _ms.Stop(); _ms.volume = _t;\n"
        f"}}"
    )


def _spawn_particle(args: dict) -> str:
    effect = _s(args.get("effect", "explosion"))
    x, y, z = _f(args.get("x", 0)), _f(args.get("y", 0)), _f(args.get("z", 0))
    scale   = _f(args.get("scale", 1))
    return (
        f"var _pfx = Resources.Load<GameObject>(\"VFX/{effect}\");\n"
        f"if (_pfx != null) {{\n"
        f"    var _inst = Instantiate(_pfx, new Vector3({x}, {y}, {z}), Quaternion.identity);\n"
        f"    _inst.transform.localScale = Vector3.one * {scale};\n"
        f"}}"
    )


def _light_flash(args: dict) -> str:
    intensity = _f(args.get("intensity", 5))
    duration  = _f(args.get("duration", 0.05))
    r = _f(args.get("color_r", 1))
    g = _f(args.get("color_g", 1))
    b = _f(args.get("color_b", 1))
    return (
        f"var _light = GetComponentInChildren<Light>();\n"
        f"if (_light != null) {{\n"
        f"    float _orig = _light.intensity;\n"
        f"    var _origCol = _light.color;\n"
        f"    _light.intensity = {intensity};\n"
        f"    _light.color = new Color({r}, {g}, {b});\n"
        f"    yield return new WaitForSeconds({duration});\n"
        f"    _light.intensity = _orig;\n"
        f"    _light.color = _origCol;\n"
        f"}}"
    )


def _screen_fade(args: dict) -> str:
    alpha    = _f(args.get("alpha", 1))
    duration = _f(args.get("duration", 1))
    return (
        f"var _fade = GameObject.Find(\"FadePanel\")?.GetComponent<CanvasGroup>();\n"
        f"if (_fade != null) {{\n"
        f"    float _t = 0f;\n"
        f"    float _start = _fade.alpha;\n"
        f"    while (_t < {duration}) {{\n"
        f"        _fade.alpha = Mathf.Lerp(_start, {alpha}, _t / {duration});\n"
        f"        _t += Time.deltaTime;\n"
        f"        yield return null;\n"
        f"    }}\n"
        f"    _fade.alpha = {alpha};\n"
        f"}}"
    )


def _wait(args: dict) -> str:
    seconds = _f(args.get("seconds", 1))
    return f"yield return new WaitForSeconds({seconds});"


def _set_flag(args: dict) -> str:
    flag  = _s(args.get("flag", "flag"))
    value = _b(args.get("value", True))
    return f"PlayerPrefs.SetInt(\"{flag}\", {value} ? 1 : 0);\nPlayerPrefs.Save();"


def _trigger_event(args: dict) -> str:
    event = _s(args.get("event", "OnEvent"))
    return f"SendMessage(\"{event}\", SendMessageOptions.DontRequireReceiver);"


def _condition_check(args: dict) -> str:
    flag   = _s(args.get("flag", "flag"))
    expect = _b(args.get("expect", True))
    return (
        f"// ConditionCheck: skip next step if {flag} != {expect}\n"
        f"if (PlayerPrefs.GetInt(\"{flag}\", 0) != ({expect} ? 1 : 0)) yield break;"
    )


def _spawn_entity(args: dict) -> str:
    etype = _s(args.get("entity_type", "Entity"))
    x, y, z = _f(args.get("x", 0)), _f(args.get("y", 0)), _f(args.get("z", 0))
    return (
        f"var _epfab = Resources.Load<GameObject>(\"Entities/{etype}\");\n"
        f"if (_epfab != null) Instantiate(_epfab, new Vector3({x}, {y}, {z}), Quaternion.identity);"
    )


def _despawn(args: dict) -> str:
    entity = _s(args.get("entity", "Entity"))
    return (
        f"var _dgo = GameObject.Find(\"{entity}\");\n"
        f"if (_dgo != null) Destroy(_dgo);"
    )


def _set_health(args: dict) -> str:
    entity = _s(args.get("entity", "Player"))
    value  = _f(args.get("value", 100))
    return (
        f"var _hgo = \"{entity}\" == \"Player\" ? gameObject : GameObject.Find(\"{entity}\");\n"
        f"var _hs = _hgo?.GetComponent<HealthSystem>();\n"
        f"if (_hs != null) _hs.SetHealth({value});"
    )


def _equip_weapon(args: dict) -> str:
    entity = _s(args.get("entity", "Player"))
    weapon = _s(args.get("weapon", "pistol"))
    return (
        f"var _wgo = \"{entity}\" == \"Player\" ? gameObject : GameObject.Find(\"{entity}\");\n"
        f"var _inv = _wgo?.GetComponent<Inventory>();\n"
        f"if (_inv != null) _inv.Equip(\"{weapon}\");"
    )


# ── Controller atoms ─────────────────────────────────────────────────────────

def _move(args: dict) -> str:
    dir_x = _f(args.get("dir_x", 0.0))
    dir_z = _f(args.get("dir_z", 1.0))
    speed = _f(args.get("speed", 6.0))
    return (
        f"var _mdir = new Vector3({dir_x}, 0f, {dir_z});\n"
        f"var _mrb = GetComponent<Rigidbody>();\n"
        f"if (_mrb != null)\n"
        f"    _mrb.MovePosition(_mrb.position + _mdir.normalized * {speed} * Time.deltaTime);\n"
        f"else\n"
        f"    transform.Translate(_mdir.normalized * {speed} * Time.deltaTime, Space.World);"
    )


def _rotate(args: dict) -> str:
    delta = _f(args.get("euler_y_delta", 0.0))
    speed = _f(args.get("speed", 1.0))
    return f"transform.Rotate(0f, {delta} * {speed} * Time.deltaTime, 0f, Space.World);"


def _jump(args: dict) -> str:
    force = _f(args.get("force", 8.0))
    return (
        f"var _jrb = GetComponent<Rigidbody>();\n"
        f"if (_jrb != null) _jrb.AddForce(Vector3.up * {force}, ForceMode.Impulse);"
    )


def _sprint(args: dict) -> str:
    active = _b(args.get("active", True))
    mult   = _f(args.get("multiplier", 1.7))
    return (
        f"var _sprintMult = {active} ? {mult} : 1.0f;\n"
        f"SendMessage(\"SetSpeedMultiplier\", _sprintMult, SendMessageOptions.DontRequireReceiver);"
    )


def _crouch(args: dict) -> str:
    active = _b(args.get("active", True))
    scale  = _f(args.get("height_scale", 0.55))
    return (
        f"var _ccol = GetComponent<CapsuleCollider>();\n"
        f"if (_ccol != null) _ccol.height = {active} ? _ccol.height * {scale} : _ccol.height;\n"
        f"transform.localScale = new Vector3(1f, {active} ? {scale} : 1.0f, 1f);"
    )


def _look(args: dict) -> str:
    pitch = _f(args.get("pitch", 0.0))
    yaw   = _f(args.get("yaw",   0.0))
    sens  = _f(args.get("sensitivity", 2.0))
    return (
        f"var _lyaw   = transform.eulerAngles.y + {yaw} * {sens};\n"
        f"var _lpitch = Mathf.Clamp(transform.eulerAngles.x - {pitch} * {sens}, -89f, 89f);\n"
        f"transform.rotation = Quaternion.Euler(_lpitch, _lyaw, 0f);"
    )


# ── HUD atoms ─────────────────────────────────────────────────────────────────

def _show_hud(args: dict) -> str:
    dur = _f(args.get("duration", 0.0))
    return (
        f"var _hudS = GameObject.Find(\"HUD\");\n"
        f"if (_hudS != null) {{\n"
        f"    var _cgS = _hudS.GetComponent<CanvasGroup>();\n"
        f"    if (_cgS != null) {{ _cgS.alpha = 1f; _cgS.interactable = true; _cgS.blocksRaycasts = true; }}\n"
        f"    else _hudS.SetActive(true);\n"
        f"}}  // duration hint: {dur}s (wire to a Lerp coroutine if needed)"
    )


def _hide_hud(args: dict) -> str:
    dur = _f(args.get("duration", 0.0))
    return (
        f"var _hudH = GameObject.Find(\"HUD\");\n"
        f"if (_hudH != null) {{\n"
        f"    var _cgH = _hudH.GetComponent<CanvasGroup>();\n"
        f"    if (_cgH != null) {{ _cgH.alpha = 0f; _cgH.interactable = false; _cgH.blocksRaycasts = false; }}\n"
        f"    else _hudH.SetActive(false);\n"
        f"}}  // duration hint: {dur}s"
    )


def _update_health(args: dict) -> str:
    value   = _f(args.get("value",     100.0))
    max_val = _f(args.get("max_value", 100.0))
    return (
        f"var _hpBar = GameObject.Find(\"HealthBar\")?.GetComponent<UnityEngine.UI.Slider>();\n"
        f"if (_hpBar != null) _hpBar.value = {value} / {max_val};"
    )


def _update_ammo(args: dict) -> str:
    current = int(args.get("current", 30))
    reserve = int(args.get("reserve", 90))
    return (
        f"var _ammoTxt = GameObject.Find(\"AmmoText\")?.GetComponent<UnityEngine.UI.Text>();\n"
        f"if (_ammoTxt != null) _ammoTxt.text = \"{current} / {reserve}\";"
    )


def _update_objective(args: dict) -> str:
    text   = _s(args.get("text", "Complete the objective"))
    marker = _b(args.get("show_marker", True))
    return (
        f"var _objTxt = GameObject.Find(\"ObjectiveText\")?.GetComponent<UnityEngine.UI.Text>();\n"
        f"if (_objTxt != null) _objTxt.text = \"{text}\";\n"
        f"var _marker = GameObject.Find(\"WaypointMarker\");\n"
        f"if (_marker != null) _marker.SetActive({marker});"
    )


# ── Physics atoms ─────────────────────────────────────────────────────────────

def _apply_force(args: dict) -> str:
    x = _f(args.get("x", 0.0))
    y = _f(args.get("y", 0.0))
    z = _f(args.get("z", 0.0))
    return (
        f"var _frb = GetComponent<Rigidbody>();\n"
        f"if (_frb != null) _frb.AddForce(new Vector3({x}, {y}, {z}), ForceMode.Force);"
    )


def _apply_impulse(args: dict) -> str:
    x = _f(args.get("x", 0.0))
    y = _f(args.get("y", 5.0))
    z = _f(args.get("z", 0.0))
    return (
        f"var _irb = GetComponent<Rigidbody>();\n"
        f"if (_irb != null) _irb.AddForce(new Vector3({x}, {y}, {z}), ForceMode.Impulse);"
    )


def _set_gravity(args: dict) -> str:
    enabled = _b(args.get("enabled", True))
    return (
        f"var _grb = GetComponent<Rigidbody>();\n"
        f"if (_grb != null) _grb.useGravity = {enabled};"
    )


def _toggle_collision(args: dict) -> str:
    enabled = _b(args.get("enabled", True))
    return f"foreach (var _col in GetComponents<Collider>()) _col.enabled = {enabled};"


# ── Input atoms ───────────────────────────────────────────────────────────────

def _bind_action(args: dict) -> str:
    action = _s(args.get("action_name", "Jump"))
    event  = _s(args.get("event_name",  "OnJump"))
    return (
        f"// BindAction: '{action}' -> SendMessage(\"{event}\")\n"
        f"if (Input.GetButtonDown(\"{action}\"))\n"
        f"    SendMessage(\"{event}\", SendMessageOptions.DontRequireReceiver);"
    )


def _bind_axis(args: dict) -> str:
    axis  = _s(args.get("axis_name",  "Horizontal"))
    param = _s(args.get("param_name", "MoveX"))
    sens  = _f(args.get("sensitivity", 1.0))
    safe  = param.replace(" ", "_").replace("-", "_")
    return (
        f"var _{safe} = Input.GetAxis(\"{axis}\") * {sens};\n"
        f"SendMessage(\"{param}\", _{safe}, SendMessageOptions.DontRequireReceiver);"
    )


def _read_input(args: dict) -> str:
    action = _s(args.get("action_name", "Fire1"))
    flag   = _s(args.get("set_flag",    "OnFire"))
    return (
        f"if (Input.GetButton(\"{action}\"))\n"
        f"    SendMessage(\"{flag}\", SendMessageOptions.DontRequireReceiver);"
    )


def _map_input_to_event(args: dict) -> str:
    action    = _s(args.get("action_name", "Fire1"))
    event     = _s(args.get("event_name",  "OnAction"))
    threshold = _f(args.get("threshold",    0.5))
    return (
        f"var _axisVal = Input.GetAxisRaw(\"{action}\");\n"
        f"if (Mathf.Abs(_axisVal) >= {threshold} || Input.GetButton(\"{action}\"))\n"
        f"    SendMessage(\"{event}\", SendMessageOptions.DontRequireReceiver);"
    )


# ── Model atoms ───────────────────────────────────────────────────────────────

def _set_model(args: dict) -> str:
    path = _s(args.get("model_path", "Models/Default"))
    return (
        f"var _prefab = Resources.Load<GameObject>(\"{path}\");\n"
        f"if (_prefab != null) {{\n"
        f"    var _mf = GetComponent<MeshFilter>(); var _src = _prefab.GetComponent<MeshFilter>();\n"
        f"    if (_mf != null && _src != null) _mf.sharedMesh = _src.sharedMesh;\n"
        f"    var _mr = GetComponent<MeshRenderer>(); var _smr = _prefab.GetComponent<MeshRenderer>();\n"
        f"    if (_mr != null && _smr != null) _mr.sharedMaterials = _smr.sharedMaterials;\n"
        f"}}"
    )


def _set_material(args: dict) -> str:
    name = _s(args.get("material_name", "Materials/Default"))
    slot = int(args.get("slot", 0))
    return (
        f"var _mat = Resources.Load<Material>(\"{name}\");\n"
        f"var _smr = GetComponent<MeshRenderer>();\n"
        f"if (_smr != null && _mat != null) {{\n"
        f"    var _mats = _smr.sharedMaterials;\n"
        f"    if ({slot} < _mats.Length) {{ _mats[{slot}] = _mat; _smr.sharedMaterials = _mats; }}\n"
        f"}}"
    )


def _set_lod(args: dict) -> str:
    level = int(args.get("level", 0))
    if level < 0:
        return "GetComponentInParent<LODGroup>()?.ForceLOD(-1);  // auto LOD"
    return f"GetComponentInParent<LODGroup>()?.ForceLOD({level});"


def _set_animation_state(args: dict) -> str:
    param      = _s(args.get("param",      "Idle"))
    value      = _s(args.get("value",      "true"))
    param_type = _s(args.get("param_type", "trigger")).lower()

    if param_type == "trigger":
        anim_call = f'_anim.SetTrigger("{param}");'
    elif param_type == "bool":
        bval = "true" if value.lower() in ("true", "1", "yes") else "false"
        anim_call = f'_anim.SetBool("{param}", {bval});'
    elif param_type == "int":
        try:
            ival = int(float(value))
        except (ValueError, TypeError):
            ival = 0
        anim_call = f'_anim.SetInteger("{param}", {ival});'
    elif param_type == "float":
        try:
            fval = float(value)
        except (ValueError, TypeError):
            fval = 0.0
        anim_call = f'_anim.SetFloat("{param}", {fval}f);'
    else:
        anim_call = f'_anim.SetTrigger("{param}");'

    return (
        f"var _anim = GetComponent<Animator>();\n"
        f"if (_anim != null) {anim_call}"
    )


# ── Dialog atoms ──────────────────────────────────────────────────────────────

def _say_line(args: dict) -> str:
    line_id  = _s(args.get("line_id", "line"))
    blocking = _b(args.get("blocking", True))
    return (
        f'SendMessage("PlayLine", "{line_id}", SendMessageOptions.DontRequireReceiver);\n'
        f'if ({blocking}) yield return new WaitUntil(\n'
        f'    () => (GetComponent<AudioSource>() == null || !GetComponent<AudioSource>().isPlaying));'
    )


def _play_dialog_audio(args: dict) -> str:
    path   = _s(args.get("path", "Dialog/line"))
    volume = _f(args.get("volume", 1.0))
    return (
        f'var _dlgClip = Resources.Load<AudioClip>("Audio/{path}");\n'
        f'if (_dlgClip != null) {{\n'
        f'    var _dlgSrc = GetComponent<AudioSource>() ?? gameObject.AddComponent<AudioSource>();\n'
        f'    _dlgSrc.volume = {volume};\n'
        f'    _dlgSrc.PlayOneShot(_dlgClip);\n'
        f'    yield return new WaitForSeconds(_dlgClip.length + 0.1f);\n'
        f'}}'
    )


def _gesture(args: dict) -> str:
    gtype    = _s(args.get("type", "Idle"))
    duration = _f(args.get("duration", 1.0))
    return (
        f'var _ganim = GetComponent<Animator>();\n'
        f'if (_ganim != null) _ganim.SetTrigger("{gtype}");\n'
        f'yield return new WaitForSeconds({duration});'
    )


def _look_at(args: dict) -> str:
    target   = _s(args.get("target", "Player"))
    duration = float(args.get("duration", 0.0))
    if duration <= 0:
        return (
            f'var _laT = GameObject.Find("{target}");\n'
            f'if (_laT != null) {{\n'
            f'    var _laD = _laT.transform.position - transform.position; _laD.y = 0f;\n'
            f'    if (_laD != Vector3.zero) transform.rotation = Quaternion.LookRotation(_laD);\n'
            f'}}'
        )
    dur = _f(duration)
    return (
        f'var _laT = GameObject.Find("{target}");\n'
        f'if (_laT != null) {{\n'
        f'    float _laEl = 0f;\n'
        f'    while (_laEl < {dur}) {{\n'
        f'        var _laD = _laT.transform.position - transform.position; _laD.y = 0f;\n'
        f'        if (_laD != Vector3.zero)\n'
        f'            transform.rotation = Quaternion.Slerp(\n'
        f'                transform.rotation, Quaternion.LookRotation(_laD), _laEl / {dur});\n'
        f'        _laEl += Time.deltaTime; yield return null;\n'
        f'    }}\n'
        f'}}'
    )


def _end_dialog(args: dict) -> str:
    return_idle = _b(args.get("return_to_idle", True))
    return (
        f'SendMessage("OnDialogEnd", SendMessageOptions.DontRequireReceiver);\n'
        f'if ({return_idle}) {{\n'
        f'    var _edAnim = GetComponent<Animator>();\n'
        f'    if (_edAnim != null) _edAnim.SetTrigger("Idle");\n'
        f'}}'
    )


# ── Wiring atoms (Unity-only seam primitives) ────────────────────────────────
# These generate the C# subscription blocks that go inside GameWiring.Awake()
# or GameWiring.Update(). Variable names match GameWiring's _kind field naming.

def _wire_health_to_hud(args: dict) -> str:
    hv = _s(args.get("health_var", "_health"))
    uv = _s(args.get("hud_var",    "_hud"))
    return (
        f"if ({hv} != null && {uv} != null)\n"
        f"{{\n"
        f"    {hv}.OnHealthChanged += {uv}.SetHealth;\n"
        f"    {uv}.SetHealth({hv}.CurrentHealth, {hv}.MaxHealth);\n"
        f"}}"
    )


def _wire_health_to_fsm(args: dict) -> str:
    hv = _s(args.get("health_var",       "_health"))
    fv = _s(args.get("statemachine_var", "_statemachine"))
    return (
        f"if ({hv} != null && {fv} != null)\n"
        f"    {hv}.OnDeath += {fv}.Kill;"
    )


def _wire_health_to_audio(args: dict) -> str:
    """Only wires roles with a real matched cue id (args value is not None) —
    a concept with no diegetic cue for a role gets that line omitted, never a
    fabricated generic id. See src/audio/resolver.py::match_cue_for_role."""
    hv = _s(args.get("health_var", "_health"))
    av = _s(args.get("audio_var",  "_audio"))
    roles = (
        ("cue_hurt",  "_onDamaged", "_", "OnDamaged"),
        ("cue_death", "_onDeath",   "",  "OnDeath"),
        ("cue_heal",  "_onHealed",  "_", "OnHealed"),
    )
    lines: list[str] = []
    subs:  list[str] = []
    for arg_name, delegate, lam_param, evt in roles:
        cue = args.get(arg_name)
        if not cue:
            continue
        lam = f"{lam_param} => {av}.Play(\"{_s(cue)}\")" if lam_param else f"() => {av}.Play(\"{_s(cue)}\")"
        lines.append(f"    {delegate} = {lam};")
        subs.append(f"    {hv}.{evt} += {delegate};")
    if not lines:
        return "// No matched audio cue for hurt/death/heal — Health→Audio seam omitted."
    body = "\n".join(lines + subs)
    return f"if ({hv} != null && {av} != null)\n{{\n{body}\n}}"


def _wire_fsm_to_animator(args: dict) -> str:
    fv = _s(args.get("statemachine_var", "_statemachine"))
    av = _s(args.get("animator_var",     "_animator"))
    return (
        f"if ({fv} != null && {av} != null)\n"
        f"    {fv}.OnStateChanged += OnFSMStateChanged;"
    )


def _wire_input_to_controller(args: dict) -> str:
    iv = _s(args.get("input_var",      "_inputSO"))
    cv = _s(args.get("controller_var", "_controller"))
    return (
        f"if ({iv} != null && {cv} != null)\n"
        f"    {cv}.SetInputReader({iv});"
    )


def _wire_input_to_fsm(args: dict) -> str:
    iv = _s(args.get("input_var",        "_inputSO"))
    fv = _s(args.get("statemachine_var", "_statemachine"))
    return (
        f"if ({iv} != null && {fv} != null)\n"
        f"{{\n"
        f"    _onFSMJump = () => {fv}.SetJump(true);\n"
        f"    {iv}.OnMove += {fv}.SetMove;\n"
        f"    {iv}.OnJump += _onFSMJump;\n"
        f"}}"
    )


def _wire_grounded_bridge(args: dict) -> str:
    cv = _s(args.get("controller_var",   "_controller"))
    fv = _s(args.get("statemachine_var", "_statemachine"))
    av = _s(args.get("animator_var",     "_animator"))
    return (
        f"if ({cv} != null)\n"
        f"{{\n"
        f"    bool grounded = {cv}.IsGrounded;\n"
        f"    {fv}?.SetGrounded(grounded);\n"
        f"    {av}?.SetGrounded(grounded);\n"
        f"}}"
    )


def _objective_seam(args: dict, *, source_key: str, source_default: str,
                    event: str, lambda_sig: str, delegate_default: str) -> str:
    """Shared shape for the three objective seams: store the delegate in a field
    so OnDestroy can unsubscribe the *same* instance, then subscribe.

    `objective_id` is required. The caller only ever passes an id it matched
    against an authored objective; with no match there is nothing honest to
    report, so the seam is omitted with a comment rather than inventing an id."""
    sv  = _s(args.get(source_key, source_default))
    ov  = _s(args.get("objectives_var", "_objectives"))
    dv  = _s(args.get("delegate_var", delegate_default))
    oid = args.get("objective_id")
    if not oid:
        return (f"// No authored objective matched this source - "
                f"{source_key.replace('_var', '')} -> objectives seam omitted.")
    return (
        f"if ({sv} != null && {ov} != null)\n"
        f"{{\n"
        f"    {dv} = {lambda_sig} => {ov}.ReportProgress(\"{_s(oid)}\");\n"
        f"    {sv}.{event} += {dv};\n"
        f"}}"
    )


def _wire_objectives_to_hud(args: dict) -> str:
    ov = _s(args.get("objectives_var", "_objectives"))
    uv = _s(args.get("hud_var",        "_hud"))
    rm = _s(args.get("refresh_method", "RefreshObjectiveHud"))
    # Created once and shared across every tracker this is emitted for. A
    # project with three missions has three trackers and ONE readout, so the
    # atom runs three times: re-assigning the field each time would leave
    # OnDestroy able to unsubscribe only the last lambda, and the earlier two
    # would keep firing into a destroyed HUD.
    return (
        f"if ({ov} != null && {uv} != null)\n"
        f"{{\n"
        f"    if (_onObjectiveHudProgress == null)\n"
        f"        _onObjectiveHudProgress = (id, progress, required) => {rm}();\n"
        f"    if (_onObjectiveHudComplete == null)\n"
        f"        _onObjectiveHudComplete = id => {rm}();\n"
        f"    {ov}.OnObjectiveProgress += _onObjectiveHudProgress;\n"
        f"    {ov}.OnObjectiveComplete += _onObjectiveHudComplete;\n"
        f"    {rm}();\n"
        f"}}"
    )


def _wire_inventory_to_hud(args: dict) -> str:
    iv = _s(args.get("inventory_var", "_inventory"))
    uv = _s(args.get("hud_var",       "_hud"))
    st = _s(args.get("setter",        "SetCarried"))
    return (
        f"if ({iv} != null && {uv} != null)\n"
        f"{{\n"
        f"    _onInventoryHud = () => {uv}.{st}({iv}.TotalCount);\n"
        f"    {iv}.OnChanged += _onInventoryHud;\n"
        f"    _onInventoryHud();\n"
        f"}}"
    )


def _wire_trigger_to_objectives(args: dict) -> str:
    return _objective_seam(
        args, source_key="trigger_var", source_default="_trigger",
        event="OnActivated", lambda_sig="()",
        delegate_default="_onReachObjective")


def _wire_interactable_to_objectives(args: dict) -> str:
    return _objective_seam(
        args, source_key="interactable_var", source_default="_interactable",
        event="OnInteract", lambda_sig="()",
        delegate_default="_onCollectObjective")


def _wire_spawner_to_objectives(args: dict) -> str:
    return _objective_seam(
        args, source_key="spawner_var", source_default="_spawner",
        event="OnDespawned", lambda_sig="_",
        delegate_default="_onEliminateObjective")


# Events the board raises, and nothing else. A seam is a promise that the method
# is really there (WIRING_SEAMS.md), and an event name that does not exist on the
# emitted class is CS1061 in generated glue — the worst place to debug.
_BOARD_EVENTS = frozenset({
    "OnPiecePlaced", "OnGroupComplete", "OnZoneFilled", "OnCommand", "OnRejected",
})


def _wire_board_to_objectives(args: dict) -> str:
    """Filtered objective seam: the board's events all carry a key, so the
    lambda advances the objective only for the key it is about."""
    bv    = _s(args.get("board_var",      "_board"))
    ov    = _s(args.get("objectives_var", "_objectives"))
    dv    = _s(args.get("delegate_var",   "_onBoardObjective"))
    event = _s(args.get("event_name",     "OnPiecePlaced"))
    key   = args.get("match_key")
    oid   = args.get("objective_id")
    if not oid or not key:
        return ("// No authored objective matched a board group - "
                "board -> objectives seam omitted.")
    if event not in _BOARD_EVENTS:
        return (f"// Board raises no event named {event!r} - "
                f"board -> objectives seam omitted.")
    try:
        amount = max(1, int(args.get("amount", 1)))
    except (TypeError, ValueError):
        amount = 1
    return (
        f"if ({bv} != null && {ov} != null)\n"
        f"{{\n"
        f"    {dv} = key => {{ if (key == \"{_s(key)}\") "
        f"{ov}.ReportProgress(\"{_s(oid)}\", {amount}); }};\n"
        f"    {bv}.{event} += {dv};\n"
        f"}}"
    )


def _wire_trigger_to_save(args: dict) -> str:
    tv = _s(args.get("trigger_var",  "_trigger"))
    sv = _s(args.get("saveload_var", "_saveload"))
    return (
        f"if ({tv} != null && {sv} != null)\n"
        f"    {tv}.gameObject.AddComponent<CheckpointSaveRelay>().Init({sv});"
    )


# ── Dispatch table ────────────────────────────────────────────────────────────

_SNIPPETS: dict[str, Any] = {
    # movement
    "MoveTo":             _move_to,
    "MoveToEntity":       _move_to_entity,
    "RotateTo":           _rotate_to,
    "Teleport":           _teleport,
    "Follow":             _follow,
    # animation
    "PlayAnimation":      _play_animation,
    "StopAnimation":      _stop_animation,
    "BlendAnimation":     _blend_animation,
    # camera
    "CameraLookAt":       _camera_look_at,
    "CameraShake":        _camera_shake,
    "CameraOffset":       _camera_offset,
    "CameraFOV":          _camera_fov,
    # audio
    "PlaySFX":            _play_sfx,
    "PlayMusic":          _play_music,
    "StopMusic":          _stop_music,
    # fx
    "SpawnParticle":      _spawn_particle,
    "LightFlash":         _light_flash,
    "ScreenFade":         _screen_fade,
    # logic
    "Wait":               _wait,
    "SetFlag":            _set_flag,
    "TriggerEvent":       _trigger_event,
    "ConditionCheck":     _condition_check,
    # entity
    "SpawnEntity":        _spawn_entity,
    "Despawn":            _despawn,
    "SetHealth":          _set_health,
    "EquipWeapon":        _equip_weapon,
    # controller
    "Move":               _move,
    "Rotate":             _rotate,
    "Jump":               _jump,
    "Sprint":             _sprint,
    "Crouch":             _crouch,
    "Look":               _look,
    # hud
    "ShowHUD":            _show_hud,
    "HideHUD":            _hide_hud,
    "UpdateHealth":       _update_health,
    "UpdateAmmo":         _update_ammo,
    "UpdateObjective":    _update_objective,
    # physics
    "ApplyForce":         _apply_force,
    "ApplyImpulse":       _apply_impulse,
    "SetGravity":         _set_gravity,
    "ToggleCollision":    _toggle_collision,
    # input
    "BindAction":         _bind_action,
    "BindAxis":           _bind_axis,
    "ReadInput":          _read_input,
    "MapInputToEvent":    _map_input_to_event,
    # model
    "SetModel":           _set_model,
    "SetMaterial":        _set_material,
    "SetLOD":             _set_lod,
    "SetAnimationState":  _set_animation_state,
    # dialog
    "SayLine":            _say_line,
    "PlayDialogAudio":    _play_dialog_audio,
    "Gesture":            _gesture,
    "LookAt":             _look_at,
    "EndDialog":          _end_dialog,
    # wiring (Unity-only seam primitives)
    "WireHealthToHUD":        _wire_health_to_hud,
    "WireHealthToFSM":        _wire_health_to_fsm,
    "WireHealthToAudio":      _wire_health_to_audio,
    "WireFSMToAnimator":      _wire_fsm_to_animator,
    "WireInputToController":  _wire_input_to_controller,
    "WireInputToFSM":         _wire_input_to_fsm,
    "WireGroundedBridge":     _wire_grounded_bridge,
    "WireTriggerToSave":      _wire_trigger_to_save,
    "WireTriggerToObjectives":      _wire_trigger_to_objectives,
    "WireInteractableToObjectives": _wire_interactable_to_objectives,
    "WireSpawnerToObjectives":      _wire_spawner_to_objectives,
    "WireBoardToObjectives":        _wire_board_to_objectives,
    "WireObjectivesToHUD":          _wire_objectives_to_hud,
    "WireInventoryToHUD":           _wire_inventory_to_hud,
}


def unity_atom_snippet(atom: str, args: dict | None = None) -> str:
    """Return a C# coroutine snippet for the given atom name + bound args.

    Returns a comment if the atom name is not in the dispatch table so the
    caller can still emit something useful without crashing the whole generator.
    """
    fn = _SNIPPETS.get(atom)
    if fn is None:
        return f"// TODO: atom '{atom}' has no Unity backend snippet"
    return fn(args or {})


def unity_coroutine_body(invocations: list[dict]) -> str:
    """Assemble a list of AtomInvocation dicts into a single IEnumerator body.

    Each multi-line snippet is wrapped in a `{ }` block so that repeated atoms
    of the same type (two MoveTo, two ScreenFade, etc.) don't produce duplicate
    local-variable declarations in the same method scope — which is a C# error.
    Single-line snippets (yield return ..., simple statements) are emitted as-is.
    """
    lines: list[str] = []
    for inv in invocations:
        atom  = inv.get("atom", "")
        args  = inv.get("args", {})
        label = inv.get("label", "")
        if label:
            lines.append(f"// {label}")
        snippet = unity_atom_snippet(atom, args)
        if "\n" in snippet:
            lines.append("{")
            for sl in snippet.split("\n"):
                lines.append(("    " + sl) if sl.strip() else "")
            lines.append("}")
        else:
            lines.append(snippet)
        lines.append("")
    return "\n".join(lines).rstrip()
