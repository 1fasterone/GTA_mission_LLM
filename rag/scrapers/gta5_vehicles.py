"""
Fetches DurtyFree vehicles.json (921 models) and writes rag/corpus/gta5/vehicles.jsonl.
"""
import sys
from pathlib import Path
import httpx

from ..schema.vehicle import make_vehicle_entry

SOURCE = "https://raw.githubusercontent.com/DurtyFree/gta-v-data-dumps/master/vehicles.json"
OUT    = Path(__file__).parent.parent / "corpus" / "gta5" / "vehicles.jsonl"


def run(force: bool = False) -> int:
    if OUT.exists() and not force:
        count = OUT.read_text(encoding="utf-8").count("\n")
        print(f"[vehicles] already present ({count} entries). Use --force to re-scrape.")
        return count

    print(f"[vehicles] fetching {SOURCE} ...")
    with httpx.Client(timeout=180.0, follow_redirects=True) as c:
        r = c.get(SOURCE)
        r.raise_for_status()
    raw_list = r.json()
    print(f"[vehicles] downloaded {len(raw_list)} records")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    ok = err = 0
    with OUT.open("w", encoding="utf-8") as f:
        for raw in raw_list:
            try:
                entry = make_vehicle_entry(raw, SOURCE)
                f.write(entry.to_jsonl() + "\n")
                ok += 1
            except Exception as e:
                print(f"  [skip] {raw.get('Name','?')}: {e}", file=sys.stderr)
                err += 1

    print(f"[vehicles] {ok} written, {err} skipped → {OUT}")
    return ok


if __name__ == "__main__":
    run(force="--force" in sys.argv)
