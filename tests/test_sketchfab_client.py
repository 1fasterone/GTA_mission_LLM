"""Tests for the Sketchfab Data API client (src/sketchfab_client.py).

No real browser/network — SketchfabClient._browser_request_ctx is
monkeypatched to a fake Playwright APIRequestContext. Every method must
degrade to []/None/{} on failure so the resolver's Tier S never blows up
generation.
"""

from contextlib import contextmanager

from src.sketchfab_client import SketchfabClient


def _fake_ctx(req):
    """A context manager yielding `req` (or None), matching the shape of
    SketchfabClient._browser_request_ctx()."""
    @contextmanager
    def _cm():
        yield req
    return _cm


class _FakeResponse:
    def __init__(self, *, ok=True, data=None, body=b""):
        self.ok = ok
        self._data = data
        self._body = body

    def json(self):
        return self._data

    def body(self):
        return self._body


class _FakeRequest:
    """Records calls and returns canned responses keyed by substring match
    on the URL, in call order."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._responses.pop(0)


def test_available_requires_token():
    assert not SketchfabClient({}).available()
    assert SketchfabClient({"api_token": "tok"}).available()


def test_env_token_wins_over_config(monkeypatch):
    monkeypatch.setenv("SKETCHFAB_API_TOKEN", "env-tok")
    client = SketchfabClient({"api_token": "config-tok"})
    assert client.token == "env-tok"


def test_license_slug_normalizes():
    client = SketchfabClient({"api_token": "tok"})
    # A literal `slug` key (if some future/other endpoint provides one) wins outright.
    assert client.license_slug({"license": {"slug": "CC0"}}) == "cc0"
    # Real search results carry {"uid": ..., "label": ...} with NO slug —
    # resolved via the verified uid->slug table, never guessed from the label.
    assert client.license_slug(
        {"license": {"uid": "7c23a1ba438d4306920229c12afcb5f9",
                     "label": "CC0 Public Domain"}}) == "cc0"
    assert client.license_slug(
        {"license": {"uid": "322a749bcfa841b29dff1e8a1bb74b0b",
                     "label": "CC Attribution"}}) == "by"
    # Unrecognized uid / missing license -> None (fail closed, never a guess).
    assert client.license_slug({"license": {"uid": "unknown-uid", "label": "by"}}) is None
    assert client.license_slug({}) is None


def test_filter_licensed_keeps_only_accepted():
    client = SketchfabClient({"api_token": "tok", "accepted_licenses": ["cc0"]})
    results = [
        {"uid": "a", "license": {"slug": "cc0"}},
        {"uid": "b", "license": {"slug": "by-nc"}},
    ]
    assert [m["uid"] for m in client.filter_licensed(results)] == ["a"]


def test_search_no_token_returns_empty():
    assert SketchfabClient({}).search("shoe") == []


def test_search_no_playwright_returns_empty(monkeypatch):
    """_browser_request_ctx yields None when playwright/Chromium isn't
    installed — search() must degrade cleanly, not raise."""
    client = SketchfabClient({"api_token": "tok"})
    monkeypatch.setattr(client, "_browser_request_ctx", _fake_ctx(None))
    assert client.search("shoe") == []


def test_search_returns_results(monkeypatch):
    client = SketchfabClient({"api_token": "tok"})
    req = _FakeRequest([_FakeResponse(data={"results": [{"uid": "abc"}]})])
    monkeypatch.setattr(client, "_browser_request_ctx", _fake_ctx(req))
    assert client.search("shoe") == [{"uid": "abc"}]
    url, kwargs = req.calls[0]
    assert "search" in url
    assert kwargs["params"]["q"] == "shoe"


def test_search_with_user_scopes_params(monkeypatch):
    """Verified live against the real API: `user` must be the Sketchfab
    `username` (login handle), not `displayName` — the client just passes
    it through as a query param, the caller is responsible for that."""
    client = SketchfabClient({"api_token": "tok"})
    req = _FakeRequest([_FakeResponse(data={"results": []})])
    monkeypatch.setattr(client, "_browser_request_ctx", _fake_ctx(req))
    client.search("sword", user="OptiCube")
    _, kwargs = req.calls[0]
    assert kwargs["params"]["user"] == "OptiCube"


def test_search_without_user_omits_param(monkeypatch):
    client = SketchfabClient({"api_token": "tok"})
    req = _FakeRequest([_FakeResponse(data={"results": []})])
    monkeypatch.setattr(client, "_browser_request_ctx", _fake_ctx(req))
    client.search("sword")
    _, kwargs = req.calls[0]
    assert "user" not in kwargs["params"]


def test_search_swallows_non_ok_response(monkeypatch):
    client = SketchfabClient({"api_token": "tok"})
    req = _FakeRequest([_FakeResponse(ok=False)])
    monkeypatch.setattr(client, "_browser_request_ctx", _fake_ctx(req))
    assert client.search("shoe") == []


def test_search_swallows_exception(monkeypatch):
    client = SketchfabClient({"api_token": "tok"})

    @contextmanager
    def _raises():
        raise RuntimeError("boom")
        yield  # pragma: no cover — unreachable, satisfies generator shape

    monkeypatch.setattr(client, "_browser_request_ctx", _raises)
    assert client.search("shoe") == []


def test_get_download_urls_no_token_returns_empty():
    assert SketchfabClient({}).get_download_urls("abc") == {}


def test_get_download_urls_returns_data(monkeypatch):
    client = SketchfabClient({"api_token": "tok"})
    req = _FakeRequest([_FakeResponse(data={"gltf": {"url": "https://x/y.zip"}})])
    monkeypatch.setattr(client, "_browser_request_ctx", _fake_ctx(req))
    urls = client.get_download_urls("abc")
    assert urls == {"gltf": {"url": "https://x/y.zip"}}
    # Auth token IS sent for the metadata call.
    _, kwargs = req.calls[0]
    assert "Authorization" in kwargs["headers"]


def test_download_archive_no_url_returns_none(tmp_path, monkeypatch):
    client = SketchfabClient({"api_token": "tok"})
    monkeypatch.setattr(client, "get_download_urls", lambda uid: {})
    assert client.download_archive("abc", tmp_path / "out.zip") is None


def test_download_archive_writes_to_disk(tmp_path, monkeypatch):
    client = SketchfabClient({"api_token": "tok"})
    monkeypatch.setattr(client, "get_download_urls",
                        lambda uid: {"gltf": {"url": "https://example/x.zip"}})
    req = _FakeRequest([_FakeResponse(body=b"zipdata")])
    monkeypatch.setattr(client, "_browser_request_ctx", _fake_ctx(req))

    dest = tmp_path / "out.zip"
    assert client.download_archive("abc", dest) == dest
    assert dest.read_bytes() == b"zipdata"
    # The signed URL fetch must NOT carry the Authorization header (S3
    # presigned URLs reject an extra Authorization header outright).
    _, kwargs = req.calls[0]
    assert "headers" not in kwargs or not kwargs.get("headers")


def test_download_archive_non_ok_returns_none(tmp_path, monkeypatch):
    client = SketchfabClient({"api_token": "tok"})
    monkeypatch.setattr(client, "get_download_urls",
                        lambda uid: {"gltf": {"url": "https://example/x.zip"}})
    req = _FakeRequest([_FakeResponse(ok=False)])
    monkeypatch.setattr(client, "_browser_request_ctx", _fake_ctx(req))
    assert client.download_archive("abc", tmp_path / "out.zip") is None
