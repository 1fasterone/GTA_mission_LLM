"""Objectives micro-generator — mission objective tracking with completion events.

Deterministic consumer for schema.missions (MissionSpec/MissionObjective,
unity_schema.py:734-764) — previously orphaned schema surface with no runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeExtensibleSchema, lint_extensible, clamp_enum, UNITY_EXTRA_INSTRUCTIONS,
)


ObjectivesVariant = Literal["tracker"]
VARIANTS = list(ObjectivesVariant.__args__)   # type: ignore[attr-defined]

ObjectiveType = Literal["reach", "collect", "eliminate", "escort", "survive", "interact"]
_VALID_OBJ_TYPE = frozenset(ObjectiveType.__args__)   # type: ignore[attr-defined]


class ObjectiveEntry(BaseModel):
    id:          str
    description: str = ""
    type:        str = "reach"
    target_ref:  str = ""
    count:       int = 1
    optional:    bool = False

    @field_validator("type", mode="before")
    @classmethod
    def _clamp_type(cls, v) -> str:
        s = str(v).strip().lower()
        return s if s in _VALID_OBJ_TYPE else "reach"

    @field_validator("count", mode="before")
    @classmethod
    def _clamp_count(cls, v) -> int:
        try:
            return max(1, min(9999, int(v)))
        except (TypeError, ValueError):
            return 1


class ObjectivesSchema(CodeExtensibleSchema):
    name:        str                    = "MissionObjectives"
    variant:     str                    = "tracker"
    title:       str                    = "Mission"
    objectives:  list[ObjectiveEntry]   = Field(default_factory=list)
    rewards:     dict                   = Field(default_factory=dict)
    description: str                    = "Mission objective tracker"
    meta:        dict                   = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        return clamp_enum(v, tuple(VARIANTS), "tracker")


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity mission-objective tracker for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- The mission's objectives (reach a place, collect items, eliminate enemies, escort,
  survive, or interact with something) and how progress is reported
- Which objectives (if any) are optional
- What reward completing the mission grants

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
OBJECTIVE TYPE — reach | collect | eliminate | escort | survive | interact

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "title": "Mission title",
  "objectives": [
    {{"id": "obj1", "description": "One sentence.", "type": "reach",
      "target_ref": "", "count": 1, "optional": false}}
  ],
  "rewards": {{}},
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS


class ObjectivesPipeline(UnityMicroPipeline):
    kind            = "objectives"
    display_name    = "Unity Mission Objectives"
    schema_cls      = ObjectivesSchema
    variants        = VARIANTS
    default_variant = "tracker"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "objectives"

    _RESERVED_FIELD_NAMES = {
        "objectives", "rewards", "title", "_progress", "_complete",
    }
    _TEMPLATE_USINGS = {"UnityEngine", "System", "System.Collections.Generic"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "MissionObjectives")
        if not schema.title:
            schema.title = "Mission"
        # Every objective needs a non-empty id — synthesize one deterministically
        # from position if the LLM (or a schema-only caller) left it blank.
        seen: set[str] = set()
        for i, obj in enumerate(schema.objectives):
            if not obj.id:
                obj.id = f"objective_{i + 1}"
            base = obj.id
            n = 2
            while obj.id in seen:
                obj.id = f"{base}_{n}"
                n += 1
            seen.add(obj.id)
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("Objectives.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
