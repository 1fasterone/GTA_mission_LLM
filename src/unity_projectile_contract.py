"""Read-only validation for the resolved physical-projectile schema contract.

Prompt interpretation lives in ``src.intent.projectile``. Asset lookup lives in
``src.unity_projectile_assets``. This module is deliberately validation-only:
it may report findings but cannot materialize, repair, or enrich schema.
"""

from __future__ import annotations

from .schema_validation import read_only_schema_validator
from .unity_schema import UnitySchema


PHYSICAL_PROJECTILE_META_KEY = "physical_projectile_contract"
PHYSICAL_PROJECTILE_CONTRACT_VERSION = "slice4-3d-v4"
_MOTION_PROFILES = frozenset({"fast_round", "thrown_object"})
# The firearm presentation adapter (Slice 4) belongs to fast rounds only.
FIREARM_ADAPTER = "firearm_animation"
_FIREARM_PROFILES = frozenset({"fast_round"})


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
    firearm = contract.get("firearm_presentation")
    if motion_profile in _FIREARM_PROFILES:
        if not isinstance(firearm, dict):
            errors.append("[projectile.validation] firearm_presentation is missing")
        else:
            if firearm.get("adapter") != FIREARM_ADAPTER:
                errors.append(
                    f"[projectile.validation] firearm adapter must be {FIREARM_ADAPTER}")
            if not str(firearm.get("release_event") or "").isidentifier():
                errors.append(
                    "[projectile.validation] firearm release_event must be a C# "
                    "method name")
            triggers = firearm.get("trigger_candidates")
            if (not isinstance(triggers, list) or not triggers
                    or not all(isinstance(t, str) and t.isidentifier() for t in triggers)):
                errors.append(
                    "[projectile.validation] firearm trigger_candidates must be "
                    "Animator parameter names")
            for field in ("release_timeout", "recoil_distance"):
                value = firearm.get(field)
                if not isinstance(value, (int, float)) or value <= 0:
                    errors.append(
                        f"[projectile.validation] firearm {field} must be positive")
    elif firearm is not None:
        errors.append(
            f"[projectile.validation] {motion_profile!r} cannot carry a firearm adapter")
    if not any(str(entity.kind).lower() == "weapon" for entity in schema.entities):
        errors.append("[projectile.validation] resolved contract has no weapon entity")
    return errors


__all__ = [
    "FIREARM_ADAPTER",
    "PHYSICAL_PROJECTILE_CONTRACT_VERSION",
    "PHYSICAL_PROJECTILE_META_KEY",
    "validate_physical_projectile_contract",
]
