"""Inventory micro-generator — slot/grid/weight/equipment/hotbar containers."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import console, to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeExtensibleSchema, lint_extensible, clamp_enum, UNITY_EXTRA_INSTRUCTIONS,
)


InventoryVariant = Literal[
    "slot_based", "grid", "weight_based",
    "equipment", "hotbar", "unlimited",
]
VARIANTS = list(InventoryVariant.__args__)   # type: ignore[attr-defined]

Stacking = Literal["stackable", "unique"]
SortMode = Literal["none", "by_name", "by_weight"]

# What USING an item does — closed set so Inventory.cs.j2's UseItem() dispatches
# with a plain switch, never free-form code (see ItemDefinition.cs.j2's mirrored
# C# enum). "none" is the only variant-agnostic default: equipment/quest/junk
# items that are carried, equipped, or shown but never consumed.
ItemEffect = Literal[
    "none", "heal", "restore_shield", "stat_boost", "unlock", "quest_flag",
]
_ITEM_EFFECTS = list(ItemEffect.__args__)   # type: ignore[attr-defined]
# unlock/quest_flag are meaningless without a target; heal/restore_shield/
# stat_boost are meaningless with amount<=0 — default_fix downgrades both
# to "none" rather than ship an effect UseItem() can fire but that changes
# nothing (see default_fix below).
_TARGETED_EFFECTS = frozenset({"unlock", "quest_flag"})
_AMOUNT_EFFECTS = frozenset({"heal", "restore_shield", "stat_boost"})


class InventoryItemSpec(BaseModel):
    """One item type: a name plus what using it does, if anything. This is
    the Step-3 landing spot for brief intent like "heals 25 HP" or "unlocks
    the cellar" — a bare item name has no field to carry that into, so it
    used to evaporate into just a display string."""
    name:      str        = "Item"
    effect:    ItemEffect  = "none"
    amount:    float       = 0.0   # heal/restore_shield HP, or stat_boost magnitude — unused by unlock/quest_flag
    target_id: str         = ""    # stat_boost's stat name, unlock's lock id, or quest_flag's flag name — unused by heal/restore_shield

    @field_validator("effect", mode="before")
    @classmethod
    def _clamp_effect(cls, v: str) -> str:
        return clamp_enum(v, tuple(_ITEM_EFFECTS), "none")


class InventorySchema(CodeExtensibleSchema):
    name:            str        = "Inventory"
    variant:         str        = "slot_based"
    stacking:        Stacking   = "stackable"
    sort_mode:       SortMode   = "none"
    slot_count:      int        = 20
    grid_width:      int        = 5
    grid_height:     int        = 4
    max_weight:      float      = 50.0
    equipment_slots: list[str]  = Field(default_factory=list)
    items:           list[InventoryItemSpec] = Field(default_factory=list)
    allow_drop:      bool       = True
    description:     str        = "Inventory system"
    meta:            dict       = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "slot_based"

    @field_validator("stacking", mode="before")
    @classmethod
    def _clamp_stacking(cls, v: str) -> str:
        return clamp_enum(v, ("stackable", "unique"), "stackable")

    @field_validator("sort_mode", mode="before")
    @classmethod
    def _clamp_sort(cls, v: str) -> str:
        return clamp_enum(v, ("none", "by_name", "by_weight"), "none")


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity inventory system for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- What items the player carries and how they are identified
- The container shape (fixed slots, a 2D grid, weight capacity, equipment slots, a hotbar)
- Whether identical items stack
- Add / remove / query behaviour and any UI hook
- What USING an item actually does, if anything (heals, restores shield,
  boosts a stat, unlocks something, sets a quest flag) — name the amount or
  target where the brief gives one

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
STACKING   — stackable | unique
SORT_MODE  — none | by_name | by_weight

Each entry in "items" is one item type. EFFECT MUST BE ONE OF:
  none            inert — equipment, quest items, junk; nothing happens on use
  heal            amount = HP restored
  restore_shield  amount = shield HP restored
  stat_boost      amount = magnitude; target_id = which stat (e.g. "speed")
  unlock          target_id = the lock/door id this item opens
  quest_flag      target_id = the flag name this item sets
unlock and quest_flag REQUIRE a non-empty target_id or the effect is dropped
to "none" — never invent a target_id; leave it "" if the brief doesn't name one.

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name (no spaces)",
  "variant": "{variant}",
  "stacking": "stackable",
  "sort_mode": "none",
  "slot_count": 20,
  "grid_width": 5,
  "grid_height": 4,
  "max_weight": 50.0,
  "equipment_slots": ["Head", "Chest", "Legs", "Weapon", "Offhand"],
  "items": [
    {{"name": "Potion", "effect": "heal", "amount": 25.0, "target_id": ""}},
    {{"name": "Sword", "effect": "none", "amount": 0.0, "target_id": ""}}
  ],
  "allow_drop": true,
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS


class InventoryPipeline(UnityMicroPipeline):
    kind            = "inventory"
    display_name    = "Unity Inventory"
    schema_cls      = InventorySchema
    variants        = VARIANTS
    default_variant = "slot_based"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "inventory"

    _RESERVED_FIELD_NAMES = {
        "stacks", "equipped", "slotcount", "gridwidth", "gridheight",
        "maxweight", "slots", "selectedindex", "_stacks", "_equipped",
        # Item-effect dispatch (UseItem() + its per-effect events — see
        # Inventory.cs.j2 and ItemDefinition.cs.j2's mirrored Effect enum)
        "useitem", "onitemused", "onhealeffect", "onshieldeffect",
        "onstatboosteffect", "onunlockeffect", "onquestflageffect",
    }
    _TEMPLATE_USINGS = {"UnityEngine", "System", "System.Collections.Generic"}

    _DEFAULT_EQUIP_SLOTS = ["Head", "Chest", "Legs", "Weapon", "Offhand"]

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "Inventory")

        # Clamp numerics.
        if schema.slot_count < 1:   schema.slot_count = 1
        if schema.grid_width < 1:   schema.grid_width = 1
        if schema.grid_height < 1:  schema.grid_height = 1
        if schema.max_weight <= 0:  schema.max_weight = 50.0

        # Variant coherence.
        if variant == "equipment":
            schema.stacking = "unique"          # equipment items never stack
            if not schema.equipment_slots:
                schema.equipment_slots = list(self._DEFAULT_EQUIP_SLOTS)
        if variant == "grid":
            # Keep slot_count consistent with the grid for display/UX.
            schema.slot_count = schema.grid_width * schema.grid_height

        # Item effects — abstain loudly (downgrade to "none") rather than ship
        # an effect UseItem() would fire but that can never do anything: a
        # targeted effect with no target, or an amount-driven one with
        # amount<=0, is a silent no-op at runtime with no error to find it by.
        for it in schema.items:
            if it.effect in _TARGETED_EFFECTS and not it.target_id.strip():
                console.print(f"  [yellow]WARN[/]  item {it.name!r}: "
                              f"effect={it.effect!r} needs a target_id — "
                              f"dropped to \"none\"")
                it.effect = "none"
            elif it.effect in _AMOUNT_EFFECTS and it.amount <= 0:
                console.print(f"  [yellow]WARN[/]  item {it.name!r}: "
                              f"effect={it.effect!r} needs amount > 0 — "
                              f"dropped to \"none\"")
                it.effect = "none"

        # hoist_field_inits: this generator's template renders awake_extra,
        # so a CS0236 initializer can be re-homed there instead of dropped.
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS,
                        hoist_field_inits=True)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (scripts_dir / f"{schema.name}ItemDefinition.cs").write_text(
            env.get_template("ItemDefinition.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("Inventory.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 3
