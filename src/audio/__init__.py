"""Procedural + generative audio content for the audio micro-generator.

  * synth    — sfxr-style DSP archetypes (deterministic, offline).
  * resolver — per-cue routing (synth/gen/tts/library) + per-game-type required cues.
  * backends — make_audio_client (dsp / local / none) with a content-hash cache.
"""

from . import synth, resolver, backends, content
from .backends import make_audio_client
from .content import generate_bank

__all__ = ["synth", "resolver", "backends", "content",
           "make_audio_client", "generate_bank"]
