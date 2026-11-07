"""Read-only validation for the resolved physical-projectile schema contract.

Prompt interpretation lives in ``src.intent.projectile``. Asset lookup lives in
``src.unity_projectile_assets``. This module is deliberately validation-only:
it may report findings but cannot materialize, repair, or enrich schema.
"""

from __future__ import annotations

from .schema_validation import read_only_schema_validator
from .unity_schema import UnitySchema


PHYSICAL_PROJECTILE_META_KEY = "physical_projectile_contract"
PHYSICAL_PROJECTILE_CONTRACT_VERSION = "slice3-3d-v3"
_MOTION_PROFILES = frozenset({"fast_round", "thrown_object"})


@read_only_schema_validator
def validate_physical_projectile_contract(schema: UnitySchema) -> list[str]:
    """Report drift in the resolved contract without receiving canonical IR."""
    contract = (schema.meta or {}).get(PHYSICAL_PROJECTILE_META_KEY)
    if contract is None:
        return []
    if not isinstance(contract, dict):
        return ["[projectile.validation] contract must be an object"]
    errors: list[str] = []
    if contract.get("version") != PHYSICAL_PROJECTILE_CONTRACT_VERSION:
        errors.append(
            "[projectile.validation] contract version must be "
            f"{PHYSICAL_PROJECTILE_CONTRACT_VERSION}"
        )
    if contract.get("delivery") != "projectile_3d":
        errors.append("[projectile.validation] delivery must be projectile_3d")
    motion_profile = contract.get("motion_profile")
    if motion_profile not in _MOTION_PROFILES:
        errors.append(
            "[projectile.validation] unknown motion_profile "
            f"{motion_profile!r}"
        )
    weapon = contract.get("weapon")
    if not isinstance(weapon, dict):
        errors.append("[projectile.validation] weapon contract is missing")
    elif weapon.get("delivery") != "projectile_3d":
        errors.append("[projectile.validation] weapon delivery must be projectile_3d")
    presentation = contract.get("presentation")
    if not isinstance(presentation, dict):
        errors.append("[projectile.validation] presentation contract is missing")
    elif not str(presentation.get("resolver_name") or "").strip():
        errors.append("[projectile.validation] presentation resolver_name is missing")
    if not any(str(entity.kind).lower() == "weapon" for entity in schema.entities):
        errors.append("[projectile.validation] resolved contract has no weapon entity")
    return errors


__all__ = [
    "PHYSICAL_PROJECTILE_CONTRACT_VERSION",
    "PHYSICAL_PROJECTILE_META_KEY",
    "validate_physical_projectile_contract",
]
