"""Waypoint micro-generator — a screen-edge marker pointing at the active
mission objective.

Closes a concrete gap: objective progress previously only ever surfaced as
flat TEXT (the HUD's objective readout, `micro/rules.py`'s briefing/hint bar)
— nothing anywhere gave the player a SPATIAL sense of where to go. This micro
is deliberately the smallest thing that closes that gap: one runtime-built
UI marker that clamps to the screen edge in the direction of the active
objective's target, using the exact same singleton-naming convention
(`"<entity>_0"`) SEAM 8b already relies on to resolve an authored entity name
to its spawned GameObject — no new entity registry, no new resolver.

Wired by `unity_compose.subsystems_for` alongside the "objectives" subsystem,
and driven at runtime by the SAME `RefreshObjectiveHud()` method that already
walks every composed Objectives tracker for the HUD text readout — the two
readouts always name the same objective because they read the same pick.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import CodeExtensibleSchema, lint_extensible, apply_extensions_safe_block


WaypointVariant = Literal["edge_arrow"]
VARIANTS = list(WaypointVariant.__args__)  # type: ignore[attr-defined]


CONCEPT_PROMPT = """\
{theme_block}
Describe a screen-edge waypoint marker for this idea:

"{user_prompt}"

Variant: {variant}

It is a small on-screen indicator that clamps to the edge of the screen and
points toward the current objective when that objective is off-screen or far
away, and hides once the player is close enough that a marker adds nothing.
No code, no JSON — plain English, a few sentences.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into a waypoint schema. Output ONLY JSON.

There is only one variant: edge_arrow — a marker that clamps to the screen
edge and points toward an externally-set target. This micro has no per-brief
content to author beyond the optional extensions sandbox; leave `variant` as
"edge_arrow" and only fill `extensions_*` if the brief calls for a genuinely
different look (e.g. a pulsing marker) that a fixed field can't express.

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCaseName",
  "variant": "edge_arrow",
  "description": "One sentence."
}}
"""


class WaypointSchema(CodeExtensibleSchema):
    name:        str  = "Waypoint"
    variant:     str  = "edge_arrow"
    description: str  = "Screen-edge marker pointing toward the active mission objective"
    meta:        dict = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "edge_arrow"


class WaypointPipeline(UnityMicroPipeline):
    kind            = "waypoint"
    display_name    = "Unity Waypoint Marker"
    schema_cls      = WaypointSchema
    variants        = VARIANTS
    default_variant = "edge_arrow"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "waypoint"

    _RESERVED_FIELD_NAMES = {
        "_canvas", "_canvasrect", "_markerrect", "_marker", "_target", "_targetname",
    }
    _TEMPLATE_USINGS = {"UnityEngine", "UnityEngine.UI"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "Waypoint")
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS)
        apply_extensions_safe_block(schema, reserved=self._RESERVED_FIELD_NAMES)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("Waypoint.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
