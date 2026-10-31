"""Small stage objects that make orchestration order and ownership explicit.

The historical pipeline remains synchronous, but every named stage registers
exactly once. A duplicate call is a compiler error instead of a quiet second
reconciliation/resolution pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from ..intent.claims import (
    claims_from_classifier,
    claims_from_schema,
    explicit_exclusion_claims,
    projectile_claims,
)
from ..intent.ledger import IntentLedger
from ..intent.resolver import IntentResolution, apply_resolution, resolve_intent
from ..unity_archetypes import classify
from ..unity_projectile_contract import physical_projectile_contract_for

T = TypeVar("T")


@dataclass
class StageExecutionRegistry:
    executed: list[str] = field(default_factory=list)

    def run(self, name: str, action: Callable[[], T]) -> T:
        if name in self.executed:
            raise RuntimeError(f"pipeline stage {name!r} executed more than once")
        result = action()
        self.executed.append(name)
        return result


class ExtractIntentStage:
    name = "extract_intent"

    def run(self, prompt: str) -> tuple[Any, IntentLedger]:
        classifier_intent = classify(prompt)
        ledger = IntentLedger()
        ledger.extend(explicit_exclusion_claims(prompt))
        ledger.extend(claims_from_classifier(classifier_intent))
        return classifier_intent, ledger


class GenerateSchemaStage:
    name = "generate_schema"

    def accept(self, schema: Any) -> Any:
        if schema is None:
            raise ValueError("schema generation produced no schema")
        return schema


class ResolveIntentStage:
    name = "resolve_intent"

    def run(self, schema: Any, ledger: IntentLedger, *, prompt: str,
            schema_source: str) -> tuple[IntentResolution, list[str]]:
        ledger.extend(claims_from_schema(schema, source=schema_source))
        contract = physical_projectile_contract_for(schema.game_type, prompt)
        ledger.extend(projectile_claims(schema.game_type, prompt, contract))
        resolution = resolve_intent(ledger)
        messages = apply_resolution(schema, resolution)
        _materialize_projectile_view_model(schema, resolution)
        return resolution, messages


def _materialize_projectile_view_model(schema: Any,
                                       resolution: IntentResolution) -> None:
    """Schema-generation responsibility for the resolved projectile intent."""
    contract = resolution.decisions.get("projectile_contract")
    if not isinstance(contract, dict):
        return
    view_model = contract.get("view_model")
    if not isinstance(view_model, dict):
        return
    if any(str(entity.kind).lower() == "weapon" for entity in schema.entities):
        return
    from ..unity_schema import EntitySpec

    schema.entities.append(EntitySpec(
        name=view_model["name"],
        kind="weapon",
        count=1,
        shape="cube",
        placement="single",
        color_role="accent",
        scale=view_model["scale"],
        model_hint=view_model["model_hint"],
    ))


class ValidateSchemaStage:
    name = "validate_schema"

    def run(self, schema: Any, validators: list[Callable[[Any], list[str]]]) -> list[str]:
        messages: list[str] = []
        before = schema.model_dump(mode="json")
        for validator in validators:
            messages.extend(validator(schema))
        after = schema.model_dump(mode="json")
        if before != after:
            raise RuntimeError("validator mutated schema; validators are read-only")
        return messages


class ComposeStage:
    name = "compose"

    def run(self, action: Callable[[], T]) -> T:
        return action()


class BindAssetsStage:
    name = "bind_assets"

    def run(self, action: Callable[[], T]) -> T:
        return action()


class GenerateStage:
    name = "generate"

    def run(self, action: Callable[[], T]) -> T:
        return action()
