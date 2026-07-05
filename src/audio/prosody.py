"""
Engine-agnostic prosody parameters for NPC dialog TTS.

Emotion → speaking-rate / pitch-shift / energy. Shared by every TTS adapter
(HiggsAdapter uses all three knobs; KokoroAdapter uses rate + energy), so the
table lives in this neutral module rather than inside any one engine — that is
what keeps higgs.py and kokoro.py from importing each other.
"""

from __future__ import annotations


# ── Emotion → prosody parameter map ──────────────────────────────────────────

EMOTION_PARAMS: dict[str, dict] = {
    "neutral":   {"speaking_rate": 1.00, "pitch_shift":  0.0, "energy": 1.00},
    "happy":     {"speaking_rate": 1.10, "pitch_shift":  1.5, "energy": 1.20},
    "sad":       {"speaking_rate": 0.85, "pitch_shift": -1.0, "energy": 0.75},
    "angry":     {"speaking_rate": 1.20, "pitch_shift":  0.5, "energy": 1.40},
    "fearful":   {"speaking_rate": 1.15, "pitch_shift":  1.0, "energy": 0.90},
    "surprised": {"speaking_rate": 1.10, "pitch_shift":  2.0, "energy": 1.10},
    "disgust":   {"speaking_rate": 0.95, "pitch_shift": -0.5, "energy": 0.95},
    "whisper":   {"speaking_rate": 0.90, "pitch_shift": -2.0, "energy": 0.50},
    "excited":   {"speaking_rate": 1.25, "pitch_shift":  2.5, "energy": 1.35},
    "calm":      {"speaking_rate": 0.90, "pitch_shift": -0.5, "energy": 0.85},
}

SUPPORTED_EMOTIONS = set(EMOTION_PARAMS)


def emotion_params(emotion: str) -> dict:
    """Return prosody parameters for an emotion label (falls back to neutral)."""
    return EMOTION_PARAMS.get(str(emotion).strip().lower(), EMOTION_PARAMS["neutral"])
