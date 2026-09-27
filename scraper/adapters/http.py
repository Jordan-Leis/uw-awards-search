"""
Polite HTTP for adapters: robots.txt enforcement, rate limiting, retries.

Stdlib only. The static sources this module serves need neither Playwright nor
BeautifulSoup — that machinery exists for UW because PeopleSoft will not render
without JavaScript, and carrying it over to a plain HTML table would be cargo
cult.

ROBOTS IS ENFORCED HERE, IN CODE.
The project's crawl policy is to respect robots.txt strictly. That decision is
recorded in sources/blocked/*.yaml, but a policy that lives only in a YAML
comment is a policy that gets broken by the next adapter someone writes in a
hurry. Every fetch goes through can_fetch() first, and a disallowed URL raises
rather than returning data. Opting out requires passing ignore_robots=True
explicitly, which exists only so that a human can test against a URL they own.
"""
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

# Identifies the crawler and points at the project, so an operator who dislikes
# this traffic can find out who to contact instead of silently blocking a UA.
USER_AGENT = (
    "uw-awards-search/2.0 (+https://github.com/Jordan-Leis/uw-awards-search; "
    "non-commercial student awards aggregator)"
)

DEFAULT_RATE_LIMIT_SECONDS = 1.0
DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 3


class RobotsDisallowed(Exception):
    """Raised when robots.txt forbids a URL. Never caught-and-ignored."""


class PoliteFetcher:
    """One instance per source. Caches robots.txt and paces requests."""

    def __init__(self, rate_limit=DEFAULT_RATE_LIMIT_SECONDS, timeout=DEFAULT_TIMEOUT,
                 user_agent=USER_AGENT, ignore_robots=False):
        self.rate_limit = rate_limit
        self.timeout = timeout
        self.user_agent = user_agent
        self.ignore_robots = ignore_robots
        self._robots = {}       # netloc -> RobotFileParser or None
        self._last_request = 0.0
        self.fetch_count = 0

    # -- robots ----------------------------------------------------------
    def _robots_for(self, url):
        netloc = urllib.parse.urlsplit(url).netloc
        if netloc in self._robots:
            return self._robots[netloc]

        robots_url = urllib.parse.urlunsplit(
            (urllib.parse.urlsplit(url).scheme, netloc, "/robots.txt", "", ""))
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        try:
            req = urllib.request.Request(robots_url, headers={"User-Agent": self.user_agent})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                parser.parse(resp.read().decode("utf-8", "replace").splitlines())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                # Directives exist but are unreadable. Treated as disallow:
                # "I could not read the rules" is not "there are no rules".
                # This is why sources/blocked/scholarshipscanada.yaml exists.
                parser = None
            else:
                # 404 and friends: no robots.txt at all, which permits crawling.
                parser.parse([])
        except Exception:
            parser = None       # network failure — fail closed, not open

        self._robots[netloc] = parser
        return parser

    def can_fetch(self, url):
        if self.ignore_robots:
            return True
        parser = self._robots_for(url)
        if parser is None:
            return False        # unreadable robots.txt == disallowed
        return parser.can_fetch(self.user_agent, url)

    def crawl_delay(self, url):
        parser = self._robots_for(url) if not self.ignore_robots else None
        if parser is None:
            return None
        try:
            return parser.crawl_delay(self.user_agent)
        except Exception:
            return None

    # -- fetching --------------------------------------------------------
    def _wait(self, url):
        delay = self.rate_limit
        declared = self.crawl_delay(url)
        if declared:
            delay = max(delay, float(declared))   # honour a site's own pacing
        elapsed = time.monotonic() - self._last_request
        if elapsed < delay:
            time.sleep(delay - elapsed)
        self._last_request = time.monotonic()

    def get(self, url, retries=DEFAULT_RETRIES):
        """Fetch a URL as text. Raises RobotsDisallowed if forbidden."""
        if not self.can_fetch(url):
            raise RobotsDisallowed(
                f"robots.txt disallows {url} for {self.user_agent!r}. "
                f"This project respects robots.txt strictly; if this source should be "
                f"ingested, resolve it with the operator and record the outcome in "
                f"sources/, do not bypass this check."
            )

        last_error = None
        for attempt in range(retries):
            self._wait(url)
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-CA,en;q=0.9",
                })
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    self.fetch_count += 1
                    charset = resp.headers.get_content_charset() or "utf-8"
                    return resp.read().decode(charset, "replace")
            except urllib.error.HTTPError as e:
                last_error = e
                if e.code in (404, 410):
                    raise           # a missing page is an answer, not a blip
                if e.code == 429:
                    time.sleep(min(60, 5 * (attempt + 1)))
            except Exception as e:
                last_error = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)

        raise RuntimeError(f"GET {url} failed after {retries} attempts: {last_error}")
