#!/usr/bin/env python3
"""
chain_finder_github.py — Pantau PR chain baru di repositori Cosmos
Output: kirim ke grup Telegram via bot (bukan stdout/Hermes)
Fitur: silent hours (21.00–05.00 WIB) — notifikasi ditahan sampai jam aktif.

Repositori yang dipantau:
  - chainapsis/keplr-chain-registry
  - cosmos/chain-registry
  - ping-pub/mainnet
  - ping-pub/testnet
"""

import requests
import json
import os
import sys
from datetime import datetime, timedelta, timezone
import html
from pathlib import Path

STATE_FILE = os.path.expanduser("~/.hermes/x-monitor/seen_prs.json")
PENDING_FILE = os.path.expanduser("~/.hermes/x-monitor/chain_finder_github_pending.json")
REPOS = [
    "chainapsis/keplr-chain-registry",
    "cosmos/chain-registry",
    "ping-pub/mainnet",
    "ping-pub/testnet",
]

# Telegram config
TG_TOKEN = None
TG_CHAT_ID = "-1003641668106"
TG_THREAD_ID = "9"  # validator news

# Silent hours (WIB = UTC+7)
SILENT_START_HOUR = 21   # 21:00
SILENT_END_HOUR = 5      # 05:00

def load_tg_token():
    global TG_TOKEN
    if TG_TOKEN:
        return
    try:
        with open("/home/hermes/.hermes/bridge_config.json") as f:
            cfg = json.load(f)
        TG_TOKEN = cfg.get("token")
        if not TG_TOKEN:
            raise ValueError("No token in bridge_config.json")
    except Exception as e:
        print(f"Failed to load TG token: {e}", file=sys.stderr)
        sys.exit(1)

def send_telegram(text):
    if not TG_TOKEN:
        load_tg_token()
    if not TG_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    params = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "message_thread_id": TG_THREAD_ID,
    }
    try:
        resp = requests.post(url, data=params, timeout=10)
        if resp.status_code == 200:
            print("Telegram notification sent", file=sys.stderr)
            return True
        else:
            print(f"Telegram failed: {resp.status_code} - {resp.text[:200]}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"Telegram error: {e}", file=sys.stderr)
        return False

def load_seen():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"prs": []}

def save_seen(data):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_pending():
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE) as f:
            return json.load(f).get("prs", [])
    return []

def save_pending(pending):
    os.makedirs(os.path.dirname(PENDING_FILE), exist_ok=True)
    with open(PENDING_FILE, "w") as f:
        json.dump({"prs": pending}, f, indent=2)

def get_pr_files(pr_url):
    try:
        r = requests.get(pr_url + "/files", timeout=10)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return []

def is_chain_pr(files, repo):
    keywords = ["chain.json", "assetlist.json", "chain.schema.json"]
    for f in files:
        fname = f["filename"]
        for kw in keywords:
            if kw in fname:
                # Only notify on genuinely new chain files, not edits to existing ones
                if f["status"] == "added" or "new folder" in f.get("patch", "").lower():
                    return True
    return False

def extract_chain_name(files, pr_title):
    import re
    for f in files:
        parts = f["filename"].split("/")
        for i, p in enumerate(parts):
            if p in ("chain-registry", "keplr-chain-registry", "mainnet", "testnet") and i+1 < len(parts):
                candidate = parts[i+1]
                if candidate and not candidate.endswith(".json") and not candidate.startswith("."):
                    return candidate
    m = re.search(r'(?:add|Add|ADD)\s+([A-Za-z0-9_-]+)', pr_title)
    if m:
        return m.group(1)
    return None

def is_silent_hours():
    now_utc = datetime.now(timezone.utc)
    wib = now_utc + timedelta(hours=7)
    hour = wib.hour
    return hour >= SILENT_START_HOUR or hour < SILENT_END_HOUR

def main():
    load_tg_token()
    seen = load_seen()
    # Store as "repo:number" to avoid cross-repo PR number collisions
    seen_prs = set(seen["prs"])
    pending = load_pending()
    pending_ids = {f"{p['repo']}#{p['pr_number']}" for p in pending}

    found = []
    headers = {"Accept": "application/vnd.github.v3+json"}
    if "GITHUB_TOKEN" in os.environ:
        headers["Authorization"] = f"token {os.environ['GITHUB_TOKEN']}"

    for repo in REPOS:
        url = f"https://api.github.com/repos/{repo}/pulls?state=open&sort=created&direction=desc"
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code != 200:
                print(f"[WARN] {repo}: HTTP {r.status_code}", file=sys.stderr)
                continue
            prs = r.json()
        except Exception as e:
            print(f"[ERROR] {repo}: {e}", file=sys.stderr)
            continue

        for pr in prs:
            pr_key = f"{repo}#{pr['number']}"
            if pr_key in seen_prs or pr_key in pending_ids:
                continue
            files = get_pr_files(pr["url"])
            if not files:
                continue
            if not is_chain_pr(files, repo):
                continue
            chain_name = extract_chain_name(files, pr["title"]) or "?"
            found.append({
                "repo": repo,
                "pr_number": pr["number"],
                "title": pr["title"],
                "url": pr["html_url"],
                "chain_name": chain_name,
                "created_at": pr["created_at"],
                "user": pr["user"]["login"],
            })
            seen_prs.add(pr_key)

    # Gabungkan pending + found baru
    all_to_notify = pending + found

    if not all_to_notify:
        # Still persist seen so we don't re-scan already-seen PRs
        save_seen({"prs": list(seen_prs)})
        print("[⏳] Tidak ada PR chain baru.", file=sys.stderr)
        return

    # Jika jam sepi, simpan ke pending dan keluar (tidak kirim notif)
    if is_silent_hours():
        # Save pending FIRST, then seen — crash between them keeps PRs in pending
        save_pending(all_to_notify)
        save_seen({"prs": list(seen_prs)})
        print(f"[🌙] Jam sepi ({SILENT_START_HOUR}-{SILENT_END_HOUR} WIB). {len(all_to_notify)} PR ditahan.", file=sys.stderr)
        return

    # Jam aktif: kirim semua dan kosongkan pending
    msg_lines = [f"<b>🔗 GitHub Chain Finder</b>", f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} — Found {len(all_to_notify)} new PRs:"]
    for item in all_to_notify:
        msg_lines.append(f"\n• <a href='{html.escape(item['url'], quote=True)}'>{html.escape(item['repo'])} #{item['pr_number']}</a>")
        msg_lines.append(f"   {html.escape(item['title'])}")
        msg_lines.append(f"   Chain: {html.escape(item['chain_name'])}")
        msg_lines.append(f"   Oleh: {html.escape(item['user'])}")
    if send_telegram("\n".join(msg_lines)):
        # Hapus pending setelah berhasil dikirim
        save_pending([])
        save_seen({"prs": list(seen_prs)})
        print("✅ Notifikasi terkirim, pending cleared.", file=sys.stderr)
    else:
        print("❌ Gagal kirim notifikasi, pending tetap disimpan.", file=sys.stderr)
        # Jika gagal, tetap simpan pending agar dicoba lagi
        save_pending(all_to_notify)
        save_seen({"prs": list(seen_prs)})

if __name__ == "__main__":
    main()
