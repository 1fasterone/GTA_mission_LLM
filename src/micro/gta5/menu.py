"""GTA5 menu micro — native-draw overlay panel (settings / pause / leaderboard)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from ...micro_pipeline import to_pascal
from ..gta5_base import GTA5MicroPipeline
from ..code_fields import GtaCodeExtensibleSchema, lint_gta_extensible, clamp_enum, GTA_EXTRA_INSTRUCTIONS


MenuVariant = Literal["settings_panel", "pause_menu", "leaderboard_view"]
VARIANTS = list(MenuVariant.__args__)   # type: ignore[attr-defined]

_HOTKEY_RE = re.compile(r"^F([1-9]|1[0-2])$")


class GtaMenuSchema(GtaCodeExtensibleSchema):
    name:        str  = "OverlayMenu"
    variant:     str  = "settings_panel"
    title:       str  = "MENU"
    hotkey:      str  = "F6"
    rows:        list[str] = Field(default_factory=lambda: ["Option A", "Option B"],
                                   max_length=8)
    description: str  = "Native-draw overlay menu"
    meta:        dict = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        return clamp_enum(v, tuple(VARIANTS), "settings_panel")

    @field_validator("hotkey", mode="before")
    @classmethod
    def _clamp_hotkey(cls, v: str) -> str:
        key = str(v).strip().upper()
        return key if _HOTKEY_RE.match(key) else "F6"

    @field_validator("rows", mode="before")
    @classmethod
    def _truncate_rows(cls, v):
        if isinstance(v, list) and len(v) > 8:
            return v[:8]
        return v


CONCEPT_PROMPT = """\
{theme_block}
Describe a GTA5 on-screen overlay menu (drawn with native DRAW_RECT / text
commands, toggled by a function key) for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- The menu's title and what each row shows
- Which function key opens/closes it
- Whether rows are informational or selectable

Do NOT write code or JSON. This is GTA5 / SHVDN3, never Unity.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
hotkey — a function key F1..F12 (string, e.g. "F6")
rows — up to 8 short row labels

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "title": "MENU TITLE",
  "hotkey": "F6",
  "rows": ["Row label", "Another row"],
  "description": "One sentence."
}}
""" + GTA_EXTRA_INSTRUCTIONS


class GtaMenuPipeline(GTA5MicroPipeline):
    kind            = "menu"
    display_name    = "GTA5 Menu"
    schema_cls      = GtaMenuSchema
    variants        = VARIANTS
    default_variant = "settings_panel"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "menu"

    _RESERVED_FIELD_NAMES = {
        "_open", "_selection", "_rows", "drawpanelrect", "drawpaneltext",
    }
    _TEMPLATE_USINGS = {"System", "System.Collections.Generic",
                        "System.Windows.Forms", "GTA", "GTA.Math", "GTA.Native"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "OverlayMenu")
        schema.title = (schema.title or "MENU").strip()[:40] or "MENU"
        schema.rows = [r.strip()[:48] for r in schema.rows if str(r).strip()][:8]
        if not schema.rows:
            schema.rows = ["Option A", "Option B"]
        lint_gta_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                            template_usings=self._TEMPLATE_USINGS)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        (out_dir / f"{schema.name}.3.cs").write_text(
            env.get_template("Menu.3.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
