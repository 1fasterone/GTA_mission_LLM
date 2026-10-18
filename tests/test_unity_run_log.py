"""Durable review-log contracts for real ``unity-create`` CLI runs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TextIO

import pytest

from src.unity_run_log import UnityCreateRunRecorder


class _FailingLog:
    def __init__(
        self,
        wrapped: TextIO,
        *,
        fail_write: bool = False,
        fail_flush: bool = False,
    ) -> None:
        self.wrapped = wrapped
        self.fail_write = fail_write
        self.fail_flush = fail_flush

    def write(self, text: str) -> int:
        if self.fail_write:
            raise OSError("disk full")
        return self.wrapped.write(text)

    def flush(self) -> None:
        if self.fail_flush:
            raise OSError("volume disconnected")
        self.wrapped.flush()

    def close(self) -> None:
        self.wrapped.close()


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        "lm_studio:\n"
        "  model: qwen-test\n"
        "unity_pipeline:\n"
        f"  unity_output_dir: '{(tmp_path / 'unity').as_posix()}'\n",
        encoding="utf-8",
    )
    return path


def _manifest(tmp_path: Path) -> dict:
    manifests = list((tmp_path / "unity" / "_run_logs").glob("*.json"))
    assert len(manifests) == 1
    return json.loads(manifests[0].read_text(encoding="utf-8"))


def test_failed_run_preserves_raw_input_stage_traceback_and_project(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    prompt = tmp_path / "prompt.txt"
    raw = "A raw 3D physical projectile arena.\n"
    prompt.write_text(raw, encoding="utf-8")
    argv = [
        "main.py", "unity-create", "--file", str(prompt), "--compose",
        "--config", str(config),
    ]

    with pytest.raises(RuntimeError, match="playability gate failed"):
        with UnityCreateRunRecorder.from_argv(argv, cwd=tmp_path):
            running = _manifest(tmp_path)
            assert running["status"] == "running"
            assert running["finished_utc"] is None
            print("STEP 7 -- Playability evaluator grades the build")
            print("Project written to: C:\\u\\unity\\ProjectileArena")
            print("PLAYABILITY 91/100 NOT PLAYABLE")
            raise RuntimeError("playability gate failed")

    manifest = _manifest(tmp_path)
    assert manifest["status"] == "failed"
    assert manifest["exit_code"] == 1
    assert manifest["raw_input"]["kind"] == "file"
    assert manifest["raw_input"]["text"] == raw
    assert manifest["raw_input"]["sha256"] == hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()
    assert manifest["last_step"] == {
        "number": 7,
        "name": "Playability evaluator grades the build",
    }
    assert manifest["project_path"] == "C:\\u\\unity\\ProjectileArena"
    assert manifest["playability_summary"] == "PLAYABILITY 91/100 NOT PLAYABLE"
    assert manifest["failure"]["exception_type"] == "RuntimeError"
    assert "raise RuntimeError" in manifest["failure"]["traceback"]

    log_text = Path(manifest["log_path"]).read_text(encoding="utf-8")
    assert "STEP 7 -- Playability evaluator grades the build" in log_text
    assert "RUN FAILURE" in log_text
    assert "playability gate failed" in log_text


def test_successful_run_also_leaves_review_log(tmp_path: Path) -> None:
    config = _config(tmp_path)
    argv = [
        "main.py", "unity-create", "A physical bullet range", "--no-llm",
        "--config", str(config),
    ]

    with UnityCreateRunRecorder.from_argv(argv, cwd=tmp_path):
        print("STEP 6 -- Generator compiles schema -> Unity project folder")
        print("Project written to: C:\\u\\unity\\BulletRange")

    manifest = _manifest(tmp_path)
    assert manifest["status"] == "succeeded"
    assert manifest["exit_code"] == 0
    assert manifest["offline_no_llm"] is True
    assert manifest["raw_input"]["text"] == "A physical bullet range"
    assert manifest["last_step"]["number"] == 6
    assert manifest["failure"] is None


def test_non_create_and_help_commands_do_not_create_logs(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with UnityCreateRunRecorder.from_argv(
        ["main.py", "unity-validate", "schema.json"], cwd=tmp_path
    ) as recorder:
        assert recorder.active is False
    with UnityCreateRunRecorder.from_argv(
        ["main.py", "unity-create", "--help", "--config", str(config)],
        cwd=tmp_path,
    ) as recorder:
        assert recorder.active is False
    assert not (tmp_path / "unity" / "_run_logs").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["main.py", "unity-validate", "unity-create"],
        ["main.py", "validate", "unity-create"],
        ["main.py", "--unknown-option", "unity-create"],
    ],
)
def test_unity_create_as_noncommand_data_does_not_activate(
    tmp_path: Path,
    argv: list[str],
) -> None:
    recorder = UnityCreateRunRecorder.from_argv(argv, cwd=tmp_path)
    assert recorder.active is False


def test_supported_global_flags_before_command_activate(tmp_path: Path) -> None:
    config = _config(tmp_path)
    recorder = UnityCreateRunRecorder.from_argv(
        [
            "main.py", "--verbose", "-d", "unity-create", "brief",
            "--config", str(config),
        ],
        cwd=tmp_path,
    )
    assert recorder.active is True
    assert recorder.command_args[0] == "brief"


def test_nonzero_system_exit_is_recorded_as_failure(tmp_path: Path) -> None:
    config = _config(tmp_path)
    argv = [
        "main.py", "unity-create", "broken prompt", "--config", str(config),
    ]

    with pytest.raises(SystemExit):
        with UnityCreateRunRecorder.from_argv(argv, cwd=tmp_path):
            raise SystemExit(2)

    manifest = _manifest(tmp_path)
    assert manifest["status"] == "failed"
    assert manifest["exit_code"] == 2
    assert manifest["failure"]["exception_type"] == "SystemExit"


def test_mid_run_transcript_failure_disables_logging_once_without_aborting(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    recorder = UnityCreateRunRecorder.from_argv(
        ["main.py", "unity-create", "brief", "--config", str(config)],
        cwd=tmp_path,
    )

    with recorder:
        original_log = recorder._log_file
        assert original_log is not None
        recorder._log_file = _FailingLog(original_log, fail_write=True)
        print("pipeline output continues")
        print("second write does not emit a second warning")

    assert recorder._logging_disabled is True
    assert capsys.readouterr().err.count("RUN LOGGING ERROR") == 1


def test_finalization_flush_failure_restores_streams_and_does_not_fail_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    recorder = UnityCreateRunRecorder.from_argv(
        ["main.py", "unity-create", "brief", "--config", str(config)],
        cwd=tmp_path,
    )

    with recorder:
        original_log = recorder._log_file
        assert original_log is not None
        recorder._log_file = _FailingLog(original_log, fail_flush=True)
        print("pipeline completed")

    print("stdout restored")
    captured = capsys.readouterr()
    assert "stdout restored" in captured.out
    assert captured.err.count("RUN LOGGING ERROR") == 1


def test_logging_failure_does_not_replace_active_pipeline_exception(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    recorder = UnityCreateRunRecorder.from_argv(
        ["main.py", "unity-create", "brief", "--config", str(config)],
        cwd=tmp_path,
    )

    with pytest.raises(RuntimeError, match="pipeline failed first"):
        with recorder:
            original_log = recorder._log_file
            assert original_log is not None
            recorder._log_file = _FailingLog(original_log, fail_write=True)
            raise RuntimeError("pipeline failed first")

    assert recorder._logging_disabled is True
