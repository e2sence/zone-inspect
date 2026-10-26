"""Inspection pipeline: reproducibility and the per-session reference caches."""

import io

import cv2
import numpy as np
import pytest

pytestmark = pytest.mark.api

ZONES = [
    {"x": 0.05, "y": 0.10, "w": 0.35, "h": 0.40, "label": "Z0",
     "subzones": [{"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5},
                  {"x": 0.5, "y": 0.5, "w": 0.5, "h": 0.5}]},
    {"x": 0.60, "y": 0.50, "w": 0.35, "h": 0.40, "label": "Z1"},
]


@pytest.fixture(scope="module")
def board():
    """Textured synthetic 'PCB' — enough structure for SIFT to lock on."""
    rng = np.random.default_rng(7)
    img = (rng.random((900, 1200, 3)) * 255).astype(np.uint8)
    img = cv2.GaussianBlur(img, (7, 7), 0)
    for _ in range(60):
        x, y = rng.integers(0, 1100), rng.integers(0, 800)
        color = tuple(int(c) for c in rng.integers(0, 255, 3))
        cv2.rectangle(img, (x, y), (x + rng.integers(15, 90),
                                    y + rng.integers(15, 90)), color, -1)
    return img


def _shoot(board, zone, defect=False):
    """'Phone photo' of one zone: crop with margin, mild perspective, upscale."""
    h, w = board.shape[:2]
    x1 = int(max(0, (zone["x"] - 0.04) * w)); y1 = int(max(0, (zone["y"] - 0.04) * h))
    x2 = int(min(w, (zone["x"] + zone["w"] + 0.04) * w))
    y2 = int(min(h, (zone["y"] + zone["h"] + 0.04) * h))
    crop = board[y1:y2, x1:x2].copy()
    if defect:
        ch, cw = crop.shape[:2]
        cv2.circle(crop, (cw // 3, ch // 3), min(ch, cw) // 8, (0, 0, 255), -1)
    ch, cw = crop.shape[:2]
    src = np.float32([[0, 0], [cw, 0], [cw, ch], [0, ch]])
    dst = np.float32([[cw * .02, ch * .01], [cw * .99, 0], [cw, ch * .98], [0, ch]])
    crop = cv2.warpPerspective(crop, cv2.getPerspectiveTransform(src, dst), (cw, ch))
    crop = cv2.resize(crop, None, fx=1.4, fy=1.4)
    return cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes()


@pytest.fixture()
def nn_app(flask_app):
    import app as app_module
    if not app_module.NN_AVAILABLE:
        pytest.skip("neural engine not available")
    return app_module


def _session(client, board, zones=ZONES):
    ref = cv2.imencode(".jpg", board, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()
    sid = client.post("/api/session", data={"image": (io.BytesIO(ref), "ref.jpg")},
                      content_type="multipart/form-data").get_json()["session_id"]
    assert client.post(f"/api/session/{sid}/zones",
                       json={"zones": zones}).status_code == 200
    return sid


def _check(app_module, client, sid, photo):
    app_module.sessions[sid]["checked"] = {}
    r = client.post(f"/api/session/{sid}/check",
                    data={"photo": (io.BytesIO(photo), "p.jpg")},
                    content_type="multipart/form-data")
    assert r.status_code == 200
    d = r.get_json()
    df = d.get("defect") or {}
    return (d["best_zone_index"], d["matched"], round(d["best_score"], 4),
            df.get("status"), df.get("defect_pct"),
            [(x["status"], x["defect_pct"]) for x in df.get("subzones", [])])


# ─── Reproducibility ─────────────────────────────────────────────────────────

class TestDeterminism:
    def test_alignment_independent_of_rng_history(self, nn_app, board):
        import nn_engine as nn
        photo = cv2.imdecode(np.frombuffer(_shoot(board, ZONES[0]), np.uint8), 1)
        first = nn.align_photo_to_ref(board, photo)
        assert first is not None
        # Disturb OpenCV's RNG the way unrelated requests would
        rng = np.random.default_rng(1)
        junk = rng.random((800, 128)).astype(np.float32)
        nn.flann_knn(junk, junk[::-1].copy())
        cv2.FlannBasedMatcher(dict(algorithm=1, trees=5), {}).knnMatch(junk, junk, k=2)
        cv2.randu(np.empty((64, 64), np.uint8), 0, 255)
        again = nn.align_photo_to_ref(board, photo)
        assert np.array_equal(first, again)

    def test_verdicts_independent_of_photo_order(self, nn_app, authed_client, board):
        photos = [_shoot(board, ZONES[0], defect=True), _shoot(board, ZONES[1]),
                  _shoot(board, ZONES[0])]
        sid = _session(authed_client, board)
        forward = [_check(nn_app, authed_client, sid, p) for p in photos]
        sid = _session(authed_client, board)
        backward = [_check(nn_app, authed_client, sid, p) for p in reversed(photos)]
        assert forward == backward[::-1]
        assert forward[0][1] and forward[1][1], "synthetic photos should match"


# ─── Skipping the per-zone fallback ──────────────────────────────────────────

class TestFallback:
    def test_out_of_frame_zone_does_not_trigger_fallback(
            self, nn_app, authed_client, board, monkeypatch):
        calls = []
        orig = nn_app._locate_and_extract
        monkeypatch.setattr(nn_app, "_locate_and_extract",
                            lambda *a, **k: calls.append(1) or orig(*a, **k))
        sid = _session(authed_client, board)
        res = _check(nn_app, authed_client, sid, _shoot(board, ZONES[0]))
        assert res[0] == 0 and res[1]
        assert calls == []          # zone 1 is out of frame: not searched

    def test_fallback_still_used_when_alignment_fails(
            self, nn_app, authed_client, board, monkeypatch):
        calls = []
        orig = nn_app._locate_and_extract
        monkeypatch.setattr(nn_app, "align_photo_to_ref", lambda *a, **k: None)
        monkeypatch.setattr(nn_app, "_locate_and_extract",
                            lambda *a, **k: calls.append(1) or orig(*a, **k))
        sid = _session(authed_client, board)
        _check(nn_app, authed_client, sid, _shoot(board, ZONES[0]))
        assert len(calls) == len(ZONES)


# ─── Per-session reference caches ────────────────────────────────────────────

class TestReferenceCaches:
    def test_reference_sift_computed_once_per_session(
            self, nn_app, authed_client, board, monkeypatch):
        calls = []
        orig = nn_app.reference_sift
        monkeypatch.setattr(nn_app, "reference_sift",
                            lambda img: calls.append(1) or orig(img))
        sid = _session(authed_client, board)
        for z in (ZONES[0], ZONES[1], ZONES[0]):
            _check(nn_app, authed_client, sid, _shoot(board, z))
        assert len(calls) == 1

    def test_reference_cnn_features_cached_and_reset_on_zone_change(
            self, nn_app, authed_client, board, monkeypatch):
        computed = []
        orig = nn_app.reference_features
        monkeypatch.setattr(nn_app, "reference_features",
                            lambda imgs, pre=None: computed.append(len(imgs)) or orig(imgs, pre))
        sid = _session(authed_client, board)
        photo = _shoot(board, ZONES[0])
        _check(nn_app, authed_client, sid, photo)
        assert sum(computed) > 0
        before = sum(computed)
        _check(nn_app, authed_client, sid, photo)
        assert sum(computed) == before          # nothing recomputed
        moved = [dict(ZONES[0], x=0.06), ZONES[1]]
        authed_client.post(f"/api/session/{sid}/zones", json={"zones": moved})
        _check(nn_app, authed_client, sid, photo)
        assert sum(computed) > before           # zones changed → recomputed

    def test_cached_similarity_equals_uncached(self, nn_app, board):
        import nn_engine as nn
        a = board[100:400, 100:500]
        b = cv2.GaussianBlur(a, (3, 3), 0)
        plain = nn.similarity_nn_batch([(a, b)])[0]
        cached = nn.similarity_nn_cached(nn.reference_features([a]), [b])[0]
        assert abs(plain - cached) < 1e-3

    def test_cached_defect_analysis_equals_uncached(self, nn_app, board):
        import nn_engine as nn
        a = board[100:400, 100:500]
        b = a.copy()
        cv2.circle(b, (120, 120), 30, (0, 0, 255), -1)
        plain = nn.analyze_defects_nn(a, b, 0.7)
        zf = nn.reference_features([a], nn.defect_preprocess)[0]
        cached = nn.analyze_defects_nn(a, b, 0.7, zone_feats=zf)
        assert plain["status"] == cached["status"]
        assert abs(plain["defect_pct"] - cached["defect_pct"]) < 0.05
        assert abs(plain["ssim"] - cached["ssim"]) < 1e-6
