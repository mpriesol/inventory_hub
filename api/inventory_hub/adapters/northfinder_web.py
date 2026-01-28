# -*- coding: utf-8 -*-
"""
Northfinder B2B Invoice Downloader

Uses Playwright for persistent session authentication and downloads invoices
via DataTables API + direct file downloads.

Mode: Playwright with persistent storage_state (cookies/localStorage)

Flow:
1. Try to use existing storage_state to access authenticated page
2. If not authenticated, perform login and save new storage_state
3. Fetch invoice list via DataTables AJAX API
4. For each invoice, fetch detail page to get download links
5. Download XLSX and PDF files
6. Convert XLSX to canonical CSV
7. Update index.latest.json
"""
from __future__ import annotations

import json
import logging
import re
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional
from urllib.parse import urljoin

import requests

logger = logging.getLogger(__name__)

# Try to import Playwright - if not available, we'll use requests-only mode
try:
    from playwright.sync_api import sync_playwright, Browser, Page, BrowserContext
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    logger.warning("Playwright not installed - will use cookies-only mode if available")


@dataclass
class NorthfinderConfig:
    """Configuration for Northfinder invoice download"""
    base_url: str = "https://b2b.northfinder.com"
    login_url: str = "https://b2b.northfinder.com/sk/login"
    username: str = ""
    password: str = ""
    storage_state_path: str = ""
    
    # Selectors for login
    cookie_accept_text: str = "Súhlasím"
    user_selector: str = "input[type='email'], input[name='email']"
    pass_selector: str = "input[type='password']"
    submit_selector: str = "button[type='submit']"
    
    # API endpoints
    invoices_list_url: str = "https://b2b.northfinder.com/invoice_list"
    datatable_ajax_url: str = "https://b2b.northfinder.com/sk/module/asdata_dashboard/invoice_list"
    invoice_detail_base: str = "https://b2b.northfinder.com/invoice_detail/"
    download_pdf_template: str = "https://b2b.northfinder.com/sk/module/asdata_dashboard/download_invoice_pdf?invoice_id={invoice_id}"
    
    months_back: int = 3
    
    @classmethod
    def from_supplier_config(cls, cfg: Dict[str, Any], data_root: Path, supplier_code: str) -> "NorthfinderConfig":
        """Build config from supplier config.json"""
        invoices = cfg.get("invoices", {})
        download = invoices.get("download", {})
        web = download.get("web", {})
        login = web.get("login", {})
        selectors = login.get("selectors", {})
        
        # Default storage state path
        default_state_path = f"suppliers/{supplier_code}/state/storage_state.json"
        state_path = login.get("storage_state_path", default_state_path)
        
        # Make absolute if relative
        if not state_path.startswith("/"):
            state_path = str(data_root / state_path)
        
        return cls(
            base_url=web.get("base_url", cls.base_url),
            login_url=login.get("login_url", cls.login_url),
            username=login.get("username", ""),
            password=login.get("password", ""),
            storage_state_path=state_path,
            cookie_accept_text=selectors.get("cookie_accept_text", cls.cookie_accept_text),
            user_selector=selectors.get("user", cls.user_selector),
            pass_selector=selectors.get("pass", cls.pass_selector),
            submit_selector=selectors.get("submit", cls.submit_selector),
            invoices_list_url=web.get("invoices_list_url", cls.invoices_list_url),
            datatable_ajax_url=web.get("datatable_ajax_url", cls.datatable_ajax_url),
            invoice_detail_base=web.get("invoice_detail_base", cls.invoice_detail_base),
            download_pdf_template=web.get("download_pdf_template", cls.download_pdf_template),
            months_back=invoices.get("months_back_default", 3),
        )


@dataclass
class RefreshResult:
    """Result of invoice refresh operation"""
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    converted: int = 0
    pages: int = 0
    invoices: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    log_files: List[str] = field(default_factory=list)


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _sha1_file(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_filename(s: str) -> str:
    """Convert string to safe filename"""
    s = re.sub(r'[<>:"/\\|?*]', '_', s)
    s = re.sub(r'\s+', '_', s)
    return s[:100]  # Limit length


class NorthfinderInvoiceDownloader:
    """
    Downloads invoices from Northfinder B2B portal.
    
    Uses Playwright for authentication with persistent session storage.
    Falls back to requests-only mode if cookies are available.
    """
    
    def __init__(self, config: NorthfinderConfig, data_root: Path, supplier_code: str):
        self.config = config
        self.data_root = data_root
        self.supplier_code = supplier_code
        
        # Paths
        self.supplier_dir = data_root / "suppliers" / supplier_code
        self.raw_dir = self.supplier_dir / "invoices" / "raw"
        self.csv_dir = self.supplier_dir / "invoices" / "csv"
        self.pdf_dir = self.supplier_dir / "invoices" / "pdf"
        self.state_dir = self.supplier_dir / "state"
        self.logs_dir = self.supplier_dir / "logs"
        self.index_path = self.supplier_dir / "invoices" / "index.latest.json"
        
        # Ensure directories
        for d in [self.raw_dir, self.csv_dir, self.pdf_dir, self.state_dir, self.logs_dir]:
            _ensure_dir(d)
        
        self.session: Optional[requests.Session] = None
        self.result = RefreshResult()
    
    def _load_storage_state(self) -> Optional[Dict[str, Any]]:
        """Load Playwright storage state if exists"""
        state_path = Path(self.config.storage_state_path)
        if state_path.exists():
            try:
                return json.loads(state_path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"Failed to load storage state: {e}")
        return None
    
    def _save_storage_state(self, state: Dict[str, Any]) -> None:
        """Save Playwright storage state"""
        state_path = Path(self.config.storage_state_path)
        _ensure_dir(state_path.parent)
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        logger.info(f"Saved storage state to {state_path}")
    
    def _build_session_from_state(self, state: Dict[str, Any]) -> requests.Session:
        """Build requests.Session from Playwright storage state"""
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "sk-SK,sk;q=0.9,en-US;q=0.8,en;q=0.7",
        })
        
        # Extract cookies from storage state
        for cookie in state.get("cookies", []):
            session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )
        
        return session
    
    def _playwright_login(self) -> Dict[str, Any]:
        """
        Perform login using Playwright and return storage state.
        
        Returns:
            Storage state dict with cookies and localStorage
        
        Raises:
            RuntimeError: If blocked by Cloudflare or login fails
        """
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright not installed. Run: pip install playwright && playwright install chromium")
        
        logger.info("Starting Playwright login...")
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            
            try:
                # Navigate to login page with longer timeout
                logger.info(f"Navigating to {self.config.login_url}")
                page.goto(self.config.login_url, wait_until="domcontentloaded", timeout=60000)
                
                # Check for Cloudflare challenge
                page_title = page.title() or ""
                page_html = page.content()
                
                is_cloudflare_blocked = (
                    "just a moment" in page_title.lower() or
                    "cloudflare" in page_title.lower() or
                    "cf-turnstile" in page_html.lower() or
                    "challenge-platform" in page_html.lower() or
                    "cdn-cgi/challenge" in page_html.lower() or
                    "cf-mitigated" in page_html.lower()
                )
                
                if is_cloudflare_blocked:
                    # Save diagnostics
                    self._save_cloudflare_diagnostics(page, "login_blocked")
                    raise RuntimeError(
                        "Blocked by Cloudflare Turnstile challenge. "
                        "The VPS IP is blocked. Options:\n"
                        "1) Ask Northfinder to allowlist your VPS IP\n"
                        "2) Use manual invoice upload instead\n"
                        "3) Login from a different network and export storage_state.json"
                    )
                
                logger.info(f"Page loaded, title: {page_title}")
                
                # Wait a bit for any JS to load
                page.wait_for_timeout(2000)
                
                # Accept cookies if dialog appears
                try:
                    cookie_btn = page.locator(f"text={self.config.cookie_accept_text}")
                    if cookie_btn.count() > 0:
                        cookie_btn.first.click(timeout=3000)
                        page.wait_for_timeout(500)
                except Exception:
                    pass  # Cookie dialog may not appear
                
                # Check if login form exists
                user_field = page.locator(self.config.user_selector)
                if user_field.count() == 0:
                    self._save_cloudflare_diagnostics(page, "no_login_form")
                    raise RuntimeError(
                        f"Login form not found. Page title: '{page_title}'. "
                        "Possible Cloudflare block or page structure changed."
                    )
                
                # Fill login form
                logger.info("Filling login form...")
                user_field.fill(self.config.username)
                page.locator(self.config.pass_selector).fill(self.config.password)
                
                # Submit
                page.locator(self.config.submit_selector).first.click()
                
                # Wait for navigation with timeout
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=30000)
                except Exception as e:
                    self._save_cloudflare_diagnostics(page, "post_login_timeout")
                    raise RuntimeError(f"Timeout after login submit: {e}")
                
                # Check for Cloudflare again after login attempt
                page_html = page.content()
                if "cf-turnstile" in page_html.lower() or "challenge-platform" in page_html.lower():
                    self._save_cloudflare_diagnostics(page, "post_login_cloudflare")
                    raise RuntimeError("Cloudflare challenge appeared after login attempt")
                
                # Check if login successful
                current_url = page.url
                if "login" in current_url.lower():
                    # Still on login page - check for error message
                    error_text = ""
                    try:
                        error_el = page.locator(".alert-danger, .error, .login-error")
                        if error_el.count() > 0:
                            error_text = error_el.first.text_content() or ""
                    except Exception:
                        pass
                    
                    self._save_cloudflare_diagnostics(page, "login_failed")
                    raise RuntimeError(f"Login failed: {error_text or 'Still on login page after submit'}")
                
                logger.info(f"Login successful, current URL: {current_url}")
                
                # Get storage state
                state = context.storage_state()
                return state
                
            finally:
                browser.close()
    
    def _save_cloudflare_diagnostics(self, page, prefix: str) -> None:
        """Save screenshot and HTML for debugging Cloudflare issues"""
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            
            # Save screenshot
            screenshot_path = self.logs_dir / f"{prefix}_{ts}.png"
            page.screenshot(path=str(screenshot_path))
            logger.info(f"Saved diagnostic screenshot: {screenshot_path}")
            
            # Save HTML
            html_path = self.logs_dir / f"{prefix}_{ts}.html"
            html_path.write_text(page.content(), encoding="utf-8")
            logger.info(f"Saved diagnostic HTML: {html_path}")
            
        except Exception as e:
            logger.warning(f"Failed to save diagnostics: {e}")
    
    def _check_authenticated(self, session: requests.Session) -> bool:
        """Check if session is authenticated by fetching a protected page"""
        try:
            resp = session.get(self.config.invoices_list_url, timeout=10, allow_redirects=False)
            # If redirected to login, not authenticated
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location", "")
                if "login" in location.lower():
                    return False
            # If we get the page content, check for login indicators
            if resp.status_code == 200:
                if "login" in resp.url.lower():
                    return False
                # Check content for login form
                if '<input type="password"' in resp.text or 'id="login-form"' in resp.text:
                    return False
                return True
            return False
        except Exception as e:
            logger.warning(f"Auth check failed: {e}")
            return False
    
    def _ensure_authenticated(self) -> requests.Session:
        """Ensure we have an authenticated session"""
        # Try existing storage state first
        state = self._load_storage_state()
        
        if state:
            logger.info("Trying existing storage state...")
            session = self._build_session_from_state(state)
            if self._check_authenticated(session):
                logger.info("Existing session is valid")
                self.session = session
                return session
            logger.info("Existing session expired, re-authenticating...")
        
        # Need to login
        if not self.config.username or not self.config.password:
            raise RuntimeError("No valid session and no credentials configured")
        
        state = self._playwright_login()
        self._save_storage_state(state)
        
        session = self._build_session_from_state(state)
        if not self._check_authenticated(session):
            raise RuntimeError("Login completed but session still not authenticated")
        
        self.session = session
        return session
    
    def _fetch_invoice_list(self) -> List[Dict[str, Any]]:
        """
        Fetch invoice list using DataTables AJAX API.
        
        Returns:
            List of invoice records from DataTables response
        """
        session = self._ensure_authenticated()
        
        # Calculate date range
        today = datetime.now()
        date_from = (today - timedelta(days=self.config.months_back * 30)).strftime("%Y-%m-%d")
        date_to = today.strftime("%Y-%m-%d")
        
        all_invoices = []
        start = 0
        length = 100  # Page size
        
        while True:
            # DataTables server-side request
            params = {
                "ajax": "1",
                "action": "filterInvoices",
                "time_filter": "own_interval",
                "time_from": date_from,
                "time_to": date_to,
                "draw": str(start // length + 1),
                "start": str(start),
                "length": str(length),
                "order[0][column]": "0",
                "order[0][dir]": "desc",
            }
            
            logger.info(f"Fetching invoices page {start // length + 1} (start={start})...")
            self.result.pages += 1
            
            resp = session.get(
                self.config.datatable_ajax_url,
                params=params,
                headers={"X-Requested-With": "XMLHttpRequest"},
                timeout=30,
            )
            resp.raise_for_status()
            
            data = resp.json()
            records = data.get("data", [])
            total = data.get("recordsTotal", 0)
            
            if not records:
                break
            
            all_invoices.extend(records)
            start += length
            
            if start >= total:
                break
        
        logger.info(f"Fetched {len(all_invoices)} invoices total")
        return all_invoices
    
    def _parse_invoice_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Parse DataTables record into standardized format"""
        # DataTables returns data as dict with column names
        row_id = record.get("DT_RowId", "")
        
        # Try to extract numeric ID
        if row_id.startswith("row_"):
            row_id = row_id[4:]
        
        return {
            "row_id": row_id,
            "number": record.get("CisloDokladu", ""),
            "company": record.get("company", ""),
            "total_base": record.get("SumaZakladCM", ""),
            "total": record.get("SumaCelkemCM", ""),
            "currency": record.get("mena", "EUR"),
            "issue_date": record.get("DatumVystaveni", ""),
            "due_date": record.get("DatumSplatnosti", ""),
            "paid": record.get("uhrazeno", ""),
            "remaining": record.get("zbyvauhradit", ""),
        }
    
    def _fetch_invoice_detail(self, row_id: str) -> Dict[str, Any]:
        """
        Fetch invoice detail page and extract download links.
        
        Returns:
            Dict with download URLs and invoice metadata
        """
        detail_url = f"{self.config.invoice_detail_base}{row_id}?content_only=1&fancybox=1"
        
        resp = self.session.get(detail_url, timeout=30)
        resp.raise_for_status()
        
        html = resp.text
        result = {
            "invoice_id": None,
            "xlsx_url": None,
            "pdf_url": None,
            "csv_url": None,
        }
        
        # Look for invoice_id (GUID) in download links
        # Pattern: download_invoice_pdf?invoice_id=<GUID>
        guid_match = re.search(r'invoice_id=([a-f0-9-]{36})', html, re.I)
        if guid_match:
            result["invoice_id"] = guid_match.group(1)
        
        # Look for download links
        # XLSX/XLS export link
        xlsx_match = re.search(r'href=["\']([^"\']*(?:\.xlsx?|export[^"\']*xlsx?)[^"\']*)["\']', html, re.I)
        if xlsx_match:
            result["xlsx_url"] = urljoin(self.config.base_url, xlsx_match.group(1))
        
        # Alternative: look for export button with specific text
        export_match = re.search(r'href=["\']([^"\']+)["\'][^>]*>(?:[^<]*)?(?:Export|XLS|Excel)', html, re.I)
        if export_match and not result["xlsx_url"]:
            result["xlsx_url"] = urljoin(self.config.base_url, export_match.group(1))
        
        # PDF download - use template if we have invoice_id
        if result["invoice_id"]:
            result["pdf_url"] = self.config.download_pdf_template.format(invoice_id=result["invoice_id"])
        
        return result
    
    def _download_file(self, url: str, target_path: Path) -> bool:
        """Download file from URL"""
        try:
            resp = self.session.get(url, timeout=60, stream=True)
            resp.raise_for_status()
            
            with open(target_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            return target_path.stat().st_size > 0
        except Exception as e:
            logger.error(f"Failed to download {url}: {e}")
            return False
    
    def _load_existing_index(self) -> Dict[str, Any]:
        """
        Load existing invoice index.
        Returns a dict mapping invoice_id -> entry (canonical format).
        """
        if not self.index_path.exists():
            return {}
        
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        
        # Already a mapping: { "<invoice_id>": {entry}, ... }
        if isinstance(data, dict):
            # Legacy format: {"invoices": [...], "updated_at": ...}
            if "invoices" in data and isinstance(data["invoices"], list):
                out: Dict[str, Any] = {}
                for i, it in enumerate(data["invoices"]):
                    if not isinstance(it, dict):
                        continue
                    key = str(it.get("invoice_id") or it.get("row_id") or it.get("number") or f"nf_{i}")
                    out[key] = it
                return out
            
            # Filter out non-entry values (updated_at, etc.)
            return {k: v for k, v in data.items() if isinstance(v, dict)}
        
        # Legacy: list of entries
        if isinstance(data, list):
            out: Dict[str, Any] = {}
            for i, it in enumerate(data):
                if not isinstance(it, dict):
                    continue
                key = str(it.get("invoice_id") or it.get("row_id") or it.get("number") or f"nf_{i}")
                out[key] = it
            return out
        
        return {}
    
    def _save_index(self, index: Dict[str, Any]) -> None:
        """Save invoice index as dict mapping invoice_id -> entry (canonical format)."""
        self.index_path.write_text(
            json.dumps(index, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    
    def refresh(self) -> RefreshResult:
        """
        Main refresh method - downloads all new invoices.
        
        Returns:
            RefreshResult with statistics
        """
        from .northfinder_xlsx_parser import parse_xlsx_to_csv
        
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = self.logs_dir / f"invoices_refresh_{ts}.log"
        
        # Always set log_files (even if we fail early)
        try:
            log_relpath = str(log_path.relative_to(self.data_root))
        except ValueError:
            log_relpath = str(log_path)
        self.result.log_files = [log_relpath]
        
        # Setup file logging
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        logger.addHandler(file_handler)
        
        try:
            logger.info(f"Starting Northfinder invoice refresh for {self.supplier_code}")
            
            # Load existing index (now returns dict mapping invoice_id -> entry)
            index = self._load_existing_index()
            existing_numbers = {v.get("number") for v in index.values() if isinstance(v, dict) and v.get("number")}
            
            # Fetch invoice list
            invoice_list = self._fetch_invoice_list()
            
            for record in invoice_list:
                inv = self._parse_invoice_record(record)
                inv_number = inv["number"]
                row_id = inv["row_id"]
                
                if not inv_number:
                    logger.warning(f"Skipping invoice with empty number, row_id={row_id}")
                    continue
                
                safe_name = _safe_filename(inv_number)
                xlsx_path = self.raw_dir / f"{safe_name}__{row_id}.xlsx"
                pdf_path = self.pdf_dir / f"{safe_name}__{row_id}.pdf"
                csv_path = self.csv_dir / f"{safe_name}.csv"
                
                # Check if already downloaded
                if xlsx_path.exists() and xlsx_path.stat().st_size > 0:
                    logger.info(f"Skipping {inv_number} - already downloaded")
                    self.result.skipped += 1
                    
                    # Still ensure it's in index (use row_id as key)
                    invoice_id = str(row_id)
                    if invoice_id not in index:
                        index_entry = self._build_index_entry(inv, xlsx_path, csv_path, pdf_path)
                        index[invoice_id] = index_entry
                        existing_numbers.add(inv_number)
                    continue
                
                logger.info(f"Processing invoice {inv_number} (row_id={row_id})")
                
                try:
                    # Get download URLs from detail page
                    detail = self._fetch_invoice_detail(row_id)
                    
                    xlsx_downloaded = False
                    pdf_downloaded = False
                    
                    # Download XLSX
                    if detail.get("xlsx_url"):
                        if self._download_file(detail["xlsx_url"], xlsx_path):
                            xlsx_downloaded = True
                            logger.info(f"Downloaded XLSX: {xlsx_path.name}")
                    
                    # Download PDF
                    if detail.get("pdf_url"):
                        if self._download_file(detail["pdf_url"], pdf_path):
                            pdf_downloaded = True
                            logger.info(f"Downloaded PDF: {pdf_path.name}")
                    
                    if not xlsx_downloaded:
                        logger.error(f"Failed to download XLSX for {inv_number}")
                        self.result.failed += 1
                        self.result.errors.append(f"No XLSX download for {inv_number}")
                        continue
                    
                    self.result.downloaded += 1
                    
                    # Convert XLSX to CSV
                    parse_result = parse_xlsx_to_csv(xlsx_path, csv_path)
                    if parse_result["success"]:
                        self.result.converted += 1
                        logger.info(f"Converted to CSV: {parse_result['rows_parsed']} rows")
                    else:
                        logger.error(f"Failed to convert XLSX: {parse_result['error']}")
                        self.result.errors.append(f"XLSX conversion failed for {inv_number}: {parse_result['error']}")
                    
                    # Add to index (use row_id as key)
                    invoice_id = str(row_id)
                    index_entry = self._build_index_entry(inv, xlsx_path, csv_path, pdf_path if pdf_downloaded else None)
                    index[invoice_id] = index_entry
                    existing_numbers.add(inv_number)
                    
                except Exception as e:
                    logger.exception(f"Failed to process invoice {inv_number}: {e}")
                    self.result.failed += 1
                    self.result.errors.append(f"{inv_number}: {str(e)}")
            
            # Save updated index
            self._save_index(index)
            
            self.result.invoices = list(index.values())
            
            logger.info(
                f"Refresh complete: downloaded={self.result.downloaded}, "
                f"skipped={self.result.skipped}, failed={self.result.failed}, "
                f"converted={self.result.converted}"
            )
            
        except Exception as e:
            logger.exception(f"Invoice refresh failed: {e}")
            self.result.errors.append(str(e))
        finally:
            logger.removeHandler(file_handler)
            file_handler.close()
        
        return self.result
    
    def _build_index_entry(
        self,
        inv: Dict[str, Any],
        xlsx_path: Path,
        csv_path: Path,
        pdf_path: Optional[Path],
    ) -> Dict[str, Any]:
        """Build invoice index entry"""
        entry = {
            "supplier": self.supplier_code,
            "invoice_id": inv["row_id"],
            "number": inv["number"],
            "issue_date": inv.get("issue_date"),
            "due_date": inv.get("due_date"),
            "total": inv.get("total"),
            "currency": inv.get("currency", "EUR"),
            "status": "new",
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
        }
        
        if xlsx_path.exists():
            entry["raw_path"] = str(xlsx_path.relative_to(self.data_root))
            entry["sha1"] = _sha1_file(xlsx_path)
        
        if csv_path.exists():
            entry["csv_path"] = str(csv_path.relative_to(self.data_root))
        
        if pdf_path and pdf_path.exists():
            entry["pdf_path"] = str(pdf_path.relative_to(self.data_root))
        
        return entry


def refresh_invoices_web(
    data_root: Path,
    supplier_code: str,
    supplier_config: Dict[str, Any],
    months_back: int = 3,
) -> RefreshResult:
    """
    Main entry point for Northfinder invoice refresh.
    
    Args:
        data_root: Path to inventory-data root
        supplier_code: Supplier code (e.g., "northfinder")
        supplier_config: Supplier config dict from config.json
        months_back: How many months back to fetch
    
    Returns:
        RefreshResult with statistics
    """
    config = NorthfinderConfig.from_supplier_config(supplier_config, data_root, supplier_code)
    config.months_back = months_back
    
    downloader = NorthfinderInvoiceDownloader(config, data_root, supplier_code)
    return downloader.refresh()
