"""Source-revision evidence and discovery for ``unity-create`` run logs."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any


ACCEPTED_S2_COMMIT = "c239ac826ba62f9cb39a981679085e8032a56e36"
SOURCE_LINEAGE_FORMAT = "unity-source-lineage/v1"
DISCOVERY_FORMAT = "unity-run-lineage-discovery/v1"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        check=False,
    )


def _git_text(cwd: Path, *args: str) -> str | None:
    result = _git(cwd, *args)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def capture_source_lineage(
    cwd: Path,
    *,
    accepted_s2_commit: str = ACCEPTED_S2_COMMIT,
) -> dict[str, Any]:
    """Capture the exact Git base and its relationship to accepted S2."""
    try:
        root_text = _git_text(cwd, "rev-parse", "--show-toplevel")
        if root_text is None:
            return {
                "format": SOURCE_LINEAGE_FORMAT,
                "classification": "unavailable",
                "reason": "working directory is not inside a Git repository",
                "accepted_s2_commit": accepted_s2_commit,
            }
        root = Path(root_text)
        head = _git_text(root, "rev-parse", "HEAD")
        if head is None:
            return {
                "format": SOURCE_LINEAGE_FORMAT,
                "classification": "unavailable",
                "reason": "Git HEAD could not be resolved",
                "repo_root": str(root),
                "accepted_s2_commit": accepted_s2_commit,
            }
        branch = _git_text(root, "branch", "--show-current")
        dirty_result = _git(root, "status", "--porcelain", "--untracked-files=normal")
        dirty = dirty_result.returncode != 0 or bool(dirty_result.stdout.strip())
        accepted_exists = _git(
            root, "cat-file", "-e", f"{accepted_s2_commit}^{{commit}}"
        ).returncode == 0
        relation = "unknown"
        tree_equivalent = False
        if accepted_exists:
            accepted_is_ancestor = _git(
                root, "merge-base", "--is-ancestor", accepted_s2_commit, head
            ).returncode == 0
            head_is_ancestor = _git(
                root, "merge-base", "--is-ancestor", head, accepted_s2_commit
            ).returncode == 0
            if accepted_is_ancestor:
                relation = "at_or_after"
            elif head_is_ancestor:
                relation = "before"
            else:
                relation = "diverged"
            head_tree = _git_text(root, "rev-parse", f"{head}^{{tree}}")
            accepted_tree = _git_text(
                root, "rev-parse", f"{accepted_s2_commit}^{{tree}}"
            )
            tree_equivalent = bool(head_tree and head_tree == accepted_tree)

        if relation == "at_or_after":
            classification = (
                "accepted_s2_or_later_with_local_changes"
                if dirty else "accepted_s2_or_later"
            )
        elif relation == "before":
            classification = "pre_s2_with_local_changes" if dirty else "pre_s2"
        elif relation == "diverged" and tree_equivalent:
            classification = (
                "accepted_s2_tree_equivalent_with_local_changes"
                if dirty else "accepted_s2_tree_equivalent"
            )
        elif relation == "diverged":
            classification = "diverged_from_accepted_s2"
        else:
            classification = "indeterminate"
        return {
            "format": SOURCE_LINEAGE_FORMAT,
            "classification": classification,
            "repo_root": str(root.resolve()),
            "head_commit": head,
            "branch": branch,
            "dirty": dirty,
            "accepted_s2_commit": accepted_s2_commit,
            "accepted_commit_available": accepted_exists,
            "relation_to_accepted_s2": relation,
            "accepted_tree_equivalent": tree_equivalent,
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "format": SOURCE_LINEAGE_FORMAT,
            "classification": "unavailable",
            "reason": f"{type(exc).__name__}: {exc}",
            "accepted_s2_commit": accepted_s2_commit,
        }


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _accepted_time(repo_root: Path, accepted_s2_commit: str) -> datetime | None:
    value = _git_text(repo_root, "show", "-s", "--format=%cI", accepted_s2_commit)
    return _parse_time(value)


def discover_run_lineage(
    log_dir: Path,
    *,
    repo_root: Path,
    accepted_s2_commit: str = ACCEPTED_S2_COMMIT,
    accepted_at: datetime | None = None,
) -> dict[str, Any]:
    """Classify run manifests without pretending legacy logs record a commit."""
    accepted_time = accepted_at or _accepted_time(repo_root, accepted_s2_commit)
    rows: list[dict[str, Any]] = []
    for path in sorted(Path(log_dir).glob("unity-create-*.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            rows.append({
                "manifest": str(path),
                "classification": "invalid_manifest",
                "exact_source": False,
                "evidence": f"{type(exc).__name__}: {exc}",
            })
            continue
        lineage = manifest.get("source_lineage")
        if isinstance(lineage, dict) and lineage.get("head_commit"):
            classification = str(lineage.get("classification") or "indeterminate")
            dirty = bool(lineage.get("dirty"))
            exact_source = not dirty and classification not in {
                "indeterminate", "unavailable", "diverged_from_accepted_s2"
            }
            evidence = (
                f"recorded HEAD {lineage['head_commit']}"
                + (" with local changes" if dirty else " from a clean tree")
            )
        else:
            started = _parse_time(manifest.get("started_utc"))
            if accepted_time is not None and started is not None and started < accepted_time:
                classification = "pre_accepted_s2_commit_unverified"
                evidence = (
                    "legacy v1 manifest predates the accepted S2 commit but does "
                    "not distinguish pre-S2 main from an uncommitted S2 candidate"
                )
            else:
                classification = "indeterminate_unrecorded_source"
                evidence = "manifest does not record source revision"
            exact_source = False
        rows.append({
            "manifest": str(path),
            "started_utc": manifest.get("started_utc"),
            "status": manifest.get("status"),
            "cwd": manifest.get("cwd"),
            "project_path": manifest.get("project_path"),
            "classification": classification,
            "exact_source": exact_source,
            "evidence": evidence,
        })
    counts = Counter(row["classification"] for row in rows)
    return {
        "format": DISCOVERY_FORMAT,
        "accepted_s2_commit": accepted_s2_commit,
        "accepted_s2_committed_utc": (
            accepted_time.isoformat().replace("+00:00", "Z")
            if accepted_time is not None else None
        ),
        "log_dir": str(Path(log_dir).resolve()),
        "summary": {
            "run_count": len(rows),
            "exact_source_count": sum(bool(row["exact_source"]) for row in rows),
            "classifications": dict(sorted(counts.items())),
        },
        "runs": rows,
    }


def write_lineage_report(report: dict[str, Any], output: Path) -> Path:
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
    return target


__all__ = [
    "ACCEPTED_S2_COMMIT",
    "DISCOVERY_FORMAT",
    "SOURCE_LINEAGE_FORMAT",
    "capture_source_lineage",
    "discover_run_lineage",
    "write_lineage_report",
]
