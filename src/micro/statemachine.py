"""State machine micro-generator — generic FSM for any game object."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

import re

from ..micro_pipeline import console, to_pascal
from .unity_base import UnityMicroPipeline
from .code_fields import (CodeField, CodeMethod, csharp_ident,
                          find_unbound_identifiers, lint_code_fragment,
                          lint_usings, lint_fields)


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
        # Death. Unlike player_fsm the template gives enemy_fsm no Kill(), so
        # without these three edges the Dead state it ships is unreachable — a
        # terminal state no enemy could ever enter, which the graph lint now
        # reports. Driven by _hp, declared in _DEFAULT_CONDITION_FIELDS below.
        {"from_state": "Patrol", "to_state": "Dead",   "condition": "_hp <= 0f"},
        {"from_state": "Chase",  "to_state": "Dead",   "condition": "_hp <= 0f"},
        {"from_state": "Attack", "to_state": "Dead",   "condition": "_hp <= 0f"},
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

# Backing fields the DEFAULT transition conditions reference. Without these the
# generated FSM references undeclared identifiers (CS0103). player_fsm needs none —
# the template already declares _moveInput / _jumpPressed / _grounded.
_DEFAULT_CONDITION_FIELDS: dict[str, list[dict]] = {
    "enemy_fsm": [
        {"name": "attackRange",   "type": "float", "default": "2f", "header": "AI"},
        {"name": "_distToPlayer", "type": "float", "default": "999f"},
        {"name": "_seesPlayer",   "type": "bool",  "default": "false"},
        {"name": "_heardNoise",   "type": "bool",  "default": "false"},
        {"name": "_alertTimer",   "type": "float", "default": "0f"},
        {"name": "_lostTimer",    "type": "float", "default": "0f"},
        # Drives the three -> Dead edges above. Serialized so a health micro or
        # a prompt-authored script can drive it without another seam.
        {"name": "_hp",           "type": "float", "default": "100f",
         "header": "Stats"},
    ],
    "game_fsm": [
        {"name": "_startPressed",   "type": "bool", "default": "false"},
        {"name": "_pausePressed",   "type": "bool", "default": "false"},
        {"name": "_playerDead",     "type": "bool", "default": "false"},
        {"name": "_restartPressed", "type": "bool", "default": "false"},
    ],
    "object_fsm": [
        {"name": "_triggered",    "type": "bool",  "default": "false"},
        {"name": "_activeDone",   "type": "bool",  "default": "false"},
        {"name": "_cooldownDone", "type": "bool",  "default": "false"},
        {"name": "_hp",           "type": "float", "default": "100f", "header": "Stats"},
    ],
}


# Fields StateMachine.cs.j2 declares itself, per variant. A transition condition
# may reference these without declaring anything; anything else it names has to
# be declared or the emitted CheckTransitions() is CS0103.
_TEMPLATE_FIELDS: dict[str, set[str]] = {
    "player_fsm": {"_currentState", "_prevState",
                   "_moveInput", "_jumpPressed", "_grounded"},
}
_ALWAYS_FIELDS = {"_currentState", "_prevState", "CurrentState", "IsIn",
                  "OnStateChanged"}

# State names the TEMPLATE itself emits code against. StateMachine.cs.j2 gives
# player_fsm a hardcoded CheckTransitions() and a Kill() that transitions to
# State.Dead, so these four must exist in the enum however the model names its
# own states — and SEAM 2 (Health.OnDeath -> FSM.Kill) depends on Dead.
_TEMPLATE_REQUIRED_STATES: dict[str, tuple[str, ...]] = {
    "player_fsm": ("Idle", "Moving", "Jumping", "Dead"),
}

# States the template reaches WITHOUT a transition, so the unreachable-state
# report must not flag them: player_fsm's Kill() transitions straight to Dead,
# and SEAM 2 (Health.OnDeath -> FSM.Kill) is what calls it.
_TEMPLATE_DRIVEN_STATES: dict[str, tuple[str, ...]] = {
    "player_fsm": ("Dead",),
}

# Type inferred for an identifier the model referenced but never declared, read
# off how the condition USES it. Ordered: the first pattern that matches wins.
_INFERRED_TYPE: tuple[tuple[str, str, str], ...] = (
    # (regex against the condition, C# type, default value)
    (r"\b{name}\s*(?:[<>]=?|[=!]=)\s*-?\d+(?:\.\d+)?f?\b", "float", "0f"),
    (r"-?\d+(?:\.\d+)?f?\s*(?:[<>]=?|[=!]=)\s*\b{name}\b", "float", "0f"),
    (r"\b{name}\s*(?:[=!]=)\s*\"",                          "string", "\"\""),
)

# A call the model invented cannot be declared as a field, so a condition that
# makes one is dropped rather than repaired.
_CALL_RE_TMPL = r"\b{name}\s*\("


def _infer_field(name: str, condition: str) -> CodeField:
    """Declare an identifier the model used but never introduced.

    This is the Tier-V half of the name/role split applied to expressions
    (docs/LLM_ENRICHMENT_STRATEGY.md): the SHAPE of the condition is the
    model's — `_alertTimer > 2f` says something real about the game — while the
    declaration that makes it compile is the compiler's. Dropping the
    transition instead would throw the model's intent away to fix a missing
    `private float`.
    """
    for pattern, ctype, default in _INFERRED_TYPE:
        if re.search(pattern.format(name=re.escape(name)), condition):
            return CodeField(name=name, type=ctype, default=default,
                             header="FSM (inferred)")
    return CodeField(name=name, type="bool", default="false",
                     header="FSM (inferred)")


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
        used_default_transitions = not schema.transitions
        if not schema.transitions:
            schema.transitions = [StateTransition(**d) for d in _DEFAULT_TRANSITIONS.get(variant, [])]
        if not schema.initial_state and schema.states:
            schema.initial_state = schema.states[0].name

        # When the default transitions are used, declare the fields their
        # conditions reference so the FSM compiles (CS0103 guard). Skip any the
        # LLM already declared.
        if used_default_transitions and variant in _DEFAULT_CONDITION_FIELDS:
            existing = {f.name for f in schema.extra_fields}
            for d in _DEFAULT_CONDITION_FIELDS[variant]:
                if d["name"] not in existing:
                    schema.extra_fields.append(CodeField(**d))

        # Make the graph provably compile BEFORE the field/using lints run, so a
        # backing field this pass infers is linted like any other.
        for note in self._lint_graph(schema, variant):
            console.print(f"  [yellow]WARN[/]  {note}")

        schema.extra_usings = lint_usings(
            schema.extra_usings, already_in_template=self._TEMPLATE_USINGS,
        )
        schema.extra_fields = lint_fields(
            schema.extra_fields, reserved=self._RESERVED_FIELD_NAMES,
        )
        for i, m in enumerate(schema.extra_methods):
            schema.extra_methods[i].body = lint_code_fragment(m.body, what="method body")
        return schema

    # ── Graph repair ──────────────────────────────────────────────────────────

    def _lint_graph(self, schema, variant) -> list[str]:
        """Make `states` + `transitions` into a graph that provably compiles.

        The same three failure modes the `ai` micro had, and for the same
        reason: state names reach a generated enum that a switch dispatches on,
        and transition conditions reach emitted C#. Deterministic repair over
        authored content — names are sanitised rather than rejected, an
        undeclared identifier is declared rather than dropped, and only a
        genuinely unrepairable edge is removed, by name. Never raises.
        """
        notes: list[str] = []

        # 1. Sanitise + de-duplicate state names (CS0117 / CS1001 at the root).
        #    (step 1b below adds back any name the TEMPLATE itself requires)
        seen: dict[str, str] = {}
        renames: dict[str, str] = {}
        kept: list[StateDefinition] = []
        for st in schema.states:
            ident = csharp_ident(st.name, "State")
            if ident in seen:
                notes.append(f"fsm state {st.name!r} dropped: collides with "
                             f"{seen[ident]!r} after C# name sanitising")
                continue
            seen[ident] = st.name
            renames[str(st.name)] = ident
            if st.name != ident:
                notes.append(f"fsm state {st.name!r} renamed to {ident} "
                             f"(valid C# enum member)")
            st.name = ident
            kept.append(st)
        if not kept:
            kept = [StateDefinition(**d)
                    for d in _DEFAULT_STATES.get(variant, _DEFAULT_STATES["player_fsm"])]
            notes.append("fsm: no usable state names survived; the default "
                         f"{variant} state set was emitted instead")
        # 1b. States the TEMPLATE's own code references by name. player_fsm's
        #     CheckTransitions() and Kill() are hardcoded blocks in
        #     StateMachine.cs.j2, so renaming these four out of the enum is
        #     CS0117 in template-owned code — and SEAM 2 calls Kill(). Missing
        #     ones are added back; the model's own states ride alongside them.
        required = _TEMPLATE_REQUIRED_STATES.get(variant, ())
        have = {st.name for st in kept}
        for want in required:
            if want not in have:
                kept.append(StateDefinition(name=want,
                                            on_update=f"// {want} (required by "
                                                      f"the {variant} template)"))
                have.add(want)
                notes.append(f"fsm state {want} re-added — the {variant} "
                             f"template's own CheckTransitions/Kill names it")
        schema.states = kept
        names = {st.name for st in kept}

        def _resolve(ref: str) -> "str | None":
            r = str(ref)
            if r in names:
                return r
            if r in renames and renames[r] in names:
                return renames[r]
            ident = csharp_ident(r, "State")
            return ident if ident in names else None

        # 2. Transitions: drop dangling endpoints and self-loops, then make
        #    every surviving condition bindable.
        declared = (set(_ALWAYS_FIELDS) | _TEMPLATE_FIELDS.get(variant, set())
                    | {f.name for f in schema.extra_fields}
                    | {m.signature.split("(")[0].split()[-1]
                       for m in schema.extra_methods if "(" in m.signature})
        added: list[CodeField] = []
        valid: list[StateTransition] = []
        for t in schema.transitions:
            src, dst = _resolve(t.from_state), _resolve(t.to_state)
            if src is None or dst is None:
                missing = t.from_state if src is None else t.to_state
                notes.append(f"fsm transition {t.from_state}->{t.to_state} "
                             f"dropped: no state named {missing!r}")
                continue
            if src == dst:
                notes.append(f"fsm transition {src}->{dst} dropped: self-loop")
                continue

            cond = (t.condition or "").strip() or "false"
            unbound: set[str] = set()
            for row in find_unbound_identifiers(
                    cond, known=declared | names | {"State"}).values():
                unbound |= row
            # A call cannot be declared as a field, so an invented method name
            # is the one case this pass cannot repair. find_unbound_identifiers
            # reports a call as "Name()", so strip that before testing.
            calls, fields = set(), set()
            for n in unbound:
                base = n.split("(")[0].strip()
                if n != base or re.search(
                        _CALL_RE_TMPL.format(name=re.escape(base)), cond):
                    calls.add(base)
                elif base:
                    fields.add(base)
            if calls:
                notes.append(
                    f"fsm transition {src}->{dst} dropped: condition calls "
                    f"{', '.join(sorted(calls))}(), which nothing declares")
                continue
            for n in sorted(fields):
                f = _infer_field(n, cond)
                added.append(f)
                declared.add(n)
                notes.append(f"fsm condition on {src}->{dst} referenced "
                             f"undeclared {n} — declared as {f.type} {n} = "
                             f"{f.default}")
            t.condition, t.from_state, t.to_state = cond, src, dst
            valid.append(t)
        schema.transitions = valid
        schema.extra_fields = list(schema.extra_fields) + added

        # 3. initial_state must name a real state.
        init = _resolve(schema.initial_state) if schema.initial_state else None
        if init is None:
            init = kept[0].name
            if schema.initial_state:
                notes.append(f"fsm initial_state {schema.initial_state!r} is "
                             f"not a state; using {init}")
        schema.initial_state = init

        # 4. Abstain loudly on states nothing can reach: they render (the
        #    model's vocabulary is preserved) but will never run.
        #    Template-owned code counts as a route in: player_fsm's Kill() is
        #    what drives Dead (SEAM 2 calls it), so no transition names it and
        #    reporting it as unreachable would be a false alarm on every build.
        reachable = ({schema.initial_state}
                     | {t.to_state for t in schema.transitions}
                     | set(_TEMPLATE_DRIVEN_STATES.get(variant, ())))
        orphans = [st.name for st in kept if st.name not in reachable]
        if orphans:
            notes.append("fsm: no transition reaches " + ", ".join(orphans)
                         + " — emitted but unreachable at runtime")
        return notes

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
