#!/bin/bash
# chain_finder_wrapper.sh — Jalankan chain_finder_github.py, kirim notif ke Telegram
set -e

cd /home/hermes/x-monitor

OUTPUT=$(python3 chain_finder_github.py 2>&1)

# Skip kalau tidak ada PR baru
if echo "$OUTPUT" | grep -q "Tidak ada PR"; then
    exit 0
fi
if [ -z "$OUTPUT" ]; then
    exit 0
fi

# Format box
BODY=$(echo "$OUTPUT" | python3 -c "
import sys
lines = [l.rstrip() for l in sys.stdin if l.strip()]
print('\n'.join(lines))
")

curl -s -X POST "https://api.telegram.org/bot8896670247:AAHM0bbuWK-wNBaRZTZLSs2s0HQNnQdA_zo/sendMessage" \
    -d "chat_id=-1003641668106" \
    -d "text=<b>🔗 Chain Registry PR Baru</b>

$BODY" \
    -d "parse_mode=HTML" \
    -d "disable_web_page_preview=true" > /dev/null 2>&1

echo "[$(date)] Notifikasi dikirim"