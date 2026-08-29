"""Platform micro-generator — moving, oscillating, and falling platforms.

Static "step-up" platforms need no runtime script at all (they're just
correctly-positioned static colliders, already handled by entity placement) —
this micro covers the genuinely new runtime behavior: platforms that move.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeExtensibleSchema, lint_extensible, clamp_enum, apply_extensions_safe_block,
    UNITY_EXTRA_INSTRUCTIONS,
)


PlatformVariant = Literal[
    "waypoint_pingpong", "waypoint_loop", "oscillate", "falling",
]
VARIANTS = list(PlatformVariant.__args__)   # type: ignore[attr-defined]


class PlatformSchema(CodeExtensibleSchema):
    name:                 str   = "MovingPlatform"
    variant:              str   = "waypoint_pingpong"
    speed:                float = 2.0
    wait_time:            float = 0.0    # pause at each waypoint/extreme, seconds
    waypoint_parent_name: str   = "Waypoints"   # EntitySpawner-style container of child transforms
    oscillate_axis:       str   = "y"           # x | y | z
    oscillate_distance:   float = 3.0
    fall_delay:           float = 0.5    # falling: seconds standing on it before it drops
    respawn_delay:        float = 3.0    # falling: seconds before it resets
    carries_rider:        bool  = True   # parent whatever stands on it while riding
    target_tag:           str   = "Player"
    description:          str   = "Platform"
    meta:                 dict  = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "waypoint_pingpong"

    @field_validator("oscillate_axis", mode="before")
    @classmethod
    def _clamp_axis(cls, v: str) -> str:
        return clamp_enum(v, ("x", "y", "z"), "y")


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity moving platform for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- How it moves: back and forth between named waypoints, looping through them,
  oscillating along one axis, or falling away after the player stands on it
- Speed and any pause at the ends
- Whether the player/rider should be carried along (parented) while standing on it

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
  waypoint_pingpong — travels to each child of waypoint_parent_name in order, then reverses
  waypoint_loop     — travels to each child of waypoint_parent_name in order, then wraps to the first
  oscillate         — no waypoints needed; slides back and forth along oscillate_axis by oscillate_distance
  falling           — static until target_tag stands on it, then drops after fall_delay and resets after respawn_delay

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "speed": 2.0,
  "wait_time": 0.0,
  "waypoint_parent_name": "Waypoints",
  "oscillate_axis": "y",
  "oscillate_distance": 3.0,
  "fall_delay": 0.5,
  "respawn_delay": 3.0,
  "carries_rider": true,
  "target_tag": "Player",
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS


class PlatformPipeline(UnityMicroPipeline):
    kind            = "platform"
    display_name    = "Unity Moving Platform"
    schema_cls      = PlatformSchema
    variants        = VARIANTS
    default_variant = "waypoint_pingpong"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "platform"

    _RESERVED_FIELD_NAMES = {
        "speed", "waittime", "waypointparentname", "oscillateaxis",
        "oscillatedistance", "falldelay", "respawndelay", "carriesrider",
        "targettag", "_waypoints", "_wpindex", "_waiting", "_waitTimer",
        "_origin", "_dir", "_standTimer", "_fallen", "_startPos", "_rider",
    }
    _TEMPLATE_USINGS = {"UnityEngine", "System.Collections.Generic"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "MovingPlatform")
        if schema.speed <= 0:
            schema.speed = 2.0
        schema.wait_time          = max(0.0, min(30.0, schema.wait_time))
        schema.oscillate_distance = max(0.1, min(100.0, schema.oscillate_distance))
        schema.fall_delay         = max(0.0, min(10.0, schema.fall_delay))
        schema.respawn_delay      = max(0.1, min(60.0, schema.respawn_delay))
        if not schema.waypoint_parent_name.strip():
            schema.waypoint_parent_name = "Waypoints"
        if not schema.target_tag.strip():
            schema.target_tag = "Player"
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS)
        apply_extensions_safe_block(schema, reserved=self._RESERVED_FIELD_NAMES)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("MovingPlatform.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
