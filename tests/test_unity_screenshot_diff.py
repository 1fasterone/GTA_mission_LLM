"""Deterministic gates for visual-comparison screenshots."""

from pathlib import Path

from PIL import Image, ImageDraw

from src.unity_screenshot_diff import (
    ScreenshotDiffThresholds,
    compare_screenshot_to_reference,
    load_landmark_rectangles,
)


SIZE = (64, 36)
BOX = {"x": 0.25, "y": 0.25, "w": 0.1875, "h": 0.277778}
LANDMARKS = [{"entity": "Lantern", "bbox": BOX}]


def _board(colour: tuple[int, int, int], *, x: int = 16) -> Image.Image:
    image = Image.new("RGB", SIZE, (20, 35, 50))
    draw = ImageDraw.Draw(image)
    draw.rectangle((x, 9, x + 11, 18), fill=colour)
    return image


def _save(image: Image.Image, path: Path) -> Path:
    image.save(path)
    return path


def test_matching_board_passes_all_visual_gates(tmp_path):
    reference = _save(_board((145, 205, 85)), tmp_path / "reference.png")
    screenshot = _save(_board((145, 205, 85)), tmp_path / "screenshot.png")

    result = compare_screenshot_to_reference(reference, screenshot, LANDMARKS)

    assert result.ok is True
    assert result.landmark_centroid_error == 0.0
    assert result.occupancy_iou == 1.0
    assert result.palette_similarity == 1.0


def test_shifted_landmark_fails_centroid_gate_without_requiring_other_metrics(
    tmp_path,
):
    reference = _save(_board((145, 205, 85)), tmp_path / "reference.png")
    screenshot = _save(_board((145, 205, 85), x=40), tmp_path / "screenshot.png")
    thresholds = ScreenshotDiffThresholds(
        max_landmark_centroid_error=0.12,
        min_occupancy_iou=0.0,
        min_palette_similarity=0.0,
    )

    result = compare_screenshot_to_reference(
        reference, screenshot, LANDMARKS, thresholds=thresholds
    )

    assert result.ok is False
    assert result.landmark_centroid_error is not None
    assert result.landmark_centroid_error > 0.12
    assert "centroid error" in result.reason


def test_extra_foreground_fails_occupancy_gate(tmp_path):
    reference = _save(_board((145, 205, 85)), tmp_path / "reference.png")
    candidate = _board((145, 205, 85))
    ImageDraw.Draw(candidate).rectangle((48, 25, 63, 35), fill=(145, 205, 85))
    screenshot = _save(candidate, tmp_path / "screenshot.png")
    thresholds = ScreenshotDiffThresholds(
        max_landmark_centroid_error=1.0,
        min_occupancy_iou=0.45,
        min_palette_similarity=0.0,
    )

    result = compare_screenshot_to_reference(
        reference, screenshot, LANDMARKS, thresholds=thresholds
    )

    assert result.ok is False
    assert result.occupancy_iou is not None
    assert result.occupancy_iou < 0.45
    assert "occupancy IoU" in result.reason


def test_palette_gate_detects_colour_drift_with_matching_occupancy(tmp_path):
    reference = _save(_board((145, 205, 85)), tmp_path / "reference.png")
    screenshot = _save(_board((220, 40, 40)), tmp_path / "screenshot.png")
    thresholds = ScreenshotDiffThresholds(
        max_landmark_centroid_error=1.0,
        min_occupancy_iou=1.0,
        min_palette_similarity=0.999,
    )

    result = compare_screenshot_to_reference(
        reference, screenshot, LANDMARKS, thresholds=thresholds
    )

    assert result.ok is False
    assert result.palette_similarity is not None
    assert result.palette_similarity < 0.999
    assert "palette similarity" in result.reason


def test_missing_landmark_pixels_are_a_loud_failure(tmp_path):
    reference = _save(_board((145, 205, 85)), tmp_path / "reference.png")
    screenshot = _save(Image.new("RGB", SIZE, (20, 35, 50)), tmp_path / "screenshot.png")

    result = compare_screenshot_to_reference(reference, screenshot, LANDMARKS)

    assert result.ok is False
    assert result.missing_landmarks == ["Lantern"]
    assert "missing landmark pixels" in result.reason


def test_load_landmarks_accepts_fusion_bindings_and_legacy_center_extent():
    payload = {
        "bindings": [
            {
                "entity": "Lantern",
                "status": "bound",
                "composition": {"bbox": BOX},
            },
            {
                "entity": "Rock",
                "status": "unbound",
                "composition": {"bbox": BOX},
            },
            {
                "entity": "Hud",
                "status": "bound",
                "schema_constraints": {"role": "ui"},
                "composition": {"bbox": BOX},
            },
            {
                "entity": "GlowEffect",
                "status": "bound",
                "role": "effect",
                "composition": {"bbox": BOX},
            },
            {
                "entity": "Water",
                "status": "bound",
                "composition": {
                    "center": {"x": 0.75, "y": 0.5},
                    "extent": {"w": 0.1, "h": 0.2},
                },
            },
        ]
    }

    result = load_landmark_rectangles(payload)

    assert [row["entity"] for row in result] == ["Lantern", "Water"]
    assert result[1]["bbox"] == {"x": 0.7, "y": 0.4, "w": 0.1, "h": 0.2}
