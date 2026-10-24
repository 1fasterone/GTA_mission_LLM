"""Deterministic bridge from a physical-projectile brief to compose.

The Pipeline LLM still owns the concept and UnitySchema fill. This module is
part of the deterministic tail: it recognizes a closed set of explicit
physical-projectile terms, selects one compiler-owned profile, records the
existing ControllerSchema ``weapons`` contract for composition, and makes
sure its one view-model entity exists.

Bare ``shoot``/FPS prompts deliberately do not pass this gate. They retain
the controller's historical hitscan delivery until a brief asks for a
physical round. The runtime ProjectileController and ProjectilePool remain
the single implementation of movement, swept collision, damage, and reuse;
profiles only specialize the typed inputs supplied to that foundation.
"""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .unity_schema import EntitySpec, UnitySchema


PHYSICAL_PROJECTILE_META_KEY = "physical_projectile_contract"
PHYSICAL_PROJECTILE_CONTRACT_VERSION = "slice3-3d-v3"

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
        "projectile_visual": {
            "entity_name": "ProjectileBulletVisual",
            "kind": "projectile_visual",
            "model_hint": "bullet cartridge ammunition",
            "fallback": "bullet",
            "target_size": 0.20,
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
        "projectile_visual": {
            "entity_name": "ProjectileRockVisual",
            "kind": "projectile_visual",
            "model_hint": "small rough throwing rock stone",
            "fallback": "rock",
            "target_size": 0.24,
        },
    },
}


def _profile_for(prompt: str) -> str | None:
    """Select one closed physical-projectile profile from raw brief text."""
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
    """Return a closed typed 3D loadout request, or ``None``.

    An explicit hitscan request wins even if it uses visual bullet language;
    that preserves the independent hitscan code path. This bridge emits no 2D
    loadout because the Rigidbody2D/Sprite projectile contract is independent.
    """
    prompt = user_prompt or ""
    profile_name = _profile_for(prompt)
    if profile_name is None:
        return None
    if str(game_type or "").strip().lower() not in _THREE_DIMENSIONAL_GAME_TYPES:
        return None

    contract = deepcopy(_PROJECTILE_PROFILES[profile_name])
    contract.update({
        "version": PHYSICAL_PROJECTILE_CONTRACT_VERSION,
        "delivery": "projectile_3d",
    })
    return contract


def projectile_visual_spec_for_entity(
    schema: UnitySchema,
    entity_name: str,
) -> dict[str, Any] | None:
    """Return the compiler-owned visual spec for one synthetic entity.

    The helper is shared with the asset resolver so strict mode can distinguish
    an allowed projectile primitive fallback from an ordinary unresolved scene
    entity. It trusts only the versioned deterministic contract stamped by this
    module; arbitrary schema metadata cannot opt an entity out of strict assets.
    """
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    contract = meta.get(PHYSICAL_PROJECTILE_META_KEY)
    if not isinstance(contract, dict):
        return None
    if contract.get("version") != PHYSICAL_PROJECTILE_CONTRACT_VERSION:
        return None
    visual = contract.get("projectile_visual")
    if not isinstance(visual, dict):
        return None
    if visual.get("entity_name") != entity_name:
        return None
    fallback = visual.get("fallback")
    if fallback not in {"bullet", "rock"}:
        return None
    return visual


def bind_projectile_visual_resolution(
    schema: UnitySchema,
    entity_model_paths: dict[str, str],
) -> dict[str, Any] | None:
    """Bind the resolver result and remove the synthetic entity from the world.

    The asset resolver must see the entity so a real model can enter the normal
    copy/Resources chain. The scene spawner must not see it as world content,
    because the controller owns its instances. This is the single deterministic
    handoff between those two responsibilities.
    """
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    contract = meta.get(PHYSICAL_PROJECTILE_META_KEY)
    if not isinstance(contract, dict):
        return None
    visual = contract.get("projectile_visual")
    if not isinstance(visual, dict):
        return None
    entity_name = visual.get("entity_name")
    fallback = visual.get("fallback")
    if not isinstance(entity_name, str) or fallback not in {"bullet", "rock"}:
        return None

    resolved = entity_model_paths.get(entity_name, f"primitive:{fallback}")
    if resolved.startswith("primitive:"):
        visual["resolution"] = "primitive"
        visual["resource_path"] = ""
    elif resolved.startswith(("skip:", "procedural:", "fx:", "ui:")):
        visual["resolution"] = "primitive"
        visual["resource_path"] = ""
    else:
        visual["resolution"] = "asset"
        visual["resource_path"] = resolved

    entity_model_paths[entity_name] = "skip:projectile-visual"
    recorded_paths = meta.get("entity_model_paths")
    if isinstance(recorded_paths, dict):
        recorded_paths[entity_name] = "skip:projectile-visual"
    return visual


def apply_physical_projectile_contract(
    schema: UnitySchema,
    user_prompt: str,
) -> list[str]:
    """Stamp the deterministic contract and ensure one held projectile model.

    ``meta`` here is compiler provenance, not an LLM-invented schema field. It
    is overwritten from the raw brief on every pass so an LLM-authored meta
    value can never silently switch a hitscan game to physical delivery.
    """
    prompt = user_prompt or ""
    contract = physical_projectile_contract_for(schema.game_type, prompt)
    meta = dict(schema.meta or {})
    previous = meta.pop(PHYSICAL_PROJECTILE_META_KEY, None)
    if contract is None:
        schema.meta = meta
        if (_profile_for(prompt) is not None
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
            f"typed projectile_3d loadout ({contract['motion_profile']})"
        )

    if not any(str(entity.kind).lower() == "weapon" for entity in schema.entities):
        view_model = contract["view_model"]
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
        messages.append(
            "[projectile.p2] added "
            f"{view_model['name']} view-model entity for the held loadout"
        )

    projectile_visual = contract["projectile_visual"]
    visual_name = projectile_visual["entity_name"]
    if not any(entity.name == visual_name for entity in schema.entities):
        schema.entities.append(EntitySpec(
            name=visual_name,
            kind=projectile_visual["kind"],
            count=1,
            shape="sphere",
            placement="single",
            color_role="accent",
            scale=projectile_visual["target_size"],
            model_hint=projectile_visual["model_hint"],
        ))
        messages.append(
            "[projectile.p3] added "
            f"{visual_name} resolver entity with named "
            f"{projectile_visual['fallback']} primitive fallback"
        )
    return messages
