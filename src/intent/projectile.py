"""Projectile intent evidence derived from the untouched user brief.

This module may interpret prompt text and construct a closed claim value. It
does not receive or mutate ``UnitySchema``. The intent resolver chooses whether
the value becomes part of the canonical schema.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from ..unity_projectile_contract import PHYSICAL_PROJECTILE_CONTRACT_VERSION


_HITSCAN_RE = re.compile(r"\bhit[- ]?scan\b", re.IGNORECASE)
_PHYSICAL_RE = re.compile(
    r"\b(?:projectile|projectiles|bullet|bullets|ballistic|slingshot|"
    r"slingshotted|slung|arrow|arrows|crossbow|bolt|bolts|rocket|rockets)\b",
    re.IGNORECASE,
)
_THROW_ACTION_RE = re.compile(
    r"\b(?:throw|throws|throwing|threw|toss|tosses|tossing|lob|lobs|lobbing|"
    r"hurl|hurls|hurling)\b",
    re.IGNORECASE,
)
_THROWN_OBJECT_RE = re.compile(
    r"\b(?:rock|rocks|stone|stones)\b",
    re.IGNORECASE,
)
_THREE_DIMENSIONAL_GAME_TYPES = frozenset({"3d", "2.5d", "vr"})

_PROJECTILE_PRESENTATIONS: dict[str, dict[str, Any]] = {
    "fast_round": {
        "resolver_name": "ProjectileVisualFastRound",
        "model_hint": "bullet projectile round",
        "resource_path": "",
        "fallback_name": "ProjectileFallback_FastRound_Capsule",
        "fallback_primitive": "Capsule",
        "uniform_scale": 0.08,
        "local_euler": [90.0, 0.0, 0.0],
        "fallback_color": [1.0, 0.58, 0.08, 1.0],
        "physics_radius": 0.04,
    },
    "thrown_object": {
        "resolver_name": "ProjectileVisualThrownObject",
        "model_hint": "rough throwing rock stone",
        "resource_path": "",
        "fallback_name": "ProjectileFallback_ThrownObject_Cube",
        "fallback_primitive": "Cube",
        "uniform_scale": 0.22,
        "local_euler": [18.0, 31.0, 12.0],
        "fallback_color": [0.34, 0.27, 0.20, 1.0],
        "physics_radius": 0.10,
    },
}

_PROJECTILE_PROFILES: dict[str, dict[str, Any]] = {
    "fast_round": {
        "motion_profile": "fast_round",
        "emission_profile": "muzzle",
        "visual_profile": "bullet",
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
        "view_model": {
            "name": "PlayerWeapon",
            "model_hint": "rifle weapon",
            "scale": 0.45,
        },
    },
    "thrown_object": {
        "motion_profile": "thrown_object",
        "emission_profile": "throw_release",
        "visual_profile": "rock",
        "weapon": {
            "name": "Thrown Rock",
            "damage": 20.0,
            "fire_rate": 0.75,
            "magazine_size": 6,
            "reload_time": 1.5,
            "shoot_range": 35.0,
            "delivery": "projectile_3d",
            "projectile_speed": 18.0,
            "projectile_use_gravity": True,
            "projectile_lifetime": 8.0,
            "projectile_pool_size": 8,
            "projectile_pool_growth": "fixed",
        },
        "view_model": {
            "name": "ThrownRock",
            "model_hint": "rough throwing rock stone",
            "scale": 0.22,
        },
    },
}


def _profile_for(prompt: str) -> str | None:
    if _HITSCAN_RE.search(prompt):
        return None
    if _THROW_ACTION_RE.search(prompt) and _THROWN_OBJECT_RE.search(prompt):
        return "thrown_object"
    if _PHYSICAL_RE.search(prompt):
        return "fast_round"
    return None


def physical_projectile_contract_for(
    game_type: str,
    user_prompt: str,
) -> dict[str, Any] | None:
    """Return one closed projectile intent value without touching schema."""
    profile_name = _profile_for(user_prompt or "")
    if profile_name is None:
        return None
    if str(game_type or "").strip().lower() not in _THREE_DIMENSIONAL_GAME_TYPES:
        return None
    contract = deepcopy(_PROJECTILE_PROFILES[profile_name])
    contract.update({
        "version": PHYSICAL_PROJECTILE_CONTRACT_VERSION,
        "delivery": "projectile_3d",
        "presentation": deepcopy(_PROJECTILE_PRESENTATIONS[profile_name]),
    })
    return contract


__all__ = ["physical_projectile_contract_for"]
