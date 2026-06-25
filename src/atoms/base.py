"""
Atom IR — the smallest, deterministic, engine-agnostic action units.

An AtomDef describes WHAT an atom does and what parameters it accepts.
An AtomInvocation is a single call to an atom with bound argument values.

Atoms are:
  - engine-valid    (each backend translator knows how to emit them)
  - schema-valid    (Pydantic-enforced params)
  - RAG-grounded    (every atom is a corpus entry the LLM can reference by name)
  - deterministic   (same invocation → same code, every time)
  - reusable        (shared across Unity, GTA5, and any future engine)
  - composable      (sequences of AtomInvocations form composite behaviors)

Atoms are NOT scenes, missions, behaviors, patterns, or multi-step logic.
One atom = one side-effect-predictable action.
"""

from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


ParamType = Literal[
    "float", "int", "bool", "string",
    "vector3",      # {"x": 0, "y": 0, "z": 0}
    "color",        # {"r": 1, "g": 1, "b": 1, "a": 1}
    "entity_ref",   # name of a scene entity / prefab key
    "anim_clip",    # animation clip name
    "sfx_id",       # sound effect identifier
    "vfx_id",       # particle effect identifier
]

Duration = Literal[
    "instant",          # executes in one frame
    "duration_param",   # has an explicit 'duration' float param
    "until_complete",   # runs until an internal condition is met (e.g. MoveTo)
]


class AtomParam(BaseModel):
    name:        str
    type:        ParamType = "float"
    default:     Any       = None
    description: str       = ""
    required:    bool      = False


class AtomDef(BaseModel):
    """Static definition of one atom type — lives in the registry."""
    name:         str              # "MoveTo"
    category:     str              # "movement" | "animation" | "camera" | "audio" | "fx" | "logic" | "entity"
    description:  str
    params:       list[AtomParam]  = Field(default_factory=list)
    duration:     Duration         = "instant"
    side_effects: list[str]        = Field(default_factory=list)
    aliases:      list[str]        = Field(default_factory=list)
    engines:      list[str]        = Field(default_factory=lambda: ["unity", "gta5"])

    def param_map(self) -> dict[str, AtomParam]:
        return {p.name: p for p in self.params}


class AtomInvocation(BaseModel):
    """One bound call to an atom — appears inside composite schemas."""
    atom:  str       # must match an AtomDef.name in the registry
    args:  dict[str, Any] = Field(default_factory=dict)
    label: str       = ""  # human-readable step label
