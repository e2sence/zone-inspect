#!/usr/bin/env bash

# Поработал локально → проверил → готов деплоить:
git add -A && git commit -m "описание"
./deploy.sh deploy

#═══════════════════════════════════════════════════════════════════════════════
#  deploy.sh — One-command deploy for PCB Inspect
#
#  Usage:
#    ./deploy.sh diff      — show what differs between local and production
#    ./deploy.sh deploy    — sync code to prod, install deps, restart service
#    ./deploy.sh logs      — tail production logs
#    ./deploy.sh status    — check if prod service is running
#    ./deploy.sh restart   — restart prod service without code sync
#    ./deploy.sh ssh       — open SSH session to server
#
#  SAFE: Does NOT touch /var/www/mpts_NSCW or any other services.
#═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────
SERVER="${DEPLOY_SERVER}"
APP_DIR="/opt/pcb-inspect"
SERVICE="pcb-inspect"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

# SSH multiplexing — one TCP connection reused for all commands
CTRL_SOCKET="/tmp/pcb-deploy-ssh-$$"
SSH_OPTS="-o ConnectTimeout=15 -o ControlMaster=auto -o ControlPath=$CTRL_SOCKET -o ControlPersist=120"
SSH="ssh $SSH_OPTS $SERVER"
SCP="scp $SSH_OPTS"

cleanup() { ssh -O exit -o ControlPath="$CTRL_SOCKET" $SERVER 2>/dev/null || true; }
trap cleanup EXIT

# Files to compare (code only, not secrets/data)
CODE_FILES=(
    app.py nn_engine.py auto_blend.py r2_storage.py inspection_config.py
    gunicorn.conf.py _check_cfg.py
    static/app.js static/style.css static/tpl-editor.js
    templates/index.html templates/login.html templates/mobile_camera.html
)

# Rsync excludes — secrets and runtime data stay on each machine
RSYNC_EXCLUDES=(
    --exclude='venv/' --exclude='.venv/' --exclude='__pycache__/'
    --exclude='*.pyc' --exclude='.git/'
    --exclude='r2_config.json' --exclude='bot_config.json' --exclude='auth_keys.json'
    --exclude='.env' --exclude='.mobile_tokens.json'
    --exclude='uploads/' --exclude='results/' --exclude='inspection_results/'
    --exclude='saved_templates/' --exclude='references/'
    --exclude='*.pt' --exclude='nohup.out' --exclude='*.log'
    --exclude='YOLO.code-workspace' --exclude='.DS_Store'
    --exclude='deploy.sh'
)

# ─── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# ─── Functions ────────────────────────────────────────────────────────────────

cmd_diff() {
    echo -e "${BOLD}Comparing local ↔ production ($SERVER:$APP_DIR)${NC}"
    echo ""

    local has_diff=0

    for f in "${CODE_FILES[@]}"; do
        local_file="$LOCAL_DIR/$f"
        if [ ! -f "$local_file" ]; then
            continue
        fi

        # Compare using remote md5sum directly (avoids pipe/newline artifacts)
        remote_md5=$($SSH "md5sum $APP_DIR/$f 2>/dev/null | cut -d' ' -f1" 2>/dev/null || echo "__MISSING__")

        if [ "$remote_md5" = "__MISSING__" ]; then
            echo -e "  ${YELLOW}+ NEW${NC}  $f  (not on prod)"
            has_diff=1
        else
            local_md5=$(md5 -q "$local_file")

            if [ "$local_md5" != "$remote_md5" ]; then
                # Count changed lines
                line_diff=$(diff <($SSH "cat $APP_DIR/$f") "$local_file" 2>/dev/null | grep -c "^[<>]" || true)
                echo -e "  ${RED}~ DIFF${NC}  $f  (${line_diff} lines changed)"
                has_diff=1
            fi
        fi
    done

    # Check for files on prod that don't exist locally
    remote_py_files=$($SSH "cd $APP_DIR && find . -maxdepth 2 -name '*.py' -o -name '*.js' -o -name '*.css' -o -name '*.html' | grep -v venv | grep -v __pycache__ | sort" 2>/dev/null || true)
    while IFS= read -r rf; do
        rf="${rf#./}"
        [ -z "$rf" ] && continue
        if [ ! -f "$LOCAL_DIR/$rf" ]; then
            echo -e "  ${CYAN}? EXTRA${NC} $rf  (only on prod)"
            has_diff=1
        fi
    done <<< "$remote_py_files"

    echo ""
    if [ $has_diff -eq 0 ]; then
        echo -e "  ${GREEN}✓ Everything in sync!${NC}"
    else
        echo -e "  Run ${BOLD}./deploy.sh deploy${NC} to push local → prod"
    fi
}

cmd_diff_detail() {
    # Show actual unified diffs
    echo -e "${BOLD}Detailed diff: local ↔ production${NC}"
    echo ""

    for f in "${CODE_FILES[@]}"; do
        local_file="$LOCAL_DIR/$f"
        [ ! -f "$local_file" ] && continue

        remote_content=$($SSH "cat $APP_DIR/$f 2>/dev/null" 2>/dev/null || echo "")
        if [ -z "$remote_content" ]; then
            continue
        fi

        file_diff=$(diff -u --label "prod:$f" --label "local:$f" <(echo "$remote_content") "$local_file" 2>/dev/null || true)
        if [ -n "$file_diff" ]; then
            echo -e "${YELLOW}─── $f ───${NC}"
            echo "$file_diff"
            echo ""
        fi
    done
}

cmd_deploy() {
    echo -e "${BOLD}═══════════════════════════════════════════════════════${NC}"
    echo -e "${BOLD}  PCB Inspect — Deploy local → production${NC}"
    echo -e "${BOLD}═══════════════════════════════════════════════════════${NC}"
    echo ""

    # Pre-flight
    echo -e "▶ ${CYAN}[1/5]${NC} Pre-flight check..."
    $SSH "echo 'SSH OK'" || { echo -e "${RED}❌ Cannot SSH to $SERVER${NC}"; exit 1; }
    echo "   Connected to $SERVER"
    echo ""

    # Show what will change
    echo -e "▶ ${CYAN}[2/5]${NC} Changes to deploy:"
    cmd_diff
    echo ""

    # Confirm
    read -p "Deploy these changes? [y/N] " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi

    # Rsync
    echo ""
    echo -e "▶ ${CYAN}[3/5]${NC} Syncing code..."
    rsync -avz --delete \
        -e "ssh $SSH_OPTS" \
        "${RSYNC_EXCLUDES[@]}" \
        "$LOCAL_DIR/" "$SERVER:$APP_DIR/"
    echo -e "   ${GREEN}Code synced${NC}"
    echo ""

    # Check if requirements changed → pip install
    echo -e "▶ ${CYAN}[4/5]${NC} Checking dependencies..."
    local_req_md5=$(md5 -q "$LOCAL_DIR/requirements-prod.txt")
    remote_req_md5=$($SSH "md5sum $APP_DIR/requirements-prod.txt 2>/dev/null | cut -d' ' -f1" || echo "none")
    if [ "$local_req_md5" != "$remote_req_md5" ]; then
        echo "   Requirements changed — installing..."
        $SSH "cd $APP_DIR && venv/bin/pip install --quiet -r requirements-prod.txt"
        echo -e "   ${GREEN}Dependencies updated${NC}"
    else
        echo -e "   ${GREEN}Dependencies unchanged, skipping${NC}"
    fi
    echo ""

    # Restart service
    echo -e "▶ ${CYAN}[5/5]${NC} Restarting service..."
    $SSH "sudo systemctl restart $SERVICE && sleep 2 && sudo systemctl is-active $SERVICE"
    echo -e "   ${GREEN}Service restarted${NC}"
    echo ""

    echo -e "${GREEN}${BOLD}✓ Deploy complete!${NC}"
    echo -e "  https://inspect.metaprodtrace.com"
}

cmd_logs() {
    echo -e "${BOLD}Production logs (Ctrl+C to stop):${NC}"
    $SSH "sudo journalctl -u $SERVICE -f --no-pager -n 50"
}

cmd_status() {
    echo -e "${BOLD}Service status:${NC}"
    $SSH "sudo systemctl status $SERVICE --no-pager -l" || true
}

cmd_restart() {
    echo -e "Restarting ${SERVICE}..."
    $SSH "sudo systemctl restart $SERVICE && sleep 2 && sudo systemctl is-active $SERVICE"
    echo -e "${GREEN}✓ Restarted${NC}"
}

cmd_ssh() {
    echo "Connecting to $SERVER..."
    ssh $SERVER
}

# ─── Main ─────────────────────────────────────────────────────────────────────
CMD="${1:-help}"

case "$CMD" in
    diff)        cmd_diff ;;
    diff-detail) cmd_diff_detail ;;
    deploy)      cmd_deploy ;;
    logs)        cmd_logs ;;
    status)      cmd_status ;;
    restart)     cmd_restart ;;
    ssh)         cmd_ssh ;;
    *)
        echo "PCB Inspect Deploy Tool"
        echo ""
        echo "Usage: ./deploy.sh <command>"
        echo ""
        echo "Commands:"
        echo "  diff         Show what differs between local and prod"
        echo "  diff-detail  Show full unified diffs"
        echo "  deploy       Sync code → prod, install deps, restart"
        echo "  logs         Tail production logs"
        echo "  status       Check production service status"
        echo "  restart      Restart prod (no code sync)"
        echo "  ssh          Open SSH session to server"
        ;;
esac
