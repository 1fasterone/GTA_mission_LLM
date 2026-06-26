"""Tests for the NPC dialog micro-generator (Higgs Audio v2 TTS pipeline)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.micro.npc_dialog import (
    NpcDialogPipeline, NpcDialogSchema, DialogLine,
    VARIANTS,
)
from src.audio.higgs import (
    emotion_params, audio_style_preset, higgs_language,
    SUPPORTED_EMOTIONS, SUPPORTED_STYLES, SUPPORTED_LANGUAGES,
    HiggsAdapter,
)
from src.audio.dialog_resolver import (
    resolve_voice, resolve_language, resolve_emotion, resolve_audio_style,
    default_lines, default_voice_for,
)
from src.atoms.dialog import DIALOG_ATOMS
from src.atoms.behavior_map import MICROGEN_ATOM_MAP


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def dummy_config():
    return {
        "lm_studio": {"base_url": "http://localhost:1234/v1", "model": "test"},
        "unity_pipeline": {
            "unity_output_dir":   str(REPO_ROOT / "output" / "unity"),
            "unity_template_dir": str(REPO_ROOT / "templates" / "unity"),
        },
        "audio": {
            "backend":        "higgs",
            "generate_clips": False,   # never call Higgs in tests
            "cache_dir":      str(REPO_ROOT / "output" / "audio_cache"),
        },
    }


# ── Schema validation ─────────────────────────────────────────────────────────

def test_schema_default_variant():
    s = NpcDialogSchema()
    assert s.variant == "quest_giver"


def test_schema_clamps_unknown_variant():
    s = NpcDialogSchema(variant="bogus_variant")
    assert s.variant == "quest_giver"


def test_schema_clamps_interaction_type():
    s = NpcDialogSchema(interaction_type="unknown_type")
    assert s.interaction_type == "quest"


def test_schema_clamps_audio_style():
    s = NpcDialogSchema(audio_style="megaphone")
    assert s.audio_style == "natural"


def test_schema_multilingual_clamps_invalid_codes():
    s = NpcDialogSchema(multilingual_targets=["en", "xx", "es", "ZZZZ"])
    assert "en" in s.multilingual_targets
    assert "es" in s.multilingual_targets
    assert "xx" not in s.multilingual_targets
    assert "zz" not in s.multilingual_targets


def test_schema_line_emotion_clamped():
    line = DialogLine(line_id="a", text="Hello", emotion="ecstatic")
    assert line.emotion == "neutral"   # unsupported → neutral


def test_schema_line_language_clamped():
    line = DialogLine(line_id="a", text="Hello", language="qq")
    assert line.language == "en"


def test_schema_line_id_snake_cased():
    line = DialogLine(line_id="My Line ID!", text="Hello")
    assert line.line_id == "my_line_id"


def test_schema_line_deduplicated_in_fixer(dummy_config):
    p = NpcDialogPipeline(dummy_config)
    s = NpcDialogSchema(lines=[
        DialogLine(line_id="greet", text="Hello."),
        DialogLine(line_id="greet", text="Hi."),   # duplicate id
    ])
    fixed = p.default_fix(s, "quest_giver")
    ids = [l.line_id for l in fixed.lines]
    assert len(ids) == len(set(ids)), "Duplicate line IDs must be renamed"


# ── Higgs module ──────────────────────────────────────────────────────────────

def test_emotion_params_known():
    p = emotion_params("happy")
    assert p["speaking_rate"] > 1.0
    assert "pitch_shift" in p


def test_emotion_params_unknown_falls_back():
    p = emotion_params("volcanic_rage")
    assert p == emotion_params("neutral")


def test_audio_style_known():
    p = audio_style_preset("robotic")
    assert p["style"] == "robotic"


def test_audio_style_unknown_falls_back():
    p = audio_style_preset("foghorn")
    assert p == audio_style_preset("natural")


def test_higgs_language_known():
    assert higgs_language("es") == "es-ES"
    assert higgs_language("ja") == "ja-JP"


def test_higgs_language_unknown_falls_back():
    assert higgs_language("xx") == "en-US"


def test_higgs_adapter_returns_none_without_package():
    """With higgs-audio not installed the adapter must return None, not raise."""
    adapter = HiggsAdapter({})
    result  = adapter.tts("Hello world", voice="default", seed=0)
    assert result is None    # higgs-audio not installed → graceful degrade


# ── Dialog resolver ───────────────────────────────────────────────────────────

def test_resolve_voice_known():
    assert resolve_voice("male_deep") == "male_deep"


def test_resolve_voice_unknown_passes_through():
    assert resolve_voice("custom_voice_123") == "custom_voice_123"


def test_resolve_language_valid():
    assert resolve_language("fr") == "fr"
    assert resolve_language("FR") == "fr"   # case-insensitive


def test_resolve_language_invalid():
    assert resolve_language("zz") == "en"


def test_resolve_emotion_valid():
    assert resolve_emotion("happy") == "happy"


def test_resolve_emotion_invalid():
    assert resolve_emotion("BLISS") == "neutral"


def test_resolve_audio_style_valid():
    assert resolve_audio_style("whisper") == "whisper"


def test_resolve_audio_style_invalid():
    assert resolve_audio_style("yell") == "natural"


def test_default_lines_all_interaction_types():
    for itype in ("quest", "vendor", "combat", "follower", "ambient"):
        lines = default_lines(itype)
        assert len(lines) >= 2
        for l in lines:
            assert "line_id" in l and "text" in l and l["text"]


def test_default_voice_for_interaction():
    assert default_voice_for("combat")  == "male_young"
    assert default_voice_for("vendor")  == "female_soft"
    assert default_voice_for("unknown") == "default"


# ── Fixer ────────────────────────────────────────────────────────────────────

def test_fixer_injects_fallback_lines_when_empty(dummy_config):
    p = NpcDialogPipeline(dummy_config)
    s = NpcDialogSchema(variant="vendor", interaction_type="vendor")
    s = p.default_fix(s, "vendor")
    assert len(s.lines) > 0, "Fixer must inject fallback lines when empty"


def test_fixer_fills_higgs_prompt(dummy_config):
    p = NpcDialogPipeline(dummy_config)
    s = NpcDialogSchema(lines=[DialogLine(line_id="a", text="Test line.")])
    s = p.default_fix(s, "quest_giver")
    for line in s.lines:
        assert line.higgs_prompt, "higgs_prompt must be filled by fixer"


def test_fixer_voice_id_defaulted(dummy_config):
    p = NpcDialogPipeline(dummy_config)
    s = NpcDialogSchema(
        interaction_type="quest",
        lines=[DialogLine(line_id="a", text="Hello.", voice_id="")]
    )
    s = p.default_fix(s, "quest_giver")
    assert s.lines[0].voice_id, "voice_id must be filled by fixer"


def test_fixer_sets_prefab_path(dummy_config):
    p = NpcDialogPipeline(dummy_config)
    s = NpcDialogSchema(speaker_name="Blacksmith")
    s = p.default_fix(s, "quest_giver")
    assert "Blacksmith" in s.prefab_path


# ── File generation ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("variant", VARIANTS)
def test_write_files_all_variants(tmp_path, dummy_config, variant):
    p = NpcDialogPipeline(dummy_config)
    s = NpcDialogSchema(
        name="TestDialog",
        variant=variant,
        speaker_name="Guard",
        interaction_type="quest",
        lines=[
            DialogLine(line_id="greet",   text="Halt! Who goes there?", emotion="neutral"),
            DialogLine(line_id="warn",    text="State your business.", emotion="angry"),
        ],
    )
    s = p.default_fix(s, variant)
    count = p.write_files(s, variant, tmp_path)
    assert count >= 4

    player_cs = tmp_path / "Scripts" / "TestDialogPlayer.cs"
    trigger_cs = tmp_path / "Scripts" / "TestDialogTrigger.cs"
    data_json  = tmp_path / "Data" / "TestDialogData.json"

    assert player_cs.exists(),  "DialogPlayer.cs must be generated"
    assert trigger_cs.exists(), "DialogTrigger.cs must be generated"
    assert data_json.exists(),  "DialogData.json must be generated"

    player_text = player_cs.read_text()
    assert "TestDialogPlayer" in player_text
    assert "StartDialog"      in player_text
    assert "OnLine"           in player_text
    assert "OnDialogEnd"      in player_text

    trigger_text = trigger_cs.read_text()
    assert "TestDialogTrigger" in trigger_text

    data_text = data_json.read_text()
    assert "greet" in data_text
    assert "Guard" in data_text


def test_player_cs_has_audio_source(tmp_path, dummy_config):
    p = NpcDialogPipeline(dummy_config)
    s = NpcDialogSchema(name="BartenderDialog", speaker_name="Bartender",
                        lines=[DialogLine(line_id="l1", text="What'll it be?")])
    s = p.default_fix(s, "quest_giver")
    p.write_files(s, "quest_giver", tmp_path)
    cs = (tmp_path / "Scripts" / "BartenderDialogPlayer.cs").read_text()
    assert "AudioSource" in cs


def test_ambient_bark_variant_has_play_bark(tmp_path, dummy_config):
    p = NpcDialogPipeline(dummy_config)
    s = NpcDialogSchema(name="StreetVendor", variant="ambient_bark",
                        speaker_name="Vendor",
                        lines=[DialogLine(line_id="b1", text="Fresh bread!")])
    s = p.default_fix(s, "ambient_bark")
    p.write_files(s, "ambient_bark", tmp_path)
    cs = (tmp_path / "Scripts" / "StreetVendorPlayer.cs").read_text()
    assert "PlayBark" in cs


def test_cutscene_variant_trigger(tmp_path, dummy_config):
    p = NpcDialogPipeline(dummy_config)
    s = NpcDialogSchema(name="CutsceneNPC", variant="cutscene",
                        speaker_name="Hero",
                        lines=[DialogLine(line_id="c1", text="This is the moment.")])
    s = p.default_fix(s, "cutscene")
    p.write_files(s, "cutscene", tmp_path)
    trigger = (tmp_path / "Scripts" / "CutsceneNPCTrigger.cs").read_text()
    assert "TriggerCutscene" in trigger


def test_readme_lists_all_lines(tmp_path, dummy_config):
    p = NpcDialogPipeline(dummy_config)
    lines = [
        DialogLine(line_id="a1", text="Line A"),
        DialogLine(line_id="b1", text="Line B"),
    ]
    s = NpcDialogSchema(name="TestNpc", speaker_name="NPC", lines=lines)
    s = p.default_fix(s, "quest_giver")
    p.write_files(s, "quest_giver", tmp_path)
    readme = (tmp_path / "README.md").read_text()
    assert "a1" in readme
    assert "b1" in readme


# ── Atoms ─────────────────────────────────────────────────────────────────────

def test_dialog_atoms_registered():
    names = {a.name for a in DIALOG_ATOMS}
    assert "SayLine"          in names
    assert "PlayDialogAudio"  in names
    assert "Gesture"          in names
    assert "LookAt"           in names
    assert "EndDialog"        in names


def test_npc_dialog_in_behavior_map():
    assert "npc_dialog" in MICROGEN_ATOM_MAP
    seq = MICROGEN_ATOM_MAP["npc_dialog"]
    atom_names = {d["atom"] for d in seq}
    assert "SayLine"  in atom_names
    assert "LookAt"   in atom_names
    assert "EndDialog" in atom_names


# ── No-LLM path ──────────────────────────────────────────────────────────────

def test_no_llm_run(tmp_path, dummy_config):
    dummy_config["unity_pipeline"]["unity_output_dir"] = str(tmp_path)
    p = NpcDialogPipeline(dummy_config)
    out_dir = p.run(
        "A blacksmith NPC gives the player a repair quest.",
        use_llm=False,
        with_audio=False,
        variant="quest_giver",
        out=str(tmp_path / "no_llm_out"),
    )
    assert Path(out_dir).exists()
    scripts = list(Path(out_dir).glob("Scripts/*.cs"))
    assert len(scripts) >= 2


# ── Pipeline metadata (mirrors test_pipeline_metadata_set in test_micro_generators) ─

def test_npc_dialog_pipeline_metadata():
    assert NpcDialogPipeline.kind            == "npc_dialog"
    assert NpcDialogPipeline.default_variant in NpcDialogPipeline.variants
    assert "{user_prompt}" in NpcDialogPipeline.concept_prompt
    assert "{variant}"     in NpcDialogPipeline.concept_prompt
    assert "{theme_block}" in NpcDialogPipeline.concept_prompt
    assert "{concept}"     in NpcDialogPipeline.schema_prompt
    assert "{schema_hints}" in NpcDialogPipeline.schema_prompt
