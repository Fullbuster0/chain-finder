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
        return {"processed": []}
    with open(STATE_FILE) as f:
        data = json.load(f)
    if isinstance(data, list):
        state = {"processed": [t.get('id') for t in data if t.get('id')]}
        save_state(state)
        return state
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
  for (const href of links) {
    const m = href.match(/^\\/([^\\/]+)\\/status\\/(\\d+)$/);
    if (!m) continue;
    if (m[1].toLowerCase() === authorL || m[1].toLowerCase() === acc) {
      permalink = href; tid = m[2]; break;
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


async def _scan_page(page, acc, cutoff, processed_ids, seen):
    """Scan current page for target account tweets. Returns list of eligible."""
    found = []
    n = await page.locator('article').count()
    for i in range(n):
        art = page.locator('article').nth(i)
        try:
            d = await parse_article(art, acc)
        except Exception:
            continue
        if not d.get('isTargetAuthor'):
            continue
        if not d.get('permalink') or not d.get('tid'):
            continue
        tid = d['tid']
        if tid in seen or tid in processed_ids:
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
            'alreadyLiked': d.get('likeTid') == 'unlike',
            'alreadyRetweeted': d.get('rtTid') == 'unretweet',
        })
    return found


async def discover_account(page, acc, cutoff, processed_ids):
    """Discover eligible tweets via profile page first, then search fallback.
    Profile page is more reliable than X search which returns inconsistent results."""
    found, seen = [], set()

    # METHOD 1: Profile page (most reliable for account's own tweets)
    since = cutoff.strftime('%Y-%m-%d')
    profile_url = f'https://x.com/{acc}'
    logger.info(f"  profile: @{acc}")
    await page.goto(profile_url, wait_until='domcontentloaded', timeout=60000)
    await asyncio.sleep(3)
    # scroll aggressively to load more tweets
    for _ in range(8):
        await page.mouse.wheel(0, 1200)
        await asyncio.sleep(0.7)
    profile_found = await _scan_page(page, acc, cutoff, processed_ids, seen)
    logger.info(f"  profile @{acc}: {len(profile_found)} eligible")
    found.extend(profile_found)

    # METHOD 2: Search fallback (catches anything profile might miss)
    q = f'from:{acc} since:{since} -filter:replies'
    search_url = 'https://x.com/search?q=' + urllib.parse.quote(q) + '&f=live'
    logger.info(f"  search: {q}")
    await page.goto(search_url, wait_until='domcontentloaded', timeout=60000)
    await asyncio.sleep(3.5)
    for _ in range(6):
        await page.mouse.wheel(0, 1200)
        await asyncio.sleep(0.7)
    search_found = await _scan_page(page, acc, cutoff, processed_ids, seen)
    logger.info(f"  search @{acc}: {len(search_found)} eligible")
    found.extend(search_found)

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
    await asyncio.sleep(random.uniform(2.0, 3.0))

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
    if '/login' in page.url or '/i/flow/login' in page.url:
        logger.error(f"Login FAIL url={page.url}")
        await browser.close(); await p.stop(); return
    logger.info("Login OK")

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
                processed_ids.add(tweet['id'])
                state['processed'] = list(processed_ids)
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
