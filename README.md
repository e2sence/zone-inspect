# PCB Zone Check

Visual inspection and defect analysis system for printed circuit boards.

## Overview

PCB Zone Check is a web-based inspection platform that uses neural network analysis (EfficientNet-B4) to detect defects on PCB assemblies by comparing photographed boards against reference templates.

## Key Features

- **Template-based inspection** -- define zones on a reference board, inspect against live photos
- **Neural network analysis** -- EfficientNet-B4 model for defect detection with configurable sensitivity
- **Subzone decomposition** -- automatic splitting of large zones for granular analysis
- **Mobile camera support** -- use a phone as a wireless camera via QR-code pairing
- **Role-based access control** -- admin / lead / operator roles with group isolation
- **External API** -- RESTful API (v1) for integration with MES and other systems
- **Inspection history** -- full audit trail with images stored in Cloudflare R2
- **Bilingual documentation** -- built-in EN/RU doc page

## How It Works

The inspection pipeline runs in several stages for each uploaded photo:

### 1. Global Alignment

The input photo is aligned to the reference board using SIFT feature matching (5000 keypoints) with FLANN-based matching and RANSAC homography. This produces a warped image that overlaps precisely with the reference, compensating for camera angle, position, and scale differences.

### 2. Zone Matching

Each defined zone is extracted from the warped image and ranked against the reference zone crops:

- **SSIM pre-ranking** — all zones are scored quickly via structural similarity; top-3 candidates advance.
- **Neural scoring** — EfficientNet-B4 computes global cosine similarity (pooled layer-7 features) for the top candidates. Best match above 0.55 threshold wins.
- **Fallback strategies** — if global homography fails, the system tries local SIFT alignment per zone, then OpenCV template matching.

### 3. Defect Detection (Neural Mode)

The matched zone pair (reference crop vs. extracted crop) is analyzed for defects:

1. **Preprocessing** — CLAHE contrast normalization on both images.
2. **Global similarity** — cosine similarity on EfficientNet-B4 layer-7 features (semantic level).
3. **SSIM map** — structural similarity computed per pixel to find difference regions.
4. **Patch CNN similarity** — layer-5 spatial feature maps are compared patch-by-patch, producing a similarity heatmap.
5. **Texture weighting** — flat/uniform regions are down-weighted to reduce false positives on backgrounds.
6. **Verdict** — weighted defect percentage is compared against sensitivity-dependent thresholds to produce OK / WARN / DEFECT status.

The final similarity score combines 50% global cosine (layer 7) + 50% patch cosine (layer 5).

### 4. Subzone Analysis

If a zone has subzones defined, each subzone is analyzed separately with stricter thresholds and an additional HSV histogram comparison to catch completely different components. The zone verdict = worst status across all subzones.

### 5. OpenCV Fallback

When the neural engine is unavailable, the system falls back to: CLAHE normalization, SSIM, absolute difference, edge analysis, adaptive thresholding, and contour-based defect measurement.

### 6. Reference Blending

The auto-blend module aligns multiple photos of the same board using SIFT (with barcode/QR anchor detection as fallback) and pixel-averages them to produce a clean, noise-reduced reference image.

## Tech Stack

| Layer     | Technology                              |
|-----------|-----------------------------------------|
| Backend   | Python, Flask, Gunicorn                 |
| ML Engine | PyTorch, EfficientNet-B4, OpenCV, SSIM  |
| Database  | MongoDB                                 |
| Storage   | Cloudflare R2 (S3-compatible)           |
| Frontend  | Vanilla JS, HTML/CSS                    |
| Server    | GCP VM, Debian, nginx                   |

## Project Structure

```
app.py                 Main Flask application (~2200 lines)
nn_engine.py           Neural network inference engine
auto_blend.py          Automatic image blending utilities
inspection_config.py   Inspection configuration defaults
r2_storage.py          Cloudflare R2 storage client
r2_config.json         R2 credentials (not in repo)
auth_keys.json         User/API keys (not in repo)
gunicorn.conf.py       Gunicorn configuration
static/                Frontend assets (JS, CSS)
templates/             HTML templates (index, login, doc, mobile)
deploy/                Deployment scripts
ROADMAP.md             Development roadmap
```

## Deployment

```bash
# Local
./run-local.sh

# Production
./deploy/deploy.sh deploy
```

## Configuration

Sensitive files excluded from the repository:

- `auth_keys.json` -- user keys, roles, groups, API keys
- `r2_config.json` -- Cloudflare R2 credentials
- `.env` -- environment variables

## License

Proprietary. See [LICENSE](LICENSE) for details.

Copyright (c) 2024-2026 MetaProdTrace. All rights reserved.
