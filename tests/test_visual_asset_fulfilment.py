from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.visual_asset_fulfilment import (
    FULFILMENT_FORMAT,
    fulfil_image_assets,
)
from src.visual_reasoning_compiler import VisualReasoningCompilerError, sha256_file
from src.visual_reasoning_reconcile import CLOSED_FORMAT, RECONCILIATION_FORMAT


def _write_case(tmp_path: Path, *, sketchfab_license: str = "by",
                ambiguous: bool = False) -> tuple[Path, Path, Path, Path]:
    lookups = tmp_path / "lookups"
    unity = lookups / "unity"
    unity.mkdir(parents=True)
    asset_root = tmp_path / "assets"
    asset_root.mkdir()

    character = asset_root / "red-explorer.fbx"
    character.write_bytes(b"character-fbx")
    base_entry = {
        "filename": character.name,
        "relpath": character.name,
        "root": str(asset_root),
        "format": "fbx",
        "source": "kenney",
        "category": "character",
        "keywords": ["red", "explorer", "backpack", "character"],
        "kind_hints": ["character"],
        "generic_score": 0,
    }
    base_entries = [base_entry]
    if ambiguous:
        duplicate = dict(base_entry)
        duplicate["relpath"] = "alt/red-explorer.fbx"
        (asset_root / "alt").mkdir()
        (asset_root / duplicate["relpath"]).write_bytes(b"character-fbx-copy")
        base_entries.append(duplicate)
    (unity / "asset_catalog.json").write_text(
        json.dumps({"version": 1, "assets": base_entries}), encoding="utf-8"
    )

    orb = asset_root / "glowing-orb.fbx"
    orb.write_bytes(b"orb-fbx")
    (unity / "sketchfab_catalog.json").write_text(json.dumps({
        "version": 1,
        "entries": [{
            "filename": orb.name,
            "relpath": orb.name,
            "root": str(asset_root),
            "format": "fbx",
            "source": "sketchfab",
            "category": "prop",
            "keywords": ["glowing", "orb", "node", "prop"],
            "kind_hints": ["prop"],
            "license": sketchfab_license,
            "sketchfab_uid": "orb-1",
            "author": "Example",
        }],
    }), encoding="utf-8")

    closed = tmp_path / "closed_image_ir.json"
    closed.write_text(json.dumps({
        "format": CLOSED_FORMAT,
        "stage": "3_image_reasoning_reconciliation",
        "status": "closed",
        "request_fingerprint": "request-fingerprint",
        "inventory": {
            "visible_elements": [
                {
                    "label": "red-hat explorer",
                    "category": "character",
                    "description": "A small explorer with a backpack.",
                    "model_hint": "stylized explorer character backpack",
                },
                {
                    "label": "glowing commit nodes",
                    "category": "prop",
                    "description": "Glowing orb-like nodes.",
                    "model_hint": "glowing orb node prop",
                },
            ],
        },
        "asset_requests": [
            {
                "inventory_label": "red-hat explorer",
                "query": "stylized explorer backpack",
                "concept_reference": "red-hat explorer",
            },
            {
                "inventory_label": "glowing commit nodes",
                "query": "glowing orb game prop",
                "concept_reference": "glowing commit nodes",
            },
        ],
    }), encoding="utf-8")
    reconciliation = tmp_path / "reconciliation.json"
    reconciliation.write_text(json.dumps({
        "format": RECONCILIATION_FORMAT,
        "stage": "3_image_reasoning_reconciliation",
        "status": "closed",
        "request_fingerprint": "request-fingerprint",
        "outputs": {"closed_image_ir": {
            "path": str(closed), "sha256": sha256_file(closed),
        }},
    }), encoding="utf-8")
    return closed, reconciliation, lookups, asset_root


def test_fulfilment_selects_local_and_frozen_assets(tmp_path: Path) -> None:
    closed, reconciliation, lookups, asset_root = _write_case(tmp_path)
    output = tmp_path / "asset_fulfilment.json"
    manifest = tmp_path / "asset_fulfilment.manifest.json"

    result = fulfil_image_assets(
        closed_ir=closed,
        reconciliation_manifest=reconciliation,
        output=output,
        manifest=manifest,
        lookups_dir=str(lookups),
        asset_root=asset_root,
    )

    assert result == manifest
    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["format"] == FULFILMENT_FORMAT
    assert data["status"] == "fulfilled"
    assert data["authority"]["llm_called_here"] is False
    assert data["authority"]["network_called_here"] is False
    assert [item["selection"]["filename"] for item in data["assets"]] == [
        "red-explorer.fbx", "glowing-orb.fbx",
    ]
    assert data["assets"][1]["selection"]["license"] == "by"
    assert data["assets"][0]["selection"]["file_sha256"] == sha256_file(
        asset_root / "red-explorer.fbx"
    )
    report = json.loads(manifest.read_text(encoding="utf-8"))
    assert report["outputs"]["asset_fulfilment"]["sha256"] == sha256_file(output)


def test_fulfilment_records_rejected_unlicensed_request(tmp_path: Path) -> None:
    closed, reconciliation, lookups, asset_root = _write_case(
        tmp_path, sketchfab_license="unknown"
    )
    output = tmp_path / "asset_fulfilment.json"
    manifest = tmp_path / "asset_fulfilment.manifest.json"

    fulfil_image_assets(
        closed_ir=closed,
        reconciliation_manifest=reconciliation,
        output=output,
        manifest=manifest,
        lookups_dir=str(lookups),
        asset_root=asset_root,
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["status"] == "rejected"
    assert data["rejections"][0]["inventory_label"] == "glowing commit nodes"
    assert "licensed" in data["rejections"][0]["reason"]


def test_fulfilment_rejects_equal_best_matches(tmp_path: Path) -> None:
    closed, reconciliation, lookups, asset_root = _write_case(tmp_path, ambiguous=True)
    output = tmp_path / "asset_fulfilment.json"
    manifest = tmp_path / "asset_fulfilment.manifest.json"

    fulfil_image_assets(
        closed_ir=closed,
        reconciliation_manifest=reconciliation,
        output=output,
        manifest=manifest,
        lookups_dir=str(lookups),
        asset_root=asset_root,
    )

    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["status"] == "rejected"
    assert any(
        item.get("reason", "").startswith("ambiguous")
        for item in data["rejections"]
    )


def test_fulfilment_requires_reconciliation_hash(tmp_path: Path) -> None:
    closed, reconciliation, lookups, asset_root = _write_case(tmp_path)
    reconciliation.write_text(
        reconciliation.read_text(encoding="utf-8").replace(
            sha256_file(closed), "0" * 64
        ),
        encoding="utf-8",
    )

    with pytest.raises(VisualReasoningCompilerError, match="hash"):
        fulfil_image_assets(
            closed_ir=closed,
            reconciliation_manifest=reconciliation,
            output=tmp_path / "asset_fulfilment.json",
            manifest=tmp_path / "asset_fulfilment.manifest.json",
            lookups_dir=str(lookups),
            asset_root=asset_root,
        )
