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

# The scrapers print "→" in their progress lines. Run as a subprocess from
# `main.py rag-build`, stdout is an inherited cp1252 console handle, and the
# arrow raises UnicodeEncodeError — which only ever fired with --force, because
# the early "already present" path prints no arrow. Same guard main.py uses.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass


CORPUS_DIR = Path(__file__).parent / "corpus"

SCRAPERS: dict[str, str] = {
    # GTA5
    "peds":              "rag.scrapers.gta5_peds",
    "vehicles":          "rag.scrapers.gta5_vehicles",
    "animations":        "rag.scrapers.gta5_animations",
    "scenarios":         "rag.scrapers.gta5_scenarios",
    "audio":             "rag.scrapers.gta5_audio",
    "weapons":           "rag.scrapers.gta5_weapons",
    "props":             "rag.scrapers.gta5_props",
    # Unity
    "unity_components":  "rag.scrapers.unity_components",
    "unity_api_verbs":   "rag.scrapers.unity_api_verbs",
    "unity_scene_rules": "rag.scrapers.unity_scene_rules",
    "unity_genre_atoms": "rag.scrapers.unity_genre_atoms",
    "unity_packages":    "rag.scrapers.unity_packages",
    # Shared (engine-agnostic)
    "atoms":             "rag.scrapers.atoms",
}

# Ordered so fast (static) scrapers run first; network scraper last
DEFAULT_ORDER = [
    "scenarios", "audio", "weapons", "peds", "vehicles", "props", "animations",
    "unity_components", "unity_api_verbs", "unity_scene_rules", "unity_genre_atoms",
    "unity_packages",
    "atoms",
]

UNITY_ORDER = [
    "unity_components", "unity_api_verbs", "unity_scene_rules",
    "unity_genre_atoms", "unity_packages",
]

ATOM_ORDER = ["atoms"]


def _count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def stats() -> None:
    print("\nCorpus stats:")
    grand = 0
    gta5_names = ["scenarios", "audio", "weapons", "peds", "vehicles", "props", "animations"]
    for name in gta5_names:
        path = CORPUS_DIR / "gta5" / f"{name}.jsonl"
        n = _count(path)
        status = f"{n:>6} entries" if n else "  MISSING"
        print(f"  gta5/{name}.jsonl  {status}")
        grand += n
    unity_names = {
        "unity_components":  "unity/components.jsonl",
        "unity_api_verbs":   "unity/api_verbs.jsonl",
        "unity_scene_rules": "unity/scene_rules.jsonl",
        "unity_genre_atoms": "unity/genre_atoms.jsonl",
        "unity_packages":    "unity/packages.jsonl",
    }
    for label, rel in unity_names.items():
        path = CORPUS_DIR / rel
        n = _count(path)
        status = f"{n:>6} entries" if n else "  MISSING"
        print(f"  {rel}  {status}")
        grand += n
    shared_path = CORPUS_DIR / "shared" / "atoms.jsonl"
    n = _count(shared_path)
    status = f"{n:>6} entries" if n else "  MISSING"
    print(f"  shared/atoms.jsonl  {status}")
    grand += n
    print(f"  {'Total':30} {grand:>6} entries\n")


def run_scraper(name: str, force: bool, extra_args: dict) -> int:
    mod = importlib.import_module(SCRAPERS[name])
    import inspect
    sig = inspect.signature(mod.run)
    params = set(sig.parameters)
    kwargs: dict = {}
    if "force" in params:
        kwargs["force"] = force
    if "limit" in params and "limit" in extra_args:
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

    if "--unity" in argv:
        targets = UNITY_ORDER
    elif not targets:
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
