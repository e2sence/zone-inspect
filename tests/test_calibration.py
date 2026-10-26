"""Calibration engine on a synthetic board (no database)."""

import cv2
import numpy as np
import pytest

from tests.synth import make_board, shoot

pytestmark = pytest.mark.unit

TICKS = []      # progress calls of the calibration in setup

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
    ticks = TICKS
    calib, blobs = C.calibrate(version, goods + defects, lambda s: photos[s["id"]],
                               lambda msg, frac=None: ticks.append((msg, frac)))
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


def test_progress_reported_per_photo(setup):
    assert TICKS
    fracs = [f for _, f in TICKS]
    assert len(fracs) >= 10 + 10 + 8 + 2          # load, features, good vs others, defects
    assert fracs == sorted(fracs) and 0 <= fracs[0] < 0.1 and fracs[-1] > 0.9
    assert all(msg.startswith("Z1: ") for msg, _ in TICKS)


def test_margin_changes_outcomes_without_features(setup):
    C, *_, calib, _ = setup
    import copy
    loose = C.apply_margin(copy.deepcopy(calib), 1.8)
    strict = C.apply_margin(copy.deepcopy(calib), 0.9)
    assert loose["zones"][0]["t_ok"] > strict["zones"][0]["t_ok"]
    assert strict["zones"][0]["false_alarms"] >= 1      # below the worst good sample


def test_zone_limit_and_missed_detail(setup):
    C, *_, calib, _ = setup
    import copy
    cal = copy.deepcopy(calib)
    z = cal["zones"][0]
    worst = max(z["good_scores"])
    for d in z["defects"]:          # synthetic defects are far too obvious for the limit range
        d["score"] = worst * 1.5
        d["sub_scores"] = [worst * 1.5 for _ in d["sub_scores"]]
    above = worst * 1.6
    cal = C.apply_margin(cal, zone_margins={0: above / worst})
    z = cal["zones"][0]
    assert abs(z["t_ok"] - above) < 1e-3 and z["caught"] == 0
    assert cal["missed"] == 2                       # the zone and its critical subzone
    assert cal["missed_detail"][0] == "Z1: 2 of 2 defect samples pass as OK"
    # a new default margin drops per-zone choices
    assert "margin" not in C.apply_margin(cal, 1.1)["zones"][0]


def _camera(C, target, wh):
    """Pinhole looking from C at target: (K, R, t) with x ~ K (R X + t)."""
    import calibration as Cm
    z = (target - C) / np.linalg.norm(target - C)
    x = np.cross([0, 1.0, 0], z); x /= np.linalg.norm(x)
    R = np.stack([x, np.cross(z, x), z])
    return Cm._K(wh), R, -R @ C


def _homography(cam_from, cam_to):
    """Homography between two views of the board plane Z = 0."""
    pts = np.array([[x, y, 0.0] for x in (-0.1, 0.1) for y in (-0.1, 0.1)])
    proj = lambda cam: np.array([(lambda p: p[:2] / p[2])(cam[0] @ (cam[1] @ X + cam[2])) for X in pts])
    H, _ = cv2.findHomography(proj(cam_from).astype(np.float32), proj(cam_to).astype(np.float32))
    return H


def test_view_deviation_with_angled_reference():
    """The reference is shot at 30°; deviation is measured between photos,
    whatever the reference angle."""
    import calibration as Cm
    ref_wh, ph_wh = (3024, 4032), (3000, 4000)
    origin = np.zeros(3)
    ref = _camera(np.array([0.0, -0.25, 0.43]), origin, ref_wh)       # ~30° off the board normal
    centres = [np.array([0.05, 0.0, 0.4]), np.array([-0.03, 0.04, 0.38]), np.array([0.25, 0.0, 0.3])]
    photos = [_camera(c, origin, ph_wh) for c in centres]
    poses = [Cm.view_poses(_homography(p, ref), ph_wh, ref_wh) for p in photos]
    normal = Cm.board_normal(poses)
    point = (lambda p: p[:2] / p[2])(ref[0] @ (ref[1] @ origin + ref[2]))     # board origin in the reference
    dirs = [Cm.view_direction(p, normal, ref_wh, point) for p in poses]
    for i, j in ((0, 1), (0, 2), (1, 2)):
        truth = Cm._angle(centres[i], centres[j])
        assert abs(Cm._angle(dirs[i], dirs[j]) - truth) < 1.0, (i, j, truth)


def test_view_hint_arrow_leads_to_the_typical_viewpoint():
    """Moving the phone the way the arrow points reduces the deviation."""
    import calibration as Cm
    ref_wh, ph_wh = (3024, 4032), (3000, 4000)
    origin = np.zeros(3)
    ref = _camera(np.array([0.0, -0.2, 0.45]), origin, ref_wh)            # reference at an angle
    goods = [np.array([dx, dy, 0.4]) for dx, dy in ((0, 0), (0.02, 0.01), (-0.01, 0.02), (0.01, -0.02))]
    poses = [Cm.view_poses(_homography(_camera(c, origin, ph_wh), ref), ph_wh, ref_wh) for c in goods]
    normal = Cm.board_normal(poses)
    point = (lambda p: p[:2] / p[2])(ref[0] @ (ref[1] @ origin + ref[2]))
    zone = {"x": point[0] / ref_wh[0] - 0.01, "y": point[1] / ref_wh[1] - 0.01, "w": 0.02, "h": 0.02}
    dirs = [Cm.view_direction(p, normal, ref_wh, Cm.zone_centre(zone, ref_wh)) for p in poses]
    typical = np.mean(dirs, 0); typical /= np.linalg.norm(typical)
    view = {"typical": typical.tolist(), "normal": normal.tolist()}

    C = np.array([0.18, 0.05, 0.35])                                     # off to the side
    for _ in range(6):
        cam = _camera(C, origin, ph_wh)
        dev, move = Cm.view_hint(_homography(cam, ref), ph_wh, ref_wh, zone, view)
        step = cam[1].T @ np.array([move[0], move[1], 0.0])               # image direction → world
        C2 = C + 0.03 * step
        dev2, _ = Cm.view_hint(_homography(_camera(C2, origin, ph_wh), ref), ph_wh, ref_wh, zone, view)
        assert dev2 < dev, (dev, dev2)
        C = C2


def test_suspect_samples():
    import calibration as C
    goods = [{"id": str(i)} for i in range(10)]
    scores = [1.0, 1.05, 0.95, 1.1, 1.02, 0.98, 1.01, 1.04, 0.97, 2.4]
    dev = {str(i): 5.0 for i in range(10)}
    dev["3"] = dev["2"] = 30.0             # "2" scores below typical: its angle does no harm
    dev["d1"], dev["d2"] = 35.0, 4.0
    sus = C._suspects(goods, scores, dev, [({"id": "d1"}, 2.0), ({"id": "d2"}, 2.5)])
    by_id = {s["sample_id"]: s for s in sus}
    assert set(by_id) == {"3", "9", "d1"}
    assert "30° away" in by_id["3"]["reasons"][0] and "far above" in by_id["9"]["reasons"][0]
    assert by_id["d1"]["kind"] == "defect" and sus[-1]["sample_id"] == "d1"


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
