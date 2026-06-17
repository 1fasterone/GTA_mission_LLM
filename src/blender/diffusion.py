"""
Diffusion fallback pipeline — the "second pipeline" from the goal.

When a job's textures can't be produced procedurally by Blender (realistic
characters, authored metal/sci-fi surfaces), we construct a prompt from the LLM
description and request the albedo/base-colour map from a diffusion backend. The
resulting image is handed back to the same Blender job, which bakes the
remaining maps (normal / AO / roughness) and assigns the material before export.

Backends (config `blender.diffusion.backend`):
  * "auto1111" — AUTOMATIC1111 / Forge `/sdapi/v1/txt2img` REST API
  * "comfyui"  — ComfyUI `/prompt` graph API (a minimal txt2img graph)
  * "none"     — disabled; build_texture_request() still produces the prompt so
                 callers/tests can inspect what *would* be sent.

Like the rest of this package it degrades gracefully: if the backend isn't
reachable, generate_albedo() returns None and the manager stubs that texture
rather than failing the whole job.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .job_schema import BlenderJob


# ── Prompt construction ───────────────────────────────────────────────────────

# Style → diffusion prompt fragment + sampler bias. Keeps generated textures
# consistent with the project's visual direction.
_STYLE_PROMPT: dict[str, str] = {
    "realistic": "photorealistic, physically based, 4k texture, fine surface detail",
    "stylized":  "stylized hand-painted texture, clean shapes, soft shading",
    "low_poly":  "flat low-poly texture, simple color blocks, minimal detail",
    "cartoon":   "cartoon texture, bold outlines, saturated colors",
    "pixel_art": "pixel-art texture, limited palette, crisp pixels",
    "retro":     "retro game texture, muted palette, slight grain",
    "sci_fi":    "sci-fi panel texture, brushed metal, emissive trim, greebles",
}

# Albedo maps must not bake lighting in — diffuse, flat-lit, tileable.
_ALBEDO_GUARD = ("seamless tileable texture, albedo / base color map, "
                 "flat even lighting, no shadows, no highlights, orthographic")

_NEGATIVE = ("lighting, shadows, highlights, specular, blurry, watermark, text, "
             "seams, distortion, jpeg artifacts")


@dataclass
class TextureRequest:
    """A fully-constructed diffusion request derived from a job's LLM brief."""
    prompt:     str
    negative:   str
    width:      int
    height:     int
    seed:       int = -1
    steps:      int = 28
    cfg_scale:  float = 6.5
    meta:       dict = field(default_factory=dict)

    def to_auto1111_payload(self) -> dict:
        return {
            "prompt": self.prompt, "negative_prompt": self.negative,
            "width": self.width, "height": self.height,
            "steps": self.steps, "cfg_scale": self.cfg_scale,
            "seed": self.seed, "sampler_name": "DPM++ 2M Karras",
            "tiling": True,
        }


def build_texture_request(job: BlenderJob, seed: Optional[int] = None) -> TextureRequest:
    """Construct a diffusion prompt from the job's LLM description + style.

    This is the "prompt constructed from the LLM description" step — pure string
    assembly, no network, so it's unit-testable without a backend.

    `seed` defaults to the job's deterministic diffusion sub-seed so the same job
    re-renders the same albedo; pass an explicit seed to override.
    """
    if seed is None:
        from .seeding import diffusion_seed
        seed = diffusion_seed(job)
    brief = (job.textures.material_brief or job.description or job.name).strip()
    style_frag = _STYLE_PROMPT.get(job.style, _STYLE_PROMPT["stylized"])
    subject = f"{job.asset_type} surface — {brief}" if brief else f"{job.asset_type} surface"
    prompt = f"{subject}, {style_frag}, {_ALBEDO_GUARD}"
    res = job.textures.resolution
    return TextureRequest(
        prompt   = prompt,
        negative = _NEGATIVE,
        width    = res,
        height   = res,
        seed     = seed,
        meta     = {"job": job.name, "asset_type": job.asset_type, "style": job.style},
    )


# ── Backend dispatch ──────────────────────────────────────────────────────────

class DiffusionClient:
    """Thin REST client for a local diffusion server. Never raises on a network
    failure — returns None so the manager can stub the texture."""

    def __init__(self, cfg: Optional[dict] = None):
        cfg = cfg or {}
        self.backend  = str(cfg.get("backend", "none")).lower()
        self.base_url = cfg.get("base_url", "http://127.0.0.1:7860").rstrip("/")
        self.timeout  = float(cfg.get("timeout", 180))

    @property
    def enabled(self) -> bool:
        return self.backend in ("auto1111", "comfyui")

    def available(self) -> bool:
        """Cheap reachability probe so the manager can pick the route up-front."""
        if not self.enabled:
            return False
        ping = "/sdapi/v1/options" if self.backend == "auto1111" else "/system_stats"
        try:
            with urllib.request.urlopen(self.base_url + ping, timeout=5):
                return True
        except (urllib.error.URLError, OSError):
            return False

    def generate_albedo(self, req: TextureRequest, out_path: Path) -> Optional[Path]:
        """Render `req` and write a PNG to out_path. Returns the path or None."""
        if not self.enabled:
            return None
        try:
            if self.backend == "auto1111":
                img_b64 = self._auto1111(req)
            else:
                img_b64 = self._comfyui(req)
            if not img_b64:
                return None
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(base64.b64decode(img_b64))
            return out_path
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            return None

    # -- backend impls ---------------------------------------------------------

    def _post_json(self, path: str, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _auto1111(self, req: TextureRequest) -> Optional[str]:
        result = self._post_json("/sdapi/v1/txt2img", req.to_auto1111_payload())
        images = result.get("images") or []
        return images[0] if images else None

    def _comfyui(self, req: TextureRequest) -> Optional[str]:
        # Minimal txt2img graph; relies on a default checkpoint being loaded.
        # ComfyUI returns history asynchronously — for brevity we only support
        # servers exposing the simple /prompt → /view flow. On anything unusual
        # we return None and let the job stub its texture.
        graph = _comfy_graph(req)
        self._post_json("/prompt", {"prompt": graph})
        return None  # async retrieval not implemented; treated as unavailable


def _comfy_graph(req: TextureRequest) -> dict:
    """A minimal ComfyUI txt2img API graph for `req`."""
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": max(req.seed, 0), "steps": req.steps, "cfg": req.cfg_scale,
            "sampler_name": "dpmpp_2m", "scheduler": "karras", "denoise": 1.0,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
            "latent_image": ["5", 0]}},
        "4": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": "model.safetensors"}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": req.width, "height": req.height, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": req.prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": req.negative, "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "tex", "images": ["8", 0]}},
    }
