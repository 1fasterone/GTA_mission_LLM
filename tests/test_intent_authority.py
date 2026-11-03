from __future__ import annotations

import pytest

from src.intent.claims import IntentClaim, explicit_exclusion_claims
from src.intent.ledger import IntentLedger
from src.intent_eval import load_intent_cases, run_intent_evals
from src.mechanics import default_mechanic_registry
from src.unity_pipeline_stages import StageExecutionRegistry, ValidateSchemaStage
from src.unity_prompts import BRIEF_CAP_CHARS, brief_block_report
from src.unity_schema import UnitySchema


def _schema(name: str) -> UnitySchema:
    return UnitySchema(
        project_name=name,
        game_type="3D",
        platform="pc",
        render_pipeline="URP",
        scenes=["Main"],
    )


def test_intent_claim_rejects_invalid_confidence():
    with pytest.raises(ValueError, match="confidence"):
        IntentClaim("genre", "fps", "test", 1.1, "bad")


def test_ledger_reports_snapshot_diffs():
    schema = _schema("Diff")
    ledger = IntentLedger()
    ledger.snapshot("before", schema)
    schema.locomotion = "fly"
    ledger.snapshot("after", schema)
    assert ledger.diffs()[0]["field"] == "locomotion"
    assert ledger.diffs()[0]["after"] == "fly"


def test_explicit_no_bikes_is_a_first_class_claim():
    claims = explicit_exclusion_claims("Drive cars, but with no bikes.")
    assert [(claim.field, claim.value) for claim in claims] == [
        ("capability.bike", False)
    ]


def test_stage_registry_rejects_second_execution():
    registry = StageExecutionRegistry()
    assert registry.run("resolve", lambda: 1) == 1
    with pytest.raises(RuntimeError, match="more than once"):
        registry.run("resolve", lambda: 2)


def test_validator_stage_detects_mutation():
    schema = _schema("Validation")

    def mutating_validator(value):
        value.locomotion = "fly"
        return []

    with pytest.raises(RuntimeError, match="validator mutated"):
        ValidateSchemaStage().run(schema, [mutating_validator])


def test_prompt_truncation_is_machine_and_human_visible():
    _block, warning = brief_block_report("x" * (BRIEF_CAP_CHARS + 17))
    assert warning is not None
    assert warning.dropped_chars == 17
    assert warning.to_dict()["message"] == "Prompt truncated"
    assert "Dropped: 17 chars" in warning.render()


def test_mechanic_registry_has_pilot_mechanics():
    registry = default_mechanic_registry()
    assert registry.get("fire").runtime == ("Controller",)
    assert registry.get("projectile").validators
    assert registry.get("health").templates == ("health",)


def test_thirty_golden_intent_cases_are_partitioned_and_pass():
    cases = load_intent_cases()
    assert len(cases) == 30
    assert {category: sum(case["category"] == category for case in cases)
            for category in ("structured", "natural", "regression")} == {
                "structured": 10, "natural": 10, "regression": 10,
            }
    failures = [result for result in run_intent_evals() if not result["pass"]]
    assert failures == []


@pytest.mark.parametrize(("seed", "resolved"), [("2D", "3D"), ("3D", "2D")])
def test_projectile_delivery_uses_resolved_dimension(seed, resolved):
    from src.unity_pipeline_stages import ResolveIntentStage

    schema = _schema("ProjectileDimension")
    schema.game_type = seed
    ledger = IntentLedger()
    ledger.register(IntentClaim("game_type", resolved, "user", 1.0, "explicit dimension"))
    ResolveIntentStage().run(
        schema, ledger, prompt="The hero tosses rocks at targets.", schema_source="schema_llm"
    )
    assert schema.game_type == resolved
    assert ("physical_projectile_contract" in schema.meta) == (resolved == "3D")
    assert any(entity.kind == "weapon" for entity in schema.entities) == (resolved == "3D")


@pytest.mark.parametrize("brief", ["An FPS with hitscan bullets.", "Explore a canyon."])
def test_resolution_clears_stale_projectile_contract(brief):
    from src.unity_pipeline_stages import ResolveIntentStage
    from src.unity_projectile_contract import physical_projectile_contract_for

    schema = _schema("StaleProjectile")
    schema.meta["physical_projectile_contract"] = physical_projectile_contract_for(
        "3D", "The hero tosses rocks at targets."
    )
    assert schema.meta["physical_projectile_contract"]
    resolution, _ = ResolveIntentStage().run(
        schema, IntentLedger(), prompt=brief, schema_source="schema_llm"
    )
    assert resolution.decisions["projectile_contract"] is None
    assert "physical_projectile_contract" not in schema.meta


def test_vehicle_exclusion_changes_controller_and_removes_vehicle_mode():
    from src.unity_compose import subsystems_for
    from src.unity_pipeline_stages import ResolveIntentStage

    schema = _schema("VehicleExcluded")
    schema.locomotion = "drive"
    schema.locomotion_modes = ["on_foot", "vehicle_car"]
    brief = "No vehicles."
    ledger = IntentLedger()
    ledger.extend(explicit_exclusion_claims(brief))
    resolution, _ = ResolveIntentStage().run(
        schema, ledger, prompt=brief, schema_source="schema_llm"
    )
    assert schema.locomotion == "walk"
    assert resolution.winners["locomotion"].source == "user_exclusion"
    assert schema.locomotion_modes == ["on_foot"]
    subsystems = subsystems_for(
        schema.game_type, schema.features, locomotion=schema.locomotion,
        locomotion_modes=schema.locomotion_modes, intent_exclusions=list(resolution.exclusions),
        user_prompt=brief,
    )
    controller = next(sub.variant for sub in subsystems if sub.kind == "controller")
    assert controller != "multi_mode" and not controller.startswith("vehicle_")
