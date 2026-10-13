"""Durable operational logs for user-requested ``unity-create`` runs.

The recorder wraps the CLI process boundary, not the compiler.  Its output is
review provenance only: it never enters the schema and is never consumed by
Steps 2-7.  A manifest is written with ``status: running`` before the pipeline
starts, so a killed or crashed process still leaves an explicit incomplete-run
record instead of disappearing with the terminal buffer.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

import yaml


_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_STEP_RE = re.compile(r"^\s*STEP\s+(\d+)\s+--\s+(.+?)\s*$", re.MULTILINE)
_PROJECT_RE = re.compile(r"Project written to:\s*(.+?)\s*$", re.MULTILINE)
_PLAYABILITY_RE = re.compile(r"^\s*PLAYABILITY\s+.+$", re.MULTILINE)

_VALUE_OPTIONS = frozenset({
    "--config", "-c", "--file", "-f", "--theme", "--mashup", "--seed",
    "--model", "-m", "--route", "--scene", "--reference-image", "--image",
    "--viz", "--viz-output", "--models-dir",
})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _option_value(args: list[str], *names: str) -> str | None:
    for index, arg in enumerate(args):
        for name in names:
            if arg == name and index + 1 < len(args):
                return args[index + 1]
            prefix = name + "="
            if arg.startswith(prefix):
                return arg[len(prefix):]
    return None


def _inline_prompt(args: list[str]) -> str | None:
    positional_mode = False
    index = 0
    while index < len(args):
        arg = args[index]
        if positional_mode:
            return arg
        if arg == "--":
            positional_mode = True
            index += 1
            continue
        if arg in _VALUE_OPTIONS:
            index += 2
            continue
        if any(arg.startswith(option + "=") for option in _VALUE_OPTIONS):
            index += 1
            continue
        if not arg.startswith("-"):
            return arg
        index += 1
    return None


def _resolve_from_cwd(value: str, cwd: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else cwd / path


def _read_raw_input(args: list[str], cwd: Path) -> dict[str, Any]:
    file_value = _option_value(args, "--file", "-f")
    if file_value:
        prompt_path = _resolve_from_cwd(file_value, cwd).resolve()
        try:
            text = prompt_path.read_text(encoding="utf-8")
            read_error = None
        except OSError as exc:
            text = ""
            read_error = f"{type(exc).__name__}: {exc}"
        return {
            "kind": "file",
            "path": str(prompt_path),
            "text": text,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "read_error": read_error,
        }

    text = _inline_prompt(args) or ""
    return {
        "kind": "inline",
        "path": None,
        "text": text,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "read_error": None,
    }


def _config_and_output(args: list[str], cwd: Path) -> tuple[Path, Path, str | None]:
    config_value = _option_value(args, "--config", "-c") or "config.yaml"
    config_path = _resolve_from_cwd(config_value, cwd).resolve()
    model = _option_value(args, "--model", "-m")
    output_value = "output/unity"
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        output_value = str(
            (config.get("unity_pipeline") or {}).get("unity_output_dir")
            or output_value
        )
        if model is None:
            model = str((config.get("lm_studio") or {}).get("model") or "") or None
    except (OSError, yaml.YAMLError, AttributeError, TypeError):
        pass
    output_root = _resolve_from_cwd(output_value, cwd).resolve()
    return config_path, output_root, model


class _Tee:
    """Mirror text to the original stream and a plain-text run log."""

    def __init__(self, original: TextIO, log_file: TextIO, lock: threading.RLock):
        self._original = original
        self._log_file = log_file
        self._lock = lock

    def write(self, text: str) -> int:
        with self._lock:
            written = self._original.write(text)
            self._log_file.write(_ANSI_RE.sub("", text))
            return written

    def flush(self) -> None:
        with self._lock:
            self._original.flush()
            self._log_file.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._original, name)


@dataclass
class UnityCreateRunRecorder:
    """Context manager that records one real ``unity-create`` CLI invocation."""

    argv: list[str]
    cwd: Path
    active: bool
    command_args: list[str] = field(default_factory=list)
    started: datetime | None = None
    manifest_path: Path | None = None
    log_path: Path | None = None
    _manifest: dict[str, Any] = field(default_factory=dict)
    _log_file: TextIO | None = None
    _stdout_redirect: Any = None
    _stderr_redirect: Any = None
    _clock_started: float = 0.0

    @classmethod
    def from_argv(
        cls,
        argv: list[str] | None = None,
        *,
        cwd: Path | None = None,
    ) -> "UnityCreateRunRecorder":
        full_argv = list(sys.argv if argv is None else argv)
        run_cwd = Path.cwd() if cwd is None else Path(cwd)
        try:
            command_index = full_argv.index("unity-create")
        except ValueError:
            return cls(argv=full_argv, cwd=run_cwd, active=False)
        command_args = full_argv[command_index + 1:]
        active = not any(arg in {"--help", "-h"} for arg in command_args)
        return cls(
            argv=full_argv,
            cwd=run_cwd,
            active=active,
            command_args=command_args,
        )

    def __enter__(self) -> "UnityCreateRunRecorder":
        if not self.active:
            return self

        self.started = _utc_now()
        self._clock_started = time.monotonic()
        config_path, output_root, model = _config_and_output(
            self.command_args, self.cwd
        )
        raw_input = _read_raw_input(self.command_args, self.cwd)
        log_dir = output_root / "_run_logs"
        stamp = self.started.strftime("%Y%m%dT%H%M%S.%fZ")
        stem = f"unity-create-{stamp}-{raw_input['sha256'][:12]}-{os.getpid()}"

        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            self.log_path = log_dir / f"{stem}.log"
            self.manifest_path = log_dir / f"{stem}.json"
            self._log_file = self.log_path.open(
                "x", encoding="utf-8", newline="", buffering=1
            )
        except OSError as exc:
            self.active = False
            sys.stderr.write(
                "RUN LOGGING ERROR: could not create the unity-create review log "
                f"under {log_dir}: {type(exc).__name__}: {exc}\n"
            )
            return self

        self._manifest = {
            "format": "unity-create-run-log/v1",
            "status": "running",
            "command": "unity-create",
            "argv": self.argv,
            "cwd": str(self.cwd.resolve()),
            "started_utc": _iso_utc(self.started),
            "finished_utc": None,
            "duration_seconds": None,
            "config_path": str(config_path),
            "model": model,
            "offline_no_llm": "--no-llm" in self.command_args,
            "compose": "--compose" in self.command_args,
            "raw_input": raw_input,
            "log_path": str(self.log_path),
            "last_step": None,
            "project_path": None,
            "playability_summary": None,
            "exit_code": None,
            "failure": None,
        }
        self._write_manifest()
        self._log_file.write(
            "UNITY-CREATE RUN LOG\n"
            f"started_utc: {self._manifest['started_utc']}\n"
            f"raw_input_sha256: {raw_input['sha256']}\n"
            f"manifest: {self.manifest_path}\n\n"
        )
        self._log_file.flush()

        lock = threading.RLock()
        self._stdout_redirect = contextlib.redirect_stdout(
            _Tee(sys.stdout, self._log_file, lock)
        )
        self._stderr_redirect = contextlib.redirect_stderr(
            _Tee(sys.stderr, self._log_file, lock)
        )
        self._stdout_redirect.__enter__()
        self._stderr_redirect.__enter__()
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None, tb: Any) -> bool:
        if not self.active or self._log_file is None:
            return False

        exit_code = 0
        status = "succeeded"
        failure = None
        if exc is not None:
            if isinstance(exc, SystemExit):
                raw_code = exc.code
                exit_code = raw_code if isinstance(raw_code, int) else 1
            elif isinstance(exc, KeyboardInterrupt):
                exit_code = 130
            else:
                exit_code = 1
            if exit_code != 0:
                status = "failed"
                rendered_traceback = "".join(
                    traceback.format_exception(exc_type, exc, tb)
                )
                failure = {
                    "exception_type": exc_type.__name__ if exc_type else type(exc).__name__,
                    "message": str(exc),
                    "traceback": rendered_traceback,
                }
                self._log_file.write(
                    "\nRUN FAILURE\n"
                    f"exception_type: {failure['exception_type']}\n"
                    f"message: {failure['message']}\n\n"
                    f"{rendered_traceback}"
                )

        finished = _utc_now()
        self._log_file.write(
            "\nUNITY-CREATE RUN END\n"
            f"status: {status}\n"
            f"exit_code: {exit_code}\n"
            f"finished_utc: {_iso_utc(finished)}\n"
        )
        self._log_file.flush()

        self._stderr_redirect.__exit__(exc_type, exc, tb)
        self._stdout_redirect.__exit__(exc_type, exc, tb)

        review_fields = self._review_fields()
        self._manifest.update({
            "status": status,
            "finished_utc": _iso_utc(finished),
            "duration_seconds": round(time.monotonic() - self._clock_started, 3),
            "exit_code": exit_code,
            "failure": failure,
            **review_fields,
        })
        self._write_manifest()
        self._log_file.close()
        self._log_file = None

        destination = self.manifest_path if self.manifest_path else self.log_path
        stream = sys.stderr if status == "failed" else sys.stdout
        stream.write(f"Unity run review log: {destination}\n")
        stream.flush()
        return False

    def _review_fields(self) -> dict[str, Any]:
        assert self.log_path is not None
        try:
            text = _ANSI_RE.sub(
                "", self.log_path.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            return {}
        steps = _STEP_RE.findall(text)
        projects = _PROJECT_RE.findall(text)
        playability = _PLAYABILITY_RE.findall(text)
        return {
            "last_step": (
                {"number": int(steps[-1][0]), "name": steps[-1][1].strip()}
                if steps else None
            ),
            "project_path": projects[-1].strip() if projects else None,
            "playability_summary": playability[-1].strip() if playability else None,
        }

    def _write_manifest(self) -> None:
        if self.manifest_path is None:
            return
        temporary = self.manifest_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps(self._manifest, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.manifest_path)
        except OSError as exc:
            if self._log_file is not None:
                self._log_file.write(
                    "RUN LOGGING ERROR: could not update manifest: "
                    f"{type(exc).__name__}: {exc}\n"
                )
