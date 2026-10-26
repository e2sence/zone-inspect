"""
Zone Inspect — visual zone-based inspection system.

Workflow:
  1. Upload a reference image of the assembly
  2. Define inspection zones (rectangles)
  3. Upload zone photos one by one — the system matches each photo to a zone
  4. All zones checked → done
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from flask import Flask, request, jsonify, render_template, send_file, make_response, redirect
from skimage.metrics import structural_similarity as ssim
from auto_blend import auto_blend_images, detect_codes
import r2_storage as r2
import recipes as recipes_mod

# ─── MongoDB ──────────────────────────────────────────────────────────────────
# MongoClient connects lazily and reconnects on its own, so availability is
# decided by configuration, not by a one-shot check at startup: a transient
# Atlas/network failure during boot must not leave the app without a DB until
# the next restart. Requests made during an outage fail; later ones recover.
try:
    from pymongo import MongoClient
    _MONGO_URI = _os.environ.get("MONGODB_URI", "")
    if _MONGO_URI:
        _mongo = MongoClient(_MONGO_URI, serverSelectionTimeoutMS=5000)
        _mongo_db = _mongo.get_default_database()
        _results_col = _mongo_db["inspection_results"]
        _templates_col = _mongo_db["templates"]
        _versions_col = _mongo_db["recipe_versions"]
        _samples_col = _mongo_db["samples"]
        MONGO_AVAILABLE = True
        try:
            _results_col.create_index("timestamp", unique=False)
            _results_col.create_index("result_id", unique=True)
            _templates_col.create_index("id", unique=True)
            _versions_col.create_index([("recipe_id", 1), ("version", 1)],
                                       unique=True)
            _samples_col.create_index("id", unique=True)
            _samples_col.create_index("recipe_id")
            print(f"🍃 MongoDB: connected to {_mongo_db.name}", flush=True)
        except Exception as _e:
            print(f"⚠️  MongoDB not reachable at startup ({_e}) — "
                  "will keep retrying on each request", flush=True)
    else:
        MONGO_AVAILABLE = False
        print("⚠️  MONGODB_URI not set — results stored in R2 only")
except ImportError:
    MONGO_AVAILABLE = False
    print("⚠️  pymongo not installed — results stored in R2 only")

# ─── Neural Engine (optional, graceful fallback to OpenCV) ────────────────────
try:
    from nn_engine import (match_score_nn, analyze_defects_nn,
                           locate_and_extract_nn, similarity_nn,
                           similarity_nn_batch, similarity_nn_from_feats,
                           similarity_nn_cached, reference_features,
                           reference_sift, defect_preprocess,
                           estimate_homography,
                           extract_features, extract_features_batch,
                           align_photo_to_ref, NN_DEVICE)
    import calibration as calibration_mod
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

# mobile_token → {"sid": session_id, "ts": last_bound}
# Authoritative copy lives in memory (single gunicorn worker); the file only
# lets phones survive a service restart without re-scanning the QR code.
_MOBILE_TOKENS_PATH = BASE_DIR / ".mobile_tokens.json"
_MOBILE_TOKENS_LOCK = threading.Lock()
_MOBILE_TOKEN_TTL = 7 * 86400


def _read_mobile_tokens_file() -> dict:
    try:
        with open(_MOBILE_TOKENS_PATH) as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    now = time.time()
    tokens = {}
    for t, v in raw.items():
        if isinstance(v, str):  # legacy format: token → sid
            v = {"sid": v, "ts": now}
        if isinstance(v, dict) and (v.get("sid") or v.get("recipe")):
            tokens[t] = v
    return tokens


_mobile_tokens: dict[str, dict] = _read_mobile_tokens_file()


def _persist_mobile_tokens():
    """Write tokens atomically (tmp + rename). Caller holds the lock."""
    now = time.time()
    for t in [t for t, v in _mobile_tokens.items()
              if now - v.get("ts", 0) > _MOBILE_TOKEN_TTL]:
        del _mobile_tokens[t]
    tmp = _MOBILE_TOKENS_PATH.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(_mobile_tokens, f)
        _os.replace(tmp, _MOBILE_TOKENS_PATH)
    except OSError:
        pass


def _get_token_session(token: str) -> str | None:
    """Get session_id for a mobile token."""
    entry = _mobile_tokens.get(token)
    return entry.get("sid") if entry else None


def _bind_token(token: str, sid: str):
    """Point a mobile token at a session. Caller holds the lock."""
    _mobile_tokens[token] = {"sid": sid, "ts": time.time()}
    _persist_mobile_tokens()


# session_id → list of uploaded photo blobs (bytes) from mobile
mobile_photos: dict[str, list[bytes]] = {}

# Thread pool for background R2 uploads (non-blocking save)
_r2_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="r2")
# r2.append_line is download + append + upload: serialize to not lose lines
_r2_append_lock = threading.Lock()

# ─── Session TTL cleanup (60 min) ────────────────────────────────────────────
SESSION_TTL = 1800  # seconds (30 min)


def _cleanup_sessions():
    """Remove sessions inactive for more than SESSION_TTL."""
    import gc
    import ctypes
    try:
        _libc = ctypes.CDLL("libc.so.6")
        _malloc_trim = _libc.malloc_trim
    except (OSError, AttributeError):
        _malloc_trim = None  # macOS / non-glibc
    while True:
        time.sleep(120)  # check every 2 min
        now = time.time()
        expired = [sid for sid, s in sessions.items()
                   if now - s.get("_last_active", s.get("_created", 0)) > SESSION_TTL]
        for sid in expired:
            sessions.pop(sid, None)
            # Clean mobile photos for this session
            mobile_photos.pop(sid, None)
            # Mobile tokens are kept (they expire by TTL): the desktop can
            # rebind its token to a fresh session so the phone keeps working.
        if expired:
            gc.collect()
            if _malloc_trim:
                _malloc_trim(0)  # return freed memory to OS
        # Purge stale login attempts / lockouts
        cutoff = now - 3600
        for ip in list(_login_attempts):
            _login_attempts[ip] = [
                t for t in _login_attempts[ip] if t > cutoff]
            if not _login_attempts[ip]:
                del _login_attempts[ip]
        for ip in list(_login_lockouts):
            if _login_lockouts[ip] < now:
                del _login_lockouts[ip]
        # Log memory usage summary
        n_sess = len(sessions)
        n_photos = sum(len(v) for v in mobile_photos.values())
        n_checked = sum(len(s.get("checked", {})) for s in sessions.values())
        if n_sess or expired:
            import logging
            logging.getLogger(__name__).info(
                "Sessions: %d active, %d expired | checked zones in mem: %d | mobile photos queued: %d",
                n_sess, len(expired), n_checked, n_photos)


_cleanup_thread = threading.Thread(target=_cleanup_sessions, daemon=True)
_cleanup_thread.start()

# ─── Auth: access key system ─────────────────────────────────────────────────
AUTH_KEYS_PATH = BASE_DIR / "auth_keys.json"
AUTH_COOKIE = "pcb_auth_key"
AUTH_COOKIE_MAX_AGE = 30 * 86400  # 30 days

# ─── Brute-force protection ──────────────────────────────────────────────────
# Keys are ~95-bit random strings, so this only throttles noise. Kept mild:
# a factory floor often shares one public IP, and a lockout hits every station.
_LOGIN_MAX_ATTEMPTS = 10      # per window
_LOGIN_WINDOW = 60            # seconds
_LOGIN_LOCKOUT = 120          # seconds after exceeding limit
_login_attempts: dict[str, list[float]] = {}  # ip -> [timestamps]
_login_lockouts: dict[str, float] = {}        # ip -> lockout_until


def _client_ip() -> str:
    """Real client IP. Behind the local nginx, trust its X-Real-IP (nginx
    resolves the Cloudflare CF-Connecting-IP via the realip module)."""
    addr = request.remote_addr or "unknown"
    if addr in ("127.0.0.1", "::1"):
        return request.headers.get("X-Real-IP", addr)
    return addr


def _load_auth_keys() -> dict:
    """Load auth_keys.json (users + api_keys)."""
    if AUTH_KEYS_PATH.exists():
        with open(AUTH_KEYS_PATH) as f:
            return json.load(f)
    return {}


def _check_auth():
    """Return username if valid auth, else None.
    Sets request.auth_scope = 'full' | 'read'.
    Sets request.auth_role  = 'admin' | 'lead' | 'operator' | 'api'.
    Sets request.auth_group = group string | None (admin).
    """
    key = request.cookies.get(AUTH_COOKIE) or request.headers.get("X-Auth-Key")
    if not key:
        return None
    data = _load_auth_keys()
    # Regular user key
    entry = data.get("users", {}).get(key)
    if entry:
        request.auth_scope = "full"
        request.auth_role = entry.get("role", "operator")
        request.auth_group = entry.get("group")  # None for admin
        return entry["name"]
    # API key (apk_ prefix)
    api_entry = data.get("api_keys", {}).get(key)
    if api_entry:
        request.auth_scope = api_entry.get("scope", "read")
        request.auth_role = "api"
        request.auth_group = api_entry.get("group")  # None = all groups
        return f"api:{api_entry['name']}"
    return None


# Routes that don't require auth
_PUBLIC_PREFIXES = ("/login", "/static/", "/api/mobile/",
                    "/mobile", "/doc", "/license", "/healthz",
                    "/robots.txt", "/sitemap.xml")


def _require_read_scope():
    """Return 403 JSON if current auth lacks read scope."""
    scope = getattr(request, "auth_scope", "full")
    if scope not in ("read", "full"):
        return jsonify({"error": "Forbidden: insufficient scope"}), 403
    return None


def _require_full_scope():
    """Return 403 JSON if current auth is read-only (API key)."""
    scope = getattr(request, "auth_scope", "full")
    if scope != "full":
        return jsonify({"error": "Forbidden: read-only API key"}), 403
    return None


def _require_lead_or_admin():
    """Return 403 JSON if current user is not admin or lead."""
    role = getattr(request, "auth_role", "")
    if role not in ("admin", "lead"):
        return jsonify({"error": "Forbidden: requires admin or lead role"}), 403
    return None


def _group_filter(field: str = "group", shared: bool = False) -> dict:
    """Return MongoDB filter dict restricting by group.
    Admin/API see everything; operators see own group.
    shared=True also includes records with empty/missing group (shared).
    """
    grp = getattr(request, "auth_group", None)
    if grp is None:
        return {}
    if shared:
        return {"$or": [{field: grp}, {field: {"$exists": False}}, {field: ""}]}
    return {field: grp}


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
    # Read-only API keys can only access /api/v1/ GET routes
    scope = getattr(request, "auth_scope", "full")
    if scope == "read":
        if path.startswith("/api/v1/") and request.method == "GET":
            return None
        return jsonify({"error": "Forbidden: read-only API key"}), 403


@app.after_request
def _no_cache(resp):
    # Никакого кеширования — всегда свежие данные
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    # Security headers
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
    resp.headers["X-XSS-Protection"] = "1; mode=block"
    return resp


@app.route("/login", methods=["GET"])
def login_page():
    return render_template("login.html")


@app.route("/login", methods=["POST"])
def login_submit():
    ip = _client_ip()
    now = time.time()

    # Check lockout
    locked_until = _login_lockouts.get(ip, 0)
    if now < locked_until:
        remaining = int(locked_until - now)
        return jsonify({"error": f"Too many attempts. Retry in {remaining}s"}), 429

    data = request.get_json(silent=True) or {}
    key = data.get("key", "").strip()
    keys = _load_auth_keys()
    entry = keys.get("users", {}).get(key)
    if not entry:
        # Record failed attempt
        attempts = _login_attempts.setdefault(ip, [])
        attempts.append(now)
        # Trim old attempts outside the window
        attempts[:] = [t for t in attempts if now - t < _LOGIN_WINDOW]
        if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
            _login_lockouts[ip] = now + _LOGIN_LOCKOUT
            _login_attempts.pop(ip, None)
            app.logger.warning("Login brute-force lockout: ip=%s", ip)
            return jsonify({"error": f"Too many attempts. Retry in {_LOGIN_LOCKOUT}s"}), 429
        return jsonify({"error": "Invalid access key"}), 403

    # Success — clear attempts
    _login_attempts.pop(ip, None)
    _login_lockouts.pop(ip, None)
    resp = jsonify({"status": "ok", "user": entry["name"]})
    resp.set_cookie(AUTH_COOKIE, key, max_age=AUTH_COOKIE_MAX_AGE,
                    httponly=True, samesite="Lax")
    return resp


@app.route("/logout")
def logout():
    resp = redirect("/login")
    resp.delete_cookie(AUTH_COOKIE)
    return resp


@app.route("/healthz")
def healthz():
    """Liveness + dependency check for external uptime monitoring.
    503 when a configured dependency is down (e.g. MongoDB unreachable)."""
    checks = {"nn": NN_AVAILABLE}
    healthy = True
    if _os.environ.get("MONGODB_URI"):
        try:
            _mongo.admin.command("ping")
            checks["mongo"] = "ok"
        except Exception:
            checks["mongo"] = "down"
            healthy = False
    else:
        checks["mongo"] = "not configured"
    return jsonify({"status": "ok" if healthy else "degraded",
                    **checks}), 200 if healthy else 503


@app.route("/doc")
def doc_page():
    return render_template("doc.html")


@app.route("/license")
def license_page():
    license_path = BASE_DIR / "LICENSE"
    if not license_path.exists():
        return "LICENSE file not found", 404
    return send_file(license_path, mimetype="text/plain")


@app.route("/robots.txt")
def robots_txt():
    base = app.config["BASE_URL"] or request.host_url.rstrip("/")
    lines = [
        "User-agent: *",
        "Allow: /doc",
        "Allow: /login",
        "Allow: /license",
        "Disallow: /api/",
        "Disallow: /uploads/",
        "Disallow: /static/",
        "",
        f"Sitemap: {base}/sitemap.xml",
    ]
    resp = make_response("\n".join(lines), 200)
    resp.headers["Content-Type"] = "text/plain"
    return resp


@app.route("/sitemap.xml")
def sitemap_xml():
    base = app.config["BASE_URL"] or request.host_url.rstrip("/")
    urls = [
        (f"{base}/doc", "monthly", "0.8"),
        (f"{base}/login", "yearly", "0.3"),
        (f"{base}/license", "yearly", "0.1"),
    ]
    xml_parts = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, freq, prio in urls:
        xml_parts.append(f"  <url><loc>{loc}</loc>"
                         f"<changefreq>{freq}</changefreq>"
                         f"<priority>{prio}</priority></url>")
    xml_parts.append("</urlset>")
    resp = make_response("\n".join(xml_parts), 200)
    resp.headers["Content-Type"] = "application/xml"
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
    cv2.setRNGSeed(0)  # deterministic kd-trees (see nn_engine.flann_knn)
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
                     strict: bool = False,
                     zone_feats=None, extracted_feats=None,
                     thr=None) -> dict:
    """
    Детекция дефектов: структурное сравнение при наличии neural engine,
    иначе classical (CLAHE + SSIM + edge diff).
    strict=True: подзоны — жёсткие пороги.
    thr: снимок порогов inspection_config.thresholds() для этого запроса.
    """
    if NN_AVAILABLE:
        return analyze_defects_nn(zone_crop, extracted, extract_nn_score,
                                  strict=strict,
                                  zone_feats=zone_feats,
                                  extracted_feats=extracted_feats,
                                  thr=thr)

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
    return render_template("index.html",
                           auth_user=getattr(request, "auth_user", ""),
                           auth_role=getattr(request, "auth_role", ""),
                           auth_group=getattr(request, "auth_group", "") or "")


@app.route("/api/auto_blend", methods=["POST"])
def api_auto_blend():
    """Принять несколько изображений, автоматически выровнять и усреднить."""
    # Ad-hoc references/zones are recipe authoring: lead/admin only.
    # Operators inspect with published recipes (load_template).
    err = _require_lead_or_admin()
    if err:
        return err
    files = request.files.getlist("images")
    if len(files) < 2:
        return jsonify({"error": "At least 2 images required"}), 400

    images = []
    for f in files:
        if not f or not _allowed(f.filename):
            continue
        data = np.frombuffer(f.read(), np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if img is not None:
            images.append(img)

    if len(images) < 2:
        return jsonify({"error": "Failed to read images (at least 2 required)"}), 400

    result, logs = auto_blend_images(images)
    if result is None:
        return jsonify({"error": "Failed to align images", "log": logs}), 422

    return jsonify({
        "image_b64": _img_to_b64(result),
        "width": result.shape[1],
        "height": result.shape[0],
        "log": logs,
    })


@app.route("/api/session", methods=["POST"])
def create_session():
    """Шаг 1: Загрузить изображение платы (одно или склеенное на клиенте) → session_id."""
    # Ad-hoc references/zones are recipe authoring: lead/admin only.
    # Operators inspect with published recipes (load_template).
    err = _require_lead_or_admin()
    if err:
        return err
    # Accept 'image' (single/stitched) or 'images' (multi, backward compat)
    file = request.files.get("image")
    if not file:
        files = request.files.getlist("images")
        file = files[0] if files else None
    if not file or not _allowed(file.filename):
        return jsonify({"error": "File not found or unsupported format"}), 400

    img_path = _save_upload(file)
    ref_img = cv2.imread(str(img_path))
    if ref_img is None:
        return jsonify({"error": "Failed to open image"}), 400

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
    # Ad-hoc references/zones are recipe authoring: lead/admin only.
    # Operators inspect with published recipes (load_template).
    err = _require_lead_or_admin()
    if err:
        return err
    if sid not in sessions:
        return jsonify({"error": "Session not found"}), 404

    data = request.get_json(silent=True)
    if not data or "zones" not in data:
        return jsonify({"error": "JSON with 'zones' field required"}), 400

    zones = data["zones"]
    if not isinstance(zones, list) or len(zones) == 0:
        return jsonify({"error": "Zone list is empty"}), 400

    for i, z in enumerate(zones):
        for key in ("x", "y", "w", "h"):
            if key not in z:
                return jsonify({"error": f"Zone {i}: missing '{key}'"}), 400
        if "label" not in z:
            z["label"] = f"Zone {i + 1}"
        # Validate subzones (coords relative to parent zone 0..1)
        subzones = z.get("subzones", [])
        if not isinstance(subzones, list):
            subzones = []
        for j, sz in enumerate(subzones):
            for key in ("x", "y", "w", "h"):
                if key not in sz:
                    return jsonify({"error": f"Zone {i}, subzone {j}: missing '{key}'"}), 400
            if "label" not in sz:
                sz["label"] = f"Subzone {j + 1}"
            # Optional per-subzone sensitivity override (0..2 or null)
            if "sensitivity" in sz and sz["sensitivity"] is not None:
                try:
                    sz["sensitivity"] = max(
                        0.0, min(2.0, float(sz["sensitivity"])))
                except (ValueError, TypeError):
                    sz.pop("sensitivity", None)
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
        return jsonify({"error": "Session not found"}), 404

    file = request.files.get("photo")
    if not file or not _allowed(file.filename):
        return jsonify({"error": "File not found"}), 400

    data = np.frombuffer(file.read(), np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        return jsonify({"error": "Failed to read image"}), 400

    codes = detect_codes(img)
    if not codes:
        return jsonify({"error": "Code not found. Try photographing closer or sharper."}), 422

    # Pick the first code with non-empty data, fallback to first
    best = next((c for c in codes if c["data"]), codes[0])
    serial = best["data"] or "unrecognized"
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
        return jsonify({"error": f"Internal error: {e}"}), 500


def _ref_cache(s: dict) -> dict:
    """Per-session cache of reference-side features. The reference image and
    zones are constant between photos, so their SIFT / CNN features are
    computed once instead of on every check. Reset when zones change."""
    key = json.dumps(s["zones"], sort_keys=True, default=str)
    c = s.get("_ref_cache")
    if c is None or c["key"] != key:
        c = s["_ref_cache"] = {"key": key, "zone": {}, "zone_pre": {},
                               "sub": {}}
    return c


def _cached_ref_feats(store: dict, crops: dict, preprocess=None) -> list:
    """Features for crops {key: image}, computing only the missing ones
    (in one batch). Returns them in crops' order."""
    missing = [k for k in crops if k not in store]
    if missing:
        feats = reference_features([crops[k] for k in missing], preprocess)
        store.update(zip(missing, feats))
    return [store[k] for k in crops]


_calib_models: dict[tuple, dict] = {}   # (recipe id, version) → {zone: ZoneModel}
_calib_models_lock = threading.Lock()


def _zone_models(s: dict) -> dict:
    key = (s.get("template_id"), s.get("template_version"))
    with _calib_models_lock:
        hit = _calib_models.get(key)
    if hit is not None:
        return hit
    ref_wh = (s["ref_img"].shape[1], s["ref_img"].shape[0])
    models = {}
    for zc in s["calibration"]["zones"]:
        data = r2.download_bytes(zc["bank_key"])
        if data:
            models[zc["zone"]] = calibration_mod.ZoneModel(s["zones"][zc["zone"]], ref_wh, zc, data)
    with _calib_models_lock:
        if len(_calib_models) > 8:
            _calib_models.clear()
        _calib_models[key] = models
    return models


def _vis_calibrated(crop, nmap, t_ok, t_def, size=(256, 256)):
    """Heatmap and outlined-deviation images in the legacy result format."""
    base = cv2.resize(crop, size)
    norm = np.clip(nmap / max(t_def, 1e-6), 0, 1.5) / 1.5
    hm = cv2.applyColorMap((norm * 255).astype(np.uint8), cv2.COLORMAP_JET)
    hm = cv2.resize(hm, size, interpolation=cv2.INTER_LINEAR)
    heat = cv2.addWeighted(base, 0.55, hm, 0.45, 0)
    mask = cv2.resize((nmap > t_ok).astype(np.uint8) * 255, size, interpolation=cv2.INTER_NEAREST)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = base.copy()
    if contours:
        red = np.zeros_like(out)
        red[:, :, 2] = mask
        out = cv2.addWeighted(out, 1.0, red, 0.35, 0)
        cv2.drawContours(out, contours, -1, (0, 0, 255), 2)
    return nn_b64(out), nn_b64(heat), len(contours)


def nn_b64(img):
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return base64.b64encode(buf.tobytes()).decode()


def _check_zone_calibrated(s: dict, photo, from_mobile: bool, photo_path):
    """Inspection with the recipe's model of normal variation. The zone is
    found by geometry (homography + coverage); the response has the same
    shape as the reference-comparison path, so the screens are unchanged."""
    ref_img, zones = s["ref_img"], s["zones"]
    if s.get("_ref_sift") is None:
        s["_ref_sift"] = reference_sift(ref_img)
    H, _info = estimate_homography(ref_img, photo, ref_sift=s["_ref_sift"])
    if from_mobile:
        photo_b64 = base64.b64encode(cv2.imencode(".jpg", photo)[1].tobytes()).decode()
    else:
        photo_b64 = _file_to_b64(photo_path)
    models = _zone_models(s)
    shares = [0.0] * len(zones)
    if H is not None:
        for zi, m in models.items():
            _, shares[zi] = m.geom.coverage(photo.shape, H)
    ok_zones = {i for i, info in s["checked"].items()
                if (info.get("defect_info") or {}).get("status") == "ok"}
    cands = [zi for zi in models if shares[zi] >= calibration_mod.ZONE_COVERAGE_MIN]
    cands.sort(key=lambda zi: (zi in ok_zones, -shares[zi]))
    res = models[cands[0]].inspect(photo, H) if cands else None
    best = cands[0] if cands else (int(np.argmax(shares)) if shares else 0)
    result = {
        "matched": res is not None,
        "best_zone_index": best,
        "best_zone_label": zones[best]["label"] if zones else "",
        "best_score": round(shares[best], 4) if shares else 0.0,
        "all_scores": [round(x, 4) for x in shares],
        "photo_b64": photo_b64,
        "mode": "calibrated",
    }
    if res is not None:
        verdict = {"ok": "OK ✅ — within the normal variation of the good samples",
                   "warn": "Warning ⚠️ — unusual area, check it",
                   "defect": "Defect ❌ — outside the normal variation"}[res["status"]]
        vis_def, vis_heat, n_regions = _vis_calibrated(res["crop"], res["map"], res["t_ok"], res["t_def"])
        sub_results = []
        gh, gw = res["map"].shape
        for sr, (ys, xs) in zip(res["subzones"], models[best].geom.subzone_slices()):
            sz = zones[best]["subzones"][sr["index"]]
            ch, cw = res["crop"].shape[:2]
            sub_crop = res["crop"][int(sz["y"] * ch):int((sz["y"] + sz["h"]) * ch) or 1,
                                   int(sz["x"] * cw):int((sz["x"] + sz["w"]) * cw) or 1]
            sd, shm, _ = _vis_calibrated(sub_crop, res["map"][ys, xs], sr["t_ok"], sr["t_def"])
            ref_crop = _crop_zone(_crop_zone(ref_img, zones[best]), sz)
            sub_results.append({
                "index": sr["index"], "label": sr["label"], "sensitivity": None,
                "status": sr["status"],
                "verdict": f"{sr['status'].upper()} — score {sr['score']:.2f} (limit {sr['t_ok']:.2f})",
                "defect_pct": round(100.0 * float((res["map"][ys, xs] > sr["t_ok"]).mean()), 2),
                "ssim": round(sr["score"], 3),
                "vis_defects_b64": sd, "vis_heatmap_b64": shm,
                "extracted_b64": nn_b64(cv2.resize(sub_crop, (256, 256))),
                "reference_b64": nn_b64(cv2.resize(ref_crop, (256, 256))) if ref_crop.size else "",
                "anomaly": {"score": sr["score"], "t_ok": sr["t_ok"], "t_def": sr["t_def"]},
            })
        status = res["status"]
        rank = {"ok": 0, "warn": 1, "defect": 2}
        for sr in sub_results:
            if rank[sr["status"]] > rank[status]:
                status = sr["status"]
                verdict = f"{verdict} (subzone: {sr['label']})"
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        s["checked"][best] = {
            "score": result["best_score"], "photo_b64": photo_b64,
            "checked_at": _dt.now(_tz(_td(hours=8))).strftime("%Y-%m-%dT%H:%M:%S.%f"),
            "operator": _check_auth() or s.get("operator", ""),
            "zone_sensitivity": None, "subzone_sensitivity": None,
            "defect_info": {"status": status, "defect_pct": res["area_pct"], "verdict": verdict},
            "subzone_results": sub_results,
            "anomaly": {"score": res["score"], "t_ok": res["t_ok"], "t_def": res["t_def"],
                        "mode": "calibrated"},
        }
        result["defect"] = {
            "status": status, "verdict": verdict,
            "ssim": round(res["score"], 3), "defect_pct": res["area_pct"],
            "defect_count": n_regions,
            "vis_defects_b64": vis_def, "vis_heatmap_b64": vis_heat,
            "extracted_b64": nn_b64(cv2.resize(res["crop"], (256, 256))),
            "zone_sensitivity": None, "subzone_sensitivity": None,
            "anomaly": {"score": res["score"], "t_ok": res["t_ok"], "t_def": res["t_def"]},
        }
        if sub_results:
            result["defect"]["subzones"] = sub_results
    total = len(zones)
    done = len(s["checked"])
    result["progress"] = {"done": done, "total": total, "complete": done >= total}
    result["checked_zones"] = list(s["checked"].keys())
    return jsonify(result)


def _check_zone_impl(sid, photo_img=None):
    if sid not in sessions:
        return jsonify({"error": "Session not found"}), 404

    s = sessions[sid]
    s["_last_active"] = time.time()
    if not s["zones"]:
        return jsonify({"error": "Zones not defined"}), 400

    # ─── Sensitivity ───────────────────────────────────────────────────────
    # Strictness belongs to the recipe. Values sent by the client are only
    # honoured from lead/admin (trial runs, ad-hoc sessions); operators always
    # inspect with the recipe's settings.
    # Thresholds are computed per request (never written to the shared
    # inspection_config module — requests run concurrently in threads).
    import inspection_config as _cfg
    zone_sens = s.get("zone_sensitivity")
    subzone_sens = s.get("subzone_sensitivity")
    if getattr(request, "auth_role", "") in ("admin", "lead"):
        # Form data first (desktop upload), then query args (mobile path)
        try:
            zs = request.form.get("zone_sensitivity") or request.args.get(
                "zone_sensitivity")
            if zs is not None:
                zone_sens = max(0.0, min(2.0, float(zs)))
        except (ValueError, TypeError):
            pass
        try:
            ss = request.form.get("subzone_sensitivity") or request.args.get(
                "subzone_sensitivity")
            if ss is not None:
                subzone_sens = max(0.0, min(2.0, float(ss)))
        except (ValueError, TypeError):
            pass
    if zone_sens is None:
        zone_sens = _cfg.ZONE_SENSITIVITY
    if subzone_sens is None:
        subzone_sens = _cfg.SUBZONE_SENSITIVITY
    thr = _cfg.thresholds(zone_sens, subzone_sens)

    if photo_img is not None:
        photo = photo_img
    else:
        if "photo" not in request.files:
            return jsonify({"error": "File 'photo' not found"}), 400

        file = request.files["photo"]
        if not file or not _allowed(file.filename):
            return jsonify({"error": "Unsupported file format"}), 400

        photo_path = _save_upload(file)
        photo = cv2.imread(str(photo_path))
        if photo is None:
            return jsonify({"error": "Failed to open photo"}), 400

    if s.get("calibration") and NN_AVAILABLE:
        return _check_zone_calibrated(s, photo, photo_img is not None,
                                      None if photo_img is not None else photo_path)

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
        if s.get("_ref_sift") is None:
            s["_ref_sift"] = reference_sift(ref_img)
        warped = align_photo_to_ref(ref_img, photo, ref_sift=s["_ref_sift"])
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

        if extracted is None and warped is None:
            # Slow fallback: per-zone localization — only when global
            # alignment failed. With a valid homography a black crop just
            # means "this zone is outside the photo"; searching for it
            # anyway cost ~1 s per zone and never produced a winning match.
            # ref_img/zone are not passed: global alignment was already
            # tried (identically) in step 1.
            extracted, method = _locate_and_extract(crop, photo)
        if extracted is None:
            candidates.append({"idx": i, "ssim_quick": 0.0,
                              "extracted": None, "method": None})
            continue

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
    top_candidates = valid[:TOP_N]
    rc = _ref_cache(s) if NN_AVAILABLE else None
    if NN_AVAILABLE and top_candidates:
        # One forward pass for the photo crops; reference side from cache
        crops = {}
        for c in top_candidates:
            c["_crop"] = crops[c["idx"]] = _crop_zone(ref_img, zones[c["idx"]])
        batch_scores = similarity_nn_cached(
            _cached_ref_feats(rc["zone"], crops),
            [c["extracted"] for c in top_candidates])
        for c, sc in zip(top_candidates, batch_scores):
            c["score"] = sc
    else:
        for c in top_candidates:
            crop = _crop_zone(ref_img, zones[c["idx"]])
            c["_crop"] = crop
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
        best_crop = best.get("_crop") if best.get(
            "_crop") is not None else _crop_zone(ref_img, zones[best["idx"]])
        extracted = best["extracted"]

        # Pre-extract features for subzones in batch (saves 2 NN passes per subzone)
        subzones = zones[best["idx"]].get("subzones", [])
        sz_pairs = []  # (idx, sz_ref, sz_ext)
        for szi, sz in enumerate(subzones):
            sz_ref = _crop_zone(best_crop, sz)
            sz_ext = _crop_zone(extracted, sz)
            if sz_ref.size == 0 or sz_ext.size == 0:
                continue
            sh, sw = sz_ref.shape[:2]
            sz_ext = cv2.resize(sz_ext, (sw, sh))
            sz_pairs.append((szi, sz, sz_ref, sz_ext))

        if NN_AVAILABLE and sz_pairs:
            # Batch: features for all subzone crops of the photo at once;
            # reference subzone features from the session cache
            ref_feats = _cached_ref_feats(
                rc["sub"], {(best["idx"], szi): sz_ref
                            for szi, _, sz_ref, _ in sz_pairs})
            ext_feats = extract_features_batch(
                [sz_ext for _, _, _, sz_ext in sz_pairs])
            for pi, (szi, sz, sz_ref, sz_ext) in enumerate(sz_pairs):
                # Per-subzone sensitivity override
                _sz_sens = sz.get("sensitivity")
                sz_thr = (_cfg.thresholds(zone_sens, _sz_sens)
                          if _sz_sens is not None else thr)
                zf = ref_feats[pi]
                ef = ext_feats[pi]
                sz_defect = _analyze_defects(
                    sz_ref, sz_ext, best_score, strict=True,
                    zone_feats=zf, extracted_feats=ef, thr=sz_thr)
                subzone_results.append({
                    "index": szi,
                    "label": sz.get("label", f"Subzone {szi + 1}"),
                    "sensitivity": _sz_sens,
                    "status": sz_defect["status"],
                    "verdict": sz_defect["verdict"],
                    "defect_pct": sz_defect["defect_pct"],
                    "ssim": sz_defect["ssim"],
                    "vis_defects_b64": sz_defect["vis_defects_b64"],
                    "vis_heatmap_b64": sz_defect["vis_heatmap_b64"],
                    "extracted_b64": sz_defect["extracted_b64"],
                    "reference_b64": sz_defect["reference_b64"],
                })
        else:
            for szi, sz, sz_ref, sz_ext in sz_pairs:
                # Per-subzone sensitivity override
                _sz_sens = sz.get("sensitivity")
                sz_thr = (_cfg.thresholds(zone_sens, _sz_sens)
                          if _sz_sens is not None else thr)
                sz_defect = _analyze_defects(sz_ref, sz_ext, best_score,
                                             strict=True, thr=sz_thr)
                subzone_results.append({
                    "index": szi,
                    "label": sz.get("label", f"Subzone {szi + 1}"),
                    "sensitivity": _sz_sens,
                    "status": sz_defect["status"],
                    "verdict": sz_defect["verdict"],
                    "defect_pct": sz_defect["defect_pct"],
                    "ssim": sz_defect["ssim"],
                    "vis_defects_b64": sz_defect["vis_defects_b64"],
                    "vis_heatmap_b64": sz_defect["vis_heatmap_b64"],
                    "extracted_b64": sz_defect["extracted_b64"],
                    "reference_b64": sz_defect["reference_b64"],
                })

        zone_feats = None
        if NN_AVAILABLE:
            zone_feats = _cached_ref_feats(
                rc["zone_pre"], {best["idx"]: best_crop},
                preprocess=defect_preprocess)[0]
        defect_info = _analyze_defects(best_crop, extracted, best_score,
                                       zone_feats=zone_feats, thr=thr)

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
                    f"(subzone: {bad_sz['label']})")

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
        return jsonify({"error": "Session not found"}), 404
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
        return jsonify({"error": "Session not found"}), 404
    data = request.get_json(silent=True) or {}
    sessions[sid]["_auto_accept"] = bool(data.get("auto_accept", True))
    return jsonify({"status": "ok"})


@app.route("/api/session/<sid>/reset", methods=["POST"])
def reset_session(sid):
    """Сбросить проверку (зоны остаются)."""
    if sid not in sessions:
        return jsonify({"error": "Session not found"}), 404
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
        return jsonify({"error": "Session not found"}), 404
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
    # Date + random suffix: a long-lived session must never produce the same
    # id twice (a repeat would overwrite an earlier board's record + images).
    rid = f"{sid[:8]}_{ts:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}"
    prefix = f"results/{rid}/"

    record = {
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S.%f"),
        "result_id": rid,
        "session_id": sid,
        "template_id": s.get("template_id", ""),
        "template_name": s.get("template_name", ""),
        "recipe_version": s.get("template_version"),
        "barcode_mask": s.get("barcode_mask", ""),
        "serial": s.get("serial", ""),
        "serial_type": s.get("serial_type", ""),
        "operator": s.get("operator", ""),
        "group": getattr(request, "auth_group", "") or "",
        "zones_total": len(s["zones"]),
        "zones_checked": len(s["checked"]),
        "zones": [],
        "overall_status": "ok",
    }
    # (path, data_bytes, content_type) — queued for parallel R2 upload
    _uploads = []
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
        if info.get("anomaly"):
            zone_rec["anomaly"] = info["anomaly"]
        # User decision override
        if i in user_decisions:
            zone_rec["user_decision"] = user_decisions[i]
        # Queue zone photo for R2 upload
        photo_b64 = info.get("photo_b64")
        if photo_b64:
            _uploads.append((prefix + f"zone_{i}.jpg",
                             base64.b64decode(photo_b64), "image/jpeg"))
            zone_rec["image"] = f"zone_{i}.jpg"

        # Queue subzone images for R2 upload and store subzone metadata
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
                    _uploads.append((prefix + fname,
                                     base64.b64decode(vis_b64), "image/jpeg"))
                    sz_rec["image_defects"] = fname
                heat_b64 = sr.get("vis_heatmap_b64")
                if heat_b64:
                    fname = f"zone_{i}_sub_{szi}_heatmap.jpg"
                    _uploads.append((prefix + fname,
                                     base64.b64decode(heat_b64), "image/jpeg"))
                    sz_rec["image_heatmap"] = fname
                ext_b64 = sr.get("extracted_b64")
                if ext_b64:
                    fname = f"zone_{i}_sub_{szi}_extracted.jpg"
                    _uploads.append((prefix + fname,
                                     base64.b64decode(ext_b64), "image/jpeg"))
                    sz_rec["image_extracted"] = fname
                ref_b64 = sr.get("reference_b64")
                if ref_b64:
                    fname = f"zone_{i}_sub_{szi}_reference.jpg"
                    _uploads.append((prefix + fname,
                                     base64.b64decode(ref_b64), "image/jpeg"))
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

    # Queue reference crop per zone for R2 upload
    ref_img = s.get("ref_img")
    if ref_img is not None:
        for i, z in enumerate(s["zones"]):
            crop = _crop_zone(ref_img, z)
            if crop is not None:
                _, buf = cv2.imencode(
                    ".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
                _uploads.append(
                    (prefix + f"ref_{i}.jpg", buf.tobytes(), "image/jpeg"))

    # Save metadata: MongoDB (primary) with R2 fallback
    if MONGO_AVAILABLE:
        _results_col.insert_one(dict(record))  # copy: insert_one adds _id
    else:
        r2.upload_json(prefix + "meta.json", record)

    # Append to daily log (R2)
    day = ts.strftime("%Y-%m-%d")
    _uploads.append((f"logs/log_{day}.jsonl",
                     json.dumps(record, ensure_ascii=False).encode(), None))

    # Fire all R2 uploads in parallel (non-blocking)
    def _do_upload(path, data, ct):
        try:
            if ct is None:
                with _r2_append_lock:
                    r2.append_line(path, data.decode())
            else:
                r2.upload_bytes(path, data, ct)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("R2 upload failed: %s", path)
    for u in _uploads:
        _r2_pool.submit(_do_upload, *u)

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
    # Free heavy in-memory blobs — images already persisted to R2/MongoDB
    sessions[sid]["checked"] = {}
    mobile_photos.pop(sid, None)
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

    q = {"result_id": rid}
    q.update(_group_filter())
    existing = _results_col.find_one(q)
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

    from datetime import datetime, timezone
    _results_col.update_one({"result_id": rid}, {
        "$set": {"zones": zones, "overall_status": overall},
        "$push": {"decision_log": {
            "at": datetime.now(timezone.utc).isoformat(),
            "by": getattr(request, "auth_user", ""),
            "previous_overall": existing.get("overall_status"),
            "overall": overall,
            "user_decisions": {str(k): v for k, v in user_decisions.items()},
            "user_sub_decisions": {
                str(z): {str(k): v for k, v in subs.items()}
                for z, subs in user_sub_decisions.items()},
        }},
    })
    return jsonify({"status": "ok", "overall": overall, "result_id": rid})


# ─── External API v1 (read-only, for integrations) ───────────────────────────

@app.route("/api/v1/results")
def api_v1_list_results():
    """External API: list inspection results with date/serial/template filters.
    Auth: X-Auth-Key header with an apk_ key (read scope).
    Query params: from, to (ISO dates), serial, template, operator, limit, offset.
    """
    import re as _re
    if not MONGO_AVAILABLE:
        return jsonify({"error": "MongoDB unavailable"}), 503

    limit = request.args.get("limit", 50, type=int)
    offset = request.args.get("offset", 0, type=int)
    limit = min(limit, 500)

    conditions = []
    # Date range filter
    date_from = request.args.get("from", "").strip()
    date_to = request.args.get("to", "").strip()
    if date_from:
        conditions.append({"timestamp": {"$gte": date_from}})
    if date_to:
        # Include the whole day
        if len(date_to) == 10:
            date_to += "T23:59:59.999999"
        conditions.append({"timestamp": {"$lte": date_to}})

    # Field filters (support * wildcard)
    for param, field in [("serial", "serial"), ("template", "template_name"),
                         ("operator", "operator"), ("status", "overall_status")]:
        val = request.args.get(param, "").strip()
        if val and val != "*":
            segments = val.split("*")
            pattern = ".*".join(_re.escape(s) for s in segments)
            conditions.append({field: {"$regex": pattern, "$options": "i"}})

    # Group filter (API keys with no group see everything)
    grp_f = _group_filter()
    if grp_f:
        conditions.append(grp_f)

    mongo_filter = {"$and": conditions} if len(conditions) > 1 else (
        conditions[0] if conditions else {})

    total = _results_col.count_documents(mongo_filter)
    cursor = (_results_col.find(mongo_filter, {"_id": 0})
              .sort("timestamp", -1).skip(offset).limit(limit))
    results = []
    for doc in cursor:
        rid = doc.get("result_id", "")
        # Enrich zone image fields with URL paths
        for z in doc.get("zones", []):
            if z.get("image"):
                z["image_url"] = f"/api/v1/results/{rid}/images/{z['image']}"
            for sz in z.get("subzones", []):
                for img_field in ("image_defects", "image_heatmap",
                                  "image_extracted", "image_reference"):
                    if sz.get(img_field):
                        sz[f"{img_field}_url"] = (
                            f"/api/v1/results/{rid}/images/{sz[img_field]}")
        results.append(doc)
    return jsonify({"total": total, "limit": limit, "offset": offset,
                    "results": results})


@app.route("/api/v1/results/<rid>")
def api_v1_get_result(rid):
    """External API: get a single inspection result by result_id.
    Optional ?images=base64 to inline images as data URIs."""
    import re as _re
    import base64
    if not _re.match(r'^[a-zA-Z0-9_\-]+$', rid):
        return jsonify({"error": "invalid result_id"}), 400
    if not MONGO_AVAILABLE:
        return jsonify({"error": "MongoDB unavailable"}), 503

    q = {"result_id": rid}
    q.update(_group_filter())
    doc = _results_col.find_one(q, {"_id": 0})
    if not doc:
        return jsonify({"error": "not found"}), 404

    inline = request.args.get("images", "").lower() == "base64"
    for z in doc.get("zones", []):
        if z.get("image"):
            if inline:
                data = r2.download_bytes(f"results/{rid}/{z['image']}")
                z["image_base64"] = base64.b64encode(
                    data).decode() if data else None
            z["image_url"] = f"/api/v1/results/{rid}/images/{z['image']}"
        for sz in z.get("subzones", []):
            for img_field in ("image_defects", "image_heatmap",
                              "image_extracted", "image_reference"):
                fname = sz.get(img_field)
                if fname:
                    if inline:
                        data = r2.download_bytes(f"results/{rid}/{fname}")
                        sz[f"{img_field}_base64"] = (
                            base64.b64encode(data).decode() if data else None)
                    sz[f"{img_field}_url"] = (
                        f"/api/v1/results/{rid}/images/{fname}")
    return jsonify(doc)


@app.route("/api/v1/results/<rid>/images/<fname>")
def api_v1_result_image(rid, fname):
    """External API: serve a result image from R2."""
    import re
    if (not re.match(r'^[a-zA-Z0-9_\-]+$', rid)
            or not re.match(r'^[a-zA-Z0-9_\-]+\.jpg$', fname)):
        return jsonify({"error": "invalid"}), 400
    data = r2.download_bytes(f"results/{rid}/{fname}")
    if not data:
        return jsonify({"error": "not found"}), 404
    return send_file(io.BytesIO(data), mimetype="image/jpeg")


# ─── Internal results API (operators, full scope) ────────────────────────────

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
    grp_f = _group_filter()
    if search:
        parts = [p.strip() for p in search.split("/")]
        conditions = []
        # Part 1 -> serial, Part 2 -> template_name, Part 3 -> operator
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
                # Extra parts -> search zones labels
                conditions.append(
                    {"zones.label": {"$regex": escaped, "$options": "i"}})
        if grp_f:
            conditions.append(grp_f)
        if conditions:
            mongo_filter = {"$and": conditions} if len(
                conditions) > 1 else conditions[0]
    elif grp_f:
        mongo_filter = grp_f

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
    """Delete all saved inspection results (admin only)."""
    if getattr(request, "auth_role", "") != "admin":
        return jsonify({"error": "Admin only"}), 403
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


# ─── Recipes (templates collection + recipe_versions + samples) ──────────────
# Operators read the recipe document, whose top-level fields mirror the ACTIVE
# version (see recipes.py). Recipe Studio (lead/admin) edits drafts in
# recipe_versions and collects samples; publishing makes a draft active.

def _r2_tpl_prefix(tid: str) -> str:
    return f"templates/{tid}/"


def _recipe_or_404(tid: str):
    q = {"id": tid}
    q.update(_group_filter())
    doc = _templates_col.find_one(q, {"_id": 0})
    if doc:
        recipes_mod.migrate_template(doc, _versions_col)
    return doc


def _studio_guard():
    """Common checks for Recipe Studio routes: lead/admin + MongoDB."""
    err = _require_lead_or_admin()
    if err:
        return err
    if not MONGO_AVAILABLE:
        return jsonify({"error": "MongoDB unavailable"}), 503
    return None


@app.route("/api/templates", methods=["GET"])
def list_templates():
    """Recipes available for inspection: those with an active version.
    A recipe without a group is shared: every group may inspect with it
    (editing it in Studio stays with admins)."""
    if not MONGO_AVAILABLE:
        return jsonify({"error": "MongoDB unavailable"}), 503
    q = _group_filter(shared=True)
    docs = _templates_col.find(q, {"_id": 0, "id": 1, "name": 1, "zones": 1,
                                   "created": 1, "barcode_mask": 1, "version": 1,
                                   "group": 1, "active_version": 1,
                                   "versions": 1})
    templates = []
    for d in docs:
        if not d.get("zones"):
            continue  # recipe created in Studio, never published
        templates.append({
            "id": d["id"],
            "name": d["name"],
            "zone_count": len(d.get("zones", [])),
            "created": d.get("created", ""),
            "barcode_mask": d.get("barcode_mask", ""),
            "version": d.get("active_version") or d.get("version", 1),
            "group": d.get("group", ""),
        })
    templates.sort(key=lambda t: t.get("created", ""))
    return jsonify({"templates": templates})


@app.route("/api/templates/<tid>", methods=["GET"])
def load_template(tid):
    """Загрузить активную версию рецепта → создать сессию с готовыми зонами."""
    if not MONGO_AVAILABLE:
        return jsonify({"error": "MongoDB unavailable"}), 503
    q = {"id": tid}
    q.update(_group_filter(shared=True))
    meta = _templates_col.find_one(q, {"_id": 0})
    if not meta or not meta.get("zones"):
        return jsonify({"error": "Template not found"}), 404

    ref_bytes = r2.download_bytes(recipes_mod.ref_key(tid, meta["ref_image"]))
    if not ref_bytes:
        return jsonify({"error": "Failed to open template reference"}), 500

    ref_img = cv2.imdecode(np.frombuffer(
        ref_bytes, np.uint8), cv2.IMREAD_COLOR)
    if ref_img is None:
        return jsonify({"error": "Failed to decode reference"}), 500

    zones = meta["zones"]
    if meta.get("anchors") and not any(z.get("anchors") for z in zones):
        zones[0]["anchors"] = meta["anchors"][:2]

    # Save ref image to temp file for session (needed by other code paths)
    tmp_ref = UPLOAD_DIR / f"{tid}_ref.jpg"
    tmp_ref.write_bytes(ref_bytes)

    version = meta.get("active_version") or meta.get("version", 1)
    sid = uuid.uuid4().hex[:12]
    sessions[sid] = {
        "ref_path": tmp_ref,
        "ref_img": ref_img,
        "zones": zones,
        "checked": {},
        "template_id": tid,
        "template_name": meta["name"],
        "template_version": version,
        "barcode_mask": meta.get("barcode_mask", ""),
        # Calibrated recipes inspect with their model of normal variation
        "calibration": meta.get("calibration"),
        # Strictness comes from the recipe (None → config defaults)
        "zone_sensitivity": meta.get("zone_sensitivity"),
        "subzone_sensitivity": meta.get("subzone_sensitivity"),
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
        "version": version,
        "operator_notes": meta.get("operator_notes", ""),
        "image_b64": ref_b64,
        "width": w,
        "height": h,
        "zones": zones,
        "previews": zone_previews,
    })


@app.route("/api/templates/<tid>", methods=["DELETE"])
def delete_template(tid):
    """Delete a recipe with all versions, samples and images (lead/admin)."""
    err = _studio_guard()
    if err:
        return err
    q = {"id": tid}
    q.update(_group_filter())
    res = _templates_col.delete_one(q)
    if res.deleted_count == 0:
        return jsonify({"error": "Template not found"}), 404
    _versions_col.delete_many({"recipe_id": tid})
    _samples_col.delete_many({"recipe_id": tid})
    r2.delete_prefix(_r2_tpl_prefix(tid))
    r2.delete_prefix(f"samples/{tid}/")
    return jsonify({"status": "ok"})


# ─── Recipe Studio API ───────────────────────────────────────────────────────

_studio_refs: dict[tuple, tuple] = {}   # (tid, ref_image) → (img, sift)
_studio_ref_lock = threading.Lock()


def _reference(tid: str, ref_image: str):
    """Decoded reference image (+ SIFT when the NN engine is available),
    cached: alignment of every sample needs it."""
    key = (tid, ref_image)
    with _studio_ref_lock:
        hit = _studio_refs.get(key)
    if hit:
        return hit
    data = r2.download_bytes(recipes_mod.ref_key(tid, ref_image))
    if not data:
        return None, None
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    sift = reference_sift(img) if NN_AVAILABLE and img is not None else None
    with _studio_ref_lock:
        if len(_studio_refs) > 16:
            _studio_refs.clear()
        _studio_refs[key] = (img, sift)
    return img, sift


def _version_doc(tid: str, version: int):
    return _versions_col.find_one({"recipe_id": tid, "version": version},
                                  {"_id": 0})


def _draft_of(tid: str):
    return _versions_col.find_one({"recipe_id": tid,
                                   "status": recipes_mod.STATUS_DRAFT},
                                  {"_id": 0})


def _derived_samples(tid: str, version: dict, kind: str | None = None):
    q = {"recipe_id": tid}
    if kind:
        q["kind"] = kind
    docs = _samples_col.find(q, {"_id": 0}).sort("uploaded_at", 1)
    return [recipes_mod.derive_sample(d, version) for d in docs]


def _calib_state(draft: dict, samples: list) -> dict:
    cal = draft.get("calibration")
    if not cal:
        return {"present": False, "fresh": False}
    return {"present": True,
            "fresh": cal.get("fingerprint") == calibration_mod.fingerprint(draft, samples),
            "missed": cal.get("missed", 0), "skipped": cal.get("skipped", []),
            "margin": cal.get("margin"), "created_at": cal.get("created_at")}


def _draft_state(tid: str, draft: dict) -> dict:
    samples = _derived_samples(tid, draft)
    summary = recipes_mod.sample_summary(draft, samples)
    cstate = _calib_state(draft, samples) if NN_AVAILABLE else None
    return {"summary": summary, "calibration_state": cstate,
            "check": recipes_mod.publish_check(draft, summary, cstate)}


@app.route("/studio")
def studio_page():
    if getattr(request, "auth_role", "") not in ("admin", "lead"):
        return redirect("/")
    return render_template("studio.html",
                           auth_user=getattr(request, "auth_user", ""),
                           auth_role=getattr(request, "auth_role", ""),
                           auth_group=getattr(request, "auth_group", "") or "")


@app.route("/api/recipes", methods=["GET"])
def studio_list_recipes():
    err = _studio_guard()
    if err:
        return err
    out = []
    # Full documents: migration needs the embedded `versions` history
    for doc in _templates_col.find(_group_filter(), {"_id": 0}):
        recipes_mod.migrate_template(doc, _versions_col)
        draft = _draft_of(doc["id"])
        counts = {"good": 0, "defect": 0}
        for row in _samples_col.aggregate([
                {"$match": {"recipe_id": doc["id"], "excluded": {"$ne": True}}},
                {"$group": {"_id": "$kind", "n": {"$sum": 1}}}]):
            counts[row["_id"]] = row["n"]
        out.append({
            "id": doc["id"], "name": doc.get("name", ""),
            "group": doc.get("group", ""),
            "barcode_mask": doc.get("barcode_mask", ""),
            "active_version": doc.get("active_version") if doc.get("zones") else None,
            "draft_version": draft["version"] if draft else None,
            "zone_count": len((draft or doc).get("zones", [])),
            "samples": counts,
            "updated": doc.get("updated") or doc.get("created", ""),
        })
    out.sort(key=lambda r: r["name"].lower())
    return jsonify({"recipes": out})


@app.route("/api/recipes", methods=["POST"])
def studio_create_recipe():
    """New recipe: header document + draft v1 (nothing visible to operators)."""
    err = _studio_guard()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Recipe name is required"}), 400
    role = getattr(request, "auth_role", "")
    group = ((data.get("group") or "").strip() if role == "admin"
             else (getattr(request, "auth_group", "") or ""))
    tid = uuid.uuid4().hex[:10]
    user = getattr(request, "auth_user", "")
    _templates_col.insert_one({
        "id": tid, "name": name, "group": group,
        "barcode_mask": (data.get("barcode_mask") or "").strip(),
        "created": recipes_mod.now_iso(), "created_by": user,
        "active_version": None, "version": None, "zones": [],
    })
    draft = recipes_mod.new_draft(tid, 1, {
        "name": name, "barcode_mask": (data.get("barcode_mask") or "").strip(),
        "shooting_plan": data.get("shooting_plan") or "zones",
        "operator_notes": data.get("operator_notes") or "",
    }, user)
    draft["parent"] = None
    _versions_col.insert_one(draft)
    return jsonify({"status": "ok", "recipe_id": tid, "version": 1})


@app.route("/api/recipes/<tid>", methods=["GET"])
def studio_get_recipe(tid):
    err = _studio_guard()
    if err:
        return err
    doc = _recipe_or_404(tid)
    if not doc:
        return jsonify({"error": "Recipe not found"}), 404
    versions = list(_versions_col.find(
        {"recipe_id": tid},
        {"_id": 0, "version": 1, "status": 1, "created_at": 1, "created_by": 1,
         "published_at": 1, "published_by": 1, "migrated": 1,
         "ref_shared_legacy": 1}).sort("version", -1))
    draft = _draft_of(tid)
    return jsonify({
        "id": tid, "name": doc.get("name", ""), "group": doc.get("group", ""),
        "barcode_mask": doc.get("barcode_mask", ""),
        "active_version": doc.get("active_version") if doc.get("zones") else None,
        "draft_version": draft["version"] if draft else None,
        "versions": versions,
    })


@app.route("/api/recipes/<tid>/draft", methods=["POST"])
def studio_open_draft(tid):
    """Open a draft: copy of the active version (or of `from_version`)."""
    err = _studio_guard()
    if err:
        return err
    doc = _recipe_or_404(tid)
    if not doc:
        return jsonify({"error": "Recipe not found"}), 404
    existing = _draft_of(tid)
    if existing:
        return jsonify({"status": "ok", "version": existing["version"],
                        "existing": True})
    data = request.get_json(silent=True) or {}
    src_v = data.get("from_version") or doc.get("active_version")
    base = _version_doc(tid, src_v) if src_v else None
    if base is None:
        return jsonify({"error": "Nothing to copy: recipe has no published version"}), 400
    last = _versions_col.find_one({"recipe_id": tid}, sort=[("version", -1)])
    new_v = last["version"] + 1
    draft = recipes_mod.new_draft(tid, new_v, base,
                                  getattr(request, "auth_user", ""))
    if not draft.get("ref_size") and draft.get("ref_image"):
        img, _ = _reference(tid, draft["ref_image"])
        if img is not None:
            draft["ref_size"] = [img.shape[1], img.shape[0]]
            if not draft.get("ref_quality"):
                draft["ref_quality"] = recipes_mod.photo_quality(img)
    _versions_col.insert_one(draft)
    return jsonify({"status": "ok", "version": new_v, "existing": False})


@app.route("/api/recipes/<tid>/draft", methods=["GET"])
def studio_get_draft(tid):
    err = _studio_guard()
    if err:
        return err
    if not _recipe_or_404(tid):
        return jsonify({"error": "Recipe not found"}), 404
    draft = _draft_of(tid)
    if not draft:
        return jsonify({"error": "No draft"}), 404
    out = dict(draft)
    out["image_b64"] = None
    if draft.get("ref_image"):
        data = r2.download_bytes(recipes_mod.ref_key(tid, draft["ref_image"]))
        if data:
            out["image_b64"] = base64.b64encode(data).decode()
    out.update(_draft_state(tid, draft))
    return jsonify(out)


@app.route("/api/recipes/<tid>/draft/state", methods=["GET"])
def studio_draft_state(tid):
    """Sample summary + publish check only (cheap; polled while capturing)."""
    err = _studio_guard()
    if err:
        return err
    if not _recipe_or_404(tid):
        return jsonify({"error": "Recipe not found"}), 404
    draft = _draft_of(tid)
    if not draft:
        return jsonify({"error": "No draft"}), 404
    return jsonify(_draft_state(tid, draft))


_DRAFT_FIELDS = ("name", "barcode_mask", "zones", "shooting_plan",
                 "operator_notes", "zone_sensitivity", "subzone_sensitivity")


@app.route("/api/recipes/<tid>/draft", methods=["PUT"])
def studio_update_draft(tid):
    err = _studio_guard()
    if err:
        return err
    if not _recipe_or_404(tid):
        return jsonify({"error": "Recipe not found"}), 404
    draft = _draft_of(tid)
    if not draft:
        return jsonify({"error": "No draft"}), 404
    data = request.get_json(silent=True) or {}
    upd = {}
    for k in _DRAFT_FIELDS:
        if k not in data:
            continue
        v = data[k]
        if k in ("zone_sensitivity", "subzone_sensitivity") and v is not None:
            try:
                v = max(0.0, min(2.0, float(v)))
            except (TypeError, ValueError):
                return jsonify({"error": f"{k} must be a number 0..2"}), 400
        if k == "shooting_plan" and v not in ("zones", "board"):
            return jsonify({"error": "shooting_plan must be 'zones' or 'board'"}), 400
        if k == "zones":
            zerr = _validate_zones(v)
            if zerr:
                return jsonify({"error": zerr}), 400
        if k in ("name", "barcode_mask", "operator_notes") and isinstance(v, str):
            v = v.strip()
        upd[k] = v
    if "name" in upd and not upd["name"]:
        return jsonify({"error": "Recipe name is required"}), 400
    if upd:
        upd["updated_at"] = recipes_mod.now_iso()
        _versions_col.update_one({"recipe_id": tid, "version": draft["version"]},
                                 {"$set": upd})
    draft.update(upd)
    return jsonify({"status": "ok", **_draft_state(tid, draft)})


def _validate_zones(zones) -> str | None:
    if not isinstance(zones, list):
        return "zones must be a list"
    for i, z in enumerate(zones):
        for k in ("x", "y", "w", "h"):
            if not isinstance(z.get(k), (int, float)):
                return f"Zone {i + 1}: missing '{k}'"
        if z["w"] <= 0 or z["h"] <= 0:
            return f"Zone {i + 1}: empty rectangle"
        for j, sz in enumerate(z.get("subzones") or []):
            for k in ("x", "y", "w", "h"):
                if not isinstance(sz.get(k), (int, float)):
                    return f"Zone {i + 1}, subzone {j + 1}: missing '{k}'"
    return None


@app.route("/api/recipes/<tid>/draft", methods=["DELETE"])
def studio_discard_draft(tid):
    err = _studio_guard()
    if err:
        return err
    if not _recipe_or_404(tid):
        return jsonify({"error": "Recipe not found"}), 404
    draft = _draft_of(tid)
    if not draft:
        return jsonify({"error": "No draft"}), 404
    doc = _templates_col.find_one({"id": tid}, {"_id": 0, "zones": 1})
    if not doc.get("zones"):
        return jsonify({"error": "This is the only version of a new recipe; delete the recipe instead"}), 400
    _versions_col.delete_one({"recipe_id": tid, "version": draft["version"]})
    r2.delete_prefix(f"{_r2_tpl_prefix(tid)}v{draft['version']}/")
    return jsonify({"status": "ok"})


@app.route("/api/recipes/<tid>/draft/reference", methods=["POST"])
def studio_upload_reference(tid):
    """Reference image for the draft, with a quality check. Accepts one
    image (`image`) or a blended image from the auto-blend tool."""
    err = _studio_guard()
    if err:
        return err
    if not _recipe_or_404(tid):
        return jsonify({"error": "Recipe not found"}), 404
    draft = _draft_of(tid)
    if not draft:
        return jsonify({"error": "No draft"}), 404
    f = request.files.get("image")
    if not f or not _allowed(f.filename):
        return jsonify({"error": "Upload a JPG/PNG image"}), 400
    data = f.read()
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jsonify({"error": "Failed to read image"}), 400
    quality = recipes_mod.photo_quality(img)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    ref_image = f"v{draft['version']}/ref.jpg"
    r2.upload_bytes(recipes_mod.ref_key(tid, ref_image), buf.tobytes(), "image/jpeg")
    with _studio_ref_lock:
        _studio_refs.pop((tid, ref_image), None)
    upd = {"ref_image": ref_image, "ref_size": [img.shape[1], img.shape[0]],
           "ref_quality": quality, "ref_shared_legacy": False,
           "updated_at": recipes_mod.now_iso()}
    _versions_col.update_one({"recipe_id": tid, "version": draft["version"]},
                             {"$set": upd})
    draft.update(upd)
    return jsonify({"status": "ok", "quality": quality,
                    "width": img.shape[1], "height": img.shape[0],
                    "image_b64": base64.b64encode(buf.tobytes()).decode(),
                    **_draft_state(tid, draft)})


@app.route("/api/recipes/<tid>/publish", methods=["POST"])
def studio_publish(tid):
    """Make the draft the active version. Blocked while publish_check has
    blockers; an admin may override with {"force": true}."""
    err = _studio_guard()
    if err:
        return err
    doc = _recipe_or_404(tid)
    if not doc:
        return jsonify({"error": "Recipe not found"}), 404
    draft = _draft_of(tid)
    if not draft:
        return jsonify({"error": "No draft"}), 404
    state = _draft_state(tid, draft)
    force = bool((request.get_json(silent=True) or {}).get("force"))
    if not state["check"]["ok"]:
        if not (force and getattr(request, "auth_role", "") == "admin"):
            return jsonify({"error": "Recipe is not ready to publish",
                            **state}), 409
    _activate(tid, draft, forced=not state["check"]["ok"])
    return jsonify({"status": "ok", "version": draft["version"]})


def _activate(tid: str, version: dict, forced: bool = False):
    user = getattr(request, "auth_user", "")
    _versions_col.update_many({"recipe_id": tid,
                               "status": recipes_mod.STATUS_ACTIVE},
                              {"$set": {"status": recipes_mod.STATUS_ARCHIVED}})
    _versions_col.update_one(
        {"recipe_id": tid, "version": version["version"]},
        {"$set": {"status": recipes_mod.STATUS_ACTIVE,
                  "published_at": recipes_mod.now_iso(),
                  "published_by": user, "published_forced": forced}})
    _templates_col.update_one({"id": tid},
                              {"$set": recipes_mod.mirror_update(version)})


@app.route("/api/recipes/<tid>/rollback/<int:ver>", methods=["POST"])
def studio_rollback(tid, ver):
    """Put an archived version back on the line."""
    err = _studio_guard()
    if err:
        return err
    if not _recipe_or_404(tid):
        return jsonify({"error": "Recipe not found"}), 404
    v = _version_doc(tid, ver)
    if not v or v["status"] != recipes_mod.STATUS_ARCHIVED:
        return jsonify({"error": f"v{ver} is not an archived version"}), 404
    _activate(tid, v)
    return jsonify({"status": "ok", "version": ver})


# ─── Calibration ─────────────────────────────────────────────────────────────

_calib_jobs: dict[str, dict] = {}     # recipe id → {state, message, ...}
_calib_jobs_lock = threading.Lock()


def _run_calibration(tid: str, user: str):
    job = _calib_jobs[tid]
    try:
        draft = _draft_of(tid)
        samples = _derived_samples(tid, draft)

        def load_photo(sm):
            data = r2.download_bytes(sm["photo_key"])
            return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)

        def progress(msg, frac=None):
            job["message"] = msg
            if frac is not None:
                job["progress"] = round(frac * 0.95, 3)     # the rest is saving

        old = draft.get("calibration") or {}
        cal, blobs = calibration_mod.calibrate(
            draft, samples, load_photo, progress,
            margin=old.get("margin") or calibration_mod.MARGIN_DEFAULT)
        job.update(message="Saving the model", progress=0.95)
        for z in cal["zones"]:
            key = f"{_r2_tpl_prefix(tid)}v{draft['version']}/calib/zone_{z['zone']}.npz"
            r2.upload_bytes(key, blobs[z["zone"]], "application/octet-stream")
            z["bank_key"] = key
        cal["fingerprint"] = calibration_mod.fingerprint(draft, samples)
        cal["created_at"] = recipes_mod.now_iso()
        cal["created_by"] = user
        _versions_col.update_one({"recipe_id": tid, "version": draft["version"]},
                                 {"$set": {"calibration": cal}})
        job.update(state="done", message="Calibration finished", progress=1.0, finished=time.time())
    except Exception as e:  # report to the UI, keep the service alive
        app.logger.exception("Calibration failed for %s", tid)
        job.update(state="error", message=f"Calibration failed: {e}", finished=time.time())


@app.route("/api/recipes/<tid>/calibrate", methods=["POST"])
def studio_calibrate(tid):
    """Start calibration of the draft in the background."""
    err = _studio_guard()
    if err:
        return err
    if not NN_AVAILABLE:
        return jsonify({"error": "Neural engine unavailable"}), 503
    if not _recipe_or_404(tid):
        return jsonify({"error": "Recipe not found"}), 404
    draft = _draft_of(tid)
    if not draft:
        return jsonify({"error": "No draft"}), 404
    if not draft.get("ref_image") or not draft.get("zones"):
        return jsonify({"error": "Add a reference and zones first"}), 400
    with _calib_jobs_lock:
        job = _calib_jobs.get(tid)
        if job and job["state"] == "running":
            return jsonify({"status": "running", "job": job}), 202
        _calib_jobs[tid] = job = {"state": "running", "message": "Starting",
                                  "progress": 0.0, "started": time.time()}
    threading.Thread(target=_run_calibration,
                     args=(tid, getattr(request, "auth_user", "")),
                     daemon=True, name=f"calib-{tid}").start()
    return jsonify({"status": "started", "job": job}), 202


@app.route("/api/recipes/<tid>/calibrate", methods=["GET"])
def studio_calibration_status(tid):
    err = _studio_guard()
    if err:
        return err
    if not _recipe_or_404(tid):
        return jsonify({"error": "Recipe not found"}), 404
    draft = _draft_of(tid)
    if not draft:
        return jsonify({"error": "No draft"}), 404
    state = _draft_state(tid, draft)
    job = _calib_jobs.get(tid)
    if job:   # server-side elapsed time, so client clock skew doesn't matter
        job = dict(job, elapsed=round((job.get("finished") or time.time()) - job["started"], 1))
    return jsonify({"job": job, "calibration": draft.get("calibration"),
                    "margin_range": calibration_mod.MARGIN_RANGE,
                    "defect_band": calibration_mod.DEFECT_BAND, **state})


@app.route("/api/recipes/<tid>/draft/calibration", methods=["PUT"])
def studio_set_strictness(tid):
    """Change the margin (strictness) of an existing calibration."""
    err = _studio_guard()
    if err:
        return err
    if not _recipe_or_404(tid):
        return jsonify({"error": "Recipe not found"}), 404
    draft = _draft_of(tid)
    if not draft or not draft.get("calibration"):
        return jsonify({"error": "Calibrate first"}), 404
    try:
        margin = float((request.get_json(silent=True) or {}).get("margin"))
    except (TypeError, ValueError):
        return jsonify({"error": "margin must be a number"}), 400
    cal = calibration_mod.apply_margin(draft["calibration"], margin)
    _versions_col.update_one({"recipe_id": tid, "version": draft["version"]},
                             {"$set": {"calibration": cal}})
    draft["calibration"] = cal
    return jsonify({"status": "ok", "calibration": cal, **_draft_state(tid, draft)})


# ─── Samples ─────────────────────────────────────────────────────────────────

def _align_sample(tid: str, version: dict, img) -> dict:
    """Homography of a sample photo onto the version's reference."""
    if not version.get("ref_image"):
        return {"ref_image": None, "H": None, "reason": "no reference yet"}
    ref, sift = _reference(tid, version["ref_image"])
    if ref is None:
        return {"ref_image": version["ref_image"], "H": None,
                "reason": "reference missing"}
    if not NN_AVAILABLE:
        return {"ref_image": version["ref_image"], "H": None,
                "reason": "alignment engine unavailable"}
    M, info = estimate_homography(ref, img, ref_sift=sift)
    return {"ref_image": version["ref_image"],
            "H": M.tolist() if M is not None else None, **info}


def _store_sample(tid: str, version: dict, data: bytes, kind: str,
                  device: str, board: str, source: str) -> dict:
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Failed to read image")
    sid = uuid.uuid4().hex[:12]
    key = f"samples/{tid}/{sid}.jpg"
    r2.upload_bytes(key, data, "image/jpeg")
    doc = {
        "id": sid, "recipe_id": tid, "kind": kind, "photo_key": key,
        "width": img.shape[1], "height": img.shape[0],
        "device": (device or "").strip()[:60] or None,
        "board": (board or "").strip()[:30] or None,
        "source": source,
        "uploaded_by": getattr(request, "auth_user", "") or source,
        "uploaded_at": recipes_mod.now_iso(),
        "quality": recipes_mod.photo_quality(img),
        "align": _align_sample(tid, version, img),
        "defects": [], "excluded": False,
    }
    _samples_col.insert_one(dict(doc))
    return recipes_mod.derive_sample(doc, version)


def _sample_kind(value) -> str | None:
    return value if value in ("good", "defect") else None


@app.route("/api/recipes/<tid>/samples", methods=["POST"])
def studio_add_samples(tid):
    """Upload one or more sample photos (field `photo`, repeatable)."""
    err = _studio_guard()
    if err:
        return err
    if not _recipe_or_404(tid):
        return jsonify({"error": "Recipe not found"}), 404
    draft = _draft_of(tid)
    if not draft:
        return jsonify({"error": "Open a draft first"}), 404
    kind = _sample_kind(request.form.get("kind"))
    if not kind:
        return jsonify({"error": "kind must be 'good' or 'defect'"}), 400
    files = request.files.getlist("photo")
    if not files:
        return jsonify({"error": "No photos"}), 400
    added = []
    for f in files:
        if not _allowed(f.filename or "x.jpg"):
            continue
        try:
            added.append(_store_sample(tid, draft, f.read(), kind,
                                       request.form.get("device", ""),
                                       request.form.get("board", ""),
                                       "upload"))
        except ValueError as e:
            added.append({"error": str(e), "filename": f.filename})
    return jsonify({"samples": added, **_draft_state(tid, draft)})


@app.route("/api/recipes/<tid>/samples", methods=["GET"])
def studio_list_samples(tid):
    err = _studio_guard()
    if err:
        return err
    if not _recipe_or_404(tid):
        return jsonify({"error": "Recipe not found"}), 404
    draft = _draft_of(tid)
    version = draft or _version_doc(tid, _templates_col.find_one(
        {"id": tid}, {"active_version": 1}).get("active_version"))
    if not version:
        return jsonify({"samples": []})
    kind = _sample_kind(request.args.get("kind"))
    return jsonify({"samples": _derived_samples(tid, version, kind)})


def _sample_or_404(sample_id: str):
    s = _samples_col.find_one({"id": sample_id}, {"_id": 0})
    if not s or not _recipe_or_404(s["recipe_id"]):
        return None
    return s


@app.route("/api/samples/<sample_id>/image")
def studio_sample_image(sample_id):
    err = _studio_guard()
    if err:
        return err
    s = _sample_or_404(sample_id)
    if not s:
        return jsonify({"error": "not found"}), 404
    data = r2.download_bytes(s["photo_key"])
    if not data:
        return jsonify({"error": "not found"}), 404
    if request.args.get("size") == "thumb":
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        scale = 360.0 / max(img.shape[:2])
        if scale < 1:
            img = cv2.resize(img, None, fx=scale, fy=scale,
                             interpolation=cv2.INTER_AREA)
        data = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])[1].tobytes()
    resp = send_file(io.BytesIO(data), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp


@app.route("/api/samples/<sample_id>/aligned")
def studio_sample_aligned(sample_id):
    """Sample warped onto the draft reference — used to draw defect boxes in
    reference coordinates."""
    err = _studio_guard()
    if err:
        return err
    s = _sample_or_404(sample_id)
    if not s:
        return jsonify({"error": "not found"}), 404
    version = _draft_of(s["recipe_id"])
    if not version:
        return jsonify({"error": "No draft"}), 404
    d = recipes_mod.derive_sample(s, version)
    if not d["aligned"]:
        return jsonify({"error": "Sample is not aligned to the current reference"}), 409
    data = r2.download_bytes(s["photo_key"])
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    rw, rh = version["ref_size"]
    warped = cv2.warpPerspective(img, np.asarray(s["align"]["H"]), (rw, rh))
    scale = 1600.0 / max(rw, rh)
    if scale < 1:
        warped = cv2.resize(warped, None, fx=scale, fy=scale,
                            interpolation=cv2.INTER_AREA)
    buf = cv2.imencode(".jpg", warped, [cv2.IMWRITE_JPEG_QUALITY, 88])[1]
    return send_file(io.BytesIO(buf.tobytes()), mimetype="image/jpeg")


@app.route("/api/samples/<sample_id>", methods=["PATCH"])
def studio_update_sample(sample_id):
    """Exclude/include, move between good and defect, set board/device,
    or replace defect labels: [{box:{x,y,w,h}, type}] in reference coords."""
    err = _studio_guard()
    if err:
        return err
    s = _sample_or_404(sample_id)
    if not s:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    upd = {}
    if "excluded" in data:
        upd["excluded"] = bool(data["excluded"])
    if "kind" in data:
        kind = _sample_kind(data["kind"])
        if not kind:
            return jsonify({"error": "kind must be 'good' or 'defect'"}), 400
        upd["kind"] = kind
        if kind == "good":
            upd["defects"] = []
    for k in ("device", "board"):
        if k in data:
            upd[k] = (data[k] or "").strip()[:60] or None
    if "defects" in data:
        defects = []
        for d in data["defects"] or []:
            box = d.get("box") or {}
            try:
                box = {k: float(box[k]) for k in ("x", "y", "w", "h")}
            except (KeyError, TypeError, ValueError):
                return jsonify({"error": "Each defect needs box {x,y,w,h}"}), 400
            if not (0 <= box["x"] <= 1 and 0 <= box["y"] <= 1
                    and box["w"] > 0 and box["h"] > 0):
                return jsonify({"error": "Defect box must be inside the image"}), 400
            dtype = d.get("type") if d.get("type") in recipes_mod.DEFECT_TYPES else "other"
            defects.append({"box": box, "type": dtype,
                            "note": (d.get("note") or "")[:200]})
        upd["defects"] = defects
        if defects:
            upd["kind"] = "defect"
    if upd:
        _samples_col.update_one({"id": sample_id}, {"$set": upd})
    s.update(upd)
    version = _draft_of(s["recipe_id"])
    out = recipes_mod.derive_sample(s, version) if version else s
    state = _draft_state(s["recipe_id"], version) if version else {}
    return jsonify({"sample": out, **state})


@app.route("/api/samples/<sample_id>", methods=["DELETE"])
def studio_delete_sample(sample_id):
    err = _studio_guard()
    if err:
        return err
    s = _sample_or_404(sample_id)
    if not s:
        return jsonify({"error": "not found"}), 404
    _samples_col.delete_one({"id": sample_id})
    r2.delete_key(s["photo_key"])
    return jsonify({"status": "ok"})


@app.route("/api/recipes/<tid>/samples/realign", methods=["POST"])
def studio_realign_samples(tid):
    """Re-align samples made against an older reference image."""
    err = _studio_guard()
    if err:
        return err
    if not _recipe_or_404(tid):
        return jsonify({"error": "Recipe not found"}), 404
    draft = _draft_of(tid)
    if not draft:
        return jsonify({"error": "No draft"}), 404
    n = 0
    for s in _samples_col.find({"recipe_id": tid}, {"_id": 0}):
        if (s.get("align") or {}).get("ref_image") == draft.get("ref_image"):
            continue
        data = r2.download_bytes(s["photo_key"])
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) if data else None
        if img is None:
            continue
        _samples_col.update_one({"id": s["id"]},
                                {"$set": {"align": _align_sample(tid, draft, img)}})
        n += 1
    return jsonify({"status": "ok", "realigned": n, **_draft_state(tid, draft)})


# ─── Sample capture from a phone ─────────────────────────────────────────────

@app.route("/api/recipes/<tid>/sample_qr", methods=["POST"])
def studio_sample_qr(tid):
    """QR for the phone page in sample mode, bound to this recipe's draft."""
    err = _studio_guard()
    if err:
        return err
    if not _recipe_or_404(tid):
        return jsonify({"error": "Recipe not found"}), 404
    if not _draft_of(tid):
        return jsonify({"error": "Open a draft first"}), 404
    user = getattr(request, "auth_user", "")
    with _MOBILE_TOKENS_LOCK:
        token = next((t for t, v in _mobile_tokens.items()
                      if v.get("recipe") == tid and v.get("user") == user), None)
        token = token or secrets.token_urlsafe(16)
        _mobile_tokens[token] = {"sid": None, "recipe": tid, "user": user,
                                 "mode": "samples", "ts": time.time()}
        _persist_mobile_tokens()
    base = app.config.get("BASE_URL", "").rstrip("/") or request.host_url.rstrip("/")
    url = f"{base}/mobile/samples?token={token}"
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=8, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="white", back_color="#1a1a2e").save(buf, format="PNG")
    return jsonify({"qr_b64": base64.b64encode(buf.getvalue()).decode(),
                    "url": url, "token": token})


def _sample_token(token: str):
    entry = _mobile_tokens.get(token)
    if not entry or entry.get("mode") != "samples":
        return None, None
    draft = _draft_of(entry["recipe"]) if MONGO_AVAILABLE else None
    return entry, draft


@app.route("/mobile/samples")
def mobile_samples_page():
    resp = make_response(render_template("mobile_samples.html"))
    resp.headers["Cache-Control"] = "no-store"
    return resp


_NO_DRAFT_MSG = ("This recipe has no open draft. Open a draft in Recipe "
                 "Studio, then scan the new QR code.")


@app.route("/api/mobile/<token>/sample_status")
def mobile_sample_status(token):
    entry, draft = _sample_token(token)
    if not entry:
        return jsonify({"error": "Invalid or expired link"}), 404
    if not draft:
        return jsonify({"error": _NO_DRAFT_MSG}), 409
    doc = _templates_col.find_one({"id": entry["recipe"]}, {"_id": 0, "name": 1})
    state = _draft_state(entry["recipe"], draft)
    return jsonify({"recipe": doc.get("name", "") if doc else "",
                    "version": draft["version"],
                    "has_reference": bool(draft.get("ref_image")),
                    "summary": state["summary"],
                    "min_good": recipes_mod.MIN_GOOD_PER_ZONE,
                    "min_defect": recipes_mod.MIN_DEFECT_PER_ZONE})


@app.route("/api/mobile/<token>/sample", methods=["POST"])
def mobile_add_sample(token):
    entry, draft = _sample_token(token)
    if not entry:
        return jsonify({"error": "Invalid or expired link"}), 404
    if not draft:
        return jsonify({"error": _NO_DRAFT_MSG}), 409
    kind = _sample_kind(request.form.get("kind"))
    f = request.files.get("photo")
    if not kind or not f:
        return jsonify({"error": "kind and photo are required"}), 400
    try:
        s = _store_sample(entry["recipe"], draft, f.read(), kind,
                          request.form.get("device", ""),
                          request.form.get("board", ""), "phone")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    _samples_col.update_one({"id": s["id"]},
                            {"$set": {"uploaded_by": entry.get("user", "phone")}})
    return jsonify({"sample": {k: s[k] for k in ("id", "kind", "quality",
                                                 "aligned", "visible_zones")},
                    "summary": _draft_state(entry["recipe"], draft)["summary"]})


# ─── Mobile Camera ────────────────────────────────────────────────────────────

@app.route("/api/session/<sid>/mobile_qr", methods=["POST"])
def generate_mobile_qr(sid):
    """Сгенерировать QR-код для мобильной камеры."""
    if sid not in sessions:
        return jsonify({"error": "Session not found"}), 404

    # Each desktop station owns its token. When the station starts a new
    # session it sends its previous token back, and only that token is
    # rebound — so its phone keeps working and other stations' phones are
    # never redirected to this session.
    data = request.get_json(silent=True) or {}
    prev = data.get("prev_token")
    with _MOBILE_TOKENS_LOCK:
        existing = next((t for t, v in _mobile_tokens.items()
                         if v.get("sid") == sid), None)
        if existing:
            token = existing
        elif isinstance(prev, str) and prev in _mobile_tokens and (
                _mobile_tokens[prev].get("mode") != "samples") and (
                _mobile_tokens[prev].get("sid") not in sessions
                or sessions[_mobile_tokens[prev]["sid"]].get("operator")
                == sessions[sid].get("operator")):
            token = prev
        else:
            token = secrets.token_urlsafe(16)
        _bind_token(token, sid)

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
        return jsonify({"error": "Invalid link"}), 404

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
        return jsonify({"error": "Invalid link"}), 404

    file = request.files.get("photo")
    if not file:
        return jsonify({"error": "Photo not found"}), 400

    data = np.frombuffer(file.read(), np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        return jsonify({"error": "Failed to read image"}), 400

    codes = detect_codes(img, quick=False)
    if not codes:
        return jsonify({"error": "Code not found. Try closer or sharper."}), 422

    best = next((c for c in codes if c["data"]), codes[0])
    serial = best["data"] or "unrecognized"
    sessions[sid]["serial"] = serial
    sessions[sid]["serial_type"] = best["type"]

    return jsonify({"serial": serial, "type": best["type"]})


@app.route("/api/mobile/<token>/retry_failed", methods=["POST"])
def mobile_retry_failed(token):
    """Clear non-OK zones from checked so they can be rescanned."""
    sid = _get_token_session(token)
    if not sid or sid not in sessions:
        return jsonify({"error": "Invalid link"}), 404

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
        return jsonify({"error": "Invalid link"}), 404

    file = request.files.get("photo")
    if not file:
        return jsonify({"error": "Photo not found"}), 400

    photo_bytes = file.read()
    if sid not in mobile_photos:
        mobile_photos[sid] = []
    mobile_photos[sid].append(photo_bytes)
    # Cap queue to prevent unbounded memory growth
    while len(mobile_photos[sid]) > 30:
        mobile_photos[sid].pop(0)

    return jsonify({"status": "ok", "count": len(mobile_photos[sid])})


@app.route("/api/session/<sid>/mobile_photos", methods=["GET"])
def get_mobile_photos(sid):
    """Desktop polls this to check for new photos from mobile."""
    if sid not in sessions:
        return jsonify({"error": "Session not found"}), 404

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
        return jsonify({"error": "Session not found"}), 404

    photos = mobile_photos.get(sid, [])
    if not photos:
        return jsonify({"error": "No photos in queue"}), 404

    photo_bytes = photos.pop(0)
    img = cv2.imdecode(np.frombuffer(photo_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jsonify({"error": "Failed to read image"}), 400

    # Downscale large mobile photos to match reference size (speed up SIFT)
    ref_img = sessions[sid]["ref_img"]
    ref_h, ref_w = ref_img.shape[:2]
    h, w = img.shape[:2]
    max_dim = max(ref_h, ref_w) * 2  # Allow up to 2x reference size
    if max(h, w) > max_dim:
        scale = max_dim / max(h, w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)))

    return _check_zone_impl(sid, photo_img=img)


# ─── R2 diagnostics ───────────────────────────────────────────────────────

@app.route("/api/r2/status")
def r2_status():
    """Check R2 connectivity and list object counts (admin only)."""
    if getattr(request, "auth_role", "") != "admin":
        return jsonify({"error": "Admin only"}), 403
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
    except Exception:
        app.logger.exception("R2 status check failed")
        return jsonify({"status": "error"}), 500


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import logging as _logging
    _logging.getLogger("werkzeug").setLevel(_logging.WARNING)
    _port = int(_os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=_port, debug=False)
