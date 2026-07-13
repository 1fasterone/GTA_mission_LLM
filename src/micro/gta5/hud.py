"""GTA5 hud micro — native-draw round HUD (score ticker / wave banner / timer bar)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from ...micro_pipeline import to_pascal
from ..gta5_base import GTA5MicroPipeline
from ..code_fields import GtaCodeExtensibleSchema, lint_gta_extensible, clamp_enum, GTA_EXTRA_INSTRUCTIONS


HudVariant = Literal["score_ticker", "wave_banner", "timer_bar"]
VARIANTS = list(HudVariant.__args__)   # type: ignore[attr-defined]

HudCorner = Literal["top_right", "top_left", "bottom_center"]


class GtaHudSchema(GtaCodeExtensibleSchema):
    name:        str   = "RoundHud"
    variant:     str   = "score_ticker"
    label:       str   = "SCORE"
    corner:      str   = "top_right"
    show_timer:  bool  = True
    show_best:   bool  = True
    description: str   = "Round HUD"
    meta:        dict  = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        return clamp_enum(v, tuple(VARIANTS), "score_ticker")

    @field_validator("corner", mode="before")
    @classmethod
    def _clamp_corner(cls, v: str) -> str:
        return clamp_enum(v, ("top_right", "top_left", "bottom_center"), "top_right")


CONCEPT_PROMPT = """\
{theme_block}
Describe a GTA5 round HUD (drawn every tick with native DRAW_RECT / text
commands) for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- What number/label the HUD tracks (score, wave, remaining time)
- Where on screen it sits and whether it shows a session best
- Any timer display

Do NOT write code or JSON. This is GTA5 / SHVDN3, never Unity.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
corner — top_right | top_left | bottom_center

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "label": "SCORE",
  "corner": "top_right",
  "show_timer": true,
  "show_best": true,
  "description": "One sentence."
}}
""" + GTA_EXTRA_INSTRUCTIONS


class GtaHudPipeline(GTA5MicroPipeline):
    kind            = "hud"
    display_name    = "GTA5 HUD"
    schema_cls      = GtaHudSchema
    variants        = VARIANTS
    default_variant = "score_ticker"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "hud"

    _RESERVED_FIELD_NAMES = {
        "_hudvalue", "_hudbest", "_hudstart", "_hudbanner", "_hudbanneruntil",
        "drawpanelrect", "drawpaneltext",
    }
    _TEMPLATE_USINGS = {"System", "System.Collections.Generic",
                        "System.Windows.Forms", "GTA", "GTA.Math", "GTA.Native"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "RoundHud")
        schema.label = (schema.label or "SCORE").strip()[:24] or "SCORE"
        lint_gta_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                            template_usings=self._TEMPLATE_USINGS)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        (out_dir / f"{schema.name}.3.cs").write_text(
            env.get_template("Hud.3.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
