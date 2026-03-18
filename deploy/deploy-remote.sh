#!/usr/bin/env bash
#═══════════════════════════════════════════════════════════════════════════════
#  deploy-remote.sh — Deploy PCB Inspect from local Mac to Google VM
#
#  Usage:
#    1. ssh-add ~/.ssh/google_compute_engine   (enter passphrase once)
#    2. cd /path/to/YOLO
#    3. bash deploy/deploy-remote.sh
#
#  This script does NOT touch the existing Node.js app on the server.
#═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────
SERVER="${DEPLOY_SERVER}"
APP_DIR="/opt/pcb-inspect"
DOMAIN="inspect.metaprodtrace.com"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"  # parent of deploy/

# SSH multiplexing — one TCP connection reused for all commands
CTRL_SOCKET="/tmp/pcb-deploy-ssh-$$"
SSH_OPTS="-o ConnectTimeout=15 -o ControlMaster=auto -o ControlPath=$CTRL_SOCKET -o ControlPersist=300"
SSH="ssh $SSH_OPTS $SERVER"
SCP="scp $SSH_OPTS"

cleanup() { ssh -O exit -o ControlPath=$CTRL_SOCKET $SERVER 2>/dev/null || true; }
trap cleanup EXIT

echo "═══════════════════════════════════════════════════════════"
echo "  PCB Inspect — Remote Deployment to $SERVER"
echo "  Source: $LOCAL_DIR"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ─── Pre-flight checks ───────────────────────────────────────────────────────
echo "▶ [0/9] Pre-flight checks..."
$SSH "echo 'SSH OK'" || { echo "❌ Cannot SSH to server. Run: ssh-add ~/.ssh/google_compute_engine"; exit 1; }
echo "   SSH connection OK"
echo ""

# ─── 1. Install system packages ──────────────────────────────────────────────
echo "▶ [1/9] Installing system packages..."
$SSH "sudo apt-get update -qq && \
      sudo apt-get install -y -qq \
        python3-venv python3-pip python3-dev \
        libglib2.0-0 libdmtx0b 2>&1 | tail -1"
echo "   System packages OK"
echo ""

# ─── 2. Create app directory ─────────────────────────────────────────────────
echo "▶ [2/9] Creating app directory..."
$SSH "sudo mkdir -p $APP_DIR && sudo chown e2sence:e2sence $APP_DIR && mkdir -p $APP_DIR/uploads"
echo "   Directory: $APP_DIR"
echo ""

# ─── 3. Sync application code ────────────────────────────────────────────────
echo "▶ [3/9] Syncing code to server..."
rsync -avz --delete \
    -e "ssh $SSH_OPTS" \
    --exclude='venv/' --exclude='.venv/' --exclude='__pycache__/' \
    --exclude='*.pyc' --exclude='.git/' \
    --exclude='r2_config.json' --exclude='bot_config.json' --exclude='auth_keys.json' \
    --exclude='.env' --exclude='.mobile_tokens.json' \
    --exclude='uploads/' --exclude='results/' --exclude='inspection_results/' \
    --exclude='saved_templates/' --exclude='references/' \
    --exclude='*.pt' --exclude='nohup.out' --exclude='*.log' \
    --exclude='YOLO.code-workspace' --exclude='.DS_Store' \
    "$LOCAL_DIR/" "$SERVER:$APP_DIR/"
echo "   Code synced"
echo ""

# ─── 4. Create .env file (from local configs) ────────────────────────────────
echo "▶ [4/9] Setting up .env..."

# Generate FLASK_SECRET if not already on server
FLASK_SECRET=$($SSH "grep '^FLASK_SECRET=' $APP_DIR/.env 2>/dev/null | cut -d= -f2" || true)
if [ -z "$FLASK_SECRET" ]; then
    FLASK_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
fi

# Read R2 config from local file
R2_ACCOUNT_ID=$(python3 -c "import json; print(json.load(open('$LOCAL_DIR/r2_config.json'))['account_id'])")
R2_ACCESS_KEY_ID=$(python3 -c "import json; print(json.load(open('$LOCAL_DIR/r2_config.json'))['access_key_id'])")
R2_SECRET_ACCESS_KEY=$(python3 -c "import json; print(json.load(open('$LOCAL_DIR/r2_config.json'))['secret_access_key'])")
R2_BUCKET=$(python3 -c "import json; print(json.load(open('$LOCAL_DIR/r2_config.json'))['bucket'])")

# Write .env to server
$SSH "cat > $APP_DIR/.env << 'ENVEOF'
FLASK_SECRET=$FLASK_SECRET
PORT=5001

R2_ACCOUNT_ID=$R2_ACCOUNT_ID
R2_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY
R2_BUCKET=$R2_BUCKET

BASE_URL=https://$DOMAIN

MAX_UPLOAD_MB=128
GUNICORN_WORKERS=2
ENVEOF
chmod 600 $APP_DIR/.env"
echo "   .env created"

# Copy auth_keys.json
$SCP "$LOCAL_DIR/auth_keys.json" "$SERVER:$APP_DIR/auth_keys.json"
$SSH "chmod 600 $APP_DIR/auth_keys.json"
echo "   auth_keys.json copied"
echo ""

# ─── 5. Python venv & dependencies ───────────────────────────────────────────
echo "▶ [5/9] Setting up Python environment (this takes a few minutes)..."
$SSH "cd $APP_DIR && \
      [ -d venv ] || python3 -m venv venv && \
      venv/bin/pip install --quiet --upgrade pip && \
      echo '   Installing PyTorch CPU...' && \
      venv/bin/pip install --quiet torch torchvision \
          --index-url https://download.pytorch.org/whl/cpu && \
      echo '   Installing other deps...' && \
      venv/bin/pip install --quiet -r requirements-prod.txt"
echo "   Python environment ready"
echo ""

# ─── 6. Pre-download model weights ───────────────────────────────────────────
echo "▶ [6/9] Pre-loading EfficientNet-B4 model..."
$SSH "cd $APP_DIR && venv/bin/python -c \
    'import torchvision.models as m; m.efficientnet_b4(weights=m.EfficientNet_B4_Weights.DEFAULT); print(\"   Model cached\")' \
    2>/dev/null" || echo "   (Will download on first request)"
echo ""

# ─── 7. Systemd service ──────────────────────────────────────────────────────
echo "▶ [7/9] Installing systemd service..."
$SSH "sudo tee /etc/systemd/system/pcb-inspect.service > /dev/null << 'SVCEOF'
[Unit]
Description=PCB Inspect — Zone Check Application
After=network.target

[Service]
User=e2sence
Group=e2sence
WorkingDirectory=/opt/pcb-inspect
EnvironmentFile=/opt/pcb-inspect/.env
ExecStart=/opt/pcb-inspect/venv/bin/gunicorn -c gunicorn.conf.py app:app
ExecReload=/bin/kill -s HUP \$MAINPID
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
SVCEOF
sudo systemctl daemon-reload && \
sudo systemctl enable pcb-inspect && \
sudo systemctl restart pcb-inspect && \
sleep 2 && \
sudo systemctl is-active pcb-inspect"
echo "   Service installed and running"
echo ""

## ─── 8-9. Nginx + SSL — SKIPPED ──────────────────────────────────────────────
## SSL already configured via certbot on first deploy.
## Re-running steps 8-9 would temporarily break HTTPS.
## To reconfigure manually:
##   ssh ${DEPLOY_SERVER}
##   sudo certbot --nginx -d inspect.metaprodtrace.com
echo "▶ [8-9/9] Nginx + SSL — skipped (already configured)"
echo ""

# ─── 10. Cleanup old journal logs ────────────────────────────────────────────
echo "▶ [10/9] Cleaning up old logs..."
$SSH "
  BEFORE=\$(sudo journalctl --disk-usage 2>&1 | head -1)
  sudo journalctl --vacuum-time=7d --vacuum-size=100M 2>/dev/null
  AFTER=\$(sudo journalctl --disk-usage 2>&1 | head -1)
  echo \"   Before: \$BEFORE\"
  echo \"   After:  \$AFTER\"
"
echo "   Logs cleaned (kept last 7 days / max 100 MB)"
echo ""

# ─── Verify ──────────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "   Verifying deployment..."
HTTP_CODE=$($SSH "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5001/login")
echo "   App health check: HTTP $HTTP_CODE"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  ✅ Deployment complete!"
echo ""
echo "  App URL:   https://$DOMAIN"
echo "  HTTP URL:  http://$DOMAIN (if SSL not yet configured)"
echo ""
echo "  Auth keys:"
echo "    admin:     dA0uLDnddWtJbUXA"
echo "    operator1: nGmz5Vy2xC5PSfrQ"
echo "    operator2: nVu5avYs-lwH8KC5"
echo ""
echo "  Useful commands (on server):"
echo "    sudo systemctl status pcb-inspect"
echo "    sudo journalctl -u pcb-inspect -f"
echo "    sudo systemctl restart pcb-inspect"
echo ""
echo "  To redeploy after code changes:"
echo "    bash deploy/deploy-remote.sh"
echo "═══════════════════════════════════════════════════════════"
