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
from copy import deepcopy
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional


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

_ATTRIBUTE_FIELDS = {
    "identity": frozenset({
        "era", "culture", "species", "class", "profession", "faction",
        "alignment",
    }),
    "visual": frozenset({
        "clothing_style", "clothing_condition", "equipment", "body_traits",
        "posture", "dirt_level",
    }),
    "behavioral": frozenset({
        "temperament", "hostility", "confidence", "work_style", "social_role",
    }),
    "environment": frozenset({
        "location_type", "weather_adaptation", "terrain_adaptation",
    }),
}
_ATTRIBUTE_LIST_FIELDS = frozenset({
    ("visual", "equipment"),
    ("visual", "body_traits"),
})
_AFFORDANCE_ALIASES = {
    "fight": "can_fight",
    "work": "can_work",
    "flee": "can_flee",
    "trade": "can_trade",
    "operate_tools": "can_operate_tools",
    "carry": "can_carry",
    "gather": "can_gather",
    "patrol": "can_patrol",
    "talk": "can_talk",
}
_ALLOWED_AFFORDANCES = frozenset({
    "can_fight", "can_work", "can_flee", "can_trade",
    "can_operate_tools", "can_carry", "can_gather", "can_patrol",
    "can_talk",
})
_CONFIDENCE_RULES = {
    "noun_map": "direct_noun_map",
    "morphology": "morphology_match",
    "context": "context_match",
    "generic": "generic_fallback",
}


@dataclass(frozen=True)
class IdentityArchetype:
    """One identity family, independent of a concrete asset noun."""

    name: str
    family: str
    kind_hints: tuple[str, ...]
    query_tokens: tuple[str, ...]
    attributes: dict[str, Any] = field(default_factory=dict)
    variants: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class IdentityResolution:
    """Auditable result of noun/context → identity resolution."""

    archetype: Optional[IdentityArchetype] = None
    matched_terms: tuple[str, ...] = ()
    rule: str = "no_identity_signal"
    used_fallback: bool = False
    query_tokens: tuple[str, ...] = ()
    attributes: dict[str, Any] = field(default_factory=dict)
    attribute_confidence: dict[str, Any] = field(default_factory=dict)
    selected_variant: Optional[str] = None
    variant_matches: tuple[str, ...] = ()
    confidence: float = 0.0
    confidence_tier: str = "none"
    attribute_evidence: tuple[dict[str, Any], ...] = ()
    attribute_conflicts: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def name(self) -> Optional[str]:
        return self.archetype.name if self.archetype else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "archetype": self.name,
            "matched_terms": list(self.matched_terms),
            "rule": self.rule,
            "used_fallback": self.used_fallback,
            "attributes": deepcopy(self.attributes),
            "attribute_confidence": deepcopy(self.attribute_confidence),
            "selected_variant": self.selected_variant,
            "variant_matches": list(self.variant_matches),
            "confidence": self.confidence,
            "confidence_tier": self.confidence_tier,
            "attribute_evidence": deepcopy(list(self.attribute_evidence)),
            "attribute_conflicts": list(self.attribute_conflicts),
            "warnings": list(self.warnings),
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
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return ()
    return tuple(str(v).strip().lower() for v in values if str(v).strip())


def _canonical_attribute_value(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _normalize_attribute_bundle(raw: Any, *, label: str) -> dict[str, Any]:
    """Validate and canonicalize one lookup-authored attribute bundle."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"attribute bundle {label} must be an object")

    out: dict[str, Any] = {}
    for category, values in raw.items():
        category = str(category).strip().lower()
        if category == "affordances":
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list):
                raise ValueError(f"attribute bundle {label}.affordances must be a list")
            normalized = []
            for value in values:
                token = _canonical_attribute_value(value)
                if not token:
                    continue
                token = _AFFORDANCE_ALIASES.get(token, token)
                if token not in _ALLOWED_AFFORDANCES:
                    raise ValueError(
                        f"attribute bundle {label}.affordances has unknown value {token!r}"
                    )
                if token not in normalized:
                    normalized.append(token)
            out[category] = normalized
            continue
        if category not in _ATTRIBUTE_FIELDS:
            raise ValueError(f"attribute bundle {label} has unknown category {category!r}")
        if not isinstance(values, dict):
            raise ValueError(f"attribute bundle {label}.{category} must be an object")
        normalized_category: dict[str, Any] = {}
        for field_name, value in values.items():
            field_name = str(field_name).strip().lower()
            if field_name not in _ATTRIBUTE_FIELDS[category]:
                raise ValueError(
                    f"attribute bundle {label}.{category} has unknown field {field_name!r}"
                )
            is_list = (category, field_name) in _ATTRIBUTE_LIST_FIELDS
            if is_list:
                if isinstance(value, str):
                    value = [value]
                if not isinstance(value, list):
                    raise ValueError(
                        f"attribute bundle {label}.{category}.{field_name} must be a list"
                    )
                normalized_value = []
                for item in value:
                    token = _canonical_attribute_value(item)
                    if token and token not in normalized_value:
                        normalized_value.append(token)
            else:
                if isinstance(value, (dict, list, tuple)):
                    raise ValueError(
                        f"attribute bundle {label}.{category}.{field_name} must be scalar"
                    )
                normalized_value = _canonical_attribute_value(value)
                if not normalized_value:
                    continue
            normalized_category[field_name] = normalized_value
        if normalized_category:
            out[category] = normalized_category
    return out


def _normalize_variants(raw: Any, *, label: str) -> dict[str, dict[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"attribute variants {label} must be an object")
    variants: dict[str, dict[str, Any]] = {}
    for variant_name, spec in raw.items():
        variant_name = _canonical_attribute_value(variant_name)
        if not variant_name or not isinstance(spec, dict):
            raise ValueError(f"attribute variant {label}.{variant_name!r} is invalid")
        context_any = _safe_tuple(spec.get("context_any"))
        try:
            priority = int(spec.get("priority", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"attribute variant {label}.{variant_name}.priority is invalid"
            ) from exc
        attributes = spec.get("attributes")
        if attributes is None:
            attributes = {
                key: value for key, value in spec.items()
                if key not in {"context_any", "priority"}
            }
        variants[variant_name] = {
            "context_any": context_any,
            "priority": priority,
            "attributes": _normalize_attribute_bundle(
                attributes, label=f"{label}.{variant_name}"
            ),
        }
    return variants


def _confidence_value(tiers: dict[str, float], rule: str) -> tuple[str, float]:
    prefix = next((key for key in _CONFIDENCE_RULES if rule.startswith(key)), "context")
    tier_name = _CONFIDENCE_RULES[prefix]
    return tier_name, float(tiers.get(tier_name, 0.0))


def _confidence_shape(attributes: dict[str, Any], score: float) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for category, values in attributes.items():
        if isinstance(values, dict):
            result[category] = {field_name: score for field_name in values}
        else:
            result[category] = score
    return result


def _merge_attributes(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for category, values in override.items():
        if isinstance(values, dict):
            result.setdefault(category, {}).update(deepcopy(values))
        else:
            result[category] = deepcopy(values)
    return result


def _select_variant(
    variants: dict[str, dict[str, Any]],
    context_tokens: tuple[str, ...],
) -> tuple[Optional[str], tuple[str, ...], dict[str, Any]]:
    candidates: list[tuple[int, int, int, str, tuple[str, ...], dict[str, Any]]] = []
    for name, spec in variants.items():
        matched = tuple(
            term for term in spec["context_any"]
            if _phrase_present(context_tokens, term)
        )
        if not matched:
            continue
        candidates.append((
            int(spec["priority"]),
            len(matched),
            max((len(_tokens(term)) for term in matched), default=0),
            name,
            tuple(sorted(set(matched))),
            spec,
        ))
    if not candidates:
        return None, (), {}
    ranked = sorted(candidates, key=lambda item: item[:4], reverse=True)
    best = ranked[0]
    tied = [item for item in ranked if item[:3] == best[:3]]
    if len(tied) > 1:
        return None, tuple(sorted(item[3] for item in tied)), {}
    return best[3], best[4], best[5]


def _apply_visual_rules(
    attributes: dict[str, Any],
    attribute_confidence: dict[str, Any],
    visual_tokens: tuple[str, ...],
    rules: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], dict[str, Any], tuple[dict[str, Any], ...], tuple[str, ...]]:
    evidence: list[dict[str, Any]] = []
    conflicts: list[str] = []
    for rule in rules:
        matched = tuple(
            term for term in rule["visual_any"]
            if _phrase_present(visual_tokens, term)
        )
        if not matched:
            continue
        for category, values in rule["attributes"].items():
            if isinstance(values, dict):
                target = attributes.setdefault(category, {})
                confidence_target = attribute_confidence.setdefault(category, {})
                for field_name, value in values.items():
                    if (category, field_name) in _ATTRIBUTE_LIST_FIELDS and field_name in target:
                        # Visual evidence can corroborate or add an observed
                        # item to an existing equipment/body-trait list, but
                        # an explicitly empty variant list remains empty.
                        if target[field_name]:
                            for item in value:
                                if item not in target[field_name]:
                                    target[field_name].append(item)
                            confidence_target[field_name] = max(
                                confidence_target.get(field_name, 0.0),
                                rule["confidence"],
                            )
                        continue
                    if field_name in target and target[field_name] != value:
                        conflicts.append(f"{category}.{field_name}")
                        continue
                    target.setdefault(field_name, deepcopy(value))
                    confidence_target.setdefault(field_name, rule["confidence"])
            else:
                if category in attributes and attributes[category] != values:
                    conflicts.append(category)
                    continue
                attributes.setdefault(category, deepcopy(values))
                attribute_confidence.setdefault(category, rule["confidence"])
        evidence.append({
            "rule": rule["name"],
            "matched_terms": list(sorted(set(matched))),
            "confidence": rule["confidence"],
        })
    return attributes, attribute_confidence, tuple(evidence), tuple(sorted(set(conflicts)))


def _load_raw_catalog(lookups_dir: str) -> Optional[dict[str, Any]]:
    path = Path(lookups_dir) / _LOOKUP_RELATIVE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid identity archetype lookup {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"identity archetype lookup {path} must be an object")
    return raw


@lru_cache(maxsize=8)
def _load_identity_catalog_details(lookups_dir: str) -> tuple[
        dict[str, IdentityArchetype], dict[str, str], tuple[dict[str, Any], ...],
        dict[str, float], tuple[dict[str, Any], ...]]:
    raw = _load_raw_catalog(lookups_dir)
    if raw is None:
        return {}, {}, (), {}, ()

    path = Path(lookups_dir) / _LOOKUP_RELATIVE
    bundles: dict[str, dict[str, Any]] = {}
    raw_bundles = raw.get("attribute_bundles", {})
    if not isinstance(raw_bundles, dict):
        raise ValueError(f"attribute_bundles in {path} must be an object")
    for bundle_name, spec in raw_bundles.items():
        bundle_name = _canonical_attribute_value(bundle_name)
        if not bundle_name or not isinstance(spec, dict):
            raise ValueError(f"attribute bundle entry in {path} is invalid")
        bundles[bundle_name] = {
            "attributes": _normalize_attribute_bundle(
                spec.get("attributes", {}), label=bundle_name
            ),
            "variants": _normalize_variants(
                spec.get("variants", {}), label=bundle_name
            ),
        }
    bundle_refs = raw.get("archetype_attribute_bundles", {})
    if not isinstance(bundle_refs, dict):
        raise ValueError(f"archetype_attribute_bundles in {path} must be an object")

    archetypes: dict[str, IdentityArchetype] = {}
    for item in raw.get("archetypes", []):
        if not isinstance(item, dict):
            raise ValueError(f"identity archetype entry in {path} is not an object")
        name = str(item.get("name") or "").strip()
        if not name or name in archetypes:
            raise ValueError(f"identity archetype name is missing or duplicated in {path}")
        bundle_name = _canonical_attribute_value(
            item.get("attribute_bundle") or bundle_refs.get(name, "")
        )
        if bundle_name and bundle_name not in bundles:
            raise ValueError(
                f"identity archetype {name!r} references unknown attribute bundle {bundle_name!r}"
            )
        bundle = bundles.get(bundle_name, {"attributes": {}, "variants": {}})
        archetypes[name] = IdentityArchetype(
            name=name,
            family=str(item.get("family") or "").strip().lower(),
            kind_hints=_safe_tuple(item.get("kind_hints")),
            query_tokens=_safe_tuple(item.get("query_tokens")),
            attributes=deepcopy(bundle["attributes"]),
            variants=deepcopy(bundle["variants"]),
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

    raw_tiers = raw.get("confidence_tiers", {})
    if not isinstance(raw_tiers, dict):
        raise ValueError(f"confidence_tiers in {path} must be an object")
    confidence_tiers: dict[str, float] = {}
    for name, value in raw_tiers.items():
        try:
            score = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"confidence tier {name!r} in {path} is invalid") from exc
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"confidence tier {name!r} in {path} is outside [0, 1]")
        confidence_tiers[str(name)] = score

    visual_rules: list[dict[str, Any]] = []
    for raw_rule in raw.get("attribute_rules", []):
        if not isinstance(raw_rule, dict):
            raise ValueError(f"attribute rule in {path} is not an object")
        name = str(raw_rule.get("name") or "").strip()
        if not name:
            raise ValueError(f"attribute rule in {path} has no name")
        visual_any = _safe_tuple(raw_rule.get("visual_any"))
        if not visual_any:
            raise ValueError(f"attribute rule {name!r} in {path} has no visual_any")
        try:
            confidence = float(raw_rule.get("confidence", 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"attribute rule {name!r} has invalid confidence") from exc
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"attribute rule {name!r} confidence is outside [0, 1]")
        attributes = _normalize_attribute_bundle(
            raw_rule.get("attributes", {}), label=f"rule:{name}"
        )
        if not attributes:
            raise ValueError(f"attribute rule {name!r} has no attributes")
        visual_rules.append({
            "name": name,
            "visual_any": visual_any,
            "confidence": confidence,
            "attributes": attributes,
        })

    return archetypes, noun_map, tuple(rules), confidence_tiers, tuple(visual_rules)


@lru_cache(maxsize=8)
def load_identity_catalog(lookups_dir: str = "lookups") -> tuple[
        dict[str, IdentityArchetype], dict[str, str], tuple[dict[str, Any], ...]]:
    """Load and validate the identity vocabulary.

    Missing lookup data is an empty catalog rather than a guessed catalog.  The
    normal repository path is tracked, so malformed checked-in data fails
    loudly at the compiler boundary instead of silently changing identity.
    """
    return _load_identity_catalog_details(str(lookups_dir))[:3]


@lru_cache(maxsize=8)
def load_identity_confidence_tiers(lookups_dir: str = "lookups") -> dict[str, float]:
    """Return validated confidence tiers without changing the catalog API."""
    return dict(_load_identity_catalog_details(str(lookups_dir))[3])


@lru_cache(maxsize=8)
def load_attribute_rules(lookups_dir: str = "lookups") -> tuple[dict[str, Any], ...]:
    """Return validated visual-token → attribute rules."""
    return _load_identity_catalog_details(str(lookups_dir))[4]


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
                     visual_tokens: Iterable[str] | str = (),
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
    confidence_tiers = load_identity_confidence_tiers(str(lookups_dir))
    attribute_rules = load_attribute_rules(str(lookups_dir))

    name_tokens = _tokens(name)
    hint_tokens = _tokens(model_hint)
    kind_tokens = _tokens(kind)
    primary_tokens = hint_tokens + name_tokens
    context_tokens = _tokens(f"{context} {genre}")
    if isinstance(visual_tokens, str):
        visual_tokens = _tokens(visual_tokens)
    else:
        visual_tokens = tuple(
            token for value in visual_tokens for token in _tokens(str(value))
        )

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
        rule = "noun_map"
        matched_terms = (noun,)
        used_fallback = False
    else:
        scope = _scope_for(kind, primary_tokens, noun_map, archetypes)
        if scope is None:
            return IdentityResolution()

        identity = None
        rule = "no_identity_signal"
        matched_terms: tuple[str, ...] = ()
        used_fallback = True

        # Context/primary rules are more specific than morphology rules,
        # regardless of their order in the editable catalog. This keeps a
        # modern factory worker from being captured by a generic -er rule.
        for fallback_rule in rules:
            if fallback_rule["scope"] != scope:
                continue
            if not (fallback_rule["primary_any"] or fallback_rule["context_any"]):
                continue
            matched = matching_rule(fallback_rule)
            if matched is None:
                continue
            identity = archetypes[fallback_rule["archetype"]]
            rule = fallback_rule["name"]
            matched_terms = matched
            break

        if identity is None:
            for fallback_rule in rules:
                if fallback_rule["scope"] != scope or not fallback_rule["match_suffix"]:
                    continue
                matched = matching_rule(fallback_rule)
                if matched is None:
                    continue
                identity = archetypes[fallback_rule["archetype"]]
                rule = fallback_rule["name"]
                matched_terms = matched
                break

        if identity is None:
            # The lookup's final family rules are deliberately explicit. A
            # shallow noun gets a stable identity, and the provenance says it
            # was a fallback.
            for fallback_rule in rules:
                if (fallback_rule["scope"] != scope or fallback_rule["primary_any"] or
                        fallback_rule["context_any"] or fallback_rule["match_suffix"]):
                    continue
                identity = archetypes[fallback_rule["archetype"]]
                rule = fallback_rule["name"]
                matched_terms = tuple(sorted(set(primary_tokens) | set(kind_tokens)))
                break

        if identity is None:
            return IdentityResolution()

    confidence_tier, confidence = _confidence_value(confidence_tiers, rule)
    attributes = deepcopy(identity.attributes)
    attribute_confidence = _confidence_shape(attributes, confidence)
    selected_variant, variant_matches, variant = _select_variant(
        identity.variants, context_tokens
    )
    warnings: list[str] = []
    if variant_matches and selected_variant is None:
        warnings.append(
            "attribute variant tie: " + ", ".join(variant_matches)
        )
    if selected_variant is not None:
        overrides = variant.get("attributes", {})
        attributes = _merge_attributes(attributes, overrides)
        variant_confidence = float(confidence_tiers.get("context_match", confidence))
        for category, values in overrides.items():
            if isinstance(values, dict):
                target = attribute_confidence.setdefault(category, {})
                for field_name in values:
                    target[field_name] = min(confidence, variant_confidence)
            else:
                attribute_confidence[category] = min(confidence, variant_confidence)

    attributes, attribute_confidence, attribute_evidence, attribute_conflicts = (
        _apply_visual_rules(
            attributes,
            attribute_confidence,
            visual_tokens,
            attribute_rules,
        )
    )
    if attribute_conflicts:
        warnings.append(
            "visual attribute conflict: " + ", ".join(attribute_conflicts)
        )
    return IdentityResolution(
        archetype=identity,
        matched_terms=matched_terms,
        rule=rule,
        used_fallback=used_fallback,
        query_tokens=identity.query_tokens,
        attributes=attributes,
        attribute_confidence=attribute_confidence,
        selected_variant=selected_variant,
        variant_matches=variant_matches,
        confidence=confidence,
        confidence_tier=confidence_tier,
        attribute_evidence=attribute_evidence,
        attribute_conflicts=attribute_conflicts,
        warnings=tuple(warnings),
    )


__all__ = [
    "IdentityArchetype",
    "IdentityResolution",
    "load_attribute_rules",
    "load_identity_catalog",
    "load_identity_confidence_tiers",
    "resolve_identity",
]
