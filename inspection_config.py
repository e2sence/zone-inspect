"""
Конфигурация чувствительности детекции дефектов.

Подход: patch-based CNN comparison (EfficientNet feature maps)
      + глобальное NN-сходство + SSIM.

"""


# ─── Предварительная обработка ────────────────────────────────────────────
import sys as _sys
CLAHE_CLIP_LIMIT = 2.0          # clipLimit для CLAHE выравнивания яркости
CLAHE_TILE_SIZE = (8, 8)        # размер тайлов CLAHE
PRE_BLUR_KERNEL = (7, 7)        # GaussianBlur перед извлечением фич

# ─── Слои EfficientNet ───────────────────────────────────────────────────
FEATURE_LAYERS = [5, 7]
# веса для объединения карт сходства разных слоев
LAYER_WEIGHTS = {5: 0.35, 7: 0.65}

# ─── Общие зоны: ЕДИНЫЙ параметр чувствительности ─────────────────────────
# Диапазон 0.0 (мягко) … 2.0 (ультра-строго). Все пороги ниже рассчитываются от него.
ZONE_SENSITIVITY = 0.500

# ─── Patch-based CNN comparison ───────────────────────────────────────────
PATCH_LAYER = 5                  # слой EfficientNet для patch map (5→~16x16)
_ZS = ZONE_SENSITIVITY
PATCH_SIM_THRESHOLD = round(0.42 + _ZS * 0.20, 3)        # 0.42…0.62
PATCH_DEFECT_WEIGHT = round(25.0 + _ZS * 20.0, 3)        # 25…45
# 0.20…0.60 дисконт при высоком глобальном сходстве
PATCH_HIGH_SIM_DISCOUNT = round(0.20 + _ZS * 0.40, 3)

# ─── Глобальные пороги ───────────────────────────────────────────────────
GLOBAL_SIM_OK = 0.65             # global_sim выше → дисконт патч-дефектов
GLOBAL_SIM_WARN = 0.45           # global_sim выше → частичный дисконт
SSIM_OK = 0.45                   # SSIM выше = OK
SSIM_WARN = 0.30                 # SSIM выше = Warning

# ─── SSIM-based discount override ────────────────────────────────────────
SSIM_DISCOUNT_CUTOFF = 0.38      # если SSIM ниже — не дисконтировать дефекты

# ─── SSIM ─────────────────────────────────────────────────────────────────
SSIM_BLUR_KERNEL = (7, 7)

# ─── Вердикт ──────────────────────────────────────────────────────────────
VERDICT_OK_THRESHOLD = round(12.0 - _ZS * 8.0, 3)        # 12…4
VERDICT_WARN_THRESHOLD = round(25.0 - _ZS * 14.0, 3)     # 25…11

# ─── Safety net ───────────────────────────────────────────────────────────
SAFETY_SSIM_LOW = round(0.20 + _ZS * 0.10, 3)            # 0.20…0.30
SAFETY_SIM_LOW = round(0.30 + _ZS * 0.10, 3)             # 0.30…0.40
SAFETY_DEFECT_OVERRIDE_PCT = 25.0  # принудительный defect_pct

# ─── Подзоны: ЕДИНЫЙ параметр чувствительности ────────────────────────────
# Диапазон 0.0 (мягко) … 2.0 (ультра-строго).
SUBZONE_SENSITIVITY = 0.510

# ─── Подзоны (субзоны) — рассчётные пороги ────────────────────────────────
_S = SUBZONE_SENSITIVITY
SUBZONE_PATCH_SIM_THRESHOLD = round(
    0.42 + _S * 0.16, 3)        # 0.42…0.58  (строже = выше)
SUBZONE_VERDICT_OK_THRESHOLD = round(
    12.0 - _S * 8.0, 3)         # 12…4       (строже = ниже)
SUBZONE_VERDICT_WARN_THRESHOLD = round(
    25.0 - _S * 12.0, 3)       # 25…13      (строже = ниже)
SUBZONE_PATCH_DEFECT_WEIGHT = round(
    22.0 + _S * 18.0, 3)        # 22…40      (строже = выше)
# 0.15…0.30  (строже = выше)
SUBZONE_SAFETY_SSIM_LOW = round(0.15 + _S * 0.15, 3)
# 0.25…0.40  (строже = выше)
SUBZONE_SAFETY_SIM_LOW = round(0.25 + _S * 0.15, 3)

# ─── Подзоны: гистограммные / дисконт пороги ─────────────────────────────
SUBZONE_HIST_COMPLETELY_DIFF = round(0.10 + _S * 0.12, 3)       # 0.10…0.22
SUBZONE_HIST_SIGNIFICANT_DIFF = round(0.20 + _S * 0.12, 3)       # 0.20…0.32
SUBZONE_SSIM_COMPLETELY_DIFF = round(0.10 + _S * 0.10, 3)       # 0.10…0.20
SUBZONE_SIM_COMPLETELY_DIFF = round(0.30 + _S * 0.10, 3)       # 0.30…0.40
SUBZONE_SSIM_SIGNIFICANT_DIFF = round(0.15 + _S * 0.10, 3)       # 0.15…0.25
SUBZONE_SIM_SIGNIFICANT_DIFF = round(0.35 + _S * 0.10, 3)       # 0.35…0.45
# принудительный минимум при "совершенно разные"
SUBZONE_FORCED_DEFECT_PCT = round(20.0 + _S * 20.0, 3)       # 20…40
# 0.65…0.85  (строже = меньше дисконт, ближе к 1.0)
SUBZONE_DISCOUNT_MINIMAL = round(0.65 + _S * 0.20, 3)
# 0.25…0.45  (строже = меньше дисконт, ближе к 1.0)
SUBZONE_DISCOUNT_GOOD = round(0.25 + _S * 0.20, 3)


# ─── Пересчёт порогов на лету ────────────────────────────────────────────


def apply_zone_sensitivity(val: float):
    """Пересчитать пороги общих зон из единого значения чувствительности."""
    val = max(0.0, min(2.0, val))
    m = _sys.modules[__name__]
    m.ZONE_SENSITIVITY = val
    m.PATCH_SIM_THRESHOLD = round(min(0.42 + val * 0.20, 0.85), 3)
    m.PATCH_DEFECT_WEIGHT = round(25.0 + val * 20.0, 3)
    m.PATCH_HIGH_SIM_DISCOUNT = round(min(0.20 + val * 0.40, 1.0), 3)
    m.VERDICT_OK_THRESHOLD = round(max(12.0 - val * 8.0, 0.5), 3)
    m.VERDICT_WARN_THRESHOLD = round(max(25.0 - val * 14.0, 1.5), 3)
    m.SAFETY_SSIM_LOW = round(min(0.20 + val * 0.10, 0.45), 3)
    m.SAFETY_SIM_LOW = round(min(0.30 + val * 0.10, 0.55), 3)


def apply_subzone_sensitivity(val: float):
    """Пересчитать пороги подзон из единого значения чувствительности."""
    val = max(0.0, min(2.0, val))
    m = _sys.modules[__name__]
    m.SUBZONE_SENSITIVITY = val
    m.SUBZONE_PATCH_SIM_THRESHOLD = round(min(0.42 + val * 0.16, 0.78), 3)
    m.SUBZONE_VERDICT_OK_THRESHOLD = round(max(12.0 - val * 8.0, 0.5), 3)
    m.SUBZONE_VERDICT_WARN_THRESHOLD = round(max(25.0 - val * 12.0, 1.5), 3)
    m.SUBZONE_PATCH_DEFECT_WEIGHT = round(22.0 + val * 18.0, 3)
    m.SUBZONE_SAFETY_SSIM_LOW = round(min(0.15 + val * 0.15, 0.50), 3)
    m.SUBZONE_SAFETY_SIM_LOW = round(min(0.25 + val * 0.15, 0.60), 3)
    m.SUBZONE_HIST_COMPLETELY_DIFF = round(min(0.10 + val * 0.12, 0.40), 3)
    m.SUBZONE_HIST_SIGNIFICANT_DIFF = round(min(0.20 + val * 0.12, 0.50), 3)
    m.SUBZONE_SSIM_COMPLETELY_DIFF = round(min(0.10 + val * 0.10, 0.35), 3)
    m.SUBZONE_SIM_COMPLETELY_DIFF = round(min(0.30 + val * 0.10, 0.55), 3)
    m.SUBZONE_SSIM_SIGNIFICANT_DIFF = round(min(0.15 + val * 0.10, 0.40), 3)
    m.SUBZONE_SIM_SIGNIFICANT_DIFF = round(min(0.35 + val * 0.10, 0.60), 3)
    m.SUBZONE_FORCED_DEFECT_PCT = round(min(20.0 + val * 20.0, 65.0), 3)
    m.SUBZONE_DISCOUNT_MINIMAL = round(min(0.65 + val * 0.20, 1.0), 3)
    m.SUBZONE_DISCOUNT_GOOD = round(min(0.25 + val * 0.20, 0.70), 3)
