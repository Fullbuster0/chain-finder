import asyncio
import json
import logging
import random
import time
from pathlib import Path
from typing import List, Dict, Optional, Any

from playwright.async_api import async_playwright, Browser, Page, BrowserContext

logger = logging.getLogger(__name__)


class XStealthBrowser:
    """
    Playwright-based stealth browser for X/Twitter automation.
    Uses cookies for authentication and mimics human-like behavior.
    """
    
    def __init__(
        self,
        cookie_file: str,
        headless: bool = True,
        user_agent: Optional[str] = None,
        viewport: Optional[Dict[str, int]] = None,
        proxy: Optional[Dict[str, str]] = None,
        timeout: int = 30000,
        slow_mo: int = 50,
    ):
        self.cookie_file = Path(cookie_file)
        self.headless = headless
        self.user_agent = user_agent or self._default_user_agent()
        self.viewport = viewport or {"width": 1280, "height": 800}
        self.proxy = proxy
        self.timeout = timeout
        self.slow_mo = slow_mo
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._playwright = None

    @staticmethod
    def _default_user_agent() -> str:
        return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()

    async def start(self):
        """Launch browser and load cookies."""
        self._playwright = await async_playwright().start()
        browser_type = self._playwright.chromium
        self.browser = await browser_type.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
            proxy=self.proxy,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        self.context = await self.browser.new_context(
            viewport=self.viewport,
            user_agent=self.user_agent,
            locale="en-US",
            timezone_id="America/New_York",
            ignore_https_errors=True,
        )
        # Load cookies if file exists
        if self.cookie_file.exists():
            cookies = self._parse_cookies(self.cookie_file)
            if cookies:
                await self.context.add_cookies(cookies)
                logger.info(f"Loaded {len(cookies)} cookies from {self.cookie_file}")
        else:
            logger.warning(f"Cookie file not found: {self.cookie_file}")
        self.page = await self.context.new_page()
        # Set default timeout
        self.page.set_default_timeout(self.timeout)
        # Add extra headers to appear more human
        await self.page.set_extra_http_headers({
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        })
        # Ensure logged in
        await self._ensure_logged_in()

    async def stop(self):
        """Close browser and playwright."""
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()
        self.browser = None
        self.context = None
        self.page = None
        self._playwright = None

    def _parse_cookies(self, cookie_file: Path) -> List[Dict[str, Any]]:
        """Parse Netscape cookie file format."""
        cookies = []
        with open(cookie_file, 'r') as f:
            lines = f.readlines()
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            parts = line.split('\t')
            if len(parts) < 7:
                continue
            # Format: domain, domain_specific, path, secure, expires, name, value
            domain = parts[0]
            domain_specific = parts[1] == 'TRUE'
            path = parts[2]
            secure = parts[3] == 'TRUE'
            expires = int(parts[4]) if parts[4] != '0' else None
            name = parts[5]
            value = parts[6]
            cookie = {
                'name': name,
                'value': value,
                'domain': domain,
                'path': path,
                'secure': secure,
                'httpOnly': False,
            }
            if expires:
                cookie['expires'] = expires
            cookies.append(cookie)
        return cookies

    async def _ensure_logged_in(self):
        """Navigate to X and ensure cookies are valid (logged in)."""
        await self.page.goto("https://x.com/home", wait_until="networkidle")
        # Check if we are on login page or home page
        if "/login" in self.page.url or "/i/flow/login" in self.page.url:
            logger.warning("Not logged in or cookies expired. Please refresh cookies.")
            # Maybe we can try to handle login, but for now raise
            raise RuntimeError("Not authenticated. Cookies may be expired.")
        logger.info("Successfully logged in to X")

    async def navigate_to_user(self, username: str):
        """Navigate to a user's profile."""
        await self.page.goto(f"https://x.com/{username}", wait_until="networkidle")
        await self._random_delay(0.5, 1.5)

    async def fetch_timeline(self, username: str, limit: int = 20) -> List[Dict]:
        """Fetch recent tweets from a user's timeline."""
        await self.navigate_to_user(username)
        # Wait for tweets to load
        await self.page.wait_for_selector("article[data-testid='tweet']", state="attached", timeout=15000)
        # Extract tweets
        tweets = await self.page.evaluate('''
            (limit) => {
                const articles = document.querySelectorAll("article[data-testid='tweet']");
                const results = [];
                for (const article of articles) {
                    if (results.length >= limit) break;
                    const tweet = {
                        id: article.getAttribute('data-tweet-id') || '',
                        text: article.querySelector('[data-testid="tweetText"]')?.innerText || '',
                        user: article.querySelector('[data-testid="User-Name"]')?.innerText || '',
                        time: article.querySelector('time')?.getAttribute('datetime') || '',
                        permalink: article.querySelector('a[href*="/status/"]')?.getAttribute('href') || '',
                    };
                    if (tweet.id) results.push(tweet);
                }
                return results;
            }
        ''', limit)
        return tweets

    async def like_tweet(self, tweet_id: str):
        """Like a tweet by ID."""
        # Find like button. Use data-testid="like"
        # Navigate to tweet permalink
        await self.page.goto(f"https://x.com/i/status/{tweet_id}", wait_until="networkidle")
        # Check if already liked
        liked = await self.page.evaluate('''
            () => {
                const likeBtn = document.querySelector('[data-testid="like"]');
                if (!likeBtn) return false;
                const ariaLabel = likeBtn.getAttribute('aria-label');
                return ariaLabel && ariaLabel.toLowerCase().includes('unlike');
            }
        ''')
        if liked:
            logger.info(f"Tweet {tweet_id} already liked. Skipping.")
            return
        # Click like button
        await self.page.click('[data-testid="like"]')
        await self._random_delay(0.5, 1.0)
        logger.info(f"Liked tweet {tweet_id}")

    async def retweet_tweet(self, tweet_id: str):
        """Retweet a tweet."""
        await self.page.goto(f"https://x.com/i/status/{tweet_id}", wait_until="networkidle")
        # Check if already retweeted
        retweeted = await self.page.evaluate('''
            () => {
                const rtBtn = document.querySelector('[data-testid="retweet"]');
                if (!rtBtn) return false;
                const ariaLabel = rtBtn.getAttribute('aria-label');
                return ariaLabel && ariaLabel.toLowerCase().includes('undoretweet');
            }
        ''')
        if retweeted:
            logger.info(f"Tweet {tweet_id} already retweeted. Skipping.")
            return
        # Click retweet button
        await self.page.click('[data-testid="retweet"]')
        await self._random_delay(0.5, 1.0)
        # Click confirm retweet (popup)
        await self.page.click('[data-testid="retweetConfirm"]')
        await self._random_delay(0.5, 1.0)
        logger.info(f"Retweeted tweet {tweet_id}")

    async def quote_tweet(self, tweet_id: str, text: str):
        """Quote tweet with custom text."""
        await self.page.goto(f"https://x.com/i/status/{tweet_id}", wait_until="networkidle")
        # Click quote tweet button (data-testid="quoteTweet")
        await self.page.click('[data-testid="quoteTweet"]')
        await self._random_delay(0.5, 1.0)
        # Fill text in the compose box
        await self.page.fill('[data-testid="tweetTextarea_0"]', text)
        await self._random_delay(0.5, 1.0)
        # Click tweet button
        await self.page.click('[data-testid="tweetButton"]')
        await self._random_delay(0.5, 1.0)
        logger.info(f"Quote tweeted {tweet_id} with text: {text[:50]}...")

    async def _random_delay(self, min_sec: float = 0.5, max_sec: float = 1.5):
        """Sleep for a random duration to mimic human behavior."""
        await asyncio.sleep(random.uniform(min_sec, max_sec))

    async def random_scroll(self, times: int = 2):
        """Scroll the page randomly."""
        for _ in range(times):
            await self.page.mouse.wheel(0, random.randint(200, 600))
            await asyncio.sleep(random.uniform(0.5, 1.5))

    async def close_popups(self):
        """Close any popups that might appear."""
        try:
            # Sometimes a login prompt appears on home page
            # Click "Not now" if exists
            not_now = await self.page.query_selector('[data-testid="DMDialog"] button:has-text("Not now")')
            if not_now:
                await not_now.click()
                logger.info("Closed login popup")
        except Exception:
            pass


def run_sync(coro):
    """Helper to run async coroutine synchronously."""
    return asyncio.run(coro)
