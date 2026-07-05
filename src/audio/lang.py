"""
Engine-agnostic ISO 639-1 language handling for NPC dialog.

Each TTS engine keeps its own target-vocabulary map — Higgs uses locale tags
(LANGUAGE_MAP in higgs.py: "en" -> "en-US"), Kokoro uses single-letter pipeline
codes (KOKORO_LANG in kokoro.py: "en" -> "a") — because they map to different
vocabularies and cannot be merged. What each of them (plus resolve_language()
and the dialog-bank builder) used to re-implement was the *normalisation* step.
That now lives here, so "strip / lower / bare 2-char ISO code" is defined once.
"""

from __future__ import annotations


def normalize_lang(code: str) -> str:
    """Normalise any language value to a bare 2-char lowercase ISO 639-1 code.

    Region/script suffixes are dropped ("pt-BR" -> "pt", "EN_US" -> "en") so an
    ISO-keyed engine map matches regardless of how the schema spelled it.
    """
    return str(code).strip().lower()[:2]
