"""Build a deterministic visual shopping list from a completed Unity run.

This is a post-schema evidence reader, not a pipeline stage.  It never calls
the LLM and never edits the schema.  The schema's declared entities are the
shopping-list demand; ``meta.entity_model_paths`` is the resolver fulfilment
candidate.  Missing or semantically unrelated candidates remain visible as
explicit review states instead of being replaced with a default prop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.ident.canon import chash, fhash
from src.ident.tags import BUILD
from src.unity_concept_fidelity import check_asset_fidelity
from src.unity_schema import UnitySchema


FORMAT = "visual-compiler-shopping-list/v1"


class ShoppingListArtifactError(ValueError):
    """The completed project is missing evidence needed for fulfilment."""


def _display_path(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _file_record(path: Path, role: str, repo_root: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ShoppingListArtifactError(
            f"[visual-shopping-list] missing {role}: {path}"
        )
    return {
        "role": role,
        "path": _display_path(path, repo_root),
        "bytes": path.stat().st_size,
        "hash": fhash(path),
    }


def _identity_file(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {"bytes": record["bytes"], "hash": record["hash"]}


def _schema_path(project: Path) -> Path:
    paths = sorted(project.glob("*.schema.json"), key=lambda p: p.name)
    if len(paths) != 1:
        raise ShoppingListArtifactError(
            f"[visual-shopping-list] expected exactly one *.schema.json in "
            f"{project}, found {len(paths)}"
        )
    return paths[0]


def build_shopping_list(
    project: str | Path,
    *,
    board_image: str | Path | None = None,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Build one fulfilment list from one completed Unity project.

    ``board_image`` is optional for production runs that have no visual board,
    but should be supplied for the visual-compiler experiment so the list
    retains the exact image bytes used to create the Concept.
    """
    root = Path(repo_root or Path.cwd()).resolve()
    project_path = Path(project).resolve()
    if not project_path.is_dir():
        raise ShoppingListArtifactError(
            f"[visual-shopping-list] project directory not found: {project_path}"
        )

    schema_path = _schema_path(project_path)
    schema_record = _file_record(schema_path, "schema artifact", root)
    concept_path = project_path / ".concept.txt"
    concept_record = _file_record(concept_path, "concept artifact", project_path)
    playability_path = project_path / "playability.txt"
    playability_record = (
        _file_record(playability_path, "playability artifact", project_path)
        if playability_path.is_file() else None
    )
    board_record = (
        _file_record(Path(board_image).resolve(), "board image", root)
        if board_image is not None else None
    )

    try:
        raw = json.loads(schema_path.read_text(encoding="utf-8"))
        schema = UnitySchema.model_validate(raw)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ShoppingListArtifactError(
            f"[visual-shopping-list] invalid Unity schema: {schema_path}"
        ) from exc

    entities = raw.get("entities")
    if not isinstance(entities, list) or not entities:
        raise ShoppingListArtifactError(
            f"[visual-shopping-list] schema declares no entities: {schema_path}"
        )
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    model_paths = meta.get("entity_model_paths")
    if not isinstance(model_paths, dict):
        model_paths = {}

    asset_gaps = check_asset_fidelity(schema, {
        str(name): str(path)
        for name, path in model_paths.items()
        if path is not None
    })
    gap_by_entity = {gap.demand: gap for gap in asset_gaps}

    entries: list[dict[str, Any]] = []
    for entity in sorted(entities, key=lambda item: str(item.get("name", ""))):
        if not isinstance(entity, dict):
            raise ShoppingListArtifactError(
                f"[visual-shopping-list] malformed entity in {schema_path}"
            )
        name = str(entity.get("name") or "").strip()
        if not name:
            raise ShoppingListArtifactError(
                f"[visual-shopping-list] entity without a name in {schema_path}"
            )
        target = str(entity.get("model_hint") or "").strip()
        sketchfab_query = str(entity.get("sketchfab_query") or "").strip()
        candidate = str(model_paths.get(name) or "").strip()
        gap = gap_by_entity.get(name)
        if not target:
            status = "unfulfilled_no_visual_target"
        elif not candidate:
            status = "unfulfilled_no_resolved_candidate"
        elif gap is not None:
            status = "review_required_asset_gap"
        else:
            status = "candidate_requires_visual_review"
        entries.append({
            "entity": name,
            "kind": str(entity.get("kind") or ""),
            "tag": str(entity.get("tag") or ""),
            "count": entity.get("count", 1),
            "visual_target": target,
            "sketchfab_query": sketchfab_query or None,
            "query_board_status": (
                "query_present" if sketchfab_query
                else "unfulfilled_no_sketchfab_query"
            ),
            "candidate": candidate or None,
            "status": status,
            "asset_gap": gap.__dict__ if gap is not None else None,
        })

    fidelity = {
        "asset_gap_count": len(asset_gaps),
        "asset_gaps": [gap.__dict__ for gap in asset_gaps],
    }
    query_board = {
        "status": "captured_from_completed_schema",
        "authority": "legacy_schema_export",
        "entries": [
            {
                "entity": entry["entity"],
                "kind": entry["kind"],
                "count": entry["count"],
                "model_hint": entry["visual_target"],
                "sketchfab_query": entry["sketchfab_query"],
                "status": entry["query_board_status"],
            }
            for entry in entries
        ],
    }
    manifest: dict[str, Any] = {
        "format": FORMAT,
        "status": "captured",
        "project": {
            "path": _display_path(project_path, root),
            "name": project_path.name,
        },
        "inputs": {
            "board_image": board_record,
            "concept": concept_record,
            "schema": schema_record,
            "playability": playability_record,
        },
        "entries": entries,
        "query_board": query_board,
        "fidelity": fidelity,
        "contract": {
            "source": "schema.entities + schema.meta.entity_model_paths",
            "query_board_source": "schema.entities[].sketchfab_query",
            "schema_mutated": False,
            "llm_invoked": False,
            "unresolved_is_explicit": True,
        },
    }
    identity_payload = {
        "format": FORMAT,
        "project_name": project_path.name,
        "inputs": {
            key: _identity_file(value)
            for key, value in sorted(manifest["inputs"].items())
        },
        "entries": entries,
        "query_board": query_board,
        "fidelity": fidelity,
        "contract": manifest["contract"],
    }
    manifest["identity"] = {
        "hash": chash(BUILD, identity_payload),
        "payload": identity_payload,
    }
    return manifest


def write_shopping_list(manifest: dict[str, Any], output: str | Path) -> Path:
    """Write a stable JSON sidecar and return its path."""
    output_path = Path(output)
    if output_path.exists():
        raise ShoppingListArtifactError(
            f"[visual-shopping-list] refusing to overwrite existing sidecar: "
            f"{output_path}"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path
