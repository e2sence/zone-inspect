#!/usr/bin/env bash
# Локальный запуск с HTTPS через постоянный Cloudflare Named Tunnel
set -euo pipefail
cd "$(dirname "$0")"

# Активировать venv
source .venv/bin/activate

PORT=5001
CF_URL="https://dev-inspect.metaprodtrace.com"

# Убить предыдущие процессы на порту (если есть)
lsof -ti:$PORT | xargs kill -9 2>/dev/null || true
pkill -f 'cloudflared tunnel run' 2>/dev/null || true

# Запустить Named Tunnel в фоне (постоянный URL)
cloudflared tunnel --no-autoupdate run pcb-inspect > /tmp/cloudflared-pcb.log 2>&1 &
CF_PID=$!

cleanup() {
    kill $CF_PID 2>/dev/null || true
    lsof -ti:$PORT | xargs kill -9 2>/dev/null || true
}
trap cleanup EXIT

# Подождать пока cloudflared подключится
echo "⏳ Запуск Cloudflare Named Tunnel..."
for i in {1..15}; do
    sleep 1
    if grep -q 'Registered tunnel connection' /tmp/cloudflared-pcb.log 2>/dev/null; then
        break
    fi
done

if ! grep -q 'Registered tunnel connection' /tmp/cloudflared-pcb.log 2>/dev/null; then
    echo "⚠️  Tunnel не подключился, проверьте /tmp/cloudflared-pcb.log"
fi

export BASE_URL="$CF_URL"

echo "════════════════════════════════════════"
echo "  PCB Inspect — Local Dev Server"
echo "  Local:    http://localhost:$PORT"
echo "  Mobile:   ${CF_URL}  (постоянный)"
echo "════════════════════════════════════════"

python app.py
