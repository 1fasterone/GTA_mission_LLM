#!/usr/bin/env python3
"""
blender_cli.py  --  Blender Job Manager: asset fulfilment for the Unity pipeline.

The Unity pipeline (unity_cli.py) compiles an LLM concept into a Unity project.
This CLI is the *external asset factory* that raises package quality: it turns
the LLM story into Blender jobs, runs Blender headless as a generator + sanitizer
(mesh cleanup, UV unwrap, LODs, Rigify auto-rig, animation, Unity-friendly FBX),
and returns an asset manifest you can overlay into the generated project.

Typical workflow
----------------
  # Check the environment (is Blender / a diffusion backend reachable?)
  python blender_cli.py doctor

  # Run a single hand-written job spec (the JSON contract)
  python blender_cli.py run-spec jobs/golem_01.json

  # Derive a job queue straight from a saved Unity schema + fulfil it
  python blender_cli.py from-schema output/NeonFugitive_schema.json

  # Inspect what would run, and how each asset is routed, without launching Blender
  python blender_cli.py from-schema output/NeonFugitive_schema.json --dry-run
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

from src.blender import (
    BlenderJob, BlenderJobManager,
    build_jobs_for_schema, find_blender,
)
from src.blender.diffusion import DiffusionClient, build_texture_request
from src.unity_schema import UnitySchema

console = Console()
app = typer.Typer(
    name="blender-cli",
    help="Blender Job Manager — generate + sanitize assets for the Unity pipeline.",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)


def _load_cfg(config_path: str) -> dict:
    p = Path(config_path)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _blender_cfg(cfg: dict) -> dict:
    return cfg.get("blender", {}) or {}


def _print_batch(batch) -> None:
    table = Table(title=f"{batch.total} job(s)", show_lines=False)
    table.add_column("Asset", style="cyan")
    table.add_column("Route", style="magenta")
    table.add_column("Status")
    table.add_column("Ops", style="dim", no_wrap=False)
    for r in batch.results:
        color = {"done": "green", "stubbed": "yellow", "failed": "red"}.get(
            r.status.value, "white")
        ops = ", ".join(r.manifest.operations) if r.manifest else r.error
        table.add_row(r.name, r.route, f"[{color}]{r.status.value}[/]", ops)
    console.print(table)
    style = "green" if batch.failed == 0 else "red"
    console.print(Panel(
        f"[bold]{batch.done} done · {batch.stubbed} stubbed · {batch.failed} failed[/]",
        style=style))


# ── commands ──────────────────────────────────────────────────────────────────

@app.command()
def doctor(
    config: str = typer.Option("config.yaml", "--config", "-c"),
) -> None:
    """Report whether Blender and a diffusion backend are reachable."""
    cfg = _blender_cfg(_load_cfg(config))
    inst = find_blender(cfg.get("blender_path"))
    console.print(Panel("[bold]Blender Job Manager — environment[/]", style="blue"))
    if inst:
        console.print(f"[green]Blender found[/]  {inst.version or '?'}  {inst.path}")
    else:
        console.print("[yellow]Blender not found.[/] Set [bold]blender.blender_path[/] "
                      "in config.yaml or $BLENDER_PATH. Jobs will be STUBBED.")
    diff = DiffusionClient(cfg.get("diffusion"))
    if not diff.enabled:
        console.print("[dim]Diffusion backend disabled (blender.diffusion.backend: none)[/]")
    elif diff.available():
        console.print(f"[green]Diffusion backend reachable[/] ({diff.backend} @ {diff.base_url})")
    else:
        console.print(f"[yellow]Diffusion backend configured but unreachable[/] "
                      f"({diff.backend} @ {diff.base_url}) — diffusion jobs fall back to procedural")


@app.command("run-spec")
def run_spec(
    spec: Path = typer.Argument(..., help="A job spec .json file (the JSON contract)."),
    config: str = typer.Option("config.yaml", "--config", "-c"),
    output_dir: str = typer.Option("", "--output-dir", "-o"),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Route + plan without launching Blender."),
) -> None:
    """Validate and run a single hand-written job spec."""
    if not spec.exists():
        console.print(f"[red]Spec not found:[/] {spec}")
        raise typer.Exit(1)
    cfg = _blender_cfg(_load_cfg(config))
    if output_dir:
        cfg["output_dir"] = output_dir
    mgr = BlenderJobManager(cfg)
    job = mgr.submit_spec(spec)
    # routing for a hand-written spec: honour explicit textures.source, else derive
    if "route" not in job.meta:
        from src.blender.job_builder import route_for
        job.meta["route"] = ("diffusion" if job.needs_diffusion()
                             else route_for(job.asset_type, job.style))
    console.print(Panel(f"[bold]Run job[/] {job.name} "
                        f"[dim]{job.asset_type}/{job.style}[/]", style="blue"))
    batch = mgr.run_all(dry_run=dry_run)
    _print_batch(batch)
    out = mgr.output_dir / "batch_manifest.json"
    batch.write(out)
    console.print(f"  [dim]manifest →[/] {out}")


@app.command("from-schema")
def from_schema(
    schema_file: Path = typer.Argument(..., help="A saved Unity JSON schema."),
    config: str = typer.Option("config.yaml", "--config", "-c"),
    output_dir: str = typer.Option("", "--output-dir", "-o"),
    max_jobs: int = typer.Option(12, "--max-jobs"),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="Show the derived job queue + routing only."),
) -> None:
    """Derive a Blender job queue from a Unity schema and fulfil it."""
    if not schema_file.exists():
        console.print(f"[red]Schema not found:[/] {schema_file}")
        raise typer.Exit(1)
    cfg = _blender_cfg(_load_cfg(config))
    if output_dir:
        cfg["output_dir"] = output_dir

    data = json.loads(schema_file.read_text(encoding="utf-8"))
    schema = UnitySchema.model_validate(data)
    concept = schema.meta.get("concept", "") if isinstance(schema.meta, dict) else ""

    jobs = build_jobs_for_schema(schema, concept,
                                 output_base=cfg.get("output_dir", "output/blender"),
                                 max_jobs=max_jobs)
    if not jobs:
        console.print("[yellow]No assets derived from this schema "
                      "(no entities or character scripts).[/]")
        raise typer.Exit(0)

    console.print(Panel(
        f"[bold]{schema.project_name}[/] → {len(jobs)} asset job(s)  "
        f"[dim]style={schema.visual_style}[/]", style="blue"))

    mgr = BlenderJobManager(cfg)
    mgr.submit_all(jobs)
    batch = mgr.run_all(dry_run=dry_run)
    _print_batch(batch)
    out = mgr.output_dir / "batch_manifest.json"
    batch.write(out)
    console.print(f"  [dim]manifest →[/] {out}")


@app.command("overlay-unity")
def overlay_unity(
    unity_project: Path = typer.Argument(..., help="Target Unity project root."),
    manifest: Path = typer.Option(
        Path("output/blender/batch_manifest.json"), "--manifest", "-m",
        help="batch_manifest.json produced by a run."),
    subdir: str = typer.Option("Assets/BlenderAssets", "--subdir",
                               help="Where under the project to drop assets."),
) -> None:
    """Copy a batch's FBX/textures/Alembic into a Unity project + merge packages."""
    from src.blender import load_batch, overlay_batch
    if not manifest.exists():
        console.print(f"[red]Manifest not found:[/] {manifest} — run a job first.")
        raise typer.Exit(1)
    if not (unity_project / "Assets").is_dir():
        console.print(f"[red]Not a Unity project (no Assets/):[/] {unity_project}")
        raise typer.Exit(1)
    batch = load_batch(manifest)
    res = overlay_batch(batch, unity_project, assets_subdir=subdir)
    console.print(Panel(f"[bold]Overlay → {unity_project}[/]", style="blue"))
    for f in res.copied:
        console.print(f"  [green]+[/] {f}")
    for pkg in res.packages_added:
        console.print(f"  [magenta]pkg[/] {pkg} added to Packages/manifest.json")
    for m in res.missing:
        console.print(f"  [yellow]missing[/] {m}")
    console.print(Panel(
        f"[bold]{len(res.copied)} file(s) copied · {len(res.packages_added)} package(s) added"
        f"{' · ' + str(len(res.missing)) + ' missing' if res.missing else ''}[/]",
        style="green" if res.ok else "yellow"))


@app.command()
def normalize(
    target: Path = typer.Argument(..., help="An .fbx/.abc file, or a directory to scan."),
) -> None:
    """Strip embedded timestamps from exported FBX/Alembic so identical geometry
    re-exports byte-identical (kills spurious git/Unity churn)."""
    from src.blender import normalize_export
    if target.is_dir():
        files = list(target.rglob("*.fbx")) + list(target.rglob("*.abc"))
    elif target.is_file():
        files = [target]
    else:
        console.print(f"[red]Not found:[/] {target}")
        raise typer.Exit(1)
    total = 0
    for f in files:
        kind, n = normalize_export(f)
        if n:
            total += n
            console.print(f"  [green]normalized[/] {f}  [dim]({kind}, {n} field(s))[/]")
        else:
            console.print(f"  [dim]clean[/] {f}")
    console.print(Panel(f"[bold]{total} timestamp field(s) canonicalized across "
                        f"{len(files)} file(s)[/]", style="green"))


@app.command()
def cache(
    clear: bool = typer.Option(False, "--clear", help="Delete every cached entry."),
    config: str = typer.Option("config.yaml", "--config", "-c"),
) -> None:
    """Inspect or clear the deterministic artifact cache. Identical job inputs
    reuse a stored result and skip Blender entirely (the skip-work layer); beneath
    it a geo_hash-keyed substrate dedups artifact bytes globally. Clear it to force
    re-runs (e.g. after changing a recipe outside the fingerprinted source)."""
    from src.blender import BlenderCache
    cfg = _blender_cfg(_load_cfg(config))
    cache_dir = cfg.get("cache_dir") or (Path(cfg.get("output_dir", "output/blender")) / "_cache")
    bc = BlenderCache(cache_dir, enabled=True)
    if clear:
        n = bc.clear()
        console.print(Panel(f"[bold]Cleared {n} fingerprint(s) + the geo substrate[/]\n"
                            f"[dim]{cache_dir}[/]", style="yellow"))
        return
    s = bc.stats()
    console.print(Panel(
        f"[bold]Artifact cache[/]  [dim]{s['dir']}[/]\n"
        f"  skip-work : {s['fingerprints']} fingerprint(s)\n"
        f"  geometry  : {s['meshes']} reusable pre-rig mesh(es)\n"
        f"  substrate : {s['geo_hashes']} geo_hash(es) · {s['variants']} variant(s) · "
        f"{s['blobs']} blob(s) · {s['bytes'] / 1_048_576:.1f} MB",
        style="blue"))


@app.command()
def prompt(
    spec: Path = typer.Argument(..., help="A job spec .json file."),
) -> None:
    """Show the diffusion prompt that would be constructed from a job's LLM
    description (the 'prompt constructed from the LLM description' step)."""
    data = json.loads(spec.read_text(encoding="utf-8"))
    job = BlenderJob.model_validate(data)
    req = build_texture_request(job)
    console.print(Panel(f"[bold]Diffusion prompt for {job.name}[/]", style="blue"))
    console.print(f"[green]prompt:[/]   {req.prompt}")
    console.print(f"[red]negative:[/] {req.negative}")
    console.print(f"[dim]{req.width}x{req.height}, steps={req.steps}, cfg={req.cfg_scale}[/]")


if __name__ == "__main__":
    app()
