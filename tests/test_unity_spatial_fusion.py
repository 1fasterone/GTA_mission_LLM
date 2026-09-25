"""Focused tests for deterministic Slice 6.3 hybrid spatial fusion."""

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
)
from src.unity_spatial_roles import write_spatial_roles
from src.unity_visual_bindings import bind_visual_assets
from src.visual_reasoning_compiler import sha256_bytes, sha256_file
from src.visual_reasoning_reconcile import CLOSED_FORMAT
from src.visual_spatial_extraction import (
    SPATIAL_MANIFEST_FORMAT,
    validate_image_spatial_response,
)


def _schema(*, ambiguous: bool = False) -> UnitySchema:
    entities = [
        EntitySpec(
            name="PlayerBoatLeft" if ambiguous else "PlayerBoat",
            kind="vehicle",
            tag="Player",
            model_hint="speedboat",
            placement="single",
        ),
        EntitySpec(
            name="PlayerBoatRight" if ambiguous else "CheckpointArch",
            kind="vehicle" if ambiguous else "checkpoint",
            model_hint="speedboat" if ambiguous else "checkpoint arch",
            placement="single",
        ),
    ]
    if ambiguous:
        entities[0].tag = ""
    else:
        entities.append(EntitySpec(
            name="PineTrees",
            kind="prop",
            model_hint="pine tree",
            placement="scatter",
            count=3,
        ))
    return UnitySchema(
        project_name="RallyBoat",
        company_name="Test",
        description="A boat race through a river checkpoint.",
        game_type="3D",
        platform="pc",
        render_pipeline="URP",
        locomotion="drive",
        scenes=["Main"],
        entities=entities,
        meta={"concept": "The PlayerBoat follows the river to the CheckpointArch."},
    )


def _closed(*, ambiguous: bool = False) -> dict:
    labels = ["Player Boat"] if ambiguous else [
        "Player Boat", "Checkpoint Archway", "Pine Trees", "River"
    ]
    elements = [
        {
            "label": "Player Boat",
            "category": "vehicle",
            "description": "A boat in the foreground.",
            "model_hint": "speedboat",
        },
    ]
    if not ambiguous:
        elements.extend([
            {
                "label": "Checkpoint Archway",
                "category": "landmark",
                "description": "An arch across the water.",
                "model_hint": "checkpoint arch",
            },
            {
                "label": "Pine Trees",
                "category": "environment",
                "description": "Trees beside the river.",
                "model_hint": "pine tree",
            },
            {
                "label": "River",
                "category": "environment",
                "description": "A broad water surface.",
                "model_hint": None,
            },
        ])
    return {
        "format": CLOSED_FORMAT,
        "stage": "3_image_reasoning_reconciliation",
        "status": "closed",
        "request_fingerprint": "closed-request-fingerprint",
        "inventory": {"visible_elements": elements},
        "entity_matches": [
            {
                "reference": "speedboat" if ambiguous else "PlayerBoat",
                "inventory_label": "Player Boat",
            },
        ] if ambiguous else [
            {"reference": "PlayerBoat", "inventory_label": "Player Boat"},
            {"reference": "CheckpointArch", "inventory_label": "Checkpoint Archway"},
        ],
        "coverage": {"required_inventory_labels": labels[:2]},
    }


def _evidence(labels: list[str]) -> dict:
    anchors = {
        "Player Boat": {
            "label": "Player Boat",
            "bbox": {"x": 0.35, "y": 0.68, "w": 0.2, "h": 0.18},
            "layer": "foreground",
            "surface": "water",
            "facing": "forward",
            "occlusion": "none",
            "confidence": 0.98,
        },
        "Checkpoint Archway": {
            "label": "Checkpoint Archway",
            "bbox": {"x": 0.42, "y": 0.4, "w": 0.24, "h": 0.2},
            "layer": "midground",
            "surface": "water",
            "facing": "forward",
            "occlusion": "none",
            "confidence": 0.9,
        },
        "Pine Trees": {
            "label": "Pine Trees",
            "bbox": {"x": 0.05, "y": 0.3, "w": 0.2, "h": 0.3},
            "layer": "background",
            "surface": "ground",
            "facing": "unknown",
            "occlusion": "partial",
            "confidence": 0.8,
        },
        "River": {
            "label": "River",
            "bbox": {"x": 0.0, "y": 0.35, "w": 1.0, "h": 0.65},
            "layer": "background",
            "surface": "water",
            "facing": "unknown",
            "occlusion": "none",
            "confidence": 0.96,
        },
    }
    payload = {
        "frame": {"horizon_y": 0.42, "vanishing_x": None},
        "surface_regions": [{
            "surface": "water",
            "bbox": {"x": 0.0, "y": 0.35, "w": 1.0, "h": 0.65},
            "confidence": 0.95,
        }],
        "anchors": [anchors[label] for label in labels],
        "relations": ([{
            "source": "Player Boat",
            "target": "Checkpoint Archway",
            "relation": "below",
            "confidence": 0.88,
        }] if "Checkpoint Archway" in labels else []),
        "clusters": [],
        "uncertainties": [],
    }
    return validate_image_spatial_response(payload, labels)


def _artifacts(tmp_path: Path, *, ambiguous: bool = False) -> tuple[dict[str, Path], list[str]]:
    schema_model = _schema(ambiguous=ambiguous)
    schema_path = tmp_path / "RallyBoat.schema.json"
    schema_raw = schema_model.model_dump_json(indent=2).encode("utf-8")
    schema_path.write_bytes(schema_raw)

    roles_path = tmp_path / "RallyBoat.spatial-roles.json"
    roles_manifest = tmp_path / "RallyBoat.spatial-roles.manifest.json"
    write_spatial_roles(schema_path, roles_path, roles_manifest)

    closed_data = _closed(ambiguous=ambiguous)
    closed_path = tmp_path / "closed.json"
    closed_path.write_text(json.dumps(closed_data), encoding="utf-8")
    closed_raw = closed_path.read_bytes()
    labels = [str(item["label"]) for item in closed_data["inventory"]["visible_elements"]]

    evidence_path = tmp_path / "image-spatial-evidence.json"
    evidence_data = _evidence(labels)
    evidence_raw = (json.dumps(evidence_data, indent=2) + "\n").encode("utf-8")
    evidence_path.write_bytes(evidence_raw)
    evidence_manifest = tmp_path / "image-spatial.json"
    evidence_manifest.write_text(json.dumps({
        "format": SPATIAL_MANIFEST_FORMAT,
        "stage": "6.1_image_spatial_extraction",
        "status": "complete",
        "inputs": {
            "closed_image_ir": {
                "path": str(closed_path),
                "sha256": sha256_bytes(closed_raw),
                "request_fingerprint": closed_data["request_fingerprint"],
            },
        },
        "outputs": {
            "image_spatial_evidence": {
                "path": str(evidence_path),
                "sha256": sha256_bytes(evidence_raw),
            },
        },
    }, indent=2), encoding="utf-8")
    return {
        "closed_ir": closed_path,
        "evidence": evidence_path,
        "evidence_manifest": evidence_manifest,
        "schema": schema_path,
        "roles": roles_path,
        "roles_manifest": roles_manifest,
    }, labels


def test_fusion_binds_declared_entities_and_keeps_adjacency_unit_free(tmp_path: Path) -> None:
    paths, labels = _artifacts(tmp_path)
    output = tmp_path / "RallyBoat.spatial-fusion.json"
    manifest = tmp_path / "RallyBoat.spatial-fusion.manifest.json"

    result = fuse_spatial_evidence(**paths, output=output, manifest=manifest)

    assert result == manifest
    fused = json.loads(output.read_text(encoding="utf-8"))
    assert fused["format"] == SPATIAL_FUSION_FORMAT
    assert fused["authority"]["world_coordinates_emitted_here"] is False
    assert fused["counts"] == {
        "declared_schema_entities": 3,
        "bound_entities": 2,
        "defaulted_entities": 1,
        "bound_visual_relations": 1,
        "unbound_visual_labels": 2,
    }
    player = next(row for row in fused["entity_regions"] if row["entity"] == "PlayerBoat")
    assert player["composition"]["center"] == {"x": 0.45, "y": 0.77}
    assert player["composition"]["extent"] == {"w": 0.2, "h": 0.18}
    assert player["composition"]["applied_surface"] == "ground"
    assert any("PlayerBoat" in warning and "water" in warning for warning in fused["warnings"])
    pine = next(row for row in fused["entity_regions"] if row["entity"] == "PineTrees")
    assert pine["binding_status"] == "defaulted"
    assert pine["composition"]["default_profile"] == "scenery_background"
    assert any("PineTrees" in warning for warning in fused["warnings"])
    assert any(
        constraint["kind"] == "semantic_adjacency"
        for constraint in fused["constraints"]
    )
    assert fused["unbound_visual_labels"] == ["Pine Trees", "River"]
    manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
    assert manifest_data["format"] == SPATIAL_FUSION_MANIFEST_FORMAT
    assert manifest_data["outputs"]["hybrid_spatial_fusion"]["sha256"] == sha256_file(output)
    assert labels == ["Player Boat", "Checkpoint Archway", "Pine Trees", "River"]


def test_fusion_rejects_ambiguous_declared_entity_binding(tmp_path: Path) -> None:
    paths, _ = _artifacts(tmp_path, ambiguous=True)

    with pytest.raises(SpatialFusionError, match="ambiguously"):
        fuse_spatial_evidence(
            **paths,
            output=tmp_path / "fusion.json",
            manifest=tmp_path / "fusion.manifest.json",
        )


def test_fusion_rejects_changed_evidence_bytes(tmp_path: Path) -> None:
    paths, _ = _artifacts(tmp_path)
    paths["evidence"].write_text("{}", encoding="utf-8")

    with pytest.raises(SpatialFusionError, match="hash does not match"):
        fuse_spatial_evidence(
            **paths,
            output=tmp_path / "fusion.json",
            manifest=tmp_path / "fusion.manifest.json",
        )


def test_visual_binding_reuses_one_unique_schema_group_for_repeated_labels() -> None:
    schema = UnitySchema(
        project_name="PlaneCombat",
        company_name="Test",
        description="aircraft group",
        game_type="3D",
        platform="pc",
        render_pipeline="URP",
        scenes=["Main"],
        entities=[EntitySpec(
            name="EnemyAircraftGroup",
            kind="enemy",
            model_hint="aircraft",
            placement="scatter",
            count=4,
        )],
    )
    records = [
        {
            "inventory_label": label,
            "element": {
                "label": label,
                "category": "vehicle",
                "model_hint": "aircraft",
            },
            "query": "aircraft",
            "selection": {"_spatial_label": label},
        }
        for label in ("aircraft_1", "aircraft_2", "aircraft_3", "aircraft_4")
    ]

    result = bind_visual_assets(schema, records)

    assert list(result) == ["EnemyAircraftGroup"]
    assert result["EnemyAircraftGroup"] == {"_spatial_label": "aircraft_1"}
