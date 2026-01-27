# pl_pdf_invoice_extractor.py
# -*- coding: utf-8 -*-
import re
import csv
import sys
import argparse
from pathlib import Path
from typing import List, Dict
from PyPDF2 import PdfReader
import datetime as dt

DEC_RE = re.compile(r'^\d{1,3}(?:[\.\s]\d{3})*,\d{2}$|^\d+,\d{2}$')

def read_pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    pages = [p.extract_text() or "" for p in reader.pages]
    return "\n".join(pages)

def parse_invoice_text(text: str) -> List[Dict]:
    """
    Heuristika prispôsobená faktúram Paul-Lange:
    - prvý token = SČM (A-Z0-9),
    - posledné 2 decimal tokeny = unit_price, line_total,
    - množstvo = celé číslo tesne pred týmito dvoma.
    """
    items = []
    lines = [ln.strip() for ln in text.splitlines()]
    for ln in lines:
        if ln.count(",") < 2:
            continue
        tokens = [t for t in re.split(r'\s+', ln) if t]
        if len(tokens) < 6:
            continue
        # nájdi posledné 2 "ceny"
        price_idx = [i for i,t in enumerate(tokens) if DEC_RE.match(t)]
        if len(price_idx) < 2:
            continue
        uidx, tidx = price_idx[-2], price_idx[-1]
        if tidx != uidx + 1:
            continue
        if uidx - 1 < 0 or not re.fullmatch(r'\d+', tokens[uidx-1]):
            continue
        qty = int(tokens[uidx-1])
        unit_price = tokens[uidx]
        line_total = tokens[tidx]

        code = tokens[0]
        if not re.fullmatch(r'[A-Z0-9]+', code):
            code = re.sub(r'[^A-Za-z0-9]', '', code).upper()
            if not code:
                continue

        # prvá decimálna cena za "MOC" po jednotke - od nej je názov (mäkká heuristika)
        first_price_idx = None
        for i in range(2, len(tokens)):
            if DEC_RE.match(tokens[i]):
                first_price_idx = i
                break
        if first_price_idx is None:
            continue

        name_tokens = tokens[first_price_idx+1:uidx-1]  # pred (VAT?) a qty
        # odstráň trailing integer (VAT) na konci názvu
        while name_tokens and re.fullmatch(r'\d{1,3}', name_tokens[-1]):
            name_tokens.pop()
        name = " ".join(name_tokens).strip()

        items.append({
            "SCM": code,
            "PRODUCT_CODE": f"PL-{code}",
            "QTY": qty,
            "UNIT_PRICE": unit_price,
            "LINE_TOTAL": line_total,
            "NAME": name,
        })
    return items

def write_csv(path: Path, rows: List[Dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols = ["SCM","PRODUCT_CODE","QTY","UNIT_PRICE","LINE_TOTAL","NAME"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

def process_one(pdf_path: Path, out_dir: Path) -> Path:
    text = read_pdf_text(pdf_path)
    items = parse_invoice_text(text)
    out = out_dir / (pdf_path.stem + "_items.csv")
    write_csv(out, items)
    print(f"[OK] {pdf_path.name}: {len(items)} items → {out}")
    return out

def main():
    ap = argparse.ArgumentParser(description="Extract line items from Paul-Lange PDF invoices")
    ap.add_argument("--in", dest="inp", required=True, help="PDF file or directory")
    ap.add_argument("--out-dir", default="faktury_parsed", help="Output directory for per-invoice CSVs")
    ap.add_argument("--merge-out", default="", help="Optional merged CSV filename")
    args = ap.parse_args()

    src = Path(args.inp)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pdfs = []
    if src.is_file():
        pdfs = [src]
    else:
        pdfs = sorted(p for p in src.glob("*.pdf"))

    merged_rows: List[Dict] = []
    for p in pdfs:
        out = process_one(p, out_dir)
        # read back for merge
        with open(out, newline="", encoding="utf-8") as f:
            rdr = csv.DictReader(f)
            merged_rows.extend(rdr)

    if args.merge_out:
        merge_path = Path(args.merge_out)
        # ak nie je absolútna cesta, ulož do out_dir
        if not merge_path.parent.exists():
            merge_path = out_dir / args.merge_out
        write_csv(merge_path, merged_rows)
        print(f"[OK] Merged CSV → {merge_path} ({len(merged_rows)} rows)")

if __name__ == "__main__":
    main()

