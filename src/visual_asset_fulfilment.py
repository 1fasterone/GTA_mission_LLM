"""Deterministic Slice 4 asset fulfilment for the image-led compiler.

Slice 3 closes the image-derived request set.  This module consumes that
closed IR and fulfils it only from the existing local asset catalog and the
already-frozen Sketchfab catalog partition.  It does not search the network,
download a model, call an LLM, or invent a fallback asset.

Live Sketchfab search/download/Blender conversion remains the explicit freeze
operation in :mod:`src.sketchfab_freeze`.  Run that operation before this
pass, then rerun this deterministic fulfilment step against the resulting
catalog.  A missing, ambiguous, semantically incompatible, unlicensed, or
unreadable asset is recorded as a rejection instead of being silently
replaced.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

from .sketchfab_client import DEFAULT_ACCEPTED_LICENSES
from .unity_asset_catalog import CATALOG_RELPATH, CURATED_RELPATH, SKETCHFAB_RELPATH, load_catalog, tokenize
from .visual_reasoning_compiler import (
    VisualReasoningCompilerError,
    sha256_bytes,
    sha256_file,
)
from .visual_reasoning_reconcile import CLOSED_FORMAT, RECONCILIATION_FORMAT


FULFILMENT_FORMAT = "visual-reasoning-compiler/asset-fulfilment/v1"
NON_ASSET_CATEGORIES = frozenset({"ui", "effect"})
SUPPORTED_FORMATS = frozenset({"fbx", "png", "glb", "gltf", "obj"})
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "in", "on", "with", "for", "to",
    "game", "player", "level", "scene", "world", "entity", "entities",
    "group", "object", "objects", "item", "items", "prop", "props",
})

# These are intentionally coarse gates.  The request's label/model_hint/query
# still has to match the catalog; a category gate only prevents a strong word
# such as "red" from choosing a vehicle for a character request.
_CATEGORY_HINTS: dict[str, frozenset[str]] = {
    "character": frozenset({"character", "human", "person", "npc", "avatar"}),
    "vehicle": frozenset({"vehicle", "car", "truck", "bike", "boat", "plane", "aircraft"}),
    "environment": frozenset({"environment", "nature", "structure", "terrain", "track", "road", "ground"}),
    # Natural landmarks such as mountains are represented by nature/terrain
    # catalog entries.  The noun and score gates still apply; this only keeps
    # the semantic category contract from rejecting a correct physical domain.
    "landmark": frozenset({
        "landmark", "structure", "building", "tower", "monument", "nature", "terrain",
    }),
    # Props are deliberately broad: catalog categories such as weapon,
    # mechanism, obstacle, nature, and structure can all be visible props.
    "prop": frozenset(),
    "other": frozenset(),
}


def _read_json(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    if not path.is_file():
        raise VisualReasoningCompilerError(f"{label} not found: {path}")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VisualReasoningCompilerError(f"{label} is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise VisualReasoningCompilerError(f"{label} must contain a JSON object: {path}")
    return value, raw


def _require_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise VisualReasoningCompilerError(f"{label} must be an object")
    return value


def _verify_closed_ir(closed: dict[str, Any], reconciliation: dict[str, Any],
                     closed_path: Path, closed_raw: bytes) -> None:
    if (closed.get("format") != CLOSED_FORMAT
            or closed.get("stage") != "3_image_reasoning_reconciliation"
            or closed.get("status") != "closed"):
        raise VisualReasoningCompilerError(
            f"closed image IR must be a closed {CLOSED_FORMAT!r} artifact"
        )
    if (reconciliation.get("format") != RECONCILIATION_FORMAT
            or reconciliation.get("stage") != "3_image_reasoning_reconciliation"):
        raise VisualReasoningCompilerError(
            f"reconciliation manifest format must be {RECONCILIATION_FORMAT!r}"
        )
    if reconciliation.get("status") != "closed":
        raise VisualReasoningCompilerError(
            "reconciliation manifest does not prove a closed Slice-3 result"
        )
    output = _require_mapping(
        _require_mapping(reconciliation.get("outputs"), label="reconciliation outputs")
        .get("closed_image_ir"),
        label="reconciliation closed_image_ir output",
    )
    expected = output.get("sha256")
    if not isinstance(expected, str) or expected != sha256_bytes(closed_raw):
        raise VisualReasoningCompilerError(
            f"reconciliation manifest hash does not match {closed_path}"
        )
    request_fingerprint = closed.get("request_fingerprint")
    if not isinstance(request_fingerprint, str) or not request_fingerprint:
        raise VisualReasoningCompilerError("closed image IR has no request fingerprint")
    if reconciliation.get("request_fingerprint") != request_fingerprint:
        raise VisualReasoningCompilerError(
            "reconciliation manifest request fingerprint does not match the closed IR"
        )


def _catalog_metadata(data: dict[str, Any], *, partition: str) -> dict[str, Any]:
    entries = data.get("assets") if partition == "asset_catalog" else data.get("entries")
    if not isinstance(entries, list):
        entries = []
    categories = sorted({
        str(entry.get("category"))
        for entry in entries
        if isinstance(entry, dict) and entry.get("category")
    })
    roots = sorted({
        str(entry.get("root"))
        for entry in entries
        if isinstance(entry, dict) and entry.get("root")
    })
    metadata: dict[str, Any] = {
        "partition": partition,
        "version": data.get("version"),
        "entry_count": len(entries),
        "categories": categories,
        "roots": roots,
    }
    provenance = data.get("provenance")
    if isinstance(provenance, dict):
        metadata["provenance"] = provenance
    return metadata


def _catalog_file_records(lookups_dir: Path) -> list[dict[str, Any]]:
    """Return hashes and deterministic provenance for loaded catalog partitions."""
    records: list[dict[str, Any]] = []
    base_path: Path | None = None
    base_data: dict[str, Any] | None = None
    for relative in (CATALOG_RELPATH, CURATED_RELPATH):
        candidate = lookups_dir / relative
        if not candidate.is_file():
            continue
        try:
            candidate_data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(candidate_data, dict):
            continue
        base_path = candidate
        base_data = candidate_data
        break
    if base_path is not None and base_data is not None:
        records.append({
            "path": base_path.as_posix(),
            "sha256": sha256_file(base_path),
            "metadata": _catalog_metadata(
                base_data,
                partition=("asset_catalog"
                           if base_path.name == CATALOG_RELPATH.name
                           else "curated_catalog"),
            ),
        })

    sketchfab_path = lookups_dir / SKETCHFAB_RELPATH
    if sketchfab_path.is_file():
        try:
            sketchfab_data = json.loads(sketchfab_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VisualReasoningCompilerError(
                f"frozen Sketchfab catalog is not valid UTF-8 JSON: {sketchfab_path}"
            ) from exc
        if not isinstance(sketchfab_data, dict):
            raise VisualReasoningCompilerError(
                f"frozen Sketchfab catalog must contain a JSON object: {sketchfab_path}"
            )
        records.append({
            "path": sketchfab_path.as_posix(),
            "sha256": sha256_file(sketchfab_path),
            "metadata": _catalog_metadata(sketchfab_data, partition="sketchfab_catalog"),
        })
    if not records:
        raise VisualReasoningCompilerError(
            f"no Unity asset catalog was found under {lookups_dir}"
        )
    return records


def _tokens(value: str) -> set[str]:
    return {
        token for token in tokenize(value)
        if len(token) > 1 and token not in _STOPWORDS
    }


def _entry_tokens(entry: dict[str, Any]) -> set[str]:
    result = _tokens(str(entry.get("filename") or ""))
    for raw_values in (entry.get("keywords") or [], entry.get("kind_hints") or []):
        values = (raw_values if isinstance(raw_values, (list, tuple, set))
                  else [raw_values])
        for value in values:
            result.update(_tokens(str(value)))
    result.update(_tokens(str(entry.get("category") or "")))
    result.update(_tokens(str(entry.get("description") or "")))
    return result


_GENERIC_VISUAL_TOKENS = frozenset({
    "asset", "assets", "game", "model", "models", "object", "objects",
    "scene", "world", "element", "elements", "feature", "natural",
    "visual", "visible", "thing", "things",
})


def _visual_signal_tokens(request: dict[str, Any], element: dict[str, Any]) -> set[str]:
    """Return noun/index terms, excluding category-only evidence.

    Category words are intentionally excluded.  A vehicle request must not
    pass freshness preflight merely because every car entry says ``vehicle``;
    it needs a current noun match such as ``speedboat`` or ``helicopter``.
    """
    category_tokens = _tokens(str(element.get("category") or ""))
    values = (
        element.get("label"),
        request.get("query"),
        element.get("model_hint"),
    )
    return {
        token for value in values for token in _tokens(str(value or ""))
        if token not in category_tokens and token not in _GENERIC_VISUAL_TOKENS
    }


def _category_compatible(category: str, entry: dict[str, Any]) -> bool:
    expected = _CATEGORY_HINTS.get(category)
    if not expected:
        return True
    entry_category = str(entry.get("category") or "").casefold()
    entry_hints = {
        token for value in (entry.get("kind_hints") or [])
        for token in _tokens(str(value))
    }
    return entry_category in expected or bool(entry_hints & expected)


def _score_request(request: dict[str, Any], element: dict[str, Any],
                   entry: dict[str, Any]) -> float:
    weights: dict[str, float] = {}

    def add(value: Any, weight: float) -> None:
        for token in _tokens(str(value or "")):
            weights[token] = max(weights.get(token, 0.0), weight)

    add(element.get("label"), 1.0)
    add(element.get("category"), 1.0)
    add(element.get("model_hint"), 2.0)
    add(request.get("query"), 2.0)
    add(element.get("description"), 0.1)
    keywords = _entry_tokens(entry)
    hits = {token: weight for token, weight in weights.items() if token in keywords}
    if not hits or max(hits.values()) < 1.0:
        return 0.0
    score = sum(hits.values()) / math.sqrt(max(1, len(keywords)))
    category = str(element.get("category") or "")
    hints = {str(value).casefold() for value in (entry.get("kind_hints") or [])}
    if category in hints:
        score += 0.5
    try:
        score += float(entry.get("generic_score") or 0) / 50.0
    except (TypeError, ValueError):
        pass
    # A frozen Sketchfab result is the deterministic answer to the exact
    # query that produced it.  Once the live freeze has selected a licensed
    # model for that query, stale local pack variants must not reopen an
    # equal/stronger lexical match during Slice 4.  This is deliberately a
    # text/provenance rule: no thumbnail or CLIP signal is consulted here.
    request_query = " ".join(_tokens(str(request.get("query") or "")))
    frozen_query = " ".join(_tokens(str(entry.get("query") or "")))
    if (
        str(entry.get("source") or "").casefold() == "sketchfab"
        and request_query
        and request_query == frozen_query
    ):
        score += 100.0
    return score


def _entry_path(entry: dict[str, Any], *, asset_root: Path | None) -> Path:
    rel_value = entry.get("relpath") or entry.get("filename")
    if not isinstance(rel_value, str) or not rel_value.strip():
        raise VisualReasoningCompilerError("catalog entry has no relative asset path")
    relative = Path(rel_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise VisualReasoningCompilerError(
            f"catalog entry path escapes its catalog root: {rel_value!r}"
        )
    root = Path(str(entry.get("root") or asset_root or "Models/FBX format"))
    root_resolved = root.resolve()
    source = (root_resolved / relative).resolve()
    try:
        source.relative_to(root_resolved)
    except ValueError as exc:
        raise VisualReasoningCompilerError(
            f"catalog entry path escapes its catalog root: {rel_value!r}"
        ) from exc
    return source


def _license_ok(entry: dict[str, Any], accepted_licenses: set[str]) -> bool:
    if str(entry.get("source") or "").casefold() != "sketchfab":
        return True
    license_slug = str(entry.get("license") or "").casefold()
    return bool(license_slug and license_slug in accepted_licenses)


def _catalog_preflight(
    *,
    requests: list[Any],
    elements_by_label: dict[str, dict[str, Any]],
    catalog_assets: list[Any],
    accepted_licenses: set[str],
    asset_root: Path | None,
) -> list[dict[str, Any]]:
    """Find stale/index-misaligned nouns before semantic scoring can hide them."""
    failures: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    for index, raw_request in enumerate(requests):
        if not isinstance(raw_request, dict):
            continue
        label = raw_request.get("inventory_label")
        if not isinstance(label, str) or not label.strip() or label in seen_labels:
            continue
        seen_labels.add(label)
        element = elements_by_label.get(label)
        if element is None:
            continue
        category = str(element.get("category") or "")
        if category in NON_ASSET_CATEGORIES:
            continue
        signal_tokens = _visual_signal_tokens(raw_request, element)
        if not signal_tokens:
            continue

        category_signal_matches: list[dict[str, Any]] = []
        category_only_matches: list[dict[str, Any]] = []
        cross_domain_matches: list[dict[str, Any]] = []
        for raw_entry in catalog_assets:
            if not isinstance(raw_entry, dict):
                continue
            fmt = str(raw_entry.get("format") or "fbx").casefold()
            if fmt not in SUPPORTED_FORMATS or not _license_ok(raw_entry, accepted_licenses):
                continue
            try:
                source = _entry_path(raw_entry, asset_root=asset_root)
            except VisualReasoningCompilerError:
                continue
            if not source.is_file():
                continue
            compatible = _category_compatible(category, raw_entry)
            overlap = sorted(signal_tokens & _entry_tokens(raw_entry))
            if not overlap:
                if compatible:
                    category_only_matches.append({
                        "filename": str(raw_entry.get("filename") or source.name),
                        "category": str(raw_entry.get("category") or ""),
                    })
                continue
            summary = {
                "filename": str(raw_entry.get("filename") or source.name),
                "category": str(raw_entry.get("category") or ""),
                "matched_tokens": overlap,
            }
            cross_domain_matches.append(summary)
            if compatible:
                category_signal_matches.append(summary)

        if category_signal_matches:
            continue
        cross_domain_matches.sort(key=lambda item: (
            item["filename"], item["category"], item["matched_tokens"],
        ))
        reason = (
            "catalog freshness preflight: no readable, licensed, "
            f"category-compatible catalog asset matches visual noun tokens "
            f"{sorted(signal_tokens)!r}"
        )
        if cross_domain_matches:
            reason += "; cross-domain matches were refused"
        if category_only_matches:
            reason += "; category-only matches were refused"
        category_only_matches.sort(key=lambda item: (
            item["filename"], item["category"],
        ))
        failures.append({
            "index": index,
            "inventory_label": label,
            "reason": reason,
            "signal_tokens": sorted(signal_tokens),
            "cross_domain_matches": cross_domain_matches[:5],
            "category_only_matches": category_only_matches[:5],
        })
    return failures


def _deduplicate_identical_candidates(
    candidates: list[tuple[float, dict[str, Any], Path]],
) -> list[tuple[float, dict[str, Any], Path]]:
    """Collapse duplicate pack copies without hiding distinct model choices."""
    unique: dict[str, tuple[float, dict[str, Any], Path]] = {}
    for candidate in candidates:
        score, entry, source = candidate
        content_hash = sha256_file(source)
        current = unique.get(content_hash)
        if current is None or (
            str(entry.get("filename") or source.name),
            str(entry.get("source") or ""),
            str(entry.get("relpath") or ""),
        ) < (
            str(current[1].get("filename") or current[2].name),
            str(current[1].get("source") or ""),
            str(current[1].get("relpath") or ""),
        ):
            unique[content_hash] = candidate
    return list(unique.values())


def _selection(entry: dict[str, Any], source: Path, score: float) -> dict[str, Any]:
    result: dict[str, Any] = {
        "filename": str(entry.get("filename") or source.name),
        "relpath": str(entry.get("relpath") or entry.get("filename") or source.name),
        "format": str(entry.get("format") or source.suffix.lstrip(".")).casefold(),
        "source": str(entry.get("source") or "kenney"),
        "category": str(entry.get("category") or ""),
        "score": round(score, 12),
        "file_sha256": sha256_file(source),
        "file_bytes": source.stat().st_size,
    }
    for key in (
        "license", "author", "author_username", "attribution",
        "sketchfab_uid", "sketchfab_url",
    ):
        value = entry.get(key)
        if value not in (None, ""):
            result[key] = value
    return result


def _assert_output_paths(output: Path, manifest: Path) -> None:
    if output.resolve() == manifest.resolve():
        raise VisualReasoningCompilerError(f"Slice-4 output paths collide: {output}")
    for path in (output, manifest):
        if path.exists():
            raise VisualReasoningCompilerError(
                f"refusing to overwrite visual-reasoning artifact: {path}"
            )


def fulfil_image_assets(*, closed_ir: Path, reconciliation_manifest: Path,
                        output: Path, manifest: Path,
                        lookups_dir: str = "lookups",
                        asset_root: Path | None = None,
                        min_score: float = 0.15,
                        accepted_licenses: Iterable[str] | None = None) -> Path:
    """Fulfil a closed image request set from frozen, local catalog data."""
    _assert_output_paths(output, manifest)
    closed, closed_raw = _read_json(closed_ir, label="closed image IR")
    reconciliation, reconciliation_raw = _read_json(
        reconciliation_manifest, label="Slice-3 reconciliation manifest"
    )
    _verify_closed_ir(closed, reconciliation, closed_ir, closed_raw)

    inventory = _require_mapping(closed.get("inventory"), label="closed inventory")
    elements = inventory.get("visible_elements")
    if not isinstance(elements, list):
        raise VisualReasoningCompilerError("closed image IR inventory has no visible_elements list")
    elements_by_label = {
        str(element.get("label")): element for element in elements
        if isinstance(element, dict) and element.get("label")
    }
    requests = closed.get("asset_requests")
    if not isinstance(requests, list):
        raise VisualReasoningCompilerError("closed image IR has no asset_requests list")

    lookup_path = Path(lookups_dir)
    catalog_files = _catalog_file_records(lookup_path)
    catalog = load_catalog(str(lookup_path))
    catalog_assets = catalog.get("assets") if isinstance(catalog, dict) else None
    if not isinstance(catalog_assets, list):
        raise VisualReasoningCompilerError("Unity asset catalog has no assets list")
    accepted = {
        str(value).casefold() for value in (accepted_licenses or DEFAULT_ACCEPTED_LICENSES)
    }

    fulfilled: list[dict[str, Any]] = []
    preflight_failures = _catalog_preflight(
        requests=requests,
        elements_by_label=elements_by_label,
        catalog_assets=catalog_assets,
        accepted_licenses=accepted,
        asset_root=asset_root,
    )
    preflight_by_label = {
        str(item["inventory_label"]): item for item in preflight_failures
    }
    rejected: list[dict[str, Any]] = list(preflight_failures)
    seen_labels: set[str] = set()
    for index, raw_request in enumerate(requests):
        request = _require_mapping(raw_request, label=f"asset_requests[{index}]")
        label = request.get("inventory_label")
        if not isinstance(label, str) or not label.strip():
            rejected.append({"index": index, "reason": "request has no inventory_label"})
            continue
        if label in seen_labels:
            rejected.append({"inventory_label": label, "reason": "duplicate asset request"})
            continue
        seen_labels.add(label)
        if label in preflight_by_label:
            continue
        element = elements_by_label.get(label)
        if element is None:
            rejected.append({"inventory_label": label, "reason": "request is not in the closed inventory"})
            continue
        category = str(element.get("category") or "")
        if category in NON_ASSET_CATEGORIES:
            rejected.append({
                "inventory_label": label,
                "reason": f"category {category!r} is not an asset fulfilment target",
            })
            continue

        candidates: list[tuple[float, dict[str, Any], Path]] = []
        path_errors: list[str] = []
        for entry in catalog_assets:
            if not isinstance(entry, dict):
                continue
            fmt = str(entry.get("format") or "fbx").casefold()
            if fmt not in SUPPORTED_FORMATS or not _category_compatible(category, entry):
                continue
            if not _license_ok(entry, accepted):
                continue
            try:
                source = _entry_path(entry, asset_root=asset_root)
            except VisualReasoningCompilerError as exc:
                path_errors.append(str(exc))
                continue
            if not source.is_file():
                continue
            score = _score_request(request, element, entry)
            if score >= min_score:
                candidates.append((score, entry, source))

        candidates.sort(key=lambda item: (
            -item[0],
            str(item[1].get("filename") or ""),
            str(item[1].get("source") or ""),
            str(item[1].get("relpath") or ""),
        ))
        candidates = _deduplicate_identical_candidates(candidates)
        candidates.sort(key=lambda item: (
            -item[0],
            str(item[1].get("filename") or ""),
            str(item[1].get("source") or ""),
            str(item[1].get("relpath") or ""),
        ))
        if not candidates:
            reason = "no readable licensed catalog asset met the semantic-kind score"
            if path_errors:
                reason += "; invalid catalog paths were rejected"
            rejected.append({"inventory_label": label, "reason": reason})
            continue
        if len(candidates) > 1 and abs(candidates[0][0] - candidates[1][0]) < 1e-12:
            rejected.append({
                "inventory_label": label,
                "reason": "ambiguous catalog match at the best deterministic score",
                "candidates": [str(item[1].get("filename") or "") for item in candidates[:3]],
            })
            continue
        score, entry, source = candidates[0]
        fulfilled.append({
            "inventory_label": label,
            "query": str(request.get("query") or ""),
            "selection": _selection(entry, source, score),
        })

    status = "fulfilled" if not rejected else "rejected"
    selection_fingerprint = {
        "closed_request_fingerprint": closed["request_fingerprint"],
        "catalog_sha256": [record["sha256"] for record in catalog_files],
        "fulfilled": fulfilled,
        "rejected": rejected,
    }
    fulfilment_data: dict[str, Any] = {
        "format": FULFILMENT_FORMAT,
        "stage": "4_asset_fulfilment",
        "status": status,
        "authority": {
            "llm_called_here": False,
            "network_called_here": False,
            "asset_source": "local_catalog_and_frozen_sketchfab_catalog",
            "original_vizconcept_sent_to_asset_resolver": False,
        },
        "inputs": {
            "closed_image_ir": {
                "path": str(closed_ir),
                "sha256": sha256_bytes(closed_raw),
            },
            "reconciliation_manifest": {
                "path": str(reconciliation_manifest),
                "sha256": sha256_bytes(reconciliation_raw),
            },
            "catalog_files": catalog_files,
        },
        "closed_request_fingerprint": closed["request_fingerprint"],
        "fulfilment_fingerprint": sha256_bytes(
            json.dumps(selection_fingerprint, sort_keys=True, separators=(",", ":"))
            .encode("utf-8")
        ),
        "assets": fulfilled,
        "rejections": rejected,
        "catalog_preflight": {
            "status": "pass" if not preflight_failures else "rejected",
            "checked_request_count": len(requests),
            "failures": preflight_failures,
        },
        "warnings": [
            "Live marketplace search/download and LLM calls are disabled in Slice 4.",
            "Board-to-mesh visual comparison is not performed; this slice verifies "
            "frozen provenance, semantic kind, license, and bytes.",
        ],
    }
    output_bytes = (
        json.dumps(fulfilment_data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    manifest_data = {
        "format": FULFILMENT_FORMAT,
        "stage": "4_asset_fulfilment",
        "status": status,
        "authority": fulfilment_data["authority"],
        "closed_request_fingerprint": closed["request_fingerprint"],
        "fulfilment_fingerprint": fulfilment_data["fulfilment_fingerprint"],
        "inputs": fulfilment_data["inputs"],
        "outputs": {
            "asset_fulfilment": {
                "path": str(output),
                "sha256": sha256_bytes(output_bytes),
            }
        },
        "fulfilled_count": len(fulfilled),
        "rejected_count": len(rejected),
        "catalog_preflight": fulfilment_data["catalog_preflight"],
        "warnings": fulfilment_data["warnings"],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(output_bytes)
    manifest.write_text(
        json.dumps(manifest_data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest
