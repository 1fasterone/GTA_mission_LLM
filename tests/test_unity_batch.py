"""unity-batch: resumable batches of live runs (src/unity_batch.py).

Test data, and who wrote it: every run here is executed by `_FAKE_CREATE` /
`_FAKE_VERIFY`, engineer-authored stand-ins for `unity-create` and
`e2e-verify`. They honour the real contract the driver depends on: read the
run's `--config`, write into its `unity_pipeline.unity_output_dir`, and leave
a unity-create-run-log/v2 manifest (and, on failure, a unity-failed-run/v1
report) there. No Pipeline LLM, LM Studio or Unity Editor is involved.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

import src.unity_batch as ub
from src.unity_batch import (Batch, BatchDriver, BatchError, BatchInterrupted,
                             DriveOptions, check_create_args, derive_run_config,
                             pid_alive, python_alive, status_rows, summarize)

_FAKE_CREATE = textwrap.dedent(r'''
    import hashlib, json, sys, time
    from pathlib import Path
    import yaml

    args = sys.argv[1:]
    brief = Path(args[args.index("--file") + 1])
    config = Path(args[args.index("--config") + 1])
    mode = args[args.index("--mode") + 1]
    out = Path(yaml.safe_load(config.read_text(encoding="utf-8"))["unity_pipeline"]["unity_output_dir"])
    text = brief.read_text(encoding="utf-8")
    print("STEP 2 -- LLM generates project concept", flush=True)
    if mode == "stall":
        time.sleep(60)
    if mode == "crash":
        print("boom: the child died before its run log existed", flush=True)
        sys.exit(3)
    print("STEP 3 -- LLM fills project schema", flush=True)
    logs = out / "_run_logs"
    logs.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format": "unity-create-run-log/v2",
        "raw_input": {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()},
        "source_lineage": {"head_commit": "abc1234567890", "dirty": False},
        "last_step": {"number": 7, "name": "Playability"},
        "project_path": None, "playability_summary": None,
        "failed_run_report": None, "failure": None,
    }
    if mode in ("pass", "fail"):
        project = out / "NeonBounty"
        (project / "Assets").mkdir(parents=True, exist_ok=True)
        print(f"Project written to: {project}", flush=True)
        manifest["project_path"] = str(project)
        manifest["playability_summary"] = "PLAYABILITY 91/100  (29 pass, 0 fail)"
        print(manifest["playability_summary"], flush=True)
    if mode in ("fail", "env"):
        category = "playability_gate" if mode == "fail" else "llm_transport"
        reports = out / "_failed_runs"
        reports.mkdir(parents=True, exist_ok=True)
        report = reports / "failed-run-1.json"
        report.write_text(json.dumps({
            "failure": {"category": category, "message": f"{category} happened"},
            "phase": {"failed_phase": "Steps 6-7 (generation + playability)"},
        }), encoding="utf-8")
        manifest["failed_run_report"] = str(report)
        manifest["status"] = "failed"
        manifest["failure"] = {"message": f"{category} happened"}
        (logs / "unity-create-1.json").write_text(json.dumps(manifest), encoding="utf-8")
        sys.exit(1)
    manifest["status"] = "succeeded"
    (logs / "unity-create-1.json").write_text(json.dumps(manifest), encoding="utf-8")
''')

_FAKE_VERIFY = textwrap.dedent(r'''
    import sys
    from pathlib import Path

    project = Path(sys.argv[1])
    mode = sys.argv[sys.argv.index("--mode") + 1]
    print("COMPILE CHECK", flush=True)
    if mode == "verify-pass":
        print("| PASS  compile=OK  playability=91/100  playtest=OK |", flush=True)
        sys.exit(0)
    copy = project.parent / f"_verify_{project.name}"
    (copy / "Library" / "Artifacts").mkdir(parents=True, exist_ok=True)
    (copy / "Library" / "Artifacts" / "blob.bin").write_bytes(b"x" * 4096)
    (copy / "Logs").mkdir(exist_ok=True)
    (copy / "Logs" / "Editor.log").write_text("error CS0246", encoding="utf-8")
    print("PLAYTEST", flush=True)
    print("Skipped - fix compile errors first.", flush=True)
    print(f"Verify copy kept for debugging: {copy}", flush=True)
    print("| FAIL  compile=FAIL  playability=91/100  playtest=OK |", flush=True)
    sys.exit(1)
''')


class _Plan:
    """Scripted modes for successive create/verify children, recording each command."""

    def __init__(self, tmp_path: Path, creates: list[str], verifies: list[str] | None = None):
        self.create_script = tmp_path / "fake_create.py"
        self.verify_script = tmp_path / "fake_verify.py"
        self.create_script.write_text(_FAKE_CREATE, encoding="utf-8")
        self.verify_script.write_text(_FAKE_VERIFY, encoding="utf-8")
        self.creates = list(creates)
        self.verifies = list(verifies or [])
        self.create_calls: list[list[str]] = []
        self.verify_calls: list[list[str]] = []

    def create(self, *, python: str, repo_root: Path, brief: Path, config: Path,
               create_args: list[str]) -> list[str]:
        cmd = [python, str(self.create_script), "--file", str(brief), "--config", str(config),
               *create_args, "--mode", self.creates.pop(0)]
        self.create_calls.append(cmd)
        return cmd

    def verify(self, *, python: str, repo_root: Path, project: Path, config: Path) -> list[str]:
        cmd = [python, str(self.verify_script), str(project), "--config", str(config),
               "--mode", self.verifies.pop(0)]
        self.verify_calls.append(cmd)
        return cmd


def _config(tmp_path: Path, **extra: Any) -> Path:
    path = tmp_path / "config.yaml"
    data = {
        "lm_studio": {"model": "qwen3.5-9b", "max_tokens": 15000},
        "unity_pipeline": {"lookups_dir": "lookups", "unity_output_dir": str(tmp_path / "out")},
        **extra,
    }
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _brief(tmp_path: Path, text: str = "A bounty hunter on a hoverbike.\n") -> Path:
    path = tmp_path / "brief.txt"
    path.write_text(text, encoding="utf-8")
    return path


def _batch(tmp_path: Path, *, target: int = 3, verify: bool = False,
           create_args: list[str] | None = None) -> Batch:
    return Batch.create(tmp_path / "out" / "_batches" / "neon", brief=_brief(tmp_path),
                        config=_config(tmp_path), create_args=create_args or [],
                        verify=verify, target=target)


def _driver(batch: Batch, plan: _Plan, lines: list[str] | None = None,
            preflight: Any = None) -> BatchDriver:
    sink = lines if lines is not None else []
    return BatchDriver(batch, repo_root=Path.cwd(), echo=sink.append,
                       preflight=preflight or (lambda *_a: None),
                       create_command=plan.create, verify_command=plan.verify,
                       poll_seconds=0.05)


def _reload(batch: Batch) -> Batch:
    return Batch.load(batch.path)


# ── creating, running, resuming ────────────────────────────────────────────────

def test_a_batch_runs_some_now_and_the_rest_later(tmp_path):
    batch = _batch(tmp_path, target=3)
    plan = _Plan(tmp_path, ["pass", "pass", "pass"])

    first = _driver(batch, plan).drive(DriveOptions(max_runs=2))
    assert (first.ran, first.exit_code) == (2, 0)
    assert "--max 2" in first.reason
    batch = _reload(batch)
    assert [r["n"] for r in batch.finished()] == [1, 2]
    assert batch.next_slot() == 3
    assert not batch.lock_path.exists()           # the driver let go

    second = _driver(batch, plan).drive(DriveOptions())
    assert (second.ran, second.exit_code) == (1, 0)
    assert "target reached" in second.reason
    batch = _reload(batch)
    assert [r["verdict"] for r in batch.finished()] == ["UNVERIFIED"] * 3
    assert batch.next_slot() is None


def test_every_run_reads_the_snapshot_in_its_own_folder(tmp_path):
    batch = _batch(tmp_path, target=2)
    plan = _Plan(tmp_path, ["pass", "pass"])
    _driver(batch, plan).drive(DriveOptions())
    batch = _reload(batch)

    assert batch.brief_path.read_bytes() == _brief(tmp_path).read_bytes()
    for cmd in plan.create_calls:
        assert cmd[cmd.index("--file") + 1] == str(batch.brief_path)
    projects = set()
    for record in batch.finished():
        folder = batch.path / record["folder"]
        cfg = yaml.safe_load((folder / "config.yaml").read_text(encoding="utf-8"))
        assert cfg["unity_pipeline"]["unity_output_dir"] == str(folder)
        create = record["create"]
        assert Path(create["project"]).parent == folder
        assert create["brief_matches_snapshot"] is True
        assert create["playability"] == 91 and create["commit"] == "abc1234567890"
        assert create["run_log"] == f"{record['folder']}/_run_logs/unity-create-1.json"
        projects.add(create["project"])
    assert len(projects) == 2                      # no run overwrote another


def test_editing_the_brief_file_cannot_change_what_runs_read(tmp_path):
    batch = _batch(tmp_path, target=1)
    _brief(tmp_path, "A completely different game.\n")
    with pytest.raises(BatchError, match="not the brief this batch was created with"):
        batch.check_brief(tmp_path / "brief.txt")
    plan = _Plan(tmp_path, ["pass"])
    _driver(batch, plan).drive(DriveOptions())
    assert _reload(batch).finished()[0]["create"]["brief_matches_snapshot"] is True


def test_resume_refuses_other_flags_config_or_verify(tmp_path):
    batch = _batch(tmp_path, create_args=["--no-micro-code"])
    batch.check_args(None)
    batch.check_args(["--no-micro-code"])
    with pytest.raises(BatchError, match="not one sample"):
        batch.check_args(["--no-think"])
    with pytest.raises(BatchError, match="runs config"):
        batch.check_config(tmp_path / "other.yaml")
    with pytest.raises(BatchError, match="verify=off"):
        batch.check_verify(True)
    batch.check_verify(None)


def test_forwarded_flags_reach_unity_create_unchanged(tmp_path):
    batch = _batch(tmp_path, target=1, create_args=["--no-micro-code", "--model", "x"])
    plan = _Plan(tmp_path, ["pass"])
    _driver(batch, plan).drive(DriveOptions())
    cmd = plan.create_calls[0]
    assert cmd[cmd.index("--no-micro-code"):cmd.index("--no-micro-code") + 3] == [
        "--no-micro-code", "--model", "x"]


@pytest.mark.parametrize("args,message", [
    (["--file", "x.txt"], "owned by the batch"),
    (["--config=x.yaml"], "owned by the batch"),
    (["-c", "x.yaml"], "owned by the batch"),
    (["a racing game"], "inline prompt"),
    (["--help"], "--help"),
])
def test_flags_the_batch_owns_are_refused(args, message):
    with pytest.raises(BatchError, match=message):
        check_create_args(args)


def test_ordinary_unity_create_flags_pass():
    assert check_create_args(["--no-micro-code", "--seed", "7", "-m", "qwen"]) == [
        "--no-micro-code", "--seed", "7", "-m", "qwen"]


@pytest.mark.parametrize("source", [
    {"unity_pipeline": {"lookups_dir": "lookups", "unity_output_dir": "i:/u/unity"}, "x": 1},
    {"pipeline": {"lookups_dir": "lookups", "output_dir": "I:/u"}, "x": 1},
])
def test_the_run_config_changes_only_the_output_dir(source, tmp_path):
    derived = derive_run_config(source, tmp_path / "runs" / "01")
    block = derived["unity_pipeline"]
    assert block["unity_output_dir"] == str(tmp_path / "runs" / "01")
    assert block["lookups_dir"] == "lookups"
    assert derived["x"] == 1
    assert source.get("unity_pipeline", {}).get("unity_output_dir") in (None, "i:/u/unity")


# ── failures are samples; attempts that did not end are not ────────────────────

def test_a_failed_run_is_a_sample_with_its_cause(tmp_path):
    batch = _batch(tmp_path, target=1)
    _driver(batch, _Plan(tmp_path, ["fail"])).drive(DriveOptions())
    (record,) = _reload(batch).finished()
    assert record["verdict"] == "FAIL"
    create = record["create"]
    assert create["exit_code"] == 1
    assert create["category"] == "playability_gate"
    assert create["failed_phase"] == "Steps 6-7 (generation + playability)"
    assert create["failed_run_report"] == "runs/01/_failed_runs/failed-run-1.json"


def test_a_child_that_dies_before_its_run_log_is_still_recorded(tmp_path):
    batch = _batch(tmp_path, target=1)
    _driver(batch, _Plan(tmp_path, ["crash"])).drive(DriveOptions())
    (record,) = _reload(batch).finished()
    assert record["verdict"] == "FAIL"
    assert record["create"]["exit_code"] == 3
    assert record["create"]["last_step"] == "2 LLM generates project concept"
    assert "boom" in record["create"]["message"]


def test_a_silent_run_is_stall_killed_and_counted(tmp_path):
    batch = _batch(tmp_path, target=1)
    lines: list[str] = []
    result = _driver(batch, _Plan(tmp_path, ["stall"]), lines).drive(
        DriveOptions(stall_minutes=1.5 / 60))
    assert result.exit_code == 0
    (record,) = _reload(batch).finished()
    assert record["verdict"] == "STALLED"
    assert record["create"]["category"] == "stalled"
    assert record["create"]["minutes"] < 0.5        # killed, not waited out
    assert any("STALL-KILLED" in line for line in lines)


def test_an_llm_backend_failure_is_discarded_and_stops_the_batch(tmp_path):
    batch = _batch(tmp_path, target=2)
    plan = _Plan(tmp_path, ["env", "pass", "pass"])
    result = _driver(batch, plan).drive(DriveOptions())
    assert result.exit_code == 3 and result.ran == 0
    batch = _reload(batch)
    assert batch.runs == []
    (gone,) = batch.discarded
    assert gone["status"] == "env" and gone["folder"] == "runs/01"

    _driver(batch, plan).drive(DriveOptions())
    batch = _reload(batch)
    assert [(r["n"], r["attempt"], r["folder"]) for r in batch.finished()] == [
        (1, 2, "runs/01-a2"), (2, 1, "runs/02")]
    assert (batch.path / "runs" / "01" / "create.log").is_file()   # evidence kept


def test_ctrl_c_discards_the_attempt_and_the_slot_runs_again(tmp_path, monkeypatch):
    batch = _batch(tmp_path, target=1)
    real_run_child = ub.run_child
    calls = {"n": 0}

    def interrupt_first(*args: Any, **kwargs: Any) -> ub.ChildOutcome:
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        return real_run_child(*args, **kwargs)

    monkeypatch.setattr(ub, "run_child", interrupt_first)
    plan = _Plan(tmp_path, ["pass", "pass"])
    with pytest.raises(BatchInterrupted):
        _driver(batch, plan).drive(DriveOptions())
    batch = _reload(batch)
    assert batch.runs == [] and batch.discarded[0]["status"] == "interrupted"
    assert not batch.lock_path.exists()

    _driver(batch, plan).drive(DriveOptions())
    (record,) = _reload(batch).finished()
    assert (record["attempt"], record["verdict"]) == (2, "UNVERIFIED")


def test_a_record_left_running_by_a_dead_driver_is_recovered(tmp_path):
    batch = _batch(tmp_path, target=1)
    (batch.runs_dir / "01").mkdir(parents=True)
    batch.runs.append({"n": 1, "attempt": 1, "state": "running", "folder": "runs/01",
                       "driver_pid": 0, "child_pid": None})
    batch.save()
    lines: list[str] = []
    _driver(_reload(batch), _Plan(tmp_path, ["pass"]), lines).drive(DriveOptions())
    batch = _reload(batch)
    assert batch.discarded[0]["status"] == "interrupted"
    assert batch.finished()[0]["folder"] == "runs/01-a2"
    assert any("cut off when its driver ended" in line for line in lines)


def test_an_orphaned_run_that_is_still_alive_blocks_the_takeover(tmp_path):
    batch = _batch(tmp_path, target=1)
    batch.runs.append({"n": 1, "attempt": 1, "state": "running", "folder": "runs/01",
                       "driver_pid": 0, "child_pid": os.getpid()})
    batch.save()
    with pytest.raises(BatchError, match="still running"):
        _driver(_reload(batch), _Plan(tmp_path, ["pass"])).drive(DriveOptions())


def test_a_preflight_problem_stops_before_a_slot_is_used(tmp_path):
    batch = _batch(tmp_path, target=2)
    plan = _Plan(tmp_path, ["pass"])
    result = _driver(batch, plan, preflight=lambda *_a: "LM Studio is not answering").drive(
        DriveOptions())
    assert result.exit_code == 2 and "LM Studio is not answering" in result.reason
    batch = _reload(batch)
    assert batch.runs == [] and batch.discarded == [] and plan.create_calls == []


# ── stopping, target changes, discards ─────────────────────────────────────────

def test_a_stop_request_ends_the_batch_after_the_current_run(tmp_path):
    batch = _batch(tmp_path, target=3)
    plan = _Plan(tmp_path, ["pass", "pass", "pass"])
    original = plan.create

    def create_then_request_stop(**kwargs: Any) -> list[str]:
        batch.stop_path.write_text("stop", encoding="utf-8")
        return original(**kwargs)

    driver = _driver(batch, plan)
    driver.create_command = create_then_request_stop
    result = driver.drive(DriveOptions())
    assert result.ran == 1 and "--stop" in result.reason
    assert not batch.stop_path.exists()


def test_stop_with_no_driver_running_leaves_nothing_behind(tmp_path):
    batch = _batch(tmp_path)
    batch.stop_path.write_text("stale", encoding="utf-8")
    assert batch.request_stop() is None
    assert not batch.stop_path.exists()


@pytest.fixture
def other_python():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


def test_a_live_driver_blocks_a_second_driver_and_manifest_edits(tmp_path, other_python):
    batch = _batch(tmp_path, target=2)
    batch.lock_path.write_text(json.dumps({"pid": other_python}), encoding="utf-8")
    assert batch.live_driver_pid() == other_python
    with pytest.raises(BatchError, match=f"pid {other_python}"):
        _driver(batch, _Plan(tmp_path, ["pass"])).drive(DriveOptions())
    with pytest.raises(BatchError, match="--stop"):
        batch.set_target(runs=None, add=1)
    assert batch.request_stop() == other_python
    assert batch.stop_path.exists()


def test_the_target_can_be_set_or_raised_but_not_below_held_runs(tmp_path):
    batch = _batch(tmp_path, target=2)
    _driver(batch, _Plan(tmp_path, ["pass", "pass"])).drive(DriveOptions())
    batch = _reload(batch)
    assert batch.set_target(runs=None, add=3) == 5
    assert batch.set_target(runs=4, add=None) == 4
    assert _reload(batch).target == 4
    with pytest.raises(BatchError, match="below the 2 run"):
        batch.set_target(runs=1, add=None)
    with pytest.raises(BatchError, match="not both"):
        batch.set_target(runs=3, add=1)


def test_a_discard_needs_a_reason_and_requeues_the_slot(tmp_path):
    batch = _batch(tmp_path, target=1)
    plan = _Plan(tmp_path, ["fail", "pass"])
    _driver(batch, plan).drive(DriveOptions())
    batch = _reload(batch)
    with pytest.raises(BatchError, match="--reason"):
        batch.discard(1, "  ")
    entry = batch.discard(1, "LM Studio stopped by hand")
    assert entry["status"] == "discarded" and entry["verdict"] == "FAIL"
    assert batch.next_slot() == 1
    _driver(batch, plan).drive(DriveOptions())
    (record,) = _reload(batch).finished()
    assert (record["attempt"], record["verdict"]) == (2, "UNVERIFIED")


# ── verify ─────────────────────────────────────────────────────────────────────

def test_a_run_passes_only_when_the_real_compile_gate_passes(tmp_path):
    batch = _batch(tmp_path, target=2, verify=True)
    plan = _Plan(tmp_path, ["pass", "pass"], ["verify-pass", "verify-fail"])
    _driver(batch, plan).drive(DriveOptions())
    passed, failed = _reload(batch).finished()

    assert passed["verdict"] == "PASS"
    assert passed["verify"]["compile"] == "OK" and passed["verify"]["playtest"] == "OK"
    verify_cfg = plan.verify_calls[0][plan.verify_calls[0].index("--config") + 1]
    assert verify_cfg == str(batch.path / "runs" / "01" / "config.yaml")

    assert failed["verdict"] == "FAIL"
    verify = failed["verify"]
    assert verify["compile"] == "FAIL"
    assert verify["playtest"] == "skipped"        # e2e-verify skips it on a compile FAIL
    assert verify["kept_copy"] == "runs/02/_verify_NeonBounty"
    kept = batch.path / "runs" / "02" / "_verify_NeonBounty"
    assert not (kept / "Library").exists()        # the Editor cache is pruned
    assert (kept / "Logs" / "Editor.log").is_file()   # the evidence is not
    assert verify["pruned_mb"] >= 0


def test_a_failed_create_is_not_verified(tmp_path):
    batch = _batch(tmp_path, target=1, verify=True)
    plan = _Plan(tmp_path, ["crash"], ["verify-pass"])
    _driver(batch, plan).drive(DriveOptions())
    (record,) = _reload(batch).finished()
    assert record["verdict"] == "FAIL" and record["verify"] is None
    assert plan.verify_calls == []


def test_an_interrupted_verify_keeps_the_create_and_reruns_only_verify(tmp_path, monkeypatch):
    batch = _batch(tmp_path, target=1, verify=True)
    real_run_child = ub.run_child
    calls = {"n": 0}

    def interrupt_verify(*args: Any, **kwargs: Any) -> ub.ChildOutcome:
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real_run_child(*args, **kwargs)

    monkeypatch.setattr(ub, "run_child", interrupt_verify)
    plan = _Plan(tmp_path, ["pass"], ["verify-pass", "verify-pass"])
    with pytest.raises(BatchInterrupted):
        _driver(batch, plan).drive(DriveOptions())
    (record,) = _reload(batch).runs
    assert record["state"] == "verifying" and record["create"]["exit_code"] == 0

    _driver(_reload(batch), plan).drive(DriveOptions())
    (record,) = _reload(batch).finished()
    assert record["verdict"] == "PASS"
    assert len(plan.create_calls) == 1            # unity-create did not run again


# ── reading it back ────────────────────────────────────────────────────────────

def test_status_rows_and_summary_report_samples_not_discards(tmp_path):
    batch = _batch(tmp_path, target=3)
    plan = _Plan(tmp_path, ["pass", "env", "fail", "pass"])
    driver = _driver(batch, plan)
    driver.drive(DriveOptions())                  # run 01 passes, run 02 hits env
    _driver(_reload(batch), plan).drive(DriveOptions())
    batch = _reload(batch)

    summary = summarize(batch)
    assert summary["finished"] == 3 and summary["target"] == 3
    assert summary["verdicts"] == {"FAIL": 1, "UNVERIFIED": 2}
    assert summary["failure_categories"] == {"playability_gate": 1}
    assert summary["discarded"] == 1
    assert summary["commits"] == ["abc123456"]
    assert summary["median_playability"] == 91
    rows = status_rows(batch)
    assert [(r["n"], r["attempt"], r["verdict"]) for r in rows] == [
        (1, 1, "UNVERIFIED"), (2, 2, "FAIL"), (3, 1, "UNVERIFIED")]
    assert rows[1]["failure"].startswith("playability_gate:")
    assert rows[1]["reached"] == "Steps 6-7 (generation + playability)"


def test_the_events_log_tells_the_story(tmp_path):
    batch = _batch(tmp_path, target=1)
    _driver(batch, _Plan(tmp_path, ["pass"])).drive(DriveOptions())
    events = (batch.path / "events.log").read_text(encoding="utf-8")
    for marker in ("BATCH CREATED", "DRIVER START", "RUN 01: unity-create starting",
                   "RUN 01: UNVERIFIED", "DRIVER STOP: target reached"):
        assert marker in events


# ── processes ──────────────────────────────────────────────────────────────────

def test_pid_probes_never_signal_and_tell_live_from_gone():
    assert pid_alive(os.getpid()) and python_alive(os.getpid())
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    assert not pid_alive(done.pid)
    assert not pid_alive(0) and not pid_alive(None)


# ── the CLI ────────────────────────────────────────────────────────────────────

def test_cli_creates_lists_and_refuses_a_second_brief(tmp_path, monkeypatch):
    from main import app

    monkeypatch.chdir(tmp_path)
    config = _config(tmp_path)
    brief = _brief(tmp_path)
    runner = CliRunner()
    created = runner.invoke(app, ["unity-batch", "neon", "--file", str(brief), "--config",
                                  str(config), "--runs", "4", "--max", "0", "--no-micro-code"])
    assert created.exit_code == 0, created.output
    batch = Batch.load(tmp_path / "out" / "_batches" / "neon")
    assert batch.target == 4 and batch.create_args == ["--no-micro-code"]

    added = runner.invoke(app, ["unity-batch", "neon", "--config", str(config),
                                "--add", "2", "--max", "0"])
    assert added.exit_code == 0, added.output
    assert Batch.load(batch.path).target == 6

    other = tmp_path / "other.txt"
    other.write_text("Something else.\n", encoding="utf-8")
    refused = runner.invoke(app, ["unity-batch", "neon", "--file", str(other),
                                  "--config", str(config), "--max", "0"])
    assert refused.exit_code == 1 and "not the brief" in refused.output

    listing = runner.invoke(app, ["unity-batch-status", "--config", str(config)])
    assert listing.exit_code == 0 and "neon" in listing.output and "0/6" in listing.output
    detail = runner.invoke(app, ["unity-batch-status", "neon", "--config", str(config), "--json"])
    assert detail.exit_code == 0
    assert json.loads(detail.output)["summary"]["target"] == 6


def test_cli_without_a_brief_names_the_remedy(tmp_path, monkeypatch):
    from main import app

    monkeypatch.chdir(tmp_path)
    config = _config(tmp_path)
    result = CliRunner().invoke(app, ["unity-batch", "nope", "--config", str(config)])
    assert result.exit_code == 1
    assert "--file" in result.output
