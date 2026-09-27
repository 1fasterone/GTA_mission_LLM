"""Crash-isolated audio generation for native/GPU-backed adapters.

Python exceptions from Stable Audio 3 already degrade inside its adapter, but
a CUDA/native termination ends the whole interpreter before a handler can run.
The Stable Audio bank therefore runs in one child process (preserving model
batching).  A non-zero exit or timeout is converted into deterministic DSP /
silent-placeholder output in the parent compiler process.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from .content import generate_bank


def _log_tail(path: Path, limit: int = 4000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:].strip()


def _fallback(
    cues: list[dict[str, Any]],
    resources_dir: Path,
    config: dict[str, Any],
    seed: int,
    warning: str,
) -> dict[str, Any]:
    fallback_config = copy.deepcopy(config or {})
    fallback_config.setdefault("audio", {})["backend"] = "dsp"
    manifest = generate_bank(cues, resources_dir, fallback_config, seed=seed)
    manifest.setdefault("warnings", []).insert(0, warning)
    manifest["requested_backend"] = "stable3"
    manifest["fallback_backend"] = "dsp"
    (resources_dir / "audio_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


def generate_bank_safe(
    cues: list[dict[str, Any]],
    resources_dir: str | Path,
    config: dict[str, Any],
    seed: int = 0,
) -> dict[str, Any]:
    """Generate a bank while containing fatal Stable Audio worker exits."""
    resources_dir = Path(resources_dir).resolve()
    audio_cfg = (config or {}).get("audio", {}) or {}
    backend = str(audio_cfg.get("backend", "dsp")).strip().lower()
    if backend != "stable3":
        return generate_bank(cues, resources_dir, config, seed=seed)

    resources_dir.mkdir(parents=True, exist_ok=True)
    log_path = resources_dir.parent / "stable3_worker.log"
    timeout_s = max(30.0, float(audio_cfg.get("worker_timeout_s", 1800)))
    stall_timeout_s = max(
        30.0, float(audio_cfg.get("worker_stall_timeout_s", 120))
    )
    payload = {
        "cues": cues,
        "resources_dir": str(resources_dir),
        "config": config,
        "seed": int(seed),
    }
    repo_root = Path(__file__).resolve().parents[2]
    worker_python = str(audio_cfg.get("stable3_python") or sys.executable)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    print(
        "[audio/stable3] starting crash-isolated worker "
        f"({len(cues)} cues, timeout={timeout_s:g}s, "
        f"stall_timeout={stall_timeout_s:g}s, python={worker_python}); "
        f"log={log_path}"
    )
    failure_reason = ""
    try:
        with tempfile.TemporaryDirectory(prefix="stable3-audio-") as temp_dir:
            request_path = Path(temp_dir) / "request.json"
            request_path.write_text(
                json.dumps(payload, default=str), encoding="utf-8"
            )
            with log_path.open("wb") as log_file:
                process = subprocess.Popen(
                    [
                        worker_python,
                        "-m",
                        "src.audio.stable3_worker",
                        "--request",
                        str(request_path),
                    ],
                    cwd=repo_root,
                    env=env,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                )
                started = time.monotonic()
                last_progress = started
                last_size = 0
                while True:
                    try:
                        return_code = process.wait(timeout=15)
                        break
                    except subprocess.TimeoutExpired:
                        now = time.monotonic()
                        try:
                            current_size = log_path.stat().st_size
                        except OSError:
                            current_size = last_size
                        if current_size != last_size:
                            last_size = current_size
                            last_progress = now
                        elapsed = now - started
                        stalled = now - last_progress
                        print(
                            "[audio/stable3] worker still running "
                            f"({elapsed:.0f}s elapsed, {stalled:.0f}s since "
                            "last log progress)"
                        )
                        if elapsed >= timeout_s:
                            failure_reason = (
                                f"exceeded the {timeout_s:g}s total timeout"
                            )
                        elif stalled >= stall_timeout_s:
                            failure_reason = (
                                f"made no log progress for {stall_timeout_s:g}s"
                            )
                        if failure_reason:
                            process.kill()
                            process.wait()
                            return_code = process.returncode
                            break
    except OSError as exc:
        warning = (
            "Stable Audio 3 worker could not start "
            f"({type(exc).__name__}: {exc}); using DSP/silent fallback."
        )
        print(f"[audio/stable3] {warning}")
        return _fallback(cues, resources_dir, config, seed, warning)

    manifest_path = resources_dir / "audio_manifest.json"
    if return_code == 0 and manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            warning = (
                "Stable Audio 3 worker returned an unreadable manifest "
                f"({type(exc).__name__}: {exc}); using DSP/silent fallback."
            )
            print(f"[audio/stable3] {warning}")
            return _fallback(cues, resources_dir, config, seed, warning)
        manifest["worker"] = {
            "isolated": True,
            "exit_code": 0,
            "log": str(log_path),
            "python": worker_python,
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        warning_count = len(manifest.get("warnings", []))
        if warning_count:
            print(
                "[audio/stable3] isolated worker completed with "
                f"{warning_count} recorded fallback warning(s)"
            )
        else:
            print("[audio/stable3] isolated worker completed successfully")
        return manifest

    tail = _log_tail(log_path)
    if failure_reason:
        warning = (
            f"Stable Audio 3 worker {failure_reason} and was stopped "
            f"(exit code {return_code}); using DSP/silent fallback. "
            f"Worker log: {log_path}"
        )
    else:
        warning = (
            "Stable Audio 3 worker terminated before completing "
            f"(exit code {return_code}); using DSP/silent fallback. "
            f"Worker log: {log_path}"
        )
    print(f"[audio/stable3] {warning}")
    if tail:
        print("[audio/stable3] worker log tail:\n" + tail)
    return _fallback(cues, resources_dir, config, seed, warning)
