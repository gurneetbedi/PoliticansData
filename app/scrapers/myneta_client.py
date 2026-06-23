"""
HTTP client for myneta.info.

IMPORTANT — Scraping is DISABLED by default.

On <DATE OF ADR REPLY>, ADR formally responded to our bulk-data partnership
request: they will not share data outside their published websites, will not
issue an API to non-media-house projects, and explicitly notified us that
"You must not conduct any systematic or automated data collection activities
(including without limitation scraping, data mining, data extraction and
data harvesting) on or in relation to this website."

To honor that notice, this module raises on any outbound network attempt
unless the environment variable ALLOW_MYNETA_SCRAPE=1 is set. The cached
data on disk (data/cache/myneta/) collected before the notice can still be
re-read; only NEW network requests are blocked.

If/when ADR explicitly authorizes the project (e.g. by adding it to their
approved-media list), set ALLOW_MYNETA_SCRAPE=1 to re-enable.
"""
import hashlib
import logging
import os
import time
from pathlib import Path

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

log = logging.getLogger(__name__)

CACHE_DIR = Path(os.getenv("MYNETA_CACHE_DIR", "data/cache/myneta"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = os.getenv(
    "MYNETA_USER_AGENT",
    "PoliTrack/0.1 (open-source transparency project; contact: gurneet.bedi@me.com)"
)

RATE_LIMIT_SECONDS = float(os.getenv("MYNETA_RATE_LIMIT", "2.0"))
_last_request_time = 0.0

# Scrape disabled-by-default guard. Set ALLOW_MYNETA_SCRAPE=1 only if ADR
# subsequently authorizes the project. Re-enabling without that authorization
# would violate the explicit notice we received and your own attribution promise.
SCRAPE_ALLOWED = os.getenv("ALLOW_MYNETA_SCRAPE") == "1"


class ScrapeDisabledError(RuntimeError):
    """Raised when the scraper would make a network request but is policy-disabled."""
    pass


def _cache_path(url: str) -> Path:
    digest = hashlib.sha1(url.encode()).hexdigest()
    return CACHE_DIR / f"{digest}.html"


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=30),
    retry=retry_if_exception_type(requests.RequestException),
)
def _http_get(url: str) -> str:
    if not SCRAPE_ALLOWED:
        raise ScrapeDisabledError(
            "myneta scraping is disabled by policy (ADR notice).\n"
            "Cached pages in data/cache/myneta/ are still usable.\n"
            "To re-enable IF ADR has explicitly authorized this project, set\n"
            "  export ALLOW_MYNETA_SCRAPE=1\n"
            "Otherwise, do not work around this guard."
        )

    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - elapsed)

    log.info("Fetching %s", url)
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    _last_request_time = time.time()
    resp.raise_for_status()
    return resp.text


def fetch(url: str, force_refresh: bool = False) -> str:
    """Fetch a URL, using on-disk cache unless force_refresh=True."""
    cache_file = _cache_path(url)
    if cache_file.exists() and not force_refresh:
        return cache_file.read_text(encoding="utf-8")

    html = _http_get(url)
    cache_file.write_text(html, encoding="utf-8")
    return html
