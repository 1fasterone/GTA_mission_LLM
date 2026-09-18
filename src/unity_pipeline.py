"""
6-step Unity Project Creator pipeline — fully synchronous.

STEP 2 — LLM generates project concept (game type, features, atmosphere)
STEP 3 — LLM fills the project schema (plain English descriptions)
STEP 4 — Resolver maps descriptions -> Unity package IDs + versions
         + injects required packages for render pipeline, game type, features
STEP 5 — Programmatic fixer + schema validation
STEP 6 — Generator compiles schema -> Unity project folder (no LLM)
"""

import re
from pathlib import Path

from pydantic import ValidationError
from rich.console import Console
from rich.panel   import Panel

from .backends           import make_chat_client, make_rag_client
from .micro_pipeline     import to_pascal
from .unity_schema       import (UnityPackage, UnitySchema, UnityScript,
                                 is_embodied)
from .unity_schema_clamp import clamp_capped_lists, render_cap_reminder
from .unity_schema_jsonschema import response_format_schema
from .capability_resolver import resolve_capabilities, resolve_microgens
from .unity_asset_catalog import asset_vocabulary
from .unity_resolver     import UnityResolver
from .unity_generator    import UnityProjectGenerator
from .unity_prompts      import (
    UNITY_CONCEPT_SYSTEM, UNITY_CONCEPT_USER,
    UNITY_SCHEMA_FILL_SYSTEM, UNITY_SCHEMA_FILL_USER,
    GAMECONTEXT_SCHEMA_GUIDANCE, render_multimodal_block, brief_block,
)
from .themes              import (
    resolve_theme, render_concept_bias, render_schema_hints,
)
from .unity_archetypes    import classify as classify_intent, apply_intent
from .unity_racetrack     import compose_racetrack, is_racing_degenerate
from .unity_concept_entities import (extract_named_entities,
                                     inject_missing_concept_entities,
                                     render_entity_checklist)
from .unity_concept_scripts import (extract_named_scripts,
                                    inject_missing_concept_scripts,
                                    render_script_checklist)
from .unity_concept_scenes import (inject_missing_concept_scenes,
                                   render_scene_checklist)
from .unity_concept_audio import (inject_missing_concept_audio,
                                  render_audio_checklist)
from .unity_concept_narrative import (inject_missing_concept_narrative,
                                      render_narrative_checklist)
from .unity_concept_title import (inject_missing_concept_title,
                                  render_title_checklist)
from .unity_concept_missions import inject_missing_concept_missions
from .unity_layout        import compute_layout
from .unity_cover_points  import compute_cover_points
from .unity_scenebind     import apply_scene
from .unity_playability   import PlayabilityGateError, gate as playability_gate
from .unity_playability_artifact import evaluate_project
from .unity_playability_repair import repair_and_regrade
from .unity_pathnorm      import normalize_paths
from .unity_objective_targets import resolve_objective_targets
from .unity_escort_follow import resolve_escort_follow
from .unity_spatial_facts import attach_spatial_facts, extract_spatial_facts

console = Console()


def _compose_palette_rgb(schema) -> dict:
    """schema.color_palette -> {slot: {r,g,b}} for the composed lighting micro.

    Same shape and same conversion the generator bakes into the scene template's
    `palette_rgb` context, so the runtime lighting guard and the scene YAML's
    m_Ambient*Color agree instead of fighting each other.
    """
    from .unity_generator import _unity_color
    pal = getattr(schema, "color_palette", None)
    if pal is None:
        return {}
    return {slot: _unity_color(getattr(pal, slot))
            for slot in ("primary", "secondary", "accent", "background", "trim")}


# ── Programmatic fixer ────────────────────────────────────────────────────────

_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")

# Script types whose class name is hard-referenced by other templates and so
# must keep a fixed, canonical name regardless of what the LLM called them.
_CANONICAL_SCRIPT_NAMES = {
    "game_manager": "GameManager",
    "mesh_builder": "MeshBuilder",
    "health":       "HealthSystem",
}

# Known public API of the canonical GameManager template (class members the LLM
# can safely call via GameManager.Instance.<member>).  Calls to anything outside
# this set are stripped from custom script bodies to prevent CS0246/CS1061.
_GAME_MANAGER_API: frozenset[str] = frozenset({
    "Instance", "State", "Score", "Lives", "Level",
    "OnScoreChanged", "OnLivesChanged", "OnStateChanged",
    "AddScore", "LoseLife", "ResumeGame", "AdvanceLevel",
    "RestartLevel", "LoadMainMenu",
})

# Public API of the composed GameContext locator (compose mode only). Custom
# scripts reach other subsystems via GameContext.Instance.Get<T>().
_GAME_CONTEXT_API: frozenset[str] = frozenset({"Instance", "Get"})

# Regex to find "SomeName.Instance.Whatever" singleton calls in C# body strings.
_SINGLETON_CALL_RE = re.compile(r"\b(\w+)\.Instance\.(\w+)\b")
# Line-level split (keeps \n in place for the join).
_LINE_RE = re.compile(r"[^\n]*\n?")

# GameManager reached indirectly via the compose locator — `var x =
# GameContext.Instance.Get<GameManager>();` then `x.Member` — must be validated
# the same way as a direct `GameManager.Instance.Member` call. _SINGLETON_CALL_RE
# only sees the `Get<GameManager>()` call itself (a valid GameContext.Get), not
# what the LLM does with the returned reference afterward.
_GET_GAMEMANAGER_BIND_RE = re.compile(r"\b(\w+)\s*=\s*GameContext\.Instance\.Get<GameManager>\s*\(\s*\)")

# Hallucinated "is the Input System available" guard. InputSystem is a real
# static class (GetDevice<T>(), devices, AddDevice() — no `.current`, and a type
# name can never be null-compared), so this pattern is always a compile error
# (CS0119 + CS0117), not a real check — the package is either referenced or not,
# resolved at compile time. No real guard is needed, so any line containing it
# is dropped like other invalid calls.
_BOGUS_INPUTSYSTEM_GUARD_RE = re.compile(r"\bInputSystem\s*!=\s*null\b|\bInputSystem\.current\b")


def _sanitize_singleton_calls(body: str, known_classes: set[str],
                              allow_context: bool = False) -> str:
    """Strip lines from a C# body string that call undefined singleton methods.

    Cases handled:
      1. ``UndefinedClass.Instance.Foo(...)`` where UndefinedClass has no script
         in the schema → entire line removed.
      2. ``GameManager.Instance.UndefinedProp(...)`` where the property/method is
         not part of the canonical GameManager template API → entire line removed.
      3. ``GameContext.Instance.Get<...>(...)`` — kept ONLY when allow_context
         (i.e. --compose, where the GameContext locator exists); otherwise the
         locator is undefined and the line is removed.
      4. ``var x = GameContext.Instance.Get<GameManager>(); ... x.UndefinedProp``
         — same undefined-member check as case 2, applied to the local variable
         the reference was bound to, not just the literal class name.
      5. ``InputSystem != null`` / ``InputSystem.current`` — a hallucinated,
         always-invalid "is input available" guard → entire line removed.

    Only lines that exclusively contain the bad call are dropped; lines with
    multiple statements are stripped only of the offending statement.
    """
    gamemanager_vars = {m.group(1) for m in _GET_GAMEMANAGER_BIND_RE.finditer(body)}
    member_re = None
    if gamemanager_vars:
        alt = "|".join(re.escape(v) for v in gamemanager_vars)
        member_re = re.compile(rf"\b(?:{alt})\.(\w+)\b")

    lines_out: list[str] = []
    for line in body.splitlines(keepends=True):
        keep = True
        if _BOGUS_INPUTSYSTEM_GUARD_RE.search(line):
            keep = False
        if keep:
            for m in _SINGLETON_CALL_RE.finditer(line):
                cls, member = m.group(1), m.group(2)
                if cls == "GameContext":
                    if allow_context and member in _GAME_CONTEXT_API:
                        continue
                    keep = False
                    break
                if cls not in known_classes:
                    keep = False
                    break
                if cls == "GameManager" and member not in _GAME_MANAGER_API:
                    keep = False
                    break
        if keep and member_re:
            for m in member_re.finditer(line):
                if m.group(1) not in _GAME_MANAGER_API:
                    keep = False
                    break
        if keep:
            lines_out.append(line)
    return "".join(lines_out)


# Unity's new Input System has no `InputSystem.current` accessor — the LLM
# regularly confuses the static package namespace (UnityEngine.InputSystem)
# with the per-device singletons (Mouse.current, Keyboard.current, ...) and
# emits the hallucinated CS0117 chain "InputSystem.current[.device]".
_INPUT_DEVICE_CLASS = {
    "mouse": "Mouse", "keyboard": "Keyboard",
    "gamepad": "Gamepad", "touchscreen": "Touchscreen",
}
_INPUT_SYSTEM_CURRENT_DEVICE_RE = re.compile(
    r"InputSystem\.current\.(mouse|keyboard|gamepad|touchscreen)\b")
_INPUT_SYSTEM_CURRENT_RE = re.compile(r"InputSystem\.current\b")


def _fix_input_system_current(code: str) -> str:
    """Rewrite the hallucinated ``InputSystem.current[.device]`` chain.

    Real API: ``Mouse.current`` / ``Keyboard.current`` / ``Gamepad.current`` /
    ``Touchscreen.current`` — there is no ``.current`` on the ``InputSystem``
    class itself. The device word (if any) also names which class the
    fragment's OTHER bare ``InputSystem.current`` null-check should become —
    LLM fragments consistently address one device per fragment.
    """
    if "InputSystem.current" not in code:
        return code
    m = _INPUT_SYSTEM_CURRENT_DEVICE_RE.search(code)
    device_cls = _INPUT_DEVICE_CLASS[m.group(1)] if m else "Mouse"
    code = _INPUT_SYSTEM_CURRENT_DEVICE_RE.sub(
        lambda mm: f"{_INPUT_DEVICE_CLASS[mm.group(1)]}.current", code)
    code = _INPUT_SYSTEM_CURRENT_RE.sub(f"{device_cls}.current", code)
    return code


# Physics.Raycast (and its Linecast/SphereCast/BoxCast/CapsuleCast siblings)
# return a bool and write the hit details to the `out RaycastHit` parameter —
# the LLM sometimes treats the bool return value itself as the RaycastHit,
# calling struct members (.collider, .point, ...) directly on it (CS1061).
_RAYCAST_HIT_MEMBERS = ("collider", "point", "normal", "distance", "transform",
                        "rigidbody", "textureCoord", "triangleIndex",
                        "barycentricCoordinate", "colliderInstanceID")
_RAYCAST_OUT_RE = re.compile(
    r"\bvar\s+(\w+)\s*=\s*Physics\.(?:Raycast|Linecast|SphereCast|BoxCast|CapsuleCast)"
    r"\s*\([^;]*?\bout\s+RaycastHit\s+(\w+)\b")


def _fix_raycast_bool_member_access(code: str) -> str:
    """Redirect RaycastHit member access from the bool return onto the out-param.

    ``var hit = Physics.Raycast(..., out RaycastHit hitInfo, ...); hit.point``
    -> the second use of ``hit.point`` must read ``hitInfo.point`` — `hit` is
    a bool and has no such member.
    """
    m = _RAYCAST_OUT_RE.search(code)
    if not m:
        return code
    bool_var, hit_var = m.group(1), m.group(2)
    if bool_var == hit_var:
        return code
    member_re = re.compile(rf"\b{re.escape(bool_var)}\.({'|'.join(_RAYCAST_HIT_MEMBERS)})\b")
    return member_re.sub(rf"{hit_var}.\1", code)


def _to_pascal(s: str, fallback: str = "Script") -> str:
    """Convert any string to PascalCase, preserving existing PascalCase."""
    if _IDENT_RE.match(s):
        return s   # already a valid identifier — don't mangle it
    parts = re.sub(r"[^a-zA-Z0-9]+", " ", s).split()
    result = "".join(p.capitalize() for p in parts)
    if not result:
        return fallback
    if result[0].isdigit():
        result = fallback + result
    return result


# Section headers that mark a fully-authored design brief (vs a one-line vibe
# prompt). Three or more = the author already themed it — see
# _is_structured_brief's docstring for what that gates.
#
# ANY line starting with a run of ALL-CAPS word(s) — not a fixed vocabulary.
# Originally a closed list (TITLE|GENRE|STYLE|CORE PREMISE|SCENES|NARRATIVE|
# SCRIPTS|ENTITIES|MISSIONS|AUDIO|FEATURES|VISUAL STYLE|COLOR PALETTE|
# PROCEDURAL) tuned for briefs that mirror the schema's own field names.
# Confirmed live (2026-08-28): a fully-authored 10-section brief using
# different, equally-structured header words (GOAL/CORE GAMEPLAY/LEVEL &
# PLATFORMS/PLAYER STATS & LIVES/UI & HUD/...) matched only 2 of the old
# list ("AUDIO", "STYLE" via "STYLE & TONE") — under the 3-marker floor — so
# resolve_theme() ran its autodetect path on brief content that had already
# fully themed itself, grafting an unrelated "Superhero Flying Mechanics /
# flight burns calories" theme onto a ground-based brawler. That is the EXACT
# failure mode this check exists to prevent (see the TerminalHorizon incident
# noted where this is called in run()) — the vocabulary was just too narrow
# to recognize a second, differently-worded but equally real structured
# brief. No colon required — matches the pre-existing convention (a bare
# "ENTITIES" or "SCENES (8 total)" line with no trailing colon was already a
# valid marker; see test_structured_brief_detection).
_BRIEF_MARKER_RE = re.compile(
    r"^\s*([A-Z][A-Z]+(?:[ &/-]+[A-Z][A-Z]+)*)\b",
    re.MULTILINE)


def _apply_ramp_tilts(schema: UnitySchema) -> list[str]:
    """Assign a default UnitySchema.EntitySpec.tilt_deg to any 2D entity whose
    name/model_hint signals a ramp but which still has tilt_deg==0 — see the
    field's own docstring in unity_schema.py. 3D-only games are left alone:
    ramps there are ordinary geometry (a rotated model/mesh), not something
    this 2D-specific BoxCollider2D-rotation technique applies to."""
    if schema.game_type != "2D":
        return []
    msgs: list[str] = []
    sign = 1.0
    for e in schema.entities:
        if e.tilt_deg != 0.0:
            continue
        haystack = f"{e.name} {e.model_hint}".lower()
        if "ramp" not in haystack:
            continue
        e.tilt_deg = 22.0 * sign
        sign = -sign
        msgs.append(f"[ramp-tilt] '{e.name}' had no tilt_deg — assigned "
                    f"{e.tilt_deg:+.1f}° (name/model_hint signaled a ramp)")
    return msgs


def _is_structured_brief(prompt: str) -> bool:
    return len(set(m.group(1).upper()
                   for m in _BRIEF_MARKER_RE.finditer(prompt or ""))) >= 3


def _hint_from_name(name: str, kind: str) -> str:
    """Deterministic model_hint from an entity's own name (+ specific kind)."""
    from .unity_asset_catalog import tokenize
    from .unity_entity_asset_resolver import _STOPWORDS
    words = [t for t in tokenize(name or "") if t not in _STOPWORDS]
    k = (kind or "").lower()
    if k and k != "prop" and k not in words:
        words.append(k)
    return " ".join(words[:4]) or (k or "prop")


def _programmatic_fix(schema: UnitySchema, compose: bool = False) -> UnitySchema:
    schema.project_name = _to_pascal(schema.project_name, "UnityProject")
    schema.company_name = re.sub(r"[/\\]", "_", schema.company_name).strip() or "MyCompany"

    seen: set[str] = set()
    scenes: list[str] = []
    for s in schema.scenes:
        clean = re.sub(r"[^a-zA-Z0-9_-]", "", s)
        if clean and clean not in seen:
            seen.add(clean)
            scenes.append(clean)
    schema.scenes = scenes[:8] or ["Main"]

    # "Main" must be first — Unity treats the first build-settings scene as entry point
    if "Main" not in schema.scenes:
        schema.scenes.insert(0, "Main")

    schema.scripts = schema.scripts[:24]
    from .unity_generator import _CS_BUILTIN_TYPES
    for script in schema.scripts:
        script.name = _to_pascal(script.name, "Script")
        # A script class named after a Unity/C# engine type (WheelCollider,
        # Animator, ...) shadows the real type project-wide — global namespace
        # wins over `using UnityEngine` — breaking every other script that uses
        # it. Rename deterministically instead of shipping the collision.
        if script.name in _CS_BUILTIN_TYPES:
            script.name = script.name + "Behaviour"

    # Several templates hard-reference singleton/component classes by name —
    # UIManager & Collectible -> GameManager, ProceduralMesh -> MeshBuilder,
    # Collectible -> HealthSystem. If the LLM names one of those scripts
    # thematically (e.g. a game_manager called "BaseBuilder"), the references
    # fail to compile (CS0246). Force the canonical class name for these types
    # so the references always resolve, and drop any duplicate singleton.
    seen_canonical: set[str] = set()
    deduped: list[UnityScript] = []
    for script in schema.scripts:
        canon = _CANONICAL_SCRIPT_NAMES.get(script.type)
        if canon:
            if canon in seen_canonical:
                continue  # only one GameManager / MeshBuilder / HealthSystem
            seen_canonical.add(canon)
            script.name = canon
        deduped.append(script)
    schema.scripts = deduped

    # Strip singleton calls in custom script bodies that reference undefined
    # classes (e.g. UIManager.Instance.XXX when no UIManager exists) or
    # undefined GameManager methods (e.g. GameManager.Instance.ActiveZone).
    # Also fix two recurring LLM API hallucinations in the same fragments:
    # the nonexistent InputSystem.current accessor, and RaycastHit members
    # called on Physics.Raycast's bool return instead of its out-parameter.
    known_classes: set[str] = {s.name for s in schema.scripts}

    def _clean(body: str) -> str:
        # Rewrite recoverable InputSystem.current[.device] hallucinations to
        # their real per-device accessor BEFORE the singleton-call sanitizer's
        # bogus-guard strip — otherwise the strip pass (which matches any bare
        # "InputSystem.current" substring) eats the whole line first and the
        # rewrite below never gets a chance to run.
        body = _fix_input_system_current(body)
        body = _sanitize_singleton_calls(body, known_classes, allow_context=compose)
        body = _fix_raycast_bool_member_access(body)
        return body

    for script in schema.scripts:
        if script.type != "custom":
            continue
        if script.update_extra:
            script.update_extra = _clean(script.update_extra)
        if script.awake_extra:
            script.awake_extra = _clean(script.awake_extra)
        if script.fixed_update_extra:
            script.fixed_update_extra = _clean(script.fixed_update_extra)
        for m in script.extra_methods:
            m.body = _clean(m.body)

    return schema


# ── Consistency checker ────────────────────────────────────────────────────────

# Kind -> the tag that makes that kind of content interactive. Only kinds whose
# gameplay role is unambiguous appear here: "prop" and "structure" are
# deliberately absent, because scenery with a collider tag is a different bug.
# Every value is one of the tags TagManager.asset.j2 already declares.
_OBJECTIVE_KIND_TAGS: dict[str, str] = {
    "collectible": "Collectible",
    "pickup":      "Collectible",
    "item":        "Collectible",
    "goal":        "Interactable",
    "objective":   "Interactable",
    "enemy":       "Enemy",
    "boss":        "Enemy",
    "obstacle":    "Obstacle",
    "hazard":      "Obstacle",
}


def _check_consistency(schema: UnitySchema) -> tuple[UnitySchema, list[str]]:
    """
    Ensure script types and features are coherent.
    Returns (fixed_schema, list_of_fix_messages).
    """
    fixes: list[str] = []

    # Helper re-derived each call so injections made above are visible below.
    def _stypes() -> set[str]:
        return {s.type for s in schema.scripts}

    # Load-bearing tags for objective content. EntitySpawner only attaches a
    # collider to a group that carries a tag (templates/unity/EntitySpawner.cs.j2)
    # and the composed micros acquire their targets with FindGameObjectWithTag,
    # so an untagged collectible cannot be picked up and an untagged enemy cannot
    # be seen, hit or chased — it is scenery with a hostile name. The rescue path
    # in unity_concept_entities already tags the entities it restores from the
    # brief; entities the fill wrote ITSELF had nothing doing the same for them,
    # which is why a skydiving build shipped four uncollectable altitude gates
    # and an enemy script with nothing tagged Enemy to drive.
    #
    # `kind` is the ground truth here — a deterministic lookup, not a guess —
    # and the generator copies every entity tag into TagManager, so the tag it
    # assigns always exists at runtime.
    for _e in schema.entities:
        _canon = _OBJECTIVE_KIND_TAGS.get((_e.kind or "").lower())
        if _canon and (not _e.tag or _e.tag == "Untagged"):
            _e.tag = _canon
            fixes.append(f"Untagged {_e.kind} {_e.name!r} -> tag={_canon} "
                         "(no tag = no collider, so it cannot be "
                         "collected/hit)")

    # VR requires Input System for XR controller input
    if schema.game_type == "VR" and "input_system" not in schema.features:
        schema.features.append("input_system")
        fixes.append("VR game: added input_system feature")

    # NavMesh has no 2D pathfinding support in Unity
    if schema.game_type == "2D" and "navmesh" in schema.features:
        schema.features.remove("navmesh")
        fixes.append("2D game: removed navmesh (not applicable to 2D)")

    if "game_manager" not in _stypes():
        schema.scripts.insert(0, UnityScript(
            name        = "GameManager",
            type        = "game_manager",
            description = "Singleton game manager controlling game state and score",
        ))
        fixes.append("Injected GameManager (required boilerplate)")

    # A "player"-type script is what makes scene.unity.j2 build the Player
    # GameObject — a capsule with a MeshRenderer, tagged Player, carrying a
    # movement controller. Injecting it into a schema that DECLARED it has no
    # avatar (locomotion='none') plants the exact object the declaration says
    # does not exist, on the one path that never runs compose: the live
    # CodeVault build shipped a walking capsule standing in the middle of a
    # repo-teaching board. Skipped loudly, never silently.
    if "player" not in _stypes() and is_embodied(schema):
        schema.scripts.append(UnityScript(
            name        = "PlayerController",
            type        = "player",
            description = f"Player movement controller for {schema.game_type} game",
        ))
        fixes.append("Injected PlayerController (required boilerplate)")
    elif "player" not in _stypes():
        fixes.append(
            "locomotion='none': did NOT inject the PlayerController "
            "boilerplate — a non-embodied game has no avatar, and the script "
            "is what builds one. Its content is driven by the pointer "
            "(compose emits a PointerController; see subsystems_for)")

    # HealthSystem must exist when another script references it: EnemyAI.cs.j2
    # calls SendMessage("TakeDamage"), and Collectible.cs.j2 hard-references the
    # HealthSystem type via GetComponent<HealthSystem>() for its heal pickups.
    # A project with collectibles but no enemies (e.g. a base-builder) would
    # otherwise fail with CS0246 'HealthSystem'.
    if ("enemy" in _stypes() or "collectible" in _stypes()) and "health" not in _stypes():
        schema.scripts.append(UnityScript(
            name        = "HealthSystem",
            type        = "health",
            description = "Health component with damage, heal, and death events",
        ))
        fixes.append("Enemy/Collectible script requires HealthSystem — injected health script")

    if "cinemachine" in schema.features:
        existing_ids = {p.package_id for p in schema.packages}
        if "com.unity.cinemachine" not in existing_ids:
            schema.packages.append(UnityPackage(description="cinemachine"))
            fixes.append("Cinemachine feature: added cinemachine package entry for resolver")

    if schema.story_beats and "dialogue_manager" not in _stypes():
        schema.scripts.append(UnityScript(
            name        = "DialogueManager",
            type        = "dialogue_manager",
            description = "Singleton story-beat and dialogue system",
        ))
        fixes.append("Injected DialogueManager (story_beats defined)")

    if schema.procedural_meshes and "mesh_builder" not in _stypes():
        schema.scripts.append(UnityScript(
            name        = "MeshBuilder",
            type        = "mesh_builder",
            description = "Singleton procedural mesh factory",
        ))
        fixes.append("Injected MeshBuilder (procedural_meshes defined)")

    # Always inject MaterialPalette for 3D/2.5D/top_down/isometric/VR projects
    # so palette colors are available at runtime even without an explicit schema entry
    if schema.game_type not in ("2D",) and "material_palette" not in _stypes():
        schema.scripts.insert(0, UnityScript(
            name        = "MaterialPalette",
            type        = "material_palette",
            description = f"Runtime {schema.color_palette.name if schema.color_palette else 'Stylized'} "
                           "palette manager — creates and caches materials from palette hex colors",
        ))
        fixes.append("Injected MaterialPalette (3D project)")

    # Inject ProBuilder package when level_generator script is present
    if "level_generator" in _stypes():
        existing_ids = {p.package_id for p in schema.packages}
        if "com.unity.probuilder" not in existing_ids:
            schema.packages.append(UnityPackage(
                description = "probuilder geometry editing tool",
                package_id  = "com.unity.probuilder",
            ))
            fixes.append("Injected ProBuilder package (level_generator script requires it)")

    # The LLM routinely keeps the Input System PACKAGE but drops the
    # input_system FEATURE flag (hit live: TerminalHorizon rerun) — the
    # generated project then ships activeInputHandler: 0 and Unity greets
    # every first open with the "native platform backends not enabled...
    # *RESTART* the editor" dialog. The flag also gates the InputActions
    # asset and the controller's new-Input-System code path, so reconcile:
    # package present → feature on.
    _has_input_pkg = any(
        "inputsystem" in (p.package_id or "").lower()
        or "input system" in (p.description or "").lower()
        for p in schema.packages)
    if _has_input_pkg and "input_system" not in schema.features:
        schema.features = list(schema.features) + ["input_system"]
        fixes.append("Input System package present — enabled input_system "
                     "feature (native backends + InputActions asset)")

    # Every entity must carry a model_hint — it drives the asset-catalog match
    # that decides which real model the group spawns. The local LLM routinely
    # omits optional fields (hit live: TerminalHorizon, all 6 hints empty), so
    # backfill deterministically from the entity's own name.
    _backfilled = []
    for e in schema.entities:
        if not (e.model_hint or "").strip():
            e.model_hint = _hint_from_name(e.name, e.kind)
            _backfilled.append(f"{e.name} -> '{e.model_hint}'")
    if _backfilled:
        fixes.append("Backfilled empty entity model_hint from names: "
                     + ", ".join(_backfilled))

    # "single" placement means every instance spawns at the SAME position —
    # EntitySpawner's PlaceAt default case returns Vector3.zero for every i,
    # so a group left on the schema default (placement="single") with
    # count > 1 stacks every copy at the origin (hit live: DriftArcade's
    # buildings piled on top of each other). The LLM regularly leaves
    # placement unset for multi-instance groups; a real layout is required,
    # not a suggestion — auto-upgrade deterministically by group size rather
    # than leaving a scene-breaking default in place. "path" placement is
    # untouched (already a real, explicit layout).
    _placement_fixed = []
    for e in schema.entities:
        if e.placement == "single" and e.count > 1:
            e.placement = "line" if e.count <= 8 else "grid"
            _placement_fixed.append(f"{e.name} (count={e.count}) -> '{e.placement}'")
    if _placement_fixed:
        fixes.append("Entities with count>1 left on placement='single' "
                     "(every instance would spawn stacked at one point) — "
                     "auto-upgraded: " + ", ".join(_placement_fixed))

    # Scene specs must reference real scenes and real entity groups, and their
    # descriptions deterministically imply a mood when the LLM leaves the
    # explicit fields unset ("thunderheads, lightning arcs" should LOOK like a
    # storm without anyone re-typing fog:true).
    schema, scene_fixes = _reconcile_scene_specs(schema)
    fixes.extend(scene_fixes)

    return schema, fixes


# Description keyword → scene mood. Checked in order; first time-of-day hit
# wins. Deterministic — pure string containment on the LLM-authored text.
_MOOD_TOD = [
    (("night", "midnight", "dark", "nocturnal"), "night"),
    (("sunset", "dusk", "evening", "golden hour"), "sunset"),
    (("dawn", "sunrise", "morning"), "dawn"),
]
_MOOD_FOG = ("storm", "thunder", "fog", "mist", "cloud", "haze", "smoke",
             "blizzard", "sandstorm")


# Scene-name fragments that mark a non-gameplay screen. Used to keep the
# player/weapon/enemy restore below out of menus and end screens.
_MENU_SCENE_WORDS = ("menu", "title", "splash", "credits", "options",
                     "settings", "lobby", "gameover", "game_over", "pause")

_MATCH_TOKEN_RE = re.compile(r"[A-Z][a-z0-9]*|[a-z0-9]+")
_MATCH_STOPWORDS = frozenset({"the", "a", "an", "of", "in", "on", "at", "to", "and"})


def _match_tokens(s: str) -> frozenset[str]:
    """Lowercase word tokens for comparing a mission title against a scene
    name — splits PascalCase/camelCase scene names ('HallOne' ->
    {'hall','one'}) and punctuation/space-separated titles alike ('Hall One
    Clearance' -> {'hall','one','clearance'}) with the SAME regex, dropping a
    short stoplist of filler words that would otherwise "match" anything."""
    return frozenset(t.lower() for t in _MATCH_TOKEN_RE.findall(s or "")
                     if t.lower() not in _MATCH_STOPWORDS)


def _infer_mission_scenes(schema: UnitySchema) -> tuple[dict[str, list[str]], list[str]]:
    """Best-effort, deterministic {mission.id: [scene_name]} inference.

    Nothing in the schema says which scene a mission's objectives belong to
    (`MissionSpec` has no scene field, and `SceneSpec.entities` — the only
    per-scene concept that exists — has never been cross-checked against
    `missions[].objectives[].target_ref`). Confirmed live on a 3-mission,
    4-hall metroidvania (GearCitadel): SEAM 8 (src/unity_compose.py) binds
    ONE global event source per objective TYPE project-wide, so only the
    first mission's objectives could ever advance; and the second mission's
    objective targets were scoped only to an unrelated scene, so even a
    correctly-wired mission had nothing to interact with. Both defects trace
    to this same missing fact, so it is computed once here and consumed by
    both `_reconcile_scene_specs` (below) and SEAM 8
    (`unity_compose.py`, via `schema.meta["mission_scenes"]`).

    Two-tier match, first hit wins per mission; ties abstain rather than
    guess:
      1. Name match — the mission title and the scene name share word
         tokens (case-insensitive, PascalCase-aware — "HallOne" <-> "Hall
         One Clearance" share {"hall","one"}; "BossArena" <-> "Boss Battle:
         Tinker Ghost" share {"boss"}). Accepted only when exactly one scene
         has the strict-maximum token overlap (a tie, e.g. both "HallOne"
         and "HallTwo" sharing just "hall", falls through to tier 2).
      2. Sibling-objective agreement — when no scene name matches, check
         which non-`None` `SceneSpec.entities` subset already contains the
         MOST of this mission's own objective target_refs (e.g. "Vertical
         Ascent" shares no words with "HallTwo", but its `obj_reach_gate_2`
         target `WallClingPoint` is already correctly scoped to HallTwo).
         Accepted only when exactly one scene has the strict maximum.

    A mission that matches neither tier is returned in `unresolved` and
    forced into no scene — its objectives keep today's global, ungated SEAM 8
    behavior and its targets are not restored into any scene's subset. That
    is a pure abstention, not a regression: unresolved missions behave
    exactly as they did before this function existed.
    """
    gameplay_scenes = [s for s in schema.scenes
                       if s and not any(w in s.lower() for w in _MENU_SCENE_WORDS)]
    scene_tokens = {s: _match_tokens(s) for s in gameplay_scenes}

    resolved: dict[str, list[str]] = {}
    unresolved: list[str] = []
    for mission in schema.missions:
        if not mission.objectives or not mission.id:
            continue

        title_tokens = _match_tokens(mission.title)
        name_hits = {s: len(title_tokens & toks)
                    for s, toks in scene_tokens.items() if title_tokens & toks}
        if name_hits:
            best = max(name_hits.values())
            winners = [s for s, c in name_hits.items() if c == best]
            if len(winners) == 1:
                resolved[mission.id] = winners
                continue

        target_names = {o.target_ref for o in mission.objectives if o.target_ref}
        if target_names:
            hits: dict[str, int] = {}
            for spec in schema.scene_specs:
                if spec.entities is None or spec.name not in scene_tokens:
                    continue
                overlap = target_names & set(spec.entities)
                if overlap:
                    hits[spec.name] = len(overlap)
            if hits:
                best = max(hits.values())
                winners = [s for s, c in hits.items() if c == best]
                if len(winners) == 1:
                    resolved[mission.id] = winners
                    continue

        unresolved.append(mission.id)
    return resolved, unresolved


def _objective_scene_map(schema: UnitySchema,
                         mission_scenes: "dict[str, list[str]] | None") -> dict[str, str]:
    """Flatten `{mission.id: [scene, ...]}` (from `_infer_mission_scenes`) to
    `{"<TrackerClass>.<objective.id>": scene}` — what SEAM 8 (`unity_compose.py`)
    needs to gate each objective's event-source wiring to the scene it can
    actually fire in. Keyed by the objectives-tracker class name
    (`f"{to_pascal(mission.title, 'Mission')}Objectives"`, the exact string
    `unity_compose.py`'s `subsystems_for` names that mission's tracker unit),
    not by the bare objective id alone: every mission in this codebase's own
    convention numbers its objectives "obj_1", "obj_2", ... independently, so
    a flat `{objective.id: scene}` map lets a LATER mission's "obj_2" silently
    overwrite an EARLIER mission's "obj_2" entry. Hit live (DropZone, three
    missions each with an "obj_2"): every mission's objectives collapsed onto
    the last-processed mission's scene, SEAM 8's per-scene bucketing then
    lumped all three missions' objectives into that one scene, and the
    one-source-per-type cap left every objective but the first-iterated
    `reach` permanently unbound — 3 of 6 required objectives unwinnable,
    though the underlying wiring capacity (one trigger per scene) was there.

    Refined per objective, not just per mission: a single mission can

    Refined per objective, not just per mission: a single mission can
    legitimately span more than one scene (an extraction runs from a start
    point to an end point), but `_infer_mission_scenes` only ever resolves
    ONE scene for the whole mission. Confirmed live (DeadReckoningProtocol):
    mission "Engineer Extraction" title-matched to scene "ExtractionPad", so
    BOTH its `reach` objectives — "Reach the Engineer in the depot yard" and
    "Reach the ExtractionPad with the Engineer" — were scoped to that one
    scene, and SEAM 8 (one trigger source per scene) could bind only the
    first; the second stayed permanently unadvanceable even though
    DepotYard's own SceneTrigger sat unused in the composed project.

    Each objective's OWN description is checked first, word-matched against
    scene names with the same `_match_tokens` a mission title is matched
    with — "in the depot yard" uniquely names DepotYard regardless of what
    the mission as a whole resolved to. The bar is deliberately stricter
    than the mission-title tier's "most shared words wins": a scene only
    qualifies when its ENTIRE token set is contained in the objective's
    description, not merely overlapping it. A single generic shared word
    (a "Hallway" scene and an unrelated objective that happens to mention a
    hallway) is not enough to reroute an objective away from its mission —
    only an objective that names the scene in full is. Multiple qualifying
    scenes (one a subset of another, e.g. "Hall" and "HallOne") resolve to
    the more specific (larger) one; an outright tie, or no scene named at
    all, is not a signal: fall back to the mission's own resolved scene
    exactly as before, so an objective whose description says nothing about
    scenes keeps today's behavior byte for byte. A mission resolved to more
    than one scene (not produced by `_infer_mission_scenes` today, but
    tolerated) still contributes its first scene as that fallback, rather
    than guessing."""
    if not mission_scenes:
        return {}
    gameplay_scenes = [s for s in schema.scenes
                       if s and not any(w in s.lower() for w in _MENU_SCENE_WORDS)]
    scene_tokens = {s: _match_tokens(s) for s in gameplay_scenes}

    def _own_scene(description: str) -> "str | None":
        tokens = _match_tokens(description)
        if not tokens:
            return None
        hits = {s: len(toks) for s, toks in scene_tokens.items()
               if toks and toks <= tokens}
        if not hits:
            return None
        best = max(hits.values())
        winners = [s for s, c in hits.items() if c == best]
        return winners[0] if len(winners) == 1 else None

    out: dict[str, str] = {}
    for mission in schema.missions:
        scenes = mission_scenes.get(mission.id)
        if not scenes:
            continue
        tracker = f"{to_pascal(mission.title, 'Mission')}Objectives"
        for o in mission.objectives:
            if o.id:
                out[f"{tracker}.{o.id}"] = _own_scene(o.description) or scenes[0]
    return out


def _reconcile_scene_specs(schema: UnitySchema) -> tuple[UnitySchema, list[str]]:
    fixes: list[str] = []
    if not schema.scene_specs:
        return schema, fixes

    scene_names = set(schema.scenes)
    entity_names = {e.name for e in schema.entities}
    seen: set[str] = set()
    kept = []
    dropped_scenes: list[str] = []
    for spec in schema.scene_specs:
        if spec.name not in scene_names or spec.name in seen:
            dropped_scenes.append(spec.name or "?")
            continue
        seen.add(spec.name)
        if spec.entities is not None:
            unknown = [n for n in spec.entities if n not in entity_names]
            if unknown:
                had_any = bool(spec.entities)
                spec.entities = [n for n in spec.entities if n in entity_names]
                fixes.append(f"Scene '{spec.name}': dropped unknown entity "
                             f"group(s) {unknown}")
                # An empty subset means "host NOTHING", which is a very
                # different statement from the one the LLM was making when it
                # named groups that simply do not exist under those names.
                # Fall back to None (= host every group) rather than shipping a
                # scene that silently spawns nothing.
                if had_any and not spec.entities:
                    spec.entities = None
                    fixes.append(f"Scene '{spec.name}': every named group was "
                                 f"unknown — hosting all groups instead of an "
                                 f"empty scene")
            # A GAMEPLAY scene must host the player, the player's weapon and the
            # enemies. This subset is LLM-authored and routinely drops them: on
            # VoidArena "Main" listed the player but not the gun (so nothing
            # could mount a weapon), and on the next roll it listed neither the
            # player nor the Blaster while keeping three prop groups.
            #
            # A scene counts as gameplay when it names ANY group and is not an
            # explicitly empty one (menus author `entities: []`). Deliberately
            # NOT gated on the player being listed — that was the earlier
            # version of this rule and it silently no-opped for exactly the
            # scenes that needed it most. Matched on kind, not name, so it
            # survives whatever the LLM calls the groups.
            _essential = [e.name for e in schema.entities
                          if (e.kind or "").lower() in ("player", "weapon", "enemy")]
            # ...but a MENU is not a gameplay scene. Gating purely on "names any
            # group" pulled the player and every enemy into a main menu that had
            # only authored a UI overlay group — 76 Cromulons spawning behind the
            # title screen. Menus are matched by name because that is the only
            # signal a scene spec carries about its purpose.
            _is_menu = any(w in (spec.name or "").lower() for w in _MENU_SCENE_WORDS)
            if spec.entities and _essential and not _is_menu:
                missing = [n for n in _essential if n not in spec.entities]
                if missing:
                    spec.entities = list(spec.entities) + missing
                    fixes.append(f"Scene '{spec.name}': restored player/weapon/"
                                 f"enemy group(s) {missing} — a gameplay scene "
                                 f"cannot omit them")
            # A mission's objective targets must be reachable in the scene
            # that mission is actually FOR, or the objective can never be
            # completed no matter how SEAM 8 (unity_compose.py) wires it —
            # confirmed live: HallOne's own entity subset omitted GearGate
            # and SaveCogShrine, the exact two entities Hall One Clearance's
            # objectives target, so even a correctly-wired mission had
            # nothing to interact with. `mission_scenes` is populated by
            # `_infer_mission_scenes` (best-effort, may be empty/absent —
            # this restoration is a no-op for any mission it could not
            # resolve, same abstain-rather-than-guess rule as everywhere
            # else in this function).
            _mission_scenes = (schema.meta.get("mission_scenes") or {}) \
                if isinstance(schema.meta, dict) else {}
            if spec.entities and _mission_scenes:
                for mission in schema.missions:
                    if spec.name not in (_mission_scenes.get(mission.id) or ()):
                        continue
                    targets = [o.target_ref for o in mission.objectives
                              if o.target_ref and o.target_ref in entity_names]
                    missing_targets = [n for n in targets if n not in spec.entities]
                    if missing_targets:
                        spec.entities = list(spec.entities) + missing_targets
                        fixes.append(
                            f"Scene '{spec.name}': restored objective target "
                            f"entity group(s) {missing_targets} for mission "
                            f"'{mission.title}' — an objective's target must "
                            f"be reachable where its mission runs")
        desc = (spec.description or "").lower()
        if desc and not spec.time_of_day:
            for words, tod in _MOOD_TOD:
                if any(w in desc for w in words):
                    spec.time_of_day = tod
                    fixes.append(f"Scene '{spec.name}': inferred "
                                 f"time_of_day={tod} from description")
                    break
        if desc and spec.fog is None and any(w in desc for w in _MOOD_FOG):
            spec.fog = True
            fixes.append(f"Scene '{spec.name}': inferred fog from description")
        kept.append(spec)
    if dropped_scenes:
        fixes.append("Dropped scene_specs not matching schema.scenes: "
                     + ", ".join(dropped_scenes))
    schema.scene_specs = kept
    return schema, fixes


# Appended to the Step-3 prompt on a re-roll. The GTA5 and micro pipelines have
# carried a reminder like this for as long as they have had a retry
# (src/pipeline.py, src/micro_pipeline.py); Unity was the one pipeline that
# validated the fill once and crashed the whole run on a bad roll. The list-size
# clause is built from the schema's own caps so the number the model is told is
# the number Pydantic enforces.
_SCHEMA_FILL_RETRY_REMINDER = (
    "\n\nREMINDER: Output ONE valid JSON object only — no prose, no markdown. "
    "Never repeat an entry you have already written, and keep every list inside "
    "its limit (" + render_cap_reminder() + "). Prefer few, substantial entries "
    "over many thin ones."
)


def _read_project_files_for_grading(project_root: Path) -> dict[str, str]:
    """Read a just-written project back off disk into the
    `{relative_path: content}` shape unity_playability_artifact's checks need.

    `write()` returns `(project_root, file_count)`, not the in-memory files
    dict `generate()` built — reading it back is cheaper than changing that
    signature. Binary assets (models, textures, ...) fail to decode as UTF-8
    and are skipped; none of the artifact checks read them."""
    out: dict[str, str] = {}
    for p in Path(project_root).rglob("*"):
        if not p.is_file():
            continue
        try:
            out[str(p.relative_to(project_root)).replace("\\", "/")] = \
                p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return out


# ── Pipeline class ─────────────────────────────────────────────────────────────

class UnityPipeline:
    # A local model that fumbles the fill (invalid JSON, a value the schema
    # rejects) gets one re-roll at a colder temperature before the run dies —
    # by Step 3 both LLM calls are already paid for.
    SCHEMA_FILL_ATTEMPTS = 2

    def __init__(self, config: dict):
        self.config = config
        lm_cfg   = config["lm_studio"]
        pipe_cfg = config.get("unity_pipeline", config.get("pipeline", {}))

        self.llm = make_chat_client(config)

        # Unity RAG: same local ChromaDB client as GTA5. Skipped in Basic Mode.
        use_rag = not config.get("basic_mode", False)
        unity_rag = make_rag_client(config) if use_rag else None

        self.lookups_dir = pipe_cfg.get("lookups_dir", "lookups")
        self.resolver = UnityResolver(
            lookups_dir = self.lookups_dir,
            rag         = unity_rag,
            use_rag     = use_rag and unity_rag is not None,
        )
        self.template_dir = pipe_cfg.get("unity_template_dir", "templates/unity")
        self.generator = UnityProjectGenerator(template_dir=self.template_dir)
        self.output_dir   = pipe_cfg.get("unity_output_dir", "output/unity")
        self.inject_mcp_bridge = pipe_cfg.get("inject_mcp_bridge", True)
        self.show_prompts = config.get("logging", {}).get("show_prompts", False)
        self.concept_temp = lm_cfg.get("concept_temperature", 0.85)
        self.schema_temp  = lm_cfg.get("schema_temperature", 0.2)
        # How many content-gated multimodal IR sections the Step-3 prompt may
        # carry beyond the always-on core. Each section costs ~150 tokens; drop
        # it to 1 or 0 on a model with a tight context window.
        # No cap by default. The relevance gate (a section the brief never
        # signals for is never asked for) lives in render_multimodal_block; this
        # is only a token-budget truncation on top of it, and the budget it was
        # sized for assumed an 8k window. Set it in config.yaml to re-impose one.
        self.multimodal_max_sections = int(
            pipe_cfg.get("multimodal_max_sections", 0)) or None
        # Unity package cache — see src/unity_package_cache.py.
        self.package_cache_cfg = pipe_cfg.get("package_cache", {}) or {}

    def _prime_package_cache(self, project_root, manifest_deps: dict[str, str]) -> None:
        """Warm Unity's global package cache and pin the graph in packages-lock.json.

        Without this, first open resolves ~8 registry packages over the network
        for every generated project even though the bytes never change. Both
        halves matter: the cache stops the *download*, the lock file stops the
        *resolution* that precedes it.

        Best-effort by contract. Every failure path here — no network, an
        unwritable cache, a registry 500 — leaves Unity doing exactly what it
        does today, so nothing in this method is allowed to fail a build.
        """
        cfg = self.package_cache_cfg
        if not cfg.get("enabled", True):
            return
        try:
            import json as _json

            from .unity_package_cache import (
                build_lock, ensure_cached, resolve_closure,
            )

            cache_dir = Path(cfg.get("dir", "cache/unity_packages"))
            global_root = cfg.get("global_root")
            report = ensure_cached(
                manifest_deps, cache_dir,
                global_root=Path(global_root) if global_root else None,
                # Default OFF: a build must never turn into a surprise
                # multi-gigabyte download (the closure measured 1.4 GB). Filling
                # is an explicit, one-time `unity-package-cache fill`; this hook
                # only primes what is already local.
                download_missing=bool(cfg.get("download", False)),
            )
            self._ok(f"Package cache: {report.summary()}")
            for slug, err in report.failed.items():
                self._warn(f"package cache: {slug} — {err}")

            if cfg.get("write_lock", True):
                pins = resolve_closure(manifest_deps,
                                       meta_dir=cache_dir / "_metadata")
                lock = Path(project_root) / "Packages" / "packages-lock.json"
                lock.parent.mkdir(parents=True, exist_ok=True)
                lock.write_text(_json.dumps(build_lock(pins, manifest_deps), indent=2)
                                + "\n", encoding="utf-8")
                self._ok(f"packages-lock.json pins {len(pins)} registry package(s)")
        except Exception as exc:  # noqa: BLE001 — never fail a build over a cache
            self._warn(f"package cache skipped ({type(exc).__name__}: {exc})")

    # ── console helpers ───────────────────────────────────────────────────────

    def _step(self, n: int, title: str) -> None:
        console.print(f"\n[bold cyan]STEP {n}[/] -- {title}")

    def _ok(self, msg: str)   -> None: console.print(f"  [green]OK[/]   {msg}")
    def _warn(self, msg: str) -> None: console.print(f"  [yellow]WARN[/] {msg}")
    def _fix(self, msg: str)  -> None: console.print(f"  [magenta]FIX[/]  {msg}")

    # ── main run ──────────────────────────────────────────────────────────────

    def _blender_model_overrides(self, schema, concept: str) -> dict[str, str]:
        """Build procedural Blender models for every supported catalog kind and return
        {kind: fbx_path}.  Degrades silently per kind if a build fails — the generator
        falls back to the static Model Pack / primitives for that kind.

        Catalog kinds built here: player, enemy (both humanoid), vehicle (car/plane/bike
        chosen from locomotion + entity/concept keywords).
        """
        from src.blender.locator import blender_available
        bl_path = self.config.get("blender", {}).get("blender_path") or None
        if not blender_available(bl_path):
            self._warn("Blender not found — using static Model Pack / primitives")
            return {}

        from src.micro.model import ModelPipeline
        mp = ModelPipeline(self.config)
        overrides: dict[str, str] = {}
        pname = schema.project_name

        # ── helpers ──────────────────────────────────────────────────────────
        def _entity_desc(kind: str) -> str:
            return next(
                (getattr(e, "description", "") or e.name
                 for e in schema.entities if getattr(e, "kind", "") == kind),
                "",
            )

        def _build(catalog_kind: str, variant: str, prompt: str, asset_name: str) -> None:
            # rig=True so movable models ship a real skeleton: humanoid -> Unity
            # Humanoid avatar (player/enemy animate + retarget clips); car/bike ->
            # wheel/steer bones. Skeleton-less bases just export static (graceful).
            res = mp.build_fbx(prompt or variant, variant=variant, rig=True,
                               name=asset_name, use_llm=False)
            if res and res.get("fbx"):
                overrides[catalog_kind] = res["fbx"]
                self._ok(f"Blender {catalog_kind} ({variant}): "
                         f"{res.get('tris', '?')} tris -> {res['fbx']}")
            else:
                self._warn(f"Blender {catalog_kind} ({variant}) build failed — "
                           "falling back to static pack")

        # ── humanoid: player + enemy ──────────────────────────────────────────
        _build("player", "humanoid",
               _entity_desc("player") or "player character",
               f"{pname}Player")
        _build("enemy",  "humanoid",
               _entity_desc("enemy")  or "enemy humanoid",
               f"{pname}Enemy")

        # ── vehicle: pick the right base from locomotion + content keywords ──
        loco = getattr(schema, "locomotion", "walk")
        combined = " ".join([
            loco,
            " ".join(e.name for e in schema.entities),
            " ".join(getattr(e, "description", "") or "" for e in schema.entities),
            concept,
        ]).lower()

        # "fly" alone doesn't imply a pilotable vehicle (a wingsuit/skydiving
        # game has none) — only "drive" is an unconditional vehicle signal;
        # "fly" needs corroborating vehicle words in the schema's own text.
        has_vehicle = (
            loco == "drive"
            or any(getattr(e, "kind", "") == "vehicle" for e in schema.entities)
            or any(w in combined for w in ("vehicle", "car", "jeep", "truck",
                                           "plane", "aircraft", "jet", "bike",
                                           "motorcycle", "motorbike"))
        )
        if has_vehicle:
            if any(w in combined for w in ("plane", "aircraft", "jet", "glider", "fly")):
                veh_variant = "plane"
            elif any(w in combined for w in ("bike", "motorcycle", "motorbike", "dirt bike", "chopper")):
                veh_variant = "bike"
            else:
                veh_variant = "car"
            veh_prompt = (_entity_desc("vehicle") or concept or "").strip()[:400]
            _build("vehicle", veh_variant, veh_prompt, f"{pname}Vehicle")

        return overrides

    def run(self, user_prompt: str,
            theme:       str | None = None,
            mashup_with: str | None = None,
            seed:        int | None = None,
            compose:     bool = False,
            blender_models: bool = False,
            route:       dict | None = None,
            scene:       dict | None = None,
            no_think:    bool = False,
            micro_code:  bool = True,
            no_llm:      bool = False) -> Path:
        console.print(Panel(
            f"[bold]Unity Project Creator Pipeline[/]\n{user_prompt}", style="blue"
        ))

        # Fixed seed → reproducible LLM generations (concept + schema). Same seed
        # the theme RNG uses, so one --seed makes the whole prompt→schema→assets
        # chain deterministic. None → server picks a random seed (today's default).
        if seed is not None:
            self.llm.seed = seed

        # A fully-authored structured brief IS its own theme — auto-detected
        # themes + twist seeds only pollute it (hit live twice on
        # TerminalHorizon: "superhero_flying" grafted "flight burns calories" /
        # "rescue costs altitude" onto a complete skydiving design). Explicit
        # --theme still wins.
        if theme is None and _is_structured_brief(user_prompt):
            theme = "none"
            self._ok("Structured brief detected — theme autodetect skipped "
                     "(pass --theme <name> to force one)")

        # Resolve theme (may be None → behaves exactly as before)
        active_theme, rng, theme_meta = resolve_theme(
            user_prompt = user_prompt,
            theme_name  = theme,
            mashup_with = mashup_with,
            seed        = seed,
        )
        if active_theme:
            theme_block, twist = render_concept_bias(active_theme, rng=rng)
            schema_hints       = render_schema_hints(active_theme)
            theme_meta["twist_seed"] = twist
            self._ok(f"Theme: {active_theme.display}")
            if twist:
                self._ok(f"Twist seed: {twist}")
        else:
            theme_block, schema_hints = "", ""

        # Deterministic intent detection — game_type / locomotion / genre from
        # keyword scoring, same weighting archetypes.py uses for GTA5 missions.
        # No LLM involved; runs once against the raw prompt.
        intent = classify_intent(user_prompt)
        theme_block  += intent.concept_hint()
        schema_hints += intent.structure_hint()
        # Asset-library vocabulary (categories + top tags from the generated
        # catalog) so entity model_hints name models that actually exist.
        # Deterministic lookup; missing catalog → empty string, prompt unchanged.
        _vocab = asset_vocabulary(self.lookups_dir)
        if _vocab:
            schema_hints += "\n" + _vocab + "\n"
        # The brief's own ENTITIES bullets, verbatim, as a required checklist.
        # Step 3 is otherwise handed ONLY the Step-2 concept prose, which has
        # already paraphrased the brief's explicit entity names away — a brief
        # that spells out its entities had no way to get them in front of the
        # schema-fill model at all (see render_entity_checklist).
        _checklist = render_entity_checklist(user_prompt)
        if _checklist:
            schema_hints += "\n" + _checklist + "\n"
            self._ok(
                f"Brief names {len(extract_named_entities(user_prompt))} "
                "entities explicitly — passed to schema-fill as a required "
                "checklist"
            )
        # Same treatment for the brief's own SCRIPTS bullets. Entities had a
        # checklist and scripts did not, so a brief could require a system
        # explicitly ("each is required; implement as its own module") and the
        # schema-fill model would never see the demand — 7 of 12 systems went
        # missing on the grandma run (see render_script_checklist).
        _script_checklist = render_script_checklist(user_prompt)
        if _script_checklist:
            schema_hints += "\n" + _script_checklist + "\n"
            self._ok(
                f"Brief names {len(extract_named_scripts(user_prompt))} "
                "scripts explicitly — passed to schema-fill as a required "
                "checklist"
            )
        # Same treatment for the brief's own SCENES/AUDIO/NARRATIVE/TITLE —
        # Solution #3 (docs/COMPILER_AUDIT.md, Gap B): the A/B study on
        # coldwater_circuit.txt measured these four sections losing 4/5 scene
        # names, 7/10 audio cues, 3/6 narrative beats and the project title to
        # invention, the same recall gap entities/scripts had before their own
        # checklists shipped.
        _scene_checklist = render_scene_checklist(user_prompt)
        if _scene_checklist:
            schema_hints += "\n" + _scene_checklist + "\n"
            self._ok("Brief names scenes explicitly — passed to schema-fill "
                     "as a required checklist")
        _audio_checklist = render_audio_checklist(user_prompt)
        if _audio_checklist:
            schema_hints += "\n" + _audio_checklist + "\n"
            self._ok("Brief names audio cues explicitly — passed to "
                     "schema-fill as a required checklist")
        _narrative_checklist = render_narrative_checklist(user_prompt)
        if _narrative_checklist:
            schema_hints += "\n" + _narrative_checklist + "\n"
            self._ok("Brief numbers narrative beats explicitly — passed to "
                     "schema-fill as a required checklist")
        _title_checklist = render_title_checklist(user_prompt)
        if _title_checklist:
            schema_hints += "\n" + _title_checklist + "\n"
            self._ok("Brief declares an explicit title — passed to "
                     "schema-fill as a required directive")
        if intent.matched:
            # Not every intent.matched key is a top-level UnityIntent field —
            # some (locomotion_suppressed, locomotion_modes_switch_signal,
            # locomotion_modes:<kind>) are sub-detail keys a caller reads
            # directly (structure_hint/apply_intent), not a value this
            # one-line summary can show. AttributeError on any of those
            # crashed this line outright — hit live once
            # `_score_multi_mode` started adding its own sub-detail keys.
            self._ok(
                "Intent: "
                + "  ".join(f"{axis}={getattr(intent, axis)!r}" for axis in intent.matched
                            if hasattr(intent, axis))
            )

        # Spatial facts: the brief's own numeric distances, extracted here —
        # after the brief is final, BEFORE any model sees it — and carried past
        # schema-fill as a sidecar (src/unity_spatial_facts.py). Deterministic
        # regex + unit normalization, no LLM. Live: a brief saying "the summit
        # is a nerve cluster 900m up" produced a schema with no height in it at
        # all, and the layout built its 60 m default wall. The number was never
        # missing from the brief — it just had no way past Step 3.
        _spatial_facts = extract_spatial_facts(user_prompt)
        if _spatial_facts:
            self._ok(
                f"Brief states {len(_spatial_facts)} spatial fact(s) — "
                + ", ".join(f"{f.value_m:g} m ({f.referent_text!r})"
                            for f in _spatial_facts[:3])
                + (" …" if len(_spatial_facts) > 3 else "")
            )

        # STEPS 2-3, offline ──────────────────────────────────────────────────
        # --no-llm: the brief's own sections ARE the schema. Steps 2 and 3 are
        # replaced by a deterministic parse (src/unity_briefparse.py) and no
        # model is contacted anywhere in the run. This is the control arm for
        # measuring what the Pipeline LLM contributes — NOT a test of Step 3,
        # which does not run. See the module docstring on Eval Discipline.
        schema: UnitySchema | None = None
        concept = ""
        if no_llm:
            self._step(2, "Deterministic brief parse (--no-llm — Steps 2-3 offline)")
            from .unity_briefparse import parse_brief
            schema, parse_msgs = parse_brief(user_prompt)
            for msg in parse_msgs:
                self._fix(msg)
            self._step(3, "Schema filled from the brief (no model contacted)")
            # Steps 4-7 below are shared verbatim with the LLM path — that is
            # the point of the control arm. Only the SOURCE of the schema
            # differs; the compiler that consumes it must not.

        else:
            # STEP 2 — concept ────────────────────────────────────────────────
            self._step(2, "LLM generates project concept")
            if no_think:
                self._ok("--no-think: reasoning disabled for the whole run "
                         "(concept quality/creativity trades for speed)")
            concept = self.llm.chat(
                system      = UNITY_CONCEPT_SYSTEM,
                user        = UNITY_CONCEPT_USER.format(user_prompt=user_prompt, theme_block=theme_block),
                temperature = self.concept_temp,
                thinking    = not no_think,
            )
            self._ok(f"{len(concept)} chars of project concept generated")
            if self.show_prompts:
                console.print(concept)

            # STEP 3 — fill schema ────────────────────────────────────────────
            self._step(3, "LLM fills project schema")
            # Multimodal IR ask: core sections always, optional ones gated on
            # the concept's own content so the prompt stays inside a local
            # model's context window (src/unity_prompts.py).
            mm = render_multimodal_block(concept, user_prompt,
                                         max_optional=self.multimodal_max_sections)
            self._ok("Multimodal IR sections requested: " + ", ".join(mm.sections))
            schema_user = UNITY_SCHEMA_FILL_USER.format(concept=concept,
                                                        schema_hints=schema_hints,
                                                        multimodal_block=mm.text,
                                                        brief_block=brief_block(user_prompt))
            # In --compose mode the micros + GameContext locator exist, so steer
            # the LLM to reach subsystems via GameContext.Instance.Get<T>()
            # instead of inventing singletons (which the fixer strips).
            if compose:
                schema_user += GAMECONTEXT_SCHEMA_GUIDANCE
            # One re-roll on a bad fill, and a deterministic clamp before
            # validation so a runaway list costs its own overflow instead of the
            # whole run (src/unity_schema_clamp.py — live: 155 scripts against a
            # cap of 24).
            # response_format on this call only (never concept generation,
            # which expects prose) — off by default. `structured_output_mode`
            # is the tri-state successor to the legacy `structured_output`
            # bool: "off" | "json_object" (syntax only, today's shipped
            # fix) | "json_schema" (real shape-constrained decoding, see
            # unity_schema_jsonschema.py). Absent -> fall back to the legacy
            # bool unchanged, so nobody's existing config silently changes
            # behavior. An unrecognized mode string aborts loudly rather than
            # silently picking "off" — see CLAUDE.md's abstain-loudly rule.
            _lm_cfg = self.config.get("lm_studio", {})
            _mode = _lm_cfg.get("structured_output_mode")
            if _mode is None:
                _mode = "json_object" if _lm_cfg.get("structured_output", False) else "off"
            if _mode not in ("off", "json_object", "json_schema"):
                raise ValueError(
                    f"lm_studio.structured_output_mode must be one of "
                    f"'off', 'json_object', 'json_schema' — got {_mode!r}"
                )
            _json_mode   = _mode == "json_object"
            _json_schema = response_format_schema()["json_schema"] if _mode == "json_schema" else None
            clamp_msgs: list[str] = []
            last_err: Exception | None = None
            for attempt in range(self.SCHEMA_FILL_ATTEMPTS):
                user_msg = schema_user
                if attempt > 0:
                    user_msg += _SCHEMA_FILL_RETRY_REMINDER
                try:
                    schema_raw = self.llm.chat(
                        system      = UNITY_SCHEMA_FILL_SYSTEM,
                        user        = user_msg,
                        temperature = (self.schema_temp if attempt == 0
                                       else min(self.schema_temp, 0.1)),
                        thinking    = False,
                        json_mode   = _json_mode,
                        json_schema = _json_schema,
                    )
                    schema_dict = self.llm.extract_json(schema_raw)
                    clamp_msgs  = clamp_capped_lists(schema_dict)
                    schema      = UnitySchema.model_validate(schema_dict)
                    break
                except (ValueError, ValidationError) as exc:
                    last_err = exc
                    if attempt + 1 < self.SCHEMA_FILL_ATTEMPTS:
                        # The exception TYPE alone says nothing actionable —
                        # a truncated JSON, an empty response and a field-level
                        # validation error all print "ValueError" and each has
                        # a different remedy (raise the loaded context, check
                        # the model is loaded, read the offending field). Carry
                        # the reason.
                        self._warn(f"schema fill attempt {attempt + 1} failed "
                                   f"({type(exc).__name__}: "
                                   f"{str(exc).splitlines()[0][:200]}); "
                                   f"retrying colder")
            if schema is None:
                raise RuntimeError(
                    f"schema fill failed after {self.SCHEMA_FILL_ATTEMPTS} "
                    f"attempts: {last_err}"
                ) from last_err
            for msg in clamp_msgs:
                self._fix(msg)

        # Shared tail of Steps 2-3 — runs for both fills. The offline fill has
        # already written meta.user_prompt/concept/fill; re-writing them here
        # keeps one source of truth for the theme + spatial-fact stamps.
        schema.meta = {**schema.meta, **theme_meta}
        # Persist the LLM concept so the Library page can describe the project.
        # Empty string under --no-llm: no concept step ran, and every consumer
        # that gates on a concept must see that and skip rather than be handed
        # a synthesised one.
        schema.meta["concept"]     = concept
        schema.meta["user_prompt"] = user_prompt
        schema.meta["fill"]        = "deterministic" if no_llm else "llm"
        # The brief's spatial facts ride alongside from here on. Written LAST
        # and unconditionally, so a model that decided to author its own
        # `meta.spatial_facts` cannot get one past the extraction pass. Step 5's
        # dimension resolution is the only consumer
        # (unity_layout_archetype._schema_height).
        attach_spatial_facts(schema, _spatial_facts)

        # Deterministic override: if the classifier had a confident direct hit
        # on the raw prompt and the LLM's own game_type/locomotion disagree,
        # trust the keyword match over the LLM. genre is always stamped into
        # schema.meta (default "universal") regardless of confidence.
        for msg in apply_intent(schema, intent):
            self._fix(msg)

        self._ok(
            f"Game: {schema.game_type}  Pipeline: {schema.render_pipeline}  "
            f"Platform: {schema.platform}  Version: {schema.unity_version}"
        )
        self._ok(
            f"{len(schema.packages)} packages  {len(schema.scripts)} scripts  "
            f"{len(schema.scenes)} scenes  {len(schema.features)} features"
        )

        # STEP 4 — resolve packages + inject required ─────────────────────────
        self._step(4, "Resolver maps packages to Unity Package Manager IDs")
        schema = self.resolver.resolve_schema(schema)
        schema = self.resolver.inject_required_packages(schema)
        unresolved = schema.unresolved_packages()
        if unresolved:
            self._warn(f"Still unresolved: {unresolved}")
        else:
            self._ok(f"All {len(schema.packages)} packages resolved")

        # STEP 5 — validate + fix ─────────────────────────────────────────────
        self._step(5, "Programmatic fixer + consistency check")
        schema = _programmatic_fix(schema, compose=compose)
        self._ok("Programmatic fixes applied (naming, scenes, script names)")

        schema, fix_msgs = _check_consistency(schema)
        for msg in fix_msgs:
            self._fix(msg)
        if not fix_msgs:
            self._ok("Schema is consistent — no fixes needed")

        # Concept-named entity rescue: a structured brief's own "ENTITIES"
        # bullets are the ground truth for what the scene needs — restore any
        # the schema-fill LLM dropped/replaced with its own invented content
        # before anything downstream (racing scaffold gate, sketchfab query
        # enrichment, asset resolution) runs against schema.entities.
        _n_entities_before_rescue = len(schema.entities)
        for msg in inject_missing_concept_entities(schema):
            self._fix(msg)

        # Concept-named SCENE rescue — Solution #3's sibling for the world's
        # own places. Runs before the reconcile pass below so that pass also
        # validates the newly-restored specs, not just the LLM's original
        # ones. Restored specs never reference an unknown entity name (they
        # always host every group), so this can never be dropped by that pass
        # itself — it only ever adds scenes to what the pass reconciles.
        for msg in inject_missing_concept_scenes(schema):
            self._fix(msg)

        # Re-reconcile the scene subsets: the rescue above ADDS entities (the
        # brief's own player/weapon/enemy groups) that did not exist when
        # _check_consistency ran, so any scene declaring an entity subset still
        # lists only the LLM's original groups and the restored ones would never
        # spawn. Live: the brief's Player, Blaster and Cromulon were all rescued
        # into the schema and then filtered straight back out of the only
        # gameplay scene. No-op when the rescue added nothing.
        if len(schema.entities) != _n_entities_before_rescue:
            schema, _rescue_scene_fixes = _reconcile_scene_specs(schema)
            for msg in _rescue_scene_fixes:
                self._fix(msg)

        # Ramp tilt rescue: an entity the LLM clearly intends as a ramp (name
        # or model_hint mentions "ramp") but left tilt_deg at its 0 default —
        # geometry a schema-fill step has no reason to reason correctly about,
        # not creative content. Positive tilt (rises left-to-right) by default;
        # alternates direction so back-to-back ramps in one row don't all lean
        # the same way. Never overrides an explicit non-zero tilt the LLM (or
        # the concept-entity rescue) already set.
        for msg in _apply_ramp_tilts(schema):
            self._fix(msg)

        # Concept-named SCRIPT rescue — the entity rescue's sibling, for the
        # brief's systems. Runs here, before Step 6, so the restored scripts are
        # generated, composed and wired exactly like the ones the LLM wrote.
        # Deliberately after the entity rescue: a restored script's description
        # names the entities it drives, and both halves of the brief should be
        # whole before the generator reads either.
        for msg in inject_missing_concept_scripts(schema):
            self._fix(msg)

        # Concept-named AUDIO CUE rescue — Solution #3's sibling for the
        # game's sound identity (not its meaning — see
        # unity_concept_audio.py's own docstring on that distinction).
        for msg in inject_missing_concept_audio(schema):
            self._fix(msg)

        # Concept-named NARRATIVE rescue — Solution #3's sibling for the
        # story's own progression. Deliberately before the mission rescue
        # below: a restored beat should be visible to whatever downstream
        # derives missions from story_beats (Step 5c,
        # src/unity_playability_repair.py), not appear one pass too late.
        for msg in inject_missing_concept_narrative(schema):
            self._fix(msg)

        # Brief-declared MISSION rescue — the same sibling, one level up: the
        # entity rescue restores the CAST the schema-fill dropped, this restores
        # the PROGRESSION it collapsed. Three consecutive live runs of a brief
        # laying out three ordered phases each came back with one mission, and
        # everything downstream reads missions plural — the rules micro states
        # every phase as a step, the board derives its verbs and ordering across
        # all of them, SEAM 9 sums one readout over every tracker.
        #
        # After the entity rescue on purpose: a restored objective's target_ref
        # names an entity, and `resolve_objective_targets` (further down, after
        # every pass that can still add one) resolves the ones the brief left
        # bare — so the cast must be whole before either runs.
        for msg in inject_missing_concept_missions(schema):
            self._fix(msg)

        # Concept-named TITLE rescue — Solution #3's last sibling, and the odd
        # one out: a scalar override-if-wrong rather than a list append (see
        # unity_concept_title.py's own docstring on the two confidence tiers
        # that guard against overwriting a good LLM title with a bad guess).
        # Independent of everything else above; placed last.
        for msg in inject_missing_concept_title(schema):
            self._fix(msg)

        # Racing-genre scaffold: a racing project whose entities carry no real
        # vehicle/track content (the LLM wrote a themed prop scatter instead
        # of an actual circuit) gets a deterministic closed-circuit injected —
        # mirrors GTA5's archetypes.apply_scaffold "trust non-degenerate LLM
        # output, rescue degenerate output" contract. 3D-only: EntitySpawner's
        # path placement (which the circuit is built from) has no 2D/2d_topdown
        # equivalent. An explicit --route (a unity-path route file: authored
        # text points or a traced image) overrides the degeneracy gate — the
        # user asked for THIS course, so the circuit is composed around it
        # regardless of what the LLM authored.
        _route_pts = (route or {}).get("path_points") or None
        if _route_pts and schema.game_type not in ("2D", "2d_topdown"):
            for msg in compose_racetrack(
                    schema, centerline=_route_pts,
                    track_width=float((route or {}).get("path_width") or 0.0)):
                self._fix(msg)
        elif (schema.meta.get("genre") == "racing"
                and schema.game_type not in ("2D", "2d_topdown")
                and is_racing_degenerate(schema)):
            for msg in compose_racetrack(schema):
                self._fix(msg)

        # P1 normalization: rigid-transform the whole scene so the primary
        # path route's first point (P1 — the start/finish line by convention)
        # sits at the world origin heading +Z (src/unity_pathnorm.py). Runs
        # after the racetrack scaffold (so injected circuits are normalized
        # too) and before the layout pass (so packing and terrain sizing see
        # normalized coordinates).
        for msg in normalize_paths(schema):
            self._fix(msg)

        # Authored scene: a Scene Compiler layout (`unity-create --scene`) is
        # the author's own hand on the model choice AND the placement, so it
        # binds here — after P1 normalization, so the coordinates they arranged
        # land in world space untransformed, and before the layout pass, which
        # then treats every authored group as PREPLACED (packed around, never
        # repacked). Wiring is untouched: only models and transforms come from
        # the layout. See src/unity_scenebind.py.
        if scene:
            _n_before_scene = len(schema.entities)
            _scenebind_cfg = self.config.get("scenebind", {}) or {}
            for msg in apply_scene(schema, scene, cfg=_scenebind_cfg):
                self._fix(msg)
            # Injected authored entities did not exist when the scene subsets
            # were reconciled, so a schema whose scenes declare explicit entity
            # lists would filter them straight back out (same failure the
            # concept-entity rescue hit). No-op when nothing was injected.
            if len(schema.entities) != _n_before_scene:
                schema, _scene_fixes = _reconcile_scene_specs(schema)
                for msg in _scene_fixes:
                    self._fix(msg)

        # Layout pass: select a spatial archetype for the scene, then lay every
        # {single,line,ring,grid,scatter} entity group out inside it and bake
        # exact per-instance positions (src/unity_layout.py) — runs after the
        # racetrack scaffold so any injected vehicle groups get packed too, and
        # after concept entity rescue/racing scaffold so it sees the FINAL
        # entity list (which the archetype selection reads).
        # Raises [layout.strict] (LayoutStrictError, a ValueError) when no
        # Stage-1 primitive covers the schema's motion axis and medium —
        # deliberately uncaught: flattening a drop corridor onto a ground plane
        # is the failure this selection exists to stop.
        for msg in compute_layout(schema):
            self._fix(msg)

        # Cover-point sidecar (Step 5d, src/unity_cover_points.py) — bakes
        # candidate cover points for enemy entities from BLOCKING_KINDS
        # obstacle footprints. Must run AFTER compute_layout, which is what
        # bakes instance_positions for both the obstacles this scans and the
        # enemies it computes points for; BEFORE compose (below), which reads
        # schema.meta["cover_points"] to give the cover_shooter AI variant
        # real geometry instead of naive distance math.
        for msg in compute_cover_points(schema):
            self._fix(msg)

        # Capability graph: schema.capabilities → atoms (Step 5b)
        schema, cap_msgs = resolve_capabilities(schema)
        for msg in cap_msgs:
            self._fix(msg)
        if not cap_msgs and schema.capabilities:
            self._ok("Capabilities present but produced no scene-init atoms "
                     "(all sub-models None)")

        # Microgens: schema.microgens[].atoms → atoms (Step 5b)
        schema, mg_msgs = resolve_microgens(schema)
        for msg in mg_msgs:
            self._fix(msg)

        # Objective targets: fill every blank `target_ref` the objective's own
        # sentence answers (src/unity_objective_targets.py). Last of the Step 5
        # content passes, because it resolves against schema.entities and every
        # pass above can still ADD one — the concept rescue, the racing
        # scaffold and an authored scene all inject groups an objective may
        # well be about. Before the pre-flight below, so the repaired refs are
        # what gets graded, and before compose, where SEAM 8a and the board's
        # own `commands`/`requires` derivations all read the field.
        for msg in resolve_objective_targets(schema):
            self._fix(msg)

        # Escort -> Follow atom (src/unity_escort_follow.py). AFTER
        # resolve_objective_targets, whose fill this pass depends on to find
        # the entity an escort objective actually names. BEFORE compose,
        # where SEAM 8's WireTriggerToEscortObjective reads the entity.atoms/
        # .behavior this pass sets.
        for msg in resolve_escort_follow(schema):
            self._fix(msg)

        # Mission -> scene inference (src/unity_pipeline.py:_infer_mission_scenes).
        # Deliberately AFTER resolve_objective_targets: the inference's tier-2
        # sibling-objective-agreement match reads target_ref, so it needs every
        # objective's target filled in first. Deliberately BEFORE the
        # playability pre-flight below, so that check grades the schema with
        # objective targets already reachable in their mission's scene, and
        # BEFORE compose (further down), which reads schema.meta["mission_scenes"]
        # to scope SEAM 8's event-source wiring per scene instead of binding
        # one global trigger/interactable to only the first mission project-wide.
        _mission_scenes, _unmapped_missions = _infer_mission_scenes(schema)
        if isinstance(schema.meta, dict):
            schema.meta["mission_scenes"] = _mission_scenes
        if _unmapped_missions:
            _titles = {m.id: m.title for m in schema.missions}
            self._warn(
                "mission_scenes: could not infer a scene for mission(s) "
                + ", ".join(_titles.get(mid, mid) for mid in _unmapped_missions)
                + " — their objectives keep today's global (ungated) SEAM 8 "
                  "binding and their targets are not restored into any scene")
        if _mission_scenes:
            schema, _mission_scene_fixes = _reconcile_scene_specs(schema)
            for msg in _mission_scene_fixes:
                self._fix(msg)

        # Step 5c — playability pre-flight. Step 7 grades the finished project,
        # which is too late to change it: a build the evaluator knows cannot be
        # completed still shipped, with a score next to it. This grades the
        # schema BEFORE the generator runs and applies the deterministic
        # remedies the findings carry, so the repair reaches the built project.
        # Deterministic and LLM-free like the rest of Steps 4-7; a remedy exists
        # only where authored content already answers the question (see
        # src/unity_playability_repair.py).
        _pre_report, _repairs = repair_and_regrade(schema)
        for msg in _repairs:
            self._fix(msg)

        # Re-resolve anything that consistency check added
        schema = self.resolver.resolve_schema(schema)
        manifest_deps = self.resolver.build_manifest_deps(
            schema, inject_mcp_bridge=self.inject_mcp_bridge
        )
        self._ok(f"manifest.json will contain {len(manifest_deps)} packages")

        # STEP 6 — generate ───────────────────────────────────────────────────
        self._step(6, "Generator compiles schema -> Unity project folder")
        # Blender model fulfilment: build real models from the story and feed them
        # into the Model Pack flow keyed by kind (replaces the static FBX/primitive).
        model_overrides: dict[str, str] = {}
        if blender_models:
            model_overrides = self._blender_model_overrides(schema, concept)
        # Step 6.5 — enrich entity sketchfab_query while the project LLM is
        # STILL WARM (mirrors the audio concept-translator's placement, and the
        # NPC dialog pre-extraction below: it MUST run before any Blender/
        # Chromium-heavy phase cools the LLM). Deliberately placed ahead of the
        # `if compose:` block below — the bulk Sketchfab collection nested in
        # there runs 10-15 rounds of search+download+Blender-convert and
        # previously ran BEFORE this step; by the time translate_concept ran,
        # the local model had gone cold (LM Studio idle-unload), llm.chat()
        # failed/timed out, translate_concept's try/except swallowed it, every
        # entity kept an empty sketchfab_query, and per-entity Tier S had
        # nothing to match on — silently falling through to Kenney. Runs
        # regardless of `compose`/`subsystems`: the concept-translated query
        # also drives the LOCAL catalog tiers (see _query_weights), so entity
        # -> model matching is concept-aware even when Sketchfab is off or fails.
        # --no-llm skips this: the enrichment is a model call, and its
        # deterministic fallback (model_hint / entity name, both already parsed
        # from the brief) is exactly what the offline arm is meant to exercise.
        if schema.entities and not no_llm:
            try:
                from .unity_sketchfab_translator import translate_concept
                _llm_seed = getattr(self.llm, "seed", None)
                _seed = int(_llm_seed) if _llm_seed is not None else (seed or 0)
                n = translate_concept(concept, schema.entities, self.llm, _seed)
                if n:
                    self._ok(f"sketchfab_query enriched for {n}/{len(schema.entities)} entities")
                else:
                    self._warn("Sketchfab query translation produced nothing — "
                              "resolver falls back to model_hint / entity name")
            except Exception as exc:  # noqa: BLE001
                self._warn(f"Sketchfab query translation failed "
                          f"({type(exc).__name__}: {exc}) — mechanical fallback used")
        # Composition: assemble the micro library into the project and wire each
        # micro into the scene as a _GameSystems component (deterministic, no LLM).
        # Computed BEFORE Step 3.5a (below) so script enrichment can show the
        # writer which composed-micro class names will actually exist to
        # GameContext.Instance.Get<T>() — subsystems_for is a pure function of
        # the schema, so moving the call earlier changes nothing about what it
        # returns, only when. composer.plan()/emit() still happen further down,
        # after Step 3.5's subsystem enrichment.
        composed_units = None
        subsystems = None
        # No `composer = None` pre-declaration: it is only ever referenced
        # inside `if compose:`-guarded code below (never unconditionally), and
        # giving it an Optional type here would make mypy lose the narrowing
        # this function's later, separately-gated `if compose ...:` blocks
        # (composer.plan/module_deps/emit/wiring_report) rely on.
        if compose:
            from .unity_compose import (MicroComposer, subsystems_for,
                                        drop_owned_scripts, refresh_cover_points)
            composer  = MicroComposer(schema.project_name,
                                      template_dir=self.template_dir,
                                      project_config=self.config)
            # Ensure audio is always composed — it's a core game system.
            if "audio" not in (schema.features or []):
                schema.features = list(schema.features or []) + ["audio"]
                self._fix("compose: injected 'audio' feature for SA3 clip generation")
            subsystems = subsystems_for(schema.game_type, schema.features,
                                        locomotion=schema.locomotion,
                                        locomotion_modes=schema.locomotion_modes,
                                        flying_abilities=schema.meta.get("flying_abilities"),
                                        capabilities=schema.capabilities,
                                        user_prompt=user_prompt,
                                        scenes=schema.scenes,
                                        entities=schema.entities,
                                        scripts=schema.scripts,
                                        genre=schema.meta.get("genre", "universal"),
                                        audio_specs=schema.audio_specs,
                                        vfx_specs=schema.vfx,
                                        shaders=schema.shaders,
                                        animations=schema.animations,
                                        characters=schema.characters,
                                        missions=schema.missions,
                                        behavior_trees=schema.behavior_trees,
                                        environment=schema.environment,
                                        visual_style=schema.visual_style,
                                        palette_rgb=_compose_palette_rgb(schema),
                                        cover_points=schema.meta.get("cover_points"))

        # STEP 3.5a — the same code-authoring step, for schema.scripts.
        #
        # OUTSIDE the compose-gated ENRICHMENT on purpose: a `type: custom`
        # script is generated whether or not micros are composed, and its
        # template (templates/unity/scripts/CustomBehaviour.cs.j2) is a bare
        # shell that renders extra_fields and the extra_* bodies and declares
        # nothing else. With no supplier for those bodies it emits a class of
        # fields with no behaviour and attaches it to a live GameObject —
        # measured on CodeBranchSim as 14 authored method blocks across the
        # micros and 0 across its four custom scripts, because Step 3.5 only
        # ever walked subsystems. Inside the warm window, ahead of the
        # Blender/Chromium phases, for the same reason every other llm.chat()
        # step here is. `subsystems` (computed just above, () when compose is
        # off) lets the writer's contract name the real composed-micro classes
        # reachable via GameContext instead of staying silent about them.
        if schema.scripts and micro_code and not no_llm:
            from .unity_micro_code import enrich_scripts
            for msg in enrich_scripts(schema.scripts, llm=self.llm,
                                      concept=concept, user_prompt=user_prompt,
                                      subsystems=subsystems or ()):
                self._fix(msg)

        if compose:
            # mypy can't carry `subsystems`'s narrowing from the first
            # `if compose:` block above across the intervening Step 3.5a
            # block — both are gated on the same unchanged `compose`, so
            # subsystems is always set by the time execution reaches here.
            assert subsystems is not None
            # STEP 3.5 — LLM authors prompt-specific C# per subsystem. Runs
            # BEFORE plan(): plan/emit must see identical options or their
            # class names and GUIDs would disagree. Only the extras vocabulary
            # is writable (see unity_micro_code.CODE_KEYS), so the scene wiring
            # stays deterministic while the bodies gain concept-specific
            # content. Placed here — inside the warm window, ahead of the bulk
            # Sketchfab/Blender phase below — for the same reason the NPC
            # dialog pre-extraction is (LM Studio idle-unload would otherwise
            # turn every call into a swallowed failure).
            from .unity_micro_code import (enrich_subsystems, merge_authored_code,
                                           sync_microgens_from_subsystems,
                                           sanitize_subsystem_composed_calls,
                                           sanitize_subsystem_invented_constructors,
                                           sanitize_subsystem_builtin_call_arity)
            if micro_code and not no_llm:
                # The tags TagManager will actually define. Handed to the author
                # because GameObject.FindGameObjectWithTag THROWS on an undefined
                # tag — live on CodeVault, an authored "FolderContainer" (the real
                # tag is "Folder") crashed in Awake before anything could run.
                _tags = sorted({(e.tag or "").strip() for e in schema.entities
                                if (e.tag or "").strip()
                                and (e.tag or "").strip() != "Untagged"})
                for msg in enrich_subsystems(subsystems, llm=self.llm,
                                             concept=concept,
                                             user_prompt=user_prompt,
                                             tags=_tags):
                    self._fix(msg)
            elif no_llm:
                self._ok("--no-llm: micros keep their deterministic baseline "
                         "bodies (Step 3.5 code enrichment skipped)")
            # Fallback: schema.microgens[].input is already LLM-authored, so it
            # costs nothing and covers whatever enrichment left empty (disabled,
            # failed, or nothing usable returned for that kind).
            for msg in merge_authored_code(subsystems, schema.microgens):
                self._fix(msg)

            # plan() only reads Subsystem.options (never schema.microgens), so it
            # can run here, ahead of the sync below, giving composed_units to the
            # sanitizer that must run BEFORE the sync — otherwise the persisted
            # schema.microgens[].input would disagree with what actually got
            # rendered, and a later no-LLM `unity-validate --compose` recompile
            # would reintroduce the very calls this pass strips.
            composed_units = composer.plan(subsystems)
            # Subsystems get the same composed-sibling-class member validation
            # schema.scripts (`type: custom`) already has via
            # _sanitize_composed_component_calls — a subsystem's extra_methods/
            # awake_extra/etc. can reach another composed micro via
            # GameContext.Instance.Get<T>() exactly like a custom script can, and
            # nothing about that path was previously checked against the
            # composed class's verified public API (src/unity_generator.py
            # _COMPOSED_CLASS_API).
            for msg in sanitize_subsystem_composed_calls(subsystems, composed_units):
                self._fix(msg)
            # Same isolation gap as above, for a type the subsystem invented
            # outright (`new ShaderPass(...)`) rather than a bad member on a
            # real composed class — see _sanitize_invented_constructor_calls.
            for msg in sanitize_subsystem_invented_constructors(subsystems, composed_units):
                self._fix(msg)
            # Same isolation gap, for GetComponent-family calls made with an
            # argument count their real overloads don't have.
            for msg in sanitize_subsystem_builtin_call_arity(subsystems):
                self._fix(msg)
            # See sync_microgens_from_subsystems' own docstring: this is what lets
            # a later no-LLM `unity-validate --compose` recover this run's
            # enrich_subsystems output instead of regressing to baseline bodies.
            for msg in sync_microgens_from_subsystems(subsystems, schema.microgens):
                self._fix(msg)

            # Composer is the source of truth for covered subsystems — drop the
            # duplicate built-in scripts so the project isn't double-wired. Keep
            # only the unique glue (GameManager, mesh/material, editor scripts).
            before = len(schema.scripts)
            schema.scripts = drop_owned_scripts(schema.scripts, composed_units)
            if before != len(schema.scripts):
                self._fix(f"composition owns {before - len(schema.scripts)} subsystem(s) — "
                          "dropped duplicate built-in/name-colliding scripts")
            # Step 7's avatar check (unity_playability._controllers) reads only
            # schema.scripts.type — exactly the field drop_owned_scripts just
            # erased a type="player" entry from, whenever compose builds its own
            # controller micro. Left unstamped, EVERY compose'd project whose
            # schema-fill didn't also tag an entity Player would hard-fail the
            # playability gate as "no player script", despite a real controller
            # existing on disk. Only true when compose actually built the
            # PLAYER's controller (target="player") — a ui_first PointerController
            # (target="systems", locomotion='none') must never satisfy this, or a
            # declared-non-embodied game would be graded as if it had an avatar.
            schema.meta["composed_player_controller"] = any(
                u.kind == "controller" and u.target == "player" for u in composed_units)
            # Contribute the micros' module deps (UI/UGUI/AI/...) so the composed
            # project compiles.
            manifest_deps = {**manifest_deps, **composer.module_deps(composed_units)}
            # Composed input/controller micros always `using UnityEngine.InputSystem`
            # (see controller.py _TEMPLATE_USINGS + the input micro), so the project
            # needs com.unity.inputsystem or it won't compile (CS0234/CS0246). Its
            # version is Unity-version-specific, so module_deps (versionless) can't
            # carry it — resolve it here through the version-aware package table.
            if any(u.kind in ("input", "controller") for u in composed_units):
                _pkg_id, _pkg_ver = self.resolver.resolve_package(
                    "input system", schema.unity_version)
                if _pkg_id and _pkg_id not in manifest_deps:
                    manifest_deps[_pkg_id] = _pkg_ver or "1.8.2"
                    self._fix(f"compose: added {_pkg_id} {manifest_deps[_pkg_id]} "
                              "(input/controller micros use UnityEngine.InputSystem)")
            self._ok(f"Composing {len(composed_units)} micro subsystems: "
                     + ", ".join(f"{u.kind}/{u.variant}" for u in composed_units))
            # Step 6.5 (cont.) — NPC dialog pre-extraction. Sibling of the
            # sketchfab_query enrichment above (same warm-window LLM family,
            # named there); the LLM call here is content extraction consumed
            # deterministically by the Step 6 generator below, not a Step 6
            # LLM call in its own right. Pre-extract concept-specific NPC
            # dialog lines NOW — while the LLM is
            # warm from the schema step and BEFORE the auto_fetch Blender/Chromium
            # phase in generator.write cools it. Inject as meta._pre_lines and pop
            # skip_audio so emit renders REAL, concept-driven Kokoro clips (mirrors
            # the audio _pre_cues path; a bare MicroComposer.emit still stays
            # TTS-free). Retry once — the local model very occasionally returns an
            # unparseable first response under load.
            _npc_subs = [s for s in subsystems if s.kind == "npc_dialog"]
            if _npc_subs and concept:
                _itype = {"quest_giver": "quest", "vendor": "vendor",
                          "ambient_bark": "ambient", "combat_chatter": "combat",
                          "follower": "follower", "cutscene": "quest"}
                try:
                    from .audio.dialog_resolver import extract_dialog_lines
                    _llm_seed = getattr(self.llm, "seed", None)
                    _dseed = int(_llm_seed) if _llm_seed is not None else (seed or 0)
                    _dialog_count = int(self.config.get("audio", {}).get("dialog_lines", 12))
                    for _sub in _npc_subs:
                        _spk = _sub.options.get("speaker_name", "NPC")
                        _lines: list[dict] = []
                        for _attempt in range(3):
                            _lines = extract_dialog_lines(
                                concept, self.llm, speaker=_spk,
                                interaction_type=_itype.get(_sub.variant or "", "quest"),
                                seed=_dseed, count=_dialog_count)
                            if _lines:
                                break
                        if _lines:
                            _meta = _sub.options.setdefault("meta", {})
                            _meta["_pre_lines"] = _lines
                            _meta.pop("skip_audio", None)   # lift default skip → emit renders Kokoro clips
                            self._ok(f"Pre-extracted {len(_lines)} NPC dialog lines for "
                                     f"{_spk} — Kokoro clips will render at emit")
                        else:
                            self._warn(f"No dialog lines extracted for {_spk} — "
                                       "npc_dialog stays text-only (preset lines, no clips)")
                except Exception as _exc:  # noqa: BLE001
                    self._warn(f"NPC dialog pre-extraction failed "
                               f"({type(_exc).__name__}: {_exc})")
            # ── Step 6.5 (cont.) — Concept-driven Sketchfab model collection
            # (10-15 models) ──────────────────────────────────────────────
            # Mirrors the audio cue pipeline: the LLM proposes the models this
            # world actually needs; each is searched + downloaded + Blender-
            # converted + frozen NOW (before generator.write) so the per-entity
            # Tier S then matches real, on-concept models instead of a stray
            # Kenney pick. Every model logs where it was written or why it failed.
            self._sketchfab_collection = None
            _coll_cfg = self.config.get("sketchfab", {}) or {}
            if _coll_cfg.get("enabled") and _coll_cfg.get("collect", True) and concept:
                if schema.game_type == "2D":
                    # Sketchfab is a 3D-model marketplace — fetch_and_freeze only
                    # ever writes FBX. The per-entity Tier S resolver already
                    # skips itself for 2D (unity_entity_asset_resolver.py: `not
                    # is_2d`) so those FBX never get matched to an entity; this
                    # batch step was the one place that gate was missing, and it
                    # unconditionally downloaded + Blender-converted 10-15 FBX
                    # models into Assets/Models/Sketchfab/ for a 2D game that
                    # can only use PNG sprites (Kenney catalog) or its own kind
                    # default models — wasted network/Blender time and orphaned
                    # files. Skip the whole step for 2D instead.
                    self._warn("Sketchfab collection skipped — 2D game "
                               "(Sketchfab only yields 3D FBX; 2D sprites come "
                               "from the Kenney PNG catalog instead)")
                else:
                    try:
                        from .unity_sketchfab_collection import collect_models
                        from .sketchfab_client import SketchfabClient
                        _llm_seed = getattr(self.llm, "seed", None)
                        _cseed = int(_llm_seed) if _llm_seed is not None else (seed or 0)
                        self._sketchfab_collection = collect_models(
                            concept, self.llm, client=SketchfabClient(_coll_cfg),
                            sk_cfg=_coll_cfg, lookups_dir=self.lookups_dir,
                            count=int(_coll_cfg.get("collection_count", 12)),
                            seed=_cseed, log_ok=self._ok, log_warn=self._warn)
                    except Exception as _exc:  # noqa: BLE001
                        self._warn(f"Sketchfab collection failed "
                                   f"({type(_exc).__name__}: {_exc})")
        _asset_cfg = self.config.get("assets", {}) or {}
        _sketchfab_cfg = self.config.get("sketchfab", {}) or {}
        from .csharp_syntax_gate import CSharpSyntaxError
        try:
            project_root, file_count = self.generator.write(
                schema, manifest_deps, self.output_dir, composed_units=composed_units,
                model_overrides=model_overrides,
                strict_assets=bool(_asset_cfg.get("strict", True)),
                asset_cfg=_asset_cfg, sketchfab_cfg=_sketchfab_cfg,
                project_config=self.config, llm=self.llm)
        except (ValueError, CSharpSyntaxError) as exc:
            # Two distinct FAIL-FAST doctrines both trip AFTER the expensive
            # LLM stages, inside generator.write(): the asset library
            # (ValueError) and the C# syntax gate (CSharpSyntaxError, e.g. a
            # malformed extra_fields default the fixer didn't repair). Either
            # way the filled schema is the expensive artifact — save it so
            # the run isn't lost. A syntax-gate failure needs a code fix
            # first (this schema will fail the same way again unchanged);
            # once that lands, `unity-validate` replays Steps 4-7 against the
            # saved schema deterministically, with no further LLM call.
            rescue = Path(self.output_dir) / f"{schema.project_name}.schema.json"
            try:
                rescue.parent.mkdir(parents=True, exist_ok=True)
                rescue.write_text(schema.model_dump_json(indent=2), encoding="utf-8")
                if isinstance(exc, CSharpSyntaxError):
                    self._warn(f"Schema rescued to {rescue} — after fixing the "
                               "generator defect the syntax gate reported, retry "
                               "with: python main.py unity-validate " + str(rescue))
                else:
                    self._warn(f"Schema rescued to {rescue} — fix entity model_hints "
                               "and retry: python main.py unity-validate " + str(rescue))
            except OSError:
                pass
            raise

        # Warm Unity's global package cache so first open resolves locally
        # instead of stalling on the registry. Best-effort by contract — any
        # failure just means Unity downloads as it always has.
        self._prime_package_cache(project_root, manifest_deps)

        if compose and subsystems:
            audio_backend = str(self.config.get("audio", {}).get("backend", "dsp")).strip().lower()
            if (audio_backend == "stable3" and not no_llm
                    and any(s.kind == "audio" for s in subsystems)):
                # 1. Extract diegetic cues from concept WHILE LM Studio is loaded.
                #    Inject into the audio subsystem meta so default_fix() finds them
                #    without needing a live LLM (it's unloaded a few lines below).
                try:
                    from .audio.resolver import extract_cues as _extract_audio_cues
                    _llm_seed = getattr(self.llm, "seed", None)
                    _seed = int(_llm_seed) if _llm_seed is not None else (seed or 0)
                    _pre_cues = _extract_audio_cues(concept, self.llm, n=10, seed=_seed)
                    if _pre_cues:
                        self._ok(f"Pre-extracted {len(_pre_cues)} diegetic audio cues from concept")
                        for _sub in subsystems:
                            if _sub.kind == "audio":
                                _sub.options.setdefault("meta", {})["_pre_cues"] = _pre_cues
                    else:
                        self._warn("Concept audio extraction returned no cues — genre preset will be used")
                except Exception as _exc:  # noqa: BLE001
                    self._warn(f"Audio pre-extraction failed ({type(_exc).__name__}: {_exc})")
                # 2. Unload LM Studio so SA3 has the full VRAM budget.
                from .audio.backends import unload_lm_studio
                self._ok("Unloading LM Studio to free VRAM for Stable Audio 3 clip generation")
                ok = unload_lm_studio(self.config)
                if ok:
                    self._ok("LM Studio model unloaded — VRAM available for Stable Audio 3")
                else:
                    self._warn("LM Studio unload failed — SA3 may OOM; run: lms unload")
            # Bridge the two islands: tell the composer whether a GameManager
            # concept script survived so GameContext can wire micro↔concept seams.
            has_game_manager = any(s.type == "game_manager" for s in (schema.scripts or []))
            # Names entities resolve_escort_follow (Step 5) gave a real
            # Follow-atom behavior to — SEAM 8b's honest signal that this
            # entity actually walks toward the player, not just a name an
            # escort objective happens to mention.
            escort_followed_entities = {
                e.name for e in schema.entities
                if e.name and e.behavior
                and any(getattr(a, "atom", "") == "Follow" for a in (e.atoms or []))
            }
            # Every named entity, spawn count included — SEAM 8c's ground
            # truth for whether a `reach` objective's target_ref is a real,
            # locatable GameObject it can attach a relay to (see
            # unity_compose.py's WireReachTargetRelay pass).
            reach_target_entities = {
                e.name: e.count for e in schema.entities if e.name
            }
            # generator.write() above may have re-laid out the scene with
            # measured model footprints, moving the obstacles and enemies Step
            # 5d baked cover points from (it re-bakes schema.meta["cover_points"]
            # when it does). `subsystems` was built before that, so re-point the
            # cover_shooter unit now — before emit renders its AI.cs.
            for msg in refresh_cover_points(subsystems, schema.entities,
                                            schema.meta.get("cover_points")):
                self._fix(msg)
            composer.emit(subsystems, project_root / "Assets",
                          has_game_manager=has_game_manager, wiring=schema.wiring,
                          objective_scenes=_objective_scene_map(
                              schema, schema.meta.get("mission_scenes")
                              if isinstance(schema.meta, dict) else None),
                          escort_followed_entities=escort_followed_entities,
                          reach_target_entities=reach_target_entities,
                          game_type=schema.game_type)
            # Hand the wiring pass's own decisions to Step 7. Whether a mission
            # can actually be completed is a COMPOSE-time fact (which seams
            # fired, which objectives found an event source), and the evaluator
            # reads the schema — so without this stamp it can only guess, which
            # is how it once graded an unwinnable build above a winnable one.
            if isinstance(schema.meta, dict):
                schema.meta["wiring"] = composer.wiring_report
            self._ok("Micro subsystems emitted to Assets/Micros + wired into scenes "
                     "(GameContext bridges micros ↔ prompt-authored scripts)")

            # World terrain — NOT a Subsystem/MicroComposer unit. Every kind
            # composer.plan()/emit() handles is a per-entity MonoBehaviour
            # that attaches to one GameObject and can participate in
            # GameWiring's cross-micro seams; terrain is world/region-scale
            # (one bake per schema.world_spec.region, not per entity) and
            # wires into nothing — see src/unity_terraingen.py's module
            # docstring for why it deliberately sits outside that model.
            if schema.world_spec and schema.world_spec.regions:
                from .unity_terraingen import project_seed_from_name
                from .micro.terrain import write_world_terrain
                terrain_results = write_world_terrain(
                    schema.world_spec, project_root / "Assets",
                    project_name=schema.project_name,
                    project_seed=project_seed_from_name(schema.project_name),
                )
                if terrain_results:
                    self._ok(f"Terrain baked for {len(terrain_results)} region(s) -> "
                             f"Assets/Terrain/<region>/ "
                             f"({', '.join(sorted(terrain_results))})")
                else:
                    self._warn("schema.world_spec declared region(s) but none had a "
                               "ground biome / non-degenerate bounds — no terrain baked "
                               "(see src/unity_terraingen.py NO_GROUND_BIOMES)")

            # Overlay audio clips:
            #   1. Assets/Audio/           — canonical user-visible folder
            #   2. Assets/Resources/Audio/ — for Resources.LoadAll<AudioClip> at runtime
            # The micro writes to Micros/audio/Resources/Audio/<id>/ — copy the
            # inner Audio/ tree into both destinations, THEN remove the source
            # Micros/audio/Resources/ copy. Unity's Resources system is a single
            # flat namespace merged across every "Resources" folder in the
            # project — leaving the pre-overlay copy in place means the SAME
            # clip exists at the same Resources-relative path under two
            # physical folders (a real "multiple assets with the same path"
            # risk), on top of tripling disk usage for no reader that needs it.
            import shutil
            audio_micro_res = project_root / "Assets" / "Micros" / "audio" / "Resources"
            audio_micro_clips = audio_micro_res / "Audio"
            if audio_micro_clips.exists():
                dst_audio = project_root / "Assets" / "Audio"
                shutil.copytree(audio_micro_clips, dst_audio, dirs_exist_ok=True)
                dst_res_audio = project_root / "Assets" / "Resources" / "Audio"
                shutil.copytree(audio_micro_clips, dst_res_audio, dirs_exist_ok=True)
                mf = audio_micro_res / "audio_manifest.json"
                if mf.exists():
                    shutil.copy2(mf, project_root / "Assets" / "Resources" / "audio_manifest.json")
                self._ok("Audio clips overlaid -> Assets/Audio/ + Assets/Resources/Audio/")
                try:
                    shutil.rmtree(audio_micro_res)
                except OSError:
                    pass   # best-effort cleanup — a locked file must not break the run
            # Overlay generated sprite frames the identical way:
            #   Micros/sprite_animator/Resources/Sprites/<entity>/<action>/ ->
            #   Assets/Sprites/ (canonical) + Assets/Resources/Sprites/ (Resources.LoadAll<Sprite>()).
            sprite_micro_res = project_root / "Assets" / "Micros" / "sprite_animator" / "Resources"
            sprite_micro_frames = sprite_micro_res / "Sprites"
            if sprite_micro_frames.exists():
                dst_sprites = project_root / "Assets" / "Sprites"
                shutil.copytree(sprite_micro_frames, dst_sprites, dirs_exist_ok=True)
                dst_res_sprites = project_root / "Assets" / "Resources" / "Sprites"
                shutil.copytree(sprite_micro_frames, dst_res_sprites, dirs_exist_ok=True)
                smf = sprite_micro_res / "sprite_manifest.json"
                if smf.exists():
                    shutil.copy2(smf, project_root / "Assets" / "Resources" / "sprite_manifest.json")
                self._ok("Sprite frames overlaid -> Assets/Sprites/ + Assets/Resources/Sprites/")
                try:
                    shutil.rmtree(sprite_micro_res)
                except OSError:
                    pass   # best-effort cleanup — a locked file must not break the run
            # Overlay NPC dialog (Kokoro) clips + manifest the same way as audio:
            #   Micros/npc_dialog/Resources/{Audio/Dialog, Dialog} -> Assets/Resources/
            #   Micros/npc_dialog/Resources/Audio/Dialog          -> Assets/Audio/Dialog
            # Then remove the source copy — same duplicate-Resources-path
            # rationale as the audio overlay above.
            npc_dialog_res = (project_root / "Assets" / "Micros" / "npc_dialog"
                              / "Resources")
            if npc_dialog_res.exists():
                shutil.copytree(npc_dialog_res,
                                project_root / "Assets" / "Resources",
                                dirs_exist_ok=True)
                _dlg_audio = npc_dialog_res / "Audio"
                if _dlg_audio.exists():
                    shutil.copytree(_dlg_audio,
                                    project_root / "Assets" / "Audio",
                                    dirs_exist_ok=True)
                    self._ok("NPC dialog (Kokoro) clips overlaid -> "
                             "Assets/Resources/Audio/Dialog/ + Assets/Audio/Dialog/")
                try:
                    shutil.rmtree(npc_dialog_res)
                except OSError:
                    pass
            # Copy the concept-driven Sketchfab collection into the project so all
            # 10-15 models are visibly written (per-entity resolution already
            # copied any it matched; this guarantees the full set is present).
            _coll = getattr(self, "_sketchfab_collection", None)
            if _coll and _coll.get("written"):
                from .unity_sketchfab_collection import copy_collection_into_project
                copy_collection_into_project(
                    _coll["written"],
                    self.config.get("sketchfab", {}).get("cache_dir", "Models/Sketchfab"),
                    project_root / "Assets", log_ok=self._ok)
            # ── External-asset collection summary (what was written / fell back) ─
            if _coll is not None:
                _w = len(_coll.get("written", []))
                _l = len(_coll.get("local", []))
                _f = len(_coll.get("failed", []))
                self._ok(f"ASSET SUMMARY — models: {_w} Sketchfab downloaded, "
                         f"{_l} concept-matched from local library, {_f} unfilled"
                         + (f" (unfilled: {', '.join(x['name'] for x in _coll['failed'][:5])})"
                            if _f else ""))
                for _x in _coll.get("local", []):
                    self._ok(f"  local: '{_x['name']}' -> {_x.get('relpath') or _x.get('filename')}")
        # Drop a discoverable sidecar so the Library page can show the concept
        # next to each saved project (Unity schemas aren't written to disk the
        # way GTA's *.schema.json files are).
        try:
            (project_root / ".concept.txt").write_text(
                f"# {schema.project_name}\n\n{user_prompt}\n\n---\n\n{concept}\n",
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 — sidecar failure shouldn't break the run
            self._warn("Could not write .concept.txt sidecar")
        # Persist the resolved schema so it can be rebuilt (`from-schema`) or fed
        # to the Blender asset pipeline (`blender_cli.py from-schema`) — the
        # unity→assets chain needs a schema on disk.
        try:
            (project_root / f"{schema.project_name}.schema.json").write_text(
                schema.model_dump_json(indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001 — sidecar failure shouldn't break the run
            self._warn("Could not write schema.json sidecar")
        self._ok(f"Project written to: {project_root}")

        # STEP 7 — grade the output ───────────────────────────────────────────
        # Every step above answers "did it compile". This one answers "is it a
        # game" (src/unity_playability.py): deterministic structural checks
        # over the final schema, so a build that lost its avatar, its
        # objectives or the entities the brief named explicitly says so here
        # instead of waiting to be noticed in the editor.
        self._step(7, "Playability evaluator grades the build")
        report = evaluate_project(schema, _read_project_files_for_grading(project_root))
        # markup=False: findings carry schema field paths, which rich would
        # otherwise read as console markup and swallow.
        console.print(report.render(), markup=False,
                      style="green" if report.playable else "yellow")
        try:
            (project_root / "playability.txt").write_text(
                report.render() + "\n", encoding="utf-8")
        except Exception:  # noqa: BLE001 — sidecar failure shouldn't break the run
            self._warn("Could not write playability.txt sidecar")
        # Advisory by default: an existing project with known warnings must not
        # start failing its build the day this lands. `playability.gate: true`
        # in config.yaml makes errors fatal; `python main.py unity-playability
        # <schema.json> --strict` grades a saved schema the same way.
        _pcfg = (self.config.get("playability") or {}) if self.config else {}
        _reason = playability_gate(
            report,
            min_score=int(_pcfg.get("min_score", 0) or 0),
            fail_on_error=bool(_pcfg.get("gate", False)),
        )
        if _reason:
            raise PlayabilityGateError(f"Playability gate failed — {_reason}")

        console.print(Panel(
            f"[bold green]Done![/]\n"
            f"Project:    [cyan]{schema.project_name}[/]\n"
            f"Game type:  [cyan]{schema.game_type}[/]\n"
            f"Pipeline:   [cyan]{schema.render_pipeline}[/]\n"
            f"Platform:   [cyan]{schema.platform}[/]\n"
            f"Unity:      [cyan]{schema.unity_version}[/]\n"
            f"Packages:   [cyan]{len(manifest_deps)}[/]\n"
            f"Scripts:    [cyan]{len(schema.scripts)}[/]\n"
            f"Files:      [cyan]{file_count}[/]\n"
            f"Output:     [cyan]{project_root}[/]",
            style="green",
        ))
        return project_root
