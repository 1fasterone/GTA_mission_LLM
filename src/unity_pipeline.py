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

from rich.console import Console
from rich.panel   import Panel

from .backends           import make_chat_client, make_rag_client
from .unity_schema       import UnityPackage, UnitySchema, UnityScript
from .capability_resolver import resolve_capabilities
from .unity_asset_catalog import asset_vocabulary
from .unity_resolver     import UnityResolver
from .unity_generator    import UnityProjectGenerator
from .unity_prompts      import (
    UNITY_CONCEPT_SYSTEM, UNITY_CONCEPT_USER,
    UNITY_SCHEMA_FILL_SYSTEM, UNITY_SCHEMA_FILL_USER,
    GAMECONTEXT_SCHEMA_GUIDANCE,
)
from .themes              import (
    resolve_theme, render_concept_bias, render_schema_hints,
)
from .unity_archetypes    import classify as classify_intent, apply_intent

console = Console()

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
# prompt). Three or more = the author already themed it.
_BRIEF_MARKER_RE = re.compile(
    r"^(TITLE|GENRE|STYLE|CORE PREMISE|SCENES|NARRATIVE|SCRIPTS|ENTITIES"
    r"|AUDIO|FEATURES|VISUAL STYLE|COLOR PALETTE|PROCEDURAL)\b",
    re.MULTILINE | re.IGNORECASE)


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
    known_classes: set[str] = {s.name for s in schema.scripts}
    for script in schema.scripts:
        if script.type != "custom":
            continue
        if script.update_extra:
            script.update_extra = _sanitize_singleton_calls(
                script.update_extra, known_classes, allow_context=compose)
        if script.awake_extra:
            script.awake_extra = _sanitize_singleton_calls(
                script.awake_extra, known_classes, allow_context=compose)
        if script.fixed_update_extra:
            script.fixed_update_extra = _sanitize_singleton_calls(
                script.fixed_update_extra, known_classes, allow_context=compose)
        for m in script.extra_methods:
            m.body = _sanitize_singleton_calls(m.body, known_classes, allow_context=compose)

    return schema


# ── Consistency checker ────────────────────────────────────────────────────────

def _check_consistency(schema: UnitySchema) -> tuple[UnitySchema, list[str]]:
    """
    Ensure script types and features are coherent.
    Returns (fixed_schema, list_of_fix_messages).
    """
    fixes: list[str] = []

    # Helper re-derived each call so injections made above are visible below.
    def _stypes() -> set[str]:
        return {s.type for s in schema.scripts}

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

    if "player" not in _stypes():
        schema.scripts.append(UnityScript(
            name        = "PlayerController",
            type        = "player",
            description = f"Player movement controller for {schema.game_type} game",
        ))
        fixes.append("Injected PlayerController (required boilerplate)")

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
                spec.entities = [n for n in spec.entities if n in entity_names]
                fixes.append(f"Scene '{spec.name}': dropped unknown entity "
                             f"group(s) {unknown}")
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


# ── Pipeline class ─────────────────────────────────────────────────────────────

class UnityPipeline:
    def __init__(self, config: dict):
        self.config = config
        lm_cfg   = config["lm_studio"]
        pipe_cfg = config.get("unity_pipeline", config.get("pipeline", {}))

        self.llm = make_chat_client(config)

        # Unity RAG: uses the same backend selection as GTA5 (local ChromaDB or
        # AnythingLLM), driven by config.rag.backend.  Skipped in Basic Mode.
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
            blender_models: bool = False) -> Path:
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
        if intent.matched:
            self._ok(
                "Intent: "
                + "  ".join(f"{axis}={getattr(intent, axis)!r}" for axis in intent.matched)
            )

        # STEP 2 — concept ────────────────────────────────────────────────────
        self._step(2, "LLM generates project concept")
        concept = self.llm.chat(
            system      = UNITY_CONCEPT_SYSTEM,
            user        = UNITY_CONCEPT_USER.format(user_prompt=user_prompt, theme_block=theme_block),
            temperature = self.concept_temp,
            thinking    = True,
        )
        self._ok(f"{len(concept)} chars of project concept generated")
        if self.show_prompts:
            console.print(concept)

        # STEP 3 — fill schema ────────────────────────────────────────────────
        self._step(3, "LLM fills project schema")
        schema_user = UNITY_SCHEMA_FILL_USER.format(concept=concept, schema_hints=schema_hints)
        # In --compose mode the micros + GameContext locator exist, so steer the
        # LLM to reach subsystems via GameContext.Instance.Get<T>() instead of
        # inventing singletons (which the fixer strips).
        if compose:
            schema_user += GAMECONTEXT_SCHEMA_GUIDANCE
        schema_raw = self.llm.chat(
            system      = UNITY_SCHEMA_FILL_SYSTEM,
            user        = schema_user,
            temperature = self.schema_temp,
            thinking    = False,
        )
        schema_dict = self.llm.extract_json(schema_raw)
        schema      = UnitySchema.model_validate(schema_dict)
        schema.meta = {**schema.meta, **theme_meta}
        # Persist the LLM concept so the Library page can describe the project.
        schema.meta["concept"]     = concept
        schema.meta["user_prompt"] = user_prompt

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

        # Capability graph: schema.capabilities → atoms (Step 5b)
        schema, cap_msgs = resolve_capabilities(schema)
        for msg in cap_msgs:
            self._fix(msg)
        if not cap_msgs and schema.capabilities:
            self._ok("Capabilities present but produced no atoms (all sub-models None)")

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
        # Composition: assemble the micro library into the project and wire each
        # micro into the scene as a _GameSystems component (deterministic, no LLM).
        composed_units = None
        subsystems = None
        if compose:
            from .unity_compose import (MicroComposer, subsystems_for,
                                        drop_owned_scripts)
            composer  = MicroComposer(schema.project_name,
                                      template_dir=self.template_dir,
                                      project_config=self.config)
            # Ensure audio is always composed — it's a core game system.
            if "audio" not in (schema.features or []):
                schema.features = list(schema.features or []) + ["audio"]
                self._fix("compose: injected 'audio' feature for SA3 clip generation")
            subsystems = subsystems_for(schema.game_type, schema.features,
                                        locomotion=schema.locomotion,
                                        capabilities=schema.capabilities,
                                        user_prompt=user_prompt,
                                        scenes=schema.scenes,
                                        entities=schema.entities,
                                        scripts=schema.scripts,
                                        genre=schema.meta.get("genre", "universal"))
            composed_units = composer.plan(subsystems)
            # Composer is the source of truth for covered subsystems — drop the
            # duplicate built-in scripts so the project isn't double-wired. Keep
            # only the unique glue (GameManager, mesh/material, editor scripts).
            before = len(schema.scripts)
            schema.scripts = drop_owned_scripts(schema.scripts, composed_units)
            if before != len(schema.scripts):
                self._fix(f"composition owns {before - len(schema.scripts)} subsystem(s) — "
                          "dropped duplicate built-in/name-colliding scripts")
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
            # Pre-extract concept-specific NPC dialog lines NOW — while the LLM is
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
                    _dseed = (int(self.llm.seed) if getattr(self.llm, "seed", None) is not None
                              else (seed or 0))
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
            # ── Concept-driven Sketchfab model collection (10-15 models) ──────
            # Mirrors the audio cue pipeline: the LLM proposes the models this
            # world actually needs; each is searched + downloaded + Blender-
            # converted + frozen NOW (before generator.write) so the per-entity
            # Tier S then matches real, on-concept models instead of a stray
            # Kenney pick. Every model logs where it was written or why it failed.
            self._sketchfab_collection = None
            _coll_cfg = self.config.get("sketchfab", {}) or {}
            if _coll_cfg.get("enabled") and _coll_cfg.get("collect", True) and concept:
                try:
                    from .unity_sketchfab_collection import collect_models
                    from .sketchfab_client import SketchfabClient
                    _cseed = (int(self.llm.seed) if getattr(self.llm, "seed", None) is not None
                              else (seed or 0))
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
        # Step 6.5 — enrich entity sketchfab_query while the project LLM is
        # still loaded (mirrors the audio concept-translator's placement).
        # Runs regardless of sketchfab.enabled: the concept-translated query now
        # ALSO drives the LOCAL catalog tiers (see _query_weights), so entity →
        # model matching is concept-aware even when Sketchfab is off or fails.
        if schema.entities:
            try:
                from .unity_sketchfab_translator import translate_concept
                _seed = (int(self.llm.seed) if getattr(self.llm, "seed", None) is not None
                        else (seed or 0))
                n = translate_concept(concept, schema.entities, self.llm, _seed)
                if n:
                    self._ok(f"sketchfab_query enriched for {n}/{len(schema.entities)} entities")
                else:
                    self._warn("Sketchfab query translation produced nothing — "
                              "resolver falls back to model_hint / entity name")
            except Exception as exc:  # noqa: BLE001
                self._warn(f"Sketchfab query translation failed "
                          f"({type(exc).__name__}: {exc}) — mechanical fallback used")
        try:
            project_root, file_count = self.generator.write(
                schema, manifest_deps, self.output_dir, composed_units=composed_units,
                model_overrides=model_overrides,
                strict_assets=bool(_asset_cfg.get("strict", True)),
                asset_cfg=_asset_cfg, sketchfab_cfg=_sketchfab_cfg)
        except ValueError:
            # FAIL-FAST asset doctrine tripped AFTER the expensive LLM stages —
            # save the filled schema so the run isn't lost: edit the entity
            # model_hints (or the asset library) and retry with unity-validate.
            rescue = Path(self.output_dir) / f"{schema.project_name}.schema.json"
            try:
                rescue.parent.mkdir(parents=True, exist_ok=True)
                rescue.write_text(schema.model_dump_json(indent=2), encoding="utf-8")
                self._warn(f"Schema rescued to {rescue} — fix entity model_hints "
                           "and retry: python main.py unity-validate " + str(rescue))
            except OSError:
                pass
            raise
        if compose and subsystems:
            audio_backend = str(self.config.get("audio", {}).get("backend", "dsp")).strip().lower()
            if audio_backend == "stable3" and any(s.kind == "audio" for s in subsystems):
                # 1. Extract diegetic cues from concept WHILE LM Studio is loaded.
                #    Inject into the audio subsystem meta so default_fix() finds them
                #    without needing a live LLM (it's unloaded a few lines below).
                try:
                    from .audio.resolver import extract_cues as _extract_audio_cues
                    _seed = int(self.llm.seed) if getattr(self.llm, "seed", None) is not None else (seed or 0)
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
            composer.emit(subsystems, project_root / "Assets",
                          has_game_manager=has_game_manager)
            self._ok("Micro subsystems emitted to Assets/Micros + wired into scenes "
                     "(GameContext bridges micros ↔ prompt-authored scripts)")
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
