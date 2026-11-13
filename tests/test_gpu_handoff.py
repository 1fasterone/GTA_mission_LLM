"""LM Studio -> GPU-stage hand-off (src/gpu_handoff.py).

Regression for the 2026-11-10 cuphead_style_brawler run: ComfyUI began its
sprite batch while qwen3.5-9b was still resident in LM Studio. The fake HTTP
client below mirrors the response shapes captured from a live LM Studio
(`/api/v1/models` -> models[].loaded_instances[].id, `/api/v0/models` ->
data[].state).
"""
from __future__ import annotations

from typing import Any, Optional
from unittest.mock import patch

import pytest

from src import gpu_handoff
from src.gpu_handoff import evict_lm_studio_models, list_loaded_instances, native_root

CFG = {"lm_studio": {"backend": "lm_studio", "base_url": "http://localhost:1234/v1"}}


class _Resp:
    def __init__(self, status: int, body: Optional[dict] = None):
        self.status_code = status
        self._body = body or {}

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeLMStudio:
    """In-memory LM Studio: `loaded` is the set of resident instance ids."""

    def __init__(self, loaded: list[str], *, v1: bool = True, v0: bool = True,
                 unload_works: bool = True):
        self.loaded = list(loaded)
        self.v1, self.v0, self.unload_works = v1, v0, unload_works
        self.unload_calls: list[str] = []

    def get(self, url: str, headers: Any = None) -> _Resp:
        if url.endswith("/api/v1/models"):
            if not self.v1:
                return _Resp(404)
            return _Resp(200, {"models": [
                {"type": "llm", "key": iid, "loaded_instances": [{"id": iid}]}
                for iid in self.loaded
            ] + [{"type": "llm", "key": "idle-model", "loaded_instances": []}]})
        if url.endswith("/api/v0/models"):
            if not self.v0:
                return _Resp(404)
            return _Resp(200, {"data": [
                {"id": iid, "state": "loaded"} for iid in self.loaded
            ] + [{"id": "idle-model", "state": "not-loaded"}]})
        return _Resp(404)

    def post(self, url: str, json: Any = None, headers: Any = None) -> _Resp:
        assert url.endswith("/api/v1/models/unload")
        iid = json["instance_id"]
        self.unload_calls.append(iid)
        if not self.unload_works:
            return _Resp(500)
        if iid in self.loaded:
            self.loaded.remove(iid)
            return _Resp(200)
        return _Resp(404)


@pytest.fixture(autouse=True)
def _no_lms_cli(monkeypatch):
    # Tests must never shell out to a real `lms` on the developer's machine.
    monkeypatch.setattr(gpu_handoff.shutil, "which", lambda _name: None)


def test_native_root_strips_v1():
    assert native_root("http://localhost:1234/v1") == "http://localhost:1234"
    assert native_root("http://localhost:1234/v1/") == "http://localhost:1234"


def test_lists_only_loaded_instances_from_v1_shape():
    fake = FakeLMStudio(["qwen3.5-9b"])
    assert list_loaded_instances("http://x", client=fake) == ["qwen3.5-9b"]


def test_falls_back_to_v0_listing():
    fake = FakeLMStudio(["qwen3.5-9b"], v1=False)
    assert list_loaded_instances("http://x", client=fake) == ["qwen3.5-9b"]


def test_evicts_every_resident_model_not_only_the_configured_one():
    # The configured model is qwen3.5-9b; a model loaded by hand still takes VRAM.
    fake = FakeLMStudio(["qwen3.5-9b", "someone-elses-model"])
    result = evict_lm_studio_models(CFG, client=fake)
    assert result.attempted and result.listed and result.ok
    assert sorted(result.evicted) == ["qwen3.5-9b", "someone-elses-model"]
    assert fake.loaded == []


def test_nothing_loaded_is_ok_and_posts_nothing():
    fake = FakeLMStudio([])
    result = evict_lm_studio_models(CFG, client=fake)
    assert result.ok and result.evicted == [] and fake.unload_calls == []
    assert "no model loaded" in result.summary()


def test_model_that_stays_resident_is_reported_by_name():
    fake = FakeLMStudio(["qwen3.5-9b"], unload_works=False)
    result = evict_lm_studio_models(CFG, client=fake)
    assert not result.ok
    assert result.still_resident == ["qwen3.5-9b"]
    assert result.evicted == []
    assert "qwen3.5-9b" in result.summary()


def test_cli_fallback_when_native_unload_fails(monkeypatch):
    fake = FakeLMStudio(["qwen3.5-9b"], unload_works=False)
    calls: list[list[str]] = []

    def _cli(args: list[str]) -> bool:
        calls.append(args)
        fake.loaded.remove(args[0])
        return True

    monkeypatch.setattr(gpu_handoff, "_unload_cli", _cli)
    result = evict_lm_studio_models(CFG, client=fake)
    assert result.ok and result.evicted == ["qwen3.5-9b"]
    assert calls == [["qwen3.5-9b"]]


def test_unreachable_server_is_an_error_not_a_silent_success():
    fake = FakeLMStudio([], v1=False, v0=False)
    result = evict_lm_studio_models(CFG, client=fake)
    assert result.attempted and not result.listed and not result.ok
    assert "unreachable" in result.summary()


@pytest.mark.parametrize("cfg", [
    {"lm_studio": {"backend": "ollama"}},
    {"lm_studio": {"backend": "lm_studio", "evict_before_gpu_stages": False}},
])
def test_disabled_or_other_backend_never_touches_the_server(cfg):
    fake = FakeLMStudio(["qwen3.5-9b"])
    result = evict_lm_studio_models(cfg, client=fake)
    assert not result.attempted and result.ok
    assert fake.loaded == ["qwen3.5-9b"] and fake.unload_calls == []


def test_comfyui_backend_evicts_once_before_its_first_graph():
    from src.sprites.comfyui_backend import ComfyUIBackend

    order: list[str] = []
    backend = ComfyUIBackend(CFG)
    with patch("src.gpu_handoff.evict_lm_studio_models",
               side_effect=lambda _cfg: order.append("evict")
               or gpu_handoff.EvictionResult()) as evict, \
         patch.object(backend, "available", return_value=True), \
         patch.object(backend, "_post_json",
                      side_effect=lambda *_a, **_k: order.append("prompt")
                      or {"prompt_id": "p"}), \
         patch.object(backend, "_poll_history", return_value=None):
        backend._run_graph({})
        backend._run_graph({})
    assert evict.call_count == 1
    assert order == ["evict", "prompt", "prompt"]


def test_audio_unload_delegates_to_shared_eviction():
    from src.audio import backends

    with patch("src.gpu_handoff.evict_lm_studio_models",
               return_value=gpu_handoff.EvictionResult(
                   attempted=True, listed=True, still_resident=["m"])) as evict:
        assert backends.unload_lm_studio(CFG) is False
    evict.assert_called_once_with(CFG)


def test_released_client_skips_end_of_run_unload():
    from src.lm_studio_client import LMStudioClient

    client = LMStudioClient("http://localhost:1234/v1", "qwen3.5-9b")
    client.chat_calls = 1
    client.released = True
    with patch("src.lm_studio_client.httpx.Client") as http_client:
        assert client.unload() is True
        http_client.assert_not_called()


def test_freeze_boundary_evicts_and_marks_client_released():
    from src.llm_boundary import freeze
    from src.lm_studio_client import LMStudioClient
    from src.unity_pipeline import UnityPipeline

    client = LMStudioClient("http://localhost:1234/v1", "qwen3.5-9b")
    client.chat_calls = 3
    pipe = UnityPipeline.__new__(UnityPipeline)
    pipe.config = CFG
    pipe.llm = freeze(client)
    done = gpu_handoff.EvictionResult(attempted=True, listed=True,
                                      evicted=["qwen3.5-9b"])
    with patch("src.gpu_handoff.evict_lm_studio_models", return_value=done) as evict:
        pipe._evict_llm_for_gpu_stages()
    evict.assert_called_once_with(CFG)
    assert client.released is True


def test_freeze_boundary_leaves_client_unreleased_when_model_stays():
    from src.llm_boundary import freeze
    from src.lm_studio_client import LMStudioClient
    from src.unity_pipeline import UnityPipeline

    client = LMStudioClient("http://localhost:1234/v1", "qwen3.5-9b")
    pipe = UnityPipeline.__new__(UnityPipeline)
    pipe.config = CFG
    pipe.llm = freeze(client)
    stuck = gpu_handoff.EvictionResult(attempted=True, listed=True,
                                       still_resident=["qwen3.5-9b"])
    with patch("src.gpu_handoff.evict_lm_studio_models", return_value=stuck):
        pipe._evict_llm_for_gpu_stages()
    # End-of-run unload in UnityPipeline.run() still gets its own try.
    assert client.released is False


def test_comfyui_backend_retries_eviction_after_a_failed_attempt():
    """Review on #293: a failed eviction must not settle the hand-off, or the
    rest of the batch runs beside a still-resident model."""
    from src.sprites.comfyui_backend import ComfyUIBackend

    stuck = gpu_handoff.EvictionResult(attempted=True, listed=True,
                                       still_resident=["qwen3.5-9b"])
    done = gpu_handoff.EvictionResult(attempted=True, listed=True,
                                      evicted=["qwen3.5-9b"])
    backend = ComfyUIBackend(CFG)
    with patch("src.gpu_handoff.evict_lm_studio_models",
               side_effect=[stuck, done]) as evict, \
         patch.object(backend, "available", return_value=True), \
         patch.object(backend, "_post_json", return_value={"prompt_id": "p"}), \
         patch.object(backend, "_poll_history", return_value=None):
        backend._run_graph({})
        backend._run_graph({})
        backend._run_graph({})
    assert evict.call_count == 2
