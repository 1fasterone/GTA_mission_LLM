"""Typed deterministic atom vocabulary and bound invocations.

AtomDef describes an action and its intended backend scope. Neither registration
nor an engines entry proves a lowerer, valid generated C#, attachment, or runtime
acceptance. See support_audit.py and docs/ATOM_BACKEND_SUPPORT.md. Atoms may express
multi-step deterministic actions; micro behavior remains separately owned until
its executable migration is verified.
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
