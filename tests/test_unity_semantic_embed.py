"""Tests for src/unity_semantic_embed.py — the deterministic CLIP/embedding
semantic gate. Every test mocks the embedders (no real model download, no
network, no torch/onnxruntime inference) so this suite runs the same in CI
as everywhere else — mirrors the existing "mock the client + Blender
conversion" precedent in tests/test_sketchfab_freeze.py.
"""

import pytest

from src import unity_semantic_embed as sem


# ── _cosine ───────────────────────────────────────────────────────────────

def test_cosine_identical_vectors_is_one():
    assert sem._cosine([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_vectors_is_zero():
    assert sem._cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_opposite_vectors_is_minus_one():
    assert sem._cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_zero_vector_never_divides_by_zero():
    assert sem._cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


# ── image_similarity / text_similarity: degrade to None, never raise ──────

def test_image_similarity_empty_text_returns_none_without_loading_a_model():
    assert sem.image_similarity("", b"fakebytes") is None
    assert sem.image_similarity("a cone", b"") is None


def test_text_similarity_empty_input_returns_none():
    assert sem.text_similarity("", "something") is None
    assert sem.text_similarity("something", "") is None


def test_image_similarity_embedder_failure_degrades_to_none(monkeypatch):
    def _boom():
        raise RuntimeError("no cached CLIP model, no network")
    monkeypatch.setattr(sem, "_clip_text_embedder", _boom)
    assert sem.image_similarity("a cone", b"fakebytes") is None


def test_text_similarity_embedder_failure_degrades_to_none(monkeypatch):
    def _boom():
        raise RuntimeError("no cached bge model, no network")
    monkeypatch.setattr(sem, "_bge_text_embedder", _boom)
    assert sem.text_similarity("a cone", "another cone") is None


def test_image_similarity_computes_cosine_of_mocked_embeddings(monkeypatch):
    class _FakeClipText:
        def embed(self, texts):
            return [[1.0, 0.0]]

    class _FakeClipImage:
        def embed(self, images):
            return [[1.0, 0.0]]

    monkeypatch.setattr(sem, "_clip_text_embedder", lambda: _FakeClipText())
    monkeypatch.setattr(sem, "_clip_image_embedder", lambda: _FakeClipImage())
    # A real 1x1 PNG so PIL.Image.open succeeds without a real thumbnail.
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buf, format="PNG")
    result = sem.image_similarity("a race marker cone", buf.getvalue())
    assert result == pytest.approx(1.0)


def test_text_similarity_computes_cosine_of_mocked_embeddings(monkeypatch):
    class _FakeBge:
        def embed(self, texts):
            return [[0.0, 1.0] for _ in texts]

    monkeypatch.setattr(sem, "_bge_text_embedder", lambda: _FakeBge())
    result = sem.text_similarity("a cone", "a different cone")
    assert result == pytest.approx(1.0)


# ── semantic_filter: the resolver-facing gate ──────────────────────────────

class _FakeClient:
    """Only fetch_image_bytes is exercised by semantic_filter itself."""

    def __init__(self, thumbnail_bytes=b"thumb"):
        self._bytes = thumbnail_bytes

    def fetch_image_bytes(self, url):
        return self._bytes


def _model(uid, name="Some Model", description="a description", thumb_url="https://x/y.jpg"):
    m = {"uid": uid, "name": name, "description": description}
    if thumb_url:
        m["thumbnails"] = {"images": [{"url": thumb_url, "width": 256, "height": 256}]}
    return m


def test_semantic_filter_keeps_and_orders_by_image_similarity(monkeypatch):
    scores = {"good": (0.9, 0.9), "bad": (0.5, 0.5)}
    monkeypatch.setattr(sem, "image_similarity",
                        lambda text, b: scores[_uid_from_bytes(b)][0])
    monkeypatch.setattr(sem, "text_similarity",
                        lambda a, b: 0.9)

    def _uid_from_bytes(b):
        return b.decode()

    monkeypatch.setattr(
        "src.unity_sketchfab_vision._thumbnail_url",
        lambda m: m["uid"])  # identity: thumbnail "url" IS the uid string

    client = _FakeClient()
    # Override fetch_image_bytes to hand back the uid itself as "bytes" so
    # the fake image_similarity above can tell candidates apart.
    client.fetch_image_bytes = lambda url: url.encode()

    candidates = [_model("bad"), _model("good")]
    result = sem.semantic_filter(
        "Cone", "prop", "a race marker cone", candidates, client=client,
        min_image_similarity=0.2, min_text_similarity=0.2)

    assert [m["uid"] for m in result] == ["good", "bad"]


def test_semantic_filter_rejects_everything_below_floor_returns_empty(monkeypatch):
    monkeypatch.setattr(sem, "image_similarity", lambda text, b: 0.05)
    monkeypatch.setattr(sem, "text_similarity", lambda a, b: 0.05)
    monkeypatch.setattr("src.unity_sketchfab_vision._thumbnail_url",
                        lambda m: "https://x/y.jpg")

    candidates = [_model("cartoon-character", description="a stylized character")]
    result = sem.semantic_filter(
        "PlayerCharacter", "character", "realistic paratrooper", candidates,
        client=_FakeClient(), min_image_similarity=0.5, min_text_similarity=0.5)

    assert result == []


def test_semantic_filter_no_measurement_at_all_returns_original_list_untouched(monkeypatch):
    """No thumbnail URL and no description anywhere in the pool — the
    embedders were never even asked to score anything. This is an
    embedder-availability/data-availability gap, not a semantic verdict, so
    the original candidate order must survive exactly as vision_select_fn
    returning None would leave it."""
    monkeypatch.setattr("src.unity_sketchfab_vision._thumbnail_url", lambda m: None)

    candidates = [_model("a", name="", description=""), _model("b", name="", description="")]
    for m in candidates:
        m.pop("thumbnails", None)
    result = sem.semantic_filter(
        "Cone", "prop", "a race marker cone", candidates, client=_FakeClient(),
        min_image_similarity=0.2, min_text_similarity=0.2)

    assert result is candidates or result == candidates


def test_semantic_filter_unscored_candidate_is_appended_not_dropped(monkeypatch):
    """One candidate is measurable and passes; a second has no thumbnail and
    no description (unmeasurable) — it must survive at the tail, never be
    silently discarded."""
    def _thumb(m):
        return "https://x/y.jpg" if m["uid"] == "measurable" else None

    monkeypatch.setattr("src.unity_sketchfab_vision._thumbnail_url", _thumb)
    monkeypatch.setattr(sem, "image_similarity", lambda text, b: 0.9)
    monkeypatch.setattr(sem, "text_similarity", lambda a, b: 0.9)

    candidates = [
        _model("unmeasurable", name="", description=""),
        _model("measurable", description="a matching description"),
    ]
    candidates[0].pop("thumbnails", None)

    result = sem.semantic_filter(
        "Cone", "prop", "a race marker cone", candidates, client=_FakeClient(),
        min_image_similarity=0.2, min_text_similarity=0.2)

    assert [m["uid"] for m in result] == ["measurable", "unmeasurable"]


def test_semantic_filter_exception_in_embedder_never_raises(monkeypatch):
    """semantic_filter itself must not raise even if the embedders blow up
    mid-pool — the caller (sketchfab_freeze) also wraps this in try/except,
    but the function should be safe standalone too since resolve_entity_
    assets never wraps the closure it builds."""
    def _boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(sem, "image_similarity", _boom)
    monkeypatch.setattr(sem, "text_similarity", _boom)
    monkeypatch.setattr("src.unity_sketchfab_vision._thumbnail_url",
                        lambda m: "https://x/y.jpg")

    candidates = [_model("a")]
    with pytest.raises(RuntimeError):
        # NOTE: semantic_filter does not itself catch embedder exceptions —
        # that is sketchfab_freeze._fetch_and_freeze_inner's job (matching
        # vision_select_fn's own contract, where the exception boundary is
        # the caller, not select_best_candidate itself). This test documents
        # that boundary explicitly rather than assuming it.
        sem.semantic_filter("Cone", "prop", "a race marker cone", candidates,
                            client=_FakeClient(), min_image_similarity=0.2,
                            min_text_similarity=0.2)


def test_semantic_filter_only_scores_the_first_max_candidates(monkeypatch):
    """Position 6 (0-indexed) would score best if measured — with
    max_candidates=6 it must never even be scored, confirming the slicing
    genuinely excludes it rather than merely ranking it last. No existing
    test in this file exercises more than 1-2 candidates, so this slicing
    behavior itself had no direct coverage before."""
    monkeypatch.setattr("src.unity_sketchfab_vision._thumbnail_url",
                        lambda m: "https://x/y.jpg")
    scored = []

    def _img_sim(text, b):
        scored.append(True)
        return 0.9

    monkeypatch.setattr(sem, "image_similarity", _img_sim)
    monkeypatch.setattr(sem, "text_similarity", lambda a, b: 0.9)

    candidates = [_model(str(i)) for i in range(8)]
    result = sem.semantic_filter(
        "Cone", "prop", "a race marker cone", candidates, client=_FakeClient(),
        min_image_similarity=0.2, min_text_similarity=0.2, max_candidates=6)

    assert len(scored) == 6  # candidates 6 and 7 were never scored at all
    assert "6" not in [m["uid"] for m in result]
    assert "7" not in [m["uid"] for m in result]


# ── image_embedding / image_similarity_from_embedding: the cache-hit path ──

def test_image_embedding_empty_bytes_returns_none():
    assert sem.image_embedding(b"") is None
    assert sem.image_embedding(None) is None


def test_image_embedding_returns_plain_float_list(monkeypatch):
    class _FakeClipImage:
        def embed(self, images):
            return [[1.0, 0.5]]
    monkeypatch.setattr(sem, "_clip_image_embedder", lambda: _FakeClipImage())
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buf, format="PNG")
    result = sem.image_embedding(buf.getvalue())
    assert result == [1.0, 0.5]
    assert all(isinstance(v, float) for v in result)


def test_image_embedding_embedder_failure_degrades_to_none(monkeypatch):
    def _boom():
        raise RuntimeError("no cached CLIP model, no network")
    monkeypatch.setattr(sem, "_clip_image_embedder", _boom)
    assert sem.image_embedding(b"fakebytes") is None


def test_image_similarity_from_embedding_empty_input_returns_none():
    assert sem.image_similarity_from_embedding("", [1.0, 0.0]) is None
    assert sem.image_similarity_from_embedding("a cone", []) is None
    assert sem.image_similarity_from_embedding("a cone", None) is None


def test_image_similarity_from_embedding_computes_cosine(monkeypatch):
    class _FakeClipText:
        def embed(self, texts):
            return [[1.0, 0.0]]
    monkeypatch.setattr(sem, "_clip_text_embedder", lambda: _FakeClipText())
    result = sem.image_similarity_from_embedding("a race marker cone", [1.0, 0.0])
    assert result == pytest.approx(1.0)


def test_image_similarity_from_embedding_no_network_call(monkeypatch):
    """The whole point: no client, no thumbnail fetch — only the stored
    embedding and a freshly computed text embedding."""
    monkeypatch.setattr(sem, "_clip_image_embedder",
                        lambda: (_ for _ in ()).throw(AssertionError(
                            "must never load the image embedder")))
    class _FakeClipText:
        def embed(self, texts):
            return [[0.0, 1.0]]
    monkeypatch.setattr(sem, "_clip_text_embedder", lambda: _FakeClipText())
    result = sem.image_similarity_from_embedding("a cone", [0.0, 1.0])
    assert result == pytest.approx(1.0)


def test_image_similarity_from_embedding_failure_degrades_to_none(monkeypatch):
    def _boom():
        raise RuntimeError("boom")
    monkeypatch.setattr(sem, "_clip_text_embedder", _boom)
    assert sem.image_similarity_from_embedding("a cone", [1.0, 0.0]) is None


# ── semantic_filter_frozen: the deterministic, network-free cache-hit gate ──

def _frozen_entry(uid, description="a description", embedding=None,
                  filename="model.fbx"):
    e = {"sketchfab_uid": uid, "description": description, "filename": filename}
    if embedding is not None:
        e["clip_image_embedding"] = embedding
    return e


def test_semantic_filter_frozen_never_touches_the_network(monkeypatch):
    """No client parameter exists at all — confirm neither embedder call
    path can reach a network-backed thumbnail fetch by construction (there
    is no client to fetch one from)."""
    monkeypatch.setattr(sem, "image_similarity_from_embedding", lambda t, e: 0.9)
    monkeypatch.setattr(sem, "text_similarity", lambda a, b: 0.9)
    candidates = [_frozen_entry("a", embedding=[1.0, 0.0])]
    result = sem.semantic_filter_frozen(
        "PlayerTruck", "vehicle", "dirt race vehicle", candidates,
        min_image_similarity=0.2, min_text_similarity=0.2)
    assert [e["sketchfab_uid"] for e in result] == ["a"]


def test_semantic_filter_frozen_keeps_and_orders_by_image_similarity(monkeypatch):
    scores = {(0.5,): 0.5, (0.9,): 0.9}
    monkeypatch.setattr(sem, "image_similarity_from_embedding",
                        lambda t, emb: scores[tuple(emb)])
    monkeypatch.setattr(sem, "text_similarity", lambda a, b: 0.9)
    candidates = [_frozen_entry("bad", embedding=(0.5,)),
                 _frozen_entry("good", embedding=(0.9,))]

    result = sem.semantic_filter_frozen(
        "Cone", "prop", "a race marker cone", candidates,
        min_image_similarity=0.2, min_text_similarity=0.2)
    assert [e["sketchfab_uid"] for e in result] == ["good", "bad"]


def test_semantic_filter_frozen_only_scores_the_first_max_candidates(monkeypatch):
    """Same slicing contract as semantic_filter itself — confirmed
    separately since semantic_filter_frozen has its own independent
    `pool = candidates[:max(1, max_candidates)]` line, not shared code."""
    scored = []

    def _img_sim(text, emb):
        scored.append(True)
        return 0.9

    monkeypatch.setattr(sem, "image_similarity_from_embedding", _img_sim)
    monkeypatch.setattr(sem, "text_similarity", lambda a, b: 0.9)

    candidates = [_frozen_entry(str(i), embedding=[1.0, 0.0]) for i in range(8)]
    result = sem.semantic_filter_frozen(
        "Cone", "prop", "a race marker cone", candidates,
        min_image_similarity=0.2, min_text_similarity=0.2, max_candidates=6)

    assert len(scored) == 6
    assert "6" not in [e["sketchfab_uid"] for e in result]
    assert "7" not in [e["sketchfab_uid"] for e in result]


def test_semantic_filter_frozen_missing_embedding_only_checks_text(monkeypatch):
    """An entry frozen before this gate existed has no clip_image_embedding
    at all — the image axis is simply unmeasured, never blocking, same
    contract as semantic_filter's missing-thumbnail case."""
    monkeypatch.setattr(sem, "text_similarity", lambda a, b: 0.9)
    candidates = [_frozen_entry("no-embedding", embedding=None)]
    result = sem.semantic_filter_frozen(
        "Cone", "prop", "a race marker cone", candidates,
        min_image_similarity=0.99, min_text_similarity=0.5)
    assert [e["sketchfab_uid"] for e in result] == ["no-embedding"]


def test_semantic_filter_frozen_rejects_everything_below_floor_returns_empty(monkeypatch):
    monkeypatch.setattr(sem, "image_similarity_from_embedding", lambda t, e: 0.05)
    monkeypatch.setattr(sem, "text_similarity", lambda a, b: 0.05)
    candidates = [_frozen_entry("cartoon-character", embedding=[1.0, 0.0],
                                description="a stylized character")]
    result = sem.semantic_filter_frozen(
        "PlayerCharacter", "character", "realistic paratrooper", candidates,
        min_image_similarity=0.5, min_text_similarity=0.5)
    assert result == []


def test_semantic_filter_frozen_no_measurement_returns_original_list_untouched():
    """Neither description nor embedding on the candidate at all — an
    embedder-availability/data-availability gap, not a semantic verdict."""
    candidates = [_frozen_entry("a", description="", embedding=None)]
    result = sem.semantic_filter_frozen(
        "Cone", "prop", "a race marker cone", candidates,
        min_image_similarity=0.2, min_text_similarity=0.2)
    assert result is candidates or result == candidates


def test_semantic_filter_frozen_unscored_candidate_is_appended_not_dropped(monkeypatch):
    monkeypatch.setattr(sem, "text_similarity",
                        lambda a, b: 0.9 if b == "matches" else None)
    candidates = [_frozen_entry("unmeasurable", description="", embedding=None),
                 _frozen_entry("measurable", description="matches", embedding=None)]

    result = sem.semantic_filter_frozen(
        "Cone", "prop", "a race marker cone", candidates,
        min_image_similarity=0.2, min_text_similarity=0.2)

    assert [e["sketchfab_uid"] for e in result] == ["measurable", "unmeasurable"]
