"""Slice 1 of the image-led Visual Reasoning Compiler.

This module owns the boundary from a visual concept prompt to one canonical
ComfyUI screenshot.  It deliberately does not call an LLM, inspect gameplay,
resolve assets, or create a Unity project.  Later slices consume the recorded
board artifact and its provenance manifest.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


FORMAT = "visual-reasoning-compiler/vizconcept-board/v1"
IMPORT_FORMAT = "visual-reasoning-compiler/imported-board/v1"
DEFAULT_CHECKPOINT = "sd15ModelLab_realV001_1651730.safetensors"
DEFAULT_SEED = 978075547277410
DEFAULT_WIDTH = 768
DEFAULT_HEIGHT = 512
DEFAULT_STEPS = 20
DEFAULT_CFG = 8.0
DEFAULT_SAMPLER = "dpmpp_2m_sde_gpu"
DEFAULT_SCHEDULER = "karras"

# Do not suppress text or UI: a game screenshot may intentionally contain both,
# and Slice 2 must be able to reason over them.  These are only generic failure
# suppressors, not hidden gameplay instructions.
DEFAULT_NEGATIVE_PROMPT = (
    "blurry, low resolution, duplicate objects, malformed geometry, "
    "cropped major objects, watermark, signature, gore, nsfw"
)


class VisualReasoningCompilerError(RuntimeError):
    """Raised when the Slice-1 visual-board contract cannot be completed."""


def build_vizconcept_prompt(source_text: str) -> str:
    """Wrap a visual concept without turning it into a semantic game brief."""
    text = source_text.strip()
    if not text:
        raise VisualReasoningCompilerError("vizconcept.txt is empty")
    return (
        "Create one readable game screenshot or game-concept frame. "
        "Treat the visual concept below as the authoritative art direction. "
        "Preserve its setting, style, composition, visible objects, and UI "
        "where requested. Give discrete objects clear silhouettes and enough "
        "separation for later visual inventory. Do not add unrelated objects "
        "or gameplay systems.\n\n"
        "VISUAL CONCEPT:\n"
        + text
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return sha256_bytes(encoded)


def _looks_like_png(data: bytes) -> bool:
    return data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 32


def build_workflow(*, checkpoint: str, positive: str, negative: str,
                   seed: int, width: int, height: int, steps: int, cfg: float,
                   sampler: str, scheduler: str,
                   filename_prefix: str = "visual_reasoning_board") -> dict:
    """Build the exact API-format graph recorded in the run manifest."""
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": max(0, seed), "steps": steps, "cfg": cfg,
            "sampler_name": sampler, "scheduler": scheduler, "denoise": 1.0,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
            "latent_image": ["5", 0],
        }},
        "4": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": checkpoint}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": positive, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": filename_prefix, "images": ["8", 0]}},
    }


def import_existing_board(*, board_image: Path, output: Path, manifest: Path,
                          vizconcept_file: Path | None = None) -> Path:
    """Import a known-good render without pretending to regenerate it.

    This is the hardware-aware companion to the Comfy generation boundary.
    The imported PNG remains the visual intermediate representation; its
    original generation settings are unknown unless the caller supplies a
    separate historical manifest.
    """
    if not board_image.is_file():
        raise VisualReasoningCompilerError(
            f"board image not found: {board_image}"
        )
    if output.exists() or manifest.exists():
        raise VisualReasoningCompilerError(
            "refusing to overwrite visual-reasoning artifact: "
            f"{output if output.exists() else manifest}"
        )
    image_bytes = board_image.read_bytes()
    if not _looks_like_png(image_bytes):
        raise VisualReasoningCompilerError(
            "board image must be a valid-looking PNG"
        )

    prompt_record: dict[str, Any] | None = None
    if vizconcept_file is not None:
        if not vizconcept_file.is_file():
            raise VisualReasoningCompilerError(
                f"vizconcept file not found: {vizconcept_file}"
            )
        prompt_bytes = vizconcept_file.read_bytes()
        try:
            prompt_text = prompt_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise VisualReasoningCompilerError(
                f"vizconcept file must be UTF-8: {vizconcept_file}"
            ) from exc
        if not prompt_text.strip():
            raise VisualReasoningCompilerError("vizconcept.txt is empty")
        prompt_record = {
            "path": str(vizconcept_file),
            "sha256": sha256_bytes(prompt_bytes),
            "text": prompt_text,
        }

    board_hash = sha256_bytes(image_bytes)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(image_bytes)
    manifest_data = {
        "format": IMPORT_FORMAT,
        "stage": "1_visual_board_import",
        "authority": {
            "input": "existing_rendered_board",
            "board_role": "visual_intermediate_representation",
            "concept_generated_here": False,
            "llm_called_here": False,
        },
        "source_board": {
            "path": str(board_image),
            "sha256": board_hash,
            "mime_type": "image/png",
        },
        "vizconcept": prompt_record,
        "generation": {
            "known": False,
            "note": "Imported existing render; original Comfy workflow "
                    "and settings were not supplied.",
        },
        "request_fingerprint": _canonical_hash({
            "source_board_sha256": board_hash,
            "vizconcept": prompt_record,
        }),
        "board": {
            "path": str(output),
            "mime_type": "image/png",
            "sha256": sha256_file(output),
        },
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(manifest_data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return output


class ComfyVizConceptClient:
    """Synchronous ComfyUI API client for the Slice-1 board boundary."""

    def __init__(self, *, base_url: str = "http://127.0.0.1:8188",
                 timeout: float = 30.0, poll_timeout: float = 900.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.poll_timeout = float(poll_timeout)

    def checkpoint_names(self) -> list[str]:
        info = self._get_json("/object_info/CheckpointLoaderSimple")
        return list(
            info.get("CheckpointLoaderSimple", {})
            .get("input", {}).get("required", {}).get("ckpt_name", [[]])[0]
        )

    def free_models(self) -> bool:
        """Best-effort Comfy unload used before the next GPU-heavy stage."""
        payload = json.dumps({
            "unload_models": True,
            "free_memory": True,
        }).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/free", data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout):
                return True
        except (urllib.error.URLError, OSError):
            return False

    def generate(self, *, vizconcept_file: Path, output: Path,
                 manifest: Path, checkpoint: str = DEFAULT_CHECKPOINT,
                 seed: int = DEFAULT_SEED, width: int = DEFAULT_WIDTH,
                 height: int = DEFAULT_HEIGHT, steps: int = DEFAULT_STEPS,
                 cfg: float = DEFAULT_CFG,
                 sampler: str = DEFAULT_SAMPLER,
                 scheduler: str = DEFAULT_SCHEDULER,
                 negative: str = DEFAULT_NEGATIVE_PROMPT) -> Path:
        """Generate a board and write its immutable provenance manifest."""
        if not vizconcept_file.is_file():
            raise VisualReasoningCompilerError(
                f"vizconcept file not found: {vizconcept_file}"
            )
        if output.exists() or manifest.exists():
            raise VisualReasoningCompilerError(
                "refusing to overwrite visual-reasoning artifact: "
                f"{output if output.exists() else manifest}"
            )
        raw_bytes = vizconcept_file.read_bytes()
        try:
            source_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise VisualReasoningCompilerError(
                f"vizconcept file must be UTF-8: {vizconcept_file}"
            ) from exc
        positive = build_vizconcept_prompt(source_text)
        available = self.checkpoint_names()
        if checkpoint not in available:
            raise VisualReasoningCompilerError(
                f"checkpoint {checkpoint!r} is not visible to ComfyUI; "
                f"available count={len(available)}"
            )
        workflow = build_workflow(
            checkpoint=checkpoint, positive=positive, negative=negative,
            seed=seed, width=width, height=height, steps=steps, cfg=cfg,
            sampler=sampler, scheduler=scheduler,
        )
        client_id = str(uuid.uuid4())
        response = self._post_json(
            "/prompt", {"prompt": workflow, "client_id": client_id}
        )
        if response.get("node_errors"):
            raise VisualReasoningCompilerError(
                f"ComfyUI rejected graph: {response['node_errors']}"
            )
        prompt_id = response.get("prompt_id")
        if not prompt_id:
            raise VisualReasoningCompilerError(
                f"ComfyUI returned no prompt_id: {response}"
            )
        history = self._poll_history(prompt_id)
        image_ref = _first_image_ref(history.get("outputs", {}))
        if image_ref is None:
            raise VisualReasoningCompilerError(
                f"ComfyUI produced no image for prompt {prompt_id}"
            )
        image_bytes = self._fetch_image(image_ref)
        if not _looks_like_png(image_bytes):
            raise VisualReasoningCompilerError(
                "ComfyUI returned a non-PNG or truncated board image"
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(image_bytes)
        settings = {
            "checkpoint": checkpoint, "seed": seed, "width": width,
            "height": height, "steps": steps, "cfg": cfg,
            "sampler": sampler, "scheduler": scheduler,
        }
        manifest_data = {
            "format": FORMAT,
            "stage": "1_visual_concept_to_board",
            "authority": {
                "input": "vizconcept.txt",
                "board_role": "visual_intermediate_representation",
                "concept_generated_here": False,
                "llm_called_here": False,
            },
            "source": {
                "path": str(vizconcept_file),
                "sha256": sha256_bytes(raw_bytes),
                "text": source_text,
            },
            "prompt": {"positive": positive, "negative": negative},
            "settings": settings,
            "request_fingerprint": _canonical_hash({
                "source_sha256": sha256_bytes(raw_bytes),
                "prompt": {"positive": positive, "negative": negative},
                "settings": settings,
                "workflow": workflow,
            }),
            "comfy": {
                "base_url": self.base_url,
                "prompt_id": prompt_id,
                "client_id": client_id,
                "output_ref": image_ref,
            },
            "workflow": workflow,
            "board": {
                "path": str(output),
                "mime_type": "image/png",
                "sha256": sha256_file(output),
            },
        }
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(manifest_data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return output

    def _post_json(self, path: str, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise VisualReasoningCompilerError(
                f"ComfyUI HTTP {exc.code}: {detail}"
            ) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise VisualReasoningCompilerError(
                f"ComfyUI request {path} failed: {exc}"
            ) from exc

    def _get_json(self, path: str) -> dict:
        try:
            with urllib.request.urlopen(self.base_url + path, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise VisualReasoningCompilerError(
                f"ComfyUI request {path} failed: {exc}"
            ) from exc

    def _fetch_image(self, image_ref: dict) -> bytes:
        query = urllib.parse.urlencode(image_ref)
        try:
            with urllib.request.urlopen(
                self.base_url + "/view?" + query, timeout=self.timeout
            ) as response:
                return response.read()
        except (urllib.error.URLError, OSError) as exc:
            raise VisualReasoningCompilerError(
                f"ComfyUI image fetch failed: {exc}"
            ) from exc

    def _poll_history(self, prompt_id: str) -> dict:
        deadline = time.monotonic() + self.poll_timeout
        while time.monotonic() < deadline:
            try:
                history = self._get_json(f"/history/{prompt_id}")
            except VisualReasoningCompilerError:
                time.sleep(1.0)
                continue
            entry = history.get(prompt_id)
            if entry:
                status = entry.get("status", {}) or {}
                if status.get("status_str") == "error":
                    raise VisualReasoningCompilerError(
                        f"ComfyUI generation failed for {prompt_id}: {status}"
                    )
                if entry.get("outputs"):
                    return entry
            time.sleep(1.0)
        raise VisualReasoningCompilerError(
            f"ComfyUI generation timed out after {self.poll_timeout:g}s "
            f"({prompt_id})"
        )


def _first_image_ref(outputs: dict) -> dict | None:
    for node_output in outputs.values():
        images = node_output.get("images") or []
        if images:
            image = images[0]
            return {
                "filename": image["filename"],
                "subfolder": image.get("subfolder", ""),
                "type": image.get("type", "output"),
            }
    return None
