"""Role-based access control tests."""

import io

import pytest

pytestmark = pytest.mark.auth


class TestOperatorRestrictions:
    """Operators cannot save/delete/update templates (lead/admin only)."""

    @pytest.fixture()
    def operator_client(self, client):
        client.set_cookie("pcb_auth_key", "test-operator-key-2222")
        return client

    @pytest.fixture()
    def lead_client(self, client):
        client.set_cookie("pcb_auth_key", "test-lead-key-1111")
        return client

    def test_operator_cannot_save_template(self, operator_client,
                                           test_image_bytes, mongo_available):
        if not mongo_available:
            pytest.skip("MongoDB not available")
        # Create session first
        resp = operator_client.post(
            "/api/session",
            data={"image": (io.BytesIO(test_image_bytes), "board.jpg")},
            content_type="multipart/form-data",
        )
        sid = resp.get_json()["session_id"]
        operator_client.post(
            f"/api/session/{sid}/zones",
            json={"zones": [{"x": 0, "y": 0, "w": 1, "h": 1}]},
        )
        resp = operator_client.post(
            "/api/templates",
            json={"session_id": sid, "name": "Test Template"},
        )
        assert resp.status_code == 403

    def test_operator_cannot_delete_template(self, operator_client,
                                             mongo_available):
        if not mongo_available:
            pytest.skip("MongoDB not available")
        resp = operator_client.delete("/api/templates/fake_id")
        assert resp.status_code == 403

    def test_operator_cannot_update_template(self, operator_client,
                                             mongo_available):
        if not mongo_available:
            pytest.skip("MongoDB not available")
        resp = operator_client.put(
            "/api/templates/fake_id",
            json={"name": "New Name"},
        )
        assert resp.status_code == 403


class TestReadOnlyApiKey:
    """API keys with scope=read can only GET /api/v1/* routes."""

    @pytest.fixture()
    def api_client(self, flask_app):
        with flask_app.test_client() as c:
            yield c

    def test_read_key_cannot_post_session(self, api_client):
        resp = api_client.post(
            "/api/session",
            headers={"X-Auth-Key": "apk_test-read-key"},
        )
        assert resp.status_code == 403

    def test_read_key_cannot_post_zones(self, api_client):
        resp = api_client.post(
            "/api/session/fake/zones",
            headers={"X-Auth-Key": "apk_test-read-key"},
            json={"zones": []},
        )
        assert resp.status_code == 403

    def test_read_key_can_get_api_v1(self, api_client, mongo_available):
        if not mongo_available:
            pytest.skip("MongoDB not available")
        resp = api_client.get(
            "/api/v1/results",
            headers={"X-Auth-Key": "apk_test-read-key"},
        )
        # Should be 200 (or 503 if no mongo), not 403
        assert resp.status_code in (200, 503)


class TestPublicRoutes:
    """Public routes should not require auth."""

    @pytest.fixture()
    def anon_client(self, flask_app):
        with flask_app.test_client() as c:
            yield c

    def test_login_public(self, anon_client):
        assert anon_client.get("/login").status_code == 200

    def test_doc_public(self, anon_client):
        resp = anon_client.get("/doc")
        assert resp.status_code == 200

    def test_robots_public(self, anon_client):
        resp = anon_client.get("/robots.txt")
        assert resp.status_code == 200
        assert b"User-agent" in resp.data

    def test_sitemap_public(self, anon_client):
        resp = anon_client.get("/sitemap.xml")
        assert resp.status_code == 200
        assert b"urlset" in resp.data

    def test_mobile_page_public(self, anon_client):
        resp = anon_client.get("/mobile")
        assert resp.status_code == 200


class TestSecurityHeaders:
    """After-request security headers should be present."""

    def test_security_headers(self, authed_client):
        resp = authed_client.get("/")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert "no-store" in resp.headers.get("Cache-Control", "")
        assert resp.headers.get("X-XSS-Protection") == "1; mode=block"
