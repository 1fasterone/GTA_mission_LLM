"""
ComfyUI backend adapter for 2D sprite frame generation.

Wraps a local ComfyUI server's /prompt -> /history -> /view HTTP API. This
completes the async retrieval that src/blender/diffusion.py's _comfyui()
explicitly left unimplemented ("async retrieval not implemented; treated as
unavailable") — that class is texture-specific (tileable albedo prompting,
`/sdapi`-style single-shot response) and out of scope to extend here; this is
a parallel, sprite-specific client with its own polling loop.

Checkpoint is HARDCODED, never derived from prompt/keyword text — see
_CHECKPOINT below. The model libraries this server can see (via
G:/comfyui/ComfyUI/extra_model_paths.yaml) include several NSFW-labeled
checkpoints; nothing in this module ever names, lists, or selects among them.

Two generation modes:
  generate()             — txt2img, pure noise start. Used for exactly one
                            frame per entity (the "reference" frame).
  generate_from_reference() — img2img at a moderate denoise, starting from a
                            previously-generated frame. Used for every OTHER
                            frame of that entity, so the character's colors/
                            costume/proportions stay anchored across actions
                            instead of drifting frame to frame — confirmed
                            live (2026-08-27) that same-seed-alone txt2img
                            was not enough: a goblin enemy's vest, belt, and
                            hair changed between its idle/attack/death frames
                            despite sharing a seed and the same character_desc
                            text. See src/sprites/content.py for the
                            reference-then-chain orchestration.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Optional

# Pinned, explicit, all-ages-safe. Present in both G:/AI MODELS/checkpoints/
# and F:/ComfyUI_windows_portable/.../checkpoints/ (via extra_model_paths.yaml).
# Never replace this with a keyword search over the checkpoint library — see
# module docstring.
_CHECKPOINT = "sdXL_v10VAEFix.safetensors"

_NEGATIVE_DEFAULT = (
    "photorealistic, photo, 3d render, realistic skin, gore, blood, nsfw, nude, "
    "text, watermark, signature, blurry, extra limbs, deformed hands, "
    "multiple characters, collage, grid"
)

# Background isolation used to be fought at the PROMPT level (a requested
# flat/green-screen background) with a chroma-key matte downstream. Confirmed
# live (2026-08-27), three attempts in a row: this SDXL checkpoint has a
# strong, prompt-resistant bias toward framing "flat cel-shaded character" as
# a mounted/presented object (sticker card, then a stretched gallery canvas,
# then a full illustrated scene) no matter how hard the negative prompt
# pushed back — burning real generation time each attempt. Switched to
# segmentation-based removal instead (src/sprites/matte.py, rembg/U2-Net) —
# it isolates the subject regardless of what SDXL draws behind it, so the
# background no longer needs prompt-level control at all. This fragment is
# now just a light nudge toward simpler compositions (helps segmentation edge
# quality a little); it is NOT load-bearing for correctness anymore.
_KEY_PROMPT_FRAGMENT = (
    "2D video game character sprite, digital illustration, flat vector shading, "
    "bold outlines, simple uncluttered background, even flat lighting on subject"
)


class ComfyUIBackend:
    """Thin, synchronous REST client for a local ComfyUI server. Never raises
    on network/generation failure — returns None so callers degrade (same
    fail-soft convention as DspAdapter / Stable3AudioAdapter)."""

    name = "comfyui"

    def __init__(self, config: Optional[dict] = None):
        cfg = (config or {}).get("sprites", {}) if config else {}
        self.base_url     = str(cfg.get("base_url", "http://127.0.0.1:8188")).rstrip("/")
        self.timeout       = float(cfg.get("timeout", 30))
        self.poll_timeout  = float(cfg.get("poll_timeout", 180))
        self.steps         = int(cfg.get("steps", 16))
        self.cfg_scale     = float(cfg.get("cfg_scale", 6.5))
        # img2img strength for every non-reference frame — low enough to hold
        # the reference frame's colors/costume, high enough to actually reach
        # a different pose. Tuned empirically (2026-08-27): 0.55 held identity
        # perfectly but barely changed pose at all (idle/attack/hurt/death
        # were nearly identical stances) — 0.72 is the next checkpoint tried.
        self.ref_denoise   = float(cfg.get("ref_denoise", 0.72))
        self.checkpoint    = _CHECKPOINT
        self._warned: set[str] = set()

    def _warn_once(self, key: str, msg: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            print(f"[sprites/comfyui] {msg}")

    def available(self) -> bool:
        try:
            with urllib.request.urlopen(self.base_url + "/system_stats", timeout=5):
                return True
        except (urllib.error.URLError, OSError):
            return False

    def free_vram(self) -> bool:
        """Explicitly unload ComfyUI's resident checkpoint and free VRAM.
        Confirmed live (2026-08-27): ComfyUI does NOT release a checkpoint on
        its own between generation calls — after a full sprite-generation
        batch, SDXL stayed resident and the next pipeline stage (Stable Audio
        3, loaded separately) had nowhere to fit on an 8 GB card. Caller MUST
        call this once a whole batch of frames is done (content.py's
        generate_sprite_bank does), same discipline as
        Stable3AudioAdapter.unload_all(). Best-effort: failure just means the
        next stage's own OOM handling has to cope, same as before this existed."""
        try:
            # Not _post_json — /free returns an empty (non-JSON) body on
            # success, confirmed live, so parsing the response is the wrong
            # success signal here; a non-raising urlopen is enough.
            data = json.dumps({"unload_models": True, "free_memory": True}).encode("utf-8")
            req = urllib.request.Request(
                self.base_url + "/free", data=data, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout):
                pass
            return True
        except (urllib.error.URLError, OSError) as exc:
            self._warn_once("free_failed", f"POST /free failed ({type(exc).__name__}: {exc})")
            return False

    def _post_json(self, path: str, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _get_json(self, path: str) -> dict:
        with urllib.request.urlopen(self.base_url + path, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def generate(self, prompt: str, *, seed: int, width: int = 1024,
                 height: int = 1024, negative: str = "") -> Optional[bytes]:
        """txt2img — one frame from pure noise. Returns raw PNG bytes, or None
        on any failure (unreachable server, node error, timeout)."""
        graph = _build_txt2img_workflow(
            checkpoint=self.checkpoint, prompt=self._full_prompt(prompt),
            negative=negative or _NEGATIVE_DEFAULT, seed=seed,
            width=width, height=height, steps=self.steps, cfg=self.cfg_scale,
        )
        return self._run_graph(graph)

    def generate_from_reference(self, prompt: str, *, seed: int, reference_png_bytes: bytes,
                                 denoise: Optional[float] = None, negative: str = "") -> Optional[bytes]:
        """img2img — one frame started from a reference image instead of pure
        noise, so the result stays visually anchored to it (same character
        colors/costume/proportions, new pose per `prompt`). Returns raw PNG
        bytes, or None on any failure (including a failed reference upload)."""
        if not self.available():
            self._warn_once("unreachable", f"ComfyUI not reachable at {self.base_url}")
            return None
        ref = self._upload_image(reference_png_bytes)
        if ref is None:
            return None
        graph = _build_img2img_workflow(
            checkpoint=self.checkpoint, prompt=self._full_prompt(prompt),
            negative=negative or _NEGATIVE_DEFAULT, seed=seed,
            ref_filename=ref["name"], denoise=denoise if denoise is not None else self.ref_denoise,
            steps=self.steps, cfg=self.cfg_scale,
        )
        return self._run_graph(graph)

    @staticmethod
    def _full_prompt(prompt: str) -> str:
        return f"{prompt}, {_KEY_PROMPT_FRAGMENT}"

    def _upload_image(self, png_bytes: bytes, filename: str = "sprite_ref.png") -> Optional[dict]:
        """POST to ComfyUI's /upload/image so a LoadImage node can read it
        back by name. Uses httpx (already a project dependency) since
        hand-rolling multipart/form-data over urllib is not worth it for one
        call site."""
        try:
            import httpx
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(
                    f"{self.base_url}/upload/image",
                    files={"image": (filename, png_bytes, "image/png")},
                    data={"overwrite": "true"},
                )
                resp.raise_for_status()
                data = resp.json()
                return {"name": data["name"], "subfolder": data.get("subfolder", ""),
                        "type": data.get("type", "input")}
        except Exception as exc:  # noqa: BLE001
            self._warn_once("upload_failed", f"reference image upload failed ({type(exc).__name__}: {exc})")
            return None

    def _run_graph(self, graph: dict) -> Optional[bytes]:
        """Shared submit -> poll -> fetch tail for both generate() and
        generate_from_reference()."""
        if not self.available():
            self._warn_once("unreachable", f"ComfyUI not reachable at {self.base_url}")
            return None

        client_id = str(uuid.uuid4())
        try:
            resp = self._post_json("/prompt", {"prompt": graph, "client_id": client_id})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self._warn_once("post_failed", f"POST /prompt failed ({type(exc).__name__}: {exc})")
            return None

        if resp.get("node_errors"):
            self._warn_once("node_errors", f"ComfyUI graph rejected: {resp['node_errors']}")
            return None
        prompt_id = resp.get("prompt_id")
        if not prompt_id:
            self._warn_once("no_prompt_id", f"no prompt_id in response: {resp}")
            return None

        history = self._poll_history(prompt_id)
        if history is None:
            return None

        image_ref = _first_image_ref(history.get("outputs", {}))
        if image_ref is None:
            self._warn_once("no_image", f"no image output in history for {prompt_id}")
            return None

        qs = urllib.parse.urlencode(image_ref)
        try:
            with urllib.request.urlopen(f"{self.base_url}/view?{qs}", timeout=self.timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError) as exc:
            self._warn_once("fetch_failed", f"GET /view failed ({type(exc).__name__}: {exc})")
            return None

    def _poll_history(self, prompt_id: str) -> Optional[dict]:
        deadline = time.monotonic() + self.poll_timeout
        while time.monotonic() < deadline:
            try:
                hist_resp = self._get_json(f"/history/{prompt_id}")
            except (urllib.error.URLError, OSError, ValueError):
                time.sleep(1.0)
                continue
            entry = hist_resp.get(prompt_id)
            if entry:
                status = entry.get("status", {}) or {}
                if status.get("status_str") == "error":
                    self._warn_once("gen_error", f"ComfyUI reported a generation error for {prompt_id}")
                    return None
                if entry.get("outputs"):
                    return entry
            time.sleep(0.75)
        self._warn_once("timeout", f"generation timed out after {self.poll_timeout}s")
        return None


def _first_image_ref(outputs: dict) -> Optional[dict]:
    for node_out in outputs.values():
        images = node_out.get("images") or []
        if images:
            img = images[0]
            return {"filename": img["filename"], "subfolder": img.get("subfolder", ""),
                    "type": img.get("type", "output")}
    return None


def _build_txt2img_workflow(*, checkpoint: str, prompt: str, negative: str, seed: int,
                             width: int, height: int, steps: int, cfg: float) -> dict:
    """Minimal SDXL txt2img graph — CheckpointLoaderSimple -> CLIPTextEncode x2
    -> EmptyLatentImage -> KSampler -> VAEDecode -> SaveImage. Same node shape
    as src/blender/diffusion.py's _comfy_graph(), with a real checkpoint name."""
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": max(0, seed), "steps": steps, "cfg": cfg,
            "sampler_name": "dpmpp_2m", "scheduler": "karras", "denoise": 1.0,
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
              "inputs": {"filename_prefix": "sprite", "images": ["8", 0]}},
    }


def _build_img2img_workflow(*, checkpoint: str, prompt: str, negative: str, seed: int,
                             ref_filename: str, denoise: float, steps: int, cfg: float) -> dict:
    """SDXL img2img graph — same tail as txt2img, but the latent comes from
    VAEEncode(LoadImage(ref_filename)) instead of EmptyLatentImage, and
    KSampler runs at `denoise` < 1.0 so the result stays anchored to the
    reference instead of starting from pure noise."""
    return {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": max(0, seed), "steps": steps, "cfg": cfg,
            "sampler_name": "dpmpp_2m", "scheduler": "karras", "denoise": denoise,
            "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
            "latent_image": ["10", 0]}},
        "4": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": checkpoint}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative, "clip": ["4", 1]}},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "sprite_ref", "images": ["8", 0]}},
        "10": {"class_type": "VAEEncode",
               "inputs": {"pixels": ["11", 0], "vae": ["4", 2]}},
        "11": {"class_type": "LoadImage",
               "inputs": {"image": ref_filename}},
    }
