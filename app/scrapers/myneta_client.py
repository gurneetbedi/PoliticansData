"""
Polite HTTP client for myneta.info.

- Caches every response to disk so re-runs do not re-hit ADR's servers
- Rate limits requests (configurable; default 2s between requests)
- Sets an honest User-Agent identifying the project
- Retries with backoff on transient errors
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
    "PoliTrack/0.1 (open-source transparency project; contact: gurneetbedi@gmail.com)"
)

RATE_LIMIT_SECONDS = float(os.getenv("MYNETA_RATE_LIMIT", "2.0"))
_last_request_time = 0.0


def _cache_path(url: str) -> Path:
    digest = hashlib.sha1(url.encode()).hexdigest()
    return CACHE_DIR / f"{digest}.html"


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=30),
    retry=retry_if_exception_type(requests.RequestException),
)
def _http_get(url: str) -> str:
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
