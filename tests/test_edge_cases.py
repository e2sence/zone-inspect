"""Edge-case and robustness tests."""

import io

import cv2
import numpy as np
import pytest


class TestLargeImage:
    def test_large_image_accepted(self, authed_client):
        """4000x3000 image should be accepted (within 128 MB limit)."""
        img = np.zeros((3000, 4000, 3), dtype=np.uint8)
        _, buf = cv2.imencode(".jpg", img)
        resp = authed_client.post(
            "/api/session",
            data={"image": (io.BytesIO(buf.tobytes()), "big.jpg")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200


class TestTinyImage:
    def test_tiny_image_accepted(self, authed_client):
        """1x1 image should still be accepted."""
        img = np.zeros((1, 1, 3), dtype=np.uint8)
        _, buf = cv2.imencode(".jpg", img)
        resp = authed_client.post(
            "/api/session",
            data={"image": (io.BytesIO(buf.tobytes()), "tiny.jpg")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200


class TestCorruptImage:
    def test_corrupt_jpeg(self, authed_client):
        """Random bytes with .jpg extension → 400 (can't decode)."""
        resp = authed_client.post(
            "/api/session",
            data={"image": (io.BytesIO(b"\xff\xd8\xff\x00garbage"), "bad.jpg")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_zero_byte_file(self, authed_client):
        """Empty file → 400."""
        resp = authed_client.post(
            "/api/session",
            data={"image": (io.BytesIO(b""), "empty.jpg")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400


class TestUnicodeFilename:
    def test_unicode_filename(self, authed_client):
        """Filename with unicode chars should be accepted (valid ext)."""
        img = np.zeros((100, 100, 3), dtype=np.uint8)
        _, buf = cv2.imencode(".png", img)
        resp = authed_client.post(
            "/api/session",
            data={"image": (io.BytesIO(buf.tobytes()), "плата.png")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200


class TestAllImageFormats:
    @pytest.mark.parametrize("ext,encode_ext", [
        ("jpg", ".jpg"),
        ("jpeg", ".jpeg"),
        ("png", ".png"),
        ("bmp", ".bmp"),
        ("webp", ".webp"),
    ])
    def test_format_accepted(self, authed_client, ext, encode_ext):
        img = np.zeros((50, 50, 3), dtype=np.uint8)
        _, buf = cv2.imencode(encode_ext, img)
        resp = authed_client.post(
            "/api/session",
            data={"image": (io.BytesIO(buf.tobytes()), f"photo.{ext}")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200


class TestSessionIsolation:
    def test_sessions_are_isolated(self, authed_client, test_image_bytes):
        """Two sessions should not share state."""
        # Create session 1
        r1 = authed_client.post(
            "/api/session",
            data={"image": (io.BytesIO(test_image_bytes), "a.jpg")},
            content_type="multipart/form-data",
        )
        sid1 = r1.get_json()["session_id"]

        # Create session 2
        r2 = authed_client.post(
            "/api/session",
            data={"image": (io.BytesIO(test_image_bytes), "b.jpg")},
            content_type="multipart/form-data",
        )
        sid2 = r2.get_json()["session_id"]

        assert sid1 != sid2

        # Set zones only on session 1
        authed_client.post(
            f"/api/session/{sid1}/zones",
            json={"zones": [{"x": 0, "y": 0, "w": 1, "h": 1, "label": "Z"}]},
        )

        # Session 2 should have no zones
        status = authed_client.get(f"/api/session/{sid2}/status").get_json()
        assert status["progress"]["total"] == 0


class TestAnalyzeDefects:
    """Unit test for _analyze_defects with synthetic images."""

    def test_identical_images_ok(self, flask_app, test_image):
        from app import _analyze_defects
        result = _analyze_defects(test_image, test_image.copy())
        assert result["status"] == "ok"
        assert result["ssim"] > 0.95

    def test_different_images_detect(self, flask_app):
        from app import _analyze_defects
        # Create two clearly different images
        img_a = np.zeros((256, 256, 3), dtype=np.uint8)
        img_a[:] = (80, 80, 80)
        img_b = np.full((256, 256, 3), 220, dtype=np.uint8)
        cv2.rectangle(img_b, (10, 10), (240, 240), (0, 0, 255), -1)
        result = _analyze_defects(img_a, img_b)
        assert result["status"] in ("warn", "defect")
        assert result["defect_pct"] > 0

    def test_output_has_all_fields(self, flask_app, test_image):
        from app import _analyze_defects
        result = _analyze_defects(test_image, test_image.copy())
        for key in ("ssim", "defect_pct", "defect_count", "verdict",
                     "status", "vis_defects_b64", "vis_heatmap_b64",
                     "extracted_b64", "reference_b64"):
            assert key in result, f"missing key: {key}"
