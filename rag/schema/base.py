"""
Base ontology record — the contract every corpus entry must satisfy.

One RagEntry = one embeddable chunk. The resolver reads `canonical` when
it matches; the vector DB embeds `chunk_text` at index time.
"""
from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator


DOMAINS = Literal["gta5", "unity", "shared"]


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


def build_chunk(entry: "RagEntry", headline: str,
                facts: dict[str, str] | None = None) -> str:
    """
    Assemble one entry's embedding text, stating each fact exactly once.

    Repetition is not free. The embedder averages over the whole chunk, so a
    sentence carried by every entry of a kind ("Use for: NPC cast, faction
    encounters, ambient crowd." on all 1,109 peds) adds no ranking signal while
    pulling that kind's vectors toward a single centroid — which squeezes the
    distance range `RagClient._best()` thresholds on until nothing is ever
    rejected. Same for restating the label, aliases and tags inside
    `description` and then again as their own lines.

    So: headline, distinguishing facts, aliases, tags, and a description that
    carries only what the other lines do not.
    """
    lines = [headline]
    for key, value in (facts or {}).items():
        if value:
            lines.append(f"{key}: {value}")
    if entry.aliases:
        lines.append("Also called: " + ", ".join(entry.aliases))
    if entry.tags:
        lines.append("Tags: " + ", ".join(entry.tags))
    if entry.description:
        lines.append(entry.description)
    return "\n".join(lines)
