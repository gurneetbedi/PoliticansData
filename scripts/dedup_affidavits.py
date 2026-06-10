"""
Group a folder of ECI affidavit PDFs by candidate, identify the canonical
filing per candidate, and emit a JSON summary.

Rules (per the migration plan + the 4-Kejriwal-sample empirical finding):

  1. Each PDF is OCR'd on its FIRST FEW PAGES only (pages 1-5).
       - Pages 1-2 → eStamp cover page → cert number + issue date
       - Pages 2-5 → Form 26 Part A → candidate name + party + constituency

  2. PDFs with NO eStamp cert (or that look like the standalone eStamp paper
     U05_*.pdf) are bucketed under "unclassified" and skipped — they're not
     full Form 26 affidavits.

  3. PDFs are grouped by **candidate signature** = sha256(
         normalised(name) | normalised(father) | normalised(constituency)
         | normalised(party) | election_marker
     )
     Two PDFs with the same signature are filed by the same person in the
     same election.

  4. Within each candidate's signature group, PDFs are further grouped by
     **estamp cert number**. Same cert = same legal filing (multiple uploads
     of the same paper).

  5. **Canonical filing rule:** among a candidate's filings, the canonical
     one is the filing with the LATEST eStamp issue date. When a single
     filing has multiple PDFs uploaded, the LARGEST file is preferred (more
     supplementary documents bundled).

Usage:
    python scripts/dedup_affidavits.py Affadivit/
    python scripts/dedup_affidavits.py Affadivit/ --out data/eci/dedup.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path

# Sibling scripts
sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract_estamp_metadata import (  # noqa: E402
    extract_from_cover_text, _ocr_page, ESTAMP_CERT_RE,
)
from parse_eci_affidavit import find_part_a_page, parse_part_a, ParsedAffidavit  # noqa: E402


# ---------------------------------------------------------------------------
# Per-PDF identity extraction (fast — first 5 pages only)
# ---------------------------------------------------------------------------

@dataclass
class PDFRecord:
    """All the per-PDF data we need to group/dedup."""
    pdf_path: str
    file_size: int
    estamp_cert: str = ""
    estamp_issue_date: str = ""
    estamp_issue_dt: str = ""           # ISO string for reliable sorting
    candidate_name: str = ""
    candidate_father: str = ""
    candidate_party: str = ""
    candidate_constituency: str = ""
    signature: str = ""                  # candidate signature hash
    notes: list[str] = field(default_factory=list)


def _normalise_for_hash(s: str) -> str:
    """Whitespace/case/punctuation normalisation for stable hashing."""
    s = (s or "").upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _parse_estamp_dt(date_str: str) -> str:
    """Parse '13-Jan-2025 05:23 PM' to ISO timestamp. Empty string if it fails."""
    if not date_str:
        return ""
    try:
        return datetime.strptime(date_str.strip(), "%d-%b-%Y %I:%M %p").isoformat()
    except Exception:
        return ""


def _ocr_first_pages(pdf_path: Path, count: int = 5) -> list[str]:
    """OCR the first N pages of a PDF — for Part A identity extraction."""
    out = []
    for p in range(1, count + 1):
        try:
            out.append(_ocr_page(pdf_path, p))
        except Exception:
            out.append("")
            break
    return out


def extract_pdf(pdf_path: Path, page_budget: int = 4) -> PDFRecord:
    """Run eStamp + Part A extraction on a single PDF.

    OCRs each page only ONCE and runs both extractors over the cached text.
    `page_budget` of 4 covers the eStamp cover (page 1, sometimes 2) AND the
    Form 26 Part A page (usually pages 2-3 in real samples).
    """
    rec = PDFRecord(pdf_path=str(pdf_path), file_size=pdf_path.stat().st_size)

    pages = _ocr_first_pages(pdf_path, count=page_budget)

    # eStamp metadata: scan ALL OCR'd pages, take first one with a cert
    for page_text in pages:
        if ESTAMP_CERT_RE.search(page_text):
            meta = extract_from_cover_text(page_text)
            rec.estamp_cert = meta.cert_number
            rec.estamp_issue_date = meta.issue_date
            rec.estamp_issue_dt = _parse_estamp_dt(meta.issue_date)
            break

    if not rec.estamp_cert:
        rec.notes.append("no_estamp_cert_found")

    # Form 26 Part A identity
    idx_a = find_part_a_page(pages)
    if idx_a is None:
        rec.notes.append("no_form_26_part_a_found")
        return rec

    parsed = ParsedAffidavit(source_pdf=str(pdf_path), pages_ocrd=len(pages))
    parse_part_a(pages[idx_a], parsed)
    rec.candidate_name = _strip_trailing_punct(parsed.name)
    rec.candidate_father = _strip_trailing_punct(parsed.father_name)
    rec.candidate_party = _strip_trailing_punct(parsed.party)
    rec.candidate_constituency = _strip_trailing_punct(parsed.constituency)

    return rec


def _strip_trailing_punct(s: str) -> str:
    """Strip trailing commas, periods, whitespace artefacts from OCR captures."""
    return (s or "").strip(" ,.:;|").strip()


def _signature_for(rec: PDFRecord) -> str:
    """Deterministic hash that identifies this candidate across re-filings.

    Robustness rule: empty fields are treated as WILDCARDS, not as the
    empty string. So an Akhilesh PDF whose OCR failed to capture
    constituency still hashes the same as another Akhilesh PDF that DID
    capture it — they collapse onto the same candidate as long as the
    non-empty fields all match.

    Implementation: we sign on the non-empty fields only. We need at least
    2 non-empty fields (otherwise the signature is too weak — e.g. just
    "BJP" wouldn't be unique to anyone). When fewer than 2 fields parsed,
    we fall back to a per-PDF unique sig so the row is preserved but not
    grouped with others.
    """
    # The fetcher's filename ("<NAME>__<AFFIDAVIT_ID>.pdf") is the most
    # reliable identity signal we have — it's pulled from the ECI listing
    # card and is consistent across re-uploads of the same candidate.
    # Use filename-name + party as the PRIMARY signature so OCR-induced
    # variance in Part A doesn't split a candidate into multiple groups.
    filename_name = _name_from_filename(rec.pdf_path)
    party = _normalise_for_hash(rec.candidate_party)

    # Tier 1: filename name + party — works for the 99% case where the
    # fetcher captured the name from the listing card.
    if filename_name and party:
        return hashlib.sha256(f"{filename_name}|{party}".encode()).hexdigest()[:16]

    # Tier 2: parsed Part A — fallback for PDFs whose filename was empty
    # (e.g. early smoke-test PDFs from before the listing-card name fix).
    parsed_name = _normalise_for_hash(rec.candidate_name)
    constituency = _normalise_for_hash(rec.candidate_constituency)
    if parsed_name and party:
        return hashlib.sha256(f"{parsed_name}|{party}".encode()).hexdigest()[:16]
    if constituency and party:
        return hashlib.sha256(f"{constituency}|{party}".encode()).hexdigest()[:16]

    # Tier 3: too little info to merge confidently — keep as single.
    return "single_" + hashlib.sha256(rec.pdf_path.encode()).hexdigest()[:12]


def _name_from_filename(pdf_path: str) -> str:
    """Recover the candidate name from a fetcher-written filename like
    'AKHILESH_PATI_TRIPATHI__1666.pdf' → 'AKHILESH PATI TRIPATHI'."""
    stem = Path(pdf_path).stem
    if "__" not in stem:
        return ""
    name_part = stem.rsplit("__", 1)[0]
    # The fetcher replaces non-alphanumeric with underscore; reverse that.
    cleaned = re.sub(r"_+", " ", name_part).strip()
    return _normalise_for_hash(cleaned)


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

@dataclass
class Filing:
    """A single legal filing (one eStamp cert). May include multiple PDFs."""
    estamp_cert: str
    estamp_issue_date: str
    estamp_issue_dt: str
    pdf_paths: list[str] = field(default_factory=list)
    pdf_sizes: dict = field(default_factory=dict)   # path → size

    def canonical_pdf(self) -> str:
        """Pick the largest PDF — most supplementary content."""
        if not self.pdf_paths:
            return ""
        return max(self.pdf_paths, key=lambda p: self.pdf_sizes.get(p, 0))


@dataclass
class CandidateGroup:
    signature: str
    name: str
    father: str
    party: str
    constituency: str
    filings: list[Filing] = field(default_factory=list)

    def canonical_filing(self) -> Filing | None:
        """Canonical = latest filing.

        Primary signal: **eStamp cert number** (numeric portion). SHCIL
        certificates are issued sequentially within a state, so a numerically
        larger cert was issued later. This is more reliable than the parsed
        issue date because the date OCR sometimes fails on rougher scans.

        Tie-breaker: largest TOTAL PDF bytes across the filing — a longer
        document usually means the more complete supplementary set.
        """
        if not self.filings:
            return None
        return max(self.filings, key=lambda f: (
            _cert_sort_key(f.estamp_cert),
            sum(f.pdf_sizes.values()),
        ))


def _cert_sort_key(cert: str) -> str:
    """eStamp cert numbers look like 'IN-DL19131279062636X'. Strip the
    state prefix and trailing alpha so we sort on the numeric core."""
    if not cert:
        return ""
    # Keep digits only; lexicographic order matches numeric order because
    # all certs from a state have the same digit count.
    return "".join(c for c in cert if c.isdigit())


def dedup(records: list[PDFRecord]) -> dict:
    """Group records → candidates → filings."""
    candidates: dict[str, CandidateGroup] = {}
    unclassified: list[dict] = []

    for r in records:
        sig = _signature_for(r)
        if not sig:
            unclassified.append({"pdf_path": r.pdf_path, "notes": r.notes,
                                 "estamp_cert": r.estamp_cert})
            continue
        cg = candidates.setdefault(sig, CandidateGroup(
            signature=sig,
            name=r.candidate_name,
            father=r.candidate_father,
            party=r.candidate_party,
            constituency=r.candidate_constituency,
        ))
        # Find or create the Filing for this eStamp cert
        f = next((x for x in cg.filings if x.estamp_cert == r.estamp_cert), None)
        if f is None:
            f = Filing(
                estamp_cert=r.estamp_cert,
                estamp_issue_date=r.estamp_issue_date,
                estamp_issue_dt=r.estamp_issue_dt,
            )
            cg.filings.append(f)
        f.pdf_paths.append(r.pdf_path)
        f.pdf_sizes[r.pdf_path] = r.file_size

    # Render to JSON-friendly output
    out_candidates = []
    for sig, cg in candidates.items():
        canonical = cg.canonical_filing()
        out_candidates.append({
            "signature": sig,
            "name": cg.name,
            "father": cg.father,
            "party": cg.party,
            "constituency": cg.constituency,
            "filings": [
                {
                    "estamp_cert": f.estamp_cert,
                    "estamp_issue_date": f.estamp_issue_date,
                    "pdf_count": len(f.pdf_paths),
                    "pdf_paths": f.pdf_paths,
                    "canonical_pdf": f.canonical_pdf(),
                }
                for f in sorted(cg.filings,
                                 key=lambda x: (_cert_sort_key(x.estamp_cert),
                                                sum(x.pdf_sizes.values())),
                                 reverse=True)
            ],
            "canonical_filing_cert": canonical.estamp_cert if canonical else "",
            "canonical_pdf": canonical.canonical_pdf() if canonical else "",
        })

    return {
        "candidate_count": len(candidates),
        "unclassified_count": len(unclassified),
        "candidates": out_candidates,
        "unclassified": unclassified,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_cache(cache_path: Path) -> dict[str, PDFRecord]:
    if not cache_path.exists():
        return {}
    raw = json.loads(cache_path.read_text())
    return {k: PDFRecord(**v) for k, v in raw.items()}


def _is_poisoned(rec: PDFRecord) -> bool:
    """A cache entry is 'poisoned' if extraction silently produced nothing.

    Happens when pdftoppm/tesseract aren't installed: the script catches
    the underlying CalledProcessError, records an empty PDFRecord with
    'no_estamp_cert_found' + 'no_form_26_part_a_found' notes, and saves
    it. Subsequent runs trust the cache and never re-OCR even after the
    tools are installed.

    Drop these on cache load so the next run re-extracts them.
    """
    if rec.estamp_cert:        # we found an eStamp → genuine extraction
        return False
    if rec.candidate_name:     # we parsed Part A → genuine extraction
        return False
    if rec.candidate_party:    # at least some Part A signal
        return False
    return True


def _save_cache(cache_path: Path, cache: dict[str, PDFRecord]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(
        {k: asdict(v) for k, v in cache.items()}, indent=2, ensure_ascii=False
    ))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", help="Folder of affidavit PDFs")
    ap.add_argument("--out", help="Path to write JSON output (defaults to stdout)")
    ap.add_argument("--cache", default="data/eci/dedup_cache.json",
                    help="Per-PDF extraction cache (so we can resume after timeouts)")
    ap.add_argument("--refresh", action="store_true",
                    help="Ignore cache and re-extract every PDF")
    args = ap.parse_args()

    folder = Path(args.dir).resolve()
    if not folder.is_dir():
        sys.exit(f"Not a directory: {folder}")

    pdfs = sorted(folder.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"No PDFs in {folder}")

    cache_path = Path(args.cache)
    cache: dict[str, PDFRecord] = {} if args.refresh else _load_cache(cache_path)
    if cache:
        # Drop poisoned entries from prior runs where OCR silently failed.
        poisoned_keys = [k for k, v in cache.items() if _is_poisoned(v)]
        if poisoned_keys:
            for k in poisoned_keys:
                del cache[k]
            print(f"Dropped {len(poisoned_keys)} empty/poisoned cache entries "
                  f"(likely from a prior run before pdftoppm/tesseract were "
                  f"installed). Re-extracting these.", file=sys.stderr)
        print(f"Loaded cache: {len(cache)} valid prior extractions from {cache_path}",
              file=sys.stderr)

    print(f"Processing {len(pdfs)} PDFs (this can take ~10 seconds per file)...",
          file=sys.stderr)
    records = []
    for i, pdf in enumerate(pdfs, 1):
        key = str(pdf)
        if key in cache:
            print(f"  [{i}/{len(pdfs)}] {pdf.name}  (cached)", file=sys.stderr)
            records.append(cache[key])
            continue
        print(f"  [{i}/{len(pdfs)}] {pdf.name}", file=sys.stderr)
        rec = extract_pdf(pdf)
        cache[key] = rec
        records.append(rec)
        _save_cache(cache_path, cache)  # checkpoint after each PDF

    grouped = dedup(records)
    output = json.dumps(grouped, indent=2, ensure_ascii=False)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(output)
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
