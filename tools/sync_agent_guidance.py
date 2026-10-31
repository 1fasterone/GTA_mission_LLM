"""Synchronize the shared generated policy block in AGENTS.md and CLAUDE.md."""

from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "policies" / "shared-agent-guidance.md"
TARGETS = (ROOT / "AGENTS.md", ROOT / "CLAUDE.md")
BEGIN = "<!-- shared-policy:begin -->"
END = "<!-- shared-policy:end -->"


def rendered_block() -> str:
    body = SOURCE.read_text(encoding="utf-8").strip()
    return f"{BEGIN}\n{body}\n{END}"


def synchronized(text: str) -> str:
    block = rendered_block()
    if BEGIN not in text or END not in text:
        return block + "\n\n" + text.lstrip()
    before, rest = text.split(BEGIN, 1)
    _old, after = rest.split(END, 1)
    return before.rstrip() + ("\n\n" if before.strip() else "") + block + after


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    drifted: list[str] = []
    for target in TARGETS:
        current = target.read_text(encoding="utf-8")
        expected = synchronized(current)
        if current == expected:
            continue
        drifted.append(target.name)
        if not args.check:
            target.write_text(expected, encoding="utf-8")
    if drifted and args.check:
        print("shared policy drift: " + ", ".join(drifted))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
