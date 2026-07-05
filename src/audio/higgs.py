"""
Higgs Audio v2 TTS backend adapter.

  pip install higgs-audio

Adapter interface matches the existing audio backends (src/audio/backends.py):
  tts(text, voice, seed, language, emotion, style) -> Optional[np.ndarray]
    mono float32 @ synth.SR (44100 Hz), or None on any failure.

Backend name: "higgs"  — set audio.backend: higgs in config.yaml.

Capabilities (Higgs Audio v2):
  - Multilingual TTS (50+ languages via ISO 639-1 codes)
  - Voice cloning / speaker control
  - Emotion-aware prosody (happy|sad|angry|fearful|neutral|...)
  - Audio style presets (natural|stylized|robotic|whisper|radio)

Graceful degradation (never raises, always returns None or audio):
  - higgs-audio not installed  → None (dialog remains text-only)
  - Model weights not found    → None
  - CUDA OOM                   → retry on CPU, then None
  - Any other inference error  → None + one-time warning
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from . import synth   # SR constant + write_wav helpers
from .lang import normalize_lang
# Emotion prosody is engine-agnostic and now lives in prosody.py; re-exported
# here so dialog_resolver + micro.npc_dialog keep importing it from .higgs.
from .prosody import SUPPORTED_EMOTIONS, emotion_params

# ── Audio style → Higgs preset map ───────────────────────────────────────────

AUDIO_STYLE_PRESETS: dict[str, dict] = {
    "natural":  {"style": "natural",    "filter": None},
    "stylized": {"style": "expressive", "filter": None},
    "robotic":  {"style": "robotic",    "filter": "bitcrusher"},
    "whisper":  {"style": "whisper",    "filter": None},
    "radio":    {"style": "natural",    "filter": "radio_eq"},
}

# ── ISO 639-1 → Higgs language tag ───────────────────────────────────────────

LANGUAGE_MAP: dict[str, str] = {
    "en": "en-US", "es": "es-ES", "fr": "fr-FR", "de": "de-DE",
    "ja": "ja-JP", "zh": "zh-CN", "pt": "pt-BR", "it": "it-IT",
    "ko": "ko-KR", "ru": "ru-RU", "ar": "ar-SA", "hi": "hi-IN",
    "nl": "nl-NL", "pl": "pl-PL", "sv": "sv-SE", "tr": "tr-TR",
    "cs": "cs-CZ", "da": "da-DK", "fi": "fi-FI", "nb": "nb-NO",
    "ro": "ro-RO", "uk": "uk-UA", "id": "id-ID", "vi": "vi-VN",
    "th": "th-TH",
}

SUPPORTED_STYLES    = set(AUDIO_STYLE_PRESETS)
SUPPORTED_LANGUAGES = set(LANGUAGE_MAP)


def audio_style_preset(style: str) -> dict:
    """Return Higgs style preset dict for an audio style label (falls back to natural)."""
    return AUDIO_STYLE_PRESETS.get(str(style).strip().lower(), AUDIO_STYLE_PRESETS["natural"])


def higgs_language(iso_code: str) -> str:
    """Convert ISO 639-1 code to a Higgs language tag (falls back to en-US)."""
    return LANGUAGE_MAP.get(normalize_lang(iso_code), "en-US")


# ── Adapter ────────────────────────────────────────────────────────────────────

class HiggsAdapter:
    """TTS adapter wrapping Higgs Audio v2.

    Lazy-imports `higgs_audio` so a missing package never breaks module load.
    All methods return Optional[np.ndarray] — mono float32 @ synth.SR.
    """

    name = "higgs"
    _SR  = synth.SR    # 44100 Hz

    def __init__(self, config: dict):
        self.config = config or {}
        self.cfg    = self.config.get("audio", {})
        self._model = None          # lazy-loaded on first tts() call
        self._warned: set[str] = set()

    # ── private helpers ────────────────────────────────────────────────────────

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            print(f"[dialog] {msg}")

    def _load_model(self):
        """Lazily load the Higgs Audio v2 model. Returns None on any failure."""
        if self._model is not None:
            return self._model
        try:
            import torch
            from higgs_audio import HiggsAudio  # type: ignore[import]
            model_id = self.cfg.get("higgs_model", "amphion/higgs-audio-v2")
            device   = "cuda" if torch.cuda.is_available() else "cpu"
            self._model = HiggsAudio.from_pretrained(model_id)
            self._model.to(device)
            return self._model
        except ImportError:
            self._warn_once("higgs_import",
                "higgs-audio not installed (pip install higgs-audio) — "
                "dialog output will be text-only")
            return None
        except RuntimeError as exc:
            # CUDA OOM — retry on CPU
            if "out of memory" in str(exc).lower():
                self._warn_once("higgs_oom",
                    f"Higgs Audio: CUDA OOM — retrying on CPU ({exc})")
                try:
                    import torch
                    from higgs_audio import HiggsAudio  # type: ignore[import]
                    model_id = self.cfg.get("higgs_model", "amphion/higgs-audio-v2")
                    self._model = HiggsAudio.from_pretrained(model_id)
                    self._model.to("cpu")
                    return self._model
                except Exception as exc2:  # noqa: BLE001
                    self._warn_once("higgs_cpu_fail",
                        f"Higgs Audio CPU fallback failed ({type(exc2).__name__}) — text-only")
                    return None
            self._warn_once("higgs_load",
                f"Higgs Audio v2 model failed to load ({type(exc).__name__}: {exc}) — text-only")
            return None
        except Exception as exc:  # noqa: BLE001
            self._warn_once("higgs_load",
                f"Higgs Audio v2 model failed to load ({type(exc).__name__}: {exc}) — text-only")
            return None

    # ── public interface ───────────────────────────────────────────────────────

    def tts(self, text: str, voice: str = "default",
            seed: int = 0, language: str = "en",
            emotion: str = "neutral", style: str = "natural") -> Optional[np.ndarray]:
        """Generate TTS audio for one NPC dialog line.

        Returns mono float32 at 44.1 kHz, or None if Higgs is unavailable.
        """
        model = self._load_model()
        if model is None:
            return None

        prosody  = emotion_params(emotion)
        preset   = audio_style_preset(style)
        lang_tag = higgs_language(language)
        voice_id = voice if voice not in ("", "default", None) else None

        try:
            import torch
            with torch.inference_mode():
                raw = model.generate(
                    text=text,
                    voice=voice_id,
                    language=lang_tag,
                    speaking_rate=prosody["speaking_rate"],
                    pitch_shift=prosody["pitch_shift"],
                    energy=prosody["energy"],
                    style=preset["style"],
                    seed=seed,
                )
            # normalise to mono float32 @ self._SR
            if hasattr(raw, "numpy"):
                raw = raw.numpy()
            audio = np.array(raw, dtype=np.float32)
            if audio.ndim == 2:
                audio = audio.mean(axis=0)          # stereo → mono
            model_sr = getattr(model, "sample_rate", self._SR)
            if model_sr != self._SR:
                audio = synth.resample(audio, model_sr, self._SR)
            return audio
        except Exception as exc:  # noqa: BLE001
            self._warn_once("higgs_gen",
                f"Higgs generation failed ({type(exc).__name__}: {exc}) — "
                "this line will be text-only")
            return None

    # Stubs for interface compatibility with generic audio backend callers.
    def sfx(self, prompt: str, dur: float, seed: int) -> Optional[np.ndarray]:
        return None

    def music(self, prompt: str, dur: float, seed: int) -> Optional[np.ndarray]:
        return None
