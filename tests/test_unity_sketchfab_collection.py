"""Tests for concept-driven batch Sketchfab collection
(src/unity_sketchfab_collection.py's collect_models).

This is the OTHER fetch_and_freeze caller besides the per-entity Tier S in
src/unity_entity_asset_resolver.py (see tests/test_entity_asset_resolver.py's
own vision_select_fn / semantic_filter_fn gating tests, which this file
mirrors). Before this wiring, collect_models called fetch_and_freeze with
neither optional QC gate at all — every LLM-proposed model brief was
searched, downloaded, and frozen into the shared catalog on keyword match
alone, with no visual/semantic check ever running.
"""

import json

import pytest

from src.unity_sketchfab_collection import (COLLECTION_LOCK_FORMAT, collect_models,
                                             frozen_collection_record,
                                             replay_collection_entries)


class FakeLLM:
    """collect_models calls llm.chat() exactly once, inside
    extract_model_briefs, to get the style + model briefs — this stubs that
    single call."""

    def __init__(self, models, style="cartoon"):
        self.models = models
        self.style = style

    def chat(self, **kwargs):
        return json.dumps({"style": self.style, "models": self.models})


class FakeClient:
    def available(self):
        return True


_ONE_BRIEF = [{"name": "Rival Truck", "query": "dirt race vehicle", "category": "vehicle"}]


def test_frozen_collection_record_replays_exact_verified_asset_bytes(tmp_path):
    cache_dir = tmp_path / "Models" / "Sketchfab"
    asset = cache_dir / "vehicle" / "rival-truck.fbx"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"frozen-fbx")
    record = frozen_collection_record({
        "style": "cartoon",
        "written": [{
            "filename": asset.name,
            "relpath": "vehicle/rival-truck.fbx",
            "root": cache_dir.as_posix(),
            "source": "sketchfab",
            "category": "vehicle",
            "query": "cartoon dirt race vehicle",
            "sketchfab_uid": "truck-uid",
            "fetched_at": "not-replay-evidence",
        }],
    }, cache_dir=cache_dir)

    assert record["format"] == COLLECTION_LOCK_FORMAT
    assert "fetched_at" not in record["entries"][0]
    verified_root, entries = replay_collection_entries(record)
    assert verified_root == cache_dir
    assert entries[0]["file_sha256"] == record["entries"][0]["file_sha256"]

    asset.write_bytes(b"changed-fbx")
    with pytest.raises(ValueError, match="bytes changed"):
        replay_collection_entries(record)


# ── vision_select_fn gating: sketchfab.vision_select must be set ────────────
# Unlike the per-entity resolver, `llm` here is not an independent gate — it's
# already required just to produce any briefs at all (extract_model_briefs
# returns none without one), so the only thing left to test is the config key.

def test_vision_select_absent_never_builds_a_vision_select_fn(tmp_path, monkeypatch):
    """Today's default (config key absent): vision_select_fn must be None,
    byte-identical to before this parameter existed."""
    seen = {}
    monkeypatch.setattr(
        "src.sketchfab_freeze.fetch_and_freeze",
        lambda *a, vision_select_fn=None, **kw: seen.update(fn=vision_select_fn) or None)
    collect_models(
        "a dusty desert trophy-truck race", FakeLLM(_ONE_BRIEF), client=FakeClient(),
        sk_cfg={}, lookups_dir=str(tmp_path / "lookups"))
    assert seen["fn"] is None


def test_vision_select_enabled_builds_a_brief_bound_closure(tmp_path, monkeypatch):
    """Config flag on: a real callable reaches fetch_and_freeze, and calling
    it dispatches to select_best_candidate with THIS brief's own
    name/category/query — confirming the closure captured the right brief,
    not whichever one happened to run last in the loop."""
    seen_fn = {}
    monkeypatch.setattr(
        "src.sketchfab_freeze.fetch_and_freeze",
        lambda *a, vision_select_fn=None, **kw: seen_fn.update(fn=vision_select_fn) or None)
    calls = []
    monkeypatch.setattr(
        "src.unity_sketchfab_vision.select_best_candidate",
        lambda name, kind, desc, cands, *, client, llm: calls.append(
            (name, kind, desc)) or "picked-uid")

    fake_llm = FakeLLM(_ONE_BRIEF)
    collect_models(
        "a dusty desert trophy-truck race", fake_llm, client=FakeClient(),
        sk_cfg={"vision_select": True}, lookups_dir=str(tmp_path / "lookups"))

    assert seen_fn["fn"] is not None
    result = seen_fn["fn"]([{"uid": "x"}])
    assert result == "picked-uid"
    assert calls == [("Rival Truck", "vehicle", "cartoon dirt race vehicle")]


# ── semantic_filter_fn gating: sketchfab.semantic_select alone is enough ────

def test_semantic_select_absent_never_builds_a_filter_fn(tmp_path, monkeypatch):
    """Today's default (config key absent): semantic_filter_fn must be None,
    byte-identical to before this parameter existed."""
    seen = {}
    monkeypatch.setattr(
        "src.sketchfab_freeze.fetch_and_freeze",
        lambda *a, semantic_filter_fn=None, **kw: seen.update(fn=semantic_filter_fn) or None)
    collect_models(
        "a dusty desert trophy-truck race", FakeLLM(_ONE_BRIEF), client=FakeClient(),
        sk_cfg={}, lookups_dir=str(tmp_path / "lookups"))
    assert seen["fn"] is None


def test_semantic_select_closure_binds_brief_query_and_thresholds(tmp_path, monkeypatch):
    """The built closure must dispatch to unity_semantic_embed.semantic_filter
    with THIS brief's own name/category/query (the full style-prefixed query
    string collect_models actually searches with) and the configured
    similarity floors — confirming both the right brief and config
    thresholds reach the scorer, mirroring the per-entity resolver's own
    wiring for parity."""
    seen = {}
    monkeypatch.setattr(
        "src.sketchfab_freeze.fetch_and_freeze",
        lambda *a, semantic_filter_fn=None, **kw: seen.update(fn=semantic_filter_fn) or None)
    calls = []
    monkeypatch.setattr(
        "src.unity_semantic_embed.semantic_filter",
        lambda name, kind, concept_text, cands, *, client, min_image_similarity,
               min_text_similarity, max_candidates: calls.append(
            (name, kind, concept_text, min_image_similarity, min_text_similarity,
             max_candidates))
            or list(cands))

    collect_models(
        "a dusty desert trophy-truck race", FakeLLM(_ONE_BRIEF), client=FakeClient(),
        sk_cfg={"semantic_select": True, "min_image_similarity": 0.33,
               "min_text_similarity": 0.44},
        lookups_dir=str(tmp_path / "lookups"))

    assert seen["fn"] is not None
    result = seen["fn"]([{"uid": "x"}])
    assert result == [{"uid": "x"}]
    assert len(calls) == 1
    name, kind, concept_text, min_img, min_txt, max_cand = calls[0]
    assert (name, kind, concept_text) == ("Rival Truck", "vehicle", "cartoon dirt race vehicle")
    assert min_img == pytest.approx(0.33)
    assert min_txt == pytest.approx(0.44)
    assert max_cand == 6  # default preserved when semantic_pool_size is absent


def test_semantic_config_pool_size_reaches_max_candidates(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "src.sketchfab_freeze.fetch_and_freeze",
        lambda *a, semantic_filter_fn=None, **kw: seen.update(fn=semantic_filter_fn) or None)
    calls = []
    monkeypatch.setattr(
        "src.unity_semantic_embed.semantic_filter",
        lambda name, kind, concept_text, cands, *, client, min_image_similarity,
               min_text_similarity, max_candidates: calls.append(max_candidates)
            or list(cands))

    collect_models(
        "a dusty desert trophy-truck race", FakeLLM(_ONE_BRIEF), client=FakeClient(),
        sk_cfg={"semantic_select": True, "semantic_pool_size": 15},
        lookups_dir=str(tmp_path / "lookups"))

    seen["fn"]([{"uid": "x"}])
    assert calls == [15]


def test_semantic_default_pool_size_is_six_when_config_omits_it(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "src.sketchfab_freeze.fetch_and_freeze",
        lambda *a, semantic_filter_fn=None, **kw: seen.update(fn=semantic_filter_fn) or None)
    calls = []
    monkeypatch.setattr(
        "src.unity_semantic_embed.semantic_filter",
        lambda name, kind, concept_text, cands, *, client, min_image_similarity,
               min_text_similarity, max_candidates: calls.append(max_candidates)
            or list(cands))

    collect_models(
        "a dusty desert trophy-truck race", FakeLLM(_ONE_BRIEF), client=FakeClient(),
        sk_cfg={"semantic_select": True}, lookups_dir=str(tmp_path / "lookups"))

    seen["fn"]([{"uid": "x"}])
    assert calls == [6]


def test_both_gates_off_by_default_leaves_fetch_and_freeze_untouched(tmp_path, monkeypatch):
    """Sanity check that an empty sk_cfg (today's real-world default before
    a project opts in) reaches fetch_and_freeze with both new kwargs present
    but None — never omitted, never truthy by accident."""
    seen = {}
    monkeypatch.setattr(
        "src.sketchfab_freeze.fetch_and_freeze",
        lambda *a, vision_select_fn=None, semantic_filter_fn=None, **kw: seen.update(
            vision=vision_select_fn, semantic=semantic_filter_fn) or None)
    collect_models(
        "a dusty desert trophy-truck race", FakeLLM(_ONE_BRIEF), client=FakeClient(),
        sk_cfg={}, lookups_dir=str(tmp_path / "lookups"))
    assert seen == {"vision": None, "semantic": None}
