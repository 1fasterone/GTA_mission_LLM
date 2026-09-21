"""Reference images are transient inputs to Unity Step 2 only."""

import base64

import pytest

from src.unity_reference_image import ReferenceImageError, encode_reference_image


def test_encode_reference_image_returns_data_uri(tmp_path):
    image = tmp_path / "racing.png"
    image.write_bytes(b"png-bytes")

    assert encode_reference_image(image) == (
        "data:image/png;base64," + base64.b64encode(b"png-bytes").decode("ascii")
    )


def test_encode_reference_image_rejects_missing_file(tmp_path):
    with pytest.raises(ReferenceImageError, match="not found"):
        encode_reference_image(tmp_path / "missing.png")


def test_encode_reference_image_rejects_unsupported_type(tmp_path):
    image = tmp_path / "racing.bmp"
    image.write_bytes(b"bmp-bytes")

    with pytest.raises(ReferenceImageError, match="unsupported reference image type"):
        encode_reference_image(image)
