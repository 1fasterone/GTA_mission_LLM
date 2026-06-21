"""
Unity genre-atom ontology entries.

Each atom is a deterministic, reusable gameplay behaviour micro-template.
Atoms are the Unity equivalent of GTA5 "atoms" (spawn_ped, drive_to, etc.).
"""
from __future__ import annotations
from .base import RagEntry


def make_genre_atom_entry(raw: dict, source: str = "Unity micro-template library") -> RagEntry:
    atom_id = raw.get("id", "")
    if not atom_id:
        raise ValueError("genre_atom record missing id")

    label = raw.get("label", atom_id.replace("_", " ").title())
    genre = raw.get("genre", "universal")
    description = raw.get("description", label)
    aliases = raw.get("aliases", [])
    required_components = raw.get("requires_components", [])
    required_packages = raw.get("requires_packages", [])
    vfx = raw.get("vfx", [])
    audio = raw.get("audio", [])
    action_type = raw.get("action_type", "behaviour")

    tags = [
        f"genre.{genre}",
        f"action.{action_type}",
        "is_atom",
    ]
    for c in required_components:
        tags.append(f"requires.{c.lower()}")
    for p in required_packages:
        tags.append(f"requires_pkg.{p}")

    desc = (
        f"Unity atom {atom_id}. {description}. "
        f"Genre: {genre}. "
        f"Requires components: {', '.join(required_components) or 'none'}. "
        f"VFX: {', '.join(vfx) or 'none'}. "
        f"Audio cues: {', '.join(audio) or 'none'}."
    ).strip()

    meta: dict = {k: v for k, v in {
        "genre": genre,
        "action_type": action_type,
        "requires_components": required_components,
        "requires_packages": required_packages,
        "vfx": vfx,
        "audio": audio,
        "template": raw.get("template", atom_id),
        "complexity": raw.get("complexity", "simple"),
    }.items() if v}

    entry = RagEntry(
        id=f"unity:genre_atom:{atom_id}",
        domain="unity", kind="genre_atom",
        canonical=atom_id, label=label,
        aliases=aliases, description=desc,
        tags=tags, meta=meta, source=source,
    )
    entry.chunk_text = (
        f"Unity atom {entry.canonical}\n"
        f"Genre: {genre} | Action: {action_type}\n"
        f"Aliases: {', '.join(entry.aliases)}\n"
        f"Tags: {', '.join(entry.tags)}\n"
        f"Description: {entry.description}"
    )
    return entry
