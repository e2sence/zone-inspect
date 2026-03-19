"""API endpoint tests for PCB Zone Check."""

import io
import time

import pytest

pytestmark = [pytest.mark.api, pytest.mark.auth]

import pytest


class TestCreateSession:
    def test_success(self, authed_client, test_image_bytes):
        resp = authed_client.post(
            "/api/session",
            data={"image": (io.BytesIO(test_image_bytes), "board.jpg")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "session_id" in data
        assert "image_b64" in data
        assert data["width"] > 0 and data["height"] > 0

    def test_no_file(self, authed_client):
        resp = authed_client.post("/api/session")
        assert resp.status_code == 400

    def test_bad_extension(self, authed_client):
        resp = authed_client.post(
            "/api/session",
            data={"image": (io.BytesIO(b"not-an-image"), "data.csv")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400


class TestSetZones:
    @pytest.fixture()
    def session_id(self, authed_client, test_image_bytes):
        """Create a session and return its ID."""
        resp = authed_client.post(
            "/api/session",
            data={"image": (io.BytesIO(test_image_bytes), "board.jpg")},
            content_type="multipart/form-data",
        )
        return resp.get_json()["session_id"]

    def test_set_zones_ok(self, authed_client, session_id):
        zones = [
            {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3, "label": "IC1"},
            {"x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2, "label": "R1"},
        ]
        resp = authed_client.post(
            f"/api/session/{session_id}/zones",
            json={"zones": zones},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["count"] == 2
        assert len(data["previews"]) == 2

    def test_empty_zones(self, authed_client, session_id):
        resp = authed_client.post(
            f"/api/session/{session_id}/zones",
            json={"zones": []},
        )
        assert resp.status_code == 400

    def test_missing_zones_field(self, authed_client, session_id):
        resp = authed_client.post(
            f"/api/session/{session_id}/zones",
            json={"something": "else"},
        )
        assert resp.status_code == 400


class TestSessionNotFound:
    def test_zones_404(self, authed_client):
        resp = authed_client.post(
            "/api/session/nonexistent/zones",
            json={"zones": [{"x": 0, "y": 0, "w": 1, "h": 1}]},
        )
        assert resp.status_code == 404

    def test_status_404(self, authed_client):
        resp = authed_client.get("/api/session/nonexistent/status")
        assert resp.status_code == 404


class TestAuthRequired:
    def test_index_redirects_to_login(self, client):
        """Unauthenticated GET / should redirect to /login."""
        resp = client.get("/")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_api_returns_401(self, client):
        """Unauthenticated API call should get 401 JSON."""
        resp = client.post("/api/session")
        assert resp.status_code == 401

    def test_login_page_is_public(self, client):
        resp = client.get("/login")
        assert resp.status_code == 200


class TestBruteForce:
    def test_lockout_after_max_attempts(self, client, flask_app):
        """6 failed login attempts from same IP should trigger 429 lockout."""
        import app as app_module

        # Clear any existing state
        app_module._login_attempts.clear()
        app_module._login_lockouts.clear()

        for i in range(5):
            resp = client.post("/login", json={"key": "wrong-key"})
            # First 4 should be 403; 5th triggers lockout and returns 429
            assert resp.status_code in (
                403, 429), f"Attempt {i+1}: {resp.status_code}"

        # Next attempt while locked out
        resp = client.post("/login", json={"key": "wrong-key"})
        assert resp.status_code == 429
        assert "Retry" in resp.get_json()["error"]

        # Cleanup
        app_module._login_attempts.clear()
        app_module._login_lockouts.clear()

    def test_valid_login(self, client, flask_app):
        """Valid key should succeed and set cookie."""
        import app as app_module
        app_module._login_attempts.clear()
        app_module._login_lockouts.clear()

        resp = client.post("/login", json={"key": "test-admin-key-0000"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["user"] == "Test Admin"
