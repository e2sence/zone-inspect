"""Tests for external API v1 endpoints (require MongoDB)."""

import pytest


class TestApiV1Results:
    """GET /api/v1/results — list inspection results."""

    @pytest.fixture(autouse=True)
    def _require_mongo(self, mongo_available):
        if not mongo_available:
            pytest.skip("MongoDB not available")

    def test_list_results(self, authed_client):
        resp = authed_client.get("/api/v1/results")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "total" in data
        assert "results" in data
        assert isinstance(data["results"], list)

    def test_list_with_limit(self, authed_client):
        resp = authed_client.get("/api/v1/results?limit=5")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["limit"] == 5

    def test_list_with_date_filter(self, authed_client):
        resp = authed_client.get("/api/v1/results?from=2020-01-01&to=2099-12-31")
        assert resp.status_code == 200

    def test_list_with_serial_filter(self, authed_client):
        resp = authed_client.get("/api/v1/results?serial=TEST*")
        assert resp.status_code == 200


class TestApiV1GetResult:
    """GET /api/v1/results/<rid> — single result."""

    @pytest.fixture(autouse=True)
    def _require_mongo(self, mongo_available):
        if not mongo_available:
            pytest.skip("MongoDB not available")

    def test_not_found(self, authed_client):
        resp = authed_client.get("/api/v1/results/nonexistent_id")
        assert resp.status_code == 404

    def test_invalid_id_chars(self, authed_client):
        resp = authed_client.get("/api/v1/results/id%20with%20spaces")
        assert resp.status_code in (400, 404)


class TestApiV1ResultImage:
    """GET /api/v1/results/<rid>/images/<fname>"""

    @pytest.fixture(autouse=True)
    def _require_mongo(self, mongo_available):
        if not mongo_available:
            pytest.skip("MongoDB not available")

    def test_not_found(self, authed_client):
        resp = authed_client.get("/api/v1/results/test123/images/zone_0.jpg")
        assert resp.status_code == 404

    def test_invalid_fname(self, authed_client):
        resp = authed_client.get("/api/v1/results/test123/images/bad name.jpg")
        assert resp.status_code in (400, 404)
