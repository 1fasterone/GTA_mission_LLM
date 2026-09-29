"""CLI coverage for image-only Unity project creation."""

from pathlib import Path

from typer.testing import CliRunner

import main
from cli import unity_project


def test_unity_create_accepts_image_alias_without_prompt(tmp_path, monkeypatch):
    image = tmp_path / "toy-track-reference-any-name.png"
    image.write_bytes(b"png-bytes")
    config = tmp_path / "config.yaml"
    config.write_text("lm_studio: {}\n", encoding="utf-8")
    captured = {}

    class FakePipeline:
        def __init__(self, _config):
            pass

        def run(self, prompt, **kwargs):
            captured["prompt"] = prompt
            captured["reference_image"] = kwargs["reference_image"]
            return Path(tmp_path)

    monkeypatch.setattr("cli.unity_project.UnityPipeline", FakePipeline)

    result = CliRunner().invoke(
        main.app,
        ["unity-create", "--image", str(image), "--config", str(config)],
    )

    assert result.exit_code == 0, result.stdout
    assert captured["reference_image"] == image
    assert captured["prompt"] == (
        "Create a complete game from the attached visual reference image. "
        "Treat the image as the only creative input."
    )


def test_unity_create_accepts_viz_alias_with_prompt_and_compose(tmp_path, monkeypatch):
    image = tmp_path / "basement-viz.png"
    image.write_bytes(b"png-bytes")
    canonical_board = tmp_path / "visual-run" / "board.png"
    context_path = tmp_path / "visual-run" / "context.txt"
    context_path.parent.mkdir(parents=True)
    context_path.write_text("robot | image_query=service robot\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text("lm_studio: {}\n", encoding="utf-8")
    captured = {}

    def fake_prepare_viz(source_image, output, cfg, overwrite=False):
        captured["viz_source"] = source_image
        captured["viz_output"] = output
        captured["viz_config"] = cfg
        captured["viz_overwrite"] = overwrite
        return canonical_board, context_path

    class FakePipeline:
        def __init__(self, _config):
            pass

        def run(self, prompt, **kwargs):
            captured["prompt"] = prompt
            captured["compose"] = kwargs["compose"]
            captured["reference_image"] = kwargs["reference_image"]
            captured["visual_context"] = kwargs["visual_context"]
            return Path(tmp_path)

    monkeypatch.setattr("cli.unity_project.UnityPipeline", FakePipeline)
    monkeypatch.setattr("cli.unity_project._prepare_viz_reasoning", fake_prepare_viz)

    prompt = "3d game FPS shooter, Robot enemies, bright backgrounds, in a basement room"
    result = CliRunner().invoke(
        main.app,
        [
            "unity-create", prompt, "--compose", "--viz", str(image),
            "--config", str(config),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert captured == {
        "prompt": prompt,
        "compose": True,
        "reference_image": canonical_board,
        "visual_context": "robot | image_query=service robot\n",
        "viz_source": image,
        "viz_output": None,
        "viz_config": {"lm_studio": {}},
        "viz_overwrite": False,
    }


def test_unity_create_requires_viz_for_greybox_plus():
    result = CliRunner().invoke(
        main.app,
        ["unity-create", "industrial greybox", "--greybox-plus"],
    )

    assert result.exit_code == 1
    assert "--greybox-plus requires --viz <reference-image>" in result.stdout


def test_unity_create_wires_greybox_plus_to_source_viz(tmp_path, monkeypatch):
    image = tmp_path / "industrial-reference.png"
    image.write_bytes(b"png-bytes")
    canonical_board = tmp_path / "visual-run" / "board.png"
    context_path = tmp_path / "visual-run" / "context.txt"
    context_path.parent.mkdir(parents=True)
    context_path.write_text("tank | pipe | catwalk\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text("lm_studio: {}\n", encoding="utf-8")
    captured = {}

    def fake_prepare_viz(source_image, output, cfg, overwrite=False):
        assert source_image == image
        return canonical_board, context_path

    class FakePipeline:
        def __init__(self, _config):
            pass

        def run(self, _prompt, **kwargs):
            captured.update(kwargs)
            return tmp_path

    monkeypatch.setattr("cli.unity_project.UnityPipeline", FakePipeline)
    monkeypatch.setattr("cli.unity_project._prepare_viz_reasoning", fake_prepare_viz)

    result = CliRunner().invoke(
        main.app,
        [
            "unity-create", "industrial greybox", "--greybox-plus", "--viz",
            str(image), "--config", str(config),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert captured["geometry_only"] is True
    assert captured["greybox_plus"] is True
    assert captured["greybox_plus_reference_image"] == image
    assert captured["reference_image"] == canonical_board
    assert captured["visual_context"] == "tank | pipe | catwalk\n"


def test_prepare_viz_reasoning_runs_import_and_reason_contract(tmp_path, monkeypatch):
    source = tmp_path / "rallyboat.png"
    source.write_bytes(b"source-png")
    run_dir = tmp_path / "rallyboat-viz"
    fake_llm = object()
    calls = {}

    def fake_make_chat_client(cfg):
        calls["cfg"] = cfg
        return fake_llm

    def fake_import_existing_board(**kwargs):
        calls["import"] = kwargs
        kwargs["output"].parent.mkdir(parents=True)
        kwargs["output"].write_bytes(b"canonical-png")
        kwargs["manifest"].write_text("{}\n", encoding="utf-8")
        return kwargs["manifest"]

    def fake_reason_over_board(**kwargs):
        calls["reason"] = kwargs
        return kwargs["manifest"]

    monkeypatch.setattr("src.backends.make_chat_client", fake_make_chat_client)
    monkeypatch.setattr(
        "src.visual_reasoning_compiler.import_existing_board",
        fake_import_existing_board,
    )
    monkeypatch.setattr(
        "src.visual_reasoning_compiler.reason_over_board",
        fake_reason_over_board,
    )

    board, context = unity_project._prepare_viz_reasoning(
        source,
        run_dir,
        {"lm_studio": {"model": "test-model"}},
    )

    assert board == run_dir / "board.png"
    assert context == run_dir / "context.txt"
    assert calls["import"] == {
        "board_image": source,
        "output": run_dir / "board.png",
        "manifest": run_dir / "board.import.json",
    }
    assert calls["reason"] == {
        "board_image": run_dir / "board.png",
        "inventory_output": run_dir / "visual_inventory.json",
        "concept_output": run_dir / "Concept.txt",
        "manifest": run_dir / "reasoning.json",
        "board_manifest": run_dir / "board.import.json",
        "context_output": run_dir / "context.txt",
        "llm": fake_llm,
    }


def test_prepare_viz_reasoning_replaces_existing_run_when_requested(tmp_path, monkeypatch):
    source = tmp_path / "grandm.png"
    source.write_bytes(b"source-png")
    run_dir = tmp_path / "grandm_unity_create"
    run_dir.mkdir()
    (run_dir / "stale.txt").write_text("old", encoding="utf-8")

    def fake_import_existing_board(**kwargs):
        kwargs["output"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["output"].write_bytes(b"canonical-png")
        kwargs["manifest"].write_text("{}\n", encoding="utf-8")

    def fake_reason_over_board(**kwargs):
        kwargs["inventory_output"].write_text("{}\n", encoding="utf-8")
        kwargs["concept_output"].write_text("concept\n", encoding="utf-8")
        kwargs["manifest"].write_text("{}\n", encoding="utf-8")
        kwargs["context_output"].write_text("context\n", encoding="utf-8")

    monkeypatch.setattr(
        "src.backends.make_chat_client", lambda _cfg: object()
    )
    monkeypatch.setattr(
        "src.visual_reasoning_compiler.import_existing_board",
        fake_import_existing_board,
    )
    monkeypatch.setattr(
        "src.visual_reasoning_compiler.reason_over_board",
        fake_reason_over_board,
    )

    board, context = unity_project._prepare_viz_reasoning(
        source, run_dir, {"lm_studio": {}}, overwrite=True
    )

    assert board.is_file()
    assert context.read_text(encoding="utf-8") == "context\n"
    assert not (run_dir / "stale.txt").exists()
