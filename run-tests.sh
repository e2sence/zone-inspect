#!/usr/bin/env bash
# ─── Zone Inspect — Test Runner ──────────────────────────────────────────────
set -euo pipefail
cd "$(dirname "$0")"

PYTEST=".venv/bin/python -m pytest"

show_help() {
    cat <<'EOF'
Usage: bash run-tests.sh [command] [pytest args...]

Commands:
  (none)      Run all 86 tests
  unit        Helper functions only (no HTTP)
  api         API endpoints + full check cycle
  auth        Authentication & roles
  mobile      Mobile workflow (QR, tokens)
  templates   Template CRUD (needs MongoDB)
  api_v1      External API v1 (needs MongoDB)
  edge        Edge cases & robustness
  mongo       All MongoDB-dependent tests
  list        Show test registry (collected tests)
  markers     Show available markers
  help        This message

Examples:
  bash run-tests.sh                   # all tests
  bash run-tests.sh unit              # only unit tests
  bash run-tests.sh auth              # auth + roles
  bash run-tests.sh -k brute          # by test name
  bash run-tests.sh --tb=long         # verbose tracebacks
  bash run-tests.sh unit -x           # stop on first failure
EOF
}

case "${1:-}" in
    help|--help|-h)
        show_help
        ;;
    list)
        shift
        $PYTEST --collect-only -q "$@"
        ;;
    markers)
        $PYTEST --markers | grep -A1 "^@pytest.mark\.\(unit\|api\|auth\|mobile\|templates\|api_v1\|edge\|mongo\)"
        ;;
    unit|api|auth|mobile|templates|api_v1|edge|mongo)
        marker="$1"; shift
        $PYTEST -m "$marker" "$@"
        ;;
    *)
        $PYTEST "$@"
        ;;
esac
