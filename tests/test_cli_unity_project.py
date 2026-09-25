"""CLI coverage for image-only Unity project creation."""

from pathlib import Path

from typer.testing import CliRunner

import main


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
