"""Focused tests for deterministic Slice 6.3 evidence fusion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.unity_schema import EntitySpec, UnitySchema
from src.unity_spatial_fusion import (
    SPATIAL_FUSION_FORMAT,
    SPATIAL_FUSION_MANIFEST_FORMAT,
    SpatialFusionError,
    fuse_spatial_evidence,
    write_spatial_fusion,
)
from src.unity_spatial_roles import derive_spatial_roles
from src.visual_reasoning_compiler import INVENTORY_FORMAT
from src.visual_spatial_extraction import SPATIAL_FORMAT


def _schema() -> UnitySchema:
    return UnitySchema(
        project_name="FusionTest",
        company_name="Test",
        description="A boat follows a river route to a checkpoint.",
        game_type="3D",
        platform="pc",
        render_pipeline="URP",
        locomotion="drive",
        scenes=["Main"],
        entities=[
            EntitySpec(name="PlayerBoat", kind="vehicle", tag="Player"),
            EntitySpec(
                name="RiverRoute", kind="river", placement="path",
                path_points=[[0, 0, 0], [0, 0, 30]], path_width=8,
            ),
            EntitySpec(name="CheckpointArch", kind="checkpoint"),
        ],
        missions=[],
    )


def _closed() -> dict:
    return {
        "format": "visual-reasoning-compiler/closed-image-ir/v1",
        "stage": "3_image_reasoning_reconciliation",
        "status": "closed",
        "request_fingerprint": "closed-request",
        "inventory": {
            "format": INVENTORY_FORMAT,
            "scene_summary": "A boat follows a river to an arch.",
            "visible_elements": [
                {
                    "label": "Player Boat",
                    "category": "vehicle",
                    "description": "A boat in the foreground.",
                    "model_hint": "speedboat",
                },
                {
                    "label": "Checkpoint Archway",
                    "category": "landmark",
                    "description": "An arch across the river.",
                    "model_hint": "checkpoint arch",
                },
                {
                    "label": "River",
                    "category": "environment",
                    "description": "A broad water surface.",
                    "model_hint": None,
                },
            ],
            "sketchfab_queries": [],
            "visible_text": [],
            "uncertainties": [],
        },
        "entity_matches": [
            {"reference": "Player Boat", "inventory_label": "Player Boat"},
            {"reference": "Checkpoint Archway", "inventory_label": "Checkpoint Archway"},
            {"reference": "River", "inventory_label": "River"},
        ],
        "coverage": {
            "required_inventory_labels": ["Player Boat", "Checkpoint Archway"],
            "covered_inventory_labels": ["Checkpoint Archway", "Player Boat"],
            "optional_unreferenced_labels": [],
        },
        "asset_requests": [],
    }


def _evidence() -> dict:
    return {
        "format": SPATIAL_FORMAT,
        "frame": {"horizon_y": None, "vanishing_x": None},
        "surface_regions": [{
            "surface": "water",
            "bbox": {"x": 0.0, "y": 0.4, "w": 1.0, "h": 0.6},
            "confidence": 0.95,
        }],
        "anchors": [
            {
                "label": "Player Boat",
                "bbox": {"x": 0.2, "y": 0.65, "w": 0.2, "h": 0.15},
                "layer": "foreground", "surface": "water",
                "facing": "forward", "occlusion": "none", "confidence": 0.9,
            },
            {
                "label": "Checkpoint Archway",
                "bbox": {"x": 0.45, "y": 0.4, "w": 0.2, "h": 0.25},
                "layer": "midground", "surface": "water",
                "facing": "forward", "occlusion": "none", "confidence": 0.85,
            },
            {
                "label": "River",
                "bbox": {"x": 0.0, "y": 0.4, "w": 1.0, "h": 0.6},
                "layer": "background", "surface": "water",
                "facing": "unknown", "occlusion": "none", "confidence": 0.95,
            },
        ],
        "relations": [{
            "source": "Player Boat", "target": "Checkpoint Archway",
            "relation": "below", "confidence": 0.8,
        }],
        "clusters": [],
        "uncertainties": [],
    }


def test_fusion_binds_declared_entities_and_keeps_authority_separate() -> None:
    schema = _schema()
    fused = fuse_spatial_evidence(
        schema,
        closed_ir=_closed(),
        image_evidence=_evidence(),
        spatial_roles=derive_spatial_roles(schema),
    )

    assert fused["format"] == SPATIAL_FUSION_FORMAT
    assert fused["status"] == "complete"
    assert fused["authority"]["world_coordinates_emitted_here"] is False
    assert fused["frame"]["horizon_y"] == 0.5
    assert fused["frame"]["vanishing_x"] == 0.5
    bindings = {row["entity"]: row for row in fused["bindings"] if row["entity"]}
    assert bindings["PlayerBoat"]["status"] == "bound"
    assert bindings["CheckpointArch"]["schema_constraints"]["role"] == "checkpoint"
    assert bindings["PlayerBoat"]["visual_constraints"]["center_xy"] == [0.3, 0.725]
    assert fused["relations"][0]["source_entity"] == "PlayerBoat"
    assert any("horizon_y=0.5" in item for item in fused["defaults_applied"])


def test_fusion_abstains_on_ambiguous_schema_binding() -> None:
    schema = _schema()
    schema.entities.append(EntitySpec(name="CheckpointArchReplica", kind="checkpoint"))
    closed = _closed()
    closed["entity_matches"] = [
        {"reference": "Checkpoint", "inventory_label": "Checkpoint Archway"},
    ]
    closed["coverage"]["required_inventory_labels"] = ["Checkpoint Archway"]
    evidence = _evidence()
    evidence["anchors"] = [evidence["anchors"][1]]
    evidence["relations"] = []
    evidence["surface_regions"] = []
    roles = derive_spatial_roles(schema)

    # The closed inventory still contains three labels, so make this a strict
    # single-label case rather than weakening the 6.1 validator contract.
    closed["inventory"]["visible_elements"] = [
        closed["inventory"]["visible_elements"][1]
    ]
    closed["inventory"]["visible_elements"][0]["label"] = "Checkpoint Archway"
    fused = fuse_spatial_evidence(
        schema, closed_ir=closed, image_evidence=evidence, spatial_roles=roles
    )

    assert fused["status"] == "abstain"
    row = fused["bindings"][0]
    assert row["status"] == "abstain"
    assert row["entity"] is None


def test_fusion_rejects_schema_role_hash_mismatch() -> None:
    schema = _schema()
    roles = derive_spatial_roles(schema)
    roles["inputs"]["schema_sha256"] = "stale"

    with pytest.raises(SpatialFusionError, match="different schema"):
        fuse_spatial_evidence(
            schema,
            closed_ir=_closed(),
            image_evidence=_evidence(),
            spatial_roles=roles,
        )


def test_fusion_writer_hash_links_manifest(tmp_path: Path) -> None:
    schema = _schema()
    schema_path = tmp_path / "FusionTest.schema.json"
    closed_path = tmp_path / "closed.json"
    evidence_path = tmp_path / "evidence.json"
    roles_path = tmp_path / "roles.json"
    output = tmp_path / "fusion.json"
    manifest = tmp_path / "fusion.manifest.json"
    schema_path.write_text(schema.model_dump_json(indent=2), encoding="utf-8")
    closed_path.write_text(json.dumps(_closed()), encoding="utf-8")
    evidence_path.write_text(json.dumps(_evidence()), encoding="utf-8")
    roles_path.write_text(json.dumps(derive_spatial_roles(schema)), encoding="utf-8")

    result = write_spatial_fusion(
        schema_path,
        closed_path,
        evidence_path,
        roles_path,
        output,
        manifest,
    )

    assert result == manifest
    assert json.loads(output.read_text(encoding="utf-8"))["format"] == SPATIAL_FUSION_FORMAT
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    assert manifest_data["format"] == SPATIAL_FUSION_MANIFEST_FORMAT
    assert manifest_data["outputs"]["spatial_fusion"]["sha256"]
