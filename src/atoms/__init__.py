"""
Atom IR — Phase 2 of the compiler pipeline.

Registered action vocabulary; actual backend support is audited separately.
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
from .wiring     import WIRING_ATOMS          # Unity-only seam primitives
from .terrain    import TERRAIN_ATOMS         # Unity-only terrain primitives
from .dialog     import DIALOG_ATOMS          # Unity-only NPC dialog beats

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
    + WIRING_ATOMS
    + TERRAIN_ATOMS
    + DIALOG_ATOMS
)

# Primary lookup by canonical name ("MoveTo")
ATOM_REGISTRY: dict[str, AtomDef] = {a.name: a for a in ALL_ATOMS}

def _build_alias_index(atoms: list[AtomDef]) -> dict[str, AtomDef]:
    """Case-insensitive token -> owner, one owner per token (R1).

    A token is an atom's canonical name or one of its aliases. Two atoms
    claiming one token is a vocabulary error raised at import, never a silent
    first-registered-wins: a lookup that depends on list order changes meaning
    whenever a module is reordered.
    """
    owners: dict[str, AtomDef] = {}
    for atom in atoms:
        for token in (atom.name, *atom.aliases):
            key = token.lower()
            holder = owners.get(key)
            if holder is not None and holder.name != atom.name:
                raise RuntimeError(
                    f"atom vocabulary collision: {token!r} is claimed by both "
                    f"{holder.name} and {atom.name}; give the token one owner "
                    f"(docs/ATOM_BACKEND_SUPPORT.md, identity policy)"
                )
            owners[key] = atom
    return owners


# Alias index — any alias (or canonical name, any case) resolves to its owner
_ALIAS_INDEX: dict[str, AtomDef] = _build_alias_index(ALL_ATOMS)

CATEGORIES: list[str] = [
    "movement", "animation", "camera", "audio", "fx", "logic", "entity",
    "controller", "hud", "physics", "input", "model",
    "wiring",   # Unity-only seam atoms — domain-isolated from GTA5
    "terrain",  # Unity-only terrain atoms — domain-isolated from GTA5
    "dialog",   # Unity-only NPC dialog beats (npc_dialog micro companion)
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
