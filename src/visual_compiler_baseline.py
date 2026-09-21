"""Capture the Phase 1 visual-compiler baseline.

The baseline is an evidence artifact, not a pipeline stage.  It compares two
already completed Unity runs that used the same natural-language prompt,
configuration, catalog snapshot, model, and seed:

* ``text_only`` — the prompt went to Concept without a reference image;
* ``reference_image`` — the same prompt also supplied one image to Concept.

The recorder never invokes an LLM, edits a schema, or passes the image to a
post-schema step.  It hashes exact input/output bytes and derives one stable
manifest identity from those hashes, so a baseline can explain what changed
without making the image part of the production schema.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.ident.canon import chash, fhash
from src.ident.tags import BUILD


FORMAT = "visual-compiler-baseline/v1"
EXPERIMENT = "canonical-board-image-vs-text"
_TRANSIENT_PROJECT_DIRS = frozenset({
    ".git", ".vs", "Library", "Logs", "Temp", "UserSettings", "obj"
})


class BaselineArtifactError(ValueError):
    """The files needed for a trustworthy baseline are incomplete."""


def _display_path(path: Path, repo_root: Path) -> str:
    """Return a portable provenance path without using it in the identity."""
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _file_record(path: str | Path, role: str, repo_root: Path) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.is_file():
        raise BaselineArtifactError(
            f"[visual-baseline.strict] missing {role}: {file_path}. "
            "Run both pipeline arms and pass the resulting artifact path."
        )
    return {
        "role": role,
        "path": _display_path(file_path, repo_root),
        "bytes": file_path.stat().st_size,
        "hash": fhash(file_path),
    }


def _project_files(project_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(project_root.rglob("*"), key=lambda p: p.as_posix()):
        if not path.is_file():
            continue
        relative = path.relative_to(project_root)
        if any(part in _TRANSIENT_PROJECT_DIRS for part in relative.parts):
            continue
        records.append({
            "path": relative.as_posix(),
            "bytes": path.stat().st_size,
            "hash": fhash(path),
        })
    if not records:
        raise BaselineArtifactError(
            f"[visual-baseline.strict] project has no stable files: {project_root}"
        )
    return records


def _project_record(project: str | Path, repo_root: Path) -> dict[str, Any]:
    project_root = Path(project)
    if not project_root.is_dir():
        raise BaselineArtifactError(
            f"[visual-baseline.strict] project directory not found: {project_root}"
        )

    schema_files = sorted(project_root.glob("*.schema.json"), key=lambda p: p.name)
    if len(schema_files) != 1:
        raise BaselineArtifactError(
            f"[visual-baseline.strict] expected exactly one *.schema.json in "
            f"{project_root}, found {len(schema_files)}"
        )

    artifact_paths = {
        "concept": project_root / ".concept.txt",
        "schema": schema_files[0],
        "playability": project_root / "playability.txt",
    }
    artifacts = {
        name: _file_record(path, f"{name} artifact", project_root)
        for name, path in artifact_paths.items()
    }
    files = _project_files(project_root)
    tree_payload = {
        "kind": "visual-compiler-project-tree",
        "files": files,
    }
    return {
        "path": _display_path(project_root, repo_root),
        "project_name": project_root.name,
        "file_count": len(files),
        "tree_hash": chash(BUILD, tree_payload),
        "files": files,
        "artifacts": artifacts,
    }


def _identity_file(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {"bytes": record["bytes"], "hash": record["hash"]}


def _identity_project(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_name": record["project_name"],
        "file_count": record["file_count"],
        "tree_hash": record["tree_hash"],
        "files": record["files"],
        "artifacts": {
            key: _identity_file(value)
            for key, value in sorted(record["artifacts"].items())
        },
    }


def build_baseline_manifest(
    prompt_file: str | Path,
    *,
    reference_image: str | Path,
    text_project: str | Path,
    image_project: str | Path,
    config_file: str | Path,
    catalog_file: str | Path,
    repo_root: str | Path | None = None,
    model: str | None = None,
    seed: int | None = None,
) -> dict[str, Any]:
    """Build a complete, deterministic Phase 1 baseline manifest.

    ``prompt_file`` must be the unchanged natural-language brief supplied to
    ``unity-create``.  The function records that declaration in the manifest,
    but intentionally does not parse or transform the prompt.
    """
    if not model:
        raise BaselineArtifactError(
            "[visual-baseline.strict] explicit model identifier required; "
            "pass the model used by both runs"
        )
    if seed is None:
        raise BaselineArtifactError(
            "[visual-baseline.strict] explicit seed required; "
            "rerun both arms with the same --seed"
        )
    root = Path(repo_root or Path.cwd()).resolve()
    if Path(text_project).resolve() == Path(image_project).resolve():
        raise BaselineArtifactError(
            "[visual-baseline.strict] text-only and reference-image arms must "
            "point to different project directories"
        )
    prompt = _file_record(prompt_file, "natural-language prompt", root)
    image = _file_record(reference_image, "reference image", root)
    config = _file_record(config_file, "pipeline config", root)
    catalog = _file_record(catalog_file, "catalog snapshot", root)
    text_run = _project_record(text_project, root)
    image_run = _project_record(image_project, root)

    manifest: dict[str, Any] = {
        "format": FORMAT,
        "experiment": EXPERIMENT,
        "status": "captured",
        "contract": {
            "input_kind": "natural_language_brief",
            "prompt_transforms": [],
            "pipeline_steps": ["concept", "schema", "resolver", "fixer",
                                "generator", "playability"],
            "reference_image_stage": "concept_only_step_2",
            "schema_receives_reference_image": False,
            "post_schema_receives_reference_image": False,
            "production_schema_unchanged": True,
        },
        "inputs": {
            "prompt": prompt,
            "reference_image": image,
            "config": config,
            "catalog": catalog,
        },
        "run": {
            "model": model,
            "seed": seed,
            "arms": [
                {
                    "name": "text_only",
                    "reference_image_attached": False,
                    "project": text_run,
                },
                {
                    "name": "reference_image",
                    "reference_image_attached": True,
                    "project": image_run,
                },
            ],
        },
        "lineage": {
            "parent": None,
            "prompt_hash": prompt["hash"],
            "reference_image_hash": image["hash"],
            "text_arm": "prompt -> concept -> schema -> deterministic tail",
            "reference_arm": (
                "prompt + reference_image -> concept -> schema -> "
                "deterministic tail"
            ),
        },
    }

    identity_payload = {
        "format": FORMAT,
        "experiment": EXPERIMENT,
        "contract": manifest["contract"],
        "inputs": {
            key: _identity_file(value)
            for key, value in sorted(manifest["inputs"].items())
        },
        "run": {
            "model": model,
            "seed": seed,
            "arms": [
                {
                    "name": arm["name"],
                    "reference_image_attached": arm["reference_image_attached"],
                    "project": _identity_project(arm["project"]),
                }
                for arm in manifest["run"]["arms"]
            ],
        },
    }
    manifest["identity"] = {
        "hash": chash(BUILD, identity_payload),
        "payload": identity_payload,
    }
    return manifest


def write_baseline_manifest(manifest: dict[str, Any], output: str | Path) -> Path:
    """Write a stable JSON manifest and return its path."""
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path


def _legacy_run_record(loop_root: Path, run: dict[str, Any],
                       repo_root: Path, missing: list[str]) -> dict[str, Any]:
    """Normalize one pre-existing concept-loop lineage entry."""
    run_id = str(run.get("id") or "unnamed")
    record: dict[str, Any] = {
        "id": run_id,
        "kind": str(run.get("kind") or "unknown"),
        "execution": str(run.get("execution") or ""),
        "parent_concept": run.get("parent_concept"),
        "declared_input_sha256": run.get("input_sha256"),
    }

    for field, role in (("input", "legacy run input"),
                        ("concept", "legacy concept output")):
        relative = run.get(field)
        if not relative:
            missing.append(f"{run_id}:lineage.{field}")
            record[field] = None
            continue
        path = loop_root / str(relative)
        if not path.is_file():
            missing.append(f"{run_id}:{relative}")
            record[field] = {"declared_path": str(relative).replace("\\", "/")}
            continue
        record[field] = _file_record(path, role, repo_root)

    prompt_file = run.get("input_source") or run.get("prompt_file")
    if prompt_file:
        prompt_path = repo_root / str(prompt_file)
        if prompt_path.is_file():
            record["prompt"] = _file_record(
                prompt_path, "legacy declared prompt", repo_root
            )
        else:
            missing.append(f"{run_id}:declared prompt {prompt_file}")
            record["prompt"] = {
                "declared_path": str(prompt_file).replace("\\", "/")
            }
    else:
        record["prompt"] = None

    project = run.get("project")
    if project:
        project_path = loop_root / str(project)
        if project_path.is_dir():
            record["project"] = _project_record(project_path, repo_root)
        else:
            missing.append(f"{run_id}:project {project}")
            record["project"] = {"declared_path": str(project).replace("\\", "/")}
    else:
        record["project"] = None
    return record


def _legacy_identity_run(record: dict[str, Any]) -> dict[str, Any]:
    def _identity(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if "hash" in value and "bytes" in value:
            return _identity_file(value)
        if "project_name" in value:
            return _identity_project(value)
        if "declared_path" in value:
            return {"declared_path": value["declared_path"]}
        return {key: _identity(item) for key, item in sorted(value.items())}

    return {
        key: _identity(value)
        for key, value in sorted(record.items())
        if key != "execution"
    }


def build_concept_loop_manifest(
    loop_root: str | Path,
    *,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Import an existing Concept-loop ``lineage.json`` as Phase 1 evidence.

    Older experiments may have real Concept-stage evidence without the strict
    same-prompt/full-project provenance required by ``build_baseline_manifest``.
    Importing them preserves that evidence while making the missing guarantees
    explicit instead of silently upgrading the experiment's status.
    """
    root = Path(repo_root or Path.cwd()).resolve()
    loop = Path(loop_root).resolve()
    lineage_path = loop / "lineage.json"
    if not lineage_path.is_file():
        raise BaselineArtifactError(
            f"[visual-baseline.strict] concept-loop lineage not found: {lineage_path}"
        )
    try:
        lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineArtifactError(
            f"[visual-baseline.strict] cannot read concept-loop lineage: {lineage_path}"
        ) from exc
    if not isinstance(lineage, dict) or not isinstance(lineage.get("runs"), list):
        raise BaselineArtifactError(
            f"[visual-baseline.strict] lineage must contain a runs list: {lineage_path}"
        )

    missing: list[str] = []
    source_lineage = _file_record(lineage_path, "concept-loop lineage", root)
    runs = [_legacy_run_record(loop, run, root, missing)
            for run in lineage["runs"] if isinstance(run, dict)]
    text_run = next((run for run in runs if run["kind"] == "text"), None)
    image_run = next(
        (run for run in runs
         if run["kind"] == "image" and "concept_only" in run["id"]),
        next((run for run in runs if run["kind"] == "image"), None),
    )
    downstream = next((run for run in runs if run["kind"] == "full_e2e"), None)

    validity_reasons = [
        "historical Concept-loop capture, not a strict same-prompt A/B run",
        "text and image arms declare different carrier prompt files",
        "model, config, and catalog snapshot are not recorded in lineage.json",
        "only the image-feedback arm has a completed Steps 2-7 project",
    ]
    manifest: dict[str, Any] = {
        "format": FORMAT,
        "experiment": str(lineage.get("experiment") or "legacy-concept-loop"),
        "status": "imported_partial",
        "source": {
            "root": _display_path(loop, root),
            "lineage": source_lineage,
        },
        "contract": {
            "input_kind": "legacy_concept_loop",
            "prompt_transforms": [
                "image arm used its declared image-only carrier prompt",
                "generated-image feedback rounds are retained as separate lineage",
            ],
            "pipeline_steps": ["concept", "schema", "resolver", "fixer",
                                "generator", "playability"],
            "reference_image_stage": str(
                (lineage.get("pipeline_contract") or {}).get(
                    "image_reaches", "Step 2 Concept only"
                )
            ),
            "schema_receives_reference_image": False,
            "post_schema_receives_reference_image": bool(
                (lineage.get("pipeline_contract") or {}).get(
                    "post_schema_steps_run", "false"
                ) == "true"
            ),
            "production_schema_unchanged": not bool(
                (lineage.get("pipeline_contract") or {}).get(
                    "schema_was_hand_authored", True
                )
            ),
            "same_prompt_across_arms": False,
        },
        "inputs": {
            "text_arm": text_run["input"] if text_run else None,
            "image_arm": image_run["input"] if image_run else None,
        },
        "run": {
            "model": None,
            "seed": lineage.get("seed"),
            "arms": [
                {
                    "name": "text_concept_only",
                    "reference_image_attached": False,
                    "concept_only": True,
                    "evidence": text_run,
                },
                {
                    "name": "reference_image_concept_only",
                    "reference_image_attached": True,
                    "concept_only": True,
                    "evidence": image_run,
                },
            ],
            "downstream_full_e2e": downstream,
        },
        "lineage": {
            "parent": None,
            "source_lineage_hash": source_lineage["hash"],
            "declared_experiment": lineage.get("purpose"),
            "generated_images": lineage.get("generated_images", []),
        },
        "validity": {
            "strict_phase1": False,
            "reasons": validity_reasons,
            "missing_or_unverifiable": sorted(set(missing)),
        },
        "legacy_runs": runs,
    }
    identity_payload = {
        "format": FORMAT,
        "experiment": manifest["experiment"],
        "source_lineage": _identity_file(source_lineage),
        "contract": manifest["contract"],
        "seed": manifest["run"]["seed"],
        "runs": [_legacy_identity_run(run) for run in runs],
    }
    manifest["identity"] = {
        "hash": chash(BUILD, identity_payload),
        "payload": identity_payload,
    }
    return manifest
