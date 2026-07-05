"""
Shared no-op stubs for TTS-only adapters.

Every dialog adapter (HiggsAdapter, KokoroAdapter, _NoneTts) implements the
generic three-method audio-backend interface (sfx/music/tts) but only ever
produces speech — sfx/music are always None. TtsOnly centralises that pair of
stubs so engines don't each carry their own copy.
"""

from __future__ import annotations

from typing import Optional

import numpy as np


class TtsOnly:
    """Mixin: sfx/music always return None. Subclasses provide `tts` + `name`."""

    def sfx(self, prompt: str, dur: float, seed: int) -> Optional[np.ndarray]:
        return None

    def music(self, prompt: str, dur: float, seed: int) -> Optional[np.ndarray]:
        return None
