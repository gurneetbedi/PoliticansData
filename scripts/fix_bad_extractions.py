"""One-shot repair pass for the bad extractions identified by
inspect_bad_extractions.py.

Handles all four verdict buckets:

  raw_recoverable      → Parse the extraction._raw string in place
                         (Gemini returned data but the pipeline stashed
                         it as text — no re-run needed, zero cost).

  right_person_bad_ocr → Delete the Gemini JSON so the next
                         llm_extract_via_gemini.py run reprocesses it.

  wrong_person         → Delete the (mis-associated) source PDF and its
                         Cloud Vision + Gemini derivatives. Then refetch
                         via the manifest profile_url. Rerun Cloud
                         Vision and Gemini to fill it in.

  empty_ocr            → Same as wrong_person — assume the PDF is bad,
                         refetch to try for a better copy.

Modes:
    --dry-run   (default: on) — classify + count, don't touch any files
    --commit                   — apply the fixes
    --skip-refetch             — do raw + gemini-delete but skip the PDF
                                 refetch (useful when Chrome CDP is down)

Usage:
    # See what would happen
    python scripts/fix_bad_extractions.py --state "Rajasthan" --year 2023

    # Do it for real
    python scripts/fix_bad_extractions.py --state "Rajasthan" --year 2023 --commit --cdp 9222

After --commit:
    python scripts/cloud_vision_preprocess.py --pdf-dir .../raw_pdfs --out-dir .../preprocessed_...
    python scripts/llm_extract_via_gemini.py --in-dir .../preprocessed_... --out-dir .../llm_extracted/... --state ... --year ...
    python scripts/apply_llm_extraction.py --cycles <slug>
"""
from __future__ import annotations
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# ---------- classify ----------
def _fuzzy_contains(hay: str, needle: str) -> bool:
    toks = [re.sub(r"[^a-z0-9]+", "", t.lower()) for t in needle.split()]
    toks = [t for t in toks if len(t) >= 4]
    if not toks:
        return False
    norm_h = re.sub(r"[^a-z0-9]+", " ", hay.lower())
    hits = sum(1 for t in toks if t in norm_h)
    return hits / len(toks) >= 0.6


def classify_all(lx: Path, pp: Path,
                  allow_stems: set[str] | None = None) -> dict[str, list[str]]:
    """Scan every Gemini JSON in `lx`. If `allow_stems` is provided,
    ONLY classify extractions whose stem is in the allowlist — fringe
    candidates outside the top-N scope get skipped."""
    verdicts = {"right_person_bad_ocr": [], "wrong_person": [],
                "empty_ocr": [], "raw_recoverable": []}
    for f in sorted(lx.glob("*.json")):
        if f.name.startswith("_"):
            continue
        stem = f.stem
        if allow_stems is not None and stem not in allow_stems:
            continue
        try:
            r = json.loads(f.read_text())
        except Exception:
            continue
        ext = r.get("extraction") or {}
        has_raw = "_raw" in ext
        name = (ext.get("identity") or {}).get("name_in_english") or ""
        const = (ext.get("political") or {}).get("constituency_name") or ""

        # Only care about broken extractions
        if not has_raw and name and const:
            continue

        # Read OCR
        text_full = ""
        pp_path = pp / (stem + ".json")
        if pp_path.exists():
            try:
                pdata = json.loads(pp_path.read_text())
                text_full = "\n".join(p.get("text", "")
                                       for p in pdata.get("pages", []))
            except Exception:
                pass

        expected = stem.rsplit("__", 1)[0].replace("_", " ")
        stripped_len = len("".join(text_full.split()))
        if has_raw:
            verdicts["raw_recoverable"].append(stem)
        elif stripped_len < 200:
            verdicts["empty_ocr"].append(stem)
        elif _fuzzy_contains(text_full, expected):
            verdicts["right_person_bad_ocr"].append(stem)
        else:
            verdicts["wrong_person"].append(stem)
    return verdicts


# ---------- salvage raw_recoverable ----------
def salvage_raw(lx: Path, stems: list[str], commit: bool) -> int:
    ok = 0
    for stem in stems:
        p = lx / (stem + ".json")
        try:
            r = json.loads(p.read_text())
        except Exception:
            continue
        raw_str = r.get("extraction", {}).get("_raw", "")
        if not raw_str:
            continue
        # Strip common junk (```json ... ``` wrappers)
        s = raw_str.strip()
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
        try:
            parsed = json.loads(s)
        except Exception:
            continue
        if commit:
            r["extraction"] = parsed
            p.write_text(json.dumps(r, indent=2, ensure_ascii=False))
        ok += 1
    return ok


# ---------- delete Gemini output (for re-run) ----------
def delete_gemini_outputs(lx: Path, stems: list[str], commit: bool) -> int:
    n = 0
    for stem in stems:
        p = lx / (stem + ".json")
        if not p.exists():
            continue
        if commit:
            p.unlink()
        n += 1
    return n


# ---------- refetch wrong_person + empty_ocr PDFs ----------
def load_manifest_index(cycle_dir: Path) -> dict[str, dict]:
    idx = {}
    for line in (cycle_dir / "manifest.jsonl").read_text().splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        p = r.get("pdf_path") or ""
        if p:
            idx[Path(p).name] = r
    return idx


async def refetch_pdfs(cycle_dir: Path, stems: list[str], cdp: int,
                        tabs: int, delay: float, commit: bool,
                        lx: Path, pp: Path) -> None:
    if not stems:
        return
    mf = load_manifest_index(cycle_dir)
    raw_dir = cycle_dir / "raw_pdfs" if (cycle_dir / "raw_pdfs").exists() else cycle_dir

    jobs = []
    no_url = []
    for stem in stems:
        row = mf.get(stem + ".pdf")
        if not row or not row.get("profile_url"):
            no_url.append(stem)
            continue
        jobs.append({
            "stem": stem, "url": row["profile_url"],
            "name": row.get("name", ""),
        })

    if no_url:
        print(f"  {len(no_url)} candidate(s) have no profile_url in manifest — skip",
              file=sys.stderr)
    if not jobs:
        return

    print(f"  Refetching {len(jobs)} PDF(s) across {tabs} tab(s)…",
          file=sys.stderr)
    if not commit:
        for j in jobs[:5]:
            print(f"    would refetch: {j['stem']} ← {j['url'][:60]}…",
                  file=sys.stderr)
        if len(jobs) > 5:
            print(f"    … + {len(jobs) - 5} more", file=sys.stderr)
        return

    # Delete stale PDFs + derived outputs first so the pipeline reprocesses
    for j in jobs:
        for path in [raw_dir / (j["stem"] + ".pdf"),
                     pp / (j["stem"] + ".json"),
                     lx / (j["stem"] + ".json")]:
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass

    from playwright.async_api import async_playwright

    shards: list[list] = [[] for _ in range(tabs)]
    for i, j in enumerate(jobs):
        shards[i % tabs].append(j)

    counter = {"ok": 0, "fail": 0}
    lock = asyncio.Lock()

    async def worker(tab_id: int, page, sublist):
        for j in sublist:
            ok, msg = await _download(page, j["url"], raw_dir, j["stem"])
            async with lock:
                counter["ok" if ok else "fail"] += 1
            mark = "✓" if ok else "✗"
            print(f"    tab{tab_id} {mark} {j['stem'][:40]:<40s}  {msg[:50]}",
                  file=sys.stderr)
            await asyncio.sleep(delay)

    async with async_playwright() as pw:
        browser = await pw.chromium.connect_over_cdp(f"http://localhost:{cdp}")
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        existing = list(ctx.pages)
        pages = [existing[i] if i < len(existing) and not existing[i].is_closed()
                 else await ctx.new_page() for i in range(tabs)]
        await asyncio.gather(*[worker(i, pages[i], shards[i])
                                for i in range(tabs)])

    print(f"\n  Refetch: {counter['ok']} ok, {counter['fail']} failed",
          file=sys.stderr)


async def _download(page, profile_url: str, raw_dir: Path,
                     stem: str) -> tuple[bool, str]:
    try:
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(1.0)
        async with page.expect_download(timeout=60000) as dl:
            btn = await page.query_selector(
                "button.download-btn, button:has-text('Download'), a:has-text('Download')")
            if not btn:
                return False, "no Download button"
            await btn.click()
        d = await dl.value
        target = raw_dir / (stem + ".pdf")
        target.parent.mkdir(parents=True, exist_ok=True)
        await d.save_as(str(target))
        return True, target.name
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:50]}"


# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", required=True)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--skip-refetch", action="store_true")
    ap.add_argument("--cdp", type=int, default=9222)
    ap.add_argument("--tabs", type=int, default=4)
    ap.add_argument("--delay", type=float, default=1.5)
    args = ap.parse_args()

    slug = args.state.lower().replace(" ", "") + f"_{args.year}"
    cycle_slug = args.state.lower().replace(" ", "")
    lx = ROOT / "data/eci/for_ai/llm_extracted" / slug
    pp = ROOT / "data/eci/for_ai" / f"preprocessed_{slug}"

    cycle_dir = None
    for d in sorted((ROOT / "data/eci/raw_pdfs").iterdir()):
        if d.name.startswith(cycle_slug) and d.name.endswith(str(args.year)):
            cycle_dir = d
            break
    if not cycle_dir or not lx.exists() or not pp.exists():
        sys.exit("Missing cycle / lx / pp folder")

    # Load the top-N allowlist so we only classify + fix candidates
    # within our target scope. Fringe candidates (3rd/4th place, etc.)
    # outside the allowlist are intentionally left alone.
    allow_dir = ROOT / "data/allowlists"
    slug_year = f"{args.state.lower().replace(' ', '')}_{args.year}"
    allow_path = allow_dir / f"{slug_year}.txt"
    if not allow_path.exists():
        matches = sorted(allow_dir.glob(f"{slug_year}_top*.txt"))
        allow_path = matches[0] if matches else None
    allow_stems: set[str] | None = None
    if allow_path and allow_path.exists():
        allow_stems = {ln.strip()[:-4] for ln in allow_path.read_text().splitlines()
                       if ln.strip().endswith(".pdf")}
        print(f"Scope: {allow_path.name} ({len(allow_stems)} candidates)",
              file=sys.stderr)
    else:
        print(f"⚠ No allowlist for {slug_year} — will scan ALL Gemini JSONs "
              f"(may include out-of-scope candidates)", file=sys.stderr)

    verdicts = classify_all(lx, pp, allow_stems=allow_stems)
    print(f"\nVerdicts for {args.state} {args.year}:")
    for k, v in verdicts.items():
        print(f"  {len(v):>3d}  {k}")

    mode = "COMMIT" if args.commit else "DRY-RUN"
    print(f"\n[{mode}] Applying fixes:\n")

    # 1. Salvage raw
    print("[1/3] Salvage raw_recoverable")
    n = salvage_raw(lx, verdicts["raw_recoverable"], args.commit)
    print(f"  parsed {n}/{len(verdicts['raw_recoverable'])}\n")

    # 2. Delete Gemini for right_person_bad_ocr
    print("[2/3] Delete Gemini outputs (right_person_bad_ocr)")
    n = delete_gemini_outputs(lx, verdicts["right_person_bad_ocr"], args.commit)
    print(f"  {'deleted' if args.commit else 'would delete'} {n} JSON(s)\n")

    # 3. Refetch wrong + empty
    print("[3/3] Refetch wrong_person + empty_ocr PDFs")
    refetch = verdicts["wrong_person"] + verdicts["empty_ocr"]
    if args.skip_refetch:
        print(f"  --skip-refetch given; skipping {len(refetch)} PDF(s)")
    else:
        asyncio.run(refetch_pdfs(cycle_dir, refetch, args.cdp, args.tabs,
                                    args.delay, args.commit, lx, pp))

    if not args.commit:
        print(f"\n(--dry-run) — nothing changed. Re-run with --commit.")
    else:
        print(f"\n✓ Done. Next: rerun Cloud Vision + Gemini + apply.")


if __name__ == "__main__":
    main()
