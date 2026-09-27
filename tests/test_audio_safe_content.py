import json
from pathlib import Path

from src.audio import safe_content


def test_nonzero_stable3_worker_exit_degrades_without_ending_parent(
    tmp_path, monkeypatch
):
    resources = tmp_path / "AudioMicro" / "Resources"
    fallback_calls = []

    class FakeProcess:
        returncode = 3221225477

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pass

    def fake_popen(*_args, **kwargs):
        kwargs["stdout"].write(b"Loading weights: 100%\nnative worker ended\n")
        kwargs["stdout"].flush()
        return FakeProcess()

    def fake_generate(cues, resources_dir, config, seed=0):
        fallback_calls.append((cues, resources_dir, config, seed))
        return {
            "backend": "dsp",
            "cue_count": len(cues),
            "cues": [],
            "warnings": [],
        }

    monkeypatch.setattr(safe_content.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(safe_content, "generate_bank", fake_generate)

    manifest = safe_content.generate_bank_safe(
        [{"id": "robot_fire"}],
        resources,
        {"audio": {"backend": "stable3", "worker_timeout_s": 60}},
        seed=9,
    )

    assert fallback_calls[0][2]["audio"]["backend"] == "dsp"
    assert manifest["requested_backend"] == "stable3"
    assert manifest["fallback_backend"] == "dsp"
    assert "exit code 3221225477" in manifest["warnings"][0]
    assert (resources.parent / "stable3_worker.log").is_file()
    persisted = json.loads(
        (resources / "audio_manifest.json").read_text(encoding="utf-8")
    )
    assert persisted["fallback_backend"] == "dsp"


def test_successful_stable3_worker_manifest_is_returned(tmp_path, monkeypatch):
    resources = tmp_path / "AudioMicro" / "Resources"
    command_seen = []

    class FakeProcess:
        returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pass

    def fake_popen(command, **_kwargs):
        command_seen.extend(command)
        request = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        target = request["resources_dir"]
        manifest_path = Path(target) / "audio_manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps({
            "backend": "stable3",
            "cue_count": 1,
            "cues": [],
            "warnings": [],
        }), encoding="utf-8")
        return FakeProcess()

    monkeypatch.setattr(safe_content.subprocess, "Popen", fake_popen)

    manifest = safe_content.generate_bank_safe(
        [{"id": "robot_fire"}],
        resources,
        {"audio": {
            "backend": "stable3",
            "stable3_python": "C:/audio/python.exe",
            "worker_timeout_s": 60,
        }},
        seed=9,
    )

    assert manifest["backend"] == "stable3"
    assert manifest["worker"]["isolated"] is True
    assert manifest["worker"]["exit_code"] == 0
    assert manifest["worker"]["python"] == "C:/audio/python.exe"
    assert command_seen[0] == "C:/audio/python.exe"


def test_stalled_worker_is_killed_and_degrades(tmp_path, monkeypatch):
    resources = tmp_path / "AudioMicro" / "Resources"

    class FakeProcess:
        returncode = None
        killed = False

        def wait(self, timeout=None):
            if not self.killed:
                raise safe_content.subprocess.TimeoutExpired("worker", timeout)
            self.returncode = -9
            return self.returncode

        def kill(self):
            self.killed = True

    monkeypatch.setattr(
        safe_content.subprocess, "Popen", lambda *_args, **_kwargs: FakeProcess()
    )
    clock = iter([0.0, 31.0])
    monkeypatch.setattr(safe_content.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(
        safe_content,
        "generate_bank",
        lambda cues, *_args, **_kwargs: {
            "backend": "dsp",
            "cue_count": len(cues),
            "cues": [],
            "warnings": [],
        },
    )

    manifest = safe_content.generate_bank_safe(
        [{"id": "robot_fire"}],
        resources,
        {"audio": {
            "backend": "stable3",
            "worker_timeout_s": 300,
            "worker_stall_timeout_s": 30,
        }},
    )

    assert "made no log progress for 30s" in manifest["warnings"][0]
    assert manifest["fallback_backend"] == "dsp"
