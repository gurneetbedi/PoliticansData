"""
Download ECI party symbols to app/static/party_symbols/.

Strategy: fetch the *primary image* from each party's English
Wikipedia article via the pageimages API. That image is the
canonical logo/flag/symbol shown in the article's infobox —
i.e. exactly the symbol the ECI has assigned to the party.

This approach avoids Commons search's first-match randomness
(which was returning tangentially related images instead of the
actual party symbol).

Usage:
    python scripts/download_party_symbols.py
    python scripts/download_party_symbols.py --refresh    # re-download all
    python scripts/download_party_symbols.py --debug      # print API responses
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


# Party short_name → English Wikipedia article title.
# We fetch each article's primary infobox image via pageimages API,
# which is deterministic (article has one canonical image) and returns
# the actual ECI party symbol shown in the infobox.
PARTY_ARTICLES: dict[str, str] = {
    # National parties
    "BJP":     "Bharatiya Janata Party",
    "INC":     "Indian National Congress",
    "AAP":     "Aam Aadmi Party",
    "BSP":     "Bahujan Samaj Party",
    "CPI":     "Communist Party of India",
    "CPM":     "Communist Party of India (Marxist)",
    "CPI(M)":  "Communist Party of India (Marxist)",
    "NCP":     "Nationalist Congress Party",
    "NCP(SP)": "Nationalist Congress Party – Sharadchandra Pawar",
    "NCP(SCP)":"Nationalist Congress Party – Sharadchandra Pawar",

    # Eastern regional
    "AITC":    "All India Trinamool Congress",
    "TMC":     "All India Trinamool Congress",
    "BJD":     "Biju Janata Dal",
    "RJD":     "Rashtriya Janata Dal",
    "JDU":     "Janata Dal (United)",
    "JD(U)":   "Janata Dal (United)",
    "JDS":     "Janata Dal (Secular)",
    "JD(S)":   "Janata Dal (Secular)",
    "LJP":     "Lok Janshakti Party",

    # Southern regional
    "DMK":     "Dravida Munnetra Kazhagam",
    "AIADMK":  "All India Anna Dravida Munnetra Kazhagam",
    "ADMK":    "All India Anna Dravida Munnetra Kazhagam",
    "IUML":    "Indian Union Muslim League",
    "MDMK":    "Marumalarchi Dravida Munnetra Kazhagam",
    "DMDK":    "Desiya Murpokku Dravida Kazhagam",
    "PMK":     "Pattali Makkal Katchi",
    "VCK":     "Viduthalai Chiruthaigal Katchi",
    "TDP":     "Telugu Desam Party",
    "YSRCP":   "YSR Congress Party",
    "YSRC":    "YSR Congress Party",
    "JSP":     "Jana Sena Party",
    "BRS":     "Bharat Rashtra Samithi",
    "TRS":     "Bharat Rashtra Samithi",
    "AIMIM":   "All India Majlis-e-Ittehadul Muslimeen",
    "TVK":     "Tamilaga Vettri Kazhagam",

    # Northern / Central regional
    "SP":      "Samajwadi Party",
    "SAD":     "Shiromani Akali Dal",
    "SAD(B)":  "Shiromani Akali Dal",
    "SS":      "Shiv Sena",
    "SHS":     "Shiv Sena",
    "SS(UBT)": "Shiv Sena (Uddhav Balasaheb Thackeray)",
    "SHS(UBT)":"Shiv Sena (Uddhav Balasaheb Thackeray)",
    "MNS":     "Maharashtra Navnirman Sena",
    "RLD":     "Rashtriya Lok Dal",
    "JKN":     "Jammu & Kashmir National Conference",
    "JKNC":    "Jammu & Kashmir National Conference",
    "JKPDP":   "Jammu and Kashmir Peoples Democratic Party",
    "NPP":     "National People's Party (India)",
    "NPF":     "Naga People's Front",
    "NDPP":    "Nationalist Democratic Progressive Party",
    "SKM":     "Sikkim Krantikari Morcha",
    "SDF":     "Sikkim Democratic Front",
    "MGP":     "Maharashtrawadi Gomantak Party",
    "AGP":     "Asom Gana Parishad",
    "AIUDF":   "All India United Democratic Front",
}

# Wikimedia asks that scripted API access identify itself.
USER_AGENT = ("LokvaniPartySymbolFetcher/1.0 "
              "(https://github.com/lokvani; contact: gurneet.bedi@me.com)")

WIKI_API = "https://en.wikipedia.org/w/api.php"


def resolve_url(session, article: str, debug: bool = False) -> str | None:
    """Return the primary infobox image URL for a Wikipedia article.

    Uses the pageimages API which returns the article's canonical image
    (the one in the infobox). For party articles this is the ECI-assigned
    election symbol or flag.

    Follows redirects (e.g. shortened aliases → canonical article title)
    and returns the full-resolution "original" image URL.
    """
    params = {
        "action":   "query",
        "titles":   article,
        "prop":     "pageimages",
        "piprop":   "original",
        "redirects": "1",
        "format":   "json",
        "formatversion": "2",
    }
    r = session.get(WIKI_API, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if debug:
        import json as _j
        print("    api:", _j.dumps(data)[:500], file=sys.stderr)
    pages = data.get("query", {}).get("pages", [])
    for p in pages:
        if p.get("missing"):
            return None
        original = p.get("original") or {}
        url = original.get("source")
        if url:
            return url
    return None


def download(session, url: str, dest: Path) -> None:
    r = session.get(url, timeout=30)
    r.raise_for_status()
    dest.write_bytes(r.content)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh", action="store_true",
                    help="Re-download even if the file already exists")
    ap.add_argument("--out-dir",
                    default="app/static/party_symbols",
                    help="Where to save the symbols")
    ap.add_argument("--debug", action="store_true",
                    help="Print API responses for troubleshooting")
    args = ap.parse_args()

    try:
        import requests
    except ImportError:
        sys.exit("pip install requests")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    ok, skipped, missing, failed = 0, 0, 0, 0
    for short, query in PARTY_ARTICLES.items():
        # Sanitize the party key to a safe filename (parens → underscores)
        safe = short.replace("(", "_").replace(")", "").replace(" ", "_")

        # Treat any existing variant of this party (png/svg/jpg) as "done"
        alt_variants = [out_dir / f"{safe}.png",
                         out_dir / f"{safe}.svg",
                         out_dir / f"{safe}.jpg",
                         out_dir / f"{safe}.jpeg"]
        if not args.refresh and any(a.exists() for a in alt_variants):
            skipped += 1
            continue

        try:
            url = resolve_url(session, query, debug=args.debug)
            if not url:
                print(f"  ? {short}: no Commons match for {query!r}",
                      file=sys.stderr)
                missing += 1
                time.sleep(0.8)
                continue
            dest = out_dir / f"{safe}{Path(url).suffix.lower() or '.svg'}"
            download(session, url, dest)
            print(f"  ✓ {short} → {dest.name}")
            ok += 1
        except Exception as e:
            print(f"  ✗ {short}: {e}", file=sys.stderr)
            failed += 1
        # Wikimedia's rate limit is ~200 req/min, but we go slower to be polite
        time.sleep(1.2)

    print(f"\n{ok} downloaded · {skipped} skipped (already exist) · "
          f"{missing} not on Commons · {failed} failed", file=sys.stderr)
    print(f"Saved to {out_dir.resolve()}", file=sys.stderr)


if __name__ == "__main__":
    main()
