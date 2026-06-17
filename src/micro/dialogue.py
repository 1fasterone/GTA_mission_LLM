"""Dialogue micro-generator — linear / branching / typewriter / cutscene / barks."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import MicroPipeline, to_pascal
from .code_fields import (
    CodeExtensibleSchema, lint_extensible, clamp_enum, UNITY_EXTRA_INSTRUCTIONS,
)


DialogueVariant = Literal[
    "linear", "branching", "typewriter",
    "cutscene", "barks", "localized",
]
VARIANTS = list(DialogueVariant.__args__)   # type: ignore[attr-defined]

Advance    = Literal["submit", "any_key", "auto"]
TextRender = Literal["instant", "typewriter"]


class DialogueChoice(BaseModel):
    text: str = "..."
    next: str = ""          # node id to jump to ("" = end the conversation)

    @field_validator("text", "next", mode="before")
    @classmethod
    def _str(cls, v):
        return "" if v is None else str(v)


class DialogueNode(BaseModel):
    id:      str
    speaker: str = ""
    text:    str = ""
    choices: list[DialogueChoice] = Field(default_factory=list)

    @field_validator("id", "speaker", "text", mode="before")
    @classmethod
    def _str(cls, v):
        return "" if v is None else str(v)


class DialogueSchema(CodeExtensibleSchema):
    name:               str                 = "Dialogue"
    variant:            str                 = "linear"
    advance:            Advance             = "submit"
    text_render:        TextRender          = "instant"
    text_speed:         float               = 30.0      # chars/sec (typewriter)
    auto_advance_delay: float               = 2.0       # seconds (cutscene)
    start_node:         str                 = ""
    nodes:              list[DialogueNode]  = Field(default_factory=list)
    description:        str                 = "Dialogue system"
    meta:               dict                = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "linear"

    @field_validator("advance", mode="before")
    @classmethod
    def _clamp_advance(cls, v: str) -> str:
        return clamp_enum(v, ("submit", "any_key", "auto"), "submit")

    @field_validator("text_render", mode="before")
    @classmethod
    def _clamp_render(cls, v: str) -> str:
        return clamp_enum(v, ("instant", "typewriter"), "instant")


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity dialogue system for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- Who speaks and a few sample lines of what they say
- Whether it is a straight sequence, a branching tree with player choices, or random barks
- How lines advance (a button, any key, or auto-timed)
- Any reveal effect (instant or typewriter)

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
ADVANCE      — submit | any_key | auto
TEXT_RENDER  — instant | typewriter

`nodes` is the actual conversation. Each node: an "id", a "speaker", the "text"
they say, and (for branching) a list of "choices" {{"text": "...", "next": "<node id>"}}.
Write real, in-character lines for THIS prompt — this is how the prompt reaches the file.

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name (no spaces)",
  "variant": "{variant}",
  "advance": "submit",
  "text_render": "instant",
  "text_speed": 30.0,
  "auto_advance_delay": 2.0,
  "start_node": "start",
  "nodes": [
    {{"id": "start", "speaker": "Guard", "text": "Halt! State your business.",
      "choices": [{{"text": "I'm a merchant.", "next": "merchant"}}, {{"text": "None of yours.", "next": "hostile"}}]}},
    {{"id": "merchant", "speaker": "Guard", "text": "Trade's that way.", "choices": []}},
    {{"id": "hostile",  "speaker": "Guard", "text": "Then move along.", "choices": []}}
  ],
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS


class DialoguePipeline(MicroPipeline):
    kind            = "dialogue"
    display_name    = "Unity Dialogue"
    schema_cls      = DialogueSchema
    variants        = VARIANTS
    default_variant = "linear"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "dialogue"

    _RESERVED_FIELD_NAMES = {
        "nodes", "current", "index", "typing", "startnode",
        "textspeed", "autoadvancedelay", "_nodes", "_index", "_current", "_typing",
    }
    _TEMPLATE_USINGS = {
        "UnityEngine", "System", "System.Collections", "System.Collections.Generic",
    }

    def _default_nodes(self, variant: str) -> list[DialogueNode]:
        if variant == "branching":
            return [
                DialogueNode(id="start", speaker="NPC", text="Can you help me with something?",
                             choices=[DialogueChoice(text="Of course.", next="accept"),
                                      DialogueChoice(text="Not now.", next="decline")]),
                DialogueNode(id="accept",  speaker="NPC", text="Thank you, traveller!"),
                DialogueNode(id="decline", speaker="NPC", text="Perhaps another time."),
            ]
        return [
            DialogueNode(id="l1", speaker="NPC", text="Hello there."),
            DialogueNode(id="l2", speaker="NPC", text="Welcome to our humble town."),
            DialogueNode(id="l3", speaker="NPC", text="Safe travels, friend."),
        ]

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "Dialogue")
        if schema.text_speed <= 0:          schema.text_speed = 30.0
        if schema.auto_advance_delay <= 0:  schema.auto_advance_delay = 2.0

        # Variant → field coherence.
        if variant == "typewriter":
            schema.text_render = "typewriter"
        if variant == "cutscene":
            schema.advance = "auto"

        # Never emit an empty conversation.
        if not schema.nodes:
            schema.nodes = self._default_nodes(variant)

        # start_node must point at a real node.
        ids = {n.id for n in schema.nodes if n.id}
        if schema.start_node not in ids:
            schema.start_node = schema.nodes[0].id

        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (scripts_dir / f"{schema.name}Node.cs").write_text(
            env.get_template("Node.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("Dialogue.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 3
