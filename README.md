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
