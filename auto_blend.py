"""
Автоматическое объединение (усреднение) нескольких фотографий одной платы.

Алгоритм:
1. Первое изображение = база (reference).
2. На каждом изображении ищем штрих-коды / QR / DataMatrix (опорные маркеры).
3. Каждое следующее выравнивается к базе через SIFT + гомографию.
   Если глобальный SIFT не сработал — пробуем SIFT только в области кода.
4. Все выровненные изображения усредняются попиксельно.

Результат — изображение с уменьшенным шумом и повышенной детализацией.
"""

import cv2
import numpy as np

try:
    import zxingcpp
    _HAS_ZXING = True
except ImportError:
    _HAS_ZXING = False

try:
    from pylibdmtx.pylibdmtx import decode as dmtx_decode
    _HAS_DMTX = True
except ImportError:
    _HAS_DMTX = False


# ── Barcode / QR detection ────────────────────────────────────────────────────

# Format name mapping for zxing-cpp
_ZXING_FORMAT_NAMES = {
    "QRCode": "QR", "DataMatrix": "DataMatrix", "Aztec": "Aztec",
    "PDF417": "PDF417", "Code128": "Code128", "Code39": "Code39",
    "Code93": "Code93", "EAN13": "EAN13", "EAN8": "EAN8",
    "UPCA": "UPCA", "UPCE": "UPCE", "ITF": "ITF",
    "Codabar": "Codabar",
}


def _downscale_for_detection(img_gray: np.ndarray, max_dim: int = 1200) -> tuple[np.ndarray, float]:
    """Downscale gray image if too large. Returns (scaled_gray, scale_factor)."""
    h, w = img_gray.shape[:2]
    if max(h, w) <= max_dim:
        return img_gray, 1.0
    scale = max_dim / max(h, w)
    small = cv2.resize(img_gray, None, fx=scale, fy=scale,
                       interpolation=cv2.INTER_AREA)
    return small, scale


def detect_codes(img: np.ndarray, dmtx_timeout: int = 1500, quick: bool = False) -> list[dict]:
    """
    Detect barcodes, QR codes, DataMatrix on the image.
    Primary: zxing-cpp (fast, all formats including DataMatrix).
    Fallback: OpenCV + pylibdmtx.
    Returns list of {type, data, bbox} where bbox is (x, y, w, h).
    """
    results = []
    seen_data = set()

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    gray_det, det_scale = _downscale_for_detection(gray, max_dim=1200)

    # ── Primary: zxing-cpp (handles ALL formats including DataMatrix instantly)
    if _HAS_ZXING:
        try:
            barcodes = zxingcpp.read_barcodes(gray_det)
            inv = 1.0 / det_scale
            for bc in barcodes:
                data = bc.text
                if not data or data in seen_data:
                    continue
                seen_data.add(data)
                fmt_name = bc.format.name if hasattr(
                    bc.format, 'name') else str(bc.format)
                btype = _ZXING_FORMAT_NAMES.get(fmt_name, fmt_name)
                pos = bc.position
                pts = np.array([
                    [pos.top_left.x, pos.top_left.y],
                    [pos.top_right.x, pos.top_right.y],
                    [pos.bottom_right.x, pos.bottom_right.y],
                    [pos.bottom_left.x, pos.bottom_left.y],
                ], dtype=np.int32)
                x, y, w, h = cv2.boundingRect(pts)
                results.append({
                    "type": btype, "data": data,
                    "bbox": (int(x * inv), int(y * inv), int(w * inv), int(h * inv)),
                })
        except Exception as e:
            print(f"   ⚠️ zxing-cpp error: {e}")

        # If zxing found nothing and not quick mode, try CLAHE-enhanced image
        if not results and not quick:
            try:
                clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
                enhanced = clahe.apply(gray_det)
                barcodes = zxingcpp.read_barcodes(enhanced)
                inv = 1.0 / det_scale
                for bc in barcodes:
                    data = bc.text
                    if not data or data in seen_data:
                        continue
                    seen_data.add(data)
                    fmt_name = bc.format.name if hasattr(
                        bc.format, 'name') else str(bc.format)
                    btype = _ZXING_FORMAT_NAMES.get(fmt_name, fmt_name)
                    pos = bc.position
                    pts = np.array([
                        [pos.top_left.x, pos.top_left.y],
                        [pos.top_right.x, pos.top_right.y],
                        [pos.bottom_right.x, pos.bottom_right.y],
                        [pos.bottom_left.x, pos.bottom_left.y],
                    ], dtype=np.int32)
                    x, y, w, h = cv2.boundingRect(pts)
                    results.append({
                        "type": btype, "data": data,
                        "bbox": (int(x * inv), int(y * inv), int(w * inv), int(h * inv)),
                    })
            except Exception:
                pass

    # ── Fallback: OpenCV + pylibdmtx (if zxing not available or found nothing)
    if not results and not _HAS_ZXING:
        results = _detect_codes_opencv(
            gray_det, det_scale, dmtx_timeout, quick)

    return results


def _detect_codes_opencv(gray, det_scale, dmtx_timeout, quick):
    """Fallback barcode detection using OpenCV + pylibdmtx."""
    results = []
    seen_data = set()

    try:
        qr = cv2.QRCodeDetector()
        data, points, _ = qr.detectAndDecode(gray)
        if points is not None and data and data not in seen_data:
            seen_data.add(data)
            pts = points[0].astype(int)
            x, y, w, h = cv2.boundingRect(pts)
            inv = 1.0 / det_scale
            results.append({"type": "QR", "data": data,
                            "bbox": (int(x * inv), int(y * inv), int(w * inv), int(h * inv))})
    except Exception:
        pass

    if not results:
        try:
            bd = cv2.barcode.BarcodeDetector()
            ok, decoded, types, points = bd.detectAndDecode(gray)
            if ok and points is not None:
                for j in range(len(decoded)):
                    bdata = decoded[j] if j < len(decoded) else ""
                    if bdata and bdata in seen_data:
                        continue
                    if bdata:
                        seen_data.add(bdata)
                    pts = points[j].astype(
                        int) if points.ndim == 3 else points.astype(int)
                    x, y, w, h = cv2.boundingRect(pts)
                    btype = types[j] if types is not None and j < len(
                        types) else "BARCODE"
                    inv = 1.0 / det_scale
                    results.append({"type": str(btype), "data": bdata,
                                    "bbox": (int(x * inv), int(y * inv), int(w * inv), int(h * inv))})
        except Exception:
            pass

    if not results and _HAS_DMTX and not quick:
        try:
            dmtx_results = dmtx_decode(gray, timeout=dmtx_timeout, max_count=3)
            for dm in dmtx_results:
                data = dm.data.decode("utf-8", errors="replace")
                if data in seen_data:
                    continue
                seen_data.add(data)
                r = dm.rect
                inv = 1.0 / det_scale
                results.append({"type": "DataMatrix", "data": data,
                                "bbox": (int(r.left * inv), int(r.top * inv),
                                         int(r.width * inv), int(r.height * inv))})
        except Exception:
            pass

    return results


def _codes_summary(codes: list[dict]) -> str:
    """Human-readable summary of detected codes."""
    if not codes:
        return "кодов не найдено"
    parts = []
    for c in codes:
        data_short = c["data"][:30] + "…" if len(c["data"]) > 30 else c["data"]
        parts.append(f"{c['type']}({data_short})")
    return ", ".join(parts)


def _code_roi_mask(img_shape, codes, margin=100):
    """Create a mask covering barcode regions + margin for focused SIFT."""
    h, w = img_shape[:2]
    mask = np.zeros((h, w), dtype=np.uint8)
    for c in codes:
        x, y, bw, bh = c["bbox"]
        x0 = max(0, x - margin)
        y0 = max(0, y - margin)
        x1 = min(w, x + bw + margin)
        y1 = min(h, y + bh + margin)
        mask[y0:y1, x0:x1] = 255
    return mask


def _align_to_base(base_gray, base_kp, base_des, base_full_shape, photo, sift,
                   max_dim=1200, base_roi_kp=None, base_roi_des=None,
                   photo_codes=None):
    """Align photo to base using precomputed base SIFT features.
    Falls back to barcode-region SIFT if global fails.
    """
    base_full_h, base_full_w = base_full_shape
    bh, bw = base_gray.shape[:2]
    ph, pw = photo.shape[:2]

    p_scale = min(max_dim / max(ph, pw), 1.0)
    photo_small = cv2.resize(photo, None, fx=p_scale, fy=p_scale)

    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    photo_gray = clahe.apply(cv2.cvtColor(photo_small, cv2.COLOR_BGR2GRAY))

    # Try global SIFT first
    result = _try_sift_align(base_kp, base_des, photo_gray, sift,
                             p_scale, base_full_shape, base_gray.shape[:2], photo)
    if result is not None:
        return result

    # Fallback: SIFT only in barcode regions
    if base_roi_kp is not None and base_roi_des is not None and photo_codes:
        photo_roi_mask = _code_roi_mask(photo_small.shape, [
            {"bbox": (int(c["bbox"][0] * p_scale), int(c["bbox"][1] * p_scale),
                      int(c["bbox"][2] * p_scale), int(c["bbox"][3] * p_scale)),
             "type": c["type"], "data": c["data"]}
            for c in photo_codes
        ], margin=int(100 * p_scale))
        kp2, des2 = sift.detectAndCompute(photo_gray, photo_roi_mask)
        if des2 is not None and len(des2) >= 10:
            result = _try_sift_match(base_roi_kp, base_roi_des, kp2, des2,
                                     p_scale, base_full_shape, base_gray.shape[:2], photo)
            if result is not None:
                return result[0], "ROI-" + result[1]

    return None, "Не удалось выровнять (ни глобально, ни по области кодов)"


def _try_sift_align(base_kp, base_des, photo_gray, sift,
                    p_scale, base_full_shape, base_small_shape, photo):
    """Full-image SIFT alignment attempt."""
    kp2, des2 = sift.detectAndCompute(photo_gray, None)
    if des2 is None or len(des2) < 10:
        return None
    return _try_sift_match(base_kp, base_des, kp2, des2,
                           p_scale, base_full_shape, base_small_shape, photo)


def _try_sift_match(base_kp, base_des, kp2, des2,
                    p_scale, base_full_shape, base_small_shape, photo):
    """Try SIFT matching and homography. Returns (warped, msg) or None."""
    base_full_h, base_full_w = base_full_shape
    bh, bw = base_small_shape

    flann = cv2.FlannBasedMatcher(dict(algorithm=1, trees=5), dict(checks=80))
    matches = flann.knnMatch(base_des, des2, k=2)

    good = [m for pair in matches if len(pair) == 2
            for m, n in [pair] if m.distance < 0.8 * n.distance]
    if len(good) < 8:
        return None

    src_pts = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    dst_pts = np.float32(
        [base_kp[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    M, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)

    if M is None or mask is None:
        return None
    inlier_ratio = mask.sum() / len(mask)
    if inlier_ratio < 0.15:
        return None

    b_scale = min(1200 / max(base_full_h, base_full_w), 1.0)
    S_src = np.diag([p_scale, p_scale, 1.0])
    S_dst_inv = np.diag([1 / b_scale, 1 / b_scale, 1.0])
    M_full = S_dst_inv @ M @ S_src

    warped = cv2.warpPerspective(photo, M_full, (base_full_w, base_full_h))
    return warped, f"OK ({int(mask.sum())} inliers, {inlier_ratio:.0%})"


def auto_blend_images(images: list[np.ndarray]) -> tuple[np.ndarray | None, list[str]]:
    """
    Автоматически выровнять и усреднить список BGR-изображений.
    Использует штрих-коды/QR/DataMatrix как опорные маркеры.

    Returns:
        (result_image, log_messages)
    """
    if len(images) < 2:
        return images[0] if images else None, ["Нужно минимум 2 изображения"]

    base = images[0]
    bh, bw = base.shape[:2]
    max_dim = 1200
    b_scale = min(max_dim / max(bh, bw), 1.0)

    # Detect codes on all images (quick mode — skip heavy DataMatrix for speed)
    all_codes = []
    for img in images:
        all_codes.append(detect_codes(img, quick=True))

    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    base_small = cv2.resize(base, None, fx=b_scale, fy=b_scale)
    base_gray = clahe.apply(cv2.cvtColor(base_small, cv2.COLOR_BGR2GRAY))

    sift = cv2.SIFT_create(nfeatures=8000)
    base_kp, base_des = sift.detectAndCompute(base_gray, None)

    if base_des is None or len(base_des) < 10:
        return None, ["Базовое изображение: недостаточно ключевых точек"]

    # Precompute base ROI SIFT (barcode regions) for fallback
    base_roi_kp, base_roi_des = None, None
    if all_codes[0]:
        base_roi_mask = _code_roi_mask(base_small.shape, [
            {"bbox": (int(c["bbox"][0] * b_scale), int(c["bbox"][1] * b_scale),
                      int(c["bbox"][2] * b_scale), int(c["bbox"][3] * b_scale)),
             "type": c["type"], "data": c["data"]}
            for c in all_codes[0]
        ], margin=int(100 * b_scale))
        base_roi_kp, base_roi_des = sift.detectAndCompute(
            base_gray, base_roi_mask)

    # Accumulate pixels (float64 for precision)
    accum = base.astype(np.float64)
    count = np.ones((bh, bw), dtype=np.float64)
    logs = [f"Фото 1: база ({bw}×{bh}), {_codes_summary(all_codes[0])}"]

    for i, photo in enumerate(images[1:], start=2):
        codes_i = all_codes[i - 1]
        warped, msg = _align_to_base(
            base_gray, base_kp, base_des, (bh, bw), photo, sift, max_dim,
            base_roi_kp=base_roi_kp, base_roi_des=base_roi_des,
            photo_codes=codes_i)
        if warped is not None:
            mask = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY) > 0
            accum[mask] += warped[mask].astype(np.float64)
            count[mask] += 1
            logs.append(f"Фото {i}: {msg}, {_codes_summary(codes_i)}")
        else:
            logs.append(f"Фото {i}: ПРОПУЩЕНО — {msg}")

    # Average
    count_3ch = np.stack([count] * 3, axis=-1)
    result = (accum / count_3ch).clip(0, 255).astype(np.uint8)

    aligned_count = sum(1 for l in logs if "OK" in l)
    logs.append(
        f"Итого: {aligned_count + 1}/{len(images)} изображений объединено")

    return result, logs
