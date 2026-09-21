from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.visual_compiler_shopping_list import (
    ShoppingListArtifactError,
    build_shopping_list,
    write_shopping_list,
)


def _project(tmp_path: Path, *, candidate: str = "Models/Vehicle/player-vehicle.fbx") -> Path:
    project = tmp_path / "Demo"
    project.mkdir()
    (project / ".concept.txt").write_text("concept\n", encoding="utf-8")
    (project / "playability.txt").write_text("PLAYABILITY 80/100\n", encoding="utf-8")
    data = {
        "project_name": "Demo",
        "game_type": "3D",
        "platform": "pc",
        "render_pipeline": "URP",
        "unity_version": "6000.0",
        "entities": [{
            "name": "PlayerVehicle",
            "kind": "vehicle",
            "tag": "Player",
            "count": 1,
            "model_hint": "offroad racing truck",
            "sketchfab_query": "low poly race car",
        }],
        "meta": {
            "concept": "An offroad racing truck game.",
            "entity_model_paths": {"PlayerVehicle": candidate},
        },
    }
    (project / "Demo.schema.json").write_text(
        json.dumps(data), encoding="utf-8"
    )
    return project


def test_shopping_list_keeps_demand_and_candidate(tmp_path: Path) -> None:
    project = _project(tmp_path)
    manifest = build_shopping_list(project, repo_root=tmp_path)

    assert manifest["contract"]["llm_invoked"] is False
    assert manifest["entries"] == [{
        "entity": "PlayerVehicle",
        "kind": "vehicle",
        "tag": "Player",
        "count": 1,
        "visual_target": "offroad racing truck",
        "sketchfab_query": "low poly race car",
        "query_board_status": "query_present",
        "candidate": "Models/Vehicle/player-vehicle.fbx",
        "status": "candidate_requires_visual_review",
        "asset_gap": None,
    }]
    assert len(manifest["identity"]["hash"]) == 32


def test_shopping_list_marks_missing_candidate(tmp_path: Path) -> None:
    project = _project(tmp_path, candidate="")
    manifest = build_shopping_list(project, repo_root=tmp_path)

    assert manifest["entries"][0]["status"] == "unfulfilled_no_resolved_candidate"
    assert manifest["entries"][0]["candidate"] is None
    assert manifest["entries"][0]["query_board_status"] == "query_present"


def test_shopping_list_flags_semantic_asset_gap(tmp_path: Path) -> None:
    project = _project(tmp_path, candidate="Models/Vehicle/stylized-fighter-plane.fbx")
    manifest = build_shopping_list(project, repo_root=tmp_path)

    assert manifest["entries"][0]["status"] == "review_required_asset_gap"
    assert manifest["fidelity"]["asset_gap_count"] == 1


def test_query_board_marks_missing_query(tmp_path: Path) -> None:
    project = _project(tmp_path)
    data = json.loads((project / "Demo.schema.json").read_text(encoding="utf-8"))
    del data["entities"][0]["sketchfab_query"]
    (project / "Demo.schema.json").write_text(json.dumps(data), encoding="utf-8")

    manifest = build_shopping_list(project, repo_root=tmp_path)

    assert manifest["entries"][0]["query_board_status"] == "unfulfilled_no_sketchfab_query"
    assert manifest["query_board"]["authority"] == "legacy_schema_export"


def test_shopping_list_requires_schema(tmp_path: Path) -> None:
    project = tmp_path / "Empty"
    project.mkdir()
    (project / ".concept.txt").write_text("concept\n", encoding="utf-8")

    with pytest.raises(ShoppingListArtifactError, match="schema"):
        build_shopping_list(project, repo_root=tmp_path)


def test_shopping_list_write_is_stable(tmp_path: Path) -> None:
    project = _project(tmp_path)
    manifest = build_shopping_list(project, repo_root=tmp_path)
    output = write_shopping_list(manifest, tmp_path / "shopping.json")

    assert json.loads(output.read_text(encoding="utf-8"))["identity"] == manifest["identity"]
