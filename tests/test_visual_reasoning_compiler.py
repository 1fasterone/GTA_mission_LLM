from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.visual_reasoning_compiler import (
    DEFAULT_NEGATIVE_PROMPT,
    FORMAT,
    CONCEPT_USER,
    INVENTORY_FORMAT,
    INVENTORY_USER,
    ComfyVizConceptClient,
    VisualReasoningCompilerError,
    build_vizconcept_prompt,
    build_workflow,
    import_existing_board,
    reason_over_board,
    sha256_file,
    validate_visual_inventory,
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


def test_image_reasoning_prompts_cover_discrete_course_objects() -> None:
    assert "ramps, buoys or barrier" in INVENTORY_USER
    assert 'category "ui" and model_hint null' in INVENTORY_USER
    assert "one short ordinary noun phrase" in INVENTORY_USER
    assert "No commentary, explanations" in INVENTORY_USER
    assert "exactly one discrete visible object" in CONCEPT_USER
    assert "describe a rocky canyon" in CONCEPT_USER
    assert "ordinary visible noun" in CONCEPT_USER
    assert "Include the foreground" in CONCEPT_USER
    assert 'write "boat" for a boat' in CONCEPT_USER
    assert "stands, bridges, ramps" in CONCEPT_USER
    assert "Describe what you see in the image" in CONCEPT_USER


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


def _inventory_response() -> str:
    return json.dumps({
        "scene_summary": "neon commit graph screen",
        "visible_elements": [
            {
                "label": "red-hat explorer",
                "category": "character",
                "description": "red-hat explorer backpack",
                "model_hint": "red-hat explorer backpack",
            },
            {
                "label": "glowing commit nodes",
                "category": "prop",
                "description": "glowing orb node",
                "model_hint": "glowing orb node",
            },
            {
                "label": "neon grid background",
                "category": "environment",
                "description": "neon grid background",
                "model_hint": None,
            },
        ],
        "sketchfab_queries": [
            {"label": "red-hat explorer", "query": "red-hat explorer backpack"},
            {"label": "glowing commit nodes", "query": "glowing orb node"},
        ],
        "visible_text": ["GIT ADVENTURE", "COMMIT", "BRANCH", "MERGE"],
        "uncertainties": ["orb depth"],
    })


def test_visual_inventory_contract_is_strict_and_canonical() -> None:
    inventory = validate_visual_inventory(_inventory_response())

    assert inventory["format"] == INVENTORY_FORMAT
    assert inventory["visible_elements"][0]["label"] == "red-hat explorer"
    assert inventory["sketchfab_queries"][1]["label"] == "glowing commit nodes"

    malformed = json.loads(_inventory_response())
    malformed["sketchfab_queries"][0]["label"] = "not-visible"
    with pytest.raises(VisualReasoningCompilerError, match="unknown visible"):
        validate_visual_inventory(malformed)


def test_visual_inventory_rejects_commentary_and_inferred_roles() -> None:
    malformed = json.loads(_inventory_response())
    malformed["visible_elements"][0]["label"] = "the player's boat"

    with pytest.raises(VisualReasoningCompilerError, match="commentary|semantic"):
        validate_visual_inventory(malformed)

    malformed = json.loads(_inventory_response())
    malformed["sketchfab_queries"][0]["query"] = "checkpoint explorer"

    with pytest.raises(VisualReasoningCompilerError, match="unsupported terms|semantic"):
        validate_visual_inventory(malformed)


class FakeVisionLLM:
    model = "qwen3.5-9b"

    def __init__(self, *, invalid_concept_once: bool = False) -> None:
        self.calls: list[dict] = []
        self.invalid_concept_once = invalid_concept_once

    def chat(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        images = kwargs["images"]
        assert isinstance(images, list)
        assert len(images) == 1
        assert str(images[0]).startswith("data:image/png;base64,")
        assert "pixel-art Git graph" not in str(kwargs["system"])
        assert "pixel-art Git graph" not in str(kwargs["user"])
        if len(self.calls) == 1:
            return _inventory_response()
        if self.invalid_concept_once and len(self.calls) == 2:
            return "The image shows a racing course without the required block."
        return (
            "VISIBLE ENTITY REFERENCES:\n"
            "- red-hat explorer\n"
            "- glowing commit nodes\n"
            "END VISIBLE ENTITY REFERENCES\n\n"
            "The player guides a small explorer across a glowing commit graph.\n\n"
            "The core interaction is choosing a branch and moving from the initial "
            "commit through visible feature and main paths toward the HEAD node. "
            "The on-screen commit, branch, and merge controls become the player's "
            "primary actions.\n\n"
            "A round is won when the explorer reaches the HEAD node after a valid "
            "merge sequence. It is lost when the player selects an invalid branch "
            "or fails to complete the visible progression before the round ends."
        )


def test_reason_over_board_sends_only_image_and_writes_linked_artifacts(
    tmp_path: Path,
) -> None:
    board = tmp_path / "board.png"
    board.write_bytes(PNG_BYTES)
    board_manifest = tmp_path / "board.import.json"
    board_manifest.write_text(json.dumps({
        "board": {"sha256": sha256_file(board)},
    }), encoding="utf-8")
    inventory = tmp_path / "run" / "visual_inventory.json"
    concept = tmp_path / "run" / "Concept.txt"
    manifest = tmp_path / "run" / "reasoning.json"
    llm = FakeVisionLLM()

    result = reason_over_board(
        board_image=board,
        board_manifest=board_manifest,
        inventory_output=inventory,
        concept_output=concept,
        manifest=manifest,
        llm=llm,
    )

    assert result == manifest
    assert len(llm.calls) == 2
    assert inventory.is_file()
    concept_text = concept.read_text(encoding="utf-8")
    assert concept_text.startswith("VISIBLE ENTITY REFERENCES:")
    assert "The player guides" in concept_text
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["format"] == "visual-reasoning-compiler/image-reasoning/v1"
    assert data["authority"]["original_vizconcept_sent_to_llm"] is False
    assert data["authority"]["semantic_context_sent_to_llm"] is False
    assert data["board"]["sha256"] == sha256_file(board)
    assert data["board_manifest"]["board_sha256"] == sha256_file(board)
    assert data["outputs"]["visual_inventory"]["sha256"] == sha256_file(inventory)
    assert data["outputs"]["concept"]["sha256"] == sha256_file(concept)
    assert inventory.with_suffix(".raw.txt").is_file()
    assert concept.with_suffix(".raw.txt").is_file()


def test_reason_over_board_retries_only_invalid_concept_format(
    tmp_path: Path,
) -> None:
    board = tmp_path / "board.png"
    board.write_bytes(PNG_BYTES)
    llm = FakeVisionLLM(invalid_concept_once=True)

    manifest = reason_over_board(
        board_image=board,
        inventory_output=tmp_path / "inventory.json",
        concept_output=tmp_path / "Concept.txt",
        manifest=tmp_path / "reasoning.json",
        llm=llm,
    )

    assert len(llm.calls) == 3
    assert llm.calls[2]["user"] != llm.calls[1]["user"]
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["llm"]["concept"]["format_retry_used"] is True


def test_reason_over_board_rejects_mismatched_board_manifest(tmp_path: Path) -> None:
    board = tmp_path / "board.png"
    board.write_bytes(PNG_BYTES)
    board_manifest = tmp_path / "board.json"
    board_manifest.write_text(json.dumps({
        "board": {"sha256": "wrong"},
    }), encoding="utf-8")

    with pytest.raises(VisualReasoningCompilerError, match="does not match"):
        reason_over_board(
            board_image=board,
            board_manifest=board_manifest,
            inventory_output=tmp_path / "inventory.json",
            concept_output=tmp_path / "Concept.txt",
            manifest=tmp_path / "reasoning.json",
            llm=FakeVisionLLM(),
        )
