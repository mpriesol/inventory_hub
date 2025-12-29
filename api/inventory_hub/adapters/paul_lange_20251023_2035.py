
from __future__ import annotations
import datetime as dt
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

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

def _is_login_page(html: str) -> bool:
    low = (html or "").lower()
    return "prihlásenie" in low or "id=login" in low and "form" in low

def _form_login_pl(session: requests.Session, login_url: str, username: str, password: str, verify: bool, logs_dir: Path) -> bool:
    # GET login page
    r0 = session.get(login_url, timeout=30, verify=verify, allow_redirects=True)
    (logs_dir / "login_get.html").write_text(r0.text, encoding="utf-8", errors="ignore")
    soup0 = BeautifulSoup(r0.text, "html.parser")
    form = soup0.find("form")
    if not form:
        # niekedy už sme prihlásení
        return True

    action = form.get("action") or login_url
    action = urljoin(login_url, action)
    method = (form.get("method") or "post").lower()

    # nazbieraj default hodnoty z inputov
    payload = {}
    user_name = None
    pass_name = None
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        itype = (inp.get("type") or "").lower()
        val = inp.get("value", "")
        payload[name] = val
        if itype in {"text", "email"} and not user_name:
            user_name = name
        if itype == "password" and not pass_name:
            pass_name = name

    # doplň mená ak chýbajú
    user_name = user_name or "login"
    pass_name = pass_name or "password"

    payload[user_name] = username or ""
    payload[pass_name] = password or ""

    # ak action je iba ?id=login, doplň cmd=login_proc
    if "id=login" in action and "cmd=" not in action:
        sep = "&" if "?" in action else "?"
        action = action + f"{sep}cmd=login_proc"

    origin = f"{urlparse(login_url).scheme}://{urlparse(login_url).netloc}"
    headers = {"Referer": login_url, "Origin": origin}

    # POST login
    if method == "get":
        session.get(action, params=payload, headers=headers, timeout=30, allow_redirects=True, verify=verify)
    else:
        session.post(action, data=payload, headers=headers, timeout=30, allow_redirects=True, verify=verify)

    # sanity check – sme prihlásení?
    check = session.get("https://vo.paul-lange-oslany.sk/index.php?id=faktury&cmd=default", timeout=30, verify=verify, allow_redirects=True)
    (logs_dir / "post_login_check.html").write_text(check.text, encoding="utf-8", errors="ignore")
    return not _is_login_page(check.text)
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

def _is_csv_response(r: requests.Response, data_head: bytes) -> bool:
    ct = (r.headers.get("Content-Type") or "").lower()
    cd = r.headers.get("Content-Disposition") or ""
    head_text = data_head.decode("utf-8", errors="ignore")
    return (
        "text/csv" in ct
        or "application/csv" in ct
        or "application/octet-stream" in ct and ".csv" in cd.lower()
        or ("," in head_text or ";" in head_text) and not head_text.lower().lstrip().startswith(("<html", "<!doctype"))
    )

def _save_debug(path: Path, content: bytes):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    except Exception:
        pass

def _try_download(session: requests.Session, src_url: str, dest: Path, referer: str, verify: bool, debug_dir: Optional[Path], label: str) -> Tuple[bool, Optional[requests.Response]]:
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
                _save_debug(debug_dir / f"{label}.meta.txt", meta.encode("utf-8", errors="ignore"))
                _save_debug(debug_dir / f"{label}.body.bin", data)
            return False, r
    except Exception as e:
        if debug_dir is not None:
            _save_debug(debug_dir / f"{label}.error.txt", str(e).encode("utf-8", errors="ignore"))
        return False, None

def _list_invoice_rows(html_text: str, base_url: str) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html_text, "html.parser")
    rows: Dict[str, Dict[str, str]] = {}
    for tr in soup.select("table.faktury_zoznam tbody tr"):
        number = None
        # number & detail link
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
            # fallback by regex
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

def _find_csv_on_detail(html: str, base_url: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    # hľadaj linky s csv/export textom alebo getcsv parametrom
    for a in soup.select('a[href]'):
        href = a.get("href", "")
        text = (a.get_text(" ", strip=True) or "").lower()
        if "csv" in href.lower() or "csv" in text or "getcsv=" in href.lower():
            return requests.compat.urljoin(base_url, href)
    return None

def _enumerate_pages(html_text: str, base_url: str) -> List[str]:
    """
    Vráti zoznam všetkých stránok zoznamu faktúr (vrátane prvej).
    Funguje aj keď pagination nie je, vtedy vráti len base_url.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    hrefs = set([base_url])

    # typicky: <div class="faktury_zoznam_pagination">…</div>
    for a in soup.select('div.faktury_zoznam_pagination a[href], ul.pagination a[href]'):
        href = a.get("href", "")
        if not href:
            continue
        full = requests.compat.urljoin(base_url, href)
        hrefs.add(full)

    # zachovaj deterministické poradie
    return sorted(hrefs)

def _is_login_page(html: str) -> bool:
    low = (html or "").lower()
    return "prihlásenie" in low or "id=login" in low

def refresh_paul_lange_invoices(data_root: Path, supplier: str, months_back: int, conf: dict) -> dict:
    base = data_root / "suppliers" / supplier
    raw_dir = base / "invoices" / "raw"
    csv_root = base / "invoices" / "csv"
    logs_dir = base / "logs"
    dbg_dir = logs_dir / "pl_download_debug"
    _ensure_dirs(raw_dir, csv_root, logs_dir, dbg_dir)

    # --- auth
    mode = (conf.get("auth_mode") or "form").lower()
    login_url = conf.get("login_url") or "https://vo.paul-lange-oslany.sk/index.php?id=login"
    user_field = (conf.get("user_field") or "").strip()  # už nemusíme spoliehať sa na toto
    pass_field = (conf.get("pass_field") or "").strip()
    username   = conf.get("username") or conf.get("basic_user") or ""
    password   = conf.get("password") or conf.get("basic_pass") or ""
    cookie     = conf.get("cookie") or ""
    insecure   = bool(conf.get("insecure_all", False))

    s = requests.Session()
    s.headers.update({
        "User-Agent": CHROME_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "sk-SK,sk;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
    })
    verify = not insecure

    if mode == "cookie" and cookie:
        for part in cookie.split(";"):
            part = part.strip()
            if part and "=" in part:
                k, v = part.split("=", 1)
                s.cookies.set(k.strip(), v.strip())

    if mode == "form":
        ok = _form_login_pl(s, login_url, username, password, verify, logs_dir)
        if not ok:
            raise RuntimeError("Paul-Lange login failed (still on login page). Skontroluj login/heslo alebo použi auth.mode=cookie na overenie.")


    # if mode == "form":
    #     r0 = s.get(login_url, timeout=30, verify=verify, allow_redirects=True)
    #     soup0 = BeautifulSoup(r0.text, "html.parser")
    #     form = soup0.find("form")
    #     form_action = login_url
    #     method = "post"
    #     payload = {}
    #     inputs = form.find_all("input") if form else []
    #     if form:
    #         form_action = form.get("action") or login_url
    #         method = (form.get("method") or "post").lower()
    #     names = {i.get("name") for i in inputs if i.get("name")}
    #     if "USERNAME" not in names:
    #         cand = soup0.select('input[type="text"],input[type="email"]')
    #         if cand:
    #             names.add(cand[0].get("name"))
    #     if "PASSWORD" not in names:
    #         candp = soup0.select('input[type="password"]')
    #         if candp:
    #             names.add(candp[0].get("name"))
    #     payload[user_field or "USERNAME"] = username or ""
    #     payload[pass_field or "PASSWORD"] = password or ""
    #     action_url = requests.compat.urljoin(login_url, form_action)
    #     if "id=login" in action_url and "cmd=" not in action_url:
    #         sep = "&" if "?" in action_url else "?"
    #         action_url = action_url + f"{sep}cmd=login_proc"
    #     s.post(action_url, data=payload, headers={"Referer": login_url}, timeout=30, allow_redirects=True, verify=verify)
    #     check = s.get("https://vo.paul-lange-oslany.sk/index.php?id=faktury&cmd=default",
    #           timeout=30, allow_redirects=True, verify=verify)
    #     if _is_login_page(check.text):
    #         raise RuntimeError("Paul-Lange login failed (still on login page). Skontroluj user_field='login', pass_field='password', prihlasovacie údaje alebo použi auth.mode=cookie na overenie.")

    # --- list
    today = dt.date.today()
    date_from = (today - dt.timedelta(days=30 * int(months_back or 3))).strftime("%Y-%m-%d")
    date_to   = today.strftime("%Y-%m-%d")
    list_url = PL_INVOICE_LIST_URL.format(od=date_from, do=date_to)

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
            skipped.append(number); continue

        detail_url = rec.get("detail_url") or list_url
        csv_url = rec.get("csv_url")
        pdf_url = rec.get("pdf_url")

        # 1st try: CSV from list link
        ok, _ = (False, None)
        if csv_url:
            ok, resp = _try_download(s, csv_url, csv_target, referer=detail_url, verify=verify, debug_dir=(dbg_dir / number), label="csv_list")
        if ok:
            (raw_dir / f"{number}.csv").write_bytes(csv_target.read_bytes())
            downloaded.append(str(csv_target.relative_to(base)))
            continue

        # 2nd try: open detail page → parse csv link there → download
        try:
            rd = s.get(detail_url, timeout=30, verify=verify, allow_redirects=True)
            (dbg_dir / number / "detail.html").write_text(rd.text, encoding="utf-8", errors="ignore")
            csv2 = _find_csv_on_detail(rd.text, rd.url)
            if csv2:
                ok2, _ = _try_download(s, csv2, csv_target, referer=rd.url, verify=verify, debug_dir=(dbg_dir / number), label="csv_detail")
                if ok2:
                    (raw_dir / f"{number}.csv").write_bytes(csv_target.read_bytes())
                    downloaded.append(str(csv_target.relative_to(base)))
                    continue
        except Exception as e:
            _save_debug(dbg_dir / number / "detail_error.txt", str(e).encode("utf-8"))

        # 3rd try: PDF fallback
        if pdf_url:
            ok_pdf, resp_pdf = _try_download(s, pdf_url, raw_dir / f"{number}.pdf", referer=detail_url, verify=verify, debug_dir=(dbg_dir / number), label="pdf")
            if ok_pdf:
                # optional: try PDF→CSV if extractor available
                try:
                    from pl_pdf_invoice_extractor import read_pdf_text, parse_invoice_text  # optional
                    text = read_pdf_text(raw_dir / f"{number}.pdf")
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
                except Exception as e:
                    _save_debug(dbg_dir / number / "pdf_parse_error.txt", str(e).encode("utf-8"))

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
        "debug_dir": str(dbg_dir.relative_to(base)),
    }
