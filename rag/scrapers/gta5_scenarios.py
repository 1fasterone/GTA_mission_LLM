"""
Fetches DurtyFree scenariosCompact.json (247 ped scenarios)
and writes rag/corpus/gta5/scenarios.jsonl.
"""
import sys
from pathlib import Path
import httpx

from ..schema.behavior import make_scenario_entry

SOURCE = "https://raw.githubusercontent.com/DurtyFree/gta-v-data-dumps/master/scenariosCompact.json"
OUT    = Path(__file__).parent.parent / "corpus" / "gta5" / "scenarios.jsonl"


def run(force: bool = False) -> int:
    if OUT.exists() and not force:
        count = OUT.read_text(encoding="utf-8").count("\n")
        print(f"[scenarios] already present ({count} entries). Use --force to re-scrape.")
        return count

    print(f"[scenarios] fetching {SOURCE} ...")
    with httpx.Client(timeout=60.0, follow_redirects=True) as c:
        r = c.get(SOURCE)
        r.raise_for_status()
    raw = r.json()

    # Format: plain array of scenario name strings
    names: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                names.append(item.strip())
            elif isinstance(item, dict):
                n = item.get("Name") or item.get("name", "")
                if n:
                    names.append(n)
    print(f"[scenarios] {len(names)} scenario names")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    ok = 0
    with OUT.open("w", encoding="utf-8") as f:
        for name in names:
            entry = make_scenario_entry(name, SOURCE)
            f.write(entry.to_jsonl() + "\n")
            ok += 1

    print(f"[scenarios] {ok} written → {OUT}")
    return ok


if __name__ == "__main__":
    run(force="--force" in sys.argv)
