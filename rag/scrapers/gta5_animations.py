"""
Fetches DurtyFree animDictsCompact.json (20,179 dicts / 269K clips)
and writes rag/corpus/gta5/animations.jsonl.

Full scrape is ~90 MB of JSON. Use --limit N to cap during development.
"""
import sys
from pathlib import Path
import httpx

from ..schema.animation import make_anim_entry

SOURCE = "https://raw.githubusercontent.com/DurtyFree/gta-v-data-dumps/master/animDictsCompact.json"
OUT    = Path(__file__).parent.parent / "corpus" / "gta5" / "animations.jsonl"


def run(force: bool = False, limit: int = 0) -> int:
    if OUT.exists() and not force:
        count = OUT.read_text(encoding="utf-8").count("\n")
        print(f"[animations] already present ({count} dicts). Use --force to re-scrape.")
        return count

    print(f"[animations] fetching {SOURCE} ...")
    with httpx.Client(timeout=300.0, follow_redirects=True) as c:
        r = c.get(SOURCE)
        r.raise_for_status()
    raw_list = r.json()
    print(f"[animations] downloaded {len(raw_list)} dicts")

    if limit:
        raw_list = raw_list[:limit]
        print(f"[animations] limited to {limit} dicts")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    ok = err = 0
    with OUT.open("w", encoding="utf-8") as f:
        for raw in raw_list:
            try:
                # DurtyFree format: {DictionaryName, Animations: [...]}
                dict_name = raw.get("DictionaryName") or raw.get("Name", "")
                clips     = raw.get("Animations") or raw.get("Clips") or []
                if not dict_name:
                    continue
                entry = make_anim_entry(dict_name, clips, SOURCE)
                f.write(entry.to_jsonl() + "\n")
                ok += 1
            except Exception as e:
                print(f"  [skip] {raw.get('DictionaryName','?')}: {e}", file=sys.stderr)
                err += 1

    print(f"[animations] {ok} written, {err} skipped → {OUT}")
    return ok


if __name__ == "__main__":
    limit = 0
    for arg in sys.argv[1:]:
        if arg.startswith("--limit="):
            limit = int(arg.split("=")[1])
    run(force="--force" in sys.argv, limit=limit)
