#!/usr/bin/env python3
"""
x_monitor.py — Pantau akun X, like + quote post baru, kirim notifikasi ke Telegram

Cara pakai:
  1. Setup auth xurl (lihat SKILL.md xurl)
  2. Isi monitored_accounts.txt (satu baris per handle, tanpa @)
  3. Jalankan: python3 x_monitor.py --test
  4. Cron: ./x_monitor_wrapper.sh

State: ~/.hermes/x-monitor/seen_tweets.json
"""

import os
import sys
import json
import subprocess
import time
import random
from datetime import datetime, timedelta
from pathlib import Path

STATE_DIR = Path(os.path.expanduser("~/.hermes/x-monitor"))
STATE_FILE = STATE_DIR / "seen_tweets.json"
ACCOUNTS_FILE = Path(__file__).parent / "monitored_accounts.txt"

STATE_DIR.mkdir(parents=True, exist_ok=True)

# Konfigurasi
INTERVAL_HOURS = 3
MAX_ACTIONS_PER_RUN = 3
DELAY_BETWEEN_ACTIONS = (15, 45)  # detik, acak

# Fallback: kalau xurl belum ready, pakai mode dummy
DUMMY_MODE = False

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"seen": []}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def get_monitored_accounts():
    if not ACCOUNTS_FILE.exists():
        print(f"⚠ File {ACCOUNTS_FILE} tidak ditemukan. Buat dengan daftar akun.")
        return []
    with open(ACCOUNTS_FILE) as f:
        return [line.strip().lstrip('@') for line in f if line.strip() and not line.startswith('#')]

def get_latest_tweet(handle):
    """Ambil tweet terakhir dari akun via xurl"""
    if DUMMY_MODE:
        # Dummy: selalu return tweet baru (untuk test)
        return {
            "id": f"dummy_{int(time.time())}",
            "text": f"Test post dari {handle} pada {datetime.now().isoformat()}",
            "created_at": datetime.now().isoformat()
        }
    try:
        # xurl user --format json @handle
        cmd = ["xurl", "user", f"@{handle}", "--format", "json"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            print(f"⚠ Gagal ambil user {handle}: {result.stderr}")
            return None
        data = json.loads(result.stdout)
        # Ambil tweet terakhir dari data
        # Format xurl user output: {...} dengan tweets di data.tweets
        if "tweets" in data and data["tweets"]:
            return data["tweets"][0]
        return None
    except Exception as e:
        print(f"⚠ Error fetch {handle}: {e}")
        return None

def generate_quote(tweet_text, handle):
    """Generate quote via 9router"""
    if DUMMY_MODE:
        return f"Menarik! {handle} mengatakan: {tweet_text[:50]}..."
    try:
        # Panggil Hermes CLI dengan prompt
        prompt = (
            f"Tweet dari @{handle}: \"{tweet_text}\".\n"
            "Buat quote tweet yang relevan, insightful, dan tidak keluar konteks. "
            "Singkat, maksimal 280 karakter. Jangan pakai emoji berlebihan. "
            "Bahasa Indonesia atau Inggris tergantung tweet asli."
        )
        cmd = [
            "/home/hermes/.local/bin/hermes",
            "chat", "-q", prompt,
            "-Q", "--provider", "custom", "--model", "Knight"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except Exception as e:
        print(f"⚠ Error generate quote: {e}")
        return None

def post_like_quote(tweet_id, quote_text):
    """Like + Quote tweet"""
    if DUMMY_MODE:
        print(f"[DUMMY] Like + Quote tweet {tweet_id}: {quote_text[:50]}...")
        return True
    try:
        # Like dulu
        like_cmd = ["xurl", "like", tweet_id]
        subprocess.run(like_cmd, check=True, timeout=10)
        time.sleep(random.uniform(1, 3))
        # Quote
        quote_cmd = ["xurl", "quote", tweet_id, quote_text]
        subprocess.run(quote_cmd, check=True, timeout=10)
        return True
    except Exception as e:
        print(f"⚠ Error like+quote {tweet_id}: {e}")
        return False

def send_telegram_notification(message, thread_id=None):
    """Kirim notifikasi ke grup Telegram"""
    # Untuk sekarang hanya print. Nanti pakai send_message tool.
    print(f"[TELEGRAM] {message}")
    # TODO: integrate with send_message

def main():
    global DUMMY_MODE
    if "--test" in sys.argv:
        DUMMY_MODE = True
        print(" DUMMY MODE — tidak benar-benar posting ke X")
    
    accounts = get_monitored_accounts()
    if not accounts:
        print("❌ Tidak ada akun yang dipantau. Isi monitored_accounts.txt")
        return
    
    state = load_state()
    seen = set(state["seen"])
    actions_taken = 0
    
    print(f" Memantau {len(accounts)} akun: {', '.join(accounts)}")
    
    for handle in accounts:
        if actions_taken >= MAX_ACTIONS_PER_RUN:
            print(f"⏹ Maks {MAX_ACTIONS_PER_RUN} aksi per run tercapai.")
            break
        
        tweet = get_latest_tweet(handle)
        if not tweet:
            continue
        
        tweet_id = tweet.get("id")
        tweet_text = tweet.get("text", "")
        
        if tweet_id in seen:
            continue
        
        print(f" Tweet baru dari @{handle}: {tweet_text[:60]}...")
        
        # Generate quote
        quote = generate_quote(tweet_text, handle)
        if not quote:
            print(f"⚠ Skip @{handle} — gagal generate quote")
            # Fallback: like+retweet
            print(f"↩ Fallback: Like + Retweet untuk @{handle}")
            if not DUMMY_MODE:
                try:
                    subprocess.run(["xurl", "like", tweet_id], check=True)
                    time.sleep(random.uniform(1, 2))
                    subprocess.run(["xurl", "repost", tweet_id], check=True)
                except Exception as e:
                    print(f"⚠ Fallback gagal: {e}")
            seen.add(tweet_id)
            actions_taken += 1
            continue
        
        # Post like + quote
        success = post_like_quote(tweet_id, quote)
        if success:
            seen.add(tweet_id)
            actions_taken += 1
            print(f"✅ Like+Quote @{handle} sukses")
            # Notif ke Telegram
            notif = f" Quote dari @{handle}\n\n{quote}\n\n https://x.com/{handle}/status/{tweet_id}"
            send_telegram_notification(notif)
        else:
            print(f"⚠ Gagal like+quote @{handle}")
        
        # Delay acak antar aksi
        if actions_taken < MAX_ACTIONS_PER_RUN:
            delay = random.randint(*DELAY_BETWEEN_ACTIONS)
            print(f"⏳ Tunggu {delay}s sebelum aksi berikutnya...")
            time.sleep(delay)
    
    # Simpan state
    state["seen"] = list(seen)
    save_state(state)
    print(f"✅ Selesai. {actions_taken} aksi dilakukan.")

if __name__ == "__main__":
    main()
