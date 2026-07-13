"""GTA5 spawner micro — enemy/vehicle/prop wave placement around the player."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from ...micro_pipeline import to_pascal
from ..gta5_base import GTA5MicroPipeline
from ..code_fields import GtaCodeExtensibleSchema, lint_gta_extensible, clamp_enum, GTA_EXTRA_INSTRUCTIONS


SpawnerVariant = Literal["radial_arena", "vehicle_pursuit", "prop_scatter"]
VARIANTS = list(SpawnerVariant.__args__)   # type: ignore[attr-defined]

_MODEL_ID_RE = re.compile(r"^[a-z][a-z0-9_]+$")

_WEAPONS = ("none", "pistol", "smg", "rifle", "shotgun", "sniper")
_WEAPON_NATIVE = {
    "pistol": "weapon_pistol", "smg": "weapon_microsmg",
    "rifle": "weapon_assaultrifle", "shotgun": "weapon_pumpshotgun",
    "sniper": "weapon_sniperrifle",
}


class GtaSpawnerSchema(GtaCodeExtensibleSchema):
    name:          str   = "WaveSpawner"
    variant:       str   = "radial_arena"
    ped_model:     str   = "g_m_y_lost_01"
    vehicle_model: str   = "police2"
    prop_model:    str   = "prop_barrel_02a"
    count:         int   = Field(default=4, ge=1, le=12)
    radius:        float = Field(default=25.0, ge=5.0, le=100.0)
    weapon:        str   = "pistol"
    hostile:       bool  = True
    description:   str   = "Wave spawner"
    meta:          dict  = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        return clamp_enum(v, tuple(VARIANTS), "radial_arena")

    @field_validator("weapon", mode="before")
    @classmethod
    def _clamp_weapon(cls, v: str) -> str:
        w = str(v).strip().lower()
        return w if w in _WEAPONS else "pistol"

    @field_validator("ped_model", "vehicle_model", "prop_model", mode="before")
    @classmethod
    def _clamp_model(cls, v, info):
        # Micro pipelines skip Step 4 (no RAG) — models must already be literal
        # lowercase GTA IDs; anything else falls back to a known-good default.
        s = str(v).strip().lower()
        if _MODEL_ID_RE.match(s):
            return s
        return {"ped_model": "g_m_y_lost_01",
                "vehicle_model": "police2",
                "prop_model": "prop_barrel_02a"}[info.field_name]

    @field_validator("count", mode="before")
    @classmethod
    def _clamp_count(cls, v):
        try:
            return max(1, min(12, int(float(v))))
        except (TypeError, ValueError):
            return 4

    @field_validator("radius", mode="before")
    @classmethod
    def _clamp_radius(cls, v):
        try:
            return max(5.0, min(100.0, float(v)))
        except (TypeError, ValueError):
            return 25.0


CONCEPT_PROMPT = """\
{theme_block}
Describe a GTA5 spawner (peds/vehicles/props placed around the player each
wave) for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- What spawns (enemy type, pursuit vehicle, or scattered props) and how many
- How far from the player they appear and whether they attack on sight
- What weapon enemies carry

Do NOT write code or JSON. This is GTA5 / SHVDN3, never Unity.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
ped_model / vehicle_model / prop_model — literal lowercase GTA5 model IDs only
  (e.g. g_m_y_lost_01, police2, prop_barrel_02a); when unsure keep the defaults
weapon — none | pistol | smg | rifle | shotgun | sniper

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "ped_model": "g_m_y_lost_01",
  "vehicle_model": "police2",
  "prop_model": "prop_barrel_02a",
  "count": 4,
  "radius": 25.0,
  "weapon": "pistol",
  "hostile": true,
  "description": "One sentence."
}}
""" + GTA_EXTRA_INSTRUCTIONS


class GtaSpawnerPipeline(GTA5MicroPipeline):
    kind            = "spawner"
    display_name    = "GTA5 Spawner"
    schema_cls      = GtaSpawnerSchema
    variants        = VARIANTS
    default_variant = "radial_arena"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "spawner"

    _RESERVED_FIELD_NAMES = {
        "_spawned", "_spawnedvehicles", "_spawnedprops", "spawnwave",
        "cleanupwave", "alivecount",
    }
    _TEMPLATE_USINGS = {"System", "System.Collections.Generic",
                        "System.Windows.Forms", "GTA", "GTA.Math", "GTA.Native"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "WaveSpawner")
        lint_gta_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                            template_usings=self._TEMPLATE_USINGS)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        weapon_native = _WEAPON_NATIVE.get(schema.weapon)
        (out_dir / f"{schema.name}.3.cs").write_text(
            env.get_template("Spawner.3.cs.j2").render(
                s=schema, variant=variant, weapon_native=weapon_native),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
