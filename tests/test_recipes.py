"""Recipe Studio: versions, samples, publish gates, recipe-owned strictness."""

import io

import cv2
import numpy as np
import pytest

import recipes as R
from tests.synth import make_board, shoot

ZONE = {"x": 0.10, "y": 0.15, "w": 0.35, "h": 0.40, "label": "Z1",
        "subzones": [{"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5,
                      "label": "S1", "critical": True}]}


# ─── Model (no database) ─────────────────────────────────────────────────────

class _FakeCol:
    def __init__(self):
        self.docs = []

    def find_one(self, q, proj=None):
        return next((d for d in self.docs
                     if all(d.get(k) == v for k, v in q.items())), None)

    def insert_many(self, docs):
        self.docs.extend(docs)


@pytest.mark.unit
class TestModel:
    def test_migrate_legacy_template(self):
        col = _FakeCol()
        doc = {"id": "t1", "name": "Old", "version": 3, "zones": [ZONE],
               "ref_image": "ref.jpg",
               "versions": [{"version": 1, "zones": []},
                            {"version": 2, "zones": [ZONE]}]}
        assert R.migrate_template(doc, col) == 3
        status = {d["version"]: d["status"] for d in col.docs}
        assert status == {1: "archived", 2: "archived", 3: "active"}
        assert R.migrate_template(doc, col) == 0          # idempotent

    def test_zone_visibility_and_locate_box(self):
        H = np.eye(3)  # photo == reference
        zones = [ZONE, {"x": 0.7, "y": 0.7, "w": 0.2, "h": 0.2}]
        # zone 1 spans 120..540 x 135..495 px; zone 2 starts at 840 px
        vis = R.zone_visibility(H, (700, 600), (1200, 900), zones)
        assert vis == [True, False]
        assert R.locate_box({"x": .12, "y": .17, "w": .02, "h": .02}, zones) == (0, 0)
        assert R.locate_box({"x": .40, "y": .50, "w": .01, "h": .01}, zones) == (0, None)
        assert R.locate_box({"x": .01, "y": .01, "w": .01, "h": .01}, zones) == (None, None)

    def test_publish_check_lists_what_is_missing(self):
        v = {"ref_image": "v1/ref.jpg", "ref_quality": {"level": "ok"},
             "zones": [ZONE]}
        summary = R.sample_summary(v, [])
        check = R.publish_check(v, summary)
        assert not check["ok"]
        text = " ".join(check["blockers"])
        assert "good samples" in text and "defect samples" in text
        assert "critical" in text and "phone" in text

    def test_photo_quality_flags_blur_and_darkness(self):
        board = make_board()
        assert R.photo_quality(board)["level"] == "ok"
        blurred = cv2.GaussianBlur(board, (0, 0), 12)
        assert R.photo_quality(blurred)["level"] == "bad"
        dark = (board * 0.12).astype(np.uint8)
        assert "Too dark" in R.photo_quality(dark)["issues"]


# ─── API (test MongoDB) ──────────────────────────────────────────────────────

pytestmark_api = [pytest.mark.mongo, pytest.mark.templates]


@pytest.fixture()
def mongo(mongo_available):
    if not mongo_available:
        pytest.skip("MongoDB not available")


@pytest.fixture()
def nn(flask_app):
    import app as app_module
    if not app_module.NN_AVAILABLE:
        pytest.skip("neural engine not available")


@pytest.fixture(scope="module")
def board():
    return make_board()


def _new_recipe(client, name="Board A"):
    r = client.post("/api/recipes", json={"name": name, "barcode_mask": "77"})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["recipe_id"]


def _upload_reference(client, tid, board):
    jpg = cv2.imencode(".jpg", board, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()
    r = client.post(f"/api/recipes/{tid}/draft/reference",
                    data={"image": (io.BytesIO(jpg), "ref.jpg")},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _add(client, tid, photo, kind, device, board_no):
    r = client.post(f"/api/recipes/{tid}/samples",
                    data={"photo": (io.BytesIO(photo), "s.jpg"), "kind": kind,
                          "device": device, "board": board_no},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _ready_recipe(client, board, zone_sens=None):
    """Recipe with reference, one zone, 15 good + 2 labelled defect samples."""
    tid = _new_recipe(client)
    _upload_reference(client, tid, board)
    upd = {"zones": [ZONE]}
    if zone_sens is not None:
        upd["zone_sensitivity"] = zone_sens
    assert client.put(f"/api/recipes/{tid}/draft", json=upd).status_code == 200
    for i in range(15):
        photo = shoot(board, ZONE, quality=80 + i, brightness=(i % 3) * 6)
        _add(client, tid, photo, "good", ("iPhone 13", "Xiaomi 14")[i % 2], str(i % 3 + 1))
    for i in range(2):
        out = _add(client, tid, shoot(board, ZONE, defect=True, quality=90 - i),
                   "defect", "Xiaomi 14", "9")
        sid = out["samples"][0]["id"]
        box = {"x": ZONE["x"] + 0.02, "y": ZONE["y"] + 0.02, "w": 0.05, "h": 0.05}
        r = client.patch(f"/api/samples/{sid}",
                         json={"defects": [{"box": box, "type": "wrong_part"}]})
        assert r.status_code == 200
    return tid


@pytest.mark.mongo
class TestRecipeLifecycle:
    def test_new_recipe_is_hidden_until_published(self, mongo, authed_client):
        tid = _new_recipe(authed_client, "Hidden")
        ids = [t["id"] for t in authed_client.get("/api/templates").get_json()["templates"]]
        assert tid not in ids
        d = authed_client.get(f"/api/recipes/{tid}/draft").get_json()
        assert d["version"] == 1 and d["status"] == "draft"

    def test_publish_blocked_until_samples(self, mongo, nn, authed_client, board):
        tid = _new_recipe(authed_client)
        q = _upload_reference(authed_client, tid, board)
        assert q["quality"]["level"] == "ok"
        authed_client.put(f"/api/recipes/{tid}/draft", json={"zones": [ZONE]})
        r = authed_client.post(f"/api/recipes/{tid}/publish")
        assert r.status_code == 409
        assert any("good samples" in b for b in r.get_json()["check"]["blockers"])

    def test_lead_cannot_force_admin_can(self, mongo, flask_app, authed_client, board):
        tid = _new_recipe(authed_client)
        _upload_reference(authed_client, tid, board)
        authed_client.put(f"/api/recipes/{tid}/draft", json={"zones": [ZONE]})
        with flask_app.test_client() as lead:
            lead.set_cookie("pcb_auth_key", "test-lead-key-1111")
            # admin-created recipe has no group → not visible to line1 lead
            assert lead.post(f"/api/recipes/{tid}/publish",
                             json={"force": True}).status_code in (404, 409)
        r = authed_client.post(f"/api/recipes/{tid}/publish", json={"force": True})
        assert r.status_code == 200

    def test_full_flow_publish_inspect_version(self, mongo, nn, authed_client, board):
        tid = _ready_recipe(authed_client, board)
        d = authed_client.get(f"/api/recipes/{tid}/draft").get_json()
        assert d["check"]["ok"], d["check"]
        assert d["summary"]["zones"][0]["good"] == 15
        assert d["summary"]["zones"][0]["defect"] == 2
        assert d["summary"]["zones"][0]["critical_subzones"][0]["defects"] == 2
        assert authed_client.post(f"/api/recipes/{tid}/publish").status_code == 200

        listed = {t["id"]: t for t in authed_client.get("/api/templates").get_json()["templates"]}
        assert listed[tid]["version"] == 1
        load = authed_client.get(f"/api/templates/{tid}").get_json()
        sid = load["session_id"]
        authed_client.post(f"/api/session/{sid}/check",
                           data={"photo": (io.BytesIO(shoot(board, ZONE)), "p.jpg")},
                           content_type="multipart/form-data")
        rid = authed_client.post(f"/api/session/{sid}/save_result",
                                 json={}).get_json()["result_id"]
        import app as app_module
        assert app_module._results_col.find_one({"result_id": rid})["recipe_version"] == 1

    def test_new_version_rollback_and_reference_per_version(
            self, mongo, nn, authed_client, board):
        tid = _ready_recipe(authed_client, board)
        authed_client.post(f"/api/recipes/{tid}/publish")
        r = authed_client.post(f"/api/recipes/{tid}/draft").get_json()
        assert r["version"] == 2
        # new reference for v2 → v1's file must stay untouched
        _upload_reference(authed_client, tid, cv2.flip(board, 1))
        import app as app_module
        from tests.conftest import FAKE_R2
        assert f"templates/{tid}/v1/ref.jpg" in FAKE_R2
        assert f"templates/{tid}/v2/ref.jpg" in FAKE_R2
        d = authed_client.get(f"/api/recipes/{tid}/draft").get_json()
        assert d["summary"]["stale"] == 17           # aligned to v1's reference
        authed_client.post(f"/api/recipes/{tid}/publish", json={"force": True})
        assert authed_client.get(f"/api/templates/{tid}").get_json()["version"] == 2
        assert authed_client.post(f"/api/recipes/{tid}/rollback/1").status_code == 200
        doc = app_module._templates_col.find_one({"id": tid})
        assert doc["active_version"] == 1 and doc["ref_image"] == "v1/ref.jpg"

    def test_samples_survive_zone_edit(self, mongo, nn, authed_client, board):
        tid = _ready_recipe(authed_client, board)
        moved = [dict(ZONE, x=ZONE["x"] + 0.01)]
        st = authed_client.put(f"/api/recipes/{tid}/draft",
                               json={"zones": moved}).get_json()
        assert st["summary"]["zones"][0]["good"] == 15

    def test_legacy_template_is_migrated_on_access(self, mongo, authed_client):
        import app as app_module
        app_module._templates_col.insert_one({
            "id": "legacy01", "name": "Legacy", "version": 2, "zones": [ZONE],
            "ref_image": "ref.jpg", "group": "",
            "versions": [{"version": 1, "zones": [ZONE], "ref_image": "ref.jpg"}]})
        r = authed_client.get("/api/recipes/legacy01").get_json()
        assert [(v["version"], v["status"]) for v in r["versions"]] == \
            [(2, "active"), (1, "archived")]


@pytest.mark.mongo
class TestRecipeStrictness:
    def test_operator_cannot_override_recipe_sensitivity(
            self, mongo, nn, flask_app, authed_client, board):
        tid = _ready_recipe(authed_client, board, zone_sens=1.9)
        # give the recipe to line1 so the operator can see it
        import app as app_module
        app_module._templates_col.update_one({"id": tid}, {"$set": {"group": "line1"}})
        authed_client.post(f"/api/recipes/{tid}/publish")
        photo = shoot(board, ZONE)
        with flask_app.test_client() as op:
            op.set_cookie("pcb_auth_key", "test-operator-key-2222")
            sid = op.get(f"/api/templates/{tid}").get_json()["session_id"]
            r = op.post(f"/api/session/{sid}/check",
                        data={"photo": (io.BytesIO(photo), "p.jpg"),
                              "zone_sensitivity": "0.0"},
                        content_type="multipart/form-data").get_json()
        assert r["defect"]["zone_sensitivity"] == 1.9
        sid = authed_client.get(f"/api/templates/{tid}").get_json()["session_id"]
        r = authed_client.post(f"/api/session/{sid}/check",
                               data={"photo": (io.BytesIO(photo), "p.jpg"),
                                     "zone_sensitivity": "0.3"},
                               content_type="multipart/form-data").get_json()
        assert r["defect"]["zone_sensitivity"] == 0.3


@pytest.mark.mongo
class TestPhoneSampleCapture:
    def test_sample_token_flow(self, mongo, nn, flask_app, authed_client, board):
        tid = _new_recipe(authed_client)
        _upload_reference(authed_client, tid, board)
        authed_client.put(f"/api/recipes/{tid}/draft", json={"zones": [ZONE]})
        token = authed_client.post(f"/api/recipes/{tid}/sample_qr").get_json()["token"]
        with flask_app.test_client() as phone:              # no auth cookie
            st = phone.get(f"/api/mobile/{token}/sample_status").get_json()
            assert st["has_reference"] and st["min_good"] == R.MIN_GOOD_PER_ZONE
            r = phone.post(f"/api/mobile/{token}/sample",
                           data={"photo": (io.BytesIO(shoot(board, ZONE)), "p.jpg"),
                                 "kind": "good", "device": "iPhone 14", "board": "1"},
                           content_type="multipart/form-data").get_json()
            assert r["sample"]["aligned"] and r["sample"]["visible_zones"] == [0]
            assert r["summary"]["zones"][0]["good"] == 1
            # an inspection token endpoint must not accept a sample token
            assert phone.get(f"/api/mobile/{token}/info").status_code == 404


@pytest.mark.mongo
class TestSharedRecipes:
    def test_recipe_without_group_is_usable_by_every_group(
            self, mongo, flask_app, authed_client):
        import app as app_module
        app_module._templates_col.insert_one({
            "id": "shared01", "name": "Shared", "version": 1, "zones": [ZONE],
            "ref_image": "ref.jpg"})            # legacy: no group field at all
        from tests.conftest import FAKE_R2
        FAKE_R2["templates/shared01/ref.jpg"] = cv2.imencode(".jpg", make_board())[1].tobytes()
        with flask_app.test_client() as op:
            op.set_cookie("pcb_auth_key", "test-operator-key-2222")   # line1
            ids = [t["id"] for t in op.get("/api/templates").get_json()["templates"]]
            assert "shared01" in ids
            assert op.get("/api/templates/shared01").status_code == 200
        with flask_app.test_client() as lead:
            lead.set_cookie("pcb_auth_key", "test-lead-key-1111")      # line1
            # editing a shared recipe is for admins
            assert lead.get("/api/recipes/shared01").status_code == 404
