"""Animator micro-generator — Animator parameter maps, blend trees, event relays."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import CodeField, CodeMethod, lint_code_fragment, lint_usings, lint_fields


AnimatorVariant = Literal[
    "humanoid",      # walk/run/jump/fall/attack blend tree
    "vehicle",       # rpm/speed → engine state
    "creature",      # custom creature rig (swim, fly, crawl)
    "object",        # simple open/close/activate loop
    "ui",            # UI panel slide/fade/bounce
]
VARIANTS = list(AnimatorVariant.__args__)  # type: ignore[attr-defined]


class AnimParam(BaseModel):
    name:       str
    param_type: str  = "float"    # float | bool | int | trigger
    default:    str  = "0"
    driven_by:  str  = ""         # C# expression that sets this param each frame


class AnimatorSchema(BaseModel):
    name:          str = "AnimationController"
    variant:       str = "humanoid"
    params:        list[AnimParam] = Field(default_factory=list)
    clips:         dict[str, str]  = Field(default_factory=dict)  # state_name → clip_name
    description:   str = "Animator parameter driver"

    extra_usings:  list[str]        = Field(default_factory=list)
    extra_fields:  list[CodeField]  = Field(default_factory=list)
    extra_methods: list[CodeMethod] = Field(default_factory=list)
    meta:          dict             = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "humanoid"


# Default param sets per variant — used when LLM leaves params empty.
# driven_by is left empty so the template declares a backing field + public
# Set<Param>() setter for each (e.g. SetGrounded, called by GameWiring). The LLM
# may still supply driven_by for params it computes from its own declared fields.
_DEFAULT_PARAMS: dict[str, list[dict]] = {
    "humanoid": [
        {"name": "Speed",   "param_type": "float",   "default": "0",     "driven_by": ""},
        {"name": "Grounded","param_type": "bool",    "default": "true",  "driven_by": ""},
        {"name": "Jump",    "param_type": "trigger", "default": "",      "driven_by": ""},
        {"name": "Attack",  "param_type": "trigger", "default": "",      "driven_by": ""},
    ],
    "vehicle": [
        {"name": "RPM",    "param_type": "float", "default": "0",     "driven_by": ""},
        {"name": "Speed",  "param_type": "float", "default": "0",     "driven_by": ""},
        {"name": "Braking","param_type": "bool",  "default": "false", "driven_by": ""},
    ],
    "creature": [
        {"name": "Speed",   "param_type": "float",   "default": "0",     "driven_by": ""},
        {"name": "Dive",    "param_type": "bool",    "default": "false", "driven_by": ""},
        {"name": "Surface", "param_type": "trigger", "default": "",      "driven_by": ""},
    ],
    "object": [
        {"name": "Open",    "param_type": "bool", "default": "false", "driven_by": ""},
        {"name": "Active",  "param_type": "bool", "default": "false", "driven_by": ""},
    ],
    "ui": [
        {"name": "Visible", "param_type": "bool",  "default": "false", "driven_by": ""},
        {"name": "Selected","param_type": "float", "default": "0",     "driven_by": ""},
    ],
}

_DEFAULT_CLIPS: dict[str, dict[str, str]] = {
    "humanoid": {"Idle": "idle_01", "Walk": "walk_02", "Run": "run_01",
                 "Jump": "jump_01", "Fall": "fall_01", "Attack": "attack_01", "Die": "die_01"},
    "vehicle":  {"Idle": "idle", "Drive": "drive"},
    "creature": {"Idle": "idle_water", "Swim": "swim_cycle", "Dive": "dive", "Surface": "surface"},
    "object":   {"Closed": "closed", "Opening": "open_anim", "Open": "open_idle"},
    "ui":       {"Hidden": "panel_out", "Visible": "panel_in"},
}


CONCEPT_PROMPT = """\
{theme_block}
Describe an Animator controller setup for this Unity game object:

"{user_prompt}"

Variant: {variant}

Cover in 1-2 paragraphs:
- What the object IS and how it moves/animates
- What Animator parameters drive its blend tree (Speed, Grounded, Attacking…)
- What animation clips map to each state (specific clip names if the prompt gives them)
- How parameter values are computed from runtime state each frame

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into an Animator schema. Output ONLY JSON.

The template renders an AnimationController MonoBehaviour that:
  - Caches a reference to the Animator component
  - Sets each parameter in LateUpdate from the described driven_by expression
  - Exposes helper methods (PlayClip, SetTrigger wrappers) for other scripts

Rules:
- param_type: float | bool | int | trigger
- driven_by: a C# expression (may reference private fields declared in extra_fields)
  e.g. "_rb.velocity.magnitude" or "_hp / maxHp"
- clips: map Animator state names to clip asset names (informational — used in README)
- extra_fields: declare private fields used in driven_by expressions
- extra_methods: helpers called by other components (e.g. public void PlayAttack())

VARIANT: {variant}

BRIEF:
{concept}

OUTPUT (JSON only):
{{
  "name": "PascalCaseName",
  "variant": "{variant}",
  "description": "One sentence.",
  "params": [
    {{"name": "Speed",   "param_type": "float",   "default": "0",     "driven_by": "_speed"}},
    {{"name": "Grounded","param_type": "bool",    "default": "true",  "driven_by": "_grounded"}},
    {{"name": "Jump",    "param_type": "trigger", "default": "",      "driven_by": ""}}
  ],
  "clips": {{"Idle": "idle_01", "Walk": "walk_02"}},
  "extra_usings": [],
  "extra_fields": [
    {{"name": "_speed",   "type": "float", "default": "0f"}},
    {{"name": "_grounded","type": "bool",  "default": "true"}}
  ],
  "extra_methods": []
}}

Derive params and clips from the prompt — not generic defaults.
Output ONLY JSON.
"""


class AnimatorPipeline(UnityMicroPipeline):
    kind            = "animator"
    display_name    = "Unity Animator (Parameter Driver)"
    schema_cls      = AnimatorSchema
    variants        = VARIANTS
    default_variant = "humanoid"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "animator"

    _RESERVED_FIELD_NAMES = {"_anim"}
    _TEMPLATE_USINGS = {"UnityEngine"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "AnimationController")
        if not schema.params:
            schema.params = [AnimParam(**d) for d in _DEFAULT_PARAMS.get(variant, _DEFAULT_PARAMS["humanoid"])]
        if not schema.clips:
            schema.clips = _DEFAULT_CLIPS.get(variant, {})
        from .code_fields import _KNOWN_NAMESPACES
        schema.extra_usings = lint_usings(
            schema.extra_usings, already_in_template=self._TEMPLATE_USINGS,
            allowed=_KNOWN_NAMESPACES,
        )
        schema.extra_fields = lint_fields(
            schema.extra_fields, reserved=self._RESERVED_FIELD_NAMES,
        )
        for i, m in enumerate(schema.extra_methods):
            schema.extra_methods[i].body = lint_code_fragment(m.body, what="method body")
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("Animator.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
