
# -*- coding: utf-8 -*-
"""
pl_feed_convert.py
------------------
Paul-Lange XML -> Upgates CSV ("ready to filter" style) converter.
- Stdlib only (xml.etree, csv); no pandas/lxml dependency.
- Columns cover your Upgates import; many left blank by design.
- PRICE_WITH_VAT "Predvolené" = PRICE_COMMON (MOC s DPH) * coefficient-by-manufacturer.
- Image URLs ";"-separated (CSV writer will quote automatically).

Usage (as library):
    from inventory_hub.adapters.pl_feed_convert import convert_xml_to_upgates
    rows = convert_xml_to_upgates(xml_path, out_csv_path, price_coeffs, vat=23, default_category="K00090", prefix="PL-")
    # returns number of rows written
"""
from __future__ import annotations
from typing import Dict, List, Tuple, Optional
from pathlib import Path
import csv, re
import xml.etree.ElementTree as ET

# Full header in fixed order (based on your script)
HEADER: List[str] = [
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
    "[STOCK \"\"]","[STOCK_POSITION \"\"]"
]

# Default coeffs per manufacturer (uppercased keys)
DEFAULT_COEFFS: Dict[str, float] = {
    "SHIMANO": 0.88,
    "PRO": 0.91,
    "LAZER": 0.90,
    "LONGUS": 0.95,
    "ELITE": 0.92,
    "MOTOREX": 0.96,
}

def _get_text(elem: ET.Element, tag: str) -> str:
    x = elem.find(tag)
    return (x.text if x is not None and x.text is not None else "").strip()

def _decimal(s: str) -> float:
    s = (s or "").strip().replace(",", ".")
    try:
        return float(s)
    except Exception:
        return 0.0

def _derive_images(item: ET.Element) -> str:
    parts: List[str] = []
    main = _get_text(item, "IMGURL")
    if main: parts.append(main)
    images = item.find("IMAGES")
    if images is not None:
        for img in images.findall("IMGURL"):
            if img.text and img.text.strip():
                parts.append(img.text.strip())
    # join with ";" -> CSV will quote as needed
    return ";".join(p for p in parts if p)

def _dyn_param(item: ET.Element, key_variants: List[str]) -> str:
    dyn = item.find("DYN_PARAMS")
    if dyn is None: return ""
    for p in dyn.findall("PARAM"):
        desc = _get_text(p, "DESC")
        val  = _get_text(p, "VAL")
        if desc:
            for k in key_variants:
                if desc.strip().lower() == k.lower():
                    return val
    return ""

def convert_xml_to_upgates(xml_path: Path, out_csv_path: Path,
                           price_coeffs: Optional[Dict[str, float]] = None,
                           vat: int = 23,
                           default_category: str = "K00090",
                           prefix: str = "PL-") -> int:
    tree = ET.parse(str(xml_path))
    root = tree.getroot()

    coeffs = dict(DEFAULT_COEFFS)
    if price_coeffs:
        # override defaults
        for k, v in price_coeffs.items():
            coeffs[str(k).upper()] = float(v)

    rows_written = 0
    out_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";", quotechar='"', quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        w.writerow(HEADER)

        for item in root.findall(".//SHOPITEM"):
            code = _get_text(item, "ITEM_ID")
            if not code:
                continue

            moc = _decimal(_get_text(item, "PRICE_VAT"))      # retail (with VAT)
            voc = _decimal(_get_text(item, "PRICE_VOC_VAT"))  # wholesale (with VAT)

            manufacturer_raw = _get_text(item, "MANUFACTURER")
            coef = coeffs.get(manufacturer_raw.upper(), 1.00)
            price_with = round(moc * coef, 2)

            title = _get_text(item, "PRODUCT")
            url   = _get_text(item, "URL")
            desc  = _get_text(item, "DESCRIPTION")
            ean   = _get_text(item, "EAN")
            imgs  = _derive_images(item)
            color = _dyn_param(item, ["Farba 1","Farba"])
            size  = _dyn_param(item, ["Veľkosť","Velkost","Veľkosť 1"])

            row_map = {
                "[PRODUCT_CODE]": f"{prefix}{code}",
                "[VARIANT_YN]": "0",
                "[VARIANT_CODE]": "",
                "[MAIN_YN]": "0",
                "[ACTIVE_YN]": "1",
                "[ARCHIVED_YN]": "0",
                "[CAN_ADD_TO_BASKET_YN]": "1",
                "[NEW_YN]": "0",
                "[SPECIAL_YN]": "0",
                "[SELLOUT_YN]": "0",
                "[LANGUAGE]": "sk",
                "[URL]": url,
                "[TITLE]": title,
                "[LONG_DESCRIPTION]": desc,
                "[SUPPLIER_CODE]": code,
                "[EAN]": ean,
                "[MANUFACTURER]": manufacturer_raw,
                "[AVAILABILITY]": "",
                "[STOCK]": "0",
                "[VAT]": str(vat),
                "[CATEGORIES]": default_category,
                "[IS_PRICES_WITH_VAT_YN]": "1",
                "[PRICE_BUY]": f"{voc:.2f}",
                "[PRICE_COMMON]": f"{moc:.2f}",
                "[PRICE_WITH_VAT „Predvolené“]": f"{price_with:.2f}",
                "[IMAGES]": imgs,
                "[PARAMETER „Farba“]": color,
                "[PARAMETER „Veľkosť“]": size,
                "[STOCK \"\"]": "",
                "[STOCK_POSITION \"\"]": "",
            }

            # write full header order
            w.writerow([row_map.get(h, "") for h in HEADER])
            rows_written += 1

    return rows_written
