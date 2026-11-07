"""Read-only execution boundary for validators over canonical schema IR."""

from __future__ import annotations

from copy import deepcopy
from functools import wraps
from typing import Any, Callable, Iterable, TypeVar


Finding = TypeVar("Finding")
Validator = Callable[[Any], Iterable[Finding]]
_READ_ONLY_MARKER = "__schema_validator_read_only__"


class SchemaValidationMutationError(RuntimeError):
    """Raised when a validator attempts to change its isolated schema copy."""


def _clone_schema(schema: Any) -> Any:
    copier = getattr(schema, "model_copy", None)
    if callable(copier):
        return copier(deep=True)
    return deepcopy(schema)


def _schema_state(schema: Any) -> Any:
    dumper = getattr(schema, "model_dump", None)
    if callable(dumper):
        return dumper(mode="json")
    return deepcopy(schema)


def run_read_only_validator(schema: Any, validator: Validator[Finding]) -> list[Finding]:
    """Run ``validator`` on a deep copy and reject any attempted write.

    The canonical schema is never passed to validator code. A mutating validator
    therefore cannot change gameplay even when it is called outside the normal
    validation stage.
    """
    isolated = _clone_schema(schema)
    before = _schema_state(isolated)
    findings = list(validator(isolated))
    after = _schema_state(isolated)
    if before != after:
        name = getattr(validator, "__qualname__", repr(validator))
        raise SchemaValidationMutationError(
            f"validator {name} mutated schema; validators are read-only"
        )
    return findings


def read_only_schema_validator(
    validator: Validator[Finding],
) -> Validator[Finding]:
    """Decorate a validator so every direct call uses the read-only boundary."""
    @wraps(validator)
    def guarded(schema: Any) -> list[Finding]:
        return run_read_only_validator(schema, validator)

    setattr(guarded, _READ_ONLY_MARKER, True)
    return guarded


def execute_schema_validator(
    schema: Any,
    validator: Validator[Finding],
) -> list[Finding]:
    """Execute decorated and ad-hoc validators through one guarded entry point."""
    if getattr(validator, _READ_ONLY_MARKER, False):
        return list(validator(schema))
    return run_read_only_validator(schema, validator)


__all__ = [
    "SchemaValidationMutationError",
    "execute_schema_validator",
    "read_only_schema_validator",
    "run_read_only_validator",
]
