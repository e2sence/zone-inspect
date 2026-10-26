"""
Calibration: a model of normal variation per zone, built from good samples
(PatchCore-style nearest-neighbour patch memory), with thresholds set from
leave-one-out scores of the good samples and checked against defect samples.

Per zone:
  1. Every photo is warped onto the reference with its homography and the
     zone is cut out at reference resolution (zone_crop).
  2. EfficientNet-B4 features of blocks 3 (stride 8) and 5 (stride 16),
     3×3-averaged and concatenated → a grid of patch descriptors.
  3. Anomaly map: for each cell, distance to the nearest descriptor of any
     good sample within ±NEIGHBOURHOOD cells (tolerates small misalignment).
  4. The map is divided by the typical deviation of that cell among good
     samples (median leave-one-out map), cells outside the photo and a thin
     border are ignored; the zone score is the maximum, a subzone score is
     the maximum inside the subzone.
  5. t_ok = worst good leave-one-out score × margin; WARN up to
     t_ok × DEFECT_BAND, DEFECT above. A defect sample is caught when its
     score exceeds t_ok.

Validated offline on the first real recipe (phase B): 7/7 defects caught and
localized, 0/5 false alarms on real inspections (old algorithm: 2/5).
"""

import hashlib
import io
import json

import cv2
import numpy as np
import torch
import torch.nn.functional as F

import nn_engine as nn

MODEL = "patch-v1"
IN_H = 480                 # zone crops are resized to this height (aspect kept)
NEIGHBOURHOOD = 2          # cells; 1 cell = 8 px at input scale
BORDER = 3                 # cells ignored along the zone edge
COVERAGE_MIN = 0.97        # a cell counts only if the photo covers it
ZONE_COVERAGE_MIN = 0.90   # share of the zone that must be in the photo
MARGIN_DEFAULT = 1.10
MARGIN_RANGE = (0.90, 1.80)
DEFECT_BAND = 1.25         # DEFECT above t_ok × DEFECT_BAND
MIN_GOOD_TO_CALIBRATE = 5


# ─── Geometry ────────────────────────────────────────────────────────────────

class ZoneGeom:
    """Zone rectangle in reference pixels + network input size."""

    def __init__(self, zone: dict, ref_wh):
        rw, rh = ref_wh
        self.x, self.y = int(zone["x"] * rw), int(zone["y"] * rh)
        self.w, self.h = max(8, int(zone["w"] * rw)), max(8, int(zone["h"] * rh))
        self.T = np.array([[1, 0, -self.x], [0, 1, -self.y], [0, 0, 1]], np.float64)
        self.in_h = IN_H
        self.in_w = max(64, int(round(IN_H * self.w / self.h / 32)) * 32)
        self.grid = (self.in_h // 8, self.in_w // 8)
        self.subzones = zone.get("subzones", []) or []

    def crop(self, photo, H):
        return cv2.warpPerspective(photo, self.T @ np.asarray(H, np.float64), (self.w, self.h))

    def coverage(self, photo_shape, H):
        """Per-cell share of the zone covered by the photo → (bool grid, zone share)."""
        ones = np.full(photo_shape[:2], 255, np.uint8)
        cov = cv2.warpPerspective(ones, self.T @ np.asarray(H, np.float64), (self.w, self.h))
        share = float((cov > 127).mean())
        small = cv2.resize(cov, (self.in_w, self.in_h), interpolation=cv2.INTER_AREA)
        cells = cv2.resize(small.astype(np.float32) / 255, (self.grid[1], self.grid[0]),
                           interpolation=cv2.INTER_AREA)
        return cells >= COVERAGE_MIN, share

    def valid_mask(self, cov_cells):
        m = cov_cells.copy()
        m[:BORDER, :] = m[-BORDER:, :] = False
        m[:, :BORDER] = m[:, -BORDER:] = False
        return m

    def subzone_slices(self):
        gh, gw = self.grid
        out = []
        for sz in self.subzones:
            y1, y2 = int(sz["y"] * gh), int(np.ceil((sz["y"] + sz["h"]) * gh))
            x1, x2 = int(sz["x"] * gw), int(np.ceil((sz["x"] + sz["w"]) * gw))
            out.append((slice(max(0, y1), max(y1 + 1, y2)), slice(max(0, x1), max(x1 + 1, x2))))
        return out


# ─── Features and anomaly maps ───────────────────────────────────────────────

@torch.no_grad()
def features(crop_bgr, geom: ZoneGeom) -> torch.Tensor:
    """[C, gh, gw] patch descriptors of a zone crop."""
    m = nn._model()
    rgb = cv2.cvtColor(cv2.resize(crop_bgr, (geom.in_w, geom.in_h), interpolation=cv2.INTER_AREA),
                       cv2.COLOR_BGR2RGB)
    x = nn._transform(rgb).unsqueeze(0).to(nn.DEVICE)
    out = {}
    for i, block in enumerate(m.blocks):
        x = block(x)
        if i in (3, 5):
            out[i] = x
        if i == 5:
            break
    f3 = F.avg_pool2d(out[3], 3, 1, 1)
    f5 = F.interpolate(F.avg_pool2d(out[5], 3, 1, 1), size=f3.shape[-2:],
                       mode="bilinear", align_corners=False)
    return torch.cat([f3, f5], 1)[0].float().cpu()


@torch.no_grad()
def anomaly_map(f: torch.Tensor, bank: list) -> np.ndarray:
    """Distance of each cell to the nearest good descriptor in its neighbourhood."""
    C, h, w = f.shape
    k = 2 * NEIGHBOURHOOD + 1
    best = torch.full((h, w), float("inf"))
    for b in bank:
        nb = F.unfold(b.unsqueeze(0), k, padding=NEIGHBOURHOOD).view(C, k * k, h, w)
        d = ((nb - f.unsqueeze(1)) ** 2).sum(0).sqrt().min(0).values
        best = torch.minimum(best, d)
    return cv2.GaussianBlur(best.numpy(), (0, 0), 1.0)


def normaliser(loo_maps: list) -> np.ndarray:
    med = np.median(np.stack(loo_maps), 0).astype(np.float32)
    return med + float(med.mean()) * 0.25


def scores(raw_map, norm, valid, geom: ZoneGeom):
    """(zone score, [subzone scores], normalized map with invalid cells = 0)."""
    m = (raw_map / norm) * valid
    zone = float(m.max()) if valid.any() else 0.0
    subs = []
    for ys, xs in geom.subzone_slices():
        part = m[ys, xs]
        subs.append(float(part.max()) if part.size else 0.0)
    return zone, subs, m


# ─── Thresholds ──────────────────────────────────────────────────────────────

def thresholds(good_scores, margin):
    t_ok = (max(good_scores) if good_scores else 0.0) * margin
    return round(t_ok, 4), round(t_ok * DEFECT_BAND, 4)


def status_of(score, t_ok, t_def):
    return "ok" if score <= t_ok else ("warn" if score <= t_def else "defect")


def apply_margin(calib: dict, margin: float) -> dict:
    """Recompute thresholds and per-sample outcomes for a new margin
    (no feature extraction needed)."""
    margin = float(min(max(margin, MARGIN_RANGE[0]), MARGIN_RANGE[1]))
    calib["margin"] = round(margin, 3)
    for z in calib["zones"]:
        z["t_ok"], z["t_def"] = thresholds(z["good_scores"], margin)
        z["false_alarms"] = sum(s > z["t_ok"] for s in z["good_scores"])
        for d in z["defects"]:
            d["status"] = status_of(d["score"], z["t_ok"], z["t_def"])
        z["caught"] = sum(d["status"] != "ok" for d in z["defects"])
        z["margin_to_defects"] = (round(min(d["score"] for d in z["defects"]) / z["t_ok"], 3)
                                  if z["defects"] and z["t_ok"] else None)
        for j, sz in enumerate(z["subzones"]):
            sz["t_ok"], sz["t_def"] = thresholds(sz["good_scores"], margin)
            sz["false_alarms"] = sum(s > sz["t_ok"] for s in sz["good_scores"])
            mine = [d for d in z["defects"] if d.get("subzone") == j]
            for d in mine:
                d["sub_status"] = status_of(d["sub_scores"][j], sz["t_ok"], sz["t_def"])
            sz["defects"] = len(mine)
            sz["caught"] = sum(d["sub_status"] != "ok" for d in mine)
    calib["missed"] = sum(z["caught"] < len(z["defects"]) for z in calib["zones"]) + sum(
        sz["caught"] < sz["defects"] for z in calib["zones"] for sz in z["subzones"] if sz["critical"])
    return calib


# ─── Fingerprint: is the calibration still valid for the draft? ──────────────

def fingerprint(version: dict, samples: list) -> str:
    """Changes when the reference, zone geometry or the samples used change."""
    used = sorted((s["id"], s["kind"], bool(s.get("excluded")),
                   json.dumps([d.get("box") for d in s.get("defects", [])], sort_keys=True))
                  for s in samples if s.get("aligned"))
    geo = [(round(z["x"], 4), round(z["y"], 4), round(z["w"], 4), round(z["h"], 4),
            [(round(s["x"], 4), round(s["y"], 4), round(s["w"], 4), round(s["h"], 4), bool(s.get("critical")))
             for s in z.get("subzones", []) or []])
           for z in version.get("zones", [])]
    raw = json.dumps([version.get("ref_image"), geo, used])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# ─── Calibration ─────────────────────────────────────────────────────────────

def calibrate(version: dict, samples: list, load_photo, progress=lambda msg: None,
              margin: float = MARGIN_DEFAULT):
    """Build per-zone models from derived samples.
    load_photo(sample) → BGR image. Returns (calib dict, {zone index: npz bytes})."""
    zones = version["zones"]
    ref_wh = version["ref_size"]
    calib = {"model": MODEL, "margin": margin, "zones": [], "skipped": []}
    blobs = {}
    photos = {}

    def photo(s):
        if s["id"] not in photos:
            photos[s["id"]] = load_photo(s)
        return photos[s["id"]]

    for zi, zone in enumerate(zones):
        geom = ZoneGeom(zone, ref_wh)
        label = zone.get("label", f"Zone {zi + 1}")
        goods, defects = [], []
        for s in samples:
            if s.get("excluded") or not s.get("aligned"):
                continue
            H = s["align"]["H"]
            img = photo(s)
            cov, share = geom.coverage(img.shape, H)
            if share < ZONE_COVERAGE_MIN:
                continue
            if s["kind"] == "good":
                goods.append((s, img, cov))
            elif any(d.get("zone") == zi for d in s.get("defects", [])):
                defects.append((s, img, cov))
        if len(goods) < MIN_GOOD_TO_CALIBRATE:
            calib["skipped"].append({"zone": zi, "label": label,
                                     "reason": f"{len(goods)} good samples (need {MIN_GOOD_TO_CALIBRATE}+)"})
            continue
        progress(f"{label}: features of {len(goods) + len(defects)} photos")
        FG = [features(geom.crop(img, s["align"]["H"]), geom) for s, img, _ in goods]
        FD = [features(geom.crop(img, s["align"]["H"]), geom) for s, img, _ in defects]
        progress(f"{label}: checking each good sample against the others")
        loo = [anomaly_map(f, FG[:i] + FG[i + 1:]) for i, f in enumerate(FG)]
        norm = normaliser(loo)
        good_scores, good_sub = [], []
        for i, (raw, (s, img, cov)) in enumerate(zip(loo, goods)):
            # normaliser without the held-out sample, as at inspection time
            n_i = normaliser(loo[:i] + loo[i + 1:]) if len(loo) > 2 else norm
            zsc, subs, _ = scores(raw, n_i, geom.valid_mask(cov), geom)
            good_scores.append(round(zsc, 4))
            good_sub.append([round(x, 4) for x in subs])
        progress(f"{label}: checking defect samples")
        drows = []
        for (s, img, cov), f in zip(defects, FD):
            zsc, subs, m = scores(anomaly_map(f, FG), norm, geom.valid_mask(cov), geom)
            yy, xx = np.unravel_index(int(m.argmax()), m.shape)
            px, py = (xx + .5) / m.shape[1], (yy + .5) / m.shape[0]
            in_box, sub_j = False, None
            for d in s.get("defects", []):
                if d.get("zone") != zi:
                    continue
                if d.get("subzone") is not None:
                    sub_j = d["subzone"]
                b = d["box"]
                bx, by = (b["x"] - zone["x"]) / zone["w"], (b["y"] - zone["y"]) / zone["h"]
                bw, bh = b["w"] / zone["w"], b["h"] / zone["h"]
                in_box |= (bx - .05 <= px <= bx + bw + .05) and (by - .05 <= py <= by + bh + .05)
            drows.append({"sample_id": s["id"], "score": round(zsc, 4),
                          "sub_scores": [round(x, 4) for x in subs],
                          "subzone": sub_j, "in_box": bool(in_box)})
        calib["zones"].append({
            "zone": zi, "label": label, "grid": list(geom.grid),
            "in_w": geom.in_w, "in_h": geom.in_h,
            "n_good": len(goods), "good_scores": good_scores,
            "good_ids": [s["id"] for s, _, _ in goods],
            "defects": drows,
            "subzones": [{"label": sz.get("label", f"S{j + 1}"), "critical": bool(sz.get("critical")),
                          "good_scores": [g[j] for g in good_sub]}
                         for j, sz in enumerate(geom.subzones)],
        })
        buf = io.BytesIO()
        np.savez_compressed(buf, bank=torch.stack(FG).numpy().astype(np.float16), norm=norm)
        blobs[zi] = buf.getvalue()
    return apply_margin(calib, margin), blobs


# ─── Inspection with a calibrated zone ───────────────────────────────────────

class ZoneModel:
    def __init__(self, zone: dict, ref_wh, zcal: dict, npz_bytes: bytes):
        self.geom = ZoneGeom(zone, ref_wh)
        data = np.load(io.BytesIO(npz_bytes))
        self.bank = [torch.from_numpy(b.astype(np.float32)) for b in data["bank"]]
        self.norm = data["norm"].astype(np.float32)
        self.cal = zcal

    def inspect(self, photo, H):
        """→ dict with status, score, thresholds, maps and subzone outcomes,
        or None when the zone is not (enough) in the photo."""
        cov, share = self.geom.coverage(photo.shape, H)
        if share < ZONE_COVERAGE_MIN:
            return None
        crop = self.geom.crop(photo, H)
        m_raw = anomaly_map(features(crop, self.geom), self.bank)
        zsc, subs, m = scores(m_raw, self.norm, self.geom.valid_mask(cov), self.geom)
        t_ok, t_def = self.cal["t_ok"], self.cal["t_def"]
        sub_out = []
        for j, sz in enumerate(self.cal["subzones"]):
            sub_out.append({"index": j, "label": sz["label"], "critical": sz["critical"],
                            "score": round(subs[j], 3), "t_ok": sz["t_ok"], "t_def": sz["t_def"],
                            "status": status_of(subs[j], sz["t_ok"], sz["t_def"])})
        return {"status": status_of(zsc, t_ok, t_def), "score": round(zsc, 3),
                "t_ok": t_ok, "t_def": t_def, "coverage": round(share, 3),
                "map": m, "crop": crop, "subzones": sub_out,
                "area_pct": round(100.0 * float((m > t_ok).mean()), 2)}
