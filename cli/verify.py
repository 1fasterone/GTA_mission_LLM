"""Manual, slow verification / batch harnesses (real Blender/Unity subprocess
calls, or LLM calls) — not part of the fast generation pipeline. Kept as thin
wrappers around the original scripts so their logic lives in one place."""
from __future__ import annotations

from typing import Optional

import typer

from .common import app, console, _cfg


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
