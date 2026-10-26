"""Synthetic board + 'phone photos' for pipeline and recipe tests."""

import cv2
import numpy as np


def make_board(seed: int = 7):
    """Textured synthetic 'PCB' — enough structure for SIFT to lock on."""
    rng = np.random.default_rng(seed)
    img = (rng.random((900, 1200, 3)) * 255).astype(np.uint8)
    img = cv2.GaussianBlur(img, (7, 7), 0)
    for _ in range(60):
        x, y = rng.integers(0, 1100), rng.integers(0, 800)
        color = tuple(int(c) for c in rng.integers(0, 255, 3))
        cv2.rectangle(img, (x, y), (x + rng.integers(15, 90),
                                    y + rng.integers(15, 90)), color, -1)
    return img


def shoot(board, zone, defect=False, quality=92, brightness=0):
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
    if brightness:
        crop = cv2.convertScaleAbs(crop, alpha=1.0, beta=brightness)
    return cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, quality])[1].tobytes()
