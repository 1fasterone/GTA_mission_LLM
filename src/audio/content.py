"""
Audio content pass — turn a list of cue specs into actual clip files + a manifest.

This is the deterministic Step-6 generator for audio (the analog of the model micro's
FBX build). It routes each cue (resolver.classify), produces the audio (DSP synth for
synthesizable SFX, the generative backend for voice/music/organic), writes the clips
under `<resources>/Audio/<id>/<id>_<n>.wav`, and records an `audio_manifest.json` the
AudioBank/AudioManager read by `id`. Decoupled from the LLM so it's reused by both the
LLM schema path and the deterministic `--no-llm` baseline, and unit-testable directly.

Degrade-never-fail: a gen/tts cue whose backend returns nothing falls back to a DSP
approximation when synthesizable, else a short silent placeholder flagged with a TODO.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

import numpy as np

from . import synth
from .resolver import classify
from .backends import make_audio_client


def _seed_for(cue_id: str, seed: int) -> int:
    h = hashlib.sha1(f"{cue_id}|{seed}".encode("utf-8")).hexdigest()[:8]
    return int(h, 16)


def _norm_cue(c) -> dict:
    """Accept a dict or a pydantic-ish object; return a plain dict with defaults."""
    g = (lambda k, d: c.get(k, d)) if isinstance(c, dict) else (lambda k, d: getattr(c, k, d))
    return {
        "id":         str(g("id", "cue")),
        "category":   str(g("category", "sfx")),
        "kind":       str(g("kind", "auto")),
        "descriptor": str(g("descriptor", "")) or str(g("id", "cue")).replace("_", " "),
        "archetype":  str(g("archetype", "")),
        "gen_prompt": str(g("gen_prompt", "")),
        "text":       str(g("text", "")),
        "voice":      str(g("voice", "default")),
        "spatial":    bool(g("spatial", False)),
        "loop":       bool(g("loop", False)),
        "variations": int(g("variations", 1) or 1),
        "duration_s": float(g("duration_s", 0.5) or 0.5),
    }


def generate_bank(cues, resources_dir: str | Path, config: dict,
                  seed: int = 0) -> dict:
    """Produce every cue's clips under <resources_dir>/Audio/<id>/ and return a
    manifest dict (also written as audio_manifest.json). `resources_dir` is a Unity
    `Resources` folder (standalone micro out, or a project's Assets/Resources)."""
    resources_dir = Path(resources_dir)
    audio_root = resources_dir / "Audio"
    client = make_audio_client(config or {})

    entries: list[dict] = []
    warnings: list[str] = []
    for raw in cues:
        cue = _norm_cue(raw)
        cid = cue["id"]
        route = classify(cue["category"], cue["kind"], cue["descriptor"])
        cseed = _seed_for(cid, seed)
        bufs: list[np.ndarray] = []
        note = ""

        if route == "synth":
            arch = cue["archetype"] or synth.keyword_archetype(cue["descriptor"]) or "blip"
            params = synth.archetype_params(arch, cue["descriptor"])
            bufs = synth.variations(params, cue["variations"], cseed)
            note = f"synth:{arch}"
        elif route in ("gen", "library"):
            prompt = cue["gen_prompt"] or cue["descriptor"]
            if cue["category"] == "music":
                buf = client.music(prompt, cue["duration_s"], cseed)
            else:
                buf = client.sfx(prompt, cue["duration_s"], cseed)
            if buf is not None:
                bufs = [buf]; note = f"{route}:{client.name}"
        elif route == "tts":
            buf = client.tts(cue["text"] or cue["descriptor"], cue["voice"], cseed)
            if buf is not None:
                bufs = [buf]; note = f"tts:{client.name}"

        # degrade: no audio yet -> DSP approximation if synthesizable, else silence
        if not bufs:
            arch = synth.keyword_archetype(cue["descriptor"])
            if arch and route != "tts":
                bufs = synth.variations(synth.archetype_params(arch, cue["descriptor"]),
                                        cue["variations"], cseed)
                note = f"degrade-synth:{arch}"
            else:
                dur = max(0.2, min(2.0, cue["duration_s"]))
                bufs = [np.zeros(int(dur * synth.SR), dtype=np.float32)]
                note = "placeholder-silent"
                warnings.append(f"{cid}: {route} backend produced nothing -> silent "
                                f"placeholder (TODO: wire a {route} model or supply a clip)")

        clip_paths = []
        for i, buf in enumerate(bufs):
            p = audio_root / cid / f"{cid}_{i}.wav"
            synth.write_wav(p, buf)
            clip_paths.append(f"Audio/{cid}/{cid}_{i}.wav")

        entries.append({
            "id": cid, "category": cue["category"], "route": route, "source": note,
            "spatial": cue["spatial"], "loop": cue["loop"], "volume": 1.0,
            "clips": clip_paths, "variations": len(bufs),
        })

    manifest = {
        "seed": int(seed), "backend": client.name, "cue_count": len(entries),
        "cues": entries, "warnings": warnings,
    }
    # Written INSIDE Resources so Unity bundles it as a TextAsset; the AudioBank
    # self-populates from Resources.Load<TextAsset>("audio_manifest") at runtime.
    resources_dir.mkdir(parents=True, exist_ok=True)
    (resources_dir / "audio_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
