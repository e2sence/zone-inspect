"""Calibration engine on a synthetic board (no database)."""

import cv2
import numpy as np
import pytest

from tests.synth import make_board, shoot

pytestmark = pytest.mark.unit

ZONE = {"x": 0.10, "y": 0.15, "w": 0.35, "h": 0.40, "label": "Z1",
        "subzones": [{"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5, "label": "S1", "critical": True}]}


@pytest.fixture(scope="module")
def setup():
    import app as app_module
    if not app_module.NN_AVAILABLE:
        pytest.skip("neural engine not available")
    import calibration as C
    import nn_engine as nn
    board = make_board()
    sift = nn.reference_sift(board)
    version = {"zones": [ZONE], "ref_size": [board.shape[1], board.shape[0]], "ref_image": "r"}
    photos = {}

    def sample(i, kind, jpg, defect_box=None):
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), 1)
        H, _ = nn.estimate_homography(board, img, sift)
        assert H is not None
        photos[str(i)] = img
        s = {"id": str(i), "kind": kind, "aligned": True, "excluded": False,
             "align": {"H": H.tolist()}, "defects": []}
        if defect_box:
            s["defects"] = [{"zone": 0, "subzone": 0, "box": defect_box}]
        return s

    goods = [sample(i, "good", shoot(board, ZONE, quality=70 + i * 2, brightness=(i % 4) * 5))
             for i in range(8)]
    # shoot() draws the defect at 1/3 of the crop → inside S1 (top-left quarter)
    box = {"x": ZONE["x"] + 0.05, "y": ZONE["y"] + 0.06, "w": 0.08, "h": 0.1}
    defects = [sample(100 + i, "defect", shoot(board, ZONE, defect=True, quality=85 - i), box)
               for i in range(2)]
    calib, blobs = C.calibrate(version, goods + defects, lambda s: photos[s["id"]])
    return C, nn, board, sift, version, calib, blobs


def test_calibration_catches_defects_without_false_alarms(setup):
    C, *_, calib, blobs = setup
    z = calib["zones"][0]
    assert z["n_good"] == 8 and len(z["defects"]) == 2
    assert z["false_alarms"] == 0
    assert z["caught"] == 2 and all(d["in_box"] for d in z["defects"])
    assert z["subzones"][0]["caught"] == 2          # critical subzone sees it too
    assert calib["missed"] == 0
    assert 0 in blobs


def test_margin_changes_outcomes_without_features(setup):
    C, *_, calib, _ = setup
    import copy
    loose = C.apply_margin(copy.deepcopy(calib), 1.8)
    strict = C.apply_margin(copy.deepcopy(calib), 0.9)
    assert loose["zones"][0]["t_ok"] > strict["zones"][0]["t_ok"]
    assert strict["zones"][0]["false_alarms"] >= 1      # below the worst good sample


def test_zone_model_inspects_new_photos(setup):
    C, nn, board, sift, version, calib, blobs = setup
    model = C.ZoneModel(ZONE, version["ref_size"], calib["zones"][0], blobs[0])

    def run(jpg):
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), 1)
        H, _ = nn.estimate_homography(board, img, sift)
        return model.inspect(img, H)

    good = run(shoot(board, ZONE, quality=88, brightness=8))
    bad = run(shoot(board, ZONE, defect=True, quality=88))
    assert good["status"] == "ok", good["score"]
    assert bad["status"] in ("warn", "defect") and bad["score"] > good["score"]


def test_fingerprint_tracks_samples_and_geometry(setup):
    C, *_ = setup
    v = {"ref_image": "r", "zones": [ZONE]}
    s = [{"id": "1", "kind": "good", "aligned": True, "excluded": False, "defects": []}]
    fp = C.fingerprint(v, s)
    assert C.fingerprint(v, s) == fp
    assert C.fingerprint(v, [dict(s[0], excluded=True)]) != fp
    assert C.fingerprint({"ref_image": "r", "zones": [dict(ZONE, x=0.11)]}, s) != fp
