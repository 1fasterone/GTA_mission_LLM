"""PostToolUse hook: ruff + mypy on a Python file Claude just edited.

Mirrors the CI gates (.github/workflows/ci.yml) but scoped to the one edited
file, so lint/type failures surface at edit time instead of on the PR.
Report-only (no --fix): rewriting the file behind Claude's back would leave
its view of the file stale. Exit 2 feeds the errors back to Claude.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Same targets CI checks: ruff/mypy src main.py cli rag
GATED_DIRS = ("src", "cli", "rag")
GATED_FILES = ("main.py",)


def _gated(path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(ROOT)
    except ValueError:
        return False
    if rel.suffix != ".py":
        return False
    return rel.parts[0] in GATED_DIRS or str(rel) in GATED_FILES


def main() -> int:
    payload = json.load(sys.stdin)
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or (payload.get("tool_response") or {}).get("filePath")
    if not file_path:
        return 0
    path = Path(file_path)
    if not _gated(path):
        return 0

    checks = [
        ["ruff", "check", "--quiet", str(path)],
        ["mypy", "--follow-imports=silent", "--no-error-summary", str(path)],
    ]
    failures = []
    for cmd in checks:
        proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        if proc.returncode != 0:
            out = (proc.stdout + proc.stderr).strip()
            failures.append(f"$ {' '.join(cmd[:2])} -> exit {proc.returncode}\n{out}")

    if failures:
        print("\n\n".join(failures), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
