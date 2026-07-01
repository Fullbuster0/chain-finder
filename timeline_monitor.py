#!/usr/bin/env python3
"""
timeline_monitor.py - Monitor specific X accounts and perform actions (like, quote, retweet)
Uses Playwright with cookies for stealth automation.
Features: created_at scraping, retry mechanism.
"""

import os
import sys
import json
import time
import random
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import argparse
import asyncio

# Import XStealthBrowser and run_sync
from x_stealth_browser import XStealthBrowser, run_sync

# Setup logging
LOG_FILE = os.path.join(os.path.dirname(__file__), "timeline_monitor.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Constants
COOKIES_PATH = os.path.join(os.path.dirname(__file__), "x_cookies.txt")
QUEUE_FILE = os.path.join(os.path.dirname(__file__), "timeline_queue.json")
SEEN_FILE = os.path.join(os.path.dirname(__file__), "timeline_seen.json")
PENDING_NOTIF_FILE = os.path.join(os.path.dirname(__file__), "pending_notifications.json")

TARGET_ACCOUNTS = [
    "_atomone",
    "Hippo_Protocol",
    "ShentuChain",
    "phoenix_dir",
    "ZetaChain"
]

MAX_AGE_DAYS = 7
MAX_QUEUE_SIZE = 50
ACTIONS_PER_RUN_MIN = 1
ACTIONS_PER_RUN_MAX = 4

WIB_OFFSET = 7
ACTIVE_HOUR_START = 8
ACTIVE_HOUR_END = 21

ACTION_DELAY_MIN = 30
ACTION_DELAY_MAX = 120

LLM_URL = "http://127.0.0.1:20128/v1/chat/completions"
LLM_MODEL = "Knight"

RETRY_ATTEMPTS = 3
RETRY_DELAY = 2  # base delay in seconds, exponential backoff


def is_wib_active() -> bool:
    now_utc = datetime.utcnow()
    now_wib = now_utc + timedelta(hours=WIB_OFFSET)
    hour = now_wib.hour
    return ACTIVE_HOUR_START <= hour < ACTIVE_HOUR_END


def load_json(filepath: str, default=None):
    if default is None:
        default = {}
    if not os.path.exists(filepath):
        return default
    try:
        with open(filepath, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return default


def save_json(filepath: str, data):
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)


def load_seen():
    return load_json(SEEN_FILE, {})


def save_seen(seen):
    save_json(SEEN_FILE, seen)


def load_queue():
    return load_json(QUEUE_FILE, [])


def save_queue(queue):
    save_json(QUEUE_FILE, queue)


def load_pending():
    return load_json(PENDING_NOTIF_FILE, [])


def save_pending(pending):
    save_json(PENDING_NOTIF_FILE, pending)


def generate_quote(tweet_text: str) -> Optional[str]:
    prompt = (
        "Buat komentar singkat (maks 2 kalimat) yang relevan dan natural untuk tweet ini: "
        f"\"{tweet_text[:200]}\". Jangan pakai emoji berlebihan. Respons hanya teks komentar."
    )
    try:
        import requests
        payload = {
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "user": "timeline-monitor"
        }
        resp = requests.post(LLM_URL, json=payload, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            if "choices" in data and data["choices"]:
                return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"LLM quote generation failed: {e}")
    return None


def is_within_7_days(created_at_str: str) -> bool:
    if not created_at_str:
        return True  # fallback: assume recent
    try:
        # Parse ISO datetime (Twitter uses UTC with Z suffix)
        created_at = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
        # Strip timezone info for naive comparison with utcnow
        created_at_utc = created_at.replace(tzinfo=None)
        now_utc = datetime.utcnow()
        return (now_utc - created_at_utc) <= timedelta(days=MAX_AGE_DAYS)
    except Exception as e:
        logger.warning(f"is_within_7_days parse error: {e}")
        return True


def fetch_timeline(account: str, max_posts: int = 10) -> List[Dict]:
    """
    Fetch recent posts from an account using XStealthBrowser.
    Returns list of dicts with 'id', 'text', 'url', 'created_at'.
    Retry on failure.
    """
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            logger.info(f"Fetching timeline for @{account} (attempt {attempt})")
            # Use run_sync to call async function
            result = run_sync(_fetch_timeline_async(account, max_posts))
            if result is not None:
                return result
            else:
                logger.warning(f"Fetch returned None for @{account}, retrying...")
        except Exception as e:
            logger.error(f"Error fetching timeline for @{account} (attempt {attempt}): {e}")
            if attempt < RETRY_ATTEMPTS:
                delay = RETRY_DELAY * (2 ** (attempt - 1))
                logger.info(f"Retrying in {delay} seconds...")
                time.sleep(delay)
            else:
                logger.error(f"All retries failed for @{account}")
                return []
    return []


async def _fetch_timeline_async(account: str, max_posts: int = 10) -> List[Dict]:
    """Async implementation of fetch_timeline."""
    posts = []
    async with XStealthBrowser(cookie_file=COOKIES_PATH, headless=True) as browser:
        await browser.navigate_to_user(account)
        # Wait for tweets to load with timeout
        try:
            await browser.page.wait_for_selector("article[data-testid='tweet']", timeout=30000)
        except Exception:
            logger.warning(f"Timeline for @{account} did not load within timeout")
            return []
        # Scroll a bit to load more
        await browser.random_scroll(times=2)
        # Extract tweets with created_at
        tweets = await browser.page.evaluate('''
            (limit) => {
                const articles = document.querySelectorAll("article[data-testid='tweet']");
                const results = [];
                for (const article of articles) {
                    if (results.length >= limit) break;
                    const id = article.getAttribute('data-tweet-id') || '';
                    const text = article.querySelector('[data-testid="tweetText"]')?.innerText || '';
                    const user = article.querySelector('[data-testid="User-Name"]')?.innerText || '';
                    const timeElem = article.querySelector('time');
                    const created_at = timeElem ? timeElem.getAttribute('datetime') : '';
                    const linkElem = article.querySelector('a[href*="/status/"]');
                    const permalink = linkElem ? linkElem.getAttribute('href') : '';
                    if (id) {
                        results.push({ id, text, user, created_at, permalink });
                    }
                }
                return results;
            }
        ''', max_posts)
        for t in tweets:
            tweet_id = t.get('id', '')
            if not tweet_id:
                continue
            # Build full URL
            url = t.get('permalink', '')
            if url and not url.startswith('http'):
                url = f"https://x.com{url}"
            posts.append({
                "id": tweet_id,
                "text": t.get('text', ''),
                "url": url,
                "author": account,
                "created_at": t.get('created_at', datetime.utcnow().isoformat())
            })
        return posts


def enqueue_new_posts(posts: List[Dict]):
    seen = load_seen()
    queue = load_queue()
    new_count = 0
    for post in posts:
        post_id = post.get("id")
        if not post_id:
            continue
        if post_id in seen:
            continue
        seen[post_id] = True
        queue.append(post)
        new_count += 1
    if len(queue) > MAX_QUEUE_SIZE:
        queue = queue[-MAX_QUEUE_SIZE:]
    save_seen(seen)
    save_queue(queue)
    logger.info(f"Enqueued {new_count} new posts. Queue size: {len(queue)}")
    return new_count


def perform_action(agent, post: Dict) -> bool:
    """Perform like + quote/retweet with retry."""
    tweet_url = post.get("url")
    if not tweet_url:
        logger.error("No URL for post")
        return False

    # Like with retry
    like_success = False
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            agent.like_tweet(tweet_url=tweet_url)
            logger.info(f"Liked: {tweet_url}")
            like_success = True
            break
        except Exception as e:
            logger.error(f"Like failed (attempt {attempt}): {e}")
            if attempt < RETRY_ATTEMPTS:
                delay = RETRY_DELAY * (2 ** (attempt - 1))
                time.sleep(delay)
    if not like_success:
        logger.error(f"Like permanently failed for {tweet_url}, skipping.")
        return False

    # Delay after like
    time.sleep(random.uniform(ACTION_DELAY_MIN, ACTION_DELAY_MAX))

    # Quote with retry, fallback retweet
    quote_text = generate_quote(post.get("text", ""))
    if quote_text and len(quote_text) > 0:
        quote_success = False
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                agent.quote_tweet(tweet_url=tweet_url, quote_text=quote_text[:280])
                logger.info(f"Quoted: {tweet_url} with: {quote_text[:50]}...")
                quote_success = True
                break
            except Exception as e:
                logger.error(f"Quote failed (attempt {attempt}): {e}")
                if attempt < RETRY_ATTEMPTS:
                    delay = RETRY_DELAY * (2 ** (attempt - 1))
                    time.sleep(delay)
        if quote_success:
            return True
        else:
            logger.warning(f"Quote permanently failed for {tweet_url}, falling back to retweet")
            # Fallback to retweet with retry
            for attempt in range(1, RETRY_ATTEMPTS + 1):
                try:
                    agent.retweet_tweet(tweet_url=tweet_url)
                    logger.info(f"Retweeted: {tweet_url}")
                    return True
                except Exception as e:
                    logger.error(f"Retweet failed (attempt {attempt}): {e}")
                    if attempt < RETRY_ATTEMPTS:
                        delay = RETRY_DELAY * (2 ** (attempt - 1))
                        time.sleep(delay)
            return False
    else:
        # No quote, just retweet with retry
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                agent.retweet_tweet(tweet_url=tweet_url)
                logger.info(f"Retweeted: {tweet_url}")
                return True
            except Exception as e:
                logger.error(f"Retweet failed (attempt {attempt}): {e}")
                if attempt < RETRY_ATTEMPTS:
                    delay = RETRY_DELAY * (2 ** (attempt - 1))
                    time.sleep(delay)
        return False


def process_queue():
    queue = load_queue()
    if not queue:
        logger.info("Queue is empty.")
        return

    num_actions = random.randint(ACTIONS_PER_RUN_MIN, min(ACTIONS_PER_RUN_MAX, len(queue)))
    logger.info(f"Processing {num_actions} actions from queue (size {len(queue)})")

    if not is_wib_active():
        logger.info("Outside active hours. Will still enqueue but not process actions.")
        return

    # Perform actions
    with XStealthBrowser(cookie_file=COOKIES_PATH, headless=True) as browser:
        # We need to wrap in a context manager properly; we'll use run_sync to call async methods
        # But XStealthBrowser is async, so we use a sync wrapper
        def _process():
            browser.start()
            processed = 0
            for _ in range(num_actions):
                if not queue:
                    break
                post = queue.pop(0)
                success = perform_action(browser, post)
                if success:
                    processed += 1
                    # Notify (log only)
                    notify_telegram(post, action="liked+quoted/retweeted")
                else:
                    logger.warning(f"Action failed for {post.get('url')}, discarding.")
                time.sleep(random.uniform(ACTION_DELAY_MIN, ACTION_DELAY_MAX))
            save_queue(queue)
            logger.info(f"Processed {processed} actions.")
        run_sync(_process())


def notify_telegram(post: Dict, action: str):
    message = (
        f" **Tweet from @{post.get('author', 'unknown')}**\n"
        f"{post.get('text', '')[:200]}\n"
        f"[Link]({post.get('url', '#')})\n"
        f"Action: {action}"
    )
    logger.info(f"Notified: {message[:100]}...")


def run_digest():
    pending = load_pending()
    if not pending:
        logger.info("No pending notifications.")
        return
    lines = [" **Daily Digest (21.00 - 05.00 WIB)**"]
    for post in pending:
        lines.append(f"- @{post.get('author', 'unknown')}: {post.get('text', '')[:100]} [Link]({post.get('url', '#')})")
    digest = "\n".join(lines)
    logger.info(f"Digest: {digest[:200]}...")
    save_pending([])


def main():
    parser = argparse.ArgumentParser(description="Timeline Monitor")
    parser.add_argument("mode", choices=["normal", "digest"], default="normal", nargs="?")
    args = parser.parse_args()

    if args.mode == "digest":
        run_digest()
        return

    logger.info("Starting timeline monitor...")
    all_posts = []
    for account in TARGET_ACCOUNTS:
        posts = fetch_timeline(account, max_posts=10)
        all_posts.extend(posts)
        time.sleep(random.uniform(2, 5))

    # Filter recent (within 7 days) based on actual created_at
    recent_posts = [p for p in all_posts if is_within_7_days(p.get('created_at', ''))]
    logger.info(f"Found {len(recent_posts)} recent posts (within {MAX_AGE_DAYS} days)")

    enqueue_new_posts(recent_posts)
    process_queue()
    logger.info("Timeline monitor finished.")


if __name__ == "__main__":
    main()
