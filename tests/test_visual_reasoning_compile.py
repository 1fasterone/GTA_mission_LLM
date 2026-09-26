"""Focused Slice 5 contract tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.unity_entity_asset_resolver import resolve_entity_assets
from src.unity_schema import EntitySpec, UnitySchema
from src.visual_reasoning_compile import (
    bind_visual_assets,
    load_visual_compile_plan,
    render_visual_entity_checklist,
)
from src.visual_reasoning_compiler import (
    VisualReasoningCompilerError,
    sha256_bytes,
    sha256_file,
)


def _fixture_artifacts(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    lookups = tmp_path / "lookups"
    (lookups / "unity").mkdir(parents=True)
    asset_root = tmp_path / "models"
    asset_root.mkdir()
    asset_path = asset_root / "wooden-crate.fbx"
    asset_path.write_bytes(b"known-good-fbx")
    catalog_path = lookups / "unity" / "asset_catalog.json"
    catalog_path.write_text(json.dumps({
        "version": 1,
        "vocabulary": {"categories": ["prop"], "tags": ["crate"]},
        "assets": [{
            "filename": "wooden-crate.fbx",
            "relpath": "wooden-crate.fbx",
            "root": asset_root.as_posix(),
            "source": "kenney",
            "format": "fbx",
            "category": "prop",
            "keywords": ["wooden", "crate", "prop"],
            "kind_hints": ["prop"],
            "game_contexts": ["any"],
        }],
    }), encoding="utf-8")

    closed = {
        "format": "visual-reasoning-compiler/closed-image-ir/v1",
        "stage": "3_image_reasoning_reconciliation",
        "status": "closed",
        "request_fingerprint": "request-1",
        "inventory": {"visible_elements": [{
            "label": "Wooden Crate",
            "category": "prop",
            "description": "a wooden crate",
            "model_hint": "wooden crate",
        }]},
        "concept": {"text": "A scene containing a wooden crate."},
        "entity_matches": [{
            "reference": "Wooden Crate",
            "inventory_label": "Wooden Crate",
        }],
        "coverage": {"required_inventory_labels": ["Wooden Crate"]},
    }
    closed_path = tmp_path / "closed.json"
    closed_path.write_text(json.dumps(closed), encoding="utf-8")
    closed_raw = closed_path.read_bytes()

    selection = {
        "filename": "wooden-crate.fbx",
        "relpath": "wooden-crate.fbx",
        "format": "fbx",
        "source": "kenney",
        "category": "prop",
        "score": 1.0,
        "file_sha256": sha256_file(asset_path),
        "file_bytes": asset_path.stat().st_size,
    }
    fulfilment = {
        "format": "visual-reasoning-compiler/asset-fulfilment/v1",
        "stage": "4_asset_fulfilment",
        "status": "fulfilled",
        "closed_request_fingerprint": "request-1",
        "rejections": [],
        "inputs": {
            "closed_image_ir": {
                "path": str(closed_path),
                "sha256": sha256_bytes(closed_raw),
            },
            "catalog_files": [{
                "path": str(catalog_path),
                "sha256": sha256_file(catalog_path),
            }],
        },
        "assets": [{
            "inventory_label": "Wooden Crate",
            "query": "wooden crate game prop",
            "selection": selection,
        }],
    }
    fulfilment_path = tmp_path / "fulfilment.json"
    fulfilment_path.write_text(json.dumps(fulfilment), encoding="utf-8")
    fulfilment_raw = fulfilment_path.read_bytes()
    manifest = {
        "format": "visual-reasoning-compiler/asset-fulfilment/v1",
        "stage": "4_asset_fulfilment",
        "status": "fulfilled",
        "closed_request_fingerprint": "request-1",
        "outputs": {"asset_fulfilment": {
            "path": str(fulfilment_path),
            "sha256": sha256_bytes(fulfilment_raw),
        }},
    }
    manifest_path = tmp_path / "fulfilment.manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return closed_path, fulfilment_path, manifest_path, lookups, asset_path


def test_slice5_plan_verifies_hashes_and_enriches_exact_catalog_entry(tmp_path):
    closed, fulfilment, manifest, lookups, asset_path = _fixture_artifacts(tmp_path)
    plan = load_visual_compile_plan(
        closed_ir=closed,
        asset_fulfilment=fulfilment,
        asset_manifest=manifest,
        lookups_dir=str(lookups),
    )
    assert plan["assets"][0]["catalog_entry"]["filename"] == asset_path.name
    assert plan["assets"][0]["source_path"] == str(asset_path.resolve())


def test_visual_entity_checklist_preserves_each_fulfilled_noun(tmp_path):
    closed, fulfilment, manifest, lookups, _ = _fixture_artifacts(tmp_path)
    plan = load_visual_compile_plan(
        closed_ir=closed,
        asset_fulfilment=fulfilment,
        asset_manifest=manifest,
        lookups_dir=str(lookups),
    )

    checklist = render_visual_entity_checklist(plan)

    assert 'visual_label="Wooden Crate"' in checklist
    assert 'model_hint="wooden crate"' in checklist
    assert "PlayerUnits" in checklist
    assert "EnemyWaves" in checklist


def test_slice5_rejects_changed_selected_asset_bytes(tmp_path):
    closed, fulfilment, manifest, lookups, asset_path = _fixture_artifacts(tmp_path)
    asset_path.write_bytes(b"changed")
    with pytest.raises(VisualReasoningCompilerError, match="bytes changed"):
        load_visual_compile_plan(
            closed_ir=closed,
            asset_fulfilment=fulfilment,
            asset_manifest=manifest,
            lookups_dir=str(lookups),
        )


def test_slice5_binds_one_visual_selection_to_one_schema_entity(tmp_path):
    closed, fulfilment, manifest, lookups, _ = _fixture_artifacts(tmp_path)
    plan = load_visual_compile_plan(
        closed_ir=closed,
        asset_fulfilment=fulfilment,
        asset_manifest=manifest,
        lookups_dir=str(lookups),
    )
    schema = SimpleNamespace(entities=[EntitySpec(
        name="CrateGroup", kind="prop", model_hint="wooden crate"
    )])
    result = bind_visual_assets(schema, plan)
    assert result["bindings"]["CrateGroup"]["inventory_label"] == "Wooden Crate"
    assert plan["binding_report"]["status"] == "bound"


def test_slice5_rejects_ambiguous_entity_binding(tmp_path):
    closed, fulfilment, manifest, lookups, _ = _fixture_artifacts(tmp_path)
    plan = load_visual_compile_plan(
        closed_ir=closed,
        asset_fulfilment=fulfilment,
        asset_manifest=manifest,
        lookups_dir=str(lookups),
    )
    schema = SimpleNamespace(entities=[
        EntitySpec(name="CrateLeft", kind="prop", model_hint="wooden crate"),
        EntitySpec(name="CrateRight", kind="prop", model_hint="wooden crate"),
    ])
    with pytest.raises(VisualReasoningCompilerError, match="ambiguous"):
        bind_visual_assets(schema, plan)


def test_slice5_reuses_one_group_for_repeated_visual_nouns(tmp_path):
    closed, fulfilment, manifest, lookups, _ = _fixture_artifacts(tmp_path)
    plan = load_visual_compile_plan(
        closed_ir=closed,
        asset_fulfilment=fulfilment,
        asset_manifest=manifest,
        lookups_dir=str(lookups),
    )
    plan["closed"]["inventory"]["visible_elements"].append({
        "label": "Wooden Crate 2",
        "category": "prop",
        "description": "a second wooden crate",
        "model_hint": "wooden crate",
    })
    plan["closed"]["entity_matches"].append({
        "reference": "Wooden Crate 2",
        "inventory_label": "Wooden Crate 2",
    })
    repeated_asset = copy.deepcopy(plan["assets"][0])
    repeated_asset["inventory_label"] = "Wooden Crate 2"
    plan["assets"].append(repeated_asset)

    schema = SimpleNamespace(entities=[EntitySpec(
        name="CrateGroup", kind="prop", model_hint="wooden crate"
    )])
    result = bind_visual_assets(schema, plan)

    assert list(result["bindings"]) == ["CrateGroup"]
    assert [row["inventory_label"] for row in plan["binding_report"]["bindings"]] == [
        "Wooden Crate", "Wooden Crate 2",
    ]


def test_slice5_preserves_numeric_identity_for_named_visual_instances():
    label = "aircraft_2"
    plan = {
        "closed": {
            "inventory": {"visible_elements": [{
                "label": label,
                "category": "vehicle",
                "description": "a fighter aircraft",
                "model_hint": "fighter aircraft",
            }]},
            "entity_matches": [{"reference": label, "inventory_label": label}],
        },
        "assets": [{
            "inventory_label": label,
            "query": "fighter aircraft",
            "selection": {"filename": "fighter.fbx", "source": "kenney"},
            "catalog_entry": {"filename": "fighter.fbx"},
            "source_path": "models/fighter.fbx",
        }],
    }
    schema = SimpleNamespace(entities=[
        EntitySpec(name="Aircraft2", kind="enemy", model_hint="fighter aircraft"),
        EntitySpec(name="Aircraft4", kind="enemy", model_hint="fighter aircraft"),
    ])

    result = bind_visual_assets(schema, plan)

    assert list(result["bindings"]) == ["Aircraft2"]


def test_slice5_does_not_bind_aircraft_to_ship_from_description_overlap():
    plan = {
        "closed": {
            "inventory": {"visible_elements": [
                {
                    "label": "aircraft_3",
                    "category": "vehicle",
                    "description": "aircraft attacking ships",
                    "model_hint": "aircraft",
                },
                {
                    "label": "ship_1",
                    "category": "vehicle",
                    "description": "ship attacked by aircraft",
                    "model_hint": "ship",
                },
            ]},
            "entity_matches": [
                {"reference": "aircraft_3", "inventory_label": "aircraft_3"},
                {"reference": "ship_1", "inventory_label": "ship_1"},
            ],
        },
        "assets": [
            {
                "inventory_label": "aircraft_3",
                "query": "aircraft",
                "selection": {"filename": "aircraft.fbx", "source": "kenney"},
                "catalog_entry": {"filename": "aircraft.fbx"},
                "source_path": "models/aircraft.fbx",
            },
            {
                "inventory_label": "ship_1",
                "query": "ship",
                "selection": {"filename": "ship.fbx", "source": "kenney"},
                "catalog_entry": {"filename": "ship.fbx"},
                "source_path": "models/ship.fbx",
            },
        ],
    }
    schema = SimpleNamespace(entities=[
        EntitySpec(
            name="EnemyAircraftGroup",
            kind="enemy",
            model_hint="interceptor aircraft",
            description="Enemy aircraft attack ships below.",
        ),
        EntitySpec(
            name="TargetShips",
            kind="pickup",
            model_hint="naval ship",
            description="Ships are attacked by enemy aircraft.",
        ),
    ])

    result = bind_visual_assets(schema, plan)

    assert result["bindings"]["EnemyAircraftGroup"]["inventory_label"] == "aircraft_3"
    assert result["bindings"]["TargetShips"]["inventory_label"] == "ship_1"


def test_resolver_uses_exact_visual_binding_before_catalog_tiers(tmp_path):
    lookups = tmp_path / "lookups"
    (lookups / "unity").mkdir(parents=True)
    root = tmp_path / "models"
    root.mkdir()
    selected = root / "selected.fbx"
    selected.write_bytes(b"selected")
    decoy = root / "decoy.fbx"
    decoy.write_bytes(b"decoy")
    catalog = {
        "version": 1,
        "assets": [
            {"filename": "selected.fbx", "relpath": "selected.fbx",
             "root": str(root), "source": "kenney", "format": "fbx",
             "category": "prop", "keywords": ["crate"]},
            {"filename": "decoy.fbx", "relpath": "decoy.fbx",
             "root": str(root), "source": "kenney", "format": "fbx",
             "category": "prop", "keywords": ["crate"]},
        ],
    }
    (lookups / "unity" / "asset_catalog.json").write_text(
        json.dumps(catalog), encoding="utf-8"
    )
    schema = UnitySchema(
        project_name="T", company_name="C", description="crate", game_type="3D",
        platform="pc", render_pipeline="URP", unity_version="6000.2",
        scenes=["Main"], entities=[EntitySpec(name="Crate", kind="prop")],
    )
    paths = resolve_entity_assets(
        schema, {}, {}, lookups_dir=str(lookups), strict=True,
        locked_asset_bindings={"Crate": {
            "filename": "selected.fbx",
            "relpath": "selected.fbx",
            "source": "kenney",
        }},
    )
    assert paths["Crate"].endswith("/Prop/selected")
