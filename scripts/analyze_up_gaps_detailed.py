"""For each UP 2022 gap candidate, show the exact pipeline state:
  - PDF: exists? size?
  - Cloud Vision: OCR ran? total chars? Devanagari chars? English chars?
  - Gemini: extraction exists? _raw? has wealth? has name+const?
  - Root-cause diagnosis
"""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data/eci/raw_pdfs/uttarpradesh-2022/raw_pdfs"
PP  = ROOT / "data/eci/for_ai/preprocessed_uttarpradesh_2022"
LX  = ROOT / "data/eci/for_ai/llm_extracted/uttarpradesh_2022"
CSV = ROOT / "data/reports/gaps_uttarpradesh_2022.csv"


def diagnose(row):
    pdf = row["pdf"]
    stem = pdf[:-4]
    result = {"candidate": row["candidate"], "const": row["constituency"],
              "party": row["party"]}

    pdf_path = RAW / pdf
    if not pdf_path.exists():
        result["issue"] = "PDF MISSING"
        return result
    result["pdf_size_kb"] = pdf_path.stat().st_size // 1024

    pp_path = PP / (stem + ".json")
    if not pp_path.exists():
        result["issue"] = "No Cloud Vision run"
        return result
    try:
        pp = json.loads(pp_path.read_text())
    except Exception:
        result["issue"] = "Cloud Vision JSON unreadable"
        return result
    if pp.get("corrupt") or pp.get("skipped_corrupt"):
        result["issue"] = "Cloud Vision: PDF corrupt (Vision refused)"
        return result
    text = "\n".join(p.get("text", "") for p in pp.get("pages", []))
    non_ws = len("".join(text.split()))
    dev = sum(1 for c in text if "ऀ" <= c <= "ॿ")
    en  = sum(1 for c in text if c.isalpha() and c.isascii())
    result.update({"ocr_chars": non_ws, "devanagari": dev, "english": en})

    if non_ws < 200:
        result["issue"] = "Cloud Vision: empty OCR (<200 chars)"
        return result

    lx_path = LX / (stem + ".json")
    if not lx_path.exists():
        result["issue"] = "OCR OK, no Gemini extraction file"
        return result
    try:
        g = json.loads(lx_path.read_text())
    except Exception:
        result["issue"] = "Gemini JSON unreadable"
        return result
    ext = g.get("extraction") or {}
    if "_raw" in ext:
        raw_len = len(ext["_raw"] or "")
        result["issue"] = f"Gemini: truncated _raw ({raw_len} chars, max-token cutoff)"
        return result

    name = (ext.get("identity") or {}).get("name_in_english") or ""
    const = (ext.get("political") or {}).get("constituency_name") or ""
    am = (ext.get("assets_movable") or {}).get("total_movable_assets_inr")
    ai = (ext.get("assets_immovable") or {}).get("total_immovable_assets_inr")

    if not name and not const:
        result["issue"] = "Gemini: both name+const empty (returned junk)"
    elif not am and not ai:
        if dev > en * 3:
            result["issue"] = (
                f"Gemini: NO wealth extracted (Devanagari-heavy: "
                f"{dev} dev / {en} en — probably parsing Hindi tables failed)"
            )
        else:
            result["issue"] = (
                f"Gemini: NO wealth extracted (mixed OCR: "
                f"{dev} dev / {en} en — may be poor OCR quality)"
            )
    else:
        result["issue"] = "Wealth extracted (why is this still a gap?)"
    return result


rows = list(csv.DictReader(CSV.open()))
print(f"UP 2022 — {len(rows)} missing candidates. Per-candidate diagnosis:\n")

for i, row in enumerate(sorted(rows, key=lambda x: x['constituency']), 1):
    d = diagnose(row)
    print(f"{i:2d}. {d['candidate']}")
    print(f"    {d['const']} · {d['party']}")
    if 'ocr_chars' in d:
        print(f"    OCR: {d['ocr_chars']:,} chars ({d['devanagari']:,} dev, {d['english']:,} en)")
    print(f"    ⇒ {d['issue']}")
    print()
