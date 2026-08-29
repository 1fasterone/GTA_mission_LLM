"""
Background removal for generated sprite frames — segmentation-based, not
color keying.

Runs once at freeze-time (see src/sprites/content.py) on the raw PNG bytes a
ComfyUIBackend.generate() call returned. rembg's U2-Net model call is the
nondeterministic-adjacent step conceptually, but the model itself is a fixed,
version-pinned local ONNX file — same input always produces the same mask —
so this is exactly as safe to run inside "freeze the output"
(docs/model-pipeline-architecture.md) as the resize/format-convert steps
elsewhere in the deterministic half of the pipeline.

Chroma-keying was the original plan here, but confirmed live (2026-08-27):
this SDXL checkpoint has a strong, prompt-resistant bias toward framing a
requested flat/green-screen background as a mounted/presented object
(sticker card, gallery canvas, illustrated scene) — three attempts in a row
defeated corner-based color-distance keying no matter how the prompt was
tuned. Segmentation isolates the subject regardless of what SDXL draws behind
it, so the background no longer needs prompt-level control at all. See
comfyui_backend.py's module comment for the same history.
"""

from __future__ import annotations

import io

from PIL import Image

_session = None   # lazy singleton — one ONNX session per process, not per frame


def _get_session():
    global _session
    if _session is None:
        from rembg import new_session
        _session = new_session("u2net")
    return _session


def remove_background_to_png(raw_png_bytes: bytes) -> bytes:
    """Segment the subject out of raw_png_bytes, return new PNG bytes with
    alpha. Raises on a corrupt/unreadable image — the caller (content.py)
    catches that and substitutes a placeholder rather than shipping garbage."""
    from rembg import remove
    return remove(raw_png_bytes, session=_get_session())


def solid_placeholder_png(width: int, height: int) -> bytes:
    """Unmistakable 'missing art' placeholder (opaque magenta) — used when
    generation fails, never disguised as real output. Same convention as
    engines' classic missing-texture magenta."""
    img = Image.new("RGBA", (width, height), (255, 0, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
