"""`unity-catalog --embed` (cli/unity_assets.py) — CLI-level coverage for the
flag that precomputes catalog embeddings (docs/COMPILER_AUDIT.md Gap E /
solution #7, src/unity_semantic_match.py). No prior test file touched this
command at all.

Offline and deterministic: build_catalog (real filesystem walk) and
build_and_write_catalog_embeddings (real bge-small embedder) are both
monkeypatched at their dotted-string origin — cli/unity_assets.py does
deferred `from src.unity_asset_catalog import (..., build_catalog, ...)` /
`from src.unity_semantic_match import build_and_write_catalog_embeddings`
INSIDE the command function body, so a patch must target the origin module,
not a name imported into cli.unity_assets itself.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import typer


def _fake_catalog(n: int = 1) -> dict:
    return {
        "version": 1,
        "vocabulary": {"categories": ["prop"], "tags": []},
        "assets": [
            {"relpath": f"asset{i}.fbx", "filename": f"asset{i}.fbx",
             "category": "prop", "keywords": ["prop"], "kind_hints": ["prop"],
             "format": "fbx", "curated": False, "source": "kenney"}
            for i in range(n)
        ],
    }


def _run_unity_catalog(monkeypatch, tmp_path, *, embed: bool,
                       dry_run: bool = False, check: bool = False):
    import src.unity_asset_catalog as catalog_mod
    monkeypatch.setattr(catalog_mod, "build_catalog", lambda *a, **kw: _fake_catalog())

    import src.unity_semantic_match as semantic_match_mod
    calls = []

    def _fake_build_and_write(catalog, lookups_dir):
        calls.append({"catalog": catalog, "lookups_dir": lookups_dir})
        return Path(lookups_dir) / "unity" / "asset_catalog_embeddings.npz"

    monkeypatch.setattr(semantic_match_mod, "build_and_write_catalog_embeddings",
                        _fake_build_and_write)

    from cli.unity_assets import unity_catalog

    try:
        unity_catalog(
            config=str(tmp_path / "nonexistent-config.yaml"),
            lookups=str(tmp_path / "lookups"),
            dry_run=dry_run, check=check, measure=False, embed=embed)
    except typer.Exit:
        pass  # --check on a catalog that was never written exits 1 (STALE)

    return calls


class TestEmbedFlag:
    def test_embed_flag_builds_and_writes_catalog_embeddings(self, monkeypatch, tmp_path):
        calls = _run_unity_catalog(monkeypatch, tmp_path, embed=True)
        assert len(calls) == 1
        assert calls[0]["catalog"]["assets"][0]["relpath"] == "asset0.fbx"
        assert calls[0]["lookups_dir"] == str(tmp_path / "lookups")

    def test_embed_flag_absent_never_calls_it(self, monkeypatch, tmp_path):
        calls = _run_unity_catalog(monkeypatch, tmp_path, embed=False)
        assert calls == []

    def test_embed_skipped_on_dry_run(self, monkeypatch, tmp_path):
        """--dry-run returns before the catalog is even written — --embed
        must not fire against a catalog that was never persisted."""
        calls = _run_unity_catalog(monkeypatch, tmp_path, embed=True, dry_run=True)
        assert calls == []

    def test_embed_skipped_on_check(self, monkeypatch, tmp_path):
        """--check is the CI staleness gate — it returns before writing too."""
        calls = _run_unity_catalog(monkeypatch, tmp_path, embed=True, check=True)
        assert calls == []
