"""
Neural Inspection Engine — глубокие признаки для инспекции PCB.

Использует предобученный EfficientNet-B4 для:
  - Устойчивого сопоставления зон (match_score) — инвариантно к бликам, ракурсу, масштабу
  - Глубокой детекции дефектов (analyze_defects) — multi-scale сравнение feature maps

Backbone можно заменить на DINOv2 или ConvNeXt для ещё большей точности.
Для production-оптимизации: экспорт в ONNX → OpenVINO / TensorRT.

Requires: torch >= 2.1, torchvision >= 0.16
"""

import base64
import ssl
import os

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import models, transforms
from skimage.metrics import structural_similarity as ssim_func

import inspection_config as cfg

# Fix SSL certificate verification on macOS
if not os.environ.get("SSL_CERT_FILE"):
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
        ssl._create_default_https_context = ssl.create_default_context
    except ImportError:
        ssl._create_default_https_context = ssl._create_unverified_context


# ═══════════════════════════════════════════════════════════════════════════════
#  DEVICE
# ═══════════════════════════════════════════════════════════════════════════════

def _detect_device():
    if torch.cuda.is_available():
        return torch.device("cuda")
    try:
        if torch.backends.mps.is_available():
            torch.zeros(1, device="mps")  # smoke test
            return torch.device("mps")
    except Exception:
        pass
    return torch.device("cpu")


DEVICE = _detect_device()
NN_DEVICE = str(DEVICE)

# ═══════════════════════════════════════════════════════════════════════════════
#  PREPROCESSING
# ═══════════════════════════════════════════════════════════════════════════════

INPUT_SIZE = (380, 380)

_transform = transforms.Compose([
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406],
                         std=[0.229, 0.224, 0.225]),
])


def _to_tensor(bgr_img, size=INPUT_SIZE):
    """BGR numpy → normalized tensor on DEVICE."""
    if bgr_img.ndim == 2:
        bgr_img = cv2.cvtColor(bgr_img, cv2.COLOR_GRAY2BGR)
    rgb = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2RGB)
    rgb = cv2.resize(rgb, size)
    return _transform(rgb).unsqueeze(0).to(DEVICE)


# ═══════════════════════════════════════════════════════════════════════════════
#  FEATURE EXTRACTOR
# ═══════════════════════════════════════════════════════════════════════════════

class _MultiScaleExtractor(nn.Module):
    """
    EfficientNet-B4 backbone с извлечением промежуточных feature maps.
    Слои 2, 3, 5, 7 дают признаки на разных масштабах:
      - Ранние (2,3): текстуры, локальные паттерны
      - Поздние (5,7): семантические признаки высокого уровня
    """
    LAYERS = [2, 3, 5, 7]

    def __init__(self):
        super().__init__()
        weights = models.EfficientNet_B4_Weights.DEFAULT
        backbone = models.efficientnet_b4(weights=weights)
        self.blocks = nn.ModuleList(list(backbone.features.children()))
        self.pool = nn.AdaptiveAvgPool2d(1)

    @torch.no_grad()
    def forward(self, x):
        feats = {}
        for i, block in enumerate(self.blocks):
            x = block(x)
            if i in self.LAYERS:
                feats[i] = x
        glob = self.pool(x).flatten(1)
        return glob, feats


_model_instance = None


def _model():
    """Lazy singleton — загружает модель при первом вызове."""
    global _model_instance
    if _model_instance is None:
        print("🧠 Loading EfficientNet-B4 feature extractor...")
        _model_instance = _MultiScaleExtractor().to(DEVICE).eval()
        print(f"   ✓ Loaded on {NN_DEVICE}")
    return _model_instance


# ═══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _b64(img):
    """BGR numpy → base64 JPEG string."""
    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return base64.b64encode(buf.tobytes()).decode()


# ═══════════════════════════════════════════════════════════════════════════════
#  PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════════

@torch.no_grad()
def extract_features(img: np.ndarray, preprocess=None):
    """
    Extract NN features for an image (for caching).
    preprocess: optional callable(img) -> BGR ndarray, applied before _to_tensor.
    Returns: (glob, feats) tuple — tensors on DEVICE.
    Caller should .clone() if storing long-term.
    """
    m = _model()
    if preprocess is not None:
        img = preprocess(img)
    t = _to_tensor(img)
    glob, feats = m(t)
    return glob, feats


@torch.no_grad()
def extract_features_batch(images: list[np.ndarray], preprocess=None):
    """
    Batch feature extraction for multiple images.
    Returns: list of (glob, feats) tuples, one per image.
    """
    if not images:
        return []
    m = _model()
    tensors = []
    for img in images:
        if preprocess is not None:
            img = preprocess(img)
        tensors.append(_to_tensor(img))
    batch = torch.cat(tensors, dim=0)
    globs, feats_dict = m(batch)
    # Split back into individual results
    results = []
    for i in range(len(images)):
        g = globs[i:i+1]
        f = {k: v[i:i+1] for k, v in feats_dict.items()}
        results.append((g, f))
    return results


@torch.no_grad()
def match_score_nn(zone_crop: np.ndarray, photo: np.ndarray) -> float:
    """
    Нейросетевой скор сопоставления зоны с фото.
    Используется ТОЛЬКО для предварительного ранжирования зон (какая зона лучше).
    Лёгкий: global cosine similarity по pooled features.
    """
    m = _model()
    z_t = _to_tensor(zone_crop)
    p_t = _to_tensor(photo)
    z_glob, _ = m(z_t)
    p_glob, _ = m(p_t)
    g_sim = F.cosine_similarity(z_glob, p_glob).item()
    return round(min(max(g_sim, 0.0), 1.0), 4)


@torch.no_grad()
def similarity_nn(img_a: np.ndarray, img_b: np.ndarray) -> float:
    """
    Детальное сходство двух изображений одного размера.
    Используется для валидации: zone_crop vs extracted.
    Комбинирует global + spatial patch similarity.
    """
    m = _model()
    a_t = _to_tensor(img_a)
    b_t = _to_tensor(img_b)
    _, a_feats = m(a_t)
    _, b_feats = m(b_t)
    return _similarity_from_feats(a_feats, b_feats)


@torch.no_grad()
def similarity_nn_from_feats(a_feats, b_feats) -> float:
    """Same as similarity_nn but using pre-extracted features."""
    return _similarity_from_feats(a_feats, b_feats)


def _similarity_from_feats(a_feats, b_feats) -> float:
    """Compute similarity score from feature dicts."""
    # Global similarity по слою 7
    a_pool = F.adaptive_avg_pool2d(a_feats[7], 1).flatten()
    b_pool = F.adaptive_avg_pool2d(b_feats[7], 1).flatten()
    g_sim = float(F.cosine_similarity(a_pool.unsqueeze(0),
                                      b_pool.unsqueeze(0)).item())

    # Patch-level mean similarity по слою 5 (пространственное сравнение)
    a_fn = F.normalize(a_feats[5], dim=1)
    b_fn = F.normalize(b_feats[5], dim=1)
    patch_sim = (a_fn * b_fn).sum(dim=1).mean().item()

    # Комбинация: 50% global + 50% patch
    score = 0.5 * max(g_sim, 0) + 0.5 * max(patch_sim, 0)
    return round(min(max(score, 0.0), 1.0), 4)


@torch.no_grad()
def similarity_nn_batch(pairs: list[tuple[np.ndarray, np.ndarray]]) -> list[float]:
    """
    Batch similarity scoring for multiple (img_a, img_b) pairs.
    Single forward pass for all images, then pairwise similarity.
    Returns: list of scores, one per pair.
    """
    if not pairs:
        return []
    m = _model()
    # Build one batch: [a0, b0, a1, b1, ...]
    tensors = []
    for img_a, img_b in pairs:
        tensors.append(_to_tensor(img_a))
        tensors.append(_to_tensor(img_b))
    batch = torch.cat(tensors, dim=0)
    _, all_feats = m(batch)

    scores = []
    for i in range(len(pairs)):
        a_idx = i * 2
        b_idx = i * 2 + 1
        a_f = {k: v[a_idx:a_idx+1] for k, v in all_feats.items()}
        b_f = {k: v[b_idx:b_idx+1] for k, v in all_feats.items()}
        scores.append(_similarity_from_feats(a_f, b_f))
    return scores


def align_photo_to_ref(ref_img: np.ndarray, photo: np.ndarray):
    """
    Глобальное выравнивание: warp фото в координаты референса.
    Вызывать ОДИН РАЗ на фото — затем кропать зоны из результата.
    Returns: warped photo (same size as ref_img) or None.
    """
    max_dim = 1200
    rh, rw = ref_img.shape[:2]
    ph, pw = photo.shape[:2]
    r_scale = min(max_dim / max(rh, rw), 1.0)
    p_scale = min(max_dim / max(ph, pw), 1.0)

    ref_small = cv2.resize(ref_img, None, fx=r_scale, fy=r_scale)
    photo_small = cv2.resize(photo, None, fx=p_scale, fy=p_scale)

    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    ref_gray = clahe.apply(cv2.cvtColor(ref_small, cv2.COLOR_BGR2GRAY))
    photo_gray = clahe.apply(cv2.cvtColor(photo_small, cv2.COLOR_BGR2GRAY))

    sift = cv2.SIFT_create(nfeatures=5000)
    kp1, des1 = sift.detectAndCompute(ref_gray, None)
    kp2, des2 = sift.detectAndCompute(photo_gray, None)

    if des1 is None or des2 is None or len(des1) < 10 or len(des2) < 10:
        return None

    index_params = dict(algorithm=1, trees=5)
    search_params = dict(checks=50)
    flann = cv2.FlannBasedMatcher(index_params, search_params)
    matches = flann.knnMatch(des1, des2, k=2)

    good = [m for m_pair in matches if len(m_pair) == 2
            for m, n in [m_pair] if m.distance < 0.7 * n.distance]
    if len(good) < 15:
        return None

    src_pts = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    dst_pts = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    M, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 3.0)

    if M is None or mask is None:
        return None
    inlier_ratio = mask.sum() / len(mask)
    if inlier_ratio < 0.25:
        return None

    S_src = np.diag([p_scale, p_scale, 1.0])
    S_dst_inv = np.diag([1 / r_scale, 1 / r_scale, 1.0])
    M_full = S_dst_inv @ M @ S_src

    warped = cv2.warpPerspective(photo, M_full, (rw, rh))
    return warped


@torch.no_grad()
def locate_and_extract_nn(zone_crop: np.ndarray, photo: np.ndarray,
                          ref_img: np.ndarray = None, zone: dict = None):
    """
    Найти зону в фото и извлечь выровненный фрагмент.

    Стратегия (от лучшей к запасной):
      A) Если есть ref_img + zone: глобальное выравнивание всего фото
         к референсу через SIFT homography → вырезаем по координатам зоны.
         Самый надёжный метод — используются ВСЕ фичи обоих изображений.
      B) Если фото крупнее зоны: SIFT homography zone_crop → photo
      C) Fallback: multi-scale template matching

    Returns: (extracted_region, method: str)
    """
    zh, zw = zone_crop.shape[:2]

    # ═══════════════════════════════════════════════════════════════════════
    # Strategy A: Global alignment (ref → photo) + crop by zone coords
    # ═══════════════════════════════════════════════════════════════════════
    if ref_img is not None and zone is not None:
        extracted = _try_global_alignment(ref_img, photo, zone)
        if extracted is not None:
            result = cv2.resize(extracted, (zw, zh))
            return result, "global_homography"

    # ═══════════════════════════════════════════════════════════════════════
    # Strategy B: Local SIFT homography (zone_crop → photo)
    # ═══════════════════════════════════════════════════════════════════════
    extracted = _try_local_sift(zone_crop, photo)
    if extracted is not None:
        return extracted, "local_homography"

    # ═══════════════════════════════════════════════════════════════════════
    # Strategy C: Multi-scale template matching
    # ═══════════════════════════════════════════════════════════════════════
    extracted = _try_template_match(zone_crop, photo)
    return cv2.resize(extracted, (zw, zh)), "template_match"


def _try_global_alignment(ref_img, photo, zone):
    """
    Выравниваем ЦЕЛОЕ фото к ЦЕЛОМУ референсу через SIFT,
    затем вырезаем зону по известным координатам.
    """
    # Уменьшаем для скорости SIFT (работаем с копиями не больше 1200px)
    max_dim = 1200
    rh, rw = ref_img.shape[:2]
    ph, pw = photo.shape[:2]
    r_scale = min(max_dim / max(rh, rw), 1.0)
    p_scale = min(max_dim / max(ph, pw), 1.0)

    ref_small = cv2.resize(ref_img, None, fx=r_scale, fy=r_scale)
    photo_small = cv2.resize(photo, None, fx=p_scale, fy=p_scale)

    ref_gray = cv2.cvtColor(ref_small, cv2.COLOR_BGR2GRAY)
    # CLAHE для устойчивости к бликам
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    ref_gray = clahe.apply(ref_gray)
    photo_gray = clahe.apply(
        cv2.cvtColor(photo_small, cv2.COLOR_BGR2GRAY))

    sift = cv2.SIFT_create(nfeatures=5000)
    kp1, des1 = sift.detectAndCompute(ref_gray, None)
    kp2, des2 = sift.detectAndCompute(photo_gray, None)

    if des1 is None or des2 is None or len(des1) < 10 or len(des2) < 10:
        return None

    # FLANN matcher — быстрее BF для больших наборов фичей
    index_params = dict(algorithm=1, trees=5)  # FLANN_INDEX_KDTREE
    search_params = dict(checks=50)
    flann = cv2.FlannBasedMatcher(index_params, search_params)
    matches = flann.knnMatch(des1, des2, k=2)

    good = [m for m_pair in matches if len(m_pair) == 2
            for m, n in [m_pair] if m.distance < 0.7 * n.distance]

    if len(good) < 15:
        return None

    # Гомография: photo → ref (чтобы вырезать из warped photo по координатам ref)
    src_pts = np.float32(
        [kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    dst_pts = np.float32(
        [kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    M, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 3.0)

    if M is None or mask is None:
        return None

    inlier_ratio = mask.sum() / len(mask)
    if inlier_ratio < 0.25:
        return None

    # Компенсируем масштабирование: переводим M из small-space в full-space
    S_src = np.diag([p_scale, p_scale, 1.0])    # photo scaling
    S_dst_inv = np.diag([1/r_scale, 1/r_scale, 1.0])  # ref inverse scaling
    M_full = S_dst_inv @ M @ S_src

    # Warp фото в координаты референса
    warped = cv2.warpPerspective(photo, M_full, (rw, rh))

    # Вырезаем зону по координатам
    x1 = int(zone["x"] * rw)
    y1 = int(zone["y"] * rh)
    x2 = int((zone["x"] + zone["w"]) * rw)
    y2 = int((zone["y"] + zone["h"]) * rh)
    # Добавляем небольшой margin внутрь для компенсации неточности выравнивания
    margin_x = int((x2 - x1) * 0.03)
    margin_y = int((y2 - y1) * 0.03)
    x1 = max(0, x1 + margin_x)
    y1 = max(0, y1 + margin_y)
    x2 = min(rw, x2 - margin_x)
    y2 = min(rh, y2 - margin_y)

    crop = warped[y1:y2, x1:x2]
    if crop.size == 0:
        return None

    # Проверяем долю чёрных (невалидных) пикселей в кропе
    gray_crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    black_ratio = (gray_crop < 5).sum() / gray_crop.size
    if black_ratio > 0.15:
        return None

    return crop


def _try_local_sift(zone_crop, photo):
    """Local SIFT: ищем zone_crop в photo через homography."""
    zh, zw = zone_crop.shape[:2]
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    ref_gray = clahe.apply(cv2.cvtColor(zone_crop, cv2.COLOR_BGR2GRAY))
    photo_gray = clahe.apply(cv2.cvtColor(photo, cv2.COLOR_BGR2GRAY))

    sift = cv2.SIFT_create(nfeatures=3000)
    kp1, des1 = sift.detectAndCompute(ref_gray, None)
    kp2, des2 = sift.detectAndCompute(photo_gray, None)

    if des1 is None or des2 is None or len(des1) < 8 or len(des2) < 8:
        return None

    bf = cv2.BFMatcher(cv2.NORM_L2)
    matches = bf.knnMatch(des1, des2, k=2)
    good = [m for m_pair in matches if len(m_pair) == 2
            for m, n in [m_pair] if m.distance < 0.7 * n.distance]

    if len(good) < 10:
        return None

    src_pts = np.float32(
        [kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    dst_pts = np.float32(
        [kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    M, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)

    if M is None or mask is None or mask.sum() < 8:
        return None

    warped = cv2.warpPerspective(photo, M, (zw, zh))
    return warped


def _try_template_match(zone_crop, photo):
    """Multi-scale template matching как последний fallback."""
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    ref_gray = clahe.apply(cv2.cvtColor(zone_crop, cv2.COLOR_BGR2GRAY))
    photo_gray = clahe.apply(cv2.cvtColor(photo, cv2.COLOR_BGR2GRAY))

    rh, rw = ref_gray.shape[:2]
    ph, pw = photo_gray.shape[:2]
    best_val, best_loc, best_scale = 0.0, (0, 0), 1.0

    for scale in [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.5]:
        tw = int(rw * scale)
        th = int(rh * scale)
        if tw >= pw or th >= ph or tw < 20 or th < 20:
            continue
        tmpl = cv2.resize(ref_gray, (tw, th))
        res = cv2.matchTemplate(photo_gray, tmpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val > best_val:
            best_val, best_loc, best_scale = max_val, max_loc, scale

    tw = int(rw * best_scale)
    th = int(rh * best_scale)
    x, y = best_loc
    crop = photo[y:y + th, x:x + tw]
    if crop.size == 0:
        return photo
    return crop


@torch.no_grad()
def analyze_defects_nn(zone_crop: np.ndarray, extracted: np.ndarray,
                       extract_nn_score: float = 0.0,
                       strict: bool = False,
                       zone_feats=None, extracted_feats=None) -> dict:
    """
    Детекция дефектов через patch-based CNN comparison.

    Подход (простой, устойчивый к свету/углу):
      1. EfficientNet feature maps → пространственная карта сходства (patch-level)
      2. Глобальное сходство (pooled features) + SSIM
      3. Патчи с низким сходством = потенциальные дефекты
      4. defect_pct = взвешенная комбинация patch defects, global_sim, SSIM

    zone_feats / extracted_feats: pre-computed (glob, feats) from extract_features().
      If provided, skips the corresponding forward pass (saves ~50-150ms each).

    strict=True: подзоны — жёсткие пороги из SUBZONE_* в inspection_config.py.
    """
    m = _model()

    # Thresholds: обычные vs строгие (подзоны)
    patch_sim_thr = cfg.SUBZONE_PATCH_SIM_THRESHOLD if strict else cfg.PATCH_SIM_THRESHOLD
    patch_def_w = cfg.SUBZONE_PATCH_DEFECT_WEIGHT if strict else cfg.PATCH_DEFECT_WEIGHT
    ok_thr = cfg.SUBZONE_VERDICT_OK_THRESHOLD if strict else cfg.VERDICT_OK_THRESHOLD
    warn_thr = cfg.SUBZONE_VERDICT_WARN_THRESHOLD if strict else cfg.VERDICT_WARN_THRESHOLD
    safety_ssim = cfg.SUBZONE_SAFETY_SSIM_LOW if strict else cfg.SAFETY_SSIM_LOW
    safety_sim = cfg.SUBZONE_SAFETY_SIM_LOW if strict else cfg.SAFETY_SIM_LOW

    # ─── Предобработка ────────────────────────────────────────────────────
    z_lab = cv2.cvtColor(zone_crop, cv2.COLOR_BGR2LAB)
    e_lab = cv2.cvtColor(extracted, cv2.COLOR_BGR2LAB)
    clahe_pre = cv2.createCLAHE(clipLimit=cfg.CLAHE_CLIP_LIMIT,
                                tileGridSize=cfg.CLAHE_TILE_SIZE)
    z_lab[:, :, 0] = clahe_pre.apply(z_lab[:, :, 0])
    e_lab[:, :, 0] = clahe_pre.apply(e_lab[:, :, 0])
    z_norm = cv2.cvtColor(z_lab, cv2.COLOR_LAB2BGR)
    e_norm = cv2.cvtColor(e_lab, cv2.COLOR_LAB2BGR)

    z_blur = cv2.GaussianBlur(z_norm, cfg.PRE_BLUR_KERNEL, 0)
    e_blur = cv2.GaussianBlur(e_norm, cfg.PRE_BLUR_KERNEL, 0)

    # Use cached features or compute fresh
    if zone_feats is not None:
        _, z_feats = zone_feats
    else:
        z_t = _to_tensor(z_blur)
        _, z_feats = m(z_t)
    if extracted_feats is not None:
        _, e_feats = extracted_feats
    else:
        e_t = _to_tensor(e_blur)
        _, e_feats = m(e_t)

    # ─── 1. Глобальное сходство ───────────────────────────────────────────
    z_pool = F.adaptive_avg_pool2d(z_feats[7], 1).flatten()
    e_pool = F.adaptive_avg_pool2d(e_feats[7], 1).flatten()
    global_sim = float(F.cosine_similarity(
        z_pool.unsqueeze(0), e_pool.unsqueeze(0)).item())

    # ─── 2. SSIM ──────────────────────────────────────────────────────────
    sz = (256, 256)
    a_g = clahe_pre.apply(cv2.cvtColor(
        cv2.resize(zone_crop, sz), cv2.COLOR_BGR2GRAY))
    b_g = clahe_pre.apply(cv2.cvtColor(
        cv2.resize(extracted, sz), cv2.COLOR_BGR2GRAY))
    a_g = cv2.GaussianBlur(a_g, cfg.SSIM_BLUR_KERNEL, 0)
    b_g = cv2.GaussianBlur(b_g, cfg.SSIM_BLUR_KERNEL, 0)
    ssim_val = float(ssim_func(a_g, b_g, data_range=255))

    # ─── 3. Patch-based CNN similarity map ────────────────────────────────
    # Сравниваем feature maps на уровне патчей (каждая позиция = ~32x32 px)
    z_feat = z_feats[cfg.PATCH_LAYER]
    e_feat = e_feats[cfg.PATCH_LAYER]
    z_fn = F.normalize(z_feat, dim=1)
    e_fn = F.normalize(e_feat, dim=1)
    # Cosine similarity на каждой пространственной позиции
    patch_sim = (z_fn * e_fn).sum(dim=1).squeeze(0)  # [H, W]
    patch_h, patch_w = patch_sim.shape

    # ─── 3b. Texture-weighted patches ─────────────────────────────────────
    # Однотонные патчи (низкий градиент) ненадёжны для CNN сравнения —
    # разница освещения создаёт ложные дефекты. Взвешиваем по текстуре.
    ref_gray = cv2.cvtColor(cv2.resize(zone_crop, (patch_w * 16, patch_h * 16)),
                            cv2.COLOR_BGR2GRAY)
    # Laplacian variance per patch block
    lap = cv2.Laplacian(ref_gray, cv2.CV_64F)
    lap_abs = np.abs(lap)
    # Средний градиент на каждый патч — vectorized reshape+mean
    block_h, block_w = 16, 16
    texture_map = lap_abs.reshape(patch_h, block_h, patch_w, block_w).mean(
        axis=(1, 3)).astype(np.float32)

    # Нормализуем: 0 = однотонный, 1 = текстурный
    tex_max = texture_map.max()
    if tex_max > 0:
        texture_weight = np.clip(texture_map / (tex_max * 0.3), 0.0, 1.0)
    else:
        texture_weight = np.ones_like(texture_map)

    # Минимальный вес для однотонных патчей (не полный ноль)
    texture_weight = np.clip(texture_weight, 0.15, 1.0)

    # Патчи с низким сходством = потенциальные дефекты, взвешенные текстурой
    raw_defect_patches = (patch_sim < patch_sim_thr).float()
    tex_w = torch.from_numpy(texture_weight).to(raw_defect_patches.device)
    defect_patches = raw_defect_patches * tex_w
    patch_defect_ratio = float(defect_patches.sum() / (patch_h * patch_w))

    # ─── 4. Вычисление defect_pct ────────────────────────────────────────
    # Основа: доля дефектных патчей × вес
    raw_defect = patch_defect_ratio * patch_def_w

    if strict:
        # ── Подзоны: другой подход ──
        # 1) НЕ используем extract_nn_score от родительской зоны
        best_global = global_sim

        # 2) Гистограммное сравнение (ловит чужие объекты / цветовые сдвиги)
        hsv_ref = cv2.cvtColor(cv2.resize(
            zone_crop, (128, 128)), cv2.COLOR_BGR2HSV)
        hsv_ext = cv2.cvtColor(cv2.resize(
            extracted, (128, 128)), cv2.COLOR_BGR2HSV)
        hist_corrs = []
        for ch in range(3):
            h_ref = cv2.calcHist([hsv_ref], [ch], None, [64], [0, 256])
            h_ext = cv2.calcHist([hsv_ext], [ch], None, [64], [0, 256])
            cv2.normalize(h_ref, h_ref)
            cv2.normalize(h_ext, h_ext)
            hist_corrs.append(cv2.compareHist(
                h_ref, h_ext, cv2.HISTCMP_CORREL))
        hist_corr = sum(hist_corrs) / len(hist_corrs)

        # 3) Комбинированный скор подзоны
        #    Если гистограмма или SSIM или global_sim очень низкие → defect
        if hist_corr < cfg.SUBZONE_HIST_COMPLETELY_DIFF or (
                ssim_val < cfg.SUBZONE_SSIM_COMPLETELY_DIFF
                and global_sim < cfg.SUBZONE_SIM_COMPLETELY_DIFF):
            # Совершенно разные изображения
            defect_pct = max(raw_defect, cfg.SUBZONE_FORCED_DEFECT_PCT)
        elif hist_corr < cfg.SUBZONE_HIST_SIGNIFICANT_DIFF or (
                ssim_val < cfg.SUBZONE_SSIM_SIGNIFICANT_DIFF
                and global_sim < cfg.SUBZONE_SIM_SIGNIFICANT_DIFF):
            # Значительные различия — минимальный дисконт
            defect_pct = round(raw_defect * cfg.SUBZONE_DISCOUNT_MINIMAL, 2)
        elif best_global >= cfg.GLOBAL_SIM_OK:
            defect_pct = round(raw_defect * cfg.SUBZONE_DISCOUNT_GOOD, 2)
        elif best_global >= cfg.GLOBAL_SIM_WARN:
            t = (best_global - cfg.GLOBAL_SIM_WARN) / \
                max(cfg.GLOBAL_SIM_OK - cfg.GLOBAL_SIM_WARN, 0.01)
            discount = 1.0 - t * (1.0 - cfg.SUBZONE_DISCOUNT_GOOD)
            defect_pct = round(raw_defect * discount, 2)
        else:
            defect_pct = round(raw_defect, 2)
    else:
        # ── Полные зоны: обычная логика с дисконтом ──
        best_global = max(global_sim, extract_nn_score)

        ssim_cutoff = getattr(cfg, 'SSIM_DISCOUNT_CUTOFF', 0.42)
        if best_global >= cfg.GLOBAL_SIM_OK:
            defect_pct = round(raw_defect * cfg.PATCH_HIGH_SIM_DISCOUNT, 2)
        elif best_global >= cfg.GLOBAL_SIM_WARN:
            t = (best_global - cfg.GLOBAL_SIM_WARN) / \
                max(cfg.GLOBAL_SIM_OK - cfg.GLOBAL_SIM_WARN, 0.01)
            discount = 1.0 - t * (1.0 - cfg.PATCH_HIGH_SIM_DISCOUNT)
            defect_pct = round(raw_defect * discount, 2)
        elif ssim_val < ssim_cutoff:
            defect_pct = round(raw_defect * 0.85, 2)
        else:
            defect_pct = round(raw_defect, 2)

    defect_pct = min(defect_pct, 100.0)

    # ─── 5. Визуализация ──────────────────────────────────────────────────
    vis_size = (256, 256)
    b_vis = cv2.resize(extracted, vis_size)

    # Similarity map → heatmap (красный = различия, синий = совпадения)
    sim_np = patch_sim.cpu().numpy()
    sim_resized = cv2.resize(sim_np, vis_size, interpolation=cv2.INTER_LINEAR)
    # Инвертируем: 0 = похоже (синий), 1 = различие (красный)
    diff_map = np.clip((1.0 - sim_resized) * 255, 0, 255).astype(np.uint8)
    heatmap = cv2.applyColorMap(diff_map, cv2.COLORMAP_JET)
    vis_heatmap = cv2.addWeighted(b_vis, 0.55, heatmap, 0.45, 0)

    # Карта дефектов: контуры вокруг дефектных зон (threshold weighted mask)
    vis_defects = b_vis.copy()
    defect_mask_np = (defect_patches.cpu().numpy()
                      > 0.3).astype(np.uint8) * 255
    defect_mask_resized = cv2.resize(defect_mask_np, vis_size,
                                     interpolation=cv2.INTER_NEAREST)
    contours, _ = cv2.findContours(defect_mask_resized, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    defect_count = len(contours)

    # ─── 6. Вердикт ──────────────────────────────────────────────────────
    # ok_thr / warn_thr / safety_ssim / safety_sim set at function top

    if defect_pct < ok_thr:
        # Safety net: если и global_sim и SSIM очень низкие — не верим OK
        if ssim_val < safety_ssim and global_sim < safety_sim:
            defect_pct = max(defect_pct, cfg.SAFETY_DEFECT_OVERRIDE_PCT)
            # verdict = "Дефект ❌ — структурные различия (низкий SSIM/similarity)"
            verdict = "Defect ❌ — structural differences (low SSIM/similarity)"
            status = "defect"
        else:
            verdict = "OK ✅ — no differences detected"
            status = "ok"
    elif defect_pct < warn_thr:
        verdict = "Warning ⚠️ — minor differences"
        status = "warn"
    else:
        verdict = "Defect ❌ — significant differences detected"
        status = "defect"

    # Всегда рисуем контуры дефектов (для ok — полупрозрачно)
    if contours:
        red_overlay = np.zeros_like(vis_defects)
        red_overlay[:, :, 2] = defect_mask_resized
        alpha = 0.4 if status != "ok" else 0.15
        vis_defects = cv2.addWeighted(vis_defects, 1.0, red_overlay, alpha, 0)
        color = (0, 0, 255) if status != "ok" else (0, 100, 255)
        thick = 2 if status != "ok" else 1
        cv2.drawContours(vis_defects, contours, -1, color, thick)

    return {
        "ssim": round(ssim_val, 4),
        "defect_pct": defect_pct,
        "defect_count": defect_count,
        "verdict": verdict,
        "status": status,
        "vis_defects_b64": _b64(vis_defects),
        "vis_heatmap_b64": _b64(vis_heatmap),
        "extracted_b64": _b64(cv2.resize(extracted, vis_size)),
        "reference_b64": _b64(cv2.resize(zone_crop, vis_size)),
    }
