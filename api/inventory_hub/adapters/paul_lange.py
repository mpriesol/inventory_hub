from __future__ import annotations
import datetime as dt, re
from pathlib import Path
from typing import Dict, List, Optional
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

from ..invoices_util import (
    effective_layout_for_supplier, compute_paths, InvoiceIndex,
    prune_raw_if_needed, retention_cleanup_raw, now_stamp
)

# listing s dátumovým filtrom (funguje s aktívnou session)
PL_INVOICE_LIST_URL = "https://vo.paul-lange-oslany.sk/index.php?id=faktury&cmd=default&vystavena_od={od}&vystavena_do={do}"

CSV_ACCEPT_HEADERS = {"Accept": "*/*", "Accept-Language": "sk,cs;q=0.9,en-US;q=0.8,en;q=0.7"}
NAV_HEADERS = {"Sec-Fetch-Dest": "document","Sec-Fetch-Mode": "navigate","Sec-Fetch-Site": "same-origin","Upgrade-Insecure-Requests": "1"}
CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"


def _ensure_dirs(*dirs: Path):
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def _is_login_page(html: str) -> bool:
    """Heuristika – Page je ešte login stránka."""
    low = (html or "").lower()
    # rôzne varianty textu + query hints
    return (
        "prihlásenie" in low or
        "prihlasenie" in low or
        ("id=login" in low and "form" in low) or
        ("name=\"login\"" in low and "type=\"password\"" in low)
    )


def _norm_login_url(url: str | None) -> str:
    """Predvolená login URL – robustnejšia na webe PL."""
    u = (url or "").strip()
    if not u:
        return "https://vo.paul-lange-oslany.sk/index.php?cmd=default&id=login"
    # ak je iba id=login bez cmd, doplň cmd=default (výrazne zvyšuje úspech prvého GET)
    if "id=login" in u and "cmd=" not in u:
        sep = "&" if "?" in u else "?"
        u = f"{u}{sep}cmd=default"
    return u


def _session_with_defaults() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": CHROME_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "sk-SK,sk;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
    })
    return s


def _save_debug(path: Path, content: bytes | str):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8", errors="ignore")
        else:
            path.write_bytes(content)
    except Exception:
        pass


def _form_login_pl(session: requests.Session, login_url: str, username: str, password: str, verify: bool, logs_dir: Path) -> bool:
    """Načíta login stránku, zozbiera hidden inputy a odošle form s loginom."""
    r0 = session.get(login_url, timeout=30, verify=verify, allow_redirects=True)
    _save_debug(logs_dir / "login_get.html", r0.text)
    soup0 = BeautifulSoup(r0.text, "html.parser")
    form = soup0.find("form")
    if not form:
        # už sme prihlásení?
        return not _is_login_page(r0.text)

    action = form.get("action") or login_url
    action = urljoin(login_url, action)
    method = (form.get("method") or "post").lower()

    payload = {}
    user_name = None
    pass_name = None
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        itype = (inp.get("type") or "").lower()
        payload[name] = inp.get("value", "")
        if itype in {"text", "email"} and not user_name:
            user_name = name
        if itype == "password" and not pass_name:
            pass_name = name
    user_name = user_name or "login"
    pass_name = pass_name or "password"
    payload[user_name] = username or ""
    payload[pass_name] = password or ""

    # niektoré varianty vyžadujú cmd=login_proc
    if "id=login" in action and "cmd=" not in action:
        sep = "&" if "?" in action else "?"
        action = action + f"{sep}cmd=login_proc"

    origin = f"{urlparse(login_url).scheme}://{urlparse(login_url).netloc}"
    headers = {"Referer": login_url, "Origin": origin}

    if method == "get":
        session.get(action, params=payload, headers=headers, timeout=30, allow_redirects=True, verify=verify)
    else:
        session.post(action, data=payload, headers=headers, timeout=30, allow_redirects=True, verify=verify)

    # sanity check – otvor zoznam faktúr
    check = session.get("https://vo.paul-lange-oslany.sk/index.php?id=faktury&cmd=default",
                        timeout=30, verify=verify, allow_redirects=True)
    _save_debug(logs_dir / "post_login_check.html", check.text)
    return not _is_login_page(check.text)


def _http_get(s: requests.Session, url: str, verify: bool = True, **kw) -> requests.Response:
    r = s.get(url, timeout=60, verify=verify, **kw)
    r.raise_for_status()
    return r


def _looks_like_html_bytes(buf: bytes) -> bool:
    s = buf.lstrip().lower()
    return s.startswith(b"<!doctype") or s.startswith(b"<html") or b"<title>" in s[:2000]


def _is_csv_response(r: requests.Response, data_head: bytes) -> bool:
    ct = (r.headers.get("Content-Type") or "").lower()
    cd = r.headers.get("Content-Disposition") or ""
    head_text = data_head.decode("utf-8", errors="ignore")
    return (
        "text/csv" in ct or
        "application/csv" in ct or
        ("application/octet-stream" in ct and ".csv" in cd.lower()) or
        (("," in head_text or ";" in head_text) and not head_text.lower().lstrip().startswith(("<html", "<!doctype")))
    )


def _try_download(session: requests.Session, src_url: str, dest: Path, referer: str, verify: bool, debug_dir: Optional[Path], label: str):
    headers = {**CSV_ACCEPT_HEADERS, **NAV_HEADERS, "Referer": referer}
    try:
        r = session.get(src_url, headers=headers, timeout=45, stream=False, verify=verify, allow_redirects=True)
        data = r.content
        head = data[:4096]
        ok = r.status_code == 200 and not _looks_like_html_bytes(head) and _is_csv_response(r, head)
        if ok:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return True, r
        else:
            if debug_dir is not None:
                meta = f"URL: {src_url}\nReferer: {referer}\nStatus: {r.status_code}\nCT: {r.headers.get('Content-Type')}\nCD: {r.headers.get('Content-Disposition')}\nLen: {len(data)}\n"
                _save_debug(debug_dir / f"{label}.meta.txt", meta)
                _save_debug(debug_dir / f"{label}.body.bin", data)
            return False, r
    except Exception as e:
        if debug_dir is not None:
            _save_debug(debug_dir / f"{label}.error.txt", str(e))
        return False, None


def _list_invoice_rows(html_text: str, base_url: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html_text, "html.parser")
    rows: Dict[str, Dict[str, str]] = {}
    for tr in soup.select("table.faktury_zoznam tbody tr"):
        number = None
        a_num = tr.select_one("td.faktury_zoznam_cislo-faktury a[href]")
        if a_num:
            number = a_num.get_text(strip=True)
            detail_url = requests.compat.urljoin(base_url, a_num.get("href", ""))
        else:
            num_el = tr.select_one("td.faktury_zoznam_cislo-faktury div")
            if num_el:
                number = num_el.get_text(strip=True)
            detail_url = base_url
        if not number:
            for td in tr.find_all("td"):
                tx = (td.get_text(strip=True) or "")
                if re.match(r"F\d+", tx):
                    number = tx
                    break
        if not number:
            continue
        rec = rows.setdefault(number, {"number": number, "detail_url": detail_url})
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
    for a in soup.select('div.faktury_zoznam_pagination a[href], ul.pagination a[href]'):
        hrefs.add(requests.compat.urljoin(base_url, a.get("href", "")))
    return sorted(hrefs)


def _find_csv_on_detail(html: str, base_url: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select('a[href]'):
        href = a.get("href", "")
        text = (a.get_text(" ", strip=True) or "").lower()
        if "csv" in href.lower() or "csv" in text or "getcsv=" in href.lower():
            return requests.compat.urljoin(base_url, href)
    return None


def _extract_login_canonical(conf: dict) -> dict:
    """
    Očakáva kanoniku:
    conf["invoices"]["download"]["web"]["login"] s kľúčmi:
    mode, login_url, user_field, pass_field, username, password, cookie, insecure_all
    """
    try:
        login = conf["invoices"]["download"]["web"]["login"]
    except Exception:
        raise ValueError("Missing invoices.download.web.login in supplier config (canonical path).")

    mode = (login.get("mode") or "form").lower()
    login_url = _norm_login_url(login.get("login_url"))
    user_field = login.get("user_field", "login")
    pass_field = login.get("pass_field", "password")
    username = login.get("username", "")
    password = login.get("password", "")
    cookie   = login.get("cookie", "")
    insecure_all = bool(login.get("insecure_all", False))
    return {
        "mode": mode,
        "login_url": login_url,
        "user_field": user_field,
        "pass_field": pass_field,
        "username": username,
        "password": password,
        "cookie": cookie,
        "verify": not insecure_all,
    }

def refresh_paul_lange_invoices(data_root: Path, supplier: str, months_back: int, conf: dict) -> dict:
    base = data_root / "suppliers" / supplier
    raw_dir = base / "invoices" / "raw"
    csv_root = base / "invoices" / "csv"
    logs_dir = base / "logs"
    dbg_dir = logs_dir / "pl_download_debug"
    _ensure_dirs(raw_dir, csv_root, logs_dir, dbg_dir)

    layout = effective_layout_for_supplier(supplier, conf.get("invoices", {}) if isinstance(conf.get("invoices", {}), dict) else {})
    prune_raw_if_needed(base, layout)
    retention_cleanup_raw(base, layout)

    # ---- AUTH (robustný extraktor) ----
    login = _extract_login_canonical(conf)
    mode = login["mode"]
    login_url = login["login_url"]
    username = login["username"]
    password = login["password"]
    cookie = login["cookie"]
    verify = login["verify"]

    s = _session_with_defaults()

    if mode == "cookie" and cookie:
        for part in cookie.split(";"):
            part = part.strip()
            if part and "=" in part:
                k, v = part.split("=", 1)
                s.cookies.set(k.strip(), v.strip())

    if mode == "form":
        ok = _form_login_pl(s, login_url, username, password, verify, logs_dir)
        if not ok:
            raise RuntimeError("Paul-Lange login failed (still on login page).")

    today = dt.date.today()
    date_from = (today - dt.timedelta(days=30 * int(months_back or 3))).strftime("%Y-%m-%d")
    date_to   = today.strftime("%Y-%m-%d")
    list_url = PL_INVOICE_LIST_URL.format(od=date_from, do=date_to)

    r = _http_get(s, list_url, verify=verify)
    _save_debug(logs_dir / "pl_invoice_list.html", r.text)

    pages = _enumerate_pages(r.text, r.url)
    entries: Dict[str, Dict[str, str]] = {}
    for i, page in enumerate(pages, 1):
        rp = _http_get(s, page, verify=verify)
        _save_debug(logs_dir / f"pl_invoice_list_page_{i}.html", rp.text)
        for rec in _list_invoice_rows(rp.text, rp.url):
            number = rec.get("number")
            if number:
                entries[number] = rec

    idx = InvoiceIndex(base)
    downloaded, skipped, failed = [], [], []

    for number, rec in sorted(entries.items()):
        raw_target, csv_target, layout_used = compute_paths(base, supplier, layout, number, None, ".csv", "csv")

        if csv_target.exists():
            skipped.append(number)
            continue

        detail_url = rec.get("detail_url") or list_url
        csv_url = rec.get("csv_url")
        pdf_url = rec.get("pdf_url")

        ok = False

        if csv_url:
            ok, _ = _try_download(s, csv_url, csv_target, referer=detail_url, verify=verify, debug_dir=(dbg_dir / number), label="csv_list")

        if not ok:
            try:
                rd = s.get(detail_url, timeout=30, verify=verify, allow_redirects=True)
                _save_debug(dbg_dir / number / "detail.html", rd.text)
                csv2 = _find_csv_on_detail(rd.text, rd.url)
                if csv2:
                    ok, _ = _try_download(s, csv2, csv_target, referer=rd.url, verify=verify, debug_dir=(dbg_dir / number), label="csv_detail")
            except Exception as e:
                _save_debug(dbg_dir / number / "detail_error.txt", str(e))

        if not ok and pdf_url:
            raw_pdf, _, _ = compute_paths(base, supplier, layout, number, None, ".pdf", "raw")
            try:
                rpdf = s.get(pdf_url, headers={**CSV_ACCEPT_HEADERS, **NAV_HEADERS, "Referer": detail_url},
                             timeout=45, stream=True, verify=verify, allow_redirects=True)
                if rpdf.status_code == 200 and not _looks_like_html_bytes(rpdf.content[:4096]):
                    raw_pdf.parent.mkdir(parents=True, exist_ok=True)
                    raw_pdf.write_bytes(rpdf.content)
                    ok = True
            except Exception as e:
                _save_debug(dbg_dir / number / "pdf_error.txt", str(e))

        if ok:
            # CSV save je priorita; ak nie je, mohol sa uložiť PDF (raw)
            raw_path = None
            sha1 = ""
            try:
                from ..invoices_util import sha1_of_file
                if csv_target.exists():
                    sha1 = sha1_of_file(csv_target)
                else:
                    sha1 = sha1_of_file(raw_pdf)  # type: ignore
                    raw_path = str(raw_pdf.relative_to(base))  # type: ignore
            except Exception:
                sha1 = ""

            entry = {
                "supplier": supplier,
                "invoice_id": f"{supplier}:{number}",
                "number": number,
                "issue_date": None,
                "issue_date_source": "fallback:download",
                "downloaded_at": now_stamp(),
                "raw_path": (raw_path or (base / "invoices" / "raw" / f"{number}.csv").relative_to(base).as_posix()),
                "csv_path": csv_target.relative_to(base).as_posix(),
                "status": "new",
                "sha1": sha1,
                "layout_used": layout_used,
            }
            idx.append(entry)
            downloaded.append(str(csv_target.relative_to(base)))
        else:
            failed.append(number)

    return {
        "from": date_from,
        "to": date_to,
        "found": len(entries),
        "downloaded": downloaded,
        "skipped": skipped,
        "failed": failed,
        "csv_dir": str((base / "invoices" / "csv").relative_to(base)),
        "raw_dir": str((base / "invoices" / "raw").relative_to(base)),
        "index": "invoices/index.latest.json",
    }
