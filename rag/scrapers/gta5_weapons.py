"""Scrape DurtyFree weapons.json → rag/corpus/gta5/weapons.jsonl."""
import json
from pathlib import Path
import httpx

from rag.schema.weapon import make_weapon_entry

URL    = "https://raw.githubusercontent.com/DurtyFree/gta-v-data-dumps/master/weapons.json"
OUT    = Path(__file__).parent.parent / "corpus" / "gta5" / "weapons.jsonl"
SOURCE = "DurtyFree/weapons.json"


def run(force: bool = False, limit: int = 0) -> int:
    if OUT.exists() and not force:
        return sum(1 for l in OUT.open(encoding="utf-8") if l.strip())

    print(f"[weapons] fetching {URL} ...")
    r = httpx.get(URL, timeout=30, follow_redirects=True)
    r.raise_for_status()
    data = r.json()
    print(f"[weapons] downloaded {len(data)} records")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    skipped = 0
    with OUT.open("w", encoding="utf-8") as f:
        for raw in data:
            if limit and written >= limit:
                break
            entry = make_weapon_entry(raw, source=SOURCE)
            if entry is None:
                skipped += 1
                continue
            f.write(entry.to_jsonl() + "\n")
            written += 1

    print(f"[weapons] {written} written, {skipped} skipped (no model) -> {OUT}")
    return written


if __name__ == "__main__":
    run(force=True)
