"""Resumable batches of live ``unity-create`` runs, examined later.

A batch is N runs of ONE brief through the real pipeline, Pipeline LLM
included, driven one at a time by ``python main.py unity-batch``. Every run
writes into its own folder, so a later run can never overwrite an earlier
result, and the batch manifest records which runs are finished. Stop the
driver whenever the machine is needed (Ctrl-C, ``--stop``, ``--max``,
``--hours``). Start it again later and it continues with the next run. Raise
the target with ``--add``. ``python main.py unity-batch-status`` reads it all
back. docs/UNITY_BATCH.md is the reference.

Contract, the same as unity-create-run-log/v2 (docs/UNITY_RUN_LOGS.md):

- Review provenance only. Nothing here enters a schema or reaches Steps 2-7.
  The driver runs the ordinary ``unity-create`` CLI in a child process; it
  never imports or alters the compiler.
- The brief is snapshotted into the batch when the batch is created, and every
  run reads that snapshot verbatim, so every sample saw byte-identical input
  (Eval Discipline: no transformation). A different brief is a different
  batch: resuming with one abstains loudly.
- The one derived input is the per-run config: the batch config with
  ``unity_pipeline.unity_output_dir`` pointed at the run's own folder. Nothing
  else in it changes.
- A run that ended is a sample, pass or fail. An attempt that did not end
  (interrupted, or the LLM backend unreachable) is not a sample: its folder is
  kept, the attempt is listed under ``discarded``, and its slot runs again.
  Discarding a finished sample is a human decision (``--discard N --reason``)
  and is recorded the same way.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from .unity_run_log import _inline_prompt, _option_value

# The checkout whose main.py every run executes: the one this module came from,
# so a batch launched from a worktree tests that worktree's code.
REPO_ROOT = Path(__file__).resolve().parents[1]

BATCH_FORMAT = "unity-batch/v1"
BATCHES_DIRNAME = "_batches"
MANIFEST_NAME = "batch.json"
BRIEF_NAME = "brief.txt"
LOCK_NAME = "driver.lock"
STOP_NAME = "STOP"
EVENTS_NAME = "events.log"
RUNS_DIRNAME = "runs"
RUN_CONFIG_NAME = "config.yaml"
CREATE_LOG_NAME = "create.log"
VERIFY_LOG_NAME = "verify.log"

DEFAULT_TARGET = 10
DEFAULT_STALL_MINUTES = 60.0
DEFAULT_MIN_FREE_GB = 10.0
VERIFY_TIMEOUT_SECONDS = 1800

# Unity caches inside a kept `_verify_<name>` copy that the Editor rebuilds on
# open. Pruned after the verdict is read; the copy's logs and sources stay.
PRUNED_VERIFY_DIRS = ("Library", "Temp")

# The run's own unity-create flags that the batch owns. Passing one through
# would make the run read something other than the snapshot or the derived
# config, so it abstains instead.
_OWNED_CREATE_FLAGS = ("--file", "-f", "--config", "-c")

# Failed-run categories (src/unity_failed_run.py) that describe the machine, not
# the pipeline: the attempt is not a sample, and the batch stops so the next
# slot does not fail the same way.
ENV_CATEGORIES = frozenset({"llm_transport"})

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_STEP_RE = re.compile(r"^\s*STEP\s+(\S+)\s+--\s+(.+?)\s*$", re.MULTILINE)
_PLAYABILITY_SCORE_RE = re.compile(r"PLAYABILITY\s+(\d+)/100")
_VERIFY_VERDICT_RE = re.compile(
    r"(PASS|FAIL)\s+compile=(OK|FAIL)\s+playability=(\d+)/100"
    r"(?:\s+\([^)]*\))?(?:\s+playtest=(OK|FAIL))?"
)
_PLAYTEST_SKIPPED_RE = re.compile(r"^PLAYTEST\s*$\s*^Skipped", re.MULTILINE)
_KEPT_COPY_RE = re.compile(r"Verify copy kept for debugging:\s*(.+?)\s*$", re.MULTILINE)

# Child output lines echoed to the console when not following everything.
_MILESTONE_RE = re.compile(
    r"^\s*STEP\s|Project written to:|PLAYABILITY\s+\d|Failed-run diagnostics:|"
    r"Unity run review log:|Traceback \(most recent call last\)|"
    r"(?:PASS|FAIL)\s+compile=|^\w*(?:Error|Exception):",
)

Echo = Callable[[str], None]


class BatchError(RuntimeError):
    """A loud abstain: the batch cannot proceed as asked. The message names the remedy."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _text_sha256(path: Path) -> str:
    """The brief hash exactly as unity-create's run log records it (text mode)."""
    return _sha256_bytes(path.read_text(encoding="utf-8").encode("utf-8"))


# ── processes ──────────────────────────────────────────────────────────────────

def pid_alive(pid: int | None) -> bool:
    """True when a process with this pid is running. Never signals it.

    `os.kill(pid, 0)` is not a probe on Windows (it terminates the process),
    so Windows asks the kernel for the exit code instead.
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return int(code.value) == 259                      # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_image(pid: int) -> str | None:
    """The executable path of a live process, or None when it cannot be read."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return None
            return str(buffer.value)
        finally:
            kernel32.CloseHandle(handle)
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None


def python_alive(pid: int | None) -> bool:
    """True when `pid` is running AND is a Python process.

    A recorded driver or run pid can be reused by an unrelated process after a
    reboot. Only a live Python process is treated as the batch's own; one whose
    image cannot be read is given the benefit of the doubt.
    """
    if not pid_alive(pid):
        return False
    assert isinstance(pid, int)
    image = process_image(pid)
    if image is None:
        return True
    return "python" in Path(image).name.lower()


def kill_tree(pid: int) -> None:
    """Kill a child and everything it started (Unity, Blender, ...)."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.2)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


@dataclass
class ChildOutcome:
    exit_code: int | None
    stalled: bool = False


def run_child(
    cmd: list[str],
    *,
    cwd: Path,
    log_path: Path,
    stall_seconds: float | None,
    env: dict[str, str] | None = None,
    on_line: Echo | None = None,
    on_start: Callable[[int], None] | None = None,
    poll_seconds: float = 1.0,
) -> ChildOutcome:
    """Run one child to completion, mirroring its output into `log_path`.

    The child gets its own process group, so Ctrl-C reaches only this driver,
    which then kills the child's whole tree and re-raises. A child that writes
    nothing for `stall_seconds` is killed and reported `stalled`.
    """
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as sink:
        proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
        if on_start is not None:
            on_start(proc.pid)
        last_output = [time.monotonic()]

        def pump() -> None:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                sink.write(raw)
                sink.flush()
                last_output[0] = time.monotonic()
                if on_line is not None:
                    try:
                        on_line(raw.decode("utf-8", errors="replace").rstrip("\r\n"))
                    except Exception:   # noqa: BLE001 - echo must never stop the run
                        pass

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        stalled = False
        try:
            while proc.poll() is None:
                time.sleep(poll_seconds)
                if stall_seconds and time.monotonic() - last_output[0] > stall_seconds:
                    stalled = True
                    kill_tree(proc.pid)
                    break
        except BaseException:
            kill_tree(proc.pid)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass
            reader.join(timeout=5)
            raise
        try:
            code: int | None = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            code = None
        reader.join(timeout=10)
    return ChildOutcome(exit_code=code, stalled=stalled)


# ── where batches live ─────────────────────────────────────────────────────────

def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise BatchError(f"cannot read config {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise BatchError(f"config {path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise BatchError(f"config {path} is not a YAML mapping")
    return data


def _unity_block(config: dict[str, Any]) -> dict[str, Any]:
    """The block UnityPipeline reads: `unity_pipeline`, else the generic `pipeline`."""
    block = config.get("unity_pipeline", config.get("pipeline", {}))
    return block if isinstance(block, dict) else {}


def batches_root(config_path: Path, cwd: Path | None = None) -> Path:
    """`<unity_output_dir>/_batches` for this config, resolved like unity-create does."""
    base = Path.cwd() if cwd is None else cwd
    output = str(_unity_block(_load_yaml(config_path)).get("unity_output_dir") or "output/unity")
    root = Path(output).expanduser()
    if not root.is_absolute():
        root = base / root
    return root.resolve() / BATCHES_DIRNAME


def resolve_batch_dir(name_or_path: str, root: Path) -> Path:
    """A batch name under `root`, or an explicit batch folder path."""
    candidate = Path(name_or_path).expanduser()
    if any(sep in name_or_path for sep in ("/", "\\")) or candidate.is_absolute():
        return candidate.resolve()
    if not _NAME_RE.match(name_or_path):
        raise BatchError(
            f"batch name {name_or_path!r} must be 1-64 letters, digits, '.', '_' or '-' "
            "(or pass a batch folder path)"
        )
    return root / name_or_path


def list_batches(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if (p / MANIFEST_NAME).is_file())


def derive_run_config(source: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """The batch config with the Unity output dir pointed at `run_dir`. Nothing else changes."""
    derived = copy.deepcopy(source)
    if not isinstance(derived.get("unity_pipeline"), dict):
        derived["unity_pipeline"] = copy.deepcopy(_unity_block(source))
    derived["unity_pipeline"]["unity_output_dir"] = str(run_dir)
    return derived


def check_create_args(args: list[str]) -> list[str]:
    """Validate the unity-create flags a batch forwards to every run."""
    for arg in args:
        flag = arg.split("=", 1)[0]
        if flag in _OWNED_CREATE_FLAGS:
            raise BatchError(
                f"{flag} is owned by the batch (every run reads the batch's brief "
                "snapshot and its own derived config). Pass --file/--config to "
                "unity-batch itself, not as a unity-create flag."
            )
        if flag in ("--help", "-h"):
            raise BatchError("--help is not a unity-create flag a batch can forward")
    prompt = _inline_prompt(args)
    if prompt is not None:
        raise BatchError(
            f"{prompt!r} would reach unity-create as an inline prompt. A batch runs "
            "the brief file given with --file; pass only unity-create flags."
        )
    return list(args)


# ── the batch ──────────────────────────────────────────────────────────────────

# Windows locks are mandatory per byte range, so the lock sits on one byte far
# past the pid text; other processes can still read the pid while it is held.
_LOCK_OFFSET = 1 << 20


def _try_lock(fd: int) -> bool:
    """Take the non-blocking exclusive OS lock on an open lock file, or return False."""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def _pid_text(pid: int) -> str:
    return f"pid {pid}" if pid > 0 else "pid unknown"


def _folder_name(n: int, attempt: int) -> str:
    return f"{n:02d}" if attempt == 1 else f"{n:02d}-a{attempt}"


@dataclass
class Batch:
    """One batch folder and its manifest (`batch.json`)."""

    path: Path
    data: dict[str, Any] = field(default_factory=dict)
    _lock_fd: int | None = field(default=None, repr=False, compare=False)

    # -- files --
    @property
    def manifest_path(self) -> Path:
        return self.path / MANIFEST_NAME

    @property
    def brief_path(self) -> Path:
        return self.path / BRIEF_NAME

    @property
    def lock_path(self) -> Path:
        return self.path / LOCK_NAME

    @property
    def stop_path(self) -> Path:
        return self.path / STOP_NAME

    @property
    def runs_dir(self) -> Path:
        return self.path / RUNS_DIRNAME

    @property
    def name(self) -> str:
        return str(self.data.get("name") or self.path.name)

    @property
    def target(self) -> int:
        return int(self.data.get("target") or 0)

    @property
    def runs(self) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = self.data.setdefault("runs", [])
        return runs

    @property
    def discarded(self) -> list[dict[str, Any]]:
        discarded: list[dict[str, Any]] = self.data.setdefault("discarded", [])
        return discarded

    @property
    def config_path(self) -> Path:
        return Path(self.data["config"]["path"])

    @property
    def create_args(self) -> list[str]:
        return list(self.data.get("create_args") or [])

    @property
    def verify(self) -> bool:
        return bool(self.data.get("verify", True))

    # -- lifecycle --
    @classmethod
    def exists(cls, path: Path) -> bool:
        return (path / MANIFEST_NAME).is_file()

    @classmethod
    def load(cls, path: Path) -> "Batch":
        manifest = path / MANIFEST_NAME
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except OSError as exc:
            raise BatchError(f"no batch at {path} ({exc})") from exc
        except json.JSONDecodeError as exc:
            raise BatchError(f"{manifest} is not valid JSON: {exc}") from exc
        if data.get("format") != BATCH_FORMAT:
            raise BatchError(f"{manifest} is {data.get('format')!r}, not {BATCH_FORMAT}")
        return cls(path=path, data=data)

    @classmethod
    def create(
        cls,
        path: Path,
        *,
        brief: Path,
        config: Path,
        create_args: list[str],
        verify: bool,
        target: int,
    ) -> "Batch":
        if cls.exists(path):
            raise BatchError(f"a batch already exists at {path}")
        if target < 1:
            raise BatchError("--runs must be at least 1")
        create_args = check_create_args(create_args)
        try:
            brief_bytes = brief.read_bytes()
        except OSError as exc:
            raise BatchError(f"cannot read brief {brief}: {exc}") from exc
        if not brief_bytes.strip():
            raise BatchError(f"brief {brief} is empty")
        config_bytes = config.read_bytes() if config.is_file() else b""
        if not config_bytes:
            raise BatchError(f"config {config} does not exist or is empty")
        _load_yaml(config)
        path.mkdir(parents=True, exist_ok=True)
        snapshot = path / BRIEF_NAME
        snapshot.write_bytes(brief_bytes)
        batch = cls(path=path, data={
            "format": BATCH_FORMAT,
            "name": path.name,
            "created_utc": _iso(_utc_now()),
            "brief": {
                "source_path": str(brief.resolve()),
                "snapshot": BRIEF_NAME,
                "sha256": _sha256_bytes(brief_bytes),
                "text_sha256": _text_sha256(snapshot),
                "bytes": len(brief_bytes),
            },
            "config": {"path": str(config.resolve()),
                       "sha256_at_create": _sha256_bytes(config_bytes)},
            "create_args": create_args,
            "verify": bool(verify),
            "target": int(target),
            "runs": [],
            "discarded": [],
        })
        batch.save()
        batch.event(
            f"BATCH CREATED: target {target} run(s) of {brief.name} "
            f"(sha256 {batch.data['brief']['sha256'][:12]}), config {config}, "
            f"verify={'on' if verify else 'off'}, unity-create flags {create_args or '(none)'}"
        )
        return batch

    def save(self) -> None:
        temporary = self.manifest_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        temporary.replace(self.manifest_path)

    def event(self, text: str, echo: Echo | None = None) -> None:
        line = f"{_utc_now().astimezone().strftime('%m-%d %H:%M:%S')} {text}"
        try:
            with (self.path / EVENTS_NAME).open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        if echo is not None:
            echo(line)

    # -- checks against what the batch was created with --
    def check_brief(self, brief: Path | None) -> None:
        if brief is None:
            return
        try:
            given = _sha256_bytes(brief.read_bytes())
        except OSError as exc:
            raise BatchError(f"cannot read brief {brief}: {exc}") from exc
        if given != self.data["brief"]["sha256"]:
            raise BatchError(
                f"{brief} is not the brief this batch was created with "
                f"(sha256 {given[:12]}, batch {self.data['brief']['sha256'][:12]}). "
                "Pooling runs of two briefs measures neither: start a new batch "
                "name for the new brief, or resume without --file (runs always "
                f"read the snapshot {self.brief_path})."
            )

    def check_config(self, config: Path | None) -> None:
        if config is None:
            return
        if config.resolve() != self.config_path.resolve():
            raise BatchError(
                f"this batch runs config {self.config_path}, not {config}. Resume "
                "without --config, or start a new batch for the other config."
            )

    def check_args(self, args: list[str] | None) -> None:
        if not args:
            return
        if check_create_args(args) != self.create_args:
            raise BatchError(
                f"this batch forwards unity-create flags {self.create_args or '(none)'}, "
                f"not {args}. Runs with different flags are not one sample: resume "
                "without them, or start a new batch."
            )

    def check_verify(self, verify: bool | None) -> None:
        if verify is None or verify == self.verify:
            return
        raise BatchError(
            f"this batch was created with verify={'on' if self.verify else 'off'}. "
            "Resume without --verify/--no-verify, or start a new batch."
        )

    def config_sha256(self) -> str:
        try:
            return _sha256_bytes(self.config_path.read_bytes())
        except OSError as exc:
            raise BatchError(f"cannot read the batch config {self.config_path}: {exc}") from exc

    # -- slots --
    def finished(self) -> list[dict[str, Any]]:
        return [r for r in self.runs if r.get("state") == "done"]

    def next_slot(self) -> int | None:
        taken = {int(r["n"]) for r in self.runs}
        for n in range(1, self.target + 1):
            if n not in taken:
                return n
        return None

    def attempts_for(self, n: int) -> int:
        return 1 + sum(1 for d in self.discarded if int(d.get("n", 0)) == n)

    def new_run_dir(self, n: int) -> tuple[Path, int]:
        attempt = self.attempts_for(n)
        while True:
            folder = self.runs_dir / _folder_name(n, attempt)
            if not folder.exists():
                return folder, attempt
            attempt += 1

    # -- the driver lock --
    # An OS lock on `driver.lock`, held by the open handle for the driver's
    # whole life. Taking it is one atomic call, so two drivers started together
    # cannot both win, and the OS drops it when the process dies however it
    # dies, so there is no stale lock to judge and no pid to trust. The pid
    # written inside is for messages only. The file is never deleted: deleting
    # it could orphan a lock a second driver has just taken.
    def _lock_pid(self) -> int:
        try:
            pid = json.loads(self.lock_path.read_text(encoding="utf-8")).get("pid")
        except (OSError, json.JSONDecodeError, AttributeError):
            return 0
        return pid if isinstance(pid, int) and pid > 0 else 0

    def live_driver_pid(self) -> int | None:
        """The pid of the driver holding this batch, 0 when held but unreadable, else None."""
        if self._lock_fd is not None:
            return os.getpid()
        try:
            fd = os.open(self.lock_path, os.O_RDWR)
        except OSError:
            return None
        try:
            if _try_lock(fd):
                _unlock(fd)
                return None
            return self._lock_pid()
        finally:
            os.close(fd)

    def require_no_driver(self, action: str) -> None:
        pid = self.live_driver_pid()
        if pid is not None:
            raise BatchError(
                f"a driver ({_pid_text(pid)}) is running this batch, so {action} would "
                f"race its manifest writes. Ask it to stop after the current run with "
                f"`unity-batch {self.name} --stop`, then retry."
            )

    def acquire(self) -> None:
        if self._lock_fd is not None:
            return
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        if not _try_lock(fd):
            os.close(fd)
            self.require_no_driver("a second driver")
            raise BatchError(f"another driver holds {self.lock_path}")
        self._lock_fd = fd
        stamp = json.dumps({"pid": os.getpid(), "started_utc": _iso(_utc_now())})
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, stamp.encode("utf-8"))

    def release(self) -> None:
        fd = self._lock_fd
        if fd is None:
            return
        self._lock_fd = None
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    # -- edits a human asks for --
    def set_target(self, *, runs: int | None, add: int | None) -> int:
        if runs is not None and add is not None:
            raise BatchError("pass --runs (set the target) or --add (raise it), not both")
        if runs is None and add is None:
            return self.target
        if add is not None and add < 1:
            raise BatchError(f"--add must be at least 1 (got {add}); --runs N sets the target")
        new = int(runs) if runs is not None else self.target + int(add or 0)
        # Run numbers can have gaps after a discard, so the floor is the
        # highest slot held, not the count: runs 2 and 3 with slot 1 free
        # still need a target of 3, or the driver refills slot 1 past it.
        highest = max((int(r["n"]) for r in self.runs), default=0)
        if new < max(highest, 1):
            raise BatchError(
                f"target {new} is below run {highest:02d}, which this batch already holds "
                f"(the target must be at least 1 and cover every held run). Discard runs "
                "first (--discard N --reason ...) if that is the intent."
            )
        if new != self.target:
            self.require_no_driver("changing the target")
            old = self.target
            self.data["target"] = new
            self.save()
            self.event(f"TARGET: {old} -> {new}")
        return new

    def discard(self, n: int, reason: str | None) -> dict[str, Any]:
        if not reason or not reason.strip():
            raise BatchError("--discard needs --reason: a discarded sample is a recorded decision")
        self.require_no_driver("discarding a run")
        for record in self.runs:
            if int(record["n"]) == n:
                if record.get("state") != "done":
                    raise BatchError(f"run {n:02d} is {record.get('state')}, not finished")
                self.runs.remove(record)
                entry = dict(record, status="discarded", reason=reason.strip(),
                             discarded_utc=_iso(_utc_now()))
                self.discarded.append(entry)
                self.save()
                self.event(f"RUN {n:02d}: DISCARDED by hand ({reason.strip()}); "
                           f"folder {record.get('folder')} kept, slot runs again")
                return entry
        raise BatchError(f"run {n:02d} is not a finished run of this batch")

    def request_stop(self) -> int | None:
        pid = self.live_driver_pid()
        if pid is None:
            try:
                self.stop_path.unlink()
            except OSError:
                pass
            return None
        self.stop_path.write_text(f"stop requested {_iso(_utc_now())}\n", encoding="utf-8")
        self.event(f"STOP requested for driver pid {pid}: it stops after the current run")
        return pid


# ── reading what a run left behind ─────────────────────────────────────────────

def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _rel(path: Path | str | None, base: Path) -> str | None:
    if path is None:
        return None
    p = Path(path)
    try:
        return p.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(p)


def _short(text: Any, limit: int = 300) -> str | None:
    if text is None:
        return None
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def harvest_create(run_dir: Path, batch_dir: Path, outcome: ChildOutcome,
                   expected_text_sha256: str) -> dict[str, Any]:
    """What unity-create left in `run_dir`: its run log, failed-run report, project."""
    result: dict[str, Any] = {
        "exit_code": outcome.exit_code,
        "stalled": outcome.stalled,
        "status": None, "last_step": None, "failed_phase": None,
        "category": None, "message": None, "project": None,
        "playability": None, "playability_line": None,
        "run_log": None, "failed_run_report": None,
        "commit": None, "dirty": None, "brief_matches_snapshot": None,
    }
    manifests = sorted((run_dir / "_run_logs").glob("unity-create-*.json"))
    manifest = _read_json(manifests[-1]) if manifests else None
    if manifest is not None:
        result["run_log"] = _rel(manifests[-1], batch_dir)
        result["status"] = manifest.get("status")
        step = manifest.get("last_step") or {}
        if isinstance(step, dict) and step.get("number") is not None:
            result["last_step"] = f"{step.get('number')} {step.get('name') or ''}".strip()
        result["project"] = manifest.get("project_path")
        line = manifest.get("playability_summary")
        result["playability_line"] = line
        match = _PLAYABILITY_SCORE_RE.search(line or "")
        result["playability"] = int(match.group(1)) if match else None
        lineage = manifest.get("source_lineage") or {}
        result["commit"] = lineage.get("head_commit")
        result["dirty"] = lineage.get("dirty")
        raw = manifest.get("raw_input") or {}
        if raw.get("sha256"):
            result["brief_matches_snapshot"] = raw.get("sha256") == expected_text_sha256
        failure = manifest.get("failure") or {}
        if failure:
            result["message"] = _short(failure.get("message"))
        report_path = manifest.get("failed_run_report")
        if report_path:
            result["failed_run_report"] = _rel(report_path, batch_dir)
            report = _read_json(Path(report_path))
            if report is not None:
                rf = report.get("failure") or {}
                result["category"] = rf.get("category")
                result["message"] = _short(rf.get("message")) or result["message"]
                phase = report.get("phase") or {}
                result["failed_phase"] = phase.get("failed_phase")
    else:
        text = _tail(run_dir / CREATE_LOG_NAME)
        steps = _STEP_RE.findall(text)
        if steps:
            result["last_step"] = f"{steps[-1][0]} {steps[-1][1]}"
        result["message"] = _short(text.strip().splitlines()[-1]) if text.strip() else None
    if outcome.stalled:
        result["category"] = "stalled"
    return result


def _tail(path: Path, limit: int = 200_000) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - limit))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def current_step(run_dir: Path) -> str | None:
    """The last `STEP n -- title` a running run printed."""
    steps = _STEP_RE.findall(_tail(run_dir / CREATE_LOG_NAME))
    return f"{steps[-1][0]} {steps[-1][1]}" if steps else None


def harvest_verify(run_dir: Path, batch_dir: Path, outcome: ChildOutcome) -> dict[str, Any]:
    """The e2e-verify verdict, then prune the Unity caches of a kept verify copy."""
    text = _tail(run_dir / VERIFY_LOG_NAME, limit=2_000_000)
    verdict = compile_ = playtest = None
    playability: int | None = None
    matches = list(_VERIFY_VERDICT_RE.finditer(text))
    if matches:
        last = matches[-1]
        verdict, compile_ = last.group(1), last.group(2)
        playability = int(last.group(3))
        playtest = last.group(4)
    if _PLAYTEST_SKIPPED_RE.search(text):
        playtest = "skipped"
    kept = None
    pruned_mb = 0.0
    kept_match = list(_KEPT_COPY_RE.finditer(text))
    if kept_match:
        kept_path = Path(kept_match[-1].group(1)).resolve()
        kept = _rel(kept_path, batch_dir)
        pruned_mb = prune_verify_copy(kept_path, run_dir)
    return {
        "exit_code": outcome.exit_code, "stalled": outcome.stalled,
        "verdict": verdict, "compile": compile_, "playability": playability,
        "playtest": playtest, "log": _rel(run_dir / VERIFY_LOG_NAME, batch_dir),
        "kept_copy": kept, "pruned_mb": round(pruned_mb, 1),
    }


def _folder_mb(path: Path) -> float:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total / 1048576


def prune_verify_copy(copy_dir: Path, run_dir: Path) -> float:
    """Delete the Editor caches of a kept verify copy. Only ever inside `run_dir`."""
    try:
        copy_dir.resolve().relative_to(run_dir.resolve())
    except ValueError:
        return 0.0
    freed = 0.0
    for name in PRUNED_VERIFY_DIRS:
        target = copy_dir / name
        if target.is_dir():
            freed += _folder_mb(target)
            shutil.rmtree(target, ignore_errors=True)
    return freed


def verdict_for(record: dict[str, Any], verify_enabled: bool) -> str:
    """PASS only when the run compiled in the real Editor (CLAUDE.md's delivery gate)."""
    create = record.get("create") or {}
    if create.get("stalled"):
        return "STALLED"
    if create.get("exit_code") != 0:
        return "FAIL"
    if not verify_enabled:
        return "UNVERIFIED"
    verify = record.get("verify") or {}
    if verify.get("stalled"):
        return "STALLED"
    return "PASS" if verify.get("verdict") == "PASS" else "FAIL"


# ── preflight ──────────────────────────────────────────────────────────────────

def default_preflight(config: dict[str, Any], create_args: list[str],
                      folder: Path, min_free_gb: float) -> str | None:
    """Why a run must not start now, or None. Checks disk, then the LLM backend."""
    probe = folder
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free_gb = shutil.disk_usage(probe).free / 1024 ** 3
    except OSError as exc:
        return f"cannot read free space for {folder}: {exc}"
    if free_gb < min_free_gb:
        return (f"only {free_gb:.1f} GB free on the drive holding {folder} "
                f"(floor {min_free_gb:g} GB, --min-free-gb). Free space, then resume.")
    lm = config.get("lm_studio") or {}
    if str(lm.get("backend") or "lm_studio").lower() != "lm_studio":
        return None
    import httpx

    base = str(lm.get("base_url") or "http://localhost:1234/v1").rstrip("/")
    model = _option_value(create_args, "--model", "-m") or lm.get("model")
    try:
        response = httpx.get(f"{base}/models", timeout=10)
        response.raise_for_status()
        ids = {str(m.get("id")) for m in (response.json().get("data") or [])}
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        return (f"LM Studio is not answering at {base}/models ({type(exc).__name__}: {exc}). "
                "Start the LM Studio server, then resume.")
    if model and str(model) not in ids:
        return (f"LM Studio at {base} does not list model {model!r}. Download or rename "
                "it, or fix lm_studio.model, then resume.")
    return None


# ── the driver ─────────────────────────────────────────────────────────────────

CommandFactory = Callable[..., list[str]]


def default_create_command(*, python: str, repo_root: Path, brief: Path, config: Path,
                           create_args: list[str]) -> list[str]:
    return [python, "-u", str(repo_root / "main.py"), "unity-create",
            "--file", str(brief), "--config", str(config), *create_args]


def default_verify_command(*, python: str, repo_root: Path, project: Path,
                           config: Path) -> list[str]:
    return [python, "-u", str(repo_root / "unity_cli.py"), "e2e-verify", str(project),
            "--playtest", "--timeout", str(VERIFY_TIMEOUT_SECONDS), "--config", str(config)]


@dataclass
class DriveOptions:
    max_runs: int | None = None
    hours: float | None = None
    stall_minutes: float = DEFAULT_STALL_MINUTES
    min_free_gb: float = DEFAULT_MIN_FREE_GB
    follow: bool = False


@dataclass
class DriveResult:
    ran: int
    reason: str
    exit_code: int


class BatchInterrupted(KeyboardInterrupt):
    """Ctrl-C reached the driver; the current attempt was recorded and its child killed."""


class BatchDriver:
    """Runs a batch's pending slots one at a time until a stop condition."""

    def __init__(
        self,
        batch: Batch,
        *,
        repo_root: Path,
        echo: Echo = print,
        python: str = sys.executable,
        preflight: Callable[[dict[str, Any], list[str], Path, float], Optional[str]] = default_preflight,
        create_command: CommandFactory = default_create_command,
        verify_command: CommandFactory = default_verify_command,
        child_env: dict[str, str] | None = None,
        poll_seconds: float = 1.0,
    ) -> None:
        self.batch = batch
        self.repo_root = repo_root
        self.echo = echo
        self.python = python
        self.preflight = preflight
        self.create_command = create_command
        self.verify_command = verify_command
        self.child_env = child_env
        self.poll_seconds = poll_seconds

    # -- public --
    def drive(self, options: DriveOptions) -> DriveResult:
        batch = self.batch
        batch.acquire()
        try:
            self._recover()
            if batch.stop_path.exists():
                batch.stop_path.unlink()
                batch.event("STOP request left by an earlier driver cleared", self.echo)
            deadline = (time.monotonic() + options.hours * 3600) if options.hours else None
            ran = 0
            batch.event(
                f"DRIVER START (pid {os.getpid()}): {len(batch.finished())}/{batch.target} "
                f"run(s) finished", self.echo)
            # A run whose create finished but whose verify did not: verify only.
            for record in [r for r in batch.runs if r.get("state") == "verifying"]:
                self._verify(record, options)
            while True:
                slot = batch.next_slot()
                if slot is None:
                    return self._end(ran, f"target reached ({batch.target} run(s))", 0)
                if options.max_runs is not None and ran >= options.max_runs:
                    return self._end(ran, f"--max {options.max_runs} reached", 0)
                if deadline is not None and time.monotonic() >= deadline:
                    return self._end(ran, f"--hours {options.hours:g} reached", 0)
                if batch.stop_path.exists():
                    batch.stop_path.unlink()
                    return self._end(ran, "stopped on request (--stop)", 0)
                config = _load_yaml(batch.config_path)
                problem = self.preflight(config, batch.create_args, batch.runs_dir,
                                         options.min_free_gb)
                if problem:
                    return self._end(ran, f"preflight: {problem}", 2)
                status = self._run(slot, config, options)
                ran += 1 if status != "env" else 0
                if status == "env":
                    return self._end(ran, "the LLM backend failed mid-run (attempt "
                                     "discarded, slot kept): check LM Studio, then resume", 3)
        finally:
            batch.release()

    # -- internals --
    def _end(self, ran: int, reason: str, code: int) -> DriveResult:
        done = len(self.batch.finished())
        self.batch.event(f"DRIVER STOP: {reason}; {ran} run(s) this session, "
                         f"{done}/{self.batch.target} finished", self.echo)
        return DriveResult(ran=ran, reason=reason, exit_code=code)

    def _recover(self) -> None:
        """Turn records a dead driver left `running` into discarded attempts."""
        batch = self.batch
        for record in list(batch.runs):
            if record.get("state") != "running":
                continue
            child = record.get("child_pid")
            if python_alive(child):
                raise BatchError(
                    f"run {int(record['n']):02d} was started by a driver that is gone, "
                    f"but its unity-create (pid {child}) is still running. Let it finish "
                    f"or kill it (taskkill /PID {child} /T /F), then resume."
                )
            batch.runs.remove(record)
            batch.discarded.append(dict(record, status="interrupted",
                                        reason="its driver ended before the run did",
                                        discarded_utc=_iso(_utc_now())))
            batch.save()
            batch.event(f"RUN {int(record['n']):02d}: attempt {record.get('attempt')} "
                        f"was cut off when its driver ended; discarded (folder "
                        f"{record.get('folder')} kept), slot runs again", self.echo)

    def _env(self) -> dict[str, str]:
        env = dict(os.environ if self.child_env is None else self.child_env)
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                    # Wide enough that rich never wraps a path the run log parses.
                    "COLUMNS": "200"})
        return env

    def _line_echo(self, prefix: str, follow: bool) -> Echo:
        def emit(line: str) -> None:
            if follow or _MILESTONE_RE.search(line):
                self.echo(f"{prefix} {line.rstrip()}")
        return emit

    def _run(self, n: int, config: dict[str, Any], options: DriveOptions) -> str:
        batch = self.batch
        folder, attempt = batch.new_run_dir(n)
        folder.mkdir(parents=True)
        run_config = folder / RUN_CONFIG_NAME
        run_config.write_text(
            yaml.safe_dump(derive_run_config(config, folder), sort_keys=False, allow_unicode=True),
            encoding="utf-8")
        config_sha = batch.config_sha256()
        if config_sha != batch.data["config"]["sha256_at_create"]:
            batch.event(f"RUN {n:02d}: NOTE the batch config {batch.config_path} changed "
                        f"since the batch was created (sha256 {config_sha[:12]})", self.echo)
        started = _utc_now()
        record: dict[str, Any] = {
            "n": n, "attempt": attempt, "state": "running",
            "folder": _rel(folder, batch.path), "started_utc": _iso(started),
            "finished_utc": None, "minutes": None,
            "driver_pid": os.getpid(), "child_pid": None,
            "config_sha256": config_sha, "verdict": None, "create": None, "verify": None,
        }
        batch.runs.append(record)
        batch.runs.sort(key=lambda r: int(r["n"]))
        batch.save()
        batch.event(f"RUN {n:02d}: unity-create starting (attempt {attempt}, folder "
                    f"{record['folder']})", self.echo)

        def started_child(pid: int) -> None:
            record["child_pid"] = pid
            batch.save()

        cmd = self.create_command(python=self.python, repo_root=self.repo_root,
                                  brief=batch.brief_path, config=run_config,
                                  create_args=batch.create_args)
        clock = time.monotonic()
        try:
            outcome = run_child(cmd, cwd=self.repo_root, log_path=folder / CREATE_LOG_NAME,
                                stall_seconds=options.stall_minutes * 60 or None,
                                env=self._env(), on_line=self._line_echo(f"  [{n:02d}]", options.follow),
                                on_start=started_child, poll_seconds=self.poll_seconds)
        except KeyboardInterrupt as exc:
            self._interrupted(record, "Ctrl-C during unity-create")
            raise BatchInterrupted() from exc
        record["create"] = harvest_create(folder, batch.path, outcome,
                                          batch.data["brief"]["text_sha256"])
        record["create"]["minutes"] = round((time.monotonic() - clock) / 60, 1)
        record["child_pid"] = None
        create = record["create"]
        if create.get("category") in ENV_CATEGORIES:
            batch.runs.remove(record)
            batch.discarded.append(dict(record, status="env",
                                        reason=f"LLM backend failure ({create.get('category')})",
                                        finished_utc=_iso(_utc_now()),
                                        discarded_utc=_iso(_utc_now())))
            batch.save()
            batch.event(f"RUN {n:02d}: ENV failure ({create.get('category')}: "
                        f"{create.get('message')}); attempt discarded, slot kept", self.echo)
            return "env"
        if create.get("stalled"):
            batch.event(f"RUN {n:02d}: STALL-KILLED after {options.stall_minutes:g} min "
                        "without output", self.echo)
        record["state"] = "verifying" if self._should_verify(record) else "done"
        batch.save()
        if record["state"] == "verifying":
            self._verify(record, options)
        else:
            self._finish(record)
        return str(record.get("verdict"))

    def _should_verify(self, record: dict[str, Any]) -> bool:
        create = record.get("create") or {}
        return bool(self.batch.verify and not create.get("stalled") and create.get("project")
                    and Path(str(create["project"])).is_dir())

    def _verify(self, record: dict[str, Any], options: DriveOptions) -> None:
        batch = self.batch
        n = int(record["n"])
        folder = batch.path / str(record["folder"])
        project = Path(str((record.get("create") or {}).get("project")))
        if not project.is_dir():
            record["verify"] = {"skipped": f"project folder {project} is gone"}
            self._finish(record)
            return
        verify_log = folder / VERIFY_LOG_NAME
        if verify_log.exists():
            verify_log.rename(verify_log.with_name(f"verify.{int(time.time())}.log"))
        batch.event(f"RUN {n:02d}: e2e-verify --playtest on {project}", self.echo)
        cmd = self.verify_command(python=self.python, repo_root=self.repo_root,
                                  project=project, config=folder / RUN_CONFIG_NAME)
        clock = time.monotonic()
        try:
            outcome = run_child(cmd, cwd=self.repo_root, log_path=verify_log,
                                stall_seconds=options.stall_minutes * 60 or None,
                                env=self._env(), on_line=self._line_echo(f"  [{n:02d}]", options.follow),
                                poll_seconds=self.poll_seconds)
        except KeyboardInterrupt as exc:
            batch.event(f"RUN {n:02d}: Ctrl-C during e2e-verify; the create result is "
                        "kept and verify runs again on resume", self.echo)
            batch.save()
            raise BatchInterrupted() from exc
        record["verify"] = harvest_verify(folder, batch.path, outcome)
        record["verify"]["minutes"] = round((time.monotonic() - clock) / 60, 1)
        self._finish(record)

    def _finish(self, record: dict[str, Any]) -> None:
        batch = self.batch
        record["state"] = "done"
        record["finished_utc"] = _iso(_utc_now())
        # Active time, create plus verify: a verify resumed the next day must
        # not count the night in between.
        spent = [float(part["minutes"]) for part in (record.get("create"), record.get("verify"))
                 if isinstance(part, dict) and part.get("minutes") is not None]
        record["minutes"] = round(sum(spent), 1) if spent else None
        record["verdict"] = verdict_for(record, batch.verify)
        batch.save()
        create = record.get("create") or {}
        verify = record.get("verify") or {}
        bits = [f"create exit={create.get('exit_code')}"]
        if create.get("playability") is not None:
            bits.append(f"playability={create['playability']}")
        if verify.get("compile"):
            bits.append(f"compile={verify['compile']}")
        if verify.get("playtest"):
            bits.append(f"playtest={verify['playtest']}")
        if create.get("category"):
            bits.append(f"{create['category']}: {_short(create.get('message'), 120)}")
        batch.event(f"RUN {int(record['n']):02d}: {record['verdict']} in {record['minutes']} min "
                    f"({', '.join(bits)})", self.echo)

    def _interrupted(self, record: dict[str, Any], reason: str) -> None:
        batch = self.batch
        if record in batch.runs:
            batch.runs.remove(record)
        batch.discarded.append(dict(record, status="interrupted", reason=reason,
                                    child_pid=None, finished_utc=_iso(_utc_now()),
                                    discarded_utc=_iso(_utc_now())))
        batch.save()
        batch.event(f"RUN {int(record['n']):02d}: INTERRUPTED ({reason}); attempt discarded "
                    f"(folder {record.get('folder')} kept), slot runs again", self.echo)


# ── reading a batch back ───────────────────────────────────────────────────────

def summarize(batch: Batch) -> dict[str, Any]:
    """Counts and medians over the finished samples (discarded attempts excluded)."""
    finished = batch.finished()
    verdicts: dict[str, int] = {}
    categories: dict[str, int] = {}
    for record in finished:
        verdict = str(record.get("verdict"))
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
        category = (record.get("create") or {}).get("category")
        if category:
            categories[str(category)] = categories.get(str(category), 0) + 1
    minutes = [float(r["minutes"]) for r in finished if r.get("minutes") is not None]
    scores = [int((r.get("create") or {})["playability"]) for r in finished
              if (r.get("create") or {}).get("playability") is not None]
    commits = sorted({str((r.get("create") or {}).get("commit"))[:9] for r in finished
                      if (r.get("create") or {}).get("commit")})
    configs = sorted({str(r.get("config_sha256"))[:12] for r in finished if r.get("config_sha256")})
    running = [r for r in batch.runs if r.get("state") in ("running", "verifying")]
    return {
        "name": batch.name, "target": batch.target, "finished": len(finished),
        "verdicts": verdicts, "failure_categories": categories,
        "median_minutes": round(statistics.median(minutes), 1) if minutes else None,
        "median_playability": statistics.median(scores) if scores else None,
        "discarded": len(batch.discarded), "commits": commits, "config_sha256s": configs,
        "driver_pid": batch.live_driver_pid(),
        "running": [{"n": r["n"], "state": r.get("state"),
                     "step": current_step(batch.path / str(r["folder"]))} for r in running],
    }


def status_rows(batch: Batch) -> list[dict[str, Any]]:
    """One row per sample, in run order, for the status table."""
    rows = []
    for record in sorted(batch.runs, key=lambda r: int(r["n"])):
        create = record.get("create") or {}
        verify = record.get("verify") or {}
        state = record.get("state")
        if state == "done":
            reached = create.get("failed_phase") or create.get("last_step")
        else:
            reached = current_step(batch.path / str(record["folder"]))
        failure = None
        if create.get("category") or create.get("message"):
            failure = f"{create.get('category') or 'error'}: {_short(create.get('message'), 90)}"
        rows.append({
            "n": int(record["n"]), "attempt": record.get("attempt"),
            "verdict": record.get("verdict") or str(state).upper(),
            "minutes": record.get("minutes"), "started": record.get("started_utc"),
            "reached": reached, "playability": create.get("playability"),
            "compile": verify.get("compile"), "playtest": verify.get("playtest"),
            "failure": failure, "folder": record.get("folder"),
            "commit": (str(create.get("commit"))[:9] if create.get("commit") else None),
        })
    return rows
