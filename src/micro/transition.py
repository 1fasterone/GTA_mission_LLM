"""Scene-transition micro-generator — instant / fade / loading bar / async / door."""

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


TransitionVariant = Literal[
    "instant", "fade", "loading_bar", "async_additive", "trigger_door",
]
VARIANTS = list(TransitionVariant.__args__)   # type: ignore[attr-defined]

LoadMode = Literal["single", "additive"]
Trigger  = Literal["manual", "on_trigger", "on_timer"]


class TransitionSchema(CodeExtensibleSchema):
    name:         str      = "SceneTransition"
    variant:      str      = "fade"
    load_mode:    LoadMode = "single"
    trigger:      Trigger  = "manual"
    target_scene: str      = "NextScene"
    fade_duration: float   = 1.0
    timer_delay:  float    = 3.0     # for on_timer
    player_tag:   str      = "Player"
    description:  str       = "Scene transition"
    meta:         dict      = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "fade"

    @field_validator("load_mode", mode="before")
    @classmethod
    def _clamp_mode(cls, v: str) -> str:
        return clamp_enum(v, ("single", "additive"), "single")

    @field_validator("trigger", mode="before")
    @classmethod
    def _clamp_trigger(cls, v: str) -> str:
        return clamp_enum(v, ("manual", "on_trigger", "on_timer"), "manual")


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity scene-transition for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- Which scene loads and whether it replaces the current one or loads additively
- The transition style (instant, fade to black, a loading bar, async streaming)
- What triggers it (a manual call, the player entering a trigger, or a timer)
- Any fade duration

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
LOAD_MODE — single | additive
TRIGGER   — manual | on_trigger | on_timer

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "load_mode": "single",
  "trigger": "manual",
  "target_scene": "NextScene",
  "fade_duration": 1.0,
  "timer_delay": 3.0,
  "player_tag": "Player",
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS


class TransitionPipeline(UnityMicroPipeline):
    kind            = "transition"
    display_name    = "Unity Scene Transition"
    schema_cls      = TransitionSchema
    variants        = VARIANTS
    default_variant = "fade"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "transition"

    _RESERVED_FIELD_NAMES = {
        "targetscene", "fadeduration", "timerdelay", "playertag", "canvasgroup",
        "_fading", "_t",
    }
    _TEMPLATE_USINGS = {
        "UnityEngine", "UnityEngine.SceneManagement", "System.Collections",
        "UnityEngine.UI",
    }

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "SceneTransition")
        if schema.fade_duration < 0:
            schema.fade_duration = 1.0
        if schema.timer_delay <= 0:
            schema.timer_delay = 3.0
        # Variant → trigger coherence for the door variant.
        if variant == "trigger_door":
            schema.trigger = "on_trigger"
        if variant == "instant":
            schema.fade_duration = 0.0
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS)
        apply_extensions_safe_block(schema, reserved=self._RESERVED_FIELD_NAMES)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("SceneTransition.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
