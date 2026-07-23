#!/home/hermes/.hermes/hermes-agent/venv/bin/python3
"""
quote_cron.py — Engage original root posts from Cosmos target accounts.
Separate from like_retweet_cron.py — does NOT modify or share state with it.

Engagement priority per tweet:
  1. Always like
  2. Prefer LLM-generated quote (natural, contextual — reads tweet body)
  3. Fallback plain retweet if LLM fails / quote UI fails
  (Never posts template/canned quote text.)

Flow:
- Discover via profile + search (same dual-scan as like_retweet)
- Only original root tweets by target account (never replies / pure reposts)
- State in quote_queue.json (separate from timeline_queue.json)
- TG thread 10 notification
"""
import asyncio
import logging
import datetime
import random
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

COOKIE_FILE = "/home/hermes/chain-finder/x_cookies.txt"
ACCOUNTS = ['_atomone', 'Hippo_Protocol', 'ShentuChain', 'phoenix_dir', 'ZetaChain', '_gnoland']
MAX_DAYS = 7
STATE_FILE = "/home/hermes/chain-finder/quote_queue.json"
BRIDGE_CONFIG = "/home/hermes/.hermes/bridge_config.json"
TELEGRAM_CHAT_ID = "-1003641668106"
TELEGRAM_THREAD = "10"
MIN_ACTIONS = 1
MAX_ACTIONS = 2  # quotes are high-visibility, keep conservative

# LLM endpoint for quote generation (local 9router)
LLM_URL = "http://localhost:20128/v1/chat/completions"
LLM_MODEL = "Knight"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def is_active_hours():
    return True


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"processed": [], "skip_reply": []}
    with open(STATE_FILE) as f:
        data = json.load(f)
    if isinstance(data, list):
        state = {
            "processed": [t.get('id') for t in data if t.get('id')],
            "skip_reply": [],
        }
        save_state(state)
        return state
    if 'skip_reply' not in data:
        data['skip_reply'] = []
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=MAX_DAYS)
    data['processed'] = [
        tid for tid in data.get('processed', [])
        if snowflake_to_dt(tid) is None or snowflake_to_dt(tid) >= cutoff
    ]
    data['skip_reply'] = [
        tid for tid in data.get('skip_reply', [])
        if snowflake_to_dt(tid) is None or snowflake_to_dt(tid) >= cutoff
    ]
    return data


def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


def load_tg_token():
    try:
        with open(BRIDGE_CONFIG) as f:
            return json.load(f).get("token", "").strip()
    except Exception as e:
        logger.error(f"TG token load fail: {e}")
        return ""


def send_telegram(message):
    try:
        token = load_tg_token()
        if not token:
            logger.error("No TG token")
            return False
        cmd = [
            "curl", "-s", "-X", "POST",
            f"https://api.telegram.org/bot{token}/sendMessage",
            "-d", f"chat_id={TELEGRAM_CHAT_ID}",
            "-d", f"message_thread_id={TELEGRAM_THREAD}",
            "-d", f"text={message}",
            "-d", "parse_mode=Markdown",
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode == 0
    except Exception as e:
        logger.error(f"Telegram error: {e}")
        return False


def load_cookies():
    cookies = []
    if not os.path.exists(COOKIE_FILE):
        return cookies
    with open(COOKIE_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) >= 7:
                cookies.append({
                    'name': parts[5], 'value': parts[6],
                    'domain': parts[0], 'path': parts[2] or '/',
                    'secure': parts[3] == 'TRUE', 'httpOnly': False,
                })
    return cookies


def snowflake_to_dt(tid):
    try:
        tid_int = int(tid)
        timestamp_ms = (tid_int >> 22) + 1288834974657
        return datetime.datetime.fromtimestamp(timestamp_ms / 1000, tz=datetime.timezone.utc)
    except (ValueError, OverflowError):
        return None


def generate_quote_text(tweet_text, account):
    """Call local 9router LLM to generate a natural quote response."""
    prompt = (
        f"You are a crypto/Cosmos ecosystem enthusiast on X (Twitter). "
        f"Someone @{account} just posted this tweet:\n\n"
        f"\"{tweet_text[:500]}\"\n\n"
        f"Write a short quote tweet response (1-2 sentences, max 200 chars). "
        f"Be genuine, varied, and contextual to the tweet content. "
        f"Sound like a real person, not a bot. No generic filler. "
        f"Match the energy of the tweet. If it's an announcement, show excitement. "
        f"If it's technical, acknowledge the substance. "
        f"Use at most 1 emoji. No hashtags unless the tweet uses them. "
        f"Reply with ONLY the quote text, nothing else."
    )
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": "You generate short, natural X/Twitter quote tweet responses. Output only the quote text."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.9,
        "max_tokens": 120,
        "stream": False,
    }).encode()
    try:
        req = urllib.request.Request(
            LLM_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            text = result["choices"][0]["message"]["content"].strip()
            # Clean up: remove surrounding quotes if LLM added them
            if text.startswith('"') and text.endswith('"'):
                text = text[1:-1]
            if text.startswith("'") and text.endswith("'"):
                text = text[1:-1]
            # Truncate to safe length
            if len(text) > 250:
                text = text[:247] + "..."
            logger.info(f"  LLM quote: \"{text}\"")
            return text
    except Exception as e:
        logger.error(f"  LLM quote generation failed: {e}")
        return None


# JS snippet: parse one article (same as like_retweet_cron)
_ARTICLE_JS = '''(el, args) => {
  const acc = (args.acc || '').toLowerCase();
  const text = el.innerText || '';
  const head = text.split('\\n').slice(0, 5).join(' ');

  let author = null;
  const userBlock = el.querySelector('[data-testid="User-Name"]');
  if (userBlock) {
    const hs = [...userBlock.querySelectorAll('a[href^="/"]')]
      .map(a => (a.getAttribute('href') || '').split('?')[0].replace(/^\\//, ''))
      .filter(h => h && !h.includes('/') && !h.startsWith('i/') && !h.startsWith('search'));
    author = hs.length ? hs[hs.length - 1] : null;
  }
  const authorL = (author || '').toLowerCase();

  const links = [...el.querySelectorAll('a[href*="/status/"]')]
    .map(a => (a.getAttribute('href') || '').split('?')[0]);
  let permalink = null, tid = null;
  for (const href of links) {
    const m = href.match(/^\\/([^\\/]+)\\/status\\/(\\d+)$/);
    if (!m) continue;
    if (m[1].toLowerCase() === acc) {
      const anchor = el.querySelector(`a[href="${href}"]`);
      if (anchor) {
        const txt = anchor.innerText || '';
        if (/[\\u00b7]|AM|PM|UTC/.test(txt)) {
          permalink = href; tid = m[2]; break;
        }
      }
      if (!permalink) {
        permalink = href; tid = m[2];
      }
    }
  }
  if (!permalink) {
    for (const href of links) {
      const m = href.match(/^\\/([^\\/]+)\\/status\\/(\\d+)$/);
      if (m && m[1].toLowerCase() === authorL) {
        permalink = href; tid = m[2]; break;
      }
    }
  }

  const socialEl = el.querySelector('[data-testid="socialContext"]');
  const social = socialEl ? (socialEl.innerText || '').trim() : '';
  const timeEl = el.querySelector('time');
  const dt = timeEl ? timeEl.getAttribute('datetime') : null;

  const isReplying =
    /\\b(Replying to|Membalas)\\b/i.test(head) ||
    /\\b(Replying to|Membalas)\\b/i.test(text.slice(0, 120));

  const isRepost = /memposting ulang|reposted|retweeted/i.test(social);

  const likeBtn = el.querySelector('[data-testid="like"], [data-testid="unlike"]');
  const rtBtn = el.querySelector('[data-testid="retweet"], [data-testid="unretweet"]');
  const likeTid = likeBtn ? likeBtn.getAttribute('data-testid') : null;
  const rtTid = rtBtn ? rtBtn.getAttribute('data-testid') : null;

  let body = '';
  const te = el.querySelector('[data-testid="tweetText"]');
  if (te) body = (te.innerText || '').slice(0, 200);

  return {
    author,
    isTargetAuthor: !!(authorL && authorL === acc),
    permalink, tid, social, dt,
    isReplying, isRepost, likeTid, rtTid, body, head: head.slice(0, 160),
  };
}'''


async def parse_article(locator, acc):
    return await locator.evaluate(_ARTICLE_JS, {"acc": acc})


async def _scan_page(page, acc, cutoff, processed_ids, seen, skip_reply=None):
    if skip_reply is None:
        skip_reply = set()
    found = []
    n = await page.locator('article').count()
    logger.info(f"    scanning {n} articles for @{acc}")
    for i in range(n):
        art = page.locator('article').nth(i)
        d = {}
        try:
            d = await parse_article(art, acc)
        except Exception:
            pass
        if not d.get('isTargetAuthor'):
            try:
                user_name = art.locator('[data-testid="User-Name"]')
                if await user_name.count():
                    author_links = await user_name.locator('a[href^="/"]').all()
                    if author_links:
                        last_href = await author_links[-1].get_attribute('href')
                        if last_href:
                            author_handle = last_href.lstrip('/').split('?')[0]
                            if author_handle.lower() == acc.lower():
                                status_links = await art.locator('a[href*="/status/"]').all()
                                for link in status_links:
                                    href = await link.get_attribute('href')
                                    if href and f'/{acc}/status/' in href:
                                        m = re.search(r'/status/(\d+)', href)
                                        if m:
                                            d['tid'] = m.group(1)
                                            d['permalink'] = href.split('?')[0]
                                            d['isTargetAuthor'] = True
                                            time_el = art.locator('time')
                                            if await time_el.count():
                                                d['dt'] = await time_el.first.get_attribute('datetime')
                                            like_btn = art.locator('[data-testid="like"], [data-testid="unlike"]')
                                            if await like_btn.count():
                                                d['likeTid'] = await like_btn.first.get_attribute('data-testid')
                                            rt_btn = art.locator('[data-testid="retweet"], [data-testid="unretweet"]')
                                            if await rt_btn.count():
                                                d['rtTid'] = await rt_btn.first.get_attribute('data-testid')
                                            tweet_text = art.locator('[data-testid="tweetText"]')
                                            if await tweet_text.count():
                                                d['body'] = (await tweet_text.first.inner_text())[:200]
                                            social_ctx = art.locator('[data-testid="socialContext"]')
                                            if await social_ctx.count():
                                                d['social'] = await social_ctx.first.inner_text()
                                            break
            except Exception as e:
                logger.debug(f"Manual extraction failed: {e}")
        if not d.get('isTargetAuthor'):
            continue
        if not d.get('permalink') or not d.get('tid'):
            continue
        tid = d['tid']
        if tid in seen or tid in processed_ids or tid in skip_reply:
            continue
        seen.add(tid)
        if d.get('isReplying'):
            logger.info(f"  skip reply {tid}")
            continue
        if d.get('isRepost'):
            continue
        if not d['permalink'].lower().startswith(f'/{acc.lower()}/status/'):
            continue
        if d['dt']:
            try:
                dt = datetime.datetime.fromisoformat(d['dt'].replace('Z', '+00:00'))
                if dt < cutoff:
                    continue
            except Exception:
                pass
        found.append({
            'id': tid,
            'permalink': d['permalink'],
            'account': acc,
            'text': (d.get('body') or '')[:200],
        })
    return found


async def discover_account(page, acc, cutoff, processed_ids):
    found, seen = [], set()
    state = load_state()
    skip_reply = set(state.get('skip_reply', []))

    # METHOD 1: Profile page
    profile_url = f'https://x.com/{acc}'
    logger.info(f"  profile: @{acc}")
    await page.goto(profile_url, wait_until='domcontentloaded', timeout=60000)
    try:
        await page.wait_for_selector('article', timeout=10000)
    except Exception:
        pass
    await asyncio.sleep(1.5)

    no_scroll_found = await _scan_page(page, acc, cutoff, processed_ids, seen, skip_reply)
    logger.info(f"  profile (no scroll) @{acc}: {len(no_scroll_found)} eligible")
    found.extend(no_scroll_found)
    seen.update({t['id'] for t in no_scroll_found})

    for _ in range(15):
        await page.mouse.wheel(0, 1200)
        await asyncio.sleep(0.7)
    scroll_found = await _scan_page(page, acc, cutoff, processed_ids, seen, skip_reply)
    logger.info(f"  profile (after scroll) @{acc}: {len(scroll_found)} eligible")
    existing_ids = {t['id'] for t in found}
    for t in scroll_found:
        if t['id'] not in existing_ids:
            found.append(t)

    # METHOD 2: Search fallback
    search_url = f'https://x.com/search?q=from%3A{acc}&src=typed_query&f=live'
    logger.info(f"  search: {acc}")
    await page.goto(search_url, wait_until='domcontentloaded', timeout=60000)
    try:
        await page.wait_for_selector('article', timeout=10000)
    except Exception:
        pass
    await asyncio.sleep(1.5)

    search_no_scroll = await _scan_page(page, acc, cutoff, processed_ids, seen, skip_reply)
    logger.info(f"  search (no scroll) @{acc}: {len(search_no_scroll)} eligible")
    existing_ids = {t['id'] for t in found}
    for t in search_no_scroll:
        if t['id'] not in existing_ids:
            found.append(t)
    seen.update({t['id'] for t in search_no_scroll})

    for _ in range(10):
        await page.mouse.wheel(0, 1000)
        await asyncio.sleep(0.5)
    search_scroll = await _scan_page(page, acc, cutoff, processed_ids, seen, skip_reply)
    logger.info(f"  search (after scroll) @{acc}: {len(search_scroll)} eligible")
    existing_ids = {t['id'] for t in found}
    for t in search_scroll:
        if t['id'] not in existing_ids:
            found.append(t)

    logger.info(f"  @{acc}: total eligible={len(found)}")
    return found


async def click_el(locator, timeout=4000):
    try:
        await locator.click(timeout=timeout)
        return True
    except Exception:
        try:
            await locator.click(force=True, timeout=timeout)
            return True
        except Exception:
            return False


async def do_like(target, first, tweet):
    """Always like the target article. Returns bool."""
    if first.get('likeTid') == 'unlike':
        tweet['liked'] = True
        logger.info("    already liked")
        return True
    like = target.locator('[data-testid="like"]')
    unlike = target.locator('[data-testid="unlike"]')
    if await like.count() > 0:
        await click_el(like.first)
        await asyncio.sleep(random.uniform(1.0, 1.8))
        tweet['liked'] = await unlike.count() > 0
    else:
        tweet['liked'] = await unlike.count() > 0
    logger.info(f"    like={'OK' if tweet['liked'] else 'FAIL'}")
    return tweet['liked']


async def do_retweet(page, target, first, tweet):
    """Plain repost (fallback when LLM quote unavailable). Returns bool."""
    if first.get('rtTid') == 'unretweet':
        tweet['retweeted'] = True
        logger.info("    already retweeted")
        return True
    rt = target.locator('[data-testid="retweet"]')
    unrt = target.locator('[data-testid="unretweet"]')
    if await rt.count() == 0:
        tweet['retweeted'] = await unrt.count() > 0
        logger.info(f"    rt={'OK' if tweet['retweeted'] else 'FAIL'} (no rt btn)")
        return tweet['retweeted']
    await click_el(rt.first)
    await asyncio.sleep(random.uniform(1.0, 2.0))
    conf = page.locator('[data-testid="retweetConfirm"]')
    if await conf.count() == 0:
        conf = page.locator('div[role="menuitem"]').filter(
            has_text=re.compile(r'Repost|Posting ulang|Retweet', re.I)
        )
    if await conf.count() > 0:
        await click_el(conf.first)
        await asyncio.sleep(random.uniform(1.2, 2.5))
    tweet['retweeted'] = await unrt.count() > 0
    logger.info(f"    rt={'OK' if tweet['retweeted'] else 'FAIL'}")
    return tweet['retweeted']


async def do_quote(page, target, quote_text, tweet):
    """Open RT menu → Quote/Kutip → type LLM text → submit. Returns bool."""
    rt_btn = target.locator('[data-testid="retweet"], [data-testid="unretweet"]')
    if await rt_btn.count() == 0:
        logger.error("  no retweet button found")
        return False

    await click_el(rt_btn.first)
    await asyncio.sleep(random.uniform(1.0, 2.0))

    quote_option = None
    for selector in [
        'div[role="menuitem"]:has-text("Quote")',
        'div[role="menuitem"]:has-text("Kutip")',
        'span:has-text("Quote")',
        'span:has-text("Kutip")',
    ]:
        loc = page.locator(selector)
        if await loc.count() > 0:
            quote_option = loc.first
            logger.info(f"  found quote option: {selector}")
            break

    if not quote_option:
        menuitems = page.locator('div[role="menuitem"]')
        mi_count = await menuitems.count()
        for i in range(mi_count):
            mi_text = await menuitems.nth(i).inner_text()
            if 'quote' in mi_text.lower() or 'kutip' in mi_text.lower():
                quote_option = menuitems.nth(i)
                logger.info(f"  found quote option via text scan: \"{mi_text}\"")
                break

    if not quote_option:
        logger.error("  Quote/Kutip menu item not found")
        await page.keyboard.press('Escape')
        await asyncio.sleep(0.5)
        return False

    await click_el(quote_option)
    await asyncio.sleep(random.uniform(1.5, 2.5))

    compose = None
    dialog = page.locator('[role="dialog"]')
    if await dialog.count() > 0:
        textarea = dialog.locator('[data-testid="tweetTextarea_0"]')
        if await textarea.count() > 0:
            compose = textarea.first
            logger.info("  compose found in dialog")

    if not compose:
        compose = page.locator('[data-testid="tweetTextarea_0"]').first
        if await compose.count() == 0:
            compose = page.locator('div[role="textbox"]').first

    if not compose or await compose.count() == 0:
        logger.error("  compose textarea not found")
        await page.keyboard.press('Escape')
        await asyncio.sleep(0.5)
        return False

    try:
        await compose.click()
        await asyncio.sleep(0.3)
        await compose.type(quote_text, delay=random.uniform(20, 50))
    except Exception:
        try:
            await compose.fill(quote_text)
        except Exception as e:
            logger.error(f"  failed to type quote text: {e}")
            await page.keyboard.press('Escape')
            return False

    logger.info(f"  typed quote text ({len(quote_text)} chars)")
    await asyncio.sleep(random.uniform(0.8, 1.5))

    submit = None
    if await dialog.count() > 0:
        for sel in [
            '[data-testid="tweetButton"]',
            'button:has-text("Post")',
            'button:has-text("Posting")',
            'button:has-text("Tweet")',
            'button:has-text("Kutip")',
        ]:
            btn = dialog.locator(sel)
            if await btn.count() > 0:
                is_disabled = await btn.first.get_attribute('disabled')
                aria_disabled = await btn.first.get_attribute('aria-disabled')
                if is_disabled is None and aria_disabled != 'true':
                    submit = btn.first
                    logger.info(f"  submit found: {sel}")
                    break

    if not submit:
        for sel in ['[data-testid="tweetButton"]', 'button:has-text("Post")', 'button:has-text("Posting")']:
            btn = page.locator(sel)
            if await btn.count() > 0:
                submit = btn.first
                logger.info(f"  submit fallback: {sel}")
                break

    if not submit:
        logger.error("  submit button not found")
        await page.keyboard.press('Escape')
        return False

    await click_el(submit)
    logger.info("  clicked submit")
    await asyncio.sleep(random.uniform(3.0, 5.0))

    dialog_still = page.locator('[role="dialog"]')
    if await dialog_still.count() == 0:
        logger.info("  dialog closed — quote likely posted")
        tweet['quote_text'] = quote_text
        return True

    toast = page.locator('[role="status"]')
    if await toast.count() > 0:
        toast_text = await toast.first.inner_text()
        logger.warning(f"  toast after submit: \"{toast_text}\"")
    try:
        remaining = await compose.inner_text()
        if not remaining.strip():
            tweet['quote_text'] = quote_text
            logger.info("  textarea empty after submit — quote posted")
            return True
        logger.warning("  textarea still has text — quote may have failed")
    except Exception:
        logger.warning("  could not verify quote status")
    await page.keyboard.press('Escape')
    await asyncio.sleep(0.5)
    return False


async def process_quote(page, tweet):
    """
    Priority engagement:
      1. Always like
      2. Prefer LLM quote (natural, contextual)
      3. Fallback plain retweet if LLM fails / quote UI fails
    Never posts template/canned quote text.
    """
    tid = str(tweet['id'])
    acc = tweet.get('account', '')
    tweet['liked'] = False
    tweet['quoted'] = False
    tweet['retweeted'] = False
    if not acc:
        return tweet

    url = f"https://x.com/{acc}/status/{tid}"
    tweet['permalink'] = f"/{acc}/status/{tid}"
    logger.info(f"  process: {url}")
    await page.goto(url, wait_until='domcontentloaded', timeout=60000)
    try:
        await page.wait_for_selector('article', timeout=15000)
    except Exception:
        pass
    await asyncio.sleep(random.uniform(1.0, 1.5))

    arts = page.locator('article')
    n = await arts.count()
    if n == 0:
        logger.error("  no articles")
        return tweet

    first = await parse_article(arts.nth(0), acc)
    first_author = (first.get('author') or '').lower()
    first_tid = str(first.get('tid') or '')
    if first_author != acc.lower() or first_tid != tid:
        logger.error(
            f"  ABORT reply-thread or wrong root: "
            f"article[0]=@{first.get('author')}/{first_tid} expected=@{acc}/{tid}"
        )
        tweet['skipped'] = 'reply_or_wrong_root'
        return tweet

    target = arts.nth(0)
    logger.info(f"  target article[0] @{first.get('author')} ok")

    # 1) Always like
    await do_like(target, first, tweet)

    # 2) Prefer LLM quote
    tweet_text = tweet.get('text', '')
    if not tweet_text or len(tweet_text) < 10:
        try:
            te = target.locator('[data-testid="tweetText"]')
            if await te.count():
                tweet_text = (await te.first.inner_text())[:500]
        except Exception:
            pass

    quote_text = None
    if tweet_text:
        quote_text = await asyncio.to_thread(generate_quote_text, tweet_text, acc)

    if quote_text:
        logger.info(f"  quote text: \"{quote_text}\"")
        tweet['quoted'] = await do_quote(page, target, quote_text, tweet)
        if tweet['quoted']:
            await asyncio.sleep(random.uniform(1.5, 3.0))
            return tweet
        logger.warning("  quote UI failed — falling back to plain retweet")
    else:
        logger.warning("  LLM failed — falling back to plain retweet (no template)")

    # 3) Fallback: plain retweet (re-parse state after failed quote attempt)
    first = await parse_article(target, acc)
    await do_retweet(page, target, first, tweet)
    await asyncio.sleep(random.uniform(1.5, 3.0))
    return tweet


async def main():
    if not is_active_hours():
        logger.info("Outside active hours. Skip.")
        return

    logger.info("=== quote_cron start ===")
    state = load_state()
    processed_ids = set(state.get("processed", []))
    logger.info(f"processed_state={len(processed_ids)}")

    p = await async_playwright().start()
    browser = await p.chromium.launch(
        headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage',
              '--disable-blink-features=AutomationControlled'],
    )
    ctx = await browser.new_context(
        viewport={'width': 1280, 'height': 900},
        user_agent=('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/125.0.0.0 Safari/537.36'),
        locale='en-US',
    )
    page = await ctx.new_page()
    await Stealth().apply_stealth_async(page)

    cookies = load_cookies()
    if not cookies:
        logger.error("No cookies")
        await browser.close(); await p.stop(); return
    await ctx.add_cookies(cookies)
    logger.info(f"cookies={len(cookies)}")

    await page.goto('https://x.com/home', wait_until='domcontentloaded', timeout=60000)
    await asyncio.sleep(3)

    # Accept cookies if popup appears
    try:
        accept_btn = page.locator('button:has-text("Accept all cookies")')
        if await accept_btn.count():
            await accept_btn.first.click()
            logger.info("Accepted cookies")
            await asyncio.sleep(1)
    except Exception:
        pass

    if '/login' in page.url or '/i/flow/login' in page.url:
        logger.error(f"Login FAIL url={page.url}")
        await browser.close(); await p.stop(); return

    art_count = await page.locator('article').count()
    if art_count == 0:
        body_preview = (await page.inner_text('body'))[:300]
        if 'cookie' in body_preview.lower() or 'kuki' in body_preview.lower():
            logger.warning("Cookie wall / stale cookies detected on home page")
        else:
            logger.warning("No articles on home — possible login issue")
    logger.info(f"Login OK (articles={art_count})")

    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now - datetime.timedelta(days=MAX_DAYS)
    logger.info(f"cutoff={cutoff.isoformat()}")

    pending = []
    for acc in ACCOUNTS:
        try:
            pending.extend(await discover_account(page, acc, cutoff, processed_ids))
        except Exception as e:
            logger.error(f"discover @{acc}: {e}")

    uniq = {t['id']: t for t in pending}
    pending = list(uniq.values())
    logger.info(f"total eligible={len(pending)}")
    if not pending:
        logger.info("no eligible tweets, exiting")
        await browser.close(); await p.stop(); return

    # Shuffle-and-pick pattern (pre-validate, don't waste slots)
    random.shuffle(pending)
    n = random.randint(MIN_ACTIONS, MAX_ACTIONS)
    logger.info(f"target={n} quotes from {len(pending)} eligible")

    done = []
    for tweet in pending:
        if len(done) >= n:
            break
        try:
            await process_quote(page, tweet)
            if tweet.get('skipped') == 'reply_or_wrong_root':
                skip_reply_set = set(state.get('skip_reply', []))
                skip_reply_set.add(tweet['id'])
                state['skip_reply'] = list(skip_reply_set)
                save_state(state)
                logger.info(f"  marked reply skipped {tweet['id']}")
                continue
            engaged = tweet.get('quoted') or tweet.get('retweeted')
            if engaged:
                processed_ids.add(tweet['id'])
                state['processed'] = list(processed_ids)
                save_state(state)
                done.append(tweet)
            else:
                # Nothing engaged (like may have worked but no quote/RT) — allow retry
                logger.warning(f"  no quote/RT for {tweet['id']}, not marking processed")
        except Exception as e:
            logger.error(f"process {tweet['id']}: {e}")

    if done:
        lines = [f"*Engage batch* · {len(done)} tweet"]
        for t in done:
            flags = []
            if t.get('liked'):
                flags.append('❤️')
            if t.get('quoted'):
                flags.append('💬')
            if t.get('retweeted'):
                flags.append('🔁')
            qt = t.get('quote_text', '')
            extra = f" \"{qt[:80]}\"" if qt else ""
            lines.append(
                f"- @{t.get('account','?')} "
                f"[{t['id']}](https://x.com/{t.get('account')}/status/{t['id']}) "
                f"{''.join(flags)}{extra}"
            )
        send_telegram('\n'.join(lines))

    await browser.close(); await p.stop()
    logger.info("=== done ===")


if __name__ == '__main__':
    asyncio.run(main())
