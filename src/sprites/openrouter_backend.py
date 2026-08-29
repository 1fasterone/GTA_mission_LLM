"""
OpenRouter backend adapter for 2D sprite frame generation.

Same public interface as ComfyUIBackend (generate / generate_from_reference /
free_vram / available / .name / .checkpoint) so content.py can select either
one without caring which it got — see generate_sprite_bank()'s `backend`
selection. Replaces local ComfyUI/SDXL as the default sprite generator
(2026-08-27, user request: live StreetBeat2D project never used its 270+
ComfyUI-generated sprites — root cause was a missing Resources overlay +
wrong texture import type, both fixed separately in unity_pipeline.py /
unity_generator.py — but the user also asked to stop depending on local
ComfyUI/GPU contention for sprite art and use OpenRouter's Gemini 2.5 Flash
Image model instead).

Endpoint confirmed against OpenRouter's own API reference
(docs/api/api-reference/images/generate-an-image, 2026-08-27): POST
{base_url}/images, JSON body {model, prompt, ...}, response
data[0].b64_json. This is a DIFFERENT endpoint shape from the classic
/chat/completions text API — no `messages` array, no `modalities` field.

Two load-bearing differences from ComfyUIBackend, both wins:
  - `background: "transparent"` is a native request parameter — Gemini
    returns pre-matted RGBA directly. content.py skips the rembg/U2-Net
    matte step entirely for this backend (see its `natively_transparent`
    check) instead of running removal on an image that's already clean.
  - No local GPU/VRAM involved at all, so this sprite-gen step no longer
    competes for the same 8GB card as LM Studio / Stable Audio 3 — a
    plausible contributor to this session's earlier full-pipeline SIGSEGVs
    even though that root cause was never fully isolated.

One real capability gap, accepted rather than faked: Gemini's image API has
no denoise/strength dial the way SD img2img does. `generate_from_reference`
accepts `denoise` for interface compatibility but ignores it — character
consistency across frames leans entirely on `input_references` (the prior
frame, passed back in) plus the prompt wording, not a numeric strength
knob. Documented, not silently pretended away.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Optional

_ENV_KEY_VARS = ("OPENROUTER_API", "OPENROUTER_API_KEY")

_DEFAULT_MODEL = "google/gemini-2.5-flash-image"

_NEGATIVE_DEFAULT = (
    "photorealistic, photo, 3d render, realistic skin, gore, blood, nsfw, nude, "
    "text, watermark, signature, blurry, extra limbs, deformed hands, "
    "multiple characters, collage, grid, background scenery, drop shadow, frame, border"
)

_KEY_PROMPT_FRAGMENT = (
    "2D video game character sprite, digital illustration, flat vector shading, "
    "bold outlines, simple silhouette, even flat lighting on subject, isolated subject"
)


class OpenRouterBackend:
    """Thin, synchronous REST client for OpenRouter's image generation API.
    Never raises on network/generation failure — returns None so callers
    degrade (same fail-soft convention as ComfyUIBackend / DspAdapter)."""

    name = "openrouter"

    def __init__(self, config: Optional[dict] = None):
        cfg = (config or {}).get("sprites", {}) if config else {}
        self.base_url = str(cfg.get("openrouter_base_url", "https://openrouter.ai/api/v1")).rstrip("/")
        self.api_key = ""
        for var in _ENV_KEY_VARS:
            val = os.environ.get(var)
            if val:
                self.api_key = val
                break
        if not self.api_key:
            self.api_key = str(cfg.get("api_key", "") or "")
        self.model = str(cfg.get("model", _DEFAULT_MODEL))
        self.checkpoint = self.model  # manifest field parity with ComfyUIBackend
        self.timeout = float(cfg.get("timeout", 120))
        self.resolution = str(cfg.get("resolution", "1K"))
        self.quality = str(cfg.get("quality", "high"))
        self._warned: set[str] = set()

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            print(f"[sprites/openrouter] {msg}")

    def available(self) -> bool:
        if not self.api_key:
            self._warn_once("no_key", f"no API key found in env {_ENV_KEY_VARS} or config.sprites.api_key")
            return False
        return True

    def free_vram(self) -> bool:
        """No local GPU involved — nothing to free. Kept for interface parity
        with content.py's unconditional post-batch call."""
        return True

    def generate(self, prompt: str, *, seed: int, width: int = 1024,
                 height: int = 1024, negative: str = "") -> Optional[bytes]:
        """One frame, no reference image. Returns raw PNG bytes (already
        alpha-matted by the API), or None on any failure."""
        body = self._base_body(prompt, negative, seed, width, height)
        return self._run(body)

    def generate_from_reference(self, prompt: str, *, seed: int, reference_png_bytes: bytes,
                                 denoise: Optional[float] = None, negative: str = "") -> Optional[bytes]:
        """One frame conditioned on a reference image (character consistency
        across an entity's actions) — see module docstring re: `denoise`
        being accepted-but-unused for this backend."""
        body = self._base_body(prompt, negative, seed, 1024, 1024)
        ref_b64 = base64.b64encode(reference_png_bytes).decode("ascii")
        body["input_references"] = [{
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{ref_b64}"},
        }]
        return self._run(body)

    def _base_body(self, prompt: str, negative: str, seed: int, width: int, height: int) -> dict:
        return {
            "model": self.model,
            "prompt": self._full_prompt(prompt, negative),
            "n": 1,
            "output_format": "png",
            "background": "transparent",
            "aspect_ratio": _aspect_ratio(width, height),
            "resolution": self.resolution,
            "quality": self.quality,
            "seed": max(0, seed),
        }

    @staticmethod
    def _full_prompt(prompt: str, negative: str) -> str:
        # No separate negative-prompt field on this API — folded into the
        # main prompt as an explicit "avoid" clause instead.
        neg = negative or _NEGATIVE_DEFAULT
        return f"{prompt}, {_KEY_PROMPT_FRAGMENT}. Avoid: {neg}."

    def _run(self, body: dict) -> Optional[bytes]:
        if not self.available():
            return None
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + "/images", data=data, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except OSError:
                pass
            self._warn_once(f"http_{exc.code}", f"POST /images failed ({exc.code}): {detail}")
            return None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self._warn_once("request_failed", f"POST /images failed ({type(exc).__name__}: {exc})")
            return None

        items = parsed.get("data") or []
        if not items or not items[0].get("b64_json"):
            self._warn_once("no_image", f"no image data in response: {str(parsed)[:300]}")
            return None
        try:
            return base64.b64decode(items[0]["b64_json"])
        except (ValueError, TypeError) as exc:
            self._warn_once("decode_failed", f"b64_json decode failed ({type(exc).__name__}: {exc})")
            return None


def _aspect_ratio(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        return "1:1"
    ratio = width / height
    candidates = {
        "1:1": 1.0, "4:3": 4 / 3, "3:4": 3 / 4,
        "16:9": 16 / 9, "9:16": 9 / 16,
    }
    return min(candidates, key=lambda k: abs(candidates[k] - ratio))
