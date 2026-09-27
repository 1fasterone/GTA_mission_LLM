from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.visual_reasoning_compiler import (
    INVENTORY_FORMAT,
    REASONING_FORMAT,
    VisualReasoningCompilerError,
    sha256_file,
)
from src.visual_reasoning_reconcile import (
    CLOSED_FORMAT,
    RECONCILIATION_FORMAT,
    reconcile_image_reasoning,
)


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"slice-three-board-bytes-long-enough"


def _write_inputs(tmp_path: Path, *, extra_reference: str | None = None) -> tuple[Path, Path, Path]:
    inventory = tmp_path / "visual_inventory.json"
    inventory.write_text(json.dumps({
        "format": INVENTORY_FORMAT,
        "scene_summary": "A small explorer stands on a glowing commit graph.",
        "visible_elements": [
            {
                "label": "red-hat explorer",
                "category": "character",
                "description": "Small pixel-art explorer wearing a red hat and backpack.",
                "model_hint": "stylized explorer character with backpack",
            },
            {
                "label": "glowing commit nodes",
                "category": "prop",
                "description": "Glowing orb-like nodes joined by directional arrows.",
                "model_hint": "glowing orb node",
            },
            {
                "label": "commit button",
                "category": "ui",
                "description": "Visible bottom action button labelled COMMIT.",
                "model_hint": None,
            },
        ],
        "sketchfab_queries": [
            {"label": "red-hat explorer", "query": "stylized explorer backpack"},
            {"label": "glowing commit nodes", "query": "glowing orb game prop"},
        ],
        "visible_text": ["COMMIT"],
        "uncertainties": [],
    }), encoding="utf-8")
    concept = tmp_path / "Concept.txt"
    reference = extra_reference or "red-hat explorer"
    concept.write_text(
        "VISIBLE ENTITY REFERENCES:\n"
        f"- {reference}\n"
        "- glowing orb nodes\n"
        "END VISIBLE ENTITY REFERENCES\n\n"
        "The player guides the explorer across the glowing commit graph. "
        "The player chooses a branch and moves between visible nodes while the "
        "action controls provide the interaction. A round is won after the "
        "explorer reaches the final node through a valid merge sequence; an "
        "invalid branch choice ends the attempt.\n",
        encoding="utf-8",
    )
    board = tmp_path / "board.png"
    board.write_bytes(PNG_BYTES)
    reasoning = tmp_path / "reasoning.json"
    reasoning.write_text(json.dumps({
        "format": REASONING_FORMAT,
        "stage": "2_image_reasoning",
        "authority": {
            "original_vizconcept_sent_to_llm": False,
            "semantic_context_sent_to_llm": False,
        },
        "board": {"path": str(board), "sha256": sha256_file(board)},
        "outputs": {
            "visual_inventory": {
                "path": str(inventory), "sha256": sha256_file(inventory),
            },
            "concept": {"path": str(concept), "sha256": sha256_file(concept)},
        },
    }), encoding="utf-8")
    return inventory, concept, reasoning


def test_reconcile_writes_closed_ir_without_llm(tmp_path: Path) -> None:
    inventory, concept, reasoning = _write_inputs(tmp_path)
    output = tmp_path / "closed_image_ir.json"
    manifest = tmp_path / "reconciliation.json"

    result = reconcile_image_reasoning(
        inventory_file=inventory,
        concept_file=concept,
        reasoning_manifest=reasoning,
        output=output,
        manifest=manifest,
    )

    assert result == manifest
    closed = json.loads(output.read_text(encoding="utf-8"))
    assert closed["format"] == CLOSED_FORMAT
    assert closed["status"] == "closed"
    assert closed["authority"]["llm_called_here"] is False
    assert closed["coverage"]["required_inventory_labels"] == [
        "glowing commit nodes", "red-hat explorer",
    ]
    assert closed["asset_requests"][0]["concept_reference"] == "red-hat explorer"
    report = json.loads(manifest.read_text(encoding="utf-8"))
    assert report["format"] == RECONCILIATION_FORMAT
    assert report["outputs"]["closed_image_ir"]["sha256"] == sha256_file(output)


def test_reconcile_rejects_unsupported_entity_reference(tmp_path: Path) -> None:
    inventory, concept, reasoning = _write_inputs(
        tmp_path, extra_reference="unseen dragon"
    )

    with pytest.raises(VisualReasoningCompilerError, match="no unique visual"):
        reconcile_image_reasoning(
            inventory_file=inventory,
            concept_file=concept,
            reasoning_manifest=reasoning,
            output=tmp_path / "closed.json",
            manifest=tmp_path / "reconcile.json",
        )


def test_reconcile_rejects_omitted_asset_element(tmp_path: Path) -> None:
    inventory, concept, reasoning = _write_inputs(tmp_path)
    concept.write_text(
        concept.read_text(encoding="utf-8").replace("- glowing orb nodes\n", ""),
        encoding="utf-8",
    )
    reasoning_data = json.loads(reasoning.read_text(encoding="utf-8"))
    reasoning_data["outputs"]["concept"]["sha256"] = sha256_file(concept)
    reasoning.write_text(json.dumps(reasoning_data), encoding="utf-8")

    with pytest.raises(VisualReasoningCompilerError, match="omitted"):
        reconcile_image_reasoning(
            inventory_file=inventory,
            concept_file=concept,
            reasoning_manifest=reasoning,
            output=tmp_path / "closed.json",
            manifest=tmp_path / "reconcile.json",
        )


def test_reconcile_accepts_unique_exact_generic_inventory_label(tmp_path: Path) -> None:
    inventory, concept, reasoning = _write_inputs(tmp_path)
    concept.write_text(
        concept.read_text(encoding="utf-8").replace(
            "- red-hat explorer\n", "- character\n"),
        encoding="utf-8",
    )
    inventory_data = json.loads(inventory.read_text(encoding="utf-8"))
    inventory_data["visible_elements"][0]["label"] = "character"
    inventory_data["sketchfab_queries"][0]["label"] = "character"
    inventory.write_text(json.dumps(inventory_data), encoding="utf-8")
    reasoning_data = json.loads(reasoning.read_text(encoding="utf-8"))
    reasoning_data["outputs"]["visual_inventory"]["sha256"] = sha256_file(inventory)
    reasoning_data["outputs"]["concept"]["sha256"] = sha256_file(concept)
    reasoning.write_text(json.dumps(reasoning_data), encoding="utf-8")

    output = tmp_path / "closed.json"
    reconcile_image_reasoning(
        inventory_file=inventory,
        concept_file=concept,
        reasoning_manifest=reasoning,
        output=output,
        manifest=tmp_path / "reconcile.json",
    )
    closed = json.loads(output.read_text(encoding="utf-8"))
    assert closed["entity_matches"][0] == {
        "reference": "character", "inventory_label": "character"
    }
