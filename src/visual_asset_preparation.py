"""Sketchfab-first preparation for imported-image asset requests.

This is the explicit nondeterministic preparation boundary before Slice 4.
It reads only the closed image IR, searches Sketchfab for missing requests,
and freezes successful licensed conversions into the local Sketchfab catalog.
Slice 4 remains deterministic: it reads that frozen catalog first and falls
back to the ordinary local catalog when no acceptable Sketchfab candidate is
present.

No LLM call is made here.  The optional semantic gate is the deterministic
embedding gate already used by the normal Sketchfab freeze path; the vision
LLM selector is deliberately not wired into this image E2E preparation
stage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .sketchfab_client import SketchfabClient
from .sketchfab_freeze import fetch_and_freeze
from .unity_asset_catalog import SKETCHFAB_RELPATH, tokenize
from .visual_asset_fulfilment import (
    NON_ASSET_CATEGORIES,
    _entry_path,
    _license_ok,
    _read_json,
    _require_mapping,
    _tokens,
    _verify_closed_ir,
    _catalog_file_records,
)
from .visual_reasoning_compiler import (
    VisualReasoningCompilerError,
    sha256_bytes,
)


PREPARATION_FORMAT = "visual-reasoning-compiler/asset-preparation/v1"


def _query_for(request: dict[str, Any], element: dict[str, Any]) -> str:
    for value in (
        request.get("query"),
        element.get("model_hint"),
        element.get("label"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _existing_frozen_queries(
    *, lookups_dir: Path, asset_root: Path | None,
    accepted_licenses: set[str],
) -> set[str]:
    """Return exact query keys whose frozen Sketchfab asset is still usable."""
    path = lookups_dir / SKETCHFAB_RELPATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return set()
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return set()

    usable: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("query"):
            continue
        if not _license_ok(entry, accepted_licenses):
            continue
        try:
            source = _entry_path(entry, asset_root=asset_root)
        except VisualReasoningCompilerError:
            continue
        if source.is_file():
            usable.add(" ".join(_tokens(str(entry["query"]))))
    return usable


def _semantic_filter(sk_cfg: dict[str, Any], client: SketchfabClient,
                     query: str, category: str):
    if not sk_cfg.get("semantic_select"):
        return None
    from .unity_semantic_embed import semantic_filter

    return lambda candidates: semantic_filter(
        query,
        category,
        query,
        candidates,
        client=client,
        min_image_similarity=float(sk_cfg.get("min_image_similarity", 0.22)),
        min_text_similarity=float(sk_cfg.get("min_text_similarity", 0.45)),
        max_candidates=int(sk_cfg.get("semantic_pool_size", 6)),
    )


def prepare_image_assets(*, closed_ir: Path, reconciliation_manifest: Path,
                         output: Path, lookups_dir: str = "lookups",
                         asset_root: Path | None = None,
                         sketchfab_config: dict[str, Any] | None = None,
                         accepted_licenses: list[str] | None = None) -> Path:
    """Freeze Sketchfab candidates for the closed image request set.

    A request that cannot be frozen is not failed here: Slice 4 gets the
    chance to resolve it from the local catalog.  The preparation report
    records every reuse, freeze, skip, and fallback so a later rejection is
    attributable to the catalog rather than hidden network behavior.
    """
    if output.exists():
        raise VisualReasoningCompilerError(
            f"refusing to overwrite visual asset preparation artifact: {output}"
        )

    closed, closed_raw = _read_json(closed_ir, label="closed image IR")
    reconciliation, reconciliation_raw = _read_json(
        reconciliation_manifest, label="Slice-3 reconciliation manifest"
    )
    _verify_closed_ir(closed, reconciliation, closed_ir, closed_raw)

    inventory = _require_mapping(closed.get("inventory"), label="closed inventory")
    elements = inventory.get("visible_elements")
    if not isinstance(elements, list):
        raise VisualReasoningCompilerError(
            "closed image IR inventory has no visible_elements list"
        )
    elements_by_label = {
        str(element.get("label")): element
        for element in elements
        if isinstance(element, dict) and element.get("label")
    }
    requests = closed.get("asset_requests")
    if not isinstance(requests, list):
        raise VisualReasoningCompilerError(
            "closed image IR has no asset_requests list"
        )

    lookup_path = Path(lookups_dir)
    sk_cfg = dict(sketchfab_config or {})
    accepted = {
        str(value).casefold()
        for value in (accepted_licenses or sk_cfg.get("accepted_licenses") or [])
    }
    if not accepted:
        from .sketchfab_client import DEFAULT_ACCEPTED_LICENSES
        accepted = set(DEFAULT_ACCEPTED_LICENSES)

    before_catalog = _catalog_file_records(lookup_path)
    existing = _existing_frozen_queries(
        lookups_dir=lookup_path,
        asset_root=asset_root,
        accepted_licenses=accepted,
    )
    client = SketchfabClient(sk_cfg)
    enabled = bool(sk_cfg.get("enabled", True))
    can_fetch = enabled and client.available()
    semantic_enabled = bool(sk_cfg.get("semantic_select"))
    attempts: list[dict[str, Any]] = []
    attempted_queries: dict[str, dict[str, Any] | None] = {}
    network_attempted = False

    for index, raw_request in enumerate(requests):
        if not isinstance(raw_request, dict):
            attempts.append({"index": index, "status": "rejected",
                             "reason": "request is not an object"})
            continue
        label = str(raw_request.get("inventory_label") or "").strip()
        element = elements_by_label.get(label)
        if not label or element is None:
            attempts.append({"index": index, "inventory_label": label,
                             "status": "rejected",
                             "reason": "request is not in the closed inventory"})
            continue
        category = str(element.get("category") or "").strip().casefold()
        if category in NON_ASSET_CATEGORIES:
            attempts.append({"index": index, "inventory_label": label,
                             "status": "skipped",
                             "reason": f"category {category!r} is not an asset target"})
            continue

        query = _query_for(raw_request, element)
        query_key = " ".join(_tokens(query))
        if not query_key:
            attempts.append({"index": index, "inventory_label": label,
                             "status": "fallback_catalog",
                             "reason": "request has no usable Sketchfab query"})
            continue

        if query_key in existing:
            attempts.append({"index": index, "inventory_label": label,
                             "query": query, "category": category,
                             "status": "reused_frozen_sketchfab"})
            continue
        if query_key in attempted_queries:
            prior = attempted_queries[query_key]
            attempts.append({
                "index": index, "inventory_label": label, "query": query,
                "category": category,
                "status": "reused_preparation_result" if prior else "fallback_catalog",
            })
            continue
        if not enabled:
            attempted_queries[query_key] = None
            attempts.append({"index": index, "inventory_label": label,
                             "query": query, "category": category,
                             "status": "fallback_catalog",
                             "reason": "sketchfab.enabled is false"})
            continue
        if not can_fetch:
            attempted_queries[query_key] = None
            attempts.append({"index": index, "inventory_label": label,
                             "query": query, "category": category,
                             "status": "fallback_catalog",
                             "reason": "no Sketchfab API token is available"})
            continue

        network_attempted = True
        query_terms = set(tokenize(query))

        def score_fn(entry: dict[str, Any], _terms=query_terms) -> float:
            keywords = {
                str(value).casefold()
                for value in (entry.get("keywords") or [])
            }
            return float(len(keywords & _terms))

        try:
            frozen = fetch_and_freeze(
                query,
                score_fn=score_fn,
                client=client,
                blender_cfg=sk_cfg,
                cache_dir=sk_cfg.get("cache_dir", "Models/Sketchfab"),
                lookups_dir=str(lookup_path),
                category=category or "prop",
                kind_hints=[category or "prop"],
                min_score=float(sk_cfg.get("min_match_score", 0.0)),
                semantic_filter_fn=_semantic_filter(sk_cfg, client, query, category),
            )
        except Exception as exc:  # pragma: no cover - defensive boundary
            frozen = None
            reason = f"Sketchfab preparation error: {exc}"
        else:
            reason = "no acceptable licensed Sketchfab result was frozen"

        attempted_queries[query_key] = frozen
        if frozen is None:
            attempts.append({"index": index, "inventory_label": label,
                             "query": query, "category": category,
                             "status": "fallback_catalog", "reason": reason})
        else:
            existing.add(query_key)
            attempts.append({
                "index": index, "inventory_label": label, "query": query,
                "category": category, "status": "frozen_sketchfab",
                "filename": frozen.get("filename"),
                "sketchfab_uid": frozen.get("sketchfab_uid"),
            })

    after_catalog = _catalog_file_records(lookup_path)
    preparation = {
        "format": PREPARATION_FORMAT,
        "stage": "pre_slice_4_sketchfab_freeze",
        "status": "prepared",
        "selection_policy": "sketchfab_first_then_local_catalog",
        "authority": {
            "llm_called_here": False,
            "network_attempted_here": network_attempted,
            "asset_source": "frozen_sketchfab_then_local_catalog",
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
            "catalog_files_before": before_catalog,
        },
        "closed_request_fingerprint": closed["request_fingerprint"],
        "sketchfab": {
            "enabled": enabled,
            "client_available": client.available(),
            "semantic_select": semantic_enabled,
        },
        "attempts": attempts,
        "catalog_files_after": after_catalog,
        "fallback_count": sum(
            1 for item in attempts if item.get("status") == "fallback_catalog"
        ),
        "frozen_count": sum(
            1 for item in attempts if item.get("status") == "frozen_sketchfab"
        ),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(preparation, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return output
