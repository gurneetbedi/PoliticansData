"""One-off: re-parse the canonical Kejriwal PDF on pages 2-8 (where Part A and
Part B abstracts live) plus pages 15-17 (market value area) and emit a JSON
summary. Used to verify the ECI extraction still matches the DB."""
from __future__ import annotations
import json, subprocess, sys, tempfile
from pathlib import Path

sys.path.insert(0, "scripts")
from parse_eci_affidavit import (
    ocr_page, find_part_a_page, parse_part_a, find_part_b_page, parse_part_b,
    scan_all_pages_for_market_value, ParsedAffidavit,
)

PDF = Path("Affadivit/Affidavit-1780926334.pdf").resolve()
import os
_env_pages = os.environ.get("PAGES")
if _env_pages:
    PAGES_TO_OCR = [int(x) for x in _env_pages.split(",")]
else:
    PAGES_TO_OCR = list(range(2, 13)) + [15, 16, 17]

def render_pages(pdf: Path, pages: list[int], wd: Path) -> dict[int, Path]:
    out = {}
    for p in pages:
        subprocess.run(
            ["pdftoppm", "-r", "200", "-f", str(p), "-l", str(p),
             str(pdf), str(wd / f"page-{p:03d}"), "-png"],
            check=True, capture_output=True,
        )
        png = next(wd.glob(f"page-{p:03d}-*.png"), None)
        if png:
            out[p] = png
    return out


def main():
    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp)
        print(f"Rendering pages {PAGES_TO_OCR} ...", file=sys.stderr)
        rendered = render_pages(PDF, PAGES_TO_OCR, wd)
        print(f"OCRing {len(rendered)} pages ...", file=sys.stderr)
        pages_text: list[str] = []
        # Build a sparse pages_text list indexed by original page number
        # The parser expects 0-indexed sequential list; we'll provide a
        # padded list where missing pages are empty strings.
        max_p = max(rendered.keys())
        for p in range(1, max_p + 1):
            if p in rendered:
                pages_text.append(ocr_page(rendered[p]))
            else:
                pages_text.append("")

        result = ParsedAffidavit(source_pdf=str(PDF), pages_ocrd=len(pages_text))

        idx_a = find_part_a_page(pages_text)
        if idx_a is not None:
            parse_part_a(pages_text[idx_a], result)

        idx_b = find_part_b_page(pages_text)
        if idx_b is not None:
            parse_part_b(pages_text[idx_b], result)

        scan_all_pages_for_market_value(pages_text, result)

        def _sum_or_none(*xs):
            xs = [x for x in xs if x is not None]
            return sum(xs) if xs else None

        movable_total = _sum_or_none(result.movable_assets_self_inr,
                                       result.movable_assets_spouse_inr)
        immovable_total = _sum_or_none(result.immovable_assets_self_inr,
                                         result.immovable_assets_spouse_inr)
        total_assets = _sum_or_none(movable_total, immovable_total)

        out = {
            "name": result.name,
            "father_name": result.father_name,
            "party": result.party,
            "constituency": result.constituency,
            "age": result.age,
            "address": result.address,
            "phone": result.phone,
            "email": result.email,
            "total_pending_criminal_cases": result.total_pending_criminal_cases,
            "total_convictions": result.total_convictions,
            "movable_assets_self_inr": result.movable_assets_self_inr,
            "movable_assets_spouse_inr": result.movable_assets_spouse_inr,
            "movable_assets_total_inr": movable_total,
            "immovable_assets_self_inr": result.immovable_assets_self_inr,
            "immovable_assets_spouse_inr": result.immovable_assets_spouse_inr,
            "immovable_assets_total_inr": immovable_total,
            "total_assets_inr": total_assets,
            "part_a_page": idx_a + 1 if idx_a is not None else None,
            "part_b_page": idx_b + 1 if idx_b is not None else None,
            "pending_cases_detail_count": len(result.pending_criminal_cases),
        }
        Path("data/eci/kejriwal_eci_parse.json").write_text(json.dumps(out, indent=2))
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
