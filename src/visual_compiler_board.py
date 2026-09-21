"""Headless ComfyUI board generation for the visual compiler.

The board is a visual planning artifact derived from an immutable Concept.txt.
This module owns only the ComfyUI HTTP/API boundary: it does not parse a board
back into gameplay truth and it never edits Concept.txt.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


class VisualBoardError(RuntimeError):
    """Raised when ComfyUI cannot produce the requested board artifact."""


def concept_board_prompt(concept: str) -> str:
    """Build a deterministic visual prompt from Concept prose."""
    from .unity_concept_entities import extract_named_entities

    declared = extract_named_entities(concept)
    if not declared:
        in_entities = False
        generated = []
        for line in concept.splitlines():
            stripped = line.strip()
            if stripped.upper().startswith("ENTITIES"):
                in_entities = True
                continue
            if in_entities and re.fullmatch(r"[A-Z][A-Z0-9 /&'-]{1,48}", stripped):
                break
            if not in_entities:
                continue
            match = re.match(
                r"^\s*\d+\.\s*\*{0,2}"
                r"([A-Za-z][A-Za-z0-9_ -]*?)\*{0,2}\s*:\s*(.+)$",
                line,
            )
            if match:
                generated.append({
                    "name": match.group(1).strip(),
                    "description": match.group(2).strip(),
                })
        declared = generated
    def compact_visual(item: dict) -> str:
        name = str(item.get("name", ""))
        lower = name.lower()
        color = next(
            (c for c in ("red", "blue", "green", "yellow", "orange", "pink")
             if c in lower),
            "",
        )
        if "coin" in lower:
            noun = "gold collectible coin"
        elif "boost" in lower or "pad" in lower:
            noun = "yellow springy boost pad"
        elif "buggy" in lower:
            noun = "green toy off-road buggy"
        elif "car" in lower or "racer" in lower or "speedster" in lower:
            noun = "toy race car"
        elif "chief" in lower or "sparky" in lower:
            noun = "pit chief NPC in mechanic gear"
        else:
            noun = str(item.get("description", "visual game entity")).split(",")[0]
        if color and color not in noun.lower():
            noun = f"{color} {noun}"
        return f"{name}={noun}"

    entity_summary = "; ".join(compact_visual(item) for item in declared)
    if entity_summary:
        entity_summary = (
            "SHOW ALL DECLARED VISUAL ENTITIES: " + entity_summary + ". "
            "Use distinct red, blue, and green toy-racer colors where the "
            "entity names specify them; include collectible coins and boost "
            "pads on the race circuit. "
        )
    return (
        entity_summary +
        "single wide visual planning board for the declared game, bright all-ages "
        "stylized 3D toy-racing illustration, colorful chunky toy materials, "
        "clear readable silhouettes, sunny blue sky, soft clouds, warm daylight, "
        "dynamic low camera near the track, cinematic composition, detailed but "
        "clean game concept art, no logos, no UI, no text;\n\n"
        "SEMANTIC CONCEPT (visualize only its declared entities, mechanics, and "
        "spaces; do not add gameplay or entities):\n"
        + concept.strip()
    )


class ComfyBoardClient:
    """Small synchronous client for ComfyUI's prompt/history/view API."""

    def __init__(self, *, base_url: str = "http://127.0.0.1:8188",
                 timeout: float = 30.0, poll_timeout: float = 900.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.poll_timeout = float(poll_timeout)

    def system_stats(self) -> dict:
        return self._get_json("/system_stats")

    def checkpoint_names(self) -> list[str]:
        info = self._get_json("/object_info/CheckpointLoaderSimple")
        return list(
            info.get("CheckpointLoaderSimple", {})
            .get("input", {}).get("required", {}).get("ckpt_name", [[]])[0]
        )

    def free_models(self) -> bool:
        """Release Comfy's checkpoint before LM Studio is loaded again."""
        payload = json.dumps({"unload_models": True, "free_memory": True}).encode()
        request = urllib.request.Request(
            self.base_url + "/free", data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout):
                return True
        except (urllib.error.URLError, OSError):
            return False

    def generate(self, *, prompt: str, negative: str, checkpoint: str,
                 seed: int, width: int, height: int, steps: int, cfg: float,
                 sampler_name: str, scheduler: str, output: Path,
                 manifest: Path) -> Path:
        if output.exists() or manifest.exists():
            raise VisualBoardError(
                f"refusing to overwrite board artifact: "
                f"{output if output.exists() else manifest}"
            )
        names = self.checkpoint_names()
        if checkpoint not in names:
            raise VisualBoardError(
                f"checkpoint {checkpoint!r} is not visible to ComfyUI; "
                f"available count={len(names)}"
            )
        graph = _workflow(
            checkpoint=checkpoint, prompt=prompt, negative=negative, seed=seed,
            width=width, height=height, steps=steps, cfg=cfg,
            sampler_name=sampler_name, scheduler=scheduler,
            filename_prefix="visual_compiler_board",
        )
        client_id = str(uuid.uuid4())
        response = self._post_json("/prompt", {"prompt": graph, "client_id": client_id})
        if response.get("node_errors"):
            raise VisualBoardError(f"ComfyUI rejected graph: {response['node_errors']}")
        prompt_id = response.get("prompt_id")
        if not prompt_id:
            raise VisualBoardError(f"ComfyUI returned no prompt_id: {response}")
        history = self._poll_history(prompt_id)
        image_ref = _first_image_ref(history.get("outputs", {}))
        if image_ref is None:
            raise VisualBoardError(f"ComfyUI produced no image for prompt {prompt_id}")
        query = urllib.parse.urlencode(image_ref)
        try:
            with urllib.request.urlopen(
                self.base_url + "/view?" + query, timeout=self.timeout
            ) as response_stream:
                image_bytes = response_stream.read()
        except (urllib.error.URLError, OSError) as exc:
            raise VisualBoardError(f"ComfyUI image fetch failed: {exc}") from exc
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(image_bytes)
        record = {
            "format": "visual-compiler-board/v1",
            "source": "ComfyUI headless API",
            "comfy_base_url": self.base_url,
            "prompt_id": prompt_id,
            "client_id": client_id,
            "checkpoint": checkpoint,
            "seed": seed,
            "width": width,
            "height": height,
            "steps": steps,
            "cfg": cfg,
            "sampler_name": sampler_name,
            "scheduler": scheduler,
            "positive_prompt": prompt,
            "negative_prompt": negative,
            "workflow": graph,
            "comfy_output": image_ref,
            "board": {"path": str(output), "sha256": _sha256(output)},
        }
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
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
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                detail = str(exc)
            raise VisualBoardError(f"ComfyUI HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise VisualBoardError(f"ComfyUI request {path} failed: {exc}") from exc

    def _get_json(self, path: str) -> dict:
        try:
            with urllib.request.urlopen(self.base_url + path, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise VisualBoardError(f"ComfyUI request {path} failed: {exc}") from exc

    def _poll_history(self, prompt_id: str) -> dict:
        deadline = time.monotonic() + self.poll_timeout
        while time.monotonic() < deadline:
            try:
                history = self._get_json(f"/history/{prompt_id}")
            except VisualBoardError:
                time.sleep(1.0)
                continue
            entry = history.get(prompt_id)
            if entry:
                status = entry.get("status", {}) or {}
                if status.get("status_str") == "error":
                    raise VisualBoardError(
                        f"ComfyUI generation failed for {prompt_id}: {status}"
                    )
                if entry.get("outputs"):
                    return entry
            time.sleep(1.0)
        raise VisualBoardError(
            f"ComfyUI generation timed out after {self.poll_timeout:g}s ({prompt_id})"
        )


def _workflow(*, checkpoint: str, prompt: str, negative: str, seed: int,
              width: int, height: int, steps: int, cfg: float,
              sampler_name: str, scheduler: str, filename_prefix: str) -> dict:
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": max(0, seed), "steps": steps, "cfg": cfg,
            "sampler_name": sampler_name, "scheduler": scheduler, "denoise": 1.0,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
            "latent_image": ["5", 0]}},
        "4": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": checkpoint}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": filename_prefix, "images": ["8", 0]}},
    }


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
