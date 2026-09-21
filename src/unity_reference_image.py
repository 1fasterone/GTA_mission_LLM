"""Transient reference-image input for the Unity Concept stage.

The image is deliberately kept outside the Unity schema and post-schema
pipeline.  This module only validates a local image and converts it to the
data-URI shape already supported by the OpenAI-compatible chat client.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path


class ReferenceImageError(ValueError):
    """The requested reference image cannot be sent to Concept."""


_SUPPORTED_MIME_TYPES = frozenset({
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
})


def encode_reference_image(path: str | Path) -> str:
    """Return a validated local image as an OpenAI vision data URI.

    The returned URI is an in-memory Step-2 input.  Callers must not persist it
    in the schema or pass it to any later pipeline stage.
    """
    image_path = Path(path)
    if not image_path.is_file():
        raise ReferenceImageError(
            f"reference image not found: {image_path}. "
            "Pass an existing PNG, JPEG, WEBP, or GIF file."
        )

    mime_type, _ = mimetypes.guess_type(image_path.name)
    if mime_type not in _SUPPORTED_MIME_TYPES:
        supported = ", ".join(sorted(_SUPPORTED_MIME_TYPES))
        raise ReferenceImageError(
            f"unsupported reference image type for {image_path.name!r}: "
            f"{mime_type or 'unknown'}; expected {supported}"
        )

    image_bytes = image_path.read_bytes()
    if not image_bytes:
        raise ReferenceImageError(f"reference image is empty: {image_path}")

    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"
