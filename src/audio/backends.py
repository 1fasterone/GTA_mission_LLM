"""
Audio generation backend selection (mirrors src/backends.py `make_chat_client`).

`make_audio_client(config)` returns an adapter exposing three methods, each producing a
mono float32 buffer @ 44.1 kHz or **None** (the caller then degrades):

    sfx(prompt, dur, seed)        — a one-shot sound effect
    music(prompt, dur, seed)      — a short music / ambient bed
    tts(text, voice, seed)        — a spoken voice line

Adapters (config `audio.backend`):
  * `dsp`   — DSP synth only. `sfx` routes the prompt to an archetype; `music`/`tts`
              return None (degrade to placeholder). Always available, deterministic.
  * `local` — open local models: Stable Audio Open for sfx/music, Bark/Piper for tts.
              Heavy deps are **lazy-imported**; if a model/dep is missing the method
              returns None (and sfx falls back to the DSP synth) — never a hard fail.
  * `none`  — everything returns None (synth-routed cues still synthesised directly).

Every successful generation is cached by content-hash under `audio.cache_dir`, so a
fixed seed reuses the same clip (skip-work / reproducibility, per the V1 spec).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

import numpy as np

from . import synth


# ── adapters ──────────────────────────────────────────────────────────────────

class _NoneAdapter:
    """Stub backend — no generative audio (synth cues are handled outside)."""
    name = "none"

    def __init__(self, config: dict):
        self.config = config or {}

    def sfx(self, prompt: str, dur: float, seed: int) -> Optional[np.ndarray]:
        return None

    def music(self, prompt: str, dur: float, seed: int) -> Optional[np.ndarray]:
        return None

    def tts(self, text: str, voice: str, seed: int) -> Optional[np.ndarray]:
        return None


class DspAdapter(_NoneAdapter):
    """DSP synth as the SFX generator; music/tts degrade (DSP can't do them)."""
    name = "dsp"

    def sfx(self, prompt: str, dur: float, seed: int) -> Optional[np.ndarray]:
        arch = synth.keyword_archetype(prompt) or "blip"
        return synth.render(synth.archetype_params(arch, prompt), seed=seed)


class LocalAdapter(DspAdapter):
    """Open local models. Stable Audio Open for sfx/music, Bark/Piper for tts. Each is
    lazy-imported; on any failure the method returns None (sfx → DSP fallback), so a
    machine without the models still produces a valid project."""
    name = "local"

    def __init__(self, config: dict):
        super().__init__(config)
        self.cfg = (config or {}).get("audio", {})
        self._warned: set[str] = set()

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            print(f"[audio] {msg} — falling back (set up the model to enable it).")

    def sfx(self, prompt: str, dur: float, seed: int) -> Optional[np.ndarray]:
        buf = self._stable_audio(prompt, dur, seed)
        if buf is not None:
            return buf
        return super().sfx(prompt, dur, seed)          # DSP fallback for synthesizable SFX

    def music(self, prompt: str, dur: float, seed: int) -> Optional[np.ndarray]:
        return self._stable_audio(prompt, max(2.0, dur), seed)

    def tts(self, text: str, voice: str, seed: int) -> Optional[np.ndarray]:
        try:
            return self._bark_tts(text, voice, seed)
        except Exception as exc:  # noqa: BLE001
            self._warn_once("tts", f"local TTS unavailable ({type(exc).__name__})")
            return None

    # -- lazy model hooks (import inside so a missing dep never breaks import) ---
    def _stable_audio(self, prompt: str, dur: float, seed: int) -> Optional[np.ndarray]:
        try:
            import torch  # noqa: F401
            from stable_audio_tools import get_pretrained_model  # type: ignore
            from stable_audio_tools.inference.generation import generate_diffusion_cond  # type: ignore
        except Exception as exc:  # noqa: BLE001
            self._warn_once("stable_audio", f"Stable Audio unavailable ({type(exc).__name__})")
            return None
        # Real inference is intentionally not run inside the deterministic test path;
        # this is the wiring point. Implementations call generate_diffusion_cond with
        # the model from get_pretrained_model(self.cfg["stable_audio"]["model_id"]),
        # then downmix to mono @ synth.SR. Kept guarded so CI never downloads weights.
        self._warn_once("stable_audio_run", "Stable Audio model wired but not invoked in this build")
        return None

    def _bark_tts(self, text: str, voice: str, seed: int) -> Optional[np.ndarray]:
        from bark import generate_audio, preload_models  # type: ignore  # noqa: F401
        self._warn_once("bark_run", "Bark model wired but not invoked in this build")
        return None


_ADAPTERS = {"none": _NoneAdapter, "dsp": DspAdapter, "local": LocalAdapter}
# "stable3" is registered lazily in make_audio_client to avoid importing torch at startup.


# ── LM Studio VRAM management ────────────────────────────────────────────────

def unload_lm_studio(config: dict) -> bool:
    """Unload all active LM Studio models to free VRAM for Stable Audio 3.

    Strategy (tried in order, first success wins):
      1. `lms unload <model_id>` CLI — the supported unload mechanism in LM Studio.
      2. REST GET /api/v0/models to discover what's loaded (used to build CLI args).

    Returns True on success or when nothing is loaded; False on failure (non-fatal).
    Graceful on Ollama (no lms CLI) and on network / subprocess errors.
    """
    import subprocess

    lm_cfg = (config or {}).get("lm_studio", {})
    v1_base = str(lm_cfg.get("base_url", "http://localhost:1234/v1")).rstrip("/")
    mgmt = v1_base.replace("/v1", "").rstrip("/") + "/api/v0"

    # Discover which models are loaded via the REST list endpoint.
    loaded_ids: list[str] = []
    try:
        import httpx
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{mgmt}/models")
            if r.status_code == 200:
                for m in r.json().get("data", []):
                    if m.get("state") == "loaded":
                        mid = m.get("id") or m.get("identifier") or ""
                        if mid:
                            loaded_ids.append(mid)
    except Exception:  # noqa: BLE001 — Ollama / network unavailable
        pass

    if not loaded_ids:
        return True  # nothing to unload

    # Unload each model via the LM Studio CLI (`lms unload <model_key>`).
    all_ok = True
    for mid in loaded_ids:
        try:
            result = subprocess.run(
                ["lms", "unload", mid],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                all_ok = False
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            # lms CLI not on PATH or timed out — non-fatal
            all_ok = False

    return all_ok


# ── caching wrapper ───────────────────────────────────────────────────────────

class CachingClient:
    """Content-addressed cache around an adapter: identical (backend, method, prompt,
    dur/voice, seed) reuses the same .wav, so generation is skip-work + reproducible."""

    def __init__(self, adapter, cache_dir: Path):
        self.adapter = adapter
        self.cache_dir = Path(cache_dir)
        self.name = adapter.name

    def _key(self, kind: str, *parts) -> Path:
        raw = "|".join([self.adapter.name, kind, *[str(p) for p in parts]])
        h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        return self.cache_dir / f"{kind}_{h}.wav"

    def _cached(self, path: Path, produce) -> Optional[np.ndarray]:
        if path.exists():
            try:
                return synth.read_wav(path)
            except Exception:  # noqa: BLE001 - corrupt cache entry -> regenerate
                pass
        buf = produce()
        if buf is not None and len(buf):
            synth.write_wav(path, buf)
            return synth.read_wav(path)        # return the artifact so hit == miss
        return buf

    def sfx(self, prompt: str, dur: float, seed: int) -> Optional[np.ndarray]:
        return self._cached(self._key("sfx", prompt, dur, seed),
                            lambda: self.adapter.sfx(prompt, dur, seed))

    def music(self, prompt: str, dur: float, seed: int) -> Optional[np.ndarray]:
        return self._cached(self._key("music", prompt, dur, seed),
                            lambda: self.adapter.music(prompt, dur, seed))

    def tts(self, text: str, voice: str, seed: int, **kw) -> Optional[np.ndarray]:
        # Dialog passes language/emotion/style beyond the base signature — they
        # must reach the adapter AND participate in the cache key, or a happy
        # line and an angry line of the same text collide on one .wav. (The
        # old 3-arg signature made every dialog call TypeError into the
        # text-only path — hit live wiring up NPC dialog TTS.)
        extras = [f"{k}={kw[k]}" for k in sorted(kw)]
        return self._cached(self._key("tts", text, voice, seed, *extras),
                            lambda: self.adapter.tts(text, voice, seed, **kw))


def make_audio_client(config: dict) -> CachingClient:
    """Select + build the audio backend from `config['audio']` (default: dsp).

    Backends:
      dsp     — offline sfxr DSP synth only (default, always available)
      local   — Stable Audio Open + Bark (legacy stub)
      stable3 — Stable Audio 3 via G:/AI MODELS/stable3audio (lazy-imported
                so torch is never loaded at module-import time)
      none    — everything returns None
    """
    cfg = (config or {}).get("audio", {})
    backend = str(cfg.get("backend", "dsp")).strip().lower()
    if backend == "stable3":
        from .stable3_backend import Stable3AudioAdapter  # lazy — avoids torch at startup
        adapter_cls = Stable3AudioAdapter
    else:
        adapter_cls = _ADAPTERS.get(backend, DspAdapter)
    cache_dir = Path(cfg.get("cache_dir", "output/audio_cache"))
    return CachingClient(adapter_cls(config or {}), cache_dir)


# ── NPC-dialog TTS backend (separate axis from sfx/music above) ───────────────

class _NoneTts:
    """audio.tts_backend: none — every line stays text-only."""
    name = "tts_none"
    def tts(self, *a, **kw):   return None   # noqa: E704
    def sfx(self, *a, **kw):   return None   # noqa: E704
    def music(self, *a, **kw): return None   # noqa: E704


def make_tts_client(config: dict) -> CachingClient:
    """Build the caching NPC-dialog TTS client from config.

    `audio.tts_backend` selects the engine (separate from `audio.backend`,
    which drives sfx/music — SA3 stays on the big GPU budget while speech
    uses a model that actually fits):
      kokoro — Kokoro-82M, ~330 MB, CPU/any-GPU (default; Higgs Audio v2's
               11 GB of weights cannot load on an 8 GB card — hit live)
      higgs  — Higgs Audio v2 (needs ~11 GB VRAM)
      none   — text-only dialog

    Engine adapters are lazy-imported so this module never drags torch (or the
    engines' cross-imports) in at import time.
    """
    cfg     = (config or {}).get("audio", {})
    backend = str(cfg.get("tts_backend", "kokoro")).strip().lower()
    if backend == "higgs":
        from .higgs import HiggsAdapter    # lazy — avoids torch at startup
        adapter = HiggsAdapter(config)
    elif backend == "none":
        adapter = _NoneTts()
    else:
        from .kokoro import KokoroAdapter  # lazy — keeps module import light
        adapter = KokoroAdapter(config)
    cache_dir = Path(cfg.get("dialog_cache_dir",
                             cfg.get("cache_dir", "output/audio_cache")))
    return CachingClient(adapter, cache_dir)


def make_higgs_client(config: dict) -> CachingClient:
    """Back-compat alias — the dialog pipeline now selects its TTS engine via
    audio.tts_backend (see make_tts_client)."""
    return make_tts_client(config)
