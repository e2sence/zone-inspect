"""Unit tests for helper functions in app.py."""

import base64

import cv2
import pytest

pytestmark = pytest.mark.unit
import numpy as np


# ── _allowed ──────────────────────────────────────────────────────────────────
class TestAllowed:
    def test_valid_extensions(self, flask_app):
        from app import _allowed
        for ext in ("png", "jpg", "jpeg", "bmp", "webp"):
            assert _allowed(f"photo.{ext}"), f"{ext} should be allowed"

    def test_uppercase_extensions(self, flask_app):
        from app import _allowed
        assert _allowed("PHOTO.JPG")
        assert _allowed("board.PNG")

    def test_invalid_extensions(self, flask_app):
        from app import _allowed
        assert not _allowed("script.py")
        assert not _allowed("data.csv")
        assert not _allowed("readme.txt")

    def test_no_extension(self, flask_app):
        from app import _allowed
        assert not _allowed("noext")
        assert not _allowed("")


# ── _crop_zone ────────────────────────────────────────────────────────────────
class TestCropZone:
    def test_basic_crop(self, flask_app, test_image):
        from app import _crop_zone
        zone = {"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}
        crop = _crop_zone(test_image, zone)
        h, w = test_image.shape[:2]
        assert crop.shape == (h // 2, w // 2, 3)

    def test_full_image(self, flask_app, test_image):
        from app import _crop_zone
        zone = {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
        crop = _crop_zone(test_image, zone)
        assert crop.shape == test_image.shape

    def test_small_corner(self, flask_app, test_image):
        from app import _crop_zone
        zone = {"x": 0.9, "y": 0.9, "w": 0.1, "h": 0.1}
        crop = _crop_zone(test_image, zone)
        assert crop.shape[0] > 0
        assert crop.shape[1] > 0

    def test_clamped_out_of_bounds(self, flask_app, test_image):
        """Coords exceeding 1.0 should be clamped to image boundary."""
        from app import _crop_zone
        zone = {"x": 0.8, "y": 0.8, "w": 0.5, "h": 0.5}
        crop = _crop_zone(test_image, zone)
        h, w = test_image.shape[:2]
        assert crop.shape[0] <= h
        assert crop.shape[1] <= w


# ── _glare_mask ───────────────────────────────────────────────────────────────
class TestGlareMask:
    def test_bright_pixels_masked(self, flask_app, bright_image):
        from app import _glare_mask
        gray = cv2.cvtColor(bright_image, cv2.COLOR_BGR2GRAY)
        mask = _glare_mask(gray)
        # Bright pixels should be masked out (0)
        assert mask.shape == gray.shape
        assert np.mean(mask) < 128  # mostly masked

    def test_dark_pixels_kept(self, flask_app, dark_image):
        from app import _glare_mask
        gray = cv2.cvtColor(dark_image, cv2.COLOR_BGR2GRAY)
        mask = _glare_mask(gray)
        # Dark pixels should be kept (255)
        assert np.mean(mask) == 255


# ── _normalize_lighting ──────────────────────────────────────────────────────
class TestNormalizeLighting:
    def test_same_shape(self, flask_app, test_image):
        from app import _normalize_lighting
        result = _normalize_lighting(test_image)
        assert result.shape == test_image.shape
        assert result.dtype == np.uint8

    def test_no_nans(self, flask_app, test_image):
        from app import _normalize_lighting
        result = _normalize_lighting(test_image)
        assert not np.any(np.isnan(result.astype(float)))


# ── _img_to_b64 ──────────────────────────────────────────────────────────────
class TestImgToB64:
    def test_roundtrip(self, flask_app, test_image):
        from app import _img_to_b64
        b64_str = _img_to_b64(test_image)
        # Decode back
        raw = base64.b64decode(b64_str)
        arr = np.frombuffer(raw, np.uint8)
        decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        assert decoded is not None
        assert decoded.shape == test_image.shape

    def test_valid_base64(self, flask_app, test_image):
        from app import _img_to_b64
        b64_str = _img_to_b64(test_image)
        # Should be pure ASCII base64 with no data URI prefix
        assert isinstance(b64_str, str)
        base64.b64decode(b64_str)  # should not raise
