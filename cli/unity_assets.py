"""Unity asset-library commands: catalog / salvage / sketchfab-fetch."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import typer
from rich.panel import Panel
from rich.table import Table
from typing import Optional

from .common import app, console, _cfg


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


@app.command("unity-api-surface")
def unity_api_surface(
    managed_dir: Optional[Path] = typer.Option(
        None, "--managed-dir", "-m",
        help="Editor/Data/Managed/UnityEngine of an installed Unity. "
             "Default: auto-detect the newest install."),
    lookups_dir: str = typer.Option(
        "lookups", "--lookups-dir", help="Where to freeze api_surface.json."),
) -> None:
    """[bold]Freeze the Unity API member surface[/] — ground truth for
    "does this member exist on this type".

    \b
    Reads the installed Editor's own XML documentation (one file beside every
    managed module DLL) and writes lookups/unity/api_surface.json. That frozen
    artifact is what the authored-code linters read; nothing in Steps 4-7 ever
    touches a Unity install.

    Rebuild it after upgrading the Editor — the file stamps the version it came
    from. Without it the member check simply abstains, so a missing artifact
    degrades to the previous behaviour rather than breaking a build.

      python main.py unity-api-surface
      python main.py unity-api-surface --managed-dir "C:/.../Managed/UnityEngine"
    """
    from src.unity_api_surface import (build_surface, discover_managed_dir,
                                       write_api_surface)

    d = Path(managed_dir) if managed_dir else discover_managed_dir()
    if d is None or not Path(d).is_dir():
        console.print(
            "[red]No Unity managed directory found.[/] Install a Unity Editor, "
            "or pass --managed-dir pointing at "
            "<Editor>/Data/Managed/UnityEngine.")
        raise typer.Exit(2)

    version = next((p for p in Path(d).parts if p and p[0].isdigit()), "")
    console.print(f"  [dim]Reading:[/] {d}")
    surface = build_surface(d)
    if not surface:
        console.print("[red]No documented types found.[/] That directory has no "
                      "UnityEngine*.xml documentation beside its DLLs.")
        raise typer.Exit(2)

    path = write_api_surface(surface, lookups_dir, unity_version=version)
    members = sum(len(v) for v in surface.values())
    console.print(Panel(
        f"[bold green]Wrote[/] {path}\n"
        f"{len(surface)} types, {members} members, Unity {version or 'unknown'}",
        style="green"))
