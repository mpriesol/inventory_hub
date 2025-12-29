
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

# Upgates hlavičky pre výstup
UPGATES_EXPORT_HEADER_MAP_EXISTING = {
    "PRODUCT_CODE": '[PRODUCT_CODE]',
    "AVAILABILITY": '[AVAILABILITY]',
    # interné pole – v ďalšom kroku ho prepočítame na [STOCK]; zatiaľ ako META
    "STOCK_INCREMENT": '[META "stock_increment"]',
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

    # --- robustné načítanie (UTF-8/CP1250; , ; \t |) ---
    shop_df = _read_csv_smart(shop_export_csv)
    inv_df  = _read_csv_smart(invoice_csv)

    # očisti hlavičky (Upgates má často [PRODUCT_CODE] atď.)
    _clean_headers_inplace(shop_df)
    _clean_headers_inplace(inv_df)
    shop_df.columns = shop_df.columns.str.strip()
    inv_df.columns  = inv_df.columns.str.strip()

    # Faktúra: SČM + množstvo (CSV: Počet kusov, PDF: Množstvo) – tolerujeme bez diakritiky
    inv_code_col = _find_col(inv_df, ["SČM", "SCM"])
    inv_qty_col  = _find_col(inv_df, ["Množstvo", "Mnozstvo", "Počet kusov", "Pocet kusov", "ks"])
    if not inv_code_col or not inv_qty_col:
        raise ValueError(f"Invoice required columns not found. Got: {list(inv_df.columns)}")

    # Množstvo -> číslo
    inv_df[inv_qty_col] = (
        inv_df[inv_qty_col]
        .astype(str)
        .str.replace("\u00A0", "", regex=False)  # NBSP
        .str.replace(" ", "", regex=False)
        .str.replace(",", ".", regex=False)
    )
    inv_df[inv_qty_col] = (
        pd.to_numeric(inv_df[inv_qty_col], errors="coerce")
          .fillna(0)
          .round()
          .astype(int)
    )

    # PRODUCT_CODE podľa pravidla
    inv_df["PRODUCT_CODE"] = "PL-" + inv_df[inv_code_col].astype(str).str.strip()

    # Shop export: zabezpeč PRODUCT_CODE (po očistení je bez [])
    prod_col = _find_col(shop_df, ["PRODUCT_CODE", "[PRODUCT_CODE]", "product_code", "CODE", "ProductCode"])
    if not prod_col:
        raise ValueError(f"Shop export is missing PRODUCT_CODE column. Got: {list(shop_df.columns)}")
    if prod_col != "PRODUCT_CODE":
        shop_df = shop_df.rename(columns={prod_col: "PRODUCT_CODE"})

    # Match
    merged = inv_df.merge(
        shop_df[["PRODUCT_CODE"]],
        on="PRODUCT_CODE",
        how="left",
        indicator=True,
    )
    matched   = merged[merged["_merge"] == "both"].copy()
    unmatched = merged[merged["_merge"] == "left_only"].copy()

    # EXISTING
    existing = matched[["PRODUCT_CODE", inv_qty_col]].copy()
    existing = existing.rename(columns={inv_qty_col: "STOCK_INCREMENT"})
    existing["AVAILABILITY"] = "Na sklade"

    # NEW – zatiaľ prázdne, doplníme po zapojení feeds_converted
    new = pd.DataFrame(columns=list(UPGATES_EXPORT_HEADER_MAP_NEW.keys()))

    # UNMATCHED – pre kontrolu
    unmatched_out = unmatched[["PRODUCT_CODE", inv_code_col]].copy()
    unmatched_out["REASON"] = "Not in shop export"

    # Premenovať výstupy do Upgates hlavičiek (s hranatými zátvorkami)
    existing = _apply_upgates_headers(existing, UPGATES_EXPORT_HEADER_MAP_EXISTING)
    new      = _apply_upgates_headers(new,      UPGATES_EXPORT_HEADER_MAP_NEW)

    return existing, new, unmatched_out
