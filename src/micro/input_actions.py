"""Input System micro-generator — .inputactions + InputReader ScriptableObject + typed wrapper."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline


InputVariant = Literal[
    "keyboard_mouse", "gamepad", "touch", "dual_stick", "racing_wheel", "vr_xr",
    # The cursor as the whole input surface. Every variant above binds a Move
    # of some kind because every one of them drives a body; a non-embodied game
    # (locomotion='none') has none to drive, and binding WASD to nothing is how
    # a project ships an input map that describes a game it is not.
    "pointer",
]
VARIANTS = list(InputVariant.__args__)   # type: ignore[attr-defined]


# ── Default action layouts per variant ────────────────────────────────────────
# Used by default_fix() when the LLM omits `maps` entirely.

_DEFAULT_LAYOUTS: dict[str, list[dict]] = {
    "keyboard_mouse": [
        {"name": "Player", "actions": [
            {"name": "Move",   "type": "Value",  "control_type": "Vector2"},
            {"name": "Look",   "type": "Value",  "control_type": "Vector2"},
            {"name": "Jump",   "type": "Button", "control_type": "Button"},
            {"name": "Fire",   "type": "Button", "control_type": "Button"},
            {"name": "Sprint", "type": "Button", "control_type": "Button"},
        ]},
    ],
    "gamepad": [
        {"name": "Player", "actions": [
            {"name": "Move",   "type": "Value",  "control_type": "Vector2"},
            {"name": "Look",   "type": "Value",  "control_type": "Vector2"},
            {"name": "Jump",   "type": "Button", "control_type": "Button"},
            {"name": "Fire",   "type": "Button", "control_type": "Button"},
        ]},
    ],
    "touch": [
        {"name": "Player", "actions": [
            {"name": "MoveStick", "type": "Value",  "control_type": "Vector2"},
            {"name": "AimStick",  "type": "Value",  "control_type": "Vector2"},
            {"name": "Tap",       "type": "Button", "control_type": "Button"},
        ]},
    ],
    "dual_stick": [
        {"name": "Player", "actions": [
            {"name": "Move", "type": "Value",  "control_type": "Vector2"},
            {"name": "Aim",  "type": "Value",  "control_type": "Vector2"},
            {"name": "Fire", "type": "Button", "control_type": "Button"},
        ]},
    ],
    "racing_wheel": [
        {"name": "Player", "actions": [
            {"name": "Steer",      "type": "Value",  "control_type": "Axis"},
            {"name": "Throttle",   "type": "Value",  "control_type": "Axis"},
            {"name": "Brake",      "type": "Value",  "control_type": "Axis"},
            {"name": "Handbrake",  "type": "Button", "control_type": "Button"},
            {"name": "ShiftUp",    "type": "Button", "control_type": "Button"},
            {"name": "ShiftDown",  "type": "Button", "control_type": "Button"},
        ]},
    ],
    "vr_xr": [
        {"name": "Player", "actions": [
            {"name": "LeftHandPos",  "type": "Value", "control_type": "Vector2"},
            {"name": "RightHandPos", "type": "Value", "control_type": "Vector2"},
            {"name": "Grip",         "type": "Button", "control_type": "Button"},
            {"name": "Trigger",      "type": "Button", "control_type": "Button"},
        ]},
    ],
    # The map named "Player" throughout this table is the map the InputReader
    # binds; the name is structural, so it stays even for a game with no player
    # object. What changes is every action in it: point, click, drag, and the
    # secondary/zoom/cancel a board needs, with no Move and no Jump.
    "pointer": [
        {"name": "Player", "actions": [
            {"name": "Point",     "type": "Value",  "control_type": "Vector2"},
            {"name": "Click",     "type": "Button", "control_type": "Button"},
            {"name": "Secondary", "type": "Button", "control_type": "Button"},
            {"name": "Drag",      "type": "Button", "control_type": "Button"},
            {"name": "Zoom",      "type": "Value",  "control_type": "Axis"},
            {"name": "Cancel",    "type": "Button", "control_type": "Button"},
        ]},
    ],
}


class InputAction(BaseModel):
    name:         str
    type:         Literal["Button", "Value", "PassThrough"] = "Button"
    control_type: Literal["Button", "Vector2", "Axis"]      = "Button"


class InputMap(BaseModel):
    name:    str
    actions: list[InputAction] = Field(default_factory=list)


class InputSchema(BaseModel):
    name:                str  = "GameInput"
    variant:             str  = "keyboard_mouse"
    maps:                list[InputMap] = Field(default_factory=list)
    use_input_reader_so: bool = True
    use_typed_wrapper:   bool = True
    description:         str  = "Input bindings"
    meta:                dict = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "keyboard_mouse"


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity Input System layout for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- What action maps are needed (e.g. Player, UI, Vehicle)
- For each map, which actions and whether they're Value or Button
- How the bindings should feel on {variant}

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}

ACTION TYPE — Button | Value | PassThrough
CONTROL TYPE — Button | Vector2 | Axis

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name (no spaces)",
  "variant": "{variant}",
  "maps": [
    {{
      "name": "Player",
      "actions": [
        {{ "name": "Move", "type": "Value",  "control_type": "Vector2" }},
        {{ "name": "Jump", "type": "Button", "control_type": "Button"  }}
      ]
    }}
  ],
  "use_input_reader_so": true,
  "use_typed_wrapper": true,
  "description": "One sentence."
}}
"""


class InputPipeline(UnityMicroPipeline):
    kind            = "input"
    display_name    = "Unity Input Bindings"
    schema_cls      = InputSchema
    variants        = VARIANTS
    default_variant = "keyboard_mouse"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "input"

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "GameInput")
        # If the LLM gave us no maps, inject the variant default
        if not schema.maps:
            schema.maps = [InputMap.model_validate(m) for m in _DEFAULT_LAYOUTS[variant]]
        # Ensure every map has at least one action
        for m in schema.maps:
            if not m.actions:
                m.actions = [InputAction(name="Action1", type="Button", control_type="Button")]
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        input_dir   = out_dir / "Input"
        scripts_dir = out_dir / "Scripts"
        input_dir.mkdir(parents=True, exist_ok=True)
        scripts_dir.mkdir(parents=True, exist_ok=True)

        # 1. The actual asset
        (input_dir / f"{schema.name}.inputactions").write_text(
            env.get_template("InputActions.inputactions.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        count = 1

        # 2. InputReader ScriptableObject
        if schema.use_input_reader_so:
            (scripts_dir / f"{schema.name}InputReader.cs").write_text(
                env.get_template("InputReader.cs.j2").render(s=schema, variant=variant),
                encoding="utf-8",
            )
            count += 1

        # 3. Typed wrapper
        if schema.use_typed_wrapper:
            (scripts_dir / f"{schema.name}Bindings.cs").write_text(
                env.get_template("Bindings.cs.j2").render(s=schema, variant=variant),
                encoding="utf-8",
            )
            count += 1

        # 4. README
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        count += 1
        return count
