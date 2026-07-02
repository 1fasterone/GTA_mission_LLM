"""
Atom IR corpus scraper.

Generates rag/corpus/shared/atoms.jsonl from the AtomDef registry.
Run via: python -m rag.build atoms
"""
from __future__ import annotations

from pathlib import Path

from rag.schema.atom import make_atom_entry


CORPUS_PATH = Path(__file__).parent.parent / "corpus" / "shared" / "atoms.jsonl"


def run(limit: int | None = None) -> int:
    from src.atoms import ALL_ATOMS

    CORPUS_PATH.parent.mkdir(parents=True, exist_ok=True)

    atoms = ALL_ATOMS if limit is None else ALL_ATOMS[:limit]
    lines = [make_atom_entry(a).to_jsonl() for a in atoms]

    CORPUS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)
