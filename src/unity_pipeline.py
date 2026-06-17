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

from .lm_studio_client   import LMStudioClient
from .backends           import make_chat_client
from .anythingllm_client import AnythingLLMClient
from .unity_schema       import UnityPackage, UnitySchema, UnityScript
from .unity_resolver     import UnityResolver
from .unity_generator    import UnityProjectGenerator
from .unity_prompts      import (
    UNITY_CONCEPT_SYSTEM, UNITY_CONCEPT_USER,
    UNITY_SCHEMA_FILL_SYSTEM, UNITY_SCHEMA_FILL_USER,
)
from .themes              import (
    resolve_theme, render_concept_bias, render_schema_hints,
)

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


def _programmatic_fix(schema: UnitySchema) -> UnitySchema:
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
    for script in schema.scripts:
        script.name = _to_pascal(script.name, "Script")

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

    return schema, fixes


# ── Pipeline class ─────────────────────────────────────────────────────────────

class UnityPipeline:
    def __init__(self, config: dict):
        lm_cfg   = config["lm_studio"]
        pipe_cfg = config.get("unity_pipeline", config.get("pipeline", {}))

        self.llm = make_chat_client(config)

        # Unity RAG is optional: skipped in Basic Mode; otherwise points at the
        # 'llm_unity' AnythingLLM workspace seeded from rag_docs/unity/.
        use_rag = not config.get("basic_mode", False)
        unity_rag: AnythingLLMClient | None = None
        if use_rag:
            llm_cfg = config.get("anythingllm", {})
            unity_slug = llm_cfg.get("unity_workspace_slug") or llm_cfg.get("workspace_slug", "")
            if llm_cfg.get("base_url") and unity_slug:
                unity_rag = AnythingLLMClient(
                    base_url       = llm_cfg["base_url"],
                    api_key        = llm_cfg.get("api_key", ""),
                    workspace_slug = unity_slug,
                )

        self.resolver = UnityResolver(
            lookups_dir = pipe_cfg.get("lookups_dir", "lookups"),
            rag         = unity_rag,
            use_rag     = use_rag,
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

    def run(self, user_prompt: str,
            theme:       str | None = None,
            mashup_with: str | None = None,
            seed:        int | None = None,
            compose:     bool = False) -> Path:
        console.print(Panel(
            f"[bold]Unity Project Creator Pipeline[/]\n{user_prompt}", style="blue"
        ))

        # Fixed seed → reproducible LLM generations (concept + schema). Same seed
        # the theme RNG uses, so one --seed makes the whole prompt→schema→assets
        # chain deterministic. None → server picks a random seed (today's default).
        if seed is not None:
            self.llm.seed = seed

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
        schema_raw = self.llm.chat(
            system      = UNITY_SCHEMA_FILL_SYSTEM,
            user        = UNITY_SCHEMA_FILL_USER.format(concept=concept, schema_hints=schema_hints),
            temperature = self.schema_temp,
            thinking    = False,
        )
        schema_dict = self.llm.extract_json(schema_raw)
        schema      = UnitySchema.model_validate(schema_dict)
        schema.meta = {**schema.meta, **theme_meta}
        # Persist the LLM concept so the Library page can describe the project.
        schema.meta["concept"]     = concept
        schema.meta["user_prompt"] = user_prompt
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
        schema = _programmatic_fix(schema)
        self._ok("Programmatic fixes applied (naming, scenes, script names)")

        schema, fix_msgs = _check_consistency(schema)
        for msg in fix_msgs:
            self._fix(msg)
        if not fix_msgs:
            self._ok("Schema is consistent — no fixes needed")

        # Re-resolve anything that consistency check added
        schema = self.resolver.resolve_schema(schema)
        manifest_deps = self.resolver.build_manifest_deps(
            schema, inject_mcp_bridge=self.inject_mcp_bridge
        )
        self._ok(f"manifest.json will contain {len(manifest_deps)} packages")

        # STEP 6 — generate ───────────────────────────────────────────────────
        self._step(6, "Generator compiles schema -> Unity project folder")
        # Composition: assemble the micro library into the project and wire each
        # micro into the scene as a _GameSystems component (deterministic, no LLM).
        composed_units = None
        subsystems = None
        if compose:
            from .unity_compose import MicroComposer, subsystems_for, COVERED_SCRIPT_TYPES
            composer  = MicroComposer(schema.project_name,
                                      template_dir=self.template_dir)
            subsystems = subsystems_for(schema.game_type, schema.features,
                                        locomotion=schema.locomotion)
            composed_units = composer.plan(subsystems)
            # Composer is the source of truth for covered subsystems — drop the
            # duplicate built-in scripts so the project isn't double-wired. Keep
            # only the unique glue (GameManager, mesh/material, editor scripts).
            before = len(schema.scripts)
            schema.scripts = [s for s in schema.scripts if s.type not in COVERED_SCRIPT_TYPES]
            if before != len(schema.scripts):
                self._fix(f"composition owns {before - len(schema.scripts)} subsystem(s) — "
                          "dropped duplicate built-in scripts")
            # Contribute the micros' module deps (UI/UGUI/AI/...) so the composed
            # project compiles.
            manifest_deps = {**manifest_deps, **composer.module_deps(composed_units)}
            self._ok(f"Composing {len(composed_units)} micro subsystems: "
                     + ", ".join(f"{u.kind}/{u.variant}" for u in composed_units))
        project_root, file_count = self.generator.write(
            schema, manifest_deps, self.output_dir, composed_units=composed_units)
        if compose and subsystems:
            composer.emit(subsystems, project_root / "Assets")
            self._ok("Micro subsystems emitted to Assets/Micros + wired into scenes")
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
