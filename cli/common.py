"""Shared CLI state: the Typer app, console, global flags, and config helpers.

Every cli/*.py command module imports what it needs from here and registers
its commands on the same `app` instance — main.py just imports each module
for its registration side effects.
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
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr,attr-defined]
    except Exception:  # noqa: BLE001
        pass

load_dotenv()

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
