"""
Mission & Project Creator — CLI entry point.

LLM front-end (Steps 1-3) + deterministic compiler back-end (Steps 4-6).
Requires LM Studio running with a model loaded, OR Ollama (set in config.yaml).

━━━ GTA5 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python main.py generate "heist at the docks"        # LLM → C# mission script
  python main.py validate schema.json                 # recompile saved schema (no LLM)
  python main.py resolve "cult member" peds           # resolve one GTA5 asset ID

━━━ RAG index (local ChromaDB — run once, or after corpus updates) ━━━━━━━━━
  python main.py rag-build                            # scrape + embed all corpora
  python main.py rag-build weapons scenarios --force  # rebuild specific scrapers
  python main.py rag-query "cult member" --kind ped  # search the local index
  python main.py rag-build --unity                    # rebuild the Unity corpus only

━━━ AnythingLLM (legacy — only needed if you run an AnythingLLM server) ━━━━
  python main.py setup-rag                            # upload GTA5 docs to workspace
  python main.py setup-workspace                      # bootstrap the workspace
  python main.py rag-status                           # check workspace doc counts

━━━ Web UI ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python main.py serve                                # http://127.0.0.1:8000

━━━ Unity — full project ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python main.py unity-create "horror survival in a maze"
  python main.py unity-create --file prompts/neon_fugitive.txt
  python main.py unity-create "racing game" --compose         # + wire micro-generators
  python main.py unity-validate MyGame.schema.json            # recompile (no LLM)
  python main.py unity-migrate MyGame.schema.json             # upgrade v1 → v2 schema
  python main.py unity-resolve "universal render pipeline"    # look up package ID

━━━ Unity — micro-generators (drop-in focused artefacts) ━━━━━━━━━━━━━━━━━━
  python main.py unity-controller "scuba diver" --variant swimming
  python main.py unity-camera     "horror game camera" --variant cinemachine_3p
  python main.py unity-physics    "floating debris" --variant buoyancy
  python main.py unity-hud        "sci-fi helmet display" --variant full_hud_bundle
  python main.py unity-input      "racing wheel + gamepad" --variant racing_wheel
  python main.py unity-ai         "guard dog that patrols and chases" --variant fsm_guard
  python main.py unity-spawner    "enemy wave system" --variant wave_spawner
  python main.py unity-saveload   "checkpoint autosave" --variant checkpoint
  python main.py unity-inventory  "equipment slots for an RPG" --variant equipment
  python main.py unity-dialogue   "branching NPC conversation" --variant branching
  python main.py unity-menu       "pause menu with settings" --variant pause_menu
  python main.py unity-interactable "locked door with keypad" --variant door
  python main.py unity-transition "fade to black on zone exit" --variant fade
  python main.py unity-health     "regenerating shield + health" --variant shielded
  python main.py unity-statemachine "zombie with 3 patrol states" --variant enemy_fsm
  python main.py unity-trigger    "boss arena that seals on entry" --variant zone_enter
  python main.py unity-animator   "humanoid soldier walk/run/shoot" --variant humanoid

━━━ Unity — audio (Stable Audio 3 by default — see config.yaml audio.backend) ━━
  python main.py unity-audio "horror maze ambient + threat SFX"
  python main.py unity-audio "arcade coin game" --no-llm       # no LLM, DSP fallback
  python main.py unity-audio "FPS soundscape" -u output/unity/MyGame  # overlay clips

━━━ Unity — Blender 3D model micro (car / plane / humanoid / bike / weapon → FBX) ━━
  python main.py unity-model "lifted matte-black pickup" --no-llm
  python main.py unity-model "tall humanoid" --variant humanoid --no-llm
  python main.py unity-model "dirt bike" --variant bike -k suspension=1 --no-llm
  python main.py unity-model "red sports coupe" -u output/unity/TopDownRacingGame
  python main.py unity-model "glider" --variant plane --emit    # emit bpy script only

━━━ Atoms (engine-agnostic IR building blocks) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python main.py atom-list                            # all atoms by category
  python main.py atom-list movement --params          # show parameter details
  python main.py atom-list --snippet MoveToTarget     # show Unity C# snippet

━━━ Enumerate tools ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python main.py unity-tools list                     # list every micro + variants

━━━ Global flags (work with any command) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  --verbose / -v   Show pipeline step summaries
  --debug   / -d   Show raw LLM requests and responses
  --model   / -m   Override the LM Studio model for this single run
  --seed           Seed for reproducible twist-seed / theme selection
  --theme          Theme name, 'random', or 'list'
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Optional

import typer
import yaml
from dotenv import load_dotenv
from rich.console import Console

# Render rich output (Panels use Unicode box-drawing) safely when stdout is a
# Windows cp1252 pipe — same guard as blender_cli.py.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

# ── GTA5 pipeline ─────────────────────────────────────────────────────────────
from src.anythingllm_client import AnythingLLMClient
from src.code_generator     import CodeGenerator
from src.mission_schema     import MissionSchema
from src.pipeline           import MissionPipeline
from src.resolver           import ModelResolver

# ── Unity pipeline ─────────────────────────────────────────────────────────────
from src.unity_generator    import UnityProjectGenerator
from src.unity_pipeline     import UnityPipeline
from src.unity_resolver     import UnityResolver
from src.unity_schema       import UnitySchema

load_dotenv()

# ── App + shared state ─────────────────────────────────────────────────────────

console = Console()

app = typer.Typer(
    name            = "mission-creator",
    help            = "Mission & Project Creator — LLM storyteller + deterministic compiler",
    add_completion  = False,
    no_args_is_help = True,
)

class _State:
    verbose: bool = False
    debug:   bool = False

state = _State()


@app.callback()
def _global(
    verbose: bool = typer.Option(False, "--verbose", "-v",
                                 help="Show pipeline step summaries"),
    debug:   bool = typer.Option(False, "--debug",   "-d",
                                 help="Show raw LLM requests and responses"),
) -> None:
    state.verbose = verbose
    state.debug   = debug
    level = logging.DEBUG if debug else (logging.INFO if verbose else logging.WARNING)
    logging.basicConfig(level=level, format="%(levelname)-8s %(name)s: %(message)s")


# ── Config helpers ─────────────────────────────────────────────────────────────

def _cfg(config_path: str, model: Optional[str] = None) -> dict:
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # --model overrides the configured LM Studio model for this run, so a loaded
    # model is actually used (LM Studio serves whatever the request names, NOT
    # whatever `lms load` put in memory).
    if model:
        cfg.setdefault("lm_studio", {})["model"] = model
        console.print(f"[dim]Model override: [cyan]{model}[/][/]")
    return cfg


def _unity_pipe_cfg(cfg: dict) -> dict:
    """Return the Unity pipeline sub-config, falling back to the generic pipeline block."""
    return cfg.get("unity_pipeline", cfg.get("pipeline", {}))


# ── GTA5 commands ──────────────────────────────────────────────────────────────

@app.command()
def generate(
    prompt: str = typer.Argument(..., help="Mission idea in plain English"),
    config: str = typer.Option("config.yaml", "--config", "-c"),
    theme:       Optional[str] = typer.Option(None, "--theme",
                                              help="Theme name, 'random', or 'list'"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup",
                                              help="Second theme to fuse with --theme"),
    seed:        Optional[int] = typer.Option(None, "--seed",
                                              help="Seed for twist-seed selection (reproducibility)"),
    model:       Optional[str] = typer.Option(None, "--model", "-m",
                                              help="Override the configured LM Studio model for this run"),
) -> None:
    """Generate a GTA5 mission C# script from a plain-English prompt."""
    if theme and theme.lower() == "list":
        from src.themes import list_themes
        for t in list_themes():
            console.print(f"  [cyan]{t.name}[/]  —  {t.display}")
        raise typer.Exit(0)
    pipeline = MissionPipeline(_cfg(config, model))
    pipeline.run(prompt, theme=theme, mashup_with=mashup_with, seed=seed)


@app.command()
def validate(
    schema_file: str = typer.Argument(..., help="Path to a mission JSON schema file"),
    config:      str = typer.Option("config.yaml", "--config", "-c"),
) -> None:
    """Recompile a saved GTA5 schema to C# without re-running the LLM."""
    cfg    = _cfg(config)
    data   = json.loads(Path(schema_file).read_text(encoding="utf-8"))
    schema = MissionSchema.model_validate(data)
    gen    = CodeGenerator(cfg["pipeline"]["template_file"])
    out    = gen.write(schema, cfg["pipeline"]["output_dir"])
    console.print(f"[green]OK[/]  Generated -> {out}")


@app.command()
def resolve(
    description: str = typer.Argument(..., help="e.g. 'cult member'"),
    category:    str = typer.Argument(..., help="peds | vehicles | objects | weapons"),
    config:      str = typer.Option("config.yaml", "--config", "-c"),
) -> None:
    """Resolve one GTA5 asset description to a model ID (RAG + local fallback)."""
    cfg      = _cfg(config)
    rag      = AnythingLLMClient(
        base_url       = cfg["anythingllm"]["base_url"],
        api_key        = cfg["anythingllm"]["api_key"],
        workspace_slug = cfg["anythingllm"]["workspace_slug"],
    )
    resolver = ModelResolver(rag=rag, lookups_dir=cfg["pipeline"]["lookups_dir"])
    result   = resolver.resolve(description, category)
    console.print(f"[cyan]{description}[/] -> [bold]{result}[/]")


@app.command("rag-build")
def rag_build(
    targets: list[str] = typer.Argument(None, help="Scrapers to run (default: all)"),
    force:   bool      = typer.Option(False, "--force", "-f", help="Re-scrape even if corpus exists"),
    limit:   int       = typer.Option(0, "--limit", "-l", help="Cap entries (0=unlimited)"),
) -> None:
    """Scrape GTA5 data → JSONL corpus, then embed → ChromaDB index."""
    import subprocess, sys
    build_args = [sys.executable, "-m", "rag.build"] + (targets or [])
    if force:   build_args.append("--force")
    if limit:   build_args += [f"--limit={limit}"]
    subprocess.run(build_args, check=True)
    console.print("\n[bold]Embedding corpus into vector index ...[/]")
    idx_args = [sys.executable, "-m", "rag.indexer"]
    subprocess.run(idx_args, check=True)


@app.command("rag-query")
def rag_query(
    query: str = typer.Argument(..., help="Natural-language search term"),
    kind:  str = typer.Option("", "--kind", "-k", help="Filter: ped|vehicle|animation|scenario|audio"),
    n:     int = typer.Option(5,  "--n",    "-n", help="Number of results"),
) -> None:
    """Search the local RAG vector index. Requires rag-build to have been run."""
    from rag.query import RagClient
    rag = RagClient()
    if not rag.is_ready():
        console.print("[red]Index not built.[/] Run: [cyan]python main.py rag-build[/]")
        raise typer.Exit(1)

    kind_arg = kind or None
    hits = rag.search(query, kind=kind_arg, n=n)
    console.print(f"\n[bold]RAG search:[/] [cyan]{query}[/]"
                  + (f"  [dim](kind={kind})[/]" if kind else ""))
    for i, h in enumerate(hits, 1):
        dist = h["distance"]
        color = "green" if dist < 0.4 else ("yellow" if dist < 0.55 else "red")
        console.print(
            f"  [{i}] [{color}]{h['canonical']}[/]  "
            f"[dim]{h['kind']} | {h['label']} | dist={dist:.3f}[/]"
        )
        if h.get("tags"):
            console.print(f"       [dim]tags: {h['tags']}[/]")


@app.command("setup-rag")
def setup_rag(
    config: str = typer.Option("config.yaml", "--config", "-c"),
) -> None:
    """Upload GTA5 RAG documents to AnythingLLM workspace (legacy — AnythingLLM only).

    For the local ChromaDB RAG pipeline, use: python main.py rag-build"""
    import setup_rag as sr          # top-level script, kept lazy to avoid startup cost
    sr.upload_all(_cfg(config))


@app.command("rag-status")
def rag_status(
    config: str = typer.Option("config.yaml", "--config", "-c"),
) -> None:
    """Print doc count + status for AnythingLLM workspaces (legacy — AnythingLLM only).

    For the local ChromaDB index, use: python main.py rag-query <term>"""
    from src.backends import check_rag
    result = check_rag(_cfg(config))
    if result.get("basic_mode"):
        console.print("[yellow]Basic Mode is on[/] — RAG is skipped, local lookups only.")
        raise typer.Exit(0)
    if result.get("error"):
        console.print(f"[red]AnythingLLM unreachable:[/] {result['error'].get('title')}")
        raise typer.Exit(1)
    for kind in ("gta", "unity"):
        sub = result.get(kind, {})
        slug = sub.get("slug")
        present = sub.get("present")
        docs = sub.get("doc_count", 0)
        if present:
            color = "green" if docs > 0 else "yellow"
            console.print(
                f"  [{color}]{kind:<5}[/] workspace [cyan]{slug}[/] — "
                f"{docs} doc(s)" + ("  [yellow](no docs — run setup-workspace)[/]" if not docs else "")
            )
        else:
            console.print(
                f"  [red]{kind:<5}[/] workspace [cyan]{slug}[/] — NOT FOUND "
                "[dim](click Create in the UI, or run setup-workspace)[/]"
            )


@app.command("setup-workspace")
def setup_workspace(
    config: str = typer.Option("config.yaml", "--config", "-c"),
) -> None:
    """Bootstrap AnythingLLM workspace (legacy — AnythingLLM server only).

    For the local ChromaDB RAG pipeline, use: python main.py rag-build"""
    import setup_anythingllm
    result = setup_anythingllm.bootstrap(_cfg(config))
    if result.get("log"):
        console.print(result["log"])
    if result.get("ok"):
        console.print("[green]OK[/]  Workspace bootstrapped")
    else:
        console.print(f"[red]Error:[/] {result.get('detail', result)}")
        raise typer.Exit(1)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port", "-p"),
) -> None:
    """Launch the local web UI — all pipelines accessible at http://host:port."""
    import uvicorn
    console.print(f"[cyan]Mission & Project Creator[/] — open http://{host}:{port}")
    uvicorn.run("webui.server:app", host=host, port=port, log_level="info")


# ── Unity commands ─────────────────────────────────────────────────────────────

@app.command("unity-create")
def unity_create(
    prompt: Optional[str]  = typer.Argument(None,  help="Project idea in plain English"),
    config: str            = typer.Option("config.yaml", "--config", "-c"),
    file:   Optional[Path] = typer.Option(None, "--file", "-f",
                                          help="Read the prompt from a .txt file"),
    theme:       Optional[str] = typer.Option(None, "--theme",
                                              help="Theme name, 'random', or 'list'"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup",
                                              help="Second theme to fuse with --theme"),
    seed:        Optional[int] = typer.Option(None, "--seed",
                                              help="Seed for twist-seed selection (reproducibility)"),
    model:       Optional[str] = typer.Option(None, "--model", "-m",
                                              help="Override the configured LM Studio model for this run"),
    compose:     bool          = typer.Option(False, "--compose",
                                              help="Assemble micros into the project + wire them into the scene (combinatorial mode)"),
    blender_models: bool       = typer.Option(False, "--blender-models",
                                              help="Build real models from the story in Blender (headless) and wire them into the scene via the Model Pack (replaces placeholders)"),
) -> None:
    """Generate a complete Unity project folder from a plain-English prompt.

    \b
    Inline prompt:
      python main.py unity-create "A 2D platformer with Cinemachine"

    Prompt from file (for long, structured briefs):
      python main.py unity-create --file prompts/neon_fugitive.txt
    """
    if theme and theme.lower() == "list":
        from src.themes import list_themes
        for t in list_themes():
            console.print(f"  [cyan]{t.name}[/]  —  {t.display}")
        raise typer.Exit(0)

    if file is not None:
        if not file.exists():
            console.print(f"[red]Error:[/] file not found: {file}")
            raise typer.Exit(1)
        prompt = file.read_text(encoding="utf-8").strip()

    if not prompt:
        console.print("[red]Error:[/] provide a prompt or use --file <path>")
        console.print("  Example: python main.py unity-create \"A 2D platformer\"")
        console.print("  Example: python main.py unity-create --file prompts/neon_fugitive.txt")
        raise typer.Exit(1)

    UnityPipeline(_cfg(config, model)).run(prompt, theme=theme, mashup_with=mashup_with, seed=seed, compose=compose, blender_models=blender_models)


@app.command("unity-resolve")
def unity_resolve(
    description:   str = typer.Argument(...,      help="e.g. 'universal render pipeline'"),
    unity_version: str = typer.Argument("6000.0", help="6000.0 | 6000.1 | 2022.3 | 2021.3"),
    config:        str = typer.Option("config.yaml", "--config", "-c"),
) -> None:
    """Resolve one Unity package description to a Package Manager ID + version."""
    cfg      = _cfg(config)
    resolver = UnityResolver(lookups_dir=_unity_pipe_cfg(cfg).get("lookups_dir", "lookups"))
    pkg_id, version = resolver.resolve_package(description, unity_version)
    if pkg_id:
        console.print(f"[cyan]{description}[/] -> [bold]{pkg_id}[/] "
                      f"[dim]{version or '(no version)'}[/]")
    else:
        console.print(f"[yellow]No match found for[/] [cyan]{description}[/]")


@app.command("unity-validate")
def unity_validate(
    schema_file:    str  = typer.Argument(..., help="Path to a Unity schema JSON file"),
    config:         str  = typer.Option("config.yaml", "--config", "-c"),
    blender_models: bool = typer.Option(False, "--blender-models",
                                        help="Build Blender FBX models (player/enemy/vehicle) "
                                             "and wire them into the regenerated project"),
) -> None:
    """Recompile a saved Unity schema to a project folder without re-running the LLM."""
    cfg      = _cfg(config)
    pipe_cfg = _unity_pipe_cfg(cfg)
    data     = json.loads(Path(schema_file).read_text(encoding="utf-8"))
    schema   = UnitySchema.model_validate(data)
    resolver = UnityResolver(lookups_dir=pipe_cfg.get("lookups_dir", "lookups"))
    schema   = resolver.resolve_schema(schema)
    schema   = resolver.inject_required_packages(schema)
    deps     = resolver.build_manifest_deps(
        schema, inject_mcp_bridge=pipe_cfg.get("inject_mcp_bridge", True)
    )
    model_overrides: dict = {}
    if blender_models:
        from src.unity_pipeline import UnityPipeline
        concept = schema.meta.get("concept", "") if schema.meta else ""
        model_overrides = UnityPipeline(cfg)._blender_model_overrides(schema, concept)
    gen      = UnityProjectGenerator(
        template_dir=pipe_cfg.get("unity_template_dir", "templates/unity")
    )
    output_dir = pipe_cfg.get("unity_output_dir", "output/unity")
    root, count = gen.write(schema, deps, output_dir,
                            model_overrides=model_overrides or None)
    # Write schema sidecar so unity-model --unity-project can find and re-inject models.
    try:
        (root / f"{schema.project_name}.schema.json").write_text(
            schema.model_dump_json(indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    console.print(f"[green]OK[/]  {count} files -> {root}")


@app.command("unity-migrate")
def unity_migrate(
    schema_file: str         = typer.Argument(..., help="Path to a Unity .schema.json file"),
    in_place:    bool        = typer.Option(False, "--in-place", "-i",
                                            help="Overwrite the source file"),
    output:      Optional[str] = typer.Option(None, "--output", "-o",
                                              help="Write migrated schema to this path"),
    dry_run:     bool        = typer.Option(False, "--dry-run",
                                            help="Print the migrated JSON without writing"),
) -> None:
    """Upgrade a data-first (v1) schema.json to a capability-aware (v2) schema.

    Synthesises a `capabilities` block from the existing `features` list and
    `locomotion` field.  Idempotent — already-migrated schemas pass through unchanged.

    \b
    Examples:
      python main.py unity-migrate MyGame.schema.json
      python main.py unity-migrate MyGame.schema.json --in-place
      python main.py unity-migrate MyGame.schema.json --output MyGame.v2.schema.json
    """
    from src.capability_resolver import migrate_schema_v1_to_v2

    src = Path(schema_file)
    if not src.exists():
        console.print(f"[red]Error:[/] file not found: {src}")
        raise typer.Exit(1)

    data    = json.loads(src.read_text(encoding="utf-8"))
    updated = migrate_schema_v1_to_v2(data)

    if "capabilities" not in data:
        added_caps = updated.get("capabilities", {})
        console.print(f"[green]Migrated[/] — added capabilities block: "
                      f"{list(added_caps.keys())}")
    else:
        console.print("[yellow]Already v2[/] — capabilities block already present, no changes")

    if dry_run:
        console.print(json.dumps(updated, indent=2))
        return

    if output:
        dest = Path(output)
    elif in_place:
        dest = src
    else:
        dest = src.with_suffix("").with_suffix(".v2.schema.json")

    dest.write_text(json.dumps(updated, indent=2), encoding="utf-8")
    console.print(f"[green]Written[/] → {dest}")


# ── Unity micro-generator subcommands ─────────────────────────────────────────

def _run_micro(kind: str, prompt: Optional[str], file: Optional[Path],
               variant: Optional[str], theme: Optional[str],
               mashup_with: Optional[str], seed: Optional[int],
               out: Optional[str], config: str, model: Optional[str] = None) -> None:
    from src.micro import REGISTRY
    if kind not in REGISTRY:
        console.print(f"[red]Error:[/] unknown micro kind: {kind}")
        raise typer.Exit(1)

    if theme and theme.lower() == "list":
        from src.themes import list_themes
        for t in list_themes():
            console.print(f"  [cyan]{t.name}[/]  —  {t.display}")
        raise typer.Exit(0)

    if file is not None:
        if not file.exists():
            console.print(f"[red]Error:[/] file not found: {file}")
            raise typer.Exit(1)
        prompt = file.read_text(encoding="utf-8").strip()

    if not prompt:
        console.print(f"[red]Error:[/] provide a prompt or use --file <path>")
        raise typer.Exit(1)

    pipeline_cls = REGISTRY[kind]
    pipeline_cls(_cfg(config, model)).run(
        prompt,
        variant=variant,
        theme=theme,
        mashup_with=mashup_with,
        seed=seed,
        out=out,
    )


def _micro_common_opts() -> dict:
    return {}   # placeholder, kept for clarity


@app.command("unity-controller")
def unity_controller(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="first_person|third_person|top_down|2d_platformer|2d_topdown|vehicle_car|vehicle_bike|flying|swimming"),
    theme:   Optional[str]  = typer.Option(None, "--theme", help="Theme name, 'random', or 'list'"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup", help="Second theme to fuse with --theme"),
    seed:    Optional[int]  = typer.Option(None, "--seed", help="RNG seed (reproducibility)"),
    out:     Optional[str]  = typer.Option(None, "--out",  help="Override output directory"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
) -> None:
    """Generate a focused Unity character controller (drop-in script + optional input asset)."""
    _run_micro("controller", prompt, file, variant, theme, mashup_with, seed, out, config, model=model)


@app.command("unity-camera")
def unity_camera(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="follow|orbit|first_person|cinemachine_3p|cinemachine_topdown|rts_pan_zoom|vehicle_chase|cinematic_dolly"),
    theme:   Optional[str]  = typer.Option(None, "--theme"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup"),
    seed:    Optional[int]  = typer.Option(None, "--seed"),
    out:     Optional[str]  = typer.Option(None, "--out"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
) -> None:
    """Generate a focused Unity camera rig (Cinemachine or plain MonoBehaviour)."""
    _run_micro("camera", prompt, file, variant, theme, mashup_with, seed, out, config, model=model)


@app.command("unity-physics")
def unity_physics(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="rigidbody_2d|rigidbody_3d|kinematic_character|ragdoll|vehicle_wheels|soft_body_stub|trigger_zone|destructible_object|buoyancy"),
    theme:   Optional[str]  = typer.Option(None, "--theme"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup"),
    seed:    Optional[int]  = typer.Option(None, "--seed"),
    out:     Optional[str]  = typer.Option(None, "--out"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
) -> None:
    """Generate a focused Unity physics/collider body."""
    _run_micro("physics", prompt, file, variant, theme, mashup_with, seed, out, config, model=model)


@app.command("unity-hud")
def unity_hud(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="health_bar|ammo_counter|minimap|crosshair|interaction_prompt|score_panel|pause_menu|dialogue_box|full_hud_bundle"),
    theme:   Optional[str]  = typer.Option(None, "--theme"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup"),
    seed:    Optional[int]  = typer.Option(None, "--seed"),
    out:     Optional[str]  = typer.Option(None, "--out"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
) -> None:
    """Generate a focused Unity Canvas / UI Toolkit HUD element."""
    _run_micro("hud", prompt, file, variant, theme, mashup_with, seed, out, config, model=model)


@app.command("unity-input")
def unity_input(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="keyboard_mouse|gamepad|touch|dual_stick|racing_wheel|vr_xr"),
    theme:   Optional[str]  = typer.Option(None, "--theme"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup"),
    seed:    Optional[int]  = typer.Option(None, "--seed"),
    out:     Optional[str]  = typer.Option(None, "--out"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
) -> None:
    """Generate a Unity Input System action map + InputReader ScriptableObject."""
    _run_micro("input", prompt, file, variant, theme, mashup_with, seed, out, config, model=model)


@app.command("unity-audio")
def unity_audio(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="2d_simple|3d_spatial|music_layers|crossfade_zones|categorical_mixer"),
    theme:   Optional[str]  = typer.Option(None, "--theme"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup"),
    seed:    Optional[int]  = typer.Option(None, "--seed"),
    out:     Optional[str]  = typer.Option(None, "--out"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
    no_llm:  bool           = typer.Option(False, "--no-llm", help="Skip the LLM; build the bank from the game-type's required cues (deterministic)"),
    clips:   bool           = typer.Option(True, "--clips/--no-clips", help="Generate actual .wav clips into Resources/Audio/"),
    unity:   Optional[str]  = typer.Option(None, "--unity", "-u", help="Overlay generated clips + manifest into this Unity project's Assets/Resources/"),
) -> None:
    """Generate a Unity audio bank (AudioCue + Bank + AudioManager) AND actual .wav clips.

    Default backend is Stable Audio 3 (config.yaml audio.backend: stable3).
    The pipeline translates the Step-2 concept brief into evocative per-cue prompts,
    unloads LM Studio to free VRAM, then runs inference.  Falls back to offline DSP
    synth when Stable Audio 3 is unavailable.  Set audio.backend: dsp to always use
    the deterministic synth, or none to skip clip generation entirely."""
    if file is not None:
        if not file.exists():
            console.print(f"[red]Error:[/] file not found: {file}"); raise typer.Exit(1)
        prompt = file.read_text(encoding="utf-8").strip()
    if not prompt:
        console.print("[red]Error:[/] provide a prompt or use --file <path>"); raise typer.Exit(1)

    from src.micro import AudioPipeline
    out_dir = AudioPipeline(_cfg(config, model)).run(
        prompt, variant=variant, theme=theme, mashup_with=mashup_with, seed=seed,
        out=out, use_llm=not no_llm, with_clips=clips)

    if unity and clips:
        import shutil
        src_res = Path(out_dir) / "Resources"
        if src_res.exists():
            dst_res = Path(unity) / "Assets" / "Resources"
            shutil.copytree(src_res, dst_res, dirs_exist_ok=True)
            console.print(f"  [green]overlay[/] -> {dst_res}")


@app.command("unity-ai")
def unity_ai(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="navmesh_patrol|chase_player|fsm_guard|behaviour_tree_lite|flocking|cover_shooter|flying_swarm"),
    theme:   Optional[str]  = typer.Option(None, "--theme"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup"),
    seed:    Optional[int]  = typer.Option(None, "--seed"),
    out:     Optional[str]  = typer.Option(None, "--out"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
) -> None:
    """Generate a Unity AI behaviour (NavMesh patrol/chase/FSM/flocking)."""
    _run_micro("ai", prompt, file, variant, theme, mashup_with, seed, out, config, model=model)


@app.command("unity-spawner")
def unity_spawner(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="wave_spawner|continuous_spawner|object_pool|trigger_spawner|point_spawner|radial_spawner"),
    theme:   Optional[str]  = typer.Option(None, "--theme", help="Theme name, 'random', or 'list'"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup", help="Second theme to fuse with --theme"),
    seed:    Optional[int]  = typer.Option(None, "--seed", help="RNG seed (reproducibility)"),
    out:     Optional[str]  = typer.Option(None, "--out"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
) -> None:
    """Generate a Unity spawner (waves, object pool, trigger/point/radial)."""
    _run_micro("spawner", prompt, file, variant, theme, mashup_with, seed, out, config, model=model)


@app.command("unity-saveload")
def unity_saveload(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="json_file|playerprefs_kv|save_slots|checkpoint|autosave|encrypted_json"),
    theme:   Optional[str]  = typer.Option(None, "--theme", help="Theme name, 'random', or 'list'"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup", help="Second theme to fuse with --theme"),
    seed:    Optional[int]  = typer.Option(None, "--seed", help="RNG seed (reproducibility)"),
    out:     Optional[str]  = typer.Option(None, "--out"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
) -> None:
    """Generate a Unity save/load system (JSON file, PlayerPrefs, slots, checkpoint, autosave)."""
    _run_micro("saveload", prompt, file, variant, theme, mashup_with, seed, out, config, model=model)


@app.command("unity-inventory")
def unity_inventory(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="slot_based|grid|weight_based|equipment|hotbar|unlimited"),
    theme:   Optional[str]  = typer.Option(None, "--theme", help="Theme name, 'random', or 'list'"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup", help="Second theme to fuse with --theme"),
    seed:    Optional[int]  = typer.Option(None, "--seed", help="RNG seed (reproducibility)"),
    out:     Optional[str]  = typer.Option(None, "--out"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
) -> None:
    """Generate a Unity inventory system (slot/grid/weight/equipment/hotbar)."""
    _run_micro("inventory", prompt, file, variant, theme, mashup_with, seed, out, config, model=model)


@app.command("unity-dialogue")
def unity_dialogue(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="linear|branching|typewriter|cutscene|barks|localized"),
    theme:   Optional[str]  = typer.Option(None, "--theme", help="Theme name, 'random', or 'list'"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup", help="Second theme to fuse with --theme"),
    seed:    Optional[int]  = typer.Option(None, "--seed", help="RNG seed (reproducibility)"),
    out:     Optional[str]  = typer.Option(None, "--out"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
) -> None:
    """Generate a Unity dialogue system (linear/branching/typewriter/cutscene/barks)."""
    _run_micro("dialogue", prompt, file, variant, theme, mashup_with, seed, out, config, model=model)


@app.command("unity-npc-dialog")
def unity_npc_dialog(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English NPC description"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="quest_giver|vendor|ambient_bark|combat_chatter|follower|cutscene"),
    theme:   Optional[str]  = typer.Option(None, "--theme", help="Theme name, 'random', or 'list'"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup", help="Second theme to fuse with --theme"),
    seed:    Optional[int]  = typer.Option(None, "--seed", help="RNG seed (reproducibility)"),
    out:     Optional[str]  = typer.Option(None, "--out"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
    no_llm:     bool           = typer.Option(False, "--no-llm", help="Skip the LLM; use preset dialog lines for the interaction type"),
    audio:      bool           = typer.Option(True,  "--audio/--no-audio", help="Generate WAV clips via Higgs Audio v2 TTS"),
    audio_style: Optional[str] = typer.Option(None,  "--audio-style", help="Override audio style: natural|stylized|robotic|whisper|radio"),
) -> None:
    """Generate NPC dialog lines + WAV clips using Higgs Audio v2 TTS.

    Produces DialogPlayer.cs + DialogTrigger.cs + DialogData.json + WAV files
    under Resources/Audio/Dialog/<speaker>/.

    Default backend: Higgs Audio v2 (config.yaml audio.backend: higgs).
    Degrades gracefully: no Higgs → text-only JSON with subtitle support.
    Set --no-audio to skip clip generation (text + C# only)."""
    if file is not None:
        if not file.exists():
            console.print(f"[red]Error:[/] file not found: {file}"); raise typer.Exit(1)
        prompt = file.read_text(encoding="utf-8").strip()
    if not prompt:
        console.print("[red]Error:[/] provide a prompt or use --file <path>"); raise typer.Exit(1)

    from src.micro import NpcDialogPipeline
    NpcDialogPipeline(_cfg(config, model)).run(
        prompt, variant=variant, theme=theme, mashup_with=mashup_with, seed=seed,
        out=out, use_llm=not no_llm, with_audio=audio, audio_style=audio_style)


@app.command("unity-menu")
def unity_menu(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="main_menu|pause_menu|settings_menu|game_over|level_select|confirm_dialog"),
    theme:   Optional[str]  = typer.Option(None, "--theme", help="Theme name, 'random', or 'list'"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup", help="Second theme to fuse with --theme"),
    seed:    Optional[int]  = typer.Option(None, "--seed", help="RNG seed (reproducibility)"),
    out:     Optional[str]  = typer.Option(None, "--out"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
) -> None:
    """Generate a Unity menu (main/pause/settings/game-over/level-select/confirm)."""
    _run_micro("menu", prompt, file, variant, theme, mashup_with, seed, out, config, model=model)


@app.command("unity-interactable")
def unity_interactable(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="pickup|door|lever|button_switch|npc_talk|readable"),
    theme:   Optional[str]  = typer.Option(None, "--theme", help="Theme name, 'random', or 'list'"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup", help="Second theme to fuse with --theme"),
    seed:    Optional[int]  = typer.Option(None, "--seed", help="RNG seed (reproducibility)"),
    out:     Optional[str]  = typer.Option(None, "--out"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
) -> None:
    """Generate a Unity interactable (pickup/door/lever/switch/npc/readable)."""
    _run_micro("interactable", prompt, file, variant, theme, mashup_with, seed, out, config, model=model)


@app.command("unity-transition")
def unity_transition(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="instant|fade|loading_bar|async_additive|trigger_door"),
    theme:   Optional[str]  = typer.Option(None, "--theme", help="Theme name, 'random', or 'list'"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup", help="Second theme to fuse with --theme"),
    seed:    Optional[int]  = typer.Option(None, "--seed", help="RNG seed (reproducibility)"),
    out:     Optional[str]  = typer.Option(None, "--out"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
) -> None:
    """Generate a Unity scene transition (instant/fade/loading bar/async/trigger door)."""
    _run_micro("transition", prompt, file, variant, theme, mashup_with, seed, out, config, model=model)


@app.command("unity-health")
def unity_health(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="simple|regenerating|shielded|armor|lives"),
    theme:   Optional[str]  = typer.Option(None, "--theme", help="Theme name, 'random', or 'list'"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup", help="Second theme to fuse with --theme"),
    seed:    Optional[int]  = typer.Option(None, "--seed", help="RNG seed (reproducibility)"),
    out:     Optional[str]  = typer.Option(None, "--out"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
) -> None:
    """Generate a Unity health system (simple/regenerating/shielded/armor/lives)."""
    _run_micro("health", prompt, file, variant, theme, mashup_with, seed, out, config, model=model)


@app.command("unity-statemachine")
def unity_statemachine(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description of the object and its states"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="player_fsm|enemy_fsm|game_fsm|object_fsm"),
    theme:   Optional[str]  = typer.Option(None, "--theme", help="Theme name, 'random', or 'list'"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup", help="Second theme to fuse with --theme"),
    seed:    Optional[int]  = typer.Option(None, "--seed", help="RNG seed (reproducibility)"),
    out:     Optional[str]  = typer.Option(None, "--out"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
) -> None:
    """Generate a Unity FSM (player_fsm/enemy_fsm/game_fsm/object_fsm).

    States and transitions are derived from the prompt concept — not generic defaults.

    \b
    Examples:
      python main.py unity-statemachine "underwater alien that stalks the player"
      python main.py unity-statemachine "deep-sea boss with 3 phases" --variant enemy_fsm
    """
    _run_micro("statemachine", prompt, file, variant, theme, mashup_with, seed, out, config, model=model)


@app.command("unity-trigger")
def unity_trigger(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description of the trigger scenario"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="zone_enter|zone_exit|timer_trigger|distance_trigger|compound"),
    theme:   Optional[str]  = typer.Option(None, "--theme", help="Theme name, 'random', or 'list'"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup", help="Second theme to fuse with --theme"),
    seed:    Optional[int]  = typer.Option(None, "--seed", help="RNG seed (reproducibility)"),
    out:     Optional[str]  = typer.Option(None, "--out"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
) -> None:
    """Generate a Unity event trigger zone (enter/exit/timer/distance/compound).

    Events fired are derived from the prompt — spawns enemies, starts dialogue, etc.

    \b
    Examples:
      python main.py unity-trigger "ambush zone that spawns enemies when player enters"
      python main.py unity-trigger "boss arena seal: lock door + start music" --variant zone_enter
    """
    _run_micro("trigger", prompt, file, variant, theme, mashup_with, seed, out, config, model=model)


@app.command("unity-animator")
def unity_animator(
    prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description of the character or object's animations"),
    file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
    variant: Optional[str]  = typer.Option(None, "--variant", help="humanoid|vehicle|creature|object|ui"),
    theme:   Optional[str]  = typer.Option(None, "--theme", help="Theme name, 'random', or 'list'"),
    mashup_with: Optional[str] = typer.Option(None, "--mashup", help="Second theme to fuse with --theme"),
    seed:    Optional[int]  = typer.Option(None, "--seed", help="RNG seed (reproducibility)"),
    out:     Optional[str]  = typer.Option(None, "--out"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m", help="Override the configured LM Studio model for this run"),
) -> None:
    """Generate a Unity Animator parameter driver (humanoid/vehicle/creature/object/ui).

    Animator params and clip names are derived from the prompt concept.

    \b
    Examples:
      python main.py unity-animator "scuba diver with swim, dive, surface, and drowning states"
      python main.py unity-animator "giant enemy crab — walk, attack, pinch, dead" --variant creature
    """
    _run_micro("animator", prompt, file, variant, theme, mashup_with, seed, out, config, model=model)


# ── Blender 3D-model micro-generator ──────────────────────────────────────────

def _parse_knobs(pairs: Optional[list[str]]) -> dict:
    """Parse repeatable `--knob key=value` flags into a param dict. Comma values
    become a list (for color), numeric strings become floats."""
    out: dict = {}
    for item in (pairs or []):
        if "=" not in item:
            console.print(f"[yellow]skip knob (need key=value):[/] {item}")
            continue
        k, v = item.split("=", 1)
        k, v = k.strip(), v.strip()
        if "," in v:
            try:
                out[k] = [float(x) for x in v.split(",")]
            except ValueError:
                out[k] = v
        else:
            try:
                out[k] = float(v)
            except ValueError:
                out[k] = v
    return out


@app.command("unity-model")
def unity_model(
    prompt:  Optional[str]  = typer.Argument(None, help="Description, e.g. 'lifted matte-black pickup' or 'tall slim humanoid'"),
    variant: Optional[str]  = typer.Option(None, "--variant",
                                           help="car | plane | humanoid | bike | furniture | weapon  (inferred from prompt if omitted)"),
    knob:    Optional[list[str]] = typer.Option(None, "--knob", "-k",
                                  help="Override a param knob (repeatable): -k wheel_r=0.8 -k color=0.02,0.02,0.02"),
    seed:    Optional[int]  = typer.Option(None, "--seed", help="RNG seed (reproducibility)"),
    rig:     bool           = typer.Option(True,  "--rig/--no-rig",
                                           help="Build armature + skin-bind the parts (game-functional FBX)"),
    demo:    bool           = typer.Option(False, "--demo",
                                           help="Pose the rig after building (visual proof; leave off for rest-pose FBX)"),
    name:    Optional[str]  = typer.Option(None, "--name", help="Asset name (PascalCase; inferred from prompt if omitted)"),
    unity_project: Optional[str] = typer.Option(None, "--unity-project", "-u",
                                  help="Unity project root — wires FBX into scene via schema regeneration; "
                                       "falls back to Assets/BlenderAssets/ if no schema.json found"),
    role:    Optional[str]  = typer.Option(None, "--role",
                                           help="Override catalog kind for scene wiring: "
                                                "player | enemy | vehicle | prop  "
                                                "(inferred from variant if omitted; humanoid defaults to 'player')"),
    emit:    bool           = typer.Option(False, "--emit",
                                           help="Emit the bpy script only (for live MCP Blender); skip headless build"),
    no_llm:  bool           = typer.Option(False, "--no-llm",
                                           help="Deterministic keyword mapping only — no LM Studio call"),
    out:     Optional[str]  = typer.Option(None, "--out", help="Override Blender output directory"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m",
                                           help="Override LM Studio model for param mapping"),
) -> None:
    """Generate a procedural 3D model in Blender (car / plane / humanoid / bike /
    furniture / weapon), export a Unity-ready FBX, and wire it into a Unity project.

    When --unity-project is given and the project has a *.schema.json sidecar
    (written by unity-create), the generator is re-run with the new FBX as a
    model_override — this updates scene.unity, EntitySpawner.cs, and ModelSwap.cs
    so the primitive placeholder is swapped for the real mesh at Play.

    Bases and their knobs
    ---------------------
    car       — body_w, body_len, body_h, cabin_h, cabin_len, cabin_pos, wheel_r,
                wheel_w, ride, bed, wheel_pairs, rim, color
    plane     — fuselage_len, fuselage_r, wing_span, wing_chord, fin_h, color
    humanoid  — height, shoulder_w, hip_w, head_r, limb_r, color
    bike      — wheelbase, wheel_r, wheel_w, frame_h, handlebar_w, rim, wheels,
                engine, tank, pedals, suspension, color
    furniture — width, depth, height, top_t, leg_r, legs, seat, back, shelves,
                drawers, cushion, color
    weapon    — length, caliber, barrel_len, stock, muzzle, magazine, scope, grip,
                foregrip, color

    \b
    Examples:
      python main.py unity-model "a lifted matte-black pickup" --no-llm
      python main.py unity-model --variant plane -k wing_span=9 -k fin_h=1.8 --no-llm
      python main.py unity-model "tall slim humanoid" --variant humanoid --no-llm
      python main.py unity-model "dirt bike" --variant bike -k suspension=1 --no-llm
      python main.py unity-model "a wooden bookcase" --variant furniture --no-llm
      python main.py unity-model "a suppressed sniper rifle" --variant weapon --no-llm
      python main.py unity-model "red sports coupe" -u output/unity/TopDownRacingGame
      python main.py unity-model "tall enemy soldier" --variant humanoid --role enemy -u output/unity/TacticalShooter
      python main.py unity-model "motorbike" --emit
    """
    from src.micro import ModelPipeline
    ModelPipeline(_cfg(config, model)).run(
        prompt, variant=variant, knobs=_parse_knobs(knob), seed=seed, rig=rig,
        demo=demo, name=name, unity_project=unity_project, role=role, emit=emit,
        use_llm=not no_llm, out=out,
    )


@app.command("unity-tools")
def unity_tools(
    action: str = typer.Argument("list", help="list — enumerate every micro-generator"),
) -> None:
    """List available Unity micro-generators."""
    from src.micro import REGISTRY
    if action == "list":
        for kind, cls in REGISTRY.items():
            console.print(f"  [cyan]unity-{kind}[/]  —  {cls.display_name}")
            console.print(f"      variants: {', '.join(cls.variants)}")
    else:
        console.print(f"[red]Unknown action:[/] {action}")
        raise typer.Exit(1)


@app.command("atom-list")
def atom_list(
    category: Optional[str] = typer.Argument(None, help="Filter by category: movement|animation|camera|audio|fx|logic|entity"),
    show_params: bool        = typer.Option(False, "--params", "-p", help="Show parameter details"),
    show_snippet: Optional[str] = typer.Option(None, "--snippet", "-s",
                                               help="Show Unity C# snippet for a named atom"),
) -> None:
    """List Phase 2 atoms — engine-agnostic building blocks for the compiler."""
    from src.atoms import list_atoms, get_atom, CATEGORIES
    from rich.table import Table
    from rich.panel import Panel

    if show_snippet:
        from src.atoms.backends.unity import unity_atom_snippet
        atom = get_atom(show_snippet)
        if atom is None:
            console.print(f"[red]Unknown atom:[/] {show_snippet}")
            raise typer.Exit(1)
        snippet = unity_atom_snippet(atom.name, {p.name: p.default for p in atom.params})
        console.print(Panel(
            snippet,
            title=f"[cyan]{atom.name}[/] — Unity C# snippet (default args)",
            subtitle=f"category: {atom.category} | duration: {atom.duration}",
            expand=False,
        ))
        return

    atoms = list_atoms(category)
    if not atoms:
        console.print(f"[yellow]No atoms found[/] for category '{category}'.")
        console.print(f"Valid categories: {', '.join(CATEGORIES)}")
        raise typer.Exit(1)

    by_cat: dict[str, list] = {}
    for a in atoms:
        by_cat.setdefault(a.category, []).append(a)

    for cat, cat_atoms in by_cat.items():
        console.print(f"\n[bold magenta]{cat.upper()}[/]")
        tbl = Table(box=None, pad_edge=False, show_header=show_params)
        tbl.add_column("Atom",        style="cyan",  no_wrap=True)
        tbl.add_column("Duration",    style="yellow", no_wrap=True)
        tbl.add_column("Description", style="dim")
        if show_params:
            tbl.add_column("Params", style="green")
        for a in cat_atoms:
            param_str = ", ".join(
                f"{p.name}:{p.type}" + (f"={p.default}" if p.default is not None else "")
                for p in a.params
            ) if show_params else ""
            row = [a.name, a.duration, a.description]
            if show_params:
                row.append(param_str)
            tbl.add_row(*row)
        console.print(tbl)

    console.print(f"\n[dim]Total: {len(atoms)} atoms | tip: --params for details, --snippet <Name> for C#[/]")


if __name__ == "__main__":
    app()
