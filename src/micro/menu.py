"""Menu micro-generator — main / pause / settings / game-over UI flow."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeExtensibleSchema, lint_extensible, clamp_enum,
    neutralize_unavailable_names, apply_extensions_safe_block,
    UNITY_EXTRA_INSTRUCTIONS,
)


MenuVariant = Literal[
    "main_menu", "pause_menu", "settings_menu",
    "game_over", "level_select", "confirm_dialog",
]
VARIANTS = list(MenuVariant.__args__)   # type: ignore[attr-defined]

Backend    = Literal["uGUI", "UIToolkit"]
Navigation = Literal["mouse", "keyboard", "gamepad"]


class MenuSchema(CodeExtensibleSchema):
    name:        str        = "Menu"
    variant:     str        = "main_menu"
    backend:     Backend    = "uGUI"
    navigation:  Navigation = "mouse"
    title:       str        = "Menu"
    items:       list[str]  = Field(default_factory=list)   # button labels
    first_scene: str        = "Game"                         # scene to load on Play
    pauses_game: bool       = False                          # toggles Time.timeScale
    description: str        = "Menu UI"
    meta:        dict       = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "main_menu"

    @field_validator("backend", mode="before")
    @classmethod
    def _clamp_backend(cls, v: str) -> str:
        return clamp_enum(v, ("uGUI", "UIToolkit"), "uGUI")

    @field_validator("navigation", mode="before")
    @classmethod
    def _clamp_nav(cls, v: str) -> str:
        return clamp_enum(v, ("mouse", "keyboard", "gamepad"), "mouse")


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity menu screen for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- What this menu is for and what buttons/options it shows
- uGUI Canvas or UI Toolkit (UXML/USS) as the backend
- Whether opening it pauses the game (pause/settings) or loads a scene (main/level select)
- How it is navigated (mouse, keyboard, gamepad)

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
BACKEND     — uGUI | UIToolkit
NAVIGATION  — mouse | keyboard | gamepad

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "backend": "uGUI",
  "navigation": "mouse",
  "title": "On-screen title",
  "items": ["Play", "Settings", "Quit"],
  "first_scene": "Game",
  "pauses_game": false,
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS


_DEFAULT_ITEMS: dict[str, list[str]] = {
    "main_menu":      ["Play", "Settings", "Quit"],
    "pause_menu":     ["Resume", "Settings", "Quit to Menu"],
    "settings_menu":  ["Back"],
    "game_over":      ["Retry", "Quit to Menu"],
    "level_select":   ["Level 1", "Level 2", "Level 3", "Back"],
    "confirm_dialog": ["Yes", "No"],
}


class MenuPipeline(UnityMicroPipeline):
    kind            = "menu"
    display_name    = "Unity Menu"
    schema_cls      = MenuSchema
    variants        = VARIANTS
    default_variant = "main_menu"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "menu"

    _RESERVED_FIELD_NAMES = {
        "buttons", "panel", "document", "_root", "title", "firstscene",
    }
    # Menu.cs.j2's `buttons`/`panel` (uGUI refs) and `document`/`_root` (UI
    # Toolkit refs) are declared in mutually exclusive branches of the same
    # backend if/else — never both. `_RESERVED_FIELD_NAMES` above only ever
    # blocked the LLM from DECLARING a new field with a colliding name; it
    # was never a claim that either pair actually exists. See default_fix's
    # neutralize_unavailable_names call.
    _UGUI_FIELDS      = {"buttons", "panel"}
    _UITOOLKIT_FIELDS = {"document", "_root"}
    _TEMPLATE_USINGS = {
        "UnityEngine", "UnityEngine.UI", "UnityEngine.SceneManagement",
        "UnityEngine.UIElements", "System",
    }

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "Menu")
        # Pause/settings overlays pause the game; main/level-select/game-over don't.
        schema.pauses_game = variant in ("pause_menu", "settings_menu")
        if not schema.items:
            schema.items = list(_DEFAULT_ITEMS.get(variant, ["Back"]))
        # Keep the button list reasonable.
        schema.items = [str(i).strip() for i in schema.items if str(i).strip()][:8]
        if not schema.items:
            schema.items = list(_DEFAULT_ITEMS.get(variant, ["Back"]))
        if not schema.title.strip():
            schema.title = variant.replace("_", " ").title()
        # hoist_field_inits: this generator's template renders awake_extra,
        # so a CS0236 initializer can be re-homed there instead of dropped.
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS,
                        hoist_field_inits=True)
        apply_extensions_safe_block(schema, reserved=self._RESERVED_FIELD_NAMES)
        # Strip whichever half of the backend split the LLM wasn't entitled
        # to (hit live on HUD's identical shape: a uGUI-backend script
        # referencing `_root` — CS0103, since Menu.cs.j2 only declares it
        # under backend == "UIToolkit").
        unavailable = (self._UGUI_FIELDS if schema.backend == "UIToolkit"
                       else self._UITOOLKIT_FIELDS)
        neutralize_unavailable_names(schema, unavailable)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("Menu.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        count = 1
        if schema.backend == "UIToolkit":
            ui_dir = out_dir / "UI"
            ui_dir.mkdir(parents=True, exist_ok=True)
            (ui_dir / f"{schema.name}.uxml").write_text(
                env.get_template("Menu.uxml.j2").render(s=schema, variant=variant),
                encoding="utf-8",
            )
            (ui_dir / f"{schema.name}.uss").write_text(
                env.get_template("Menu.uss.j2").render(s=schema, variant=variant),
                encoding="utf-8",
            )
            count += 2

        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        count += 1
        return count
