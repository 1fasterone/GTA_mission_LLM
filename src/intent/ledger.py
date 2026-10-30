"""Append-only intent ledger and pipeline snapshot report."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .claims import IntentClaim


@dataclass
class IntentLedger:
    claims: list[IntentClaim] = field(default_factory=list)
    snapshots: list[dict[str, Any]] = field(default_factory=list)

    def register(self, claim: IntentClaim) -> None:
        self.claims.append(claim)

    def extend(self, claims: Iterable[IntentClaim]) -> None:
        for claim in claims:
            self.register(claim)

    def for_field(self, field: str) -> list[IntentClaim]:
        return [claim for claim in self.claims if claim.field == field]

    def snapshot(self, name: str, schema: Any) -> None:
        self.snapshots.append({"name": name, "state": _schema_state(schema)})

    def snapshot_state(self, name: str, state: dict[str, Any]) -> None:
        self.snapshots.append({"name": name, "state": dict(state)})

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "intent-ledger/v1",
            "claims": [asdict(claim) for claim in self.claims],
            "snapshots": list(self.snapshots),
            "diffs": self.diffs(),
        }

    def diffs(self) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for before, after in zip(self.snapshots, self.snapshots[1:]):
            fields = sorted(set(before["state"]) | set(after["state"]))
            for field_name in fields:
                old = before["state"].get(field_name)
                new = after["state"].get(field_name)
                if old != new:
                    changes.append({
                        "field": field_name,
                        "before": old,
                        "after": new,
                        "from": before["name"],
                        "to": after["name"],
                    })
        return changes


def _schema_state(schema: Any) -> dict[str, Any]:
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    return {
        "game_type": schema.game_type,
        "locomotion": schema.locomotion,
        "locomotion_modes": list(schema.locomotion_modes or []),
        "genre": meta.get("genre"),
        "flying_abilities": list(meta.get("flying_abilities") or []),
        "projectile_contract": meta.get("physical_projectile_contract"),
        "entities": [entity.name for entity in schema.entities],
        "scripts": [script.name for script in schema.scripts],
        "features": list(schema.features or []),
    }
