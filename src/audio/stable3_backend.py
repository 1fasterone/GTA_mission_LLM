"""
Stable Audio 3 backend adapter.

Wraps G:/AI MODELS/stable3audio/main.py (StableAudioModel) and presents the
same sfx / music / tts interface as DspAdapter / LocalAdapter.

VRAM strategy for 8 GB cards
──────────────────────────────
  * LM Studio must be unloaded before this adapter is called (AudioPipeline
    handles the unload via unload_lm_studio() before write_files runs).
  * The sfx and music models are kept resident ACROSS consecutive same-type
    calls (a batch of 10 sfx cues loads the sfx model once, not 10 times —
    load from disk is ~3-13s vs. ~2s of actual diffusion work per cue, so
    reloading per-call was paying more in load time than in generation).
  * The two models are still NEVER both resident at the same time: switching
    from sfx to music (or back) unloads the other model first.
  * Caller MUST call unload_all() once the whole batch is done (content.py's
    generate_bank() does this) so VRAM is freed before the next pipeline stage.
  * tts returns None — content.py degrades to a silent placeholder.
  * Any import error, CUDA OOM, or generation failure degrades silently:
      sfx  → falls back to the DSP synth archetype (inherited from DspAdapter)
      music → returns None → content.py writes a silent placeholder
"""

from __future__ import annotations

import os
import sys
from typing import Optional

import numpy as np

from . import synth
from .backends import DspAdapter  # base for sfx DSP fallback

_DEFAULT_STABLE3_DIR = r"G:\AI MODELS\stable3audio"


class Stable3AudioAdapter(DspAdapter):
    name = "stable3"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = (config or {}).get("audio", {})
        self.stable3_dir = str(cfg.get("stable3_dir", _DEFAULT_STABLE3_DIR))
        self.steps       = int(cfg.get("steps", 8))
        self._warned: set[str] = set()
        # model_key ("sfx" | "music") -> loaded StableAudioModel. At most ONE
        # entry at a time — _ensure_loaded() evicts the other key before a load.
        self._loaded: dict[str, object] = {}

    # ── internal helpers ──────────────────────────────────────────────────────

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            print(f"[audio/stable3] {msg}")

    def _import_load_audio_model(self):
        """Import load_audio_model from stable3audio/main.py. Returns the
        function, or None if the dir/import is unavailable."""
        if not os.path.isdir(self.stable3_dir):
            self._warn_once(
                "dir",
                f"stable3_dir not found: {self.stable3_dir!r} — "
                "set audio.stable3_dir in config.yaml",
            )
            return None
        _inserted = self.stable3_dir not in sys.path
        if _inserted:
            sys.path.insert(0, self.stable3_dir)
        try:
            from main import load_audio_model  # type: ignore  # noqa: PLC0415
            return load_audio_model
        except ImportError as exc:
            self._warn_once("import", f"stable3audio import error ({exc})")
            return None
        finally:
            if _inserted and self.stable3_dir in sys.path:
                sys.path.remove(self.stable3_dir)

    def _ensure_loaded(self, model_key: str):
        """Return the resident model for model_key, loading it if needed.
        Evicts the OTHER model_key first — sfx and music never coexist in VRAM
        — but a repeat call for the SAME key (the common case: N cues of the
        same type in a batch) is a cache hit, no reload."""
        if model_key in self._loaded:
            return self._loaded[model_key]

        for other in list(self._loaded):
            if other != model_key:
                self._unload(other)

        load_fn = self._import_load_audio_model()
        if load_fn is None:
            return None
        try:
            model = load_fn(model_key)
        except Exception as exc:  # noqa: BLE001
            self._warn_once(f"load_{model_key}", f"model load failed ({type(exc).__name__}: {exc})")
            return None
        self._loaded[model_key] = model
        return model

    def _unload(self, model_key: str) -> None:
        model = self._loaded.pop(model_key, None)
        if model is None:
            return
        del model
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    def unload_all(self) -> None:
        """Free every resident model. Callers MUST invoke this once a whole
        batch of sfx/music cues is done (content.py's generate_bank does)."""
        for key in list(self._loaded):
            self._unload(key)

    def _generate(self, model_key: str, prompt: str,
                  dur: float, seed: int) -> Optional[np.ndarray]:
        """Generate one cue against the resident model for model_key (loading
        it — and evicting the other model_key — only on the first call of a
        run of same-type cues). Caller must eventually call unload_all()."""
        try:
            import torch
        except ImportError:
            self._warn_once("torch", "torch not available")
            return None

        model = self._ensure_loaded(model_key)
        if model is None:
            return None

        try:
            if seed:
                torch.manual_seed(seed & 0x7FFFFFFF)
                if torch.cuda.is_available():
                    torch.cuda.manual_seed(seed & 0x7FFFFFFF)

            # StableAudioModel.generate returns a tensor (batch, channels, samples)
            audio_tensor = model.generate(
                prompt=prompt,
                duration=float(max(0.1, dur)),
                steps=self.steps,
            )
            # Take first batch item → (channels, samples); average to mono
            audio_np = audio_tensor[0].cpu().float().numpy()  # (channels, samples)
            mono = audio_np.mean(axis=0).astype(np.float32)   # (samples,)

            # Resample to synth.SR (44100) if the model runs at a different rate
            model_sr = getattr(getattr(model, "model", None), "sample_rate", synth.SR)
            if model_sr and model_sr != synth.SR:
                n_out = int(len(mono) * synth.SR / model_sr)
                mono = np.interp(
                    np.linspace(0, len(mono) - 1, n_out),
                    np.arange(len(mono)),
                    mono,
                ).astype(np.float32)

            # Normalise to ~90 % of full scale. SA3 output amplitude varies
            # widely with prompt and step count; without this, low-amplitude
            # buffers produce near-silent / garbled 16-bit PCM.
            peak = float(np.abs(mono).max())
            if peak > 1e-4:
                mono = (mono / peak * 0.9).astype(np.float32)
            else:
                return None  # truly silent — let caller degrade to DSP

            return mono

        except Exception as exc:  # noqa: BLE001
            # Evict this model_key so the NEXT cue gets a fresh load instead of
            # reusing a possibly-poisoned instance (e.g. after a CUDA OOM).
            self._unload(model_key)
            msg = str(exc)
            if "out of memory" in msg.lower() or "OOM" in msg:
                self._warn_once(
                    "oom",
                    f"CUDA OOM generating {model_key!r} — "
                    "ensure LM Studio is unloaded before running the audio pipeline "
                    "(python main.py unity-audio ... automatically handles this)",
                )
            else:
                self._warn_once(
                    f"gen_{model_key}",
                    f"generation failed ({type(exc).__name__}: {exc})",
                )
            return None

    # ── public adapter interface ──────────────────────────────────────────────

    def sfx(self, prompt: str, dur: float, seed: int) -> Optional[np.ndarray]:
        # SA3 sfx model needs at least ~4 s to produce audible output;
        # sub-second requests result in near-silent buffers that fail normalisation.
        buf = self._generate("sfx", prompt, max(4.0, dur), seed)
        if buf is not None:
            return buf
        # DSP archetype fallback so a failed SFX cue is never silent
        return super().sfx(prompt, dur, seed)

    def music(self, prompt: str, dur: float, seed: int) -> Optional[np.ndarray]:
        # No DSP fallback for music; content.py writes a silent placeholder
        return self._generate("music", prompt, max(2.0, dur), seed)

    def tts(self, text: str, voice: str, seed: int) -> Optional[np.ndarray]:
        return None   # no TTS model; content.py degrades to silent placeholder
