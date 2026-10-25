# -*- coding: utf-8 -*-
"""
Northfinder Invoice XLSX → CSV Parser

Converts Northfinder B2B invoice XLSX exports to canonical CSV format
compatible with the Receiving workflow.

Column mapping from XLSX:
  Kód -> SCM (variant code like 108023-277-103)
  Názov -> TITLE
  Množstvo -> QTY
  Jednotková cena v EUR -> PRICE
  EAN -> EAN
  Katalóg -> CATALOG
  Farba -> COLOR
  Veľkosť -> SIZE
  MOC -> RRP (recommended retail price)
  Spolu v EUR -> TOTAL_EUR
  Spolu v EUR s DPH -> TOTAL_EUR_VAT
"""
from __future__ import annotations

import csv
import logging
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, List, Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Column mapping: XLSX column name -> CSV column name
XLSX_TO_CSV_MAPPING = {
    "Kód": "SCM",
    "Názov": "TITLE",
    "Množstvo": "QTY",
    "Jednotková cena v EUR": "PRICE",
    "EAN": "EAN",
    "Katalóg": "CATALOG",
    "Farba": "COLOR",
    "Veľkosť": "SIZE",
    "MOC": "RRP",
    "Spolu v EUR": "TOTAL_EUR",
    "Spolu v EUR s DPH": "TOTAL_EUR_VAT",
    "Kód farby": "COLOR_CODE",
}

# Required columns for Receiving (in order)
REQUIRED_COLUMNS = ["SCM", "TITLE", "QTY", "PRICE"]

# Optional columns to include if present
OPTIONAL_COLUMNS = ["EAN", "CATALOG", "COLOR", "SIZE", "RRP", "TOTAL_EUR", "TOTAL_EUR_VAT", "COLOR_CODE"]


def validated_receiving_rows(xlsx_path: Path) -> list[dict]:
    """Read the supplier's explicit EUR/net columns, rejecting manufactured zeroes."""
    from inventory_hub.services.receiving_source import ReceivingSourceError, decimal_value
    try:
        frame = pd.read_excel(xlsx_path, dtype=object, keep_default_na=False)
    except Exception:
        raise ReceivingSourceError("receiving_source_unverified") from None
    required = ("Kód", "Názov", "Množstvo", "Jednotková cena v EUR", "Spolu v EUR", "Spolu v EUR s DPH")
    if any(name not in frame.columns for name in required):
        raise ReceivingSourceError("receiving_source_vat_unverified")
    rows = []
    for index, raw in frame.iterrows():
        if not any(str(value).strip() for value in raw):
            continue
        line = len(rows) + 2
        if not str(raw["Kód"]).strip():
            raise ReceivingSourceError("receiving_csv_invalid", line_number=line, field="sku")
        quantity = decimal_value(raw["Množstvo"], 3, "quantity", line)
        price = decimal_value(raw["Jednotková cena v EUR"], 4, "price", line)
        net = decimal_value(raw["Spolu v EUR"], 2, "price", line)
        gross = decimal_value(raw["Spolu v EUR s DPH"], 2, "price", line)
        if (quantity * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) != net or gross < net:
            raise ReceivingSourceError("receiving_csv_invalid", line_number=line, field="total")
        barcode = raw.get("EAN", "")
        if barcode == 0 or barcode == "":
            barcode = ""
        elif isinstance(barcode, (int, float)):
            if not float(barcode).is_integer():
                raise ReceivingSourceError("receiving_csv_invalid", line_number=line, field="ean")
            barcode = str(int(barcode))
        rows.append({"supplier_sku": str(raw["Kód"]).strip(), "description": str(raw["Názov"]).strip(),
                     "ean": str(barcode).strip(), "ordered_qty": quantity, "unit_price": price})
    if not rows:
        raise ReceivingSourceError("receiving_csv_invalid", field="rows")
    return rows


def parse_xlsx_to_csv(
    xlsx_path: Path,
    csv_path: Path,
    delimiter: str = ";",
    encoding: str = "utf-8",
) -> Dict[str, Any]:
    """
    Parse Northfinder invoice XLSX and convert to canonical CSV.
    
    Args:
        xlsx_path: Path to input XLSX file
        csv_path: Path to output CSV file
        delimiter: CSV delimiter (default: semicolon for Slovak locale)
        encoding: Output encoding
    
    Returns:
        Dict with parsing statistics:
        {
            "success": bool,
            "rows_parsed": int,
            "rows_skipped": int,
            "columns_found": list,
            "columns_missing": list,
            "error": str or None
        }
    """
    result = {
        "success": False,
        "rows_parsed": 0,
        "rows_skipped": 0,
        "columns_found": [],
        "columns_missing": [],
        "error": None,
    }
    
    try:
        verified_rows = validated_receiving_rows(xlsx_path)
        # Load XLSX
        df = pd.read_excel(xlsx_path)
        logger.info(f"Loaded XLSX with {len(df)} rows, columns: {df.columns.tolist()}")
        
        # Map columns
        rename_map = {}
        for xlsx_col, csv_col in XLSX_TO_CSV_MAPPING.items():
            if xlsx_col in df.columns:
                rename_map[xlsx_col] = csv_col
                result["columns_found"].append(csv_col)
        
        # Check for missing required columns
        for req_col in REQUIRED_COLUMNS:
            xlsx_name = next((k for k, v in XLSX_TO_CSV_MAPPING.items() if v == req_col), None)
            if xlsx_name and xlsx_name not in df.columns:
                result["columns_missing"].append(req_col)
        
        if result["columns_missing"]:
            # Try alternative column names
            alt_mappings = {
                "Kód": ["Code", "Kod", "SKU", "Variant"],
                "Názov": ["Name", "Nazov", "Title", "Product"],
                "Množstvo": ["Quantity", "Mnozstvo", "Qty", "Ks"],
                "Jednotková cena v EUR": ["Unit price", "Price", "Cena", "Unit Price EUR"],
            }
            
            for xlsx_col, alternatives in alt_mappings.items():
                if xlsx_col not in df.columns:
                    for alt in alternatives:
                        if alt in df.columns:
                            rename_map[alt] = XLSX_TO_CSV_MAPPING[xlsx_col]
                            result["columns_found"].append(XLSX_TO_CSV_MAPPING[xlsx_col])
                            if XLSX_TO_CSV_MAPPING[xlsx_col] in result["columns_missing"]:
                                result["columns_missing"].remove(XLSX_TO_CSV_MAPPING[xlsx_col])
                            break
        
        # Still missing required columns?
        if result["columns_missing"]:
            result["error"] = f"Missing required columns: {result['columns_missing']}"
            logger.error(result["error"])
            return result
        
        # Rename columns
        df = df.rename(columns=rename_map)
        
        # Select only mapped columns (in preferred order)
        output_columns = []
        for col in REQUIRED_COLUMNS + OPTIONAL_COLUMNS:
            if col in df.columns:
                output_columns.append(col)
        
        df_output = df[output_columns].copy()
        
        # Clean data
        # Remove rows where SCM is empty/NaN
        initial_rows = len(df_output)
        df_output = df_output.dropna(subset=["SCM"])
        df_output = df_output[df_output["SCM"].astype(str).str.strip() != ""]
        
        # Handle boolean False values (seen in Farba column)
        for col in df_output.columns:
            df_output[col] = df_output[col].apply(
                lambda x: "" if x is False or (isinstance(x, float) and pd.isna(x)) else x
            )
        
        # Convert numeric columns
        numeric_cols = ["RRP", "TOTAL_EUR", "TOTAL_EUR_VAT"]
        for col in numeric_cols:
            if col in df_output.columns:
                df_output[col] = pd.to_numeric(df_output[col], errors="raise")
        df_output["QTY"] = [row["ordered_qty"] for row in verified_rows]
        df_output["PRICE"] = [row["unit_price"] for row in verified_rows]
        df_output["SCM"] = [row["supplier_sku"] for row in verified_rows]
        df_output["TITLE"] = [row["description"] for row in verified_rows]
        
        # Convert EAN to string (avoid scientific notation)
        if "EAN" in df_output.columns:
            df_output["EAN"] = [row["ean"] for row in verified_rows]
        
        result["rows_skipped"] = initial_rows - len(df_output)
        result["rows_parsed"] = len(df_output)
        
        # Ensure output directory exists
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Write CSV
        df_output.to_csv(
            csv_path,
            sep=delimiter,
            index=False,
            encoding=encoding,
            quoting=csv.QUOTE_MINIMAL,
        )
        
        result["success"] = True
        logger.info(f"Wrote {result['rows_parsed']} rows to {csv_path}")
        
        # Log first 3 rows for verification
        if len(df_output) > 0:
            logger.debug("First 3 rows:")
            for i, row in df_output.head(3).iterrows():
                logger.debug(f"  {row.to_dict()}")
        
    except Exception as e:
        result["error"] = str(e)
        result["code"] = getattr(e, "code", "receiving_csv_invalid")
        logger.exception(f"Failed to parse XLSX: {e}")
    
    return result


def validate_xlsx_structure(xlsx_path: Path) -> Dict[str, Any]:
    """
    Validate XLSX file structure without converting.
    
    Returns:
        Dict with validation info
    """
    try:
        df = pd.read_excel(xlsx_path, nrows=5)
        
        found_required = []
        missing_required = []
        
        for xlsx_col, csv_col in XLSX_TO_CSV_MAPPING.items():
            if xlsx_col in df.columns and csv_col in REQUIRED_COLUMNS:
                found_required.append(csv_col)
        
        for req in REQUIRED_COLUMNS:
            if req not in found_required:
                missing_required.append(req)
        
        return {
            "valid": len(missing_required) == 0,
            "columns": df.columns.tolist(),
            "row_count": len(df),
            "found_required": found_required,
            "missing_required": missing_required,
        }
    except Exception as e:
        return {
            "valid": False,
            "error": str(e),
        }


# CLI for testing
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python northfinder_xlsx_parser.py <input.xlsx> [output.csv]")
        sys.exit(1)
    
    xlsx_file = Path(sys.argv[1])
    csv_file = Path(sys.argv[2]) if len(sys.argv) > 2 else xlsx_file.with_suffix(".csv")
    
    logging.basicConfig(level=logging.DEBUG)
    
    result = parse_xlsx_to_csv(xlsx_file, csv_file)
    print(f"Result: {result}")
