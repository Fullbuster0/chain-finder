#!/home/hermes/.hermes/hermes-agent/venv/bin/python3
"""
like_retweet_cron.py — Like+RT original root posts from Cosmos target accounts.

Rules:
- Discover via search: from:user since:YYYY-MM-DD -filter:replies
- Only original root tweets by target account (never replies / pure reposts)
- Process: engage ONLY the article authored by target with matching status id
- Abort if status page is a reply thread (parent tweet sits above target)
- Random 1-4 per run; state in timeline_queue.json; TG thread 10
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
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

COOKIE_FILE = "/home/hermes/chain-finder/x_cookies.txt"
ACCOUNTS = ['_atomone', 'Hippo_Protocol', 'ShentuChain', 'phoenix_dir', 'ZetaChain', '_gnoland']
MAX_DAYS = 7
STATE_FILE = "/home/hermes/chain-finder/timeline_queue.json"
BRIDGE_CONFIG = "/home/hermes/.hermes/bridge_config.json"
TELEGRAM_CHAT_ID = "-1003641668106"
TELEGRAM_THREAD = "10"
MIN_ACTIONS = 1
MAX_ACTIONS = 4

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def is_active_hours():
    return True  # ponytail: gate open


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
    # backfill skip_reply if missing
    if 'skip_reply' not in data:
        data['skip_reply'] = []
    # auto-clean entries older than MAX_DAYS so state never bloat
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
    """Extract datetime from Twitter snowflake ID for state cleanup."""
    try:
        tid_int = int(tid)
        timestamp_ms = (tid_int >> 22) + 1288834974657
        return datetime.datetime.fromtimestamp(timestamp_ms / 1000, tz=datetime.timezone.utc)
    except (ValueError, OverflowError):
        return None


# JS snippet: parse one article (single arg bag)
_ARTICLE_JS = '''(el, args) => {
  const acc = (args.acc || '').toLowerCase();
  const text = el.innerText || '';
  const head = text.split('\\n').slice(0, 5).join(' ');

  // screen name from User-Name — last single-segment handle is @user
  let author = null;
  const userBlock = el.querySelector('[data-testid="User-Name"]');
  if (userBlock) {
    const hs = [...userBlock.querySelectorAll('a[href^="/"]')]
      .map(a => (a.getAttribute('href') || '').split('?')[0].replace(/^\\//, ''))
      .filter(h => h && !h.includes('/') && !h.startsWith('i/') && !h.startsWith('search'));
    author = hs.length ? hs[hs.length - 1] : null;
  }
  const authorL = (author || '').toLowerCase();

  // status links (no analytics/photo suffixes for identity)
  const links = [...el.querySelectorAll('a[href*="/status/"]')]
    .map(a => (a.getAttribute('href') || '').split('?')[0]);
  let permalink = null, tid = null;
  // Pick the link that is likely the tweet's own permalink:
  // - prefer link that has a timestamp text (contains · or AM/PM)
  // - or prefer link whose path matches /{acc}/status/{id} where acc matches target
  for (const href of links) {
    const m = href.match(/^\\/([^\\/]+)\\/status\\/(\\d+)$/);
    if (!m) continue;
    // If this link's author is the target account, consider it
    if (m[1].toLowerCase() === acc) {
      // Check if this link's text contains a timestamp marker
      const anchor = el.querySelector(`a[href="${href}"]`);
      if (anchor) {
        const txt = anchor.innerText || '';
        // If it has · or AM/PM/UTC, it's likely the main tweet timestamp
        if (/[\u00b7]|AM|PM|UTC/.test(txt)) {
          permalink = href; tid = m[2]; break;
        }
      }
      // If no timestamp marker found, but it's the only candidate, use it
      if (!permalink) {
        permalink = href; tid = m[2];
      }
    }
  }
  // If still no match, fallback to the first link that matches authorL
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

  // reply signals (ID + EN)
  const isReplying =
    /\\b(Replying to|Membalas)\\b/i.test(head) ||
    /\\b(Replying to|Membalas)\\b/i.test(text.slice(0, 120)) ||
    !!el.querySelector('[data-testid="tweet"] a[href*="/status/"]') === false && false;

  // pure repost of others
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
    """Scan current page for target account tweets. Returns list of eligible."""
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
        # Jika JS gagal mendeteksi target, coba ekstrak manual
        if not d.get('isTargetAuthor'):
            try:
                # Ambil author dari User-Name
                user_name = art.locator('[data-testid="User-Name"]')
                if await user_name.count():
                    author_links = await user_name.locator('a[href^="/"]').all()
                    if author_links:
                        # Ambil link terakhir (handle)
                        last_href = await author_links[-1].get_attribute('href')
                        if last_href:
                            author_handle = last_href.lstrip('/').split('?')[0]
                            if author_handle.lower() == acc.lower():
                                # Cari status link
                                status_links = await art.locator('a[href*="/status/"]').all()
                                for link in status_links:
                                    href = await link.get_attribute('href')
                                    if href and f'/{acc}/status/' in href:
                                        m = re.search(r'/status/(\d+)', href)
                                        if m:
                                            d['tid'] = m.group(1)
                                            d['permalink'] = href.split('?')[0]
                                            d['isTargetAuthor'] = True
                                            # Ambil time
                                            time_el = art.locator('time')
                                            if await time_el.count():
                                                d['dt'] = await time_el.first.get_attribute('datetime')
                                            # like/rt
                                            like_btn = art.locator('[data-testid="like"], [data-testid="unlike"]')
                                            if await like_btn.count():
                                                d['likeTid'] = await like_btn.first.get_attribute('data-testid')
                                            rt_btn = art.locator('[data-testid="retweet"], [data-testid="unretweet"]')
                                            if await rt_btn.count():
                                                d['rtTid'] = await rt_btn.first.get_attribute('data-testid')
                                            # body
                                            tweet_text = art.locator('[data-testid="tweetText"]')
                                            if await tweet_text.count():
                                                d['body'] = (await tweet_text.first.inner_text())[:200]
                                            # social context
                                            social_ctx = art.locator('[data-testid="socialContext"]')
                                            if await social_ctx.count():
                                                d['social'] = await social_ctx.first.inner_text()
                                            break
            except Exception as e:
                logger.debug(f"Manual extraction failed: {e}")
        if not d.get('isTargetAuthor'):
            logger.debug(f"    art {i}: skip not target author (author={d.get('author')})")
            continue
        if not d.get('permalink') or not d.get('tid'):
            logger.debug(f"    art {i}: skip no permalink/tid")
            continue
        tid = d['tid']
        if tid in seen or tid in processed_ids or tid in skip_reply:
            logger.debug(f"    art {i}: skip known {tid}")
            continue
        seen.add(tid)
        if d.get('isReplying'):
            logger.info(f"  skip reply {tid}")
            continue
        if d.get('isRepost'):
            logger.debug(f"    art {i}: skip repost {tid}")
            continue
        if not d['permalink'].lower().startswith(f'/{acc.lower()}/status/'):
            logger.debug(f"    art {i}: skip permalink mismatch {d.get('permalink')}")
            continue
        if d['dt']:
            try:
                dt = datetime.datetime.fromisoformat(d['dt'].replace('Z', '+00:00'))
                if dt < cutoff:
                    logger.debug(f"    art {i}: skip old {tid} ({d['dt']})")
                    continue
            except Exception:
                pass
        found.append({
            'id': tid,
            'permalink': d['permalink'],
            'account': acc,
            'text': (d.get('body') or '')[:200],
            'alreadyLiked': d.get('likeTid') == 'unlike',
            'alreadyRetweeted': d.get('rtTid') == 'unretweet',
        })
    return found


async def discover_account(page, acc, cutoff, processed_ids):
    """Discover eligible tweets via profile page first, then search fallback.
    Profile page is more reliable than X search which returns inconsistent results."""
    found, seen = [], set()
    # load skip_reply list
    state = load_state()
    skip_reply = set(state.get('skip_reply', []))

    # METHOD 1: Profile page (most reliable for account's own tweets)
    profile_url = f'https://x.com/{acc}'
    logger.info(f"  profile: @{acc}")
    await page.goto(profile_url, wait_until='domcontentloaded', timeout=60000)
    try:
        await page.wait_for_selector('article', timeout=10000)
    except Exception:
        pass
    await asyncio.sleep(1.5)
    
    # Scan before scrolling — catches newest tweets at top
    no_scroll_found = await _scan_page(page, acc, cutoff, processed_ids, seen, skip_reply)
    logger.info(f"  profile (no scroll) @{acc}: {len(no_scroll_found)} eligible")
    found.extend(no_scroll_found)
    seen.update({t['id'] for t in no_scroll_found})
    
    # scroll aggressively to load more tweets (older ones)
    for _ in range(15):
        await page.mouse.wheel(0, 1200)
        await asyncio.sleep(0.7)
    scroll_found = await _scan_page(page, acc, cutoff, processed_ids, seen, skip_reply)
    logger.info(f"  profile (after scroll) @{acc}: {len(scroll_found)} eligible")
    # deduplicate
    existing_ids = {t['id'] for t in found}
    for t in scroll_found:
        if t['id'] not in existing_ids:
            found.append(t)

    # METHOD 2: Search fallback (catches tweets not visible on profile page)
    # NOTE: no since, no -filter:replies — X search filters are too aggressive.
    # We handle reply filtering in _scan_page already.
    search_url = f'https://x.com/search?q=from%3A{acc}&src=typed_query&f=live'
    logger.info(f"  search: {acc}")
    await page.goto(search_url, wait_until='domcontentloaded', timeout=60000)
    try:
        await page.wait_for_selector('article', timeout=10000)
    except Exception:
        pass
    await asyncio.sleep(1.5)
    
    # Scan before scrolling — catches newest tweets at top
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


async def process_tweet(page, tweet):
    """
    Hard rules:
    1. Canonical URL only: /{acc}/status/{tid}
    2. After load, article[0] MUST be authored by acc with status tid
       → if parent sits above (reply thread), ABORT with zero clicks
    3. All like/RT clicks scoped to that single article locator
    """
    tid = str(tweet['id'])
    acc = tweet.get('account', '')
    if not acc:
        tweet['liked'] = tweet['retweeted'] = False
        return tweet

    url = f"https://x.com/{acc}/status/{tid}"
    tweet['permalink'] = f"/{acc}/status/{tid}"
    logger.info(f"  process: {url}")
    await page.goto(url, wait_until='domcontentloaded', timeout=60000)
    # wait for article to render (X can be slow to hydrate)
    try:
        await page.wait_for_selector('article', timeout=15000)
    except Exception:
        pass
    await asyncio.sleep(random.uniform(1.0, 1.5))

    arts = page.locator('article')
    n = await arts.count()
    if n == 0:
        logger.error("  no articles")
        tweet['liked'] = tweet['retweeted'] = False
        return tweet

    # CRITICAL: on a root tweet page, the main tweet is article[0].
    # On a reply page, article[0] is the PARENT (other user). Never touch that.
    first = await parse_article(arts.nth(0), acc)
    first_author = (first.get('author') or '').lower()
    first_tid = str(first.get('tid') or '')
    if first_author != acc.lower() or first_tid != tid:
        logger.error(
            f"  ABORT reply-thread or wrong root: "
            f"article[0]=@{first.get('author')}/{first_tid} expected=@{acc}/{tid}"
        )
        tweet['liked'] = tweet['retweeted'] = False
        tweet['skipped'] = 'reply_or_wrong_root'
        return tweet

    target = arts.nth(0)
    logger.info(f"  target article[0] @{first.get('author')} ok")

    # LIKE
    if tweet.get('alreadyLiked') or first.get('likeTid') == 'unlike':
        tweet['liked'] = True
        logger.info("    already liked")
    else:
        like = target.locator('[data-testid="like"]')
        unlike = target.locator('[data-testid="unlike"]')
        if await like.count() > 0:
            await click_el(like.first)
            await asyncio.sleep(random.uniform(1.2, 2.2))
            tweet['liked'] = await unlike.count() > 0
        else:
            tweet['liked'] = await unlike.count() > 0
        logger.info(f"    like={'OK' if tweet['liked'] else 'FAIL'}")

    # RETWEET
    if tweet.get('alreadyRetweeted') or first.get('rtTid') == 'unretweet':
        tweet['retweeted'] = True
        logger.info("    already retweeted")
    else:
        rt = target.locator('[data-testid="retweet"]')
        unrt = target.locator('[data-testid="unretweet"]')
        if await rt.count() > 0:
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
        else:
            tweet['retweeted'] = await unrt.count() > 0
        logger.info(f"    rt={'OK' if tweet['retweeted'] else 'FAIL'}")

    # post-check: no non-target article on this page should flip to unlike/unrt from us
    # (cheap: only log if article[1+] got engaged — shouldn't happen with scoped clicks)
    if n > 1:
        other = await parse_article(arts.nth(1), acc)
        if other.get('likeTid') == 'unlike' or other.get('rtTid') == 'unretweet':
            if (other.get('author') or '').lower() != acc.lower():
                logger.warning(
                    f"  WARN non-target engaged? @{other.get('author')} "
                    f"like={other.get('likeTid')} rt={other.get('rtTid')}"
                )

    await asyncio.sleep(random.uniform(1.5, 3.0))
    return tweet


async def main():
    if not is_active_hours():
        logger.info("Outside active hours. Skip.")
        return

    logger.info("=== like_retweet_cron start ===")
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
    # verify articles render (detect stale cookie / cookie wall)
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
        await browser.close(); await p.stop(); return

    n = random.randint(MIN_ACTIONS, MAX_ACTIONS)
    selected = random.sample(pending, min(n, len(pending)))
    logger.info(f"selected={len(selected)} of {len(pending)}")
    for t in selected:
        logger.info(f"  pick @{t['account']} {t['id']} {t['permalink']}")

    done = []
    for tweet in selected:
        try:
            await process_tweet(page, tweet)
            # only mark processed if we actually acted or confirmed skip permanently
            if tweet.get('skipped') == 'reply_or_wrong_root':
                # permanent skip — it's a reply, don't retry
                skip_reply_set = set(state.get('skip_reply', []))
                skip_reply_set.add(tweet['id'])
                state['skip_reply'] = list(skip_reply_set)
                save_state(state)
                logger.info(f"  marked reply skipped {tweet['id']}")
                continue
            processed_ids.add(tweet['id'])
            state['processed'] = list(processed_ids)
            save_state(state)
            done.append(tweet)
        except Exception as e:
            logger.error(f"process {tweet['id']}: {e}")

    if done:
        lines = [f"*Like/RT batch* · {len(done)} tweet"]
        for t in done:
            flags = []
            if t.get('liked'):
                flags.append('❤️')
            if t.get('retweeted'):
                flags.append('🔁')
            if not flags:
                flags.append('⚠️')
            lines.append(
                f"- @{t.get('account','?')} "
                f"[{t['id']}](https://x.com/{t.get('account')}/status/{t['id']}) "
                f"{''.join(flags)}"
            )
        send_telegram('\n'.join(lines))

    await browser.close(); await p.stop()
    logger.info("=== done ===")


if __name__ == '__main__':
    asyncio.run(main())
