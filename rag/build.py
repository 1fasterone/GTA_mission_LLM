"""
rag/build.py — Populate rag/corpus/ from public data sources.

Usage:
    python -m rag.build                  # run all scrapers
    python -m rag.build peds vehicles    # run specific scrapers
    python -m rag.build --force          # re-scrape even if corpus exists
    python -m rag.build --stats          # print corpus stats and exit
    python -m rag.build animations --limit=2000   # cap anim scrape for dev
"""
import sys
import importlib
from pathlib import Path


CORPUS_DIR = Path(__file__).parent / "corpus"

SCRAPERS: dict[str, str] = {
    "peds":       "rag.scrapers.gta5_peds",
    "vehicles":   "rag.scrapers.gta5_vehicles",
    "animations": "rag.scrapers.gta5_animations",
    "scenarios":  "rag.scrapers.gta5_scenarios",
    "audio":      "rag.scrapers.gta5_audio",
    "weapons":    "rag.scrapers.gta5_weapons",
    "props":      "rag.scrapers.gta5_props",
}

# Ordered so fast scrapes run first
DEFAULT_ORDER = ["scenarios", "audio", "weapons", "peds", "vehicles", "props", "animations"]


def _count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def stats() -> None:
    print("\nCorpus stats:")
    grand = 0
    for name in DEFAULT_ORDER:
        path = CORPUS_DIR / "gta5" / f"{name}.jsonl"
        n    = _count(path)
        status = f"{n:>6} entries" if n else "  MISSING"
        print(f"  gta5/{name}.jsonl  {status}")
        grand += n
    print(f"  {'Total':30} {grand:>6} entries\n")


def run_scraper(name: str, force: bool, extra_args: dict) -> int:
    mod = importlib.import_module(SCRAPERS[name])
    kwargs = {"force": force}
    if name == "animations" and "limit" in extra_args:
        kwargs["limit"] = extra_args["limit"]
    return mod.run(**kwargs)


def main() -> None:
    argv   = sys.argv[1:]
    force  = "--force"  in argv
    do_stats = "--stats" in argv
    limit  = 0
    for a in argv:
        if a.startswith("--limit="):
            limit = int(a.split("=")[1])
    targets = [a for a in argv if not a.startswith("--")]

    if do_stats:
        stats()
        return

    if not targets:
        targets = DEFAULT_ORDER

    invalid = [t for t in targets if t not in SCRAPERS]
    if invalid:
        print(f"Unknown scrapers: {invalid}. Valid: {list(SCRAPERS)}", file=sys.stderr)
        sys.exit(1)

    extra = {"limit": limit} if limit else {}
    results: dict[str, int] = {}
    for name in targets:
        print(f"\n{'-'*50}")
        results[name] = run_scraper(name, force, extra)

    print(f"\n{'='*50}")
    print("Build complete:")
    for name, n in results.items():
        print(f"  {name:15} {n:>6} entries")
    stats()


if __name__ == "__main__":
    main()
