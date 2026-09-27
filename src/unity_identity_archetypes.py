"""Deterministic identity archetypes for Unity entity semantics.

An identity archetype is a semantic family such as ``medieval_human_knight``
or ``vehicle_boat``.  It is deliberately not a model noun: ``knight`` and
``paladin`` can resolve to the same identity, while a shallow noun such as
``Unit`` can inherit an identity from its kind and scene context.

This module is post-schema compiler logic.  It performs no model call and does
not add fields to ``EntitySpec``.  The lookup file is the editable vocabulary;
the resolver returns provenance so callers can audit whether the identity came
from an explicit noun or a fallback rule.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional


_LOOKUP_RELATIVE = Path("unity") / "identity_archetypes.json"
_HUMAN_KINDS = frozenset({
    "character", "npc", "crowd", "human", "person", "civilian",
    "enemy", "player", "boss", "ped",
})
_VEHICLE_KINDS = frozenset({"vehicle", "car", "boat", "ship", "craft"})
_CREATURE_KINDS = frozenset({"creature", "animal", "beast", "monster"})
_OBJECT_KINDS = frozenset({
    "prop", "object", "obstacle", "pickup", "item", "collectible",
    "decoration", "building", "ground", "road", "track",
})
_SHALLOW_HUMAN_TOKENS = frozenset({
    "human", "person", "people", "character", "npc", "figure", "citizen",
    "worker", "staff", "employee", "unit", "ped", "pedestrian",
})
_SHALLOW_VEHICLE_TOKENS = frozenset({"vehicle", "car", "transport", "craft"})
_SHALLOW_CREATURE_TOKENS = frozenset({"creature", "animal", "beast", "monster"})


@dataclass(frozen=True)
class IdentityArchetype:
    """One identity family, independent of a concrete asset noun."""

    name: str
    family: str
    kind_hints: tuple[str, ...]
    query_tokens: tuple[str, ...]


@dataclass(frozen=True)
class IdentityResolution:
    """Auditable result of noun/context → identity resolution."""

    archetype: Optional[IdentityArchetype] = None
    matched_terms: tuple[str, ...] = ()
    rule: str = "no_identity_signal"
    used_fallback: bool = False
    query_tokens: tuple[str, ...] = ()

    @property
    def name(self) -> Optional[str]:
        return self.archetype.name if self.archetype else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "archetype": self.name,
            "matched_terms": list(self.matched_terms),
            "rule": self.rule,
            "used_fallback": self.used_fallback,
        }


def _tokens(text: str) -> tuple[str, ...]:
    """Split prose and Pascal/camel case into stable lowercase tokens."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text or "")
    return tuple(t for t in re.split(r"[^a-zA-Z0-9]+", spaced.lower()) if t)


def _phrase_present(tokens: tuple[str, ...], phrase: str) -> bool:
    wanted = _tokens(phrase)
    if not wanted or len(wanted) > len(tokens):
        return False
    width = len(wanted)
    return any(tokens[i:i + width] == wanted for i in range(len(tokens) - width + 1))


def _safe_tuple(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    return tuple(str(v).strip().lower() for v in values if str(v).strip())


@lru_cache(maxsize=8)
def load_identity_catalog(lookups_dir: str = "lookups") -> tuple[
        dict[str, IdentityArchetype], dict[str, str], tuple[dict[str, Any], ...]]:
    """Load and validate the identity vocabulary.

    Missing lookup data is an empty catalog rather than a guessed catalog.  The
    normal repository path is tracked, so malformed checked-in data fails
    loudly at the compiler boundary instead of silently changing identity.
    """
    path = Path(lookups_dir) / _LOOKUP_RELATIVE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, {}, ()
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid identity archetype lookup {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"identity archetype lookup {path} must be an object")

    archetypes: dict[str, IdentityArchetype] = {}
    for item in raw.get("archetypes", []):
        if not isinstance(item, dict):
            raise ValueError(f"identity archetype entry in {path} is not an object")
        name = str(item.get("name") or "").strip()
        if not name or name in archetypes:
            raise ValueError(f"identity archetype name is missing or duplicated in {path}")
        archetypes[name] = IdentityArchetype(
            name=name,
            family=str(item.get("family") or "").strip().lower(),
            kind_hints=_safe_tuple(item.get("kind_hints")),
            query_tokens=_safe_tuple(item.get("query_tokens")),
        )

    noun_map = {
        str(noun).strip().lower(): str(target).strip()
        for noun, target in (raw.get("noun_map") or {}).items()
        if str(noun).strip()
    }
    unknown = sorted({target for target in noun_map.values() if target not in archetypes})
    if unknown:
        raise ValueError(f"identity noun map in {path} references unknown archetypes: {unknown}")

    rules: list[dict[str, Any]] = []
    for rule in raw.get("fallback_rules", []):
        if not isinstance(rule, dict):
            raise ValueError(f"identity fallback rule in {path} is not an object")
        target = str(rule.get("archetype") or "").strip()
        if target not in archetypes:
            raise ValueError(f"identity fallback rule in {path} references {target!r}")
        scope = str(rule.get("scope") or "").strip().lower()
        if not scope:
            family = archetypes[target].family
            if family in {"human", "vehicle", "creature", "object"}:
                scope = family
        rules.append({
            **rule,
            "name": str(rule.get("name") or "unnamed_rule"),
            "archetype": target,
            "scope": scope,
            "primary_any": _safe_tuple(rule.get("primary_any")),
            "context_any": _safe_tuple(rule.get("context_any")),
            "match_suffix": _safe_tuple(rule.get("match_suffix")),
        })
    return archetypes, noun_map, tuple(rules)


def _scope_for(kind: str, primary_tokens: tuple[str, ...], noun_map: dict[str, str], archetypes: dict[str, IdentityArchetype]) -> Optional[str]:
    kind = kind.lower()
    primary = set(primary_tokens)
    # Kind is the disambiguator for intentionally shallow words such as
    # "Unit". Check it before vocabulary overlap so a character named Unit is
    # not mistaken for a vehicle merely because another family can also use
    # that noun.
    if kind in _VEHICLE_KINDS:
        return "vehicle"
    if kind in _CREATURE_KINDS:
        return "creature"
    if kind in _HUMAN_KINDS:
        return "human"
    if primary & _SHALLOW_VEHICLE_TOKENS:
        return "vehicle"
    if primary & _SHALLOW_CREATURE_TOKENS:
        return "creature"
    if primary & _SHALLOW_HUMAN_TOKENS:
        return "human"
    # A distinctive mapped noun also supplies its family, even if a weak schema
    # kind says only "prop". This is useful for visual inventories that retain
    # the noun but lose the original entity kind.
    for noun, target in noun_map.items():
        if _phrase_present(primary_tokens, noun):
            family = archetypes[target].family
            if family in {"human", "vehicle", "creature", "object"}:
                return family
    if kind in _OBJECT_KINDS:
        return "object"
    return None


def resolve_identity(*, name: str = "", kind: str = "", model_hint: str = "",
                     context: str = "", genre: str = "",
                     lookups_dir: str = "lookups") -> IdentityResolution:
    """Resolve an entity into an identity-bearing archetype.

    Precedence is intentionally explicit:

    1. distinctive noun in ``model_hint`` / entity ``name`` (longest phrase,
       then model hint before name),
    2. context-aware fallback for shallow human/vehicle/creature nouns,
    3. family fallback from the entity kind.

    ``context`` is evidence only; it cannot override a distinctive noun.  The
    sketchfab query is intentionally not an input because it is a marketplace
    search string, not an assertion of what the entity is.
    """
    archetypes, noun_map, rules = load_identity_catalog(str(lookups_dir))
    if not archetypes:
        return IdentityResolution()

    name_tokens = _tokens(name)
    hint_tokens = _tokens(model_hint)
    kind_tokens = _tokens(kind)
    primary_tokens = hint_tokens + name_tokens
    context_tokens = _tokens(f"{context} {genre}")

    def matching_terms(tokens: tuple[str, ...], values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(value for value in values if _phrase_present(tokens, value))

    def matching_rule(rule: dict[str, Any]) -> Optional[tuple[str, ...]]:
        primary_matches = matching_terms(primary_tokens, rule["primary_any"])
        context_matches = matching_terms(context_tokens, rule["context_any"])
        suffix_matches = tuple(
            token for token in primary_tokens
            if token != "human" and any(
                token.endswith(suffix) for suffix in rule["match_suffix"]
            )
        )
        if rule["primary_any"] and not primary_matches:
            return None
        if rule["context_any"] and not context_matches:
            return None
        if rule["match_suffix"] and not suffix_matches:
            return None
        if not (rule["primary_any"] or rule["context_any"] or rule["match_suffix"]):
            return None
        return tuple(sorted(set(primary_matches) | set(context_matches) | set(suffix_matches)))

    # Distinctive noun mappings are the strongest identity evidence. Prefer a
    # longer phrase, then model_hint, then the stable lexical target/name tie.
    matches: list[tuple[int, int, str, str]] = []
    for noun, target in noun_map.items():
        noun_len = len(_tokens(noun))
        if _phrase_present(hint_tokens, noun):
            matches.append((noun_len, 2, noun, target))
        if _phrase_present(name_tokens, noun):
            matches.append((noun_len, 1, noun, target))
    if matches:
        _, _, noun, target = max(matches, key=lambda row: (row[0], row[1], row[2], row[3]))
        identity = archetypes[target]
        return IdentityResolution(identity, (noun,), "noun_map", False, identity.query_tokens)

    scope = _scope_for(kind, primary_tokens, noun_map, archetypes)
    if scope is None:
        return IdentityResolution()

    # Context/primary rules are more specific than morphology rules, regardless
    # of their order in the editable catalog. This keeps a medieval worker in a
    # forge on its explicit worker context path rather than a generic -er rule.
    for rule in rules:
        if rule["scope"] != scope:
            continue
        if not (rule["primary_any"] or rule["context_any"]):
            continue
        matched = matching_rule(rule)
        if matched is None:
            continue
        identity = archetypes[rule["archetype"]]
        return IdentityResolution(identity, matched, rule["name"], True, identity.query_tokens)

    for rule in rules:
        if rule["scope"] != scope or not rule["match_suffix"]:
            continue
        matched = matching_rule(rule)
        if matched is None:
            continue
        identity = archetypes[rule["archetype"]]
        return IdentityResolution(identity, matched, rule["name"], True, identity.query_tokens)

    # The lookup's final family rules are deliberately explicit. A shallow
    # noun gets a stable identity, and the provenance says it was a fallback.
    for rule in rules:
        if (rule["scope"] != scope or rule["primary_any"] or
                rule["context_any"] or rule["match_suffix"]):
            continue
        identity = archetypes[rule["archetype"]]
        matched = tuple(sorted(set(primary_tokens) | set(kind_tokens)))
        return IdentityResolution(identity, matched, rule["name"], True, identity.query_tokens)

    return IdentityResolution()


__all__ = [
    "IdentityArchetype",
    "IdentityResolution",
    "load_identity_catalog",
    "resolve_identity",
]
