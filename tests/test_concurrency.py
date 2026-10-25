"""Multi-station safety: several operators / lines using the server at once."""

import io
import threading

import pytest


def _new_session(client, image_bytes):
    resp = client.post(
        "/api/session",
        data={"image": (io.BytesIO(image_bytes), "board.jpg")},
        content_type="multipart/form-data",
    )
    sid = resp.get_json()["session_id"]
    client.post(f"/api/session/{sid}/zones", json={"zones": [
        {"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5, "label": "IC1"},
    ]})
    return sid


def _qr(client, sid, prev_token=None):
    body = {"prev_token": prev_token} if prev_token else None
    resp = client.post(f"/api/session/{sid}/mobile_qr", json=body)
    assert resp.status_code == 200
    return resp.get_json()["token"]


# ─── Sensitivity is per request ──────────────────────────────────────────────

@pytest.mark.unit
class TestThresholds:
    def test_thresholds_do_not_touch_module(self):
        import inspection_config as cfg
        before = cfg.PATCH_SIM_THRESHOLD
        strict = cfg.thresholds(2.0, 2.0)
        soft = cfg.thresholds(0.0, 0.0)
        assert cfg.PATCH_SIM_THRESHOLD == before
        assert strict.PATCH_SIM_THRESHOLD > soft.PATCH_SIM_THRESHOLD
        assert strict.SUBZONE_VERDICT_OK_THRESHOLD < soft.SUBZONE_VERDICT_OK_THRESHOLD

    def test_defaults_match_module(self):
        import inspection_config as cfg
        t = cfg.thresholds()
        assert t.VERDICT_OK_THRESHOLD == cfg.VERDICT_OK_THRESHOLD
        assert t.SUBZONE_SENSITIVITY == cfg.SUBZONE_SENSITIVITY

    def test_concurrent_thresholds_isolated(self):
        """Threads with different sensitivities never see each other's values."""
        import inspection_config as cfg
        expected = {s: cfg.thresholds(s).VERDICT_OK_THRESHOLD
                    for s in (0.0, 1.0, 2.0)}
        errors = []

        def worker(sens):
            for _ in range(2000):
                if cfg.thresholds(sens).VERDICT_OK_THRESHOLD != expected[sens]:
                    errors.append(sens)
                    return

        threads = [threading.Thread(target=worker, args=(s,))
                   for s in expected for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors

    def test_check_does_not_change_global_sensitivity(
            self, authed_client, session_with_zones, test_image_bytes):
        import inspection_config as cfg
        sid, _ = session_with_zones
        before = (cfg.ZONE_SENSITIVITY, cfg.SUBZONE_SENSITIVITY)
        authed_client.post(
            f"/api/session/{sid}/check",
            data={"photo": (io.BytesIO(test_image_bytes), "p.jpg"),
                  "zone_sensitivity": "1.9", "subzone_sensitivity": "1.9"},
            content_type="multipart/form-data",
        )
        assert (cfg.ZONE_SENSITIVITY, cfg.SUBZONE_SENSITIVITY) == before


# ─── Mobile tokens belong to one station ─────────────────────────────────────

@pytest.mark.mobile
class TestMobileTokenIsolation:
    def test_other_station_qr_does_not_steal_phone(
            self, flask_app, test_image_bytes):
        import app as app_module
        with flask_app.test_client() as a, flask_app.test_client() as b:
            a.set_cookie("pcb_auth_key", "test-operator-key-2222")
            b.set_cookie("pcb_auth_key", "test-lead-key-1111")
            sid_a = _new_session(a, test_image_bytes)
            tok_a = _qr(a, sid_a)
            sid_b = _new_session(b, test_image_bytes)
            tok_b = _qr(b, sid_b)
            assert tok_a != tok_b
            assert app_module._get_token_session(tok_a) == sid_a
            assert app_module._get_token_session(tok_b) == sid_b

    def test_station_keeps_token_across_sessions(
            self, authed_client, test_image_bytes):
        import app as app_module
        sid1 = _new_session(authed_client, test_image_bytes)
        tok = _qr(authed_client, sid1)
        sid2 = _new_session(authed_client, test_image_bytes)
        assert _qr(authed_client, sid2, prev_token=tok) == tok
        assert app_module._get_token_session(tok) == sid2

    def test_cannot_take_live_token_of_other_operator(
            self, flask_app, test_image_bytes):
        import app as app_module
        with flask_app.test_client() as a, flask_app.test_client() as b:
            a.set_cookie("pcb_auth_key", "test-operator-key-2222")
            b.set_cookie("pcb_auth_key", "test-lead-key-1111")
            sid_a = _new_session(a, test_image_bytes)
            tok_a = _qr(a, sid_a)
            sid_b = _new_session(b, test_image_bytes)
            tok_b = _qr(b, sid_b, prev_token=tok_a)
            assert tok_b != tok_a
            assert app_module._get_token_session(tok_a) == sid_a

    def test_many_stations_do_not_evict_each_other(
            self, authed_client, test_image_bytes):
        import app as app_module
        pairs = []
        for _ in range(12):
            sid = _new_session(authed_client, test_image_bytes)
            pairs.append((sid, _qr(authed_client, sid)))
        for sid, tok in pairs:
            assert app_module._get_token_session(tok) == sid

    def test_tokens_survive_restart_file(self, authed_client, test_image_bytes):
        import app as app_module
        sid = _new_session(authed_client, test_image_bytes)
        tok = _qr(authed_client, sid)
        reloaded = app_module._read_mobile_tokens_file()
        assert reloaded[tok]["sid"] == sid


# ─── Saved results never overwrite each other ────────────────────────────────

@pytest.mark.api
class TestResultIds:
    def test_two_saves_same_second_are_distinct(
            self, authed_client, session_with_zones):
        import app as app_module
        from tests.conftest import FAKE_R2
        sid, _ = session_with_zones
        rids = set()
        for _ in range(3):
            app_module.sessions[sid]["checked"][0] = {
                "score": 0.9, "defect_info": {"status": "ok"}}
            resp = authed_client.post(f"/api/session/{sid}/save_result", json={})
            assert resp.status_code == 200
            rids.add(resp.get_json()["result_id"])
        assert len(rids) == 3
        for rid in rids:
            if app_module.MONGO_AVAILABLE:
                assert app_module._results_col.find_one({"result_id": rid})
            else:
                assert f"results/{rid}/meta.json" in FAKE_R2

    def test_daily_log_keeps_every_line(self, authed_client, session_with_zones):
        import app as app_module
        from tests.conftest import FAKE_R2
        for k in [k for k in FAKE_R2 if k.startswith("logs/")]:
            del FAKE_R2[k]
        sid, _ = session_with_zones
        for _ in range(5):
            app_module.sessions[sid]["checked"][0] = {
                "score": 0.9, "defect_info": {"status": "ok"}}
            authed_client.post(f"/api/session/{sid}/save_result", json={})
        lines = b"".join(v for k, v in FAKE_R2.items()
                         if k.startswith("logs/")).splitlines()
        assert len(lines) == 5


# ─── Login lockout uses the real client IP ───────────────────────────────────

@pytest.mark.auth
class TestClientIp:
    def test_lockout_is_per_client_not_per_proxy(self, client):
        import app as app_module
        app_module._login_attempts.clear()
        app_module._login_lockouts.clear()
        for _ in range(app_module._LOGIN_MAX_ATTEMPTS):
            client.post("/login", json={"key": "wrong"},
                        headers={"X-Real-IP": "203.0.113.1"})
        locked = client.post("/login", json={"key": "wrong"},
                             headers={"X-Real-IP": "203.0.113.1"})
        other = client.post("/login", json={"key": "test-operator-key-2222"},
                            headers={"X-Real-IP": "203.0.113.2"})
        assert locked.status_code == 429
        assert other.status_code == 200
        app_module._login_attempts.clear()
        app_module._login_lockouts.clear()


# ─── Admin-only diagnostics ──────────────────────────────────────────────────

@pytest.mark.auth
class TestAdminEndpoints:
    def test_r2_status_requires_admin(self, client):
        client.set_cookie("pcb_auth_key", "test-operator-key-2222")
        assert client.get("/api/r2/status").status_code == 403

    def test_migrations_removed(self, authed_client):
        assert authed_client.post("/api/r2/migrate").status_code in (404, 405)
        assert authed_client.post(
            "/api/admin/migrate-templates").status_code in (404, 405)


@pytest.mark.api
class TestHealthz:
    def test_healthz_public(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.get_json()["mongo"] in ("ok", "not configured")


@pytest.mark.mongo
class TestUpdateResult:
    def _save(self, client, sid):
        import app as app_module
        app_module.sessions[sid]["checked"][0] = {
            "score": 0.9, "defect_info": {"status": "ok"}}
        return client.post(f"/api/session/{sid}/save_result",
                           json={}).get_json()["result_id"]

    def test_update_is_logged(self, authed_client, session_with_zones,
                              mongo_available):
        if not mongo_available:
            pytest.skip("MongoDB not available")
        import app as app_module
        sid, _ = session_with_zones
        rid = self._save(authed_client, sid)
        resp = authed_client.post(f"/api/session/{sid}/update_result", json={
            "result_id": rid, "user_decisions": {"0": "defect"}})
        assert resp.get_json()["overall"] == "defect"
        doc = app_module._results_col.find_one({"result_id": rid})
        assert doc["overall_status"] == "defect"
        assert doc["decision_log"][0]["previous_overall"] == "ok"
        assert doc["decision_log"][0]["by"] == "Test Admin"

    def test_operator_cannot_update_other_group(
            self, authed_client, flask_app, session_with_zones,
            mongo_available):
        if not mongo_available:
            pytest.skip("MongoDB not available")
        sid, _ = session_with_zones
        rid = self._save(authed_client, sid)  # admin → group ""
        with flask_app.test_client() as op:
            op.set_cookie("pcb_auth_key", "test-operator-key-2222")  # line1
            resp = op.post(f"/api/session/{sid}/update_result", json={
                "result_id": rid, "user_decisions": {"0": "ok"}})
        assert resp.status_code == 404
