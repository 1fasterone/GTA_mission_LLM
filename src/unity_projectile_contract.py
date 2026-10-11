"""Deterministic Slice-2 bridge from a physical-projectile brief to compose.

The Pipeline LLM still owns the concept and UnitySchema fill.  This module is
part of the deterministic tail: it only recognizes a closed set of explicit
physical-projectile terms, records the existing ControllerSchema ``weapons``
contract for composition, and makes sure its one view-model entity exists.

Bare ``shoot``/FPS prompts deliberately do *not* pass this gate.  They retain
the controller's historical hitscan delivery until a brief asks for a physical
round (bullet, projectile, slingshot, arrow, and so on).
"""

from __future__ import annotations

import re
from typing import Any

from .unity_schema import EntitySpec, UnitySchema


PHYSICAL_PROJECTILE_META_KEY = "physical_projectile_contract"
PHYSICAL_PROJECTILE_CONTRACT_VERSION = "slice2-3d-v1"

_HITSCAN_RE = re.compile(r"\bhit[- ]?scan\b", re.IGNORECASE)
_PHYSICAL_RE = re.compile(
    r"\b(?:projectile|projectiles|bullet|bullets|ballistic|slingshot|"
    r"slingshotted|slung|arrow|arrows|crossbow|bolt|bolts|rocket|rockets)\b",
    re.IGNORECASE,
)
_THREE_DIMENSIONAL_GAME_TYPES = frozenset({"3d", "2.5d", "vr"})


def physical_projectile_contract_for(
    game_type: str,
    user_prompt: str,
) -> dict[str, Any] | None:
    """Return the closed typed P2 3D loadout request, or ``None``.

    An explicit hitscan request wins even if it uses visual bullet language;
    that preserves the independent hitscan code path.  P2 emits no 2D loadout
    because P4 owns the Rigidbody2D/Sprite projectile contract.
    """
    prompt = user_prompt or ""
    if _HITSCAN_RE.search(prompt) or not _PHYSICAL_RE.search(prompt):
        return None
    if str(game_type or "").strip().lower() not in _THREE_DIMENSIONAL_GAME_TYPES:
        return None
    return {
        "version": PHYSICAL_PROJECTILE_CONTRACT_VERSION,
        "delivery": "projectile_3d",
        "weapon": {
            "name": "Projectile Weapon",
            "damage": 25.0,
            "fire_rate": 0.15,
            "magazine_size": 30,
            "reload_time": 2.0,
            "shoot_range": 100.0,
            "delivery": "projectile_3d",
            "projectile_speed": 60.0,
            "projectile_use_gravity": False,
            "projectile_lifetime": 5.0,
            "projectile_pool_size": 8,
            "projectile_pool_growth": "fixed",
        },
    }


def apply_physical_projectile_contract(
    schema: UnitySchema,
    user_prompt: str,
) -> list[str]:
    """Stamp P2's deterministic contract and ensure a single held weapon.

    ``meta`` here is compiler provenance, not an LLM-invented schema field.
    It is overwritten from the raw brief on every pass so an LLM-authored meta
    value can never silently switch a hitscan game to physical delivery.
    """
    contract = physical_projectile_contract_for(schema.game_type, user_prompt)
    meta = dict(schema.meta or {})
    previous = meta.pop(PHYSICAL_PROJECTILE_META_KEY, None)
    if contract is None:
        schema.meta = meta
        if (_PHYSICAL_RE.search(user_prompt or "")
                and not _HITSCAN_RE.search(user_prompt or "")
                and str(schema.game_type or "").strip().lower()
                not in _THREE_DIMENSIONAL_GAME_TYPES):
            return [
                "[projectile.p2] physical projectile request is outside Slice 2's "
                "3D contract; preserved hitscan delivery (2D belongs to Slice 4)."
            ]
        return []

    meta[PHYSICAL_PROJECTILE_META_KEY] = contract
    schema.meta = meta
    messages = []
    if previous != contract:
        messages.append(
            "[projectile.p2] explicit 3D physical-projectile brief -> "
            "typed projectile_3d loadout"
        )

    if not any(str(entity.kind).lower() == "weapon" for entity in schema.entities):
        schema.entities.append(EntitySpec(
            name="PlayerWeapon",
            kind="weapon",
            count=1,
            shape="cube",
            placement="single",
            color_role="accent",
            scale=0.45,
            model_hint="rifle weapon",
        ))
        messages.append(
            "[projectile.p2] added PlayerWeapon view-model entity for the held loadout"
        )
    return messages
