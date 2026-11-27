"""Verdict by serial number: /api/v1/verdict and the /r/<rid> result page."""

import pytest

READ_KEY = {"X-Auth-Key": "apk_test-read-key"}       # group line1


def _zone(label="Z1", status="ok", **extra):
    return dict({"label": label, "status": status}, **extra)


def _record(zones, overall="ok", checked=None, **extra):
    rec = {"zones": zones, "overall_status": overall,
           "zones_total": len(zones),
           "zones_checked": len(zones) if checked is None else checked}
    rec.update(extra)
    return rec


@pytest.mark.unit
class TestVerdictFromRecord:
    @pytest.fixture(autouse=True)
    def _fn(self, flask_app):
        import app as app_module
        self.verdict = app_module._verdict_from_record

    def test_all_ok_passes(self):
        v = self.verdict(_record([_zone(), _zone("Z2")]))
        assert (v["verdict"], v["reason"], v["problem_zones"]) == ("pass", "ok", [])

    def test_defect_fails(self):
        v = self.verdict(_record([_zone(), _zone("Z2", "defect")], "defect"))
        assert (v["verdict"], v["reason"]) == ("fail", "defect")
        assert v["problem_zones"] == [{"label": "Z2", "status": "defect"}]

    def test_warn_fails(self):
        v = self.verdict(_record([_zone("Z1", "warn")], "warn"))
        assert (v["verdict"], v["reason"]) == ("fail", "warn")

    def test_operator_accepts_a_calculated_defect(self):
        v = self.verdict(_record([_zone("Z1", "defect", user_decision="ok")]))
        assert v["verdict"] == "pass"

    def test_operator_rejects_a_calculated_ok(self):
        v = self.verdict(_record([_zone("Z1", "ok", user_decision="defect")],
                                 "defect"))
        assert (v["verdict"], v["reason"]) == ("fail", "defect")

    def test_subzone_defect_fails(self):
        z = _zone("Z1", "ok", subzones=[{"status": "ok"}, {"status": "defect"}])
        assert self.verdict(_record([z], "defect"))["reason"] == "defect"

    def test_subzone_decision_overrides(self):
        z = _zone("Z1", "defect",
                  subzones=[{"status": "defect", "user_decision": "ok"}])
        assert self.verdict(_record([z]))["verdict"] == "pass"

    def test_unchecked_zone_is_incomplete(self):
        v = self.verdict(_record([_zone(), _zone("Z2", "unchecked")], checked=1))
        assert (v["verdict"], v["reason"]) == ("fail", "incomplete")

    def test_fewer_checked_than_total_is_incomplete(self):
        rec = _record([_zone()], zones_total=3, zones_checked=1)
        assert self.verdict(rec)["reason"] == "incomplete"

    def test_no_zones_is_incomplete(self):
        assert self.verdict(_record([]))["verdict"] == "fail"

    def test_defect_outranks_incomplete(self):
        v = self.verdict(_record([_zone("Z1", "defect"),
                                  _zone("Z2", "unchecked")], "defect", checked=1))
        assert v["reason"] == "defect"

    def test_stored_overall_defect_is_never_a_pass(self):
        assert self.verdict(_record([_zone()], "defect"))["verdict"] == "fail"

    def test_unknown_status_is_never_a_pass(self):
        assert self.verdict(_record([_zone("Z1", "weird")]))["verdict"] == "fail"
        assert self.verdict(_record([_zone()], overall=None))["verdict"] == "fail"


@pytest.mark.api_v1
@pytest.mark.mongo
class TestVerdictApi:
    @pytest.fixture(autouse=True)
    def _data(self, mongo_available):
        if not mongo_available:
            pytest.skip("MongoDB not available")
        import app as app_module
        self.col = app_module._results_col
        self.col.delete_many({"serial": {"$regex": "^VT-"}})
        yield
        self.col.delete_many({"serial": {"$regex": "^VT-"}})

    def _add(self, rid, serial, ts, zones=None, overall="ok", group="line1",
             template="Board-X", **extra):
        zones = [_zone()] if zones is None else zones
        doc = _record(zones, overall, result_id=rid, serial=serial,
                      timestamp=ts, group=group, template_name=template,
                      recipe_version=3, operator="op1")
        doc.update(extra)
        self.col.insert_one(doc)

    def _get(self, client, query, headers=READ_KEY):
        resp = client.get("/api/v1/verdict?" + query, headers=headers)
        return resp.status_code, resp.get_json()

    def test_needs_a_key(self, client):
        assert self._get(client, "serial=VT-1", headers={})[0] == 401

    def test_serial_is_required(self, client):
        assert self._get(client, "serial=")[0] == 400

    def test_not_inspected(self, client):
        code, data = self._get(client, "serial=VT-NONE")
        assert code == 200
        assert (data["verdict"], data["inspections"]) == ("not_inspected", 0)

    def test_pass_with_link(self, client):
        self._add("vt_pass_1", "VT-100", "2026-11-26T10:00:00.000000")
        code, data = self._get(client, "serial=VT-100")
        assert code == 200
        assert (data["verdict"], data["reason"]) == ("pass", "ok")
        assert data["url"].endswith("/r/vt_pass_1")
        assert (data["template"], data["operator"]) == ("Board-X", "op1")

    def test_latest_inspection_counts(self, client):
        self._add("vt_old", "VT-200", "2026-11-25T10:00:00.000000",
                  [_zone("Z1", "defect")], "defect")
        self._add("vt_new", "VT-200", "2026-11-26T10:00:00.000000")
        data = self._get(client, "serial=VT-200")[1]
        assert (data["verdict"], data["result_id"], data["inspections"]) == (
            "pass", "vt_new", 2)

    def test_latest_defect_fails_after_an_earlier_pass(self, client):
        self._add("vt_a", "VT-210", "2026-11-25T10:00:00.000000")
        self._add("vt_b", "VT-210", "2026-11-26T10:00:00.000000",
                  [_zone("IC U5", "defect")], "defect")
        data = self._get(client, "serial=VT-210")[1]
        assert (data["verdict"], data["reason"]) == ("fail", "defect")
        assert data["problem_zones"] == [{"label": "IC U5", "status": "defect"}]

    def test_match_is_exact_not_substring(self, client):
        self._add("vt_long", "VT-3000", "2026-11-26T10:00:00.000000")
        assert self._get(client, "serial=VT-300")[1]["verdict"] == "not_inspected"
        assert self._get(client, "serial=T-3000")[1]["verdict"] == "not_inspected"
        assert self._get(client, "serial=VT-30.0")[1]["verdict"] == "not_inspected"

    def test_match_ignores_case_and_spaces(self, client):
        self._add("vt_case", "VT-AbC", "2026-11-26T10:00:00.000000")
        assert self._get(client, "serial=%20vt-abc%20")[1]["verdict"] == "pass"

    def test_other_group_is_invisible_to_a_group_key(self, client):
        self._add("vt_g2", "VT-400", "2026-11-26T10:00:00.000000", group="line2")
        assert self._get(client, "serial=VT-400")[1]["verdict"] == "not_inspected"
        admin = {"X-Auth-Key": "test-admin-key-0000"}
        assert self._get(client, "serial=VT-400", headers=admin)[1][
            "verdict"] == "pass"

    def test_template_filter(self, client):
        self._add("vt_t", "VT-500", "2026-11-26T10:00:00.000000",
                  template="Board-Y")
        assert self._get(client, "serial=VT-500&template=Board-X")[1][
            "verdict"] == "not_inspected"
        assert self._get(client, "serial=VT-500&template=board-y")[1][
            "verdict"] == "pass"

    def test_max_age(self, client):
        self._add("vt_stale", "VT-600", "2020-01-01T10:00:00.000000")
        assert self._get(client, "serial=VT-600&max_age_hours=24")[1][
            "verdict"] == "not_inspected"
        assert self._get(client, "serial=VT-600")[1]["verdict"] == "pass"
        assert self._get(client, "serial=VT-600&max_age_hours=0")[0] == 400

    def test_unrecognized_serial_never_matches(self, client):
        self._add("vt_unrec", "unrecognized", "2026-11-26T10:00:00.000000")
        try:
            assert self._get(client, "serial=unrecognized")[1][
                "verdict"] == "not_inspected"
        finally:
            self.col.delete_many({"result_id": "vt_unrec"})

    def test_result_page(self, authed_client):
        self._add("vt_page", "VT-700", "2026-11-26T10:00:00.000000",
                  [_zone("IC U5", "defect")], "defect")
        resp = authed_client.get("/r/vt_page")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "VT-700" in html and "IC U5" in html and "Fail" in html
        assert authed_client.get("/r/nope").status_code == 404

    def test_result_page_login_returns_to_the_result(self, client):
        resp = client.get("/r/vt_page")
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/login?next=/r/vt_page")

    def test_result_page_respects_group(self, client):
        self._add("vt_page_g2", "VT-800", "2026-11-26T10:00:00.000000",
                  group="line2")
        client.set_cookie("pcb_auth_key", "test-operator-key-2222")
        assert client.get("/r/vt_page_g2").status_code == 404


@pytest.mark.auth
@pytest.mark.mongo
class TestResultImagesRespectGroup:
    """Result images are addressed by result id alone; a key of another
    group must not get them."""

    ROUTES = ("/api/results/{}/image/zone_0.jpg",
              "/api/v1/results/{}/images/zone_0.jpg")

    @pytest.fixture(autouse=True)
    def _data(self, mongo_available):
        if not mongo_available:
            pytest.skip("MongoDB not available")
        import app as app_module
        from tests.conftest import FAKE_R2
        col = app_module._results_col
        rids = {"vt_img_l1": "line1", "vt_img_l2": "line2", "vt_img_none": ""}
        for rid, group in rids.items():
            col.insert_one({"result_id": rid, "group": group, "serial": "VT-IMG",
                            "timestamp": "2026-11-26T10:00:00.000000"})
            FAKE_R2[f"results/{rid}/zone_0.jpg"] = b"\xff\xd8jpeg"
        yield
        col.delete_many({"result_id": {"$in": list(rids)}})
        for rid in rids:
            FAKE_R2.pop(f"results/{rid}/zone_0.jpg", None)

    def _codes(self, client, rid, key):
        return [client.get(route.format(rid), headers={"X-Auth-Key": key}).status_code
                for route in self.ROUTES]

    def test_operator_gets_own_group_only(self, client):
        key = "test-operator-key-2222"                      # line1
        assert self._codes(client, "vt_img_l1", key) == [200, 200]
        assert self._codes(client, "vt_img_l2", key) == [404, 404]
        assert self._codes(client, "vt_img_none", key) == [404, 404]

    def test_group_api_key_gets_own_group_only(self, client):
        key = "apk_test-read-key"                           # line1, /api/v1 only
        assert self._codes(client, "vt_img_l1", key) == [403, 200]
        assert self._codes(client, "vt_img_l2", key) == [403, 404]

    def test_admin_gets_everything(self, client):
        key = "test-admin-key-0000"
        for rid in ("vt_img_l1", "vt_img_l2", "vt_img_none"):
            assert self._codes(client, rid, key) == [200, 200]

    def test_missing_result_is_404(self, client):
        assert self._codes(client, "vt_img_absent", "test-admin-key-0000") == [404, 404]
