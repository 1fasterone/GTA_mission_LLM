"""Interactable micro-generator — pickup / door / lever / switch / NPC / readable."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from ..micro_pipeline import console, to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (
    CodeExtensibleSchema, lint_extensible, clamp_enum, apply_extensions_safe_block,
    UNITY_EXTRA_INSTRUCTIONS,
)


InteractableVariant = Literal[
    "pickup", "door", "lever", "button_switch", "npc_talk", "readable",
]
VARIANTS = list(InteractableVariant.__args__)   # type: ignore[attr-defined]

# "trigger"  a tagged body walks into it
# "raycast"  the camera's forward ray hits it — a crosshair
# "pointer"  the CURSOR's ray hits it and the player clicks. The detection mode
#            for a game with no avatar: the other two both wait for a body,
#            one to arrive and one to aim, and a non-embodied game has neither.
Detection = Literal["trigger", "raycast", "pointer"]
InputKey  = Literal["E", "F", "Space", "Return"]


class InteractableSchema(CodeExtensibleSchema):
    name:        str       = "Interactable"
    variant:     str       = "pickup"
    detection:   Detection = "trigger"
    interact_key: InputKey = "E"
    prompt_text: str       = "Press [E]"
    radius:      float     = 2.0
    one_shot:    bool      = False     # consumed after first use (pickups)
    player_tag:  str       = "Player"
    # Unset by default — zero behavior change for every existing project. When
    # set, the whole locked/lockId/OnLocked/UnlockAny surface renders in the
    # template (Interactable.cs.j2 gates it behind `{% if s.locked %}`, same
    # discipline as AI's squad_alert_radius). A composed project's
    # Inventory.OnUnlockEffect(target_id) binds directly to the static
    # UnlockAny(targetId) broadcast — see unity_compose.py's _emit_context.
    locked:      bool      = False
    lock_id:     str       = ""    # the id an Unlock-effect item's target_id must match
    description: str       = "Interactable object"
    meta:        dict      = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "pickup"

    @field_validator("detection", mode="before")
    @classmethod
    def _clamp_detection(cls, v: str) -> str:
        return clamp_enum(v, ("trigger", "raycast", "pointer"), "trigger")

    @field_validator("interact_key", mode="before")
    @classmethod
    def _clamp_key(cls, v: str) -> str:
        return clamp_enum(v, ("E", "F", "Space", "Return"), "E")


CONCEPT_PROMPT = """\
{theme_block}
Describe a Unity interactable object for this idea:

"{user_prompt}"

Variant requested: {variant}

Cover in 1-2 paragraphs:
- What the object is and what happens when the player interacts
- How the player gets in range (a trigger collider, or a look-at raycast)
- The prompt shown and which key confirms
- Whether it is one-shot (a pickup) or repeatable (a lever/door)
- Whether it starts locked and needs a specific key/item to open

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into JSON. Output JSON only.

VARIANT MUST BE: {variant}
DETECTION    — trigger | raycast
INTERACT_KEY — E | F | Space | Return
"locked"   — true only if the brief says this needs a specific key/item first.
"lock_id"  — the id of the key/item that opens it (must match an Inventory
  item's target_id for its "unlock" effect) — REQUIRED whenever locked is
  true; leave both "" / false if the brief doesn't describe a lock.

BRIEF:
{concept}

OUTPUT:
{{
  "name": "PascalCase name",
  "variant": "{variant}",
  "detection": "trigger",
  "interact_key": "E",
  "prompt_text": "Press [E] to open",
  "radius": 2.0,
  "one_shot": false,
  "player_tag": "Player",
  "locked": false,
  "lock_id": "",
  "description": "One sentence."
}}
""" + UNITY_EXTRA_INSTRUCTIONS


class InteractablePipeline(UnityMicroPipeline):
    kind            = "interactable"
    display_name    = "Unity Interactable"
    schema_cls      = InteractableSchema
    variants        = VARIANTS
    default_variant = "pickup"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "interactable"

    _RESERVED_FIELD_NAMES = {
        "radius", "prompttext", "playertag", "_inrange", "_used", "_player",
        "interactkey",
        # Locking surface (s.locked only — see Interactable.cs.j2's
        # `{% if s.locked %}` gate and UnlockAny's static-broadcast docstring)
        "locked", "lockid", "onlocked", "unlockany", "_lockedinstances",
    }
    _TEMPLATE_USINGS = {"UnityEngine", "System", "System.Collections.Generic"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "Interactable")
        if schema.radius <= 0:
            schema.radius = 2.0
        # Pickups are inherently one-shot; doors/levers/switches repeat.
        if variant == "pickup":
            schema.one_shot = True
        if not schema.prompt_text.strip():
            schema.prompt_text = f"Press [{schema.interact_key}]"
        # Same abstain-loudly doctrine as Inventory's targeted effects
        # (src/micro/inventory.py): a lock with no id UnlockAny could ever
        # match is a silent runtime no-op — downgrade to unlocked with a
        # printed WARN rather than ship a door nothing can ever open.
        if schema.locked and not schema.lock_id.strip():
            console.print(f"  [yellow]WARN[/]  {schema.name}: locked=true "
                          f"needs a non-empty lock_id — dropped to unlocked")
            schema.locked = False
        lint_extensible(schema, reserved_fields=self._RESERVED_FIELD_NAMES,
                        template_usings=self._TEMPLATE_USINGS)
        apply_extensions_safe_block(schema, reserved=self._RESERVED_FIELD_NAMES)
        return schema

    def write_files(self, schema, variant, out_dir: Path) -> int:
        env = self.jinja()
        scripts_dir = out_dir / "Scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        (scripts_dir / f"{schema.name}.cs").write_text(
            env.get_template("Interactable.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
