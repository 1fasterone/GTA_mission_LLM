"""Tests for vision-based Sketchfab candidate selection
(src/unity_sketchfab_vision.py). No real LLM, no real network — a fake
`llm.chat()` returns canned JSON (mirrors tests/test_unity_sketchfab_
translator.py's convention) and a fake client hands back canned thumbnail
bytes, keyed by URL.
"""

from src.unity_sketchfab_vision import VisionSelection, select_best_candidate


class _FakeLLM:
    def __init__(self, response: str):
        self._response = response
        self.calls = []

    def chat(self, system, user, images=None, temperature=None,
             thinking=True, seed=None):
        self.calls.append({"system": system, "user": user, "images": images})
        return self._response


class _FakeClient:
    def __init__(self, bytes_by_url: dict):
        self._bytes = bytes_by_url
        self.fetch_calls = []

    def fetch_image_bytes(self, url):
        self.fetch_calls.append(url)
        return self._bytes.get(url)


def _candidate(uid, thumb_url="https://cdn.example/x.jpg", width=256):
    return {
        "uid": uid,
        "thumbnails": {"images": [{"url": thumb_url, "width": width}]},
    }


def test_picks_the_named_candidate():
    llm = _FakeLLM('{"pick": 2, "reason": "looks like a skydiving helmet"}')
    client = _FakeClient({
        "https://cdn.example/baseball.jpg": b"BASEBALL-BYTES",
        "https://cdn.example/skydive.jpg": b"SKYDIVE-BYTES",
    })
    candidates = [
        _candidate("baseball1", "https://cdn.example/baseball.jpg"),
        _candidate("skydive1", "https://cdn.example/skydive.jpg"),
    ]
    picked = select_best_candidate(
        "LeadSkydiverJumpsuit", "player", "lead skydiver jumpsuit helmet",
        candidates, client=client, llm=llm)
    assert isinstance(picked, VisionSelection)
    assert picked.decision == "select"
    assert picked.uid == "skydive1"
    # Both candidates' thumbnails were fetched and attached as images.
    assert len(llm.calls[0]["images"]) == 2


def test_none_match_verdict_returns_none():
    llm = _FakeLLM('{"pick": null, "reason": "neither looks like a helmet at all"}')
    client = _FakeClient({
        "https://cdn.example/a.jpg": b"A", "https://cdn.example/b.jpg": b"B",
    })
    candidates = [_candidate("a", "https://cdn.example/a.jpg"),
                 _candidate("b", "https://cdn.example/b.jpg")]
    decision = select_best_candidate(
        "Thing", "prop", "a thing", candidates, client=client, llm=llm)
    assert decision.decision == "refuse"
    assert decision.uid is None


def test_out_of_range_pick_returns_none():
    llm = _FakeLLM('{"pick": 99}')
    client = _FakeClient({
        "https://cdn.example/a.jpg": b"A", "https://cdn.example/b.jpg": b"B",
    })
    candidates = [_candidate("a", "https://cdn.example/a.jpg"),
                 _candidate("b", "https://cdn.example/b.jpg")]
    decision = select_best_candidate(
        "Thing", "prop", "a thing", candidates, client=client, llm=llm)
    assert decision.decision == "unavailable"


def test_malformed_json_returns_none():
    llm = _FakeLLM("not json at all")
    client = _FakeClient({
        "https://cdn.example/a.jpg": b"A", "https://cdn.example/b.jpg": b"B",
    })
    candidates = [_candidate("a", "https://cdn.example/a.jpg"),
                 _candidate("b", "https://cdn.example/b.jpg")]
    decision = select_best_candidate(
        "Thing", "prop", "a thing", candidates, client=client, llm=llm)
    assert decision.decision == "unavailable"


def test_strips_think_block_and_markdown_fence():
    llm = _FakeLLM("<think>comparing...</think>```json\n"
                   '{"pick": 1, "reason": "matches"}\n```')
    client = _FakeClient({
        "https://cdn.example/a.jpg": b"A", "https://cdn.example/b.jpg": b"B",
    })
    candidates = [_candidate("a", "https://cdn.example/a.jpg"),
                 _candidate("b", "https://cdn.example/b.jpg")]
    decision = select_best_candidate(
        "Thing", "prop", "a thing", candidates, client=client, llm=llm)
    assert decision.decision == "select"
    assert decision.uid == "a"


def test_llm_exception_returns_none():
    class _Explodes:
        def chat(self, **kw):
            raise RuntimeError("model has no attached mmproj")

    client = _FakeClient({
        "https://cdn.example/a.jpg": b"A", "https://cdn.example/b.jpg": b"B",
    })
    candidates = [_candidate("a", "https://cdn.example/a.jpg"),
                 _candidate("b", "https://cdn.example/b.jpg")]
    decision = select_best_candidate(
        "Thing", "prop", "a thing", candidates,
        client=client, llm=_Explodes())
    assert decision.decision == "unavailable"


def test_fewer_than_two_fetchable_thumbnails_skips_the_llm_call():
    """One candidate has no thumbnails field at all (an older/malformed API
    shape); the other's fetch fails. Nothing to meaningfully compare, so the
    LLM is never even called."""
    llm = _FakeLLM('{"pick": 1}')
    client = _FakeClient({})  # every fetch_image_bytes call returns None
    candidates = [
        {"uid": "no-thumb"},  # no "thumbnails" key at all
        _candidate("unfetchable", "https://cdn.example/missing.jpg"),
    ]
    decision = select_best_candidate(
        "Thing", "prop", "a thing", candidates, client=client, llm=llm)
    assert decision.decision == "unavailable"
    assert llm.calls == []


def test_candidates_beyond_max_images_are_not_fetched():
    llm = _FakeLLM('{"pick": 1}')
    urls = [f"https://cdn.example/{i}.jpg" for i in range(5)]
    client = _FakeClient({u: f"BYTES-{i}".encode() for i, u in enumerate(urls)})
    candidates = [_candidate(f"c{i}", u) for i, u in enumerate(urls)]
    select_best_candidate(
        "Thing", "prop", "a thing", candidates,
        client=client, llm=llm, max_images=3)
    assert len(client.fetch_calls) == 3


def test_thumbnail_closest_to_target_width_is_chosen():
    """Sketchfab's own multi-resolution thumbnail set — this picks the entry
    closest to the small target width, not always the largest, so a vision
    call isn't wastefully attaching a multi-megapixel render."""
    llm = _FakeLLM('{"pick": 1}')
    candidates = [
        {"uid": "a", "thumbnails": {"images": [
            {"url": "https://cdn.example/a-huge.jpg", "width": 2048},
            {"url": "https://cdn.example/a-small.jpg", "width": 260},
        ]}},
        _candidate("b", "https://cdn.example/b.jpg"),
    ]
    client = _FakeClient({
        "https://cdn.example/a-small.jpg": b"SMALL",
        "https://cdn.example/a-huge.jpg": b"HUGE",
        "https://cdn.example/b.jpg": b"B",
    })
    select_best_candidate(
        "Thing", "prop", "a thing", candidates, client=client, llm=llm)
    assert "https://cdn.example/a-small.jpg" in client.fetch_calls
    assert "https://cdn.example/a-huge.jpg" not in client.fetch_calls
