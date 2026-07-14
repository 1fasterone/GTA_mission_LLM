"""
Tests for Stable3AudioAdapter's model-residency batching (src/audio/stable3_backend.py).

Verifies the fix for redundant per-cue reloads: the sfx/music model must stay
resident across consecutive same-type calls and only reload on a model-type
switch or after unload_all(). No torch, no real weights — load_audio_model is
faked via monkeypatch so this runs on any machine.
"""

import sys
import types

import pytest

from src.audio.stable3_backend import Stable3AudioAdapter


class _FakeModel:
    """Stands in for StableAudioModel: records how many buffers it produced."""
    def __init__(self, key):
        self.key = key
        self.calls = 0

    def generate(self, prompt, duration, steps):
        self.calls += 1
        import torch
        # A distinct non-silent tone per model so callers can assert identity.
        # Real .generate() returns a torch tensor (batch, channels, samples);
        # _generate() calls .cpu().float().numpy() on the result, so the fake
        # must satisfy that interface too, not a bare ndarray.
        n = int(44100 * max(0.1, duration))
        t = torch.linspace(0, 1, n)
        return (t + (0.01 if self.key == "sfx" else 0.02)).unsqueeze(0).unsqueeze(0)


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    load_calls = []

    def fake_load_audio_model(model_key):
        load_calls.append(model_key)
        return _FakeModel(model_key)

    fake_main = types.ModuleType("main")
    fake_main.load_audio_model = fake_load_audio_model
    monkeypatch.setitem(sys.modules, "main", fake_main)

    a = Stable3AudioAdapter({"audio": {"stable3_dir": str(tmp_path), "steps": 4}})
    monkeypatch.setattr("os.path.isdir", lambda p: True)  # tmp_path exists anyway; explicit for clarity
    a._load_calls = load_calls  # stash for assertions
    return a


def test_consecutive_same_type_calls_load_once(adapter):
    adapter.sfx("steam hiss", 4.0, 1)
    adapter.sfx("gear grind", 4.0, 2)
    adapter.sfx("pendulum swing", 4.0, 3)
    assert adapter._load_calls == ["sfx"]          # loaded exactly once for 3 cues
    assert adapter._loaded["sfx"].calls == 3        # but generated 3 times


def test_switching_model_type_evicts_the_other(adapter):
    adapter.sfx("steam hiss", 4.0, 1)
    adapter.music("mainspring drone", 8.0, 2)
    assert adapter._load_calls == ["sfx", "music"]
    assert "sfx" not in adapter._loaded             # evicted, never coexists with music
    assert "music" in adapter._loaded

    adapter.sfx("gear grind", 4.0, 3)                # switching back reloads sfx
    assert adapter._load_calls == ["sfx", "music", "sfx"]
    assert "music" not in adapter._loaded


def test_unload_all_clears_everything(adapter):
    adapter.sfx("steam hiss", 4.0, 1)
    assert adapter._loaded
    adapter.unload_all()
    assert adapter._loaded == {}

    adapter.sfx("steam hiss", 4.0, 1)               # reloads after explicit unload
    assert adapter._load_calls == ["sfx", "sfx"]


def test_generation_error_evicts_only_the_failing_model(adapter):
    good = adapter._ensure_loaded("sfx")
    assert good is not None

    def boom(*a, **kw):
        raise RuntimeError("CUDA out of memory")
    adapter._loaded["sfx"].generate = boom

    result = adapter._generate("sfx", "steam hiss", 4.0, 1)
    assert result is None
    assert "sfx" not in adapter._loaded              # poisoned instance evicted

    adapter.sfx("steam hiss", 4.0, 2)                # next call gets a fresh model
    assert adapter._load_calls == ["sfx", "sfx"]
