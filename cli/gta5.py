"""GTA5 commands: generate / validate / resolve."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from src.code_generator import CodeGenerator
from src.mission_schema import MissionSchema
from src.pipeline import MissionPipeline
from src.resolver import ModelResolver

from .common import app, console, _cfg


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
