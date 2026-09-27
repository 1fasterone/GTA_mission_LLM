"""Slice 5: compile a closed visual IR through the Unity pipeline.

Slice 5 is the authority boundary between the image-led front half and the
existing Unity compiler.  It verifies the immutable Slice 3 and Slice 4
artifacts, binds the fulfilled visual selections to schema entities, and
hands the generator the exact catalog records selected by Slice 4.

There is deliberately no search, ranking, network request, or LLM call here.
The only model call in a visual compile is the existing Step-3 schema fill,
using the Concept text carried by the closed image IR as a precomputed Step-2
result.  A missing or ambiguous binding is an abstention, never a catalog
fallback.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .unity_asset_catalog import load_catalog, tokenize
from .visual_asset_fulfilment import FULFILMENT_FORMAT
from .visual_reasoning_compiler import (
    VisualReasoningCompilerError,
    sha256_bytes,
    sha256_file,
)
from .visual_reasoning_reconcile import CLOSED_FORMAT


COMPILE_FORMAT = "visual-reasoning-compiler/compile/v1"
COMPILE_STAGE = "5_visual_compile"
_STOPWORDS = frozenset({
    "a", "an", "and", "asset", "background", "character", "entity",
    "environment", "game", "group", "item", "level", "object", "of",
    "on", "or", "player", "prop", "scene", "the", "thing", "to",
    "vehicle", "world", "with",
})


def _read_json(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    if not path.is_file():
        raise VisualReasoningCompilerError(f"{label} not found: {path}")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VisualReasoningCompilerError(
            f"{label} is not valid UTF-8 JSON: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise VisualReasoningCompilerError(f"{label} must contain a JSON object: {path}")
    return value, raw


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VisualReasoningCompilerError(f"{label} must be an object")
    return value


def render_visual_entity_checklist(plan: dict[str, Any]) -> str:
    """Render the closed Slice-4 nouns as Step-3 entity guidance.

    A visual compile has already closed the image inventory and frozen one
    exact asset selection per asset-worthy noun.  The schema-fill model still
    owns the schema, but it must expose an identity-bearing entity group for
    every frozen noun so Slice 5 can bind the immutable selection without
    guessing through broad groups such as PlayerUnits or EnemyWaves.
    This function exposes only closed visual labels and categories; it never
    authors schema JSON or chooses gameplay behavior.
    """
    closed = _mapping(plan.get("closed"), label="visual compile closed IR")
    inventory = _mapping(closed.get("inventory"), label="visual compile inventory")
    elements = {
        str(element.get("label")): element
        for element in inventory.get("visible_elements", [])
        if isinstance(element, dict) and element.get("label")
    }
    assets = plan.get("assets")
    if not isinstance(assets, list) or not assets:
        raise VisualReasoningCompilerError(
            "visual compile has no fulfilled assets for the Step-3 entity checklist"
        )

    lines = [
        "VISUAL ASSET ENTITY CONTRACT — closed Slice-4 handoff:",
        "The following fulfilled visual nouns each require ONE DISTINCT entity "
        "group in the schema's entities array.",
        "Preserve each noun in that group's PascalCase name and set model_hint "
        "to the same physical noun. Do not collapse distinct lines into broad "
        "groups such as PlayerUnits or EnemyWaves; those names are not valid "
        "bindings for the individual nouns below.",
        "This is an identity/asset-binding requirement, not permission to invent "
        "new gameplay systems. Keep roles, counts, and mechanics grounded in "
        "the Concept; add only the required identity-bearing groups.",
    ]
    for raw_asset in assets:
        asset = _mapping(raw_asset, label="visual compile asset")
        label = str(asset.get("inventory_label") or "").strip()
        element = elements.get(label)
        if not label or element is None:
            raise VisualReasoningCompilerError(
                f"fulfilled visual asset {label!r} is absent from the closed inventory"
            )
        category = str(element.get("category") or "other").strip() or "other"
        hint = str(element.get("model_hint") or label).strip() or label
        lines.append(
            f'  - visual_label="{label}" category="{category}" '
            f'model_hint="{hint}"'
        )
    return "\n".join(lines) + "\n"


def _sha256_record(path: Path, record: Any, *, label: str) -> str:
    entry = _mapping(record, label=f"{label} hash record")
    expected = entry.get("sha256")
    if not isinstance(expected, str) or not expected:
        raise VisualReasoningCompilerError(f"{label} has no SHA-256 record")
    actual = sha256_file(path)
    if actual != expected:
        raise VisualReasoningCompilerError(f"{label} hash does not match: {path}")
    return actual


def _resolve_catalog_file(path_value: Any, *, lookups_dir: Path) -> Path:
    if not isinstance(path_value, str) or not path_value.strip():
        raise VisualReasoningCompilerError("catalog hash record has no path")
    path = Path(path_value)
    if path.is_absolute():
        return path
    candidate = Path.cwd() / path
    if candidate.is_file():
        return candidate
    return lookups_dir / path


def _verify_catalog_hashes(fulfilment: dict[str, Any], *, lookups_dir: Path) -> None:
    inputs = _mapping(fulfilment.get("inputs"), label="Slice-4 inputs")
    records = inputs.get("catalog_files")
    if not isinstance(records, list) or not records:
        raise VisualReasoningCompilerError(
            "Slice-4 fulfilment has no frozen catalog hash records"
        )
    for index, raw in enumerate(records):
        record = _mapping(raw, label=f"Slice-4 catalog_files[{index}]")
        path = _resolve_catalog_file(record.get("path"), lookups_dir=lookups_dir)
        _sha256_record(path, record, label=f"Slice-4 catalog file {path}")


def _safe_asset_path(entry: dict[str, Any], *, asset_root: Path | None) -> Path:
    value = entry.get("relpath") or entry.get("filename")
    if not isinstance(value, str) or not value.strip():
        raise VisualReasoningCompilerError("selected catalog entry has no relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise VisualReasoningCompilerError(
            f"selected catalog path escapes its root: {value!r}"
        )
    root = Path(str(entry.get("root") or asset_root or "Models/FBX format"))
    root = root.resolve()
    source = (root / relative).resolve()
    try:
        source.relative_to(root)
    except ValueError as exc:
        raise VisualReasoningCompilerError(
            f"selected catalog path escapes its root: {value!r}"
        ) from exc
    return source


def _verify_slice_inputs(*, closed: dict[str, Any], closed_raw: bytes,
                         fulfilment: dict[str, Any], fulfilment_raw: bytes,
                         fulfilment_manifest: dict[str, Any],
                         closed_path: Path, fulfilment_path: Path) -> None:
    if (closed.get("format") != CLOSED_FORMAT
            or closed.get("stage") != "3_image_reasoning_reconciliation"
            or closed.get("status") != "closed"):
        raise VisualReasoningCompilerError(
            f"closed image IR must be a closed {CLOSED_FORMAT!r} artifact"
        )
    request_fingerprint = closed.get("request_fingerprint")
    if not isinstance(request_fingerprint, str) or not request_fingerprint:
        raise VisualReasoningCompilerError("closed image IR has no request fingerprint")

    if (fulfilment.get("format") != FULFILMENT_FORMAT
            or fulfilment.get("stage") != "4_asset_fulfilment"
            or fulfilment.get("status") != "fulfilled"):
        raise VisualReasoningCompilerError(
            f"Slice-4 asset fulfilment must be a fulfilled {FULFILMENT_FORMAT!r} artifact"
        )
    if fulfilment.get("closed_request_fingerprint") != request_fingerprint:
        raise VisualReasoningCompilerError(
            "Slice-4 closed_request_fingerprint does not match the closed image IR"
        )
    if fulfilment.get("rejections"):
        raise VisualReasoningCompilerError(
            "Slice-4 contains rejected visual assets; compile requires a fulfilled artifact"
        )

    if (fulfilment_manifest.get("format") != FULFILMENT_FORMAT
            or fulfilment_manifest.get("stage") != "4_asset_fulfilment"
            or fulfilment_manifest.get("status") != "fulfilled"):
        raise VisualReasoningCompilerError("Slice-4 manifest is not a fulfilled artifact")
    if fulfilment_manifest.get("closed_request_fingerprint") != request_fingerprint:
        raise VisualReasoningCompilerError(
            "Slice-4 manifest fingerprint does not match the closed image IR"
        )
    manifest_output = _mapping(
        _mapping(fulfilment_manifest.get("outputs"), label="Slice-4 manifest outputs")
        .get("asset_fulfilment"),
        label="Slice-4 manifest asset_fulfilment output",
    )
    expected_output_hash = manifest_output.get("sha256")
    if expected_output_hash != sha256_bytes(fulfilment_raw):
        raise VisualReasoningCompilerError(
            f"Slice-4 manifest hash does not match {fulfilment_path}"
        )

    fulfilment_inputs = _mapping(fulfilment.get("inputs"), label="Slice-4 inputs")
    closed_record = _mapping(
        fulfilment_inputs.get("closed_image_ir"),
        label="Slice-4 closed_image_ir input",
    )
    if closed_record.get("sha256") != sha256_bytes(closed_raw):
        raise VisualReasoningCompilerError(
            f"Slice-4 input hash does not match {closed_path}"
        )


def _catalog_entry_for_selection(selection: dict[str, Any],
                                 catalog_assets: list[dict[str, Any]]) -> dict[str, Any]:
    source = str(selection.get("source") or "kenney")
    relpath = str(selection.get("relpath") or "")
    filename = str(selection.get("filename") or "")
    candidates = [
        entry for entry in catalog_assets
        if str(entry.get("source") or "kenney") == source
        and str(entry.get("relpath") or entry.get("filename") or "") == relpath
        and str(entry.get("filename") or "") == filename
    ]
    if len(candidates) != 1:
        raise VisualReasoningCompilerError(
            f"Slice-4 selection {filename!r} is not a unique current catalog entry"
        )
    return candidates[0]


def load_visual_compile_plan(*, closed_ir: Path, asset_fulfilment: Path,
                             asset_manifest: Path, lookups_dir: str = "lookups",
                             asset_root: Path | None = None) -> dict[str, Any]:
    """Verify Slice 3/4 artifacts and return an exact, generator-ready plan."""
    closed, closed_raw = _read_json(closed_ir, label="closed image IR")
    fulfilment, fulfilment_raw = _read_json(
        asset_fulfilment, label="Slice-4 asset fulfilment"
    )
    fulfilment_manifest, _ = _read_json(
        asset_manifest, label="Slice-4 fulfilment manifest"
    )
    _verify_slice_inputs(
        closed=closed, closed_raw=closed_raw,
        fulfilment=fulfilment, fulfilment_raw=fulfilment_raw,
        fulfilment_manifest=fulfilment_manifest,
        closed_path=closed_ir, fulfilment_path=asset_fulfilment,
    )

    lookup_path = Path(lookups_dir)
    _verify_catalog_hashes(fulfilment, lookups_dir=lookup_path)
    catalog = load_catalog(str(lookup_path))
    raw_assets = catalog.get("assets") if isinstance(catalog, dict) else None
    if not isinstance(raw_assets, list):
        raise VisualReasoningCompilerError("Unity asset catalog has no assets list")
    catalog_assets = [entry for entry in raw_assets if isinstance(entry, dict)]

    assets: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    for index, raw_asset in enumerate(fulfilment.get("assets") or []):
        asset = _mapping(raw_asset, label=f"Slice-4 assets[{index}]")
        label = asset.get("inventory_label")
        if not isinstance(label, str) or not label.strip():
            raise VisualReasoningCompilerError(
                f"Slice-4 assets[{index}] has no inventory_label"
            )
        if label in seen_labels:
            raise VisualReasoningCompilerError(
                f"Slice-4 contains duplicate fulfilled visual asset {label!r}"
            )
        seen_labels.add(label)
        selection = _mapping(asset.get("selection"), label=f"asset {label!r} selection")
        entry = _catalog_entry_for_selection(selection, catalog_assets)
        source = _safe_asset_path(entry, asset_root=asset_root)
        if not source.is_file():
            raise VisualReasoningCompilerError(
                f"Slice-4 selected asset is missing: {source}"
            )
        expected_hash = selection.get("file_sha256")
        if not isinstance(expected_hash, str) or sha256_file(source) != expected_hash:
            raise VisualReasoningCompilerError(
                f"Slice-4 selected asset bytes changed: {source}"
            )
        expected_bytes = selection.get("file_bytes")
        if isinstance(expected_bytes, int) and source.stat().st_size != expected_bytes:
            raise VisualReasoningCompilerError(
                f"Slice-4 selected asset byte count changed: {source}"
            )
        assets.append({
            "inventory_label": str(label),
            "query": str(asset.get("query") or ""),
            "selection": dict(selection),
            "catalog_entry": dict(entry),
            "source_path": str(source),
        })

    inventory = _mapping(closed.get("inventory"), label="closed inventory")
    elements = inventory.get("visible_elements")
    if not isinstance(elements, list):
        raise VisualReasoningCompilerError("closed inventory has no visible_elements list")
    matches = closed.get("entity_matches")
    if not isinstance(matches, list):
        raise VisualReasoningCompilerError("closed image IR has no entity_matches list")
    coverage = _mapping(closed.get("coverage"), label="closed coverage")
    required_labels = coverage.get("required_inventory_labels")
    if not isinstance(required_labels, list):
        raise VisualReasoningCompilerError(
            "closed image IR has no required_inventory_labels coverage"
        )
    missing_labels = sorted(set(map(str, required_labels)) - seen_labels)
    if missing_labels:
        raise VisualReasoningCompilerError(
            "Slice-4 omitted required visual asset selection(s): "
            + ", ".join(repr(label) for label in missing_labels)
        )

    return {
        "format": COMPILE_FORMAT,
        "stage": COMPILE_STAGE,
        "closed": closed,
        "fulfilment": fulfilment,
        "inputs": {
            "closed_image_ir": {
                "path": str(closed_ir),
                "sha256": sha256_bytes(closed_raw),
            },
            "asset_fulfilment": {
                "path": str(asset_fulfilment),
                "sha256": sha256_bytes(fulfilment_raw),
            },
            "asset_manifest": {
                "path": str(asset_manifest),
                "sha256": sha256_file(asset_manifest),
            },
        },
        "assets": assets,
        "binding_report": None,
    }


def _terms(value: Any) -> set[str]:
    raw = str(value or "")
    result: set[str] = set()
    for token in tokenize(raw):
        token = re.sub(r"[^a-z0-9]", "", token)
        if len(token) > 1 and token not in _STOPWORDS:
            result.add(token)
    # Catalog tokenization intentionally drops numeric fragments, but visual
    # labels use them as instance identity (aircraft_2 != aircraft_4).  Keep a
    # compact alphanumeric form only when digits are present so CamelCase and
    # underscore spellings compare deterministically without changing ordinary
    # noun scoring.
    compact = re.sub(r"[^a-z0-9]", "", raw.casefold())
    if len(compact) > 1 and any(char.isdigit() for char in compact):
        result.add(compact)
    return result


def _instance_numbers(value: Any) -> set[str]:
    """Numeric instance identity kept separate from noun token scoring.

    Visual labels use human-readable suffixes (``stone block 2``), while the
    schema-fill model commonly wraps the same identity in a PascalCase group
    name (``StoneBlock2Group``).  Token overlap alone drops the digit and makes
    every repeated noun tie.  Numbers are safe to use only alongside a shared
    noun, so an unrelated ``rock 2`` cannot claim ``stone block 2``.
    """
    return set(re.findall(r"\d+", str(value or "")))


def _entity_score(entity: Any, aliases: list[tuple[str, float]]) -> float:
    # Spatial/asset binding must follow the entity's declared identity, not
    # incidental prose that may mention nearby aircraft, ships, or effects.
    # Descriptions remain useful to the LLM, but letting them score here can
    # make an aircraft label claim a ship group merely because the concept
    # describes both in one sentence.
    fields = (
        (getattr(entity, "name", ""), 4.0),
        (getattr(entity, "model_hint", ""), 5.0),
        (getattr(entity, "sketchfab_query", ""), 3.0),
        (getattr(entity, "kind", ""), 0.5),
    )
    score = 0.0
    for alias, weight in aliases:
        alias_terms = _terms(alias)
        if not alias_terms:
            continue
        for field, field_weight in fields:
            field_terms = _terms(field)
            overlap = alias_terms & field_terms
            if overlap:
                score += weight * field_weight * len(overlap) / len(alias_terms)
                if (_instance_numbers(alias) & _instance_numbers(field)):
                    # The shared noun guard above is intentional: the number
                    # disambiguates repeated visual instances, it never creates
                    # a binding by itself.
                    score += 100.0 * weight * field_weight
    return score


def bind_visual_assets(schema: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """Bind each fulfilled visual selection to one unique schema entity.

    The result is intentionally separate from ``schema.meta``.  Metadata is a
    report for replay and inspection; the exact catalog records are passed to
    the resolver through the explicit generator parameter.
    """
    closed = _mapping(plan.get("closed"), label="visual compile closed IR")
    inventory = _mapping(closed.get("inventory"), label="visual compile inventory")
    elements = {
        str(element.get("label")): element
        for element in inventory.get("visible_elements", [])
        if isinstance(element, dict) and element.get("label")
    }
    references_by_label: dict[str, list[str]] = {}
    for raw in closed.get("entity_matches", []):
        match = _mapping(raw, label="closed entity match")
        label = str(match.get("inventory_label") or "")
        reference = str(match.get("reference") or "")
        if label and reference:
            references_by_label.setdefault(label, []).append(reference)

    entities = list(getattr(schema, "entities", []) or [])
    bindings: dict[str, dict[str, Any]] = {}
    used_entities: set[str] = set()
    rows: list[dict[str, Any]] = []
    for raw_asset in plan.get("assets", []):
        asset = _mapping(raw_asset, label="visual compile asset")
        label = str(asset.get("inventory_label") or "")
        element = elements.get(label)
        if element is None:
            raise VisualReasoningCompilerError(
                f"fulfilled visual asset {label!r} is absent from the closed inventory"
            )
        aliases: list[tuple[str, float]] = [(label, 2.0)]
        aliases.extend((reference, 4.0) for reference in references_by_label.get(label, []))
        aliases.append((str(element.get("model_hint") or ""), 3.0))
        aliases.append((str(asset.get("query") or ""), 2.0))

        def score_entities(candidates: list[Any]) -> list[tuple[float, str, Any]]:
            scored = sorted(
                (
                    _entity_score(entity, aliases),
                    str(getattr(entity, "name", "")),
                    entity,
                )
                for entity in candidates
            )
            return [row for row in scored if row[0] > 0.0]

        scored = score_entities([
            entity for entity in entities
            if str(getattr(entity, "name", "")) not in used_entities
        ])
        # Repeated image nouns can be instances of one schema group (for
        # example aircraft_2..aircraft_5 in an EnemyAircraftGroup).  Once all
        # distinct matching groups have been consumed, permit reuse only when
        # the all-entity score still has one unique best target.  The exact
        # Slice-4 selection must also agree with the existing group binding.
        if not scored:
            scored = score_entities(entities)
        if not scored:
            raise VisualReasoningCompilerError(
                f"visual asset {label!r} has no unique schema entity binding"
            )
        scored.sort(key=lambda row: (-row[0], row[1]))
        if len(scored) > 1 and abs(scored[0][0] - scored[1][0]) < 1e-12:
            raise VisualReasoningCompilerError(
                f"visual asset {label!r} has an ambiguous schema entity binding: "
                f"{scored[0][1]!r}, {scored[1][1]!r}"
            )
        score, entity_name, _entity = scored[0]
        used_entities.add(entity_name)
        binding = {
            "inventory_label": label,
            "entity_name": entity_name,
            "score": round(score, 12),
            "selection": dict(asset["selection"]),
            "catalog_entry": dict(asset["catalog_entry"]),
            "source_path": asset["source_path"],
        }
        existing = bindings.get(entity_name)
        if existing is not None:
            if existing["selection"] != binding["selection"]:
                raise VisualReasoningCompilerError(
                    f"visual assets {label!r} and "
                    f"{existing['inventory_label']!r} require conflicting "
                    f"selections for schema entity {entity_name!r}"
                )
        else:
            bindings[entity_name] = binding
        rows.append({
            "inventory_label": label,
            "entity_name": entity_name,
            "score": round(score, 12),
            "source": asset["selection"].get("source"),
            "filename": asset["selection"].get("filename"),
            "file_sha256": asset["selection"].get("file_sha256"),
        })

    report = {
        "format": COMPILE_FORMAT,
        "stage": COMPILE_STAGE,
        "status": "bound",
        "authority": {
            "authored_scene_compiler_outranks_exact_visual_binding": True,
            "exact_slice_4_selection_outranks_catalog_search": True,
            "catalog_rescoring_or_substitution": False,
        },
        "bindings": rows,
        "unbound_schema_entities": sorted(
            str(getattr(entity, "name", ""))
            for entity in entities
            if str(getattr(entity, "name", "")) not in bindings
        ),
    }
    plan["visual_asset_bindings"] = bindings
    plan["binding_report"] = report
    return {"bindings": bindings, "report": report}
