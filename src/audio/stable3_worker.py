"""Child-process entry point for crash-isolated Stable Audio 3 generation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .content import generate_bank


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.request.read_text(encoding="utf-8"))
    generate_bank(
        payload["cues"],
        Path(payload["resources_dir"]),
        payload["config"],
        seed=int(payload.get("seed", 0)),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
