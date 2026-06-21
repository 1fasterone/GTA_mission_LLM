"""
Base ontology record — the contract every corpus entry must satisfy.

One RagEntry = one embeddable chunk. The resolver reads `canonical` when
it matches; the vector DB embeds `chunk_text` at index time.
"""
from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator


DOMAINS = Literal["gta5", "unity"]


class RagEntry(BaseModel):
    # ── Identity ────────────────────────────────────────────────────────────
    id: str = ""                          # "{domain}:{kind}:{canonical}" — stable, globally unique
    domain: DOMAINS
    kind: str                             # ped | vehicle | animation | scenario | audio | weapon | ...

    # ── Primary resolution target ────────────────────────────────────────────
    canonical: str                        # exact game ID (model name, pkg id, anim dict name)
    label: str                            # human-readable display name

    # ── Semantic surface — what an LLM or user might say ────────────────────
    aliases: list[str] = Field(default_factory=list)
    description: str = ""

    # ── Ontology position ────────────────────────────────────────────────────
    tags: list[str] = Field(default_factory=list)   # dot-notation: faction.law, class.super

    # ── Kind-specific structured data ────────────────────────────────────────
    meta: dict[str, Any] = Field(default_factory=dict)

    # ── Embedding target — assembled by each schema module ───────────────────
    chunk_text: str = ""

    # ── Provenance ───────────────────────────────────────────────────────────
    source: str = ""
    corpus_version: str = "1.0"

    @model_validator(mode="after")
    def _fill_id(self) -> "RagEntry":
        if not self.id:
            self.id = f"{self.domain}:{self.kind}:{self.canonical}"
        return self

    def to_jsonl(self) -> str:
        return self.model_dump_json()
