"""Focused tests for the deterministic Slice-6.7 spatial freeze."""

from __future__ import annotations

import json
from pathlib import Path

from src.unity_schema import EntitySpec, UnitySchema
from src.unity_spatial_freeze import (audit_frozen_models, _transform_snapshot,
                                      freeze_spatial_build)
from src.unity_spatial_pipeline import freeze_spatial_sidecars
from src.visual_reasoning_compiler import sha256_file


def _write_json(path: Path, value: dict) -> str:
    raw = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    path.write_text(raw, encoding="utf-8")
    return sha256_file(path)


def test_transform_snapshot_is_ordered_by_schema_and_hashes_coordinates() -> None:
    schema = UnitySchema(
        project_name="TransformTest",
        company_name="Test",
        description="A boat crosses a river.",
        game_type="3D",
        platform="pc",
        render_pipeline="URP",
        locomotion="drive",
        scenes=["Main"],
        entities=[
            EntitySpec(name="PlayerBoat", kind="vehicle", tag="Player"),
            EntitySpec(name="Checkpoint", kind="checkpoint"),
        ],
    )
    world = {
        "layout": {
            "entities": [
                {
                    "entity": "Checkpoint",
                    "origin_offset": [4.0, 5.0],
                    "group_radius": 2.0,
                    "elevation": 0.0,
                    "instance_positions": [[4.0, 0.0, 5.0]],
                    "instance_yaws": [90.0],
                    "path_points": [],
                },
                {
                    "entity": "PlayerBoat",
                    "origin_offset": [0.0, 0.0],
                    "group_radius": 1.0,
                    "elevation": 0.0,
                    "instance_positions": [[0.0, 0.0, 0.0]],
                    "instance_yaws": [0.0],
                    "path_points": [],
                },
            ]
        }
    }

    snapshot = _transform_snapshot(schema, world)

    assert [row["entity"] for row in snapshot["entities"]] == [
        "PlayerBoat", "Checkpoint"
    ]
    assert snapshot["entities"][1]["instance_positions"] == [[4.0, 0.0, 5.0]]
    assert len(snapshot["sha256"]) == 64


def test_frozen_model_audit_rejects_obvious_identity_contradictions() -> None:
    report = audit_frozen_models([
        {"inventory_label": "crusader", "selection": {
            "filename": "crusader-mug.fbx", "file_sha256": "x",
        }},
        {"inventory_label": "goblin", "selection": {
            "filename": "fictional-supercar-v12-goblin.fbx",
            "file_sha256": "y",
        }},
        {"inventory_label": "horse", "selection": {
            "filename": "rocking-horse.fbx", "file_sha256": "z",
        }},
        {"inventory_label": "pikeman", "selection": {
            "filename": "mordor-orck-armor.fbx", "file_sha256": "w",
        }},
    ])
    assert report["status"] == "rejected"
    assert {item["inventory_label"] for item in report["findings"]} == {
        "crusader", "goblin", "horse", "pikeman",
    }


def test_frozen_model_audit_keeps_uncertain_models_reviewable() -> None:
    report = audit_frozen_models([
        {"inventory_label": "peasant", "selection": {
            "filename": "medieval-peasant-female-outfit.fbx",
            "file_sha256": "x",
        }},
        {"inventory_label": "knight", "selection": {
            "filename": "bulky-knight.fbx", "file_sha256": "y",
        }},
    ])
    assert report["status"] == "pass"
    assert report["findings"] == []


def _make_fixture(root: Path) -> dict[str, Path]:
    run = root / "run"
    project = root / "Build" / "FreezeTest"
    run.mkdir(parents=True)
    project.mkdir(parents=True)

    concept_text = "VISIBLE ENTITY REFERENCES:\n- aircraft\nEND VISIBLE ENTITY REFERENCES\n\nFly to the checkpoint."
    schema = UnitySchema(
        project_name="FreezeTest",
        company_name="Test",
        description="A plane flies to a checkpoint.",
        game_type="3D",
        platform="pc",
        render_pipeline="URP",
        locomotion="fly",
        scenes=["Main"],
        meta={"concept": concept_text},
        entities=[
            EntitySpec(name="PlayerAircraft", kind="vehicle", tag="Player"),
        ],
    )
    schema_path = project / "FreezeTest.schema.json"
    schema_path.write_text(schema.model_dump_json(indent=2) + "\n", encoding="utf-8")

    source = root / "plane.png"
    source.write_bytes(b"not-a-real-png-for-provenance")
    board = run / "board.png"
    board.write_bytes(source.read_bytes())
    source_hash = sha256_file(source)
    board_hash = sha256_file(board)
    board_manifest = run / "board.import.json"
    board_manifest_hash = _write_json(board_manifest, {
        "format": "visual-reasoning-compiler/imported-board/v1",
        "source_board": {"path": str(source), "sha256": source_hash},
        "board": {"path": str(board), "sha256": board_hash},
    })

    inventory = run / "visual_inventory.json"
    inventory_hash = _write_json(inventory, {
        "format": "visual-reasoning-compiler/visual-inventory/v1",
        "scene_summary": "An aircraft above a checkpoint.",
        "visible_elements": [{
            "label": "aircraft",
            "category": "vehicle",
            "description": "aircraft",
            "model_hint": "aircraft",
        }],
        "sketchfab_queries": [{"label": "aircraft", "query": "aircraft"}],
        "visible_text": [],
        "uncertainties": [],
    })
    concept = run / "Concept.txt"
    concept.write_text(concept_text + "\n", encoding="utf-8")
    concept_hash = sha256_file(concept)
    reasoning = run / "reasoning.json"
    reasoning_hash = _write_json(reasoning, {
        "format": "visual-reasoning-compiler/image-reasoning/v1",
        "board": {"path": str(board), "sha256": board_hash},
        "board_manifest": {"path": str(board_manifest), "sha256": board_manifest_hash},
        "outputs": {
            "visual_inventory": {"path": str(inventory), "sha256": inventory_hash},
            "concept": {"path": str(concept), "sha256": concept_hash},
        },
    })

    closed = run / "closed_image_ir.json"
    closed_value = {
        "format": "visual-reasoning-compiler/closed-image-ir/v1",
        "stage": "3_image_reasoning_reconciliation",
        "status": "closed",
        "board": {"path": str(board), "sha256": board_hash},
        "inputs": {
            "reasoning_manifest": {"path": str(reasoning), "sha256": reasoning_hash},
            "visual_inventory": {"path": str(inventory), "sha256": inventory_hash},
            "concept": {"path": str(concept), "sha256": concept_hash},
        },
        "inventory": json.loads(inventory.read_text(encoding="utf-8")),
        "concept": {"text": concept_text},
        "entity_matches": [{"inventory_label": "aircraft", "reference": "aircraft"}],
        "asset_requests": [{"inventory_label": "aircraft", "query": "aircraft"}],
        "request_fingerprint": "request-fingerprint",
    }
    closed_hash = _write_json(closed, closed_value)
    reconciliation = run / "reconciliation.json"
    _write_json(reconciliation, {
        "format": "visual-reasoning-compiler/reconciliation/v1",
        "stage": "3_image_reasoning_reconciliation",
        "status": "closed",
        "inputs": closed_value["inputs"],
        "request_fingerprint": "request-fingerprint",
        "outputs": {"closed_image_ir": {"path": str(closed), "sha256": closed_hash}},
    })

    evidence = run / "image_spatial_evidence.json"
    evidence_hash = _write_json(evidence, {
        "format": "visual-reasoning-compiler/image-spatial-evidence/v1",
        "frame": {"horizon_y": 0.5, "vanishing_x": 0.5},
        "surface_regions": [],
        "anchors": [{
            "label": "aircraft",
            "bbox": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2},
            "layer": "foreground",
            "surface": "air",
            "facing": "forward",
            "occlusion": "none",
            "confidence": 1.0,
        }],
        "relations": [],
        "clusters": [],
        "uncertainties": [],
    })
    evidence_manifest = run / "image_spatial_evidence.manifest.json"
    _write_json(evidence_manifest, {
        "format": "visual-reasoning-compiler/image-spatial-extraction/v1",
        "stage": "6.1_image_spatial_extraction",
        "status": "complete",
        "inputs": {
            "board": {"path": str(board), "sha256": board_hash},
            "closed_image_ir": {"path": str(closed), "sha256": closed_hash},
        },
        "outputs": {"image_spatial_evidence": {"path": str(evidence), "sha256": evidence_hash}},
    })

    catalog = run / "asset_catalog.json"
    catalog_hash = _write_json(catalog, {"format": "test-catalog/v1", "assets": []})
    assets = run / "asset_fulfilment.json"
    assets_hash = _write_json(assets, {
        "format": "visual-reasoning-compiler/asset-fulfilment/v1",
        "stage": "4_asset_fulfilment",
        "status": "fulfilled",
        "closed_request_fingerprint": "request-fingerprint",
        "fulfilment_fingerprint": "fulfilment-fingerprint",
        "assets": [{
            "inventory_label": "aircraft",
            "query": "aircraft",
            "selection": {
                "category": "vehicle",
                "filename": "aircraft.fbx",
                "file_sha256": "asset-bytes-hash",
                "source": "test",
            },
        }],
        "rejections": [],
        "catalog_preflight": {"status": "pass", "checked_request_count": 1},
        "inputs": {
            "closed_image_ir": {"path": str(closed), "sha256": closed_hash},
            "catalog_files": [{"path": str(catalog), "sha256": catalog_hash}],
        },
    })
    assets_manifest = run / "asset_fulfilment.manifest.json"
    _write_json(assets_manifest, {
        "format": "visual-reasoning-compiler/asset-fulfilment/v1",
        "stage": "4_asset_fulfilment",
        "status": "fulfilled",
        "closed_request_fingerprint": "request-fingerprint",
        "fulfilment_fingerprint": "fulfilment-fingerprint",
        "outputs": {"asset_fulfilment": {"path": str(assets), "sha256": assets_hash}},
    })

    sidecars = freeze_spatial_sidecars(
        project,
        schema,
        closed_image_ir=json.loads(closed.read_text(encoding="utf-8")),
        image_spatial_evidence=json.loads(evidence.read_text(encoding="utf-8")),
        measured_footprints={"PlayerAircraft": 1.0},
    )
    compile_manifest = root / "Build" / "visual_compile.manifest.json"
    _write_json(compile_manifest, {
        "format": "visual-reasoning-compiler/compile/v1",
        "stage": "5_visual_compile",
        "status": "compiled",
        "inputs": {
            "closed_image_ir": {"path": str(closed), "sha256": closed_hash},
            "asset_fulfilment": {"path": str(assets), "sha256": assets_hash},
            "asset_manifest": {"path": str(assets_manifest), "sha256": sha256_file(assets_manifest)},
        },
        "outputs": {
            "project_root": str(project),
            "schema": {"path": str(schema_path), "sha256": sha256_file(schema_path)},
        },
        "binding_report": {"bindings": []},
    })
    config = root / "config.yaml"
    config.write_text("test: true\n", encoding="utf-8")
    return {
        "project": project,
        "source": source,
        "reasoning": reasoning,
        "inventory": inventory,
        "concept": concept,
        "closed": closed,
        "reconciliation": reconciliation,
        "evidence": evidence,
        "evidence_manifest": evidence_manifest,
        "assets": assets,
        "assets_manifest": assets_manifest,
        "compile_manifest": compile_manifest,
        "config": config,
        "sidecars": sidecars,
    }


def test_freeze_writes_hash_linked_accepted_build(tmp_path: Path) -> None:
    files = _make_fixture(tmp_path)

    output = freeze_spatial_build(
        project_root=files["project"],
        source_image=files["source"],
        reasoning_manifest=files["reasoning"],
        inventory=files["inventory"],
        concept=files["concept"],
        closed_ir=files["closed"],
        reconciliation_manifest=files["reconciliation"],
        image_evidence=files["evidence"],
        image_evidence_manifest=files["evidence_manifest"],
        asset_fulfilment=files["assets"],
        asset_manifest=files["assets_manifest"],
        compile_manifest=files["compile_manifest"],
        config=files["config"],
    )

    frozen = json.loads(output.read_text(encoding="utf-8"))
    manifest = json.loads(
        output.with_name(output.stem + ".manifest.json").read_text(encoding="utf-8")
    )
    assert frozen["status"] == "frozen"
    assert frozen["validation"]["status"] == "pass"
    assert frozen["lineage_sha256"]
    assert frozen["transforms"]["sha256"]
    assert manifest["outputs"]["spatial_freeze"]["sha256"] == sha256_file(output)
    assert frozen["lineage"]["source"]["source_board_same_bytes"] is True
