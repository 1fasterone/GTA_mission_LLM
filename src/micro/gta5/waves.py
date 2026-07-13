"""GTA5 waves micro — escalation director (wave sizing, intermissions, pressure)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from ...micro_pipeline import to_pascal
from ..gta5_base import GTA5MicroPipeline
from ..code_fields import GtaCodeExtensibleSchema, lint_gta_extensible, clamp_enum, GTA_EXTRA_INSTRUCTIONS


WavesVariant = Literal["linear", "exponential", "timed_pressure"]
VARIANTS = list(WavesVariant.__args__)   # type: ignore[attr-defined]


class GtaWavesSchema(GtaCodeExtensibleSchema):
    name:                  str   = "WaveDirector"
    variant:               str   = "linear"
    base_count:            int   = Field(default=3, ge=1, le=8)
    growth:                float = Field(default=1.5, ge=1.0, le=3.0)
    max_waves:             int   = Field(default=5, ge=1, le=20)
    intermission_seconds:  int   = Field(default=8, ge=2, le=60)
    pressure_seconds:      int   = Field(default=45, ge=10, le=300)   # timed_pressure
    description:           str   = "Wave escalation director"
    meta:                  dict  = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        return clamp_enum(v, tuple(VARIANTS), "linear")

    @field_validator("base_count", "max_waves", "intermission_seconds",
                     "pressure_seconds", mode="before")
    @classmethod
    def _clamp_ints(cls, v, info):
        bounds = {"base_count": (1, 8, 3), "max_waves": (1, 20, 5),
                  "intermission_seconds": (2, 60, 8), "pressure_seconds": (10, 300, 45)}
        lo, hi, default = bounds[info.field_name]
        try:
            return max(lo, min(hi, int(float(v))))
        except (TypeError, ValueError):
            return default

    @field_validator("growth", mode="before")
    @classmethod
    def _clamp_growth(cls, v):
        try:
            return max(1.0, min(3.0, float(v)))
        except (TypeError, ValueError):
            return 1.5


CONCEPT_PROMPT = """\
{theme_block}
Describe a GTA5 wave escalation director (tracks the wave number, sizes each
wave, times the breather between waves) for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- How many enemies the first wave has and how fast later waves grow
- How many waves before victory, and the intermission length
- For timed pressure: the per-wave time limit

Do NOT write code or JSON. This is GTA5 / SHVDN3, never Unity.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "base_count": 3,
  "growth": 1.5,
  "max_waves": 5,
  "intermission_seconds": 8,
  "pressure_seconds": 45,
  "description": "One sentence."
}}
""" + GTA_EXTRA_INSTRUCTIONS


class GtaWavesPipeline(GTA5MicroPipeline):
    kind            = "waves"
    display_name    = "GTA5 Waves"
    schema_cls      = GtaWavesSchema
    variants        = VARIANTS
    default_variant = "linear"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "waves"

    _RESERVED_FIELD_NAMES = {
        "_wave", "_intermissionuntil", "_waveendsat", "wavesizefor",
        "beginwave", "beginintermission", "wavesremaining",
    }
    _TEMPLATE_USINGS = {"System", "System.Collections.Generic",
                        "System.Windows.Forms", "GTA", "GTA.Math", "GTA.Native"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "WaveDirector")
        lint_gta_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                            template_usings=self._TEMPLATE_USINGS)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        (out_dir / f"{schema.name}.3.cs").write_text(
            env.get_template("Waves.3.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
