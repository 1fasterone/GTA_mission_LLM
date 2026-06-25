"""
RAG schema for Atom IR entries.

Atoms are domain="shared" because they're engine-agnostic — the resolver
returns them regardless of whether the current pipeline is Unity or GTA5.
"""
from __future__ import annotations
from .base import RagEntry


def make_atom_entry(atom_def) -> RagEntry:
    """Build a RagEntry from an AtomDef."""
    tags = [
        f"category.{atom_def.category}",
        f"duration.{atom_def.duration}",
        "is_atom",
        "phase2_atom",
    ]
    for engine in atom_def.engines:
        tags.append(f"engine.{engine}")
    for effect in atom_def.side_effects:
        tags.append(f"side_effect.{effect}")

    param_desc = ", ".join(
        f"{p.name}: {p.type}" + (f" = {p.default}" if p.default is not None else "")
        for p in atom_def.params
    )

    desc = (
        f"Atom {atom_def.name}. {atom_def.description} "
        f"Category: {atom_def.category}. "
        f"Duration: {atom_def.duration}. "
        f"Params: {param_desc or 'none'}. "
        f"Engines: {', '.join(atom_def.engines)}."
    ).strip()

    entry = RagEntry(
        id=f"shared:atom:{atom_def.name}",
        domain="shared",
        kind="atom",
        canonical=atom_def.name,
        label=atom_def.name,
        aliases=list(atom_def.aliases),
        description=desc,
        tags=tags,
        meta={
            "category":     atom_def.category,
            "duration":     atom_def.duration,
            "side_effects": atom_def.side_effects,
            "engines":      atom_def.engines,
            "params": [
                {"name": p.name, "type": p.type, "default": p.default, "required": p.required}
                for p in atom_def.params
            ],
        },
        source="Atom IR Phase 2",
    )
    entry.chunk_text = (
        f"Atom {entry.canonical}\n"
        f"Category: {atom_def.category} | Duration: {atom_def.duration}\n"
        f"Aliases: {', '.join(entry.aliases)}\n"
        f"Tags: {', '.join(entry.tags)}\n"
        f"Params: {param_desc or 'none'}\n"
        f"Description: {atom_def.description}"
    )
    return entry
