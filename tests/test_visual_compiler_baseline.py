"""Phase 1 visual-compiler baseline capture is deterministic and auditable."""

from __future__ import annotations

import json

import pytest

from src.visual_compiler_baseline import (BaselineArtifactError,
                                          build_concept_loop_manifest,
                                          build_baseline_manifest,
                                          write_baseline_manifest)


def _project(root, name: str, marker: str):
    project = root / name
    project.mkdir()
    (project / ".concept.txt").write_text(
        f"# {name}\n\nA natural-language prompt.\n\n---\n\n{marker}\n",
        encoding="utf-8",
    )
    (project / f"{name}.schema.json").write_text(
        json.dumps({"project_name": name, "marker": marker}, indent=2) + "\n",
        encoding="utf-8",
    )
    (project / "playability.txt").write_text(
        f"PASS {marker}\n", encoding="utf-8"
    )
    (project / "Assets").mkdir()
    (project / "Assets" / "Main.cs").write_text(
        f"// {marker}\n", encoding="utf-8"
    )
    (project / "Library").mkdir()
    (project / "Library" / "transient.bin").write_bytes(b"ignored")
    return project


def _inputs(tmp_path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("A natural-language racing game brief.\n", encoding="utf-8")
    image = tmp_path / "reference.png"
    image.write_bytes(b"reference-image-bytes")
    config = tmp_path / "config.yaml"
    config.write_text("seed: 17\n", encoding="utf-8")
    catalog = tmp_path / "asset_catalog.json"
    catalog.write_text('{"assets": []}\n', encoding="utf-8")
    return prompt, image, config, catalog


def test_baseline_records_both_arms_and_excludes_transient_project_files(tmp_path):
    prompt, image, config, catalog = _inputs(tmp_path)
    text_project = _project(tmp_path, "TextRun", "text")
    image_project = _project(tmp_path, "ImageRun", "image")

    manifest = build_baseline_manifest(
        prompt,
        reference_image=image,
        text_project=text_project,
        image_project=image_project,
        config_file=config,
        catalog_file=catalog,
        repo_root=tmp_path,
        model="test-model",
        seed=17,
    )

    assert manifest["contract"]["prompt_transforms"] == []
    assert manifest["contract"]["reference_image_stage"] == "concept_only_step_2"
    assert manifest["contract"]["schema_receives_reference_image"] is False
    assert [arm["name"] for arm in manifest["run"]["arms"]] == [
        "text_only", "reference_image"
    ]
    assert manifest["run"]["arms"][0]["reference_image_attached"] is False
    assert manifest["run"]["arms"][1]["reference_image_attached"] is True
    assert all(
        item["path"] != "Library/transient.bin"
        for arm in manifest["run"]["arms"]
        for item in arm["project"]["files"]
    )
    assert len(manifest["identity"]["hash"]) == 32


def test_baseline_identity_is_stable_for_same_inputs(tmp_path):
    prompt, image, config, catalog = _inputs(tmp_path)
    text_project = _project(tmp_path, "TextRun", "text")
    image_project = _project(tmp_path, "ImageRun", "image")
    kwargs = {
        "reference_image": image,
        "text_project": text_project,
        "image_project": image_project,
        "config_file": config,
        "catalog_file": catalog,
        "repo_root": tmp_path,
        "model": "test-model",
        "seed": 17,
    }

    first = build_baseline_manifest(prompt, **kwargs)
    second = build_baseline_manifest(prompt, **kwargs)

    assert first["identity"] == second["identity"]
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_baseline_changes_when_an_output_changes(tmp_path):
    prompt, image, config, catalog = _inputs(tmp_path)
    text_project = _project(tmp_path, "TextRun", "text")
    image_project = _project(tmp_path, "ImageRun", "image")
    kwargs = {
        "reference_image": image,
        "text_project": text_project,
        "image_project": image_project,
        "config_file": config,
        "catalog_file": catalog,
        "repo_root": tmp_path,
        "model": "test-model",
        "seed": 17,
    }

    before = build_baseline_manifest(prompt, **kwargs)
    (image_project / "Assets" / "Main.cs").write_text("// changed\n", encoding="utf-8")
    after = build_baseline_manifest(prompt, **kwargs)

    assert before["identity"]["hash"] != after["identity"]["hash"]


def test_baseline_requires_playability_and_schema_artifacts(tmp_path):
    prompt, image, config, catalog = _inputs(tmp_path)
    text_project = _project(tmp_path, "TextRun", "text")
    image_project = _project(tmp_path, "ImageRun", "image")
    (image_project / "playability.txt").unlink()

    with pytest.raises(BaselineArtifactError, match="playability artifact"):
        build_baseline_manifest(
            prompt,
            reference_image=image,
            text_project=text_project,
            image_project=image_project,
            config_file=config,
            catalog_file=catalog,
            repo_root=tmp_path,
            model="test-model",
            seed=17,
        )


def test_write_baseline_manifest_is_sorted_and_round_trips(tmp_path):
    prompt, image, config, catalog = _inputs(tmp_path)
    text_project = _project(tmp_path, "TextRun", "text")
    image_project = _project(tmp_path, "ImageRun", "image")
    manifest = build_baseline_manifest(
        prompt,
        reference_image=image,
        text_project=text_project,
        image_project=image_project,
        config_file=config,
        catalog_file=catalog,
        repo_root=tmp_path,
        model="test-model",
        seed=17,
    )

    path = write_baseline_manifest(manifest, tmp_path / "out" / "baseline.json")
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == manifest
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_concept_loop_import_preserves_partial_status_and_lineage(tmp_path):
    loop = tmp_path / "output" / "concept_loop"
    text_run = loop / "02_text_brief_concept_only"
    image_run = loop / "01_source_image_concept_only"
    final_run = loop / "05_final_e2e_round2" / "DirtDrifter"
    text_run.mkdir(parents=True)
    image_run.mkdir(parents=True)
    (final_run / "Assets").mkdir(parents=True)
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "turbo_toys.txt").write_text(
        "text brief\n", encoding="utf-8"
    )
    (loop / "lineage.json").write_text(json.dumps({
        "experiment": "turbo_toys_image_text_concept_loop",
        "purpose": "compare text and image at Concept",
        "seed": 42,
        "pipeline_contract": {
            "image_reaches": "Step 2 Concept only",
            "post_schema_steps_run": "true",
            "schema_was_hand_authored": False,
        },
        "runs": [
            {
                "id": "01_source_image_concept_only",
                "kind": "image",
                "prompt_file": "prompts/e2e_image_only.txt",
                "input": "01_source_image_concept_only/input.png",
                "concept": "01_source_image_concept_only/concept.txt",
            },
            {
                "id": "02_text_brief_concept_only",
                "kind": "text",
                "prompt_file": "prompts/e2e_turbo_toys.txt",
                "input_source": "prompts/turbo_toys.txt",
                "input": "02_text_brief_concept_only/input.txt",
                "concept": "02_text_brief_concept_only/concept.txt",
            },
            {
                "id": "05_final_e2e_round2",
                "kind": "full_e2e",
                "input": "05_final_e2e_round2/input.png",
                "concept": "05_final_e2e_round2/concept.txt",
                "project": "05_final_e2e_round2/DirtDrifter",
            },
        ],
    }, indent=2), encoding="utf-8")
    (image_run / "input.png").write_bytes(b"image")
    (image_run / "concept.txt").write_text("image concept", encoding="utf-8")
    (text_run / "input.txt").write_text("text brief\n", encoding="utf-8")
    (text_run / "concept.txt").write_text("text concept", encoding="utf-8")
    (loop / "05_final_e2e_round2" / "input.png").write_bytes(b"feedback")
    (loop / "05_final_e2e_round2" / "concept.txt").write_text(
        "final concept", encoding="utf-8"
    )
    (final_run / ".concept.txt").write_text("final", encoding="utf-8")
    (final_run / "DirtDrifter.schema.json").write_text("{}\n", encoding="utf-8")
    (final_run / "playability.txt").write_text("98/100 PLAYABLE\n", encoding="utf-8")
    (final_run / "Assets" / "Main.cs").write_text("// generated\n", encoding="utf-8")

    manifest = build_concept_loop_manifest(loop, repo_root=tmp_path)

    assert manifest["status"] == "imported_partial"
    assert manifest["validity"]["strict_phase1"] is False
    assert manifest["contract"]["schema_receives_reference_image"] is False
    assert manifest["run"]["seed"] == 42
    assert manifest["run"]["downstream_full_e2e"]["project"]["project_name"] == "DirtDrifter"
    assert any("different carrier prompt" in reason
               for reason in manifest["validity"]["reasons"])
    assert len(manifest["identity"]["hash"]) == 32
