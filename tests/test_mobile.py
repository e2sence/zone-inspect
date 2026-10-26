"""Mobile workflow tests: QR generation, token-based photo upload."""

import io

import pytest

pytestmark = pytest.mark.mobile


class TestMobileQR:
    def test_generate_qr(self, authed_client, session_with_zones):
        sid, _ = session_with_zones
        resp = authed_client.post(f"/api/session/{sid}/mobile_qr")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "qr_b64" in data
        assert "url" in data
        assert "token" in data
        assert "/mobile?token=" in data["url"]

    def test_qr_session_not_found(self, authed_client):
        resp = authed_client.post("/api/session/nonexistent/mobile_qr")
        assert resp.status_code == 404


class TestMobileInfo:
    @pytest.fixture()
    def token_and_sid(self, authed_client, session_with_zones):
        sid, _ = session_with_zones
        resp = authed_client.post(f"/api/session/{sid}/mobile_qr")
        token = resp.get_json()["token"]
        return token, sid

    def test_mobile_info(self, client, token_and_sid, session_with_zones):
        token, sid = token_and_sid
        _, zones = session_with_zones
        resp = client.get(f"/api/mobile/{token}/info")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["session_id"] == sid
        assert data["zones_total"] == len(zones)
        assert data["inspection_state"] == "scanning"

    def test_invalid_token(self, client):
        resp = client.get("/api/mobile/bad_token_xyz/info")
        assert resp.status_code == 404


class TestMobileZonePhoto:
    @pytest.fixture()
    def token(self, authed_client, session_with_zones):
        sid, _ = session_with_zones
        resp = authed_client.post(f"/api/session/{sid}/mobile_qr")
        return resp.get_json()["token"]

    def test_photo_is_checked_on_upload(self, client, authed_client, token, test_image_bytes,
                                        session_with_zones):
        sid, _ = session_with_zones
        resp = client.post(
            f"/api/mobile/{token}/zone_photo",
            data={"photo": (io.BytesIO(test_image_bytes), "photo.jpg")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok" and "matched" in data      # verdict comes back to the phone
        # the desktop picks up the finished result, no processing on its side
        assert authed_client.get(f"/api/session/{sid}/mobile_photos").get_json()["count"] == 1
        result = authed_client.post(f"/api/session/{sid}/mobile_photos/next").get_json()
        assert "matched" in result and "all_scores" in result
        assert authed_client.post(f"/api/session/{sid}/mobile_photos/next").status_code == 404

    def test_upload_no_photo(self, client, token):
        resp = client.post(f"/api/mobile/{token}/zone_photo")
        assert resp.status_code == 400


class TestMobileSerial:
    def test_serial_read_on_the_phone(self, client, authed_client, session_with_zones):
        sid, _ = session_with_zones
        token = authed_client.post(f"/api/session/{sid}/mobile_qr").get_json()["token"]
        r = client.post(f"/api/mobile/{token}/serial", data={"code": "7334001122", "code_type": "Code128"})
        assert r.get_json() == {"serial": "7334001122", "type": "Code128"}
        assert authed_client.get(f"/api/session/{sid}/mobile_photos").get_json()["serial"] == "7334001122"


class TestMobilePhotosPolling:
    def test_desktop_polls_photos(self, authed_client, session_with_zones):
        sid, _ = session_with_zones
        resp = authed_client.get(f"/api/session/{sid}/mobile_photos")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "count" in data
        assert "serial" in data


class TestMobileRetryFailed:
    @pytest.fixture()
    def token_and_sid(self, authed_client, session_with_zones):
        sid, _ = session_with_zones
        resp = authed_client.post(f"/api/session/{sid}/mobile_qr")
        return resp.get_json()["token"], sid

    def test_mobile_retry(self, client, token_and_sid):
        token, sid = token_and_sid
        import app as app_module
        app_module.sessions[sid]["checked"][0] = {
            "score": 0.8,
            "defect_info": {"status": "defect"},
        }
        resp = client.post(f"/api/mobile/{token}/retry_failed")
        assert resp.status_code == 200
        assert resp.get_json()["cleared"] == 1


class TestMobileSkipBoard:
    @pytest.fixture()
    def token_and_sid(self, authed_client, session_with_zones):
        sid, _ = session_with_zones
        resp = authed_client.post(f"/api/session/{sid}/mobile_qr")
        return resp.get_json()["token"], sid

    def test_skip_clears_session(self, client, token_and_sid):
        token, sid = token_and_sid
        import app as app_module
        # Set some checked data
        app_module.sessions[sid]["checked"][0] = {
            "score": 0.9, "defect_info": {"status": "ok"},
        }
        resp = client.post(f"/api/mobile/{token}/skip")
        assert resp.status_code == 200
        assert app_module.sessions[sid]["checked"] == {}
        assert app_module.sessions[sid].get("_board_seq", 0) >= 1
