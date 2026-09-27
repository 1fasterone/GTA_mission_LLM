"""Deterministic screenshot comparison for visual-reference playtests.

The Unity test owns capture; this module owns the judgment.  It compares the
reference board and the scene-only screenshot on three independent signals:

* normalized landmark centroid error,
* foreground occupancy intersection-over-union, and
* coarse RGB palette cosine similarity.

No image model, OCR, feature matcher, or random sampling is used.  Landmark
rectangles come from the closed Slice-6.3 spatial-fusion sidecar, never from
keyword inference in the comparison gate.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image


class ScreenshotDiffError(ValueError):
    """Raised when a visual-comparison input cannot be validated."""


@dataclass(frozen=True)
class ScreenshotDiffThresholds:
    max_landmark_centroid_error: float = 0.12
    min_occupancy_iou: float = 0.45
    min_palette_similarity: float = 0.75


@dataclass
class ScreenshotDiffResult:
    ok: bool
    reason: str = ""
    landmark_centroid_error: float | None = None
    occupancy_iou: float | None = None
    palette_similarity: float | None = None
    landmark_errors: dict[str, float] = field(default_factory=dict)
    missing_landmarks: list[str] = field(default_factory=list)

    def render(self) -> str:
        status = "PASS" if self.ok else "FAIL"
        metrics = (
            f"centroid_error={self.landmark_centroid_error:.3f} "
            f"occupancy_iou={self.occupancy_iou:.3f} "
            f"palette_similarity={self.palette_similarity:.3f}"
            if self.landmark_centroid_error is not None
            and self.occupancy_iou is not None
            and self.palette_similarity is not None
            else "metrics unavailable"
        )
        return f"VISUAL DIFF {status} — {metrics}" + (
            f" — {self.reason}" if self.reason else ""
        )


def _read_payload(source: Path | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(source, Mapping):
        return source
    try:
        value = json.loads(Path(source).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScreenshotDiffError(
            f"visual spatial-fusion sidecar is not valid JSON: {source}"
        ) from exc
    if not isinstance(value, Mapping):
        raise ScreenshotDiffError("visual spatial-fusion sidecar must be an object")
    return value


def _bbox(value: Any, *, label: str) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise ScreenshotDiffError(f"{label} must be an object")
    try:
        result = {key: float(value[key]) for key in ("x", "y", "w", "h")}
    except (KeyError, TypeError, ValueError) as exc:
        raise ScreenshotDiffError(f"{label} must contain numeric x/y/w/h") from exc
    x, y, width, height = (result[key] for key in ("x", "y", "w", "h"))
    if not all(math.isfinite(number) for number in result.values()):
        raise ScreenshotDiffError(f"{label} contains a non-finite value")
    if (x < 0.0 or y < 0.0 or width <= 0.0 or height <= 0.0
            or x + width > 1.000001 or y + height > 1.000001):
        raise ScreenshotDiffError(f"{label} is outside normalized image bounds")
    return {key: round(result[key], 6) for key in ("x", "y", "w", "h")}


def load_landmark_rectangles(
    source: Path | Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Read bound normalized landmark rectangles from a fusion sidecar."""
    payload = _read_payload(source)
    rows = payload.get("bindings")
    if not isinstance(rows, list):
        rows = payload.get("entity_regions")
    if not isinstance(rows, list):
        raise ScreenshotDiffError(
            "visual spatial-fusion sidecar has no bindings/entity_regions list"
        )

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ScreenshotDiffError(f"visual fusion row {index} must be an object")
        status = row.get("status") or row.get("binding_status")
        if status != "bound":
            continue
        schema_constraints = row.get("schema_constraints")
        role = ""
        if isinstance(schema_constraints, Mapping):
            role = str(schema_constraints.get("role") or "")
        role = role or str(row.get("role") or "")
        if role.strip().lower() in {"ui", "effect"}:
            continue
        entity = str(row.get("entity") or "").strip()
        if not entity:
            raise ScreenshotDiffError(f"visual fusion row {index} has no entity")
        if entity in seen:
            raise ScreenshotDiffError(f"visual fusion repeats bound entity {entity!r}")
        composition = row.get("composition")
        if not isinstance(composition, Mapping):
            raise ScreenshotDiffError(
                f"visual fusion bound entity {entity!r} has no composition"
            )
        raw_box = composition.get("bbox")
        if raw_box is None:
            center = composition.get("center")
            extent = composition.get("extent")
            if not isinstance(center, Mapping) or not isinstance(extent, Mapping):
                raise ScreenshotDiffError(
                    f"visual fusion bound entity {entity!r} has no landmark rectangle"
                )
            try:
                cx, cy = float(center["x"]), float(center["y"])
                width, height = float(extent["w"]), float(extent["h"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ScreenshotDiffError(
                    f"visual fusion landmark rectangle for {entity!r} is invalid"
                ) from exc
            raw_box = {
                "x": cx - width / 2.0,
                "y": cy - height / 2.0,
                "w": width,
                "h": height,
            }
        seen.add(entity)
        result.append({"entity": entity, "bbox": _bbox(raw_box, label=f"{entity}.bbox")})
    return sorted(result, key=lambda row: row["entity"])


def _open_rgb(path: Path) -> Image.Image:
    if not path.is_file():
        raise ScreenshotDiffError(f"screenshot comparison image not found: {path}")
    try:
        return Image.open(path).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - all decode failures are input failures
        raise ScreenshotDiffError(f"could not open screenshot comparison image: {path}") from exc


def _border_colour(image: Image.Image) -> tuple[int, int, int]:
    width, height = image.size
    points: list[tuple[int, int, int]] = []
    step_x = max(1, width // 64)
    step_y = max(1, height // 64)

    def add(x: int, y: int) -> None:
        pixel: Any = image.getpixel((x, y))
        if isinstance(pixel, tuple) and len(pixel) >= 3:
            points.append((int(pixel[0]), int(pixel[1]), int(pixel[2])))

    for x in range(0, width, step_x):
        add(x, 0)
        add(x, height - 1)
    for y in range(0, height, step_y):
        add(0, y)
        add(width - 1, y)
    if not points:
        return (0, 0, 0)
    return (
        sorted(point[0] for point in points)[len(points) // 2],
        sorted(point[1] for point in points)[len(points) // 2],
        sorted(point[2] for point in points)[len(points) // 2],
    )


def _colour_distance(left: tuple[int, int, int], right: tuple[int, int, int]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)))


def _rgb_pixels(image: Image.Image) -> list[tuple[int, int, int]]:
    width, height = image.size
    pixels: Any = image.load()
    return [
        (int(pixels[x, y][0]), int(pixels[x, y][1]), int(pixels[x, y][2]))
        for y in range(height)
        for x in range(width)
    ]


def _foreground_mask(image: Image.Image, threshold: float = 24.0) -> list[bool]:
    background = _border_colour(image)
    return [
        _colour_distance(pixel, background) >= threshold
        for pixel in _rgb_pixels(image)
    ]


def _window(
    bbox: Mapping[str, float], width: int, height: int, expand: float,
) -> tuple[int, int, int, int]:
    x0 = max(0, int(math.floor((bbox["x"] - expand) * width)))
    y0 = max(0, int(math.floor((bbox["y"] - expand) * height)))
    x1 = min(width, int(math.ceil((bbox["x"] + bbox["w"] + expand) * width)))
    y1 = min(height, int(math.ceil((bbox["y"] + bbox["h"] + expand) * height)))
    return x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)


def _salient_centroid(
    image: Image.Image,
    mask: Sequence[bool],
    bbox: Mapping[str, float],
    *,
    expand: float,
) -> tuple[float, float] | None:
    width, height = image.size
    x0, y0, x1, y1 = _window(bbox, width, height, expand)
    points: list[tuple[int, int]] = []
    for y in range(y0, y1):
        for x in range(x0, x1):
            if mask[y * width + x]:
                points.append((x, y))
    if not points:
        return None
    return (
        sum(point[0] for point in points) / len(points) / width,
        sum(point[1] for point in points) / len(points) / height,
    )


def _occupancy_iou(left: Sequence[bool], right: Sequence[bool]) -> float:
    intersection = sum(a and b for a, b in zip(left, right))
    union = sum(a or b for a, b in zip(left, right))
    return 1.0 if union == 0 else intersection / union


def _palette_histogram(image: Image.Image, bins: int = 4) -> Counter[tuple[int, int, int]]:
    histogram: Counter[tuple[int, int, int]] = Counter()
    for r, g, b in _rgb_pixels(image):
        histogram[(r * bins // 256, g * bins // 256, b * bins // 256)] += 1
    return histogram


def _palette_similarity(left: Image.Image, right: Image.Image) -> float:
    lhs = _palette_histogram(left)
    rhs = _palette_histogram(right)
    keys = set(lhs) | set(rhs)
    dot = sum(lhs[key] * rhs[key] for key in keys)
    left_norm = math.sqrt(sum(lhs[key] ** 2 for key in keys))
    right_norm = math.sqrt(sum(rhs[key] ** 2 for key in keys))
    if left_norm == 0.0 or right_norm == 0.0:
        return 1.0 if left == right else 0.0
    return dot / (left_norm * right_norm)


def compare_screenshot_to_reference(
    reference_image: Path,
    screenshot: Path,
    landmarks: Sequence[Mapping[str, Any]],
    *,
    thresholds: ScreenshotDiffThresholds | None = None,
) -> ScreenshotDiffResult:
    """Compare one scene-only screenshot against its canonical board image."""
    thresholds = thresholds or ScreenshotDiffThresholds()
    if not landmarks:
        return ScreenshotDiffResult(
            ok=False,
            reason="visual comparison has no bound landmark rectangles",
        )
    reference = _open_rgb(reference_image)
    candidate = _open_rgb(screenshot)
    if reference.size != candidate.size:
        candidate = candidate.resize(reference.size, Image.Resampling.BILINEAR)

    reference_mask = _foreground_mask(reference)
    candidate_mask = _foreground_mask(candidate)
    errors: dict[str, float] = {}
    missing: list[str] = []
    for landmark in landmarks:
        entity = str(landmark.get("entity") or "").strip()
        if not entity:
            return ScreenshotDiffResult(
                ok=False, reason="visual comparison contains a landmark without an entity"
            )
        box = _bbox(landmark.get("bbox"), label=f"{entity}.bbox")
        expected = _salient_centroid(reference, reference_mask, box, expand=0.0)
        actual = _salient_centroid(candidate, candidate_mask, box, expand=0.18)
        if expected is None or actual is None:
            missing.append(entity)
            errors[entity] = 1.0
            continue
        errors[entity] = math.dist(expected, actual)

    centroid_error = sum(errors.values()) / len(errors) if errors else 1.0
    occupancy = _occupancy_iou(reference_mask, candidate_mask)
    palette = _palette_similarity(reference, candidate)
    reasons: list[str] = []
    if missing:
        reasons.append("missing landmark pixels: " + ", ".join(sorted(missing)))
    if centroid_error > thresholds.max_landmark_centroid_error:
        reasons.append(
            f"landmark centroid error {centroid_error:.3f} exceeds "
            f"{thresholds.max_landmark_centroid_error:.3f}"
        )
    if occupancy < thresholds.min_occupancy_iou:
        reasons.append(
            f"occupancy IoU {occupancy:.3f} is below "
            f"{thresholds.min_occupancy_iou:.3f}"
        )
    if palette < thresholds.min_palette_similarity:
        reasons.append(
            f"palette similarity {palette:.3f} is below "
            f"{thresholds.min_palette_similarity:.3f}"
        )
    return ScreenshotDiffResult(
        ok=not reasons,
        reason="; ".join(reasons),
        landmark_centroid_error=round(centroid_error, 6),
        occupancy_iou=round(occupancy, 6),
        palette_similarity=round(palette, 6),
        landmark_errors={key: round(value, 6) for key, value in sorted(errors.items())},
        missing_landmarks=sorted(missing),
    )


__all__ = [
    "ScreenshotDiffError",
    "ScreenshotDiffResult",
    "ScreenshotDiffThresholds",
    "compare_screenshot_to_reference",
    "load_landmark_rectangles",
]
