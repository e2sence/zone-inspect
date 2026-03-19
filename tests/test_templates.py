"""Template CRUD tests (require MongoDB)."""

import io

import pytest


@pytest.fixture(autouse=True)
def _require_mongo(mongo_available):
    if not mongo_available:
        pytest.skip("MongoDB not available")


class TestListTemplates:
    def test_list_empty(self, authed_client):
        resp = authed_client.get("/api/templates")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "templates" in data
        assert isinstance(data["templates"], list)


class TestSaveTemplate:
    def test_save_requires_session_and_name(self, authed_client):
        resp = authed_client.post("/api/templates", json={})
        assert resp.status_code == 400

    def test_save_empty_name(self, authed_client, session_with_zones):
        sid, _ = session_with_zones
        resp = authed_client.post(
            "/api/templates",
            json={"session_id": sid, "name": ""},
        )
        assert resp.status_code == 400

    def test_save_session_not_found(self, authed_client):
        resp = authed_client.post(
            "/api/templates",
            json={"session_id": "nonexistent", "name": "Test"},
        )
        assert resp.status_code == 404


class TestDeleteTemplate:
    def test_delete_not_found(self, authed_client):
        resp = authed_client.delete("/api/templates/nonexistent_id")
        assert resp.status_code == 404


class TestUpdateTemplate:
    def test_update_not_found(self, authed_client):
        resp = authed_client.put(
            "/api/templates/nonexistent_id",
            json={"name": "Updated"},
        )
        assert resp.status_code == 404
