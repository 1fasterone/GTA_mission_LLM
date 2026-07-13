"""GTA5 saveload micro — ScriptSettings-backed .ini persistence (scores/settings)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from ...micro_pipeline import to_pascal
from ..gta5_base import GTA5MicroPipeline
from ..code_fields import GtaCodeExtensibleSchema, lint_gta_extensible, clamp_enum, GTA_EXTRA_INSTRUCTIONS


SaveLoadVariant = Literal["highscore_table", "settings_profile"]
VARIANTS = list(SaveLoadVariant.__args__)   # type: ignore[attr-defined]

_INI_NAME_RE = re.compile(r"[^a-z0-9_]+")


class GtaSaveLoadSchema(GtaCodeExtensibleSchema):
    name:        str  = "ScoreVault"
    variant:     str  = "highscore_table"
    ini_name:    str  = "minigame_vault"
    section:     str  = "Scores"
    max_entries: int  = Field(default=5, ge=1, le=10)
    description: str  = ".ini persistence vault"
    meta:        dict = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        return clamp_enum(v, tuple(VARIANTS), "highscore_table")

    @field_validator("ini_name", mode="before")
    @classmethod
    def _sanitize_ini_name(cls, v) -> str:
        cleaned = _INI_NAME_RE.sub("_", str(v).strip().lower()).strip("_")
        return cleaned or "minigame_vault"

    @field_validator("section", mode="before")
    @classmethod
    def _sanitize_section(cls, v) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9]+", "", str(v).strip())
        return cleaned or "Scores"

    @field_validator("max_entries", mode="before")
    @classmethod
    def _clamp_entries(cls, v):
        try:
            return max(1, min(10, int(float(v))))
        except (TypeError, ValueError):
            return 5


CONCEPT_PROMPT = """\
{theme_block}
Describe a GTA5 persistence vault (SHVDN ScriptSettings reading/writing an
.ini next to the script) for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- What gets persisted (a ranked high-score table, or named settings)
- The ini file name and how many entries to keep
- When values load (script start) and save (on change)

Do NOT write code or JSON. This is GTA5 / SHVDN3, never Unity.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
ini_name — lowercase snake_case file stem (no extension)

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "ini_name": "minigame_vault",
  "section": "Scores",
  "max_entries": 5,
  "description": "One sentence."
}}
""" + GTA_EXTRA_INSTRUCTIONS


class GtaSaveLoadPipeline(GTA5MicroPipeline):
    kind            = "saveload"
    display_name    = "GTA5 SaveLoad"
    schema_cls      = GtaSaveLoadSchema
    variants        = VARIANTS
    default_variant = "highscore_table"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "saveload"

    _RESERVED_FIELD_NAMES = {
        "_vault", "_loaded", "loadvault", "savevault", "recordscore", "bestscore",
    }
    _TEMPLATE_USINGS = {"System", "System.Collections.Generic",
                        "System.Windows.Forms", "GTA", "GTA.Math", "GTA.Native"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "ScoreVault")
        lint_gta_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                            template_usings=self._TEMPLATE_USINGS)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        (out_dir / f"{schema.name}.3.cs").write_text(
            env.get_template("SaveLoad.3.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
