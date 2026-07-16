"""
Download CM + PM photos from Wikipedia once, save as JPGs in
app/static/cms/. Frontend then serves them locally — no runtime
MediaWiki API calls, no CORS, no rate limits, fast page loads.

Reads:
  app/static/chief_ministers.json    (31 state CMs with wiki_page slug)
  app/static/lok_sabha_2024.json     (PM Modi with wiki_page slug)

Writes:
  app/static/cms/<state_slug>.jpg    (one per CM)
  app/static/cms/pm.jpg              (PM photo)

Usage:
    python scripts/fetch_cm_photos.py             # only fetch missing
    python scripts/fetch_cm_photos.py --refresh   # re-download everything
"""
from __future__ import annotations
import argparse
import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
OUT_DIR = STATIC / "cms"

WIKI_API = "https://en.wikipedia.org/w/api.php"
# Wikimedia asks that clients identify themselves + include contact URL
# per their user-agent policy. Anonymous / generic UAs get rate-limited
# aggressively.
USER_AGENT = ("Lokvani/1.0 (https://github.com/gurneetbedi/lokvani; "
              "civic transparency dashboard; one-time photo backfill)")

# Delay between requests to stay under Wikimedia's rate limit
# (their guidance: ≤ 200 req/sec, but be polite for background jobs).
BASE_DELAY_SEC = 1.5
MAX_RETRIES = 4


def slugify(s: str) -> str:
    return (
        s.lower()
        .replace(" and ", "-")
        .replace(" & ", "-")
        .replace(" ", "-")
        .replace("(", "")
        .replace(")", "")
    )


def _http_get(url: str, timeout: int = 15) -> bytes | None:
    """GET with retry+backoff for HTTP 429 / transient errors."""
    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # Rate limited — exponential backoff
                backoff = (2 ** attempt) * BASE_DELAY_SEC
                print(f"  ⏳ 429 rate-limited, sleeping {backoff:.0f}s "
                      f"(attempt {attempt+1}/{MAX_RETRIES})", file=sys.stderr)
                time.sleep(backoff)
                continue
            print(f"  ✗ HTTP {e.code}: {e.reason}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"  ✗ Error: {type(e).__name__}: {e}", file=sys.stderr)
            return None
    print(f"  ✗ Gave up after {MAX_RETRIES} retries", file=sys.stderr)
    return None


def fetch_thumb_url(wiki_page: str, size: int = 240) -> str | None:
    """Return the pageimage thumbnail URL for a Wikipedia article, or None."""
    params = (
        f"?action=query&format=json&titles={urllib.request.quote(wiki_page)}"
        f"&prop=pageimages&pithumbsize={size}"
    )
    body = _http_get(WIKI_API + params)
    if not body:
        return None
    try:
        data = json.loads(body.decode())
    except Exception as e:
        print(f"  ✗ JSON parse error: {e}", file=sys.stderr)
        return None
    pages = (data.get("query") or {}).get("pages") or {}
    if not pages:
        return None
    first = next(iter(pages.values()))
    thumb = (first.get("thumbnail") or {}).get("source")
    return thumb


COMMONS_API = "https://commons.wikimedia.org/w/api.php"


def search_commons_photo(name: str, size: int = 240) -> str | None:
    """Fallback: search Wikimedia Commons for a photo when the Wikipedia
    article's infobox has no pageimage. Uses the CirrusSearch backend
    scoped to File: namespace (srnamespace=6). Returns the thumbnail URL
    of the first plausible result, or None.
    """
    query = urllib.request.quote(name)
    # Search for image files with the name as query
    search_params = (
        f"?action=query&format=json&list=search&srnamespace=6"
        f"&srlimit=5&srsearch={query}"
    )
    body = _http_get(COMMONS_API + search_params)
    if not body:
        return None
    try:
        data = json.loads(body.decode())
    except Exception:
        return None
    results = ((data.get("query") or {}).get("search") or [])
    for hit in results:
        title = hit.get("title", "")  # e.g. "File:Foo.jpg"
        if not title.startswith("File:"):
            continue
        # Filter out obviously wrong file types (audio/video/pdf/svg-logo)
        low = title.lower()
        if any(low.endswith(ext) for ext in (".ogg", ".webm", ".mp4", ".pdf", ".mp3", ".gif")):
            continue
        # Get thumbnail via imageinfo
        info_params = (
            f"?action=query&format=json&titles={urllib.request.quote(title)}"
            f"&prop=imageinfo&iiprop=url&iiurlwidth={size}"
        )
        info_body = _http_get(COMMONS_API + info_params)
        if not info_body:
            continue
        try:
            info = json.loads(info_body.decode())
        except Exception:
            continue
        pages = (info.get("query") or {}).get("pages") or {}
        for p in pages.values():
            ii = (p.get("imageinfo") or [])
            if not ii:
                continue
            thumb = ii[0].get("thumburl") or ii[0].get("url")
            if thumb:
                print(f"  ↳ Commons fallback: {title}", file=sys.stderr)
                return thumb
    return None


def download_image(url: str, out_path: Path) -> bool:
    body = _http_get(url)
    if body is None:
        return False
    out_path.write_bytes(body)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true",
                    help="Re-download even if the file already exists")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    cms = json.loads((STATIC / "chief_ministers.json").read_text())
    ls  = json.loads((STATIC / "lok_sabha_2024.json").read_text())

    tasks: list[tuple[str, str, str, str, Path]] = []
    # (label, wiki_page, override_url, search_name, out_path)
    for state, cm in cms.items():
        if state.startswith("_"):
            continue
        wp = cm.get("wiki_page") or ""
        override = cm.get("photo_url") or ""
        # Search fallback query = "<CM name> chief minister <state>"
        search_q = f"{cm.get('name','')} chief minister {state}"
        out = OUT_DIR / f"{slugify(state)}.jpg"
        tasks.append((state, wp, override, search_q, out))

    pm = ls.get("prime_minister") or {}
    if pm.get("wiki_page"):
        tasks.append((
            "Prime Minister",
            pm["wiki_page"],
            pm.get("photo_url") or "",
            f"{pm.get('name','')} Prime Minister India",
            OUT_DIR / "pm.jpg",
        ))

    print(f"→ {len(tasks)} photos to consider "
          f"(delay {BASE_DELAY_SEC}s between requests)", file=sys.stderr)
    fetched, skipped, failed = 0, 0, 0
    fallback_used = 0
    first = True
    for label, wp, override, search_q, out in tasks:
        if out.exists() and not args.refresh:
            skipped += 1
            continue
        # Politeness sleep between requests (skip the first one)
        if not first:
            time.sleep(BASE_DELAY_SEC)
        first = False
        print(f"  {label:22s}  {wp:35s}", file=sys.stderr, end="")

        # Fallback chain: manual override → Wikipedia pageimages → Commons search
        url = None
        if override:
            url = override
            print(f"  (manual override)", file=sys.stderr, end="")
        if not url and wp:
            url = fetch_thumb_url(wp)
        if not url and search_q:
            print(f"  no pageimage, trying Commons search…", file=sys.stderr)
            time.sleep(BASE_DELAY_SEC)
            url = search_commons_photo(search_q)
            if url:
                fallback_used += 1
                print(f"  {label:22s}  {wp:35s}", file=sys.stderr, end="")

        if not url:
            print(f"  ✗ no photo found anywhere", file=sys.stderr)
            failed += 1
            continue
        time.sleep(0.3)
        ok = download_image(url, out)
        if ok:
            print(f"  ✓ {out.stat().st_size // 1024} KB", file=sys.stderr)
            fetched += 1
        else:
            failed += 1

    print(file=sys.stderr)
    print(f"========== FETCH SUMMARY ==========", file=sys.stderr)
    print(f"  Fetched:  {fetched}", file=sys.stderr)
    print(f"  Skipped:  {skipped}  (already on disk; use --refresh to redo)", file=sys.stderr)
    print(f"  Failed:   {failed}", file=sys.stderr)
    if fallback_used:
        print(f"  Used Commons search fallback for {fallback_used} of the fetched.",
              file=sys.stderr)
    print(f"  Saved to: {OUT_DIR}", file=sys.stderr)
    if failed:
        print(f"\nTip: for CMs that still fail, add a `photo_url` field "
              f"to their entry in app/static/chief_ministers.json — "
              f"any direct image URL works.", file=sys.stderr)


if __name__ == "__main__":
    main()
