# -*- coding: utf-8 -*-
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
import csv, datetime as dt, hashlib, json, re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, parse_qs, urlparse

CSV_ACCEPT_HEADERS = {"Accept": "text/csv,application/octet-stream,application/*;q=0.9,*/*;q=0.8"}
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
PL_INVOICE_LIST_URL = "https://vo.paul-lange-oslany.sk/index.php?id=faktury&cmd=default&vystavena_od={od}&vystavena_do={do}"

# Smart quotes META column used across BikeTrek (idempotency marker)
INVOICE_META_COL = '[META „stock_updated_by_invoices“]'

@dataclass
class LoginConfig:
    mode: str = "none"   # "form" | "cookie" | "basic" | "none"
    login_url: str = ""
    user_field: str = "login"
    pass_field: str = "password"
    username: str = ""
    password: str = ""
    cookie: str = ""
    insecure_all: bool = False

@dataclass
class RefreshResult:
    downloaded: int
    skipped: int
    failed: int
    pages: int
    log_files: List[str]

def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def _sha1(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1<<16), b""):
            h.update(chunk)
    return h.hexdigest()

def _normalize_header_name(name: str) -> str:
    n = (name or "").strip().replace("„", '"').replace("“", '"').replace("”", '"')
    n = re.sub(r"\s+", " ", n)
    return n

def _find_col_idx(header: List[str], target: str) -> Optional[int]:
    tn = _normalize_header_name(target).lower()
    for i, h in enumerate(header):
        if _normalize_header_name(h).lower() == tn:
            return i
    return None

def build_session(cfg: LoginConfig) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": CHROME_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "sk-SK,sk;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
    })
    if cfg.mode == "cookie" and cfg.cookie:
        for part in cfg.cookie.split(";"):
            part = part.strip()
            if part and "=" in part:
                k, v = part.split("=", 1)
                s.cookies.set(k.strip(), v.strip())
        return s
    if cfg.mode == "basic":
        return s
    if cfg.mode == "form" and cfg.login_url:
        r = s.get(cfg.login_url, timeout=30, verify=not cfg.insecure_all, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        form = soup.find("form")
        action = cfg.login_url
        method = "post"
        inputs = form.find_all("input") if form else []
        if form:
            action = form.get("action") or action
            method = (form.get("method") or "post").lower()

        payload = {}
        for inp in inputs:
            name = inp.get("name")
            if not name: continue
            itype = (inp.get("type") or "").lower()
            if itype in {"checkbox","radio"}:
                if inp.has_attr("checked"):
                    payload[name] = inp.get("value", "on")
            else:
                payload[name] = inp.get("value", "")

        user_field = cfg.user_field or "login"
        pass_field = cfg.pass_field or "password"
        names = {i.get("name") for i in inputs if i.get("name")}
        if user_field not in names:
            cand = soup.select('input[type="text"],input[type="email"]')
            if cand: user_field = cand[0].get("name") or user_field
        if pass_field not in names:
            candp = soup.select('input[type="password"]')
            if candp: pass_field = candp[0].get("name") or pass_field

        payload[user_field] = cfg.username or ""
        payload[pass_field] = cfg.password or ""

        action_url = urljoin(cfg.login_url, action)
        if "id=login" in action_url and "cmd=" not in action_url:
            sep = "&" if "?" in action_url else "?"
            action_url = action_url + f"{sep}cmd=login_proc"

        headers = {"Referer": cfg.login_url}
        if method == "get":
            pr = s.get(action_url, params=payload, headers=headers, timeout=30, allow_redirects=True,
                       verify=not cfg.insecure_all)
        else:
            pr = s.post(action_url, data=payload, headers=headers, timeout=30, allow_redirects=True,
                        verify=not cfg.insecure_all)
        pr.raise_for_status()
    return s

def enumerate_invoice_pages(html_text: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    hrefs = set([base_url])
    for a in soup.select("div.faktury_zoznam_pagination a[href]"):
        hrefs.add(urljoin(base_url, a.get("href", "")))
    return sorted(hrefs)

def list_invoice_links(html_text: str, base_url: str) -> List[Tuple[str, str]]:
    soup = BeautifulSoup(html_text, "html.parser")
    pairs: List[Tuple[str, str]] = []
    for tr in soup.select("table.faktury_zoznam tbody tr"):
        number = None
        num_el = tr.select_one("td.faktury_zoznam_cislo-faktury div")
        if num_el:
            number = num_el.get_text(strip=True)
        if not number:
            for td in tr.find_all("td"):
                tx = (td.get_text(strip=True) or "")
                if re.match(r"F\d+", tx):
                    number = tx
                    break
        href = None
        a_csv = tr.select_one('td.faktury_zoznam_link-na-csv a[href]')
        if a_csv:
            href = a_csv.get("href", "")
        if not href:
            for a in tr.find_all("a", href=True):
                h = a["href"]
                if re.search(r'(^|[?&])getcsv=', h, flags=re.I):
                    href = h
                    break
        if not href or not number:
            continue
        abs_href = urljoin(base_url, href)
        pairs.append((abs_href, f"{number}.csv"))
    if not pairs:
        for a in soup.select('a[href]'):
            h = a.get("href", "")
            if re.search(r'(^|[?&])getcsv=', h, flags=re.I):
                abs_href = urljoin(base_url, h)
                qs = parse_qs(urlparse(abs_href).query)
                number = qs.get("number", [None])[0]
                if not number:
                    txt = a.get_text(strip=True)
                    m = re.search(r"(F\d+)", txt or "", flags=re.I)
                    if m: number = m.group(1)
                if number:
                    pairs.append((abs_href, f"{number}.csv"))
    out, seen = [], set()
    for href, fn in pairs:
        if fn in seen: continue
        seen.add(fn)
        out.append((href, fn))
    return out

def try_download_csv(session: requests.Session, cfg: LoginConfig, src_url: str, dest: Path, referer: str) -> bool:
    headers = {**CSV_ACCEPT_HEADERS, "Referer": referer}
    try:
        r = session.get(src_url, headers=headers, timeout=30, stream=True, verify=not cfg.insecure_all)
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1<<14):
                if chunk: f.write(chunk)
        return True
    except Exception:
        return False

def to_date(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")

def refresh_invoices_web(data_root: Path, supplier: str, login: LoginConfig, months_back: int = 3) -> RefreshResult:
    sup_root = data_root / "suppliers" / supplier
    dir_csv = sup_root / "invoices" / "csv"
    dir_logs = sup_root / "logs"
    _ensure_dir(dir_csv); _ensure_dir(dir_logs)

    session = build_session(login)

    today = dt.date.today()
    date_from = (today - dt.timedelta(days=30 * months_back)).strftime("%Y-%m-%d")

    list_url = PL_INVOICE_LIST_URL.format(od=date_from, do=to_date(today))
    r = session.get(list_url, timeout=60, verify=not login.insecure_all)
    r.raise_for_status()
    (dir_logs / "last_invoice_list.html").write_text(r.text, encoding="utf-8", errors="ignore")

    pages = enumerate_invoice_pages(r.text, r.url)
    all_links: List[Tuple[str, str]] = []
    for i, page in enumerate(pages, 1):
        rp = session.get(page, timeout=60, verify=not login.insecure_all)
        rp.raise_for_status()
        (dir_logs / f"invoice_list_page_{i}.html").write_text(rp.text, encoding="utf-8", errors="ignore")
        all_links.extend(list_invoice_links(rp.text, rp.url))

    uniq: Dict[str, str] = {}
    for href, fn in all_links:
        uniq[fn] = href
    links = [(url, fn) for fn, url in uniq.items()]

    downloaded = skipped = failed = 0
    try:
        idx = json.loads((sup_root / "invoices" / "index.latest.json").read_text(encoding="utf-8"))
    except Exception:
        idx = {}

    for src_url, filename in links:
        out = dir_csv / f"{filename}"
        invoice_id = f"{supplier}:{filename.removesuffix('.csv')}"

        if out.exists():
            skipped += 1
        else:
            ok = try_download_csv(session, login, src_url, out, referer=list_url)
            if ok and out.exists() and out.stat().st_size > 0:
                downloaded += 1
            else:
                failed += 1
                continue

        idx[invoice_id] = {
            "supplier": supplier,
            "invoice_id": invoice_id,
            "number": filename.replace(".csv",""),
            "issue_date": None,
            "issue_date_source": "fallback:download",
            "downloaded_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "csv_path": str(out.relative_to(sup_root).as_posix()),
            "status": "new",
            "sha1": _sha1(out) if out.exists() else None,
            "layout_used": "flat",
        }

    (sup_root / "invoices").mkdir(parents=True, exist_ok=True)
    (sup_root / "invoices" / "index.latest.json").write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")

    return RefreshResult(downloaded=downloaded, skipped=skipped, failed=failed, pages=len(pages), log_files=[
        str((dir_logs / "last_invoice_list.html").relative_to(sup_root).as_posix())
    ])

def _ensure_meta_columns(header: List[str]) -> Tuple[List[str], Dict[str, int]]:
    """
    Ensure presence of Upgates META columns using smart quotes, e.g.
    [META „original_product_code“], [META „validation_required“], [META „stock_updated_by_invoices“].
    Returns (new_header, index_map).
    """
    wants = [
        '[META „original_product_code“]',
        '[META „validation_required“]',
        INVOICE_META_COL,
    ]

    def eq(a: str, b: str) -> bool:
        def norm(x: str) -> str:
            x = x.replace("„", '"').replace("“", '"').replace("”", '"')
            return re.sub(r"\s+", " ", x.strip()).lower()
        return norm(a) == norm(b)

    new_header = header[:]
    idxmap: Dict[str, int] = {}

    for want in wants:
        found = None
        for i, h in enumerate(new_header):
            if eq(h, want):
                found = i
                break
        if found is None:
            new_header.append(want)
            found = len(new_header) - 1
        idxmap[want] = found

    return new_header, idxmap

def _load_upgates_export(path: Path):
    data: Dict[str, Dict[str, str]] = {}
    with open(path, "r", encoding="utf-8-sig", errors="ignore", newline="") as f:
        reader = csv.reader(f, delimiter=";")
        header = next(reader)
        # make sure META columns exist (keys present in each row dict)
        header, _idxmap = _ensure_meta_columns(header)
        idx_product_code = _find_col_idx(header, "[PRODUCT_CODE]")
        if idx_product_code is None:
            raise RuntimeError("Upgates export must contain [PRODUCT_CODE].")
        for row in reader:
            if not row or idx_product_code >= len(row):
                continue
            if len(row) < len(header):
                row = row + [""] * (len(header) - len(row))
            code = row[idx_product_code].strip()
            if not code:
                continue
            rec = {header[i]: (row[i] if i < len(row) else "") for i in range(len(header))}
            data[code] = rec
    return data, header

def _parse_invoice_csv(path: Path):
    items: List[tuple] = []
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        header_line = ""
        while True:
            header_line = f.readline()
            if header_line == "": break
            if header_line.strip(): break
        if not header_line: return items
        sep = ";" if header_line.count(";") >= header_line.count(",") else ","
        headers = [h.strip().strip('"') for h in header_line.strip().split(sep)]
        hdr_norm = [re.sub(r"\s+", " ", h.strip().lower().replace("„", '"').replace("“", '"').replace("”", '"')) for h in headers]
        def find_idx(cands: List[str]) -> Optional[int]:
            for i, hn in enumerate(hdr_norm):
                if hn in cands: return i
            for i, hn in enumerate(hdr_norm):
                if any(c in hn for c in cands): return i
            return None
        idx_scm = find_idx(["sčm","scm","kod","kód"])
        idx_qty = find_idx(["počet kusov","množstvo","quantity","qty","ks"])
        if idx_scm is None or idx_qty is None:
            raise RuntimeError(f"Cannot find SČM/Quantity in {path.name} header: {headers}")
        reader = csv.reader(f, delimiter=sep)
        for row in reader:
            if not row or all((c or '').strip() == "" for c in row): continue
            row = [c.strip().strip('"') for c in row]
            if max(idx_scm, idx_qty) >= len(row): continue
            scm = row[idx_scm].strip()
            qty_raw = (row[idx_qty] or "").strip()
            qty_clean = re.sub(r"[^0-9,\.-]", "", qty_raw).replace(",", ".")
            try:
                qty_f = float(qty_clean or "0")
                qty = int(round(qty_f))
            except Exception:
                qty = 0
            if scm:
                items.append((scm, qty if qty > 0 else 1))
    return items

def prepare_from_invoice(data_root: Path, supplier: str, shop: str, invoice_relpath: str, use_invoice_qty: bool = True):
    """
    Process selected invoice into three CSVs (updates_existing, new_products, unmatched).
    Adds/uses META columns with smart quotes and idempotency via [META „stock_updated_by_invoices“].
    """
    sup_root = data_root / "suppliers" / supplier
    inv_path = sup_root / invoice_relpath
    if not inv_path.is_file():
        raise FileNotFoundError(f"Invoice not found: {invoice_relpath}")

    upg_csv = data_root / "shops" / shop / "latest.csv"
    if not upg_csv.exists():
        raise FileNotFoundError(f"Upgates export missing: shops/{shop}/latest.csv")

    # find converted feed
    conv_dir_new = sup_root / "feeds" / "converted"
    conv_dir_old = sup_root / "feeds_converted"
    candidates = []
    if conv_dir_new.exists(): candidates += sorted(conv_dir_new.glob("*.csv"))
    if conv_dir_old.exists(): candidates += sorted(conv_dir_old.glob("*.csv"))
    if not candidates:
        raise FileNotFoundError("No converted feed found (looked in feeds/converted and feeds_converted).")
    xml_converted_csv = candidates[-1]

    # parse invoice
    inv_items = _parse_invoice_csv(inv_path)
    if not inv_items:
        return {"existing": 0, "new": 0, "unmatched": 0, "invoice_items": 0, "outputs": {}}

    # aggregate qty per SCM
    increments: Dict[str, int] = {}
    for scm, qty in inv_items:
        inc = qty if use_invoice_qty else 1
        increments[scm] = increments.get(scm, 0) + inc

    # load Upgates export
    upg_data, upg_header = _load_upgates_export(upg_csv)
    idx_code  = _find_col_idx(upg_header, "[PRODUCT_CODE]")
    idx_stock = _find_col_idx(upg_header, "[STOCK]")
    idx_avail = _find_col_idx(upg_header, "[AVAILABILITY]")
    upg_header, _upg_meta_idx = _ensure_meta_columns(upg_header)
    if idx_code is None or idx_stock is None or idx_avail is None:
        raise RuntimeError("Upgates export must have [PRODUCT_CODE], [STOCK], [AVAILABILITY].")

    # load converted XML feed (for NEW products)
    with open(xml_converted_csv, "r", encoding="utf-8-sig", errors="ignore", newline="") as fxml:
        xreader = csv.reader(fxml, delimiter=";")
        xheader = next(xreader)
        xheader, x_idx_map = _ensure_meta_columns(xheader)
        x_idx_code = _find_col_idx(xheader, "[PRODUCT_CODE]")
        if x_idx_code is None:
            raise RuntimeError("Converted XML CSV is missing [PRODUCT_CODE].")

        xml_map: Dict[str, List[str]] = {}
        for row in xreader:
            if len(row) < len(xheader):
                row = row + [""] * (len(xheader) - len(row))
            code = (row[x_idx_code] or "").strip()
            if not code:
                continue
            # map under both keys: raw and PL- prefixed
            xml_map[code] = row
            if not code.upper().startswith("PL-"):
                xml_map[f"PL-{code}"] = row

    out_dir = sup_root / "imports" / "upgates"
    _ensure_dir(out_dir)
    datestr = dt.date.today().strftime("%Y%m%d")
    inv_stem = Path(invoice_relpath).stem

    updates_csv   = out_dir / f"{inv_stem}_updates_existing_{datestr}.csv"
    new_csv       = out_dir / f"{inv_stem}_new_products_{datestr}.csv"
    unmatched_csv = out_dir / f"{inv_stem}_unmatched_{datestr}.csv"

    # headers
    existing_header = ["[PRODUCT_CODE]", "[STOCK]", "[AVAILABILITY]",
                       '[META „original_product_code“]', '[META „validation_required“]',
                       INVOICE_META_COL]
    new_header = xheader
    unmatched_header = ["SCM","PRODUCT_CODE","QTY","REASON"]

    existing_rows: List[List[str]] = []
    new_rows: List[List[str]] = []
    unmatched_rows: List[List[str]] = []

    existing_count = new_count = unmatched_count = 0
    total_items = 0

    # zero flags for NEW products if present
    zero_cols_new = [
        "[NEW_YN]","[SPECIAL_YN]","[SELLOUT_YN]",
        '[LABEL_ACTIVE_YN „Akcia“]','[LABEL_ACTIVE_YN „Výpredaj“]',
        '[LABEL_ACTIVE_YN „Odporúčané“]','[LABEL_ACTIVE_YN „Tip“]','[LABEL_ACTIVE_YN „Sezónne“]',
        '["LABEL_ACTIVE_YN „Akcia“"]','["LABEL_ACTIVE_YN „Výpredaj“"]',
        '["LABEL_ACTIVE_YN „Odporúčané“"]','["LABEL_ACTIVE_YN „Tip“"]','["LABEL_ACTIVE_YN „Sezónne“"]',
    ]

    for scm, inc in increments.items():
        total_items += inc
        code = f"PL-{scm}".strip()

        # EXISTING in Upgates export -> update stock + append invoice to META
        if code in upg_data:
            src_row = upg_data[code]

            # idempotency: skip if this invoice already applied
            already = [x.strip() for x in (src_row.get(INVOICE_META_COL, "") or "").split(";") if x.strip()]
            if inv_stem in already:
                unmatched_rows.append([scm, code, str(inc), f"invoice {inv_stem} already applied"])
                unmatched_count += 1
                continue

            try:
                current_stock = int(re.sub(r"[^0-9-]", "", src_row.get("[STOCK]", "")) or "0")
            except Exception:
                current_stock = 0
            new_stock = max(0, current_stock + inc)

            # extend meta with this invoice id
            new_meta_val = ";".join(filter(None, [src_row.get(INVOICE_META_COL, "").strip(), inv_stem]))

            existing_rows.append([code, str(new_stock), "Na sklade", scm, "0", new_meta_val])
            existing_count += 1
            continue

        # NEW product -> lookup in feed (works for raw and PL- thanks to xml_map mapping)
        row = xml_map.get(code)
        if row is None:
            unmatched_rows.append([scm, code, str(inc), "not found in Upgates export nor in XML feed"])
            unmatched_count += 1
            continue

        # clone and pad
        row = row[:]
        if len(row) < len(new_header):
            row = row + [""] * (len(new_header) - len(row))

        # force PRODUCT_CODE = PL-<scm> (normalize)
        ix_code = _find_col_idx(new_header, "[PRODUCT_CODE]")
        if ix_code is not None:
            row[ix_code] = code

        # IMAGES: '|' -> ';'
        ix_img = _find_col_idx(new_header, "[IMAGES]")
        if ix_img is not None and row[ix_img]:
            row[ix_img] = row[ix_img].replace("|", ";")

        # STOCK = invoice qty
        ix_stock_new = _find_col_idx(new_header, "[STOCK]")
        if ix_stock_new is not None:
            row[ix_stock_new] = str(inc)

        # AVAILABILITY = Na sklade
        ix_avail_new = _find_col_idx(new_header, "[AVAILABILITY]")
        if ix_avail_new is not None:
            row[ix_avail_new] = "Na sklade"

        # META fields
        _, idxmap_meta = _ensure_meta_columns(new_header)
        row[idxmap_meta['[META „original_product_code“]']] = scm
        row[idxmap_meta['[META „validation_required“]']] = "1"
        row[idxmap_meta[INVOICE_META_COL]] = inv_stem

        # zero label/special flags if columns exist
        for col_name in zero_cols_new:
            ix = _find_col_idx(new_header, col_name)
            if ix is not None and ix < len(row):
                row[ix] = "0"

        new_rows.append(row)
        new_count += 1

    # writers
    def _wcsv(path: Path, header: List[str], rows: List[List[str]]):
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";", quotechar='"', quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
            w.writerow(header)
            for r in rows:
                w.writerow(r)

    if existing_rows: _wcsv(updates_csv, existing_header, existing_rows)
    if new_rows:      _wcsv(new_csv, new_header, new_rows)
    if unmatched_rows:_wcsv(unmatched_csv, unmatched_header, unmatched_rows)

    return {
        "existing": existing_count,
        "new": new_count,
        "unmatched": unmatched_count,
        "invoice_items": total_items,
        "outputs": {
            "updates_existing": str(updates_csv.relative_to(sup_root).as_posix()) if existing_rows else None,
            "new_products":    str(new_csv.relative_to(sup_root).as_posix()) if new_rows else None,
            "unmatched":       str(unmatched_csv.relative_to(sup_root).as_posix()) if unmatched_rows else None,
        }
    }
