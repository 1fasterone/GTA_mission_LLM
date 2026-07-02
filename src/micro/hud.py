"""Canvas / UI Toolkit HUD micro-generator."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeExtensibleSchema, lint_extensible, clamp_enum, UNITY_EXTRA_INSTRUCTIONS,
)


HUDVariant = Literal[
    "health_bar", "ammo_counter", "minimap",
    "crosshair", "interaction_prompt", "score_panel",
    "pause_menu", "dialogue_box", "full_hud_bundle",
]
VARIANTS = list(HUDVariant.__args__)   # type: ignore[attr-defined]

Backend = Literal["uGUI", "UIToolkit"]
Anchor  = Literal["TL", "TR", "BL", "BR", "center"]
Binding = Literal["events", "find"]


class HUDSchema(CodeExtensibleSchema):
    name:        str     = "HUD"
    variant:     str     = "health_bar"
    backend:     Backend = "uGUI"
    anchor:      Anchor  = "TL"
    binding:     Binding = "events"
    animate:     bool    = False
    description: str     = "HUD element"
    meta:        dict    = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "health_bar"

    @field_validator("backend", mode="before")
    @classmethod
    def _clamp_backend(cls, v: str) -> str:
        return clamp_enum(v, ("uGUI", "UIToolkit"), "uGUI")

    @field_validator("anchor", mode="before")
    @classmethod
    def _clamp_anchor(cls, v: str) -> str:
        return clamp_enum(v, ("TL", "TR", "BL", "BR", "center"), "TL")

    @field_validator("binding", mode="before")
    @classmethod
    def _clamp_binding(cls, v: str) -> str:
        return clamp_enum(v, ("events", "find"), "events")


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity HUD element for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- What data the HUD shows and how it's positioned on screen
- uGUI Canvas or UI Toolkit (UXML/USS) as the backend
- Whether it listens to events or polls via FindObjectOfType
- Show/hide animation if any

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
BACKEND — uGUI | UIToolkit
ANCHOR  — TL | TR | BL | BR | center
BINDING — events | find

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "backend": "uGUI",
  "anchor": "TL",
  "binding": "events",
  "animate": false,
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS


class HUDPipeline(UnityMicroPipeline):
    kind            = "hud"
    display_name    = "Unity HUD"
    schema_cls      = HUDSchema
    variants        = VARIANTS
    default_variant = "health_bar"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "hud"

    _RESERVED_FIELD_NAMES = {
        "bar", "label", "body", "group", "mapimage", "mapcamera",
        "crosshairimage", "document", "_root",
    }
    _TEMPLATE_USINGS = {"UnityEngine", "UnityEngine.UI", "TMPro", "UnityEngine.UIElements"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "HUD")
        # Crosshair is always centered
        if variant == "crosshair":
            schema.anchor = "center"
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("HUD.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        count = 1

        if schema.backend == "UIToolkit":
            ui_dir = out_dir / "UI"
            ui_dir.mkdir(parents=True, exist_ok=True)
            (ui_dir / f"{schema.name}.uxml").write_text(
                env.get_template("HUD.uxml.j2").render(s=schema, variant=variant),
                encoding="utf-8",
            )
            (ui_dir / f"{schema.name}.uss").write_text(
                env.get_template("HUD.uss.j2").render(s=schema, variant=variant),
                encoding="utf-8",
            )
            count += 2

        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        count += 1
        return count
