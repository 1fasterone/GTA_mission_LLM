"""Unity asset-library commands: catalog / salvage / sketchfab-fetch / sketchfab-audit."""
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
    embed:   bool = typer.Option(False, "--embed",
                                 help="Precompute BAAI/bge-small-en-v1.5 embeddings for every "
                                      "(non-Sketchfab) catalog entry, cached to lookups/unity/"
                                      "asset_catalog_embeddings.{npz,meta.json} — needed for "
                                      "assets.embedding_select: true (docs/COMPILER_AUDIT.md "
                                      "Gap E). Slow, one-time; first run may need network to "
                                      "fetch the ONNX model into the fastembed cache."),
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
      python main.py unity-catalog --embed        # + precompute embeddings
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

    if embed:
        from src.unity_semantic_match import build_and_write_catalog_embeddings
        console.print("[cyan]Embedding[/] catalog entries (BAAI/bge-small-en-v1.5)…")
        emb_path = build_and_write_catalog_embeddings(catalog, lookups_dir=str(lookups_dir))
        console.print(f"[green]Written[/] → {emb_path}")


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
    semantic_select: Optional[bool] = typer.Option(
        None, "--semantic-select/--no-semantic-select",
        help="Gate candidates by CLIP/embedding similarity before download "
             "(src.unity_semantic_embed) — deterministic, no LLM. Defaults to "
             "config.yaml's sketchfab.semantic_select when unset."),
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

    # Deterministic CLIP/embedding gate (src.unity_semantic_embed) -- the
    # same one unity_entity_asset_resolver.py wires into unity-create's
    # Tier S, ported here so a solo sketchfab-fetch freeze gets the same
    # protection instead of trusting keyword overlap alone. Byte-reproducible
    # (pinned ONNX weights via fastembed, no LLM, no sampling): a candidate
    # only clears the gate if its own thumbnail/description actually measures
    # up, closing exactly the "realistic paratrooper" -> "Cartoon Character"
    # miss keyword scoring cannot catch on its own.
    use_semantic = sk_cfg.get("semantic_select") if semantic_select is None else semantic_select
    semantic_filter_fn = None
    if use_semantic:
        from src.unity_semantic_embed import semantic_filter
        semantic_filter_fn = lambda cands, _q=query, _kind=(hints[0] if hints else category): semantic_filter(
            _q, _kind, _q, cands, client=client,
            # Defaults match config.example.yaml's calibrated values
            # (tools/calibrate_semantic_score.py) — kept in sync there.
            min_image_similarity=float(sk_cfg.get("min_image_similarity", 0.22)),
            min_text_similarity=float(sk_cfg.get("min_text_similarity", 0.45)))
        console.print("[dim]semantic gate: on "
                      f"(image>={sk_cfg.get('min_image_similarity', 0.22)}, "
                      f"text>={sk_cfg.get('min_text_similarity', 0.45)})[/]")

    from src.sketchfab_freeze import fetch_and_freeze
    entry = fetch_and_freeze(
        query, score_fn=_score_fn, client=client, blender_cfg=sk_cfg,
        cache_dir=sk_cfg.get("cache_dir", "Models/Sketchfab"),
        lookups_dir=lookups, category=category, kind_hints=hints,
        min_score=float(sk_cfg.get("min_match_score", 0.0)),
        semantic_filter_fn=semantic_filter_fn)
    if entry is None and use_semantic:
        console.print("[yellow]Note:[/] if every candidate failed the semantic "
                      "floor, re-run with --no-semantic-select to see the "
                      "keyword-only pick, or --dry-run to inspect candidates.")
    if entry is None:
        console.print("[red]Error:[/] fetch/convert failed — check that Blender is "
                      "installed (blender.blender_path in config.yaml) and reachable")
        raise typer.Exit(1)
    console.print(f"[green]Written[/] → {entry['relpath']} "
                  f"(license={entry['license']}, author={entry['author']!r}) "
                  f"→ {Path(lookups) / 'unity' / 'sketchfab_catalog.json'}")


@app.command("sketchfab-audit")
def sketchfab_audit(
    config:        str            = typer.Option("config.yaml", "--config", "-c"),
    lookups:       str            = typer.Option("lookups", "--lookups",
                                                 help="Lookups dir holding sketchfab_catalog.json"),
    offset:        int            = typer.Option(
        0, "--offset", help="Skip the first N entries — combine with --limit "
                            "to work through a large catalog in chunks across "
                            "several runs instead of one long-lived call"),
    limit:         Optional[int]  = typer.Option(
        None, "--limit", help="Only audit N entries starting at --offset — "
                              "smoke-test a run before committing to the "
                              "whole catalog"),
    sleep_between: float          = typer.Option(
        0.5, "--sleep", help="Seconds to wait between entries (be a good citizen "
                             "of Sketchfab's API across a large catalog)"),
    apply:         bool           = typer.Option(
        False, "--apply", help="Actually remove failed entries from the catalog "
                               "(default: report only). Writes a timestamped "
                               ".bak copy first."),
) -> None:
    """Re-check every already-frozen Sketchfab catalog entry against its OWN
    search query, using the same deterministic CLIP/embedding floor
    (sketchfab.semantic_select) fetch_and_freeze applies to a candidate
    BEFORE it is ever written. Entries frozen before that gate existed, or
    while a run had it off, were never checked at all — this is the
    one-time (or periodic) pass that catches up the backlog.

    Read-only by default: prints a pass/fail/unmeasured scorecard and a
    table of the failed entries' own name/query/scores. Pass --apply to
    actually remove the failed ones (a `sketchfab_catalog.bak-<UTC
    timestamp>.json` is written first — this never deletes without a
    recovery copy) AND backfill `clip_image_embedding` onto surviving
    entries that don't already have one — a free byproduct of the thumbnail
    this command already downloaded to score them. That field is what lets
    unity_entity_asset_resolver.py's Tier S re-check a cache-hit reuse of
    these entries against a completely different LATER entity's query, with
    zero network, closing the gap where a keyword-match cache hit used to
    bypass sketchfab.semantic_select forever after one freeze. Entries that
    can't be measured at all (model deleted or made private on Sketchfab, no
    token, network down) are left untouched — missing data is not evidence
    of a bad pick.

    \b
    Examples:
      python main.py sketchfab-audit --limit 10          # smoke-test first
      python main.py sketchfab-audit                     # full dry-run report
      python main.py sketchfab-audit --apply              # report + clean up
    """
    from src.sketchfab_catalog_audit import apply_removals, audit_catalog, backfill_embeddings
    from src.sketchfab_client import SketchfabClient
    from src.unity_asset_catalog import SKETCHFAB_RELPATH

    sk_cfg = (_cfg(config).get("sketchfab", {}) or {})
    client = SketchfabClient(sk_cfg)
    if not client.available():
        console.print("[red]Error:[/] no Sketchfab API token — set sketchfab.api_token "
                      "in config.yaml or the SKETCHFAB_API_TOKEN env var")
        raise typer.Exit(1)

    catalog_path = Path(lookups) / SKETCHFAB_RELPATH
    if not catalog_path.is_file():
        console.print(f"[yellow]Nothing to audit[/] — {catalog_path} does not exist")
        return

    try:
        total_on_disk = len(json.loads(catalog_path.read_text(encoding="utf-8")).get("entries") or [])
    except (OSError, ValueError):
        total_on_disk = 0
    remaining = max(0, total_on_disk - offset)
    n_to_run = min(limit, remaining) if limit is not None else remaining
    min_img = float(sk_cfg.get("min_image_similarity", 0.22))
    min_txt = float(sk_cfg.get("min_text_similarity", 0.45))
    span = f"[{offset}:{offset + n_to_run}]" if offset else ""
    console.print(f"[cyan]Auditing[/] {n_to_run}/{total_on_disk} entries {span} in {catalog_path} "
                  f"(image>={min_img}, text>={min_txt})…")

    _VERDICT_STYLE = {"pass": "[green]OK[/]", "fail": "[red]FAIL[/]",
                      "unmeasured": "[dim]?[/]"}

    def _progress(i: int, n: int, r: dict) -> None:
        img = f"{r['image_similarity']:.3f}" if r["image_similarity"] is not None else "-"
        txt = f"{r['text_similarity']:.3f}" if r["text_similarity"] is not None else "-"
        console.print(f"  [{i}/{n}] {_VERDICT_STYLE[r['verdict']]} {r['name']} "
                      f"(q={r['query']!r}) img={img} text={txt}")

    results = audit_catalog(
        catalog_path, client=client, min_image_similarity=min_img,
        min_text_similarity=min_txt, sleep_between=sleep_between,
        offset=offset, limit=limit, progress_fn=_progress)

    n_pass = sum(1 for r in results if r["verdict"] == "pass")
    n_fail = sum(1 for r in results if r["verdict"] == "fail")
    n_unmeasured = sum(1 for r in results if r["verdict"] == "unmeasured")
    console.print(f"\n[cyan]Scorecard[/] {len(results)} checked — "
                  f"[green]{n_pass} pass[/], [red]{n_fail} fail[/], "
                  f"[dim]{n_unmeasured} unmeasured[/]")

    failed = [r for r in results if r["verdict"] == "fail"]
    if failed:
        table = Table(title=f"{len(failed)} failed the semantic floor")
        table.add_column("Name", style="cyan")
        table.add_column("Query")
        table.add_column("Image")
        table.add_column("Text")
        for r in failed:
            img = f"{r['image_similarity']:.3f}" if r["image_similarity"] is not None else "-"
            txt = f"{r['text_similarity']:.3f}" if r["text_similarity"] is not None else "-"
            table.add_row(r["name"], r["query"], img, txt)
        console.print(table)

    if not apply:
        if failed:
            console.print("[yellow]Dry run[/] — re-run with --apply to remove these from the catalog")
        return

    if failed:
        removal = apply_removals(catalog_path, {r["uid"] for r in failed})
        console.print(f"[green]Removed[/] {removal['removed']} entries — "
                      f"{removal['kept']} remain. Backup: {removal['backup_path']}")
    else:
        console.print("[green]Nothing to remove[/]")

    # Passing entries already had their thumbnail downloaded to score them
    # above — stamp that same CLIP embedding into the catalog as
    # clip_image_embedding so unity_entity_asset_resolver.py's Tier S can
    # re-check a FUTURE cache-hit reuse of these entries deterministically
    # (unity_semantic_embed.semantic_filter_frozen), with no more network,
    # even though they predate that gate. Free — no extra fetches beyond
    # what audit_catalog already did.
    embeddings_by_uid = {r["uid"]: r["embedding"] for r in results
                        if r["verdict"] == "pass" and r.get("embedding")}
    if embeddings_by_uid:
        backfill = backfill_embeddings(catalog_path, embeddings_by_uid)
        if backfill["updated"]:
            console.print(f"[green]Backfilled[/] clip_image_embedding on "
                          f"{backfill['updated']} passing entr"
                          f"{'y' if backfill['updated'] == 1 else 'ies'} "
                          "(future cache-hit reuse of these can now be "
                          "re-checked with zero network)")


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
