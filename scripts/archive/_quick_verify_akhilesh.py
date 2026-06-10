"""Parse all 3 Akhilesh Pati Tripathi PDFs on the pages we know matter
and compare the headline numbers against politrack.db."""
import json, subprocess, sys, tempfile
from pathlib import Path

sys.path.insert(0, "scripts")
from parse_eci_affidavit import (
    ocr_page, find_part_a_page, parse_part_a,
    find_part_b_page, parse_part_b,
    scan_all_pages_for_market_value, ParsedAffidavit,
)

import os
# Allow CLI override for quick iteration during parser tweaking
_pdfs_env = os.environ.get("PDFS")
if _pdfs_env:
    PDFS = [Path(p) for p in _pdfs_env.split(",")]
else:
    PDFS = [
        Path("/tmp/akhilesh/AKHILESH_PATI_TRIPATHI__1666.pdf"),
    ]
# Tight page set — at 300 DPI we can afford ~5 pages per PDF in 45s
PAGES = [2, 3, 22, 26, 27]


def parse_one(pdf: Path) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        wd = Path(tmp)
        rendered: dict[int, Path] = {}
        for p in PAGES:
            subprocess.run(
                ["pdftoppm", "-r", "300", "-f", str(p), "-l", str(p),
                 str(pdf), str(wd / f"page-{p:03d}"), "-png"],
                check=True, capture_output=True,
            )
            png = next(wd.glob(f"page-{p:03d}-*.png"), None)
            if png:
                rendered[p] = png

        max_p = max(rendered.keys())
        pages_text: list[str] = []
        for p in range(1, max_p + 1):
            pages_text.append(ocr_page(rendered[p]) if p in rendered else "")

        result = ParsedAffidavit(source_pdf=str(pdf), pages_ocrd=len(pages_text))
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
    return {
        "file": pdf.name,
        "size_mb": round(pdf.stat().st_size / 1e6, 1),
        "name": result.name,
        "party": result.party,
        "constituency": result.constituency,
        "age": result.age,
        "pending_criminal_cases": result.total_pending_criminal_cases,
        "convictions": result.total_convictions,
        "movable_total_inr": movable_total,
        "immovable_total_inr": immovable_total,
        "total_assets_inr": _sum_or_none(movable_total, immovable_total),
        "part_a_page": (idx_a + 1) if idx_a is not None else None,
        "part_b_page": (idx_b + 1) if idx_b is not None else None,
    }


def fmt_rs(n):
    if n is None:
        return "—"
    return f"₹{n:,}"


# DB row for comparison
DB = {
    "name": "Akhilesh Pati Tripathi",
    "party": "AAP",
    "constituency": "MODEL TOWN",
    "total_assets_inr": 6724979,
    "total_liabilities_inr": 400926,
    "criminal_cases_count": 10,
    "education": "Post Graduate",
}

results = []
for pdf in PDFS:
    print(f"\n=== {pdf.name} ===", file=sys.stderr)
    r = parse_one(pdf)
    results.append(r)
    for k, v in r.items():
        print(f"  {k}: {v}", file=sys.stderr)

def col(r, k, w=16):
    v = r.get(k)
    if v is None: return "—".ljust(w)
    if k in ("total_assets_inr", "movable_total_inr", "immovable_total_inr"):
        return fmt_rs(v).ljust(w)
    return str(v)[:w].ljust(w)

print("\n" + "=" * (24 + 18 * (len(results)+1)))
hdr_cells = [f"{'DB (myneta)':16s}"] + [f"{Path(r['file']).stem.split('__')[-1][:16]:16s}" for r in results]
print(f"{'FIELD':22s}  " + "  ".join(hdr_cells))
print("-" * (24 + 18 * (len(results)+1)))
rows = [
    ("name", DB['name'][:16].ljust(16)),
    ("party", DB['party'][:16].ljust(16)),
    ("constituency", DB['constituency'][:16].ljust(16)),
    ("pending_criminal_cases", str(DB['criminal_cases_count']).ljust(16)),
    ("total_assets_inr", fmt_rs(DB['total_assets_inr']).ljust(16)),
    ("movable_total_inr", "—".ljust(16)),
    ("immovable_total_inr", "—".ljust(16)),
    ("size_mb", "—".ljust(16)),
]
for k, db_val in rows:
    cells = [db_val] + [col(r, k) for r in results]
    label = k if k != "pending_criminal_cases" else "pending crim cases"
    label = label if k != "total_assets_inr" else "total assets"
    label = label if k != "movable_total_inr" else "movable subtotal"
    label = label if k != "immovable_total_inr" else "immovable subtotal"
    label = label if k != "size_mb" else "file size"
    print(f"{label:22s}  " + "  ".join(cells))

Path("/tmp/akhilesh/parsed_compare.json").write_text(
    json.dumps({"db": DB, "eci": results}, indent=2)
)
