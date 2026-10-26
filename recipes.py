"""
Recipe model: versions, samples, publish gates.

A recipe is the `templates` document (id, name, group, barcode_mask, ...).
Its top-level zones / ref_image / version / sensitivity mirror the ACTIVE
version, so the inspection path (load_template, history, API v1) keeps
reading one document. Every version — draft, active or archived — lives in
`recipe_versions`; Recipe Studio edits drafts there and publishing copies the
draft over the mirror.

Each version has its own reference image in R2 (templates/<tid>/v<N>/ref.jpg).
Before this model the reference was overwritten in place on every update.
"""

import datetime

import cv2
import numpy as np

STATUS_DRAFT = "draft"
STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"

# Fields copied from a version onto the recipe document when it goes live
MIRROR_FIELDS = ("name", "barcode_mask", "zones", "ref_image",
                 "zone_sensitivity", "subzone_sensitivity",
                 "shooting_plan", "operator_notes")

# Sample requirements (user decisions 2026-10-26)
MIN_GOOD_PER_ZONE = 15
MIN_DEFECT_PER_ZONE = 2
MIN_DEFECT_PER_CRITICAL_SUBZONE = 1
MIN_DEVICES = 2
MIN_BOARDS = 3

DEFECT_TYPES = ("missing", "wrong_part", "shifted", "rotated",
                "contamination", "damage", "other")


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def ref_key(tid: str, ref_image: str) -> str:
    return f"templates/{tid}/{ref_image}"


# ─── Migration of pre-versioning templates ───────────────────────────────────

def migrate_template(doc: dict, versions_col) -> int:
    """Create recipe_versions records for a template saved before versioning.
    Idempotent: does nothing when the recipe already has versions.
    Returns the number of version records created."""
    tid = doc["id"]
    if versions_col.find_one({"recipe_id": tid}, {"_id": 1}):
        return 0
    current = doc.get("version", 1)
    records = []
    for old in doc.get("versions", []):
        if old.get("version") == current:
            continue
        records.append(_legacy_version(tid, old, STATUS_ARCHIVED))
    records.append(_legacy_version(tid, doc, STATUS_ACTIVE))
    seen = set()
    unique = []
    for r in sorted(records, key=lambda r: r["version"]):
        if r["version"] not in seen:
            seen.add(r["version"])
            unique.append(r)
    if unique:
        versions_col.insert_many(unique)
    return len(unique)


def _legacy_version(tid: str, src: dict, status: str) -> dict:
    return {
        "recipe_id": tid,
        "version": src.get("version", 1),
        "status": status,
        "name": src.get("name", ""),
        "barcode_mask": src.get("barcode_mask", ""),
        "zones": src.get("zones", []),
        # Old versions shared one overwritten file: keep its key, flag it
        "ref_image": src.get("ref_image", "ref.jpg"),
        "ref_shared_legacy": True,
        "zone_sensitivity": src.get("zone_sensitivity"),
        "subzone_sensitivity": src.get("subzone_sensitivity"),
        "shooting_plan": src.get("shooting_plan", "zones"),
        "operator_notes": src.get("operator_notes", ""),
        "created_at": src.get("updated") or src.get("created", ""),
        "created_by": src.get("created_by", ""),
        "published_at": src.get("updated") or src.get("created", ""),
        "published_by": "",
        "parent": None,
        "migrated": True,
    }


# ─── Version helpers ─────────────────────────────────────────────────────────

def new_draft(tid: str, version: int, base: dict | None, user: str) -> dict:
    """Draft record; copies geometry and settings from `base` if given."""
    base = base or {}
    return {
        "recipe_id": tid,
        "version": version,
        "status": STATUS_DRAFT,
        "name": base.get("name", ""),
        "barcode_mask": base.get("barcode_mask", ""),
        "zones": base.get("zones", []),
        "ref_image": base.get("ref_image"),
        "ref_size": base.get("ref_size"),
        "ref_quality": base.get("ref_quality"),
        "zone_sensitivity": base.get("zone_sensitivity"),
        "subzone_sensitivity": base.get("subzone_sensitivity"),
        "shooting_plan": base.get("shooting_plan", "zones"),
        "operator_notes": base.get("operator_notes", ""),
        "created_at": now_iso(),
        "created_by": user,
        "parent": base.get("version"),
    }


def mirror_update(version: dict) -> dict:
    """$set for the recipe document when `version` becomes active."""
    upd = {k: version.get(k) for k in MIRROR_FIELDS}
    upd["version"] = version["version"]
    upd["active_version"] = version["version"]
    upd["updated"] = now_iso()
    return upd


def geometry_key(version: dict) -> tuple:
    """Samples stay valid across versions while reference and zone
    rectangles are unchanged (labels, sensitivity, notes may differ)."""
    rects = tuple((round(z["x"], 4), round(z["y"], 4),
                   round(z["w"], 4), round(z["h"], 4))
                  for z in version.get("zones", []))
    return version.get("ref_image"), rects


# ─── Photo quality ───────────────────────────────────────────────────────────

# Thresholds on a 1000 px long side; tuned to phone shots of boards
SHARPNESS_MIN = 60.0          # variance of Laplacian
BRIGHT_MIN, BRIGHT_MAX = 40, 215
CLIPPED_MAX = 0.08            # share of pixels at 0-4 or 251-255
MIN_LONG_SIDE = 800


def photo_quality(img: np.ndarray) -> dict:
    """Sharpness / exposure / resolution check for references and samples.
    level: ok | warn | bad, with human-readable issues (English, shown in UI)."""
    h, w = img.shape[:2]
    scale = 1000.0 / max(h, w)
    small = cv2.resize(img, None, fx=scale, fy=scale,
                       interpolation=cv2.INTER_AREA) if scale < 1 else img
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    mean = float(gray.mean())
    clipped = float(((gray <= 4) | (gray >= 251)).mean())
    issues, level = [], "ok"

    def flag(msg, lvl):
        nonlocal level
        issues.append(msg)
        if lvl == "bad" or level == "ok":
            level = lvl

    if max(h, w) < MIN_LONG_SIDE:
        flag(f"Low resolution ({w}x{h}); use at least {MIN_LONG_SIDE} px", "bad")
    if sharp < SHARPNESS_MIN * 0.5:
        flag("Blurry: hold the phone still and tap to focus", "bad")
    elif sharp < SHARPNESS_MIN:
        flag("Slightly soft focus", "warn")
    if mean < BRIGHT_MIN:
        flag("Too dark", "bad" if mean < BRIGHT_MIN * 0.6 else "warn")
    elif mean > BRIGHT_MAX:
        flag("Overexposed", "bad" if mean > 240 else "warn")
    if clipped > CLIPPED_MAX:
        flag(f"Glare or deep shadows on {clipped:.0%} of the image", "warn")
    return {"level": level, "issues": issues, "sharpness": round(sharp, 1),
            "brightness": round(mean, 1), "clipped": round(clipped, 3),
            "width": w, "height": h}


# ─── Samples: geometry derived from the stored homography ────────────────────

def zone_visibility(H, photo_wh, ref_wh, zones, tol_px: float = 2.0) -> list[bool]:
    """Which zones lie fully inside the photo once it is mapped onto the
    reference. Derived from the homography on demand, so editing zones never
    requires re-labelling samples."""
    if H is None:
        return [False] * len(zones)
    pw, ph = photo_wh
    rw, rh = ref_wh
    corners = np.float32([[0, 0], [pw, 0], [pw, ph], [0, ph]]).reshape(-1, 1, 2)
    quad = cv2.perspectiveTransform(corners, np.asarray(H, np.float64)).reshape(-1, 2)
    quad = quad.astype(np.float32)
    out = []
    for z in zones:
        x1, y1 = z["x"] * rw, z["y"] * rh
        x2, y2 = (z["x"] + z["w"]) * rw, (z["y"] + z["h"]) * rh
        inside = all(cv2.pointPolygonTest(quad, (float(px), float(py)), True) >= -tol_px
                     for px, py in ((x1, y1), (x2, y1), (x2, y2), (x1, y2)))
        out.append(bool(inside))
    return out


def locate_box(box: dict, zones: list[dict]):
    """(zone, subzone) whose rectangle contains the centre of a defect box
    given in normalized reference coordinates; None where nothing matches.
    The smallest containing zone wins when zones overlap."""
    cx, cy = box["x"] + box["w"] / 2, box["y"] + box["h"] / 2
    best = None
    for i, z in enumerate(zones):
        if z["x"] <= cx <= z["x"] + z["w"] and z["y"] <= cy <= z["y"] + z["h"]:
            if best is None or z["w"] * z["h"] < zones[best]["w"] * zones[best]["h"]:
                best = i
    if best is None:
        return None, None
    z = zones[best]
    rx, ry = (cx - z["x"]) / z["w"], (cy - z["y"]) / z["h"]
    for j, sz in enumerate(z.get("subzones", [])):
        if sz["x"] <= rx <= sz["x"] + sz["w"] and sz["y"] <= ry <= sz["y"] + sz["h"]:
            return best, j
    return best, None


def derive_sample(sample: dict, version: dict) -> dict:
    """Sample as seen by a version: visible zones and defect locations computed
    against the version's reference and zones. `stale` = aligned to another
    reference image (needs re-alignment)."""
    out = dict(sample)
    align = sample.get("align") or {}
    zones = version.get("zones", [])
    stale = align.get("ref_image") != version.get("ref_image")
    out["stale"] = stale
    out["aligned"] = bool(align.get("H")) and not stale
    vis = ([] if not out["aligned"] else
           zone_visibility(align["H"], (sample["width"], sample["height"]),
                           version.get("ref_size") or (1, 1), zones))
    out["visible_zones"] = [i for i, v in enumerate(vis) if v]
    out["zones"] = {str(i): "ok" for i in out["visible_zones"]}
    defects = []
    for d in sample.get("defects", []):
        zi, si = locate_box(d["box"], zones) if d.get("box") else (None, None)
        defects.append({**d, "zone": zi, "subzone": si})
    out["defects"] = defects
    return out


# ─── Publish gates ───────────────────────────────────────────────────────────

def sample_summary(version: dict, samples: list[dict]) -> dict:
    """Per-zone sample counts for a version (excluded samples ignored)."""
    zones = version.get("zones", [])
    per_zone = []
    # Only samples aligned to this version's reference count
    active = [s for s in samples if not s.get("excluded") and s.get("aligned")]
    for i, z in enumerate(zones):
        key = str(i)
        good = [s for s in active if s["kind"] == "good"
                and s.get("zones", {}).get(key) == "ok"]
        bad = [s for s in active if s["kind"] == "defect"
               and any(d.get("zone") == i for d in s.get("defects", []))]
        crit = []
        for j, sz in enumerate(z.get("subzones", [])):
            if sz.get("critical"):
                n = sum(1 for s in bad for d in s.get("defects", [])
                        if d.get("zone") == i and d.get("subzone") == j)
                crit.append({"subzone": j, "label": sz.get("label", f"S{j+1}"),
                             "defects": n})
        per_zone.append({
            "zone": i, "label": z.get("label", f"Zone {i+1}"),
            "good": len(good), "defect": len(bad),
            "critical_subzones": crit,
        })
    devices = sorted({s.get("device") or "unknown" for s in active
                      if s.get("device")})
    boards = sorted({s.get("board") for s in active
                     if s["kind"] == "good" and s.get("board")})
    return {"zones": per_zone, "devices": devices, "boards": boards,
            "stale": sum(1 for s in samples if s.get("stale") and not s.get("excluded")),
            "unaligned": sum(1 for s in samples if not s.get("stale")
                             and not s.get("aligned") and not s.get("excluded")),
            "good_total": sum(1 for s in active if s["kind"] == "good"),
            "defect_total": sum(1 for s in active if s["kind"] == "defect")}


def publish_check(version: dict, summary: dict) -> dict:
    """What blocks publishing and what only warns.
    blockers/warnings are English strings for the Studio UI."""
    blockers, warnings = [], []
    if not version.get("ref_image"):
        blockers.append("No reference image")
    elif (version.get("ref_quality") or {}).get("level") == "bad":
        blockers.append("Reference image failed the quality check")
    if not version.get("zones"):
        blockers.append("No zones defined")
    for z in summary["zones"]:
        if z["good"] < MIN_GOOD_PER_ZONE:
            blockers.append(f"{z['label']}: {z['good']}/{MIN_GOOD_PER_ZONE} good samples")
        if z["defect"] < MIN_DEFECT_PER_ZONE:
            blockers.append(f"{z['label']}: {z['defect']}/{MIN_DEFECT_PER_ZONE} defect samples")
        for c in z["critical_subzones"]:
            if c["defects"] < MIN_DEFECT_PER_CRITICAL_SUBZONE:
                blockers.append(f"{z['label']} / {c['label']} (critical): no defect sample")
    if summary.get("stale"):
        warnings.append(f"{summary['stale']} sample(s) were aligned to an older reference; re-align them to count")
    real_devices = [d for d in summary["devices"] if d != "unknown"]
    if len(real_devices) < MIN_DEVICES:
        blockers.append(f"Samples from {len(real_devices)} phone(s); at least {MIN_DEVICES} different phones required")
    if len(summary["boards"]) < MIN_BOARDS:
        warnings.append(f"Good samples from {len(summary['boards'])} board(s); {MIN_BOARDS}+ recommended")
    return {"ok": not blockers, "blockers": blockers, "warnings": warnings}
