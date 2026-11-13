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
from typing import Any

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, StrictUndefined

from . import firing_provider
from .atom_bindings import (AtomBindings, BindingReport, contract_implementers_for,
                            derive_atom_bindings, stamp_atom_bindings)
from .unity_schema import (ProceduralMesh, UnitySchema, UnityScript,
                           is_embodied)
from .unity_asset_resolver import resolve_assets
from .unity_asset_catalog import load_catalog
from .unity_entity_asset_resolver import (resolve_entity_assets,
                                          texture_dirs_with_res_root_for)
from .unity_model_resolver import resolve_default_models, models_for_schema
from .unity_layout import terrain_bounds
from .unity_layout_archetype import pick_player_entity
from .unity_projectile_assets import (
    projectile_visual_resolver_entity,
    resolved_projectile_presentation,
)
from .unity_atmosphere import assert_aaa_contract
from .unity_greybox import (GREYBOX_SCRIPT_TYPES, apply_greybox_spatial_layout,
                             greybox_plus_ir_for_schema, greybox_ir_for_schema,
                             is_geometry_only_greybox, is_greybox_enabled,
                             is_greybox_plus_enabled, unity_greybox_asset_payload)
from .micro.code_fields import (CodeField, lint_extensible, _code_mask,
                                _code_brace_depth, _code_paren_depth)

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


def _frozen_asset_selection(model, catalog_assets: list[dict], *,
                            sketchfab_root: str | None = None) -> dict | None:
    """Return an exact catalog selection for a resolved marketplace model.

    The generated Resource path is an output name, not enough information to
    replay the input selection.  Match the resolved source bytes back to the
    frozen catalog entry and carry the catalog coordinates plus a content hash
    into the schema sidecar.  Non-catalog/default models intentionally return
    ``None``; they have their own deterministic resolver authority.
    """
    if getattr(model, "source", None) != "sketchfab":
        return None
    try:
        source_path = Path(model.src).resolve()
    except OSError:
        return None
    for entry in catalog_assets:
        if not isinstance(entry, dict) or entry.get("source") != "sketchfab":
            continue
        relpath = str(entry.get("relpath") or entry.get("filename") or "")
        root_text = str(sketchfab_root or entry.get("root") or "").strip()
        if not relpath or not root_text:
            continue
        root = Path(root_text)
        try:
            candidate = (root / relpath).resolve()
        except OSError:
            continue
        if candidate != source_path or not candidate.is_file():
            continue
        digest = hashlib.sha256()
        with candidate.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        selection: dict[str, Any] = {
            "filename": str(entry.get("filename") or candidate.name),
            "relpath": relpath.replace("\\", "/"),
            "source": "sketchfab",
            "file_sha256": digest.hexdigest(),
        }
        for key in ("category", "license", "author", "author_username",
                    "attribution", "sketchfab_uid", "sketchfab_url"):
            value = entry.get(key)
            if value not in (None, ""):
                selection[key] = value
        return selection
    return None


def _apply_model_overrides(resolved: dict, overrides: dict[str, str] | None) -> dict:
    """Inject Blender-built FBX paths into the resolved model catalog.

    For kinds already in the catalog: swaps only the `src` path, keeping the
    catalog's dest_rel / resource_path / rig / scale unchanged.
    For kinds NOT yet in the catalog (no static fallback file existed): bootstraps
    a complete ResolvedModel so the copy + .meta + ModelSwap chain still fires.
    Skips (loudly) any entry whose FBX file doesn't exist on disk yet, degrading
    that kind back to whatever the catalog tiers already resolved.
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
            _log.warning("[model-overrides] %s FBX missing on disk (%s) — "
                         "falling back to the catalog tiers for this kind",
                         kind, src or "<empty>")
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


def wants_atmosphere(schema, *, schema_only: bool = False) -> bool:
    """Deterministic gate: 3D URP project whose concept implies a real sky.

    A persisted compose selection is authoritative. Before selection (and for
    legacy non-composed projects), scans schema fields and Step-2 inputs
    (schema.meta["user_prompt"]/["concept"]) — the Step-2 LLM elaboration
    routinely compresses a rich brief down to a terse schema.description
    that drops style/atmosphere words the user actually wrote (e.g. "sky"),
    which silently starved this gate even when the user's own prompt asked
    for one. The raw prompt is the ground truth for what the user wants.
    """
    meta = getattr(schema, "meta", None) or {}
    if "compose_selection" in meta:
        from .unity_compose_selection import ComposeSelection
        return ComposeSelection.model_validate(meta["compose_selection"]).atmosphere
    if schema.render_pipeline != "URP" or schema.game_type not in ("3D", "VR"):
        return False
    if getattr(schema, "locomotion", "walk") == "fly":
        return True
    meta = getattr(schema, "meta", None) or {}
    text = " ".join([
        schema.description or "",
        " ".join(schema.scenes or []),
        " ".join(sp.description or "" for sp in getattr(schema, "scene_specs", []) or []),
        "" if schema_only else str(meta.get("user_prompt") or ""),
        "" if schema_only else str(meta.get("concept") or ""),
    ]).lower()
    words = set(re.split(r"[^a-z]+", text))
    return bool(words & _ATMOSPHERE_WORDS)


def _environment_ctx(time_of_day: str, fog, fog_color, sun_intensity,
                     lighting: dict, bg: dict,
                     render_pipeline: str = "URP") -> dict:
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
    # EnvironmentSpec is pipeline-neutral. HDRP directional lights are
    # physical Lux, so normalize the authored day baseline at this backend
    # boundary rather than allowing Unity to reinterpret a unitless value.
    if render_pipeline == "HDRP":
        try:
            raw_intensity = float(intensity)
        except (TypeError, ValueError):
            raw_intensity = 1.2
        intensity = (raw_intensity if raw_intensity > 10.0
                     else (raw_intensity / 1.2) * 100000.0)
        intensity = round(max(1000.0, min(150000.0, intensity)))
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
# This digest is a COMPATIBILITY CONTRACT, not an identity hash: every project
# already generated carries these GUIDs, so it deliberately stays md5 and does
# NOT move onto src/ident (see src/ident/frozen.py). Any change to how
# _make_guid/_make_uuid derive their output must bump GUID_SCHEME, and
# tests/test_ident.py pins the scheme-1 outputs.
GUID_SCHEME = 1


def _make_guid(seed: str) -> str:
    return hashlib.md5(seed.encode("utf-8")).hexdigest()


def _make_uuid(pn: str, seed: str) -> str:
    """Deterministic UUID-shaped string (version 4, variant 10xx) from project + seed."""
    h = hashlib.md5(f"{pn}/{seed}".encode("utf-8")).hexdigest()
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:32]}"


_PASCAL_WORD_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _to_display_title(name: str, fallback: str = "Untitled") -> str:
    """Best-effort human title from a PascalCase project_name.

    Mirror of micro_pipeline.to_display_title — kept local to avoid a
    circular import, same convention as to_pascal above. `project_name` is
    an IDENTIFIER (folder/class-safe: no spaces, no apostrophe); a template
    that shows it to the PLAYER shows the identifier instead of a title.
    Confirmed live — Grandma's Antique Shop's own main menu read
    "GrandmaSAntiqueShopIsARoguelikeDungeon" as its title text. This only
    recovers word boundaries, not the original punctuation ("Grandma's"
    comes back as two words, "Grandma" and "S") — still far more readable
    than the identifier it is built from.
    """
    if not name:
        return fallback
    words = _PASCAL_WORD_BOUNDARY_RE.sub(" ", name).split()
    return " ".join(words) if words else fallback


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

# Deprecated compatibility map for historical non-compose schema replay.
# New unity-create runs default to Compose Micros; do not add gameplay systems
# here. MechanicRegistry + MicroComposer are the active assembly pathway.
_LEGACY_SCRIPT_TEMPLATES = {
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
    # Greybox generators are Editor scripts — handled separately in generate().
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


# A bare C# identifier — what a `public class <name>` stub needs its name to be.
_CS_IDENTIFIER_RE = re.compile(r"^[A-Za-z_]\w*$")


def _extract_base_type(type_str: str) -> str:
    """Strip generics/arrays to return the innermost concrete type name."""
    # A MULTI-dimensional suffix has commas in it — `GridCell[,]`. rstrip("[]")
    # alone stops at the comma and returns "GridCell[," , a name that matches
    # nothing and stubs nothing.
    t = re.sub(r"(?:\s*\[[,\s]*\])+\s*$", "", type_str.strip()).strip()
    t = t.rstrip("[]").strip()
    m = _DICT_RE.match(t)
    if m:
        return _extract_base_type(m.group(2))   # value type is more often custom
    m = _GENERIC_WRAPPER_RE.match(t)
    if m:
        return _extract_base_type(m.group(1))
    return t.split("<")[0].strip()


# `private GridCell[,] RotateGrid(GridPiece piece)` — the declared return type
# and each parameter type. A type the author only ever names in a SIGNATURE was
# invisible to the stub collector, which read `extra_fields` alone: hit live in
# BlockWell's GridLogic, where `GridPiece` (a field type) got its stub and
# `GridCell` (a return type) did not, so the script shipped one CS0246.
_SIGNATURE_RE = re.compile(
    r"^\s*(?:(?:public|private|protected|internal|static|virtual|override|async|sealed|new)\s+)*"
    r"(?P<ret>[A-Za-z_][\w.]*(?:\s*<[^>]*>)?(?:\s*\[[,\s]*\])*)"
    r"\s+[A-Za-z_]\w*\s*\((?P<args>[^)]*)\)"
)


class _TypeCarrier:
    """Minimal `.type` holder so a signature-derived type can go
    through the same filter `_stub_type_names` applies to fields."""
    __slots__ = ("type",)

    def __init__(self, ty: str) -> None:
        self.type = ty


# Parameter modifiers that sit BEFORE the type and are not part of it.
# `ref Foo bar` split naively yields the type "ref Foo", which is not a type.
_PARAM_MODIFIERS = frozenset({"ref", "out", "in", "params", "this", "readonly", "scoped"})


def _signature_type_names(extra_methods: list) -> list[str]:
    """Every type named in a method signature — the return type and each
    parameter type, in source order.

    A parameter is `<modifiers> <type> <name> [= <default>]`, and only the
    middle part is a type. Both ends were being read as one before: an OPTIONAL
    parameter's default made `float lockoutSeconds = 1.5f` yield the "type"
    `float lockoutSeconds =`, which `_collect_custom_type_stubs` then emitted as
    `public class float lockoutSeconds =` — five CS syntax errors from one
    authored default value. Hit live on Chromabreak's Interactable.cs.
    """
    out: list[str] = []
    for m in (extra_methods or []):
        sig = str(getattr(m, "signature", "") or "")
        hit = _SIGNATURE_RE.match(sig)
        if not hit:
            continue
        out.append(hit.group("ret"))
        for arg in (hit.group("args") or "").split(","):
            # Everything from the '=' is the default VALUE, not the type.
            parts = [p for p in arg.split("=", 1)[0].strip().split()
                     if p not in _PARAM_MODIFIERS]
            if len(parts) >= 2:
                out.append(" ".join(parts[:-1]))
    return [t for t in out if t]


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
            # A stub is emitted as `public class {base}`, so `base` MUST be a
            # bare C# identifier. Anything else — a whole declaration, a
            # leftover modifier, punctuation — is not a type, and stubbing it
            # writes a syntax error into the project instead of the CS0246 fix
            # it was reaching for. The producers are guarded individually
            # (see _signature_type_names); this is the one place every one of
            # them funnels through, so it is also guarded here.
            or not _CS_IDENTIFIER_RE.match(base)
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


# Members every MonoBehaviour/Component/Object instance legitimately has — never
# grounds to strip. Shared by the undeclared self-call and condition-read
# passes so a member allowed on one path is allowed on the other.
_MB_COMMON_MEMBERS = frozenset({
    "transform", "gameObject", "enabled", "name", "tag",
    "isActiveAndEnabled", "hideFlags",
    "CompareTag", "GetComponent", "GetComponents", "GetComponentInChildren",
    "GetComponentsInChildren", "GetComponentInParent", "TryGetComponent",
    "SendMessage", "BroadcastMessage", "Invoke", "InvokeRepeating", "CancelInvoke",
    "StartCoroutine", "StopCoroutine", "StopAllCoroutines",
    "ToString", "GetType", "Equals", "GetHashCode",
})


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


# ── Undeclared self-reference (bare field/method the script never declared) ──
#
# Distinct from DOTTED cross-reference member access on another type reached
# via Get<T>() (validated by the S2 selected-source Roslyn contract) — this
# catches a script hallucinating members on ITSELF, with no
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
_METHOD_PARAM_LIST_RE = re.compile(r"\(([^)]*)\)")


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


# CS0019 — `Vector3 * Vector3` has no `operator*` in UnityEngine (only
# `Vector3 * float`/`float * Vector3` scalar scaling exist); component-wise
# multiplication needs `Vector3.Scale(a, b)`. Confirmed live, DropZone's
# WindProfileManager.cs(13): `_windForce * -direction` — `_windForce` is the
# script's own `[SerializeField] private Vector3 _windForce`, `direction` a
# local `var direction = Vector3.up;`.
#
# A mechanical, meaning-preserving rewrite exists here — `Vector3.Scale(a, b)`
# IS what `a * b` would mean if C# allowed it, componentwise — unlike
# `CharacterController.velocity`'s read-only misuse (code_fields.py), which
# has no safe replacement expression, so that one is neutralized and this one
# is repaired instead.
#
# Scoped to two BARE identifiers either side of `*` (one optionally negated):
# an arbitrary expression on either side would need a real parser to prove is
# a Vector3, and guessing wrong there would rewrite something that was never
# broken. Both operands must be independently provable as Vector3 — the
# script's own `extra_fields`, or a local `var x = Vector3.Y;`/`Vector3 x =
# Vector3.Y;` declaration in the SAME fragment — or this leaves the line
# alone for the syntax gate to report, same as every other case here that
# cannot ground a repair.
_VECTOR_MULTIPLY_RE = re.compile(
    r"(?<![\w.])(?P<lhs>[A-Za-z_]\w*)[ \t]*\*[ \t]*"
    r"(?P<neg>-?)(?P<rhs>[A-Za-z_]\w*)(?![\w.(])")
_LOCAL_VECTOR3_DECL_RE = re.compile(
    r"\b(?:var|Vector3)\s+([A-Za-z_]\w*)\s*=\s*Vector3\.")


def _sanitize_vector_scale_misuse(script: UnityScript) -> None:
    """Rewrite `<Vector3> * <Vector3>` to `Vector3.Scale(...)` — see the
    module comment above."""
    field_types = {f.name: (f.type or "").strip().lower()
                   for f in (script.extra_fields or [])}

    def _fix(body: str) -> str:
        if not body or "*" not in body:
            return body
        local_vec3 = {m.group(1) for m in _LOCAL_VECTOR3_DECL_RE.finditer(body)}
        mask = _code_mask(body)

        def is_vec3(name: str) -> bool:
            return field_types.get(name) == "vector3" or name in local_vec3

        def repl(m: "re.Match[str]") -> str:
            if not mask[m.start()]:
                return m.group(0)
            lhs, neg, rhs = m.group("lhs"), m.group("neg"), m.group("rhs")
            if not (is_vec3(lhs) and is_vec3(rhs)):
                return m.group(0)
            return f"Vector3.Scale({lhs}, {neg}{rhs})"

        return _VECTOR_MULTIPLY_RE.sub(repl, body)

    script.awake_extra        = _fix(script.awake_extra)
    script.update_extra       = _fix(script.update_extra)
    script.fixed_update_extra = _fix(script.fixed_update_extra)
    for m in script.extra_methods:
        m.body = _fix(m.body)


def _reject_unbalanced_custom_bodies(script: UnityScript) -> None:
    """Final safety net for custom (schema.scripts, type="custom") bodies —
    the equivalent of code_fields.py's lint_extensible reject-on-surplus-
    brace check (see its module comment: dropping a surplus `}` merges two
    blocks that were meant to be separate, so REJECT rather than guess-repair),
    but re-run AFTER every _sanitize_* pass above instead of only once before
    them.

    lint_extensible already runs first in the per-script loop this is called
    from (`if s.type == "custom": lint_extensible(s, ...)`), so an LLM body
    that was ALREADY unbalanced gets rejected there. Confirmed live
    (2026-08-28, ObjectThrow.cs, CS1022): that first check passed — the LLM's
    own text was balanced — but the body ended up with a surplus `}` anyway
    after the ~10 regex-rewrite sanitize passes between here and there, none
    of which re-verify brace balance afterward. A regex substitution that
    trims/drops a matched span can change brace count as a side effect even
    when that was never its intent. Clearing (not guessing) here is the
    identical trade-off lint_extensible already made for the same reason —
    a generic/incomplete custom script still builds; a poisoned one is
    dropped and the syntax gate never sees it, instead of failing the whole
    project days later in Unity.
    """
    def _starts_with_orphaned_else(body: str) -> bool:
        """Whether a prior line-removal pass has left an `else` at body root.

        Several defensive custom-script passes reject whole source lines.  If
        one rejects an `if (...)` head while the LLM wrote the matching `else`
        on the following line, the braces can remain perfectly balanced even
        though the rendered method is invalid C#.  A leading `else` has no
        recoverable condition to attach to, so this is a structural rejection,
        not an opportunity to invent one.  Comments and string literals are
        ignored through the shared code mask.
        """
        mask = _code_mask(body)
        for token in re.finditer(r"[A-Za-z_]\w*", body):
            if mask[token.start()]:
                return token.group(0) == "else"
        return False

    def _check(body: str, where: str) -> str:
        # Silent like every sibling _sanitize_* pass above (this file has no
        # console/message-return channel of its own — unlike code_fields.py,
        # whose reject-on-surplus-brace message this mirrors). Discoverable
        # via this function's docstring, not a runtime print.
        if not body:
            return body
        if _starts_with_orphaned_else(body):
            _log.warning("[custom-script] rejected %s: body begins with an "
                         "orphaned else after sanitization", where)
            return ""
        if _code_paren_depth(body) != 0 or _code_brace_depth(body) != 0:
            return ""
        return body

    script.awake_extra        = _check(script.awake_extra,        "awake_extra")
    script.update_extra       = _check(script.update_extra,       "update_extra")
    script.fixed_update_extra = _check(script.fixed_update_extra, "fixed_update_extra")
    for m in script.extra_methods:
        # Body cleared, signature kept (not dropped whole): another body in
        # this same script (awake_extra, update_extra, a sibling method) may
        # already call this method by name — removing the definition entirely
        # would trade one compile error for a new CS0103 elsewhere. An empty
        # body is always valid C#, so this is the safe end-state either way.
        m.body = _check(m.body, f"extra_methods[{m.signature}]")


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


def _needs_player_rigidbody(script: UnityScript) -> bool:
    """Render a known player binding, respecting authored declarations."""
    if script.type != "custom" or any(f.name == "playerRigidbody" for f in script.extra_fields):
        return False
    bodies = [(script.awake_extra, ""), (script.update_extra, ""),
              (script.fixed_update_extra, "")]
    bodies += [(m.body, m.signature) for m in script.extra_methods]
    for body, signature in bodies:
        mask = _code_mask(body)
        code = "".join(c if mask[i] else " " for i, c in enumerate(body))
        declared = {m.group(1) for m in _LOCAL_DECL_RE.finditer(code)}
        declared.update(m.group(2) for m in _LOCAL_DECL_NO_INIT_RE.finditer(code))
        declared.update(_method_param_names(signature))
        if "playerRigidbody" not in declared and re.search(
                r"(?<![\w.])(?:this\.)?playerRigidbody\b", code):
            return True
    return False


def _auto_declare_undeclared_scalars(
    script: UnityScript,
    sibling_defaults: dict[str, str] | None = None,
) -> None:
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
        declared.add(tok)

    # A read-only numeric field can be just as undeclared as an assignment
    # target. The generated stamina script hit this shape: the body multiplied
    # by `_staminaDrainRate`, while Step 3 had omitted that field entirely.
    # Restrict the repair to private-looking identifiers used beside numeric
    # operators, so component/member accesses and method calls are never
    # guessed into floats. When another authored custom script declares the
    # same field, its explicit default is deterministic ground truth for the
    # repaired declaration; otherwise C#'s zero default is preserved.
    numeric_read = re.compile(r"(?<![.\w])(_[A-Za-z_]\w*)\b")
    numeric_operator = re.compile(r"(?:[+\-*/%<>]=?|==|!=)\s*$")
    for m in numeric_read.finditer(combined):
        tok = m.group(1)
        if tok in declared or tok in _CS_KEYWORDS or tok in _STATIC_CLASS_ALLOW:
            continue
        after = combined[m.end():]
        if re.match(r"\s*(?:[.(]|\[)", after):
            continue
        before = combined[max(0, m.start() - 12):m.start()]
        right = after[:12]
        if not (numeric_operator.search(before) or numeric_operator.match(right)):
            continue
        declared.add(tok)
        script.extra_fields.append(CodeField(
            name=tok,
            type="float",
            default=(sibling_defaults or {}).get(tok),
        ))


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


def _is_climbing_contract(schema: UnitySchema, composed_units: list | None = None) -> bool:
    """True when the deterministic project contract is wall climbing.

    A persisted compose selection follows the chosen climbing controller.
    Legacy projects derive it from brief/schema content and entity vocabulary;
    this helper never asks the Pipeline LLM to invent a controller mode.
    """
    meta = getattr(schema, "meta", None) or {}
    if "compose_selection" in meta:
        from .unity_compose_selection import ComposeSelection
        return ComposeSelection.model_validate(meta["compose_selection"]).climbing_contract
    if composed_units is not None:
        return any(unit.kind == "controller" and unit.variant == "climbing"
                   for unit in composed_units)
    meta = getattr(schema, "meta", None) or {}
    text = " ".join([
        str(getattr(schema, "description", "") or ""),
        str(meta.get("user_prompt") or ""),
        str(meta.get("concept") or ""),
        " ".join(str(getattr(e, "name", "") or "") for e in schema.entities),
        " ".join(str(getattr(e, "tag", "") or "") for e in schema.entities),
    ]).lower()
    return ("climb" in text or "climber" in text) and any(
        word in text for word in ("hold", "ledge", "rock face", "vertical wall", "summit"))


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


def _humanoid_model_meta(guid: str, scale: float = 1.0,
                         readable: bool = False) -> str:
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
        f"    isReadable: {1 if readable else 0}\n"
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


def _model_meta(guid: str, readable: bool = False) -> str:
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
        f"    isReadable: {1 if readable else 0}\n"
        "  platforms: []\n"
        "  userData: \n"
        "  assetBundleName: \n"
        "  assetBundleVariant: \n"
    )


def _generic_model_meta(guid: str, scale: float = 1.0,
                        readable: bool = False) -> str:
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
        f"    isReadable: {1 if readable else 0}\n"
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


# ── Post-processing (URP Volume) ──────────────────────────────────────────────
# GUIDs sourced from real installed Unity Editors, read directly from
# Editor/Data/Resources/PackageManager/BuiltInPackages/*/*.cs.meta — not
# guessed. Cross-checked identical (guid AND every VolumeParameter field name
# below) between com.unity.render-pipelines.{universal,core} 14.0.11 (Unity
# 2022.3.30f1) and 17.0.4 (Unity 6000.0.66f2 — this repo's own default
# `_VERSION_MAP` target). A package's foundational MonoScript guids are the
# class's permanent identity; Unity does not regenerate a .meta's guid across
# version bumps (every existing user project's serialized references would
# break), which is why they hold across this jump. A future URP major that
# did change one fails soft regardless (Unity shows "script cannot be
# loaded" for that one override, not a crash or a broken Volume).
_VOLUME_GUID             = "172515602e62fb746b5d573b38a5fe58"
_VOLUME_PROFILE_GUID     = "d7fd9488000d3734a9e00ee676215985"
_BLOOM_GUID              = "0b2db86121404754db890f4c8dfe81b2"
_COLOR_ADJUSTMENTS_GUID  = "66f335fb1ffd8684294ad653bf1c7564"
_TONEMAPPING_GUID        = "97c23e3b12dc18c42a140437e53d3951"
_VIGNETTE_GUID           = "899c54efeace73346a0a16faa3afe726"
_UNIVERSAL_CAM_DATA_GUID = "a79441f348de89743a2939f4d699eac1"


def _generate_postprocess_profile() -> str:
    """One conservative, fixed URP Volume Profile: a Neutral tonemap, a small
    contrast lift + saturation trim, a subtle vignette, and a low bloom.

    Fixed constants for v1, not schema/palette-driven — the point is a small
    universal grade that keeps a scene's baked palette materials, runtime
    primitive fallbacks (see EntitySpawner's fallback branch), and untouched
    Kenney/FBX asset textures from reading as an uncalibrated clash of
    saturated colors sitting side by side with nothing tying them together,
    not a stylistic statement. Neutral (not ACES) for the tonemap: minimal
    hue/saturation impact, so it can't fight the "never black" ambient floor
    this repo already enforces (src/micro/lighting.py).

    Field names/types read directly from the real URP override sources
    (Bloom.cs, ColorAdjustments.cs, Tonemapping.cs, Vignette.cs) — a
    VolumeParameter<T> always serializes as {m_OverrideState, m_Value}; an
    omitted field just keeps that class's own default, exactly like adding a
    new field to an existing component never breaks old scenes.
    """
    return (
        "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
        "--- !u!114 &11400000\n"
        "MonoBehaviour:\n"
        "  m_ObjectHideFlags: 0\n"
        "  m_CorrespondingSourceObject: {fileID: 0}\n"
        "  m_PrefabInstance: {fileID: 0}\n"
        "  m_PrefabAsset: {fileID: 0}\n"
        "  m_GameObject: {fileID: 0}\n"
        "  m_Enabled: 1\n"
        "  m_EditorHideFlags: 0\n"
        f"  m_Script: {{fileID: 11500000, guid: {_VOLUME_PROFILE_GUID}, type: 3}}\n"
        "  m_Name: PostProcessProfile\n"
        "  m_EditorClassIdentifier: \n"
        "  components:\n"
        "  - {fileID: 11400001}\n"
        "  - {fileID: 11400002}\n"
        "  - {fileID: 11400003}\n"
        "  - {fileID: 11400004}\n"
        "--- !u!114 &11400001\n"
        "MonoBehaviour:\n"
        "  m_ObjectHideFlags: 3\n"
        "  m_CorrespondingSourceObject: {fileID: 0}\n"
        "  m_PrefabInstance: {fileID: 0}\n"
        "  m_PrefabAsset: {fileID: 0}\n"
        "  m_GameObject: {fileID: 0}\n"
        "  m_Enabled: 1\n"
        "  m_EditorHideFlags: 0\n"
        f"  m_Script: {{fileID: 11500000, guid: {_COLOR_ADJUSTMENTS_GUID}, type: 3}}\n"
        "  m_Name: ColorAdjustments\n"
        "  m_EditorClassIdentifier: \n"
        "  active: 1\n"
        "  postExposure:\n"
        "    m_OverrideState: 0\n"
        "    m_Value: 0\n"
        "  contrast:\n"
        "    m_OverrideState: 1\n"
        "    m_Value: 8\n"
        "  colorFilter:\n"
        "    m_OverrideState: 0\n"
        "    m_Value: {r: 1, g: 1, b: 1, a: 1}\n"
        "  hueShift:\n"
        "    m_OverrideState: 0\n"
        "    m_Value: 0\n"
        "  saturation:\n"
        "    m_OverrideState: 1\n"
        "    m_Value: -6\n"
        "--- !u!114 &11400002\n"
        "MonoBehaviour:\n"
        "  m_ObjectHideFlags: 3\n"
        "  m_CorrespondingSourceObject: {fileID: 0}\n"
        "  m_PrefabInstance: {fileID: 0}\n"
        "  m_PrefabAsset: {fileID: 0}\n"
        "  m_GameObject: {fileID: 0}\n"
        "  m_Enabled: 1\n"
        "  m_EditorHideFlags: 0\n"
        f"  m_Script: {{fileID: 11500000, guid: {_TONEMAPPING_GUID}, type: 3}}\n"
        "  m_Name: Tonemapping\n"
        "  m_EditorClassIdentifier: \n"
        "  active: 1\n"
        "  mode:\n"
        "    m_OverrideState: 1\n"
        "    m_Value: 1\n"
        "--- !u!114 &11400003\n"
        "MonoBehaviour:\n"
        "  m_ObjectHideFlags: 3\n"
        "  m_CorrespondingSourceObject: {fileID: 0}\n"
        "  m_PrefabInstance: {fileID: 0}\n"
        "  m_PrefabAsset: {fileID: 0}\n"
        "  m_GameObject: {fileID: 0}\n"
        "  m_Enabled: 1\n"
        "  m_EditorHideFlags: 0\n"
        f"  m_Script: {{fileID: 11500000, guid: {_VIGNETTE_GUID}, type: 3}}\n"
        "  m_Name: Vignette\n"
        "  m_EditorClassIdentifier: \n"
        "  active: 1\n"
        "  color:\n"
        "    m_OverrideState: 0\n"
        "    m_Value: {r: 0, g: 0, b: 0, a: 1}\n"
        "  center:\n"
        "    m_OverrideState: 0\n"
        "    m_Value: {x: 0.5, y: 0.5}\n"
        "  intensity:\n"
        "    m_OverrideState: 1\n"
        "    m_Value: 0.2\n"
        "  smoothness:\n"
        "    m_OverrideState: 0\n"
        "    m_Value: 0.2\n"
        "  rounded:\n"
        "    m_OverrideState: 0\n"
        "    m_Value: 0\n"
        "--- !u!114 &11400004\n"
        "MonoBehaviour:\n"
        "  m_ObjectHideFlags: 3\n"
        "  m_CorrespondingSourceObject: {fileID: 0}\n"
        "  m_PrefabInstance: {fileID: 0}\n"
        "  m_PrefabAsset: {fileID: 0}\n"
        "  m_GameObject: {fileID: 0}\n"
        "  m_Enabled: 1\n"
        "  m_EditorHideFlags: 0\n"
        f"  m_Script: {{fileID: 11500000, guid: {_BLOOM_GUID}, type: 3}}\n"
        "  m_Name: Bloom\n"
        "  m_EditorClassIdentifier: \n"
        "  active: 1\n"
        "  threshold:\n"
        "    m_OverrideState: 0\n"
        "    m_Value: 0.9\n"
        "  intensity:\n"
        "    m_OverrideState: 1\n"
        "    m_Value: 0.15\n"
        "  scatter:\n"
        "    m_OverrideState: 0\n"
        "    m_Value: 0.7\n"
        "  clamp:\n"
        "    m_OverrideState: 0\n"
        "    m_Value: 65472\n"
        "  tint:\n"
        "    m_OverrideState: 0\n"
        "    m_Value: {r: 1, g: 1, b: 1, a: 1}\n"
        "  highQualityFiltering:\n"
        "    m_OverrideState: 0\n"
        "    m_Value: 0\n"
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
    # `Platform` is the closed literal pc|mobile|console|webgl|all — never a
    # store name. This read against ("Android", "iOS") could not fire on any
    # VALIDATED schema, so the touch bindings below were unreachable in every
    # real run; the one test covering it assigned `platform = "Android"` after
    # construction, which skips the field validator (no validate_assignment).
    mobile = schema.platform == "mobile"

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


def normalize_custom_script(s: UnityScript, scripts: "list[UnityScript]") -> None:
    """The deterministic pre-render passes every `type: custom` script gets.

    Reached only through `prepare_scripts`, which Step 6
    (`UnityProjectGenerator.generate`) and the custom-script compiler check
    (`unity_authored_check`) both call, so a proposal is judged as exactly the
    source that will ship. Mutates `s` in place.
    """
    # hoist_field_inits: CustomBehaviour.cs.j2 renders awake_extra
    # (and emits an Awake() whenever it is non-empty), so an
    # instance-referencing initializer the model wrote one line too
    # early — `private Rigidbody rb = GetComponent<Rigidbody>();`,
    # CS0236, live on a Monaco monobike run — is moved into Awake()
    # rather than dropped to a null the scene never assigns.
    lint_extensible(s, reserved_fields=set(),
                    template_usings={"UnityEngine"},
                    hoist_field_inits=True)
    # R0: collaborator erasers (invented namespace/stub/constructor/
    # find filters, wrong-arity GetComponent strips, undeclared
    # self-call/condition/foreach filters) are retired. Their
    # defects are now reported, with the authored source intact, by
    # the selected-source Roslyn check against the real Unity
    # reference profile (schema.meta.component_api_final
    # .script_source_check) and by the Editor compile gate. Only
    # the structural normalizers below remain: each rewrites a
    # value whose correct form is fixed by the engine API.
    _sanitize_coroutine_field_type(s)
    _sanitize_nullable_valuetype_access(s)    # X?.transform.position -> CS1503
    _sanitize_vector_scale_misuse(s)          # Vector3 * Vector3 -> CS0019
    # Sibling custom fields are only a source of an explicit
    # default for a same-named numeric read; they do not create a
    # cross-component reference or alter schema semantics.
    _auto_declare_undeclared_scalars(
        s, {
            f.name: f.default
            for other in scripts
            if other is not s and other.type == "custom"
            for f in (other.extra_fields or [])
            if f.default and (f.type or "").lower() in {
                "float", "double", "int", "long",
            }
        })
    # Must run LAST: a final brace/paren-balance check over the
    # cumulative effect of every pass above (see its own
    # docstring — lint_extensible's check at the top of this loop
    # only catches an already-unbalanced LLM body, not one a
    # later regex rewrite unbalances as a side effect).
    _reject_unbalanced_custom_bodies(s)


def prepare_scripts(schema: UnitySchema) -> None:
    """Step 6's pre-render pass over `schema.scripts`, in place.

    One of four pieces (with `stub_known_types`, `script_render_context` and
    `UnityProjectGenerator.render_project_scripts`) that `generate()` and the
    custom-script compiler check (`unity_authored_check`) share, so the check
    compiles exactly the Assets/Scripts source Step 6 writes. The check runs
    this on a deep copy; Step 6 runs it on the schema it ships.
    """
    for s in schema.scripts:
        if s.type == "custom":
            normalize_custom_script(s, schema.scripts)
            # A custom script can only attach to the Player if one exists.
            if s.attach_to == "player" and not any(
                    x.type == "player" for x in schema.scripts):
                s.attach_to = "systems"


def stub_known_types(schema: UnitySchema,
                     composed_units: "list | None") -> frozenset[str]:
    """Every type name this project already declares.

    The stub emitter in `_generate_script` invents a `[System.Serializable]`
    class for a field or signature type it does not recognise. Naming a type
    the project already declares would be a second definition (CS0101), so
    this set has to cover every declaration source:

    - every script name, custom or not: a custom script's field can name
      ANOTHER custom script as its type (live: MuseumMetaProgression.cs got
      its own empty `class GossipHub {}` beside the real GossipHub.cs);
    - every composed unit's own class;
    - every AUXILIARY type a composed micro's template declares. The board
      micro's GameBoard.cs declares `Piece`, `Zone`, `CommandSpec`; a custom
      script with a `Piece` field got its own stub beside the real one
      (live in PuzzleWell, twice in one build).

    Declaration ownership only; it is not a guessed member API.
    """
    names: set[str] = {s.name for s in schema.scripts}
    names |= {u.class_name for u in (composed_units or [])}
    for unit in composed_units or []:
        try:
            from .unity_micro_code import _template_dir_for
            from .micro.code_fields import template_types
            template_dir = _template_dir_for(unit)
            if template_dir is not None:
                names |= set(template_types(str(template_dir)))
        except Exception:              # noqa: BLE001 — never block codegen
            continue
    return frozenset(names)


def _palette_rgb_for(palette: Any) -> dict[str, Any]:
    return {
        "primary":    _unity_color(palette.primary),
        "secondary":  _unity_color(palette.secondary),
        "accent":     _unity_color(palette.accent),
        "background": _unity_color(palette.background),
        "trim":       _unity_color(palette.trim),
    }


def script_render_context(schema: UnitySchema,
                          composed_units: "list | None") -> dict[str, Any]:
    """The context every Assets/Scripts template renders with.

    `generate()` builds its wider `base_ctx` on top of this and renders the
    scripts with THIS dict, so the custom-script check renders them with the
    same values. A script template that starts reading a key this dict does
    not carry fails loudly in both paths (StrictUndefined); add the key here.
    """
    palette = schema.color_palette  # always non-None after model_validator
    assert palette is not None
    return {
        "project_name":      schema.project_name,
        "game_type":         schema.game_type,
        "platform":          schema.platform,
        "render_pipeline":   schema.render_pipeline,
        "visual_style":      schema.visual_style,
        "color_palette":     palette,
        "palette_rgb":       _palette_rgb_for(palette),
        "features":          schema.features,
        "story_beats":       schema.story_beats,
        "climbing_contract": _is_climbing_contract(schema, composed_units),
    }


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
        # Exposed for the post-generator spatial freeze.  The generator is the
        # only authority that sees measured catalog bounds, so the persisted
        # 6.5 artifact must use this exact table rather than re-discovering it.
        self.last_measured_footprints: dict[str, float] = {}
        self.last_projectile_presentation: dict[str, Any] = {}
        # The typed atom routes the last generate() placed (R2) and what
        # stamping them into schema.meta found. Callers print the report.
        self.last_atom_bindings: AtomBindings = AtomBindings(routes=[])
        self.last_atom_binding_report: BindingReport = BindingReport()
        # Invented types already stubbed in this project; reset per project by
        # render_project_scripts.
        self._stubbed_types: set[str] = set()

    def render_project_scripts(self, schema: UnitySchema, ctx: dict,
                               known_types: frozenset[str]) -> dict[str, str]:
        """Every Assets/Scripts file Step 6 writes, path -> source, in order.

        `ctx` is `script_render_context(...)`, `known_types` is
        `stub_known_types(...)`, and `schema` has already been through
        `prepare_scripts`. `generate()` writes the result; the custom-script
        compiler check (`unity_authored_check`) compiles it, so both see one
        rendering of one project.
        """
        # A type stubbed for ONE custom script must not be stubbed again for
        # the next: two scripts naming the same invented type emitted two
        # definitions of it (CS0101). _generate_script runs once per script, so
        # the accumulator has to outlive a single call — and must not outlive
        # the project.
        self._stubbed_types = set()
        pn = schema.project_name
        rendered: dict[str, str] = {}
        # The interface is emitted only for the deterministic climbing
        # contract. The composed controller implements it and the generated
        # stamina/interaction systems consume it without coupling those
        # systems to a Pipeline-LLM-authored class name.
        if ctx["climbing_contract"]:
            rendered[f"{pn}/Assets/Scripts/IClimbingState.cs"] = self._render(
                "scripts/IClimbingState.cs.j2", ctx)
        for script in schema.scripts:
            script_content, script_path = self._generate_script(
                schema, script, ctx, known_types)
            if "/_skip_" in script_path:
                continue  # greybox generator handled separately as Editor script
            rendered[script_path] = script_content
        return rendered

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

    @staticmethod
    def _model_aware_relayout(schema: UnitySchema,
                              footprints: dict[str, float],
                              spatial_fusion: dict | None = None) -> None:
        """Redo Step 5's packing with real measured footprints, then re-bake
        every Step 5 sidecar derived from the layout it just superseded.

        Groups whose positions came from a deterministic scaffold (racetrack
        start grids) keep them — they are anchored to real geometry, not to
        this pass's packing. Everything with a measured footprint is re-packed.

        The re-bake is the point of keeping this in one place. Step 5d's cover
        points (src/unity_cover_points.py) were computed from the obstacle and
        enemy positions this pass moves; left alone they point at where the
        obstacles USED to be, and the cover_shooter AI ships them. Whoever
        moves the layout re-derives what was computed from it.
        """
        # In greybox mode the room/corridor IR owns the world coordinate frame.
        # Asset footprints may inform later QA, but must never repack entities
        # out of their ProBuilder rooms.
        if is_greybox_enabled(schema):
            return

        from .unity_cover_points import META_KEY, compute_cover_points
        from .unity_layout import compute_layout

        for e in schema.entities:
            if e.name in footprints and e.placement in ("scatter", "grid", "ring", "line"):
                e.instance_positions = []
        if spatial_fusion is None:
            compute_layout(schema, footprints)
        else:
            # Re-run the same 6.5 authority used by the main pipeline so a
            # measured-footprint relayout cannot discard fused composition.
            # Measured scenery groups are cleared above, which makes them
            # eligible for the safe non-authored composition translation.
            from .unity_spatial_realization import realize_spatial_layout
            realize_spatial_layout(
                schema,
                footprints=footprints,
                spatial_fusion=spatial_fusion,
            )

        before = schema.meta.get(META_KEY) if isinstance(schema.meta, dict) else None
        compute_cover_points(schema)
        after = schema.meta.get(META_KEY) if isinstance(schema.meta, dict) else None
        if before is not None and after != before:
            _log.info("[relayout] cover points re-baked from the model-aware "
                      "layout: %d -> %d point(s)",
                      sum(len(v) for v in before.values()),
                      sum(len(v) for v in (after or {}).values()))

    def _generate_greybox_only_files(self, schema: UnitySchema,
                                     manifest_deps: dict[str, str]) -> dict[str, str]:
        """Emit a clean project shell with no gameplay, art or runtime assets."""
        pn = schema.project_name
        ir = greybox_ir_for_schema(schema)
        ev, revision = _VERSION_MAP.get(
            schema.unity_version, ("6000.0.66f2", "0000000000000")
        )
        title = _to_display_title(pn)
        base_ctx = {
            "project_name": pn,
            "display_title": title,
            "company_name": schema.company_name,
            "platform": "pc",
            "features": [],
            "game_type": "3D",
            "render_pipeline": "builtin",
            "color_space": "Gamma",
        }
        files: dict[str, str] = {
            f"{pn}/ProjectVersion.txt": self._render(
                "ProjectVersion.txt.j2",
                {"editor_version": ev, "editor_revision": revision},
            ),
            f"{pn}/Packages/manifest.json": self._render(
                "manifest.json.j2",
                {"dependencies": manifest_deps},
            ),
            f"{pn}/ProjectSettings/ProjectSettings.asset": self._render(
                "ProjectSettings.asset.j2",
                {**base_ctx, "product_guid": _make_guid(pn)},
            ),
            f"{pn}/ProjectSettings/EditorBuildSettings.asset": self._render(
                "EditorBuildSettings.asset.j2",
                {"scenes": [], "scene_guids": {}},
            ),
        }

        canonical_path = f"{pn}/Assets/Greybox/{pn}.greybox.json"
        unity_path = f"{pn}/Assets/Greybox/{pn}.greybox.unity.json"
        editor_path = f"{pn}/Assets/Editor/GreyboxLevelGenerator.cs"
        files[canonical_path] = json.dumps(
            ir.model_dump(mode="json"), indent=2, ensure_ascii=False
        ) + "\n"
        files[unity_path] = json.dumps(
            unity_greybox_asset_payload(ir), indent=2, ensure_ascii=False
        ) + "\n"
        files[editor_path] = self._render(
            "LevelGenerator.cs.j2",
            {"project_name": pn, "greybox_seed": ir.seed},
        )
        files[f"{canonical_path}.meta"] = _default_meta(
            _make_guid(f"{pn}/Greybox/{pn}.greybox")
        )
        files[f"{unity_path}.meta"] = _default_meta(
            _make_guid(f"{pn}/Greybox/{pn}.greybox.unity")
        )
        if is_greybox_plus_enabled(schema):
            plus_ir = greybox_plus_ir_for_schema(schema, ir)
            plus_path = f"{pn}/Assets/Greybox/{pn}.greybox_plus.json"
            files[plus_path] = json.dumps(
                plus_ir.model_dump(mode="json"), indent=2, ensure_ascii=False
            ) + "\n"
            files[f"{plus_path}.meta"] = _default_meta(
                _make_guid(f"{pn}/Greybox/{pn}.greybox_plus")
            )
        for folder in ("Assets/Greybox", "Assets/Editor", "Assets/Scenes"):
            files[f"{pn}/{folder}.meta"] = _default_meta(
                _make_guid(f"{pn}/{folder}"), folder=True
            )
        files[f"{editor_path}.meta"] = _meta_file(
            _make_guid(f"{pn}/Editor/GreyboxLevelGenerator")
        )
        files[f"{pn}/README.md"] = (
            f"# {title} — geometry-only greybox\n\n"
            f"Seed: `{ir.seed}`  \n"
            f"Rooms: {len(ir.rooms)}  \n"
            f"Corridors: {len(ir.corridors)}  \n"
            f"Boss depth: {ir.boss_depth}\n\n"
            "On first open, the Editor bootstrap builds and saves the ProBuilder "
            "world to `Assets/Scenes/Main.unity`. Regenerate with **Tools → "
            f"{title} → Generate Greybox**. The canonical source is "
            f"`Assets/Greybox/{pn}.greybox.json`.\n"
        )
        if is_greybox_plus_enabled(schema):
            files[f"{pn}/README.md"] += (
                "\nGreybox+ planning evidence is recorded in "
                f"`Assets/Greybox/{pn}.greybox_plus.json`. It contains image "
                "provenance and deterministic Macro/Meso/Micro primitive roles; "
                "it does not add final-art assets.\n"
            )
        return files

    def _resolve_all_models(self, schema: UnitySchema, lookups_dir: str,
                            model_source_dir: str | None,
                            model_overrides: dict[str, str] | None,
                            composed_units: list | None,
                            strict_assets: bool,
                            asset_cfg: dict | None,
                            sketchfab_cfg: dict | None = None,
                            llm: object | None = None,
                            locked_asset_bindings: dict[str, dict] | None = None
                            ) -> tuple[dict, dict[str, str]]:
        """Resolve every model the project uses: default pack → Blender
        overrides → per-entity catalog picks → rig downgrades.

        Both generate() (.meta emission + template paths) and write() (binary
        copies) call this — they MUST see identical results or the copies and
        .meta files desync. Deterministic: pure lookups, no LLM — with one
        opt-in exception threaded straight through to resolve_entity_assets:
        `llm`, when given AND `sketchfab.vision_select` is enabled, lets a
        Tier S live fetch visually verify its keyword-ranked pick (see that
        function's own docstring). None (every caller before this parameter
        existed, and unity-validate's no-LLM replay) is the same pure lookup
        it always was.

        Returns (used_models, entity_model_paths).
        """
        self.last_projectile_presentation = {}
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
            sketchfab_cfg=sketchfab_cfg, llm=llm,
            locked_asset_bindings=locked_asset_bindings)

        # Slice 3 projectile presentation is a compiler-owned resolver request,
        # not a world entity.  Resolve it through the exact same catalog/model
        # registration path so copying, textures, .meta files, attribution and
        # Resources.Load identity stay unified, but never append it to
        # schema.entities (which would invent a spawn and a scene-coverage
        # obligation).  A semantic miss deliberately bypasses the generic prop
        # fallback: Controller.cs.j2 owns the named primitive fallback.
        projectile_visual = projectile_visual_resolver_entity(schema)
        if projectile_visual is not None:
            visual_schema = schema.model_copy(deep=True)
            visual_schema.entities = [projectile_visual]
            visual_locked_bindings = None
            if (
                isinstance(locked_asset_bindings, dict)
                and projectile_visual.name in locked_asset_bindings
            ):
                visual_locked_bindings = {
                    projectile_visual.name: locked_asset_bindings[
                        projectile_visual.name
                    ]
                }
            visual_paths = resolve_entity_assets(
                visual_schema, used_models, all_models,
                lookups_dir=lookups_dir, asset_cfg=asset_cfg, strict=False,
                sketchfab_cfg=sketchfab_cfg, llm=None,
                locked_asset_bindings=visual_locked_bindings,
                allow_prop_fallback=False,
            )
            visual_path = visual_paths.get(projectile_visual.name, "")
            self.last_projectile_presentation = resolved_projectile_presentation(
                schema, visual_path
            )
            if visual_path:
                # Retain the compiler-only identity in model provenance/copy
                # tables. EntitySpawner only iterates schema.entities, so this
                # key can never create a loose world object.
                entity_model_paths[projectile_visual.name] = visual_path

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
            models_by_resource = {
                model.resource_path: model for model in used_models.values()
            }
            _sk_cfg = sketchfab_cfg or {}
            _sk_enabled = (bool(_sk_cfg.get("enabled"))
                           and schema.game_type not in ("2D", "2d_topdown"))
            _external_sk_catalog = (
                str(_sk_cfg.get("frozen_catalog_path") or "").strip() or None
            ) if _sk_enabled else None
            catalog_assets = list(load_catalog(
                str(lookups_dir), sketchfab_catalog_path=_external_sk_catalog
            ).get("assets") or [])
            _shared_sketchfab_root = (
                str(_sk_cfg.get("cache_dir") or "").strip()
                if _external_sk_catalog else None
            )
            bindings: dict[str, dict[str, Any]] = {}
            for entity_name, resource_path in sorted(entity_model_paths.items()):
                model = models_by_resource.get(resource_path)
                if model is None:
                    continue
                binding: dict[str, Any] = {
                    "filename": Path(model.dest_rel).name,
                    "source_filename": Path(model.src).name,
                    "resource_path": model.resource_path,
                    "source": model.source,
                    "license": model.license,
                    "attribution": model.attribution,
                }
                selection = _frozen_asset_selection(
                    model, catalog_assets, sketchfab_root=_shared_sketchfab_root
                )
                if selection is not None:
                    binding["catalog_selection"] = selection
                bindings[entity_name] = binding
            schema.meta["entity_model_bindings"] = bindings
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
                 sketchfab_cfg: dict | None = None,
                 llm: object | None = None,
                 locked_asset_bindings: dict[str, dict] | None = None,
                 spatial_fusion: dict | None = None
                 ) -> dict[str, str]:
        """
        Returns {relative_path: file_content} for every file in the project.
        Caller writes the dict to disk.

        `composed_units` (from MicroComposer.plan) are spliced into each scene as a
        `_GameSystems` GameObject carrying one MonoBehaviour per composed micro,
        referenced by its deterministic .cs.meta GUID.
        """
        self.last_projectile_presentation = {}
        self.last_atom_bindings = AtomBindings(routes=[])
        self.last_atom_binding_report = BindingReport()
        if is_geometry_only_greybox(schema):
            return self._generate_greybox_only_files(schema, manifest_deps)

        files: dict[str, str] = {}
        pn = schema.project_name

        # The ProBuilder greybox is the world coordinate authority for an
        # explicitly greybox-enabled build.  Stamp the deterministic IR before
        # model-aware relayout or template rendering so the generated project,
        # spatial sidecars and Step 7 all read one contract.  Theme/twist data
        # never enters this path; geometry-only IR forces theme="none".
        greybox_ir = None
        if is_greybox_enabled(schema):
            greybox_ir = greybox_ir_for_schema(schema)
            for message in apply_greybox_spatial_layout(schema, greybox_ir):
                _log.info("[greybox] %s", message)

        # ── Custom-script lint pass ───────────────────────────────────────────
        # LLM-authored C# fragments get the deterministic structural passes
        # before any render. Done here — not in the pipeline — so unity-validate
        # and the webui get them too. The custom-script compiler check
        # (unity_authored_check) calls the same three helpers on a copy, so it
        # compiles exactly what this method writes.
        _stub_known_types = stub_known_types(schema, composed_units)
        prepare_scripts(schema)

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
            # That class is the ONLY thing the entity's atoms render into, so it
            # is the entity's behavior: a blank or foreign name left the class
            # emitted but never added at spawn, and the atoms never ran (R2,
            # src/atom_bindings.py reports the route).
            for e in schema.entities:
                if e.atoms:
                    cls = entity_behavior_class_name(e.name)
                    known.add(cls)
                    if e.behavior != cls:
                        _log.info("[atom bindings] %s.behavior %r -> %s (its atoms "
                                  "render only into %s)", e.name, e.behavior, cls, cls)
                        e.behavior = cls
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
            composed_units, strict_assets, asset_cfg, sketchfab_cfg, llm,
            locked_asset_bindings)
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
        self.last_measured_footprints = dict(_footprints)
        if _footprints:
            self._model_aware_relayout(schema, _footprints, spatial_fusion)

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
        script_ctx = script_render_context(schema, composed_units)
        _palette_rgb = script_ctx["palette_rgb"]

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
        env_ctx = _environment_ctx(_tod, _fog, _fog_color, _sun_i, lighting, bg,
                                   schema.render_pipeline)
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
            # Every key the Assets/Scripts templates read, from the one
            # builder the custom-script compiler check also uses.
            **script_ctx,
            "display_title":     _to_display_title(pn),
            "company_name":      schema.company_name,
            "description":       schema.description,
            # Which subsystem kinds the composer owns — lets IR-driven scripts
            # (e.g. SceneSetup) defer UI to the GameHud/PauseMenu micros instead
            # of building a duplicate generic HUD.
            "composed_kinds":    sorted({u.kind for u in (composed_units or [])}),
            "has_npc_dialog_runtime": any(
                getattr(u, "kind", "") == "npc_dialog"
                and getattr(u, "target", "") == "npc"
                and bool(getattr(u, "dialog", {}).get("entity_name"))
                for u in (composed_units or [])),
            "unity_version":     schema.unity_version,
            "color_space":       schema.color_space,
            **_ambient_ctx(_palette_rgb, _lighting_variant_for(schema, composed_units)),
            "mat_guids":         mat_guids,
            "lighting":          lighting,
            "sun_rotation":      sun_rotation,
            "sun_euler":         sun_euler,
            "sun_color":         sun_color,
            "sun_intensity":     sun_intensity,
            "fog_enabled":       fog_enabled,
            "fog_color":         fog_color,
            "scenes":            schema.scenes,
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
            "greybox_enabled":   greybox_ir is not None,
            "greybox_ir":        greybox_ir.model_dump(mode="json") if greybox_ir else None,
            "greybox_seed":      greybox_ir.seed if greybox_ir else 0,
        }

        _ev, _er = _VERSION_MAP.get(schema.unity_version, ("6000.0.66f2", "0000000000000"))
        files[f"{pn}/ProjectSettings/ProjectVersion.txt"] = self._render(
            "ProjectVersion.txt.j2", {"editor_version": _ev, "editor_revision": _er}
        )

        files[f"{pn}/Packages/manifest.json"] = self._render(
            "manifest.json.j2", {**base_ctx, "dependencies": manifest_deps}
        )

        if greybox_ir is not None:
            greybox_asset = f"{pn}/Assets/Greybox/{pn}.greybox.json"
            files[greybox_asset] = json.dumps(
                greybox_ir.model_dump(mode="json"),
                indent=2,
                ensure_ascii=False,
            ) + "\n"
            files[f"{greybox_asset}.meta"] = _default_meta(
                _make_guid(f"{pn}/Greybox/{pn}.greybox")
            )
            files[f"{pn}/Assets/Greybox.meta"] = _default_meta(
                _make_guid(f"{pn}/Assets/Greybox"), folder=True
            )
            unity_asset = f"{pn}/Assets/Greybox/{pn}.greybox.unity.json"
            files[unity_asset] = json.dumps(
                unity_greybox_asset_payload(greybox_ir),
                indent=2,
                ensure_ascii=False,
            ) + "\n"
            files[f"{unity_asset}.meta"] = _default_meta(
                _make_guid(f"{pn}/Greybox/{pn}.greybox.unity")
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

        # Mandatory AAA Visual Atmosphere Pass.  This is emitted for every
        # Unity pipeline and every gameplay context, not only sky/flight
        # concepts.  The runtime bootstrap self-heals the volume/profile and
        # pipeline-specific camera/light hooks; the scene template places it
        # before gameplay objects so its DefaultExecutionOrder runs first.
        _aaa_atmosphere_guid = _make_guid(f"{pn}/Scripts/AAAAtmosphere")
        _aaa_template = ("scripts/AAAAtmosphere.cs.j2"
                         if schema.render_pipeline in ("URP", "HDRP")
                         else "scripts/AAAAtmosphereBuiltin.cs.j2")
        files[f"{pn}/Assets/Scripts/AAAAtmosphere.cs"] = self._render(
            _aaa_template, base_ctx)
        files[f"{pn}/Assets/Scripts/AAAAtmosphere.cs.meta"] = _meta_file(
            _aaa_atmosphere_guid)

        # A --compose build gets its own palette-tinted fill/rim/practical
        # light rig (the "lighting" core subsystem); a build without --compose
        # otherwise got only the single Directional Light + ambient gradient
        # baked above. Backfill the same rig here so both paths agree.
        _baseline_lighting_guid = self._emit_baseline_lighting(
            files, schema, pn, _palette_rgb, composed_units)
        _postprocess_profile_guid = self._emit_postprocess_volume(files, schema, pn)

        # ── Atom-route bindings (R2) ──────────────────────────────────────────
        # Derived here, after entity-behavior reconciliation, script
        # preparation and model resolution have decided which class each atom
        # list reaches, and BEFORE any host is placed: the scenes below and the
        # GameSequencer emission read the same record that gets persisted, so
        # what is attached and what is recorded cannot disagree.
        self.last_atom_bindings = derive_atom_bindings(
            schema, entity_model_paths, contract_implementers_for(composed_units))
        self.last_atom_binding_report = stamp_atom_bindings(schema, self.last_atom_bindings)
        for _msg in self.last_atom_binding_report.warnings:
            _log.warning("[atom bindings] %s", _msg)
        # A lowered atom that resolves the firing contract (Shoot, R3) must
        # compile even where no composed controller implements it. The
        # controller micro emits the contract beside its implementer; with no
        # implementer, emit the same contract alone, so the atom compiles and
        # refuses at runtime with FireResult.NoProvider (warned above).
        if any(binding.runtime_provider is not None
               and binding.runtime_provider.contract == firing_provider.CONTRACT
               and binding.runtime_provider.implementer is None
               for route in self.last_atom_bindings.routes
               for binding in route.atoms):
            _fp_path = f"{pn}/Assets/Scripts/{firing_provider.SCRIPT}"
            files[_fp_path] = self._render(firing_provider.PROJECT_TEMPLATE,
                                           {"emitter": "Unity Project Pipeline"})
            files[f"{_fp_path}.meta"] = _meta_file(
                _make_guid(f"{pn}/Scripts/{Path(firing_provider.SCRIPT).stem}"))
        _sequencer_route = self.last_atom_bindings.route("atoms")
        _sequencer_hosted = _sequencer_route is not None and _sequencer_route.attached

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
            # schema.atoms' host: a _GameSequencer object in each scene its
            # binding names (every gameplay scene). None = no route to host.
            "game_sequencer_guid": (_make_guid(f"{pn}/Scripts/GameSequencer")
                                    if _sequencer_hosted else None),
            "game_sequencer_scenes": (list(_sequencer_route.scenes)
                                      if _sequencer_route is not None and _sequencer_hosted
                                      else []),
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
            "aaa_atmosphere_guid": _aaa_atmosphere_guid,
            "baseline_lighting_guid": _baseline_lighting_guid,
            "postprocess_profile_guid": _postprocess_profile_guid,
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
                        lighting, bg, schema.render_pipeline))
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

        for script_path, script_content in self.render_project_scripts(
                schema, script_ctx, _stub_known_types).items():
            files[script_path]           = script_content
            files[f"{script_path}.meta"] = _meta_file(
                _make_guid(f"{pn}/Scripts/{Path(script_path).stem}")
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
                    _make_guid(f"{pn}/Models/{mesh.name}"),
                    readable="navmesh" in schema.features,
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
                        meta_fn = _humanoid_model_meta(
                            guid, item.scale, readable="navmesh" in schema.features)
                    elif item.rig == "generic":
                        meta_fn = _generic_model_meta(
                            guid, item.scale, readable="navmesh" in schema.features)
                    else:
                        meta_fn = _model_meta(
                            guid, readable="navmesh" in schema.features)
                    files[f"{pn}/Assets/Resources/{item.dest_rel}.meta"] = meta_fn
            ms_path = f"{pn}/Assets/Scripts/ModelSwap.cs"
            files[ms_path]           = self._render("ModelSwap.cs.j2", base_ctx)
            files[f"{ms_path}.meta"] = _meta_file(_make_guid(f"{pn}/Scripts/ModelSwap"))
            # A generated walking player still needs the deterministic
            # DefaultCharacter controller even when its selected visual is a
            # Generic mesh with no embedded clips (the blacksmith asset is
            # exactly that case). Vehicle-only projects keep the old behavior;
            # VehicleBoneDriver is their animation contract.
            needs_player_animator = any_humanoid or player_avatar_model is not None
            if needs_player_animator:
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
        # Schema-level atoms (intro fade, music start, etc.) fire once per
        # gameplay-scene load from a GameSequencer on the scene's own
        # _GameSequencer object (scene.unity.j2), placed from the binding above.
        # A route with no host scene emits nothing: a sequencer no scene
        # attaches is code that reads like it runs but never does.
        if _sequencer_hosted:
            from .atoms.backends.unity import unity_coroutine_body as _ucb
            gs_path = f"{pn}/Assets/Scripts/GameSequencer.cs"
            files[gs_path]           = self._render("GameSequencer.cs.j2", {
                **base_ctx,
                "atom_body": _ucb([inv.model_dump() for inv in schema.atoms]),
            })
            files[f"{gs_path}.meta"] = _meta_file(_make_guid(f"{pn}/Scripts/GameSequencer"))

        # ── Deterministic ProBuilder greybox editor script ────────────────────
        _stypes = {s.type for s in schema.scripts}
        _greybox_scripts = [s for s in schema.scripts if s.type in GREYBOX_SCRIPT_TYPES]
        if _greybox_scripts:
            ed_path = f"{pn}/Assets/Editor/GreyboxLevelGenerator.cs"
            files[ed_path] = self._render("LevelGenerator.cs.j2", {
                **base_ctx, "script": _greybox_scripts[0],
            })
            files[f"{ed_path}.meta"] = _meta_file(
                _make_guid(f"{pn}/Editor/GreyboxLevelGenerator")
            )

        # ── Kenney asset importer editor script ──────────────────────────────
        if kenney_assets:
            ki_path = f"{pn}/Assets/Editor/KenneyImporter.cs"
            files[ki_path]            = self._render("KenneyImporter.cs.j2", base_ctx)
            files[f"{ki_path}.meta"]  = _meta_file(
                _make_guid(f"{pn}/Editor/KenneyImporter")
            )

        # ── Editor folder (shared by SetupRenderPipeline + greybox + KenneyImporter) ─
        # (only create if not already handled by the URP/HDRP block below)
        has_editor_content = bool(_greybox_scripts) or bool(kenney_assets)
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

        # ── Active build target (mobile only) ────────────────────────────────
        # The Android PLAYER settings written above say how this builds FOR
        # Android; they do not SELECT Android. The active target lives in
        # Library/EditorUserBuildSettings.asset, a binary artifact Unity owns,
        # so it cannot be emitted next to ProjectSettings — a generated mobile
        # project opened on whatever the editor last used (Standalone) and read
        # as a desktop game. An editor script is the supported way to set it.
        if schema.platform == "mobile":
            bt_path = f"{pn}/Assets/Editor/SetupBuildTarget.cs"
            files[bt_path] = self._render("editor/SetupBuildTarget.cs.j2", base_ctx)
            files[f"{bt_path}.meta"] = _meta_file(
                _make_guid(f"{pn}/Editor/SetupBuildTarget"))
            files[f"{pn}/Assets/Editor.meta"] = _default_meta(
                _make_guid(f"{pn}/Assets/Editor"), folder=True)

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
                **base_ctx, "has_atmosphere": wants_atmosphere(schema, schema_only=composed_units is not None),
            })
            files[f"{script_path}.meta"] = _meta_file(
                _make_guid(f"{pn}/Editor/SetupRenderPipeline")
            )
            files[f"{pn}/Assets/Editor.meta"] = _default_meta(
                _make_guid(f"{pn}/Assets/Editor"), folder=True
            )

            aaa_editor_path = f"{pn}/Assets/Editor/SetupAAAVisualAtmosphere.cs"
            files[aaa_editor_path] = self._render(
                "editor/SetupAAAVisualAtmosphere.cs.j2", base_ctx)
            files[f"{aaa_editor_path}.meta"] = _meta_file(
                _make_guid(f"{pn}/Editor/SetupAAAVisualAtmosphere"))
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
        if wants_atmosphere(schema, schema_only=composed_units is not None):
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
            "has_atmosphere":  wants_atmosphere(schema, schema_only=composed_units is not None),
            "has_scene_audio": "audio" in schema.features or bool(schema.audio_tracks),
        }
        files[f"{pn}/README.md"] = self._render("readme.md.j2", readme_ctx)

        # ── compile-map sidecar ───────────────────────────────────────────────
        # Maps each generated .cs file (project-relative, forward slashes) back to
        # the template that produced it, so `unity_cli.py verify` can attribute a
        # compile error to its source .j2 during the template-hardening loop.
        files[f"{pn}/.compile_map.json"] = json.dumps(
            self._compile_map(schema, bool(kenney_assets), composed_units), indent=2
        )

        # Regeneration is the deterministic repair action for a missing pass:
        # re-emitting the canonical script/scene references is safe and stable.
        # If the canonical output itself is incomplete, abstain loudly rather
        # than shipping a blank URP/HDRP template.
        assert_aaa_contract(files, pn, schema.scenes, schema.render_pipeline)

        return files

    # ── compile-map ────────────────────────────────────────────────────────────

    def _compile_map(self, schema: UnitySchema, has_kenney: bool, composed_units: list | None = None) -> dict[str, str]:
        """file (relative to project root) -> source template path under templates/unity/."""
        m: dict[str, str] = {}
        for s in schema.scripts:
            if s.type in GREYBOX_SCRIPT_TYPES:
                m["Assets/Editor/GreyboxLevelGenerator.cs"] = "editor/LevelGenerator.cs.j2"
                continue
            tpl = _LEGACY_SCRIPT_TEMPLATES.get(s.type)
            if tpl:
                m[f"Assets/Scripts/{s.name}.cs"] = f"scripts/{tpl}"
        for mesh in schema.procedural_meshes:
            m[f"Assets/Scripts/{mesh.name}Mesh.cs"] = "scripts/ProceduralMesh.cs.j2"
        if schema.render_pipeline in ("URP", "HDRP"):
            m["Assets/Editor/SetupRenderPipeline.cs"] = "editor/SetupRenderPipeline.cs.j2"
            m["Assets/Editor/SetupAAAVisualAtmosphere.cs"] = (
                "editor/SetupAAAVisualAtmosphere.cs.j2")
        if schema.render_pipeline == "URP":
            m["Assets/Editor/URPMaterialPostprocessor.cs"] = "editor/URPMaterialPostprocessor.cs.j2"
        m["Assets/Scripts/AAAAtmosphere.cs"] = (
            "scripts/AAAAtmosphere.cs.j2"
            if schema.render_pipeline in ("URP", "HDRP")
            else "scripts/AAAAtmosphereBuiltin.cs.j2")
        if _is_climbing_contract(schema, composed_units):
            m["Assets/Scripts/IClimbingState.cs"] = "scripts/IClimbingState.cs.j2"
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
              project_config: dict | None = None,
              llm: object | None = None,
              locked_asset_bindings: dict[str, dict] | None = None,
              spatial_fusion: dict | None = None
              ) -> tuple[Path, int]:
        """Write the project to disk; return (project_root, file_count)."""
        files = self.generate(schema, manifest_deps,
                              lookups_dir=lookups_dir,
                              kenney_source_dir=kenney_source_dir,
                              composed_units=composed_units,
                              model_source_dir=model_source_dir,
                              model_overrides=model_overrides,
                              strict_assets=strict_assets,
                              asset_cfg=asset_cfg,
                              sketchfab_cfg=sketchfab_cfg,
                              llm=llm,
                              locked_asset_bindings=locked_asset_bindings,
                              spatial_fusion=spatial_fusion)
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

        # Sweep BEFORE the gate below, not after. The sweep is manifest-driven,
        # and the manifest is written by the same call — so a run that raised at
        # the gate used to leave its files on disk having recorded NOTHING, and
        # every later run's sweep was blind to them forever. Those orphans then
        # compile alongside the new build: an aborted run's Interactable.cs
        # (never gated, hence broken — that is why the run aborted) failed the
        # real Unity compile of a LATER, clean build of the same project. Hit
        # live on Chromabreak.
        #
        # The files are on disk the moment the loop above finishes, which is
        # exactly what the manifest claims, so recording it here is the honest
        # order — and it means a build can never again be poisoned by the
        # wreckage of the run that failed before it.
        self._sweep_stale_outputs(root, schema.project_name, set(files))

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

        # Geometry-only output intentionally has no model, texture, material,
        # lighting, runtime-script or gameplay-asset fulfilment phase.
        if is_geometry_only_greybox(schema):
            return root / schema.project_name, len(files)

        # Binary model copies. Same _resolve_all_models call as generate() —
        # deterministic, so the .meta files emitted there and the binaries
        # copied here always line up (including rig downgrades that drop
        # clips). Still true with vision_select on: a Tier S auto_fetch only
        # ever fires on a catalog cache MISS, and generate()'s own call above
        # already froze this run's picks to lookups/unity/sketchfab_catalog.json
        # before this second call's fresh load_catalog() read — so this call
        # finds every entity's pick already cached and never re-triggers
        # fetch_and_freeze (or a second, potentially different, vision call)
        # for it at all.
        used_models, _ = self._resolve_all_models(
            schema, lookups_dir, model_source_dir, model_overrides,
            composed_units, strict_assets, asset_cfg, sketchfab_cfg, llm,
            locked_asset_bindings)
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
        if wants_atmosphere(schema, schema_only=composed_units is not None):
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

    def _emit_baseline_lighting(self, files: dict, schema: UnitySchema, pn: str,
                                palette_rgb: dict, composed_units: list | None) -> "str | None":
        """Bake the LightingDirector rig for a build that did not `--compose`.

        `--compose` already emits its own LightingDirector (see MicroComposer /
        subsystems_for's core "lighting" unit) — this is a no-op then, so a
        project never gets two competing rigs. Otherwise every non-composed
        build previously got only the single hard-coded scene Directional
        Light plus the ambient gradient (both baked unconditionally into
        scene.unity.j2 already); this adds the same palette-tinted fill/rim/
        practical rig a `--compose` build gets.

        Uses build_default_lighting_schema — the pure, LLM-free extraction of
        what LightingPipeline.default_fix bakes for an unauthored schema —
        rather than constructing a LightingPipeline, whose __init__ builds an
        LLM chat client this deterministic path (Steps 4-7) must never touch.
        Returns the new script's GUID, or None when nothing was emitted.
        """
        if any(u.kind == "lighting" for u in (composed_units or [])):
            return None
        from .micro.lighting import AMBIENT_LUMINANCE_FLOOR, build_default_lighting_schema

        variant = _lighting_variant_for(schema, composed_units)
        light_schema = build_default_lighting_schema(variant, palette_rgb)
        # Same loader-root recipe MicroPipeline.jinja() uses for this template
        # (search the lighting micro's own folder, then the shared micro root
        # its `{% import "_inject.j2" %}` resolves against) — a DIFFERENT env
        # from self._env (which doesn't cover templates/unity/micro/) and with
        # no trim_blocks/lstrip_blocks, so a --compose build and this baseline
        # path render byte-identical output for the same schema.
        tpl_root = self.template_dir / "micro" / "lighting"
        env = Environment(
            loader=FileSystemLoader([str(tpl_root), str(tpl_root.parent)]),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )
        cs_text = env.get_template("LightingDirector.cs.j2").render(
            s=light_schema, variant=variant, ambient_floor=AMBIENT_LUMINANCE_FLOOR)
        guid = _make_guid(f"{pn}/Scripts/LightingDirector")
        files[f"{pn}/Assets/Scripts/LightingDirector.cs"] = cs_text
        files[f"{pn}/Assets/Scripts/LightingDirector.cs.meta"] = _meta_file(guid)
        return guid

    def _emit_postprocess_volume(self, files: dict, schema: UnitySchema, pn: str) -> "str | None":
        """Bake one conservative global URP Volume + VolumeProfile per project
        (see _generate_postprocess_profile). Returns the profile asset's GUID
        for the scene template's Volume component, or None when nothing was
        emitted.

        URP-only for v1: HDRP's post-processing overrides are a DIFFERENT set
        of Runtime classes (different GUIDs, different property surfaces) even
        though the concepts (Bloom, Tonemapping, ...) are the same, so this
        abstains rather than guessing at an HDRP profile. Per the "abstain
        loudly" doctrine, that's a log line, not a silent no-op.
        """
        if schema.render_pipeline != "URP":
            _log.info(
                "post-processing volume skipped for %s: render_pipeline=%r "
                "(only URP is implemented for v1)", pn, schema.render_pipeline)
            return None
        profile_guid = _make_guid(f"{pn}/Settings/PostProcessProfile")
        files[f"{pn}/Assets/Settings/PostProcessProfile.asset"] = _generate_postprocess_profile()
        files[f"{pn}/Assets/Settings/PostProcessProfile.asset.meta"] = \
            _native_meta(profile_guid, 11400000)
        return profile_guid

    def _generate_script(self, schema: UnitySchema, script: UnityScript,
                         base_ctx: dict,
                         known_types: "frozenset[str]" = frozenset()) -> tuple[str, str]:
        tpl_file = _LEGACY_SCRIPT_TEMPLATES.get(script.type)
        pn       = schema.project_name

        # Greybox generators live in Assets/Editor — handled in generate(); emit nothing here.
        if script.type in GREYBOX_SCRIPT_TYPES:
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
        if script.type == "custom":
            # Method SIGNATURES name types too. Reading extra_fields alone
            # left a type used only as a return type or a parameter
            # unstubbed: live in BlockWell's GridLogic, `GridPiece` (a
            # field) was stubbed and `GridCell` (the return type of
            # RotateGrid) was not — one CS0246 on first open.
            sig_types = [_TypeCarrier(ty) for ty in _signature_type_names(
                getattr(script, "extra_methods", None) or [])]
            candidates = list(script.extra_fields or []) + sig_types
            if candidates:
                stubbed = self._stubbed_types
                names = _stub_type_names(
                    candidates, known_types | frozenset(stubbed))
                if names:
                    stubbed.update(names)
                    stub_classes = "\n".join(
                        f"[System.Serializable]\npublic class {n}\n{{\n"
                        f"    // Auto-generated stub — add fields as needed\n}}\n"
                        for n in names) + "\n"

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
                                              "needs_rigidbody": needs_rigidbody,
                                              "needs_player_rigidbody": _needs_player_rigidbody(script)})
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
