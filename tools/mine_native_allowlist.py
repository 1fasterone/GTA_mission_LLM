#!/usr/bin/env python3
"""
Mine GTA.Native.Hash ground truth from the GTA V Script Decompiler's bundled
native tables (natives.dat / x64natives.dat), and publish the merged name set
to every consumer that needs it:

  - lookups/gta_hash_allowlist.json   (this repo; src/gta_allowlists.py)
  - <odds_dir>/hash_allowlist.json    (audit4.6.py rule 21-Y, currently dormant)

The decompiler's *.dat files are plain text `HASH:NAMESPACE:NAME` lines (see
NativeFiles.cs in the decompiler source) - no build, no game files, no .ysc
extraction required. This is a one-time/rerunnable offline data refresh, not
a pipeline step: it only ever widens the deterministic allowlist that Step 5
already consults, never touches Steps 4-7 logic itself.

Run:
    python tools/mine_native_allowlist.py --decompiler "F:/Downloads/GTA-V-Script-Decompiler-master/GTA-V-Script-Decompiler-master" [--odds "C:/Users/Owner/claude projects/Odds"] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

_LINE_RE = re.compile(r"^0x([0-9A-Fa-f]+):([^:]*):(.+)$")


def parse_native_dat(path: Path) -> set[str]:
    """Parse a decompiler HASH:NAMESPACE:NAME table into a set of NAMEs."""
    names: set[str] = set()
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        name = m.group(3).strip()
        if name:
            names.add(name.upper())
    return names


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--decompiler", required=True,
                     help="Path to the GTA-V-Script-Decompiler-master checkout")
    ap.add_argument("--odds", default=None,
                     help="Path to the Odds project (audit4.6.py). "
                          "If given, also writes hash_allowlist.json there.")
    ap.add_argument("--lookups", default="lookups",
                     help="This repo's lookups/ dir (default: ./lookups)")
    ap.add_argument("--dry-run", action="store_true",
                     help="Report the diff without writing any files")
    args = ap.parse_args()

    decompiler_root = Path(args.decompiler)
    resources = decompiler_root / "GTA V Script Decompiler" / "Resources"
    natives_dat = resources / "natives.dat"
    x64natives_dat = resources / "x64natives.dat"

    for p in (natives_dat, x64natives_dat):
        if not p.is_file():
            print(f"ABSTAIN: missing {p} - is --decompiler pointed at the "
                  f"GTA-V-Script-Decompiler-master checkout root?")
            return 1

    mined = parse_native_dat(natives_dat) | parse_native_dat(x64natives_dat)
    print(f"Mined {len(mined)} unique native names from natives.dat + x64natives.dat")

    allowlist_path = Path(args.lookups) / "gta_hash_allowlist.json"
    existing: set[str] = set()
    if allowlist_path.is_file():
        existing = set(json.loads(allowlist_path.read_text(encoding="utf-8")))

    merged = sorted(existing | mined)
    added = sorted(mined - existing)
    print(f"Existing allowlist: {len(existing)} names")
    print(f"New names to add:   {len(added)}")
    if added[:20]:
        print("Sample of new names:")
        for n in added[:20]:
            print(f"  {n}")
        if len(added) > 20:
            print(f"  ... and {len(added) - 20} more")

    if args.dry_run:
        print("\n--dry-run: no files written")
        return 0

    allowlist_path.parent.mkdir(parents=True, exist_ok=True)
    allowlist_path.write_text(
        json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {allowlist_path} ({len(merged)} names)")

    if args.odds:
        odds_path = Path(args.odds) / "hash_allowlist.json"
        odds_path.write_text(
            json.dumps(merged, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {odds_path} ({len(merged)} names) "
              f"- activates audit4.6.py rule 21-Y")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
