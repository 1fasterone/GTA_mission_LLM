"""Source-lineage capture and legacy run-discovery contracts."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from src.unity_run_lineage import (
    ACCEPTED_S2_COMMIT,
    capture_source_lineage,
    discover_run_lineage,
)


def _write_manifest(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_capture_outside_git_is_explicitly_unavailable(tmp_path: Path) -> None:
    lineage = capture_source_lineage(tmp_path)

    assert lineage["classification"] == "unavailable"
    assert lineage["accepted_s2_commit"] == ACCEPTED_S2_COMMIT
    assert lineage["reason"]


def test_discovery_distinguishes_recorded_source_from_legacy_time_bound(
    tmp_path: Path,
) -> None:
    accepted_at = datetime(2026, 11, 6, 23, 47, tzinfo=timezone.utc)
    _write_manifest(
        tmp_path / "unity-create-legacy.json",
        {
            "format": "unity-create-run-log/v1",
            "started_utc": "2026-11-06T22:46:51Z",
            "status": "failed",
            "cwd": "C:/tmp/GTA_mission_LLM-s2-consumer-cutover",
        },
    )
    _write_manifest(
        tmp_path / "unity-create-recorded.json",
        {
            "format": "unity-create-run-log/v2",
            "started_utc": "2026-11-07T00:00:00Z",
            "status": "succeeded",
            "source_lineage": {
                "format": "unity-source-lineage/v1",
                "classification": "accepted_s2_or_later",
                "head_commit": "a" * 40,
                "dirty": False,
            },
        },
    )

    report = discover_run_lineage(
        tmp_path,
        repo_root=tmp_path,
        accepted_at=accepted_at,
    )

    assert report["summary"] == {
        "run_count": 2,
        "exact_source_count": 1,
        "classifications": {
            "accepted_s2_or_later": 1,
            "pre_accepted_s2_commit_unverified": 1,
        },
    }
    legacy, recorded = report["runs"]
    assert legacy["exact_source"] is False
    assert "uncommitted S2 candidate" in legacy["evidence"]
    assert recorded["exact_source"] is True
    assert recorded["classification"] == "accepted_s2_or_later"


def test_post_acceptance_legacy_manifest_remains_indeterminate(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path / "unity-create-unrecorded.json",
        {
            "format": "unity-create-run-log/v1",
            "started_utc": "2026-11-07T00:00:00Z",
            "status": "succeeded",
        },
    )

    report = discover_run_lineage(
        tmp_path,
        repo_root=tmp_path,
        accepted_at=datetime(2026, 11, 6, 23, 47, tzinfo=timezone.utc),
    )

    assert report["runs"][0]["classification"] == (
        "indeterminate_unrecorded_source"
    )
    assert report["runs"][0]["exact_source"] is False
