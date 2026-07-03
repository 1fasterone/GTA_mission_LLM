"""
Dialog content pass — generate WAV files from DialogLine specs using Higgs Audio v2.

Mirrors audio/content.py's generate_bank() but for NPC dialog:
  generate_dialog_bank(lines, resources_dir, config, seed) -> manifest dict

Output layout (under resources_dir):
  Audio/Dialog/<speaker_slug>/<line_id>_<lang>.wav
  Dialog/<speaker_slug>/dialog_manifest.json

Degrade-never-fail contract:
  - Higgs unavailable   → placeholder silent clip + manifest entry flagged text_only=True
  - Translation missing → English text used, language tag still written
  - Voice missing       → "default" voice used
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import numpy as np

from . import synth
from .higgs import make_tts_client


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).strip().lower()).strip("_") or "npc"


def _seed_for(line_id: str, lang: str, seed: int) -> int:
    h = hashlib.sha1(f"{line_id}|{lang}|{seed}".encode()).hexdigest()[:8]
    return int(h, 16)


def _silent_placeholder(duration_s: float = 0.5) -> np.ndarray:
    """Short silence buffer — used when Higgs is unavailable."""
    return np.zeros(int(synth.SR * duration_s), dtype=np.float32)


def generate_dialog_bank(
    lines: list[Any],
    speaker: str,
    resources_dir: str | Path,
    config: dict,
    multilingual_targets: list[str] | None = None,
    seed: int = 0,
) -> dict:
    """Generate WAV clips for every NPC dialog line + language variant.

    Args:
        lines:                List of DialogLine dicts or Pydantic objects.
        speaker:              Speaker name (used for directory naming).
        resources_dir:        Unity Resources folder root.
        config:               Project config dict.
        multilingual_targets: Extra ISO 639-1 codes beyond each line's `language`.
        seed:                 Global RNG seed for reproducibility.

    Returns:
        Manifest dict written as dialog_manifest.json alongside the clips.
    """
    resources_dir = Path(resources_dir)
    speaker_slug  = _slug(speaker)
    audio_dir     = resources_dir / "Audio" / "Dialog" / speaker_slug
    manifest_dir  = resources_dir / "Dialog" / speaker_slug
    audio_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir.mkdir(parents=True, exist_ok=True)

    client   = make_tts_client(config)
    warnings: list[str] = []
    entries:  list[dict] = []

    extra_langs = [str(c).strip().lower() for c in (multilingual_targets or [])]

    for line in lines:
        g = (lambda k, d: line.get(k, d)) if isinstance(line, dict) else \
            (lambda k, d: getattr(line, k, d))

        line_id  = str(g("line_id",  "line"))
        text     = str(g("text",     ""))
        emotion  = str(g("emotion",  "neutral"))
        language = str(g("language", "en")).strip().lower()
        voice_id = str(g("voice_id", "default"))
        style    = str(g("audio_style", "natural"))   # may be at line level or schema level

        # All languages this line needs: its own language + global extra targets
        all_langs = list({language, *extra_langs})

        clips: list[dict] = []
        for lang in all_langs:
            # Use original text for primary language; fallback to English text for others
            line_text = text if lang == language else text  # future: call translator
            clip_name = f"{line_id}_{lang}.wav"
            clip_path = audio_dir / clip_name
            cue_seed  = _seed_for(line_id, lang, seed)

            text_only = False
            try:
                buf = client.tts(
                    text=line_text,
                    voice=voice_id,
                    seed=cue_seed,
                    language=lang,
                    emotion=emotion,
                    style=style,
                )
                if buf is None or len(buf) == 0:
                    buf = _silent_placeholder()
                    text_only = True
                    warnings.append(
                        f"{line_id}[{lang}]: Higgs returned no audio — silent placeholder")
                synth.write_wav(clip_path, buf)
            except Exception as exc:  # noqa: BLE001 — content never breaks codegen
                synth.write_wav(clip_path, _silent_placeholder())
                text_only = True
                warnings.append(
                    f"{line_id}[{lang}]: clip generation failed "
                    f"({type(exc).__name__}: {exc}) — silent placeholder")

            clips.append({
                "language": lang,
                "path":     f"Audio/Dialog/{speaker_slug}/{clip_name}",
                "text_only": text_only,
            })

        entries.append({
            "line_id":  line_id,
            "text":     text,
            "emotion":  emotion,
            "voice_id": voice_id,
            "clips":    clips,
        })

    manifest = {
        "speaker":      speaker,
        "speaker_slug": speaker_slug,
        "line_count":   len(entries),
        "lines":        entries,
        "warnings":     warnings,
    }
    manifest_path = manifest_dir / "dialog_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                             encoding="utf-8")
    return manifest
