"""Spawner micro-generator — waves, pools, trigger/point/radial spawners."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeExtensibleSchema, lint_extensible, clamp_enum, apply_extensions_safe_block,
    UNITY_EXTRA_INSTRUCTIONS, template_field_types,
)


SpawnerVariant = Literal[
    "wave_spawner", "continuous_spawner", "object_pool",
    "trigger_spawner", "point_spawner", "radial_spawner",
]
VARIANTS = list(SpawnerVariant.__args__)   # type: ignore[attr-defined]

SpawnShape = Literal["point", "circle", "box", "spawn_points"]
Pooling    = Literal["pooled", "instantiate"]
Trigger    = Literal["timer", "on_enter", "manual", "wave"]


class SpawnerSchema(CodeExtensibleSchema):
    name:           str        = "Spawner"
    variant:        str        = "wave_spawner"
    spawn_shape:    SpawnShape = "point"
    pooling:        Pooling    = "instantiate"
    trigger:        Trigger    = "timer"
    max_alive:      int        = 20
    spawn_interval: float      = 2.0
    per_wave_count: int        = 5
    wave_count:     int        = 3
    spawn_radius:   float      = 5.0
    warmup_count:   int        = 0
    target_tag:     str        = "Player"
    prefab_keys:    list[str]  = Field(default_factory=list)
    description:    str        = "Spawner"
    meta:           dict       = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "wave_spawner"

    @field_validator("spawn_shape", mode="before")
    @classmethod
    def _clamp_shape(cls, v: str) -> str:
        return clamp_enum(v, ("point", "circle", "box", "spawn_points"), "point")

    @field_validator("pooling", mode="before")
    @classmethod
    def _clamp_pooling(cls, v: str) -> str:
        return clamp_enum(v, ("pooled", "instantiate"), "instantiate")

    @field_validator("trigger", mode="before")
    @classmethod
    def _clamp_trigger(cls, v: str) -> str:
        return clamp_enum(v, ("timer", "on_enter", "manual", "wave"), "timer")


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity spawning system for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- What gets spawned and where (a point, a circle/box area, or fixed spawn points)
- When it spawns (steady timer, discrete waves, on player trigger, or one-shot at start)
- Whether instances are object-pooled or freshly instantiated
- Any live-count cap, wave ramping, or warm-up behaviour

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
SPAWN_SHAPE — point | circle | box | spawn_points
POOLING     — pooled | instantiate
TRIGGER     — timer | on_enter | manual | wave

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name (no spaces)",
  "variant": "{variant}",
  "spawn_shape": "point",
  "pooling": "instantiate",
  "trigger": "timer",
  "max_alive": 20,
  "spawn_interval": 2.0,
  "per_wave_count": 5,
  "wave_count": 3,
  "spawn_radius": 5.0,
  "warmup_count": 0,
  "target_tag": "Player",
  "prefab_keys": ["Enemy"],
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS


class SpawnerPipeline(UnityMicroPipeline):
    kind            = "spawner"
    display_name    = "Unity Spawner"
    schema_cls      = SpawnerSchema
    variants        = VARIANTS
    default_variant = "wave_spawner"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "spawner"

    _RESERVED_FIELD_NAMES = {
        "prefab", "spawnpoints", "spawnradius", "boxsize", "maxalive",
        "spawninterval", "perwavecount", "wavecount", "warmupcount", "pool",
        "targettag", "_alive", "_timer", "_wave", "_started",
    }
    _TEMPLATE_USINGS = {
        "System", "UnityEngine", "System.Collections", "System.Collections.Generic",
    }

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "Spawner")
        # Clamp numerics to sane positive values.
        if schema.max_alive < 1:        schema.max_alive = 1
        if schema.spawn_interval <= 0:  schema.spawn_interval = 2.0
        if schema.per_wave_count < 1:   schema.per_wave_count = 1
        if schema.wave_count < 1:       schema.wave_count = 1
        if schema.spawn_radius < 0:     schema.spawn_radius = 5.0
        if schema.warmup_count < 0:     schema.warmup_count = 0

        # Variant → field coherence.
        if variant == "object_pool":
            schema.pooling = "pooled"
        elif variant == "wave_spawner":
            schema.trigger = "wave"
        elif variant == "trigger_spawner":
            schema.trigger = "on_enter"
        elif variant == "continuous_spawner":
            schema.trigger = "timer"
        elif variant in ("point_spawner", "radial_spawner"):
            schema.trigger = "manual"

        # radial_spawner only makes sense with an area shape.
        if variant == "radial_spawner" and schema.spawn_shape in ("point", "spawn_points"):
            schema.spawn_shape = "circle"

        # hoist_field_inits: this generator's template renders awake_extra,
        # so a CS0236 initializer can be re-homed there instead of dropped.
        # template_field_types: without it, the template's OWN fields (e.g.
        # `private readonly List<GameObject> _alive`) are invisible to the
        # misuse checks — only schema.extra_fields were ground truth before.
        # Confirmed live (2026-08-28), EnemySpawner.cs(161): `_alive <
        # maxAlive` (CS0019) went uncaught for exactly this reason — see
        # _repair_collection_compared_as_scalar's own docstring. Same
        # convention rules.py already uses for GameRules.cs's fields.
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS,
                        hoist_field_inits=True,
                        template_field_types=template_field_types(
                            str(self.template_root), variant))
        apply_extensions_safe_block(schema, reserved=self._RESERVED_FIELD_NAMES)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("Spawner.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        count = 1

        if schema.pooling == "pooled":
            (scripts_dir / f"{schema.name}Pool.cs").write_text(
                env.get_template("ObjectPool.cs.j2").render(s=schema, variant=variant),
                encoding="utf-8",
            )
            count += 1

        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        count += 1
        return count
