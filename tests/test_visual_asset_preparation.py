"""Tests for the explicit pre-Slice-4 Sketchfab preparation boundary."""

from __future__ import annotations

import json
from pathlib import Path

from src.visual_asset_preparation import (
    PREPARATION_FORMAT,
    prepare_image_assets,
)
from src.visual_reasoning_compiler import sha256_file
from src.visual_reasoning_reconcile import CLOSED_FORMAT, RECONCILIATION_FORMAT


def _case(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    lookups = tmp_path / "lookups"
    (lookups / "unity").mkdir(parents=True)
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "peasant.fbx").write_bytes(b"local-peasant")
    (lookups / "unity" / "asset_catalog.json").write_text(json.dumps({
        "version": 1,
        "assets": [{
            "filename": "peasant.fbx",
            "relpath": "peasant.fbx",
            "root": str(assets),
            "format": "fbx",
            "source": "kenney",
            "category": "character",
            "keywords": ["peasant", "character"],
            "kind_hints": ["character"],
        }],
    }), encoding="utf-8")
    closed = tmp_path / "closed.json"
    closed.write_text(json.dumps({
        "format": CLOSED_FORMAT,
        "stage": "3_image_reasoning_reconciliation",
        "status": "closed",
        "request_fingerprint": "fp",
        "inventory": {"visible_elements": [{
            "label": "peasant",
            "category": "character",
            "model_hint": "peasant",
            "description": "A peasant.",
        }]},
        "asset_requests": [{
            "inventory_label": "peasant",
            "query": "peasant",
            "concept_reference": "peasant",
        }],
    }), encoding="utf-8")
    reconciliation = tmp_path / "reconciliation.json"
    reconciliation.write_text(json.dumps({
        "format": RECONCILIATION_FORMAT,
        "stage": "3_image_reasoning_reconciliation",
        "status": "closed",
        "request_fingerprint": "fp",
        "outputs": {"closed_image_ir": {
            "path": str(closed), "sha256": sha256_file(closed),
        }},
    }), encoding="utf-8")
    return closed, reconciliation, lookups, assets


def test_preparation_reuses_no_llm_and_records_catalog_fallback(
    tmp_path: Path, monkeypatch,
) -> None:
    closed, reconciliation, lookups, _assets = _case(tmp_path)

    class _NoTokenClient:
        def __init__(self, _cfg):
            pass

        def available(self) -> bool:
            return False

    monkeypatch.setattr(
        "src.visual_asset_preparation.SketchfabClient", _NoTokenClient
    )
    output = tmp_path / "preparation.json"
    prepare_image_assets(
        closed_ir=closed,
        reconciliation_manifest=reconciliation,
        output=output,
        lookups_dir=str(lookups),
        sketchfab_config={"enabled": True},
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["format"] == PREPARATION_FORMAT
    assert data["selection_policy"] == "sketchfab_first_then_local_catalog"
    assert data["authority"]["llm_called_here"] is False
    assert data["authority"]["network_attempted_here"] is False
    assert data["fallback_count"] == 1
    assert data["attempts"][0]["status"] == "fallback_catalog"
