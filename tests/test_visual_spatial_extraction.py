"""Focused tests for Slice 6.1 image-space evidence extraction."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.visual_reasoning_compiler import VisualReasoningCompilerError
from src.visual_spatial_extraction import (
    SPATIAL_FORMAT,
    SPATIAL_MANIFEST_FORMAT,
    VisualSpatialExtractionError,
    build_spatial_prompt,
    extract_image_spatial_evidence,
    validate_image_spatial_response,
)


BOARD_BYTES = b"fake-png-board-for-spatial-slice-6-1"
LABELS = ["Player Boat", "Checkpoint Archway", "River"]


def _inventory() -> dict:
    return {
        "format": "visual-reasoning-compiler/visual-inventory/v1",
        "scene_summary": "A boat approaches an arch on a river.",
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
                "description": "An arch across the water.",
                "model_hint": "checkpoint arch",
            },
            {
                "label": "River",
                "category": "environment",
                "description": "A broad water surface.",
                "model_hint": None,
            },
        ],
        "sketchfab_queries": [
            {"label": "Player Boat", "query": "speedboat"},
            {"label": "Checkpoint Archway", "query": "checkpoint arch"},
        ],
        "visible_text": [],
        "uncertainties": [],
    }


def _closed(board: Path) -> dict:
    return {
        "format": "visual-reasoning-compiler/closed-image-ir/v1",
        "stage": "3_image_reasoning_reconciliation",
        "status": "closed",
        "board": {
            "path": str(board),
            "mime_type": "image/png",
            "sha256": hashlib.sha256(BOARD_BYTES).hexdigest(),
        },
        "inventory": _inventory(),
        "request_fingerprint": "closed-request-fingerprint",
    }


def _response() -> dict:
    return {
        "frame": {"horizon_y": 0.42, "vanishing_x": None},
        "surface_regions": [
            {
                "surface": "water",
                "bbox": {"x": 0.0, "y": 0.35, "w": 1.0, "h": 0.65},
                "confidence": 0.95,
            }
        ],
        "anchors": [
            {
                "label": "Player Boat",
                "bbox": {"x": 0.35, "y": 0.68, "w": 0.2, "h": 0.18},
                "layer": "foreground",
                "surface": "water",
                "facing": "forward",
                "occlusion": "none",
                "confidence": 0.98,
            },
            {
                "label": "Checkpoint Archway",
                "bbox": {"x": 0.42, "y": 0.4, "w": 0.24, "h": 0.2},
                "layer": "midground",
                "surface": "water",
                "facing": "forward",
                "occlusion": "none",
                "confidence": 0.9,
            },
            {
                "label": "River",
                "bbox": {"x": 0.0, "y": 0.35, "w": 1.0, "h": 0.65},
                "layer": "background",
                "surface": "water",
                "facing": "unknown",
                "occlusion": "none",
                "confidence": 0.96,
            },
        ],
        "relations": [
            {
                "source": "Player Boat",
                "target": "Checkpoint Archway",
                "relation": "below",
                "confidence": 0.88,
            },
            {
                "source": "Player Boat",
                "target": "Checkpoint Archway",
                "relation": "near",
                "confidence": 0.72,
            },
        ],
        "clusters": [
            {
                "members": ["Checkpoint Archway", "Player Boat"],
                "cohesion": "loose",
                "confidence": 0.7,
            }
        ],
        "uncertainties": ["The exact depth of the river beyond the arch is unclear."],
    }


class FakeSpatialLLM:
    model = "qwen3.5-9b"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def chat(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        assert kwargs["thinking"] is False
        assert kwargs["seed"] == 731949
        assert len(kwargs["images"]) == 1
        return json.dumps(_response())


def test_prompt_is_image_only_with_closed_label_whitelist() -> None:
    prompt = build_spatial_prompt(LABELS)

    assert "SUPPLIED VISIBLE LABELS:" in prompt
    assert "- Player Boat" in prompt
    assert "metres" not in prompt
    assert "Do not emit any other anchor" in prompt
    assert "x + w <= 1 and y + h <= 1" in prompt
    assert "do not close the JSON object until every supplied label is present" in prompt


def test_spatial_response_is_canonical_and_strict() -> None:
    evidence = validate_image_spatial_response(_response(), LABELS)

    assert evidence["format"] == SPATIAL_FORMAT
    assert [item["label"] for item in evidence["anchors"]] == sorted(LABELS)
    assert evidence["clusters"][0]["members"] == [
        "Checkpoint Archway", "Player Boat"
    ]

    missing = _response()
    missing["anchors"] = missing["anchors"][:-1]
    with pytest.raises(VisualReasoningCompilerError, match="exactly one"):
        validate_image_spatial_response(missing, LABELS)

    contradictory = _response()
    contradictory["relations"].append({
        "source": "Player Boat",
        "target": "Checkpoint Archway",
        "relation": "above",
        "confidence": 0.4,
    })
    with pytest.raises(VisualReasoningCompilerError, match="contradicts"):
        validate_image_spatial_response(contradictory, LABELS)

    outside = _response()
    outside["anchors"][0]["bbox"]["x"] = 0.9
    with pytest.raises(VisualReasoningCompilerError, match="inside"):
        validate_image_spatial_response(outside, LABELS)


def test_extract_freezes_evidence_and_provenance(tmp_path: Path) -> None:
    board = tmp_path / "board.png"
    board.write_bytes(BOARD_BYTES)
    closed = tmp_path / "closed.json"
    closed.write_text(json.dumps(_closed(board)), encoding="utf-8")
    output = tmp_path / "run" / "image_spatial_evidence.json"
    manifest = tmp_path / "run" / "image_spatial.json"
    llm = FakeSpatialLLM()

    result = extract_image_spatial_evidence(
        board_image=board,
        closed_ir=closed,
        output=output,
        manifest=manifest,
        llm=llm,
    )

    assert result == manifest
    assert len(llm.calls) == 1
    assert json.loads(output.read_text(encoding="utf-8"))["format"] == SPATIAL_FORMAT
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["format"] == SPATIAL_MANIFEST_FORMAT
    assert data["stage"] == "6.1_image_spatial_extraction"
    assert data["authority"]["world_coordinates_emitted_here"] is False
    assert data["authority"]["frozen_asset_bindings_used_here"] is False
    assert output.with_suffix(".raw.txt").is_file()


def test_extract_rejects_board_not_matching_closed_ir(tmp_path: Path) -> None:
    board = tmp_path / "board.png"
    board.write_bytes(BOARD_BYTES)
    closed = tmp_path / "closed.json"
    payload = _closed(board)
    payload["board"]["sha256"] = "wrong"
    closed.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(VisualSpatialExtractionError, match="does not match"):
        extract_image_spatial_evidence(
            board_image=board,
            closed_ir=closed,
            output=tmp_path / "evidence.json",
            manifest=tmp_path / "manifest.json",
            llm=FakeSpatialLLM(),
        )
