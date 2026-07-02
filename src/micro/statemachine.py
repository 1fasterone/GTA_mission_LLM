"""State machine micro-generator — generic FSM for any game object."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..micro_pipeline import to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import CodeField, CodeMethod, lint_code_fragment, lint_usings, lint_fields


StateMachineVariant = Literal[
    "player_fsm", "enemy_fsm", "game_fsm", "object_fsm",
]
VARIANTS = list(StateMachineVariant.__args__)  # type: ignore[attr-defined]


class StateDefinition(BaseModel):
    name:        str
    on_enter:    str = ""   # C# statements inside Enter case
    on_update:   str = ""   # C# statements inside Update case
    on_exit:     str = ""   # C# statements inside Exit case
    is_terminal: bool = False  # true → Despawn/GameOver states


class StateTransition(BaseModel):
    from_state: str
    to_state:   str
    condition:  str   # C# bool expression evaluated every Update tick


class StateMachineSchema(BaseModel):
    name:          str = "StateMachine"
    variant:       str = "player_fsm"
    states:        list[StateDefinition] = Field(default_factory=list)
    transitions:   list[StateTransition] = Field(default_factory=list)
    initial_state: str = ""
    description:   str = "Generic state machine"

    extra_usings:  list[str]        = Field(default_factory=list)
    extra_fields:  list[CodeField]  = Field(default_factory=list)
    extra_methods: list[CodeMethod] = Field(default_factory=list)
    meta:          dict             = Field(default_factory=dict)

    @field_validator("variant", mode="before")
    @classmethod
    def _clamp_variant(cls, v: str) -> str:
        s = str(v).strip().lower()
        return s if s in VARIANTS else "player_fsm"


# ── Default state sets per variant ────────────────────────────────────────────

_DEFAULT_STATES: dict[str, list[dict]] = {
    "player_fsm": [
        {"name": "Idle",   "on_update": "// waiting for input"},
        {"name": "Moving", "on_update": "// apply movement"},
        {"name": "Jumping","on_enter": "// launch jump", "is_terminal": False},
        {"name": "Dead",   "on_enter": "// disable controls; play death anim", "is_terminal": True},
    ],
    "enemy_fsm": [
        {"name": "Patrol", "on_update": "// move along waypoints"},
        {"name": "Alert",  "on_enter": "// stop; look toward noise", "on_update": "// scan"},
        {"name": "Chase",  "on_update": "// pursue player"},
        {"name": "Attack", "on_enter": "// trigger attack anim", "on_update": "// attack cooldown"},
        {"name": "Dead",   "on_enter": "// ragdoll / despawn", "is_terminal": True},
    ],
    "game_fsm": [
        {"name": "MainMenu"},
        {"name": "Playing", "on_enter": "// resume time; show HUD"},
        {"name": "Paused",  "on_enter": "Time.timeScale = 0f;", "on_exit": "Time.timeScale = 1f;"},
        {"name": "GameOver","on_enter": "// show game over screen", "is_terminal": True},
    ],
    "object_fsm": [
        {"name": "Inactive"},
        {"name": "Active",   "on_enter": "// enable; start coroutine"},
        {"name": "Cooldown", "on_update": "// wait for cooldown timer"},
        {"name": "Destroyed","on_enter": "Destroy(gameObject);", "is_terminal": True},
    ],
}

_DEFAULT_TRANSITIONS: dict[str, list[dict]] = {
    "player_fsm": [
        {"from_state": "Idle",    "to_state": "Moving",  "condition": "_moveInput.magnitude > 0.1f"},
        {"from_state": "Moving",  "to_state": "Idle",    "condition": "_moveInput.magnitude < 0.05f"},
        {"from_state": "Idle",    "to_state": "Jumping", "condition": "_jumpPressed"},
        {"from_state": "Moving",  "to_state": "Jumping", "condition": "_jumpPressed"},
        {"from_state": "Jumping", "to_state": "Idle",    "condition": "_grounded"},
    ],
    "enemy_fsm": [
        {"from_state": "Patrol", "to_state": "Alert",  "condition": "_heardNoise"},
        {"from_state": "Alert",  "to_state": "Chase",  "condition": "_seesPlayer"},
        {"from_state": "Alert",  "to_state": "Patrol", "condition": "_alertTimer <= 0f"},
        {"from_state": "Chase",  "to_state": "Attack", "condition": "_distToPlayer < attackRange"},
        {"from_state": "Attack", "to_state": "Chase",  "condition": "_distToPlayer >= attackRange"},
        {"from_state": "Chase",  "to_state": "Patrol", "condition": "!_seesPlayer && _lostTimer <= 0f"},
    ],
    "game_fsm": [
        {"from_state": "MainMenu", "to_state": "Playing",  "condition": "_startPressed"},
        {"from_state": "Playing",  "to_state": "Paused",   "condition": "_pausePressed"},
        {"from_state": "Paused",   "to_state": "Playing",  "condition": "_pausePressed"},
        {"from_state": "Playing",  "to_state": "GameOver", "condition": "_playerDead"},
        {"from_state": "GameOver", "to_state": "MainMenu", "condition": "_restartPressed"},
    ],
    "object_fsm": [
        {"from_state": "Inactive", "to_state": "Active",    "condition": "_triggered"},
        {"from_state": "Active",   "to_state": "Cooldown",  "condition": "_activeDone"},
        {"from_state": "Cooldown", "to_state": "Active",    "condition": "_cooldownDone"},
        {"from_state": "Active",   "to_state": "Destroyed", "condition": "_hp <= 0f"},
    ],
}


CONCEPT_PROMPT = """\
{theme_block}
Describe a state machine for this Unity game object:

"{user_prompt}"

Variant: {variant}

Cover in 1-2 paragraphs:
- What the object IS (player, enemy, door, boss, vehicle...)
- What states it cycles through (e.g. Idle→Alert→Chase→Attack→Dead)
- What triggers each transition (distance to player, health threshold, input, timer...)
- Any concept-specific state behaviour (e.g. an alien enemy that "burrows" underground)

Do NOT write code or JSON.
"""

SCHEMA_PROMPT = """\
{schema_hints}
Convert the brief into a state machine schema. Output ONLY JSON.

The template renders:
  - An enum of all states
  - Enter / Update / Exit switch blocks per state
  - A CheckTransitions() method that runs every Update

Rules:
- on_enter / on_update / on_exit are raw C# statement strings (no braces).
- condition fields are C# bool expressions evaluated every tick.
- Use private fields for state data (declare in extra_fields).
- Name states in PascalCase (e.g. "Patrolling", "ChasePlayer").
- initial_state must match one of the state names exactly.
- extra_fields: add any private tracking vars (timers, cached refs, etc.)
- extra_methods: helpers called from on_* or conditions

VARIANT: {variant}

BRIEF:
{concept}

OUTPUT (JSON only):
{{
  "name": "PascalCaseName",
  "variant": "{variant}",
  "description": "One sentence.",
  "initial_state": "StateNameHere",
  "states": [
    {{"name": "Idle",  "on_enter": "", "on_update": "// idle logic", "on_exit": "", "is_terminal": false}},
    {{"name": "Dead",  "on_enter": "// death sequence", "on_update": "", "on_exit": "", "is_terminal": true}}
  ],
  "transitions": [
    {{"from_state": "Idle", "to_state": "Dead", "condition": "_hp <= 0f"}}
  ],
  "extra_usings": [],
  "extra_fields": [
    {{"name": "_hp", "type": "float", "default": "100f", "header": "Stats"}}
  ],
  "extra_methods": []
}}

Derive states and transitions FROM the prompt — not generic defaults.
Output ONLY the JSON.
"""


class StateMachinePipeline(UnityMicroPipeline):
    kind            = "statemachine"
    display_name    = "Unity State Machine (FSM)"
    schema_cls      = StateMachineSchema
    variants        = VARIANTS
    default_variant = "player_fsm"
    concept_prompt  = CONCEPT_PROMPT
    schema_prompt   = SCHEMA_PROMPT
    template_subdir = "statemachine"

    _RESERVED_FIELD_NAMES = {"_currentState", "_prevState"}
    _TEMPLATE_USINGS = {"UnityEngine", "System.Collections"}

    def default_fix(self, schema, variant):
        schema.name = to_pascal(schema.name, "StateMachine")

        # Fill states and transitions from defaults if LLM left them empty
        if not schema.states:
            schema.states = [StateDefinition(**d) for d in _DEFAULT_STATES.get(variant, _DEFAULT_STATES["player_fsm"])]
        if not schema.transitions:
            schema.transitions = [StateTransition(**d) for d in _DEFAULT_TRANSITIONS.get(variant, [])]
        if not schema.initial_state and schema.states:
            schema.initial_state = schema.states[0].name

        schema.extra_usings = lint_usings(
            schema.extra_usings, already_in_template=self._TEMPLATE_USINGS,
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
            env.get_template("StateMachine.cs.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        (out_dir / "README.md").write_text(
            env.get_template("README.md.j2").render(s=schema, variant=variant),
            encoding="utf-8",
        )
        return 2
