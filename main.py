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
  python main.py rag-status                           # check index status (gta5 + unity)

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

━━━ Sketchfab — freeze real marketplace models into the Unity catalog ━━━━━━
  python main.py sketchfab-fetch "corsair fighter plane" --category vehicle
  python main.py sketchfab-fetch "red high heel shoe" --dry-run   # preview only, no download
  #   Solo tool: searches Sketchfab, freezes the best license-compliant hit to
  #   lookups/unity/sketchfab_catalog.json; unity-create then resolves it OFFLINE.
  #   Needs SKETCHFAB_API_TOKEN + `pip install playwright && playwright install chromium` + Blender.
  #   See `python main.py sketchfab-fetch --help` for the full solo workflow.

━━━ Atoms (engine-agnostic IR building blocks) ━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python main.py atom-list                            # all atoms by category
  python main.py atom-list movement --params          # show parameter details
  python main.py atom-list --snippet MoveToTarget     # show Unity C# snippet

━━━ Enumerate tools ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  python main.py unity-tools list                     # list every micro + variants

━━━ Verification / batch (manual, slow — real Blender/Unity/LLM calls) ━━━
  python main.py blender-verify-phase3                 # emit Phase-3 script matrix
  python main.py blender-verify-run                    # run matrix against headless Blender
  python main.py blender-verify-manager                # verify BlenderJobManager reuse tiering
  python main.py unity-verify-import                   # verify Phase-3 FBX imports into Unity
  python main.py batch prompts.txt                     # batch-generate GTA5 missions
  python main.py blender-llm-test generate              # LLM-authored bpy test scripts (manual review)

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
import shutil
import sys
from pathlib import Path
from typing import Optional

import typer
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

# Render rich output (Panels use Unicode box-drawing) safely when stdout is a
# Windows cp1252 pipe — same guard as blender_cli.py.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr,attr-defined]
    except Exception:  # noqa: BLE001
        pass

# ── GTA5 pipeline ─────────────────────────────────────────────────────────────
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


def _scene_cache_dir(cfg: dict) -> Path:
    """Where GLB→FBX conversions of authored models are cached — beside the
    other Blender artifacts, keyed per source file inside."""
    blender = cfg.get("blender") or {}
    return Path(blender.get("output_dir") or "output/blender") / "scene_compiler"


def _load_authored_scene(scene_path: Path, models_dir: Optional[Path],
                         cfg: dict) -> dict:
    """Read a Scene Compiler layout (or an already-normalized scene file),
    convert any GLB items to FBX, and return the scene dict the pipeline
    consumes. Raises SceneInputError with an explicit remedy on any problem —
    an authored scene degrades to nothing useful, so it never degrades."""
    from src.unity_sceneinput import (build_scene, convert_gltf_groups,
                                      load_layout, load_scene)
    # A normalized scene file already carries absolute model paths, so
    # --models-dir has nothing to apply to; only a raw layout resolves its
    # `file` entries against a models dir.
    is_layout = "items" in (json.loads(scene_path.read_text(encoding="utf-8"))
                            if scene_path.is_file() else {})
    if models_dir is not None and is_layout:
        scene, warnings = build_scene(load_layout(scene_path), models_dir,
                                      layout_path=scene_path)
    else:
        if models_dir is not None:
            console.print("[yellow]![/] --models-dir ignored: "
                          f"{scene_path.name} is a normalized scene file, "
                          "which already carries absolute model paths")
        scene, warnings = load_scene(scene_path)
    for w in warnings:
        console.print(f"[yellow]![/] {w}")
    for msg in convert_gltf_groups(scene, _scene_cache_dir(cfg),
                                   cfg.get("blender")):
        console.print(f"[dim]{msg}[/]")
    n_inst = sum(g["count"] for g in scene["groups"])
    console.print(f"[cyan]Authored scene:[/] {len(scene['groups'])} group(s), "
                  f"{n_inst} instance(s) from {scene_path}")
    return scene


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
    gen    = CodeGenerator(
        cfg["pipeline"]["template_file"],
        minigame_template_file=cfg["pipeline"].get("minigame_template_file",
                                                   "templates/minigame.cs.j2"),
    )
    out    = gen.write(schema, cfg["pipeline"]["output_dir"])
    console.print(f"[green]OK[/]  Generated -> {out}")


@app.command()
def resolve(
    description: str = typer.Argument(..., help="e.g. 'cult member'"),
    category:    str = typer.Argument(..., help="peds | vehicles | objects | weapons"),
    config:      str = typer.Option("config.yaml", "--config", "-c"),
) -> None:
    """Resolve one GTA5 asset description to a model ID (RAG + local fallback)."""
    from src.backends import make_rag_client
    cfg      = _cfg(config)
    rag      = make_rag_client(cfg)
    resolver = ModelResolver(rag=rag, lookups_dir=cfg["pipeline"]["lookups_dir"])
    result   = resolver.resolve(description, category)
    console.print(f"[cyan]{description}[/] -> [bold]{result}[/]")


def _rag_step(args: list[str], label: str) -> None:
    """Run one RAG subprocess; surface its exit code instead of a traceback."""
    import subprocess
    code = subprocess.run(args).returncode
    if code:
        console.print(f"\n[red]RAG {label} failed[/] (exit {code}) — "
                      f"[dim]{' '.join(args[1:])}[/]")
        raise typer.Exit(code)


@app.command("rag-build")
def rag_build(
    targets: list[str] = typer.Argument(None, help="Scrapers to run (default: all)"),
    unity:   bool      = typer.Option(False, "--unity", "-u", help="Unity corpus + unity collection only"),
    force:   bool      = typer.Option(False, "--force", "-f", help="Re-scrape even if corpus exists"),
    limit:   int       = typer.Option(0, "--limit", "-l", help="Cap entries (0=unlimited)"),
) -> None:
    """Scrape data → JSONL corpus, then embed → ChromaDB index."""
    import sys
    if unity and targets:
        console.print("[red]--unity runs the whole Unity scraper set[/] — "
                      "drop the flag or drop the explicit targets.")
        raise typer.Exit(2)

    build_args = [sys.executable, "-m", "rag.build"] + (targets or [])
    if unity:   build_args.append("--unity")
    if force:   build_args.append("--force")
    if limit:   build_args += [f"--limit={limit}"]
    _rag_step(build_args, "corpus scrape")

    console.print("\n[bold]Embedding corpus into vector index ...[/]")
    idx_args = [sys.executable, "-m", "rag.indexer"]
    if unity:   idx_args += ["--domain", "unity"]
    _rag_step(idx_args, "vector index")


@app.command("rag-query")
def rag_query(
    query:  str = typer.Argument(..., help="Natural-language search term"),
    kind:   str = typer.Option("", "--kind",   "-k", help="Filter by kind: ped|vehicle|animation|package|component|atom ..."),
    domain: str = typer.Option("gta5", "--domain", "-d", help="Collection to search: gta5|unity|shared"),
    n:      int = typer.Option(5,  "--n",    "-n", help="Number of results"),
) -> None:
    """Search the local RAG vector index. Requires rag-build to have been run."""
    from rag.query import RagClient
    rag = RagClient()
    if not rag.is_ready(domain=domain):
        console.print(f"[red]Index not built for domain '{domain}'.[/] Run: [cyan]python main.py rag-build[/]")
        raise typer.Exit(1)

    kind_arg = kind or None
    hits = rag.search(query, domain=domain, kind=kind_arg, n=n)
    console.print(f"\n[bold]RAG search:[/] [cyan]{query}[/]"
                  + f"  [dim](domain={domain}" + (f", kind={kind}" if kind else "") + ")[/]")
    for i, h in enumerate(hits, 1):
        dist = h["distance"]
        color = "green" if dist < 0.4 else ("yellow" if dist < 0.55 else "red")
        console.print(
            f"  [{i}] [{color}]{h['canonical']}[/]  "
            f"[dim]{h['kind']} | {h['label']} | dist={dist:.3f}[/]"
        )
        if h.get("tags"):
            console.print(f"       [dim]tags: {h['tags']}[/]")


@app.command("rag-status")
def rag_status(
    config: str = typer.Option("config.yaml", "--config", "-c"),
) -> None:
    """Print index status for the local RAG domains (gta5 + unity)."""
    from src.backends import check_rag
    result = check_rag(_cfg(config))
    if result.get("basic_mode"):
        console.print("[yellow]Basic Mode is on[/] — RAG is skipped, local lookups only.")
        raise typer.Exit(0)
    for kind in ("gta", "unity"):
        sub = result.get(kind, {})
        domain = sub.get("domain")
        present = sub.get("present")
        if present:
            console.print(f"  [green]{kind:<5}[/] domain [cyan]{domain}[/] — index ready")
        else:
            console.print(
                f"  [red]{kind:<5}[/] domain [cyan]{domain}[/] — NOT BUILT "
                "[dim](run: python main.py rag-build"
                + ("" if kind == "gta" else " --unity") + ")[/]"
            )

    # The index can be complete while the ONNX weights are missing — that state
    # fails every embed with NO_SUCHFILE, so report it separately.
    from rag.embedder import status as embed_status
    emb = embed_status()
    if emb["complete"]:
        console.print(f"  [green]embed[/] model [cyan]{emb['model']}[/] — cached")
    elif emb["cached"]:
        console.print(f"  [red]embed[/] model [cyan]{emb['model']}[/] — cache INCOMPLETE "
                      f"[dim]{emb['cache_dir']}[/] (repaired automatically on next build)")
    else:
        console.print(f"  [yellow]embed[/] model [cyan]{emb['model']}[/] — not downloaded yet "
                      f"[dim]{emb['cache_dir']}[/]")


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
    route: Optional[Path]      = typer.Option(None, "--route",
                                              help="Route file from `unity-path` (routes.json) — compose the circuit around this authored/traced course instead of the parametric oval"),
    scene: Optional[Path]      = typer.Option(None, "--scene",
                                              help="Scene Compiler layout (layouts/<name>.json) or a scene file from `unity-scene` — the authored models AND their placement win over Sketchfab/Kenney"),
    models_dir: Optional[Path] = typer.Option(None, "--models-dir",
                                              help="--scene only: the Scene Compiler's models/ folder (default: the layout's own ../models sibling)"),
    no_think:    bool          = typer.Option(False, "--no-think",
                                              help="Disable LLM reasoning for every step (including the concept step, which normally reasons). Faster, but trades away concept creativity/quality — useful for timing/plumbing runs."),
    micro_code:  bool          = typer.Option(True, "--micro-code/--no-micro-code",
                                              help="With --compose: let the LLM author prompt-specific C# into each composed micro (one focused call per subsystem). --no-micro-code keeps the deterministic baseline bodies and is faster."),
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

    route_data = None
    if route is not None:
        if not route.exists():
            console.print(f"[red]Error:[/] route file not found: {route}")
            raise typer.Exit(1)
        route_data = json.loads(route.read_text(encoding="utf-8"))
        if not route_data.get("path_points"):
            console.print(f"[red]Error:[/] {route} has no path_points — "
                          "generate it with: python main.py unity-path ...")
            raise typer.Exit(1)

    cfg = _cfg(config, model)

    scene_data = None
    if scene is not None:
        from src.unity_sceneinput import SceneInputError
        try:
            scene_data = _load_authored_scene(scene, models_dir, cfg)
        except SceneInputError as exc:
            console.print(f"[red]Error:[/] {exc}")
            raise typer.Exit(1)

    UnityPipeline(cfg).run(prompt, theme=theme, mashup_with=mashup_with, seed=seed, compose=compose, blender_models=blender_models, route=route_data, scene=scene_data, no_think=no_think, micro_code=micro_code)


@app.command("unity-path")
def unity_path(
    points: Optional[str]  = typer.Option(None, "--points", "-p",
                                          help="Point spec: 'x,z; x,z; ...' — 2, 3 or 4 numbers per point (x,z | x,y,z | x,y,z,bank°), meters"),
    file:   Optional[Path] = typer.Option(None, "--file", "-f",
                                          help="Text file containing the same point spec (';' or newline separated)"),
    image:  Optional[Path] = typer.Option(None, "--image", "-i",
                                          help="Top-down drawing of the course (dark line on light background, or inverted)"),
    track:  Optional[str]  = typer.Option(None, "--track",
                                          help="Generate a procedural race circuit instead of authoring points. Style: flowing | technical | high_speed | kart | random"),
    seed:   int            = typer.Option(42, "--seed",
                                          help="--track only: seed for the procedural circuit (reproducible)"),
    width:  float          = typer.Option(10.0, "--width", "-w",
                                          help="Track ribbon width in meters (path_width)"),
    world_size: float      = typer.Option(160.0, "--world-size",
                                          help="Image only: meters the drawing's larger extent maps to"),
    closed: Optional[bool] = typer.Option(None, "--closed/--open",
                                          help="Force a closed loop / open course (default: auto-detect near endpoints)"),
    out:    Path           = typer.Option(Path("routes.json"), "--out", "-o",
                                          help="Route file to write"),
) -> None:
    """Author a P1-normalized route file (<= 64 points) from TEXT points or an IMAGE.

    Deterministic, LLM-free. The emitted route already honors the P1
    invariants: path_points[0] is the start line, sits at the world origin,
    and the direction of travel heads +Z. Feed it to unity-create:

    \b
      python main.py unity-path --points "0,0; 60,0; 80,40; 40,80; 0,60" --closed
      python main.py unity-path --file mytrack.txt --width 12 -o routes.json
      python main.py unity-path --image sketch.png --world-size 200 -o routes.json
      python main.py unity-path --track flowing --seed 7 -o routes.json
      python main.py unity-create "desert rally at dusk" --route routes.json
    """
    from src.unity_pathinput import build_route, parse_points_text, trace_image
    from src.unity_trackgen import STYLES, generate_track_route

    sources = [s for s in (points, file, image, track) if s is not None]
    if len(sources) != 1:
        console.print("[red]Error:[/] provide exactly one of --points / --file / --image / --track")
        raise typer.Exit(1)

    if track is not None:
        # Procedural circuit: the generator emits the SAME canonical route dict
        # (build_route → normalize) as authored/traced input, so it flows into
        # `unity-create --route` unchanged.
        if track not in STYLES:
            console.print(f"[red]Error:[/] unknown --track style {track!r}; "
                          f"choose one of {', '.join(STYLES)}")
            raise typer.Exit(1)
        route = generate_track_route(style=track, seed=seed, width=width)
        source = route["source"]
    else:
        try:
            if image is not None:
                if not image.exists():
                    console.print(f"[red]Error:[/] image not found: {image}")
                    raise typer.Exit(1)
                pts = trace_image(str(image), world_size=world_size)
                source = f"image:{image.name}"
            else:
                spec = points if points is not None else None
                if spec is None:
                    assert file is not None, "exactly one of --points/--file/--image/--track is guaranteed above"
                    if not file.exists():
                        console.print(f"[red]Error:[/] file not found: {file}")
                        raise typer.Exit(1)
                    spec = file.read_text(encoding="utf-8")
                    source = f"text:{file.name}"
                else:
                    source = "text:inline"
                pts = parse_points_text(spec)
        except ImportError:
            console.print("[red]Error:[/] --image needs Pillow: pip install pillow")
            raise typer.Exit(1)
        except ValueError as exc:
            console.print(f"[red]Error:[/] {exc}")
            raise typer.Exit(1)

        route = build_route(pts, width=width, close=closed, source=source)

    out.write_text(json.dumps(route, indent=2), encoding="utf-8")
    n = len(route["path_points"])
    kind = "closed loop" if route["closed"] else "open course"
    console.print(f"[green]OK[/]  {source} -> [bold]{out}[/]  "
                  f"({n} points, {kind}, {width:g} m wide; P1 at origin heading +Z)")
    console.print(f"  next: python main.py unity-create \"your prompt\" --route {out}")


@app.command("unity-scene")
def unity_scene(
    layout: Path            = typer.Argument(..., help="Scene Compiler layout: layouts/<name>.json"),
    models_dir: Optional[Path] = typer.Option(None, "--models-dir", "-m",
                                              help="The Scene Compiler's models/ folder (default: the layout's own ../models sibling)"),
    config: str             = typer.Option("config.yaml", "--config", "-c"),
    convert: bool           = typer.Option(True, "--convert/--no-convert",
                                           help="Convert GLB/glTF items to FBX via headless Blender (cached). --no-convert leaves them for inspection only"),
    out:    Optional[Path]  = typer.Option(None, "--out", "-o",
                                           help="Write the canonical scene file here (default: print only)"),
) -> None:
    """Normalize a Scene Compiler layout into a scene file for `unity-create --scene`.

    Deterministic, LLM-free. Converts three.js coordinates to Unity, groups
    repeated models into one entity each with baked per-instance positions and
    yaws, and converts GLB items to FBX.

    \b
      python main.py unity-scene "C:/Users/me/Documents/Blender/layouts/MONACO.json"
      python main.py unity-scene layouts/MONACO.json -o scene.json
      python main.py unity-create --file prompts/race.txt --scene scene.json
    """
    from src.unity_sceneinput import (build_scene, convert_gltf_groups,
                                      describe, load_layout, SceneInputError)
    cfg = _cfg(config)
    try:
        root = models_dir if models_dir is not None else None
        if root is None:
            from src.unity_sceneinput import default_models_dir
            root = default_models_dir(layout)
        scene, warnings = build_scene(load_layout(layout), root,
                                      layout_path=layout)
        for w in warnings:
            console.print(f"[yellow]![/] {w}")
        if convert:
            for msg in convert_gltf_groups(scene, _scene_cache_dir(cfg),
                                           cfg.get("blender")):
                console.print(f"[dim]{msg}[/]")
    except SceneInputError as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(1)

    for line in describe(scene):
        console.print(f"  {line}")
    n_inst = sum(g["count"] for g in scene["groups"])
    console.print(f"[green]OK[/]  {layout.name} -> {len(scene['groups'])} group(s), "
                  f"{n_inst} instance(s)")
    if out is not None:
        out.write_text(json.dumps(scene, indent=2), encoding="utf-8")
        console.print(f"  wrote [bold]{out}[/]")
        console.print(f"  next: python main.py unity-create \"your prompt\" --scene {out}")
    else:
        console.print(f"  next: python main.py unity-create \"your prompt\" --scene {layout}")


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


@app.command("unity-playability")
def unity_playability(
    schema_file: str = typer.Argument(..., help="Path to a Unity schema JSON file"),
    min_score:   int = typer.Option(0, "--min-score",
                                    help="Exit non-zero below this 0-100 score"),
    strict:     bool = typer.Option(False, "--strict",
                                    help="Exit non-zero on any playability ERROR"),
) -> None:
    """Grade a Unity schema on whether it is actually a GAME.

    Deterministic structural checks — no LLM, no Unity, no runtime. Every
    other command answers "did it compile"; this one answers whether the
    player has an avatar, an objective, and content they can reach, and
    whether the entities the brief named explicitly survived the build.

      python main.py unity-playability output/unity/DropZone/DropZone.schema.json
      python main.py unity-playability MyGame.schema.json --strict
    """
    from src.unity_playability import evaluate, gate

    data   = json.loads(Path(schema_file).read_text(encoding="utf-8"))
    report = evaluate(UnitySchema.model_validate(data))
    # markup=False: findings carry schema field paths that rich reads as markup.
    console.print(report.render(), markup=False)
    reason = gate(report, min_score=min_score, fail_on_error=strict)
    if reason:
        console.print(f"[red]FAIL[/] {reason}")
        raise typer.Exit(1)


@app.command("unity-eval")
def unity_eval(
    case:  list[str] = typer.Option(None, "--case",
                                    help="Case name(s) to run; default all"),
    live:  bool = typer.Option(False, "--live",
                               help="Run the LIVE tier (full pipeline through "
                                    "the LLM). Deferred — no live cases ship yet"),
    update_floors: bool = typer.Option(False, "--update-floors",
                                       help="Re-lock each case's floor to what "
                                            "it scores now (after a deliberate fix)"),
    quiet: bool = typer.Option(False, "--quiet", "-q",
                               help="One line per case instead of full scorecards"),
) -> None:
    """Playability regression eval — replay captured schemas, grade, compare.

    Each case replays a filled schema through the deterministic tail (Steps
    4-6), grades the final schema AND the files the generator emitted, and
    compares the result to a floor locked in the case file. Exits non-zero when
    a case drops below its floor, loses a check it used to pass, or stops being
    able to grade an ERROR-class check at all.

      python main.py unity-eval
      python main.py unity-eval --case dropzone_arcade
      python main.py unity-eval --update-floors

    Logs land in evals/playability/_runs/<case>.{json,txt} — the JSON names the
    pipeline stage behind every finding.
    """
    from src.unity_eval import (CASES_DIR, EvalCaseError, floor_from_result,
                                load_cases, run_cases)

    if live:
        console.print("[yellow]--live is not wired yet[/]: the live tier runs "
                      "the full pipeline from brief text through LM Studio, "
                      "but no live cases ship in evals/playability/cases yet. "
                      "Run without --live for the replay tier.")
        raise typer.Exit(2)
    try:
        cases   = load_cases(only=case or None)
        results = run_cases(cases)
    except EvalCaseError as exc:
        console.print(f"[red]eval case error[/] {exc}")
        raise typer.Exit(2) from exc

    failed = [r for r in results if not r.ok]
    for r in results:
        tag = "[green]PASS[/]" if r.ok else "[red]FAIL[/]"
        console.print(f"\n{tag} [bold]{r.case.name}[/] "
                      f"[dim]({r.case.input_kind})[/]  "
                      f"score {r.score}/100  floor {r.case.floor_score}")
        if not quiet:
            # markup=False: findings carry schema field paths and C# generics
            # that rich would read as console markup.
            console.print(r.report.render(), markup=False)
        for reason in r.reasons():
            console.print(f"  [red]![/] {reason}", markup=False)
        if not r.ok:
            for hint in r.stage_hints():
                console.print(f"  [dim]look at:[/] {hint}", markup=False)

    if update_floors:
        for r in results:
            path = CASES_DIR / f"{r.case.name}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["floor"] = floor_from_result(r)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            console.print(f"[cyan]re-locked[/] {path.name} -> "
                          f"score {r.score}, "
                          f"{len(data['floor']['must_pass'])} must-pass checks")
        return

    console.print(f"\n{len(results) - len(failed)}/{len(results)} case(s) at or "
                  f"above their floor")
    if failed:
        raise typer.Exit(1)


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
    _asset_cfg = cfg.get("assets", {}) or {}
    _sketchfab_cfg = cfg.get("sketchfab", {}) or {}
    root, count = gen.write(schema, deps, output_dir,
                            model_overrides=model_overrides or None,
                            strict_assets=bool(_asset_cfg.get("strict", True)),
                            asset_cfg=_asset_cfg, sketchfab_cfg=_sketchfab_cfg)
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


@app.command("unity-catalog")
def unity_catalog(
    config:  str  = typer.Option("config.yaml", "--config", "-c"),
    lookups: str  = typer.Option("lookups", "--lookups",
                                 help="Lookups directory (catalog output location)"),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Print stats without writing"),
    check:   bool = typer.Option(False, "--check",
                                 help="Exit 1 if the on-disk catalog is stale (CI hook)"),
    measure: bool = typer.Option(False, "--measure",
                                 help="Import every model in Unity once and record real "
                                      "bounds (slow; needed for model-aware group spacing)"),
) -> None:
    """Scan the asset library and (re)generate lookups/unity/asset_catalog.json.

    Walks the roots from config `assets.roots` (default: Models/FBX format),
    derives category/keywords/kind hints from each filename, and merges curated
    metadata from kenney_catalog.json. Output is byte-idempotent — safe to
    re-run and diff.

    \b
    Examples:
      python main.py unity-catalog                # scan + write
      python main.py unity-catalog --dry-run      # stats only
      python main.py unity-catalog --check        # CI staleness gate
    """
    from src.unity_asset_catalog import (CATALOG_RELPATH, CURATED_RELPATH,
                                         DEFAULT_ROOTS, DEFAULT_SPRITE_ROOTS,
                                         build_catalog, is_stale, write_catalog)

    try:
        asset_cfg = _cfg(config).get("assets", {}) or {}
    except FileNotFoundError:
        asset_cfg = {}
    roots        = asset_cfg.get("roots") or list(DEFAULT_ROOTS)
    sprite_roots = asset_cfg.get("sprite_roots") or list(DEFAULT_SPRITE_ROOTS)

    lookups_dir  = Path(lookups)
    curated_path = lookups_dir / CURATED_RELPATH
    out_path     = lookups_dir / CATALOG_RELPATH

    catalog = build_catalog(roots, curated_path=curated_path,
                            sprite_roots=sprite_roots)

    # Real imported sizes, measured by Unity. Without them the layout pass sizes
    # every group as if each model were ~1 unit across, so large assets bake
    # into an overlapping clump. Slow (one full import of the library), so it is
    # opt-in; previously-measured bounds are carried forward when not re-run.
    if measure:
        from src.unity_model_measure import measure_roots
        work = Path("output/unity/_measure").resolve()
        console.print(f"[cyan]Measuring[/] model bounds in Unity → {work} "
                      "(one-time, slow)…")
        sizes = measure_roots(roots, work)
        hit = 0
        for a in catalog["assets"]:
            b = sizes.get(a["relpath"])
            if b:
                a["bounds"] = [round(float(v), 4) for v in b]
                hit += 1
        console.print(f"[green]Measured[/] {hit}/{len(catalog['assets'])} assets")
        shutil.rmtree(work, ignore_errors=True)
    else:
        _prev = {}
        if out_path.is_file():
            try:
                _prev = {a["relpath"]: a.get("bounds")
                         for a in json.loads(out_path.read_text(encoding="utf-8"))["assets"]}
            except (OSError, ValueError, KeyError):
                _prev = {}
        for a in catalog["assets"]:
            b = _prev.get(a["relpath"])
            if b:
                a["bounds"] = b

    n       = len(catalog["assets"])
    n_2d    = sum(1 for a in catalog["assets"] if a["format"] != "fbx")
    by_cat  = {c: sum(1 for a in catalog["assets"] if a["category"] == c)
               for c in catalog["vocabulary"]["categories"]}
    curated_n = sum(1 for a in catalog["assets"] if a["curated"])

    console.print(f"[cyan]Catalog[/] {n} assets ({n - n_2d} 3D models, "
                  f"{n_2d} 2D sprites) from {roots} + {sprite_roots} "
                  f"({curated_n} curated) — {by_cat}")
    if n == 0:
        console.print("[red]Error:[/] no assets found — check assets.roots in config")
        raise typer.Exit(1)

    if check:
        if is_stale(catalog, out_path):
            console.print(f"[red]STALE[/] {out_path} differs from a fresh build — "
                          "run: python main.py unity-catalog")
            raise typer.Exit(1)
        console.print(f"[green]OK[/] {out_path} is up to date")
        return
    if dry_run:
        return
    write_catalog(catalog, out_path)
    console.print(f"[green]Written[/] → {out_path}")


@app.command("unity-salvage")
def unity_salvage(
    project: str = typer.Argument(..., help="Project name (under unity_pipeline.unity_output_dir) "
                                              "or a direct path to a generated Unity project"),
    dest: Path = typer.Option(Path("salvage"), "--dest", "-d",
                              help="Root folder to collect salvaged files into"),
    config: str = typer.Option("config.yaml", "--config", "-c"),
    promote_sketchfab: bool = typer.Option(True, "--promote-sketchfab/--no-promote-sketchfab",
                                           help="Also copy any Sketchfab-sourced FBX the project "
                                                "downloaded into Models/Sketchfab/ + freeze a "
                                                "catalog entry, so they survive deleting the project"),
) -> None:
    """Copy the keeper files out of a generated Unity project before you delete it.

    Sorts schema/concept/scripts/scenes/prefabs/materials/models/textures/audio/
    logs into dest/<project>/<kind>/, plus a MANIFEST.json — nothing is deleted.
    Also rescues any Sketchfab FBX the project downloaded that never made it
    into the shared Models/Sketchfab/ cache (attribution can't be reconstructed
    from the file alone, so promoted catalog entries are flagged for manual
    license verification instead of guessing).

    \b
    Examples:
      python main.py unity-salvage DirtRacerPro
      python main.py unity-salvage DirtRacerPro --dest salvage --no-promote-sketchfab
      python main.py unity-salvage C:/u/DirtRacerPro
    """
    from src.unity_salvage import promote_sketchfab_models, salvage_generation

    cfg = _cfg(config)
    try:
        result = salvage_generation(project, dest, cfg)
    except FileNotFoundError as exc:
        console.print(f"[red]Error:[/] {exc}")
        raise typer.Exit(1)

    console.print(f"[cyan]Salvaged[/] {result['total_files']} files "
                  f"({result['total_bytes'] / 1024:.0f} KB) from {result['project_dir']}")
    for bucket, n in sorted(result["counts"].items()):
        console.print(f"  {bucket:<10} {n}")
    console.print(f"[green]→[/] {result['dest_dir']}")

    if promote_sketchfab:
        promo = promote_sketchfab_models(project, cfg)
        if promo["promoted"]:
            console.print(f"\n[cyan]Promoted[/] {len(promo['promoted'])} Sketchfab model(s) "
                          f"into {promo['cache_dir']} + {promo['catalog_path']}:")
            for e in promo["promoted"]:
                console.print(f"  {e['relpath']}  [yellow](needs attribution check)[/]")
        if promo["skipped"]:
            console.print(f"[dim]Skipped {len(promo['skipped'])} already-cataloged model(s)[/]")
        if not promo["promoted"] and not promo["skipped"]:
            console.print("[dim]No Sketchfab-sourced FBX found in this project[/]")


@app.command("sketchfab-fetch")
def sketchfab_fetch(
    query:      str  = typer.Argument(..., help="Concept to search, e.g. 'red high heel shoe'"),
    config:     str  = typer.Option("config.yaml", "--config", "-c",
                                    help="Pipeline config YAML (Sketchfab token, licenses, Blender path)"),
    lookups:    str  = typer.Option("lookups", "--lookups",
                                    help="Lookups dir the frozen catalog is written under"),
    category:   str  = typer.Option("prop", "--category",
                                    help="Catalog category / entity kind for the pick"),
    kind_hints: str  = typer.Option("", "--kind-hints",
                                    help="Comma-separated entity kinds this asset should "
                                         "serve (defaults to --category)"),
    limit:      int  = typer.Option(24, "--limit",
                                    help="Number of search results to rank before picking"),
    dry_run:    bool = typer.Option(False, "--dry-run",
                                    help="Search + rank + print candidates only — no download/convert/write"),
) -> None:
    """Search Sketchfab and freeze the best licensed model into the catalog.

    Run this SOLO to hand-pick real marketplace models ahead of time — it is a
    standalone tool, independent of `unity-create`. Given a plain-English
    `query`, it searches Sketchfab, keeps only license-compliant hits, ranks
    them by keyword overlap, then downloads + Blender-converts the top pick to a
    Unity-ready FBX and writes a frozen entry into
    lookups/unity/sketchfab_catalog.json. From then on `unity-create` resolves
    that concept OFFLINE (resolver Tier S) — the network is never touched at
    generation time.

    This is the `sketchfab.auto_fetch: false` workflow: pre-populate the cache
    for the concepts you care about, same shape as `unity-catalog`. (With
    `auto_fetch: true` the resolver runs this same freeze on a cache miss instead.)

    \b
    Prerequisites (all three — otherwise the command exits with a clear error):
      * Token   — set SKETCHFAB_API_TOKEN, or sketchfab.api_token in config.yaml
      * Browser — pip install playwright && playwright install chromium
      * Blender — blender.blender_path in config.yaml (glTF -> FBX convert step)

    \b
    Solo workflow:
      1. python main.py sketchfab-fetch "corsair fighter plane" --dry-run
         -> preview the licensed candidates + authors; downloads nothing
      2. python main.py sketchfab-fetch "corsair fighter plane" --category vehicle
         -> freeze the top pick into lookups/unity/sketchfab_catalog.json
      3. python main.py unity-create "dogfight over the channel"
         -> the frozen plane is resolved offline as a Tier S model
    """
    from src.sketchfab_client import SketchfabClient
    from src.unity_asset_catalog import tokenize

    sk_cfg = (_cfg(config).get("sketchfab", {}) or {})
    client = SketchfabClient(sk_cfg)
    if not client.available():
        console.print("[red]Error:[/] no Sketchfab API token — set sketchfab.api_token "
                      "in config.yaml or the SKETCHFAB_API_TOKEN env var")
        raise typer.Exit(1)

    hints = [h.strip() for h in kind_hints.split(",") if h.strip()] or [category]

    results = client.filter_licensed(client.search(query, count=limit))
    if not results:
        console.print(f"[red]Error:[/] no license-compliant Sketchfab results for {query!r} "
                      f"(accepted: {', '.join(sorted(client.accepted_licenses))})")
        raise typer.Exit(1)

    def _rank_key(m: dict) -> float:
        """Ranks RAW Sketchfab search-result dicts (has `name`) for the
        display table below — distinct from `_score_fn`, which scores the
        catalog-entry-shaped dicts fetch_and_freeze() actually calls its
        score_fn with (has `keywords`, not `name`)."""
        kw = set(tokenize(m.get("name") or ""))
        return float(len(kw & set(tokenize(query))))

    def _score_fn(entry: dict) -> float:
        """The score_fn fetch_and_freeze() calls — receives a synthetic,
        catalog-entry-shaped dict (src.sketchfab_freeze._synthetic_entry),
        not the raw search-result dict `_rank_key` above ranks."""
        kw = set(entry.get("keywords") or [])
        return float(len(kw & set(tokenize(query))))

    ranked = sorted(results, key=_rank_key, reverse=True)
    table = Table(title=f"Sketchfab — {len(ranked)} candidate(s) for {query!r}")
    table.add_column("Name", style="cyan")
    table.add_column("License", style="magenta")
    table.add_column("Author")
    table.add_column("UID", style="dim")
    for m in ranked[:10]:
        user = m.get("user") or {}
        table.add_row(m.get("name", "?"), client.license_slug(m) or "?",
                     user.get("displayName") or user.get("username") or "?",
                     m.get("uid", "?"))
    console.print(table)

    if dry_run:
        return

    from src.sketchfab_freeze import fetch_and_freeze
    entry = fetch_and_freeze(
        query, score_fn=_score_fn, client=client, blender_cfg=sk_cfg,
        cache_dir=sk_cfg.get("cache_dir", "Models/Sketchfab"),
        lookups_dir=lookups, category=category, kind_hints=hints,
        min_score=float(sk_cfg.get("min_match_score", 0.0)))
    if entry is None:
        console.print("[red]Error:[/] fetch/convert failed — check that Blender is "
                      "installed (blender.blender_path in config.yaml) and reachable")
        raise typer.Exit(1)
    console.print(f"[green]Written[/] → {entry['relpath']} "
                  f"(license={entry['license']}, author={entry['author']!r}) "
                  f"→ {Path(lookups) / 'unity' / 'sketchfab_catalog.json'}")


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
        console.print("[red]Error:[/] provide a prompt or use --file <path>")
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


def _register_micro_command(kind: str, variant_help: str, docstring: str) -> None:
    """Register `unity-<kind>` as a typer command wrapping `_run_micro`.

    Every simple micro-generator (one that just takes prompt/--file/--variant/
    --theme/--mashup/--seed/--out/--config/--model, with no bespoke flags) shares
    this exact CLI shape — defining it once here instead of duplicating a ~15-line
    typer command per kind keeps that shape from drifting (e.g. --variant help
    text quietly falling out of sync with the kind's real Literal variants).
    Kinds with extra flags (unity-audio, unity-npc-dialog, unity-model, ...)
    stay as their own bespoke @app.command functions below.
    """
    def _cmd(
        prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description"),
        file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
        variant: Optional[str]  = typer.Option(None, "--variant", help=variant_help),
        theme:   Optional[str]  = typer.Option(None, "--theme", help="Theme name, 'random', or 'list'"),
        mashup_with: Optional[str] = typer.Option(None, "--mashup", help="Second theme to fuse with --theme"),
        seed:    Optional[int]  = typer.Option(None, "--seed", help="RNG seed (reproducibility)"),
        out:     Optional[str]  = typer.Option(None, "--out", help="Override output directory"),
        config:  str            = typer.Option("config.yaml", "--config", "-c"),
        model:   Optional[str]  = typer.Option(None, "--model", "-m",
                                               help="Override the configured LM Studio model for this run"),
    ) -> None:
        _run_micro(kind, prompt, file, variant, theme, mashup_with, seed, out, config, model=model)

    _cmd.__doc__  = docstring
    _cmd.__name__ = f"unity_{kind}"
    app.command(f"unity-{kind}")(_cmd)


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


# ── Simple micro-generators (share the exact CLI shape above) ────────────────
# kind -> (--variant help text, command docstring). Keep each kind's variant
# list in sync with its Literal in src/micro/<kind>.py — this is the one place
# it's declared for the CLI now, instead of N near-duplicate command functions.
_SIMPLE_MICROS: dict[str, tuple[str, str]] = {
    "controller": (
        "first_person|third_person|top_down|2d_platformer|2d_topdown|platformer_2_5d|"
        "vehicle_car|vehicle_bike|flying|swimming|aircraft|fighting",
        "Generate a focused Unity character controller (drop-in script + optional input asset).",
    ),
    "camera": (
        "follow|follow_2d|follow_2_5d|orbit|first_person|cinemachine_3p|cinemachine_topdown|"
        "rts_pan_zoom|vehicle_chase|cinematic_dolly",
        "Generate a focused Unity camera rig (Cinemachine or plain MonoBehaviour).",
    ),
    "physics": (
        "rigidbody_2d|rigidbody_3d|kinematic_character|ragdoll|vehicle_wheels|soft_body_stub|"
        "trigger_zone|destructible_object|buoyancy",
        "Generate a focused Unity physics/collider body.",
    ),
    "hud": (
        "health_bar|ammo_counter|minimap|crosshair|interaction_prompt|score_panel|"
        "pause_menu|dialogue_box|full_hud_bundle|racing_hud",
        "Generate a focused Unity Canvas / UI Toolkit HUD element.",
    ),
    "input": (
        "keyboard_mouse|gamepad|touch|dual_stick|racing_wheel|vr_xr",
        "Generate a Unity Input System action map + InputReader ScriptableObject.",
    ),
    "ai": (
        "navmesh_patrol|chase_player|fsm_guard|behaviour_tree_lite|flocking|cover_shooter|"
        "flying_swarm|waypoint_follow",
        "Generate a Unity AI behaviour (NavMesh patrol/chase/FSM/flocking; falls back to "
        "direct 2D seeking with no NavMesh when composed into a 2D project).",
    ),
    "spawner": (
        "wave_spawner|continuous_spawner|object_pool|trigger_spawner|point_spawner|radial_spawner",
        "Generate a Unity spawner (waves, object pool, trigger/point/radial).",
    ),
    "saveload": (
        "json_file|playerprefs_kv|save_slots|checkpoint|autosave|encrypted_json",
        "Generate a Unity save/load system (JSON file, PlayerPrefs, slots, checkpoint, autosave).",
    ),
    "inventory": (
        "slot_based|grid|weight_based|equipment|hotbar|unlimited",
        "Generate a Unity inventory system (slot/grid/weight/equipment/hotbar).",
    ),
    "dialogue": (
        "linear|branching|typewriter|cutscene|barks|localized",
        "Generate a Unity dialogue system (linear/branching/typewriter/cutscene/barks).",
    ),
    "menu": (
        "main_menu|pause_menu|settings_menu|game_over|level_select|confirm_dialog",
        "Generate a Unity menu (main/pause/settings/game-over/level-select/confirm).",
    ),
    "interactable": (
        "pickup|door|lever|button_switch|npc_talk|readable",
        "Generate a Unity interactable (pickup/door/lever/switch/npc/readable).",
    ),
    "transition": (
        "instant|fade|loading_bar|async_additive|trigger_door",
        "Generate a Unity scene transition (instant/fade/loading bar/async/trigger door).",
    ),
    "health": (
        "simple|regenerating|shielded|armor|lives",
        "Generate a Unity health system (simple/regenerating/shielded/armor/lives).",
    ),
    "objectives": (
        "tracker",
        "Generate a Unity mission objective tracker (progress + completion events).",
    ),
    "behavior_tree": (
        "tree",
        "Generate a minimal Unity behavior-tree interpreter (Sequence/Selector; "
        "Action/Condition leaves are SendMessage/RegisterCondition hooks, not autonomous AI).",
    ),
    "statemachine": (
        "player_fsm|enemy_fsm|game_fsm|object_fsm",
        """Generate a Unity FSM (player_fsm/enemy_fsm/game_fsm/object_fsm).

        States and transitions are derived from the prompt concept — not generic defaults.

        \b
        Examples:
          python main.py unity-statemachine "underwater alien that stalks the player"
          python main.py unity-statemachine "deep-sea boss with 3 phases" --variant enemy_fsm
        """,
    ),
    "trigger": (
        "zone_enter|zone_exit|timer_trigger|distance_trigger|compound",
        """Generate a Unity event trigger zone (enter/exit/timer/distance/compound).

        Events fired are derived from the prompt — spawns enemies, starts dialogue, etc.

        \b
        Examples:
          python main.py unity-trigger "ambush zone that spawns enemies when player enters"
          python main.py unity-trigger "boss arena seal: lock door + start music" --variant zone_enter
        """,
    ),
    "animator": (
        "humanoid|vehicle|creature|object|ui",
        """Generate a Unity Animator parameter driver (humanoid/vehicle/creature/object/ui).

        Animator params and clip names are derived from the prompt concept.

        \b
        Examples:
          python main.py unity-animator "scuba diver with swim, dive, surface, and drowning states"
          python main.py unity-animator "giant enemy crab — walk, attack, pinch, dead" --variant creature
        """,
    ),
    "shader": (
        "color_cycle|dissolve|hit_flash|uv_scroll|outline_pulse",
        """Generate a Unity runtime shader/material controller (color cycle/dissolve/hit flash/UV scroll/outline pulse).

        Drives shader properties via MaterialPropertyBlock — never touches the shared Material asset.

        \b
        Examples:
          python main.py unity-shader "enemy flashes white when hit"
          python main.py unity-shader "collectible pulses a glowing outline" --variant outline_pulse
        """,
    ),
    "lighting": (
        "three_point|outdoor_sun|interior_practical|flat_2d|stylized_flood",
        """Generate a Unity lighting rig + environment-lighting guard.

        Owns the light rig (key/fill/rim/practical/flood) AND RenderSettings ambient,
        skybox and reflections — the runtime guard that stops materials rendering
        black on Play. Always composed by `unity-create --compose`.

        \b
        Examples:
          python main.py unity-lighting "neon-lit rain-slick alley at midnight" --variant interior_practical
          python main.py unity-lighting "harsh desert noon, long hard shadows" --variant outdoor_sun
        """,
    ),
    "vfx": (
        "particle_system|vfx_sequence|screen_fx|impact_fx",
        """Generate a Unity VFX controller (particle system/scripted sequence/screen fx/impact fx).

        \b
        Examples:
          python main.py unity-vfx "explosion burst when the barrel is shot"
          python main.py unity-vfx "screen flashes red and fades when the player takes damage" --variant screen_fx
        """,
    ),
}
for _kind, (_variant_help, _doc) in _SIMPLE_MICROS.items():
    _register_micro_command(_kind, _variant_help, _doc)
del _kind, _variant_help, _doc


# ── GTA5 micro-generators (gta-<kind>) ────────────────────────────────────────
# Same shared CLI shape as the unity-<kind> commands, backed by GTA_REGISTRY —
# a separate namespace so "menu"/"hud"/... never collide with the Unity kinds.

def _run_gta_micro(kind: str, prompt: Optional[str], file: Optional[Path],
                   variant: Optional[str], theme: Optional[str],
                   mashup_with: Optional[str], seed: Optional[int],
                   out: Optional[str], config: str, model: Optional[str] = None) -> None:
    from src.micro import GTA_REGISTRY
    if kind not in GTA_REGISTRY:
        console.print(f"[red]Error:[/] unknown GTA micro kind: {kind}")
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
        console.print("[red]Error:[/] provide a prompt or use --file <path>")
        raise typer.Exit(1)

    GTA_REGISTRY[kind](_cfg(config, model)).run(
        prompt, variant=variant, theme=theme,
        mashup_with=mashup_with, seed=seed, out=out,
    )


def _register_gta_micro_command(kind: str, variant_help: str, docstring: str) -> None:
    def _cmd(
        prompt:  Optional[str]  = typer.Argument(None, help="Plain-English description"),
        file:    Optional[Path] = typer.Option(None, "--file", "-f", help="Read prompt from file"),
        variant: Optional[str]  = typer.Option(None, "--variant", help=variant_help),
        theme:   Optional[str]  = typer.Option(None, "--theme", help="Theme name, 'random', or 'list'"),
        mashup_with: Optional[str] = typer.Option(None, "--mashup", help="Second theme to fuse with --theme"),
        seed:    Optional[int]  = typer.Option(None, "--seed", help="RNG seed (reproducibility)"),
        out:     Optional[str]  = typer.Option(None, "--out", help="Override output directory"),
        config:  str            = typer.Option("config.yaml", "--config", "-c"),
        model:   Optional[str]  = typer.Option(None, "--model", "-m",
                                               help="Override the configured LM Studio model for this run"),
    ) -> None:
        _run_gta_micro(kind, prompt, file, variant, theme, mashup_with, seed, out, config, model=model)

    _cmd.__doc__  = docstring
    _cmd.__name__ = f"gta_{kind}"
    app.command(f"gta-{kind}")(_cmd)


_GTA_SIMPLE_MICROS: dict[str, tuple[str, str]] = {
    "menu": (
        "settings_panel|pause_menu|leaderboard_view",
        "Generate a GTA5 native-draw overlay menu (drop-in SHVDN3 .3.cs).",
    ),
    "hud": (
        "score_ticker|wave_banner|timer_bar",
        "Generate a GTA5 round HUD drawn with natives (drop-in SHVDN3 .3.cs).",
    ),
    "spawner": (
        "radial_arena|vehicle_pursuit|prop_scatter",
        "Generate a GTA5 wave spawner (deterministic ring placement, drop-in .3.cs).",
    ),
    "waves": (
        "linear|exponential|timed_pressure",
        "Generate a GTA5 wave-escalation director (drop-in SHVDN3 .3.cs).",
    ),
    "saveload": (
        "highscore_table|settings_profile",
        "Generate a GTA5 ScriptSettings .ini persistence vault (drop-in .3.cs).",
    ),
}

for _kind, (_variant_help, _doc) in _GTA_SIMPLE_MICROS.items():
    _register_gta_micro_command(_kind, _variant_help, _doc)
del _kind, _variant_help, _doc


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
                                               help="Show C# snippet for a named atom"),
    engine: str = typer.Option("unity", "--engine", "-e",
                               help="Snippet backend: unity | gta5"),
) -> None:
    """List Phase 2 atoms — engine-agnostic building blocks for the compiler."""
    from src.atoms import list_atoms, get_atom, CATEGORIES
    from rich.table import Table
    from rich.panel import Panel

    if show_snippet:
        from src.atoms.backends import atom_snippet
        atom = get_atom(show_snippet)
        if atom is None:
            console.print(f"[red]Unknown atom:[/] {show_snippet}")
            raise typer.Exit(1)
        try:
            snippet = atom_snippet(engine, atom.name,
                                   {p.name: p.default for p in atom.params})
        except ValueError as e:
            console.print(f"[red]{e}[/]")
            raise typer.Exit(1)
        console.print(Panel(
            snippet,
            title=f"[cyan]{atom.name}[/] — {engine} C# snippet (default args)",
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


# ── Verification / batch tools ──────────────────────────────────────────────────
# Manual, slow harnesses (real Blender/Unity subprocess calls, or LLM calls) — not
# part of the fast generation pipeline. Kept as thin wrappers around the original
# scripts so their logic lives in one place.

@app.command("blender-verify-phase3")
def blender_verify_phase3() -> None:
    """Emit the Phase-3 Blender reuse/rig verification script matrix (no Blender call)."""
    from verify_phase3 import main as _main
    _main()
    console.print("[green]OK[/]  scripts + index.json written -> output/blender/_verify/_scripts")


@app.command("blender-verify-run")
def blender_verify_run() -> None:
    """Run the Phase-3 verification matrix against headless Blender and report PASS/FAIL.

    Requires `blender-verify-phase3` to have been run first (reads its index.json)
    and a locatable Blender install. Slow — several minutes.
    """
    from run_verify_headless import main as _main
    rc = _main()
    if rc != 0:
        raise typer.Exit(rc)
    console.print("[green]OK[/]  all Phase-3 checks passed")


@app.command("blender-verify-manager")
def blender_verify_manager() -> None:
    """Verify BlenderJobManager's cold-build / geo-reuse tiering against real Blender."""
    from run_verify_manager import main as _main
    rc = _main()
    if rc != 0:
        raise typer.Exit(rc)
    console.print("[green]OK[/]  manager reuse verification passed")


@app.command("unity-verify-import")
def unity_verify_import() -> None:
    """Import the Phase-3 verified FBX assets into a throwaway Unity project and assert correctness.

    Requires `blender-verify-phase3` + `blender-verify-run` to have produced the
    FBX files, and a locatable Unity Editor. Slow — batchmode first-run package
    resolution can take minutes.
    """
    from run_verify_unity import main as _main
    rc = _main()
    if rc != 0:
        raise typer.Exit(rc)
    console.print("[green]OK[/]  Unity import verification passed")


@app.command("batch")
def batch(
    prompts_file: str = typer.Argument(..., help="Text file, one GTA5 mission prompt per line"),
    config: str = typer.Option("config.yaml", "--config", "-c"),
) -> None:
    """Generate multiple GTA5 missions from a text file of prompts (one per line, '#' comments ignored)."""
    from batch_generate import batch as _batch
    _batch(prompts_file, config)


@app.command("unity-package-cache")
def unity_package_cache(
    action: str = typer.Argument("status", help="status | fill | prime"),
    config: str = typer.Option("config.yaml", "--config", "-c"),
    features: Optional[str] = typer.Option(
        None, "--features",
        help="Comma-separated feature list to resolve packages for "
             "(default: every feature the resolver knows)"),
    unity_version: str = typer.Option("6000.0", "--unity-version"),
) -> None:
    """Pre-download Unity packages so first open resolves locally, not over the network.

    Unity keeps ONE global package cache shared by every project on the machine
    (%LOCALAPPDATA%\\Unity\\cache), so a package present there is never fetched
    again. This fills a local cache from packages.unity.com once, then copies it
    in — turning every later project's first open into a no-op.

    Built-in com.unity.modules.* and the editor-bundled SRP/UI packages (URP,
    shadergraph, ugui, ui) are skipped: they ship inside the editor and are
    never downloaded.

    \b
    Actions:
      status  What the closure needs and what is already cached. No network
              writes, no downloads — safe to run any time.
      fill    Download every missing package into the local cache.
      prime   Copy the local cache into Unity's global cache. Never overwrites
              an existing entry.

    \b
    Examples:
      python main.py unity-package-cache status
      python main.py unity-package-cache fill
      python main.py unity-package-cache prime
      python main.py unity-package-cache fill --features physics,cinemachine
    """
    from pathlib import Path as _P

    from src.unity_package_cache import (
        CacheReport, fill, is_globally_cached, local_entry_dir, prime,
        resolve_closure, unity_cache_root,
    )
    from src.unity_resolver import UnityResolver
    from src.unity_schema import UnitySchema

    if action not in ("status", "fill", "prime"):
        console.print(f"[red]Unknown action:[/] {action} (expected status | fill | prime)")
        raise typer.Exit(1)

    # Everything below has a working default, so a missing config.yaml is not a
    # reason to refuse — this command is useful on a bare checkout.
    try:
        cfg = _cfg(config)
    except FileNotFoundError:
        console.print(f"[dim]No {config} — using built-in defaults.[/]")
        cfg = {}
    pc = (cfg.get("unity_pipeline", {}) or {}).get("package_cache", {}) or {}
    cache_dir = _P(pc.get("dir", "cache/unity_packages"))
    root = _P(pc["global_root"]) if pc.get("global_root") else unity_cache_root()

    feats = ([f.strip() for f in features.split(",") if f.strip()] if features else
             ["physics", "ui_toolkit", "input_system", "cinemachine", "navmesh",
              "audio", "vfx", "animation_rigging"])
    resolver = UnityResolver()
    schema = resolver.inject_required_packages(UnitySchema.model_validate({
        "project_name": "PackageCacheProbe", "game_type": "3D", "platform": "pc",
        "render_pipeline": "URP", "unity_version": unity_version, "features": feats,
    }))
    deps = resolver.build_manifest_deps(schema, inject_mcp_bridge=False)

    console.print(f"\n[bold]Unity global cache:[/] {root}")
    console.print(f"[bold]Local cache:[/]        {cache_dir.resolve()}\n")

    report = CacheReport()
    if action == "fill":
        pins = fill(deps, cache_dir, report)
    else:
        pins = resolve_closure(deps, meta_dir=cache_dir / "_metadata", report=report)
    if action == "prime":
        prime(pins.values(), cache_dir, root, report)

    from rich.table import Table
    table = Table(show_header=True, header_style="bold")
    table.add_column("package"); table.add_column("local"); table.add_column("global")
    for name, pin in sorted(pins.items()):
        local = (local_entry_dir(cache_dir, pin) / "package" / "package.json").is_file()
        glob_ = is_globally_cached(pin, root)
        table.add_row(pin.slug,
                      "[green]yes[/]" if local else "[yellow]no[/]",
                      "[green]yes[/]" if glob_ else "[yellow]no[/]")
    console.print(table)

    if report.not_on_registry:
        console.print(f"\n[dim]Editor-bundled, nothing to cache: "
                      f"{', '.join(report.not_on_registry)}[/]")
    for slug, err in report.failed.items():
        console.print(f"[red]FAIL[/] {slug} — {err}")
    console.print(f"\n{report.summary()}")
    if action == "status":
        need = [p for n, p in pins.items()
                if not (local_entry_dir(cache_dir, p) / "package" / "package.json").is_file()]
        if need:
            console.print(f"[yellow]{len(need)} package(s) not cached locally[/] — "
                          "run: python main.py unity-package-cache fill")


@app.command("blender-llm-test")
def blender_llm_test(
    action: str = typer.Argument("generate", help="generate | repair"),
    name:    Optional[str] = typer.Argument(None, help="test case name (repair only): airplane | jeep | humanoid"),
    errfile: Optional[str] = typer.Argument(None, help="path to an error/traceback file (repair only)"),
) -> None:
    """Prompt the local LLM to author bpy scripts for airplane/jeep/humanoid test cases.

    Output is written for manual review + execution via the connected Blender MCP —
    this command does not touch Blender itself, and generated code MUST be reviewed
    before running it in a live scene.
    """
    from live_llm_blender_test import generate as _generate, repair as _repair
    if action == "repair":
        if not name or not errfile:
            console.print("[red]Error:[/] repair requires <name> <errfile>")
            raise typer.Exit(1)
        _repair(name, errfile)
    elif action == "generate":
        _generate()
    else:
        console.print(f"[red]Unknown action:[/] {action} (expected generate | repair)")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
