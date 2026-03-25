#!/usr/bin/env bash
#═══════════════════════════════════════════════════════════════════════════════
#  run-monitor.sh — Monitor user connections and errors on production
#
#  Usage:
#    ./run-monitor.sh live         — live tail of real user requests
#    ./run-monitor.sh errors       — show 4xx/5xx errors (last 24h)
#    ./run-monitor.sh users        — unique users today (IP, UA, last page)
#    ./run-monitor.sh slow         — slow requests >2s (last 24h)
#    ./run-monitor.sh status       — quick health check: service + last requests
#    ./run-monitor.sh geo IP       — geolocate an IP address
#═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

SERVER="${DEPLOY_SERVER:?Set DEPLOY_SERVER env var, e.g. user@host}"
LOG="/var/log/nginx/json_access.log"
# Filter out bots/scanners
BOT_FILTER='select(.http_user_agent | test("bot|crawl|spider|zgrab|censys|palo|scan|curl/|Go-http|python|wget|nikto|nmap|masscan|shodan"; "i") | not)'
# Filter only inspect.metaprodtrace.com
SITE_FILTER='select(.server_name == "inspect.metaprodtrace.com")'

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

ssh_cmd() { ssh -o ConnectTimeout=10 "$SERVER" "$@"; }

cmd_live() {
    echo -e "${BOLD}Live requests — inspect.metaprodtrace.com (Ctrl+C to stop)${NC}"
    echo ""
    ssh_cmd "sudo tail -f $LOG" | jq --unbuffered -r "
        ${SITE_FILTER} | ${BOT_FILTER} |
        \"\\(.time_local | split(\" \")[0]) \\(.remote_addr | .[0:15])  \\(.status)  \\(.request_time)s  \\(.request_method) \\(.request_uri) \\(.http_user_agent | .[0:50])\"
    " 2>/dev/null
}

cmd_errors() {
    echo -e "${BOLD}Errors (4xx/5xx) — last 24h${NC}"
    echo ""
    ssh_cmd "sudo cat $LOG" | jq -r "
        ${SITE_FILTER} | ${BOT_FILTER} |
        select(.status | tonumber >= 400) |
        \"\\(.time_local)  \\(.remote_addr)  \\(.status)  \\(.request_method) \\(.request_uri)  UA: \\(.http_user_agent | .[0:60])\"
    " 2>/dev/null | sort -t' ' -k1 | tail -50
}

cmd_users() {
    echo -e "${BOLD}Unique users today — inspect.metaprodtrace.com${NC}"
    echo ""
    ssh_cmd "sudo cat $LOG" | jq -r "
        ${SITE_FILTER} | ${BOT_FILTER} |
        select(.status | tonumber < 400) |
        select(.request_uri | test(\"^/login|^/$|^/doc\")) |
        \"\\(.remote_addr)  \\(.time_local)  \\(.status)  \\(.request_uri)  \\(.http_user_agent | .[0:70])\"
    " 2>/dev/null | sort -u -t' ' -k1,1 | column -t
}

cmd_slow() {
    echo -e "${BOLD}Slow requests (>2s) — last 24h${NC}"
    echo ""
    ssh_cmd "sudo cat $LOG" | jq -r "
        ${SITE_FILTER} | ${BOT_FILTER} |
        select(.request_time | tonumber > 2) |
        \"\\(.time_local)  \\(.request_time)s  \\(.remote_addr)  \\(.request_method) \\(.request_uri)  \\(.upstream_response_time)s upstream\"
    " 2>/dev/null | sort -t' ' -k1 | tail -30
    echo ""
    echo -e "${CYAN}(upstream_response_time = gunicorn processing time)${NC}"
}

cmd_status() {
    echo -e "${BOLD}Quick health check${NC}"
    echo ""

    # Service status
    local svc_status
    svc_status=$(ssh_cmd "systemctl is-active pcb-inspect 2>/dev/null" || echo "unknown")
    if [[ "$svc_status" == "active" ]]; then
        echo -e "  Service:  ${GREEN}active${NC}"
    else
        echo -e "  Service:  ${RED}${svc_status}${NC}"
    fi

    # Last 5 real user requests
    echo ""
    echo -e "  ${CYAN}Last 5 user requests:${NC}"
    ssh_cmd "sudo tail -200 $LOG" | jq -r "
        ${SITE_FILTER} | ${BOT_FILTER} |
        \"  \\(.time_local | split(\" \")[0])  \\(.remote_addr | .[0:15])  \\(.status)  \\(.request_method) \\(.request_uri | .[0:40])\"
    " 2>/dev/null | tail -5

    # Error count today
    echo ""
    local err_count
    err_count=$(ssh_cmd "sudo cat $LOG" | jq -r "
        ${SITE_FILTER} | ${BOT_FILTER} |
        select(.status | tonumber >= 400) | .status
    " 2>/dev/null | wc -l | tr -d ' ')
    if [[ "$err_count" -gt 0 ]]; then
        echo -e "  Errors today: ${RED}${err_count}${NC} (run ./run-monitor.sh errors)"
    else
        echo -e "  Errors today: ${GREEN}0${NC}"
    fi

    # Check from Russia
    echo ""
    echo -e "  ${CYAN}Checking from Russia (check-host.net)...${NC}"
    local check_id
    check_id=$(ssh_cmd "curl -s --max-time 10 'https://check-host.net/check-http?host=https://inspect.metaprodtrace.com/login&node=ru1.node.check-host.net&node=ru2.node.check-host.net&node=ru3.node.check-host.net' -H 'Accept: application/json' 2>/dev/null" | jq -r '.request_id // empty')

    if [[ -n "$check_id" ]]; then
        sleep 8
        local results
        results=$(ssh_cmd "curl -s --max-time 10 'https://check-host.net/check-result/${check_id}' -H 'Accept: application/json' 2>/dev/null")
        echo "$results" | jq -r '
            to_entries[] |
            if .value[0][1] == 1 or (.value[0][3] // "" | tostring | test("200"))
            then "  ✓ \(.key): HTTP \(.value[0][3]) (\(.value[0][1])s)"
            else "  ✗ \(.key): FAILED — \(.value[0][3] // .value[0][1])"
            end
        ' 2>/dev/null || echo "  (couldn't parse results)"
    else
        echo "  (check-host.net unavailable)"
    fi
}

cmd_geo() {
    local ip="${1:?Usage: ./run-monitor.sh geo IP_ADDRESS}"
    echo -e "${BOLD}GeoIP: ${ip}${NC}"
    ssh_cmd "curl -s 'http://ip-api.com/json/${ip}?fields=country,regionName,city,isp,as' 2>/dev/null" | jq -r '"  Country: \(.country)\n  Region:  \(.regionName)\n  City:    \(.city)\n  ISP:     \(.isp)\n  AS:      \(.as)"'
}

case "${1:-help}" in
    live)    cmd_live ;;
    errors)  cmd_errors ;;
    users)   cmd_users ;;
    slow)    cmd_slow ;;
    status)  cmd_status ;;
    geo)     cmd_geo "${2:-}" ;;
    *)
        echo "Usage: ./run-monitor.sh <command>"
        echo ""
        echo "  live     — live tail of real user requests"
        echo "  errors   — 4xx/5xx errors (last 24h)"
        echo "  users    — unique users today"
        echo "  slow     — slow requests >2s"
        echo "  status   — health check + Russia availability"
        echo "  geo IP   — geolocate an IP address"
        ;;
esac
