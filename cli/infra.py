"""Cross-cutting commands: RAG index, web UI, atom listing."""
from __future__ import annotations

from typing import Optional

import typer

from .common import app, console, _cfg


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


# ── Engine-agnostic atom listing ─────────────────────────────────────────────

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
