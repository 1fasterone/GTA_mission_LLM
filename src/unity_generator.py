"""
Unity Project Generator — Step 6.
Compiles UnitySchema -> full Unity project folder tree.
No LLM involved — deterministic from schema.
"""

import hashlib
import json
import logging
import math
import os
import re
from pathlib import Path

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, StrictUndefined

from .unity_schema import (ProceduralMesh, UnitySchema, UnityScript,
                           is_embodied)
from .unity_asset_resolver import resolve_assets
from .unity_entity_asset_resolver import (resolve_entity_assets,
                                          texture_dirs_with_res_root_for)
from .unity_model_resolver import resolve_default_models, models_for_schema
from .unity_layout import terrain_bounds
from .unity_layout_archetype import pick_player_entity
from .micro.code_fields import CodeField, lint_extensible, _code_mask

_log = logging.getLogger(__name__)


def _render_credits_md(used_models: dict) -> str | None:
    """CREDITS.md content for every non-CC0 Sketchfab pick in `used_models`,
    or None if there are none (CC0 needs no attribution, and Kenney/default
    picks carry no license obligation this pipeline tracks).

    Pure string formatting over the already-resolved `used_models` — no
    network, deterministic given its input — so it's safe to call from inside
    generate()/write() without touching the determinism boundary.
    """
    credited = sorted(
        (m for m in used_models.values()
         if m.source == "sketchfab" and (m.license or "cc0") != "cc0"),
        key=lambda m: m.attribution or "")
    if not credited:
        return None
    lines = ["# Credits", "",
             "Third-party assets used in this project, per their license terms:", ""]
    for m in credited:
        lines.append(f"- {m.attribution}" if m.attribution
                     else f"- {m.dest_rel} ({m.license})")
    lines.append("")
    return "\n".join(lines)


def _apply_model_overrides(resolved: dict, overrides: dict[str, str] | None) -> dict:
    """Inject Blender-built FBX paths into the resolved model catalog.

    For kinds already in the catalog: swaps only the `src` path, keeping the
    catalog's dest_rel / resource_path / rig / scale unchanged.
    For kinds NOT yet in the catalog (no static fallback file existed): bootstraps
    a complete ResolvedModel so the copy + .meta + ModelSwap chain still fires.
    Silently skips any entry whose FBX file doesn't exist on disk yet.
    """
    if not overrides:
        return resolved
    from .unity_model_resolver import ResolvedModel
    _RES_ROOT = "Models/Default"
    # player/enemy ship a real Unity Humanoid skeleton from the Blender rigger, so
    # they import as Humanoid (and humanoid clips retarget). Everything else
    # (vehicle/prop) carries a vehicle/no skeleton -> Generic, never Humanoid.
    for kind, src in overrides.items():
        if not src or not Path(src).is_file():
            continue
        src_abs = str(Path(src).resolve())
        rig = "humanoid" if kind in ("player", "enemy") else "generic"
        if kind in resolved:
            update: dict[str, object] = {"src": src_abs, "rig": rig}
            # Generic models can't use humanoid retarget clips — drop them so the
            # importer doesn't choke on mismatched animation files.
            if rig != "humanoid":
                update["clips"] = {}
            resolved[kind] = resolved[kind].model_copy(update=update)
        else:
            fname    = Path(src).name
            dest_rel = f"{_RES_ROOT}/{kind.capitalize()}/{fname}"
            resolved[kind] = ResolvedModel(
                kind          = kind,
                src           = src_abs,
                dest_rel      = dest_rel,
                resource_path = dest_rel.rsplit(".", 1)[0],
                rig           = rig,
                scale         = 1.0,
            )
    return resolved


# Concept keywords that imply an atmosphere/sky is part of the game feel —
# gates shipping the vendored URP-Atmosphere scattering feature (MIT, sinnwrig;
# see templates/unity/vendor/URPAtmosphere/VENDORED.md).
_ATMOSPHERE_WORDS = frozenset({
    "atmosphere", "atmospheric", "sky", "skies", "altitude", "aerial",
    "airborne", "stratosphere", "stratospheric", "cloud", "clouds",
    "horizon", "planet", "planetary", "freefall", "skydiver", "skydiving",
    "wingsuit", "jetstream",
})


def wants_atmosphere(schema) -> bool:
    """Deterministic gate: 3D URP project whose concept implies a real sky.

    Scans the compressed schema fields AND the original Step-2 inputs
    (schema.meta["user_prompt"]/["concept"]) — the Step-2 LLM elaboration
    routinely compresses a rich brief down to a terse schema.description
    that drops style/atmosphere words the user actually wrote (e.g. "sky"),
    which silently starved this gate even when the user's own prompt asked
    for one. The raw prompt is the ground truth for what the user wants.
    """
    if schema.render_pipeline != "URP" or schema.game_type not in ("3D", "VR"):
        return False
    if getattr(schema, "locomotion", "walk") == "fly":
        return True
    meta = getattr(schema, "meta", None) or {}
    text = " ".join([
        schema.description or "",
        " ".join(schema.scenes or []),
        " ".join(sp.description or "" for sp in getattr(schema, "scene_specs", []) or []),
        str(meta.get("user_prompt") or ""),
        str(meta.get("concept") or ""),
    ]).lower()
    words = set(re.split(r"[^a-z]+", text))
    return bool(words & _ATMOSPHERE_WORDS)


def _environment_ctx(time_of_day: str, fog, fog_color, sun_intensity,
                     lighting: dict, bg: dict) -> dict:
    """Sun + fog template context for one environment (project or per-scene).

    "day" / empty keeps the historical style lighting byte-for-byte; other
    times of day swing the sun and tint it. Pure function — the same inputs
    always render the same scene YAML.
    """
    preset = _TIME_OF_DAY_PRESETS.get(time_of_day) if time_of_day else None
    if preset:
        sun_rotation, sun_euler = _sun_rotation(preset["pitch"])
        sun_color = {"r": preset["color"][0], "g": preset["color"][1],
                     "b": preset["color"][2]}
        intensity = preset["intensity"]
    else:
        sun_rotation = "{x: 0.40821788, y: -0.23456968, z: 0.10938163, w: 0.8754261}"
        sun_euler    = "{x: 50, y: -30, z: 0}"
        sun_color    = {"r": lighting["r"], "g": lighting["g"], "b": lighting["b"]}
        intensity    = lighting["intensity"]
    if sun_intensity is not None:
        intensity = sun_intensity
    return {
        "sun_rotation":  sun_rotation,
        "sun_euler":     sun_euler,
        "sun_color":     sun_color,
        "sun_intensity": intensity,
        "fog_enabled":   fog if fog is not None else lighting["fog"],
        "fog_color":     _unity_color(fog_color) if fog_color else bg,
    }


# WorldSpec.weather -> fog on/off. WorldSpec has no direct sun_intensity/fog_color
# equivalent to EnvironmentSpec's — those stay None (the preset/lighting default
# applies), only time_of_day and the derived fog flag are populated.
_WEATHER_FOG_ON = frozenset({"rain", "storm", "snow", "fog"})


def _world_spec_env_fallback(world_spec) -> tuple:
    """Derive (time_of_day, fog, fog_color, sun_intensity) from schema.world_spec
    for _environment_ctx() when schema.environment is absent. Additive fallback
    only — schema.environment always wins when present (no behavior change for
    existing projects). WorldSpec.biome/scale/procedural_rules have no natural
    consumer here and are deliberately left unmapped."""
    if world_spec is None:
        return "", None, None, None
    return world_spec.time_of_day, world_spec.weather in _WEATHER_FOG_ON, None, None


def entity_behavior_class_name(entity_name: str) -> str:
    """The class name an atom-driven entity's generated MonoBehaviour gets —
    single source of truth, since EntitySpawner resolves it at runtime via
    Type.GetType(entity.behavior) (case-sensitive): the entity-behavior
    reconciliation pass below, the file-generation pass further down, and
    any deterministic fixer that pre-assigns `entity.behavior` (e.g.
    src/unity_escort_follow.py) must all agree on the exact same string, or
    AddComponent silently no-ops."""
    parts = re.sub(r"[^a-zA-Z0-9]+", " ", entity_name or "").split()
    return ("".join(p.capitalize() for p in parts) or "Entity") + "Behavior"


# ── GUID generation ────────────────────────────────────────────────────────────
# Deterministic MD5-based GUIDs keep .meta files stable across regenerations.

def _make_guid(seed: str) -> str:
    return hashlib.md5(seed.encode("utf-8")).hexdigest()


def _make_uuid(pn: str, seed: str) -> str:
    """Deterministic UUID-shaped string (version 4, variant 10xx) from project + seed."""
    h = hashlib.md5(f"{pn}/{seed}".encode("utf-8")).hexdigest()
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:32]}"


# ── Camera presets per game type ───────────────────────────────────────────────

_CAMERA_PRESETS = {
    "2D": {
        "position": "{x: 0, y: 0, z: -10}",
        "rotation": "{x: 0, y: 0, z: 0, w: 1}",
        "euler":    "{x: 0, y: 0, z: 0}",
    },
    # Same flat/orthographic framing as "2D" — 2d_topdown is Rigidbody2D too,
    # just free 2-axis movement instead of gravity+jump (see PlayerController.cs.j2).
    "2d_topdown": {
        "position": "{x: 0, y: 0, z: -10}",
        "rotation": "{x: 0, y: 0, z: 0, w: 1}",
        "euler":    "{x: 0, y: 0, z: 0}",
    },
    "2.5D": {
        "position": "{x: 0, y: 3, z: -8}",
        "rotation": "{x: 0.18198, y: 0, z: 0, w: 0.98325}",
        "euler":    "{x: 21, y: 0, z: 0}",
    },
    "3D": {
        "position": "{x: 0, y: 5, z: -8}",
        "rotation": "{x: 0.30072, y: 0, z: 0, w: 0.95372}",
        "euler":    "{x: 35, y: 0, z: 0}",
    },
    "top_down": {
        "position": "{x: 0, y: 15, z: -3}",
        "rotation": "{x: 0.96362, y: 0, z: 0, w: 0.26724}",
        "euler":    "{x: 75, y: 0, z: 0}",
    },
    "isometric": {
        "position": "{x: -10, y: 10, z: -10}",
        "rotation": "{x: 0.30072, y: -0.43388, z: 0.14645, w: 0.83470}",
        "euler":    "{x: 35, y: -45, z: 0}",
    },
    "VR": {
        "position": "{x: 0, y: 1.6, z: 0}",
        "rotation": "{x: 0, y: 0, z: 0, w: 1}",
        "euler":    "{x: 0, y: 0, z: 0}",
    },
}

# ── Script type -> template file ───────────────────────────────────────────────

_SCRIPT_TEMPLATES = {
    "player":           "PlayerController.cs.j2",
    "camera":           "CameraFollow.cs.j2",
    "game_manager":     "GameManager.cs.j2",
    "enemy":            "EnemyAI.cs.j2",
    "collectible":      "Collectible.cs.j2",
    "ui_manager":       "UIManager.cs.j2",
    "audio_manager":    "AudioManager.cs.j2",
    "health":           "HealthSystem.cs.j2",
    "dialogue_manager": "DialogueManager.cs.j2",
    "mesh_builder":     "MeshBuilder.cs.j2",
    "material_palette": "MaterialPalette.cs.j2",
    "custom":           "CustomBehaviour.cs.j2",
    # level_generator is an Editor script — handled separately in generate()
}

# ── Custom-type stub emitter ───────────────────────────────────────────────────
# When the LLM invents a type name in `extra_fields` (e.g. List<BountyContract>)
# that isn't a standard Unity/C# type, the generator auto-emits a minimal
# [System.Serializable] stub class above the MonoBehaviour so the script compiles.

_CS_BUILTIN_TYPES: frozenset[str] = frozenset({
    # C# primitives / value types
    "string", "int", "float", "bool", "double", "long", "short", "byte",
    "char", "object", "void", "uint", "ulong", "ushort", "sbyte", "decimal",
    # Generic collection open names
    "List", "Dictionary", "Array", "Queue", "Stack", "HashSet",
    "LinkedList", "IEnumerable", "IEnumerator", "IList", "IDictionary",
    "ICollection", "IReadOnlyList", "IReadOnlyDictionary",
    # Delegates / events
    "Action", "Func", "Predicate", "EventHandler", "Delegate", "UnityEvent",
    "UnityAction",
    # Unity structs / value types
    "Vector2", "Vector3", "Vector4", "Vector2Int", "Vector3Int",
    "Quaternion", "Color", "Color32",
    "Rect", "RectInt", "Bounds", "BoundsInt", "RangeInt", "RectOffset",
    "Matrix4x4", "Plane", "Ray", "Ray2D",
    "RaycastHit", "RaycastHit2D", "LayerMask", "GUIStyle", "GUIContent",
    "Keyframe", "AnimationCurve",
    # Unity components
    "Transform", "GameObject", "MonoBehaviour", "ScriptableObject",
    "Component", "Behaviour", "Object",
    "Rigidbody", "Rigidbody2D", "Collider", "Collider2D",
    "BoxCollider", "SphereCollider", "CapsuleCollider", "MeshCollider",
    "BoxCollider2D", "CircleCollider2D", "PolygonCollider2D",
    "Animator", "Animation", "AnimationClip", "AnimationState",
    "AudioSource", "AudioClip", "AudioMixer", "AudioMixerGroup",
    "Camera", "Light", "LineRenderer", "TrailRenderer",
    "Material", "Mesh", "MeshRenderer", "MeshFilter", "SkinnedMeshRenderer",
    "Sprite", "SpriteRenderer", "Texture", "Texture2D", "RenderTexture",
    "Shader", "ParticleSystem", "NavMeshAgent", "NavMeshPath",
    # UI
    "Canvas", "CanvasGroup", "CanvasScaler", "GraphicRaycaster",
    "Text", "Image", "Button", "InputField", "Slider", "Toggle",
    "ScrollRect", "Scrollbar", "Dropdown", "RawImage",
    "TextMeshPro", "TMP_Text", "TMP_InputField",
    "UIDocument", "VisualElement",
    # Coroutines / async
    "WaitForSeconds", "WaitForFixedUpdate", "WaitForEndOfFrame",
    "YieldInstruction", "Coroutine", "AsyncOperation",
    # Scene
    "Scene",
    # PhysicMaterial spellings (PhysicsMaterial is the Unity 6 rename)
    "PhysicMaterial", "PhysicsMaterial", "PhysicsMaterial2D",
    # Physics / vehicles / joints — emitting a stub for any of these SHADOWS the
    # real UnityEngine type project-wide (global namespace beats `using
    # UnityEngine`), breaking every micro that uses it (hit live: an LLM
    # extra_field `List<WheelCollider>` stubbed WheelCollider and broke the
    # vehicle controller with CS1061/CS0311 on every member).
    "WheelCollider", "WheelHit", "WheelFrictionCurve", "JointSpring",
    "CharacterController", "ConstantForce", "ArticulationBody",
    "Joint", "FixedJoint", "HingeJoint", "SpringJoint", "ConfigurableJoint",
    "CharacterJoint", "Cloth", "Collision", "Collision2D",
    "ContactPoint", "ContactPoint2D", "EdgeCollider2D", "CompositeCollider2D",
    "TerrainCollider", "Terrain", "TerrainData",
    # More engine components/types LLMs routinely reference in fields
    "RectTransform", "Renderer", "AudioListener", "VideoPlayer",
    "Gradient", "LODGroup", "ReflectionProbe", "WindZone",
    "RuntimeAnimatorController", "AnimatorOverrideController", "Avatar",
    "NavMeshObstacle", "OffMeshLink", "ParticleSystemRenderer",
    "TextMeshProUGUI", "EventSystem",
    # Input System
    "InputAction", "InputActionAsset", "InputActionMap",
    "InputActionReference", "PlayerInput",
    # Cinemachine
    "CinemachineCamera", "CinemachineVirtualCamera",
})

_GENERIC_WRAPPER_RE = re.compile(
    r"^(?:List|IEnumerable|IList|ICollection|IReadOnlyList|Queue|Stack|HashSet)<(.+)>$"
)
_DICT_RE = re.compile(r"^Dictionary<([^,]+),\s*(.+)>$")


def _extract_base_type(type_str: str) -> str:
    """Strip generics/arrays to return the innermost concrete type name."""
    t = type_str.strip().rstrip("[]").strip()
    m = _DICT_RE.match(t)
    if m:
        return _extract_base_type(m.group(2))   # value type is more often custom
    m = _GENERIC_WRAPPER_RE.match(t)
    if m:
        return _extract_base_type(m.group(1))
    return t.split("<")[0].strip()


def _stub_type_names(extra_fields: list,
                     known_types: "frozenset[str]" = frozenset()) -> list[str]:
    """Return base type names for LLM-invented types in extra_fields (first-seen
    order). Types already defined in Unity/C# (_CS_BUILTIN_TYPES) are excluded,
    as are Manager/System/Controller/Handler singletons (those are expected to
    be separate scripts, not local stub classes), and as is anything in
    `known_types` — a name the project already defines elsewhere (a composed
    micro's class name, or another script). Stubbing one of those is CS0101
    (duplicate definition), not CS0246: the field's type ALREADY resolves, so
    no stub is needed at all. Hit live on a RepoFlow `--compose` run —
    TutorialIcon.cs (a `type: custom` script) declared
    `[SerializeField] private WorldInteractable _interactable`, and the
    composed `interactable` micro had already emitted a real
    `WorldInteractable : MonoBehaviour` class of its own."""
    seen: set[str] = set()
    names: list[str] = []
    for f in extra_fields:
        base = _extract_base_type(getattr(f, "type", "") or "")
        if (
            not base
            or base in _CS_BUILTIN_TYPES
            or "<" in base
            or "." in base
            or base in seen
            or base in known_types
            or base.endswith("Manager")
            or base.endswith("System")
            or base.endswith("Controller")
            or base.endswith("Handler")
        ):
            continue
        seen.add(base)
        names.append(base)
    return names


def _collect_custom_type_stubs(extra_fields: list,
                               known_types: "frozenset[str]" = frozenset()) -> list[str]:
    """Return [System.Serializable] stub strings for LLM-invented types in extra_fields.

    Types already defined in Unity/C# (_CS_BUILTIN_TYPES) are skipped, as are
    Manager/System/Controller singletons (those are expected to be separate scripts)
    and anything in `known_types` (already a real class in this project — see
    `_stub_type_names`).
    """
    return [
        f"[System.Serializable]\n"
        f"public class {name}\n"
        f"{{\n"
        f"    // Auto-generated stub — add fields as needed\n"
        f"}}\n"
        for name in _stub_type_names(extra_fields, known_types)
    ]


# ── Invented type via inline `new T(...)` (never touches a declared field) ───
# _collect_custom_type_stubs/_sanitize_stub_type_usage only ever see an
# invented type reached through a DECLARED field -- a constructor call used
# inline, as a bare statement or buried in an argument list, never appears in
# extra_fields at all and is invisible to every check above. Hit live:
#   Graphics.Blit(source, dest, new ShaderPass(profile), 0);
# `ShaderPass` is not a real Unity or URP type -- not a missing `using`, a
# type that does not exist anywhere in the engine. (Graphics.Blit also has no
# overload matching those arguments, but overload resolution is out of reach
# for a regex pass; the invented TYPE is the part checked here.)
#
# `_EXPRESSION_BUILTIN_TYPES` widens `_CS_BUILTIN_TYPES` with common BCL
# utility/exception types that legitimately appear in a `new T(...)`
# EXPRESSION but would never be the declared TYPE of a `[SerializeField]`
# field -- so they were never candidates for the narrower, field-oriented list.
_EXPRESSION_BUILTIN_TYPES: frozenset[str] = _CS_BUILTIN_TYPES | frozenset({
    "Exception", "ArgumentException", "ArgumentNullException",
    "InvalidOperationException", "NotImplementedException",
    "NotSupportedException", "IndexOutOfRangeException", "NullReferenceException",
    "StringBuilder", "Regex", "TimeSpan", "DateTime", "Random",
    "WaitUntil", "WaitWhile", "GUILayoutOption", "Tuple", "ValueTuple",
    "Guid", "Uri",
})
_NEW_EXPR_RE = re.compile(r"\bnew\s+([A-Za-z_]\w*)\s*(?:<[^>()]*>)?\s*\(")


def _invented_constructor_type(name: str, known_types: "frozenset[str]") -> bool:
    """True if `name` (the type argument of a `new name(...)` expression) is
    not `_EXPRESSION_BUILTIN_TYPES`, not in `known_types` (every sibling
    script and composed micro class name -- same ground truth
    `_sanitize_stub_type_usage` uses), and not a Manager/System/Controller/
    Handler-suffixed name (those are expected to be separate scripts this
    check cannot see the full project roster of -- same exemption
    `_stub_type_names` applies)."""
    return not (
        name in _EXPRESSION_BUILTIN_TYPES
        or name in known_types
        or name.endswith("Manager")
        or name.endswith("System")
        or name.endswith("Controller")
        or name.endswith("Handler")
    )


def _strip_invented_constructors(body: str, known_types: "frozenset[str]") -> str:
    """Drop any physical line constructing a type nobody declares anywhere —
    see `_sanitize_invented_constructor_calls` for the live bug this exists
    for. There is no field to null out here and no stub that would make the
    call meaningful in place, so the whole statement goes."""
    if not body or "new " not in body:
        return body
    out = []
    for line in body.splitlines(keepends=True):
        bad = any(_invented_constructor_type(m.group(1), known_types)
                  for m in _NEW_EXPR_RE.finditer(line))
        if not bad:
            out.append(line)
    return "".join(out)


def _sanitize_invented_constructor_calls(script: UnityScript,
                                         known_types: "frozenset[str]" = frozenset()) -> None:
    """Strip any statement that constructs a type nobody declares anywhere:
    not `_EXPRESSION_BUILTIN_TYPES`, not `known_types` (every sibling script
    and composed micro class name -- same ground truth `_sanitize_stub_type_
    usage` uses), and not a Manager/System/Controller/Handler-suffixed name
    (those are expected to be separate scripts this pass cannot see the full
    project roster of -- same exemption `_stub_type_names` applies). There is
    no field to null out here and no stub that would make the call
    meaningful in place, so the statement is dropped outright rather than
    half-repaired."""
    script.awake_extra        = _strip_invented_constructors(script.awake_extra, known_types)
    script.update_extra       = _strip_invented_constructors(script.update_extra, known_types)
    script.fixed_update_extra = _strip_invented_constructors(script.fixed_update_extra, known_types)
    for m in script.extra_methods:
        m.body = _strip_invented_constructors(m.body, known_types)


# ── Built-in Component-lookup call, wrong argument count ──────────────────────
# GetComponent and its siblings are exempted from every other check in this
# module (_BARE_CALLABLE_ALLOW, _MB_COMMON_MEMBERS) because they legitimately
# exist on every MonoBehaviour/Component/GameObject -- but "exists" was never
# "called with a valid argument count." Hit-live shape: a script calling
# GetComponent<T>() with an extra argument that overload doesn't have
# (CS1501) -- the built-in-API twin of _COMPOSED_METHOD_ARITY, scoped to the
# fixed, well-known Component-lookup family rather than attempting general
# Unity API arity checking (unbounded, no closed set to check against).
_MB_METHOD_ARITY: dict[str, frozenset[int]] = {
    "GetComponent":             frozenset({0, 1}),   # <T>() | (Type) | (string)
    "GetComponents":            frozenset({0, 1}),   # <T>() | <T>(List<T> results)
    "GetComponentInChildren":   frozenset({0, 1}),   # <T>() | <T>(bool includeInactive)
    "GetComponentsInChildren":  frozenset({0, 1}),   # <T>() | <T>(bool) | <T>(List<T>)
    "GetComponentInParent":     frozenset({0, 1}),   # <T>() | <T>(bool includeInactive)
    "GetComponentsInParent":    frozenset({0, 1}),   # <T>() | <T>(bool) | <T>(List<T>)
    "TryGetComponent":          frozenset({1, 2}),   # <T>(out T) | (Type, out Component)
}
_MB_METHOD_CALL_RE = re.compile(
    r"\b(" + "|".join(_MB_METHOD_ARITY) + r")\s*(?:<[^<>()]*>)?\s*\("
)


def _strip_builtin_call_arity(body: str) -> str:
    """Drop any physical line calling a GetComponent-family method with an
    argument count that method's overloads don't have. See `_MB_METHOD_ARITY`
    for the real overload shapes this validates against."""
    if not body:
        return body
    out = []
    for line in body.splitlines(keepends=True):
        bad = False
        for m in _MB_METHOD_CALL_RE.finditer(line):
            allowed = _MB_METHOD_ARITY[m.group(1)]
            count = _call_arg_count(line, m.end() - 1)
            if count is not None and count not in allowed:
                bad = True
                break
        if not bad:
            out.append(line)
    return "".join(out)


def _sanitize_builtin_component_call_arity(script: UnityScript) -> None:
    """Strip a statement calling GetComponent<T>() (or a sibling —
    GetComponents/*InChildren/*InParent/TryGetComponent) with an argument
    count that method's real overloads don't have — CS1501, a real member,
    wrong arity, invisible to every other check here since these names are
    always exempted as legitimate (see `_MB_METHOD_ARITY`'s docstring)."""
    script.awake_extra        = _strip_builtin_call_arity(script.awake_extra)
    script.update_extra       = _strip_builtin_call_arity(script.update_extra)
    script.fixed_update_extra = _strip_builtin_call_arity(script.fixed_update_extra)
    for m in script.extra_methods:
        m.body = _strip_builtin_call_arity(m.body)


# ── Field holding a StartCoroutine() result, declared with the wrong type ───
# StartCoroutine's return type is fixed by the engine API -- always
# `UnityEngine.Coroutine`, never `IEnumerator` -- so a field the LLM declared
# as anything else and then assigned that call's result into is an
# unconditional CS0029 regardless of what the field was meant to represent.
# Hit live: TimelineController.cs declared `_transitionCoroutine` as
# `System.Collections.IEnumerator` (its own Tooltip called it "the coroutine
# reference to cancel if needed" -- the LLM confused the routine's BODY type
# with the handle StartCoroutine hands back) then assigned
# `_transitionCoroutine = StartCoroutine(FadeToBlack(_fadeDuration));`.
_START_COROUTINE_ASSIGN_RE = re.compile(
    r"^\s*(\w+)\s*=\s*StartCoroutine\s*\(.*\)\s*;\s*$", re.MULTILINE)


def _sanitize_coroutine_field_type(script: UnityScript) -> None:
    """Retype any field directly assigned a `StartCoroutine(...)` result to
    `Coroutine` -- the only type that call ever actually returns."""
    if not script.extra_fields:
        return
    bodies = [script.awake_extra, script.update_extra, script.fixed_update_extra]
    bodies += [m.body for m in script.extra_methods]
    blob = "\n".join(b for b in bodies if b)
    if "StartCoroutine" not in blob:
        return
    assigned = {m.group(1) for m in _START_COROUTINE_ASSIGN_RE.finditer(blob)}
    if not assigned:
        return
    for f in script.extra_fields:
        if f.name in assigned and (f.type or "").strip() != "Coroutine":
            f.type = "Coroutine"


# ── Field type qualified with an invented namespace (never resolves) ─────────
# `_stub_type_names` exempts any field type containing a "." on the assumption
# a dot always means "already fully qualified, will resolve" -- true for a
# genuine `UnityEngine.X` / `System.X` spelling, false for a wholesale
# invented root. Hit live: TimelineController.cs declared
# `[SerializeField] private Timeline.TimelineClip _timelineClip` -- Unity's
# real namespace is `UnityEngine.Timeline`, not a bare `Timeline`, so this was
# CS0246 on the FIELD DECLARATION itself, one step before
# `_sanitize_stub_type_usage` (CS1061) ever gets a chance to run.
_TRUSTED_NAMESPACE_ROOTS: frozenset[str] = frozenset({
    "UnityEngine", "UnityEditor", "System", "TMPro", "Unity", "Cinemachine",
})


def _sanitize_invented_namespace_field_types(script: UnityScript,
                                             known_types: "frozenset[str]" = frozenset()) -> None:
    """Flatten a field's dotted type to its bare leaf name when the dotted
    prefix is neither a real namespace root (`_TRUSTED_NAMESPACE_ROOTS`) nor a
    class this project already defines (`known_types`) -- the same "prefer
    inert over broken" repair `_collect_custom_type_stubs` applies to a bare
    invented name, extended to a dotted one so it reaches that stub machinery
    at all instead of being waved through as already-qualified. Must run
    BEFORE `_sanitize_stub_type_usage`/`_collect_custom_type_stubs`: both key
    off `_extract_base_type(f.type)`, which only sees the invented type once
    it is flat."""
    for f in script.extra_fields:
        raw = getattr(f, "type", "") or ""
        base = _extract_base_type(raw)
        if "." not in base:
            continue
        prefix = base.split(".", 1)[0]
        if prefix in known_types or prefix in _TRUSTED_NAMESPACE_ROOTS:
            continue
        leaf = base.rsplit(".", 1)[-1]
        f.type = raw.replace(base, leaf, 1)


def _sanitize_stub_type_usage(script: UnityScript,
                              known_types: "frozenset[str]" = frozenset()) -> None:
    """LLM-invented field types get an EMPTY auto-generated stub class (see
    _collect_custom_type_stubs) — that alone prevents CS0246 (type not found),
    but the LLM's own code fragments routinely still call methods on that field
    (e.g. a hand-rolled polymorphic FSM state object with .Update()/.NextState())
    or assign it an invented enum-like default (e.g. `= Idle`) — both guaranteed
    CS1061 / CS0103 since the stub has zero members (hit live: ClimbStateFSM.cs,
    2x CS0103 + 3x CS1061). Deterministically strip any statement that calls a
    method on a stub-typed field (mirrors _sanitize_singleton_calls' line-level
    strip for undefined singletons) and clear an unresolvable default so the
    field just defaults to null — the same "prefer inert over broken" contract
    _collect_custom_type_stubs already uses for the type itself."""
    stub_names = set(_stub_type_names(script.extra_fields, known_types))
    if not stub_names:
        return

    _bare_ident_re = re.compile(r"^[A-Za-z_]\w*$")
    stub_fields: set[str] = set()
    for f in script.extra_fields:
        if _extract_base_type(f.type or "") not in stub_names:
            continue
        stub_fields.add(f.name)
        default = (f.default or "").strip()
        if default and default not in ("null", "true", "false") and _bare_ident_re.match(default):
            f.default = None   # e.g. `= Idle` — Idle is never declared anywhere

    if not stub_fields:
        return
    # `\??\.` (not `\.`) — a stub field reached through the C# null-conditional
    # operator (`_activeTimeline?.Play()`) is exactly how the model guards a
    # SerializeField it expects to be assigned in the inspector, and was the
    # live failure shape (TimelineController.cs CS1061: stub `Timeline` has no
    # `Play()`) that a bare `\.` silently let through uncaught.
    call_re = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in stub_fields) + r")\s*\??\.\s*\w+\s*\(")

    def _strip(body: str) -> str:
        if not body:
            return body
        return "".join(line for line in body.splitlines(keepends=True) if not call_re.search(line))

    script.awake_extra        = _strip(script.awake_extra)
    script.update_extra       = _strip(script.update_extra)
    script.fixed_update_extra = _strip(script.fixed_update_extra)
    for m in script.extra_methods:
        m.body = _strip(m.body)


# Verified public API of composed micro classes (--compose mode) a custom script
# might reach via GetComponent<T>()/GameContext.Instance.Get<T>(). Maximal union
# across every variant of that kind — a member valid for one variant but not the
# active one survives unstripped (acceptable false-negative); a member that is
# never real on ANY variant (the actual failure mode) never does. Extend this
# table as more of these calls get hit live; an unlisted composed class is left
# untouched (nothing to validate against) — this drifted out of sync with
# GAMECONTEXT_SCHEMA_GUIDANCE (src/unity_prompts.py) once already: LightingDirector
# was advertised there as Get<T>()-able with no entry here, so any invented member
# on it went uncaught silently rather than getting stripped. Covers every type
# GAMECONTEXT_SCHEMA_GUIDANCE tells the LLM it may Get<T>() (plus GameManager,
# validated separately by _sanitize_singleton_calls) — keep the two lists in sync.
_COMPOSED_CLASS_API: dict[str, frozenset[str]] = {
    # Controller.cs.j2 PUBLIC PROPERTIES section.
    "PlayerController": frozenset({
        "IsGrounded", "SpeedKmh", "ForwardSpeed", "SteerNormalized",
        "ThrottleNormalized", "WheelRadius", "ThrottleLevel", "AirSpeedKmh",
        "IsBlocking", "IsInvincible", "IsAiming", "CurrentAmmo", "IsReloading",
        "CurrentSpeed",
        # ControllerMultiMode.cs.j2 (variant "multi_mode") — Control Surface
        # Stack, see docs/CONTROL_SURFACE_STACK.md.
        "CurrentMode", "SwitchMode", "CycleMode", "OnModeChanged",
    }),
    # Health.cs.j2 public surface — Die() is `private void Die()`, only reachable
    # indirectly via TakeDamage driving _current to 0 (hit live: OxygenManager.cs
    # called GameContext.Instance.Get<PlayerHealth>().Die() — CS0122).
    "PlayerHealth": frozenset({
        "CurrentHealth", "MaxHealth", "TakeDamage", "Heal",
        "OnHealthChanged", "OnDamaged", "OnHealed", "OnDeath",
    }),
    # CameraRig.cs.j2 — zero public members beyond MonoBehaviour; ANY member
    # access must come from _MB_COMMON_MEMBERS or it's invented.
    "PlayerCamera": frozenset(),
    # StateMachine.cs.j2 public surface.
    "PlayerFSM": frozenset({
        "State", "OnStateChanged", "CurrentState", "IsIn",
        "SetMove", "SetJump", "SetGrounded", "Kill",
    }),
    # HUD.cs.j2 public surface.
    "GameHud": frozenset({
        "SetHealth", "SetAmmo", "SetScore", "Show", "Hide", "ShowLine", "SetPaused",
    }),
    # Menu.cs.j2 public surface.
    "PauseMenu": frozenset({
        "OnItemSelected", "Items", "Select", "Activate", "Show", "Hide",
    }),
    # SaveSystem.cs.j2 public surface (union across json_file/slotted/checkpoint variants).
    "SaveSystem": frozenset({
        "Data", "SlotCount", "Save", "Load", "HasSave", "DeleteSave",
        "SetCheckpoint", "Respawn",
    }),
    # Body.cs.j2 public surface — mass/drag are private serialized fields.
    "PhysicsBody": frozenset({"OnImpact", "OnTriggerEnterEvent"}),
    # Interactable.cs.j2 public surface (hit live: InventoryManager.cs called
    # GameContext.Instance.Get<WorldInteractable>().CanPickup()/.ItemName —
    # neither exists; only Interact() + the three events are real — CS1061 x2).
    "WorldInteractable": frozenset({"OnPrompt", "OnInteract", "OnToggled", "Interact"}),
    # TriggerZone.cs.j2 — the entire public surface is Reset().
    "SceneTrigger": frozenset({"Reset"}),
    # Animator.cs.j2 — SetGrounded/CrossFade are fixed; Play{Param}()/
    # Set{Param}(value) are dynamic per-project animator parameters not known
    # at this point in the pipeline, so they're matched by shape instead of
    # name (see _ANIMATOR_DYNAMIC_MEMBER_RE / _composed_member_ok).
    "PlayerAnimator": frozenset({"SetGrounded", "CrossFade"}),
    # LightingDirector.cs.j2 public surface — constant across every variant.
    "LightingDirector": frozenset({"Apply", "ApplyEnvironment", "BuildRig",
                                   "RepairMaterials", "KeyLight"}),
}

# PlayerAnimator's per-project dynamic parameter setters/triggers — real names
# aren't known until scene composition, well after this sanitizer runs.
_ANIMATOR_DYNAMIC_MEMBER_RE = re.compile(r"^(?:Play|Set)[A-Z]\w*$")

# Required argument counts for members of _COMPOSED_CLASS_API whose signature
# is the SAME across every variant of the class — the arity companion to the
# membership table above (hit live: `PauseMenu.Activate()` called with none
# of its 1 required arg — a real member, wrong arity, invisible to a
# name-only check). A method is listed here only when every variant that
# has it agrees on its parameter count; anything variant-gated is left out
# on purpose rather than risk a false strip:
#   - SaveSystem's Save/Load/HasSave/DeleteSave take 1 arg (int slot) on the
#     save_slots variant and 0 on every other storage variant — no per-
#     project override exists for SaveSystem the way `hud_api` overrides
#     GameHud's variant-gated membership, so arity here would be a guess.
#   - Widget setters on GameHud (Set<WidgetId>) are per-project dynamic
#     names outside this static table entirely — never reached here.
_COMPOSED_METHOD_ARITY: dict[str, dict[str, frozenset[int]]] = {
    "PlayerHealth": {"TakeDamage": frozenset({1}), "Heal": frozenset({1})},
    "PlayerFSM": {
        "IsIn": frozenset({1}), "SetMove": frozenset({1}),
        "SetJump": frozenset({1}), "SetGrounded": frozenset({1}),
        "Kill": frozenset({0}),
    },
    "GameHud": {
        "SetHealth": frozenset({2}), "SetAmmo": frozenset({2}),
        "SetScore": frozenset({1}), "Show": frozenset({1}),
        "Hide": frozenset({0}), "ShowLine": frozenset({2}),
        "SetPaused": frozenset({1}),
    },
    "PauseMenu": {
        "Select": frozenset({1}), "Activate": frozenset({1}),
        "Show": frozenset({0}), "Hide": frozenset({0}),
    },
    # SetCheckpoint/Respawn exist only on the checkpoint variant, always at
    # this arity — the variant-gated storage methods above are excluded.
    "SaveSystem": {"SetCheckpoint": frozenset({0}), "Respawn": frozenset({0})},
    "WorldInteractable": {"Interact": frozenset({0})},
    "SceneTrigger": {"Reset": frozenset({0})},
    "PlayerAnimator": {
        "SetGrounded": frozenset({1}),
        "CrossFade": frozenset({1, 2}),   # transitionDuration defaults
    },
}

# Members of _COMPOSED_CLASS_API that are C# EVENTS, not callable methods —
# real per the membership table (so _composed_member_ok correctly says they
# exist), but a field-like event's backing delegate is only ever directly
# INVOKABLE from within its OWN declaring class; every other class may only
# subscribe (`+=`/`-=`). Populated only for classes with a CONFIRMED live
# misuse, same discipline as _COMPOSED_METHOD_ARITY above. Hit live,
# InventoryManager.cs(27,112)/(32,112): `gate.OnToggled()` on a
# WorldInteractable parameter — OnToggled is real (Interactable.cs.j2:
# `public event Action<bool> OnToggled;`), just never callable with parens
# from outside (CS1061; see _composed_event_call_ok).
_COMPOSED_CLASS_EVENTS: dict[str, frozenset[str]] = {
    "WorldInteractable": frozenset({"OnPrompt", "OnInteract", "OnToggled"}),
}


def _call_arg_count(line: str, idx: int) -> "int | None":
    """Argument count for the call whose '(' should appear at-or-after
    `idx` (the position right after a matched member name). Returns None
    for a property/field read (no '(' there) or a call whose parens don't
    balance on this single line (e.g. a multi-line argument list) — both
    left unchecked rather than guessed at, matching every other pass in
    this module's false-negatives-over-false-positives stance."""
    n = len(line)
    while idx < n and line[idx] in " \t":
        idx += 1
    if idx >= n or line[idx] != "(":
        return None
    depth = 0
    args = 0
    seen_token = False
    in_str = False
    i = idx
    while i < n:
        c = line[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            seen_token = True
        elif c in "([{":
            depth += 1
            if depth > 1:
                seen_token = True
        elif c in ")]}":
            depth -= 1
            if depth == 0 and c == ")":
                if seen_token:
                    args += 1
                return args
        elif depth == 1 and c == ",":
            args += 1
            seen_token = False
        elif not c.isspace():
            seen_token = True
        i += 1
    return None   # unbalanced on this line — don't guess


def _composed_arity_ok(cls: str, member: str, line: str, after_idx: int) -> bool:
    """True unless `_COMPOSED_METHOD_ARITY` has a fixed arity for cls.member
    AND the call on this line was parsed with a different argument count.
    No entry, or an unparsable call, means "nothing to check" — this is a
    secondary check layered on top of `_composed_member_ok`, never a
    replacement for it."""
    allowed = _COMPOSED_METHOD_ARITY.get(cls, {}).get(member)
    if allowed is None:
        return True
    count = _call_arg_count(line, after_idx)
    if count is None:
        return True
    return count in allowed


def _composed_member_ok(cls: str, member: str) -> bool:
    """A member access on a Get<T>()-bound composed-micro reference is valid if
    it's in that class's verified API, a base MonoBehaviour/Component/Object
    member (always legitimate, never authored by the template itself), or —
    PlayerAnimator only — matches the dynamic Play*/Set* parameter shape."""
    return (member in _COMPOSED_CLASS_API[cls]
            or member in _MB_COMMON_MEMBERS
            or (cls == "PlayerAnimator" and bool(_ANIMATOR_DYNAMIC_MEMBER_RE.match(member))))


def _composed_event_call_ok(cls: str, member: str, line: str, after_idx: int) -> bool:
    """False when `member` is a known EVENT on the composed class `cls`
    (`_COMPOSED_CLASS_EVENTS`) and this call site invokes it with parens —
    `gate.OnToggled()` — rather than subscribing (`+=`/`-=`) or reading it
    bare. An event's backing delegate is only directly invocable from WITHIN
    its declaring class; from any other class (the only kind of call this
    module ever sanitizes) a paren-call is illegal regardless of the member's
    real existence. True (nothing to flag) for every non-event member, and
    for an event referenced any way OTHER than a call — narrow on purpose,
    matching this module's false-negatives-over-false-positives stance."""
    events = _COMPOSED_CLASS_EVENTS.get(cls)
    if not events or member not in events:
        return True
    n = len(line)
    i = after_idx
    while i < n and line[i] in " \t":
        i += 1
    return not (i < n and line[i] == "(")


def _composed_call_invalid(cls: str, member: str, line: str, after_idx: int) -> bool:
    """True when a call to `cls.member` on this line must be stripped: the
    member doesn't exist, exists at the wrong arity, or is an event invoked
    like a method. The one predicate every composed-call call site below
    shares, so the event check added for `_COMPOSED_CLASS_EVENTS` protects
    all of them at once rather than needing to be repeated at each."""
    return (not _composed_member_ok(cls, member)
            or not _composed_arity_ok(cls, member, line, after_idx)
            or not _composed_event_call_ok(cls, member, line, after_idx))


# How a custom script reaches a composed micro class instance: a MonoBehaviour
# sibling via GetComponent<T>(), or any composed system via the GameContext
# service locator (the --compose bridge micros<->prompt-authored scripts use).
_COMPOSED_GETTER_ALT = r"(?:GameContext\.Instance\.Get|GetComponent)"


def _make_composed_call_stripper(composed_units: list | None):
    """Build a `body -> body` closure that strips any statement calling an
    invalid member on a composed micro class — the LLM assumes a method that
    was never authored on the template. Hit live:
      - O2Manager.cs: GetComponent<PlayerController>()?.GetSpeed() — doesn't
        exist, Controller.cs.j2 exposes ForwardSpeed/SpeedKmh instead (CS1061).
      - OxygenManager.cs: `var context = GameContext.Instance.Get<PlayerHealth>();
        ... context.Die();` — Die() is private on Health.cs.j2, only reachable
        via TakeDamage (CS0122). This is the chained-getter call PLUS the
        assign-to-a-variable-then-call-later shape, both handled below.
      - InventoryManager.cs: `var interact = GameContext.Instance.Get<
        WorldInteractable>(); ... interact.CanPickup(); ... interact.ItemName` —
        neither exists on WorldInteractable (CS1061 x2).
      - EngineSound.cs (SkyRace2D): `var pc = GameContext.Instance.Get<
        PlayerController>(); ... pc.currentSpeed` — a bare PROPERTY READ (no
        trailing parens) through a bound variable. The var-bound regex below
        used to require a trailing `(`, so it only ever caught var-bound
        METHOD calls (`pc.GetSpeed()`), never property reads — this line
        reached Unity untouched and was CS1061 (no such member; the real
        template exposes CurrentSpeed). The direct chained-getter regex above
        never had this gap (no trailing-paren requirement), so the two paths
        must stay symmetric.
    Only checked for classes with a verified entry in _COMPOSED_CLASS_API;
    anything else is left alone rather than risk a false strip. Returns None
    when there is nothing to check against, so a caller can skip cheaply
    instead of building a no-op closure — shared by
    `_sanitize_composed_component_calls` (schema.scripts, `type: custom`) and
    `unity_micro_code.sanitize_subsystem_composed_calls` (schema.microgens),
    the two places generated C# can reach a composed sibling class."""
    if not composed_units:
        return None
    known = {u.class_name for u in composed_units if u.class_name in _COMPOSED_CLASS_API}
    if not known:
        return None
    names_alt = "|".join(re.escape(n) for n in known)
    chained_call_re = re.compile(
        _COMPOSED_GETTER_ALT + r"<(" + names_alt + r")>\(\)\s*\??\.\s*(\w+)"
    )
    var_decl_re = re.compile(
        r"\b\w+\s+(\w+)\s*=\s*" + _COMPOSED_GETTER_ALT + r"<(" + names_alt + r")>\(\)"
    )

    def _strip(body: str) -> str:
        if not body:
            return body
        # Variable bindings (`var context = GameContext.Instance.Get<T>();`) can
        # be declared on one line and used on a later one, so resolve them
        # against the whole body before scanning line-by-line.
        var_types = {m.group(1): m.group(2) for m in var_decl_re.finditer(body)}
        var_call_re = None
        if var_types:
            var_alt = "|".join(re.escape(v) for v in var_types)
            # No trailing `(` requirement — must catch property/field READS
            # (`pc.currentSpeed`) as well as method CALLS (`pc.GetSpeed()`),
            # same shape as chained_call_re above.
            var_call_re = re.compile(r"\b(" + var_alt + r")\s*\??\.\s*(\w+)")
        out = []
        for line in body.splitlines(keepends=True):
            bad = any(_composed_call_invalid(m.group(1), m.group(2), line, m.end())
                      for m in chained_call_re.finditer(line))
            if not bad and var_call_re:
                bad = any(_composed_call_invalid(var_types[m.group(1)], m.group(2), line, m.end())
                          for m in var_call_re.finditer(line))
            if not bad:
                out.append(line)
        return "".join(out)

    return _strip


def _sanitize_composed_component_calls(script: UnityScript, composed_units: list | None) -> None:
    """Strip calls to a composed micro class whose verified public API doesn't
    include the called member — see `_make_composed_call_stripper` for the
    live bugs this catches. Only ever applied to `schema.scripts` (`type:
    custom`); `unity_micro_code.sanitize_subsystem_composed_calls` covers the
    same gap for `schema.microgens`."""
    strip = _make_composed_call_stripper(composed_units)
    if strip is None:
        return
    script.awake_extra        = strip(script.awake_extra)
    script.update_extra       = strip(script.update_extra)
    script.fixed_update_extra = strip(script.fixed_update_extra)
    for m in script.extra_methods:
        m.body = strip(m.body)


# Members every MonoBehaviour/Component/Object instance legitimately has — never
# grounds to strip. Shared by _sanitize_composed_component_calls (via
# _composed_member_ok) and _sanitize_project_service_calls — one source of
# truth so a member allowed on one path is allowed on the other.
_MB_COMMON_MEMBERS = frozenset({
    "transform", "gameObject", "enabled", "name", "tag",
    "isActiveAndEnabled", "hideFlags",
    "CompareTag", "GetComponent", "GetComponents", "GetComponentInChildren",
    "GetComponentsInChildren", "GetComponentInParent", "TryGetComponent",
    "SendMessage", "BroadcastMessage", "Invoke", "InvokeRepeating", "CancelInvoke",
    "StartCoroutine", "StopCoroutine", "StopAllCoroutines",
    "ToString", "GetType", "Equals", "GetHashCode",
})


_PUBLIC_SIG_RE = re.compile(r"^\s*public\b")
_SIG_NAME_RE   = re.compile(r"(\w+)\s*\(")


def _custom_script_api(scripts: list[UnityScript]) -> dict[str, frozenset[str]]:
    """A custom script's real externally-reachable surface: PUBLIC extra_methods
    only. extra_fields ALWAYS render `[SerializeField] private`
    (templates/unity/scripts/CustomBehaviour.cs.j2) — never part of the public
    API regardless of name, so cross-script field access is always CS0122 (hit
    live: MoraleSystem.cs did `inv.moraleValue` where inv = GameContext.Instance.
    Get<InventoryManager>() and moraleValue is one of InventoryManager's own
    extra_fields). CodeMethod.signature (src/micro/code_fields.py) carries
    whichever access modifier the LLM wrote, defaulting to `private` only when
    the LLM supplied a bare name — so a method must explicitly say `public` to
    count."""
    return {
        s.name: frozenset(
            mm.group(1) for m in s.extra_methods
            if _PUBLIC_SIG_RE.match(m.signature or "")
            and (mm := _SIG_NAME_RE.search(m.signature or ""))
        )
        for s in scripts if s.type == "custom"
    }


def _sanitize_project_service_calls(script: UnityScript,
                                    custom_api: dict[str, frozenset],
                                    opaque_types: frozenset[str]) -> None:
    """Strip GameContext.Instance.Get<T>() misuse the composed-API pass can't see:

      - T is a phantom type the LLM invented (hit live: TerminalHorizon's
        VelocityManager called Get<PlayerFSM>().SetState(...) — no PlayerFSM
        script, stub has no members → CS1061). Every line touching the getter
        or its bound variable is dead code.
      - T is another GENERATED CUSTOM script: its complete public surface IS
        its PUBLIC extra_methods only (see _custom_script_api) — never its
        extra_fields, which always render private — so calls to anything else
        are guaranteed CS1061/CS0122 (hit live: DialogueManager.TriggerBeat;
        MoraleSystem.cs -> InventoryManager.moraleValue).

    Canonical script classes (GameManager...), composed micro classes and Unity
    types are opaque here — their template APIs are validated by the existing
    _sanitize_singleton_calls / _sanitize_composed_component_calls passes."""
    getter_chain_re = re.compile(
        r"GameContext\.Instance\.Get<(\w+)>\(\)\s*\??\.\s*(\w+)")
    var_decl_re = re.compile(
        r"\b\w+\s+(\w+)\s*=\s*GameContext\.Instance\.Get<(\w+)>\(\)")

    def _member_bad(t: str, member: str) -> bool:
        if t in opaque_types:
            return False
        if t in custom_api:
            return member not in custom_api[t] and member not in _MB_COMMON_MEMBERS
        return True                       # phantom type — nothing on it is real

    def _strip(body: str) -> str:
        if not body or "GameContext" not in body:
            return body
        var_types = {m.group(1): m.group(2) for m in var_decl_re.finditer(body)}
        # variables bound to phantom types poison every line that touches them
        phantom_vars = {v for v, t in var_types.items()
                        if t not in opaque_types and t not in custom_api}
        var_call_re = None
        if var_types:
            var_alt = "|".join(re.escape(v) for v in var_types)
            var_call_re = re.compile(r"\b(" + var_alt + r")\s*\??\.\s*(\w+)")
        out = []
        for line in body.splitlines(keepends=True):
            bad = any(_member_bad(m.group(1), m.group(2))
                      for m in getter_chain_re.finditer(line))
            if not bad and phantom_vars:
                bad = any(re.search(r"\b" + re.escape(v) + r"\b", line)
                          for v in phantom_vars)
            if not bad and var_call_re:
                bad = any(_member_bad(var_types[m.group(1)], m.group(2))
                          for m in var_call_re.finditer(line))
            if not bad:
                out.append(line)
        return "".join(out)

    script.awake_extra        = _strip(script.awake_extra)
    script.update_extra       = _strip(script.update_extra)
    script.fixed_update_extra = _strip(script.fixed_update_extra)
    for m in script.extra_methods:
        m.body = _strip(m.body)


def _sanitize_field_typed_member_calls(script: UnityScript,
                                       custom_api: dict[str, frozenset],
                                       composed_units: list | None) -> None:
    """A field DECLARED with a cross-reference type — `[SerializeField]
    private SaveSystem _saveSystem;` — is typically bound in one method
    (Awake/OnEnable, via GetComponent<T>()/GameContext.Instance.Get<T>()) and
    CALLED from a different method later. _sanitize_composed_component_calls
    and _sanitize_project_service_calls only resolve a Get<T>()/GetComponent
    binding within the SAME method body they scan, so a field whose binding
    and use are split across two methods is invisible to both. extra_fields
    already carries the LLM's own declared type for the field — no getter
    pattern needed to know it. Hit live: SaveShrineTrigger.cs bound
    `_saveSystem` in OnEnable(), then called `_saveSystem.SetCheckpoint(
    transform.position)` in a separate ActivateShrine() method; the project's
    composed SaveSystem variant (json_file) has no such method — only the
    checkpoint variant does — CS1061. Also catches a plain declared field
    called in the SAME body it's declared in (hit live: InsuranceAudit.cs and
    PhotoMode.cs each declare `[SerializeField] private GameHud _hud;` and call
    `_hud.ShowLine(...)` / `_hud.SetPaused(...)` directly on it, no getter
    chain in sight for either getter-chain pass to key off).

    Validates every field whose declared base type resolves to a composed
    micro class (_COMPOSED_CLASS_API, gated to classes actually composed into
    this project — same ground-truth table _sanitize_composed_component_calls
    uses) or another generated custom script (_custom_script_api) — across the
    WHOLE script, not per-body. Anything else is left untouched.

    Checked by member NAME, and by argument COUNT wherever `_COMPOSED_METHOD_
    ARITY` has an entry for that member (hit live: `PauseMenu.Activate()`
    called with none of its 1 required arg — a real member, wrong arity,
    invisible to a name-only check). Arity data only covers methods whose
    signature is constant across every variant of the class — see
    `_COMPOSED_METHOD_ARITY`'s docstring for what's deliberately excluded.
    Argument TYPE mismatches remain uncaught.

    `_COMPOSED_CLASS_API` is a STATIC, variant-blind table — it lists the
    union of every variant's public methods, so a class whose real API is
    variant-gated (HUD.cs.j2's `ShowLine`/`SetPaused`/etc. each live behind
    a mutually-exclusive `{% elif variant == ... %}` branch) looks like it
    always has all of them, when a given project's HUD only ever compiles
    ONE variant's worth in. Confirmed live: this project's `hud/health_bar`
    build genuinely has only `SetHealth` — calling `ShowLine`/`SetPaused` on
    it is CS1061 either way, but the static table said both were fine.
    `ComposedUnit.setters` (hud kind only) is computed PER PROJECT from the
    actual variant + widget list (`micro.hud.hud_setters`) and is trusted
    over the static table wherever it exists — `hud_api` below overrides
    `_composed_member_ok`'s static-table answer for hud classes specifically."""
    hud_api: dict[str, frozenset[str]] = {
        u.class_name: frozenset(u.setters)
        for u in (composed_units or []) if u.kind == "hud"
    }
    composed_known = ({u.class_name for u in (composed_units or [])
                       if u.class_name in _COMPOSED_CLASS_API}
                       | set(hud_api))
    field_types = {f.name: _extract_base_type(f.type or "") for f in script.extra_fields}
    composed_fields = {n: t for n, t in field_types.items() if t in composed_known}
    custom_fields = {n: t for n, t in field_types.items()
                     if t in custom_api and t != script.name}
    if not composed_fields and not custom_fields:
        return

    alt = "|".join(re.escape(n) for n in list(composed_fields) + list(custom_fields))
    call_re = re.compile(r"\b(" + alt + r")\s*\??\.\s*(\w+)")

    def _bad(field: str, member: str, line: str, after_idx: int) -> bool:
        if field in composed_fields:
            t = composed_fields[field]
            if t in hud_api:
                if member not in hud_api[t] and member not in _MB_COMMON_MEMBERS:
                    return True
                return not _composed_arity_ok(t, member, line, after_idx)
            return _composed_call_invalid(t, member, line, after_idx)
        t = custom_fields[field]
        return member not in custom_api[t] and member not in _MB_COMMON_MEMBERS

    def _strip(body: str) -> str:
        if not body:
            return body
        out = []
        for line in body.splitlines(keepends=True):
            bad = any(_bad(m.group(1), m.group(2), line, m.end())
                      for m in call_re.finditer(line))
            if not bad:
                out.append(line)
        return "".join(out)

    script.awake_extra        = _strip(script.awake_extra)
    script.update_extra       = _strip(script.update_extra)
    script.fixed_update_extra = _strip(script.fixed_update_extra)
    for m in script.extra_methods:
        m.body = _strip(m.body)


_SIG_PARAMLIST_RE = re.compile(r"\((.*)\)", re.DOTALL)
_PARAM_MODIFIERS = frozenset({"ref", "out", "in", "params", "this"})


def _method_param_types(signature: str) -> dict[str, str]:
    """`{param_name: base_type}` for a method signature's parameter list —
    the identifiers `_sanitize_param_typed_member_calls` may treat as typed
    locals, scoped to that one method (a parameter's scope, unlike a field's,
    never extends past its own method's body)."""
    if not signature:
        return {}
    m = _SIG_PARAMLIST_RE.search(signature)
    if not m:
        return {}
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in m.group(1):
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    parts.append("".join(current))

    out: dict[str, str] = {}
    for raw in parts:
        part = raw.split("=", 1)[0].strip()          # drop a default value
        tokens = [t for t in re.split(r"\s+", part) if t
                  and t not in _PARAM_MODIFIERS]
        if len(tokens) >= 2:                          # "<type> <name>"
            out[tokens[-1]] = _extract_base_type(" ".join(tokens[:-1]))
    return out


def _sanitize_param_typed_member_calls(script: UnityScript,
                                       custom_api: dict[str, frozenset],
                                       composed_units: list | None) -> None:
    """Strip an invalid composed/custom-class member call reached through a
    METHOD PARAMETER rather than a field — `_sanitize_field_typed_member_calls`
    only ever indexed `script.extra_fields`, so a class typed as a parameter
    (a helper method taking `WorldInteractable gate` and calling
    `gate.OnToggled()`) was invisible to every existing composed-call check.
    A parameter's scope is its own method only, unlike a field's whole-script
    scope, so each method is checked against its own signature alone. Hit
    live: InventoryManager.cs `TryUnlockGate(WorldInteractable gate)` /
    `TryUnlockGateWithCog(WorldInteractable gate)` both called
    `gate.OnToggled()` — a real member (`_COMPOSED_CLASS_API`), but an EVENT,
    not a callable method (CS1061; see `_composed_event_call_ok`)."""
    hud_api: dict[str, frozenset[str]] = {
        u.class_name: frozenset(u.setters)
        for u in (composed_units or []) if u.kind == "hud"
    }
    composed_known = ({u.class_name for u in (composed_units or [])
                       if u.class_name in _COMPOSED_CLASS_API}
                       | set(hud_api))
    if not composed_known and not custom_api:
        return

    def _bad(t: str, member: str, line: str, after_idx: int) -> bool:
        if t in hud_api:
            if member not in hud_api[t] and member not in _MB_COMMON_MEMBERS:
                return True
            return not _composed_arity_ok(t, member, line, after_idx)
        if t in composed_known:
            return _composed_call_invalid(t, member, line, after_idx)
        return (member not in custom_api.get(t, frozenset())
                and member not in _MB_COMMON_MEMBERS)

    for m in script.extra_methods:
        if not m.body:
            continue
        params = _method_param_types(m.signature)
        typed = {n: t for n, t in params.items()
                 if t in composed_known or (t in custom_api and t != script.name)}
        if not typed:
            continue
        alt = "|".join(re.escape(n) for n in typed)
        call_re = re.compile(r"\b(" + alt + r")\s*\??\.\s*(\w+)")
        out = []
        for line in m.body.splitlines(keepends=True):
            bad = any(_bad(typed[mm.group(1)], mm.group(2), line, mm.end())
                      for mm in call_re.finditer(line))
            if not bad:
                out.append(line)
        m.body = "".join(out)


_FOREACH_BARE_RE = re.compile(
    r"foreach\s*\(\s*(?:var|[\w<>\[\],.\s]+?)\s+\w+\s+in\s+([A-Za-z_]\w*)\s*\)")
_LOCAL_DECL_RE = re.compile(r"\b(?:var|[\w<>\[\],.]+)\s+(\w+)\s*=")
# A bare declaration with no initializer -- `RaycastHit hit;` ahead of
# `Physics.Raycast(..., out hit, ...)` -- never matched _LOCAL_DECL_RE (it
# requires a trailing `=`), so `hit` never entered `declared` and a later
# condition reading it (`if (hit.collider...)`) got misread as an undeclared
# value read. Restricted to a PascalCase/primitive-keyword "type" token so
# `return x;`/`throw e;`/`yield x;`/`break;` (a statement keyword, not a
# type, immediately followed by its own value/nothing) can never match —
# those lowercase keywords are neither PascalCase nor in the primitive set.
# Confirmed live: VectorCore's SurfaceTypeDetector.cs.
_LOCAL_DECL_NO_INIT_RE = re.compile(
    r"\b((?:[A-Z]\w*(?:<[^<>()]*>)?"
    r"|int|float|bool|string|double|long|byte|short|char|uint|ulong|var))"
    r"\s+(\w+)\s*;")
_FOREACH_VAR_RE = re.compile(r"foreach\s*\(\s*(?:var|[\w<>\[\],.\s]+?)\s+(\w+)\s+in\b")


def _strip_undeclared_foreach(script: UnityScript) -> None:
    """foreach over a bare identifier that is declared NOWHERE (not a field,
    not a local, not a loop variable) is a guaranteed CS0103 — the LLM
    referenced a collection it never created (hit live: TerminalHorizon's
    BirdSwarmFlocking iterated `birds`). Strip the offending line; everything
    declared stays untouched."""
    fields = {f.name for f in script.extra_fields}

    def _strip(body: str) -> str:
        if not body or "foreach" not in body:
            return body
        declared = set(fields)
        declared.update(m.group(1) for m in _LOCAL_DECL_RE.finditer(body))
        declared.update(m.group(2) for m in _LOCAL_DECL_NO_INIT_RE.finditer(body))
        declared.update(m.group(1) for m in _FOREACH_VAR_RE.finditer(body))
        out = []
        for line in body.splitlines(keepends=True):
            bad = any(m.group(1) not in declared
                      for m in _FOREACH_BARE_RE.finditer(line))
            if not bad:
                out.append(line)
        return "".join(out)

    script.awake_extra        = _strip(script.awake_extra)
    script.update_extra       = _strip(script.update_extra)
    script.fixed_update_extra = _strip(script.fixed_update_extra)
    for m in script.extra_methods:
        m.body = _strip(m.body)


# ── Undeclared self-reference (bare field/method the script never declared) ──
#
# Distinct from _sanitize_composed_component_calls/_sanitize_project_service_calls
# (which validate DOTTED cross-reference member access on another type reached
# via Get<T>()) — this catches a script hallucinating members on ITSELF, with no
# dot at all (hit live: PlatformTrigger.cs, extra_fields=[] and extra_methods=[],
# but update_extra referenced both a bare field read (healthThreshold) and a
# bare method call (TriggerCollapse()) — neither ever declared — 2x CS0103).
#
# Bare identifiers appear in many LEGITIMATE positions (type names in
# declarations, `new Vector3(...)` constructors, inherited MonoBehaviour
# methods called with an implicit `this`, C# keyword-paren constructs,
# `is`-pattern matching, lambda parameters) — this is regex-based "good enough"
# validation, not a real parser, so it's split into two confidence tiers and
# scoped narrowly rather than scanning the whole body indiscriminately.

# Part A — bare method calls (`Foo()`, no receiver). Higher confidence: a truly
# bare call in this codebase's generated C# is either a self-authored method or
# one of a small, fixed set of inherited MonoBehaviour/Component/Object methods.
_BARE_CALL_RE = re.compile(r"(?<![.\w])(new\s+)?([A-Za-z_]\w*)\s*\(")
_KEYWORD_PAREN_ALLOW = frozenset({
    "if", "while", "for", "foreach", "switch", "catch", "using", "lock", "fixed",
    "typeof", "nameof", "sizeof", "default", "checked", "unchecked", "return", "new",
})
# Inherited MonoBehaviour/Component/Object methods legitimately called with no
# receiver (implicit `this`) — never authored by the template itself.
_BARE_CALLABLE_ALLOW = frozenset({
    "Instantiate", "Destroy", "StartCoroutine", "StopCoroutine", "StopAllCoroutines",
    "Invoke", "InvokeRepeating", "CancelInvoke", "GetComponent", "GetComponents",
    "GetComponentInChildren", "GetComponentsInChildren", "GetComponentInParent",
    "TryGetComponent", "AddComponent", "SendMessage", "BroadcastMessage",
    "CompareTag", "GetType", "GetHashCode", "ToString", "Equals",
})
_DELEGATE_TYPE_RE = re.compile(r"\b(?:Action|Func|UnityEvent)\b")


def _sanitize_undeclared_self_calls(script: UnityScript) -> None:
    """Strip a bare method call (no receiver — not `x.Foo()`/`x?.Foo()`, those
    are already excluded by the `(?<![.\\w])` lookbehind) that is neither one of
    this script's own extra_methods, a delegate/Action/Func/UnityEvent-typed
    extra_field invoked with bare-call syntax (e.g. onCollapse()), nor a
    legitimate inherited MonoBehaviour method — always CS0103."""
    own_callable: set[str] = set()
    for m in script.extra_methods:
        mm = _SIG_NAME_RE.search(m.signature or "")
        if mm:
            own_callable.add(mm.group(1))
    for f in script.extra_fields:
        if _DELEGATE_TYPE_RE.search(f.type or ""):
            own_callable.add(f.name)
    known = own_callable | _BARE_CALLABLE_ALLOW

    def _strip(body: str) -> str:
        if not body:
            return body
        out = []
        for line in body.splitlines(keepends=True):
            bad = any(
                m.group(1) is None                      # not `new Foo(...)`
                and m.group(2) not in known
                and m.group(2) not in _KEYWORD_PAREN_ALLOW
                for m in _BARE_CALL_RE.finditer(line)
            )
            if not bad:
                out.append(line)
        return "".join(out)

    script.awake_extra        = _strip(script.awake_extra)
    script.update_extra       = _strip(script.update_extra)
    script.fixed_update_extra = _strip(script.fixed_update_extra)
    for m in script.extra_methods:
        m.body = _strip(m.body)


# Part B — bare value reads inside if/while conditions. Deliberately scoped to
# boolean-condition context only (not the whole body) — a condition is far
# less likely to contain a type-declaration position than arbitrary code, so
# this is the safest place to catch a bare undeclared read without risking a
# false-positive strip of legitimate type/constructor syntax elsewhere.
_COND_KEYWORD_ALLOW = frozenset({"true", "false", "null", "this", "base", "is", "var"})
_STATIC_CLASS_ALLOW = frozenset({
    "Time", "Mathf", "Debug", "Physics", "Physics2D", "Input", "Keyboard", "Gamepad",
    "Random", "Resources", "Application", "SceneManager",
    "Vector2", "Vector3", "Vector4", "Quaternion", "Color",
    "GameObject", "Transform", "Rigidbody", "Rigidbody2D", "Collider", "Collider2D",
    "float", "int", "bool", "string", "double", "long",
})
# `is Foo f` pattern-matching binds BOTH a type name and a new local — neither
# is declared anywhere else, so both must be seeded as "known" for that body.
_IS_PATTERN_RE   = re.compile(r"\bis\s+(\w+)(?:\s+(\w+))?")
_LAMBDA_PARAM_RE = re.compile(r"(\w+)\s*=>")
_COND_HEADER_RE  = re.compile(r"\b(?:if|while)\s*\(")
# Bare identifier not preceded by `.`/word-char (collapses dotted chains like
# `transform.position.y` to just the root `transform`) and not immediately
# followed by `(` — OR by a generic type-argument list then `(`, e.g.
# `ReadValue<Vector2>(...)` (that's Part A's job, a method call, not a value
# read). Missing the generic case here misread InputActionReference.
# ReadValue<Vector2>(...) inside an `if (...)` as a bare undeclared VALUE,
# stripping the whole condition line -- if that line also carried the
# block's own opening `{` (K&R `if (cond) {` style), the strip took the
# brace with it, orphaning the body and its closer (CS1022/CS8803/CS0106).
# Confirmed live: VectorCore's InputMapper.cs.
_COND_TOKEN_RE   = re.compile(r"(?<![.\w])([A-Za-z_]\w*)\b(?!\s*(?:<[^<>()]*>)?\s*\()")
_METHOD_PARAM_LIST_RE = re.compile(r"\(([^)]*)\)")


def _extract_conditions(text: str) -> list[str]:
    """Text inside every if(...)/while(...) in `text`, matched by paren-count
    (not a naive non-greedy regex, which truncates at the first `)` inside a
    nested call like `if (Foo(x) > 0 && Bar())`)."""
    conditions = []
    for m in _COND_HEADER_RE.finditer(text):
        depth = 1
        i = m.end()
        start = i
        while i < len(text) and depth > 0:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        conditions.append(text[start:i - 1])
    return conditions


def _method_param_names(signature: str) -> set[str]:
    """Parameter names declared in an extra_methods signature — these are
    locally in scope for that method's own body, same as a field or local."""
    m = _METHOD_PARAM_LIST_RE.search(signature or "")
    if not m:
        return set()
    names: set[str] = set()
    for part in m.group(1).split(","):
        tokens = part.strip().split()
        if tokens:
            names.add(tokens[-1].lstrip("*"))
    return names


def _sanitize_undeclared_condition_reads(script: UnityScript) -> None:
    """Strip an if/while condition that reads a bare value declared NOWHERE —
    always CS0103 (hit live: PlatformTrigger.cs checked `healthThreshold < 0.2f`
    with extra_fields=[] — the LLM invented a field it never declared)."""
    own_fields = {f.name for f in script.extra_fields}

    def _strip(body: str, extra_declared: frozenset = frozenset()) -> str:
        if not body:
            return body
        declared = set(own_fields) | set(extra_declared)
        declared.update(m.group(1) for m in _LOCAL_DECL_RE.finditer(body))
        declared.update(m.group(2) for m in _LOCAL_DECL_NO_INIT_RE.finditer(body))
        declared.update(m.group(1) for m in _FOREACH_VAR_RE.finditer(body))
        for m in _IS_PATTERN_RE.finditer(body):
            declared.add(m.group(1))
            if m.group(2):
                declared.add(m.group(2))
        declared.update(m.group(1) for m in _LAMBDA_PARAM_RE.finditer(body))

        out = []
        for line in body.splitlines(keepends=True):
            bad = False
            for cond in _extract_conditions(line):
                # String/char literal content ("Fire1", "Horizontal", ...) is
                # not a value read — Input.GetButtonDown("Jump")-shaped calls
                # are common, real Unity API, not an undeclared identifier.
                mask = _code_mask(cond)
                for m in _COND_TOKEN_RE.finditer(cond):
                    if not mask[m.start(1)]:
                        continue
                    tok = m.group(1)
                    if (tok in declared or tok in _COND_KEYWORD_ALLOW
                            or tok in _STATIC_CLASS_ALLOW or tok in _MB_COMMON_MEMBERS):
                        continue
                    bad = True
                    break
                if bad:
                    break
            if not bad:
                out.append(line)
        return "".join(out)

    script.awake_extra        = _strip(script.awake_extra)
    script.update_extra       = _strip(script.update_extra)
    script.fixed_update_extra = _strip(script.fixed_update_extra)
    for m in script.extra_methods:
        m.body = _strip(m.body, frozenset(_method_param_names(m.signature)))


# ── Nullable value-type null-conditional chains ────────────────────────────────
# `X?.transform.position` (X reference-typed, possibly null) infers Vector3? —
# passing that anywhere a plain Vector3/Quaternion is expected is CS1503 (hit
# live: AIManager.cs did `Vector3.Distance(GameContext.Instance.Get<PlayerController>()
# ?.transform.position, ...)`). This is the direct, expected consequence of
# GAMECONTEXT_SCHEMA_GUIDANCE (src/unity_prompts.py) steering the LLM toward
# `GameContext.Instance.Get<T>()?.X` instead of a null check — the guidance
# stays (it's the right pattern for a possibly-absent service), the fix lives
# here.
#
# The declaration itself is left ALONE (stays Vector3?) — the LLM routinely
# null-checks that same variable right after (`if (playerPos != null)`), which
# only compiles while it's still nullable; converting the declaration itself
# to a plain Vector3 would fix the Distance() call but break that null check
# (CS0019, Vector3 has no operator!=(Vector3, <null>)). Instead, every OTHER
# bare use of the variable — anything that isn't the declaration and isn't
# part of a null check — gets `.GetValueOrDefault()` appended, converting back
# to a plain value (Vector3.zero / Quaternion.identity on a miss) only where
# a non-nullable value is actually required.
_NULLABLE_MEMBER_SUFFIX = (
    r"(?:transform\.)?(?:position|rotation|localPosition|localRotation|"
    r"forward|right|up|localScale|eulerAngles)"
)
_NULLABLE_DECL_RE = re.compile(
    r"\b(?:var|[\w<>\[\],.]+)\s+(\w+)\s*=\s*[^;{}]*?\?\." + _NULLABLE_MEMBER_SUFFIX + r"\b"
)
_NULL_COMPARE_AFTER_RE      = re.compile(r"\A\s*(?:!=|==)\s*null\b")
_NULL_COMPARE_BEFORE_RE     = re.compile(r"null\s*(?:!=|==)\s*\Z")
_NULLABLE_ACCESSOR_AFTER_RE = re.compile(r"\A\s*\.(?:HasValue|Value|GetValueOrDefault)\b")


def _sanitize_nullable_valuetype_access(script: UnityScript) -> None:
    """Fix `X?.transform.position`-shaped CS1503s — see module comment above."""
    def _fix(body: str) -> str:
        if not body or "?." not in body:
            return body
        decls = list(_NULLABLE_DECL_RE.finditer(body))
        if not decls:
            return body
        decl_spans = [(m.start(1), m.end(1)) for m in decls]
        names = sorted({m.group(1) for m in decls}, key=len, reverse=True)
        name_re = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b")

        def _replace(m: "re.Match[str]") -> str:
            start, end = m.start(), m.end()
            if any(s <= start and end <= e for s, e in decl_spans):
                return m.group(0)          # the declaration's own LHS — leave nullable
            after  = body[end:end + 24]
            before = body[max(0, start - 24):start]
            if (_NULL_COMPARE_AFTER_RE.match(after)
                    or _NULL_COMPARE_BEFORE_RE.search(before)
                    or _NULLABLE_ACCESSOR_AFTER_RE.match(after)):
                return m.group(0)          # a null check / already-safe access — leave it
            return m.group(0) + ".GetValueOrDefault()"

        return name_re.sub(_replace, body)

    script.awake_extra        = _fix(script.awake_extra)
    script.update_extra       = _fix(script.update_extra)
    script.fixed_update_extra = _fix(script.fixed_update_extra)
    for m in script.extra_methods:
        m.body = _fix(m.body)


# ── Undeclared persisted scalars (auto-declare, don't strip) ──────────────────
# Unlike the strip passes above (self-calls, condition reads — genuinely unsafe
# to guess a fix for), a bare assignment target the LLM never gave a field
# (hit live: DriftCamera.cs tracked frame-to-frame delta via `lastX` but never
# wrote `private float lastX;`, so both the read and the write were CS0103) is
# a case where AUTO-DECLARING the field preserves the intended behavior instead
# of deleting it — the same rationale as the existing needs_rigidbody
# auto-declare below, generalized to any plain persisted scalar. Fields are
# shared across a script's whole class, so this scans awake+update+
# fixed_update+extra_methods TOGETHER before deciding what's genuinely
# undeclared (a `var`/typed local declared in one body must not be flagged).
_ASSIGN_TARGET_RE = re.compile(r"(?<![.\w])([A-Za-z_]\w*)\s*(?:[+\-*/%&|^])?=(?!=)")
_CS_KEYWORDS = frozenset({
    "if", "else", "for", "foreach", "while", "do", "switch", "case", "default",
    "return", "break", "continue", "new", "true", "false", "null", "this",
    "base", "var", "is", "as", "in", "out", "ref", "throw", "try", "catch",
    "finally", "using", "yield", "await", "async",
})


def _auto_declare_undeclared_scalars(script: UnityScript) -> None:
    """Auto-declare a bare assignment-target identifier the LLM never gave a
    field for (see module comment above). Skips anything already known:
    extra_fields, method params, locally var/typed-declared, foreach vars,
    `is`-pattern bindings, lambda params, or Unity/C#/keyword built-ins.

    Not fully precise (e.g. an object-initializer `new Foo { X = 1 }` reads as
    an assignment target too) — worst case is one harmless extra unused field,
    never a compile break, matching this module's existing heuristic passes.
    """
    combined = "\n".join(filter(None, [
        script.awake_extra, script.update_extra, script.fixed_update_extra,
    ] + [m.body for m in script.extra_methods]))
    if not combined:
        return

    declared: set[str] = {f.name for f in script.extra_fields}
    declared.update(m.group(1) for m in _LOCAL_DECL_RE.finditer(combined))
    declared.update(m.group(2) for m in _LOCAL_DECL_NO_INIT_RE.finditer(combined))
    declared.update(m.group(1) for m in _FOREACH_VAR_RE.finditer(combined))
    for m in _IS_PATTERN_RE.finditer(combined):
        declared.add(m.group(1))
        if m.group(2):
            declared.add(m.group(2))
    declared.update(m.group(1) for m in _LAMBDA_PARAM_RE.finditer(combined))
    for method in script.extra_methods:
        declared.update(_method_param_names(method.signature))

    seen: set[str] = set()
    for m in _ASSIGN_TARGET_RE.finditer(combined):
        tok = m.group(1)
        if (tok in declared or tok in _CS_KEYWORDS or tok in _STATIC_CLASS_ALLOW
                or tok in _MB_COMMON_MEMBERS or tok in seen):
            continue
        seen.add(tok)
        script.extra_fields.append(CodeField(name=tok, type="float"))


# ── Environment lighting: scene YAML ↔ runtime guard agreement ────────────────
# The lighting micro (src/micro/lighting.py) writes RenderSettings at Play; the
# scene template bakes RenderSettings at rest. Both read the SAME resolver so
# they cannot drift — before this, the scene shipped AmbientMode.Skybox over the
# built-in Default-Skybox and only the runtime guard corrected it, which meant
# the Editor showed a black scene until you pressed Play.

def _lighting_variant_for(schema, composed_units=None) -> str:
    """The lighting variant this project's scene is lit by.

    In --compose mode the composed unit is authoritative (it's the variant the
    emitted LightingDirector was actually built from). Without compose there is
    no unit, so re-run the same selector on the same inputs.
    """
    for u in (composed_units or []):
        if u.kind == "lighting":
            return u.variant
    from .micro.lighting import select_variant
    env = getattr(schema, "environment", None)
    return select_variant(
        schema.game_type,
        visual_style=schema.visual_style,
        time_of_day=str(getattr(env, "time_of_day", "") or ""),
        explicit_env=env is not None,
        user_prompt=str((getattr(schema, "meta", None) or {}).get("user_prompt") or ""),
        scenes=schema.scenes,
    )


def _ambient_ctx(palette_rgb: dict, variant: str) -> dict:
    """Scene-template context for RenderSettings' ambient block."""
    from .micro.lighting import resolve_ambient
    res = resolve_ambient(palette_rgb, variant)
    as_color = lambda t: {"r": round(t[0], 8), "g": round(t[1], 8), "b": round(t[2], 8)}
    return {
        "ambient_mode":      res.unity_mode,
        "ambient_sky":       as_color(res.sky),
        "ambient_equator":   as_color(res.equator),
        "ambient_ground":    as_color(res.ground),
        "ambient_intensity": res.intensity,
    }


# ── Visual style -> lighting config ───────────────────────────────────────────

_LIGHTING_BY_STYLE: dict[str, dict] = {
    "cartoon":   {"intensity": 1.3, "r": 1.0,  "g": 0.98, "b": 0.90, "fog": False},
    "realistic": {"intensity": 1.0, "r": 1.0,  "g": 0.957,"b": 0.839,"fog": True },
    "low_poly":  {"intensity": 1.2, "r": 1.0,  "g": 0.95, "b": 0.85, "fog": False},
    "stylized":  {"intensity": 1.1, "r": 1.0,  "g": 0.90, "b": 0.85, "fog": False},
    "pixel_art": {"intensity": 1.0, "r": 1.0,  "g": 1.0,  "b": 1.0,  "fog": False},
    "retro":     {"intensity": 0.95,"r": 1.0,  "g": 0.85, "b": 0.70, "fog": False},
}

# ── Environment mood: time-of-day sun presets ─────────────────────────────────
# pitch = sun elevation (low = long shadows); color/intensity set the mood.
# "day" is None → keep the visual-style lighting (exact pre-environment output).

_TIME_OF_DAY_PRESETS: dict[str, dict | None] = {
    "dawn":   {"pitch": 15.0, "color": (1.0, 0.75, 0.62), "intensity": 0.9},
    "day":    None,
    "sunset": {"pitch": 8.0,  "color": (1.0, 0.45, 0.25), "intensity": 1.0},
    "night":  {"pitch": 35.0, "color": (0.5, 0.6, 0.9),   "intensity": 0.25},
}


def _sun_rotation(pitch_deg: float, yaw_deg: float = -30.0) -> tuple[str, str]:
    """Unity light rotation for (pitch, yaw, 0) → (quaternion str, euler-hint str).
    Unity's Quaternion.Euler order: q = Qy(yaw) * Qx(pitch). Reproduces the
    template's historical (50, -30, 0) quat bit-for-bit at 8 decimals."""
    px, yy = math.radians(pitch_deg) / 2.0, math.radians(yaw_deg) / 2.0
    sx, cx = math.sin(px), math.cos(px)
    sy, cy = math.sin(yy), math.cos(yy)
    qw = cy * cx
    qx = cy * sx
    qy = sy * cx
    qz = -sy * sx
    quat  = (f"{{x: {round(qx, 8)}, y: {round(qy, 8)}, "
             f"z: {round(qz, 8)}, w: {round(qw, 8)}}}")
    euler = f"{{x: {round(pitch_deg, 4)}, y: {round(yaw_deg, 4)}, z: 0}}"
    return quat, euler


# ── Unity version -> exact editor build string ─────────────────────────────────

_VERSION_MAP = {
    "6000.4": ("6000.4.1f1",  "0000000000000"),
    "6000.1": ("6000.1.4f1",  "0000000000000"),
    "6000.0": ("6000.0.66f2", "0000000000000"),
    "2022.3": ("2022.3.62f1", "0000000000000"),
    "2021.3": ("2021.3.45f1", "0000000000000"),
}


# ── .meta file helpers ─────────────────────────────────────────────────────────

def _meta_file(guid: str) -> str:
    """MonoImporter .meta for C# scripts."""
    return (
        "fileFormatVersion: 2\n"
        f"guid: {guid}\n"
        "MonoImporter:\n"
        "  externalObjects: {}\n"
        "  serializedVersion: 2\n"
        "  defaultReferences: []\n"
        "  executionOrder: 0\n"
        "  icon: {instanceID: 0}\n"
        "  userData: \n"
        "  assetBundleName: \n"
        "  assetBundleVariant: \n"
    )


def _default_meta(guid: str, *, folder: bool = False) -> str:
    """DefaultImporter .meta for scenes, folders, and other raw assets."""
    folder_line = "folderAsset: yes\n" if folder else ""
    return (
        "fileFormatVersion: 2\n"
        f"guid: {guid}\n"
        f"{folder_line}"
        "DefaultImporter:\n"
        "  externalObjects: {}\n"
        "  userData: \n"
        "  assetBundleName: \n"
        "  assetBundleVariant: \n"
    )


def _native_meta(guid: str, main_object_id: int) -> str:
    """NativeFormatImporter .meta for .inputactions and .prefab files."""
    return (
        "fileFormatVersion: 2\n"
        f"guid: {guid}\n"
        "NativeFormatImporter:\n"
        "  externalObjects: {}\n"
        f"  mainObjectFileID: {main_object_id}\n"
        "  userData: \n"
        "  assetBundleName: \n"
        "  assetBundleVariant: \n"
    )


# All three ModelImporter .meta emitters below use materialImportMode: 2
# (ImportViaMaterialDescription): the active render pipeline's material
# description postprocessor generates the materials, so URP projects get
# URP/Lit instead of the built-in Standard shader (mode 1), which URP cannot
# render — every imported FBX showed up magenta/black in Play mode.


def _humanoid_model_meta(guid: str, scale: float = 1.0) -> str:
    """ModelImporter .meta for rigged characters/clips — Humanoid rig
    (animationType: 3) so any Mixamo-style clip retargets to any character."""
    return (
        "fileFormatVersion: 2\n"
        f"guid: {guid}\n"
        "ModelImporter:\n"
        "  serializedVersion: 21300\n"
        "  internalIDToNameTable: []\n"
        "  externalObjects: {}\n"
        "  materials:\n"
        "    materialImportMode: 2\n"
        "    materialName: 0\n"
        "    materialSearch: 1\n"
        "    materialLocation: 1\n"
        "  animations:\n"
        "    legacyGenerateAnimations: 4\n"
        "    bakeSimulation: 0\n"
        "    resampleCurves: 1\n"
        "    optimizeGameObjects: 0\n"
        "    animationCompression: 1\n"
        "    animationRotationError: 0.5\n"
        "    animationPositionError: 0.5\n"
        "    animationScaleError: 0.5\n"
        "    animationWrapMode: 0\n"
        "    clipAnimations: []\n"
        "    isReadable: 0\n"
        f"  globalScale: {scale}\n"
        "  useFileScale: 1\n"
        "  animationType: 3\n"
        "  humanoidOversampling: 1\n"
        "  avatarSetup: 1\n"
        "  humanDescription:\n"
        "    serializedVersion: 3\n"
        "    human: []\n"
        "    skeleton: []\n"
        "    armTwist: 0.5\n"
        "    foreArmTwist: 0.5\n"
        "    upperLegTwist: 0.5\n"
        "    legTwist: 0.5\n"
        "    armStretch: 0.05\n"
        "    legStretch: 0.05\n"
        "    feetSpacing: 0\n"
        "    globalScale: 1\n"
        "    rootMotionBoneName: \n"
        "    hasTranslationDoF: 0\n"
        "    hasExtraRoot: 0\n"
        "    skeletonHasParents: 1\n"
        "  platforms: []\n"
        "  userData: \n"
        "  assetBundleName: \n"
        "  assetBundleVariant: \n"
    )


def _model_meta(guid: str) -> str:
    """ModelImporter .meta for .obj placeholder files."""
    return (
        "fileFormatVersion: 2\n"
        f"guid: {guid}\n"
        "ModelImporter:\n"
        "  serializedVersion: 21300\n"
        "  internalIDToNameTable: []\n"
        "  externalObjects: {}\n"
        "  materials:\n"
        "    materialImportMode: 2\n"
        "    materialName: 0\n"
        "    materialSearch: 1\n"
        "    materialLocation: 1\n"
        "  animations:\n"
        "    legacyGenerateAnimations: 4\n"
        "    bakeSimulation: 0\n"
        "    resampleCurves: 1\n"
        "    optimizeGameObjects: 0\n"
        "    animationCompression: 1\n"
        "    animationRotationError: 0.5\n"
        "    animationPositionError: 0.5\n"
        "    animationScaleError: 0.5\n"
        "    animationWrapMode: 0\n"
        "    clipAnimations: []\n"
        "    isReadable: 0\n"
        "  platforms: []\n"
        "  userData: \n"
        "  assetBundleName: \n"
        "  assetBundleVariant: \n"
    )


def _generic_model_meta(guid: str, scale: float = 1.0) -> str:
    """ModelImporter .meta for skeleton-less procedural FBX (Blender bases).

    animationType: 2 (Generic) so Unity does NOT try to build a Humanoid avatar —
    these loose-mesh models have no 'Hips'/standard bones, and Humanoid import
    fails with "Invalid Avatar Rig Configuration". Generic imports cleanly; the
    mesh swaps in over the primitive at Play."""
    return (
        "fileFormatVersion: 2\n"
        f"guid: {guid}\n"
        "ModelImporter:\n"
        "  serializedVersion: 21300\n"
        "  internalIDToNameTable: []\n"
        "  externalObjects: {}\n"
        "  materials:\n"
        "    materialImportMode: 2\n"
        "    materialName: 0\n"
        "    materialSearch: 1\n"
        "    materialLocation: 1\n"
        "  animations:\n"
        "    legacyGenerateAnimations: 4\n"
        "    bakeSimulation: 0\n"
        "    resampleCurves: 1\n"
        "    optimizeGameObjects: 0\n"
        "    animationCompression: 1\n"
        "    animationRotationError: 0.5\n"
        "    animationPositionError: 0.5\n"
        "    animationScaleError: 0.5\n"
        "    animationWrapMode: 0\n"
        "    clipAnimations: []\n"
        "    isReadable: 0\n"
        f"  globalScale: {scale}\n"
        "  useFileScale: 1\n"
        "  animationType: 2\n"
        "  avatarSetup: 0\n"
        "  platforms: []\n"
        "  userData: \n"
        "  assetBundleName: \n"
        "  assetBundleVariant: \n"
    )


# ── Color helpers ─────────────────────────────────────────────────────────────

def _hex_to_rgb(hex_color: str) -> tuple[float, float, float]:
    """#RRGGBB → (r, g, b) as 0.0–1.0 floats."""
    h = hex_color.lstrip('#')
    return (int(h[0:2], 16) / 255.0,
            int(h[2:4], 16) / 255.0,
            int(h[4:6], 16) / 255.0)


def _unity_color(hex_color: str) -> dict:
    """#RRGGBB → {r: float, g: float, b: float} Unity dict (rounded to 8 dp)."""
    r, g, b = _hex_to_rgb(hex_color)
    return {"r": round(r, 8), "g": round(g, 8), "b": round(b, 8)}


def _generate_material(mat_name: str, hex_color: str,
                        render_pipeline: str, smoothness: float = 0.3) -> str:
    """Return a .mat file string for the given hex color."""
    r, g, b = _hex_to_rgb(hex_color)
    r8, g8, b8 = round(r, 8), round(g, 8), round(b, 8)

    if render_pipeline == "builtin":
        return (
            "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
            "--- !u!21 &2100000\nMaterial:\n"
            "  serializedVersion: 8\n"
            "  m_ObjectHideFlags: 0\n"
            "  m_CorrespondingSourceObject: {fileID: 0}\n"
            "  m_PrefabInstance: {fileID: 0}\n"
            "  m_PrefabAsset: {fileID: 0}\n"
            f"  m_Name: {mat_name}\n"
            "  m_Shader: {fileID: 46, guid: 0000000000000000f000000000000000, type: 0}\n"
            "  m_ShaderKeywords: _GLOSSYREFLECTIONS_OFF _SPECULARHIGHLIGHTS_OFF\n"
            "  m_LightmapFlags: 4\n"
            "  m_EnableInstancing: 0\n"
            "  m_DoubleSidedGI: 0\n"
            "  m_CustomRenderQueue: -1\n"
            "  stringTagMap: {}\n"
            "  disabledShaderPasses: []\n"
            "  m_SavedProperties:\n"
            "    serializedVersion: 3\n"
            "    m_TexEnvs:\n"
            "    - _MainTex:\n"
            "        m_Texture: {fileID: 0}\n"
            "        m_Scale: {x: 1, y: 1}\n"
            "        m_Offset: {x: 0, y: 0}\n"
            "    m_Floats:\n"
            f"    - _Glossiness: {smoothness}\n"
            "    - _Metallic: 0\n"
            "    m_Colors:\n"
            f"    - _Color: {{r: {r8}, g: {g8}, b: {b8}, a: 1}}\n"
            f"    - _EmissionColor: {{r: 0, g: 0, b: 0, a: 1}}\n"
        )
    elif render_pipeline == "HDRP":
        # HDRP/Lit shader (guid 6e4ae40…). Minimal opaque-Lit material — HDRP's
        # MaterialPostprocessor expands the remaining defaults on import. Using the
        # URP Lit shader here previously fell back to InternalErrorShader (magenta).
        return (
            "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
            "--- !u!21 &2100000\nMaterial:\n"
            "  serializedVersion: 8\n"
            "  m_ObjectHideFlags: 0\n"
            "  m_CorrespondingSourceObject: {fileID: 0}\n"
            "  m_PrefabInstance: {fileID: 0}\n"
            "  m_PrefabAsset: {fileID: 0}\n"
            f"  m_Name: {mat_name}\n"
            "  m_Shader: {fileID: 4800000, guid: 6e4ae4064600d784cac1e41a9e6f2e59, type: 3}\n"
            "  m_ValidKeywords: []\n"
            "  m_InvalidKeywords: []\n"
            "  m_LightmapFlags: 4\n"
            "  m_EnableInstancingVariants: 0\n"
            "  m_DoubleSidedGI: 0\n"
            "  m_CustomRenderQueue: -1\n"
            "  stringTagMap: {}\n"
            "  disabledShaderPasses: []\n"
            "  m_SavedProperties:\n"
            "    serializedVersion: 3\n"
            "    m_TexEnvs:\n"
            "    - _BaseColorMap:\n"
            "        m_Texture: {fileID: 0}\n"
            "        m_Scale: {x: 1, y: 1}\n"
            "        m_Offset: {x: 0, y: 0}\n"
            "    m_Ints: []\n"
            "    m_Floats:\n"
            f"    - _Smoothness: {smoothness}\n"
            "    - _Metallic: 0\n"
            "    - _SurfaceType: 0\n"
            "    - _BlendMode: 0\n"
            "    m_Colors:\n"
            f"    - _BaseColor: {{r: {r8}, g: {g8}, b: {b8}, a: 1}}\n"
            "    - _EmissiveColor: {r: 0, g: 0, b: 0, a: 1}\n"
        )
    else:  # URP — URP Lit shader
        return (
            "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
            "--- !u!21 &2100000\nMaterial:\n"
            "  serializedVersion: 8\n"
            "  m_ObjectHideFlags: 0\n"
            "  m_CorrespondingSourceObject: {fileID: 0}\n"
            "  m_PrefabInstance: {fileID: 0}\n"
            "  m_PrefabAsset: {fileID: 0}\n"
            f"  m_Name: {mat_name}\n"
            "  m_Shader: {fileID: 4800000, guid: 933532a4fcc9baf4fa0491de14d08ed7, type: 3}\n"
            "  m_ShaderKeywords: _ENVIRONMENTREFLECTIONS_OFF _SPECULARHIGHLIGHTS_OFF\n"
            "  m_LightmapFlags: 4\n"
            "  m_EnableInstancing: 0\n"
            "  m_DoubleSidedGI: 0\n"
            "  m_CustomRenderQueue: -1\n"
            "  stringTagMap:\n"
            "    RenderType: Opaque\n"
            "  disabledShaderPasses: []\n"
            "  m_SavedProperties:\n"
            "    serializedVersion: 3\n"
            "    m_TexEnvs:\n"
            "    - _BaseMap:\n"
            "        m_Texture: {fileID: 0}\n"
            "        m_Scale: {x: 1, y: 1}\n"
            "        m_Offset: {x: 0, y: 0}\n"
            "    - _BumpMap:\n"
            "        m_Texture: {fileID: 0}\n"
            "        m_Scale: {x: 1, y: 1}\n"
            "        m_Offset: {x: 0, y: 0}\n"
            "    m_Floats:\n"
            f"    - _Smoothness: {smoothness}\n"
            "    - _Metallic: 0\n"
            "    m_Colors:\n"
            f"    - _BaseColor: {{r: {r8}, g: {g8}, b: {b8}, a: 1}}\n"
            f"    - _EmissionColor: {{r: 0, g: 0, b: 0, a: 1}}\n"
        )


def _material_hex_and_smoothness(spec) -> tuple[str, float]:
    """MaterialSpec has no direct color field (pbr_maps are texture PATHS, not
    generated texture data — no real texture-generation backend exists). Reuse
    an author-supplied hex color from `parameters` when present; otherwise fall
    back to a neutral mid-gray so the material is a valid, honest placeholder
    rather than a fabricated color standing in for missing data."""
    params = spec.parameters or {}
    hexval = str(params.get("color", "")).strip()
    if hexval.startswith("#") and len(hexval) in (4, 7):
        try:
            _hex_to_rgb(hexval)   # validates it actually parses
            color = hexval
        except ValueError:
            color = "#B0B0B0"
    else:
        color = "#B0B0B0"
    try:
        smoothness = max(0.0, min(1.0, float(params.get("smoothness", 0.3))))
    except (TypeError, ValueError):
        smoothness = 0.3
    return color, smoothness


# ── Built-in UGUI script GUIDs (com.unity.ugui, verified on Unity 6000.0) ───────
# NOTE: currently unused — the scene authors these GUIDs inline in scene.unity.j2.
# Kept as the single source of truth; GraphicRaycaster/EventSystem GUIDs changed
# from the old 2021–2022 values, so the inline ones must match these.
_UGUI_GUIDS = {
    "CanvasScaler":          "0cd44c1031e13a943bb63640046fad76",
    "GraphicRaycaster":      "dc42784cf147c0c48a680349fa168899",
    "EventSystem":           "76c392e42b5098c458856cdf6ecaaaa1",
    "StandaloneInputModule": "4f231c4fb786f3946a6b90b886c48677",
    "InputSystemUIInputModule": "01614664b831546d2ae94a42149d80ac",
}


# ── Input Actions builder ──────────────────────────────────────────────────────

def _generate_input_actions(schema: UnitySchema, pn: str) -> str:
    """Return a .inputactions JSON string tailored to the schema's game type."""

    def u(seed: str) -> str:
        return _make_uuid(pn, f"ia/{seed}")

    def act(name: str, act_type: str = "Button", control: str = "",
            initial: bool = False) -> dict:
        return {
            "name": name, "type": act_type,
            "id": u(f"act/{name}"),
            "expectedControlType": control,
            "processors": "", "interactions": "",
            "initialStateCheck": initial,
        }

    def bind(path: str, action: str, groups: str = "", part_name: str = "",
             is_composite: bool = False, is_part: bool = False) -> dict:
        return {
            "name": part_name,
            "id": u(f"bind/{action}/{path}"),
            "path": path,
            "interactions": "", "processors": "",
            "groups": groups, "action": action,
            "isComposite": is_composite,
            "isPartOfComposite": is_part,
        }

    def wasd(action_name: str, stick: str = "<Gamepad>/leftStick") -> list:
        return [
            bind("2DVector",          action_name, part_name="WASD", is_composite=True),
            bind("<Keyboard>/w",      action_name, "Keyboard&Mouse", "up",    is_part=True),
            bind("<Keyboard>/s",      action_name, "Keyboard&Mouse", "down",  is_part=True),
            bind("<Keyboard>/a",      action_name, "Keyboard&Mouse", "left",  is_part=True),
            bind("<Keyboard>/d",      action_name, "Keyboard&Mouse", "right", is_part=True),
            *([bind(stick, action_name, "Gamepad")] if stick else []),
        ]

    gt     = schema.game_type
    is_2d  = gt in ("2D", "2.5D", "2d_topdown")
    is_vr  = gt == "VR"
    mobile = schema.platform in ("Android", "iOS")

    # ── Player map ────────────────────────────────────────────────────────────
    p_acts:  list[dict] = [act("Move", "Value", "Vector2", initial=True)]
    p_binds: list[dict] = wasd("Move")

    if mobile:
        p_binds += [
            bind("2DVector",                         "Move", part_name="Touch", is_composite=True),
            bind("<Touchscreen>/primaryTouch/delta", "Move", "Touch", "xy", is_part=True),
        ]

    if is_vr:
        p_acts  += [act("Trigger", "Button", "Button"), act("Grip", "Button", "Button")]
        p_binds += [
            bind("<XRController>{LeftHand}/trigger",  "Trigger", "XRController"),
            bind("<XRController>{RightHand}/trigger", "Trigger", "XRController"),
            bind("<XRController>{LeftHand}/grip",     "Grip",    "XRController"),
            bind("<XRController>{RightHand}/grip",    "Grip",    "XRController"),
        ]
    else:
        if not is_2d:
            p_acts  += [act("Look", "Value", "Vector2", initial=True)]
            p_binds += [
                bind("<Mouse>/delta",        "Look", "Keyboard&Mouse"),
                bind("<Gamepad>/rightStick", "Look", "Gamepad"),
            ]
        p_acts  += [act("Jump",     "Button", "Button")]
        p_binds += [
            bind("<Keyboard>/space",       "Jump",     "Keyboard&Mouse"),
            bind("<Gamepad>/buttonSouth",  "Jump",     "Gamepad"),
        ]
        p_acts  += [act("Attack",   "Button", "Button")]
        p_binds += [
            bind("<Mouse>/leftButton",     "Attack",   "Keyboard&Mouse"),
            bind("<Gamepad>/buttonWest",   "Attack",   "Gamepad"),
        ]
        p_acts  += [act("Interact", "Button", "Button")]
        p_binds += [
            bind("<Keyboard>/e",           "Interact", "Keyboard&Mouse"),
            bind("<Gamepad>/buttonNorth",  "Interact", "Gamepad"),
        ]

    p_acts  += [act("Pause", "Button", "Button")]
    p_binds += [
        bind("<Keyboard>/escape", "Pause", "Keyboard&Mouse"),
        bind("<Gamepad>/start",   "Pause", "Gamepad"),
    ]

    player_map = {
        "name": "Player", "id": u("map/player"),
        "actions": p_acts, "bindings": p_binds,
    }

    # ── UI map ────────────────────────────────────────────────────────────────
    ui_acts = [
        act("Navigate",    "Value",       "Vector2", initial=True),
        act("Submit",      "Button"),
        act("Cancel",      "Button"),
        act("Point",       "PassThrough", "Vector2", initial=True),
        act("Click",       "PassThrough", "Button",  initial=True),
        act("ScrollWheel", "PassThrough", "Vector2", initial=True),
    ]
    ui_binds = [
        bind("2DVector",              "Navigate", part_name="Arrows", is_composite=True),
        bind("<Keyboard>/upArrow",    "Navigate", "Keyboard&Mouse", "up",    is_part=True),
        bind("<Keyboard>/downArrow",  "Navigate", "Keyboard&Mouse", "down",  is_part=True),
        bind("<Keyboard>/leftArrow",  "Navigate", "Keyboard&Mouse", "left",  is_part=True),
        bind("<Keyboard>/rightArrow", "Navigate", "Keyboard&Mouse", "right", is_part=True),
        bind("<Gamepad>/leftStick",   "Navigate", "Gamepad"),
        bind("<Gamepad>/dpad",        "Navigate", "Gamepad"),
        bind("<Keyboard>/enter",      "Submit",      "Keyboard&Mouse"),
        bind("<Gamepad>/buttonSouth", "Submit",      "Gamepad"),
        bind("<Keyboard>/escape",     "Cancel",      "Keyboard&Mouse"),
        bind("<Gamepad>/buttonEast",  "Cancel",      "Gamepad"),
        bind("<Mouse>/position",      "Point",       "Keyboard&Mouse"),
        bind("<Mouse>/leftButton",    "Click",       "Keyboard&Mouse"),
        bind("<Mouse>/scroll",        "ScrollWheel", "Keyboard&Mouse"),
    ]
    ui_map = {
        "name": "UI", "id": u("map/ui"),
        "actions": ui_acts, "bindings": ui_binds,
    }

    # ── Control schemes ───────────────────────────────────────────────────────
    def scheme(name: str, *device_paths: str) -> dict:
        return {
            "name": name, "id": u(f"scheme/{name}"),
            "bindingGroup": name,
            "devices": [
                {"devicePath": dp, "isOptional": False, "isOR": False}
                for dp in device_paths
            ],
        }

    schemes = [scheme("Keyboard&Mouse", "<Keyboard>", "<Mouse>"), scheme("Gamepad", "<Gamepad>")]
    if mobile:
        schemes.append(scheme("Touch", "<Touchscreen>"))
    if is_vr:
        schemes.append(scheme("XRController", "<XRController>"))

    return json.dumps(
        {"name": f"{pn}InputActions", "maps": [player_map, ui_map], "controlSchemes": schemes},
        indent=4,
    )


def _generate_obj(mesh: ProceduralMesh) -> str:
    """Minimal quad-plane .obj placeholder — rebuilt at runtime by MeshBuilder.cs."""
    return (
        f"# Auto-generated placeholder — {mesh.name}\n"
        f"# Type: {mesh.mesh_type}  Resolution: {mesh.resolution}\n"
        "# This geometry is rebuilt at runtime by MeshBuilder.cs\n"
        f"o {mesh.name}\n"
        "v -1.0  0.0 -1.0\n"
        "v  1.0  0.0 -1.0\n"
        "v  1.0  0.0  1.0\n"
        "v -1.0  0.0  1.0\n"
        "vn  0.0  1.0  0.0\n"
        "vt 0.0 0.0\n"
        "vt 1.0 0.0\n"
        "vt 1.0 1.0\n"
        "vt 0.0 1.0\n"
        "f 1/1/1 2/2/1 3/3/1 4/4/1\n"
    )


# ── UI Toolkit HUD assets (emitted when a ui_manager script exists, non-VR) ─────
# These back the HUD_Root → UIDocument the scene template authors. The guids and
# the UXML's importer-derived sourceAsset fileID (9197481963319205126) are fixed:
# the UXML content is byte-stable, so Unity's UXML importer reproduces the same
# fileID on every project, keeping the scene's sourceAsset reference valid.
_HUD_UXML_GUID         = "c2a4bf7586ffc544d98475ce2234da1f"
_HUD_PANELSETTINGS_GUID = "89a879e23b1fe464db3acbc7007a2bd1"

_HUD_UXML = (
    '<ui:UXML xmlns:ui="UnityEngine.UIElements" editor-extension-mode="False">\n'
    '  <ui:VisualElement name="HUD" style="flex-grow:1; padding:16px;">\n'
    '    <ui:Label name="ScoreLabel" text="Score: 0" style="color:rgb(255,255,255); font-size:28px; -unity-font-style:bold;"/>\n'
    '    <ui:Label name="LivesLabel" text="Lives: 3" style="color:rgb(255,255,255); font-size:28px; -unity-font-style:bold;"/>\n'
    '  </ui:VisualElement>\n'
    '  <ui:VisualElement name="PausePanel" style="display:none; position:absolute; top:0; left:0; right:0; bottom:0; align-items:center; justify-content:center; background-color:rgba(0,0,0,0.6);">\n'
    '    <ui:Label text="PAUSED" style="color:rgb(255,255,255); font-size:48px;"/>\n'
    '    <ui:Button name="ResumeButton" text="Resume"/>\n'
    '    <ui:Button name="RestartButton" text="Restart"/>\n'
    '    <ui:Button name="MenuButton" text="Main Menu"/>\n'
    '  </ui:VisualElement>\n'
    '  <ui:VisualElement name="GameOverPanel" style="display:none; position:absolute; top:0; left:0; right:0; bottom:0; align-items:center; justify-content:center; background-color:rgba(0,0,0,0.75);">\n'
    '    <ui:Label text="GAME OVER" style="color:rgb(255,80,80); font-size:48px;"/>\n'
    '    <ui:Button name="RestartButton" text="Restart"/>\n'
    '    <ui:Button name="MenuButton" text="Main Menu"/>\n'
    '  </ui:VisualElement>\n'
    '</ui:UXML>'
)

_HUD_UXML_META = (
    "fileFormatVersion: 2\n"
    f"guid: {_HUD_UXML_GUID}\n"
    "ScriptedImporter:\n"
    "  internalIDToNameTable: []\n"
    "  externalObjects: {}\n"
    "  serializedVersion: 2\n"
    "  userData: \n"
    "  assetBundleName: \n"
    "  assetBundleVariant: \n"
    "  script: {fileID: 13804, guid: 0000000000000000e000000000000000, type: 0}\n"
)

_HUD_PANELSETTINGS = (
    "%YAML 1.1\n"
    "%TAG !u! tag:unity3d.com,2011:\n"
    "--- !u!114 &11400000\n"
    "MonoBehaviour:\n"
    "  m_ObjectHideFlags: 0\n"
    "  m_CorrespondingSourceObject: {fileID: 0}\n"
    "  m_PrefabInstance: {fileID: 0}\n"
    "  m_PrefabAsset: {fileID: 0}\n"
    "  m_GameObject: {fileID: 0}\n"
    "  m_Enabled: 1\n"
    "  m_EditorHideFlags: 0\n"
    "  m_Script: {fileID: 19101, guid: 0000000000000000e000000000000000, type: 0}\n"
    "  m_Name: HUDPanelSettings\n"
    "  m_EditorClassIdentifier: UnityEngine.dll::UnityEngine.UIElements.PanelSettings\n"
    "  themeUss: {fileID: -4733365628477956816, guid: 7312b696fbaa8d342bc32d6d1307f3a8, type: 3}\n"
    "  m_DisableNoThemeWarning: 0\n"
    "  m_TargetTexture: {fileID: 0}\n"
    "  m_RenderMode: 0\n"
    "  m_ColliderUpdateMode: 0\n"
    "  m_ColliderIsTrigger: 1\n"
    "  m_ScaleMode: 2\n"
    "  m_ReferenceSpritePixelsPerUnit: 100\n"
    "  m_PixelsPerUnit: 100\n"
    "  m_Scale: 1\n"
    "  m_ReferenceDpi: 96\n"
    "  m_FallbackDpi: 96\n"
    "  m_ReferenceResolution: {x: 1920, y: 1080}\n"
    "  m_ScreenMatchMode: 0\n"
    "  m_Match: 0\n"
    "  m_SortingOrder: 0\n"
    "  m_TargetDisplay: 0\n"
    "  m_BindingLogLevel: 0\n"
    "  m_ClearDepthStencil: 1\n"
    "  m_ClearColor: 0\n"
    "  m_ColorClearValue: {r: 0, g: 0, b: 0, a: 0}\n"
    "  m_VertexBudget: 0\n"
    "  m_TextureSlotCount: 8\n"
    "  m_DynamicAtlasSettings:\n"
    "    m_MinAtlasSize: 64\n"
    "    m_MaxAtlasSize: 4096\n"
    "    m_MaxSubTextureSize: 64\n"
    "    m_ActiveFilters: 31\n"
    "  m_AtlasBlitShader: {fileID: 9101, guid: 0000000000000000f000000000000000, type: 0}\n"
    "  m_DefaultShader: {fileID: 9100, guid: 0000000000000000f000000000000000, type: 0}\n"
    "  m_RuntimeGaussianBlurShader: {fileID: 20300, guid: 0000000000000000f000000000000000, type: 0}\n"
    "  m_RuntimeColorEffectShader: {fileID: 20301, guid: 0000000000000000f000000000000000, type: 0}\n"
    "  m_SDFShader: {fileID: 19011, guid: 0000000000000000f000000000000000, type: 0}\n"
    "  m_BitmapShader: {fileID: 9001, guid: 0000000000000000f000000000000000, type: 0}\n"
    "  m_SpriteShader: {fileID: 19012, guid: 0000000000000000f000000000000000, type: 0}\n"
    "  m_ICUDataAsset: {fileID: 0}\n"
    "  forceGammaRendering: 0\n"
    "  textSettings: {fileID: 0}\n"
)

_HUD_PANELSETTINGS_META = (
    "fileFormatVersion: 2\n"
    f"guid: {_HUD_PANELSETTINGS_GUID}\n"
    "NativeFormatImporter:\n"
    "  externalObjects: {}\n"
    "  mainObjectFileID: 11400000\n"
    "  userData: \n"
    "  assetBundleName: \n"
    "  assetBundleVariant: \n"
)


class UnityProjectGenerator:
    def __init__(self, template_dir: str = "templates/unity"):
        tpl_path = Path(template_dir)
        # Single environment searches root templates then scripts/ subdirectory.
        # ChoiceLoader tries loaders in order, so root templates take priority.
        self._env = Environment(
            loader        = ChoiceLoader([
                FileSystemLoader(str(tpl_path)),
                FileSystemLoader(str(tpl_path / "scripts")),
                FileSystemLoader(str(tpl_path / "editor")),
            ]),
            undefined     = StrictUndefined,
            trim_blocks   = True,
            lstrip_blocks = True,
            # Jinja's default strips the template source's own trailing newline.
            # Unity's native YAML-ish asset format (.prefab/.unity/.asset) is NOT
            # lenient about a missing final newline — its parser reads past EOF
            # looking for the value after the last key and reports a confused
            # "Parser Failure ... Expect ':'" one line past the real content
            # (hit live: StickmanPlayer.prefab). C# scripts tolerate it, but
            # every file this env renders shares one setting, so keep it on.
            keep_trailing_newline = True,
        )
        self.template_dir = tpl_path

    # ── model resolution (single source of truth) ──────────────────────────────

    @staticmethod
    def _entity_footprints(schema: UnitySchema,
                           entity_model_paths: dict,
                           by_resource_path: dict,
                           lookups_dir: str) -> dict[str, float]:
        """Entity name -> real ground footprint (metres) of its resolved model.

        Reads the `bounds` recorded on catalog entries by
        `python main.py unity-catalog --measure` (Unity's own import size — see
        src/unity_model_measure.py for why nothing else is trustworthy here).
        Entities with no measured model are simply absent, and the layout pass
        keeps its primitive approximation for them.
        """
        cat_path = Path(lookups_dir) / "unity" / "asset_catalog.json"
        try:
            assets = json.loads(cat_path.read_text(encoding="utf-8"))["assets"]
        except (OSError, ValueError, KeyError):
            return {}

        # Key on the RESOLVED absolute source path: catalog roots are stored
        # relative to the repo ("Models/FBX format") while ResolvedModel.src is
        # absolute, so comparing them raw silently matches nothing and this
        # whole pass no-ops. Also index the bare relpath as a fallback for
        # callers whose roots differ from the catalog's.
        by_src: dict[str, list] = {}
        for a in assets:
            b = a.get("bounds")
            if not b:
                continue
            rel = Path(a["root"]) / a["relpath"]
            try:
                by_src[rel.resolve().as_posix().lower()] = b
            except OSError:
                pass
            by_src.setdefault(rel.as_posix().lower(), b)

        out: dict[str, float] = {}
        for e in schema.entities:
            res_path = entity_model_paths.get(e.name) or ""
            model = by_resource_path.get(res_path)
            if model is None or not getattr(model, "src", ""):
                continue
            src = Path(model.src)
            try:
                key = src.resolve().as_posix().lower()
            except OSError:
                key = src.as_posix().lower()
            b = by_src.get(key) or by_src.get(src.as_posix().lower())
            if not b:
                continue
            footprint = max(float(b[0]), float(b[2]))   # ground plane only
            if footprint > 0:
                out[e.name] = footprint
        return out

    def _resolve_all_models(self, schema: UnitySchema, lookups_dir: str,
                            model_source_dir: str | None,
                            model_overrides: dict[str, str] | None,
                            composed_units: list | None,
                            strict_assets: bool,
                            asset_cfg: dict | None,
                            sketchfab_cfg: dict | None = None) -> tuple[dict, dict[str, str]]:
        """Resolve every model the project uses: default pack → Blender
        overrides → per-entity catalog picks → rig downgrades.

        Both generate() (.meta emission + template paths) and write() (binary
        copies) call this — they MUST see identical results or the copies and
        .meta files desync. Deterministic: pure lookups, no LLM.

        Returns (used_models, entity_model_paths).
        """
        _override_kinds = set(model_overrides.keys()) if model_overrides else None
        all_models = _apply_model_overrides(
            resolve_default_models(lookups_dir=lookups_dir,
                                   model_source_dir=model_source_dir),
            model_overrides)
        used_models = models_for_schema(
            schema, all_models, composed_units, override_kinds=_override_kinds)

        # Per-entity assignment: explicit kind → full-catalog semantic match →
        # strict hard-fail / logged prop fallback (FAIL-FAST doctrine: scene
        # content comes from a real asset or the build stops — the schema
        # default kind "prop" no longer funnels everything to one model).
        entity_model_paths = resolve_entity_assets(
            schema, used_models, all_models,
            lookups_dir=lookups_dir, asset_cfg=asset_cfg, strict=strict_assets,
            sketchfab_cfg=sketchfab_cfg)

        # Downgrade humanoid -> generic (clean FBX import, no retarget clips) in
        # two independent cases:
        #  1. 2D games render orthographically (flat sprite-plane) — never
        #     import a Humanoid avatar there regardless of the model's actual
        #     rig quality. 2.5D no longer belongs here: it composes a real 3D
        #     CharacterController + world (see unity_compose.py's
        #     GOLDEN_2_5D_CORE_SIGNATURE) so a genuine humanoid rig is fine.
        #  2. The STATIC default pack's player/enemy FBX placeholders are
        #     confirmed to carry NO real humanoid skeleton at all (verified:
        #     zero Hips/Spine/UpperLeg/LowerLeg bone names in
        #     Models/Defaults/{player,enemy}/*.fbx) despite the catalog
        #     labeling them `rig: "humanoid"` — forcing Humanoid on them always
        #     throws "Invalid Avatar Rig Configuration: Required human bone
        #     'LeftLowerLeg' not found" (hit live on a VR project — the crash
        #     is NOT 2D-specific, gating on game_type alone was too narrow).
        #     Only a genuine Blender-built override (_apply_model_overrides,
        #     kind in model_overrides) actually ships a real named skeleton, so
        #     only THAT case is trusted to keep Humanoid.
        # Runs after entity resolution: the fallback path can still insert a
        # fresh used_models entry, and a humanoid model added that late would
        # otherwise skip the downgrade. Living here (not generate()) also keeps
        # write()'s binary copies in step — a downgrade that drops clips must
        # drop their copies too, not just their .meta files.
        _blender_kinds = set(model_overrides.keys()) if model_overrides else set()
        # 2d_topdown is flat-sprite like "2D" (SpriteRenderer, not a real
        # humanoid mesh) — unlike 2.5D, which uses real 3D models and keeps
        # Humanoid. See unity_asset_resolver.py's _GAME_TYPE_CONTEXTS: "2D" and
        # "2d_topdown" both skip 3D Kenney models entirely.
        _is_2d = schema.game_type in ("2D", "2d_topdown")
        for _kind, _m in used_models.items():
            if _m.rig == "humanoid" and (_is_2d or _kind not in _blender_kinds):
                _m.rig = "generic"
                _m.clips = {}

        # Stamp the resolved table so Step 7 can grade WHAT each entity became,
        # not just that it exists. entity_model_paths is a return value consumed
        # by wiring and never persisted, so `concept_fidelity`'s asset half —
        # the one that catches "commit node -> marble-center-butterfly-low.fbx",
        # where the entity name survived and the model is a marble run — had
        # nothing to read and silently graded only modifiers. Same sidecar
        # pattern as authored_models / spatial_facts, and it makes a saved
        # schema replayable through `unity-fidelity` with its assets intact.
        if isinstance(schema.meta, dict):
            schema.meta["entity_model_paths"] = dict(entity_model_paths)
        return used_models, entity_model_paths

    # ── public entry point ─────────────────────────────────────────────────────

    def generate(self, schema: UnitySchema, manifest_deps: dict[str, str],
                 lookups_dir: str = "lookups",
                 kenney_source_dir: str | None = None,
                 composed_units: list | None = None,
                 model_source_dir: str | None = None,
                 model_overrides: dict[str, str] | None = None,
                 strict_assets: bool = False,
                 asset_cfg: dict | None = None,
                 sketchfab_cfg: dict | None = None) -> dict[str, str]:
        """
        Returns {relative_path: file_content} for every file in the project.
        Caller writes the dict to disk.

        `composed_units` (from MicroComposer.plan) are spliced into each scene as a
        `_GameSystems` GameObject carrying one MonoBehaviour per composed micro,
        referenced by its deterministic .cs.meta GUID.
        """
        files: dict[str, str] = {}
        pn = schema.project_name

        # ── Custom-script lint pass ───────────────────────────────────────────
        # LLM-authored C# fragments are defensively cleaned before any render
        # (same battle-tested pass the micro generators run in default_fix).
        # Done here — not in the pipeline — so unity-validate and the webui get
        # it too. CustomBehaviour.cs.j2 emits no fields of its own, so the only
        # reserved name is nothing; usings dedupe against the template's.
        # Service-locator validation context: a custom script's public surface
        # is its PUBLIC extra_methods only (never extra_fields — always
        # private); canonical/composed/Unity classes are opaque (validated by
        # their own dedicated passes).
        _custom_api = _custom_script_api(schema.scripts)
        _opaque_types: frozenset[str] = frozenset(
            {s.name for s in schema.scripts if s.type != "custom"}
            | {u.class_name for u in (composed_units or [])})
        # Stub-avoidance needs a WIDER set than _opaque_types: a custom script's
        # field can legitimately name ANOTHER custom script as its type (e.g.
        # MuseumMetaProgression referencing GossipHub) -- that name already
        # resolves to a real class this same loop generates, so stubbing it too
        # is CS0101 (duplicate definition), not a missing type. _opaque_types
        # excludes "custom" scripts on purpose for the OTHER two call sites
        # below (their public surface is validated via the richer _custom_api
        # instead), but _collect_custom_type_stubs has no such richer path --
        # it only ever asks "is this name already real," so it needs every
        # script name, custom or not. Confirmed live: MuseumMetaProgression.cs
        # got its own empty `class GossipHub {}` stub alongside the real
        # GossipHub.cs script — CS0101, "already contains a definition".
        _stub_known_types: frozenset[str] = _opaque_types | frozenset(
            s.name for s in schema.scripts)
        for s in schema.scripts:
            if s.type == "custom":
                # hoist_field_inits: CustomBehaviour.cs.j2 renders awake_extra
                # (and emits an Awake() whenever it is non-empty), so an
                # instance-referencing initializer the model wrote one line too
                # early — `private Rigidbody rb = GetComponent<Rigidbody>();`,
                # CS0236, live on a Monaco monobike run — is moved into Awake()
                # rather than dropped to a null the scene never assigns.
                lint_extensible(s, reserved_fields=set(),
                                template_usings={"UnityEngine"},
                                hoist_field_inits=True)
                # Must run AFTER lint_extensible: it needs the final, cleaned
                # extra_fields to know which field types are our own stubs.
                # _stub_known_types (not _opaque_types): must agree with the
                # stub-generation call below on what counts as "already real,"
                # or a field whose type resolves to a REAL sibling script (no
                # stub emitted for it) gets its method calls stripped here as
                # if it pointed at an empty stub.
                # Must run BEFORE _sanitize_stub_type_usage: see its docstring.
                _sanitize_invented_namespace_field_types(s, _stub_known_types)
                _sanitize_stub_type_usage(s, _stub_known_types)
                _sanitize_invented_constructor_calls(s, _stub_known_types)
                _sanitize_builtin_component_call_arity(s)
                _sanitize_coroutine_field_type(s)
                _sanitize_composed_component_calls(s, composed_units)
                _sanitize_project_service_calls(s, _custom_api, _opaque_types)
                _sanitize_field_typed_member_calls(s, _custom_api, composed_units)
                _sanitize_param_typed_member_calls(s, _custom_api, composed_units)
                _sanitize_nullable_valuetype_access(s)    # X?.transform.position -> CS1503
                _auto_declare_undeclared_scalars(s)       # bare persisted field, never declared
                _strip_undeclared_foreach(s)
                _sanitize_undeclared_self_calls(s)        # bare self-call, no receiver
                _sanitize_undeclared_condition_reads(s)   # bare if/while value read
                # A custom script can only attach to the Player if one exists.
                if s.attach_to == "player" and not any(
                        x.type == "player" for x in schema.scripts):
                    s.attach_to = "systems"

        # ── Entity behavior reconciliation ────────────────────────────────────
        # `behavior` must name a class that actually exists or AddComponent
        # silently no-ops (hit live: LLM invented "AIWaypointDriver" while the
        # composed AI class was "EnemyAI" → rivals never moved). Unknown names
        # rewire to the composed AI class, else an enemy-type script, else "".
        if schema.entities:
            known = {s.name for s in schema.scripts}
            ai_fallback = next((s.name for s in schema.scripts if s.type == "enemy"), "")
            for u in (composed_units or []):
                known.add(u.class_name)
                if u.kind == "ai":
                    ai_fallback = u.class_name
            # Atom-driven entities get a generated <Name>Behavior class — add those
            # class names to `known` so the reconciliation below does not clear them.
            for e in schema.entities:
                if e.atoms:
                    known.add(entity_behavior_class_name(e.name))
            for e in schema.entities:
                if e.behavior and e.behavior not in known:
                    e.behavior = ai_fallback if e.kind == "enemy" else ""
                # LLM schema-fill routinely leaves `behavior` blank entirely
                # (copies the prompt's own empty-string example verbatim —
                # same failure mode as PR#75's extra_* fields) even for
                # kind="enemy" entities whose concept explicitly describes
                # patrol/chase AI. Live-caught on RustbeltDepthTestbed: six
                # "SentryDrones" with kind="enemy" and behavior="" never got
                # EnemyAI attached at spawn — inert props, not enemies.
                # Backfill from the same composed AI class the line above
                # already resolves, so an enemy entity is never silently
                # left with no AI just because the LLM left the field empty.
                elif not e.behavior and e.kind == "enemy" and ai_fallback:
                    e.behavior = ai_fallback

        # ── Kenney asset resolution ───────────────────────────────────────────
        kenney_assets = resolve_assets(schema, lookups_dir=lookups_dir,
                                       source_dir=kenney_source_dir)

        # ── Model resolution (default pack + per-entity catalog picks) ────────
        used_models, entity_model_paths = self._resolve_all_models(
            schema, lookups_dir, model_source_dir, model_overrides,
            composed_units, strict_assets, asset_cfg, sketchfab_cfg)
        any_humanoid = any(m.rig == "humanoid" for m in used_models.values())

        # Entity name -> bounds-fit target (0.0 = no fit), for BoundsFit at
        # spawn time. entity_model_paths maps name -> resource_path; look up
        # the ResolvedModel that owns that path to read its fit_size (set only
        # for Sketchfab picks — see unity_entity_asset_resolver._register).
        _by_resource_path = {m.resource_path: m for m in used_models.values()}

        # ── Model-aware re-layout ────────────────────────────────────────────
        # compute_layout() already ran in Step 5, but the layout pass happens
        # BEFORE model resolution and so had to size every group as if each
        # instance were ~1 unit across. Now that we know which model each entity
        # actually resolved to, redo the packing with real measured footprints
        # (catalog `bounds`, produced by `unity-catalog --measure`). Without
        # this, large models bake onto a grid smaller than a single instance and
        # spawn as one overlapping clump. compute_layout is idempotent, so the
        # second run simply supersedes the first; with no measured bounds
        # available it is a no-op and output stays byte-identical.
        _footprints = self._entity_footprints(schema, entity_model_paths,
                                              _by_resource_path, lookups_dir)
        if _footprints:
            from .unity_layout import compute_layout as _relayout
            for e in schema.entities:
                # Drop the model-blind bake so the pass recomputes it; groups
                # whose positions came from a deterministic scaffold (racetrack
                # start grids) are left alone — they are anchored to real
                # geometry, not to this pass's packing.
                if e.name in _footprints and e.placement in ("scatter", "grid", "ring", "line"):
                    e.instance_positions = []
            _relayout(schema, _footprints)

        # ── Playable-player merge ──────────────────────────────────────────
        # The scene template ALWAYS builds a dedicated Player object (tag
        # Player + CapsuleCollider + Rigidbody + PlayerController) — that is
        # the object CameraFollow tracks and every CompareTag(playerTag) gate
        # in the interactable / transition / npc_dialog micros latches onto.
        # A brief-named entity tagged Player (a count-1 "vehicle" racing kart,
        # OR now a walking "character"/"npc" avatar after the concept-entity
        # rescue tags it) would otherwise ALSO spawn — a static second
        # Player-tagged object with a collider but no Rigidbody and no
        # controller, while GameObject.FindWithTag("Player") picks between the
        # two arbitrarily. That splits the player: the pickup trigger may find
        # the inert model, the camera the real one. Merge instead — the scene
        # Player takes the entity's resolved model, bounds-fit and baked spawn,
        # and the spawner skips the duplicate — so exactly one, fully-equipped
        # Player carries the tag.
        #
        # Kinds that can BE the avatar; mirrors unity_concept_entities'
        # _AVATAR_KINDS (the same set that earns the Player tag), minus none.
        _PLAYER_MERGE_KINDS = {"vehicle", "character", "player", "npc", "creature"}
        player_spawn = None
        player_vehicle_model = None
        player_avatar_model = None
        # A dedicated Player object exists when the schema carries a "player"
        # script (scene.unity.j2's scripts_by_type["player"] path) OR — in
        # --compose mode, where drop_owned_scripts removes that script because
        # the controller micro owns it — when a composed unit targets "player"
        # (scene.unity.j2's player_units block). Gating on the script alone
        # missed the compose case entirely: the merge never ran, so BOTH the
        # racing kart and the walking avatar spawned as a second Player-tagged
        # object alongside the composed one.
        _has_player_object = (
            any(s.type == "player" for s in schema.scripts)
            or any(getattr(u, "target", None) == "player"
                   for u in (composed_units or [])))
        if schema.game_type not in ("2D", "2d_topdown") and _has_player_object:
            _pe = next(
                (e for e in schema.entities
                 if e.tag == "Player" and e.count == 1
                 and (e.kind or "").lower() in _PLAYER_MERGE_KINDS), None)
            # ── Spawn transfer — deliberately NOT gated on the merge ──────────
            # The layout pass decides where the player starts, and this was the
            # only code that carried that decision into the scene. Nested inside
            # the merge it inherited all three of the merge's conditions, so any
            # schema that failed one of them silently spawned the avatar at the
            # template's origin instead: a captured DropZone run laid the player
            # out at the top of a 2000 m descent corridor and shipped a scene
            # with the avatar at y=1, 2000 m below its own game, and every
            # automated check passed. The model transfer below still needs a
            # mergeable entity; the START POSITION only needs the layout to have
            # chosen one, so it is read from the player's group directly (the
            # same rank-based resolution the layout used to pick it).
            # Capsule pivot is its centre — feet 1 m below.
            _spawn_src, _spawn_signal = pick_player_entity(schema.entities)
            if _spawn_src is not None and _spawn_src.instance_positions:
                _px, _py, _pz = (round(v, 3)
                                 for v in _spawn_src.instance_positions[0])
                _yaw = (round(_spawn_src.instance_yaws[0], 3)
                        if _spawn_src.instance_yaws else 0.0)
                _half = math.radians(_yaw) / 2.0
                player_spawn = {
                    "position": f"{{x: {_px}, y: {round(_py + 1.0, 3)}, z: {_pz}}}",
                    "rotation": (f"{{x: 0, y: {round(math.sin(_half), 6)}, "
                                 f"z: 0, w: {round(math.cos(_half), 6)}}}"),
                    "euler":    f"{{x: 0, y: {_yaw}, z: 0}}",
                }
            if _pe is not None:
                _is_vehicle = (_pe.kind or "").lower() == "vehicle"
                _pe_path = entity_model_paths.get(_pe.name, "")
                if _pe_path and not _pe_path.startswith(
                        ("skip:", "procedural:", "fx:", "ui:")):
                    _pe_model = _by_resource_path.get(_pe_path)
                    _merged = {
                        "resource_path": _pe_path,
                        "scale": _pe.scale,
                        "fit_size": _pe_model.fit_size if _pe_model else 0.0,
                    }
                    if _is_vehicle:
                        # A vehicle visual — the humanoid DefaultCharacter
                        # animator controller doesn't apply (VehicleBoneDriver
                        # still drives wheel bones from motion).
                        _merged["controller"] = ""
                        player_vehicle_model = _merged
                    else:
                        # A humanoid visual — leave "controller" unset so the
                        # scene template falls back to the DefaultCharacter
                        # animator controller (character_controller_res).
                        player_avatar_model = _merged
                entity_model_paths[_pe.name] = "skip:merged-into-player"

        entity_fit_sizes = {
            name: (_by_resource_path[path].fit_size
                   if path in _by_resource_path else 0.0)
            for name, path in entity_model_paths.items()
        }
        # Negative fit_size = shrink-only clamp (kind_sizes.json clamp_sizes,
        # any source) — gate the spawner's clamp branch so projects with no
        # clamped entity render byte-identical C#.
        has_clamped_entities = any(v < 0 for v in entity_fit_sizes.values())

        # Attribution for non-CC0 Sketchfab picks (pure string formatting over
        # used_models — no network, deterministic given the resolution above).
        _credits = _render_credits_md(used_models)
        if _credits is not None:
            files[f"{pn}/CREDITS.md"] = _credits

        # Entities routed to schema procedural meshes build via MeshBuilder at
        # spawn time — make sure the class exists even when the LLM authored
        # procedural_meshes without a mesh_builder script entry.
        has_procedural_entities = any(
            p.startswith("procedural:") for p in entity_model_paths.values())
        # fx-kind entities spawn runtime ParticleSystems instead of meshes —
        # gate the spawner's particle branch so meshes-only projects stay
        # byte-identical.
        has_fx_entities = any(
            p.startswith("fx:") for p in entity_model_paths.values())
        # A non-embodied game's content is UI: entities routed to "ui:<element>"
        # are BUILT as board controls (BuildUIElement) rather than loaded, and
        # the flag gates that whole branch so every other project's spawner
        # stays byte-identical. See Tier U in unity_entity_asset_resolver.py.
        has_ui_entities = any(
            p.startswith("ui:") for p in entity_model_paths.values())
        # A board is laid out by compute_layout (the UISurface primitive) and
        # spawned by the generic branch, which is the only one that honours the
        # baked instance positions. The two 2D builders lay their content out
        # around a PLAYER — a side-scroller's lane, a top-down arena about the
        # spawn — so for a game with no avatar they arrange the board around
        # something that is not there. That is exactly how CodeVault's file
        # icons ended up scattered like debris.
        ui_board = not is_embodied(schema)
        # path_width > 0 on a path-placed group = end-to-end segment tiling
        # (SpawnTiledGroup) — gate the spawner's tile branch so projects
        # without tiled paths stay byte-identical. 3D only: 2D archetypes
        # never take the path branch.
        has_tiled_paths = schema.game_type not in ("2D", "2d_topdown") and any(
            e.placement == "path" and e.path_points
            and getattr(e, "path_width", 0.0) > 0.0
            for e in schema.entities)
        # Baked per-instance facing (start grids) — gate the spawner's yaw
        # arrays/param so projects without any stay byte-identical.
        has_baked_yaws = schema.game_type not in ("2D", "2d_topdown") and any(
            e.instance_yaws and e.instance_positions
            and not entity_model_paths.get(e.name, "").startswith("skip:")
            for e in schema.entities)
        # Banked path points ([x, y, z, bank°] — schema clamps bank to ±45
        # and drops zeros): per-tiled-group roll arrays lerped along the
        # polyline at runtime, gated so unbanked projects stay byte-identical.
        entity_path_banks: dict[str, list[float]] = {}
        if schema.game_type not in ("2D", "2d_topdown"):
            for e in schema.entities:
                if (e.placement == "path" and e.path_points
                        and getattr(e, "path_width", 0.0) > 0.0
                        and not entity_model_paths.get(e.name, "").startswith("skip:")
                        and any(len(p) > 3 for p in e.path_points)):
                    entity_path_banks[e.name] = [
                        round(p[3], 3) if len(p) > 3 else 0.0
                        for p in e.path_points]
        has_banked_paths = bool(entity_path_banks)
        if has_procedural_entities and not any(
                s.type == "mesh_builder" for s in schema.scripts):
            schema.scripts.append(UnityScript(
                name="MeshBuilder", type="mesh_builder",
                description="Singleton procedural mesh factory"))

        palette   = schema.color_palette  # always non-None after model_validator
        assert palette is not None
        lighting  = _LIGHTING_BY_STYLE.get(schema.visual_style,
                                           _LIGHTING_BY_STYLE["stylized"])
        bg        = _unity_color(palette.background)
        _palette_rgb = {
            "primary":    _unity_color(palette.primary),
            "secondary":  _unity_color(palette.secondary),
            "accent":     _unity_color(palette.accent),
            "background": bg,
            "trim":       _unity_color(palette.trim),
        }

        # ── Environment mood (time of day / fog) ──────────────────────────────
        # "day" (or no environment block) keeps the historical style lighting
        # byte-for-byte; other times of day swing the sun + tint it. Scene specs
        # recompute this per scene so StormShelf can be fogged night while
        # FinalApproach is a clear dawn.
        env = schema.environment
        if env is not None:
            _tod, _fog, _fog_color, _sun_i = (
                env.time_of_day, env.fog, env.fog_color, env.sun_intensity)
        else:
            # No explicit environment block — fall back to schema.world_spec's
            # time_of_day/weather (previously orphaned: WorldSpec had zero
            # consumers outside .regions). Additive only, no behavior change
            # when neither block is present.
            _tod, _fog, _fog_color, _sun_i = _world_spec_env_fallback(schema.world_spec)
        env_ctx = _environment_ctx(_tod, _fog, _fog_color, _sun_i, lighting, bg)
        sun_rotation  = env_ctx["sun_rotation"]
        sun_euler     = env_ctx["sun_euler"]
        sun_color     = env_ctx["sun_color"]
        sun_intensity = env_ctx["sun_intensity"]
        fog_enabled   = env_ctx["fog_enabled"]
        fog_color     = env_ctx["fog_color"]

        # GUIDs for generated .mat files (deterministic)
        mat_guids = {
            "primary":   _make_guid(f"{pn}/Materials/Primary"),
            "secondary": _make_guid(f"{pn}/Materials/Secondary"),
            "accent":    _make_guid(f"{pn}/Materials/Accent"),
            "trim":      _make_guid(f"{pn}/Materials/Trim"),
        }

        base_ctx = {
            "project_name":      pn,
            "company_name":      schema.company_name,
            "description":       schema.description,
            # Which subsystem kinds the composer owns — lets IR-driven scripts
            # (e.g. SceneSetup) defer UI to the GameHud/PauseMenu micros instead
            # of building a duplicate generic HUD.
            "composed_kinds":    sorted({u.kind for u in (composed_units or [])}),
            "game_type":         schema.game_type,
            "platform":          schema.platform,
            "render_pipeline":   schema.render_pipeline,
            "unity_version":     schema.unity_version,
            "color_space":       schema.color_space,
            "visual_style":      schema.visual_style,
            "color_palette":     palette,
            "palette_rgb": _palette_rgb,
            **_ambient_ctx(_palette_rgb, _lighting_variant_for(schema, composed_units)),
            "mat_guids":         mat_guids,
            "lighting":          lighting,
            "sun_rotation":      sun_rotation,
            "sun_euler":         sun_euler,
            "sun_color":         sun_color,
            "sun_intensity":     sun_intensity,
            "fog_enabled":       fog_enabled,
            "fog_color":         fog_color,
            "features":          schema.features,
            "scenes":            schema.scenes,
            "story_beats":       schema.story_beats,
            "audio_tracks":      schema.audio_tracks,
            "procedural_meshes": schema.procedural_meshes,
            "entities":          schema.entities,
            "entity_model_paths": entity_model_paths,
            "entity_fit_sizes":  entity_fit_sizes,
            "has_procedural_entities": has_procedural_entities,
            "has_fx_entities":   has_fx_entities,
            "has_ui_entities":   has_ui_entities,
            "ui_board":          ui_board,
            "has_tiled_paths":   has_tiled_paths,
            "has_baked_yaws":    has_baked_yaws,
            "has_banked_paths":  has_banked_paths,
            "entity_path_banks": entity_path_banks,
            "has_clamped_entities": has_clamped_entities,
            # Scene → entity-group names for specs that declare a subset (None
            # = all groups, absent from the map). Drives EntitySpawner's
            # runtime per-scene filter.
            "scene_entity_map":  {sp.name: list(sp.entities)
                                  for sp in schema.scene_specs
                                  if sp.entities is not None},
            "kenney_assets":     kenney_assets,
            "materials":         schema.materials,
            "shaders":           schema.shaders,
        }

        _ev, _er = _VERSION_MAP.get(schema.unity_version, ("6000.0.66f2", "0000000000000"))
        files[f"{pn}/ProjectSettings/ProjectVersion.txt"] = self._render(
            "ProjectVersion.txt.j2", {"editor_version": _ev, "editor_revision": _er}
        )

        files[f"{pn}/Packages/manifest.json"] = self._render(
            "manifest.json.j2", {**base_ctx, "dependencies": manifest_deps}
        )

        files[f"{pn}/ProjectSettings/ProjectSettings.asset"] = self._render(
            "ProjectSettings.asset.j2", {**base_ctx, "product_guid": _make_guid(pn)}
        )
        files[f"{pn}/ProjectSettings/QualitySettings.asset"] = self._render(
            "QualitySettings.asset.j2", base_ctx
        )
        # Entity tags must exist in TagManager or `go.tag = ...` errors at runtime
        # ("Tag: EnemyCar is not defined" — hit live on the racing beta run).
        _builtin_tags = {"Player", "Enemy", "Collectible", "Ground", "Obstacle",
                         "Interactable", "Projectile", "Trigger", "Untagged"}
        entity_tags = sorted({
            e.tag for e in schema.entities
            if e.tag and e.tag not in _builtin_tags
        })
        files[f"{pn}/ProjectSettings/TagManager.asset"] = self._render(
            "TagManager.asset.j2", {**base_ctx, "entity_tags": entity_tags}
        )
        scene_guids = {s: _make_guid(f"{pn}/scene/{s}") for s in schema.scenes}
        files[f"{pn}/ProjectSettings/EditorBuildSettings.asset"] = self._render(
            "EditorBuildSettings.asset.j2", {**base_ctx, "scene_guids": scene_guids}
        )

        # A UI board is the X-Y plane at z = 0 (src/unity_layout._layout_ui_board),
        # so the camera has to look STRAIGHT AT it from -Z. Every 3D-ish preset
        # is tilted down at a ground plane — the 3D one by 35 degrees — which
        # frames a board edge-on. Keyed off the declaration rather than the
        # game_type because a non-embodied game can be authored 2D, top-down or
        # 3D and the board is the same shape in all three. The composed
        # board_framing camera re-fits this at runtime once the board exists;
        # this is what a build WITHOUT --compose gets, and it is already
        # correct because the layout centres the board on the origin.
        _cam = (_CAMERA_PRESETS["2D"] if not is_embodied(schema)
                else _CAMERA_PRESETS.get(schema.game_type, _CAMERA_PRESETS["3D"]))
        # scripts_by_type lets the scene template wire MonoBehaviour components
        # to the correct script GUID without looping in Jinja2.
        scripts_by_type = {s.type: s for s in schema.scripts}
        script_guids    = {
            s.name: _make_guid(f"{pn}/Scripts/{s.name}") for s in schema.scripts
        }
        # Custom scripts attach per `attach_to`; each gets a deterministic scene
        # MonoBehaviour fileID (base 900003010) so regeneration is stable.
        custom_scripts = [
            {
                "name":      s.name,
                "guid":      script_guids[s.name],
                "file_id":   900003010 + i,
                "attach_to": s.attach_to,
            }
            for i, s in enumerate(x for x in schema.scripts if x.type == "custom")
        ]
        # SceneSetup.cs is always emitted — it builds the per-scene UI at runtime.
        _scene_setup_guid = _make_guid(f"{pn}/Scripts/SceneSetup")
        files[f"{pn}/Assets/Scripts/SceneSetup.cs"] = self._render(
            "scripts/SceneSetup.cs.j2", {**base_ctx, "project_name": pn}
        )
        files[f"{pn}/Assets/Scripts/SceneSetup.cs.meta"] = _meta_file(_scene_setup_guid)

        scene_ctx = {
            **base_ctx,
            "camera_position":  _cam["position"],
            "camera_rotation":  _cam["rotation"],
            "camera_euler":     _cam["euler"],
            "scripts_by_type":  scripts_by_type,
            "script_guids":     script_guids,
            "composed_units":   composed_units or [],
            "custom_scripts":   custom_scripts,
            "entity_spawner_guid": _make_guid(f"{pn}/Scripts/EntitySpawner"),
            # Model Pack v1 — drive mode keeps the car visual, so no player
            # swap; a merged player-vehicle group (racing start grid) swaps
            # in the kart regardless of locomotion, and a merged walking
            # avatar (character/npc tagged Player) swaps in its brief model
            # over the generic default character.
            "player_model": player_vehicle_model or player_avatar_model or (
                used_models.get("player") if schema.locomotion != "drive" else None),
            # Start-line transform for the playable Player (racing merge);
            # None keeps the historical origin placement byte-identical.
            "player_spawn": player_spawn,
            "enemy_model":  used_models.get("enemy"),
            "model_swap_guid": _make_guid(f"{pn}/Scripts/ModelSwap"),
            "character_controller_res": "Models/Default/DefaultCharacter",
            "scene_setup_guid": _scene_setup_guid,
        }
        # Per-scene content (SceneSpec): mood overrides recompute the sun/fog
        # block for that scene only; an entity filter controls which groups the
        # scene hosts (and whether it gets an EntitySpawner at all). Scenes
        # without a spec render exactly as before.
        _specs_by_scene = {sp.name: sp for sp in schema.scene_specs}
        for scene_name in schema.scenes:
            ctx = {**scene_ctx, "scene_name": scene_name}
            sp = _specs_by_scene.get(scene_name)
            if sp is not None:
                if sp.time_of_day or sp.fog is not None or sp.fog_color:
                    ctx.update(_environment_ctx(
                        sp.time_of_day or (env.time_of_day if env else ""),
                        sp.fog if sp.fog is not None else (env.fog if env else None),
                        sp.fog_color or (env.fog_color if env else None),
                        env.sun_intensity if env else None,
                        lighting, bg))
                if sp.entities is not None:
                    _allowed = set(sp.entities)
                    ctx["entities"] = [e for e in schema.entities
                                       if e.name in _allowed]
            # Ground plane sized to THIS scene's hosted entity groups (a
            # scene-filtered subset per SceneSpec.entities) rather than a
            # fixed constant — see src/unity_layout.py:terrain_bounds.
            _gxmin, _gzmin, _gxmax, _gzmax = terrain_bounds(
                ctx["entities"], getattr(schema, "locomotion", "walk"))
            ctx["ground_center_x"] = round((_gxmin + _gxmax) / 2.0, 3)
            ctx["ground_center_z"] = round((_gzmin + _gzmax) / 2.0, 3)
            ctx["ground_scale_x"]  = round(max(1.0, _gxmax - _gxmin), 3)
            ctx["ground_scale_z"]  = round(max(1.0, _gzmax - _gzmin), 3)
            scene_path = f"{pn}/Assets/Scenes/{scene_name}.unity"
            files[scene_path] = self._render("scene.unity.j2", ctx)
            files[f"{scene_path}.meta"] = _default_meta(
                _make_guid(f"{pn}/scene/{scene_name}")
            )
        files[f"{pn}/Assets/Scenes.meta"] = _default_meta(
            _make_guid(f"{pn}/Assets/Scenes"), folder=True
        )

        for script in schema.scripts:
            script_content, script_path = self._generate_script(
                schema, script, base_ctx, _stub_known_types)
            if "/_skip_" in script_path:
                continue  # level_generator handled separately as Editor script
            files[script_path]           = script_content
            files[f"{script_path}.meta"] = _meta_file(
                _make_guid(f"{pn}/Scripts/{script.name}")
            )
        files[f"{pn}/Assets/Scripts.meta"] = _default_meta(
            _make_guid(f"{pn}/Assets/Scripts"), folder=True
        )

        # ── Procedural Meshes ─────────────────────────────────────────────────
        if schema.procedural_meshes:
            for mesh in schema.procedural_meshes:
                obj_path = f"{pn}/Assets/Models/{mesh.name}.obj"
                files[obj_path]           = _generate_obj(mesh)
                files[f"{obj_path}.meta"] = _model_meta(
                    _make_guid(f"{pn}/Models/{mesh.name}")
                )
                mesh_cs  = f"{mesh.name}Mesh"
                cs_path  = f"{pn}/Assets/Scripts/{mesh_cs}.cs"
                files[cs_path]              = self._render("ProceduralMesh.cs.j2", {
                    **base_ctx, "mesh": mesh, "class_name": mesh_cs,
                })
                files[f"{cs_path}.meta"]    = _meta_file(
                    _make_guid(f"{pn}/Scripts/{mesh_cs}")
                )
            files[f"{pn}/Assets/Models.meta"] = _default_meta(
                _make_guid(f"{pn}/Assets/Models"), folder=True
            )

        # ── Palette Materials ─────────────────────────────────────────────────
        _mat_defs = [
            ("Primary",   palette.primary,    0.25),
            ("Secondary", palette.secondary,  0.15),
            ("Accent",    palette.accent,     0.40),
            ("Trim",      palette.trim,       0.10),
        ]
        for mat_label, mat_hex, mat_smooth in _mat_defs:
            mat_path = f"{pn}/Assets/Materials/{mat_label}.mat"
            files[mat_path]            = _generate_material(
                mat_label, mat_hex, schema.render_pipeline, mat_smooth
            )
            files[f"{mat_path}.meta"]  = _default_meta(mat_guids[mat_label.lower()])

        # ── schema.materials (LLM-authored MaterialSpec entries) ──────────────
        # No real PBR-texture generation backend exists (pbr_maps are author-
        # supplied paths, not generated data), so each MaterialSpec becomes a
        # real, valid solid-tint .mat asset — same _generate_material() as the
        # palette materials above, keyed off render_pipeline so the shader
        # always matches the active pipeline. Additive: doesn't touch the
        # palette materials or any existing wiring.
        for mspec in schema.materials:
            mcolor, msmooth = _material_hex_and_smoothness(mspec)
            mat_path = f"{pn}/Assets/Materials/{mspec.name}.mat"
            files[mat_path]           = _generate_material(
                mspec.name, mcolor, schema.render_pipeline, msmooth
            )
            files[f"{mat_path}.meta"] = _default_meta(
                _make_guid(f"{pn}/Materials/{mspec.id}")
            )

        # ── UI Toolkit HUD assets (UIDocument host for ui_manager, non-VR) ────
        # The scene template authors a HUD_Root → UIDocument that references these
        # by their fixed guids; emitting them makes the HUD render with no manual
        # wiring. VR drives its UI differently, so skip there (matches the Canvas).
        _stypes_for_ui = {s.type for s in schema.scripts}
        if "ui_manager" in _stypes_for_ui and schema.game_type != "VR":
            files[f"{pn}/Assets/UI.meta"] = _default_meta(
                _make_guid(f"{pn}/Assets/UI"), folder=True
            )
            files[f"{pn}/Assets/UI/HUD.uxml"]                   = _HUD_UXML
            files[f"{pn}/Assets/UI/HUD.uxml.meta"]              = _HUD_UXML_META
            files[f"{pn}/Assets/UI/HUDPanelSettings.asset"]     = _HUD_PANELSETTINGS
            files[f"{pn}/Assets/UI/HUDPanelSettings.asset.meta"] = _HUD_PANELSETTINGS_META

        # ── Default model pack emission ───────────────────────────────────────
        # Metas for every model/clip (binaries are copied by write()); the
        # ModelSwap runtime; and the run-once editor pass that forces Humanoid
        # rigs and builds the DefaultCharacter AnimatorController.
        if used_models:
            files[f"{pn}/Assets/Resources.meta"] = _default_meta(
                _make_guid(f"{pn}/Assets/Resources"), folder=True)
            for m in used_models.values():
                for item in [m] + list(m.clips.values()):
                    if item.rig == "sprite":
                        # PNG sprites: no authored .meta — Unity's default
                        # TextureImporter handles them, and the spawner loads
                        # Texture2D by path (no GUID references anywhere).
                        continue
                    guid = _make_guid(f"{pn}/{item.dest_rel}")
                    if item.rig == "humanoid":
                        meta_fn = _humanoid_model_meta(guid, item.scale)
                    elif item.rig == "generic":
                        meta_fn = _generic_model_meta(guid, item.scale)
                    else:
                        meta_fn = _model_meta(guid)
                    files[f"{pn}/Assets/Resources/{item.dest_rel}.meta"] = meta_fn
            ms_path = f"{pn}/Assets/Scripts/ModelSwap.cs"
            files[ms_path]           = self._render("ModelSwap.cs.j2", base_ctx)
            files[f"{ms_path}.meta"] = _meta_file(_make_guid(f"{pn}/Scripts/ModelSwap"))
            if any_humanoid:
                sdm_path = f"{pn}/Assets/Editor/SetupDefaultModels.cs"
                files[sdm_path]           = self._render("SetupDefaultModels.cs.j2", base_ctx)
                files[f"{sdm_path}.meta"] = _meta_file(_make_guid(f"{pn}/Editor/SetupDefaultModels"))
                files[f"{pn}/Assets/Editor.meta"] = _default_meta(
                    _make_guid(f"{pn}/Assets/Editor"), folder=True)

        # ── Bone-driver runtime ───────────────────────────────────────────────
        # VehicleBoneDriver spins/steers a rigged model's wheel/prop bones from the
        # vehicle's motion; IVehicleState is the contract the vehicle controller
        # exposes for the precise path. Emit when there are models to drive (ModelSwap
        # attaches the driver) OR a vehicle controller is composed (it implements
        # IVehicleState). The driver self-disables on humanoids (Animator owns those).
        _has_vehicle_ctrl = any(
            getattr(u, "kind", "") == "controller" and "vehicle" in getattr(u, "variant", "")
            for u in (composed_units or [])
        )
        if used_models or _has_vehicle_ctrl:
            iv_path = f"{pn}/Assets/Scripts/IVehicleState.cs"
            files[iv_path]           = self._render("IVehicleState.cs.j2", base_ctx)
            files[f"{iv_path}.meta"] = _meta_file(_make_guid(f"{pn}/Scripts/IVehicleState"))
            bd_path = f"{pn}/Assets/Scripts/VehicleBoneDriver.cs"
            files[bd_path]           = self._render("VehicleBoneDriver.cs.j2", base_ctx)
            files[f"{bd_path}.meta"] = _meta_file(_make_guid(f"{pn}/Scripts/VehicleBoneDriver"))

        # ── EntitySpawner (Tier-2 prompt-derived scene content) ───────────────
        # When the schema carries `entities`, emit a runtime spawner that builds
        # `count` primitives per group (N enemies, a ring of buildings, a crowd)
        # so the concept's content reaches the scene. The scene template hosts it
        # on an `EntitySpawner` GameObject (guarded by `entities`, gameplay scenes).
        if schema.entities:
            esp_path = f"{pn}/Assets/Scripts/EntitySpawner.cs"
            files[esp_path]           = self._render("EntitySpawner.cs.j2", base_ctx)
            files[f"{esp_path}.meta"] = _meta_file(_make_guid(f"{pn}/Scripts/EntitySpawner"))

        # ── Atom-driven entity behaviors ──────────────────────────────────────
        # For each entity that has an atom sequence, generate a MonoBehaviour
        # coroutine.  EntitySpawner resolves the class name at runtime via
        # Type.GetType(entity.behavior) and AddComponent — the name must match.
        entities_with_atoms = [e for e in schema.entities if getattr(e, "atoms", None)]
        if entities_with_atoms:
            from .atoms.backends.unity import unity_coroutine_body as _ucb
            from .atoms.behavior_map import behavior_loops

            for e in entities_with_atoms:
                cls       = entity_behavior_class_name(e.name)
                atom_dicts = [inv.model_dump() for inv in e.atoms]
                body      = _ucb(atom_dicts)
                eb_path   = f"{pn}/Assets/Scripts/{cls}.cs"
                files[eb_path]           = self._render("EntityBehavior.cs.j2", {
                    **base_ctx,
                    "entity":       e,
                    "class_name":   cls,
                    "atom_body":    body,
                    "loop_behavior": behavior_loops(e.atoms),
                })
                files[f"{eb_path}.meta"] = _meta_file(_make_guid(f"{pn}/Scripts/{cls}"))

        # ── Global atom sequencer (schema.atoms) ──────────────────────────────
        # Schema-level atoms (intro fade, music start, etc.) fire once on Start
        # via a GameSequencer MonoBehaviour on the GameManager object.
        if getattr(schema, "atoms", None):
            from .atoms.backends.unity import unity_coroutine_body as _ucb
            gs_path = f"{pn}/Assets/Scripts/GameSequencer.cs"
            files[gs_path]           = self._render("GameSequencer.cs.j2", {
                **base_ctx,
                "atom_body": _ucb([inv.model_dump() for inv in schema.atoms]),
            })
            files[f"{gs_path}.meta"] = _meta_file(_make_guid(f"{pn}/Scripts/GameSequencer"))

        # ── Level Generator editor script (ProBuilder) ────────────────────────
        _stypes = {s.type for s in schema.scripts}
        if "level_generator" in _stypes:
            for sc in schema.scripts:
                if sc.type != "level_generator":
                    continue
                ed_path = f"{pn}/Assets/Editor/{sc.name}.cs"
                files[ed_path]            = self._render("LevelGenerator.cs.j2", {
                    **base_ctx, "script": sc,
                })
                files[f"{ed_path}.meta"]  = _meta_file(
                    _make_guid(f"{pn}/Editor/{sc.name}")
                )

        # ── Kenney asset importer editor script ──────────────────────────────
        if kenney_assets:
            ki_path = f"{pn}/Assets/Editor/KenneyImporter.cs"
            files[ki_path]            = self._render("KenneyImporter.cs.j2", base_ctx)
            files[f"{ki_path}.meta"]  = _meta_file(
                _make_guid(f"{pn}/Editor/KenneyImporter")
            )

        # ── Editor folder (shared by SetupRenderPipeline + LevelGenerator + KenneyImporter) ─
        # (only create if not already handled by the URP/HDRP block below)
        has_editor_content = "level_generator" in _stypes or bool(kenney_assets)
        if has_editor_content and schema.render_pipeline == "builtin":
            files[f"{pn}/Assets/Editor.meta"] = _default_meta(
                _make_guid(f"{pn}/Assets/Editor"), folder=True
            )

        for folder in ("Prefabs", "Audio", "Animations", "Textures"):
            folder_path = f"{pn}/Assets/{folder}"
            files[f"{folder_path}/.gitkeep"]  = ""
            files[f"{folder_path}.meta"]       = _default_meta(
                _make_guid(f"{pn}/Assets/{folder}"), folder=True
            )

        # ── Editor setup script (URP / HDRP only) ────────────────────────────
        if schema.render_pipeline in ("URP", "HDRP"):
            editor_tpl = "editor/SetupRenderPipeline.cs.j2"
            script_path = f"{pn}/Assets/Editor/SetupRenderPipeline.cs"
            # Projects that don't qualify for the full atmosphere feature
            # (see wants_atmosphere()) still need SOMETHING better than the
            # Built-in-RP "Default-Skybox" material, which URP renders flat/
            # wrong and which feeds RenderSettings.ambientMode=Skybox with
            # near-black GI — the scene looks lightless even with a normal
            # directional light. When atmosphere IS shipping, its own runtime
            # bootstrap owns sky/ambient, so this fallback stays off.
            files[script_path] = self._render(editor_tpl, {
                **base_ctx, "has_atmosphere": wants_atmosphere(schema),
            })
            files[f"{script_path}.meta"] = _meta_file(
                _make_guid(f"{pn}/Editor/SetupRenderPipeline")
            )
            files[f"{pn}/Assets/Editor.meta"] = _default_meta(
                _make_guid(f"{pn}/Assets/Editor"), folder=True
            )
            if schema.render_pipeline == "URP":
                mp_path = f"{pn}/Assets/Editor/URPMaterialPostprocessor.cs"
                files[mp_path] = self._render(
                    "editor/URPMaterialPostprocessor.cs.j2", base_ctx)
                files[f"{mp_path}.meta"] = _meta_file(
                    _make_guid(f"{pn}/Editor/URPMaterialPostprocessor")
                )
            # Settings folder — created at runtime by the script; stub keeps git happy
            settings_path = f"{pn}/Assets/Settings"
            files[f"{settings_path}/.gitkeep"]  = ""
            files[f"{settings_path}.meta"] = _default_meta(
                _make_guid(f"{pn}/Assets/Settings"), folder=True
            )

        # ── Scene audio (Stable Audio 3 clip playback) ────────────────────────
        # The audio stage generates real clips into Resources/Audio/ but runs
        # AFTER write(), so SceneAudio discovers clips at runtime instead of
        # bake time: scene-matched ambient beds + a global music bed + a fuzzy
        # SceneAudio.Play("cue") API for gameplay scripts. Emitted whenever the
        # schema carries audio content; silent no-op when no clips exist yet.
        if "audio" in schema.features or schema.audio_tracks:
            sau_path = f"{pn}/Assets/Scripts/SceneAudio.cs"
            files[sau_path]           = self._render("scripts/SceneAudio.cs.j2", base_ctx)
            files[f"{sau_path}.meta"] = _meta_file(_make_guid(f"{pn}/Scripts/SceneAudio"))

        # ── Atmosphere (vendored URP-Atmosphere, MIT) ─────────────────────────
        # write() copies the vendored feature + profile binaries; here we emit
        # the two glue scripts: run-once renderer registration and the runtime
        # bootstrap that spawns the effect in every scene.
        if wants_atmosphere(schema):
            sa_path = f"{pn}/Assets/Editor/SetupAtmosphere.cs"
            files[sa_path]           = self._render("editor/SetupAtmosphere.cs.j2", base_ctx)
            files[f"{sa_path}.meta"] = _meta_file(_make_guid(f"{pn}/Editor/SetupAtmosphere"))
            files[f"{pn}/Assets/Editor.meta"] = _default_meta(
                _make_guid(f"{pn}/Assets/Editor"), folder=True)
            ab_path = f"{pn}/Assets/Scripts/AtmosphereBootstrap.cs"
            files[ab_path]           = self._render("scripts/AtmosphereBootstrap.cs.j2", base_ctx)
            files[f"{ab_path}.meta"] = _meta_file(_make_guid(f"{pn}/Scripts/AtmosphereBootstrap"))
            _log.info("[atmosphere] concept implies a sky — shipping vendored "
                      "URP-Atmosphere (MIT, sinnwrig)")

        files[f"{pn}/Assets.meta"] = _default_meta(_make_guid(f"{pn}/Assets"), folder=True)

        # ── Input Actions (New Input System) ──────────────────────────────────
        if "input_system" in schema.features:
            ia_name = f"{pn}InputActions"
            ia_path = f"{pn}/Assets/Settings/{ia_name}.inputactions"
            files[ia_path] = _generate_input_actions(schema, pn)
            files[f"{ia_path}.meta"] = _native_meta(
                _make_guid(f"{pn}/InputActions/{ia_name}"), main_object_id=13400000
            )

        # ── Prefabs ───────────────────────────────────────────────────────────
        prefab_ctx = {
            **base_ctx,
            "scripts_by_type": scripts_by_type,
            "script_guids":    script_guids,
        }
        for prefab_type, prefab_tpl in (("player", "Player.prefab.j2"),
                                         ("enemy",  "Enemy.prefab.j2")):
            if prefab_type not in scripts_by_type:
                continue
            prefab_name = scripts_by_type[prefab_type].name
            prefab_path = f"{pn}/Assets/Prefabs/{prefab_name}.prefab"
            files[prefab_path] = self._render(prefab_tpl, prefab_ctx)
            files[f"{prefab_path}.meta"] = _native_meta(
                _make_guid(f"{pn}/Prefabs/{prefab_name}"), main_object_id=1001000000
            )

        # ── _UserAssets/ skeleton ─────────────────────────────────────────────
        # Drops a category tree under Assets/_UserAssets/ where the tester
        # places their own meshes, audio, etc. See docs/FREE_ASSETS.md and
        # templates/unity/user_assets_skeleton/.
        files.update(self._user_assets_files(pn))

        # ── README.md ─────────────────────────────────────────────────────────
        # Data-driven per project: what THIS build contains (scene moods,
        # entity→model table, audio, atmosphere), not cookie-cutter advice.
        readme_ctx = {
            **base_ctx,
            "description": schema.description,
            "scripts":     schema.scripts,
            # Pass as sorted list of (id, version) tuples for stable output
            "packages":    sorted(manifest_deps.items()),
            "scene_specs_by_name": {sp.name: sp for sp in schema.scene_specs},
            "locomotion":      getattr(schema, "locomotion", "walk"),
            "has_atmosphere":  wants_atmosphere(schema),
            "has_scene_audio": "audio" in schema.features or bool(schema.audio_tracks),
        }
        files[f"{pn}/README.md"] = self._render("readme.md.j2", readme_ctx)

        # ── compile-map sidecar ───────────────────────────────────────────────
        # Maps each generated .cs file (project-relative, forward slashes) back to
        # the template that produced it, so `unity_cli.py verify` can attribute a
        # compile error to its source .j2 during the template-hardening loop.
        files[f"{pn}/.compile_map.json"] = json.dumps(
            self._compile_map(schema, bool(kenney_assets)), indent=2
        )

        return files

    # ── compile-map ────────────────────────────────────────────────────────────

    def _compile_map(self, schema: UnitySchema, has_kenney: bool) -> dict[str, str]:
        """file (relative to project root) -> source template path under templates/unity/."""
        m: dict[str, str] = {}
        for s in schema.scripts:
            if s.type == "level_generator":
                m[f"Assets/Editor/{s.name}.cs"] = "editor/LevelGenerator.cs.j2"
                continue
            tpl = _SCRIPT_TEMPLATES.get(s.type)
            if tpl:
                m[f"Assets/Scripts/{s.name}.cs"] = f"scripts/{tpl}"
        for mesh in schema.procedural_meshes:
            m[f"Assets/Scripts/{mesh.name}Mesh.cs"] = "scripts/ProceduralMesh.cs.j2"
        if schema.render_pipeline in ("URP", "HDRP"):
            m["Assets/Editor/SetupRenderPipeline.cs"] = "editor/SetupRenderPipeline.cs.j2"
        if schema.render_pipeline == "URP":
            m["Assets/Editor/URPMaterialPostprocessor.cs"] = "editor/URPMaterialPostprocessor.cs.j2"
        if has_kenney:
            m["Assets/Editor/KenneyImporter.cs"] = "editor/KenneyImporter.cs.j2"
        return m

    # ── _UserAssets skeleton ─────────────────────────────────────────────────

    def _user_assets_files(self, project_name: str) -> dict[str, str]:
        """
        Mirror templates/unity/user_assets_skeleton/ into
        Assets/_UserAssets/, generating a folder .meta for every directory
        and a DefaultImporter .meta for every README file. Returns an empty
        dict if the skeleton dir is missing (lets the generator stay
        backward-compatible).
        """
        skel = self.template_dir / "user_assets_skeleton"
        out: dict[str, str] = {}
        if not skel.exists():
            return out

        pn = project_name
        # Always emit the top-level _UserAssets folder meta.
        out[f"{pn}/Assets/_UserAssets.meta"] = _default_meta(
            _make_guid(f"{pn}/Assets/_UserAssets"), folder=True
        )

        for path in sorted(skel.rglob("*")):
            rel = path.relative_to(skel).as_posix()
            target_rel = f"_UserAssets/{rel}" if rel != "." else "_UserAssets"
            target_path = f"{pn}/Assets/{target_rel}"
            if path.is_dir():
                out[f"{target_path}.meta"] = _default_meta(
                    _make_guid(f"{pn}/Assets/{target_rel}"), folder=True
                )
            else:
                out[target_path] = path.read_text(encoding="utf-8")
                out[f"{target_path}.meta"] = _default_meta(
                    _make_guid(f"{pn}/Assets/{target_rel}")
                )
        return out

    def write(self, schema: UnitySchema, manifest_deps: dict[str, str],
              output_dir: str = "output/unity",
              lookups_dir: str = "lookups",
              kenney_source_dir: str | None = None,
              composed_units: list | None = None,
              model_source_dir: str | None = None,
              model_overrides: dict[str, str] | None = None,
              strict_assets: bool = False,
              asset_cfg: dict | None = None,
              sketchfab_cfg: dict | None = None,
              project_config: dict | None = None) -> tuple[Path, int]:
        """Write the project to disk; return (project_root, file_count)."""
        files = self.generate(schema, manifest_deps,
                              lookups_dir=lookups_dir,
                              kenney_source_dir=kenney_source_dir,
                              composed_units=composed_units,
                              model_source_dir=model_source_dir,
                              model_overrides=model_overrides,
                              strict_assets=strict_assets,
                              asset_cfg=asset_cfg,
                              sketchfab_cfg=sketchfab_cfg)
        root  = Path(output_dir)

        # Create all unique parent directories in one pass before writing files.
        for d in {(root / p).parent for p in files}:
            d.mkdir(parents=True, exist_ok=True)

        # Regenerating into a project the Unity Editor already has open (the
        # normal "live Unity" test loop) races the Editor's file watcher: a
        # plain write_text() truncates the file to 0 bytes before writing the
        # new content, and a reimport triggered inside that window sees a
        # truncated/torn file — the exact shape of "Parser Failure ... Expect
        # ':' between key and value" and "same file identifier" duplicate-fileID
        # errors. Write to a sibling temp file and os.replace() into place so
        # the Editor only ever observes the old complete file or the new
        # complete file, never a partial one.
        for rel_path, content in files.items():
            dest = root / rel_path
            tmp  = dest.with_name(dest.name + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, dest)

        # ── Syntax gate ──────────────────────────────────────────────────────
        # Every .cs file this call just wrote, through the same Roslyn parser
        # gate --compose already runs on composed micros (src/csharp_syntax_gate.py).
        # type="custom" scripts (CustomBehaviour.cs.j2) splice in LLM-authored
        # method bodies (script.extra_methods[].body) inside braces the template
        # already supplies — a stray brace in the LLM's own text is enough to
        # desync the file, and a character-counting check can't tell (see that
        # module's docstring). Raises CSharpSyntaxError with file/line/column
        # rather than shipping a script Unity reports CS1022 on days later.
        from .csharp_syntax_gate import enforce
        enforce([root / rel for rel in files if rel.endswith(".cs")],
                enabled=bool((project_config or {}).get("unity_pipeline", {})
                             .get("syntax_gate", True)),
                label="generated script(s)")

        self._sweep_stale_outputs(root, schema.project_name, set(files))

        # Binary model copies. Same _resolve_all_models call as generate() —
        # deterministic, so the .meta files emitted there and the binaries
        # copied here always line up (including rig downgrades that drop clips).
        used_models, _ = self._resolve_all_models(
            schema, lookups_dir, model_source_dir, model_overrides,
            composed_units, strict_assets, asset_cfg, sketchfab_cfg)
        n_models = 0
        for m in used_models.values():
            for item in [m] + list(m.clips.values()):
                # Primary copy: Assets/Resources/Models/... (for Resources.Load)
                dest = root / schema.project_name / "Assets" / "Resources" / item.dest_rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(Path(item.src).read_bytes())
                # Secondary copy: Assets/<Root>/<Kind>/<file> (user-visible clean
                # folder). item.dest_rel starts with "Models/Default/",
                # "Models/Kenney/" or "Sprites/Kenney/" — strip the two-part
                # prefix and mirror under the matching top folder.
                _rel_parts = Path(item.dest_rel).parts
                _kind_relative = Path(*_rel_parts[2:]) if len(_rel_parts) > 2 else Path(_rel_parts[-1])
                _top = _rel_parts[0] if _rel_parts[0] in ("Models", "Sprites") else "Models"
                dest2 = root / schema.project_name / "Assets" / _top / _kind_relative
                dest2.parent.mkdir(parents=True, exist_ok=True)
                dest2.write_bytes(Path(item.src).read_bytes())
                n_models += 1

        # Vendored URP-Atmosphere (MIT) — the render feature sources ship with
        # their original .metas so the profile→compute-shader GUID references
        # stay intact; the default profile lands under Resources for the
        # runtime bootstrap. copytree handles the binary files (png/asset).
        if wants_atmosphere(schema):
            import shutil
            vendor = Path(self.template_dir) / "vendor" / "URPAtmosphere"
            if vendor.is_dir():
                proj = root / schema.project_name
                shutil.copytree(vendor / "Atmosphere",
                                proj / "Assets" / "Atmosphere",
                                dirs_exist_ok=True)
                shutil.copy2(vendor / "LICENSE.md",
                             proj / "Assets" / "Atmosphere" / "LICENSE.md")
                res_dir = proj / "Assets" / "Resources" / "Atmosphere"
                res_dir.mkdir(parents=True, exist_ok=True)
                for f in sorted((vendor / "Profile").iterdir()):
                    shutil.copy2(f, res_dir / f.name)
                n_models += sum(1 for _ in (proj / "Assets" / "Atmosphere").rglob("*")
                                if _.is_file())
            else:
                _log.warning("[atmosphere] vendor dir missing: %s — skipping "
                             "atmosphere shipment", vendor)

        # Texture folders for catalog picks (Kenney AND Sketchfab). Both ship
        # PNGs from a Textures/ folder beside the model; Unity's importer only
        # finds it by walking UP the parent chain from the model's OWN copied
        # location, so the destination must mirror the SAME res_root
        # ("Models/Kenney" or "Models/Sketchfab") each model was itself copied
        # under — a texture dropped under the wrong subtree is never found,
        # which is exactly why every Sketchfab-sourced model rendered
        # untextured/black in Play mode before this fix. The Assets/Models/
        # mirror copy stays a single shared folder: the model's OWN secondary
        # copy strips its res_root segment (see the dual-copy loop above), so
        # Assets/Models/Textures/ is already the correct ancestor for every
        # source there. Unity generates the TextureImporter .metas itself on
        # first open.
        _tex_exts = {".png", ".jpg", ".jpeg", ".tga"}
        for tex_dir, res_root in texture_dirs_with_res_root_for(used_models):
            for tex in sorted(tex_dir.iterdir()):
                if tex.suffix.lower() not in _tex_exts or not tex.is_file():
                    continue
                for dest in (
                    root / schema.project_name / "Assets" / "Resources"
                         / res_root / "Textures" / tex.name,
                    root / schema.project_name / "Assets" / "Models"
                         / "Textures" / tex.name,
                ):
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(tex.read_bytes())
                n_models += 1

        return root / schema.project_name, len(files) + n_models

    # ── stale-output sweep ─────────────────────────────────────────────────────

    # Name of the per-project manifest of everything the LAST write() rendered.
    _MANIFEST_NAME = ".generated_files.json"

    def _sweep_stale_outputs(self, root: Path, project_name: str,
                             current: set[str]) -> list[str]:
        """Delete files an earlier write() rendered that this one no longer does.

        `write()` renders into the project directory in place, and Unity
        compiles EVERY .cs under Assets/ — not just the ones in the current
        build. So a regeneration that renames a script leaves the old class
        behind as a live compilation unit. Hit live on the Monaco monobike
        project: a 14:57 run produced MonobikeController/MonobikeAI, the 15:36
        regeneration of the same directory produced PlayerMonobikeController/
        RivalAIController instead, and Unity kept reporting CS0236 in two files
        that the current schema does not contain and nothing references.

        The sweep is driven by a manifest of the previous run's own output,
        never by scanning the directory: only a path THIS generator wrote
        before, and does not write now, is removed. A hand-added script, a
        Unity Library/ artifact and a copied model binary are all invisible to
        it. Paths are re-checked against `root` before unlinking so a
        hand-edited manifest cannot reach outside the project.

        Returns the project-relative paths removed (for logging and tests).
        """
        manifest = root / project_name / self._MANIFEST_NAME
        previous: list[str] = []
        if manifest.is_file():
            try:
                loaded = json.loads(manifest.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    previous = [p for p in loaded if isinstance(p, str)]
            except (json.JSONDecodeError, OSError) as exc:
                _log.warning("[sweep] unreadable manifest %s (%s) — no stale "
                             "files removed this run", manifest, exc)

        root_res = root.resolve()
        removed: list[str] = []
        for rel in previous:
            if rel in current:
                continue
            target = (root / rel)
            try:
                if not target.resolve().is_relative_to(root_res):
                    _log.warning("[sweep] manifest entry escapes the output "
                                 "root, ignored: %s", rel)
                    continue
            except OSError:
                continue
            if not target.is_file():
                continue
            target.unlink()
            removed.append(rel)
            # Unity writes a sidecar .meta for every asset it imports. Left
            # behind it is inert, but it also re-imports as a broken reference,
            # so it goes with the file it described — never on its own.
            meta = target.with_name(target.name + ".meta")
            if f"{rel}.meta" not in current and meta.is_file():
                meta.unlink()

        if removed:
            _log.info("[sweep] removed %d stale generated file(s): %s",
                      len(removed), ", ".join(sorted(removed)))

        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(sorted(current), indent=2), encoding="utf-8")
        return removed

    # ── private helpers ────────────────────────────────────────────────────────

    def _render(self, template_name: str, ctx: dict) -> str:
        return self._env.get_template(template_name).render(**ctx)

    def _generate_script(self, schema: UnitySchema, script: UnityScript,
                         base_ctx: dict,
                         known_types: "frozenset[str]" = frozenset()) -> tuple[str, str]:
        tpl_file = _SCRIPT_TEMPLATES.get(script.type)
        pn       = schema.project_name

        # level_generator lives in Assets/Editor — handled in generate(); emit nothing here
        if script.type == "level_generator":
            return "", f"{pn}/Assets/Scripts/_skip_{script.name}"

        path = f"{pn}/Assets/Scripts/{script.name}.cs"

        # Compute atom coroutine body for custom scripts whose description resolved
        # to an atom sequence (resolver fills script.atoms when extra_* are empty).
        atom_body = ""
        if script.type == "custom" and getattr(script, "atoms", None):
            from .atoms.backends.unity import unity_coroutine_body
            atom_body = unity_coroutine_body([inv.model_dump() for inv in script.atoms])

        # Emit [System.Serializable] stub classes for any field types the LLM
        # invented that aren't standard Unity/C# types.  Without this, references
        # like List<BountyContract> cause CS0246 on first open in Unity.
        stub_classes = ""
        if script.type == "custom" and script.extra_fields:
            stubs = _collect_custom_type_stubs(script.extra_fields, known_types)
            if stubs:
                stub_classes = "\n".join(stubs) + "\n"

        # Auto-declare a Rigidbody when the LLM-authored update/awake/fixed code
        # references `rb` but never declares it. Without this the raw fragment
        # (e.g. `rb.AddForce(...)`) is CS0103 and breaks the whole assembly.
        needs_rigidbody = False
        if script.type == "custom":
            blob = " ".join(filter(None, [
                getattr(script, "awake_extra", ""),
                getattr(script, "update_extra", ""),
                getattr(script, "fixed_update_extra", ""),
            ]))
            declared = {(getattr(f, "name", "") or "") for f in (script.extra_fields or [])}
            if re.search(r"\brb\b", blob) and "rb" not in declared:
                needs_rigidbody = True

        if tpl_file and (self.template_dir / "scripts" / tpl_file).exists():
            content = self._render(tpl_file, {**base_ctx, "script": script,
                                              "atom_body": atom_body,
                                              "stub_classes": stub_classes,
                                              "needs_rigidbody": needs_rigidbody})
        else:
            content = (
                f"// Auto-generated stub — {script.name}\n"
                f"// {script.description}\n"
                "using UnityEngine;\n\n"
                f"public class {script.name} : MonoBehaviour\n"
                "{\n"
                "    // TODO: implement\n"
                "}\n"
            )
        return content, path
