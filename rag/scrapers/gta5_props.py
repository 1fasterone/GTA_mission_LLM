"""
Scrape DurtyFree ObjectList.ini → rag/corpus/gta5/props.jsonl.

Filters the full 21k-object list down to ~3k mission-relevant props
using keyword matching. Skips LOD/proxy/geometry-only objects.
"""
import re
from pathlib import Path
import httpx

from rag.schema.prop import make_prop_entry

URL    = "https://raw.githubusercontent.com/DurtyFree/gta-v-data-dumps/master/ObjectList.ini"
OUT    = Path(__file__).parent.parent / "corpus" / "gta5" / "props.jsonl"
SOURCE = "DurtyFree/ObjectList.ini"

# Name must contain at least one of these keywords to be kept.
_MISSION_KW = [
    "laptop", "phone", "tablet", "radio", "camera", "cam", "server",
    "bag", "brief", "suitcase", "luggage", "case", "crate", "box", "locker",
    "barrel", "drum", "tank", "gas", "gascan", "fuel",
    "money", "cash", "dollar", "bank", "gold", "bullion", "bling", "jewel",
    "drug", "meth", "weed", "coke", "narco", "acetone", "powder",
    "ammo", "ammunition",
    "bomb", "explo", "mine", "grenade", "molotov",
    "missile", "rocket", "warhead",
    "key", "keycard", "badge", "fob", "keypad",
    "safe", "vault",
    "med", "health", "medic", "first_aid",
    "file", "doc", "note", "evidence", "folder",
    "trophy", "award", "prize",
    "pick", "drop", "collect",
]

# Reject names containing these patterns — world geometry, not spawnable props.
_SKIP_RE = re.compile(
    r"(_lod\d*|_slod\d*|_proxy|_phys|_emissive|_em$|_glassfix"
    r"|_det$|_detail|_generic|_col$|_shadow|_decal|_overlay"
    r"|_shad$|_spec$|_interior$|_ext$)"
)


def _is_mission_prop(name: str) -> bool:
    low = name.lower()
    if _SKIP_RE.search(low):
        return False
    return any(kw in low for kw in _MISSION_KW)


def run(force: bool = False, limit: int = 0) -> int:
    if OUT.exists() and not force:
        return sum(1 for l in OUT.open(encoding="utf-8") if l.strip())

    print(f"[props] fetching {URL} ...")
    r = httpx.get(URL, timeout=30, follow_redirects=True)
    r.raise_for_status()
    lines = [l.strip() for l in r.text.splitlines() if l.strip()]
    print(f"[props] downloaded {len(lines)} total objects")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    written  = 0
    filtered = 0
    with OUT.open("w", encoding="utf-8") as f:
        for name in lines:
            if limit and written >= limit:
                break
            if not _is_mission_prop(name):
                filtered += 1
                continue
            entry = make_prop_entry(name, source=SOURCE)
            f.write(entry.to_jsonl() + "\n")
            written += 1

    print(f"[props] {written} written, {filtered} filtered -> {OUT}")
    return written


if __name__ == "__main__":
    run(force=True)
