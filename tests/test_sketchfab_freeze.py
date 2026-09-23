"""Tests for the fetch-and-freeze orchestration (src/sketchfab_freeze.py).

This is the ONE place a live Sketchfab search/download/convert is allowed to
run — every test here mocks the client + the Blender conversion step, so no
test requires a real SKETCHFAB_API_TOKEN or a real Blender install (mirrors
the existing "STUBBED if Blender isn't found" CI precedent for
src/blender/job_manager.py).
"""

import json

from src.sketchfab_freeze import fetch_and_freeze
from src.unity_asset_catalog import SKETCHFAB_RELPATH
from src.unity_sketchfab_vision import VisionSelection


class _FakeClient:
    """Canned SketchfabClient stand-in — no network."""

    def __init__(self, results=None, download_ok=True, author_results=None):
        self._results = results if results is not None else [
            {"uid": "abc123", "name": "Red High Heel Shoe",
             "description": "a shiny red shoe",
             "viewerUrl": "https://sketchfab.com/3d-models/abc123",
             "license": {"slug": "by"},
             "user": {"displayName": "Jane Doe", "username": "janedoe",
                     "profileUrl": "https://sketchfab.com/janedoe"},
             "tags": [{"name": "shoe"}, {"name": "fashion"}]},
        ]
        # Results returned ONLY when search() is called with a matching
        # `user=` filter — simulates the author-scoped catalog.
        self._author_results = author_results
        self._download_ok = download_ok
        self.download_calls = 0
        self.search_calls = []

    def available(self):
        return True

    def search(self, query, count=24, user=None):
        self.search_calls.append({"query": query, "user": user})
        if user is not None:
            return self._author_results or []
        return self._results

    def license_slug(self, model):
        return (model.get("license") or {}).get("slug")

    def filter_licensed(self, results):
        return [m for m in results if self.license_slug(m) in
               {"cc0", "by", "by-sa", "by-nd"}]

    def download_archive(self, uid, dest_zip):
        self.download_calls += 1
        if not self._download_ok:
            return None
        # A minimal valid zip containing a fake .gltf file.
        import zipfile
        dest_zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest_zip, "w") as zf:
            zf.writestr("scene.gltf", "{}")
        return dest_zip

    def fetch_image_bytes(self, url):
        # Unused by every test above (none of their candidates carry a
        # "thumbnails" field, so _thumbnail_url short-circuits to None
        # before this is ever called) — only the embedding-capture tests
        # below exercise it.
        return None


def _score_all_equal(entry: dict) -> float:
    return 1.0


def test_no_token_returns_none(tmp_path):
    class _NoToken:
        def available(self):
            return False

    result = fetch_and_freeze(
        "red shoe", _score_all_equal, client=_NoToken(),
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"))
    assert result is None


def test_no_search_results_returns_none(tmp_path):
    client = _FakeClient(results=[])
    result = fetch_and_freeze(
        "red shoe", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"))
    assert result is None


def test_no_blender_returns_none_clean_miss(tmp_path, monkeypatch):
    """CI has no Blender installed — convert_gltf_to_fbx returns None, and
    that must propagate as a clean None, not an exception."""
    monkeypatch.setattr(
        "src.blender.sketchfab_convert.convert_gltf_to_fbx", lambda *a, **kw: None)
    client = _FakeClient()
    result = fetch_and_freeze(
        "red high heel shoe", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"))
    assert result is None
    assert client.download_calls == 1


def test_download_failure_returns_none(tmp_path):
    client = _FakeClient(download_ok=False)
    result = fetch_and_freeze(
        "red high heel shoe", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"))
    assert result is None


def test_successful_fetch_freezes_entry(tmp_path, monkeypatch):
    def _fake_convert(gltf_path, fbx_out, textures_dir, *, blender_cfg=None, **_kw):
        fbx_out.parent.mkdir(parents=True, exist_ok=True)
        fbx_out.write_bytes(b"FBX-DUMMY")
        return fbx_out

    monkeypatch.setattr(
        "src.blender.sketchfab_convert.convert_gltf_to_fbx", _fake_convert)

    client = _FakeClient()
    lookups = tmp_path / "lookups"
    cache_dir = tmp_path / "cache"
    entry = fetch_and_freeze(
        "red high heel shoe", _score_all_equal, client=client,
        cache_dir=cache_dir, lookups_dir=str(lookups),
        category="prop", kind_hints=["prop"])

    assert entry is not None
    assert entry["source"] == "sketchfab"
    assert entry["sketchfab_uid"] == "abc123"
    assert entry["license"] == "by"
    assert entry["author"] == "Jane Doe"
    assert entry["author_username"] == "janedoe"
    assert "CC-BY" in entry["attribution"]
    # Deterministic path derived from the model uid, not a timestamp.
    assert "abc123" in entry["filename"]
    assert (cache_dir / entry["relpath"]).is_file()

    on_disk = json.loads((lookups / SKETCHFAB_RELPATH).read_text(encoding="utf-8"))
    assert on_disk["entries"][0]["sketchfab_uid"] == "abc123"


def test_convert_diagnostics_round_trip_into_entry(tmp_path, monkeypatch):
    """Convert-time size diagnostics (size_after / scale_corrected from the
    .convert.json sidecar) must be persisted into the frozen catalog entry as
    bbox_meters / scale_corrected — greppable/testable scale sanity without
    re-opening Blender."""
    def _fake_convert(gltf_path, fbx_out, textures_dir, *, blender_cfg=None, **_kw):
        fbx_out.parent.mkdir(parents=True, exist_ok=True)
        fbx_out.write_bytes(b"FBX-DUMMY")
        fbx_out.with_suffix(".convert.json").write_text(json.dumps({
            "ok": True, "textures": 0,
            "scale_corrected": 0.0132, "largest_dim_before": 341.2,
            "size_after": [4.5, 1.9, 1.4],
        }), encoding="utf-8")
        return fbx_out

    monkeypatch.setattr(
        "src.blender.sketchfab_convert.convert_gltf_to_fbx", _fake_convert)

    entry = fetch_and_freeze(
        "red high heel shoe", _score_all_equal, client=_FakeClient(),
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"),
        category="prop", kind_hints=["prop"])

    assert entry is not None
    assert entry["bbox_meters"] == [4.5, 1.9, 1.4]
    assert entry["scale_corrected"] == 0.0132


def test_missing_convert_sidecar_leaves_entry_clean(tmp_path, monkeypatch):
    """No .convert.json (older cache, stubbed Blender) → no bbox keys at all,
    not bogus/None-filled ones."""
    def _fake_convert(gltf_path, fbx_out, textures_dir, *, blender_cfg=None, **_kw):
        fbx_out.parent.mkdir(parents=True, exist_ok=True)
        fbx_out.write_bytes(b"FBX-DUMMY")
        return fbx_out

    monkeypatch.setattr(
        "src.blender.sketchfab_convert.convert_gltf_to_fbx", _fake_convert)

    entry = fetch_and_freeze(
        "red high heel shoe", _score_all_equal, client=_FakeClient(),
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"),
        category="prop", kind_hints=["prop"])

    assert entry is not None
    assert "bbox_meters" not in entry
    assert "scale_corrected" not in entry


def test_below_min_score_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.blender.sketchfab_convert.convert_gltf_to_fbx", lambda *a, **kw: None)
    client = _FakeClient()
    result = fetch_and_freeze(
        "red high heel shoe", lambda entry: 0.0, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"),
        min_score=0.5)
    assert result is None


def test_never_raises_on_internal_error(tmp_path):
    """fetch_and_freeze must swallow ANY exception, not just the expected
    failure modes — the resolver's Tier S has zero exception handling of its
    own around this call by design."""
    class _Explodes:
        def available(self):
            raise RuntimeError("boom")

    result = fetch_and_freeze(
        "red shoe", _score_all_equal, client=_Explodes(),
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"))
    assert result is None


# ── style-coherence: author-affinity biasing ──────────────────────────────────

def _install_fake_convert(monkeypatch):
    def _fake_convert(gltf_path, fbx_out, textures_dir, *, blender_cfg=None, **_kw):
        fbx_out.parent.mkdir(parents=True, exist_ok=True)
        fbx_out.write_bytes(b"FBX-DUMMY")
        return fbx_out
    monkeypatch.setattr(
        "src.blender.sketchfab_convert.convert_gltf_to_fbx", _fake_convert)


def test_preferred_author_tried_before_global_search(tmp_path, monkeypatch):
    """When preferred_author is set and that author has a real match, the
    global marketplace search must never even run — style coherence wins
    outright, not as a tiebreak."""
    _install_fake_convert(monkeypatch)
    author_hit = {"uid": "sword1", "name": "Stylized Longsword",
                 "viewerUrl": "https://sketchfab.com/3d-models/sword1",
                 "license": {"slug": "by"},
                 "user": {"displayName": "OptiCube", "username": "OptiCube",
                         "profileUrl": "https://sketchfab.com/OptiCube"},
                 "tags": [{"name": "sword"}]}
    client = _FakeClient(author_results=[author_hit])

    entry = fetch_and_freeze(
        "sword", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"),
        preferred_author="OptiCube")

    assert entry is not None
    assert entry["sketchfab_uid"] == "sword1"
    assert entry["author_username"] == "OptiCube"
    # Only the author-scoped call happened — no global (user=None) search.
    assert client.search_calls == [{"query": "sword", "user": "OptiCube"}]


def test_preferred_author_no_match_falls_back_to_global(tmp_path, monkeypatch):
    """The author has nothing matching this query — falls through to the
    open marketplace rather than returning nothing."""
    _install_fake_convert(monkeypatch)
    client = _FakeClient(author_results=[])  # OptiCube has nothing for this query

    entry = fetch_and_freeze(
        "red high heel shoe", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"),
        preferred_author="OptiCube")

    assert entry is not None
    assert entry["sketchfab_uid"] == "abc123"   # the global-search fixture result
    assert client.search_calls == [
        {"query": "red high heel shoe", "user": "OptiCube"},
        {"query": "red high heel shoe", "user": None},
    ]


def test_no_preferred_author_only_does_global_search(tmp_path, monkeypatch):
    _install_fake_convert(monkeypatch)
    client = _FakeClient()
    fetch_and_freeze(
        "red high heel shoe", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"))
    assert client.search_calls == [{"query": "red high heel shoe", "user": None}]


# ── visual QC: vision_select_fn is an authoritative pre-freeze decision ─────
# Live regression: a keyword search for "helmet" returns every helmet on the
# marketplace — sports, motorcycle, medieval, skydiving — all scoring
# identically on the shared word. vision_select_fn is the honest visual check
# text scoring cannot do; these pin that a selected UID is the only candidate
# that can freeze and refusal/unavailable never fall back to keyword order.

_TWO_CANDIDATES = [
    {"uid": "baseball1", "name": "Baseball Helmet Sports Asset",
     "viewerUrl": "https://sketchfab.com/3d-models/baseball1",
     "license": {"slug": "cc0"},
     "user": {"displayName": "SportsGuy", "username": "sportsguy"},
     "tags": [{"name": "helmet"}, {"name": "sports"}]},
    {"uid": "skydive1", "name": "Skydiving Helmet",
     "viewerUrl": "https://sketchfab.com/3d-models/skydive1",
     "license": {"slug": "cc0"},
     "user": {"displayName": "SkyArtist", "username": "skyartist"},
     "tags": [{"name": "helmet"}, {"name": "skydiving"}]},
]


def test_vision_select_fn_freezes_only_its_pick(tmp_path, monkeypatch):
    _install_fake_convert(monkeypatch)
    client = _FakeClient(results=_TWO_CANDIDATES)
    seen_pool = []

    def _vision(pool):
        seen_pool.append(pool)
        return VisionSelection(
            "select", uid="skydive1", reason="thumbnail is a skydiving helmet",
            candidate_uids=tuple(m["uid"] for m in pool),
        )

    entry = fetch_and_freeze(
        "helmet", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"),
        vision_select_fn=_vision)

    assert entry is not None
    assert entry["sketchfab_uid"] == "skydive1"
    assert entry["visual_selection"]["decision"] == "select"
    assert entry["visual_selection"]["reason"] == "thumbnail is a skydiving helmet"
    # Handed the full keyword-ranked pool (raw model dicts), not a subset.
    assert len(seen_pool) == 1
    assert {m["uid"] for m in seen_pool[0]} == {"baseball1", "skydive1"}


def test_vision_select_fn_none_refuses_without_keyword_fallback(tmp_path, monkeypatch):
    """An explicit 'none of these match' verdict must stop the freeze."""
    _install_fake_convert(monkeypatch)
    client = _FakeClient(results=_TWO_CANDIDATES)

    entry = fetch_and_freeze(
        "helmet", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"),
        vision_select_fn=lambda pool: None)

    assert entry is None
    assert client.download_calls == 0


def test_vision_select_fn_unrecognized_uid_refuses_without_keyword_fallback(tmp_path, monkeypatch):
    """A pick that doesn't match any candidate in the pool (a stale/garbled
    response) is treated the same as no pick — never an error."""
    _install_fake_convert(monkeypatch)
    client = _FakeClient(results=_TWO_CANDIDATES)

    entry = fetch_and_freeze(
        "helmet", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"),
        vision_select_fn=lambda pool: "nonexistent-uid")

    assert entry is None
    assert client.download_calls == 0


def test_vision_select_fn_exception_refuses_without_keyword_fallback(tmp_path, monkeypatch):
    """A visual gate failure must not silently become a keyword pick."""
    _install_fake_convert(monkeypatch)
    client = _FakeClient(results=_TWO_CANDIDATES)

    def _explodes(pool):
        raise RuntimeError("no vision-capable model loaded")

    entry = fetch_and_freeze(
        "helmet", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"),
        vision_select_fn=_explodes)

    assert entry is None
    assert client.download_calls == 0


def test_vision_select_fn_is_called_even_with_a_single_candidate(tmp_path, monkeypatch):
    """A visual gate must be allowed to refuse even its only candidate."""
    _install_fake_convert(monkeypatch)
    client = _FakeClient()  # the default single-result fixture
    calls = []

    entry = fetch_and_freeze(
        "red high heel shoe", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"),
        vision_select_fn=lambda pool: calls.append(pool) or None)

    assert entry is None
    assert len(calls) == 1


def test_no_vision_select_fn_is_byte_identical_to_before(tmp_path, monkeypatch):
    """The default (None) must reproduce pre-existing behavior exactly —
    every caller from before this parameter existed passes nothing for it."""
    _install_fake_convert(monkeypatch)
    client = _FakeClient(results=_TWO_CANDIDATES)

    entry = fetch_and_freeze(
        "helmet", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"))

    assert entry is not None
    assert entry["sketchfab_uid"] == "baseball1"


# ── deterministic semantic gate: semantic_filter_fn can REJECT candidates ────
# vision_select_fn (above) only ever reorders within candidates min_match_score
# already accepted. semantic_filter_fn is the sibling that can actually shrink
# the pool to nothing — a genuine "every candidate is semantically wrong"
# verdict must fall through to the caller's next asset tier, not accept a
# same-word-coincidence pick, mirroring the live "realistic paratrooper" ->
# "Cartoon Character" miss that motivated this feature.

def test_semantic_filter_fn_reorders_like_vision_select(tmp_path, monkeypatch):
    _install_fake_convert(monkeypatch)
    client = _FakeClient(results=_TWO_CANDIDATES)

    seen_pool = []

    def _semantic(pool):
        seen_pool.append(pool)
        return [m for m in pool if m["uid"] == "skydive1"] + \
               [m for m in pool if m["uid"] != "skydive1"]

    entry = fetch_and_freeze(
        "helmet", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"),
        semantic_filter_fn=_semantic)

    assert entry is not None
    assert entry["sketchfab_uid"] == "skydive1"
    assert len(seen_pool) == 1
    assert {m["uid"] for m in seen_pool[0]} == {"baseball1", "skydive1"}


def test_semantic_filter_fn_empty_result_is_a_clean_miss(tmp_path, monkeypatch):
    """Every candidate failing the semantic floor must return None (fall
    through to the caller's next asset tier), not raise and not fall back to
    downloading the keyword-ranked top pick anyway."""
    _install_fake_convert(monkeypatch)
    client = _FakeClient(results=_TWO_CANDIDATES)

    entry = fetch_and_freeze(
        "helmet", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"),
        semantic_filter_fn=lambda pool: [])

    assert entry is None
    assert client.download_calls == 0


def test_semantic_filter_fn_runs_before_vision_select_fn(tmp_path, monkeypatch):
    """The semantic gate narrows the pool FIRST; vision_select_fn (when also
    configured) only ever sees what survived it."""
    _install_fake_convert(monkeypatch)
    client = _FakeClient(results=_TWO_CANDIDATES)
    vision_seen = []

    entry = fetch_and_freeze(
        "helmet", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"),
        semantic_filter_fn=lambda pool: [m for m in pool if m["uid"] == "skydive1"],
        vision_select_fn=lambda pool: vision_seen.append(pool) or None)

    assert entry is None
    # The visual gate still runs on the single surviving candidate and can
    # refuse it; semantic filtering does not become an implicit acceptance.
    assert len(vision_seen) == 1
    assert vision_seen[0][0]["uid"] == "skydive1"


def test_semantic_filter_fn_exception_degrades_to_keyword_order(tmp_path, monkeypatch):
    """The semantic gate is advisory-strict about REJECTING, but a crash in
    it must degrade exactly like a crashing vision_select_fn — never take
    down the whole fetch."""
    _install_fake_convert(monkeypatch)
    client = _FakeClient(results=_TWO_CANDIDATES)

    def _explodes(pool):
        raise RuntimeError("no cached CLIP model, no network")

    entry = fetch_and_freeze(
        "helmet", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"),
        semantic_filter_fn=_explodes)

    assert entry is not None
    assert entry["sketchfab_uid"] == "baseball1"  # untouched keyword order


def test_semantic_filter_fn_none_return_keeps_keyword_order(tmp_path, monkeypatch):
    """semantic_filter.py returns the ORIGINAL candidates list (not an empty
    one) when nothing in the whole pool was measurable at all — an embedder
    availability failure, not a semantic verdict. Confirm that shape reaches
    fetch_and_freeze correctly: same untouched keyword order as no gate at
    all, and a real download still happens."""
    _install_fake_convert(monkeypatch)
    client = _FakeClient(results=_TWO_CANDIDATES)

    entry = fetch_and_freeze(
        "helmet", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"),
        semantic_filter_fn=lambda pool: pool)

    assert entry is not None
    assert entry["sketchfab_uid"] == "baseball1"


def test_no_semantic_filter_fn_is_byte_identical_to_before(tmp_path, monkeypatch):
    """The default (None) must reproduce pre-existing behavior exactly —
    every caller from before this parameter existed passes nothing for it."""
    _install_fake_convert(monkeypatch)
    client = _FakeClient(results=_TWO_CANDIDATES)

    entry = fetch_and_freeze(
        "helmet", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"))

    assert entry is not None
    assert entry["sketchfab_uid"] == "baseball1"


# ── clip_image_embedding capture: turns this freeze into a reusable ────────
# artifact for unity_entity_asset_resolver.py's Tier S cache-hit gate later
# (unity_semantic_embed.semantic_filter_frozen) — only attempted when this
# freeze itself opted into semantic_select.

_ONE_CANDIDATE_WITH_THUMBNAIL = [
    {"uid": "abc123", "name": "Red High Heel Shoe",
     "description": "a shiny red shoe",
     "viewerUrl": "https://sketchfab.com/3d-models/abc123",
     "license": {"slug": "by"},
     "user": {"displayName": "Jane Doe", "username": "janedoe"},
     "tags": [{"name": "shoe"}],
     "thumbnails": {"images": [{"url": "https://x/thumb.jpg",
                               "width": 256, "height": 256}]}},
]


def test_semantic_filter_fn_stamps_embedding_when_thumbnail_available(tmp_path, monkeypatch):
    _install_fake_convert(monkeypatch)
    client = _FakeClient(results=_ONE_CANDIDATE_WITH_THUMBNAIL)
    client.fetch_image_bytes = lambda url: b"thumbnail-bytes"
    monkeypatch.setattr("src.unity_semantic_embed.image_embedding",
                        lambda b: [1.0, 2.0, 3.0] if b == b"thumbnail-bytes" else None)

    entry = fetch_and_freeze(
        "red shoe", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"),
        semantic_filter_fn=lambda pool: pool)

    assert entry is not None
    assert entry["clip_image_embedding"] == [1.0, 2.0, 3.0]
    on_disk = json.loads((tmp_path / "lookups" / SKETCHFAB_RELPATH).read_text(encoding="utf-8"))
    assert on_disk["entries"][0]["clip_image_embedding"] == [1.0, 2.0, 3.0]


def test_no_semantic_filter_fn_never_stamps_embedding(tmp_path, monkeypatch):
    """semantic_select off for this freeze — no embedding is captured, even
    though a thumbnail is available. An entry frozen with the gate off
    simply carries no clip_image_embedding, and any later cache-hit
    re-check leaves that axis unmeasured for it."""
    _install_fake_convert(monkeypatch)
    client = _FakeClient(results=_ONE_CANDIDATE_WITH_THUMBNAIL)
    client.fetch_image_bytes = lambda url: b"thumbnail-bytes"

    entry = fetch_and_freeze(
        "red shoe", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"))

    assert entry is not None
    assert "clip_image_embedding" not in entry


def test_semantic_filter_fn_present_but_no_thumbnail_leaves_embedding_unstamped(
        tmp_path, monkeypatch):
    _install_fake_convert(monkeypatch)
    client = _FakeClient(results=_TWO_CANDIDATES)  # no "thumbnails" field

    entry = fetch_and_freeze(
        "helmet", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"),
        semantic_filter_fn=lambda pool: pool)

    assert entry is not None
    assert "clip_image_embedding" not in entry


def test_embedding_capture_exception_never_blocks_the_freeze(tmp_path, monkeypatch):
    """A missing fetch_image_bytes, a network hiccup, an unloadable CLIP
    model — none of it may cost the entry itself: the embedding is a bonus,
    writing the frozen entry is not optional."""
    _install_fake_convert(monkeypatch)
    client = _FakeClient(results=_ONE_CANDIDATE_WITH_THUMBNAIL)

    def _boom(url):
        raise RuntimeError("no browser, no network")
    client.fetch_image_bytes = _boom

    entry = fetch_and_freeze(
        "red shoe", _score_all_equal, client=client,
        cache_dir=tmp_path / "cache", lookups_dir=str(tmp_path / "lookups"),
        semantic_filter_fn=lambda pool: pool)

    assert entry is not None
    assert entry["sketchfab_uid"] == "abc123"
    assert "clip_image_embedding" not in entry
