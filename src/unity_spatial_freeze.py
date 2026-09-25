"""Deterministic Slice 6.7 freeze for an accepted Unity spatial build.

Slice 6.7 does not create a new schema authority or emit new world
coordinates.  It verifies the already-produced sidecars, records every
upstream artifact by content hash, and writes one immutable build lineage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional

from .unity_schema import UnitySchema
from .unity_spatial_identity import semantic_schema_hash
from .unity_spatial_validation import validate_spatial_layout
from .visual_reasoning_compiler import sha256_bytes, sha256_file


SPATIAL_FREEZE_FORMAT = "visual-reasoning-compiler/spatial-freeze/v1"
SPATIAL_FREEZE_MANIFEST_FORMAT = (
    "visual-reasoning-compiler/spatial-freeze-manifest/v1"
)
SPATIAL_FREEZE_STAGE = "6.7_spatial_freeze"


class SpatialFreezeError(ValueError):
    """Raised when a build cannot be frozen without breaking lineage."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _canonical_hash(value: Any) -> str:
    return sha256_bytes(_canonical_bytes(value))


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if not path.is_file():
        raise SpatialFreezeError(f"{label} not found: {path}")
    try:
        raw = path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpatialFreezeError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise SpatialFreezeError(f"{label} must be a JSON object: {path}")
    return data, raw


def _resolve_path(raw: Any, *, base: Path, label: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise SpatialFreezeError(f"{label} has no path")
    path = Path(raw)
    if path.is_file() or path.is_dir():
        return path
    relative = base / path
    if relative.is_file() or relative.is_dir():
        return relative
    raise SpatialFreezeError(f"{label} path does not exist: {raw}")


def _file_ref(path: Path, kind: str) -> dict[str, Any]:
    if not path.is_file():
        raise SpatialFreezeError(f"{kind} not found: {path}")
    try:
        return {
            "kind": kind,
            "path": str(path),
            "sha256": sha256_file(path),
        }
    except OSError as exc:
        raise SpatialFreezeError(f"could not hash {kind}: {path}") from exc


def _require_format(
    data: Mapping[str, Any],
    *,
    expected: str | tuple[str, ...],
    label: str,
    statuses: Optional[set[str]] = None,
) -> None:
    expected_values = {expected} if isinstance(expected, str) else set(expected)
    if data.get("format") not in expected_values:
        raise SpatialFreezeError(
            f"{label} has format {data.get('format')!r}; "
            f"expected one of {sorted(expected_values)!r}"
        )
    if statuses is not None and data.get("status") not in statuses:
        raise SpatialFreezeError(
            f"{label} has status {data.get('status')!r}; "
            f"expected one of {sorted(statuses)!r}"
        )


def _manifest_output(
    manifest_path: Path,
    output_path: Path,
    *,
    keys: str | tuple[str, ...],
    label: str,
    expected_format: Optional[str | tuple[str, ...]] = None,
    statuses: Optional[set[str]] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest, _raw = _read_json(manifest_path, f"{label} manifest")
    if expected_format is not None:
        _require_format(
            manifest,
            expected=expected_format,
            label=f"{label} manifest",
            statuses=statuses,
        )
    elif statuses is not None and manifest.get("status") not in statuses:
        raise SpatialFreezeError(
            f"{label} manifest has status {manifest.get('status')!r}"
        )
    key_values = [keys] if isinstance(keys, str) else list(keys)
    outputs = manifest.get("outputs")
    if not isinstance(outputs, Mapping):
        raise SpatialFreezeError(f"{label} manifest has no outputs")
    record = None
    for key in key_values:
        candidate = outputs.get(key)
        if isinstance(candidate, Mapping):
            record = candidate
            break
    if record is None or not isinstance(record.get("sha256"), str):
        raise SpatialFreezeError(
            f"{label} manifest has no output hash for {key_values!r}"
        )
    actual = sha256_file(output_path)
    if actual != record["sha256"]:
        raise SpatialFreezeError(
            f"{label} manifest hash does not match {output_path}"
        )
    return manifest, dict(record)


def _embedded_hash(
    record: Any,
    path: Path,
    *,
    label: str,
    required: bool = True,
) -> None:
    if not isinstance(record, Mapping):
        if required:
            raise SpatialFreezeError(f"{label} has no embedded artifact record")
        return
    expected = record.get("sha256")
    if required and not isinstance(expected, str):
        raise SpatialFreezeError(f"{label} has no embedded sha256")
    if expected is not None and expected != sha256_file(path):
        raise SpatialFreezeError(f"{label} hash does not match {path}")


def _path_from_record(record: Any, *, base: Path, label: str) -> Path:
    if not isinstance(record, Mapping):
        raise SpatialFreezeError(f"{label} is not an artifact record")
    return _resolve_path(record.get("path"), base=base, label=label)


def _without_paths(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_paths(item)
            for key, item in value.items()
            if key not in {"path", "project_root"}
        }
    if isinstance(value, list):
        return [_without_paths(item) for item in value]
    return value


def _transform_snapshot(
    schema: UnitySchema,
    world_layout: Mapping[str, Any],
) -> dict[str, Any]:
    layout = world_layout.get("layout")
    rows = layout.get("entities") if isinstance(layout, Mapping) else None
    if not isinstance(rows, list):
        raise SpatialFreezeError("world layout has no entity transform rows")
    by_name = {
        str(row.get("entity")): row
        for row in rows
        if isinstance(row, Mapping) and row.get("entity")
    }
    transforms: list[dict[str, Any]] = []
    for entity in schema.entities:
        row = by_name.get(str(entity.name))
        if row is None:
            raise SpatialFreezeError(
                f"world layout has no transform row for {entity.name!r}"
            )
        transforms.append({
            "entity": str(entity.name),
            "count": int(entity.count),
            "scale": float(entity.scale),
            "origin_offset": list(row.get("origin_offset") or [0.0, 0.0]),
            "group_radius": float(row.get("group_radius") or 0.0),
            "elevation": float(row.get("elevation") or 0.0),
            "instance_positions": [
                list(point) for point in row.get("instance_positions") or []
            ],
            "instance_yaws": [
                float(value) for value in row.get("instance_yaws") or []
            ],
            "path_points": [list(point) for point in row.get("path_points") or []],
        })
    return {
        "format": "visual-reasoning-compiler/spatial-transforms/v1",
        "entities": transforms,
        "sha256": _canonical_hash(transforms),
    }


def _sidecar_paths(project_root: Path, project_name: str) -> dict[str, Path]:
    return {
        "spatial_roles": project_root / f"{project_name}.spatial-roles.json",
        "scene_graph": project_root / f"{project_name}.scene-graph.json",
        "spatial_fusion": project_root / f"{project_name}.spatial-fusion.json",
        "world_layout": project_root / f"{project_name}.world-layout.json",
        "spatial_validation": project_root / f"{project_name}.spatial-validation.json",
    }


def freeze_spatial_build(
    *,
    project_root: Path,
    source_image: Path,
    reasoning_manifest: Path,
    inventory: Path,
    concept: Path,
    closed_ir: Path,
    reconciliation_manifest: Path,
    image_evidence: Path,
    image_evidence_manifest: Path,
    asset_fulfilment: Path,
    asset_manifest: Path,
    compile_manifest: Path,
    config: Optional[Path] = None,
    output: Optional[Path] = None,
    manifest: Optional[Path] = None,
) -> Path:
    """Freeze a complete, already-accepted visual Unity build.

    Every supplied stage is checked against its neighboring provenance record.
    The function refuses a missing artifact, stale hash, rejected asset result,
    or non-passing Slice-6.6 report.  No LLM, network, catalog search, or
    coordinate solver is called here.
    """
    project_root = Path(project_root)
    if not project_root.is_dir():
        raise SpatialFreezeError(f"Unity project root not found: {project_root}")
    if output is None:
        output = project_root / f"{project_root.name}.spatial-freeze.json"
    else:
        output = Path(output)
    if manifest is None:
        manifest = output.with_name(output.stem + ".manifest.json")
    else:
        manifest = Path(manifest)
    existing = [path for path in (output, manifest) if path.exists()]
    if existing:
        raise SpatialFreezeError(
            "refusing to overwrite Slice-6.7 artifact(s): "
            + ", ".join(str(path) for path in existing)
        )

    source_ref = _file_ref(Path(source_image), "source_image")
    reasoning, _reasoning_raw = _read_json(
        Path(reasoning_manifest), "Slice-2 reasoning manifest"
    )
    _require_format(
        reasoning,
        expected="visual-reasoning-compiler/image-reasoning/v1",
        label="Slice-2 reasoning manifest",
    )
    reasoning_base = Path(reasoning_manifest).parent
    board_record = reasoning.get("board")
    board_path = _path_from_record(
        board_record, base=reasoning_base, label="Slice-2 board"
    )
    board_manifest_path = _path_from_record(
        reasoning.get("board_manifest"),
        base=reasoning_base,
        label="Slice-1 board manifest",
    )
    board_ref = _file_ref(board_path, "board_image")
    _embedded_hash(board_record, board_path, label="Slice-2 board")
    if board_ref["sha256"] != reasoning.get("board", {}).get("sha256"):
        raise SpatialFreezeError("Slice-2 board hash is not self-consistent")
    board_manifest, _board_manifest_output = _read_json(
        board_manifest_path, "Slice-1 board manifest"
    )
    manifest_board = board_manifest.get("board")
    manifest_board_hash = (
        manifest_board.get("sha256")
        if isinstance(manifest_board, Mapping)
        else board_manifest.get("board_sha256")
    )
    manifest_source_board = board_manifest.get("source_board")
    manifest_source_hash = (
        manifest_source_board.get("sha256")
        if isinstance(manifest_source_board, Mapping)
        else None
    )
    if manifest_board_hash != board_ref["sha256"]:
        raise SpatialFreezeError("Slice-1 board manifest does not match board image")
    if manifest_source_hash is not None and manifest_source_hash != source_ref["sha256"]:
        raise SpatialFreezeError("Slice-1 source image does not match source board")
    board_manifest_ref = _file_ref(board_manifest_path, "board_manifest")
    if reasoning.get("board_manifest", {}).get("sha256") != board_manifest_ref["sha256"]:
        raise SpatialFreezeError("Slice-2 board manifest hash does not match")

    inventory = Path(inventory)
    concept = Path(concept)
    inventory_ref = _file_ref(inventory, "visual_inventory")
    concept_ref = _file_ref(concept, "Concept")
    reasoning_output = reasoning.get("outputs")
    if not isinstance(reasoning_output, Mapping):
        raise SpatialFreezeError("Slice-2 reasoning manifest has no outputs")
    _embedded_hash(
        reasoning_output.get("visual_inventory"),
        inventory,
        label="Slice-2 visual inventory",
    )
    _embedded_hash(
        reasoning_output.get("concept"),
        concept,
        label="Slice-2 Concept",
    )
    inventory_data, _inventory_raw = _read_json(inventory, "Slice-2 visual inventory")
    _require_format(
        inventory_data,
        expected="visual-reasoning-compiler/visual-inventory/v1",
        label="Slice-2 visual inventory",
    )
    raw_refs: dict[str, dict[str, Any]] = {}
    for stage_name in ("inventory", "concept"):
        stage_output = reasoning_output.get(stage_name)
        if not isinstance(stage_output, Mapping):
            continue
        raw_path = stage_output.get("raw_response_path")
        if isinstance(raw_path, str):
            path = _resolve_path(
                raw_path,
                base=reasoning_base,
                label=f"Slice-2 {stage_name} raw response",
            )
            raw_refs[stage_name] = _file_ref(
                path, f"slice_2_{stage_name}_raw_response"
            )

    reconciliation, _reconciliation_raw = _read_json(
        Path(reconciliation_manifest), "Slice-3 reconciliation manifest"
    )
    _require_format(
        reconciliation,
        expected="visual-reasoning-compiler/reconciliation/v1",
        label="Slice-3 reconciliation manifest",
        statuses={"closed"},
    )
    reconciliation_inputs = reconciliation.get("inputs")
    if not isinstance(reconciliation_inputs, Mapping):
        raise SpatialFreezeError("Slice-3 reconciliation has no inputs")
    for key, path, label in (
        ("reasoning_manifest", Path(reasoning_manifest), "Slice-3 reasoning input"),
        ("visual_inventory", inventory, "Slice-3 inventory input"),
        ("concept", concept, "Slice-3 Concept input"),
    ):
        _embedded_hash(reconciliation_inputs.get(key), path, label=label)
    closed_ir = Path(closed_ir)
    closed_ref = _file_ref(closed_ir, "closed_image_ir")
    _embedded_hash(
        (reconciliation.get("outputs") or {}).get("closed_image_ir"),
        closed_ir,
        label="Slice-3 closed image IR",
    )
    closed_data, closed_raw = _read_json(closed_ir, "Slice-3 closed image IR")
    _require_format(
        closed_data,
        expected="visual-reasoning-compiler/closed-image-ir/v1",
        label="Slice-3 closed image IR",
        statuses={"closed"},
    )

    image_evidence = Path(image_evidence)
    image_evidence_manifest = Path(image_evidence_manifest)
    evidence_manifest, _evidence_output = _manifest_output(
        image_evidence_manifest,
        image_evidence,
        keys="image_spatial_evidence",
        label="Slice-6.1 image evidence",
        expected_format="visual-reasoning-compiler/image-spatial-extraction/v1",
        statuses={"complete"},
    )
    evidence_data, evidence_raw = _read_json(
        image_evidence, "Slice-6.1 image spatial evidence"
    )
    _require_format(
        evidence_data,
        expected="visual-reasoning-compiler/image-spatial-evidence/v1",
        label="Slice-6.1 image spatial evidence",
    )
    evidence_inputs = evidence_data.get("inputs")
    if isinstance(evidence_inputs, Mapping):
        evidence_closed = evidence_inputs.get("closed_image_ir")
        if isinstance(evidence_closed, Mapping) and evidence_closed.get("sha256") != closed_ref["sha256"]:
            raise SpatialFreezeError("Slice-6.1 closed IR hash does not match")
    manifest_evidence_inputs = evidence_manifest.get("inputs")
    if isinstance(manifest_evidence_inputs, Mapping):
        board_input = manifest_evidence_inputs.get("board")
        closed_input = manifest_evidence_inputs.get("closed_image_ir")
        if isinstance(board_input, Mapping) and board_input.get("sha256") != board_ref["sha256"]:
            raise SpatialFreezeError("Slice-6.1 board hash does not match")
        if isinstance(closed_input, Mapping) and closed_input.get("sha256") != closed_ref["sha256"]:
            raise SpatialFreezeError("Slice-6.1 manifest closed IR hash does not match")
    labels = {
        str(element.get("label"))
        for element in (closed_data.get("inventory") or {}).get("visible_elements") or []
        if isinstance(element, Mapping) and element.get("label")
    }
    anchors = evidence_data.get("anchors")
    anchor_labels = [
        str(anchor.get("label"))
        for anchor in anchors or []
        if isinstance(anchor, Mapping) and anchor.get("label")
    ]
    if labels and set(anchor_labels) != labels or len(anchor_labels) != len(set(anchor_labels)):
        raise SpatialFreezeError("Slice-6.1 anchors do not cover the closed image labels")

    asset_fulfilment = Path(asset_fulfilment)
    asset_manifest = Path(asset_manifest)
    fulfilment_manifest, _fulfilment_output = _manifest_output(
        asset_manifest,
        asset_fulfilment,
        keys="asset_fulfilment",
        label="Slice-4 asset fulfilment",
        expected_format="visual-reasoning-compiler/asset-fulfilment/v1",
        statuses={"fulfilled"},
    )
    fulfilment, _fulfilment_raw = _read_json(
        asset_fulfilment, "Slice-4 asset fulfilment"
    )
    _require_format(
        fulfilment,
        expected="visual-reasoning-compiler/asset-fulfilment/v1",
        label="Slice-4 asset fulfilment",
        statuses={"fulfilled"},
    )
    if fulfilment.get("rejections"):
        raise SpatialFreezeError("Slice-4 contains rejected asset requests")
    fulfilment_inputs = fulfilment.get("inputs")
    if isinstance(fulfilment_inputs, Mapping):
        closed_input = fulfilment_inputs.get("closed_image_ir")
        if isinstance(closed_input, Mapping) and closed_input.get("sha256") != closed_ref["sha256"]:
            raise SpatialFreezeError("Slice-4 closed IR hash does not match")
    request_fingerprint = closed_data.get("request_fingerprint")
    for label, data in (
        ("Slice-4 fulfilment", fulfilment),
        ("Slice-4 fulfilment manifest", fulfilment_manifest),
    ):
        if request_fingerprint and data.get("closed_request_fingerprint") not in {
            None, request_fingerprint
        }:
            raise SpatialFreezeError(f"{label} request fingerprint does not match")
    catalog_refs: list[dict[str, Any]] = []
    catalog_records = (fulfilment_inputs or {}).get("catalog_files") if isinstance(fulfilment_inputs, Mapping) else None
    if not isinstance(catalog_records, list) or not catalog_records:
        raise SpatialFreezeError("Slice-4 has no frozen catalog file records")
    for index, record in enumerate(catalog_records):
        catalog_path = _path_from_record(
            record,
            base=asset_manifest.parent,
            label=f"Slice-4 catalog[{index}]",
        )
        _embedded_hash(record, catalog_path, label=f"Slice-4 catalog[{index}]")
        catalog_refs.append(_file_ref(catalog_path, "asset_catalog"))

    assets = fulfilment.get("assets")
    if not isinstance(assets, list) or not assets:
        raise SpatialFreezeError("Slice-4 has no fulfilled asset bindings")
    asset_labels = [
        str(asset.get("inventory_label"))
        for asset in assets
        if isinstance(asset, Mapping) and asset.get("inventory_label")
    ]
    required_labels = {
        str(request.get("inventory_label"))
        for request in closed_data.get("asset_requests") or []
        if isinstance(request, Mapping) and request.get("inventory_label")
    }
    if len(asset_labels) != len(set(asset_labels)) or (
        required_labels and set(asset_labels) != required_labels
    ):
        raise SpatialFreezeError("Slice-4 asset bindings are not one-to-one with requests")
    for index, asset in enumerate(assets):
        selection = asset.get("selection") if isinstance(asset, Mapping) else None
        if not isinstance(selection, Mapping) or not selection.get("file_sha256"):
            raise SpatialFreezeError(f"Slice-4 asset[{index}] has no frozen file hash")

    compile_manifest = Path(compile_manifest)
    compile_data, _compile_raw = _read_json(
        compile_manifest, "Slice-5 visual compile manifest"
    )
    _require_format(
        compile_data,
        expected="visual-reasoning-compiler/compile/v1",
        label="Slice-5 visual compile manifest",
        statuses={"compiled"},
    )
    compile_inputs = compile_data.get("inputs")
    if isinstance(compile_inputs, Mapping):
        for key, path, label in (
            ("closed_image_ir", closed_ir, "Slice-5 closed IR input"),
            ("asset_fulfilment", asset_fulfilment, "Slice-5 asset input"),
            ("asset_manifest", asset_manifest, "Slice-5 asset manifest input"),
        ):
            record = compile_inputs.get(key)
            if isinstance(record, Mapping) and record.get("sha256") != sha256_file(path):
                raise SpatialFreezeError(f"{label} hash does not match")
    project_name = project_root.name
    schema_path = project_root / f"{project_name}.schema.json"
    schema_data, schema_raw = _read_json(schema_path, "Unity schema")
    try:
        schema = UnitySchema.model_validate(schema_data)
    except (TypeError, ValueError) as exc:
        raise SpatialFreezeError("Unity schema is not valid") from exc
    schema_output = (compile_data.get("outputs") or {}).get("schema")
    if isinstance(schema_output, Mapping):
        _embedded_hash(schema_output, schema_path, label="Slice-5 schema output")

    paths = _sidecar_paths(project_root, project_name)
    sidecar_data: dict[str, dict[str, Any]] = {}
    sidecar_refs: dict[str, dict[str, Any]] = {}
    sidecar_manifests: dict[str, dict[str, Any]] = {}
    sidecar_specs = {
        "spatial_roles": (
            "schema-spatial-roles",
            "visual-reasoning-compiler/schema-spatial-roles/v1",
            {"closed"},
            "spatial-roles",
            "visual-reasoning-compiler/schema-spatial-roles-manifest/v1",
        ),
        "scene_graph": (
            "scene-relation-graph",
            "visual-reasoning-compiler/scene-relation-graph/v1",
            {"closed"},
            "scene-graph",
            "visual-reasoning-compiler/scene-relation-graph-manifest/v1",
        ),
        "spatial_fusion": (
            "hybrid-spatial-fusion",
            (
                "visual-reasoning-compiler/hybrid-spatial-fusion/v1",
                "visual-reasoning-compiler/spatial-fusion/v1",
            ),
            {"closed", "complete"},
            "spatial-fusion",
            "visual-reasoning-compiler/hybrid-spatial-fusion-manifest/v1",
        ),
        "world_layout": (
            "world-realization",
            "visual-reasoning-compiler/world-layout/v1",
            {"complete"},
            "world-layout",
            "visual-reasoning-compiler/world-layout-manifest/v1",
        ),
        "spatial_validation": (
            "spatial validation",
            "visual-reasoning-compiler/spatial-validation/v1",
            {"pass"},
            "spatial-validation",
            "visual-reasoning-compiler/spatial-validation-manifest/v1",
        ),
    }
    for key, (
        label,
        expected_format,
        statuses,
        output_key,
        manifest_format,
    ) in sidecar_specs.items():
        path = paths[key]
        data, _raw = _read_json(path, label)
        payload_format: str | tuple[str, ...] = (
            expected_format
            if isinstance(expected_format, str)
            else tuple(expected_format)
        )
        _require_format(data, expected=payload_format, label=label, statuses=statuses)
        manifest_path = path.with_name(path.stem + ".manifest.json")
        manifest_data, _record = _manifest_output(
            manifest_path,
            path,
            keys=(output_key, "spatial_validation") if key == "spatial_validation" else output_key,
            label=label,
            expected_format=manifest_format,
            statuses={"complete", "pass"},
        )
        sidecar_data[key] = data
        sidecar_refs[key] = _file_ref(path, key)
        sidecar_manifests[key] = _file_ref(manifest_path, f"{key}_manifest")
        sidecar_manifests[key]["manifest_status"] = manifest_data.get("status")

    validation = sidecar_data["spatial_validation"]
    if validation.get("errors") or validation.get("warnings"):
        raise SpatialFreezeError("Slice-6.6 report is not clean")
    try:
        live_validation = validate_spatial_layout(
            schema,
            world_layout=sidecar_data["world_layout"],
            spatial_roles=sidecar_data["spatial_roles"],
            spatial_fusion=sidecar_data["spatial_fusion"],
        )
    except (TypeError, ValueError) as exc:
        raise SpatialFreezeError("Slice-6 sidecars failed the live validation replay") from exc
    if live_validation.get("status") != "pass" or live_validation.get("errors") or live_validation.get("warnings"):
        raise SpatialFreezeError("Slice-6 sidecars do not replay to a clean validation")

    schema_ref = _file_ref(schema_path, "schema")
    schema_meta = schema.meta if isinstance(schema.meta, dict) else {}
    schema_concept = str(schema_meta.get("concept") or "").strip()
    concept_text = concept.read_text(encoding="utf-8").strip()
    if not schema_concept or schema_concept != concept_text:
        raise SpatialFreezeError(
            "schema concept lineage does not match the Slice-2 Concept text"
        )
    role_concept = (
        ((sidecar_data["spatial_roles"].get("inputs") or {}).get("concept_lineage") or {})
        .get("concept_sha256")
    )
    if role_concept != sha256_bytes(schema_concept.encode("utf-8")):
        raise SpatialFreezeError("schema role Concept hash does not match the schema")
    concept_schema_link = {
        "concept_file_sha256": concept_ref["sha256"],
        "schema_meta_concept_sha256": sha256_bytes(schema_concept.encode("utf-8")),
        "schema_roles_concept_sha256": role_concept,
        "trailing_whitespace_normalized": concept_text != concept.read_text(encoding="utf-8"),
    }
    config_ref = _file_ref(Path(config), "config") if config is not None else None
    transform_snapshot = _transform_snapshot(schema, sidecar_data["world_layout"])
    asset_bindings = {
        "fulfilment_fingerprint": fulfilment.get("fulfilment_fingerprint"),
        "catalog_preflight": fulfilment.get("catalog_preflight"),
        "catalogs": catalog_refs,
        "selections": [
            {
                "inventory_label": asset.get("inventory_label"),
                "query": asset.get("query"),
                "selection": asset.get("selection"),
            }
            for asset in assets
        ],
        "compile_binding_report": compile_data.get("binding_report"),
    }
    lineage = {
        "source": {
            "source_image": source_ref,
            "board_image": board_ref,
            "board_manifest": board_manifest_ref,
            "source_board_same_bytes": source_ref["sha256"] == board_ref["sha256"],
        },
        "slice_2": {
            "reasoning_manifest": _file_ref(Path(reasoning_manifest), "reasoning_manifest"),
            "visual_inventory": inventory_ref,
            "concept": concept_ref,
            "concept_schema_link": concept_schema_link,
            "raw_responses": raw_refs,
        },
        "slice_3": {
            "reconciliation_manifest": _file_ref(
                Path(reconciliation_manifest), "reconciliation_manifest"
            ),
            "closed_image_ir": closed_ref,
        },
        "slice_4": {
            "asset_fulfilment": _file_ref(asset_fulfilment, "asset_fulfilment"),
            "asset_manifest": _file_ref(asset_manifest, "asset_manifest"),
            "asset_bindings": asset_bindings,
        },
        "slice_5": {
            "compile_manifest": _file_ref(compile_manifest, "compile_manifest"),
            "schema": schema_ref,
        },
        "slice_6": {
            "image_spatial_evidence": _file_ref(
                image_evidence, "image_spatial_evidence"
            ),
            "image_spatial_evidence_manifest": _file_ref(
                image_evidence_manifest, "image_spatial_evidence_manifest"
            ),
            "spatial_roles": sidecar_refs["spatial_roles"],
            "spatial_roles_manifest": sidecar_manifests["spatial_roles"],
            "scene_graph": sidecar_refs["scene_graph"],
            "scene_graph_manifest": sidecar_manifests["scene_graph"],
            "spatial_fusion": sidecar_refs["spatial_fusion"],
            "spatial_fusion_manifest": sidecar_manifests["spatial_fusion"],
            "world_layout": sidecar_refs["world_layout"],
            "world_layout_manifest": sidecar_manifests["world_layout"],
            "spatial_validation": sidecar_refs["spatial_validation"],
            "spatial_validation_manifest": sidecar_manifests["spatial_validation"],
        },
        "configuration": {"config": config_ref} if config_ref else {},
    }
    lineage_basis = _without_paths({
        "lineage": lineage,
        "transforms": transform_snapshot,
        "validation": validation.get("checks"),
    })
    lineage_sha256 = _canonical_hash(lineage_basis)
    freeze_payload = {
        "format": SPATIAL_FREEZE_FORMAT,
        "stage": SPATIAL_FREEZE_STAGE,
        "status": "frozen",
        "authority": {
            "llm_called_here": False,
            "network_called_here": False,
            "schema_semantics_authority": True,
            "image_evidence_authority": closed_data.get("status") == "closed",
            "asset_catalog_substitution_here": False,
            "world_coordinates_emitted_here": False,
            "accepted_validation_required": True,
        },
        "project": {
            "name": project_name,
            "root": str(project_root),
            "schema_semantic_sha256": semantic_schema_hash(schema),
        },
        "lineage_sha256": lineage_sha256,
        "lineage": lineage,
        "transforms": transform_snapshot,
        "validation": {
            "status": validation.get("status"),
            "check_count": len(validation.get("checks") or []),
            "checks": validation.get("checks") or [],
            "report": sidecar_refs["spatial_validation"],
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.write_bytes(
            (json.dumps(freeze_payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        )
        freeze_hash = sha256_file(output)
        manifest_payload = {
            "format": SPATIAL_FREEZE_MANIFEST_FORMAT,
            "stage": SPATIAL_FREEZE_STAGE,
            "status": "frozen",
            "authority": freeze_payload["authority"],
            "inputs": {
                "lineage_sha256": lineage_sha256,
                "validation_status": validation.get("status"),
                "schema_semantic_sha256": semantic_schema_hash(schema),
            },
            "outputs": {
                "spatial_freeze": {
                    "path": str(output),
                    "sha256": freeze_hash,
                }
            },
        }
        manifest.write_bytes(
            (json.dumps(manifest_payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        )
    except OSError as exc:
        raise SpatialFreezeError("could not write Slice-6.7 freeze artifacts") from exc
    return output


__all__ = [
    "SPATIAL_FREEZE_FORMAT",
    "SPATIAL_FREEZE_MANIFEST_FORMAT",
    "SPATIAL_FREEZE_STAGE",
    "SpatialFreezeError",
    "freeze_spatial_build",
]
