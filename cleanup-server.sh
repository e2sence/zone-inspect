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
set -euo pipefail

SERVER="${DEPLOY_SERVER}"
SSH="ssh -o ConnectTimeout=15 $SERVER"

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

    # Show before
    $SSH "df -h / | tail -1 | awk '{printf \"   Before: Used %s / %s (%s)\n\", \$3, \$2, \$5}'"
    echo ""

    # 1. pip cache
    echo -e "▶ ${CYAN}[1/7]${NC} Clearing pip cache..."
    $SSH "pip cache purge 2>/dev/null; rm -rf ~/.cache/pip/ 2>/dev/null; sudo rm -rf /root/.cache/pip/ 2>/dev/null" || true
    echo -e "   ${GREEN}Done${NC}"

    # 2. apt cache
    echo -e "▶ ${CYAN}[2/7]${NC} Clearing apt cache..."
    $SSH "sudo apt-get clean -qq"
    echo -e "   ${GREEN}Done${NC}"

    # 3. Journal logs (keep 1 day)
    echo -e "▶ ${CYAN}[3/7]${NC} Trimming journal logs (keeping 1 day)..."
    $SSH "sudo journalctl --vacuum-time=1d --vacuum-size=16M 2>&1 | tail -1"
    echo -e "   ${GREEN}Done${NC}"

    # 4. PyTorch test binaries (unused, ~28MB)
    echo -e "▶ ${CYAN}[4/7]${NC} Removing PyTorch test binaries..."
    $SSH "rm -rf /opt/pcb-inspect/venv/lib/python3.11/site-packages/torch/bin/test_* 2>/dev/null" || true
    echo -e "   ${GREEN}Done${NC}"

    # 5. __pycache__
    echo -e "▶ ${CYAN}[5/7]${NC} Clearing __pycache__..."
    $SSH "find /opt/pcb-inspect/ -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null" || true
    echo -e "   ${GREEN}Done${NC}"

    # 6. /tmp cleanup
    echo -e "▶ ${CYAN}[6/7]${NC} Cleaning /tmp (files older than 2 days)..."
    $SSH "sudo find /tmp -type f -atime +2 -delete 2>/dev/null" || true
    echo -e "   ${GREEN}Done${NC}"

    # 7. Old kernels
    echo -e "▶ ${CYAN}[7/7]${NC} Removing old kernels..."
    $SSH "sudo apt-get autoremove -y -qq 2>&1 | tail -2"
    echo -e "   ${GREEN}Done${NC}"

    echo ""
    $SSH "df -h / | tail -1 | awk '{printf \"   After:  Used %s / %s (%s)\n\", \$3, \$2, \$5}'"
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
