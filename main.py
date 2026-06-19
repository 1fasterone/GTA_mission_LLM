"""
Mission & Project Creator — CLI entry point.

GTA5 mode  (AnythingLLM RAG for model-ID resolution):
  python main.py generate "Make a heist mission at the docks."
  python main.py validate schema.json
  python main.py resolve "cult member" peds
  python main.py setup-rag

Unity mode  (local lookup tables only, no RAG required):
  python main.py unity-create "A 2D platformer with Cinemachine"
  python main.py unity-create --file prompts/neon_fugitive.txt
  python main.py unity-resolve "universal render pipeline"
  python main.py unity-validate schema.json

Blender model micro (car / plane / humanoid / bike → FBX):
  python main.py unity-model "lifted matte-black pickup" --no-llm
  python main.py unity-model "tall humanoid" --variant humanoid --no-llm
  python main.py unity-model "dirt bike" --variant bike -k suspension=1 --no-llm
  python main.py unity-model "glider" --variant plane --emit

Global flags (work with any command):
  --verbose / -v   Show pipeline step output
  --debug   / -d   Show raw LLM requests and responses
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


@app.command("setup-rag")
def setup_rag(
    config: str = typer.Option("config.yaml", "--config", "-c"),
) -> None:
    """Upload GTA5 RAG documents to AnythingLLM workspace (one-time setup)."""
    import setup_rag as sr          # top-level script, kept lazy to avoid startup cost
    sr.upload_all(_cfg(config))


@app.command("rag-status")
def rag_status(
    config: str = typer.Option("config.yaml", "--config", "-c"),
) -> None:
    """Print per-workspace doc count + embedding status for both RAG workspaces."""
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
    """Bootstrap the pre-built AnythingLLM workspace (create + query mode + upload docs)."""
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
    """Launch the local web UI (setup, connection testing, and generation)."""
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
    root, count = gen.write(schema, deps, pipe_cfg.get("unity_output_dir", "output/unity"),
                            model_overrides=model_overrides or None)
    console.print(f"[green]OK[/]  {count} files -> {root}")


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
) -> None:
    """Generate a Unity audio bank (AudioCue + Bank ScriptableObjects + AudioManager)."""
    _run_micro("audio", prompt, file, variant, theme, mashup_with, seed, out, config, model=model)


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
                                           help="car | plane | humanoid | bike  (inferred from prompt if omitted)"),
    knob:    Optional[list[str]] = typer.Option(None, "--knob", "-k",
                                  help="Override a param knob (repeatable): -k wheel_r=0.8 -k color=0.02,0.02,0.02"),
    seed:    Optional[int]  = typer.Option(None, "--seed", help="RNG seed (reproducibility)"),
    rig:     bool           = typer.Option(True,  "--rig/--no-rig",
                                           help="Build armature + skin-bind the parts (game-functional FBX)"),
    demo:    bool           = typer.Option(False, "--demo",
                                           help="Pose the rig after building (visual proof; leave off for rest-pose FBX)"),
    name:    Optional[str]  = typer.Option(None, "--name", help="Asset name (PascalCase; inferred from prompt if omitted)"),
    unity_project: Optional[str] = typer.Option(None, "--unity-project", "-u",
                                  help="Unity project root — FBX is overlaid into Assets/BlenderAssets/<name>/"),
    emit:    bool           = typer.Option(False, "--emit",
                                           help="Emit the bpy script only (for live MCP Blender); skip headless build"),
    no_llm:  bool           = typer.Option(False, "--no-llm",
                                           help="Deterministic keyword mapping only — no LM Studio call"),
    out:     Optional[str]  = typer.Option(None, "--out", help="Override Blender output directory"),
    config:  str            = typer.Option("config.yaml", "--config", "-c"),
    model:   Optional[str]  = typer.Option(None, "--model", "-m",
                                           help="Override LM Studio model for param mapping"),
) -> None:
    """Generate a procedural 3D model in Blender (car / plane / humanoid / bike),
    export a Unity-ready FBX, and optionally overlay it into a Unity project.

    Bases and their knobs
    ---------------------
    car      — body_w, body_len, body_h, cabin_h, cabin_len, cabin_pos, wheel_r,
               wheel_w, ride, bed, wheel_pairs, rim, color
    plane    — fuselage_len, fuselage_r, wing_span, wing_chord, fin_h, color
    humanoid — height, shoulder_w, hip_w, head_r, limb_r, color
    bike     — wheelbase, wheel_r, wheel_w, frame_h, handlebar_w, rim, wheels,
               engine, tank, pedals, suspension, color

    \b
    Examples:
      python main.py unity-model "a lifted matte-black pickup" --no-llm
      python main.py unity-model --variant plane -k wing_span=9 -k fin_h=1.8 --no-llm
      python main.py unity-model "tall slim humanoid" --variant humanoid --no-llm
      python main.py unity-model "dirt bike" --variant bike -k suspension=1 --no-llm
      python main.py unity-model "red sports coupe" -u output/unity/TopDownRacingGame
      python main.py unity-model "motorbike" --emit
    """
    from src.micro import ModelPipeline
    ModelPipeline(_cfg(config, model)).run(
        prompt, variant=variant, knobs=_parse_knobs(knob), seed=seed, rig=rig,
        demo=demo, name=name, unity_project=unity_project, emit=emit,
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


if __name__ == "__main__":
    app()
