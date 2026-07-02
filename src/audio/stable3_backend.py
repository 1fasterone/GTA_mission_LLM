"""
Stable Audio 3 backend adapter.

Wraps G:/AI MODELS/stable3audio/main.py (StableAudioModel) and presents the
same sfx / music / tts interface as DspAdapter / LocalAdapter.

VRAM strategy for 8 GB cards
──────────────────────────────
  * LM Studio must be unloaded before this adapter is called (AudioPipeline
    handles the unload via unload_lm_studio() before write_files runs).
  * The sfx and music models are loaded per-call and immediately deleted after
    generation; torch.cuda.empty_cache() is called after every del so the next
    load starts with a clean slab.
  * The two models are NEVER both resident at the same time.
  * tts returns None — content.py degrades to a silent placeholder.
  * Any import error, CUDA OOM, or generation failure degrades silently:
      sfx  → falls back to the DSP synth archetype (inherited from DspAdapter)
      music → returns None → content.py writes a silent placeholder
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
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

    # ── internal helpers ──────────────────────────────────────────────────────

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            print(f"[audio/stable3] {msg}")

    def _load_model(self, model_key: str):
        """Import load_audio_model from stable3audio/main.py and call it.
        Returns the StableAudioModel or None on failure. Caller must del it."""
        if not os.path.isdir(self.stable3_dir):
            self._warn_once(
                "dir",
                f"stable3_dir not found: {self.stable3_dir!r} — "
                "set audio.stable3_dir in config.yaml",
            )
            return None

        # Prepend stable3audio directory so `import main` resolves correctly
        _inserted = self.stable3_dir not in sys.path
        if _inserted:
            sys.path.insert(0, self.stable3_dir)
        try:
            from main import load_audio_model  # type: ignore  # noqa: PLC0415
            return load_audio_model(model_key)
        except ImportError as exc:
            self._warn_once("import", f"stable3audio import error ({exc})")
            return None
        except Exception as exc:  # noqa: BLE001
            self._warn_once(f"load_{model_key}", f"model load failed ({type(exc).__name__}: {exc})")
            return None
        finally:
            if _inserted and self.stable3_dir in sys.path:
                sys.path.remove(self.stable3_dir)

    def _generate(self, model_key: str, prompt: str,
                  dur: float, seed: int) -> Optional[np.ndarray]:
        """Full generate cycle for one cue: load → infer → del → empty_cache."""
        try:
            import torch
        except ImportError:
            self._warn_once("torch", "torch not available")
            return None

        model = self._load_model(model_key)
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

        finally:
            try:
                del model
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass

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
