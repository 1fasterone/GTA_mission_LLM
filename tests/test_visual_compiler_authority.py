from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.visual_compiler_authority import (
    SemanticAuthorityError,
    write_semantic_authority,
)


def test_semantic_authority_writes_concept_and_manifest(tmp_path: Path) -> None:
    prompt = tmp_path / "semantic.txt"
    prompt.write_text("A racer must beat three rivals over three laps.", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text("lm_studio:\n  model: test\n", encoding="utf-8")

    concept, manifest = write_semantic_authority(
        "The player races three rivals over three laps.",
        tmp_path / "Concept.txt",
        prompt_text=prompt.read_text(encoding="utf-8"),
        prompt_source=prompt,
        config_file=config,
        model="test-model",
        seed=42,
        repo_root=tmp_path,
    )

    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert concept.read_text(encoding="utf-8").endswith("\n")
    assert data["authority"] == "semantic"
    assert data["contract"]["image_attached"] is False
    assert data["contract"]["schema_generated"] is False
    assert data["concept"]["hash"]
    assert data["source_prompt"]["hash"]
    assert data["identity"]["hash"]


def test_semantic_authority_never_overwrites(tmp_path: Path) -> None:
    output = tmp_path / "Concept.txt"
    output.write_text("keep me\n", encoding="utf-8")

    with pytest.raises(SemanticAuthorityError, match="overwrite"):
        write_semantic_authority(
            "new concept",
            output,
            prompt_text="prompt",
            repo_root=tmp_path,
        )
