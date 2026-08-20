#!/usr/bin/env python3
"""
Mine native call argument-shapes (arity) from real decompiled Rockstar
mission scripts.

Source corpus: F:/Downloads/_ysc_v2/exported - 715 .c files produced by
GTA-V-Script-Decompiler's "Export > Directory" batch pass over .ysc scripts
extracted from a real, owned GTA V Enhanced install (see
tools/mine_native_allowlist.py for the sibling name-only mining pass this
extends). Extraction/decompilation is deterministic and offline; this script
adds no new keys, no LLM calls, and touches no pipeline step - it only
widens ground-truth data Step 5's fixer and audit4.6.py may consult.

Method: for every call site `name(args...)` in the decompiled source where
`name` (case-insensitive; decompiler emits lowercase) is a real native from
lookups/gta_hash_allowlist.json, parse the balanced argument list - respecting
nested parens/brackets/braces, string/char literals, and C-style comments -
and count top-level arguments. Aggregate the distinct arg counts seen per
native across the whole corpus. A native with more than one observed arity is
flagged rather than silently averaged: that's exactly the kind of signal that
needs a human's eyes before it becomes ground truth (could be a genuinely
variadic-looking native, or a decompiler mis-parse worth discounting).

Run:
    python tools/mine_native_arity.py --decompiled "F:/Downloads/_ysc_v2/exported" [--odds "C:/Users/Owner/claude projects/Odds"] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

_STRIP_RE = re.compile(
    r'/\*.*?\*/'          # block comment
    r'|//[^\n]*'           # line comment
    r'|"(?:[^"\\]|\\.)*"'  # double-quoted string
    r"|'(?:[^'\\]|\\.)*'", # single-quoted char literal
    re.DOTALL,
)

_CALL_START_RE = re.compile(r'\b([A-Za-z_]\w*)\s*\(')

_OPEN = {'(': ')', '[': ']', '{': '}'}
_CLOSE = {')': '(', ']': '[', '}': '{'}


def _clean(text: str) -> str:
    """Blank out string/char literals and comments, preserving length and
    all structural characters, so bracket/comma scanning downstream is safe."""
    def repl(m: "re.Match[str]") -> str:
        s = m.group(0)
        return ''.join(c if c == '\n' else 'x' for c in s)
    return _STRIP_RE.sub(repl, text)


def _find_matching_close(cleaned: str, open_pos: int) -> "int | None":
    """Given cleaned text and the position of an opening bracket, return the
    position of its matching close bracket, or None if unbalanced."""
    stack = [cleaned[open_pos]]
    i = open_pos + 1
    n = len(cleaned)
    while i < n:
        c = cleaned[i]
        if c in _OPEN:
            stack.append(c)
        elif c in _CLOSE:
            if not stack or stack[-1] != _CLOSE[c]:
                return None
            stack.pop()
            if not stack:
                return i
        i += 1
    return None


def _split_top_level_args(cleaned_inner: str) -> int:
    """Count comma-separated top-level arguments in the (cleaned) text
    between a call's parens. Depth-aware over (), [], {}."""
    if not cleaned_inner.strip():
        return 0
    depth = 0
    count = 1
    for c in cleaned_inner:
        if c in _OPEN:
            depth += 1
        elif c in _CLOSE:
            depth -= 1
        elif c == ',' and depth == 0:
            count += 1
    return count


def mine_file(text: str, native_names_lower: "set[str]",
              natives: "defaultdict[str, Counter]",
              samples: "defaultdict[str, dict]") -> None:
    cleaned = _clean(text)
    for m in _CALL_START_RE.finditer(cleaned):
        name = m.group(1)
        name_lower = name.lower()
        if name_lower not in native_names_lower:
            continue
        open_pos = m.end() - 1  # position of '('
        close_pos = _find_matching_close(cleaned, open_pos)
        if close_pos is None:
            continue
        inner_cleaned = cleaned[open_pos + 1:close_pos]
        arity = _split_top_level_args(inner_cleaned)

        name_upper = name.upper()
        natives[name_upper][arity] += 1
        bucket = samples[name_upper]
        if arity not in bucket:
            raw_call = text[m.start():close_pos + 1]
            if len(raw_call) > 200:
                raw_call = raw_call[:197] + "..."
            bucket[arity] = raw_call


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--decompiled", required=True,
                     help="Folder of decompiled .c files (GTA-V-Script-Decompiler "
                          "Export > Directory output)")
    ap.add_argument("--odds", default=None,
                     help="Path to the Odds project. If given, also writes "
                          "native_arity.json there.")
    ap.add_argument("--lookups", default="lookups",
                     help="This repo's lookups/ dir (default: ./lookups)")
    ap.add_argument("--dry-run", action="store_true",
                     help="Report stats without writing any files")
    args = ap.parse_args()

    allowlist_path = Path(args.lookups) / "gta_hash_allowlist.json"
    if not allowlist_path.is_file():
        print(f"ABSTAIN: {allowlist_path} not found - run "
              f"tools/mine_native_allowlist.py first.")
        return 1
    real_natives = set(json.loads(allowlist_path.read_text(encoding="utf-8")))
    native_names_lower = {n.lower() for n in real_natives}
    print(f"Loaded {len(real_natives)} known native names from {allowlist_path}")

    src_dir = Path(args.decompiled)
    files = sorted(src_dir.glob("*.c"))
    if not files:
        print(f"ABSTAIN: no .c files found under {src_dir}")
        return 1
    print(f"Mining {len(files)} decompiled scripts...")

    natives: "defaultdict[str, Counter]" = defaultdict(Counter)
    samples: "defaultdict[str, dict]" = defaultdict(dict)

    for i, f in enumerate(files, 1):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"  skip {f.name}: {e}")
            continue
        mine_file(text, native_names_lower, natives, samples)
        if i % 100 == 0:
            print(f"  ...{i}/{len(files)} files, {len(natives)} distinct natives so far")

    print(f"Done. {len(natives)} distinct natives observed with at least one call site.")

    consistent = sum(1 for c in natives.values() if len(c) == 1)
    inconsistent = len(natives) - consistent
    print(f"  {consistent} natives with a single consistent arity")
    print(f"  {inconsistent} natives with multiple observed arities (flagged for review)")

    result = {}
    for name, counter in natives.items():
        total = sum(counter.values())
        primary_arity, primary_count = counter.most_common(1)[0]
        result[name] = {
            "primary_arity": primary_arity,
            "consistent": len(counter) == 1,
            "observed_arities": {str(k): v for k, v in sorted(counter.items())},
            "total_calls": total,
            "samples": {str(k): samples[name][k] for k in sorted(samples[name])},
        }

    if args.dry_run:
        print("\n--dry-run: no files written")
        print("\nSample of inconsistent-arity natives (worth a human look):")
        shown = 0
        for name, data in result.items():
            if not data["consistent"]:
                print(f"  {name}: {data['observed_arities']}")
                shown += 1
                if shown >= 15:
                    break
        return 0

    out_path = Path(args.lookups) / "gta_native_arity.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {out_path} ({len(result)} natives)")

    if args.odds:
        odds_path = Path(args.odds) / "native_arity.json"
        odds_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Wrote {odds_path} ({len(result)} natives)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
