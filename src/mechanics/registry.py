"""One registration point for schema/runtime/generator/validation mechanics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable


Predicate = Callable[[Any], bool]


@dataclass(frozen=True)
class Mechanic:
    name: str
    schema_fields: tuple[str, ...]
    runtime: tuple[str, ...]
    generator: tuple[str, ...]
    templates: tuple[str, ...]
    validators: tuple[str, ...]
    enabled_when: Predicate = field(compare=False, repr=False)

    def describe(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("enabled_when", None)
        return data


class MechanicRegistry:
    def __init__(self) -> None:
        self._mechanics: dict[str, Mechanic] = {}

    def register(self, mechanic: Mechanic) -> None:
        if mechanic.name in self._mechanics:
            raise ValueError(f"mechanic {mechanic.name!r} is already registered")
        self._mechanics[mechanic.name] = mechanic

    def get(self, name: str) -> Mechanic:
        return self._mechanics[name]

    def enabled(self, schema: Any) -> list[Mechanic]:
        return [mechanic for mechanic in self._mechanics.values()
                if mechanic.enabled_when(schema)]

    def manifest(self, schema: Any) -> dict[str, Any]:
        return {
            "format": "mechanic-registry/v1",
            "enabled": [mechanic.describe() for mechanic in self.enabled(schema)],
        }


def _has_weapon(schema: Any) -> bool:
    return any(str(entity.kind).lower() == "weapon" for entity in schema.entities)


def _has_projectile(schema: Any) -> bool:
    return isinstance((schema.meta or {}).get("physical_projectile_contract"), dict)


def _has_health(schema: Any) -> bool:
    return ("health" in set(schema.features or [])
            or any(str(entity.kind).lower() == "enemy" for entity in schema.entities))


def default_mechanic_registry() -> MechanicRegistry:
    registry = MechanicRegistry()
    registry.register(Mechanic(
        "fire", ("entities[kind=weapon]", "meta.genre"),
        ("Controller",), ("MicroComposer",), ("controller",),
        ("controller_api",), _has_weapon,
    ))
    registry.register(Mechanic(
        "projectile", ("meta.physical_projectile_contract",),
        ("ProjectileController", "ProjectilePool"), ("MicroComposer",),
        ("controller", "projectile_controller", "projectile_pool"),
        ("physical_projectile_contract", "unity_compile"), _has_projectile,
    ))
    registry.register(Mechanic(
        "health", ("features[health]", "entities[kind=enemy]"),
        ("HealthSystem",), ("MicroComposer",), ("health",),
        ("health_contract", "unity_compile"), _has_health,
    ))
    return registry
