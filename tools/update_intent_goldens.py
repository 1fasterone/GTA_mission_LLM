"""Refresh prompt-level intent goldens after an explicitly reviewed change."""

from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.intent_eval import CASES_PATH, evaluate_intent_case


def main() -> int:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    for case in cases:
        case["expected"] = evaluate_intent_case(case)
    CASES_PATH.write_text(json.dumps(cases, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
