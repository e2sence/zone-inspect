#!/usr/bin/env bash
#═══════════════════════════════════════════════════════════════════════════════
#  run-deploy.sh — deploy Zone Inspect to production
#
#  Usage:
#    ./run-deploy.sh diff          — list files that differ between local and prod
#    ./run-deploy.sh diff-detail   — show the differences line by line
#    ./run-deploy.sh deploy        — back up, copy changed files, restart, health check
#    ./run-deploy.sh rollback [archive]
#                                  — list backups, or restore one and restart
#    ./run-deploy.sh logs          — tail production logs
#    ./run-deploy.sh status        — check if the prod service is running
#    ./run-deploy.sh restart       — restart the prod service without copying code
#    ./run-deploy.sh ssh           — open an SSH session to the server
#
#  What deploy does and does not do:
#    - Copies only git-tracked runtime files (top-level *.py, static/, templates/,
#      requirements-prod.txt) and only the ones that differ from prod.
#    - Never deletes anything on the server. Files that exist only on prod are
#      listed as EXTRA and left alone.
#    - Never touches secrets, runtime data, the venv or other services.
#    - Saves the files it is about to overwrite to $DEPLOY_BACKUP_DIR first.
#    - A restart drops in-flight inspection sessions (the app clears uploads/ on
#      start), so deploy shows recent activity before asking for confirmation.
#═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ─── Configuration ────────────────────────────────────────────────────────────
SERVER="${DEPLOY_SERVER:?Set DEPLOY_SERVER env var, e.g. user@host}"
APP_DIR="${DEPLOY_APP_DIR:-/opt/pcb-inspect}"
SERVICE="${DEPLOY_SERVICE:-pcb-inspect}"
BACKUP_DIR="${DEPLOY_BACKUP_DIR:-backups}"          # relative to the remote home
HEALTH_URL="${DEPLOY_HEALTH_URL:-http://127.0.0.1:5001/healthz}"
ACTIVITY_MINUTES=15
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

# SSH multiplexing — one TCP connection reused for all commands
CTRL_SOCKET="/tmp/pcb-deploy-ssh-$$"
SSH_OPTS="-o ConnectTimeout=15 -o ControlMaster=auto -o ControlPath=$CTRL_SOCKET -o ControlPersist=120"
SSH="ssh -n $SSH_OPTS $SERVER"        # -n: must not swallow the answer to a prompt
SSH_STDIN="ssh $SSH_OPTS $SERVER"    # for commands that are fed a file list

TMP="$(mktemp -d)"
cleanup() {
    rm -rf "$TMP"
    ssh -O exit -o ControlPath="$CTRL_SOCKET" "$SERVER" 2>/dev/null || true
}
trap cleanup EXIT

# ─── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

die() { echo -e "${RED}$*${NC}" >&2; exit 1; }

# ─── File lists ───────────────────────────────────────────────────────────────

# Runtime files that belong on the server: tracked by git, nothing else.
list_deploy_files() {
    # ':(glob)*.py' = top-level modules only (not tests/ or integrations/).
    git -C "$LOCAL_DIR" ls-files -- ':(glob)*.py' static templates requirements-prod.txt \
        | sort
}

local_md5() {
    if command -v md5sum >/dev/null 2>&1; then
        md5sum "$1" | cut -d' ' -f1
    else
        md5 -q "$1"
    fi
}

# Fills $TMP/files (everything deployable), $TMP/new and $TMP/changed
# (what differs), $TMP/extra (present only on prod — reported, never removed).
compute_changes() {
    list_deploy_files > "$TMP/files"
    [ -s "$TMP/files" ] || die "No deployable files found — is this a git checkout?"

    : > "$TMP/local.sums"
    while IFS= read -r f; do
        [ -f "$LOCAL_DIR/$f" ] || die "Tracked file is missing locally: $f"
        printf '%s  %s\n' "$(local_md5 "$LOCAL_DIR/$f")" "$f" >> "$TMP/local.sums"
    done < "$TMP/files"

    $SSH_STDIN "cd '$APP_DIR' && while IFS= read -r f; do
              if [ -f \"\$f\" ]; then md5sum \"\$f\"; else echo \"MISSING  \$f\"; fi
          done" < "$TMP/files" > "$TMP/remote.sums" \
        || die "Could not read file checksums from $SERVER:$APP_DIR"

    # A short answer would make every file look changed — refuse to guess.
    local n_local n_remote
    n_local=$(wc -l < "$TMP/local.sums" | tr -d ' ')
    n_remote=$(wc -l < "$TMP/remote.sums" | tr -d ' ')
    [ "$n_local" = "$n_remote" ] \
        || die "Checksum list from the server is incomplete ($n_remote of $n_local) — aborting."

    awk 'NR==FNR { r[$2]=$1; next } r[$2]=="MISSING" { print $2 }' \
        "$TMP/remote.sums" "$TMP/local.sums" > "$TMP/new"
    awk 'NR==FNR { r[$2]=$1; next } r[$2]!="MISSING" && r[$2]!=$1 { print $2 }' \
        "$TMP/remote.sums" "$TMP/local.sums" > "$TMP/changed"

    $SSH "cd '$APP_DIR' && { find . -maxdepth 1 -type f -name '*.py';
              find static templates -type f 2>/dev/null; } | sed 's|^\./||' | sort" \
        > "$TMP/remote.files" || die "Could not list files on $SERVER:$APP_DIR"
    comm -13 "$TMP/files" "$TMP/remote.files" > "$TMP/extra"
}

print_changes() {
    local f
    while IFS= read -r f; do echo -e "  ${YELLOW}+ NEW${NC}   $f  (not on prod)"; done < "$TMP/new"
    while IFS= read -r f; do echo -e "  ${RED}~ DIFF${NC}  $f"; done < "$TMP/changed"
    while IFS= read -r f; do echo -e "  ${CYAN}? EXTRA${NC} $f  (only on prod, left alone)"; done < "$TMP/extra"
    if [ ! -s "$TMP/new" ] && [ ! -s "$TMP/changed" ]; then
        echo -e "  ${GREEN}Prod already has every local file — nothing to deploy.${NC}"
    fi
}

print_local_state() {
    local branch commit dirty
    branch=$(git -C "$LOCAL_DIR" rev-parse --abbrev-ref HEAD)
    commit=$(git -C "$LOCAL_DIR" rev-parse --short HEAD)
    echo -e "  Local: branch ${BOLD}$branch${NC}, commit ${BOLD}$commit${NC}"
    dirty=$(cd "$LOCAL_DIR" && git status --porcelain -- $(tr '\n' ' ' < "$TMP/files"))
    if [ -n "$dirty" ]; then
        echo -e "  ${YELLOW}Uncommitted changes would be deployed as they are on disk:${NC}"
        echo "$dirty" | sed 's/^/    /'
    fi
}

wait_healthy() {
    local i
    for i in $(seq 1 30); do
        if $SSH "curl -fsS -m 5 '$HEALTH_URL'" > "$TMP/health" 2>/dev/null; then
            echo "   $(cat "$TMP/health")"
            return 0
        fi
        sleep 2
    done
    return 1
}

restart_and_check() {
    $SSH "sudo systemctl restart '$SERVICE'" || return 1
    if wait_healthy; then
        return 0
    fi
    echo -e "${RED}Service did not become healthy within 60 s. Last log lines:${NC}"
    $SSH "sudo journalctl -u '$SERVICE' --no-pager -n 30" || true
    return 1
}

# ─── Commands ─────────────────────────────────────────────────────────────────

cmd_diff() {
    echo -e "${BOLD}Comparing local ↔ production ($SERVER:$APP_DIR)${NC}"
    echo ""
    compute_changes
    print_local_state
    echo ""
    print_changes
}

cmd_diff_detail() {
    echo -e "${BOLD}Detailed diff: local ↔ production${NC}"
    echo ""
    compute_changes
    local f
    while IFS= read -r f; do
        case "$f" in
            *.py|*.js|*.css|*.html|*.txt) ;;
            *) echo -e "${YELLOW}─── $f ─── (binary, not shown)${NC}"; continue ;;
        esac
        echo -e "${YELLOW}─── $f ───${NC}"
        $SSH "cat '$APP_DIR/$f'" > "$TMP/remote.file"
        diff -u --label "prod:$f" --label "local:$f" "$TMP/remote.file" "$LOCAL_DIR/$f" || true
        echo ""
    done < "$TMP/changed"
    print_changes
}

cmd_deploy() {
    echo -e "${BOLD}═══════════════════════════════════════════════════════${NC}"
    echo -e "${BOLD}  Zone Inspect — Deploy local → $SERVER:$APP_DIR${NC}"
    echo -e "${BOLD}═══════════════════════════════════════════════════════${NC}"
    echo ""

    echo -e "[${CYAN}1/6${NC}] Comparing with production..."
    compute_changes
    print_local_state
    print_changes
    cat "$TMP/new" "$TMP/changed" | sort > "$TMP/send"
    [ -s "$TMP/send" ] || exit 0
    echo ""

    # A file that does not parse must never reach the server.
    echo -e "[${CYAN}2/6${NC}] Checking Python syntax locally..."
    local py_files
    py_files=$(grep '\.py$' "$TMP/send" || true)
    if [ -n "$py_files" ]; then
        (cd "$LOCAL_DIR" && python3 -c '
import ast, sys
for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as fh:
        ast.parse(fh.read(), path)
' $py_files) || die "Syntax error — nothing was sent."
    fi
    echo -e "   ${GREEN}OK${NC}"
    echo ""

    local active
    active=$($SSH "find '$APP_DIR/uploads' -type f -mmin -$ACTIVITY_MINUTES 2>/dev/null | wc -l" | tr -d ' ')
    if [ "$active" != "0" ]; then
        echo -e "${YELLOW}Stations look active: $active upload(s) in the last $ACTIVITY_MINUTES minutes.${NC}"
        echo -e "${YELLOW}The restart will drop their current inspection sessions.${NC}"
    else
        echo "No uploads in the last $ACTIVITY_MINUTES minutes."
    fi
    local reply
    read -r -p "Deploy $(wc -l < "$TMP/send" | tr -d ' ') file(s) and restart $SERVICE? [y/N] " reply || reply=""
    [[ "$reply" =~ ^[Yy]$ ]] || { echo "Aborted — nothing was changed."; exit 0; }
    echo ""

    echo -e "[${CYAN}3/6${NC}] Backing up the files that will be overwritten..."
    local backup=""
    if [ -s "$TMP/changed" ]; then
        backup="$BACKUP_DIR/pcb-inspect-$(date -u +%Y%m%d-%H%M%S).tgz"
        local expected saved
        expected=$(wc -l < "$TMP/changed" | tr -d ' ')
        saved=$($SSH_STDIN "umask 077 && mkdir -p '$BACKUP_DIR' \
                      && tar czf '$backup' -C '$APP_DIR' -T - \
                      && tar tzf '$backup' | wc -l" < "$TMP/changed" | tr -d ' ') \
            || die "Backup failed — nothing was changed."
        [ "$saved" = "$expected" ] \
            || die "Backup holds $saved of $expected files — nothing was changed."
        echo -e "   ${GREEN}~/$backup${NC} ($saved files)"
    else
        echo "   Only new files — nothing to back up."
    fi
    echo ""

    # Install dependencies while the old code is still in place and running.
    echo -e "[${CYAN}4/6${NC}] Dependencies..."
    if grep -qx 'requirements-prod.txt' "$TMP/send"; then
        echo "   requirements-prod.txt changed — installing..."
        rsync -tz -e "ssh $SSH_OPTS" \
            "$LOCAL_DIR/requirements-prod.txt" "$SERVER:$APP_DIR/requirements-prod.txt"
        $SSH "cd '$APP_DIR' && venv/bin/pip install --quiet -r requirements-prod.txt" \
            || die "pip install failed. Code was not copied and the service was not restarted."
        echo -e "   ${GREEN}Dependencies updated${NC}"
    else
        echo "   Unchanged, skipping"
    fi
    echo ""

    echo -e "[${CYAN}5/6${NC}] Copying files..."
    rsync -tz --itemize-changes --files-from="$TMP/send" -e "ssh $SSH_OPTS" \
        "$LOCAL_DIR/" "$SERVER:$APP_DIR/"
    echo -e "   ${GREEN}Copied${NC}"
    echo ""

    echo -e "[${CYAN}6/6${NC}] Restarting $SERVICE and waiting for $HEALTH_URL ..."
    if restart_and_check; then
        echo ""
        echo -e "${GREEN}${BOLD}Deploy complete.${NC}"
        [ -z "$backup" ] || echo "  Previous files: ~/$backup"
    else
        echo ""
        echo -e "${RED}${BOLD}Deploy copied the files but the service is not healthy.${NC}"
        [ -z "$backup" ] || echo -e "  Restore the previous files:  ${BOLD}$0 rollback $backup${NC}"
        if [ -s "$TMP/new" ]; then
            echo "  Files added by this deploy (a rollback leaves them in place):"
            sed 's/^/    /' "$TMP/new"
        fi
        exit 1
    fi
}

cmd_rollback() {
    local archive="${1:-}"
    if [ -z "$archive" ]; then
        echo -e "${BOLD}Backups on $SERVER (newest last):${NC}"
        $SSH "ls -1 '$BACKUP_DIR'/pcb-inspect-*.tgz 2>/dev/null | tail -15" || true
        echo ""
        echo "Restore one with:  $0 rollback <archive>"
        return 0
    fi

    echo -e "${BOLD}Files in ~/$archive:${NC}"
    $SSH "tar tzf '$archive'" | sed 's/^/  /' || die "Cannot read ~/$archive on $SERVER"
    echo ""
    local reply
    read -r -p "Overwrite these files in $APP_DIR and restart $SERVICE? [y/N] " reply || reply=""
    [[ "$reply" =~ ^[Yy]$ ]] || { echo "Aborted — nothing was changed."; exit 0; }

    $SSH "tar xzf '$archive' -C '$APP_DIR'" || die "Restore failed."
    echo "Restored. Restarting $SERVICE..."
    restart_and_check || die "Service is not healthy after the rollback."
    echo -e "${GREEN}${BOLD}Rollback complete.${NC}"
}

cmd_logs() {
    echo -e "${BOLD}Production logs (Ctrl+C to stop):${NC}"
    $SSH "sudo journalctl -u '$SERVICE' -f --no-pager -n 50"
}

cmd_status() {
    echo -e "${BOLD}Service status:${NC}"
    $SSH "sudo systemctl status '$SERVICE' --no-pager -l" || true
}

cmd_restart() {
    echo "Restarting ${SERVICE}..."
    restart_and_check || die "Service is not healthy after the restart."
    echo -e "${GREEN}Restarted.${NC}"
}

cmd_ssh() {
    echo "Connecting to $SERVER..."
    ssh "$SERVER"
}

# ─── Main ─────────────────────────────────────────────────────────────────────
CMD="${1:-help}"

case "$CMD" in
    diff)        cmd_diff ;;
    diff-detail) cmd_diff_detail ;;
    deploy)      cmd_deploy ;;
    rollback)    cmd_rollback "${2:-}" ;;
    logs)        cmd_logs ;;
    status)      cmd_status ;;
    restart)     cmd_restart ;;
    ssh)         cmd_ssh ;;
    *)
        echo "Zone Inspect Deploy Tool"
        echo ""
        echo "Usage: ./run-deploy.sh <command>"
        echo ""
        echo "Commands:"
        echo "  diff         List files that differ between local and prod"
        echo "  diff-detail  Show the differences line by line"
        echo "  deploy       Back up, copy changed files, restart, health check"
        echo "  rollback     List backups, or restore one: rollback <archive>"
        echo "  logs         Tail production logs"
        echo "  status       Check production service status"
        echo "  restart      Restart prod (no code copy)"
        echo "  ssh          Open SSH session to server"
        ;;
esac
