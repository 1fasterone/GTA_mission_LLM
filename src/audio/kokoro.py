"""
Kokoro-82M TTS backend adapter — the small-VRAM speech synth for NPC dialog.

  pip install kokoro soundfile

Why Kokoro: Higgs Audio v2 is ~11 GB of weights and cannot load on an 8 GB
GPU; Kokoro-82M (hexgrad/Kokoro-82M, Apache-2.0) is ~330 MB, StyleTTS2-based,
runs comfortably on CPU or any GPU, and is the current quality leader in the
sub-1GB class. English G2P ships pure-Python (misaki), so no espeak-ng install
is needed for English.

Adapter interface matches HiggsAdapter exactly (src/audio/higgs.py):
  tts(text, voice, seed, language, emotion, style) -> Optional[np.ndarray]
    mono float32 @ synth.SR (44100 Hz), or None on any failure.

Backend selection: audio.tts_backend in config.yaml — kokoro (default) |
higgs | none. See make_tts_client() in higgs.py.

Graceful degradation (never raises, always returns None or audio):
  - kokoro not installed       → None (dialog remains text-only)
  - voice/lang pipeline error  → None + one-time warning
"""

from __future__ import annotations

import re
from typing import Optional

import numpy as np

from . import synth
from .higgs import _resample, emotion_params

# ── ISO 639-1 → Kokoro lang_code ──────────────────────────────────────────────
# Kokoro pipelines are per-language: 'a' American English, 'b' British English,
# 'e' Spanish, 'f' French, 'h' Hindi, 'i' Italian, 'j' Japanese, 'p' Brazilian
# Portuguese, 'z' Mandarin. Unsupported languages fall back to English rather
# than failing the line.
KOKORO_LANG: dict[str, str] = {
    "en": "a", "es": "e", "fr": "f", "hi": "h",
    "it": "i", "ja": "j", "pt": "p", "zh": "z",
}

# ── voice mapping ─────────────────────────────────────────────────────────────
# Kokoro voice ids are <lang><gender>_<name> (af_heart = American female).
# Schema voice_id values are free-form ("default", "male", "gruff_sergeant",
# "female_scientist"...) — map deterministically: gender keywords pick the
# pool, then a stable hash of the full voice string picks the member, so the
# same NPC always gets the same voice across runs.
_FEMALE_VOICES = ("af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky")
_MALE_VOICES   = ("am_adam", "am_michael", "am_eric", "am_liam", "am_onyx")
_FEMALE_HINTS  = ("female", "woman", "girl", "she", "lady", "queen", "mother")
_MALE_HINTS    = ("male", "man", "boy", "he", "guy", "king", "father",
                  "sergeant", "soldier")


def kokoro_voice(voice: str, language: str = "en") -> str:
    """Deterministic Kokoro voice id for a schema voice label.

    Gender hints match whole tokens, not substrings — "female_scientist"
    contains the substring "male" and must still land in the female pool.
    """
    v = str(voice or "default").strip().lower()
    tokens = set(re.split(r"[^a-z]+", v))
    pool = _FEMALE_VOICES  # default: af_heart is Kokoro's flagship voice
    if tokens & set(_FEMALE_HINTS):
        pool = _FEMALE_VOICES
    elif tokens & set(_MALE_HINTS):
        pool = _MALE_VOICES
    if v in ("", "default", "narrator"):
        return pool[0]
    # stable member pick — same label, same voice, every run
    idx = sum(ord(c) for c in v) % len(pool)
    return pool[idx]


class KokoroAdapter:
    """TTS adapter wrapping Kokoro-82M (lazy-imported; per-language pipelines)."""

    name = "kokoro"
    _SR  = synth.SR          # 44100 Hz target
    _KOKORO_SR = 24000       # Kokoro native output rate

    def __init__(self, config: dict):
        self.config = config or {}
        self.cfg    = self.config.get("audio", {})
        self._pipelines: dict[str, object] = {}   # lang_code -> KPipeline
        self._failed = False
        self._warned: set[str] = set()

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            print(f"[dialog] {msg}")

    def _pipeline(self, lang_code: str):
        if self._failed:
            return None
        if lang_code in self._pipelines:
            return self._pipelines[lang_code]
        try:
            from kokoro import KPipeline  # type: ignore[import]
            pipe = KPipeline(lang_code=lang_code,
                             repo_id=self.cfg.get("kokoro_model",
                                                  "hexgrad/Kokoro-82M"))
            self._pipelines[lang_code] = pipe
            return pipe
        except ImportError:
            self._failed = True
            self._warn_once("kokoro_import",
                "kokoro not installed (pip install kokoro soundfile) — "
                "dialog output will be text-only")
            return None
        except Exception as exc:  # noqa: BLE001
            self._failed = True
            self._warn_once("kokoro_load",
                f"Kokoro-82M failed to load ({type(exc).__name__}: {exc}) — text-only")
            return None

    # ── public interface (mirrors HiggsAdapter) ────────────────────────────────

    def tts(self, text: str, voice: str = "default",
            seed: int = 0, language: str = "en",
            emotion: str = "neutral", style: str = "natural") -> Optional[np.ndarray]:
        """Generate one NPC dialog line. Mono float32 @ 44.1 kHz, or None.

        Kokoro inference is deterministic, so `seed` only participates in the
        cache key. Emotion maps to speaking rate + a gain nudge (Kokoro has no
        native pitch/emotion control at this size — acceptable trade for
        11 GB → 330 MB).
        """
        if not (text or "").strip():
            return None
        lang_code = KOKORO_LANG.get(str(language).strip().lower())
        if lang_code is None:
            self._warn_once(f"lang_{language}",
                f"Kokoro has no '{language}' pipeline — using English")
            lang_code = "a"
        pipe = self._pipeline(lang_code)
        if pipe is None:
            return None

        prosody = emotion_params(emotion)
        try:
            chunks: list[np.ndarray] = []
            for _gs, _ps, audio in pipe(text,
                                        voice=kokoro_voice(voice, language),
                                        speed=prosody["speaking_rate"]):
                if hasattr(audio, "numpy"):
                    audio = audio.numpy()
                chunks.append(np.asarray(audio, dtype=np.float32))
            if not chunks:
                return None
            buf = np.concatenate(chunks)
            # emotion energy → gentle gain (clamped to avoid clipping)
            gain = float(np.clip(prosody.get("energy", 1.0), 0.5, 1.4))
            buf = np.clip(buf * gain, -1.0, 1.0)
            return _resample(buf, self._KOKORO_SR, self._SR)
        except Exception as exc:  # noqa: BLE001
            self._warn_once("kokoro_gen",
                f"Kokoro generation failed ({type(exc).__name__}: {exc}) — "
                "this line will be text-only")
            return None

    # Interface-compatibility stubs (dialog client is TTS-only).
    def sfx(self, prompt: str, dur: float, seed: int) -> Optional[np.ndarray]:
        return None

    def music(self, prompt: str, dur: float, seed: int) -> Optional[np.ndarray]:
        return None
