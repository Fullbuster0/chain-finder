#!/home/hermes/.hermes/hermes-agent/venv/bin/python3
"""
Chain Finder X — Twitter keyword monitoring for crypto validator mentions.
Uses Playwright browser-search (same engine as like_retweet_cron).
Searches keywords, notifies Telegram thread 9 with new mentions.
"""
import asyncio
import html
import json
import logging
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

BASE_DIR = Path("/home/hermes/chain-finder")
COOKIE_FILE = BASE_DIR / "x_cookies.txt"
# Active seen-state (IDs only). Legacy chain_finder_seen.json is merged once on load.
STATE_FILE = BASE_DIR / "chain_finder_x_state.json"
LEGACY_SEEN_FILE = BASE_DIR / "chain_finder_seen.json"
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

MAX_AGE_DAYS = 7
MAX_TWEETS_PER_KEYWORD = 10
MAX_POSTS = 5  # max notifications per run
MAX_SEEN = 2000  # hard cap after prune

# Telegram
TG_CHAT_ID = "-1003641668106"
TG_THREAD_ID = "9"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("chain_finder_x")


def load_tg_token():
    try:
        with open("/home/hermes/.hermes/bridge_config.json") as f:
            return json.load(f).get("token", "").strip()
    except Exception as e:
        logger.error(f"TG token: {e}")
        return ""


def load_cookies():
    cookies = []
    if not COOKIE_FILE.exists():
        return cookies
    with open(COOKIE_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            p = line.split('\t')
            if len(p) >= 7:
                cookies.append({
                    'name': p[5], 'value': p[6],
                    'domain': p[0], 'path': p[2] or '/',
                    'secure': p[3] == 'TRUE', 'httpOnly': False,
                })
    return cookies


def snowflake_to_dt(tid):
    try:
        timestamp_ms = (int(tid) >> 22) + 1288834974657
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    except (ValueError, OverflowError, TypeError):
        return None


def _prune_seen(seen_ids):
    """Keep only IDs younger than MAX_AGE_DAYS; cap at MAX_SEEN (newest first)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    kept = []
    for tid in seen_ids:
        dt = snowflake_to_dt(tid)
        if dt is None or dt >= cutoff:
            kept.append(str(tid))
    # Newest snowflakes are larger — keep the tail if still over cap
    if len(kept) > MAX_SEEN:
        kept = sorted(kept, key=lambda x: int(x) if str(x).isdigit() else 0)[-MAX_SEEN:]
    return kept


def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
        except Exception:
            data = {"seen": []}
    else:
        data = {"seen": []}

    seen = set(str(x) for x in data.get("seen", []))
    # One-shot absorb of legacy seen file so we don't re-notify old hits
    if LEGACY_SEEN_FILE.exists():
        try:
            with open(LEGACY_SEEN_FILE) as f:
                legacy = json.load(f)
            legacy_ids = legacy.get("tweets", legacy.get("seen", []))
            before = len(seen)
            for x in legacy_ids:
                if isinstance(x, dict):
                    tid = x.get("id")
                    if tid:
                        seen.add(str(tid))
                else:
                    seen.add(str(x))
            absorbed = len(seen) - before
            if absorbed:
                logger.info(f"merged {absorbed} legacy seen ids")
        except Exception as e:
            logger.warning(f"legacy seen merge fail: {e}")

    data["seen"] = _prune_seen(seen)
    return data


def save_state(state):
    state = dict(state)
    state["seen"] = _prune_seen(state.get("seen", []))
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def send_telegram(text):
    token = load_tg_token()
    if not token:
        logger.error("No TG token")
        return False
    import subprocess
    cmd = [
        "curl", "-s", "-X", "POST",
        f"https://api.telegram.org/bot{token}/sendMessage",
        "--data-urlencode", f"chat_id={TG_CHAT_ID}",
        "--data-urlencode", f"message_thread_id={TG_THREAD_ID}",
        "--data-urlencode", f"text={text}",
        "--data-urlencode", "parse_mode=HTML",
        "--data-urlencode", "disable_web_page_preview=true",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        logger.error(f"TG fail: {r.stderr[:200]}")
        return False
    return True


async def search_keyword(page, keyword):
    """Search keyword via browser, return list of tweet dicts."""
    # No since: filter — X search is incomplete with since:; age-filter in code instead
    q = f'"{keyword}" -filter:replies'
    url = 'https://x.com/search?q=' + urllib.parse.quote(q) + '&f=live'
    logger.info(f"  search: {keyword}")
    try:
        await page.goto(url, wait_until='domcontentloaded', timeout=60000)
    except Exception:
        await page.goto(url, timeout=60000)
    await asyncio.sleep(3)
    # scroll a bit
    for _ in range(3):
        try:
            await page.mouse.wheel(0, 1000)
            await asyncio.sleep(0.5)
        except Exception:
            break

    results = []
    n = await page.locator('article').count()
    logger.info(f"    articles={n}")
    for i in range(n):
        art = page.locator('article').nth(i)
        try:
            d = await art.evaluate('''(el) => {
                const text = el.innerText || '';
                const te = el.querySelector('[data-testid="tweetText"]');
                const body = te ? (te.innerText || '').slice(0, 300) : '';
                
                // author
                const ub = el.querySelector('[data-testid="User-Name"]');
                let author = null;
                if (ub) {
                    const hs = [...ub.querySelectorAll('a[href^="/"]')]
                        .map(a => (a.getAttribute('href') || '').split('?')[0].replace(/^\\//, ''))
                        .filter(h => h && !h.includes('/') && !h.startsWith('i/'));
                    author = hs.length ? hs[hs.length - 1] : null;
                }
                
                // status links
                const links = [...el.querySelectorAll('a[href*="/status/"]')]
                    .map(a => (a.getAttribute('href') || '').split('?')[0]);
                let permalink = null, tid = null;
                for (const h of links) {
                    const m = h.match(/^\\/([^\\/]+)\\/status\\/(\\d+)$/);
                    if (m) { permalink = h; tid = m[2]; break; }
                }
                
                const timeEl = el.querySelector('time');
                const dt = timeEl ? timeEl.getAttribute('datetime') : null;
                
                const head = text.split('\\n').slice(0, 4).join(' ');
                const isReplying = /\\b(Replying to|Membalas)\\b/i.test(head);
                
                return {author, body, permalink, tid, dt, isReplying};
            }''')
        except Exception:
            continue

        if d.get('isReplying'):
            continue
        if not d.get('tid') or not d.get('permalink'):
            continue
        if d.get('dt'):
            try:
                dt_parsed = datetime.fromisoformat(d['dt'].replace('Z', '+00:00'))
                if dt_parsed < datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS):
                    continue
            except Exception:
                pass

        results.append({
            'id': d['tid'],
            'username': d.get('author', '?'),
            'text': (d.get('body') or '')[:300],
            'url': f"https://x.com{d['permalink']}",
        })
        if len(results) >= MAX_TWEETS_PER_KEYWORD:
            break
    return results


async def main():
    logger.info("=== Chain Finder X start ===")
    state = load_state()
    seen = set(state.get("seen", []))
    logger.info(f"seen={len(seen)}")

    p = await async_playwright().start()
    browser = await p.chromium.launch(
        headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage',
              '--disable-blink-features=AutomationControlled'],
    )
    ctx = await browser.new_context(
        viewport={'width': 1280, 'height': 900},
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        locale='en-US',
    )
    page = await ctx.new_page()
    await Stealth().apply_stealth_async(page)

    cookies = load_cookies()
    if cookies:
        await ctx.add_cookies(cookies)

    # verify login
    await page.goto('https://x.com/home', wait_until='domcontentloaded', timeout=60000)
    await asyncio.sleep(2.5)
    if '/login' in page.url or '/i/flow/login' in page.url:
        logger.error(f"Login FAIL {page.url}")
        await browser.close(); await p.stop(); return
    logger.info("Login OK")

    all_new = []
    for kw in KEYWORDS:
        try:
            tweets = await search_keyword(page, kw)
        except Exception as e:
            logger.error(f"search '{kw}': {e}")
            continue

        for t in tweets:
            if t['id'] in seen:
                continue
            seen.add(t['id'])
            all_new.append(t)

    save_state({"seen": list(seen)})
    logger.info(f"new tweets: {len(all_new)}")

    if all_new:
        # deduplicate
        uniq = {}
        for t in all_new:
            if t['id'] not in uniq:
                uniq[t['id']] = t
        all_new = list(uniq.values())[:MAX_POSTS]

        lines = ["<b>🔍 New Validator Mentions</b>"]
        for t in all_new:
            text = html.escape(t['text'][:200])
            user = html.escape(t['username'] or '?')
            url = html.escape(t['url'], quote=True)
            lines.append(
                f"• <a href='{url}'>{user}</a>: {text}"
            )
        msg = "\n".join(lines)
        send_telegram(msg)
        logger.info(f"sent {len(all_new)} notifications")
    else:
        logger.info("no new tweets")

    await browser.close()
    await p.stop()
    logger.info("=== done ===")


if __name__ == "__main__":
    asyncio.run(main())
