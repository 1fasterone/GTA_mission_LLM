"""Repository-level contracts for the required GitHub Actions checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
ACCEPTANCE_NAME = "Acceptance (frozen Step 3.5)"
ACCEPTANCE_TEST = "pytest -q tests/test_frozen_step35_acceptance.py"


def test_ci_runs_the_frozen_step35_acceptance_check() -> None:
    """Keep the frozen Step 3.5 check present and executable in CI."""
    workflow: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "ci.yml must declare a jobs mapping"

    acceptance_jobs = [
        job
        for job in jobs.values()
        if isinstance(job, dict) and job.get("name") == ACCEPTANCE_NAME
    ]
    assert len(acceptance_jobs) == 1, (
        f"ci.yml must contain exactly one job named {ACCEPTANCE_NAME!r}"
    )

    steps = acceptance_jobs[0].get("steps")
    assert isinstance(steps, list), f"{ACCEPTANCE_NAME!r} must declare steps"
    run_commands = [
        step.get("run")
        for step in steps
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    ]
    assert ACCEPTANCE_TEST in run_commands, (
        f"{ACCEPTANCE_NAME!r} must run {ACCEPTANCE_TEST!r}"
    )
