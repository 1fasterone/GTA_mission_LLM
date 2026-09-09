"""Tests for src/sketchfab_catalog_audit.py — the offline re-check of
already-frozen Sketchfab catalog entries against their own stored query.

Mocks the embedders and the Sketchfab client the same way
tests/test_unity_semantic_embed.py and tests/test_sketchfab_freeze.py do —
no real network, no real CLIP/embedding inference.
"""

import json
from pathlib import Path

from src import unity_semantic_embed as sem
from src.sketchfab_catalog_audit import (apply_removals, audit_catalog, audit_entry,
                                         backfill_embeddings)


class _FakeClient:
    def __init__(self, models=None, images=None):
        self._models = models or {}
        self._images = images or {}

    def get_model(self, uid):
        return self._models.get(uid, {})

    def fetch_image_bytes(self, url):
        return self._images.get(url)


def _model(uid, description="a description", thumb_url="https://x/y.jpg"):
    m = {"uid": uid, "description": description}
    if thumb_url:
        m["thumbnails"] = {"images": [{"url": thumb_url, "width": 256, "height": 256}]}
    return m


def _entry(uid="abc", query="dirt race vehicle", filename="truck-abc.fbx", **extra):
    return {"sketchfab_uid": uid, "query": query, "filename": filename, **extra}


# ── audit_entry ──────────────────────────────────────────────────────────────

def test_missing_uid_or_query_is_unmeasured_without_any_network_call():
    client = _FakeClient()
    assert audit_entry({"sketchfab_uid": "", "query": "x"}, client=client,
                       min_image_similarity=0.2, min_text_similarity=0.2)["verdict"] == "unmeasured"
    assert audit_entry({"sketchfab_uid": "x", "query": ""}, client=client,
                       min_image_similarity=0.2, min_text_similarity=0.2)["verdict"] == "unmeasured"


def test_model_no_longer_fetchable_is_unmeasured():
    """get_model returns {} — deleted/private model, no token, network down.
    Must not be flagged as a failure; there is no evidence either way."""
    client = _FakeClient(models={})  # get_model("abc") -> {}
    result = audit_entry(_entry(uid="abc"), client=client,
                         min_image_similarity=0.2, min_text_similarity=0.2)
    assert result["verdict"] == "unmeasured"
    assert result["image_similarity"] is None
    assert result["text_similarity"] is None


def test_entry_that_clears_both_floors_passes(monkeypatch):
    monkeypatch.setattr(sem, "image_similarity", lambda text, b: 0.9)
    monkeypatch.setattr(sem, "text_similarity", lambda a, b: 0.9)
    client = _FakeClient(
        models={"abc": _model("abc")},
        images={"https://x/y.jpg": b"bytes"})
    result = audit_entry(_entry(uid="abc"), client=client,
                         min_image_similarity=0.5, min_text_similarity=0.5)
    assert result == {"uid": "abc", "name": "truck-abc.fbx", "query": "dirt race vehicle",
                      "image_similarity": 0.9, "text_similarity": 0.9, "verdict": "pass",
                      "embedding": None}


def test_entry_that_passes_also_returns_the_image_embedding_as_a_byproduct(monkeypatch):
    """audit_entry already downloaded the thumbnail to score it — the same
    bytes can produce an embedding for backfill_embeddings to stamp into
    the catalog for free, no extra network call."""
    monkeypatch.setattr(sem, "image_similarity", lambda text, b: 0.9)
    monkeypatch.setattr(sem, "text_similarity", lambda a, b: 0.9)
    monkeypatch.setattr(sem, "image_embedding", lambda b: [1.0, 2.0] if b == b"bytes" else None)
    client = _FakeClient(models={"abc": _model("abc")}, images={"https://x/y.jpg": b"bytes"})
    result = audit_entry(_entry(uid="abc"), client=client,
                         min_image_similarity=0.5, min_text_similarity=0.5)
    assert result["embedding"] == [1.0, 2.0]


def test_unmeasurable_entry_returns_no_embedding():
    client = _FakeClient(models={})
    result = audit_entry(_entry(uid="abc"), client=client,
                         min_image_similarity=0.2, min_text_similarity=0.2)
    assert result["embedding"] is None


def test_entry_that_misses_the_image_floor_fails(monkeypatch):
    """Confirmed live regression shape: a real, licensed, on-topic-sounding
    model whose actual picture doesn't match the query it was frozen under."""
    monkeypatch.setattr(sem, "image_similarity", lambda text, b: 0.10)
    monkeypatch.setattr(sem, "text_similarity", lambda a, b: 0.9)
    client = _FakeClient(
        models={"abc": _model("abc")},
        images={"https://x/y.jpg": b"bytes"})
    result = audit_entry(_entry(uid="abc"), client=client,
                         min_image_similarity=0.22, min_text_similarity=0.45)
    assert result["verdict"] == "fail"


def test_entry_with_no_thumbnail_but_good_text_never_blocks_on_the_unmeasured_axis(monkeypatch):
    """Only description is measurable (no thumbnail) — text alone clearing
    its floor is enough to pass, same as semantic_filter's own per-axis
    contract: an axis that couldn't be measured never blocks on its own."""
    monkeypatch.setattr(sem, "text_similarity", lambda a, b: 0.9)
    client = _FakeClient(models={"abc": _model("abc", thumb_url=None)})
    result = audit_entry(_entry(uid="abc"), client=client,
                         min_image_similarity=0.9, min_text_similarity=0.5)
    assert result["image_similarity"] is None
    assert result["verdict"] == "pass"


def test_falls_back_to_frozen_description_when_live_model_has_none(monkeypatch):
    """The re-fetched model has an empty description but the ORIGINAL frozen
    entry stored one at freeze time — that stored copy is still usable."""
    captured = {}

    def _txt(a, b):
        captured["b"] = b
        return 0.9

    monkeypatch.setattr(sem, "text_similarity", _txt)
    client = _FakeClient(models={"abc": _model("abc", description="", thumb_url=None)})
    audit_entry(_entry(uid="abc", description="a frozen description"), client=client,
               min_image_similarity=0.2, min_text_similarity=0.5)
    assert captured["b"] == "a frozen description"


# ── audit_catalog ────────────────────────────────────────────────────────────

def _write_catalog(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "entries": entries}), encoding="utf-8")


def test_audit_catalog_missing_file_returns_empty_list(tmp_path):
    assert audit_catalog(tmp_path / "nope.json", client=_FakeClient()) == []


def test_audit_catalog_runs_every_entry_and_reports_progress(monkeypatch, tmp_path):
    monkeypatch.setattr(sem, "image_similarity", lambda text, b: 0.9)
    monkeypatch.setattr(sem, "text_similarity", lambda a, b: 0.9)
    path = tmp_path / "sketchfab_catalog.json"
    _write_catalog(path, [_entry(uid="a"), _entry(uid="b")])
    client = _FakeClient(
        models={"a": _model("a"), "b": _model("b")},
        images={"https://x/y.jpg": b"bytes"})

    seen = []
    results = audit_catalog(path, client=client, min_image_similarity=0.5,
                            min_text_similarity=0.5, sleep_between=0,
                            progress_fn=lambda i, n, r: seen.append((i, n, r["uid"])))

    assert [r["uid"] for r in results] == ["a", "b"]
    assert all(r["verdict"] == "pass" for r in results)
    assert seen == [(1, 2, "a"), (2, 2, "b")]


def test_audit_catalog_limit_only_checks_the_first_n(monkeypatch, tmp_path):
    monkeypatch.setattr(sem, "image_similarity", lambda text, b: 0.9)
    monkeypatch.setattr(sem, "text_similarity", lambda a, b: 0.9)
    path = tmp_path / "sketchfab_catalog.json"
    _write_catalog(path, [_entry(uid="a"), _entry(uid="b"), _entry(uid="c")])
    client = _FakeClient(
        models={u: _model(u) for u in "abc"},
        images={"https://x/y.jpg": b"bytes"})

    results = audit_catalog(path, client=client, limit=1, sleep_between=0)
    assert [r["uid"] for r in results] == ["a"]


def test_audit_catalog_offset_and_limit_select_a_chunk(monkeypatch, tmp_path):
    """A large catalog can be worked through in bounded chunks across
    several runs — offset skips, limit then bounds what's left."""
    monkeypatch.setattr(sem, "image_similarity", lambda text, b: 0.9)
    monkeypatch.setattr(sem, "text_similarity", lambda a, b: 0.9)
    path = tmp_path / "sketchfab_catalog.json"
    _write_catalog(path, [_entry(uid=u) for u in "abcd"])
    client = _FakeClient(
        models={u: _model(u) for u in "abcd"},
        images={"https://x/y.jpg": b"bytes"})

    results = audit_catalog(path, client=client, offset=1, limit=2, sleep_between=0)
    assert [r["uid"] for r in results] == ["b", "c"]


def test_audit_catalog_offset_past_the_end_returns_empty(monkeypatch, tmp_path):
    path = tmp_path / "sketchfab_catalog.json"
    _write_catalog(path, [_entry(uid="a")])
    results = audit_catalog(path, client=_FakeClient(), offset=5, sleep_between=0)
    assert results == []


def test_audit_catalog_never_writes_the_file(monkeypatch, tmp_path):
    monkeypatch.setattr(sem, "image_similarity", lambda text, b: 0.05)
    monkeypatch.setattr(sem, "text_similarity", lambda a, b: 0.05)
    path = tmp_path / "sketchfab_catalog.json"
    _write_catalog(path, [_entry(uid="a")])
    before = path.read_text(encoding="utf-8")
    client = _FakeClient(models={"a": _model("a")}, images={"https://x/y.jpg": b"bytes"})

    audit_catalog(path, client=client, min_image_similarity=0.9,
                 min_text_similarity=0.9, sleep_between=0)
    assert path.read_text(encoding="utf-8") == before


# ── apply_removals ───────────────────────────────────────────────────────────

def test_apply_removals_empty_set_is_a_true_noop(tmp_path):
    path = tmp_path / "sketchfab_catalog.json"
    _write_catalog(path, [_entry(uid="a")])
    before = path.read_text(encoding="utf-8")
    result = apply_removals(path, set())
    assert result == {"removed": 0, "kept": 0, "backup_path": None}
    assert path.read_text(encoding="utf-8") == before


def test_apply_removals_drops_only_named_uids(tmp_path):
    path = tmp_path / "sketchfab_catalog.json"
    _write_catalog(path, [_entry(uid="a"), _entry(uid="b"), _entry(uid="c")])

    result = apply_removals(path, {"b"})

    assert result["removed"] == 1
    assert result["kept"] == 2
    data = json.loads(path.read_text(encoding="utf-8"))
    assert sorted(e["sketchfab_uid"] for e in data["entries"]) == ["a", "c"]


def test_apply_removals_writes_a_backup_by_default(tmp_path):
    path = tmp_path / "sketchfab_catalog.json"
    _write_catalog(path, [_entry(uid="a"), _entry(uid="b")])
    original = path.read_text(encoding="utf-8")

    result = apply_removals(path, {"a"})

    backup_path = result["backup_path"]
    assert backup_path is not None
    assert Path(backup_path).read_text(encoding="utf-8") == original


def test_apply_removals_backup_false_writes_no_backup(tmp_path):
    path = tmp_path / "sketchfab_catalog.json"
    _write_catalog(path, [_entry(uid="a"), _entry(uid="b")])
    result = apply_removals(path, {"a"}, backup=False)
    assert result["backup_path"] is None


def test_apply_removals_preserves_uids_added_after_the_audit_snapshot(tmp_path):
    """The core race-safety property: apply_removals re-reads the file FRESH
    at write time. An entry frozen by a concurrent pipeline run AFTER the
    audit's own read (and therefore never scored at all) must survive —
    only the explicitly-named uids are ever removed."""
    path = tmp_path / "sketchfab_catalog.json"
    _write_catalog(path, [_entry(uid="a"), _entry(uid="b")])

    # Simulate a concurrent fetch_and_freeze call landing a brand-new entry
    # in between the audit run (which produced the failed-uid set below) and
    # this apply_removals call.
    data = json.loads(path.read_text(encoding="utf-8"))
    data["entries"].append(_entry(uid="new-concurrent"))
    path.write_text(json.dumps(data), encoding="utf-8")

    result = apply_removals(path, {"a"})

    data_after = json.loads(path.read_text(encoding="utf-8"))
    uids = {e["sketchfab_uid"] for e in data_after["entries"]}
    assert uids == {"b", "new-concurrent"}
    assert result["removed"] == 1
    assert result["kept"] == 2


def test_apply_removals_missing_file_is_a_noop(tmp_path):
    result = apply_removals(tmp_path / "nope.json", {"a"})
    assert result == {"removed": 0, "kept": 0, "backup_path": None}


# ── backfill_embeddings ──────────────────────────────────────────────────────

def test_backfill_embeddings_stamps_only_named_uids(tmp_path):
    path = tmp_path / "sketchfab_catalog.json"
    _write_catalog(path, [_entry(uid="a"), _entry(uid="b"), _entry(uid="c")])

    result = backfill_embeddings(path, {"a": [1.0, 2.0]})

    assert result == {"updated": 1}
    data = json.loads(path.read_text(encoding="utf-8"))
    by_uid = {e["sketchfab_uid"]: e for e in data["entries"]}
    assert by_uid["a"]["clip_image_embedding"] == [1.0, 2.0]
    assert "clip_image_embedding" not in by_uid["b"]
    assert "clip_image_embedding" not in by_uid["c"]


def test_backfill_embeddings_never_overwrites_an_existing_embedding(tmp_path):
    """An entry already carrying an embedding (stamped at freeze time by
    sketchfab_freeze.py itself) must keep ITS OWN embedding, never be
    silently replaced by a re-audit's freshly computed one."""
    path = tmp_path / "sketchfab_catalog.json"
    _write_catalog(path, [_entry(uid="a", clip_image_embedding=[9.0, 9.0])])

    result = backfill_embeddings(path, {"a": [1.0, 2.0]})

    assert result == {"updated": 0}
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["entries"][0]["clip_image_embedding"] == [9.0, 9.0]


def test_backfill_embeddings_empty_dict_is_a_true_noop(tmp_path):
    path = tmp_path / "sketchfab_catalog.json"
    _write_catalog(path, [_entry(uid="a")])
    before = path.read_text(encoding="utf-8")
    result = backfill_embeddings(path, {})
    assert result == {"updated": 0}
    assert path.read_text(encoding="utf-8") == before


def test_backfill_embeddings_missing_file_is_a_noop(tmp_path):
    result = backfill_embeddings(tmp_path / "nope.json", {"a": [1.0]})
    assert result == {"updated": 0}


def test_backfill_embeddings_ignores_uids_it_was_never_told_about(tmp_path):
    """A uid frozen by a concurrent pipeline run after this audit started
    (or simply never scored) has no entry in embeddings_by_uid at all —
    left exactly as it was, same race-safety property as apply_removals."""
    path = tmp_path / "sketchfab_catalog.json"
    _write_catalog(path, [_entry(uid="a"), _entry(uid="concurrent-new")])

    result = backfill_embeddings(path, {"a": [1.0, 2.0]})

    assert result == {"updated": 1}
    data = json.loads(path.read_text(encoding="utf-8"))
    by_uid = {e["sketchfab_uid"]: e for e in data["entries"]}
    assert "clip_image_embedding" not in by_uid["concurrent-new"]
