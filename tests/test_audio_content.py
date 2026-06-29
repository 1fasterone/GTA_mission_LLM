"""
Tests for the audio content pipeline (src/audio/* + the audio micro's content pass).
Pure host-side: DSP determinism, routing, cue clamping, bank generation + degrade.
No model downloads, no LLM, no Unity.
"""

import json

import numpy as np
import pytest

from src.audio import synth, resolver
from src.audio.content import generate_bank
from src.micro.audio import AudioPipeline, AudioCueSpec, AudioSchema


# ── DSP synth ─────────────────────────────────────────────────────────────────

def test_synth_deterministic_and_audible():
    p = synth.archetype_params("coin", "collect a gold coin")
    a = synth.render(p, seed=5)
    b = synth.render(p, seed=5)
    assert np.array_equal(a, b)                 # same params+seed -> identical bytes
    assert synth.rms(a) > 0.05                  # not silent
    assert a.dtype == np.float32


@pytest.mark.parametrize("arch", synth.ARCHETYPE_NAMES)
def test_every_archetype_renders_nonsilent(arch):
    s = synth.render(synth.archetype_params(arch), seed=3)
    assert len(s) > 0 and synth.rms(s) > 0.02


def test_clamp_params_is_robust():
    p = synth.clamp_params({"wave": "bogus", "base_freq": 1e9, "decay": -5, "duty": "x"})
    assert p["wave"] == synth.DEFAULT_PARAMS["wave"]
    assert p["base_freq"] <= 18_000.0 and p["decay"] >= 0.001
    assert p["duty"] == synth.DEFAULT_PARAMS["duty"]   # bad value -> default


@pytest.mark.parametrize("prompt,expected", [
    ("gun clip empty", "empty_clip"),
    ("laser blaster pew", "laser"),
    ("a huge explosion", "explosion"),
    ("collect a gold coin", "coin"),
    ("heavy footsteps", "footstep"),
    ("menu select beep", "blip"),
    ("a glowing crystal", None),
])
def test_keyword_archetype(prompt, expected):
    assert synth.keyword_archetype(prompt) == expected


# ── resolver ──────────────────────────────────────────────────────────────────

def test_classify_routes():
    assert resolver.classify("voice", "auto", "npc line") == "tts"
    assert resolver.classify("music", "auto", "tense theme") == "gen"
    assert resolver.classify("sfx", "auto", "collect a coin") == "synth"
    assert resolver.classify("sfx", "auto", "splashing water") == "gen"
    # explicit synth with no matching archetype falls back to gen
    assert resolver.classify("sfx", "synth", "ocean waves") == "gen"
    assert resolver.classify("sfx", "gen", "coin") == "gen"   # explicit wins


def test_game_type_and_required_cues():
    assert resolver.game_type_from_prompt("a fast shoot-em-up") == "shooter"
    assert resolver.game_type_from_prompt("a cozy puzzle game") == "puzzle"
    assert resolver.game_type_from_prompt("a calm garden sim") == "generic"
    ids = [c["id"] for c in resolver.required_cues("shooter")]
    assert {"gun_fire", "empty_clip", "reload"} <= set(ids)


# ── cue spec validation ───────────────────────────────────────────────────────

def test_audiocuespec_clamps():
    c = AudioCueSpec(id="Gun Fire!", category="weird", kind="nonsense",
                     variations=99, duration_s=999)
    assert c.id == "gun_fire" and c.category == "sfx" and c.kind == "auto"
    assert c.variations == 8 and c.duration_s == 20.0


# ── bank generation ───────────────────────────────────────────────────────────

def _cfg(tmp_path, backend="dsp"):
    return {
        "lm_studio": {"backend": "lm_studio", "model": "x"},
        "audio": {"backend": backend, "generate_clips": True,
                  "cache_dir": str(tmp_path / "cache")},
    }


def test_generate_bank_synth_deterministic(tmp_path):
    cfg = _cfg(tmp_path)
    cues = resolver.required_cues("platformer")
    res1 = tmp_path / "p1" / "Resources"
    res2 = tmp_path / "p2" / "Resources"
    m1 = generate_bank(cues, res1, cfg, seed=1)
    m2 = generate_bank(cues, res2, cfg, seed=1)
    assert m1["cue_count"] == len(cues)
    # sfx cues route through DSP synth; music/ambient require stable3 (not available here)
    sfx_entries = [e for e in m1["cues"] if e["category"] == "sfx"]
    assert all(e["route"] == "synth" for e in sfx_entries)
    sfx_ids = {e["id"] for e in sfx_entries}
    assert not [w for w in m1["warnings"] if any(sid in w for sid in sfx_ids)]
    # sfx clips exist, are audible, and are byte-identical across runs (determinism)
    for e in sfx_entries:
        w = res1 / e["clips"][0]
        assert w.exists() and synth.rms(synth.read_wav(w)) > 0.02
        assert w.read_bytes() == (res2 / e["clips"][0]).read_bytes()
    assert (res1 / "audio_manifest.json").exists()


def test_generate_bank_degrades_voice_without_backend(tmp_path):
    cfg = _cfg(tmp_path, backend="dsp")     # dsp can't do TTS
    cues = [{"id": "npc_hi", "category": "voice", "kind": "auto",
             "descriptor": "npc greeting", "text": "hello"}]
    m = generate_bank(cues, tmp_path / "Resources", cfg, seed=1)
    e = m["cues"][0]
    assert e["route"] == "tts" and e["source"] == "placeholder-silent"
    assert m["warnings"]                    # flagged TODO, not a crash
    silent = synth.read_wav(tmp_path / "Resources" / e["clips"][0])
    assert synth.rms(silent) == 0.0


# ── audio micro integration (no LLM) ──────────────────────────────────────────

def test_default_fix_injects_required_cues():
    pipe = AudioPipeline(_cfg_min())
    schema = AudioSchema()
    schema.meta = {"user_prompt": "a twin-stick shooter"}
    schema = pipe.default_fix(schema, "categorical_mixer")
    ids = {c.id for c in schema.cues}
    assert schema.game_type == "shooter"
    assert {"gun_fire", "empty_clip", "reload"} <= ids


def test_no_llm_run_writes_bank_and_clips(tmp_path):
    pipe = AudioPipeline(_cfg(tmp_path))
    out_dir = pipe.run("a retro platformer", use_llm=False, seed=2,
                       out=str(tmp_path / "proj"))
    assert (out_dir / "Scripts" / "AudioCue.cs").exists()
    assert (out_dir / "Resources" / "audio_manifest.json").exists()
    man = json.loads((out_dir / "Resources" / "audio_manifest.json").read_text())
    ids = {c["id"] for c in man["cues"]}
    assert {"jump", "coin", "hit"} <= ids       # platformer required cues present
    # at least one real clip on disk
    first = out_dir / "Resources" / man["cues"][0]["clips"][0]
    assert first.exists()


def _cfg_min():
    return {"lm_studio": {"backend": "lm_studio", "model": "x"},
            "audio": {"backend": "dsp", "generate_clips": True,
                      "cache_dir": "output/audio_cache"}}
