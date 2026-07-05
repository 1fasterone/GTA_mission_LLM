"""
sfxr / Bfxr-style procedural sound-effect synthesis (the deterministic audio tier).

The lesson from the model bases applies to audio too: a local LLM can't *author* a
recognisable sound, but a human can author a believable PARAMETRIC archetype once and
let the prompt turn knobs. So a small set of arcade archetypes (click, pickup, coin,
jump, laser, hit, explosion, powerup, footstep, clank, empty_clip, blip) are authored
here as parameter presets; `keyword_archetype` maps a prompt/descriptor to one and
`archetype_params` nudges its knobs from obvious cues. `render` turns params + a seed
into a mono float32 buffer at 44.1 kHz — fully DETERMINISTIC (same params+seed -> same
bytes), instant and offline. This is the "synthesizable" half of the audio pipeline;
voice/music/organic SFX go to the generative backend instead.

Pieces (mirrors src/blender/base_models.py):
  * `ARCHETYPES`      — per archetype, the parameter overrides (the authored presets).
  * `DEFAULT_PARAMS`  — every knob + its default (the full synth model).
  * `clamp_params`    — fill/clamp a raw param dict (robust; a bad value can't crash).
  * `keyword_archetype` / `archetype_params` — deterministic prompt -> archetype + nudges.
  * `render` / `variations` / `write_wav` — synth one buffer / a seeded bank / a .wav.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path
from typing import Optional

import numpy as np

SR = 44_100                      # sample rate (Hz)

# Waveform ids
WAVES = ("sine", "square", "saw", "triangle", "noise")

# ── the synth model: every knob + default ─────────────────────────────────────
# Durations in seconds; frequencies in Hz; factors are dimensionless.
DEFAULT_PARAMS: dict = {
    "wave":         "square",
    "attack":       0.004,    # s — fade-in
    "sustain":      0.06,     # s — hold at full
    "decay":        0.16,     # s — fade-out to silence
    "base_freq":    520.0,    # Hz — starting pitch
    "freq_slide":   0.0,      # per-second multiplicative pitch slide (>0 up, <0 down)
    "arp_mult":     1.0,      # arpeggio: pitch jumps by this factor at arp_time
    "arp_time":     0.0,      # s — when the arpeggio jump happens (0 = none)
    "duty":         0.5,      # square duty cycle (0..1)
    "vib_depth":    0.0,      # vibrato depth (fraction of freq)
    "vib_speed":    6.0,      # vibrato rate (Hz)
    "noise_mix":    0.0,      # 0 = pure tone, 1 = pure noise
    "lpf":          1.0,      # one-pole low-pass: 1 = open, ->0 = darker
    "drive":        1.0,      # soft-clip drive (>1 = grittier)
    "volume":       0.85,     # peak target after normalisation (0..1)
}

# Authored presets — only the knobs that differ from DEFAULT_PARAMS.
ARCHETYPES: dict[str, dict] = {
    "click":      {"wave": "square", "base_freq": 900, "attack": 0.001, "sustain": 0.01, "decay": 0.04, "freq_slide": -3.0, "lpf": 0.7},
    "blip":       {"wave": "square", "base_freq": 1200, "attack": 0.001, "sustain": 0.02, "decay": 0.05, "duty": 0.35},
    "pickup":     {"wave": "square", "base_freq": 620, "sustain": 0.03, "decay": 0.12, "arp_mult": 1.5, "arp_time": 0.045},
    "coin":       {"wave": "square", "base_freq": 760, "sustain": 0.02, "decay": 0.18, "arp_mult": 1.5, "arp_time": 0.05, "duty": 0.45},
    "powerup":    {"wave": "saw", "base_freq": 320, "sustain": 0.05, "decay": 0.35, "freq_slide": 3.2, "vib_depth": 0.03, "vib_speed": 9},
    "jump":       {"wave": "square", "base_freq": 380, "sustain": 0.04, "decay": 0.14, "freq_slide": 6.0, "duty": 0.55},
    "laser":      {"wave": "saw", "base_freq": 1100, "sustain": 0.03, "decay": 0.22, "freq_slide": -6.5, "lpf": 0.85},
    "hit":        {"wave": "noise", "base_freq": 380, "attack": 0.001, "sustain": 0.01, "decay": 0.16, "noise_mix": 0.8, "freq_slide": -2.0, "lpf": 0.5},
    "explosion":  {"wave": "noise", "base_freq": 200, "attack": 0.002, "sustain": 0.08, "decay": 0.55, "noise_mix": 1.0, "freq_slide": -1.2, "lpf": 0.35, "drive": 1.6},
    "footstep":   {"wave": "noise", "base_freq": 150, "attack": 0.001, "sustain": 0.008, "decay": 0.09, "noise_mix": 0.95, "lpf": 0.28, "drive": 1.2},
    "clank":      {"wave": "square", "base_freq": 240, "attack": 0.001, "sustain": 0.01, "decay": 0.3, "noise_mix": 0.35, "freq_slide": -1.5, "lpf": 0.8, "drive": 1.3},
    "empty_clip": {"wave": "square", "base_freq": 2600, "attack": 0.0005, "sustain": 0.004, "decay": 0.03, "noise_mix": 0.5, "lpf": 0.9},
}

ARCHETYPE_NAMES = tuple(ARCHETYPES.keys())

# prompt keyword -> archetype (first match wins; order matters — empty/laser are
# checked before gun/explosion so "gun clip empty" and "laser blaster" route right)
_KEYWORD_ARCHETYPE = [
    (("empty", "out of ammo", "no ammo", "dry fire", "clip empty", "click empty"), "empty_clip"),
    (("laser", "beam", "zap", "blaster", "phaser", "plasma"), "laser"),
    (("explosion", "explode", "blast", "boom", "grenade", "bomb"), "explosion"),
    (("gun", "gunfire", "gunshot", "shoot", "shot", "fire", "rifle", "pistol", "cannon"), "hit"),
    (("footstep", "step", "walk", "footfall", "stomp"), "footstep"),
    (("clank", "clang", "metal", "metallic", "wrench", "pipe", "anvil"), "clank"),
    (("coin", "gold", "money", "cash", "treasure"), "coin"),
    (("pickup", "pick up", "collect", "grab", "item", "gem", "star"), "pickup"),
    (("powerup", "power up", "power-up", "buff", "upgrade", "level up", "levelup"), "powerup"),
    (("jump", "hop", "leap", "bounce", "spring"), "jump"),
    (("hit", "punch", "impact", "thud", "damage", "hurt", "smack"), "hit"),
    (("blip", "beep", "ui", "menu", "select", "cursor", "tick"), "blip"),
    (("click", "button", "press", "tap", "confirm"), "click"),
]


def keyword_archetype(prompt: str) -> Optional[str]:
    """Deterministic prompt/descriptor -> archetype, no LLM. None if nothing matches."""
    p = (prompt or "").lower()
    for words, arch in _KEYWORD_ARCHETYPE:
        if any(w in p for w in words):
            return arch
    return None


def archetype_params(archetype: str, prompt: str = "") -> dict:
    """Authored preset for `archetype`, filled over the defaults, then nudged by a few
    obvious prompt cues (big/small/long/short/deep/high). Conservative — unmistakable
    cues only, so the result stays recognisable."""
    arch = archetype if archetype in ARCHETYPES else "blip"
    params = dict(DEFAULT_PARAMS)
    params.update(ARCHETYPES[arch])
    p = (prompt or "").lower()
    if any(w in p for w in ("big", "huge", "large", "heavy", "massive")):
        params["decay"] *= 1.7; params["base_freq"] *= 0.8; params["drive"] *= 1.15
    if any(w in p for w in ("small", "tiny", "light", "soft", "little")):
        params["decay"] *= 0.7; params["base_freq"] *= 1.25; params["volume"] *= 0.85
    if any(w in p for w in ("long", "slow", "sustained")):
        params["decay"] *= 1.6
    if any(w in p for w in ("short", "quick", "snappy", "fast")):
        params["decay"] *= 0.6
    if any(w in p for w in ("deep", "low", "bass", "boom")):
        params["base_freq"] *= 0.6; params["lpf"] = min(params["lpf"], 0.5)
    if any(w in p for w in ("high", "bright", "sharp", "shrill")):
        params["base_freq"] *= 1.5; params["lpf"] = 1.0
    return clamp_params(params)


# ── param validation ──────────────────────────────────────────────────────────

_RANGES: dict[str, tuple] = {
    "attack": (0.0, 1.0), "sustain": (0.0, 2.0), "decay": (0.001, 3.0),
    "base_freq": (20.0, 18_000.0), "freq_slide": (-40.0, 40.0),
    "arp_mult": (0.25, 4.0), "arp_time": (0.0, 2.0), "duty": (0.05, 0.95),
    "vib_depth": (0.0, 0.5), "vib_speed": (0.0, 40.0), "noise_mix": (0.0, 1.0),
    "lpf": (0.01, 1.0), "drive": (1.0, 4.0), "volume": (0.0, 1.0),
}


def clamp_params(raw: Optional[dict]) -> dict:
    """Fill defaults + clamp every known knob; drop unknowns; validate wave. A bad
    LLM/keyword token can never break the synth."""
    raw = raw or {}
    out = dict(DEFAULT_PARAMS)
    w = str(raw.get("wave", out["wave"])).strip().lower()
    out["wave"] = w if w in WAVES else DEFAULT_PARAMS["wave"]
    for name, (lo, hi) in _RANGES.items():
        try:
            out[name] = float(max(lo, min(hi, float(raw.get(name, DEFAULT_PARAMS[name])))))
        except (TypeError, ValueError):
            out[name] = DEFAULT_PARAMS[name]
    return out


# ── synthesis ─────────────────────────────────────────────────────────────────

def _oscillator(phase: np.ndarray, wave: str, duty: float, rng: np.random.Generator) -> np.ndarray:
    frac = np.mod(phase / (2.0 * math.pi), 1.0)
    if wave == "sine":
        return np.sin(phase)
    if wave == "saw":
        return 2.0 * frac - 1.0
    if wave == "triangle":
        return 2.0 * np.abs(2.0 * frac - 1.0) - 1.0
    if wave == "noise":
        return rng.uniform(-1.0, 1.0, size=phase.shape)
    # square (default), with duty cycle
    return np.where(frac < duty, 1.0, -1.0)


def _one_pole_lpf(x: np.ndarray, coef: float) -> np.ndarray:
    if coef >= 0.999:
        return x
    y = np.empty_like(x)
    acc = 0.0
    for i in range(x.shape[0]):
        acc += coef * (x[i] - acc)
        y[i] = acc
    return y


def render(params: dict, seed: int = 0) -> np.ndarray:
    """Synthesize one mono float32 buffer @ 44.1 kHz from `params`. Deterministic:
    identical (params, seed) -> identical samples. Returns peak-normalised audio."""
    p = clamp_params(params)
    rng = np.random.default_rng(int(seed) & 0x7FFFFFFF)

    total = max(0.01, p["attack"] + p["sustain"] + p["decay"])
    n = max(1, int(total * SR))
    t = np.arange(n, dtype=np.float64) / SR

    # instantaneous frequency: exponential slide + optional arpeggio jump + vibrato
    freq = p["base_freq"] * np.power(2.0, p["freq_slide"] * t)
    if p["arp_time"] > 0.0:
        freq = np.where(t >= p["arp_time"], freq * p["arp_mult"], freq)
    if p["vib_depth"] > 0.0:
        freq = freq * (1.0 + p["vib_depth"] * np.sin(2.0 * math.pi * p["vib_speed"] * t))
    freq = np.clip(freq, 20.0, SR * 0.45)

    phase = np.cumsum(2.0 * math.pi * freq / SR)
    osc = _oscillator(phase, p["wave"], p["duty"], rng)
    if 0.0 < p["noise_mix"] < 1.0 and p["wave"] != "noise":
        noise = rng.uniform(-1.0, 1.0, size=n)
        osc = (1.0 - p["noise_mix"]) * osc + p["noise_mix"] * noise

    # amplitude envelope (attack -> hold -> decay)
    env = np.ones(n, dtype=np.float64)
    a = int(p["attack"] * SR); d = int(p["decay"] * SR)
    if a > 0:
        env[:a] = np.linspace(0.0, 1.0, a, endpoint=False)
    if d > 0:
        env[n - d:] = np.linspace(1.0, 0.0, d)
    sig = osc * env

    if p["drive"] > 1.0:
        sig = np.tanh(sig * p["drive"])
    sig = _one_pole_lpf(sig, p["lpf"])

    peak = float(np.max(np.abs(sig))) or 1.0
    sig = (sig / peak) * p["volume"]
    return sig.astype(np.float32)


def variations(params: dict, n: int, seed: int = 0) -> list[np.ndarray]:
    """A seeded bank of `n` slightly-varied renders (pitch/decay jitter) — fills a
    cue's clips[] so repeated footsteps/hits don't sound machine-identical."""
    n = max(1, int(n))
    out = []
    for i in range(n):
        rng = np.random.default_rng((int(seed) * 2654435761 + i * 40503) & 0x7FFFFFFF)
        v = dict(params)
        v["base_freq"] = params.get("base_freq", DEFAULT_PARAMS["base_freq"]) * float(rng.uniform(0.94, 1.06))
        v["decay"] = params.get("decay", DEFAULT_PARAMS["decay"]) * float(rng.uniform(0.9, 1.1))
        out.append(render(v, seed=int(seed) + i + 1))
    return out


def write_wav(path: str | Path, samples: np.ndarray, sr: int = SR) -> Path:
    """Write a mono 16-bit PCM WAV. Pure stdlib `wave` so there's no soundfile dep."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(samples, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())
    return path


def read_wav(path: str | Path) -> np.ndarray:
    """Read a mono 16-bit PCM WAV back to float32 in [-1, 1] (used by the cache)."""
    with wave.open(str(path), "rb") as w:
        n = w.getnframes()
        raw = w.readframes(n)
        ch = w.getnchannels()
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data


def rms(samples: np.ndarray) -> float:
    """Root-mean-square level — used by tests to assert a clip isn't silent."""
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))


def resample(audio: np.ndarray, from_sr: int, to_sr: int) -> np.ndarray:
    """Resample a mono float32 buffer. Prefers scipy; falls back to np.interp.

    Shared by the TTS adapters (Higgs/Kokoro emit at their own native rates and
    must land at synth.SR), so it lives in this neutral DSP module rather than
    inside either engine.
    """
    if from_sr == to_sr:
        return audio
    try:
        from scipy.signal import resample_poly  # type: ignore
        from math import gcd
        g = gcd(from_sr, to_sr)
        return resample_poly(audio, to_sr // g, from_sr // g).astype(np.float32)
    except ImportError:
        n_out = int(len(audio) * to_sr / from_sr)
        idx   = np.linspace(0, len(audio) - 1, n_out)
        return np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
