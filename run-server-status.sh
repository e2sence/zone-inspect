#!/usr/bin/env bash
#═══════════════════════════════════════════════════════════════════════════════
#  server-status.sh — Quick status check for production VM
#
#  Usage:  ./server-status.sh
#═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

SERVER="${DEPLOY_SERVER:?Set DEPLOY_SERVER env var, e.g. user@host}"
SSH="ssh -o ConnectTimeout=15 $SERVER"

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${BOLD}═══════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  VM Status — $SERVER${NC}"
echo -e "${BOLD}═══════════════════════════════════════════════════════${NC}"
echo ""

$SSH "
# ── Uptime & Load ──
UPTIME=\$(uptime -p 2>/dev/null || uptime | sed 's/.*up /up /' | sed 's/,.*//')
LOAD=\$(cat /proc/loadavg | cut -d' ' -f1-3)
CPUS=\$(nproc)
echo \"⏱  Uptime: \$UPTIME\"
echo \"📊 Load:   \$LOAD  (CPUs: \$CPUS)\"
echo ''

# ── RAM ──
read TOTAL USED FREE AVAIL <<< \$(free -m | awk '/^Mem:/ {print \$2, \$3, \$4, \$7}')
PCT=\$((USED * 100 / TOTAL))
echo \"RAM:    \${USED}M / \${TOTAL}M (\${PCT}%)  free: \${AVAIL}M\"

# ── Disk ──
read SIZE USED_D AVAIL_D PCT_D <<< \$(df -h / | awk 'NR==2 {print \$2, \$3, \$4, \$5}')
echo \"Disk:   \${USED_D} / \${SIZE} (\${PCT_D})  free: \${AVAIL_D}\"
echo ''

# ── Services ──
echo 'Services:'
for SVC in pcb-inspect nginx; do
    STATUS=\$(systemctl is-active \$SVC 2>/dev/null || echo 'not found')
    if [ \"\$STATUS\" = 'active' ]; then
        SINCE=\$(systemctl show \$SVC --property=ActiveEnterTimestamp --value 2>/dev/null | sed 's/ [A-Z]*$//')
        printf '   %-20s [OK] active  (since %s)\n' \"\$SVC\" \"\$SINCE\"
    else
        printf '   %-20s [FAIL] %s\n' \"\$SVC\" \"\$STATUS\"
    fi
done

# PM2 (Node app)
if command -v pm2 &>/dev/null; then
    PM2_STATUS=\$(pm2 jlist 2>/dev/null | python3 -c \"
import json,sys
apps=json.load(sys.stdin)
for a in apps:
    print(f'   {a[\"name\"]:20s} {\"[OK]\" if a[\"pm2_env\"][\"status\"]==\"online\" else \"[FAIL]\"} {a[\"pm2_env\"][\"status\"]}  ({a[\"pm2_env\"][\"restart_time\"]} restarts)')
\" 2>/dev/null) || PM2_STATUS='   pm2: no apps'
    echo \"\$PM2_STATUS\"
fi
echo ''

# ── HTTP check ──
echo 'HTTP:'
HTTP=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:5001/ 2>/dev/null || echo 'timeout')
if [ \"\$HTTP\" = '302' ] || [ \"\$HTTP\" = '200' ]; then
    printf '   %-20s [OK] %s\n' 'pcb-inspect:5001' \"\$HTTP\"
else
    printf '   %-20s [FAIL] %s\n' 'pcb-inspect:5001' \"\$HTTP\"
fi
echo ''

# ── Top processes ──
echo 'Top processes by RAM:'
ps aux --sort=-%mem | awk 'NR>1 && NR<=6 {printf \"   %-6s %5.1f%% RAM  %s\n\", \$1, \$4, \$11}'
echo ''

# ── Network connections ──
CONNS=\$(ss -tun | tail -n +2 | wc -l)
echo \"Active connections: \$CONNS\"
"
