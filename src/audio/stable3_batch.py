"""Standalone Stable Audio 3 batch consumer.

This module is launched with the interpreter configured for Stable Audio 3,
not necessarily the compiler's lightweight virtual environment.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from .content import generate_bank


def _project_root(batch_path: Path, explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit).resolve()
    # audio_batch.json lives at <project>/Assets/Micros/audio/.
    candidate = batch_path.resolve().parent.parent.parent.parent
    return candidate if (candidate / "Assets").is_dir() else None


def _overlay(resources_dir: Path, project_root: Path) -> None:
    """Copy generated clips to the canonical Unity folders and remove only
    the generated staging Resources folder, avoiding duplicate Resources paths.
    """
    if not resources_dir.is_dir():
        return
    assets = project_root / "Assets"
    (assets / "Resources").mkdir(parents=True, exist_ok=True)
    audio_dir = resources_dir / "Audio"
    if audio_dir.is_dir():
        shutil.copytree(audio_dir, assets / "Audio", dirs_exist_ok=True)
        shutil.copytree(audio_dir, assets / "Resources" / "Audio", dirs_exist_ok=True)
    manifest = resources_dir / "audio_manifest.json"
    if manifest.is_file():
        shutil.copy2(manifest, assets / "Resources" / "audio_manifest.json")
    shutil.rmtree(resources_dir)


def run_request(request_path: str | Path) -> dict[str, Any]:
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    batch_path = Path(request["batch_path"]).resolve()
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    config = request.get("config") or {}
    config.setdefault("audio", {})["backend"] = "stable3"
    resources_dir = batch_path.parent / batch.get("resources_dir", "Resources")
    cues = list(batch.get("cues") or [])
    print(
        f"[audio/stable3] standalone batch: {len(cues)} cue(s) -> "
        f"{resources_dir}",
        flush=True,
    )
    manifest = generate_bank(cues, resources_dir, config, seed=int(batch.get("seed", 0)))
    project = _project_root(batch_path, request.get("unity"))
    if project is not None:
        _overlay(resources_dir, project)
        print(f"[audio/stable3] overlaid clips into {project / 'Assets'}", flush=True)
    result = {
        "status": "complete",
        "batch": str(batch_path),
        "project": str(project) if project else None,
        "manifest": manifest,
    }
    batch_result = batch_path.with_name("audio_batch.result.json")
    batch_result.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a deferred Stable Audio 3 batch")
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    run_request(args.request)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
