"""Write the immutable semantic Concept authority artifact.

This module is intentionally small and LLM-free.  The LLM call that produces
the concept lives in ``UnityPipeline.run_concept_only``; this module only
persists the returned text and exact provenance so later visual stages cannot
silently replace the semantic source.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.ident.canon import chash, fhash
from src.ident.tags import BUILD


FORMAT = "visual-compiler-semantic-authority/v1"


class SemanticAuthorityError(ValueError):
    """The authority artifact cannot be created without overwriting evidence."""


def _display_path(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _file_record(path: Path, role: str, repo_root: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SemanticAuthorityError(f"[semantic-authority] missing {role}: {path}")
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


def write_semantic_authority(
    concept: str,
    output: str | Path,
    *,
    prompt_text: str,
    prompt_source: str | Path | None = None,
    config_file: str | Path | None = None,
    model: str | None = None,
    seed: int | None = None,
    repo_root: str | Path | None = None,
) -> tuple[Path, Path]:
    """Write ``Concept.txt`` and its immutable provenance manifest.

    Existing files are never overwritten.  A caller that wants a new capture
    must choose a new output path, preserving every previous authority.
    """
    if not concept.strip():
        raise SemanticAuthorityError("[semantic-authority] empty concept refused")
    if not prompt_text.strip():
        raise SemanticAuthorityError("[semantic-authority] empty source prompt refused")

    root = Path(repo_root or Path.cwd()).resolve()
    output_path = Path(output)
    manifest_path = output_path.with_name(output_path.stem + ".authority.json")
    if output_path.exists():
        raise SemanticAuthorityError(
            f"[semantic-authority] refusing to overwrite Concept.txt: {output_path}"
        )
    if manifest_path.exists():
        raise SemanticAuthorityError(
            f"[semantic-authority] refusing to overwrite authority manifest: {manifest_path}"
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(concept.strip() + "\n", encoding="utf-8")
    concept_record = _file_record(output_path, "Concept.txt", root)

    if prompt_source is not None:
        prompt_path = Path(prompt_source)
        prompt_record = _file_record(prompt_path, "semantic source prompt", root)
    else:
        prompt_record = {
            "role": "semantic source prompt",
            "path": None,
            "bytes": len(prompt_text.encode("utf-8")),
            "hash": chash(BUILD, {"text": prompt_text}),
        }

    config_record = None
    if config_file is not None:
        config_record = _file_record(Path(config_file), "pipeline config", root)

    manifest: dict[str, Any] = {
        "format": FORMAT,
        "authority": "semantic",
        "status": "immutable_capture",
        "concept": concept_record,
        "source_prompt": prompt_record,
        "config": config_record,
        "run": {
            "model": model,
            "seed": seed,
            "reference_image": None,
            "board": None,
            "visual_dictionary": None,
        },
        "contract": {
            "concept_is_semantic_truth": True,
            "image_attached": False,
            "schema_generated": False,
            "post_schema_steps_run": False,
            "llm_invoked_for_visual_annotation": False,
        },
    }
    identity_payload = {
        "format": FORMAT,
        "authority": "semantic",
        "concept": _identity_file(concept_record),
        "source_prompt": _identity_file(prompt_record),
        "config": _identity_file(config_record),
        "run": manifest["run"],
        "contract": manifest["contract"],
    }
    manifest["identity"] = {
        "hash": chash(BUILD, identity_payload),
        "payload": identity_payload,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path, manifest_path
