"""
Opt-in headless compile gate.

Launches the real Unity Editor in batchmode against a generated project and
asserts it compiles with zero C# errors. This is SLOW (first import can take
minutes and downloads packages), so it is gated behind its own marker and is
not part of the default smoke suite.

Run with:    pytest -m unity_compile
Skips when:  no Unity Editor is installed, or no generated project on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import unity_cli

ROOT = Path(__file__).resolve().parent.parent
UNITY_OUT = ROOT / "output" / "unity"
TEMPLATE_DIR = ROOT / "templates" / "unity"


def _first_project() -> Path | None:
    if not UNITY_OUT.exists():
        return None
    for d in sorted(UNITY_OUT.iterdir()):
        # Skip the micro/ generators dir — those aren't full projects.
        if d.is_dir() and d.name != "micro" and (d / "Assets").is_dir():
            return d
    return None


@pytest.mark.unity_compile
def test_generated_project_compiles_clean() -> None:
    if unity_cli._find_editor() is None:
        pytest.skip("No Unity Editor installed — skipping headless compile gate.")

    project = _first_project()
    if project is None:
        pytest.skip("No full Unity project under output/unity/ — generate one first.")

    unity_version = unity_cli._read_unity_version(project)
    rc = unity_cli._run_compile_check(
        project_root=project,
        template_dir=TEMPLATE_DIR,
        unity_version=unity_version,
        timeout=1500,
    )
    assert rc == 0, f"{project.name} failed to compile clean (verify exit code {rc})"
