
from __future__ import annotations
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Any
from datetime import datetime
import re
import csv
import unicodedata
import pandas as pd

import httpx
from lxml import etree
from decimal import Decimal, ROUND_HALF_UP

# ========================
# Helpers (generic)
# ========================

def _norm(s: str) -> str:
    s = str(s).replace("\u00A0", " ").strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    s = s.lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = s.replace(" ", "").replace("_", "")
    return s

def _find_col(df, candidates: List[str]) -> str | None:
    norm_map = { _norm(c): c for c in df.columns }
    for cand in candidates:
        n = _norm(cand)
        if n in norm_map:
            return norm_map[n]
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
                df = pd.read_csv(path, encoding=enc, sep=sep, dtype=str, on_bad_lines="skip")
                if df.shape[1] > 1:
                    return df
            except Exception as e:
                last_err = e
                continue
    if last_err:
        raise last_err
    raise ValueError(f"Cannot parse CSV: {path}")

def _clean_headers_inplace(df: pd.DataFrame) -> None:
    new_cols = []
    for c in df.columns:
        s = str(c).replace("\u00A0", " ").strip()
        if s.startswith("[") and s.endswith("]"):
            s = s[1:-1]
        new_cols.append(s)
    df.columns = new_cols

def _to_float(val: str) -> float:
    if val is None:
        return 0.0
    s = str(val).replace("\u00A0", "").strip()
    s = s.replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return 0.0

def _gather_image_columns(df: pd.DataFrame) -> List[str]:
    cols = []
    for c in df.columns:
        n = _norm(c)
        if any(key in n for key in ["img", "image", "obraz", "foto", "picture", "images"]):
            cols.append(c)
    return cols

def _apply_upgates_headers(df: pd.DataFrame | None, mapping: dict) -> pd.DataFrame | None:
    if df is None or df.empty:
        return df
    return df.rename(columns=mapping)

# ========================
# Upgates mappings & pricing
# ========================

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

MANUFACTURER_FACTORS = {
    "SHIMANO": Decimal("0.80"),
    "PRO":     Decimal("0.80"),
    "LAZER":   Decimal("0.90"),
    "LONGUS":  Decimal("0.90"),
    "ELITE":   Decimal("0.90"),
    "MOTOREX": Decimal("0.95"),
    "SCHWALBE": Decimal("0.75"),
}

def d2(x: str | Decimal) -> str:
    return f"{Decimal(str(x).replace(',', '.')).quantize(Decimal('0.01'), ROUND_HALF_UP)}"

def _brand_coeff_decimal(brand: str) -> Decimal:
    if not brand:
        return Decimal("1.00")
    return MANUFACTURER_FACTORS.get(brand.upper(), Decimal("1.00"))

UPGATES_HEADER = [
    "[PRODUCT_CODE]","[VARIANT_YN]","[VARIANT_CODE]","[MAIN_YN]",
    "[ACTIVE_YN]","[ARCHIVED_YN]","[CAN_ADD_TO_BASKET_YN]",
    "[NEW_YN]","[NEW_FROM]","[NEW_TO]","[SPECIAL_YN]","[SPECIAL_FROM]",
    "[SPECIAL_TO]","[SELLOUT_YN]","[SELLOUT_FROM]","[SELLOUT_TO]",
    "[LABEL_ACTIVE_YN „Akcia“]","[LABEL_ACTIVE_FROM „Akcia“]","[LABEL_ACTIVE_TO „Akcia“]",
    "[LABEL_ACTIVE_YN „Novinka“]","[LABEL_ACTIVE_FROM „Novinka“]","[LABEL_ACTIVE_TO „Novinka“]",
    "[LABEL_ACTIVE_YN „Výpredaj“]","[LABEL_ACTIVE_FROM „Výpredaj“]","[LABEL_ACTIVE_TO „Výpredaj“]",
    "[LABEL_ACTIVE_YN „Odporúčané“]","[LABEL_ACTIVE_FROM „Odporúčané“]","[LABEL_ACTIVE_TO „Odporúčané“]",
    "[LABEL_ACTIVE_YN „Tip“]","[LABEL_ACTIVE_FROM „Tip“]","[LABEL_ACTIVE_TO „Tip“]",
    "[LABEL_ACTIVE_YN „Sezónne“]","[LABEL_ACTIVE_FROM „Sezónne“]","[LABEL_ACTIVE_TO „Sezónne“]",
    "[LANGUAGE]","[URL]","[TITLE]","[LONG_DESCRIPTION]","[SHORT_DESCRIPTION]",
    "[SEO_URL]","[SEO_TITLE]","[SEO_DESCRIPTION]","[SUPPLIER_CODE]","[EAN]",
    "[MANUFACTURER]","[AVAILABILITY]","[AVAILABILITY_NOTE]","[STOCK]","[WEIGHT]",
    "[UNIT]","[SHIPMENT_GROUP]","[VAT]","[CATEGORIES]","[IS_PRICES_WITH_VAT_YN]",
    "[PRICE_ORIGINAL „Predvolené“]","[PRODUCT_DISCOUNT „Predvolené“]",
    "[PRODUCT_DISCOUNT_REAL „Predvolené“]","[PRICE_SALE „Predvolené“]",
    "[PRICE_BUY]","[PRICE_COMMON]","[PRICE_WITH_VAT „Predvolené“]",
    "[PRICE_WITHOUT_VAT „Predvolené“]","[PRICES_FORMULAS]","[IMAGES]","[FILES]",
    "[BENEFITS]","[PARAMETER „Farba“]","[PARAMETER „Veľkosť“]","[PARAMETER „Materiál“]",
    "[PARAMETER „Rozmer“]","[PARAMETER „Hmotnosť“]","[PARAMETER „Nosnosť“]",
    "[PARAMETER „Objem“]","[PARAMETER „Výbava“]","[PARAMETER „Počet komôr“]",
    "[PARAMETER „Chrbtový systém“]","[PARAMETER „Určenie“]",
    "[PARAMETER „Výškovo nastaviteľný chrbtový systém“]",
    "[PARAMETER „Odnímateľný bedrový popruh“]",
    "[PARAMETER „Nastaviteľná dĺžka popruhov“]","[RELATED]","[ALTERNATIVE]",
    "[ACCESSORIES]","[GIFTS]","[SETS]","[META „gallery3d“]","[META „cont“]",
    "[META „col“]","[META „product_text1“]","[META „product_text2“]",
    "[META „product_text3“]","[META „categorytext_glami“]",
    "[META „categorytext_heureka“]","[META „product_heureka“]",
    "[META „productname_heureka“]","[META „invoice_info“]","[META „redirect_301“]",
    "[META „variant_table“]","[META „product_tab1“]","[META „product_tab2“]",
    "[STOCK]","[STOCK_POSITION]"
]

def _derive_out_name_from_stem(stem: str) -> str:
    m = re.search(r"(\d{8})", stem)
    if m:
        return f"export_vs_{m.group(1)}_filtered_ready_to_import.csv"
    return f"{stem}_filtered_ready_to_import.csv"

def refresh_feed(supplier_base: Path, feed_config: Dict[str, Any]):
    """
    Načíta Paul-Lange XML (lokálny path alebo URL) a vyrobí Upgates CSV
    v stave 'ready_to_filter' s NÁZVOM ZHODNÝM SO STEMOM XML (iba .csv).
    Príklad:
      XML: feeds/xml/export_v2_20251013.xml
      CSV: feeds/converted/export_v2_20251013.csv
    """
    url = (feed_config or {}).get("url")
    if not url:
        raise RuntimeError("feed_config.url missing")

    method   = (feed_config.get("method") or "GET").upper()
    headers  = dict(feed_config.get("headers") or {})
    params   = dict(feed_config.get("params") or {})
    body     = feed_config.get("body")
    timeout  = float(feed_config.get("timeout") or 60.0)
    verify   = bool(feed_config.get("verify_ssl", True))
    save_raw = bool(feed_config.get("save_raw", True))
    auth_cfg = (feed_config.get("auth") or {})

    # Defaulty (ako v pôvodnom skripte – môžeš presunúť do SupplierConfig.feed)
    DEFAULT_VAT      = int(feed_config.get("vat", 23))
    DEFAULT_CATEGORY = str(feed_config.get("category", "K00090"))
    DEFAULT_PREFIX   = str(feed_config.get("prefix", "PL-"))

    # auth
    auth = None
    t = (auth_cfg.get("type") or "none").lower()
    if t == "basic":
        auth = (auth_cfg.get("username", ""), auth_cfg.get("password", ""))
    elif t == "bearer":
        headers["Authorization"] = f"Bearer {auth_cfg.get('token','')}"
    elif t == "header":
        headers[auth_cfg.get("header_name","Authorization")] = auth_cfg.get("token","")
    elif t == "query":
        params[auth_cfg.get("query_param","token")] = auth_cfg.get("token","")

    xml_dir  = supplier_base / "feeds" / "xml"
    conv_dir = supplier_base / "feeds" / "converted"
    xml_dir.mkdir(parents=True, exist_ok=True)
    conv_dir.mkdir(parents=True, exist_ok=True)

    # ---- načítanie XML + odvodenie 'stem' ----
    p = Path(url)
    if p.exists() and p.is_file():
        content = p.read_bytes()
        stem = p.stem
        raw_xml_path = p if save_raw else (xml_dir / f"{stem}.xml")
        if save_raw and not p.parent.samefile(xml_dir):
            (xml_dir / f"{stem}.xml").write_bytes(content)
    else:
        with httpx.Client(follow_redirects=True, timeout=timeout, verify=verify, auth=auth) as client:
            resp = client.request(method, url, headers=headers, params=params, json=body if method == "POST" else None)
            resp.raise_for_status()
            content = resp.content
        from urllib.parse import urlparse
        import os
        path_name = os.path.basename(urlparse(url).path) or ""
        stem = os.path.splitext(path_name)[0] or f"export_v2_{datetime.now().strftime('%Y%m%d')}"
        raw_xml_path = xml_dir / f"{stem}.xml"
        if save_raw:
            raw_xml_path.write_bytes(content)

    root = etree.fromstring(content)

    # ---- názov CSV presne podľa XML stemu (ready_to_filter obsah) ----
    out_csv = conv_dir / f"{stem}.csv"

    with out_csv.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=UPGATES_HEADER, delimiter=";")
        w.writeheader()

        for item in root.xpath("//SHOPITEM"):
            code = (item.findtext("ITEM_ID", "") or "").strip()
            price_common = Decimal((item.findtext("PRICE_VAT", "0") or "0").replace(",", "."))
            price_buy    = Decimal((item.findtext("PRICE_VOC_VAT", "0") or "0").replace(",", "."))

            manufacturer_raw = (item.findtext("MANUFACTURER", "") or "").strip()
            coef = _brand_coeff_decimal(manufacturer_raw)
            price_with = (price_common * coef)

            base_img = (item.findtext("IMGURL", "") or "").strip()
            extra_imgs = []
            for i in item.xpath("./IMAGES/IMGURL"):
                if i is not None and i.text:
                    extra_imgs.append(i.text.strip())
            imgs = [base_img] + extra_imgs
            img_list = ";".join([u for u in imgs if u])

            # parametre
            param_dict = {}
            for pnode in item.xpath("./DYN_PARAMS/PARAM"):
                desc = pnode.findtext("DESC")
                val  = pnode.findtext("VAL")
                if desc:
                    param_dict[desc] = val or ""
            color = param_dict.get("Farba 1", "") or param_dict.get("Farba", "")
            size  = param_dict.get("Veľkosť", "") or param_dict.get("Velkost", "") or param_dict.get("Veľkosť 1", "")

            row = {col: "" for col in UPGATES_HEADER}  # všetky polia existujú
            row.update({
                "[PRODUCT_CODE]":  f"{DEFAULT_PREFIX}{code}" if code else "",
                "[VARIANT_YN]":    0,
                "[MAIN_YN]":       0,
                "[ACTIVE_YN]":     1,
                "[CAN_ADD_TO_BASKET_YN]": 1,
                "[LANGUAGE]":      "sk",
                "[URL]":           item.findtext("URL", "") or "",
                "[TITLE]":         (item.findtext("PRODUCT", "") or "").strip(),
                "[LONG_DESCRIPTION]": item.findtext("DESCRIPTION", "") or "",
                "[SUPPLIER_CODE]": code,
                "[EAN]":           item.findtext("EAN", "") or "",
                "[MANUFACTURER]":  manufacturer_raw,
                "[STOCK]":         0,
                "[VAT]":           DEFAULT_VAT,
                "[CATEGORIES]":    DEFAULT_CATEGORY,
                "[PRICE_BUY]":     d2(price_buy),
                "[PRICE_COMMON]":  d2(price_common),
                "[PRICE_WITH_VAT „Predvolené“]": d2(price_with),
                "[IMAGES]":        img_list,
                "[PARAMETER „Farba“]":  color or "",
                "[PARAMETER „Veľkosť“]": size or "",
            })
            w.writerow(row)

    # vráť cesty (pre frontend používame /data/… aliasy v main.py)
    return {"raw_path": raw_xml_path, "converted_csv": out_csv}


# ========================
# EXISTING process_invoice (kept same)
# ========================

def process_invoice(
    supplier_base: Path,
    shop_export_csv: Path,
    invoice_csv: Path,
    as_of: Optional[datetime] = None,
):
    from math import isnan
    as_of = as_of or datetime.now()

    shop_df = _read_csv_smart(shop_export_csv)
    inv_df  = _read_csv_smart(invoice_csv)

    _clean_headers_inplace(shop_df)
    _clean_headers_inplace(inv_df)
    shop_df.columns = shop_df.columns.str.strip()
    inv_df.columns  = inv_df.columns.str.strip()

    inv_code_col = _find_col(inv_df, ["SČM", "SCM"])
    inv_qty_col  = _find_col(inv_df, ["Množstvo", "Mnozstvo", "Počet kusov", "Pocet kusov", "ks"])
    if not inv_code_col or not inv_qty_col:
        raise ValueError(f"Invoice required columns not found. Got: {list(inv_df.columns)}")

    inv_df[inv_qty_col] = (
        inv_df[inv_qty_col].astype(str)
        .str.replace("\u00A0", "", regex=False)
        .str.replace(" ", "", regex=False)
        .str.replace(",", ".", regex=False)
    )
    inv_df[inv_qty_col] = pd.to_numeric(inv_df[inv_qty_col], errors="coerce").fillna(0).round().astype(int)

    inv_df["PRODUCT_CODE"] = "PL-" + inv_df[inv_code_col].astype(str).str.strip()

    prod_col = _find_col(shop_df, ["PRODUCT_CODE", "[PRODUCT_CODE]", "product_code", "CODE", "ProductCode"])
    if not prod_col:
        raise ValueError(f"Shop export is missing PRODUCT_CODE column. Got: {list(shop_df.columns)}")
    if prod_col != "PRODUCT_CODE":
        shop_df = shop_df.rename(columns={prod_col: "PRODUCT_CODE"})

    merged = inv_df.merge(shop_df[["PRODUCT_CODE"]], on="PRODUCT_CODE", how="left", indicator=True)
    matched   = merged[merged["_merge"] == "both"].copy()
    unmatched = merged[merged["_merge"] == "left_only"].copy()

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

    invoice_id = Path(invoice_csv).stem

    if not incr.empty:
        base = base.merge(incr, on="PRODUCT_CODE", how="left")
        base["STOCK_INCREMENT"] = base["STOCK_INCREMENT"].fillna(0).astype(int)
        def _contains(meta, token):
            if meta is None or (isinstance(meta, float) and pd.isna(meta)):
                return False
            return str(meta).find(token) != -1
        already = base["META_stock_updated_by_invoices"].apply(lambda x: _contains(x, invoice_id))
        base.loc[already, "STOCK_INCREMENT"] = 0

        base["STOCK"] = (pd.to_numeric(base["STOCK"], errors="coerce").fillna(0).astype(int)
                         + base["STOCK_INCREMENT"].astype(int))
        def _append(meta, token):
            if not meta or (isinstance(meta, float) and pd.isna(meta)) or str(meta).strip() == "":
                return token
            s = str(meta).strip()
            if token in s:
                return s
            return f"{s}; {token}"
        base["META_stock_updated_by_invoices"] = base.apply(
            lambda r: _append(r["META_stock_updated_by_invoices"], invoice_id)
                      if r["STOCK_INCREMENT"] != 0 else r["META_stock_updated_by_invoices"],
            axis=1
        )
    else:
        base["STOCK_INCREMENT"] = 0

    existing = base.loc[base["STOCK_INCREMENT"] > 0, ["PRODUCT_CODE", "STOCK"]].copy()
    existing["AVAILABILITY"] = "Na sklade"
    existing = existing.merge(base[["PRODUCT_CODE", "META_stock_updated_by_invoices"]], on="PRODUCT_CODE", how="left")

    new = pd.DataFrame(columns=list(UPGATES_EXPORT_HEADER_MAP_NEW.keys()))

    conv = supplier_base / "feeds" / "converted"
    latest_feed = None
    if conv.exists():
        cands = sorted([p for p in conv.glob("*.csv") if p.is_file()], key=lambda p: p.stat().st_mtime)
        latest_feed = cands[-1] if cands else None

    if latest_feed is not None and not unmatched.empty:
        feed_df = _read_csv_smart(latest_feed)
        _clean_headers_inplace(feed_df)
        feed_df.columns = feed_df.columns.str.strip()

        feed_code_col = _find_col(feed_df, ["SČM", "SCM", "Kod", "Kód", "Catalog", "ProductCode", "CatalogCode", "SUPPLIER_CODE"])
        brand_col     = _find_col(feed_df, ["Značka", "Znacka", "Brand", "Výrobca", "Vyrobca", "Manufacturer", "MANUFACTURER"])
        moc_col       = _find_col(feed_df, ["MOC", "MOC s DPH", "Doporučená MOC", "Doporucena MOC", "MSRP", "Cena MOC", "Cena MOC s DPH", "MOC EUR", "MOC_EUR", "PRICE_COMMON"])

        img_cols = _gather_image_columns(feed_df)

        if feed_code_col:
            cand = unmatched[[inv_code_col, "PRODUCT_CODE"]].drop_duplicates()
            cand = cand.merge(feed_df, left_on=inv_code_col, right_on=feed_code_col, how="left", suffixes=("", "_feed"))

            found = cand[~cand[feed_code_col].isna()].copy()
            if not found.empty:
                rows = []
                for _, r in found.iterrows():
                    scm_val = str(r[inv_code_col]).strip()
                    prod    = str(r["PRODUCT_CODE"]).strip()

                    brand = str(r[brand_col]).strip() if brand_col and (brand_col in r and pd.notna(r[brand_col])) else ""
                    moc_val = r[moc_col] if (moc_col and (moc_col in r) and pd.notna(r[moc_col])) else "0"
                    try:
                        moc = float(str(moc_val).replace(" ", "").replace(",", "."))
                    except Exception:
                        moc = 0.0
                    coeff = float(_brand_coeff_decimal(brand))
                    price = f"{(moc * coeff):.2f}"

                    imgs = []
                    for c in img_cols:
                        v = r.get(c)
                        if pd.notna(v) and str(v).strip():
                            s = str(v).strip()
                            if s.startswith("http://") or s.startswith("https://"):
                                imgs.append(s)
                    images_field = f"\"{';'.join(imgs)}\"" if imgs else ""

                    rows.append({
                        "PRODUCT_CODE": prod,
                        "META_validation_required": 1,
                        "META_original_product_code": scm_val,
                        "NEW_YN": 0,
                        "SPECIAL_YN": 0,
                        "SELLOUT_YN": 0,
                        "LABEL_AKCIA_YN": 0,
                        "LABEL_VYPREDAJ_YN": 0,
                        "LABEL_ODPORUCANE_YN": 0,
                        "LABEL_TIP_YN": 0,
                        "LABEL_SEZONNE_YN": 0,
                        "PRICE_WITH_VAT_Predvolene": price,
                        "IMAGES": images_field,
                    })
                if rows:
                    new = pd.DataFrame(rows, columns=list(UPGATES_EXPORT_HEADER_MAP_NEW.keys()))

    cols = ["PRODUCT_CODE"]
    if inv_code_col in unmatched.columns:
        cols.append(inv_code_col)
    unmatched_out = unmatched[cols].copy()
    unmatched_out["REASON"] = "Not in shop export"

    existing = _apply_upgates_headers(existing, UPGATES_EXPORT_HEADER_MAP_EXISTING)
    new      = _apply_upgates_headers(new,      UPGATES_EXPORT_HEADER_MAP_NEW)

    return existing, new, unmatched_out
