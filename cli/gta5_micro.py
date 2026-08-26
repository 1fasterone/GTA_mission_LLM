"""GTA5 micro-generators (gta-<kind>)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from .common import app, console, _cfg


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
