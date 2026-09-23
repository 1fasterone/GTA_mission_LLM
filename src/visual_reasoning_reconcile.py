"""Deterministic Slice 3 reconciliation for the image-led compiler.

Slice 2 is allowed to interpret pixels; Slice 3 is not.  This module only
validates recorded artifacts, checks their hashes, and reconciles the explicit
entity-reference block in Concept.txt against the visual inventory.  It never
calls an LLM, performs a semantic search, or guesses a missing entity.
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from .visual_reasoning_compiler import (
    INVENTORY_FORMAT,
    REASONING_FORMAT,
    VisualReasoningCompilerError,
    extract_visible_entity_references,
    sha256_bytes,
    sha256_file,
    validate_visual_inventory,
)


CLOSED_FORMAT = "visual-reasoning-compiler/closed-image-ir/v1"
RECONCILIATION_FORMAT = "visual-reasoning-compiler/reconciliation/v1"
NON_ASSET_CATEGORIES = frozenset({"ui", "effect"})
GENERIC_TERMS = frozenset({
    "asset", "background", "character", "element", "environment", "effect",
    "entity", "game", "item", "object", "player", "prop", "scene", "thing",
    "vehicle", "world",
})


def _read_json(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    if not path.is_file():
        raise VisualReasoningCompilerError(f"{label} not found: {path}")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VisualReasoningCompilerError(
            f"{label} is not valid UTF-8 JSON: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise VisualReasoningCompilerError(f"{label} must contain a JSON object: {path}")
    return payload, raw


def _require_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VisualReasoningCompilerError(f"{label} must be an object")
    return value


def _verify_file_record(path: Path, record: Any, *, label: str) -> str:
    record_dict = _require_mapping(record, label=f"{label} manifest record")
    expected = record_dict.get("sha256")
    if not isinstance(expected, str) or not expected:
        raise VisualReasoningCompilerError(f"{label} manifest record has no SHA-256")
    actual = sha256_file(path)
    if actual != expected:
        raise VisualReasoningCompilerError(
            f"{label} hash does not match the Slice-2 manifest: {path}"
        )
    return actual


def _normalise_terms(value: str) -> set[str]:
    folded = unicodedata.normalize("NFKC", value).casefold()
    terms: set[str] = set()
    for token in re.findall(r"[a-z0-9]+", folded):
        if len(token) > 5 and token.endswith("ies"):
            token = token[:-3] + "y"
        elif len(token) > 5 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        if token and token not in GENERIC_TERMS:
            terms.add(token)
    return terms


def _best_inventory_match(reference: str,
                          elements: list[dict[str, Any]]) -> str | None:
    reference_terms = _normalise_terms(reference)
    if not reference_terms:
        return None
    candidates: list[tuple[bool, float, float, int, str]] = []
    for element in elements:
        label = str(element["label"])
        sources = [
            label,
            str(element.get("model_hint") or ""),
            str(element.get("description") or ""),
        ]
        for rank, source in enumerate(sources):
            source_terms = _normalise_terms(source)
            overlap = reference_terms & source_terms
            if not overlap:
                continue
            coverage = len(overlap) / len(reference_terms)
            # One distinctive shared token is enough only when it is an exact
            # full label.  Otherwise require at least half of the reference's
            # non-generic terms so "orb" cannot silently bind to any object.
            exact_label = _normalise_terms(reference) == _normalise_terms(label)
            if not exact_label and coverage < 0.5:
                continue
            candidates.append((exact_label, coverage, len(overlap), -rank, label))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    best = candidates[0]
    ties = [candidate for candidate in candidates if candidate[:4] == best[:4]]
    if len({candidate[4] for candidate in ties}) != 1:
        return None
    return best[4]


def _assert_output_paths(output: Path, manifest: Path) -> None:
    if output.resolve() == manifest.resolve():
        raise VisualReasoningCompilerError(
            f"Slice-3 output paths collide: {output}"
        )
    for path in (output, manifest):
        if path.exists():
            raise VisualReasoningCompilerError(
                f"refusing to overwrite visual-reasoning artifact: {path}"
            )


def reconcile_image_reasoning(*, inventory_file: Path, concept_file: Path,
                              reasoning_manifest: Path, output: Path,
                              manifest: Path) -> Path:
    """Close the image-derived game/asset IR without an LLM."""
    _assert_output_paths(output, manifest)
    reasoning, reasoning_raw = _read_json(
        reasoning_manifest, label="Slice-2 reasoning manifest"
    )
    if reasoning.get("format") != REASONING_FORMAT:
        raise VisualReasoningCompilerError(
            f"Slice-2 reasoning manifest format must be {REASONING_FORMAT!r}"
        )
    if reasoning.get("stage") != "2_image_reasoning":
        raise VisualReasoningCompilerError(
            "Slice-2 reasoning manifest has the wrong stage"
        )
    authority = _require_mapping(reasoning.get("authority"), label="authority")
    image_only_boundary = (
        authority.get("original_vizconcept_sent_to_llm") is False
        and authority.get("external_semantic_context_sent_to_llm", False) is False
        and (
            authority.get("semantic_context_sent_to_llm") is False
            or (
                authority.get("semantic_context_sent_to_llm") is True
                and authority.get("semantic_context_source")
                == "validated_image_inventory_labels"
            )
        )
    )
    if not image_only_boundary:
        raise VisualReasoningCompilerError(
            "Slice-2 manifest does not prove an image-derived input boundary"
        )
    board = _require_mapping(reasoning.get("board"), label="board")
    board_hash = board.get("sha256")
    if not isinstance(board_hash, str) or not board_hash:
        raise VisualReasoningCompilerError("Slice-2 manifest has no board hash")

    inventory_payload, _ = _read_json(
        inventory_file, label="visual inventory"
    )
    inventory = validate_visual_inventory(inventory_payload)
    outputs = _require_mapping(reasoning.get("outputs"), label="outputs")
    inventory_hash = _verify_file_record(
        inventory_file, outputs.get("visual_inventory"), label="visual inventory"
    )

    if not concept_file.is_file():
        raise VisualReasoningCompilerError(f"Concept.txt not found: {concept_file}")
    concept_raw = concept_file.read_bytes()
    try:
        concept_text = concept_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VisualReasoningCompilerError(
            f"Concept.txt must be UTF-8: {concept_file}"
        ) from exc
    if not concept_text.strip():
        raise VisualReasoningCompilerError("Concept.txt is empty")
    concept_hash = _verify_file_record(
        concept_file, outputs.get("concept"), label="Concept.txt"
    )
    references, prose = extract_visible_entity_references(concept_text)

    elements = inventory["visible_elements"]
    elements_by_label = {str(element["label"]): element for element in elements}
    matched: list[dict[str, str]] = []
    errors: list[str] = []
    for reference in references:
        label = _best_inventory_match(reference, elements)
        if label is None:
            errors.append(
                f"Concept entity reference {reference!r} has no unique visual "
                "inventory match (unsupported or ambiguous entity)"
            )
            continue
        matched.append({"reference": reference, "inventory_label": label})

    query_labels = {
        str(query["label"]) for query in inventory["sketchfab_queries"]
    }
    for query in inventory["sketchfab_queries"]:
        label = str(query["label"])
        element = elements_by_label[label]
        if element["category"] in NON_ASSET_CATEGORIES:
            errors.append(
                f"Sketchfab query {label!r} targets non-asset category "
                f"{element['category']!r}"
            )
        if element.get("model_hint") is None:
            errors.append(
                f"Sketchfab query {label!r} has no visual model hint"
            )

    required_labels = {
        str(element["label"])
        for element in elements
        if str(element["label"]) in query_labels
        or (element["category"] not in NON_ASSET_CATEGORIES
            and element.get("model_hint") is not None)
    }
    matched_labels = {entry["inventory_label"] for entry in matched}
    for label in sorted(required_labels - matched_labels):
        errors.append(
            f"asset-worthy inventory element {label!r} is omitted from "
            "Concept.txt entity references"
        )

    if errors:
        raise VisualReasoningCompilerError(
            "Slice-3 reconciliation rejected:\n- " + "\n- ".join(errors)
        )

    optional_unreferenced = sorted(
        set(elements_by_label) - matched_labels - required_labels
    )
    asset_requests = []
    for query in inventory["sketchfab_queries"]:
        label = str(query["label"])
        reference = next(
            entry["reference"] for entry in matched
            if entry["inventory_label"] == label
        )
        asset_requests.append({
            "inventory_label": label,
            "query": str(query["query"]),
            "concept_reference": reference,
        })

    inputs = {
        "reasoning_manifest": {
            "path": str(reasoning_manifest),
            "sha256": sha256_bytes(reasoning_raw),
        },
        "visual_inventory": {
            "path": str(inventory_file),
            "sha256": inventory_hash,
        },
        "concept": {
            "path": str(concept_file),
            "sha256": concept_hash,
        },
    }
    fingerprint_payload = {
        "board_sha256": board_hash,
        "inventory_sha256": inventory_hash,
        "concept_sha256": concept_hash,
        "references": matched,
        "asset_requests": asset_requests,
    }
    closed_data: dict[str, Any] = {
        "format": CLOSED_FORMAT,
        "stage": "3_image_reasoning_reconciliation",
        "status": "closed",
        "authority": {
            "llm_called_here": False,
            "visual_support_source": "validated_slice_2_inventory",
            "concept_source": "validated_slice_2_concept",
        },
        "board": board,
        "inputs": inputs,
        "inventory": inventory,
        "concept": {
            "text": concept_text,
            "prose": prose,
            "entity_references": references,
        },
        "entity_matches": matched,
        "asset_requests": asset_requests,
        "coverage": {
            "required_inventory_labels": sorted(required_labels),
            "covered_inventory_labels": sorted(matched_labels),
            "optional_unreferenced_labels": optional_unreferenced,
        },
        "warnings": [
            "Only explicit Concept.txt entity references are reconciled; "
            "generic prose is not treated as a hidden entity channel.",
            *[
                f"Visible non-asset element {label!r} was not referenced."
                for label in optional_unreferenced
            ],
        ],
        "request_fingerprint": sha256_bytes(
            json.dumps(
                fingerprint_payload, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ),
    }
    closed_bytes = (
        json.dumps(closed_data, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    manifest_data = {
        "format": RECONCILIATION_FORMAT,
        "stage": "3_image_reasoning_reconciliation",
        "status": "closed",
        "authority": closed_data["authority"],
        "board": board,
        "inputs": inputs,
        "request_fingerprint": closed_data["request_fingerprint"],
        "outputs": {
            "closed_image_ir": {
                "path": str(output),
                "sha256": sha256_bytes(closed_bytes),
            }
        },
        "coverage": closed_data["coverage"],
        "warnings": closed_data["warnings"],
    }
    for path in (output, manifest):
        path.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(closed_bytes)
    manifest.write_text(
        json.dumps(manifest_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest
