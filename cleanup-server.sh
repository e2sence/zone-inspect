#!/usr/bin/env bash
#═══════════════════════════════════════════════════════════════════════════════
#  cleanup-server.sh — Free disk space on production VM
#
#  Usage:
#    ./cleanup-server.sh          — show what can be cleaned (dry run)
#    ./cleanup-server.sh clean    — actually clean
#
#  SAFE: Does NOT touch /var/www/mpts_NSCW or application data.
#═══════════════════════════════════════════════════════════════════════════════
set -u

SERVER="${DEPLOY_SERVER}"
SSH="ssh -o ConnectTimeout=15 -o ServerAliveInterval=5 $SERVER"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

cmd_report() {
    echo -e "${BOLD}═══════════════════════════════════════════════════════${NC}"
    echo -e "${BOLD}  Disk Usage Report — $SERVER${NC}"
    echo -e "${BOLD}═══════════════════════════════════════════════════════${NC}"
    echo ""

    $SSH "
        echo '📊 Disk overview:'
        df -h / | tail -1 | awk '{printf \"   Total: %s  Used: %s  Free: %s  (%s used)\n\", \$2, \$3, \$4, \$5}'
        echo ''

        echo '🗂  Space by category:'
        printf '   %-40s %s\n' 'pip cache' \$(du -sh ~/.cache/pip/ 2>/dev/null | cut -f1 || echo '0')
        printf '   %-40s %s\n' 'apt cache' \$(sudo du -sh /var/cache/apt/ 2>/dev/null | cut -f1 || echo '0')
        printf '   %-40s %s\n' 'Journal logs' \$(sudo journalctl --disk-usage 2>/dev/null | grep -oP '[\d.]+[KMGT]' || echo '0')
        printf '   %-40s %s\n' 'Torch model cache' \$(du -sh ~/.cache/torch/ 2>/dev/null | cut -f1 || echo '0')
        printf '   %-40s %s\n' 'PyTorch test binaries (in venv)' \$(du -sh /opt/pcb-inspect/venv/lib/python3.11/site-packages/torch/bin/ 2>/dev/null | cut -f1 || echo '0')
        printf '   %-40s %s\n' '__pycache__' \$(du -sh /opt/pcb-inspect/__pycache__/ 2>/dev/null | cut -f1 || echo '0')
        printf '   %-40s %s\n' '/tmp' \$(sudo du -sh /tmp/ 2>/dev/null | cut -f1 || echo '0')
        echo ''

        KERNELS=\$(dpkg -l 'linux-image-*' 2>/dev/null | grep '^ii' | wc -l)
        CURRENT=\$(uname -r)
        echo \"   Kernel images installed: \$KERNELS (current: \$CURRENT)\"
        echo ''

        echo '📦 Application data (NOT cleaned):'
        printf '   %-40s %s\n' 'pcb-inspect venv' \$(du -sh /opt/pcb-inspect/venv/ 2>/dev/null | cut -f1 || echo '0')
        printf '   %-40s %s\n' 'pcb-inspect app' \$(du -sh --exclude=venv --exclude=uploads --exclude=__pycache__ /opt/pcb-inspect/ 2>/dev/null | cut -f1 || echo '0')
        printf '   %-40s %s\n' 'mpts_NSCW' \$(du -sh /var/www/mpts_NSCW/ 2>/dev/null | cut -f1 || echo 'N/A')
        printf '   %-40s %s\n' 'Google Ops Agent' \$(sudo du -sh /opt/google-cloud-ops-agent/ 2>/dev/null | cut -f1 || echo '0')
    "

    echo ""
    echo -e "  Run ${BOLD}./cleanup-server.sh clean${NC} to free space"
}

cmd_clean() {
    echo -e "${BOLD}═══════════════════════════════════════════════════════${NC}"
    echo -e "${BOLD}  Cleaning disk — $SERVER${NC}"
    echo -e "${BOLD}═══════════════════════════════════════════════════════${NC}"
    echo ""

    # Run ALL cleanup in a single SSH session to avoid connection drops
    $SSH 'bash -s' <<'REMOTE_SCRIPT'
        step() { printf "▶ [\033[0;36m%s\033[0m] %s\n" "$1" "$2"; }
        ok()   { printf "   \033[0;32mDone\033[0m\n"; }

        df -h / | tail -1 | awk '{printf "   Before: Used %s / %s (%s)\n\n", $3, $2, $5}'

        step "1/7" "Clearing pip cache..."
        pip cache purge 2>/dev/null || true
        rm -rf ~/.cache/pip/ 2>/dev/null
        sudo rm -rf /root/.cache/pip/ 2>/dev/null
        ok

        step "2/7" "Clearing apt cache..."
        sudo apt-get clean -qq
        ok

        step "3/7" "Trimming journal logs (keeping 1 day)..."
        sudo journalctl --vacuum-time=1d --vacuum-size=16M 2>&1 | tail -1
        ok

        step "4/7" "Removing PyTorch test binaries..."
        rm -rf /opt/pcb-inspect/venv/lib/python3.11/site-packages/torch/bin/test_* 2>/dev/null
        ok

        step "5/7" "Clearing __pycache__..."
        find /opt/pcb-inspect/ -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
        ok

        step "6/7" "Cleaning /tmp (files older than 2 days)..."
        sudo find /tmp -type f -atime +2 -delete 2>/dev/null || true
        ok

        step "7/7" "Removing old kernels..."
        sudo apt-get autoremove -y -qq 2>&1 | tail -2
        ok

        echo ""
        df -h / | tail -1 | awk '{printf "   After:  Used %s / %s (%s)\n", $3, $2, $5}'
REMOTE_SCRIPT

    echo ""
    echo -e "${GREEN}${BOLD}✓ Cleanup complete!${NC}"
}

# ─── Main ─────────────────────────────────────────────────────────────────────
CMD="${1:-report}"

case "$CMD" in
    report)  cmd_report ;;
    clean)   cmd_clean ;;
    *)
        echo "Usage: ./cleanup-server.sh [report|clean]"
        echo ""
        echo "  report   Show what can be cleaned (default)"
        echo "  clean    Actually free disk space"
        ;;
esac
