"""
The Step 3.5 freeze boundary (src/llm_boundary.py, UnityPipeline._run_llm_enrichment).

CLAUDE.md gives the Pipeline LLM Steps 2-3 only. Every LLM-authored artifact
beyond the schema fill is written in ONE declared phase, Step 3.5, into the
schema; after it the client is frozen, so Steps 4-7 cannot call the model —
not by convention, by a loud PostSchemaLLMError.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import src.unity_pipeline as unity_pipeline
from src.llm_boundary import (ENRICHMENT_FORMAT, ENRICHMENT_META_KEY,
                              FrozenLLM, PostSchemaLLMError, freeze, thaw)
from src.unity_spatial_pipeline import SpatialPipelineError
from src.unity_pipeline import (UnityPipeline, _inject_npc_dialog_lines,
                                _persist_collection_bindings)
from src.unity_schema import UnitySchema

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config(tmp_path) -> dict:
    return {
        "lm_studio": {
            "base_url": "http://localhost:1234/v1", "model": "test-model",
            "temperature": 0.7, "max_tokens": 8192, "timeout": 30.0,
            "concept_temperature": 0.85, "schema_temperature": 0.2,
        },
        "unity_pipeline": {
            "lookups_dir": "lookups",
            "unity_template_dir": "templates/unity",
            "unity_output_dir": str(tmp_path),
        },
        "logging": {"show_prompts": False},
    }


def _schema_json(**meta) -> str:
    return json.dumps({
        "project_name": "FreezeCheck",
        "company_name": "TestCo",
        "game_type": "3D",
        "platform": "PC",
        "render_pipeline": "URP",
        "unity_version": "6000.0",
        "color_space": "Linear",
        "description": "A small arena game",
        "features": ["input_system"],
        "scenes": ["Main"],
        "scripts": [
            {"name": "GameManager", "type": "game_manager", "description": "GM"},
            {"name": "PlayerController", "type": "player", "description": "Player"},
            {"name": "ScoreKeeper", "type": "custom",
             "description": "Tracks the score"},
        ],
        "entities": [
            {"name": "Player", "kind": "player", "tag": "Player", "count": 1},
            {"name": "Crate", "kind": "prop", "count": 3, "placement": "line"},
        ],
        "packages": [{"description": "universal render pipeline"}],
        "meta": meta,
    })


def _recording_llm(pipeline: UnityPipeline, schema_json: str) -> list[str]:
    """Script the two Steps 2-3 answers; answer every later call with '{}'.

    Returns the event log: one "chat" per LLM call, one "resolve" per Step 4
    resolver call, in the order they happened.
    """
    events: list[str] = []
    answers = iter(["An arena where the player stacks crates.", schema_json])

    def _chat(*_args, **_kwargs):
        events.append("chat")
        return next(answers, "{}")

    pipeline.llm.chat = MagicMock(side_effect=_chat)
    real_resolve = pipeline.resolver.resolve_schema

    def _resolve(schema):
        events.append("resolve")
        return real_resolve(schema)

    pipeline.resolver.resolve_schema = _resolve
    return events


# ── the guard itself ──────────────────────────────────────────────────────────

class _Client:
    seed = 7

    def __init__(self):
        self.unloaded = False

    def chat(self, *a, **k):
        return "live"

    def extract_json(self, text):
        return {"parsed": text}

    def unload(self):
        self.unloaded = True
        return True


def test_frozen_client_refuses_inference_loudly():
    frozen = freeze(_Client())
    with pytest.raises(PostSchemaLLMError, match="freeze boundary"):
        frozen.chat(system="s", user="u")


def test_frozen_client_passes_lifecycle_and_pure_helpers_through():
    inner = _Client()
    frozen = freeze(inner)
    assert frozen.seed == 7
    assert frozen.extract_json("x") == {"parsed": "x"}
    assert frozen.unload() is True and inner.unloaded
    frozen.seed = 11                       # writes reach the live client
    assert inner.seed == 11


def test_freeze_and_thaw_round_trip():
    inner = _Client()
    frozen = freeze(inner)
    assert isinstance(frozen, FrozenLLM)
    assert freeze(frozen) is frozen        # idempotent
    assert thaw(frozen) is inner
    assert thaw(inner) is inner
    assert freeze(None) is None


# ── the pipeline honours it ──────────────────────────────────────────────────

@pytest.mark.parametrize("compose", [False, True])
def test_every_llm_call_happens_before_step_4(config, compose):
    pipeline = UnityPipeline(config)
    events = _recording_llm(pipeline, _schema_json())
    pipeline.run("an arena game", compose=compose)

    assert "resolve" in events
    first_resolve = events.index("resolve")
    assert events.count("chat") > 2, "Step 3.5 should have enriched something"
    assert "chat" not in events[first_resolve:], (
        f"LLM called after Step 4 began: {events}")


def test_step_3_5_record_is_persisted_in_the_written_schema(config, tmp_path):
    pipeline = UnityPipeline(config)
    _recording_llm(pipeline, _schema_json())
    root = pipeline.run("an arena game", compose=True)

    saved = json.loads((root / "FreezeCheck.schema.json").read_text(encoding="utf-8"))
    record = saved["meta"][ENRICHMENT_META_KEY]
    assert record["format"] == ENRICHMENT_FORMAT
    assert record["stage"] == "3.5"


def test_schema_fill_cannot_author_the_compiler_owned_record(config):
    pipeline = UnityPipeline(config)
    forged = {ENRICHMENT_META_KEY: {"audio_cues": [{"id": "forged"}]}}
    _recording_llm(pipeline, _schema_json(**forged))
    root = pipeline.run("an arena game", compose=True)

    saved = json.loads((root / "FreezeCheck.schema.json").read_text(encoding="utf-8"))
    record = saved["meta"][ENRICHMENT_META_KEY]
    assert record.get("audio_cues") != [{"id": "forged"}]
    assert record["format"] == ENRICHMENT_FORMAT


def test_step_6_disables_live_asset_fetch_after_the_schema_boundary(config):
    config["sketchfab"] = {
        "enabled": True, "auto_fetch": True, "vision_select": True,
    }
    pipeline = UnityPipeline(config)
    _recording_llm(pipeline, _schema_json())
    captured: dict = {}
    real_write = pipeline.generator.write

    def _write(*args, **kwargs):
        captured.update(kwargs["sketchfab_cfg"])
        return real_write(*args, **kwargs)

    pipeline.generator.write = _write
    pipeline.run("an arena game")

    assert captured["auto_fetch"] is False
    assert captured["vision_select"] is False


def test_acceptance_retries_a_lossy_schema_fill_instead_of_shipping_defaults(config):
    config["acceptance"] = {"reject_lossy_normalization": True}
    lossy = json.loads(_schema_json())
    lossy["platform"] = "toaster"
    pipeline = UnityPipeline(config)
    pipeline.llm.chat = MagicMock(side_effect=[
        "An arena where the player stacks crates.", json.dumps(lossy), json.dumps(lossy),
    ])

    with pytest.raises(RuntimeError, match="unsupported values"):
        pipeline.run("an arena game")
    assert pipeline.llm.chat.call_count == 3  # concept + two schema attempts


def test_acceptance_error_names_dropped_feature_values(config):
    config["acceptance"] = {"reject_lossy_normalization": True}
    lossy = json.loads(_schema_json())
    lossy["features"] = ["physics", "unsupported_projectile_feature"]
    pipeline = UnityPipeline(config)
    pipeline.llm.chat = MagicMock(side_effect=[
        "An arena where the player stacks crates.", json.dumps(lossy), json.dumps(lossy),
    ])

    with pytest.raises(RuntimeError, match="dropped: 'unsupported_projectile_feature'"):
        pipeline.run("an arena game")


def test_spatial_acceptance_gate_refuses_generation_before_step_6(config, monkeypatch):
    config["acceptance"] = {"require_spatial_validation": True}
    pipeline = UnityPipeline(config)
    _recording_llm(pipeline, _schema_json())
    pipeline.generator.write = MagicMock(side_effect=AssertionError("Step 6 must not run"))
    monkeypatch.setattr(
        unity_pipeline,
        "prepare_spatial_layout",
        lambda *_args, **_kwargs: {
            "world_layout": {"layout": {"messages": []}},
            "spatial_validation": {"status": "fail"},
        },
    )

    with pytest.raises(SpatialPipelineError, match="refusing generation"):
        pipeline.run("an arena game")


def test_post_boundary_llm_call_raises(config, monkeypatch):
    """A Step 5 pass that reached for the model would fail the run loudly."""
    pipeline = UnityPipeline(config)
    _recording_llm(pipeline, _schema_json())
    real_layout = unity_pipeline.compute_layout

    def _layout_that_calls_the_llm(schema):
        pipeline.llm.chat(system="s", user="u")
        return real_layout(schema)

    monkeypatch.setattr(unity_pipeline, "compute_layout", _layout_that_calls_the_llm)
    with pytest.raises(PostSchemaLLMError):
        pipeline.run("an arena game")


def test_client_is_live_again_after_the_run(config):
    pipeline = UnityPipeline(config)
    _recording_llm(pipeline, _schema_json())
    pipeline.run("an arena game")
    assert not isinstance(pipeline.llm, FrozenLLM)


def test_no_llm_run_never_contacts_the_model(config):
    """--no-llm skips Step 3.5 entirely; the record is absent, not empty."""
    brief = (REPO_ROOT / "prompts" / "coldwater_circuit.txt").read_text(encoding="utf-8")
    pipeline = UnityPipeline(config)
    pipeline.llm.chat = MagicMock(side_effect=AssertionError("no LLM under --no-llm"))
    root = pipeline.run(brief, no_llm=True)
    pipeline.llm.chat.assert_not_called()
    saved = json.loads(next(root.glob("*.schema.json")).read_text(encoding="utf-8"))
    assert ENRICHMENT_META_KEY not in saved["meta"]


# ── Step 6 reads what Step 3.5 persisted ─────────────────────────────────────

class _Sub:
    def __init__(self, kind, variant="", **options):
        self.kind, self.variant, self.options = kind, variant, dict(options)


def test_npc_dialog_lines_reach_the_matching_unit_only():
    lines = [{"line_id": "l1", "text": "Welcome, traveller."}]
    meta = {ENRICHMENT_META_KEY: {"npc_dialog_lines": {"vendor:Mira": lines}}}
    mira = _Sub("npc_dialog", "vendor", speaker_name="Mira",
                meta={"skip_audio": True})
    other = _Sub("npc_dialog", "vendor", speaker_name="Oren")
    audio = _Sub("audio")

    msgs = _inject_npc_dialog_lines([mira, other, audio], meta)

    assert mira.options["meta"]["_pre_lines"] == lines
    assert "skip_audio" not in mira.options["meta"]
    assert "meta" not in other.options and "meta" not in audio.options
    assert len(msgs) == 1 and "Mira" in msgs[0]


def test_injection_is_a_noop_without_a_record():
    sub = _Sub("npc_dialog", "vendor", speaker_name="Mira")
    assert _inject_npc_dialog_lines([sub], {}) == []
    assert "meta" not in sub.options


def test_step6_persists_sketchfab_entity_binding_with_the_collection():
    schema = UnitySchema.model_validate(json.loads(_schema_json()))
    selection = {
        "filename": "rival-truck.fbx",
        "relpath": "vehicle/rival-truck.fbx",
        "source": "sketchfab",
        "file_sha256": "a" * 64,
        "sketchfab_uid": "truck-uid",
    }
    schema.meta[ENRICHMENT_META_KEY] = {
        "sketchfab_collection": {"entries": [], "entity_bindings": {}},
    }
    schema.meta["entity_model_bindings"] = {
        "Player": {"catalog_selection": selection},
    }

    _persist_collection_bindings(schema)

    collection = schema.meta[ENRICHMENT_META_KEY]["sketchfab_collection"]
    assert collection["entity_bindings"] == {"Player": selection}
    assert collection["entries"] == [selection]
