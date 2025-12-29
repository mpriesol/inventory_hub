from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Dict, List

import requests
from bs4 import BeautifulSoup

PL_INVOICE_LIST_URL = "https://vo.paul-lange-oslany.sk/index.php?id=faktury&cmd=default&vystavena_od={od}&vystavena_do={do}"

CSV_ACCEPT_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "sk,cs;q=0.9,en-US;q=0.8,en;q=0.7",
}
NAV_HEADERS = {
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1",
}
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

def _ensure_dirs(*dirs: Path):
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

def _http_get(s: requests.Session, url: str, verify: bool = True, **kw) -> requests.Response:
    r = s.get(url, timeout=60, verify=verify, **kw)
    r.raise_for_status()
    return r

def _looks_like_html_bytes(buf: bytes) -> bool:
    s = buf.lstrip().lower()
    return s.startswith(b"<!doctype") or s.startswith(b"<html") or b"<title>" in s[:2000]

def _looks_like_csv_text(txt: str) -> bool:
    low = txt.lower()
    if "<html" in low or "<!doctype" in low:
        return False
    first_line = txt.splitlines()[0] if txt else ""
    return ("," in first_line or ";" in first_line) and len(first_line) > 2

def _try_download_csv(session: requests.Session, src_url: str, dest: Path, referer: str, verify: bool) -> bool:
    headers = {**CSV_ACCEPT_HEADERS, **NAV_HEADERS, "Referer": referer}
    try:
        r = session.get(src_url, headers=headers, timeout=30, stream=False, verify=verify, allow_redirects=True)
        r.raise_for_status()
        data = r.content
        head = data[:2048].decode("utf-8", errors="ignore").lower()
        if _looks_like_html_bytes(data) or not _looks_like_csv_text(head):
            return False
        dest.write_bytes(data)
        return True
    except Exception:
        return False

def _try_download_pdf(session: requests.Session, src_url: str, dest: Path, referer: str, verify: bool) -> bool:
    headers = {**CSV_ACCEPT_HEADERS, **NAV_HEADERS, "Referer": referer}
    try:
        r = session.get(src_url, headers=headers, timeout=45, stream=True, verify=verify, allow_redirects=True)
        r.raise_for_status()
        data = r.content
        if _looks_like_html_bytes(data[:4096]):
            return False
        dest.write_bytes(data)
        return True
    except Exception:
        return False

def _list_invoice_rows(html_text: str, base_url: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html_text, "html.parser")
    rows: Dict[str, Dict[str, str]] = {}
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
        if not number:
            continue
        rec = rows.setdefault(number, {"number": number})

        a_csv = tr.select_one('td.faktury_zoznam_link-na-csv a[href]')
        if a_csv:
            rec["csv_url"] = requests.compat.urljoin(base_url, a_csv.get("href", ""))
        else:
            for a in tr.find_all("a", href=True):
                h = a["href"]
                if re.search(r'(^|[?&])getcsv=', h, flags=re.I):
                    rec["csv_url"] = requests.compat.urljoin(base_url, h)
                    break

        a_pdf = tr.select_one('td.faktury_zoznam_link-na-pdf a[href]')
        if a_pdf:
            rec["pdf_url"] = requests.compat.urljoin(base_url, a_pdf.get("href", ""))

    return list(rows.values())

def _enumerate_pages(html_text: str, base_url: str) -> List[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    hrefs = set([base_url])
    for a in soup.select("div.faktury_zoznam_pagination a[href]"):
        hrefs.add(requests.compat.urljoin(base_url, a.get("href", "")))
    return sorted(hrefs)

def _build_session(auth_mode: str, login_url: str = "", user_field: str = "", pass_field: str = "", username: str = "", password: str = "", cookie: str = "", insecure_all: bool = False) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": CHROME_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "sk-SK,sk;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
    })
    verify = not insecure_all

    if auth_mode == "cookie":
        if cookie:
            for part in cookie.split(";"):
                part = part.strip()
                if part and "=" in part:
                    k, v = part.split("=", 1)
                    s.cookies.set(k.strip(), v.strip())
        return s

    if auth_mode == "basic":
        return s

    if auth_mode == "form":
        if not login_url:
            raise RuntimeError("auth_mode=form requires login_url")
        r = s.get(login_url, timeout=30, verify=verify, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        form = soup.find("form")
        form_action = login_url
        method = "post"
        inputs = []
        if form:
            form_action = form.get("action") or login_url
            method = (form.get("method") or "post").lower()
            inputs = form.find_all("input")
        payload = {}
        for inp in inputs:
            name = inp.get("name")
            if not name:
                continue
            itype = (inp.get("type") or "").lower()
            if itype in {"checkbox", "radio"}:
                if inp.has_attr("checked"):
                    payload[name] = inp.get("value", "on")
            else:
                payload[name] = inp.get("value", "")
        names = {i.get("name") for i in inputs if i.get("name")}
        if user_field not in names:
            cand = soup.select('input[type="text"],input[type="email"]')
            if cand:
                user_field = cand[0].get("name") or user_field
        if pass_field not in names:
            candp = soup.select('input[type="password"]')
            if candp:
                pass_field = candp[0].get("name") or pass_field
        payload[user_field or "USERNAME"] = username or ""
        payload[pass_field or "PASSWORD"] = password or ""

        action_url = requests.compat.urljoin(login_url, form_action)
        if "id=login" in action_url and "cmd=" not in action_url:
            sep = "&" if "?" in action_url else "?"
            action_url = action_url + f"{sep}cmd=login_proc"

        headers = {"Referer": login_url}
        if method == "get":
            s.get(action_url, params=payload, headers=headers, timeout=30, allow_redirects=True, verify=verify)
        else:
            s.post(action_url, data=payload, headers=headers, timeout=30, allow_redirects=True, verify=verify)
        return s

    return s

def refresh_paul_lange_invoices(data_root: Path, supplier: str, months_back: int, conf: dict) -> dict:
    base = data_root / "suppliers" / supplier
    raw_dir = base / "invoices" / "raw"
    csv_root = base / "invoices" / "csv"
    logs_dir = base / "logs"
    _ensure_dirs(raw_dir, csv_root, logs_dir)

    auth_mode = (conf.get("auth_mode") or "form").lower()
    login_url = conf.get("login_url") or "https://vo.paul-lange-oslany.sk/index.php?id=login"
    user_field = conf.get("user_field") or "USERNAME"
    pass_field = conf.get("pass_field") or "PASSWORD"
    username   = conf.get("username") or ""
    password   = conf.get("password") or ""
    cookie     = conf.get("cookie") or ""
    insecure   = bool(conf.get("insecure_all", False))

    s = _build_session(auth_mode, login_url, user_field, pass_field, username, password, cookie, insecure)

    today = dt.date.today()
    date_from = (today - dt.timedelta(days=30 * int(months_back or 3))).strftime("%Y-%m-%d")
    date_to   = today.strftime("%Y-%m-%d")
    list_url = PL_INVOICE_LIST_URL.format(od=date_from, do=date_to)

    verify = not insecure
    r = _http_get(s, list_url, verify=verify)
    (logs_dir / "pl_invoice_list.html").write_text(r.text, encoding="utf-8", errors="ignore")

    pages = _enumerate_pages(r.text, r.url)
    entries: Dict[str, Dict[str, str]] = {}
    for i, page in enumerate(pages, 1):
        rp = _http_get(s, page, verify=verify)
        (logs_dir / f"pl_invoice_list_page_{i}.html").write_text(rp.text, encoding="utf-8", errors="ignore")
        for rec in _list_invoice_rows(rp.text, rp.url):
            number = rec.get("number")
            if number:
                entries[number] = rec

    downloaded, skipped, failed = [], [], []

    for number, rec in sorted(entries.items()):
        year = number[1:5] if len(number) >= 5 and number[1:5].isdigit() else "YYYY"
        month = number[5:7] if len(number) >= 7 and number[5:7].isdigit() else "MM"

        csv_target = csv_root / year / month / f"{number}.csv"
        csv_target.parent.mkdir(parents=True, exist_ok=True)

        if csv_target.exists():
            skipped.append(number)
            continue

        csv_url = rec.get("csv_url")
        pdf_url = rec.get("pdf_url")
        referer = list_url

        if csv_url and _try_download_csv(s, csv_url, csv_target, referer, verify=verify):
            raw_copy = raw_dir / f"{number}.csv"
            try:
                raw_copy.write_bytes(csv_target.read_bytes())
            except Exception:
                pass
            downloaded.append(str(csv_target.relative_to(base)))
            continue

        if pdf_url:
            pdf_path = raw_dir / f"{number}.pdf"
            if _try_download_pdf(s, pdf_url, pdf_path, referer, verify=verify):
                # optional PDF->CSV, if user has an extractor module available
                try:
                    from pl_pdf_invoice_extractor import read_pdf_text, parse_invoice_text  # optional
                    text = read_pdf_text(pdf_path)
                    items = parse_invoice_text(text) or []
                    import csv as _csv
                    with open(csv_target, "w", encoding="utf-8-sig", newline="") as f:
                        w = _csv.writer(f, delimiter=",")
                        w.writerow(["SČM", "Počet kusov"])
                        for it in items:
                            scm = (it.get("SCM") or "").strip()
                            qty = int(str(it.get("QTY") or "1").strip() or "1")
                            if scm:
                                w.writerow([scm, qty if qty > 0 else 1])
                    downloaded.append(str(csv_target.relative_to(base)))
                    continue
                except Exception:
                    pass

        failed.append(number)

    return {
        "from": date_from,
        "to": date_to,
        "found": len(entries),
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
        "raw_dir": str(raw_dir.relative_to(base)),
        "csv_dir": str(csv_root.relative_to(base)),
    }
