#!/usr/bin/env bash
# deploy.sh — Deploy PCB Inspect to Google Cloud VM
# Usage: ssh into VM, then run: bash deploy.sh
set -euo pipefail

APP_DIR="/opt/pcb-inspect"
APP_USER="pcbinspect"
DOMAIN="inspect.metaprodtrace.com"

echo "═══════════════════════════════════════════════════════"
echo "  PCB Inspect — Production Deployment"
echo "═══════════════════════════════════════════════════════"

# ─── 1. System packages ───────────────────────────────────────────────────────
echo ""
echo "▶ [1/8] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3.11 python3.11-venv python3.11-dev \
    nginx certbot python3-certbot-nginx \
    libgl1-mesa-glx libglib2.0-0 libdmtx0b \
    git curl

# ─── 2. Create app user ──────────────────────────────────────────────────────
echo ""
echo "▶ [2/8] Setting up user & directories..."
if ! id "$APP_USER" &>/dev/null; then
    sudo useradd --system --shell /usr/sbin/nologin --home-dir "$APP_DIR" "$APP_USER"
fi
sudo mkdir -p "$APP_DIR"
sudo chown "$APP_USER:$APP_USER" "$APP_DIR"

# ─── 3. Copy application code ────────────────────────────────────────────────
echo ""
echo "▶ [3/8] Syncing application code..."
# Copy all Python + template files (NOT secrets, NOT venv)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
for item in app.py nn_engine.py auto_blend.py r2_storage.py inspection_config.py \
            gunicorn.conf.py requirements-prod.txt templates/ setup_bot.py; do
    if [ -e "$SCRIPT_DIR/../$item" ]; then
        sudo cp -r "$SCRIPT_DIR/../$item" "$APP_DIR/"
    elif [ -e "$SCRIPT_DIR/$item" ]; then
        sudo cp -r "$SCRIPT_DIR/$item" "$APP_DIR/"
    fi
done
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# ─── 4. Python venv & deps ───────────────────────────────────────────────────
echo ""
echo "▶ [4/8] Setting up Python environment..."
if [ ! -d "$APP_DIR/venv" ]; then
    sudo -u "$APP_USER" python3.11 -m venv "$APP_DIR/venv"
fi
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements-prod.txt"

# Pre-download model weights
echo "   Warming up EfficientNet-B4 model..."
sudo -u "$APP_USER" "$APP_DIR/venv/bin/python" -c "
import torchvision.models as m; m.efficientnet_b4(weights=m.EfficientNet_B4_Weights.DEFAULT)
print('   Model ready.')
" 2>/dev/null || echo "   (Model download will happen on first request)"

# ─── 5. Environment file ─────────────────────────────────────────────────────
echo ""
echo "▶ [5/8] Checking .env..."
if [ ! -f "$APP_DIR/.env" ]; then
    echo "   ⚠️  No .env found! Creating from template..."
    sudo cp "$SCRIPT_DIR/../.env.example" "$APP_DIR/.env" 2>/dev/null || \
    sudo cp "$SCRIPT_DIR/.env.example" "$APP_DIR/.env" 2>/dev/null || true
    # Generate a random FLASK_SECRET
    RAND_SECRET=$(python3.11 -c "import secrets; print(secrets.token_hex(32))")
    sudo sed -i "s|FLASK_SECRET=.*|FLASK_SECRET=$RAND_SECRET|" "$APP_DIR/.env"
    sudo chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
    sudo chmod 600 "$APP_DIR/.env"
    echo ""
    echo "   ╔══════════════════════════════════════════════════╗"
    echo "   ║  IMPORTANT: Edit /opt/pcb-inspect/.env           ║"
    echo "   ║  Fill in R2 credentials and other settings       ║"
    echo "   ╚══════════════════════════════════════════════════╝"
    echo ""
else
    echo "   .env exists, keeping it."
fi

# ─── 6. Auth keys ────────────────────────────────────────────────────────────
if [ ! -f "$APP_DIR/auth_keys.json" ]; then
    echo "   Creating default auth_keys.json..."
    K1=$(python3.11 -c "import secrets; print(secrets.token_urlsafe(12))")
    cat > /tmp/auth_keys.json <<EOF
{
  "$K1": "admin"
}
EOF
    sudo mv /tmp/auth_keys.json "$APP_DIR/auth_keys.json"
    sudo chown "$APP_USER:$APP_USER" "$APP_DIR/auth_keys.json"
    sudo chmod 600 "$APP_DIR/auth_keys.json"
    echo "   Admin key: $K1  (save this!)"
fi

# ─── 7. Create directories ───────────────────────────────────────────────────
sudo -u "$APP_USER" mkdir -p "$APP_DIR/uploads" "$APP_DIR/static"

# ─── 8. Systemd service ──────────────────────────────────────────────────────
echo ""
echo "▶ [6/8] Installing systemd service..."
sudo cp "$SCRIPT_DIR/pcb-inspect.service" /etc/systemd/system/pcb-inspect.service 2>/dev/null || \
sudo cp "$APP_DIR/deploy/pcb-inspect.service" /etc/systemd/system/pcb-inspect.service 2>/dev/null || true
sudo systemctl daemon-reload
sudo systemctl enable pcb-inspect
sudo systemctl restart pcb-inspect
echo "   Service status:"
sudo systemctl status pcb-inspect --no-pager -l | head -5

# ─── 9. Nginx ─────────────────────────────────────────────────────────────────
echo ""
echo "▶ [7/8] Configuring nginx..."
sudo cp "$SCRIPT_DIR/nginx-pcb-inspect.conf" /etc/nginx/sites-available/pcb-inspect 2>/dev/null || \
sudo cp "$APP_DIR/deploy/nginx-pcb-inspect.conf" /etc/nginx/sites-available/pcb-inspect 2>/dev/null || true
sudo ln -sf /etc/nginx/sites-available/pcb-inspect /etc/nginx/sites-enabled/pcb-inspect
sudo rm -f /etc/nginx/sites-enabled/default

# Test before reload
sudo nginx -t && sudo systemctl reload nginx
echo "   Nginx OK"

# ─── 10. SSL ──────────────────────────────────────────────────────────────────
echo ""
echo "▶ [8/8] SSL certificate..."
if [ ! -d "/etc/letsencrypt/live/$DOMAIN" ]; then
    echo "   Requesting Let's Encrypt certificate..."
    sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
         --email admin@metaprodtrace.com --redirect || \
    echo "   ⚠️  Certbot failed — run manually: sudo certbot --nginx -d $DOMAIN"
else
    echo "   Certificate exists. Renewal is automatic via certbot timer."
fi

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  ✅ Deployment complete!"
echo ""
echo "  App:    https://$DOMAIN"
echo "  Logs:   sudo journalctl -u pcb-inspect -f"
echo "  Config: $APP_DIR/.env"
echo "  Auth:   $APP_DIR/auth_keys.json"
echo ""
echo "  Next steps:"
echo "  1. Edit $APP_DIR/.env with real R2 credentials"
echo "  2. sudo systemctl restart pcb-inspect"
echo "  3. Open https://$DOMAIN and login"
echo "═══════════════════════════════════════════════════════"
