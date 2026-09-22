from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.visual_reasoning_compiler import (
    DEFAULT_NEGATIVE_PROMPT,
    FORMAT,
    ComfyVizConceptClient,
    VisualReasoningCompilerError,
    build_vizconcept_prompt,
    build_workflow,
    import_existing_board,
    sha256_file,
)


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"test-board-bytes-which-are-long-enough"


def test_vizconcept_prompt_is_visual_only_and_preserves_source() -> None:
    prompt = build_vizconcept_prompt("pixel-art git forest with a branch sign")

    assert "VISUAL CONCEPT:" in prompt
    assert "pixel-art git forest with a branch sign" in prompt
    assert "Concept.txt" not in prompt
    assert "Sketchfab" not in prompt


def test_empty_vizconcept_is_rejected() -> None:
    with pytest.raises(VisualReasoningCompilerError, match="empty"):
        build_vizconcept_prompt(" \n\t")


def test_workflow_wires_checkpoint_clip_and_sampler() -> None:
    graph = build_workflow(
        checkpoint="model.safetensors",
        positive="positive",
        negative="negative",
        seed=7,
        width=768,
        height=512,
        steps=20,
        cfg=8.0,
        sampler="dpmpp_2m_sde_gpu",
        scheduler="karras",
    )

    assert graph["4"]["class_type"] == "CheckpointLoaderSimple"
    assert graph["4"]["inputs"]["ckpt_name"] == "model.safetensors"
    assert graph["6"]["inputs"] == {"text": "positive", "clip": ["4", 1]}
    assert graph["7"]["inputs"] == {"text": "negative", "clip": ["4", 1]}
    assert graph["3"]["inputs"]["sampler_name"] == "dpmpp_2m_sde_gpu"
    assert graph["3"]["inputs"]["scheduler"] == "karras"
    assert graph["5"]["inputs"]["width"] == 768
    assert graph["5"]["inputs"]["height"] == 512


class FakeComfyClient(ComfyVizConceptClient):
    def __init__(self) -> None:
        super().__init__(base_url="http://fake", poll_timeout=0.01)
        self.freed = False
        self.submitted: dict | None = None

    def checkpoint_names(self) -> list[str]:
        return ["model.safetensors"]

    def _post_json(self, path: str, payload: dict) -> dict:
        assert path == "/prompt"
        self.submitted = payload
        return {"prompt_id": "prompt-1"}

    def _get_json(self, path: str) -> dict:
        assert path == "/history/prompt-1"
        return {
            "prompt-1": {
                "status": {"status_str": "success"},
                "outputs": {"9": {"images": [{
                    "filename": "board.png",
                    "subfolder": "",
                    "type": "output",
                }]}},
            }
        }

    def _fetch_image(self, image_ref: dict) -> bytes:
        assert image_ref["filename"] == "board.png"
        return PNG_BYTES

    def free_models(self) -> bool:
        self.freed = True
        return True


def test_generate_writes_png_and_provenance_manifest(tmp_path: Path) -> None:
    source = tmp_path / "vizconcept.txt"
    source.write_text("pixel-art Git forest", encoding="utf-8")
    output = tmp_path / "run" / "board.png"
    manifest = tmp_path / "run" / "board.comfy.json"
    client = FakeComfyClient()

    result = client.generate(
        vizconcept_file=source,
        output=output,
        manifest=manifest,
        checkpoint="model.safetensors",
        seed=7,
        width=768,
        height=512,
        steps=20,
        cfg=8.0,
        sampler="dpmpp_2m_sde_gpu",
        scheduler="karras",
    )

    assert result == output
    assert output.read_bytes() == PNG_BYTES
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["format"] == FORMAT
    assert data["stage"] == "1_visual_concept_to_board"
    assert data["authority"]["concept_generated_here"] is False
    assert data["authority"]["llm_called_here"] is False
    assert data["source"]["text"] == "pixel-art Git forest"
    assert data["board"]["sha256"] == sha256_file(output)
    assert data["prompt"]["negative"] == DEFAULT_NEGATIVE_PROMPT
    assert client.submitted is not None


def test_generate_refuses_existing_artifacts(tmp_path: Path) -> None:
    source = tmp_path / "vizconcept.txt"
    source.write_text("concept", encoding="utf-8")
    output = tmp_path / "board.png"
    output.write_bytes(PNG_BYTES)
    manifest = tmp_path / "board.comfy.json"
    client = FakeComfyClient()

    with pytest.raises(VisualReasoningCompilerError, match="overwrite"):
        client.generate(
            vizconcept_file=source,
            output=output,
            manifest=manifest,
            checkpoint="model.safetensors",
        )


def test_import_existing_board_preserves_image_and_links_vizconcept(
    tmp_path: Path,
) -> None:
    source_image = tmp_path / "known-good.png"
    source_image.write_bytes(PNG_BYTES)
    vizconcept = tmp_path / "vizconcept.txt"
    vizconcept.write_text("pixel-art Git graph", encoding="utf-8")
    output = tmp_path / "run" / "board.png"
    manifest = tmp_path / "run" / "board.import.json"

    result = import_existing_board(
        board_image=source_image,
        output=output,
        manifest=manifest,
        vizconcept_file=vizconcept,
    )

    assert result == output
    assert output.read_bytes() == PNG_BYTES
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["format"] == "visual-reasoning-compiler/imported-board/v1"
    assert data["stage"] == "1_visual_board_import"
    assert data["source_board"]["sha256"] == sha256_file(source_image)
    assert data["vizconcept"]["text"] == "pixel-art Git graph"
    assert data["generation"]["known"] is False


def test_import_rejects_non_png(tmp_path: Path) -> None:
    source_image = tmp_path / "not-an-image.png"
    source_image.write_bytes(b"not png")

    with pytest.raises(VisualReasoningCompilerError, match="PNG"):
        import_existing_board(
            board_image=source_image,
            output=tmp_path / "board.png",
            manifest=tmp_path / "board.import.json",
        )
