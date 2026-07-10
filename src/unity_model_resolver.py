"""
Default-model resolution — kind → real model files that replace the primitive
placeholders (Player capsule, Enemy sphere, entity cubes, vehicle box).

Reads lookups/unity/default_models.json and checks which files actually exist
under `model_source_dir` (default Models/Defaults). Anything missing degrades
to the primitive — the pack is upgradeable by dropping files in (see
Models/Defaults/README.md for the shopping list).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class ResolvedModel(BaseModel):
    kind:          str
    src:           str            # absolute source path on disk
    dest_rel:      str            # path under Assets/Resources/ (e.g. Models/Default/Player/Player.fbx)
    resource_path: str            # Resources.Load path (no extension)
    rig:           str = "none"   # "humanoid" | "none"
    scale:         float = 1.0
    clips:         dict[str, "ResolvedModel"] = Field(default_factory=dict)
    # Provenance — additive, defaults preserve every pre-existing caller.
    # Populated for catalog picks (Kenney or Sketchfab) by
    # unity_entity_asset_resolver._register(); default-pack models stay
    # source="kenney" (no license/attribution obligation).
    source:        str = "kenney"        # "kenney" | "sketchfab"
    license:       Optional[str] = None  # e.g. "cc0" | "by" | "by-sa" | "by-nd"
    attribution:   Optional[str] = None  # required-credit text for non-CC0 picks
    # Runtime bounds-fit target (longest-axis meters), Sketchfab picks only —
    # Kenney/default-pack models keep their pack-native scale (0.0 = no fit;
    # BoundsFit.cs.j2 is a no-op at 0). See lookups/unity/kind_sizes.json and
    # unity_entity_asset_resolver._register.
    fit_size:      float = 0.0


def resolve_default_models(lookups_dir: str = "lookups",
                           model_source_dir: Optional[str] = None) -> dict[str, ResolvedModel]:
    """Return {kind: ResolvedModel} for every catalog kind whose model file exists."""
    src_root = Path(model_source_dir or "Models/Defaults")
    catalog_path = Path(lookups_dir) / "unity" / "default_models.json"
    if not catalog_path.exists() or not src_root.exists():
        return {}
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    res_root = catalog.get("_config", {}).get("resources_root", "Models/Default")
    out: dict[str, ResolvedModel] = {}
    for kind, info in catalog.get("kinds", {}).items():
        model_rel = info.get("model", "")
        src = src_root / model_rel
        if not model_rel or not src.is_file():
            continue
        dest_rel = f"{res_root}/{kind.capitalize()}/{src.name}"
        rm = ResolvedModel(
            kind=kind,
            src=str(src.resolve()),
            dest_rel=dest_rel,
            resource_path=dest_rel.rsplit(".", 1)[0],
            rig=str(info.get("rig", "none")).lower(),
            scale=float(info.get("scale", 1.0) or 1.0),
        )
        for clip_name, clip_rel in (info.get("clips") or {}).items():
            csrc = src_root / clip_rel
            if not csrc.is_file():
                continue
            cdest = f"{res_root}/{kind.capitalize()}/{csrc.name}"
            rm.clips[clip_name] = ResolvedModel(
                kind=f"{kind}/{clip_name}", src=str(csrc.resolve()),
                dest_rel=cdest, resource_path=cdest.rsplit(".", 1)[0],
                rig=rm.rig, scale=rm.scale,
            )
        out[kind] = rm
    return out


_VEHICLE_WORDS = ("vehicle", "car", "jeep", "truck", "plane", "aircraft",
                  "jet", "bike", "motorcycle", "motorbike", "ship", "tank")


def models_for_schema(schema, resolved: dict[str, ResolvedModel],
                      composed_units: list | None = None,
                      override_kinds: set[str] | None = None) -> dict[str, ResolvedModel]:
    """Filter the resolved pack down to the kinds this project actually uses.

    Selection rules (first match wins per kind):
    - player   : player script present OR composed controller
    - enemy    : enemy script / composed ai / any enemy entity
    - vehicle  : locomotion is drive, OR locomotion is fly AND the schema's own
                 text actually names a vehicle, OR any vehicle entity —
                 "fly" alone is NOT enough (hit live: a wingsuit/skydiving
                 game with locomotion=fly has no pilotable vehicle at all;
                 the player's own body flies, so the blanket assumption
                 shipped an unused Car.fbx into every such project)
    - <other>  : any entity whose kind has a catalog entry
    - overrides: any kind in override_kinds is always included when resolved
                 (ensures Blender-built models are wired in regardless of schema)
    """
    if not resolved:
        return {}
    used: dict[str, ResolvedModel] = {}
    stypes = {s.type for s in schema.scripts}
    ckinds = {u.kind for u in (composed_units or [])}

    if ("player" in stypes or "controller" in ckinds) and "player" in resolved:
        used["player"] = resolved["player"]

    wants_enemy = ("enemy" in stypes or "ai" in ckinds
                   or any(e.kind == "enemy" for e in schema.entities))
    if wants_enemy and "enemy" in resolved:
        used["enemy"] = resolved["enemy"]

    loco = getattr(schema, "locomotion", "walk")
    combined = f"{schema.description or ''} " + " ".join(e.name for e in schema.entities)
    combined = combined.lower()
    wants_vehicle = (loco == "drive"
                     or (loco == "fly" and any(w in combined for w in _VEHICLE_WORDS))
                     or any(e.kind == "vehicle" for e in schema.entities))
    if wants_vehicle and "vehicle" in resolved:
        used["vehicle"] = resolved["vehicle"]

    for e in schema.entities:
        if e.kind in resolved and e.kind not in used:
            used[e.kind] = resolved[e.kind]

    # Always include explicitly Blender-built kinds — the pipeline built them
    # for this project, so they should be wired in regardless of schema flags.
    for kind in (override_kinds or set()):
        if kind in resolved and kind not in used:
            used[kind] = resolved[kind]

    return used
