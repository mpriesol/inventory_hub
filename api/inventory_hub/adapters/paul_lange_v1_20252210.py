from __future__ import annotations
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime
import pandas as pd
import unicodedata

from ..utils import upgates_output_names  # noqa: F401


# ----------------- helpers -----------------

def _norm(s: str) -> str:
    """lower, strip, bez diakritiky, NBSP -> space"""
    s = str(s).replace("\u00A0", " ").strip().lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s

def _find_col(df, candidates: list[str]) -> str | None:
    """Najdi stĺpec podľa kandidátov; najprv presná zhoda po _norm, inak partial match."""
    norm_map = { _norm(c): c for c in df.columns }
    for cand in candidates:
        n = _norm(cand)
        if n in norm_map:
            return norm_map[n]
    # fallback: partial
    for c in df.columns:
        if any(_norm(cand) in _norm(c) for cand in candidates):
            return c
    return None

def _read_csv_smart(path: Path):
    encodings = ["utf-8-sig", "cp1250", "latin-1"]
    seps = [",", ";", "\t", "|"]
    last_err = None
    for enc in encodings:
        for sep in seps:
            try:
                df = pd.read_csv(
                    path,
                    encoding=enc,
                    sep=sep,
                    dtype=str,                # nestratíme nuly a kódy
                    on_bad_lines="skip"       # vynechá rozbité riadky
                )
                if df.shape[1] > 1:  # oddeľovač sedí
                    return df
            except Exception as e:
                last_err = e
                continue
    if last_err:
        raise last_err
    raise ValueError(f"Cannot parse CSV: {path}")

def _clean_headers_inplace(df: pd.DataFrame) -> None:
    """Odstráni hranaté zátvorky okolo názvov stĺpcov (Upgates) a otrimuje."""
    new_cols = []
    for c in df.columns:
        s = str(c).replace("\u00A0", " ").strip()
        if s.startswith("[") and s.endswith("]"):
            s = s[1:-1]
        new_cols.append(s)
    df.columns = new_cols
def _meta_contains(meta: str | float | None, token: str) -> bool:
    if meta is None or (isinstance(meta, float) and pd.isna(meta)):
        return False
    return str(meta).find(token) != -1

def _meta_append(meta: str | float | None, token: str) -> str:
    if not meta or (isinstance(meta, float) and pd.isna(meta)) or str(meta).strip() == "":
        return token
    s = str(meta).strip()
    # jednoduchý oddelovač „; “
    if token in s:
        return s
    return f"{s}; {token}"
# Upgates hlavičky pre výstup
# Upgates hlavičky pre výstup (idempotentné updates EXISTING)
UPGATES_EXPORT_HEADER_MAP_EXISTING = {
    "PRODUCT_CODE": '[PRODUCT_CODE]',
    "STOCK": '[STOCK]',
    "AVAILABILITY": '[AVAILABILITY]',
    "META_stock_updated_by_invoices": '[META "stock_updated_by_invoices"]',
}
UPGATES_EXPORT_HEADER_MAP_NEW = {
    "PRODUCT_CODE": '[PRODUCT_CODE]',
    "META_validation_required": '[META "validation_required"]',
    "META_original_product_code": '[META "original_product_code"]',
    "NEW_YN": '[NEW_YN]',
    "SPECIAL_YN": '[SPECIAL_YN]',
    "SELLOUT_YN": '[SELLOUT_YN]',
    "LABEL_AKCIA_YN": '[LABEL_ACTIVE_YN "Akcia"]',
    "LABEL_VYPREDAJ_YN": '[LABEL_ACTIVE_YN "Výpredaj"]',
    "LABEL_ODPORUCANE_YN": '[LABEL_ACTIVE_YN "Odporúčané"]',
    "LABEL_TIP_YN": '[LABEL_ACTIVE_YN "Tip"]',
    "LABEL_SEZONNE_YN": '[LABEL_ACTIVE_YN "Sezónne"]',
    "PRICE_WITH_VAT_Predvolene": '[PRICE_WITH_VAT "Predvolené"]',
    "IMAGES": '[IMAGES]',
}

def _apply_upgates_headers(df: pd.DataFrame | None, mapping: dict) -> pd.DataFrame | None:
    if df is None or df.empty:
        return df
    return df.rename(columns=mapping)


# ----------------- main -----------------

def process_invoice(
    supplier_base: Path,
    shop_export_csv: Path,
    invoice_csv: Path,
    as_of: Optional[datetime] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    as_of = as_of or datetime.now()

    # 1) načítaj vstupy robustne
    shop_df = _read_csv_smart(shop_export_csv)
    inv_df  = _read_csv_smart(invoice_csv)

    # 2) očisti hlavičky (len pre čítanie)
    _clean_headers_inplace(shop_df)
    _clean_headers_inplace(inv_df)
    shop_df.columns = shop_df.columns.str.strip()
    inv_df.columns  = inv_df.columns.str.strip()

    # 3) nájdi kľúčové stĺpce vo faktúre
    inv_code_col = _find_col(inv_df, ["SČM", "SCM"])
    inv_qty_col  = _find_col(inv_df, ["Množstvo", "Mnozstvo", "Počet kusov", "Pocet kusov", "ks"])
    if not inv_code_col or not inv_qty_col:
        raise ValueError(f"Invoice required columns not found. Got: {list(inv_df.columns)}")

    # 4) množstvo -> číslo
    inv_df[inv_qty_col] = (
        inv_df[inv_qty_col].astype(str)
        .str.replace("\u00A0", "", regex=False)
        .str.replace(" ", "", regex=False)
        .str.replace(",", ".", regex=False)
    )
    inv_df[inv_qty_col] = pd.to_numeric(inv_df[inv_qty_col], errors="coerce").fillna(0).round().astype(int)

    # 5) faktúra -> PRODUCT_CODE
    inv_df["PRODUCT_CODE"] = "PL-" + inv_df[inv_code_col].astype(str).str.strip()

    # 6) shop export musí mať PRODUCT_CODE
    prod_col = _find_col(shop_df, ["PRODUCT_CODE", "[PRODUCT_CODE]", "product_code", "CODE", "ProductCode"])
    if not prod_col:
        raise ValueError(f"Shop export is missing PRODUCT_CODE column. Got: {list(shop_df.columns)}")
    if prod_col != "PRODUCT_CODE":
        shop_df = shop_df.rename(columns={prod_col: "PRODUCT_CODE"})

    # 7) match / unmatched
    merged = inv_df.merge(shop_df[["PRODUCT_CODE"]], on="PRODUCT_CODE", how="left", indicator=True)
    matched   = merged[merged["_merge"] == "both"].copy()
    unmatched = merged[merged["_merge"] == "left_only"].copy()

    # 8) idempotentné EXISTING: spočítaj inkrementy a pripočítaj k current STOCK, zapíš META
    incr = matched[["PRODUCT_CODE", inv_qty_col]].copy().rename(columns={inv_qty_col: "STOCK_INCREMENT"})
    incr = incr.groupby("PRODUCT_CODE", as_index=False)["STOCK_INCREMENT"].sum()

    stock_col = _find_col(shop_df, ["STOCK", "[STOCK]"]) or "STOCK"
    if stock_col not in shop_df.columns:
        shop_df[stock_col] = 0
    meta_col  = _find_col(shop_df, ['META "stock_updated_by_invoices"', '[META "stock_updated_by_invoices"]', 'stock_updated_by_invoices']) \
                or 'META "stock_updated_by_invoices"'
    if meta_col not in shop_df.columns:
        shop_df[meta_col] = ""

    base = shop_df[["PRODUCT_CODE", stock_col, meta_col]].copy().rename(
        columns={stock_col: "STOCK", meta_col: "META_stock_updated_by_invoices"}
    )

    invoice_id = Path(invoice_csv).stem  # napr. F2025060682

    if not incr.empty:
        base = base.merge(incr, on="PRODUCT_CODE", how="left")
        base["STOCK_INCREMENT"] = base["STOCK_INCREMENT"].fillna(0).astype(int)

        already = base["META_stock_updated_by_invoices"].apply(lambda x: _meta_contains(x, invoice_id))
        base.loc[already, "STOCK_INCREMENT"] = 0  # idempotencia: už sme naskladnili

        base["STOCK"] = (pd.to_numeric(base["STOCK"], errors="coerce").fillna(0).astype(int)
                         + base["STOCK_INCREMENT"].astype(int))
        base["META_stock_updated_by_invoices"] = base.apply(
            lambda r: _meta_append(r["META_stock_updated_by_invoices"], invoice_id)
                      if r["STOCK_INCREMENT"] != 0 else r["META_stock_updated_by_invoices"],
            axis=1
        )
    else:
        base["STOCK_INCREMENT"] = 0

    existing = base.loc[base["STOCK_INCREMENT"] > 0, ["PRODUCT_CODE", "STOCK"]].copy()
    existing["AVAILABILITY"] = "Na sklade"
    existing = existing.merge(base[["PRODUCT_CODE", "META_stock_updated_by_invoices"]], on="PRODUCT_CODE", how="left")

    # 9) NEW – zatiaľ prázdny skeleton (doplníme z feeds_converted)
    new = pd.DataFrame(columns=list(UPGATES_EXPORT_HEADER_MAP_NEW.keys()))

    # 10) UNMATCHED – auditný výstup
    cols = ["PRODUCT_CODE"]
    if inv_code_col in unmatched.columns:
        cols.append(inv_code_col)
    unmatched_out = unmatched[cols].copy()
    unmatched_out["REASON"] = "Not in shop export"

    # 11) premenuj hlavičky EXISTING/NEW do Upgates formátu (s hranatými zátvorkami)
    existing = _apply_upgates_headers(existing, UPGATES_EXPORT_HEADER_MAP_EXISTING)
    new      = _apply_upgates_headers(new,      UPGATES_EXPORT_HEADER_MAP_NEW)

    return existing, new, unmatched_out
