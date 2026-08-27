"""Behavior-tree micro-generator — a minimal, generic BT interpreter.

Deterministic consumer for schema.behavior_trees (AIBehaviorTree/BehaviorTree/
BTNode, unity_schema.py:563-581, 769-772) — previously orphaned schema surface
with zero runtime (src/micro/ai.py's "behaviour_tree_lite" variant is actually
just the flat FSM reused, not a tree).

Scope is deliberately bounded: the tree STRUCTURE and Sequence/Selector
evaluation are real and deterministic. Leaf nodes (Action/Condition) are
extension hooks, not autonomous AI — there is no generic way to know what an
arbitrary `BTNode.action` string should actually do in a specific game, so
Action nodes fire a SendMessage and Condition nodes look up a
runtime-registered predicate. See the rendered README for the exact contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeExtensibleSchema, lint_extensible, clamp_enum, apply_extensions_safe_block,
    UNITY_EXTRA_INSTRUCTIONS,
)


BehaviorTreeVariant = Literal["tree"]
VARIANTS = list(BehaviorTreeVariant.__args__)   # type: ignore[attr-defined]

BTNodeType = Literal["sequence", "selector", "action", "condition", "decorator"]
_VALID_BT_NODE = frozenset(BTNodeType.__args__)   # type: ignore[attr-defined]


class BTNodeEntry(BaseModel):
    id:       str
    type:     str       = "action"
    children: list[str] = Field(default_factory=list)
    action:   str        = ""

    @field_validator("type", mode="before")
    @classmethod
    def _clamp_type(cls, v) -> str:
        s = str(v).strip().lower()
        return s if s in _VALID_BT_NODE else "action"


class BehaviorTreeSchema(CodeExtensibleSchema):
    name:          str               = "BehaviorTree"
    variant:       str               = "tree"
    root:          str               = ""
    nodes:         list[BTNodeEntry] = Field(default_factory=list)
    tick_interval: float             = 0.2
    description:   str               = "Behavior tree interpreter"
    meta:          dict              = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        return clamp_enum(v, tuple(VARIANTS), "tree")

    @field_validator("tick_interval", mode="before")
    @classmethod
    def _clamp_tick(cls, v) -> float:
        try:
            return max(0.02, float(v))
        except (TypeError, ValueError):
            return 0.2


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity AI behavior tree for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- The high-level decision structure (sequence = all steps must succeed in order;
  selector = try each option until one succeeds)
- The concrete actions/conditions at the leaves (e.g. "chase player", "in attack range")

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
NODE TYPE — sequence | selector | action | condition | decorator
Each node's "children" is a list of other node ids (sequence/selector/decorator only).
"action" on an action/condition node names a hook the running game must implement.

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "root": "root_id",
  "nodes": [
    {{"id": "root_id", "type": "selector", "children": ["chase", "idle"], "action": ""}},
    {{"id": "chase", "type": "action", "children": [], "action": "ChasePlayer"}},
    {{"id": "idle", "type": "action", "children": [], "action": "Idle"}}
  ],
  "tick_interval": 0.2,
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS


class BehaviorTreePipeline(UnityMicroPipeline):
    kind            = "behavior_tree"
    display_name    = "Unity Behavior Tree"
    schema_cls      = BehaviorTreeSchema
    variants        = VARIANTS
    default_variant = "tree"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "behavior_tree"

    _RESERVED_FIELD_NAMES = {
        "root", "nodes", "tickinterval", "_defs", "_byid", "_conditions", "_timer",
    }
    _TEMPLATE_USINGS = {"UnityEngine", "System", "System.Collections.Generic"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "BehaviorTree")
        # Every node needs a non-empty, unique id — synthesize deterministically
        # from position if left blank (mirrors ObjectivesPipeline's approach).
        seen: set[str] = set()
        for i, node in enumerate(schema.nodes):
            if not node.id:
                node.id = f"node_{i + 1}"
            base = node.id
            n = 2
            while node.id in seen:
                node.id = f"{base}_{n}"
                n += 1
            seen.add(node.id)
        valid_ids = {n.id for n in schema.nodes}
        # Drop child references to nodes that don't exist (defensive — an LLM
        # or hand-authored schema can reference a typo'd id) rather than let
        # the C# tree walker hit a missing-key failure at runtime.
        for node in schema.nodes:
            node.children = [c for c in node.children if c in valid_ids]
        if not schema.root and schema.nodes:
            schema.root = schema.nodes[0].id
        elif schema.root and schema.root not in valid_ids and schema.nodes:
            schema.root = schema.nodes[0].id
        # hoist_field_inits: this generator's template renders awake_extra,
        # so a CS0236 initializer can be re-homed there instead of dropped.
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS,
                        hoist_field_inits=True)
        apply_extensions_safe_block(schema, reserved=self._RESERVED_FIELD_NAMES)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("BehaviorTree.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
