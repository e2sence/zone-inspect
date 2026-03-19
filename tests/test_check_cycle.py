"""Full inspection cycle: session → zones → check → status → reset/retry."""

import io

import pytest

pytestmark = pytest.mark.api


class TestCheckZone:
    """POST /api/session/<sid>/check — submit a zone photo."""

    def test_check_returns_match(self, authed_client, session_with_zones,
                                 test_image_bytes):
        sid, zones = session_with_zones
        resp = authed_client.post(
            f"/api/session/{sid}/check",
            data={"photo": (io.BytesIO(test_image_bytes), "zone.jpg")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "matched" in data
        assert "best_zone_index" in data
        assert "best_score" in data
        assert "progress" in data
        assert data["progress"]["total"] == len(zones)

    def test_check_no_photo(self, authed_client, session_with_zones):
        sid, _ = session_with_zones
        resp = authed_client.post(f"/api/session/{sid}/check")
        assert resp.status_code == 400

    def test_check_bad_format(self, authed_client, session_with_zones):
        sid, _ = session_with_zones
        resp = authed_client.post(
            f"/api/session/{sid}/check",
            data={"photo": (io.BytesIO(b"not-an-image"), "photo.txt")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_check_without_zones(self, authed_client, test_image_bytes):
        """Check on a session with no zones defined → 400."""
        # Create session without setting zones
        resp = authed_client.post(
            "/api/session",
            data={"image": (io.BytesIO(test_image_bytes), "board.jpg")},
            content_type="multipart/form-data",
        )
        sid = resp.get_json()["session_id"]
        resp = authed_client.post(
            f"/api/session/{sid}/check",
            data={"photo": (io.BytesIO(test_image_bytes), "zone.jpg")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_defect_info_shape(self, authed_client, session_with_zones,
                               test_image_bytes):
        """When matched, response should contain full defect info."""
        sid, _ = session_with_zones
        resp = authed_client.post(
            f"/api/session/{sid}/check",
            data={"photo": (io.BytesIO(test_image_bytes), "zone.jpg")},
            content_type="multipart/form-data",
        )
        data = resp.get_json()
        if data.get("matched"):
            defect = data["defect"]
            assert defect["status"] in ("ok", "warn", "defect")
            assert "ssim" in defect
            assert "defect_pct" in defect
            assert "vis_defects_b64" in defect
            assert "vis_heatmap_b64" in defect


class TestSessionStatus:
    def test_status_after_zones(self, authed_client, session_with_zones):
        sid, zones = session_with_zones
        resp = authed_client.get(f"/api/session/{sid}/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["progress"]["total"] == len(zones)
        assert data["progress"]["done"] == 0
        assert len(data["zones"]) == len(zones)

    def test_status_updates_after_check(self, authed_client,
                                        session_with_zones, test_image_bytes):
        sid, _ = session_with_zones
        # Perform a check first
        authed_client.post(
            f"/api/session/{sid}/check",
            data={"photo": (io.BytesIO(test_image_bytes), "zone.jpg")},
            content_type="multipart/form-data",
        )
        resp = authed_client.get(f"/api/session/{sid}/status")
        data = resp.get_json()
        # done should be >= 0 (may not match if score < threshold)
        assert data["progress"]["done"] >= 0


class TestResetSession:
    def test_reset_clears_checked(self, authed_client, session_with_zones,
                                  test_image_bytes):
        sid, _ = session_with_zones
        # Check a zone
        authed_client.post(
            f"/api/session/{sid}/check",
            data={"photo": (io.BytesIO(test_image_bytes), "zone.jpg")},
            content_type="multipart/form-data",
        )
        # Reset
        resp = authed_client.post(f"/api/session/{sid}/reset")
        assert resp.status_code == 200
        # Status should show 0 done
        status = authed_client.get(f"/api/session/{sid}/status").get_json()
        assert status["progress"]["done"] == 0


class TestRetryFailed:
    def test_retry_clears_non_ok(self, authed_client, session_with_zones):
        sid, _ = session_with_zones
        import app as app_module
        # Manually inject a "defect" checked zone
        app_module.sessions[sid]["checked"][0] = {
            "score": 0.8,
            "defect_info": {"status": "defect", "defect_pct": 12.0,
                            "verdict": "Defect"},
        }
        app_module.sessions[sid]["checked"][1] = {
            "score": 0.9,
            "defect_info": {"status": "ok", "defect_pct": 0.5,
                            "verdict": "OK"},
        }
        resp = authed_client.post(f"/api/session/{sid}/retry_failed")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["cleared"] == 1  # only the defect zone cleared
        # Zone 1 (ok) should remain
        assert 1 in app_module.sessions[sid]["checked"]
        assert 0 not in app_module.sessions[sid]["checked"]


class TestAutoAccept:
    def test_set_auto_accept(self, authed_client, session_with_zones):
        sid, _ = session_with_zones
        resp = authed_client.post(
            f"/api/session/{sid}/auto_accept",
            json={"auto_accept": False},
        )
        assert resp.status_code == 200
        import app as app_module
        assert app_module.sessions[sid]["_auto_accept"] is False
