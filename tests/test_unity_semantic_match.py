"""Tests for src/unity_semantic_match.py — the embedding glue for Solution #7
(docs/COMPILER_AUDIT.md Gap E). Every test mocks the embedder (no real model
download, no network, no ONNX inference) — mirrors
tests/test_unity_semantic_embed.py's convention, adapted for this module's
deferred `from . import unity_semantic_embed as sem` (patch the ORIGIN via
its dotted string path, "src.unity_semantic_embed._bge_text_embedder", not a
local binding — a top-level patch wouldn't be seen by a fresh import
resolved inside the function body at call time).
"""
from __future__ import annotations

import json

import numpy as np
import pytest

import src.unity_semantic_match as sm
from src.unity_schema import EntitySpec


class _FakeBge:
    """Duck-typed fake matching fastembed's real .embed(texts) ->
    list[list[float]] shape. Fixed, hand-picked vectors per known text (not
    hash()-derived — this repo has already learned the hard way, via
    unity_entity_asset_resolver._stable_seed, that hash() is
    PYTHONHASHSEED-salted and not safe for anything that must be
    reproducible), so cosine-similarity assertions mean exactly what they say."""

    VECTORS = {
        "alpha": [1.0, 0.0],
        "alpha twin": [1.0, 0.0],       # identical vector -> cosine 1.0
        "beta": [0.0, 1.0],             # orthogonal to alpha -> cosine 0.0
    }

    def embed(self, texts):
        return [self.VECTORS.get(t, [0.5, 0.5]) for t in texts]


def _patch_bge(monkeypatch):
    monkeypatch.setattr("src.unity_semantic_embed._bge_text_embedder", lambda: _FakeBge())


# ── shared text builders ────────────────────────────────────────────────────

class TestTextBuilders:

    def test_entity_embedding_text_fixed_field_order(self):
        e = EntitySpec(name="Rivals", kind="vehicle", count=3,
                       model_hint="sports car", sketchfab_query="racing car")
        assert sm.entity_embedding_text(e) == "Rivals vehicle sports car racing car"

    def test_entity_embedding_text_kind_override(self):
        e = EntitySpec(name="Rivals", kind="vehicle", count=3)
        assert sm.entity_embedding_text(e, kind="car") == "Rivals car"

    def test_entity_embedding_text_skips_empty_fields(self):
        e = EntitySpec(name="Trees", kind="prop", count=5)
        assert sm.entity_embedding_text(e) == "Trees prop"

    def test_group_embedding_text_prefers_source_file_then_file(self):
        assert sm.group_embedding_text(
            {"name": "Bike", "source_file": "victorian_monobike.fbx"}
        ) == "Bike victorian_monobike.fbx"
        assert sm.group_embedding_text(
            {"name": "Bike", "file": "monobike.fbx"}
        ) == "Bike monobike.fbx"

    def test_catalog_entry_embedding_text_uses_keywords_and_filename_not_description(self):
        entry = {
            "filename": "tree_pineDefaultA.fbx",
            "category": "nature",
            "keywords": ["tree", "pine", "forest"],
            "description": "tree pine default a — nature model",  # deliberately ignored
        }
        text = sm.catalog_entry_embedding_text(entry)
        assert "description" not in text
        assert "tree" in text and "pine" in text and "nature" in text


# ── catalog_content_hash ─────────────────────────────────────────────────────

class TestCatalogContentHash:

    def _asset(self, relpath="a.fbx", keywords=("x",), source="kenney", **extra):
        return {"relpath": relpath, "filename": relpath, "category": "prop",
                "keywords": list(keywords), "source": source, **extra}

    def test_order_independent(self):
        a, b = self._asset("a.fbx"), self._asset("b.fbx")
        assert sm.catalog_content_hash([a, b]) == sm.catalog_content_hash([b, a])

    def test_changes_when_keywords_change(self):
        h1 = sm.catalog_content_hash([self._asset(keywords=("x",))])
        h2 = sm.catalog_content_hash([self._asset(keywords=("y",))])
        assert h1 != h2

    def test_ignores_fields_that_dont_feed_embedding_text(self):
        h1 = sm.catalog_content_hash([self._asset(bounds=[1, 2, 3], generic_score=0)])
        h2 = sm.catalog_content_hash([self._asset(bounds=[9, 9, 9], generic_score=20)])
        assert h1 == h2

    def test_excludes_sketchfab_entries(self):
        kenney_only = sm.catalog_content_hash([self._asset("a.fbx", source="kenney")])
        with_sketchfab = sm.catalog_content_hash([
            self._asset("a.fbx", source="kenney"),
            self._asset("b.fbx", source="sketchfab"),
        ])
        assert kenney_only == with_sketchfab


# ── build / write / load round-trip ─────────────────────────────────────────

class TestCatalogEmbeddingsRoundTrip:

    def test_build_write_load_round_trip(self, tmp_path, monkeypatch):
        _patch_bge(monkeypatch)
        assets = [
            {"relpath": "alpha.fbx", "filename": "alpha.fbx", "category": "prop",
             "keywords": [], "source": "kenney"},
            {"relpath": "beta.fbx", "filename": "beta.fbx", "category": "prop",
             "keywords": [], "source": "kenney"},
        ]
        # Filenames alone must land on the fixed VECTORS keys for this test's
        # fake to give useful (non-default) vectors — override the text
        # builder indirectly isn't worth it; instead confirm the round-trip
        # is faithful regardless of which vectors came back.
        ce = sm.build_catalog_embeddings(assets)
        assert ce.vectors.shape[0] == 2
        assert ce.keys == ("alpha.fbx", "beta.fbx")  # sorted by relpath

        sm.write_catalog_embeddings(ce, str(tmp_path))
        loaded = sm.load_catalog_embeddings(str(tmp_path))
        assert loaded is not None
        assert loaded.keys == ce.keys
        assert loaded.catalog_hash == ce.catalog_hash
        assert loaded.model == ce.model
        np.testing.assert_allclose(loaded.vectors, ce.vectors, atol=1e-6)

    def test_vectors_are_l2_normalized(self, monkeypatch):
        _patch_bge(monkeypatch)
        assets = [{"relpath": "alpha.fbx", "filename": "alpha.fbx", "category": "",
                   "keywords": [], "source": "kenney"}]
        ce = sm.build_catalog_embeddings(assets)
        norms = np.linalg.norm(ce.vectors, axis=1)
        np.testing.assert_allclose(norms, [1.0], atol=1e-6)

    def test_empty_catalog_produces_empty_matrix(self, monkeypatch):
        _patch_bge(monkeypatch)
        ce = sm.build_catalog_embeddings([])
        assert ce.vectors.shape[0] == 0
        assert ce.keys == ()


class TestLoadCatalogEmbeddingsDegradation:
    """Every failure mode degrades to None — never raises. sm.load_catalog_embeddings
    is @lru_cache'd, so each test uses its own tmp_path to avoid cross-test
    cache collisions, and clears the cache afterward for hygiene."""

    def teardown_method(self):
        sm.load_catalog_embeddings.cache_clear()

    def test_missing_files_returns_none(self, tmp_path):
        assert sm.load_catalog_embeddings(str(tmp_path)) is None

    def test_malformed_meta_json_returns_none(self, tmp_path):
        base = tmp_path / "unity"
        base.mkdir()
        (base / sm.CATALOG_EMBEDDINGS_META_FILENAME).write_text("{not valid json")
        assert sm.load_catalog_embeddings(str(tmp_path)) is None

    def test_wrong_model_name_returns_none(self, tmp_path, monkeypatch):
        _patch_bge(monkeypatch)
        ce = sm.build_catalog_embeddings(
            [{"relpath": "a.fbx", "filename": "a.fbx", "category": "", "keywords": [], "source": "kenney"}])
        sm.write_catalog_embeddings(ce, str(tmp_path))
        sm.load_catalog_embeddings.cache_clear()
        meta_path = tmp_path / "unity" / sm.CATALOG_EMBEDDINGS_META_FILENAME
        meta = json.loads(meta_path.read_text())
        meta["model"] = "some/other-model"
        meta_path.write_text(json.dumps(meta))
        assert sm.load_catalog_embeddings(str(tmp_path)) is None

    def test_shape_mismatch_returns_none(self, tmp_path, monkeypatch):
        _patch_bge(monkeypatch)
        ce = sm.build_catalog_embeddings(
            [{"relpath": "a.fbx", "filename": "a.fbx", "category": "", "keywords": [], "source": "kenney"}])
        sm.write_catalog_embeddings(ce, str(tmp_path))
        sm.load_catalog_embeddings.cache_clear()
        meta_path = tmp_path / "unity" / sm.CATALOG_EMBEDDINGS_META_FILENAME
        meta = json.loads(meta_path.read_text())
        meta["keys"] = ["a.fbx", "b.fbx"]  # now mismatched against a 1-row matrix
        meta_path.write_text(json.dumps(meta))
        assert sm.load_catalog_embeddings(str(tmp_path)) is None


# ── catalog_similarity_vector ────────────────────────────────────────────────

class TestCatalogSimilarityVector:

    def test_matches_manual_cosine(self):
        ce = sm.CatalogEmbeddings(
            vectors=np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
            keys=("a.fbx", "b.fbx"), catalog_hash="h", model="m")
        sims = sm.catalog_similarity_vector(ce, np.array([1.0, 0.0], dtype=np.float32))
        assert sims["a.fbx"] == pytest.approx(1.0)
        assert sims["b.fbx"] == pytest.approx(0.0)

    def test_zero_query_vector_returns_empty(self):
        ce = sm.CatalogEmbeddings(
            vectors=np.array([[1.0, 0.0]], dtype=np.float32),
            keys=("a.fbx",), catalog_hash="h", model="m")
        assert sm.catalog_similarity_vector(ce, np.array([0.0, 0.0], dtype=np.float32)) == {}


# ── embed_query_cached ───────────────────────────────────────────────────────

class TestEmbedQueryCached:

    def test_reuses_cached_result_for_identical_text(self, monkeypatch):
        calls = []

        class _CountingBge:
            def embed(self, texts):
                calls.extend(texts)
                return [[1.0, 0.0] for _ in texts]

        monkeypatch.setattr("src.unity_semantic_embed._bge_text_embedder",
                            lambda: _CountingBge())
        cache: dict = {}
        v1 = sm.embed_query_cached("alpha", cache)
        v2 = sm.embed_query_cached("alpha", cache)
        np.testing.assert_allclose(v1, v2)
        assert calls == ["alpha"]   # embedded once, not twice

    def test_empty_text_returns_none_without_calling_embedder(self, monkeypatch):
        def _boom():
            raise AssertionError("should never be called for empty text")
        monkeypatch.setattr("src.unity_semantic_embed._bge_text_embedder", _boom)
        assert sm.embed_query_cached("", {}) is None

    def test_embedder_failure_degrades_to_none(self, monkeypatch):
        monkeypatch.setattr("src.unity_semantic_embed._bge_text_embedder",
                            lambda: (_ for _ in ()).throw(RuntimeError("no model")))
        assert sm.embed_query_cached("alpha", {}) is None
