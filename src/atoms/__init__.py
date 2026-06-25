"""
Atom IR — Phase 2 of the compiler pipeline.

26 engine-agnostic atomic actions across 7 categories.
These are the smallest deterministic building blocks the LLM and composer use.

Usage:
    from src.atoms import ATOM_REGISTRY, get_atom

    atom = get_atom("MoveTo")          # → AtomDef
    atom = get_atom("move_to")         # → same (alias lookup)
    atoms = list_atoms("movement")     # → [AtomDef, ...]
"""

from __future__ import annotations

from .base       import AtomDef, AtomParam, AtomInvocation
from .movement   import MOVEMENT_ATOMS
from .animation  import ANIMATION_ATOMS
from .camera     import CAMERA_ATOMS
from .audio      import AUDIO_ATOMS
from .fx         import FX_ATOMS
from .logic      import LOGIC_ATOMS
from .entity     import ENTITY_ATOMS
from .controller import CONTROLLER_ATOMS
from .hud        import HUD_ATOMS
from .physics    import PHYSICS_ATOMS
from .input      import INPUT_ATOMS
from .model      import MODEL_ATOMS

ALL_ATOMS: list[AtomDef] = (
    MOVEMENT_ATOMS
    + ANIMATION_ATOMS
    + CAMERA_ATOMS
    + AUDIO_ATOMS
    + FX_ATOMS
    + LOGIC_ATOMS
    + ENTITY_ATOMS
    + CONTROLLER_ATOMS
    + HUD_ATOMS
    + PHYSICS_ATOMS
    + INPUT_ATOMS
    + MODEL_ATOMS
)

# Primary lookup by canonical name ("MoveTo")
ATOM_REGISTRY: dict[str, AtomDef] = {a.name: a for a in ALL_ATOMS}

# Alias index — any alias also resolves to the canonical AtomDef
_ALIAS_INDEX: dict[str, AtomDef] = {}
for _atom in ALL_ATOMS:
    for _alias in _atom.aliases:
        _ALIAS_INDEX.setdefault(_alias.lower(), _atom)

CATEGORIES: list[str] = [
    "movement", "animation", "camera", "audio", "fx", "logic", "entity",
    "controller", "hud", "physics", "input", "model",
]


def get_atom(name: str) -> AtomDef | None:
    """Look up an atom by canonical name or alias (case-insensitive)."""
    return ATOM_REGISTRY.get(name) or _ALIAS_INDEX.get(name.lower())


def list_atoms(category: str | None = None) -> list[AtomDef]:
    """Return all atoms, optionally filtered by category."""
    if category is None:
        return list(ALL_ATOMS)
    return [a for a in ALL_ATOMS if a.category == category]


__all__ = [
    "AtomDef", "AtomParam", "AtomInvocation",
    "ALL_ATOMS", "ATOM_REGISTRY", "CATEGORIES",
    "get_atom", "list_atoms",
]
