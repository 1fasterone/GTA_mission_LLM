"""NPC-dialog TTS backend selection + Kokoro adapter contract."""

import numpy as np
import pytest

from src.audio.backends import CachingClient
from src.audio.higgs import HiggsAdapter, make_tts_client
from src.audio.kokoro_tts import KOKORO_LANG, KokoroAdapter, kokoro_voice


# ── factory selection ─────────────────────────────────────────────────────────

def test_default_tts_backend_is_kokoro():
    client = make_tts_client({"audio": {}})
    assert client.adapter.name == "kokoro"


def test_backend_selection():
    assert make_tts_client({"audio": {"tts_backend": "higgs"}}).adapter.name == "higgs"
    assert make_tts_client({"audio": {"tts_backend": "none"}}).adapter.name == "tts_none"
    assert make_tts_client({"audio": {"tts_backend": "kokoro"}}).adapter.name == "kokoro"


def test_make_higgs_client_alias_respects_config():
    # Back-compat entry point must not silently force the 11 GB model.
    from src.audio.higgs import make_higgs_client
    assert make_higgs_client({"audio": {}}).adapter.name == "kokoro"


# ── caching wrapper passes dialog kwargs through ──────────────────────────────

class _SpyAdapter:
    name = "spy"
    def __init__(self):
        self.calls = []
    def tts(self, text, voice, seed, **kw):
        self.calls.append((text, voice, seed, kw))
        return np.zeros(64, dtype=np.float32)


def test_caching_client_forwards_dialog_kwargs(tmp_path):
    # Regression: CachingClient.tts used to accept only (text, voice, seed) —
    # dialog's language/emotion/style kwargs raised TypeError and every line
    # silently became text-only.
    spy = _SpyAdapter()
    client = CachingClient(spy, tmp_path)
    out = client.tts("hello", "guard", 7, language="en",
                     emotion="angry", style="radio")
    assert out is not None and len(out)
    assert spy.calls[0][3] == {"language": "en", "emotion": "angry", "style": "radio"}


def test_caching_key_includes_kwargs(tmp_path):
    # Same text/voice/seed but different emotion must NOT share a cache entry.
    spy = _SpyAdapter()
    client = CachingClient(spy, tmp_path)
    client.tts("hello", "guard", 7, emotion="angry")
    client.tts("hello", "guard", 7, emotion="calm")
    assert len(list(tmp_path.glob("tts_*.wav"))) == 2


# ── kokoro adapter contract (no model required) ───────────────────────────────

def test_kokoro_voice_mapping_is_deterministic():
    assert kokoro_voice("default") == "af_heart"
    assert kokoro_voice("gruff_sergeant").startswith("am_")
    assert kokoro_voice("female_scientist").startswith("af_")
    assert kokoro_voice("gruff_sergeant") == kokoro_voice("gruff_sergeant")


def test_kokoro_language_map_falls_back_to_english():
    assert KOKORO_LANG["ja"] == "j"
    a = KokoroAdapter({"audio": {}})
    # unsupported language must not raise (warns once, uses English pipeline
    # or degrades to None if kokoro isn't installed)
    result = a.tts("hi", language="xx")
    assert result is None or isinstance(result, np.ndarray)


def test_kokoro_empty_text_is_none():
    assert KokoroAdapter({"audio": {}}).tts("") is None


def test_higgs_adapter_still_degrades_gracefully():
    # No higgs-audio installed → None, never an exception.
    assert HiggsAdapter({"audio": {}}).tts("hello") is None
