"""`sketchfab-fetch --semantic-select` (cli/unity_assets.py) — CLI-level
coverage for the flag/config wiring that constructs `semantic_filter_fn` and
hands it to `fetch_and_freeze`. src.sketchfab_freeze and
src.unity_semantic_embed each have their own unit tests (test_sketchfab_freeze.py,
test_unity_semantic_embed.py); nothing previously exercised the CLI command
itself deciding WHETHER to build that closure, from WHICH thresholds, and
with WHICH query/kind — this fills that gap.

Offline and deterministic: SketchfabClient, fetch_and_freeze, and
semantic_filter are all faked/monkeypatched, so no network, no Blender, no
Sketchfab token required.
"""
from __future__ import annotations

import os

import pytest
import typer
import yaml

_FAKE_ENTRY = {"relpath": "abc123.fbx", "license": "by", "author": "Jane Doe"}


@pytest.fixture(autouse=True)
def _isolate_environ():
    """Importing cli.unity_assets (needed to call sketchfab_fetch directly)
    triggers a real .env load that can inject a live SKETCHFAB_API_TOKEN into
    os.environ as a side effect — a plain monkeypatch.delenv wouldn't undo
    that, since the value was never set through monkeypatch in the first
    place. Snapshot/restore the whole environment so nothing leaks past this
    file into test_sketchfab_client.py's "no token configured" tests."""
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


class _FakeClient:
    """Stands in for src.sketchfab_client.SketchfabClient — no network."""

    def __init__(self, cfg=None):
        self.cfg = cfg or {}

    def available(self) -> bool:
        return True

    def search(self, query, count=24, user=None):
        return [
            {"uid": "abc123", "name": "Realistic Paratrooper",
             "license": {"slug": "by"},
             "user": {"displayName": "Jane Doe", "username": "janedoe"},
             "tags": [{"name": "paratrooper"}]},
        ]

    def license_slug(self, model):
        return (model.get("license") or {}).get("slug")

    def filter_licensed(self, results):
        return list(results)


def _config(tmp_path, sketchfab_extra: dict | None = None) -> str:
    cfg = {"sketchfab": {"enabled": True, **(sketchfab_extra or {})}}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return str(path)


def _run_sketchfab_fetch(monkeypatch, tmp_path, *, semantic_select, config_path,
                         freeze_result=_FAKE_ENTRY):
    """Calls the sketchfab_fetch typer command directly (same convention as
    test_cli_unity_validate.py) with every optional argument passed
    explicitly — a Typer command still takes plain kwargs when called this
    way, but a parameter left at its default receives the typer.Option(...)
    sentinel instead of the plain value.

    Returns (freeze_calls, semantic_calls) — the kwargs fetch_and_freeze was
    called with, and the args semantic_filter (if built) was called with.
    """
    import src.sketchfab_client as sketchfab_client_mod
    import src.sketchfab_freeze as sketchfab_freeze_mod

    monkeypatch.setattr(sketchfab_client_mod, "SketchfabClient", _FakeClient)

    freeze_calls = []

    def _fake_fetch_and_freeze(query, **kwargs):
        freeze_calls.append(kwargs)
        return freeze_result

    monkeypatch.setattr(sketchfab_freeze_mod, "fetch_and_freeze", _fake_fetch_and_freeze)

    import src.unity_semantic_embed as semantic_embed_mod

    semantic_calls = []

    def _fake_semantic_filter(query, kind, text, candidates, *, client, **kw):
        semantic_calls.append({"query": query, "kind": kind, "candidates": candidates, **kw})
        return candidates

    monkeypatch.setattr(semantic_embed_mod, "semantic_filter", _fake_semantic_filter)

    from cli.unity_assets import sketchfab_fetch

    try:
        sketchfab_fetch(
            "realistic paratrooper", config=config_path, lookups=str(tmp_path / "lookups"),
            category="prop", kind_hints="", limit=24, dry_run=False,
            semantic_select=semantic_select)
    except typer.Exit:
        pass  # entry is None -> command exits 1 after printing its diagnostics

    return freeze_calls, semantic_calls


class TestSemanticSelectWiring:
    def test_explicit_flag_on_builds_semantic_filter_fn_from_config_thresholds(
            self, monkeypatch, tmp_path):
        config_path = _config(tmp_path, {
            "min_image_similarity": 0.3, "min_text_similarity": 0.55,
        })
        freeze_calls, semantic_calls = _run_sketchfab_fetch(
            monkeypatch, tmp_path, semantic_select=True, config_path=config_path)

        assert len(freeze_calls) == 1
        semantic_filter_fn = freeze_calls[0]["semantic_filter_fn"]
        assert semantic_filter_fn is not None

        # Calling the closure fetch_and_freeze would call must reach the real
        # semantic_filter with THIS run's query/kind and the config's
        # calibrated thresholds — not the module defaults.
        candidates = [{"uid": "abc123"}]
        semantic_filter_fn(candidates)
        assert len(semantic_calls) == 1
        call = semantic_calls[0]
        assert call["query"] == "realistic paratrooper"
        assert call["kind"] == "prop"  # kind_hints="" falls back to --category
        assert call["candidates"] == candidates
        assert call["min_image_similarity"] == 0.3
        assert call["min_text_similarity"] == 0.55

    def test_explicit_flag_off_passes_no_semantic_filter_fn(self, monkeypatch, tmp_path):
        config_path = _config(tmp_path, {"semantic_select": True})  # config says on...
        freeze_calls, semantic_calls = _run_sketchfab_fetch(
            monkeypatch, tmp_path, semantic_select=False, config_path=config_path)  # ...CLI overrides off

        assert len(freeze_calls) == 1
        assert freeze_calls[0]["semantic_filter_fn"] is None
        assert semantic_calls == []

    def test_unset_flag_falls_back_to_config_default_on(self, monkeypatch, tmp_path):
        config_path = _config(tmp_path, {"semantic_select": True})
        freeze_calls, _ = _run_sketchfab_fetch(
            monkeypatch, tmp_path, semantic_select=None, config_path=config_path)

        assert freeze_calls[0]["semantic_filter_fn"] is not None

    def test_unset_flag_falls_back_to_config_default_off(self, monkeypatch, tmp_path):
        config_path = _config(tmp_path)  # no semantic_select key at all
        freeze_calls, _ = _run_sketchfab_fetch(
            monkeypatch, tmp_path, semantic_select=None, config_path=config_path)

        assert freeze_calls[0]["semantic_filter_fn"] is None

    def test_default_thresholds_used_when_config_omits_them(self, monkeypatch, tmp_path):
        # config.example.yaml's calibrated defaults (tools/calibrate_semantic_score.py)
        config_path = _config(tmp_path)
        freeze_calls, semantic_calls = _run_sketchfab_fetch(
            monkeypatch, tmp_path, semantic_select=True, config_path=config_path)

        freeze_calls[0]["semantic_filter_fn"]([{"uid": "abc123"}])
        call = semantic_calls[-1]
        assert call["min_image_similarity"] == 0.22
        assert call["min_text_similarity"] == 0.45

    def test_config_pool_size_reaches_max_candidates(self, monkeypatch, tmp_path):
        config_path = _config(tmp_path, {"semantic_pool_size": 10})
        freeze_calls, semantic_calls = _run_sketchfab_fetch(
            monkeypatch, tmp_path, semantic_select=True, config_path=config_path)

        freeze_calls[0]["semantic_filter_fn"]([{"uid": "abc123"}])
        assert semantic_calls[-1]["max_candidates"] == 10

    def test_default_pool_size_used_when_config_omits_it(self, monkeypatch, tmp_path):
        config_path = _config(tmp_path)
        freeze_calls, semantic_calls = _run_sketchfab_fetch(
            monkeypatch, tmp_path, semantic_select=True, config_path=config_path)

        freeze_calls[0]["semantic_filter_fn"]([{"uid": "abc123"}])
        assert semantic_calls[-1]["max_candidates"] == 6

    def test_entry_none_with_semantic_on_prints_fallback_hint(self, monkeypatch, tmp_path, capsys):
        config_path = _config(tmp_path)
        _run_sketchfab_fetch(
            monkeypatch, tmp_path, semantic_select=True, config_path=config_path,
            freeze_result=None)
        out = capsys.readouterr().out
        assert "--no-semantic-select" in out

    def test_entry_none_with_semantic_off_omits_semantic_hint(self, monkeypatch, tmp_path, capsys):
        """The fallback hint only makes sense when the semantic gate was
        actually on — must not appear for a plain keyword-only miss."""
        config_path = _config(tmp_path)
        _run_sketchfab_fetch(
            monkeypatch, tmp_path, semantic_select=False, config_path=config_path,
            freeze_result=None)
        out = capsys.readouterr().out
        assert "--no-semantic-select" not in out
