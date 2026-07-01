#!/usr/bin/env python3
"""
Chain Finder X — Twitter keyword monitoring for new Cosmos chains.
Only searches and notifies (no likes/quotes/retweets).
Runs on separate interval from general Twitter monitor.
"""

import os
import sys
import json
import time
import random
import logging
from datetime import datetime, timedelta
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------- CONFIG ----------
BASE_DIR = Path("/home/hermes/x-monitor")
COOKIE_FILE = BASE_DIR / "x_cookies.txt"
SEEN_FILE = BASE_DIR / "chain_finder_seen.json"
PENDING_FILE = BASE_DIR / "chain_finder_pending.json"
LOG_FILE = BASE_DIR / "chain_finder_x.log"

KEYWORDS = [
    "becoming validator",
    "new validator",
    "open delegation",
    "delegation program",
    "new delegation",
    "cosmos validator",
    "node validator",
]

# Telegram
TG_TOKEN = None
TG_CHAT_ID = "-1003641668106"
TG_THREAD_ID = "9"   # validator news
TG_API = "https://api.telegram.org/bot{token}/sendMessage"

# Silent hours (WIB = UTC+7)
SILENT_START_HOUR = 21   # 21:00
SILENT_END_HOUR = 5      # 05:00

# ---------- SETUP LOGGING ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("chain_finder_x")

# ---------- LOAD CONFIG ----------
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
        logger.error(f"Failed to load TG token: {e}")
        sys.exit(1)

load_tg_token()

# ---------- SESSION ----------
def get_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.cookies = requests.utils.cookiejar_from_dict(parse_netscape_cookies(COOKIE_FILE))
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://x.com/",
        "Origin": "https://x.com",
    })
    csrf = session.cookies.get("ct0")
    if csrf:
        session.headers.update({"x-csrf-token": csrf})
    return session

def parse_netscape_cookies(filepath):
    cookies = {}
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                domain, flag, path, secure, expires, name, value = parts[:7]
                cookies[name] = value
    return cookies

# ---------- STATE ----------
def load_seen():
    if SEEN_FILE.exists():
        try:
            with open(SEEN_FILE) as f:
                return json.load(f).get("tweets", [])
        except:
            return []
    return []

def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump({"tweets": seen}, f, indent=2)

def load_pending():
    if PENDING_FILE.exists():
        try:
            with open(PENDING_FILE) as f:
                return json.load(f).get("tweets", [])
        except:
            return []
    return []

def save_pending(pending):
    with open(PENDING_FILE, "w") as f:
        json.dump({"tweets": pending}, f, indent=2)

# ---------- SEARCH ----------
def search_tweets(session, keyword, count=20):
    url = "https://x.com/i/api/1.1/search/universal.json"
    params = {
        "q": keyword,
        "count": count,
        "include_entities": "true",
        "result_type": "recent",
        "tweet_mode": "extended",
    }
    try:
        resp = session.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            logger.warning(f"Search failed for '{keyword}': {resp.status_code}")
            return []
        data = resp.json()
        tweets = []
        for item in data.get("statuses", []):
            tweet_id = str(item.get("id_str", ""))
            text = item.get("full_text", "") or item.get("text", "")
            user = item.get("user", {}).get("screen_name", "")
            if tweet_id and text:
                tweets.append({
                    "id": tweet_id,
                    "text": text,
                    "user": user,
                    "created_at": item.get("created_at", ""),
                    "url": f"https://x.com/{user}/status/{tweet_id}"
                })
        return tweets
    except Exception as e:
        logger.error(f"Search error for '{keyword}': {e}")
        return []

# ---------- NOTIFICATION ----------
def send_telegram(text, thread_id=TG_THREAD_ID):
    if not TG_TOKEN:
        return False
    url = TG_API.format(token=TG_TOKEN)
    params = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if thread_id:
        params["message_thread_id"] = thread_id
    try:
        resp = requests.post(url, data=params, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False

# ---------- MAIN ----------
def main():
    logger.info("Chain Finder X started")
    session = get_session()
    seen = load_seen()
    pending = load_pending()
    now = datetime.now().astimezone()
    wib = now + timedelta(hours=7)
    hour = wib.hour
    is_silent = (hour >= SILENT_START_HOUR or hour < SILENT_END_HOUR)

    new_tweets = []
    for kw in KEYWORDS:
        tweets = search_tweets(session, kw, count=10)
        for t in tweets:
            if t["id"] not in seen:
                new_tweets.append(t)
                seen.append(t["id"])
        time.sleep(random.randint(3, 10))

    if new_tweets:
        logger.info(f"Found {len(new_tweets)} new chain-related tweets")
        save_seen(seen)

        if is_silent:
            # Store pending for digest
            pending.extend(new_tweets)
            save_pending(pending)
            logger.info(f"Stored {len(new_tweets)} tweets in pending (silent hours)")
        else:
            # Send each tweet as notification
            for t in new_tweets[:10]:  # limit per run
                msg = f" <b>New Chain Finder Tweet</b>\n@{t['user']}: {t['text'][:200]}\n<a href='{t['url']}'>View</a>"
                send_telegram(msg)
                time.sleep(random.randint(5, 15))
    else:
        logger.info("No new chain-related tweets found")

    # If we are in digest mode (05.05), also send pending
    if len(sys.argv) > 1 and sys.argv[1] == "digest":
        if pending:
            digest_lines = [" <b>Chain Finder Daily Digest</b>"]
            digest_lines.append(f"\n <b>{len(pending)} new tweets found during silent hours:</b>")
            for t in pending[:15]:
                digest_lines.append(f"- @{t['user']}: {t['text'][:150]}… <a href='{t['url']}'>link</a>")
            send_telegram("\n".join(digest_lines))
            pending = []
            save_pending(pending)
            logger.info("Sent digest and cleared pending")

    logger.info("Chain Finder X finished")

if __name__ == "__main__":
    main()
