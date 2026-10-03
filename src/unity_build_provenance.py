"""Deterministic provenance for a generated Unity project.

The project directory is an output, not the source of truth.  This manifest
binds it to the final schema IR and the checked-in compiler inputs which
rendered it, without serialising credentials or wall-clock timestamps.  It is
therefore useful both for a reproducibility check and for explaining why two
otherwise similar builds differ.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from .unity_schema import UnitySchema
from .unity_spatial_identity import semantic_schema_hash
from .ident import Q_ROT, chash, fhash, q
from .ident.tags import BUILD


FORMAT = "game-o-matic/unity-build-provenance/v1"
MANIFEST_NAME = "Project.build.manifest.json"
_SECRET_MARKERS = ("token", "secret", "password", "api_key", "apikey")
_STATUSES = frozenset({"accepted", "replayed_unverified", "unverified"})


def _float_free(value: Any) -> Any:
    """Quantize numeric evidence before it crosses the identity boundary.

    ``src.ident`` deliberately refuses raw floats.  Build provenance contains
    general schema/config data rather than one narrow geometry type, so it
    carries each real as an explicitly tagged 1e-6 integer.  The tag prevents
    a quantized float from being confused with an authored integer of the same
    value while retaining cross-machine reproducibility.
    """
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return {"float_q1e6": q(value, Q_ROT)}
    if isinstance(value, Mapping):
        return {str(key): _float_free(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_float_free(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_float_free(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(
            item, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    raise TypeError(f"unsupported provenance identity value: {type(value)!r}")


def _build_identity(value: Any) -> str:
    """Hash structured build evidence through the canonical identity substrate."""
    return chash(BUILD, _float_free(value))


def _safe_config(value: Any) -> Any:
    """Return config-shaped data with secret values removed before hashing."""
    if isinstance(value, Mapping):
        return {
            str(key): ("<redacted>" if any(marker in str(key).lower()
                                             for marker in _SECRET_MARKERS)
                       else _safe_config(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_config(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_config(item) for item in value]
    return value


def _source_tree(path: Path, *, suffixes: set[str] | None = None) -> dict[str, Any]:
    """Fingerprint a compiler input tree with stable relative paths."""
    root = Path(path)
    if not root.is_dir():
        return {"status": "missing", "file_count": 0, "identity": None, "files": []}
    rows: list[dict[str, str]] = []
    for candidate in sorted(root.rglob("*"), key=lambda item: item.as_posix().lower()):
        if not candidate.is_file():
            continue
        if suffixes is not None and candidate.suffix.lower() not in suffixes:
            continue
        rows.append({
            "path": candidate.relative_to(root).as_posix(),
            "identity": fhash(candidate),
        })
    return {
        "status": "complete",
        "file_count": len(rows),
        "identity": _build_identity(rows),
        "files": rows,
    }


def _generated_output_index(project_root: Path) -> dict[str, Any]:
    """Hash only generator-owned output, never arbitrary Unity/user files."""
    generated = project_root / ".generated_files.json"
    declared: list[str] = []
    if generated.is_file():
        try:
            decoded = json.loads(generated.read_text(encoding="utf-8"))
            if isinstance(decoded, list):
                declared = sorted({item for item in decoded if isinstance(item, str)})
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            declared = []
    rows: list[dict[str, str | None]] = []
    root_resolved = project_root.resolve()
    output_resolved = project_root.parent.resolve()
    for relpath in declared:
        relative = Path(relpath)
        if relative.is_absolute():
            continue
        # Generator manifests are relative to output_dir and therefore include
        # the project directory (for example, ``Project/Assets/Game.cs``).
        # Older manifests can be project-relative (``Assets/Game.cs``). Accept
        # either form, but only when the resolved path remains inside this
        # project: a manifest is evidence, never an authority to hash arbitrary
        # output-directory or user files.
        target = output_resolved / relative
        try:
            if not target.resolve().is_relative_to(root_resolved):
                target = root_resolved / relative
                if not target.resolve().is_relative_to(root_resolved):
                    continue
        except OSError:
            continue
        rows.append({
            "path": relpath.replace("\\", "/"),
            "identity": fhash(target) if target.is_file() else None,
        })
    return {
        "declared_file_count": len(rows),
        "identity": _build_identity(rows),
        "generated_files_manifest_identity": fhash(generated) if generated.is_file() else None,
        "files": rows,
    }


def _sidecar_hashes(project_root: Path, project_name: str) -> dict[str, str | None]:
    names = (
        f"{project_name}.schema.json",
        f"{project_name}.spatial-roles.json",
        f"{project_name}.scene-graph.json",
        f"{project_name}.spatial-fusion.json",
        f"{project_name}.world-layout.json",
        f"{project_name}.spatial-validation.json",
        "playability.txt",
        ".concept.txt",
        "Packages/packages-lock.json",
    )
    return {
        name: fhash(project_root / name) if (project_root / name).is_file() else None
        for name in names
    }


def build_provenance(
    schema: UnitySchema,
    *,
    project_root: Path,
    manifest_deps: Mapping[str, str],
    config: Mapping[str, Any],
    template_dir: str | Path,
    lookups_dir: str | Path,
    source_schema_path: str | Path | None = None,
    status: str = "unverified",
) -> dict[str, Any]:
    """Build, but do not write, the stable provenance payload."""
    if status not in _STATUSES:
        raise ValueError(f"unsupported build provenance status: {status!r}")
    root = Path(project_root)
    source_path = Path(source_schema_path) if source_schema_path else None
    source_record = None
    if source_path is not None and source_path.is_file():
        source_record = {
            "name": source_path.name,
            "identity": fhash(source_path),
        }
    safe_config = _safe_config(dict(config))
    dependencies = {str(key): str(value) for key, value in manifest_deps.items()}
    return {
        "format": FORMAT,
        "status": status,
        "schema": {
            "semantic_sha256": semantic_schema_hash(schema),
            "canonical_identity": _build_identity(schema.model_dump(mode="json")),
            "source": source_record,
        },
        "compiler_inputs": {
            "config_identity": _build_identity(safe_config),
            "templates": _source_tree(Path(template_dir), suffixes={".j2"}),
            "lookups": _source_tree(Path(lookups_dir), suffixes={".json", ".yaml", ".yml"}),
            "manifest_dependencies": {
                "identity": _build_identity(dependencies),
                "values": dict(sorted(dependencies.items())),
            },
        },
        "outputs": {
            "generated_files": _generated_output_index(root),
            "compiler_sidecars": _sidecar_hashes(root, schema.project_name),
            "entity_asset_bindings": (schema.meta or {}).get("entity_model_bindings", {}),
        },
    }


def write_build_provenance(
    schema: UnitySchema,
    *,
    project_root: str | Path,
    manifest_deps: Mapping[str, str],
    config: Mapping[str, Any],
    template_dir: str | Path,
    lookups_dir: str | Path,
    source_schema_path: str | Path | None = None,
    status: str = "unverified",
) -> Path:
    """Atomically write ``Project.build.manifest.json`` and return its path."""
    root = Path(project_root)
    root.mkdir(parents=True, exist_ok=True)
    payload = build_provenance(
        schema,
        project_root=root,
        manifest_deps=manifest_deps,
        config=config,
        template_dir=template_dir,
        lookups_dir=lookups_dir,
        source_schema_path=source_schema_path,
        status=status,
    )
    target = root / MANIFEST_NAME
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8") + b"\n")
    os.replace(temporary, target)
    return target


__all__ = ["FORMAT", "MANIFEST_NAME", "build_provenance", "write_build_provenance"]
