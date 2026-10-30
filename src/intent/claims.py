"""Claim producers for the intent ledger.

This module converts observations into evidence. It never mutates a schema and
never chooses a winner; those are resolver responsibilities.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable


@dataclass(frozen=True)
class IntentClaim:
    field: str
    value: Any
    source: str
    confidence: float
    rationale: str

    def __post_init__(self) -> None:
        if not self.field.strip():
            raise ValueError("intent claim field must not be empty")
        if not self.source.strip():
            raise ValueError("intent claim source must not be empty")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("intent claim confidence must be between 0 and 1")


_EXCLUSION_TERMS: dict[str, tuple[str, ...]] = {
    "bike": ("bike", "bikes", "motorbike", "motorbikes", "motorcycle", "motorcycles"),
    "helicopter": ("helicopter", "helicopters", "chopper", "choppers"),
    "aircraft": ("aircraft", "airplane", "airplanes", "plane", "planes"),
    "vehicle": ("vehicle", "vehicles", "car", "cars"),
    "projectile": ("projectile", "projectiles", "bullet", "bullets"),
}


def explicit_exclusion_claims(prompt: str) -> list[IntentClaim]:
    """Extract closed-vocabulary user exclusions such as ``no bikes``.

    The short clause window deliberately ends at punctuation/conjunctions so a
    denial cannot silence an assertion later in the sentence.
    """
    text = prompt or ""
    claims: list[IntentClaim] = []
    for capability, terms in _EXCLUSION_TERMS.items():
        term_pattern = "|".join(re.escape(term) for term in terms)
        pattern = re.compile(
            rf"\b(?:no|without|exclude|excluding|never\s+(?:use|include|enable))\s+"
            rf"(?:\w+\s+){{0,2}}(?:{term_pattern})\b",
            re.IGNORECASE,
        )
        match = pattern.search(text)
        if match:
            claims.append(IntentClaim(
                field=f"capability.{capability}",
                value=False,
                source="user_exclusion",
                confidence=1.0,
                rationale=f"explicit exclusion: {match.group(0)!r}",
            ))
    return claims


def claims_from_classifier(intent: Any, *, min_confidence: int = 3) -> list[IntentClaim]:
    """Convert ``UnityIntent`` scores into evidence without touching schema."""
    out: list[IntentClaim] = []
    axes = (
        ("game_type", "game_type_confidence"),
        ("locomotion", "locomotion_confidence"),
        ("genre", "genre_confidence"),
        ("locomotion_modes", "locomotion_modes_confidence"),
    )
    for field, confidence_field in axes:
        value = getattr(intent, field, None)
        raw = int(getattr(intent, confidence_field, 0) or 0)
        if value in (None, "", []):
            continue
        confidence = min(1.0, raw / max(min_confidence, 1))
        hits = list(getattr(intent, "matched", {}).get(field, []))
        out.append(IntentClaim(
            field=field,
            value=list(value) if isinstance(value, list) else value,
            source="keyword_classifier",
            confidence=confidence,
            rationale=("matched: " + ", ".join(hits)) if hits else "classifier default",
        ))
    flying = list(getattr(intent, "flying_abilities", []) or [])
    if flying:
        hits = list(getattr(intent, "matched", {}).get("flying_abilities", []))
        out.append(IntentClaim(
            field="flying_abilities",
            value=flying,
            source="keyword_classifier",
            confidence=1.0,
            rationale=("matched: " + ", ".join(hits)) if hits else "flying ability evidence",
        ))
    return out


def claims_from_schema(schema: Any, *, source: str) -> list[IntentClaim]:
    """Register the schema fill as evidence before canonical resolution."""
    out = [
        IntentClaim("game_type", schema.game_type, source, 0.70, "schema fill"),
        IntentClaim(
            "locomotion", schema.locomotion, source,
            1.0 if schema.locomotion == "none" else 0.70,
            "explicit non-embodied declaration" if schema.locomotion == "none" else "schema fill",
        ),
    ]
    meta = schema.meta if isinstance(schema.meta, dict) else {}
    genre = meta.get("genre")
    if genre:
        out.append(IntentClaim("genre", genre, source, 0.70, "schema metadata"))
    if schema.locomotion_modes:
        out.append(IntentClaim(
            "locomotion_modes", list(schema.locomotion_modes), source, 1.0,
            "explicit schema registry",
        ))
    flying = meta.get("flying_abilities")
    if isinstance(flying, list) and flying:
        out.append(IntentClaim(
            "flying_abilities", list(flying), source, 1.0,
            "explicit schema metadata",
        ))
    return out


def projectile_claims(game_type: str, prompt: str, contract: dict[str, Any] | None) -> Iterable[IntentClaim]:
    """Register projectile detection as intent evidence, never schema mutation."""
    if contract is not None:
        yield IntentClaim(
            "projectile_contract", contract, "projectile_classifier", 0.95,
            f"explicit physical projectile language for {game_type}",
        )
