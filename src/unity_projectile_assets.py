"""Pure asset-binding helpers for a resolved projectile schema contract."""

from __future__ import annotations

from copy import deepcopy

from .unity_projectile_contract import PHYSICAL_PROJECTILE_META_KEY
from .unity_schema import EntitySpec, UnitySchema


def _presentation(schema: UnitySchema) -> dict:
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    contract = meta.get(PHYSICAL_PROJECTILE_META_KEY)
    if not isinstance(contract, dict):
        return {}
    motion_profile = str(contract.get("motion_profile") or "")
    presentation = contract.get("presentation")
    if not motion_profile:
        raise ValueError("[projectile.visual] contract has no motion profile")
    if not isinstance(presentation, dict):
        raise ValueError(
            "[projectile.visual] resolved contract has no presentation for "
            f"{motion_profile!r}; rerun schema materialization"
        )
    return presentation


def projectile_visual_resolver_entity(schema: UnitySchema) -> EntitySpec | None:
    """Return a transient resolver request without changing canonical schema."""
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    if not isinstance(meta.get(PHYSICAL_PROJECTILE_META_KEY), dict):
        return None
    presentation = _presentation(schema)
    resolver_name = str(presentation.get("resolver_name") or "").strip()
    model_hint = str(presentation.get("model_hint") or "").strip()
    if not resolver_name or not model_hint:
        raise ValueError(
            "[projectile.visual] closed presentation is missing its resolver identity"
        )
    return EntitySpec(
        name=resolver_name,
        kind="prop",
        count=1,
        shape="cube",
        placement="single",
        color_role="accent",
        scale=1.0,
        model_hint=model_hint,
    )


def resolved_projectile_presentation(
    schema: UnitySchema,
    resource_path: str,
) -> dict:
    """Return compiler-owned binding data detached from canonical schema."""
    presentation = _presentation(schema)
    if not presentation:
        return {}
    resolved = deepcopy(presentation)
    resolved["resource_path"] = str(resource_path or "")
    return resolved


__all__ = [
    "projectile_visual_resolver_entity",
    "resolved_projectile_presentation",
]
