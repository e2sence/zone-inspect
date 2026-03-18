"""
PCB Zone Check — проверка зон печатной платы.

Workflow:
  1. Загрузить референсное изображение платы
  2. Разметить зоны контроля (прямоугольники)
  3. Загружать фото зон по одному — система сопоставляет каждое фото с зоной
  4. Все зоны проверены → готово
"""

# Load .env before any other imports that use os.environ
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv not installed — env vars must be set externally

import io
import qrcode
import secrets
import os as _os
import json
import uuid
import base64
import time
import threading
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, request, jsonify, render_template, send_file, make_response, redirect
from skimage.metrics import structural_similarity as ssim
from auto_blend import auto_blend_images, detect_codes
import r2_storage as r2

# ─── MongoDB ──────────────────────────────────────────────────────────────────
try:
    from pymongo import MongoClient
    _MONGO_URI = _os.environ.get("MONGODB_URI", "")
    if _MONGO_URI:
        _mongo = MongoClient(_MONGO_URI, serverSelectionTimeoutMS=5000)
        _mongo_db = _mongo.get_default_database()
        _results_col = _mongo_db["inspection_results"]
        _results_col.create_index("timestamp", unique=False)
        _results_col.create_index("result_id", unique=True)
        _templates_col = _mongo_db["templates"]
        _templates_col.create_index("id", unique=True)
        MONGO_AVAILABLE = True
        print(f"🍃 MongoDB: connected to {_mongo_db.name}")
    else:
        MONGO_AVAILABLE = False
        print("⚠️  MONGODB_URI not set — results stored in R2 only")
except Exception as _e:
    MONGO_AVAILABLE = False
    print(f"⚠️  MongoDB unavailable ({_e}) — results stored in R2 only")

# ─── Neural Engine (optional, graceful fallback to OpenCV) ────────────────────
try:
    from nn_engine import (match_score_nn, analyze_defects_nn,
                           locate_and_extract_nn, similarity_nn,
                           align_photo_to_ref, NN_DEVICE)
    NN_AVAILABLE = True
except ImportError:
    NN_AVAILABLE = False

# ─── Config ───────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
TEMPLATE_DIR = BASE_DIR / "saved_templates"

UPLOAD_DIR.mkdir(exist_ok=True)
TEMPLATE_DIR.mkdir(exist_ok=True)

# Очистка uploads при старте (шаблоны не трогаем)
for f in UPLOAD_DIR.iterdir():
    if f.is_file():
        f.unlink()

ALLOWED_EXT = {"png", "jpg", "jpeg", "bmp", "webp"}

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = int(
    _os.environ.get("MAX_UPLOAD_MB", 128)) * 1024 * 1024
app.secret_key = _os.environ.get("FLASK_SECRET") or secrets.token_hex(32)
if not _os.environ.get("FLASK_SECRET"):
    print("⚠️  FLASK_SECRET not set — sessions will reset on restart")

app.config["BASE_URL"] = _os.environ.get("BASE_URL", "")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# Cache-bust: меняется при каждом рестарте → браузер всегда грузит свежие файлы
_CACHE_BUST = str(int(time.time()))


@app.context_processor
def _inject_cache_bust():
    return dict(v=_CACHE_BUST)


if NN_AVAILABLE:
    print(f"🧠 Neural engine: ON ({NN_DEVICE})")
else:
    print("⚙️  Neural engine: OFF — using OpenCV fallback")

# ─── In-memory session store ─────────────────────────────────────────────────
# session_id → { "ref_path": Path, "ref_img": ndarray, "zones": [...], "checked": {...} }
sessions: dict[str, dict] = {}

# mobile_token → session_id  (shared across gunicorn workers via file)
_MOBILE_TOKENS_PATH = BASE_DIR / ".mobile_tokens.json"
_MOBILE_TOKENS_LOCK = threading.Lock()


def _load_mobile_tokens() -> dict:
    """Read tokens from shared file (called on every lookup for multi-worker sync)."""
    if _MOBILE_TOKENS_PATH.exists():
        try:
            with open(_MOBILE_TOKENS_PATH) as f:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_SH)
                data = json.load(f)
                fcntl.flock(f, fcntl.LOCK_UN)
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_mobile_tokens(tokens: dict):
    """Write tokens to shared file with exclusive lock."""
    try:
        with open(_MOBILE_TOKENS_PATH, "w") as f:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(tokens, f)
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        pass


def _get_token_session(token: str) -> str | None:
    """Get session_id for a mobile token (reads from shared file)."""
    return _load_mobile_tokens().get(token)


def _set_token_session(token: str, sid: str):
    """Set or update a mobile token → session mapping."""
    with _MOBILE_TOKENS_LOCK:
        tokens = _load_mobile_tokens()
        tokens[token] = sid
        _save_mobile_tokens(tokens)


def _remove_tokens_for_session(sid: str):
    """Remove all mobile tokens pointing to a given session."""
    with _MOBILE_TOKENS_LOCK:
        tokens = _load_mobile_tokens()
        tokens = {t: s for t, s in tokens.items() if s != sid}
        _save_mobile_tokens(tokens)


# session_id → list of uploaded photo blobs (bytes) from mobile
mobile_photos: dict[str, list[bytes]] = {}

# ─── Session TTL cleanup (60 min) ────────────────────────────────────────────
SESSION_TTL = 3600  # seconds


def _cleanup_sessions():
    """Remove sessions inactive for more than SESSION_TTL."""
    while True:
        time.sleep(120)  # check every 2 min
        now = time.time()
        expired = [sid for sid, s in sessions.items()
                   if now - s.get("_last_active", s.get("_created", 0)) > SESSION_TTL]
        for sid in expired:
            sessions.pop(sid, None)
            # Clean mobile photos for this session
            mobile_photos.pop(sid, None)
            # Remove mobile tokens pointing to this session
            _remove_tokens_for_session(sid)


_cleanup_thread = threading.Thread(target=_cleanup_sessions, daemon=True)
_cleanup_thread.start()

# ─── Auth: access key system ─────────────────────────────────────────────────
AUTH_KEYS_PATH = BASE_DIR / "auth_keys.json"
AUTH_COOKIE = "pcb_auth_key"
AUTH_COOKIE_MAX_AGE = 30 * 86400  # 30 days


def _load_auth_keys() -> dict:
    """Load {key: username} from auth_keys.json."""
    if AUTH_KEYS_PATH.exists():
        with open(AUTH_KEYS_PATH) as f:
            return json.load(f)
    return {}


def _check_auth():
    """Return username if valid auth, else None."""
    key = request.cookies.get(AUTH_COOKIE) or request.headers.get("X-Auth-Key")
    if not key:
        return None
    keys = _load_auth_keys()
    return keys.get(key)


# Routes that don't require auth
_PUBLIC_PREFIXES = ("/login", "/static/", "/api/mobile/", "/mobile")


@app.before_request
def _require_auth():
    path = request.path
    # Public routes — no auth needed
    for prefix in _PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return None
    # Check auth
    user = _check_auth()
    if not user:
        # API calls get 401 JSON, browsers get redirect
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized"}), 401
        return redirect("/login")
    # Tag request with user
    request.auth_user = user


@app.after_request
def _no_cache(resp):
    # Никакого кеширования — всегда свежие данные
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/login", methods=["GET"])
def login_page():
    return render_template("login.html")


@app.route("/login", methods=["POST"])
def login_submit():
    data = request.get_json(silent=True) or {}
    key = data.get("key", "").strip()
    keys = _load_auth_keys()
    username = keys.get(key)
    if not username:
        return jsonify({"error": "Invalid access key"}), 403
    resp = jsonify({"status": "ok", "user": username})
    resp.set_cookie(AUTH_COOKIE, key, max_age=AUTH_COOKIE_MAX_AGE,
                    httponly=True, samesite="Lax")
    return resp


@app.route("/logout")
def logout():
    resp = redirect("/login")
    resp.delete_cookie(AUTH_COOKIE)
    return resp


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _allowed(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def _save_upload(file_storage) -> Path:
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    safe_name = f"{uuid.uuid4().hex}.{ext}"
    path = UPLOAD_DIR / safe_name
    file_storage.save(str(path))
    return path


def _img_to_b64(img: np.ndarray) -> str:
    """BGR numpy → base64 JPEG."""
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return base64.b64encode(buf.tobytes()).decode()


def _file_to_b64(path: Path) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _crop_zone(img: np.ndarray, zone: dict) -> np.ndarray:
    """Вырезаем зону из изображения по нормализованным координатам (0..1)."""
    h, w = img.shape[:2]
    x1 = int(zone["x"] * w)
    y1 = int(zone["y"] * h)
    x2 = int((zone["x"] + zone["w"]) * w)
    y2 = int((zone["y"] + zone["h"]) * h)
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    return img[y1:y2, x1:x2]


def _precompute_anchor_crops(sid, ref_img, zones):
    """Crop square patches around per-zone anchor points for template matching on mobile."""
    h, w = ref_img.shape[:2]
    patch_frac = 0.08  # 8% of image dimension
    patch_sz = int(max(h, w) * patch_frac)
    half = patch_sz // 2
    all_crops = {}  # {zone_index: [crop1, crop2]}
    for zi, zone in enumerate(zones):
        zone_anchors = zone.get("anchors", [])
        if not zone_anchors:
            continue
        crops = []
        for a in zone_anchors[:2]:
            cx = int(a["x"] * w)
            cy = int(a["y"] * h)
            x1 = max(0, cx - half)
            y1 = max(0, cy - half)
            x2 = min(w, cx + half)
            y2 = min(h, cy + half)
            crop = ref_img[y1:y2, x1:x2]
            if crop.size > 0:
                crop = cv2.resize(crop, (96, 96))
            crops.append(crop)
        all_crops[zi] = crops
    sessions[sid]["_anchor_crops"] = all_crops


def _normalize_lighting(img: np.ndarray) -> np.ndarray:
    """
    Нормализация освещения: CLAHE в LAB-пространстве.
    Убирает влияние бликов и неравномерного освещения.
    """
    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    l = clahe.apply(l)
    lab = cv2.merge([l, a, b])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def _glare_mask(gray: np.ndarray, threshold: int = 220) -> np.ndarray:
    """
    Маска бликов — яркие пятна, которые нужно исключить из анализа.
    Возвращает маску: 255 = нормальный пиксель, 0 = блик.
    """
    _, bright = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY)
    # Расширяем область блика чтобы захватить ореол
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))
    bright = cv2.dilate(bright, kernel, iterations=1)
    return cv2.bitwise_not(bright)


def _match_score(zone_crop: np.ndarray, photo: np.ndarray) -> float:
    """
    Скор определения: насколько зона присутствует в фото.
    Neural engine (EfficientNet-B4) при наличии, иначе OpenCV fallback.
    """
    if NN_AVAILABLE:
        return match_score_nn(zone_crop, photo)

    # ── OpenCV fallback ──────────────────────────────────────────────────
    # Нормализуем освещение перед всеми операциями
    zone_n = _normalize_lighting(zone_crop)
    photo_n = _normalize_lighting(photo)
    ref_gray = cv2.cvtColor(zone_n, cv2.COLOR_BGR2GRAY)
    photo_gray = cv2.cvtColor(photo_n, cv2.COLOR_BGR2GRAY)

    # 1. Multi-scale template matching: ищем зону в фото на разных масштабах
    best_tmpl = 0.0
    rh, rw = ref_gray.shape[:2]
    ph, pw = photo_gray.shape[:2]
    for scale in [0.5, 0.7, 0.85, 1.0, 1.2, 1.5, 2.0]:
        tw = int(rw * scale)
        th = int(rh * scale)
        if tw >= pw or th >= ph or tw < 16 or th < 16:
            continue
        tmpl = cv2.resize(ref_gray, (tw, th))
        res = cv2.matchTemplate(photo_gray, tmpl, cv2.TM_CCOEFF_NORMED)
        best_tmpl = max(best_tmpl, float(res.max()))

    # 2. SIFT feature matching
    sift = cv2.SIFT_create(nfeatures=1500)
    kp1, des1 = sift.detectAndCompute(ref_gray, None)
    kp2, des2 = sift.detectAndCompute(photo_gray, None)
    sift_score = 0.0
    if des1 is not None and des2 is not None and len(des1) > 4 and len(des2) > 4:
        bf = cv2.BFMatcher(cv2.NORM_L2)
        matches = bf.knnMatch(des1, des2, k=2)
        good = [m for m_pair in matches if len(m_pair) == 2
                for m, n in [m_pair] if m.distance < 0.7 * n.distance]
        sift_score = min(len(good) / max(len(kp1), 1), 1.0)

    # 3. Histogram correlation (на нормализованных изображениях)
    hist_a = cv2.calcHist([zone_n], [0, 1, 2], None,
                          [32, 32, 32], [0, 256] * 3)
    hist_b = cv2.calcHist([photo_n], [0, 1, 2], None,
                          [32, 32, 32], [0, 256] * 3)
    cv2.normalize(hist_a, hist_a)
    cv2.normalize(hist_b, hist_b)
    hist_corr = max(cv2.compareHist(hist_a, hist_b, cv2.HISTCMP_CORREL), 0)

    # 4. Edge-based matching (устойчиво к бликам — края не зависят от яркости)
    ref_edges = cv2.Canny(ref_gray, 50, 150)
    photo_edges = cv2.Canny(photo_gray, 50, 150)
    edge_score = 0.0
    for scale in [0.7, 0.85, 1.0, 1.2, 1.5]:
        tw = int(rw * scale)
        th = int(rh * scale)
        if tw >= pw or th >= ph or tw < 16 or th < 16:
            continue
        tmpl_e = cv2.resize(ref_edges, (tw, th))
        res_e = cv2.matchTemplate(photo_edges, tmpl_e, cv2.TM_CCOEFF_NORMED)
        edge_score = max(edge_score, float(res_e.max()))

    score = 0.30 * best_tmpl + 0.25 * sift_score + \
        0.15 * hist_corr + 0.30 * edge_score
    return round(score, 4)


def _try_global_alignment_cv(ref_img, photo, zone):
    """
    OpenCV fallback: глобальное выравнивание фото с референсом через SIFT,
    затем кроп по координатам зоны.
    """
    warped = _try_global_align_full(ref_img, photo)
    if warped is None:
        return None

    rh, rw = ref_img.shape[:2]
    x = int(zone["x"] * rw)
    y = int(zone["y"] * rh)
    w = int(zone["w"] * rw)
    h = int(zone["h"] * rh)
    x = max(0, min(x, rw - 1))
    y = max(0, min(y, rh - 1))
    w = max(1, min(w, rw - x))
    h = max(1, min(h, rh - y))

    cropped = warped[y:y + h, x:x + w]
    if cropped.size == 0:
        return None
    return cropped


def _try_global_align_full(ref_img, photo):
    """
    Глобальное SIFT-выравнивание: warp фото в координаты ref_img.
    Возвращает полное warped фото или None.
    """
    MAX_DIM = 1200
    rh, rw = ref_img.shape[:2]
    ph, pw = photo.shape[:2]

    r_scale = min(1.0, MAX_DIM / max(rh, rw))
    p_scale = min(1.0, MAX_DIM / max(ph, pw))

    ref_sm = cv2.resize(ref_img, None, fx=r_scale, fy=r_scale)
    photo_sm = cv2.resize(photo, None, fx=p_scale, fy=p_scale)

    ref_gray = cv2.cvtColor(_normalize_lighting(ref_sm), cv2.COLOR_BGR2GRAY)
    photo_gray = cv2.cvtColor(
        _normalize_lighting(photo_sm), cv2.COLOR_BGR2GRAY)

    sift = cv2.SIFT_create(nfeatures=5000)
    kp1, des1 = sift.detectAndCompute(ref_gray, None)
    kp2, des2 = sift.detectAndCompute(photo_gray, None)

    if des1 is None or des2 is None or len(des1) < 10 or len(des2) < 10:
        return None

    index_params = dict(algorithm=1, trees=5)
    search_params = dict(checks=50)
    flann = cv2.FlannBasedMatcher(index_params, search_params)
    matches = flann.knnMatch(des2, des1, k=2)
    good = [m for m_pair in matches if len(m_pair) == 2
            for m, n in [m_pair] if m.distance < 0.7 * n.distance]

    if len(good) < 15:
        return None

    src_pts = np.float32([kp2[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    dst_pts = np.float32([kp1[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    M, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)

    if M is None:
        return None
    inlier_ratio = float(mask.sum()) / len(mask)
    if inlier_ratio < 0.25:
        return None

    S_photo = np.diag([p_scale, p_scale, 1.0])
    S_ref_inv = np.diag([1.0 / r_scale, 1.0 / r_scale, 1.0])
    M_full = S_ref_inv @ M @ S_photo

    return cv2.warpPerspective(photo, M_full, (rw, rh))


def _locate_and_extract(zone_crop: np.ndarray, photo: np.ndarray,
                        ref_img: np.ndarray = None, zone: dict = None):
    """
    Найти зону внутри фото и извлечь выровненный фрагмент.
    Neural engine при наличии, иначе OpenCV fallback.
    Возвращает (extracted_region, method: str).
    """
    if NN_AVAILABLE:
        return locate_and_extract_nn(zone_crop, photo, ref_img, zone)

    # ── OpenCV fallback ──────────────────────────────────────────────────
    # Strategy A: global alignment (if ref_img and zone provided)
    if ref_img is not None and zone is not None:
        warped = _try_global_alignment_cv(ref_img, photo, zone)
        if warped is not None:
            zh, zw = zone_crop.shape[:2]
            return cv2.resize(warped, (zw, zh)), "global_homography"

    zone_n = _normalize_lighting(zone_crop)
    photo_n = _normalize_lighting(photo)
    ref_gray = cv2.cvtColor(zone_n, cv2.COLOR_BGR2GRAY)
    photo_gray = cv2.cvtColor(photo_n, cv2.COLOR_BGR2GRAY)

    # Попробуем SIFT + Homography для точного выравнивания
    sift = cv2.SIFT_create(nfeatures=2000)
    kp1, des1 = sift.detectAndCompute(ref_gray, None)
    kp2, des2 = sift.detectAndCompute(photo_gray, None)

    if des1 is not None and des2 is not None and len(des1) > 6 and len(des2) > 6:
        bf = cv2.BFMatcher(cv2.NORM_L2)
        matches = bf.knnMatch(des1, des2, k=2)
        good = [m for m_pair in matches if len(m_pair) == 2
                for m, n in [m_pair] if m.distance < 0.7 * n.distance]

        if len(good) >= 8:
            src_pts = np.float32(
                [kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
            dst_pts = np.float32(
                [kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
            M, mask = cv2.findHomography(dst_pts, src_pts, cv2.RANSAC, 5.0)
            if M is not None:
                h, w = zone_crop.shape[:2]
                warped = cv2.warpPerspective(photo, M, (w, h))
                return warped, True

    # Fallback: multi-scale template matching + crop (на нормализованных)
    rh, rw = ref_gray.shape[:2]
    ph, pw = photo_gray.shape[:2]
    best_val, best_loc, best_scale = 0.0, (0, 0), 1.0
    for scale in [0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.5, 2.0]:
        tw = int(rw * scale)
        th = int(rh * scale)
        if tw >= pw or th >= ph or tw < 16 or th < 16:
            continue
        tmpl = cv2.resize(ref_gray, (tw, th))
        res = cv2.matchTemplate(photo_gray, tmpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val > best_val:
            best_val, best_loc, best_scale = max_val, max_loc, scale

    # Вырезаем найденную область и ресайзим до размера зоны
    tw = int(rw * best_scale)
    th = int(rh * best_scale)
    x, y = best_loc
    crop = photo[y:y + th, x:x + tw]
    if crop.size == 0:
        return cv2.resize(photo, (rw, rh)), False
    return cv2.resize(crop, (rw, rh)), False


def _analyze_defects(zone_crop: np.ndarray, extracted: np.ndarray,
                     extract_nn_score: float = 0.0,
                     strict: bool = False) -> dict:
    """
    Детекция дефектов: структурное сравнение при наличии neural engine,
    иначе classical (CLAHE + SSIM + edge diff).
    strict=True: подзоны — жёсткие пороги.
    """
    if NN_AVAILABLE:
        return analyze_defects_nn(zone_crop, extracted, extract_nn_score,
                                  strict=strict)

    # ── OpenCV fallback ──────────────────────────────────────────────────
    size = (256, 256)
    # Нормализуем освещение ДО ресайза для лучшего качества
    a_norm = _normalize_lighting(zone_crop)
    b_norm = _normalize_lighting(extracted)
    a = cv2.resize(a_norm, size)
    b = cv2.resize(b_norm, size)
    a_gray = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
    b_gray = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)

    # Создаём маски бликов — исключаем их из анализа
    a_orig_resized = cv2.resize(zone_crop, size)
    b_orig_resized = cv2.resize(extracted, size)
    a_orig_gray = cv2.cvtColor(a_orig_resized, cv2.COLOR_BGR2GRAY)
    b_orig_gray = cv2.cvtColor(b_orig_resized, cv2.COLOR_BGR2GRAY)
    mask_a = _glare_mask(a_orig_gray)
    mask_b = _glare_mask(b_orig_gray)
    # Общая маска: исключаем блики с обоих изображений
    valid_mask = cv2.bitwise_and(mask_a, mask_b)
    valid_pixels = cv2.countNonZero(valid_mask)
    total_area = size[0] * size[1]

    # SSIM с картой (на нормализованных данных)
    ssim_val, ssim_map = ssim(a_gray, b_gray, data_range=255, full=True)
    ssim_val = float(ssim_val)

    # Абсолютная разница (на нормализованных)
    diff = cv2.absdiff(a, b)
    diff_gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)

    # Разница по краям — устойчива к освещению
    edges_a = cv2.Canny(a_gray, 40, 120)
    edges_b = cv2.Canny(b_gray, 40, 120)
    edge_diff = cv2.absdiff(edges_a, edges_b)

    # Комбинируем: diff_gray + edge_diff для более надёжного порога
    combined_diff = cv2.addWeighted(diff_gray, 0.6, edge_diff, 0.4, 0)

    # Адаптивный порог (устойчивее к неравномерному освещению)
    thresh_adapt = cv2.adaptiveThreshold(
        combined_diff, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 21, -15
    )
    # Также фиксированный порог на нормализованных данных
    _, thresh_fixed = cv2.threshold(combined_diff, 35, 255, cv2.THRESH_BINARY)
    # Пересечение: только то, что оба метода считают дефектом
    thresh = cv2.bitwise_and(thresh_adapt, thresh_fixed)

    # Исключаем области бликов из карты дефектов
    thresh = cv2.bitwise_and(thresh, valid_mask)

    # Морфология — убрать шум
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_DILATE, kernel, iterations=1)

    # Контуры дефектов
    contours, _ = cv2.findContours(
        thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    defect_areas = [cv2.contourArea(c)
                    for c in contours if cv2.contourArea(c) > 50]
    total_defect_area = sum(defect_areas)
    # Считаем процент только от валидных пикселей (без бликов)
    effective_area = max(valid_pixels, 1)
    defect_pct = round(total_defect_area / effective_area * 100, 2)

    # Визуализация: показываем оригинал, не нормализованный
    vis = cv2.resize(b_orig_resized, size)

    # Heatmap разницы (на нормализованных, без бликов)
    diff_masked = cv2.bitwise_and(diff_gray, valid_mask)
    heatmap = cv2.applyColorMap(diff_masked, cv2.COLORMAP_JET)
    overlay = cv2.addWeighted(cv2.resize(
        b_orig_resized, size), 0.6, heatmap, 0.4, 0)

    # Пороги вердикта (строгие для подзон)
    ok_thr = 1.5 if strict else 2.5
    warn_thr = 5.0 if strict else 8.0

    if defect_pct < ok_thr:
        verdict = "OK ✅ — there are no significant differences"
        status = "ok"
    elif defect_pct < warn_thr:
        verdict = "Warning ⚠️ — minor differences detected"
        status = "warn"
    else:
        verdict = "Defect ❌ — significant differences detected"
        status = "defect"

    # Всегда рисуем контуры дефектов (для ok — полупрозрачно)
    big_contours = [c for c in contours if cv2.contourArea(c) > 50]
    if big_contours:
        glare_inv = cv2.bitwise_not(valid_mask)
        glare_overlay = vis.copy()
        glare_overlay[glare_inv > 0] = (255, 200, 50)
        w = 0.7 if status != "ok" else 0.85
        vis = cv2.addWeighted(vis, w, glare_overlay, 1.0 - w, 0)
        color = (0, 0, 255) if status != "ok" else (0, 100, 255)
        thick = 2 if status != "ok" else 1
        cv2.drawContours(vis, big_contours, -1, color, thick)

    return {
        "ssim": round(ssim_val, 4),
        "defect_pct": defect_pct,
        "defect_count": len(defect_areas),
        "verdict": verdict,
        "status": status,
        "vis_defects_b64": _img_to_b64(vis),
        "vis_heatmap_b64": _img_to_b64(overlay),
        "extracted_b64": _img_to_b64(b),
        "reference_b64": _img_to_b64(cv2.resize(zone_crop, size)),
    }


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/auto_blend", methods=["POST"])
def api_auto_blend():
    """Принять несколько изображений, автоматически выровнять и усреднить."""
    files = request.files.getlist("images")
    if len(files) < 2:
        return jsonify({"error": "Нужно минимум 2 изображения"}), 400

    images = []
    for f in files:
        if not f or not _allowed(f.filename):
            continue
        data = np.frombuffer(f.read(), np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if img is not None:
            images.append(img)

    if len(images) < 2:
        return jsonify({"error": "Не удалось прочитать изображения (нужно минимум 2)"}), 400

    result, logs = auto_blend_images(images)
    if result is None:
        return jsonify({"error": "Не удалось выровнять изображения", "log": logs}), 422

    return jsonify({
        "image_b64": _img_to_b64(result),
        "width": result.shape[1],
        "height": result.shape[0],
        "log": logs,
    })


@app.route("/api/session", methods=["POST"])
def create_session():
    """Шаг 1: Загрузить изображение платы (одно или склеенное на клиенте) → session_id."""
    # Accept 'image' (single/stitched) or 'images' (multi, backward compat)
    file = request.files.get("image")
    if not file:
        files = request.files.getlist("images")
        file = files[0] if files else None
    if not file or not _allowed(file.filename):
        return jsonify({"error": "Файл не найден или недопустимый формат"}), 400

    img_path = _save_upload(file)
    ref_img = cv2.imread(str(img_path))
    if ref_img is None:
        return jsonify({"error": "Не удалось открыть изображение"}), 400

    sid = uuid.uuid4().hex[:12]
    sessions[sid] = {
        "ref_path": img_path,
        "ref_img": ref_img,
        "zones": [],
        "checked": {},
        "operator": _check_auth() or "",
        "_created": time.time(),
        "_last_active": time.time(),
    }

    h, w = ref_img.shape[:2]
    return jsonify({
        "session_id": sid,
        "image_b64": _file_to_b64(img_path),
        "width": w,
        "height": h,
    })


@app.route("/api/session/<sid>/zones", methods=["POST"])
def set_zones(sid):
    """Шаг 2: Задать зоны контроля. Body: { zones: [{x,y,w,h,label},...] } — нормализованные 0..1."""
    if sid not in sessions:
        return jsonify({"error": "Сессия не найдена"}), 404

    data = request.get_json(silent=True)
    if not data or "zones" not in data:
        return jsonify({"error": "Нужен JSON с полем 'zones'"}), 400

    zones = data["zones"]
    if not isinstance(zones, list) or len(zones) == 0:
        return jsonify({"error": "Список зон пуст"}), 400

    for i, z in enumerate(zones):
        for key in ("x", "y", "w", "h"):
            if key not in z:
                return jsonify({"error": f"Зона {i}: отсутствует '{key}'"}), 400
        if "label" not in z:
            z["label"] = f"Зона {i + 1}"
        # Validate subzones (coords relative to parent zone 0..1)
        subzones = z.get("subzones", [])
        if not isinstance(subzones, list):
            subzones = []
        for j, sz in enumerate(subzones):
            for key in ("x", "y", "w", "h"):
                if key not in sz:
                    return jsonify({"error": f"Зона {i}, подзона {j}: отсутствует '{key}'"}), 400
            if "label" not in sz:
                sz["label"] = f"Подзона {j + 1}"
        z["subzones"] = subzones

    sessions[sid]["zones"] = zones
    sessions[sid]["checked"] = {}

    # Pre-compute per-zone anchor crops for template matching on mobile
    ref_img = sessions[sid]["ref_img"]
    _precompute_anchor_crops(sid, ref_img, zones)

    zone_previews = []
    for z in zones:
        crop = _crop_zone(ref_img, z)
        zone_previews.append(_img_to_b64(crop))

    return jsonify({"status": "ok", "count": len(zones), "previews": zone_previews})


@app.route("/api/session/<sid>/serial", methods=["POST"])
def set_serial(sid):
    """Загрузить фото серийного номера — распознать штрих-код/QR."""
    if sid not in sessions:
        return jsonify({"error": "Сессия не найдена"}), 404

    file = request.files.get("photo")
    if not file or not _allowed(file.filename):
        return jsonify({"error": "Файл не найден"}), 400

    data = np.frombuffer(file.read(), np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        return jsonify({"error": "Не удалось прочитать изображение"}), 400

    codes = detect_codes(img)
    if not codes:
        return jsonify({"error": "Код не найден. Попробуйте сфотографировать ближе или чётче."}), 422

    # Pick the first code with non-empty data, fallback to first
    best = next((c for c in codes if c["data"]), codes[0])
    serial = best["data"] or "не распознано"
    sessions[sid]["serial"] = serial
    sessions[sid]["serial_type"] = best["type"]

    return jsonify({
        "serial": serial,
        "type": best["type"],
        "all_codes": [{"type": c["type"], "data": c["data"]} for c in codes],
    })


@app.route("/api/session/<sid>/check", methods=["POST"])
def check_zone(sid):
    """Шаг 3: Загрузить фото зоны → система ищет, какой зоне оно соответствует."""
    import traceback
    try:
        return _check_zone_impl(sid)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Внутренняя ошибка: {e}"}), 500


def _check_zone_impl(sid, photo_img=None):
    if sid not in sessions:
        return jsonify({"error": "Сессия не найдена"}), 404

    s = sessions[sid]
    s["_last_active"] = time.time()
    if not s["zones"]:
        return jsonify({"error": "Зоны не заданы"}), 400

    # ─── Sensitivity overrides from frontend ──────────────────────────────
    import inspection_config as _cfg
    zone_sens = None
    subzone_sens = None
    # Try form data first (desktop upload), then query args (mobile path)
    try:
        zs = request.form.get("zone_sensitivity") or request.args.get(
            "zone_sensitivity")
        if zs is not None:
            zone_sens = max(0.0, min(2.0, float(zs)))
            _cfg.apply_zone_sensitivity(zone_sens)
    except (ValueError, TypeError):
        pass
    try:
        ss = request.form.get("subzone_sensitivity") or request.args.get(
            "subzone_sensitivity")
        if ss is not None:
            subzone_sens = max(0.0, min(2.0, float(ss)))
            _cfg.apply_subzone_sensitivity(subzone_sens)
    except (ValueError, TypeError):
        pass
    if zone_sens is None:
        zone_sens = _cfg.ZONE_SENSITIVITY
    if subzone_sens is None:
        subzone_sens = _cfg.SUBZONE_SENSITIVITY

    if photo_img is not None:
        photo = photo_img
    else:
        if "photo" not in request.files:
            return jsonify({"error": "Файл 'photo' не найден"}), 400

        file = request.files["photo"]
        if not file or not _allowed(file.filename):
            return jsonify({"error": "Недопустимый формат файла"}), 400

        photo_path = _save_upload(file)
        photo = cv2.imread(str(photo_path))
        if photo is None:
            return jsonify({"error": "Не удалось открыть фото"}), 400

    ref_img = s["ref_img"]
    zones = s["zones"]

    # ═══════════════════════════════════════════════════════════════════════
    # Оптимизированный пайплайн:
    #   1. Глобальное выравнивание ОДИН РАЗ (SIFT — дорого, но только 1×)
    #   2. Кроп зон из warped фото (мгновенно)
    #   3. SSIM pre-filter → top кандидаты
    #   4. NN scoring только для top-3 кандидатов
    #   5. Лучший → анализ дефектов
    # ═══════════════════════════════════════════════════════════════════════

    # STEP 1: Global alignment ONCE
    warped = None
    if NN_AVAILABLE:
        warped = align_photo_to_ref(ref_img, photo)
    else:
        warped = _try_global_align_full(ref_img, photo)

    if warped is None:
        pass  # print("   ⚠️ Global alignment failed — per-zone fallback")

    # STEP 2: Extract each zone + quick SSIM ranking
    candidates = []
    for i, z in enumerate(zones):
        crop = _crop_zone(ref_img, z)
        if crop.size == 0:
            candidates.append({"idx": i, "ssim_quick": 0.0,
                              "extracted": None, "method": None})
            continue

        zh, zw = crop.shape[:2]
        extracted = None
        method = None

        if warped is not None:
            raw = _crop_zone(warped, z)
            if raw.size > 0:
                gray_raw = cv2.cvtColor(raw, cv2.COLOR_BGR2GRAY)
                black_ratio = (gray_raw < 5).sum() / gray_raw.size
                if black_ratio <= 0.15:
                    extracted = cv2.resize(raw, (zw, zh))
                    method = "global_homography"

        if extracted is None:
            # Slow fallback: per-zone localization
            extracted, method = _locate_and_extract(crop, photo, ref_img, z)

        # Quick SSIM for ranking (near-instant)
        a_g = cv2.cvtColor(cv2.resize(crop, (128, 128)), cv2.COLOR_BGR2GRAY)
        b_g = cv2.cvtColor(cv2.resize(extracted, (128, 128)),
                           cv2.COLOR_BGR2GRAY)
        ssim_quick = float(ssim(a_g, b_g, data_range=255))

        candidates.append({"idx": i, "ssim_quick": ssim_quick,
                          "extracted": extracted, "method": method})

    # STEP 3: NN scoring only for top candidates (saves EfficientNet passes)
    valid = [c for c in candidates if c["extracted"] is not None]
    valid.sort(key=lambda c: c["ssim_quick"], reverse=True)

    TOP_N = min(3, len(valid))
    for c in valid[:TOP_N]:
        crop = _crop_zone(ref_img, zones[c["idx"]])
        if NN_AVAILABLE:
            c["score"] = similarity_nn(crop, c["extracted"])
        else:
            c["score"] = c["ssim_quick"]

    for c in valid[TOP_N:]:
        c["score"] = c["ssim_quick"] * 0.5
    for c in candidates:
        if "score" not in c:
            c["score"] = 0.0

    # STEP 4: Best candidate (skip zones already checked OK)
    ok_zones = {idx for idx, info in s["checked"].items()
                if (info.get("defect_info") or {}).get("status") == "ok"}
    scores = [0.0] * len(zones)
    for c in candidates:
        scores[c["idx"]] = c["score"]

    active = [c for c in candidates if c["idx"] not in ok_zones]
    if not active:
        active = candidates  # fallback: all zones rechecked

    best = max(active, key=lambda c: c["score"])
    best_score = best["score"]

    THRESHOLD = 0.55
    matched = best_score >= THRESHOLD and best["extracted"] is not None

    if photo_img is not None:
        # Mobile photo — encode from memory
        _, buf = cv2.imencode(".jpg", photo)
        photo_b64 = base64.b64encode(buf.tobytes()).decode()
    else:
        photo_b64 = _file_to_b64(photo_path)

    defect_info = None
    subzone_results = []
    if matched:
        best_crop = _crop_zone(ref_img, zones[best["idx"]])
        extracted = best["extracted"]
        defect_info = _analyze_defects(best_crop, extracted, best_score)

        # ── Подзоны: жёсткий анализ критических областей внутри зоны ──
        subzones = zones[best["idx"]].get("subzones", [])
        for szi, sz in enumerate(subzones):
            sz_ref = _crop_zone(best_crop, sz)
            sz_ext = _crop_zone(extracted, sz)
            if sz_ref.size == 0 or sz_ext.size == 0:
                continue
            # Приведём к одному размеру
            sh, sw = sz_ref.shape[:2]
            sz_ext = cv2.resize(sz_ext, (sw, sh))
            sz_defect = _analyze_defects(sz_ref, sz_ext, best_score,
                                         strict=True)
            subzone_results.append({
                "index": szi,
                "label": sz.get("label", f"Подзона {szi + 1}"),
                "status": sz_defect["status"],
                "verdict": sz_defect["verdict"],
                "defect_pct": sz_defect["defect_pct"],
                "ssim": sz_defect["ssim"],
                "vis_defects_b64": sz_defect["vis_defects_b64"],
                "vis_heatmap_b64": sz_defect["vis_heatmap_b64"],
                "extracted_b64": sz_defect["extracted_b64"],
                "reference_b64": sz_defect["reference_b64"],
            })

        # Итоговый статус зоны = худший из (зона, подзоны)
        if subzone_results:
            worst = defect_info["status"]
            status_rank = {"ok": 0, "warn": 1, "defect": 2}
            for sr in subzone_results:
                if status_rank.get(sr["status"], 0) > status_rank.get(worst, 0):
                    worst = sr["status"]
            if worst != defect_info["status"]:
                defect_info["status"] = worst
                bad_sz = next(sr for sr in subzone_results
                              if sr["status"] == worst)
                defect_info["verdict"] = (
                    f"{bad_sz['verdict']} "
                    f"(подзона: {bad_sz['label']})")

        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        _tz8 = _tz(_td(hours=8))
        s["checked"][best["idx"]] = {
            "score": best_score,
            "photo_b64": photo_b64,
            "checked_at": _dt.now(_tz8).strftime("%Y-%m-%dT%H:%M:%S.%f"),
            "operator": _check_auth() or s.get("operator", ""),
            "zone_sensitivity": zone_sens,
            "subzone_sensitivity": subzone_sens,
            "defect_info": {
                "status": defect_info["status"],
                "defect_pct": defect_info["defect_pct"],
                "verdict": defect_info["verdict"],
            },
            "subzone_results": subzone_results,
        }

    total = len(zones)
    done = len(s["checked"])

    result = {
        "matched": matched,
        "best_zone_index": best["idx"],
        "best_zone_label": zones[best["idx"]]["label"],
        "best_score": best_score,
        "all_scores": scores,
        "photo_b64": photo_b64,
        "progress": {"done": done, "total": total, "complete": done >= total},
        "checked_zones": list(s["checked"].keys()),
    }

    if defect_info:
        result["defect"] = {
            "status": defect_info["status"],
            "verdict": defect_info["verdict"],
            "ssim": defect_info["ssim"],
            "defect_pct": defect_info["defect_pct"],
            "defect_count": defect_info["defect_count"],
            "vis_defects_b64": defect_info["vis_defects_b64"],
            "vis_heatmap_b64": defect_info["vis_heatmap_b64"],
            "extracted_b64": defect_info["extracted_b64"],
            "zone_sensitivity": zone_sens,
            "subzone_sensitivity": subzone_sens,
        }
        if subzone_results:
            result["defect"]["subzones"] = subzone_results

    return jsonify(result)


@app.route("/api/session/<sid>/status", methods=["GET"])
def session_status(sid):
    """Текущий прогресс сессии."""
    if sid not in sessions:
        return jsonify({"error": "Сессия не найдена"}), 404
    s = sessions[sid]
    return jsonify({
        "zones": [z["label"] for z in s["zones"]],
        "checked": {str(k): v["score"] for k, v in s["checked"].items()},
        "progress": {
            "done": len(s["checked"]),
            "total": len(s["zones"]),
            "complete": len(s["checked"]) >= len(s["zones"]),
        },
    })


@app.route("/api/session/<sid>/auto_accept", methods=["POST"])
def set_auto_accept(sid):
    """Store auto-accept flag so mobile companion knows whether to auto-advance."""
    if sid not in sessions:
        return jsonify({"error": "Сессия не найдена"}), 404
    data = request.get_json(silent=True) or {}
    sessions[sid]["_auto_accept"] = bool(data.get("auto_accept", True))
    return jsonify({"status": "ok"})


@app.route("/api/session/<sid>/reset", methods=["POST"])
def reset_session(sid):
    """Сбросить проверку (зоны остаются)."""
    if sid not in sessions:
        return jsonify({"error": "Сессия не найдена"}), 404
    sessions[sid]["checked"] = {}
    sessions[sid].pop("serial", None)
    sessions[sid].pop("serial_type", None)
    # Increment board sequence so mobile detects new-board transition
    sessions[sid]["_board_seq"] = sessions[sid].get("_board_seq", 0) + 1
    return jsonify({"status": "ok"})


@app.route("/api/session/<sid>/retry_failed", methods=["POST"])
def session_retry_failed(sid):
    """Clear only non-OK zones so they can be rescanned (no board_seq bump)."""
    if sid not in sessions:
        return jsonify({"error": "Сессия не найдена"}), 404
    checked = sessions[sid].get("checked", {})
    cleared = []
    for key in list(checked.keys()):
        info = checked[key]
        di = info.get("defect_info", {}) if isinstance(info, dict) else {}
        if di.get("status") != "ok":
            del checked[key]
            cleared.append(key)
    return jsonify({"status": "ok", "cleared": len(cleared)})


RESULTS_DIR = BASE_DIR / "inspection_results"
RESULTS_DIR.mkdir(exist_ok=True)


# ─── Shared: save inspection record (MongoDB + R2 images) ─────────────────────

def _save_inspection_record(s, sid, user_decisions=None, user_sub_decisions=None):
    """Build record from session, save metadata to MongoDB, images to R2.
    Returns (record dict, result_id)."""
    if user_decisions is None:
        user_decisions = {}
    if user_sub_decisions is None:
        user_sub_decisions = {}
    from datetime import datetime, timezone, timedelta
    _tz8 = timezone(timedelta(hours=8))
    ts = datetime.now(_tz8)
    rid = sid[:8] + "_" + ts.strftime("%H%M%S")
    prefix = f"results/{rid}/"

    record = {
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%f"),
        "result_id": rid,
        "session_id": sid,
        "template_id": s.get("template_id", ""),
        "template_name": s.get("template_name", ""),
        "barcode_mask": s.get("barcode_mask", ""),
        "serial": s.get("serial", ""),
        "serial_type": s.get("serial_type", ""),
        "operator": s.get("operator", ""),
        "zones_total": len(s["zones"]),
        "zones_checked": len(s["checked"]),
        "zones": [],
        "overall_status": "ok",
    }
    for i, z in enumerate(s["zones"]):
        info = s["checked"].get(i, {})
        di = info.get("defect_info", {})
        zone_rec = {
            "label": z["label"],
            "status": di.get("status", "unchecked"),
            "score": info.get("score", 0),
            "defect_pct": di.get("defect_pct", 0),
            "checked_at": info.get("checked_at", ""),
            "operator": info.get("operator", s.get("operator", "")),
            "zone_sensitivity": info.get("zone_sensitivity"),
            "subzone_sensitivity": info.get("subzone_sensitivity"),
        }
        # User decision override
        if i in user_decisions:
            zone_rec["user_decision"] = user_decisions[i]
        # Upload zone photo to R2
        photo_b64 = info.get("photo_b64")
        if photo_b64:
            r2.upload_bytes(
                prefix + f"zone_{i}.jpg", base64.b64decode(photo_b64), "image/jpeg")
            zone_rec["image"] = f"zone_{i}.jpg"

        # Upload subzone images to R2 and store subzone metadata
        sz_results = info.get("subzone_results", [])
        if sz_results:
            sz_recs = []
            for szi, sr in enumerate(sz_results):
                sz_rec = {
                    "label": sr.get("label", f"S{szi+1}"),
                    "status": sr.get("status", "unchecked"),
                    "defect_pct": sr.get("defect_pct", 0),
                    "ssim": sr.get("ssim", 0),
                }
                vis_b64 = sr.get("vis_defects_b64")
                if vis_b64:
                    fname = f"zone_{i}_sub_{szi}_defects.jpg"
                    r2.upload_bytes(
                        prefix + fname,
                        base64.b64decode(vis_b64), "image/jpeg")
                    sz_rec["image_defects"] = fname
                heat_b64 = sr.get("vis_heatmap_b64")
                if heat_b64:
                    fname = f"zone_{i}_sub_{szi}_heatmap.jpg"
                    r2.upload_bytes(
                        prefix + fname,
                        base64.b64decode(heat_b64), "image/jpeg")
                    sz_rec["image_heatmap"] = fname
                ext_b64 = sr.get("extracted_b64")
                if ext_b64:
                    fname = f"zone_{i}_sub_{szi}_extracted.jpg"
                    r2.upload_bytes(
                        prefix + fname,
                        base64.b64decode(ext_b64), "image/jpeg")
                    sz_rec["image_extracted"] = fname
                ref_b64 = sr.get("reference_b64")
                if ref_b64:
                    fname = f"zone_{i}_sub_{szi}_reference.jpg"
                    r2.upload_bytes(
                        prefix + fname,
                        base64.b64decode(ref_b64), "image/jpeg")
                    sz_rec["image_reference"] = fname
                # User sub-decision override
                if i in user_sub_decisions and szi in user_sub_decisions[i]:
                    sz_rec["user_decision"] = user_sub_decisions[i][szi]
                sz_recs.append(sz_rec)
            zone_rec["subzones"] = sz_recs

        record["zones"].append(zone_rec)
        # Effective status: user zone-level override > worst-of-subzone-overrides > auto
        status_rank = {"ok": 0, "warn": 1, "defect": 2}
        if i in user_decisions:
            effective = user_decisions[i]
        elif zone_rec.get("subzones"):
            worst = 0
            for sz_r in zone_rec["subzones"]:
                sz_eff = sz_r.get("user_decision") or sz_r.get(
                    "status", "unchecked")
                worst = max(worst, status_rank.get(sz_eff, 0))
            effective = {0: "ok", 1: "warn", 2: "defect"}.get(
                worst, di.get("status", "unchecked"))
        else:
            effective = di.get("status", "unchecked")
        if effective == "defect":
            record["overall_status"] = "defect"
        elif effective == "warn" and record["overall_status"] == "ok":
            record["overall_status"] = "warn"

    # Upload reference crop per zone to R2
    ref_img = s.get("ref_img")
    if ref_img is not None:
        for i, z in enumerate(s["zones"]):
            crop = _crop_zone(ref_img, z)
            if crop is not None:
                _, buf = cv2.imencode(
                    ".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
                r2.upload_bytes(
                    prefix + f"ref_{i}.jpg", buf.tobytes(), "image/jpeg")

    # Save metadata: MongoDB (primary) with R2 fallback
    if MONGO_AVAILABLE:
        _results_col.replace_one(
            {"result_id": rid}, record, upsert=True)
    else:
        r2.upload_json(prefix + "meta.json", record)

    # Append to daily log (R2)
    day = ts.strftime("%Y-%m-%d")
    r2.append_line(f"logs/log_{day}.jsonl",
                   json.dumps(record, ensure_ascii=False))

    return record, rid


@app.route("/api/session/<sid>/save_result", methods=["POST"])
def save_result(sid):
    """Save inspection result."""
    if sid not in sessions:
        return jsonify({"error": "Session not found"}), 404
    user_decisions = {}
    user_sub_decisions = {}
    if request.is_json and request.json:
        raw = request.json.get("user_decisions", {})
        allowed = {"ok", "warn", "defect"}
        for k, v in raw.items():
            if str(v) in allowed:
                user_decisions[int(k)] = str(v)
        raw_sub = request.json.get("user_sub_decisions", {})
        for zk, subs in raw_sub.items():
            zi = int(zk)
            user_sub_decisions[zi] = {}
            for sk, sv in subs.items():
                if str(sv) in allowed:
                    user_sub_decisions[zi][int(sk)] = str(sv)
    else:
        app.logger.info(
            "[SAVE_RESULT] No JSON body received, is_json=%s", request.is_json)
    app.logger.info("[SAVE_RESULT] sid=%s user_decisions=%s user_sub_decisions=%s",
                    sid[:8], user_decisions, user_sub_decisions)
    record, rid = _save_inspection_record(
        sessions[sid], sid, user_decisions=user_decisions,
        user_sub_decisions=user_sub_decisions)
    return jsonify({"status": "ok", "overall": record["overall_status"], "result_id": rid})


@app.route("/api/session/<sid>/update_result", methods=["POST"])
def update_result(sid):
    """Update user decisions on an existing saved result (in-place)."""
    if not request.is_json or not request.json:
        return jsonify({"error": "JSON body required"}), 400
    rid = request.json.get("result_id")
    if not rid:
        return jsonify({"error": "result_id required"}), 400
    if not MONGO_AVAILABLE:
        return jsonify({"error": "MongoDB unavailable"}), 503

    existing = _results_col.find_one({"result_id": rid})
    if not existing:
        return jsonify({"error": "Record not found"}), 404

    allowed = {"ok", "warn", "defect"}
    raw = request.json.get("user_decisions", {})
    user_decisions = {}
    for k, v in raw.items():
        if str(v) in allowed:
            user_decisions[int(k)] = str(v)
    raw_sub = request.json.get("user_sub_decisions", {})
    user_sub_decisions = {}
    for zk, subs in raw_sub.items():
        zi = int(zk)
        user_sub_decisions[zi] = {}
        for sk, sv in subs.items():
            if str(sv) in allowed:
                user_sub_decisions[zi][int(sk)] = str(sv)
    app.logger.info("[UPDATE_RESULT] rid=%s user_decisions=%s user_sub_decisions=%s",
                    rid, user_decisions, user_sub_decisions)

    # Update zones in-place
    zones = existing.get("zones", [])
    overall = "ok"
    status_rank = {"ok": 0, "warn": 1, "defect": 2}
    for i, z in enumerate(zones):
        if i in user_decisions:
            z["user_decision"] = user_decisions[i]
        elif "user_decision" in z:
            del z["user_decision"]
        # Update subzone decisions
        for si, sz in enumerate(z.get("subzones", [])):
            if i in user_sub_decisions and si in user_sub_decisions[i]:
                sz["user_decision"] = user_sub_decisions[i][si]
            elif "user_decision" in sz:
                del sz["user_decision"]
        # Recompute zone effective status:
        # 1) Explicit zone-level override takes precedence
        # 2) Otherwise, worst of effective subzone statuses (if subzones exist)
        # 3) Otherwise, calculated zone status
        if i in user_decisions:
            eff = user_decisions[i]
        elif z.get("subzones"):
            worst = 0
            for sz in z["subzones"]:
                sz_eff = sz.get("user_decision") or sz.get(
                    "status", "unchecked")
                worst = max(worst, status_rank.get(sz_eff, 0))
            eff = {0: "ok", 1: "warn", 2: "defect"}.get(
                worst, z.get("status", "unchecked"))
        else:
            eff = z.get("status", "unchecked")
        if eff == "defect":
            overall = "defect"
        elif eff == "warn" and overall == "ok":
            overall = "warn"

    _results_col.update_one({"result_id": rid}, {"$set": {
        "zones": zones,
        "overall_status": overall,
    }})
    return jsonify({"status": "ok", "overall": overall, "result_id": rid})


@app.route("/api/results")
def list_results():
    """List saved inspection results (most recent first).
    Supports ?limit=N&offset=M for pagination and ?q= for search.
    Search query parts separated by / filter: serial / template / operator."""
    import re as _re
    limit = request.args.get("limit", type=int)
    offset = request.args.get("offset", 0, type=int)
    search = (request.args.get("q") or "").strip()
    if not MONGO_AVAILABLE:
        return jsonify({"error": "MongoDB unavailable"}), 503

    mongo_filter = {}
    if search:
        parts = [p.strip() for p in search.split("/")]
        conditions = []
        # Part 1 → serial, Part 2 → template_name, Part 3 → operator
        field_map = ["serial", "template_name", "operator"]
        for i, part in enumerate(parts):
            if not part or part == "*":
                continue
            # Convert wildcard * to .* while escaping the rest
            segments = part.split("*")
            escaped = ".*".join(_re.escape(s) for s in segments)
            if i < len(field_map):
                conditions.append(
                    {field_map[i]: {"$regex": escaped, "$options": "i"}})
            else:
                # Extra parts → search zones labels
                conditions.append(
                    {"zones.label": {"$regex": escaped, "$options": "i"}})
        if conditions:
            mongo_filter = {"$and": conditions} if len(
                conditions) > 1 else conditions[0]

    total = _results_col.count_documents(mongo_filter)
    cursor = _results_col.find(mongo_filter, {"_id": 0}).sort("timestamp", -1)
    if offset:
        cursor = cursor.skip(offset)
    if limit:
        cursor = cursor.limit(limit)
    results = list(cursor)
    return jsonify({"results": results, "total": total})


@app.route("/api/results", methods=["DELETE"])
def clear_results():
    """Delete all saved inspection results."""
    deleted = 0
    if MONGO_AVAILABLE:
        res = _results_col.delete_many({})
        deleted = res.deleted_count
    count = r2.delete_prefix("results/")
    return jsonify({"deleted": deleted or count})


@app.route("/api/results/<rid>/image/<fname>")
def result_image(rid, fname):
    """Serve a saved result image from R2."""
    import re
    if not re.match(r'^[a-zA-Z0-9_\-]+$', rid) or not re.match(r'^[a-zA-Z0-9_\-]+\.jpg$', fname):
        return jsonify({"error": "invalid"}), 400
    data = r2.download_bytes(f"results/{rid}/{fname}")
    if not data:
        return jsonify({"error": "not found"}), 404
    resp = send_file(io.BytesIO(data), mimetype="image/jpeg")
    return resp


# ─── Template management (MongoDB + R2 images) ───────────────────────────────

def _r2_tpl_prefix(tid: str) -> str:
    return f"templates/{tid}/"


@app.route("/api/templates", methods=["GET"])
def list_templates():
    """Список сохранённых шаблонов."""
    if not MONGO_AVAILABLE:
        return jsonify({"error": "MongoDB unavailable"}), 503
    docs = _templates_col.find({}, {"_id": 0, "id": 1, "name": 1, "zones": 1,
                                    "created": 1, "barcode_mask": 1, "version": 1})
    templates = []
    for d in docs:
        templates.append({
            "id": d["id"],
            "name": d["name"],
            "zone_count": len(d.get("zones", [])),
            "created": d.get("created", ""),
            "barcode_mask": d.get("barcode_mask", ""),
            "version": d.get("version", 1),
        })
    templates.sort(key=lambda t: t.get("created", ""))
    return jsonify({"templates": templates})


@app.route("/api/templates", methods=["POST"])
def save_template():
    """Сохранить текущую сессию (изображение + зоны) как шаблон."""
    if not MONGO_AVAILABLE:
        return jsonify({"error": "MongoDB unavailable"}), 503
    data = request.get_json(silent=True)
    if not data or "session_id" not in data or "name" not in data:
        return jsonify({"error": "Нужны session_id и name"}), 400

    sid = data["session_id"]
    name = data["name"].strip()
    if not name:
        return jsonify({"error": "Имя шаблона не может быть пустым"}), 400
    if sid not in sessions:
        return jsonify({"error": "Сессия не найдена"}), 404

    s = sessions[sid]

    if "zones" in data and isinstance(data["zones"], list) and len(data["zones"]) > 0:
        s["zones"] = data["zones"]

    if not s["zones"]:
        return jsonify({"error": "Зоны не заданы"}), 400

    import datetime
    tid = uuid.uuid4().hex[:10]
    prefix = _r2_tpl_prefix(tid)

    # Upload reference image to R2
    ref_name = "ref" + s["ref_path"].suffix
    with open(s["ref_path"], "rb") as f:
        r2.upload_bytes(prefix + ref_name, f.read(), "image/jpeg")

    # Save metadata to MongoDB
    meta = {
        "id": tid,
        "name": name,
        "barcode_mask": data.get("barcode_mask") or "",
        "ref_image": ref_name,
        "zones": s["zones"],
        "version": 1,
        "created": datetime.datetime.now().isoformat(),
        "versions": [],
    }
    _templates_col.insert_one(meta)

    return jsonify({"status": "ok", "template_id": tid})


@app.route("/api/templates/<tid>", methods=["GET"])
def load_template(tid):
    """Загрузить шаблон → создать сессию с готовыми зонами."""
    if not MONGO_AVAILABLE:
        return jsonify({"error": "MongoDB unavailable"}), 503
    meta = _templates_col.find_one({"id": tid}, {"_id": 0})
    if not meta:
        return jsonify({"error": "Шаблон не найден"}), 404

    ref_bytes = r2.download_bytes(f"templates/{tid}/{meta['ref_image']}")
    if not ref_bytes:
        return jsonify({"error": "Не удалось открыть референс шаблона"}), 500

    ref_img = cv2.imdecode(np.frombuffer(
        ref_bytes, np.uint8), cv2.IMREAD_COLOR)
    if ref_img is None:
        return jsonify({"error": "Не удалось декодировать референс"}), 500

    zones = meta["zones"]
    if meta.get("anchors") and not any(z.get("anchors") for z in zones):
        zones[0]["anchors"] = meta["anchors"][:2]

    # Save ref image to temp file for session (needed by other code paths)
    tmp_ref = UPLOAD_DIR / f"{tid}_ref.jpg"
    tmp_ref.write_bytes(ref_bytes)

    sid = uuid.uuid4().hex[:12]
    sessions[sid] = {
        "ref_path": tmp_ref,
        "ref_img": ref_img,
        "zones": zones,
        "checked": {},
        "template_id": tid,
        "template_name": meta["name"],
        "barcode_mask": meta.get("barcode_mask", ""),
        "operator": _check_auth() or "",
        "_created": time.time(),
        "_last_active": time.time(),
    }
    _precompute_anchor_crops(sid, ref_img, zones)

    zone_previews = []
    for z in zones:
        crop = _crop_zone(ref_img, z)
        zone_previews.append(_img_to_b64(crop))

    h, w = ref_img.shape[:2]
    ref_b64 = base64.b64encode(ref_bytes).decode()
    return jsonify({
        "session_id": sid,
        "template_name": meta["name"],
        "barcode_mask": meta.get("barcode_mask", ""),
        "version": meta.get("version", 1),
        "image_b64": ref_b64,
        "width": w,
        "height": h,
        "zones": zones,
        "previews": zone_previews,
    })


@app.route("/api/templates/<tid>", methods=["DELETE"])
def delete_template(tid):
    """Удалить шаблон."""
    if not MONGO_AVAILABLE:
        return jsonify({"error": "MongoDB unavailable"}), 503
    res = _templates_col.delete_one({"id": tid})
    if res.deleted_count == 0:
        return jsonify({"error": "Шаблон не найден"}), 404
    # Also delete images from R2
    r2.delete_prefix(_r2_tpl_prefix(tid))
    return jsonify({"status": "ok"})


@app.route("/api/templates/<tid>", methods=["PUT"])
def update_template(tid):
    """Update template (new version): archive old version, overwrite with new data."""
    if not MONGO_AVAILABLE:
        return jsonify({"error": "MongoDB unavailable"}), 503
    import datetime
    old_meta = _templates_col.find_one({"id": tid}, {"_id": 0})
    if not old_meta:
        return jsonify({"error": "Шаблон не найден"}), 404

    data = request.get_json(silent=True) or {}
    old_version = old_meta.get("version", 1)
    new_version = old_version + 1

    # Archive old version as subdocument (without versions array)
    archived = {k: v for k, v in old_meta.items(
    ) if k not in ("_id", "versions")}

    # Update fields
    update_fields = {}
    sid = data.get("session_id")
    new_zones = data.get("zones")
    prefix = _r2_tpl_prefix(tid)

    if sid and sid in sessions and new_zones:
        update_fields["zones"] = new_zones
        s = sessions[sid]
        ref_name = "ref" + s["ref_path"].suffix
        with open(s["ref_path"], "rb") as f:
            r2.upload_bytes(prefix + ref_name, f.read(), "image/jpeg")
        update_fields["ref_image"] = ref_name
    elif new_zones:
        update_fields["zones"] = new_zones

    if "name" in data and data["name"].strip():
        update_fields["name"] = data["name"].strip()
    if "barcode_mask" in data:
        update_fields["barcode_mask"] = data["barcode_mask"] or ""

    update_fields["version"] = new_version
    update_fields["updated"] = datetime.datetime.now().isoformat()

    _templates_col.update_one({"id": tid}, {
        "$set": update_fields,
        "$push": {"versions": archived}
    })

    return jsonify({"status": "ok", "version": new_version})


@app.route("/api/templates/<tid>/versions", methods=["GET"])
def list_template_versions(tid):
    """List version history for a template."""
    if not MONGO_AVAILABLE:
        return jsonify({"error": "MongoDB unavailable"}), 503
    doc = _templates_col.find_one({"id": tid}, {"_id": 0, "name": 1, "version": 1,
                                                "created": 1, "updated": 1, "versions": 1})
    if not doc:
        return jsonify({"error": "Шаблон не найден"}), 404

    versions = [{"version": doc.get("version", 1),
                 "created": doc.get("updated", doc.get("created", "")),
                 "current": True}]
    for v in doc.get("versions", []):
        versions.append({"version": v.get("version", 1),
                         "created": v.get("created", ""), "current": False})
    versions.sort(key=lambda v: v["version"], reverse=True)
    return jsonify({"template_id": tid, "name": doc["name"], "versions": versions})


@app.route("/api/templates/<tid>/detail", methods=["GET"])
def template_detail(tid):
    """Return full template data (meta + reference image b64) for editing."""
    if not MONGO_AVAILABLE:
        return jsonify({"error": "MongoDB unavailable"}), 503
    meta = _templates_col.find_one({"id": tid}, {"_id": 0})
    if not meta:
        return jsonify({"error": "Шаблон не найден"}), 404

    prefix = _r2_tpl_prefix(tid)
    ref_bytes = r2.download_bytes(prefix + meta["ref_image"])
    if not ref_bytes:
        return jsonify({"error": "Не удалось открыть референс"}), 500

    ref_img = cv2.imdecode(np.frombuffer(
        ref_bytes, np.uint8), cv2.IMREAD_COLOR)
    if ref_img is None:
        return jsonify({"error": "Не удалось декодировать референс"}), 500

    h, w = ref_img.shape[:2]
    zone_previews = []
    for z in meta["zones"]:
        crop = _crop_zone(ref_img, z)
        zone_previews.append(_img_to_b64(crop))

    # Version history from embedded versions array
    versions_list = [{"version": meta.get("version", 1),
                      "date": meta.get("updated", meta.get("created", "")),
                      "current": True}]
    for v in meta.get("versions", []):
        versions_list.append({"version": v.get("version", 1),
                              "date": v.get("updated", v.get("created", "")),
                              "current": False,
                              "name": v.get("name", ""),
                              "zone_count": len(v.get("zones", []))})
    versions_list.sort(key=lambda v: v["version"], reverse=True)

    return jsonify({
        "id": tid,
        "name": meta["name"],
        "barcode_mask": meta.get("barcode_mask", ""),
        "version": meta.get("version", 1),
        "created": meta.get("created", ""),
        "updated": meta.get("updated", ""),
        "zones": meta["zones"],
        "image_b64": base64.b64encode(ref_bytes).decode(),
        "width": w,
        "height": h,
        "previews": zone_previews,
        "versions": versions_list,
    })


@app.route("/api/templates/<tid>/restore/<int:ver>", methods=["POST"])
def restore_template_version(tid, ver):
    """Restore a template to a specific version (creating a new version)."""
    if not MONGO_AVAILABLE:
        return jsonify({"error": "MongoDB unavailable"}), 503
    import datetime
    doc = _templates_col.find_one({"id": tid}, {"_id": 0})
    if not doc:
        return jsonify({"error": "Шаблон не найден"}), 404

    # Find target version in embedded versions array
    target = None
    for v in doc.get("versions", []):
        if v.get("version") == ver:
            target = v
            break
    if not target:
        return jsonify({"error": f"Версия v{ver} не найдена"}), 404

    old_version = doc.get("version", 1)
    new_version = old_version + 1

    # Archive current as subdocument
    archived = {k: v for k, v in doc.items() if k not in ("_id", "versions")}

    # Restore target fields but bump version
    restored = {k: v for k, v in target.items()}
    restored["version"] = new_version
    restored["updated"] = datetime.datetime.now().isoformat()
    restored["id"] = tid

    # Check reference image exists in R2
    prefix = _r2_tpl_prefix(tid)
    ref_name = restored.get("ref_image", doc.get("ref_image"))
    if not r2.key_exists(prefix + ref_name):
        ref_name = doc.get("ref_image")
    restored["ref_image"] = ref_name

    _templates_col.update_one({"id": tid}, {
        "$set": {k: v for k, v in restored.items()},
        "$push": {"versions": archived}
    })

    return jsonify({"status": "ok", "version": new_version})


@app.route("/api/admin/migrate-templates", methods=["POST"])
def migrate_templates_to_mongo():
    """One-time migration: move template JSONs from R2 to MongoDB."""
    if not MONGO_AVAILABLE:
        return jsonify({"error": "MongoDB unavailable"}), 503

    all_keys = r2.list_keys("templates/")
    meta_keys = [k for k in all_keys if k.endswith("/meta.json")]
    migrated = 0
    skipped = 0

    for mk in meta_keys:
        meta = r2.download_json(mk)
        if not meta:
            continue
        tid = meta.get("id")
        if not tid:
            continue

        # Check if already in MongoDB
        if _templates_col.find_one({"id": tid}):
            skipped += 1
            continue

        # Collect version history from R2
        prefix = _r2_tpl_prefix(tid)
        ver_keys = [k for k in r2.list_keys(
            prefix + "versions/") if k.endswith(".json")]
        versions = []
        for vk in sorted(ver_keys):
            vm = r2.download_json(vk)
            if vm:
                versions.append(vm)

        meta["versions"] = versions
        meta.pop("_id", None)
        _templates_col.insert_one(meta)
        migrated += 1

    return jsonify({"status": "ok", "migrated": migrated, "skipped": skipped})


# ─── Mobile Camera ────────────────────────────────────────────────────────────

@app.route("/api/session/<sid>/mobile_qr", methods=["POST"])
def generate_mobile_qr(sid):
    """Сгенерировать QR-код для мобильной камеры."""
    if sid not in sessions:
        return jsonify({"error": "Сессия не найдена"}), 404

    # Generate a fresh token for each new session so the displayed URL changes,
    # but keep old tokens alive (remapped) so mobile phones don't need to re-scan.
    with _MOBILE_TOKENS_LOCK:
        tokens = _load_mobile_tokens()
        existing = next((t for t, s in tokens.items() if s == sid), None)
        if existing:
            token = existing
        else:
            # Remap old tokens to new session (backward compat for mobile)
            for t in list(tokens):
                if tokens[t] != sid:
                    tokens[t] = sid
            # Always generate a fresh token so the URL visually changes
            token = secrets.token_urlsafe(16)
            tokens[token] = sid
            # Cap at 5 tokens max — remove oldest (first inserted) ones
            while len(tokens) > 5:
                oldest = next(iter(tokens))
                del tokens[oldest]
            _save_mobile_tokens(tokens)

    if sid not in mobile_photos:
        mobile_photos[sid] = []

    # Build public URL — prefer BASE_URL env, then X-Forwarded headers
    configured_url = app.config.get("BASE_URL", "")
    if configured_url:
        base_url = configured_url.rstrip("/")
    else:
        host = request.headers.get("X-Forwarded-Host", request.host)
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
        base_url = f"{scheme}://{host}"

    url = f"{base_url}/mobile?token={token}"

    # Generate QR as base64 PNG
    qr = qrcode.QRCode(
        version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="white", back_color="#1a1a2e")
    buf = io.BytesIO()
    qr_img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    return jsonify({
        "qr_b64": qr_b64,
        "url": url,
        "token": token,
    })


@app.route("/mobile")
def mobile_camera_page():
    """Serve the mobile camera page."""
    resp = make_response(render_template("mobile_camera.html"))
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/mobile/<token>/info")
def mobile_info(token):
    """Get session info for a mobile token."""
    sid = _get_token_session(token)
    if not sid or sid not in sessions:
        return jsonify({"error": "Недействительная ссылка"}), 404

    sess = sessions[sid]
    zones = sess.get("zones", [])
    checked = sess.get("checked", {})
    # Build per-zone info with anchors and status
    zones_info = []
    has_issues = False
    for i, z in enumerate(zones):
        is_checked = i in checked or str(i) in checked
        info = checked.get(i, checked.get(str(i), {}))
        di = info.get("defect_info", {}) if isinstance(info, dict) else {}
        status = di.get("status", "unchecked") if is_checked else "unchecked"
        if status in ("warn", "defect"):
            has_issues = True
        zones_info.append({
            "label": z.get("label", f"Zone {i+1}"),
            "anchors": z.get("anchors", []),
            "checked": is_checked,
            "status": status,
        })
    # Determine inspection state
    all_checked = len(checked) >= len(zones) and len(zones) > 0
    auto_accept = sess.get("_auto_accept", True)
    if not all_checked:
        inspection_state = "scanning"
    elif has_issues:
        inspection_state = "waiting_decision"
    elif not auto_accept:
        inspection_state = "waiting_decision"
    else:
        inspection_state = "complete"
    return jsonify({
        "session_id": sid,
        "serial": sess.get("serial", ""),
        "zones_total": len(zones),
        "zones_checked": len(checked),
        "zones": zones_info,
        "inspection_state": inspection_state,
        "board_seq": sess.get("_board_seq", 0),
    })


@app.route("/api/mobile/<token>/detect_anchors", methods=["POST"])
def mobile_detect_anchors(token):
    """Detect anchor points in a camera frame via multi-scale template matching."""
    sid = _get_token_session(token)
    if not sid or sid not in sessions:
        return jsonify({"error": "invalid"}), 404

    sess = sessions[sid]
    zone_index = request.form.get("zone_index", type=int)
    all_crops = sess.get("_anchor_crops", {})
    if zone_index is None or zone_index not in all_crops:
        return jsonify({"anchors": []})
    crops = all_crops[zone_index]
    if not crops:
        return jsonify({"anchors": []})

    file = request.files.get("photo")
    if not file:
        return jsonify({"anchors": []})

    data = np.frombuffer(file.read(), np.uint8)
    frame = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if frame is None:
        return jsonify({"anchors": []})

    # Downscale frame for speed
    fh, fw = frame.shape[:2]
    max_dim = 640
    f_scale = min(max_dim / max(fh, fw), 1.0)
    if f_scale < 1.0:
        frame_small = cv2.resize(frame, None, fx=f_scale, fy=f_scale)
    else:
        frame_small = frame

    frame_gray = cv2.cvtColor(frame_small, cv2.COLOR_BGR2GRAY)
    sfh, sfw = frame_gray.shape[:2]

    found = []
    for i, crop in enumerate(crops):
        if crop is None or crop.size == 0:
            found.append(None)
            continue
        crop_gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        best_val, best_cx, best_cy = 0.0, 0.5, 0.5
        # Multi-scale search
        for scale in [0.3, 0.5, 0.7, 0.9, 1.1, 1.4, 1.8]:
            tw = int(crop_gray.shape[1] * scale)
            th = int(crop_gray.shape[0] * scale)
            if tw < 16 or th < 16 or tw >= sfw or th >= sfh:
                continue
            tmpl = cv2.resize(crop_gray, (tw, th))
            res = cv2.matchTemplate(frame_gray, tmpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val > best_val:
                best_val = max_val
                best_cx = (max_loc[0] + tw / 2) / sfw
                best_cy = (max_loc[1] + th / 2) / sfh
        if best_val > 0.35:
            found.append({"x": round(best_cx, 4), "y": round(best_cy, 4),
                          "score": round(best_val, 3)})
        else:
            found.append(None)

    return jsonify({"anchors": found})


@app.route("/api/mobile/<token>/serial", methods=["POST"])
def mobile_serial(token):
    """Upload serial number photo from mobile."""
    sid = _get_token_session(token)
    if not sid or sid not in sessions:
        return jsonify({"error": "Недействительная ссылка"}), 404

    file = request.files.get("photo")
    if not file:
        return jsonify({"error": "Фото не найдено"}), 400

    data = np.frombuffer(file.read(), np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        return jsonify({"error": "Не удалось прочитать изображение"}), 400

    codes = detect_codes(img, quick=False)
    if not codes:
        return jsonify({"error": "Code not found. Try closer or sharper."}), 422

    best = next((c for c in codes if c["data"]), codes[0])
    serial = best["data"] or "не распознано"
    sessions[sid]["serial"] = serial
    sessions[sid]["serial_type"] = best["type"]

    return jsonify({"serial": serial, "type": best["type"]})


@app.route("/api/mobile/<token>/retry_failed", methods=["POST"])
def mobile_retry_failed(token):
    """Clear non-OK zones from checked so they can be rescanned."""
    sid = _get_token_session(token)
    if not sid or sid not in sessions:
        return jsonify({"error": "Недействительная ссылка"}), 404

    sess = sessions[sid]
    checked = sess.get("checked", {})
    cleared = []
    for key in list(checked.keys()):
        info = checked[key]
        di = info.get("defect_info", {}) if isinstance(info, dict) else {}
        if di.get("status") != "ok":
            del checked[key]
            cleared.append(key)
    return jsonify({"status": "ok", "cleared": len(cleared)})


@app.route("/api/mobile/<token>/skip", methods=["POST"])
def mobile_skip_board(token):
    """Save current result and clear session for next board."""
    sid = _get_token_session(token)
    if not sid or sid not in sessions:
        return jsonify({"error": "invalid"}), 404

    s = sessions[sid]
    # Save result if any zones were checked
    saved = False
    if s.get("checked"):
        _save_inspection_record(s, sid)
        saved = True

    # Clear checked zones + serial for next board
    s["checked"] = {}
    s.pop("serial", None)
    s.pop("serial_type", None)

    # Increment board sequence so mobile can detect skip reliably
    s["_board_seq"] = s.get("_board_seq", 0) + 1

    return jsonify({"status": "ok", "saved": saved})


@app.route("/api/mobile/<token>/zone_photo", methods=["POST"])
def mobile_zone_photo(token):
    """Upload a zone photo from mobile → stored for desktop to pick up."""
    sid = _get_token_session(token)
    if not sid or sid not in sessions:
        return jsonify({"error": "Недействительная ссылка"}), 404

    file = request.files.get("photo")
    if not file:
        return jsonify({"error": "Фото не найдено"}), 400

    photo_bytes = file.read()
    if sid not in mobile_photos:
        mobile_photos[sid] = []
    mobile_photos[sid].append(photo_bytes)

    return jsonify({"status": "ok", "count": len(mobile_photos[sid])})


@app.route("/api/session/<sid>/mobile_photos", methods=["GET"])
def get_mobile_photos(sid):
    """Desktop polls this to check for new photos from mobile."""
    if sid not in sessions:
        return jsonify({"error": "Сессия не найдена"}), 404

    photos = mobile_photos.get(sid, [])
    serial = sessions[sid].get("serial", "")
    serial_type = sessions[sid].get("serial_type", "")

    return jsonify({
        "count": len(photos),
        "serial": serial,
        "serial_type": serial_type,
    })


@app.route("/api/session/<sid>/mobile_photos/next", methods=["POST"])
def pop_mobile_photo(sid):
    """Desktop fetches the next mobile photo (FIFO) and processes it as a zone check."""
    if sid not in sessions:
        return jsonify({"error": "Сессия не найдена"}), 404

    photos = mobile_photos.get(sid, [])
    if not photos:
        return jsonify({"error": "Нет фото в очереди"}), 404

    photo_bytes = photos.pop(0)
    img = cv2.imdecode(np.frombuffer(photo_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jsonify({"error": "Не удалось прочитать изображение"}), 400

    # Downscale large mobile photos to match reference size (speed up SIFT)
    ref_img = sessions[sid]["ref_img"]
    ref_h, ref_w = ref_img.shape[:2]
    h, w = img.shape[:2]
    max_dim = max(ref_h, ref_w) * 2  # Allow up to 2x reference size
    if max(h, w) > max_dim:
        scale = max_dim / max(h, w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)))

    return _check_zone_impl(sid, photo_img=img)


# ─── R2 Migration: upload existing local data ─────────────────────────────────

@app.route("/api/r2/migrate", methods=["POST"])
def r2_migrate():
    """Upload all local templates and results to R2 (one-time migration)."""
    uploaded = {"templates": 0, "results": 0, "logs": 0}

    # Migrate templates
    if TEMPLATE_DIR.exists():
        for tpl_dir in TEMPLATE_DIR.iterdir():
            if not tpl_dir.is_dir():
                continue
            tid = tpl_dir.name
            prefix = f"templates/{tid}/"
            for fp in tpl_dir.rglob("*"):
                if not fp.is_file():
                    continue
                key = prefix + str(fp.relative_to(tpl_dir))
                ct = "application/json" if fp.suffix == ".json" else "image/jpeg"
                r2.upload_bytes(key, fp.read_bytes(), ct)
                uploaded["templates"] += 1

    # Migrate results
    if RESULTS_DIR.exists():
        for item in RESULTS_DIR.iterdir():
            if item.is_dir():
                rid = item.name
                prefix = f"results/{rid}/"
                for fp in item.rglob("*"):
                    if not fp.is_file():
                        continue
                    key = prefix + str(fp.relative_to(item))
                    ct = "application/json" if fp.suffix == ".json" else "image/jpeg"
                    r2.upload_bytes(key, fp.read_bytes(), ct)
                    uploaded["results"] += 1
            elif item.suffix == ".jsonl":
                # Migrate log files
                key = f"logs/{item.name}"
                r2.upload_bytes(key, item.read_bytes(), "text/plain")
                uploaded["logs"] += 1

    return jsonify({"status": "ok", "uploaded": uploaded})


@app.route("/api/r2/status")
def r2_status():
    """Check R2 connectivity and list object counts."""
    try:
        tpl_keys = r2.list_keys("templates/")
        res_keys = r2.list_keys("results/")
        log_keys = r2.list_keys("logs/")
        return jsonify({
            "status": "connected",
            "templates_objects": len(tpl_keys),
            "results_objects": len(res_keys),
            "logs_objects": len(log_keys),
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import logging as _logging
    _logging.getLogger("werkzeug").setLevel(_logging.WARNING)
    _port = int(_os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=_port, debug=False)
