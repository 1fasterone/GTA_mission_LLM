"""The single authority that resolves intent claims into schema decisions."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from .claims import IntentClaim
from .ledger import IntentLedger


_SOURCE_PRIORITY = {
    "user_exclusion": 100,
    "user": 90,
    "schema_deterministic": 75,
    "schema_llm": 70,
    "projectile_classifier": 65,
    "keyword_classifier": 60,
    "rule_system": 50,
}


@dataclass(frozen=True)
class IntentResolution:
    decisions: dict[str, Any]
    winners: dict[str, IntentClaim]
    exclusions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "intent-resolution/v1",
            "decisions": deepcopy(self.decisions),
            "winners": {field: asdict(claim) for field, claim in self.winners.items()},
            "exclusions": list(self.exclusions),
        }


def _rank(claim: IntentClaim) -> tuple[float, int]:
    return (claim.confidence, _SOURCE_PRIORITY.get(claim.source, 0))


def resolve_intent(ledger: IntentLedger) -> IntentResolution:
    winners: dict[str, IntentClaim] = {}
    exclusions: list[str] = []
    for claim in ledger.claims:
        if claim.field.startswith("capability.") and claim.value is False:
            exclusions.append(claim.field.split(".", 1)[1])
            continue
        previous = winners.get(claim.field)
        if previous is None or _rank(claim) > _rank(previous):
            winners[claim.field] = claim

    decisions = {field: claim.value for field, claim in winners.items()}
    modes = list(decisions.get("locomotion_modes") or [])
    if "bike" in exclusions:
        modes = [mode for mode in modes if mode not in ("bike", "drive")]
        if modes:
            decisions["locomotion_modes"] = modes
        elif "locomotion_modes" in decisions:
            decisions["locomotion_modes"] = []
    if "vehicle" in exclusions:
        if "locomotion_modes" in decisions:
            decisions["locomotion_modes"] = [
                mode for mode in decisions["locomotion_modes"] if mode != "vehicle_car"
            ]
        if decisions.get("locomotion") == "drive":
            alternatives = [
                claim for claim in ledger.for_field("locomotion") if claim.value != "drive"
            ]
            winner = max(alternatives, key=_rank, default=IntentClaim(
                "locomotion", "walk", "user_exclusion", 1.0,
                "vehicle exclusion requires a non-driving fallback",
            ))
            decisions["locomotion"] = winner.value
            winners["locomotion"] = winner
    # Keep decisions, winning evidence and the ledger independent of each other.
    return IntentResolution(deepcopy(decisions), deepcopy(winners), tuple(sorted(set(exclusions))))


def apply_resolution(schema: Any, resolution: IntentResolution) -> list[str]:
    """Apply canonical decisions and record their provenance in schema meta."""
    messages: list[str] = []
    scalar_fields = ("game_type", "locomotion")
    for field in scalar_fields:
        if field not in resolution.decisions:
            continue
        old = getattr(schema, field)
        new = resolution.decisions[field]
        if old != new:
            setattr(schema, field, new)
            winner = resolution.winners[field]
            messages.append(
                f"Intent resolver: {field} {old!r} -> {new!r} "
                f"(source={winner.source}; {winner.rationale})"
            )

    if "locomotion_modes" in resolution.decisions:
        old_modes = list(schema.locomotion_modes or [])
        new_modes = list(resolution.decisions["locomotion_modes"] or [])
        if old_modes != new_modes:
            schema.locomotion_modes = new_modes
            messages.append(
                f"Intent resolver: locomotion_modes {old_modes!r} -> {new_modes!r}"
            )

    meta = dict(schema.meta or {})
    for field in ("genre", "flying_abilities", "projectile_contract"):
        if field not in resolution.decisions:
            continue
        key = "physical_projectile_contract" if field == "projectile_contract" else field
        old = meta.get(key)
        new = resolution.decisions[field]
        if field == "projectile_contract" and new is None:
            if key in meta:
                del meta[key]
                messages.append(f"Intent resolver: cleared {key}")
        elif old != new:
            meta[key] = deepcopy(new)
            messages.append(f"Intent resolver: {key} {old!r} -> {new!r}")
    # Composition reads this field, including when a fresh resolution clears
    # exclusions retained in a saved schema.
    meta["intent_exclusions"] = list(resolution.exclusions)
    meta["intent_resolution"] = resolution.to_dict()
    schema.meta = meta
    return messages
