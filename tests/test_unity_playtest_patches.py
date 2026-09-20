"""Acceptance tests for the deterministic mechanics-playtest patch boundary."""

from __future__ import annotations

import copy
import json

import pytest

from src.unity_playtest_patches import (
    PatchIdentityError,
    PatchPreconditionError,
    PlaytestPatchError,
    apply_atom_patch,
    apply_patch_plan,
    apply_schema_patch,
    diff_atom_ir,
    diff_schema_snapshots,
    snapshot_hash,
)


def _move_atom(atom_id: str = "move-1", x: int = 1) -> dict:
    return {
        "id": atom_id,
        "atom": "MoveTo",
        "args": {"x": x, "y": 0, "z": 0},
        "label": "reach gate",
    }


def test_schema_patch_is_detached_and_requires_exact_precondition():
    schemas = {
        "missions": [{"id": "main", "objectives": [{"id": "gate", "target_ref": "old"}]}]
    }
    baseline = copy.deepcopy(schemas)
    patch = [{
        "id": "schema-1",
        "operation": "modify",
        "target": "missions[0].objectives[0].target_ref",
        "expected": "old",
        "replacement": "gate",
        "trigger": "PLAYABILITY_REPORT.PLAYABILITY_FIXES[0]",
    }]

    updated = apply_schema_patch(schemas, patch)

    assert schemas == baseline
    assert updated["missions"][0]["objectives"][0]["target_ref"] == "gate"
    with pytest.raises(PatchPreconditionError, match="expected-value precondition"):
        apply_schema_patch(updated, patch)


def test_normal_schema_field_invention_is_rejected():
    with pytest.raises(PlaytestPatchError, match="invent a normal schema field"):
        apply_schema_patch(
            {"missions": [{"id": "main"}]},
            [{
                "id": "schema-1",
                "operation": "add",
                "target": "missions[0].invented_field",
                "expected": None,
                "replacement": "guess",
            }],
        )
    with pytest.raises(PlaytestPatchError, match="invent normal schema field"):
        apply_schema_patch(
            {"missions": [{"id": "main"}]},
            [{
                "id": "schema-2",
                "operation": "add",
                "target": "missions[1]",
                "expected": None,
                "replacement": {"id": "side", "invented_field": True},
            }],
        )


def test_extension_namespace_allows_structurally_valid_additions_only():
    schemas = {"scripts": [{"name": "Player", "extensions_fields": []}]}
    patch = [{
        "id": "schema-extension-1",
        "operation": "add",
        "target": "scripts[0].extensions_fields[0]",
        "expected": None,
        "replacement": {"name": "stamina", "type": "float", "default": "10f"},
    }]

    updated = apply_schema_patch(schemas, patch)

    assert updated["scripts"][0]["extensions_fields"][0]["name"] == "stamina"
    with pytest.raises(PatchIdentityError, match="duplicate extension"):
        apply_schema_patch(updated, patch)
    with pytest.raises(PlaytestPatchError, match="valid C# name"):
        apply_schema_patch(
            schemas,
            [{**patch[0], "replacement": {"name": "not valid", "type": "float"}}],
        )


def test_atom_patch_validates_registry_identity_and_preserves_input_order():
    atoms = [_move_atom()]
    replacement = _move_atom(x=2)
    patch = [{
        "id": "atom-1",
        "operation": "modify",
        "target": "move-1",
        "expected": _move_atom(),
        "replacement": replacement,
    }]

    updated = apply_atom_patch(atoms, patch)

    assert atoms[0]["args"]["x"] == 1
    assert updated[0]["args"]["x"] == 2
    with pytest.raises(PatchPreconditionError):
        apply_atom_patch(updated, patch)
    with pytest.raises(PatchIdentityError, match="unknown atom"):
        apply_atom_patch(
            atoms,
            [{
                "id": "atom-unknown",
                "operation": "add",
                "target": "append",
                "replacement": {"id": "bad", "atom": "InventedAtom"},
            }],
        )


def test_atom_duplicates_are_rejected_before_mutation():
    with pytest.raises(PatchIdentityError, match="duplicate atom identity"):
        apply_atom_patch([_move_atom(), _move_atom()], [])


def test_plan_enforces_atom_mutation_rules_and_applies_detached_snapshots():
    schemas = {"rules": {"step": "old"}}
    atoms = [_move_atom()]
    plan = {
        "REQUIRED_SCHEMA_UPDATES": {
            "SCHEMA_PATCH": [{
                "id": "schema-1",
                "operation": "modify",
                "target": "rules.step",
                "expected": "old",
                "replacement": "new",
                "trigger": "PLAYABILITY_REPORT.MISSION_FLOW_VALIDATION[0]",
            }],
        },
        "REQUIRED_ATOM_UPDATES": {
            "ATOM_PATCH": [{
                "id": "atom-1",
                "operation": "add",
                "target": "append",
                "replacement": {"id": "move-2", "atom": "MoveTo", "args": {"x": 5}},
                "required_by": "schema-1",
            }],
        },
        "RESOLVER_PATCH_GENERATOR": {"RESOLVER_PATCH": []},
    }
    schema_before = copy.deepcopy(schemas)
    atoms_before = copy.deepcopy(atoms)

    result = apply_patch_plan(schemas, atoms, plan)

    assert schemas == schema_before
    assert atoms == atoms_before
    assert result["UPDATED_SCHEMAS"]["rules"]["step"] == "new"
    assert [item["id"] for item in result["UPDATED_ATOM_IR"]] == ["move-1", "move-2"]
    assert result["SCHEMA_DIFF"]["modified_rules"][0]["operation_id"] == "snapshot-diff"
    assert result["ATOM_DIFF"]["added_atoms"][0]["source_report_path"] == "ATOM_IR"


def test_plan_rejects_atom_add_without_accepted_schema_update():
    plan = {
        "REQUIRED_SCHEMA_UPDATES": {"SCHEMA_PATCH": []},
        "REQUIRED_ATOM_UPDATES": {"ATOM_PATCH": [{
            "id": "atom-1",
            "operation": "add",
            "target": "append",
            "replacement": {"id": "move-2", "atom": "MoveTo"},
        }]},
        "RESOLVER_PATCH_GENERATOR": {"RESOLVER_PATCH": []},
    }
    with pytest.raises(PlaytestPatchError, match="accepted schema operation"):
        apply_patch_plan({}, [_move_atom()], plan)


def test_abstention_is_blocking_in_strict_mode_and_visible_in_review_mode():
    plan = {
        "REQUIRED_SCHEMA_UPDATES": {"SCHEMA_PATCH": [{
            "id": "blocked-1",
            "operation": "abstain",
            "target": "missions[0].reward",
            "reason": "No safe reward consumer is declared.",
        }]},
        "REQUIRED_ATOM_UPDATES": {"ATOM_PATCH": []},
        "RESOLVER_PATCH_GENERATOR": {"RESOLVER_PATCH": []},
    }
    with pytest.raises(PlaytestPatchError, match="explicit abstention"):
        apply_patch_plan({"missions": []}, [], plan)

    result = apply_patch_plan({"missions": []}, [], plan, strict=False)
    assert result["ABSTENTIONS"][0]["id"] == "blocked-1"
    assert result["status"] == "unchanged"


def test_diffs_are_deterministic_and_include_rules_transitions_and_provenance():
    old_schema = {"rules": {"step": "old"}, "name": "demo"}
    new_schema = {"rules": {"step": "new", "transition": "complete"}, "name": "demo"}
    schema_diff = diff_schema_snapshots(
        old_schema,
        new_schema,
        operation_id="schema-1",
        source_report_path="PLAYABILITY_REPORT.MISSION_FLOW_VALIDATION[0]",
    )
    assert schema_diff["modified_rules"][0]["old"] == "old"
    assert schema_diff["modified_rules"][0]["new"] == "new"
    assert schema_diff["modified_rules"][0]["operation_id"] == "schema-1"
    assert schema_diff["added_rules"][0]["source_report_path"].startswith("PLAYABILITY_REPORT")

    old_atoms = [_move_atom()]
    new_atoms = old_atoms + [{"id": "transition-1", "atom": "MoveTo", "kind": "transition"}]
    atom_diff = diff_atom_ir(old_atoms, new_atoms, operation_id="atom-1", source_report_path="REPORT[0]")
    assert atom_diff["added_transitions"][0]["id"] == "transition-1"
    assert atom_diff["added_transitions"][0]["operation_id"] == "atom-1"

    assert snapshot_hash(old_schema) == snapshot_hash(json.loads(json.dumps(old_schema)))
    assert json.dumps(schema_diff, sort_keys=True) == json.dumps(
        diff_schema_snapshots(old_schema, new_schema, operation_id="schema-1", source_report_path="PLAYABILITY_REPORT.MISSION_FLOW_VALIDATION[0]"),
        sort_keys=True,
    )
