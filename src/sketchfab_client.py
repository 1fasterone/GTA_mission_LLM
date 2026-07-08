"""
Sketchfab Data API v3 client — search + license filter + download.

Transport: a headless Chromium instance (Playwright), NOT a plain HTTP client.

Verified 2026-07: Sketchfab's API sits behind AWS WAF Bot Control that
fingerprints the calling client's TLS/JA3 handshake. Plain `httpx` and
`curl_cffi` (even with a spoofed Chrome User-Agent and Chrome TLS
impersonation) get silently challenged — HTTP 202, empty body, no error to
catch. A completely cold, fresh headless Chromium instance (zero cookies, zero
history) passes on the very first request with no cookie-harvesting or
challenge-solving required — this is a client-fingerprint gate, not an IP or
session-reputation check. Playwright's `BrowserContext.request` (not
`page.goto`) is used specifically because it returns the raw JSON/bytes
response directly — no HTML-viewer wrapping to scrape, unlike a real page
navigation to a JSON endpoint.

Same shape as src/anythingllm_client.py and src/blender/diffusion.py::
DiffusionClient otherwise. Never raises on a network/browser failure — every
method returns []/None/{} so the resolver's Sketchfab tier degrades to a
clean miss (falls through to the Kenney catalog) instead of blowing up
generation. Missing `playwright` or its Chromium binary degrades the same
way — see _browser_request_ctx(). No scoring/ranking logic lives here — that
stays in src/unity_entity_asset_resolver.py so Kenney and Sketchfab
candidates are ranked by the exact same weighted-keyword-overlap function.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

log = logging.getLogger(__name__)

# Sketchfab v3 license slugs this pipeline is willing to import. Config-driven
# (see config.yaml `sketchfab.accepted_licenses`).
DEFAULT_ACCEPTED_LICENSES = ("cc0", "by", "by-sa", "by-nd")

_ENV_TOKEN_VAR = "SKETCHFAB_API_TOKEN"

# Verified 2026-07 against a live GET /v3/licenses response — a search result's
# `license` field is `{"uid": ..., "label": ...}` with NO `slug` at all, so the
# slug has to come from this table, keyed by the stable license uid (labels are
# free text and shouldn't be pattern-matched for a compliance-sensitive
# decision). An unrecognized uid returns None from license_slug() below, which
# filter_licensed() excludes — unknown licenses fail closed, never silently
# pass as compliant.
_LICENSE_UID_TO_SLUG: dict[str, str] = {
    "322a749bcfa841b29dff1e8a1bb74b0b": "by",
    "b9ddc40b93e34cdca1fc152f39b9f375": "by-sa",
    "72360ff1740d419791934298b8b6d270": "by-nd",
    "bbfe3f7dbcdd4122b966b85b9786a989": "by-nc",
    "2628dbe5140a4e9592126c8df566c0b7": "by-nc-sa",
    "34b725081a6a4184957efaec2cb84ed3": "by-nc-nd",
    "7c23a1ba438d4306920229c12afcb5f9": "cc0",
    "72eb2b1960364637901eacce19283624": "free-st",
    "783b685da9bf457d81e829fa283f3567": "st",
    "5b54cf13b1a4422ca439696eb152070d": "ed",
}


class SketchfabClient:
    """Search/download client for the Sketchfab Data API v3.

    Token resolution: SKETCHFAB_API_TOKEN env var always wins over
    `cfg["api_token"]` — a personal API token should not need to live in a
    git-tracked config.yaml.
    """

    def __init__(self, cfg: Optional[dict] = None):
        cfg = cfg or {}
        self.base_url = str(cfg.get("base_url", "https://api.sketchfab.com/v3")).rstrip("/")
        self.token = os.environ.get(_ENV_TOKEN_VAR) or str(cfg.get("api_token", "") or "")
        self.timeout = float(cfg.get("timeout", 30))
        # Separate, longer timeout for the actual archive fetch in
        # download_archive() — search/download-urls are small JSON calls that
        # finish in ~1s, but the signed archive itself can be tens of MB
        # (photogrammetry "raw scan" uploads run 25-40MB+) and transfer speed
        # to Sketchfab's S3 bucket is noticeably variable in practice:
        # measured a 25MB archive take ~31s, then a 70MB archive take ~102s
        # (a ~0.7 MB/s rate) on the SAME connection minutes apart. This is a
        # one-shot offline freeze step, not a hot path — err generous rather
        # than flake on a slow-network day.
        self.download_timeout = float(cfg.get("download_timeout", 240))
        self.accepted_licenses = frozenset(
            s.lower() for s in (cfg.get("accepted_licenses") or DEFAULT_ACCEPTED_LICENSES))

    def available(self) -> bool:
        """True when a token is configured — cheap up-front gate, no network."""
        return bool(self.token)

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Token {self.token}"} if self.token else {}

    @contextmanager
    def _browser_request_ctx(self) -> Iterator[Optional[Any]]:
        """Yield a Playwright APIRequestContext backed by a fresh, cold
        headless Chromium instance, or None if `playwright` (or its Chromium
        binary) isn't installed — callers must treat None as a clean miss,
        same as any other unavailable backend in this codebase (e.g. missing
        Blender). ALL response bodies must be read (`.json()`/`.body()`)
        inside this `with` block — the browser tears down on exit.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.debug("playwright not installed — Sketchfab client unavailable "
                      "(pip install playwright && playwright install chromium)")
            yield None
            return
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    yield browser.new_context().request
                finally:
                    browser.close()
        except Exception as exc:  # noqa: BLE001 — browser launch/runtime failure
            log.debug("sketchfab browser request failed: %s", exc)
            yield None

    def search(self, query: str, *, count: int = 24,
              user: Optional[str] = None) -> list[dict]:
        """GET /search?type=models — raw Sketchfab model result dicts, [] on
        any failure (no token, no browser, network error, non-2xx, malformed
        body). Public search does not actually require a token, but this
        pipeline gates it on one anyway to keep `available()` a single,
        simple readiness check for the whole client.

        `user` scopes the search to one author's own catalog — verified live
        against the real API with the author's `username` (the login-style
        handle in a model's `user.username` field, NOT `user.displayName`).
        Used for style-coherent sourcing: once one good model is found, later
        entities try that same author's catalog first (see
        src/sketchfab_freeze.py's `preferred_author`) before falling back to
        the open marketplace, so a project doesn't end up as a "constructed
        chaos" of mismatched independent artists' styles.
        """
        if not self.available():
            return []
        params: dict[str, str | int] = {
            "type": "models", "q": query,
            "downloadable": "true", "count": count,
        }
        if user:
            params["user"] = user
        try:
            with self._browser_request_ctx() as req:
                if req is None:
                    return []
                r = req.get(f"{self.base_url}/search", headers=self._headers,
                           params=params, timeout=self.timeout * 1000)
                if not r.ok:
                    return []
                return list(r.json().get("results") or [])
        except Exception as exc:  # noqa: BLE001
            log.debug("sketchfab search(%r, user=%r) failed: %s", query, user, exc)
            return []

    def license_slug(self, model: dict) -> Optional[str]:
        """Normalize a search-result model's license field to a bare slug
        ("cc0", "by", "by-sa", "by-nd", ...), or None if absent/unrecognized.

        Search results carry `license: {"uid": ..., "label": ...}` — no slug
        field — so this maps the stable uid through _LICENSE_UID_TO_SLUG. A
        `slug` key is honored directly first in case a future/other endpoint
        (e.g. /v3/licenses itself) already provides one.
        """
        lic = model.get("license") or {}
        if lic.get("slug"):
            return str(lic["slug"]).strip().lower()
        return _LICENSE_UID_TO_SLUG.get(str(lic.get("uid") or ""))

    def filter_licensed(self, results: list[dict]) -> list[dict]:
        """Keep only models whose license slug is in self.accepted_licenses."""
        return [m for m in results
                if self.license_slug(m) in self.accepted_licenses]

    def get_download_urls(self, model_uid: str) -> dict:
        """GET /models/{uid}/download — signed, time-limited archive URLs
        keyed by format ("gltf", "glb", "source", "usdz"), each a
        {"url", "size", "expires"} dict. {} on any failure, including models
        that are downloadable in the UI but not to this token (license gate
        not accepted by the account, etc.)."""
        if not self.available():
            return {}
        try:
            with self._browser_request_ctx() as req:
                if req is None:
                    return {}
                r = req.get(f"{self.base_url}/models/{model_uid}/download",
                           headers=self._headers, timeout=self.timeout * 1000)
                if not r.ok:
                    return {}
                return dict(r.json() or {})
        except Exception as exc:  # noqa: BLE001
            log.debug("sketchfab get_download_urls(%s) failed: %s", model_uid, exc)
            return {}

    def download_archive(self, model_uid: str, dest_zip: Path) -> Optional[Path]:
        """Fetch the gltf archive for `model_uid` and write it to dest_zip.
        Returns dest_zip, or None on any failure.

        The signed download URL must be fetched WITHOUT the Authorization
        header — it's a pre-signed S3 URL (auth is in its own query-string
        signature) and an extra Authorization header makes S3 reject it with
        "Unsupported Authorization Type" / signature mismatch.
        """
        urls = self.get_download_urls(model_uid)
        url = (urls.get("gltf") or {}).get("url")
        if not url:
            return None
        try:
            with self._browser_request_ctx() as req:
                if req is None:
                    return None
                r = req.get(url, timeout=self.download_timeout * 1000)
                if not r.ok:
                    return None
                body = r.body()
            dest_zip.parent.mkdir(parents=True, exist_ok=True)
            dest_zip.write_bytes(body)
            return dest_zip
        except Exception as exc:  # noqa: BLE001
            log.debug("sketchfab download_archive(%s) failed: %s", model_uid, exc)
            return None
