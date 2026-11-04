"""Engineer-authored fixtures for detached intent history and deterministic replay."""

from copy import deepcopy
import json

import pytest

from src.intent.claims import IntentClaim
from src.intent.ledger import IntentLedger
from src.intent.resolver import apply_resolution, resolve_intent
from src.unity_pipeline_stages import ResolveIntentStage
from src.unity_projectile_contract import (
    physical_projectile_contract_for,
    stamp_projectile_visual_resolution,
)
from src.unity_schema import UnitySchema


def _schema():
    return UnitySchema(
        project_name="LedgerFixture", game_type="3D", platform="pc",
        render_pipeline="URP", scenes=["Main"],
    )


def _contract():
    return physical_projectile_contract_for("3D", "The hero tosses rocks at targets.")


def _two_snapshots():
    ledger = IntentLedger()
    state = {"projectile_contract": _contract()}
    ledger.snapshot_state("before", state)
    state = deepcopy(state)
    state["projectile_contract"]["presentation"]["resource_path"] = "Models/Rock"
    ledger.snapshot_state("after", state)
    return ledger


def test_schema_snapshots_preserve_projectile_asset_resolution_history():
    schema = _schema()
    schema.meta["physical_projectile_contract"] = _contract()
    ledger = IntentLedger()
    ledger.snapshot("Post-Resolution", schema)
    stamp_projectile_visual_resolution(schema, "Models/Rock")
    ledger.snapshot("Pre-Generation", schema)

    report = ledger.to_dict()
    before, after = report["snapshots"]
    assert before["state"]["projectile_contract"]["presentation"]["resource_path"] == ""
    assert after["state"]["projectile_contract"]["presentation"]["resource_path"] == "Models/Rock"
    assert report["diffs"] == [{
        "field": "projectile_contract", "before": before["state"]["projectile_contract"],
        "after": after["state"]["projectile_contract"],
        "from": "Post-Resolution", "to": "Pre-Generation",
    }]


def test_snapshot_state_detaches_nested_containers_from_its_input():
    state = {"nested": {"items": [{"value": 1}]}}
    ledger = IntentLedger()
    ledger.snapshot_state("before", state)
    state["nested"]["items"][0]["value"] = 2
    state["nested"]["items"].append({"value": 3})
    assert ledger.to_dict()["snapshots"][0]["state"] == {
        "nested": {"items": [{"value": 1}]},
    }


@pytest.mark.parametrize("section", ["snapshots", "diffs_before", "diffs_after"])
def test_exported_report_mutation_does_not_rewrite_history(section):
    ledger = _two_snapshots()
    expected = deepcopy(ledger.to_dict())
    report = ledger.to_dict()
    if section == "snapshots":
        contract = report["snapshots"][0]["state"]["projectile_contract"]
    else:
        contract = report["diffs"][0][section.removeprefix("diffs_")]
    contract["presentation"]["local_euler"][0] = 999.0
    assert ledger.to_dict() == expected


@pytest.mark.parametrize("side", ["before", "after"])
def test_exported_diffs_do_not_alias_snapshots(side):
    ledger = _two_snapshots()
    expected = deepcopy(ledger.to_dict())
    diffs = ledger.diffs()
    diffs[0][side]["presentation"]["resource_path"] = "Corrupted"
    assert ledger.to_dict() == expected


def test_register_captures_evidence_before_the_producer_changes_it():
    contract = _contract()
    ledger = IntentLedger()
    ledger.register(IntentClaim("projectile_contract", contract, "test", 1.0, "fixture"))
    expected = deepcopy(ledger.to_dict())
    contract["presentation"]["local_euler"][0] = 999.0
    assert ledger.to_dict() == expected


def test_resolution_and_its_export_do_not_rewrite_registered_evidence():
    ledger = IntentLedger()
    ledger.register(IntentClaim("projectile_contract", _contract(), "test", 1.0, "fixture"))
    expected = deepcopy(ledger.to_dict())
    resolution = resolve_intent(ledger)
    original_resolution = deepcopy(resolution.to_dict())
    exported = resolution.to_dict()
    exported["decisions"]["projectile_contract"]["presentation"]["resource_path"] = "Export"
    assert resolution.to_dict() == original_resolution

    resolution.decisions["projectile_contract"]["presentation"]["resource_path"] = "Decision"
    assert ledger.to_dict() == expected
    assert resolution.winners["projectile_contract"].value["presentation"]["resource_path"] == ""
    resolution.winners["projectile_contract"].value["presentation"]["resource_path"] = "Winner"
    assert ledger.to_dict() == expected


def test_asset_binding_preserves_claims_resolution_and_saved_report():
    schema = _schema()
    ledger = IntentLedger()
    resolution, _ = ResolveIntentStage().run(
        schema, ledger, prompt="The hero tosses rocks at targets.", schema_source="schema_llm",
    )
    ledger.snapshot("Post-Resolution", schema)
    report = ledger.to_dict()
    report_json = json.dumps(report, sort_keys=True)
    resolution_json = json.dumps(resolution.to_dict(), sort_keys=True)
    schema.meta["intent_ledger"] = report

    stamp_projectile_visual_resolution(schema, "Models/Rock")
    ledger.snapshot("Pre-Generation", schema)
    assert json.dumps(report, sort_keys=True) == report_json
    assert json.dumps(resolution.to_dict(), sort_keys=True) == resolution_json
    assert json.dumps(schema.meta["intent_resolution"], sort_keys=True) == resolution_json
    assert json.dumps(resolve_intent(ledger).to_dict(), sort_keys=True) == resolution_json

    replayed = UnitySchema.model_validate_json(schema.model_dump_json())
    replayed_resolution = resolve_intent(ledger)
    apply_resolution(replayed, replayed_resolution)
    assert replayed.meta["physical_projectile_contract"]["presentation"]["resource_path"] == ""
    assert json.dumps(replayed.meta["intent_resolution"], sort_keys=True) == resolution_json
    assert json.loads(report_json)["format"] == ledger.to_dict()["format"] == "intent-ledger/v1"
