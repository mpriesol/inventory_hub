"""Synthetic invoice fixtures only; no customer PDF or financial records in Git."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from inventory_hub import config_io
from inventory_hub.adapters import sk_profi_bike_invoice as adapter
from inventory_hub.routers import suppliers, receiving_db


def invoice_text():
    # EUR 0.03 per line gives 0.01 tax each, but total invoice tax is 0.02.
    return """SK - PROFI BIKE, s.r.o.
Dátum vystavenia  Dátum dodania  Dátum splatnosti  Variabilný symbol
01.02.2026  01.02.2026  15.02.2026  99999999
Faktúra 9/26/00999
Interná referencia  Name  Množstvo  Jednotková cena  DPH  Celkom bez DPH  Celkom s DPH
SO-SYNTHETIC-ONLY
1 DEMO-A Test product alpha 1,00 Ks 0,03 23 % 0,03 € 0,04 €
2 DEMO-B Test product beta  1,00 Ks 0,03 23 % 0,03 € 0,04 €
3 DEMO-C Test product gamma 1,00 Ks 0,03 23 % 0,03 € 0,04 €
Suma bez DPH 0,09 €
DPH 23 % 0,02 €
Celkom 0,11 €
Splatná suma 0,11 €
"""


def discount_invoice_pages():
    """Four synthetic pages, including a free item before a summary-only page."""
    header = (
        "     Interná            Name                                 Množstvo  Jednotková cena  Zľava  DPH  Celkom bez  Celkom s\n"
        "     referencia                                                                           %             DPH       DPH\n"
    )

    def row(number, sku, title, qty, unit, price, discount, net, gross):
        return (
            f"{number:<4}{sku:<20}{title:<36}{qty:>8} {unit:<4}{price:>10}"
            f"{discount:>9}    23 % {net:>10} € {gross:>10} €\n"
        )

    identity = "SK - PROFI BIKE, s.r.o.\nFaktúra 9/26/00999\n"
    dates = (
        "Dátum vystavenia  Dátum dodania  Dátum splatnosti  Variabilný symbol\n"
        "01.02.2026  01.02.2026  15.02.2026  99999999\n"
    )
    pages = [
        identity + dates + header
        + row(1, "DEMO-A", "Wrapped product", "2,00", "Ks", "5,79", "50,00", "5,79", "7,12")
        + " " * 24 + "extended synthetic description\n"
        + row(2, "DEMO-B", "Synthetic pair", "1,00", "Pár", "50,79", "50,00", "25,40", "31,24"),
        identity + header
        + row(3, "DEMO-C", "Synthetic set", "2,00", "Sada", "4,00", "0,00", "8,00", "9,84")
        + row(4, "DEMO-A", "Paid duplicate", "1,00", "Bal", "5,00", "0,00", "5,00", "6,15"),
        identity + header
        + row(5, "DEMO-A", "Gift duplicate", "1,00", "Ks", "5,00", "100,00", "0,00", "0,00"),
        identity + "Suma bez DPH 44,19 €\nDPH 23 % 10,16 €\nCelkom 54,35 €\nSplatná suma 54,35 €\n",
    ]
    return [text + f"Strana {number} / 4\n" for number, text in enumerate(pages, 1)]


class SkpInvoiceParserTests(unittest.TestCase):
    def parse_pages(self, texts):
        pages = [SimpleNamespace(extract_text=Mock(return_value=text)) for text in texts]
        reader = SimpleNamespace(is_encrypted=False, pages=pages)
        with patch.object(adapter, "PdfReader", return_value=reader):
            invoice = adapter.parse_invoice_pdf(b"%PDF-1.7 synthetic multipage fixture")
        for page in pages:
            page.extract_text.assert_called_once_with(extraction_mode="layout")
        return invoice

    def test_rounded_discount_costs_cannot_accumulate_a_receiving_value_difference(self):
        prefix = invoice_text().split("1 DEMO-A", 1)[0].replace(
            "Jednotková cena  DPH", "Jednotková cena  Zľava %  DPH")
        row = "{number} DEMO-{number} Synthetic discount 101,00 Ks 1,01 50,00 23 % 51,01 € 62,74 €\n"
        single = prefix + row.format(number=1) + "Suma bez DPH 51,01 €\nDPH 23 % 11,73 €\nCelkom 62,74 €\n"
        result = adapter.parse_invoice_text(single)
        # Each individual 4dp cost still rounds to the billed line amount.
        self.assertEqual(result.lines[0].price, Decimal("0.5050"))
        self.assertEqual(adapter._money(result.lines[0].qty * result.lines[0].price), Decimal("51.01"))
        multiple = prefix + row.format(number=1) + row.format(number=2) + (
            "Suma bez DPH 102,02 €\nDPH 23 % 23,46 €\nCelkom 125,48 €\n")
        # Together they would value the receipt at 102.01 rather than 102.02.
        with self.assertRaisesRegex(adapter.InvoiceParseError, "Súčet nákupných cien"):
            adapter.parse_invoice_text(multiple)

    def test_net_prices_blank_ean_and_invoice_level_tax_rounding(self):
        invoice = adapter.parse_invoice_text(invoice_text())
        self.assertEqual(invoice.number, "9/26/00999")
        self.assertEqual(invoice.key, "9_26_00999")
        self.assertEqual(invoice.issue_date, date(2026, 2, 1))
        self.assertEqual(invoice.due_date, date(2026, 2, 15))
        self.assertEqual(invoice.gross, Decimal("0.11"))
        self.assertEqual(sum(line.gross for line in invoice.lines), Decimal("0.12"))
        # Existing uploaded invoices keep their identity after parser upgrades.
        self.assertEqual(invoice.fingerprint(), "8280f222810fb6fb57de989b9071ce81308b4a66df6b1eae8da62e0d4d2c95ab")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "invoice.csv"
            path.write_bytes(invoice.csv_bytes())
            rows = receiving_db._parse_invoice_csv(path)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["supplier_sku"], "DEMO-A")
        self.assertEqual(rows[0]["ordered_qty"], Decimal("1"))
        self.assertEqual(rows[0]["unit_price"], Decimal("0.03"))
        self.assertEqual(rows[0]["ean"], "")

    def test_layout_reader_is_used_and_validated(self):
        page = SimpleNamespace(extract_text=Mock(return_value=invoice_text()))
        reader = SimpleNamespace(is_encrypted=False, pages=[page])
        with patch.object(adapter, "PdfReader", return_value=reader):
            result = adapter.parse_invoice_pdf(b"%PDF-1.7 synthetic fixture")
        page.extract_text.assert_called_once_with(extraction_mode="layout")
        self.assertEqual(len(result.lines), 3)

    def test_tax_summary_accepts_only_exact_aggregate_or_line_rounding(self):
        for tax, total in (("0,02", "0,11"), ("0,03", "0,12")):
            with self.subTest(tax=tax):
                text = invoice_text().replace("DPH 23 % 0,02", f"DPH 23 % {tax}")
                text = text.replace("0,11 €", f"{total} €")
                invoice = adapter.parse_invoice_text(text)
                self.assertEqual(invoice.vat, Decimal(tax.replace(",", ".")))
        for tax, total in (("0,01", "0,10"), ("0,04", "0,13")):
            # Net + tax still equals total, but neither rounding method gives
            # this tax. Do not turn support for both methods into a tolerance.
            with self.subTest(tax=tax), self.assertRaises(adapter.InvoiceParseError):
                text = invoice_text().replace("DPH 23 % 0,02", f"DPH 23 % {tax}")
                adapter.parse_invoice_text(text.replace("0,11 €", f"{total} €"))

    def test_tax_rounding_is_checked_for_each_rate(self):
        text = invoice_text().replace(
            "3 DEMO-C Test product gamma 1,00 Ks 0,03 23 %",
            "3 DEMO-C Test product gamma 1,00 Ks 0,03 20 %",
        )
        for tax_23, total in (("0,01", "0,11"), ("0,02", "0,12")):
            with self.subTest(tax_23=tax_23):
                value = text.replace("DPH 23 % 0,02 €", f"DPH 23 % {tax_23} €\nDPH 20 % 0,01 €")
                invoice = adapter.parse_invoice_text(value.replace("0,11 €", f"{total} €"))
                self.assertEqual(invoice.gross, Decimal(total.replace(",", ".")))
        with self.assertRaises(adapter.InvoiceParseError):
            # The invoice tax still adds to 0.02, but belongs to wrong rates.
            adapter.parse_invoice_text(text.replace("DPH 23 % 0,02 €", "DPH 23 % 0,00 €\nDPH 20 % 0,02 €"))

    def test_order_reference_and_known_payment_terms_do_not_become_items(self):
        for label in ("Payment terms", "Platobné podmienky"):
            with self.subTest(label=label):
                text = invoice_text().replace("SO-SYNTHETIC-ONLY", "SO/SYNTHETIC-ONLY")
                text = text.replace("Suma bez DPH", f"{label}: Synthetic terms\nSuma bez DPH")
                invoice = adapter.parse_invoice_text(text)
                self.assertEqual(len(invoice.lines), 3)
                self.assertEqual(invoice.lines[-1].title, "Test product gamma")
        for text in (
            invoice_text().replace("Suma bez DPH", "Unknown note: Synthetic terms\nSuma bez DPH"),
            invoice_text().replace("2 DEMO-B", "Payment terms: Synthetic terms\n2 DEMO-B"),
        ):
            with self.subTest(text=text), self.assertRaises(adapter.InvoiceParseError):
                adapter.parse_invoice_text(text)

    def test_multiple_pages_discounts_wrapped_titles_and_repeated_skus(self):
        invoice = self.parse_pages(discount_invoice_pages())
        self.assertEqual([line.number for line in invoice.lines], [1, 2, 3, 4, 5])
        self.assertEqual(invoice.lines[0].title, "Wrapped product extended synthetic description")
        self.assertEqual(invoice.lines[0].price, Decimal("2.8950"))
        # The billed line is rounded before the effective receiving unit price.
        self.assertEqual(invoice.lines[1].price, Decimal("25.4000"))
        self.assertEqual(invoice.lines[-1].price, Decimal("0.0000"))
        self.assertEqual((invoice.net, invoice.vat, invoice.gross),
                         (Decimal("44.19"), Decimal("10.16"), Decimal("54.35")))
        self.assertEqual([line.sku for line in invoice.lines],
                         ["DEMO-A", "DEMO-B", "DEMO-C", "DEMO-A", "DEMO-A"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "invoice.csv"
            path.write_bytes(invoice.csv_bytes())
            rows = receiving_db._parse_invoice_csv(path)
        self.assertEqual(len(rows), 5)
        self.assertEqual([row["unit_price"] for row in rows],
                         [Decimal("2.8950"), Decimal("25.4000"), Decimal("4.00"),
                          Decimal("5.00"), Decimal("0.0000")])
        self.assertEqual([row["supplier_sku"] for row in rows].count("DEMO-A"), 3)
        self.assertTrue(all(row["ean"] == "" for row in rows))

    def test_single_line_header_with_discount_column(self):
        pages = discount_invoice_pages()
        header = (
            "Interná referencia  Name  Množstvo  Jednotková cena  Zľava %  DPH  Celkom bez DPH  Celkom s DPH\n"
        )
        for index in range(3):
            lines = pages[index].splitlines(keepends=True)
            start = next(number for number, value in enumerate(lines) if "Interná" in value)
            pages[index] = "".join(lines[:start]) + header + "".join(lines[start + 2:])
        self.assertEqual(len(self.parse_pages(pages).lines), 5)

    def test_discount_column_requires_valid_percentage_and_matching_line_totals(self):
        pages = discount_invoice_pages()
        for discount in ("", "unknown", "-1,00", "100,01", "101,00"):
            with self.subTest(discount=discount), self.assertRaises(adapter.InvoiceParseError):
                self.parse_pages([pages[0].replace("50,00", discount, 1), *pages[1:]])
        for old, new in (("50,00", "25,00"), ("7,12 €", "7,13 €"),
                         ("44,19 €", "44,20 €"), ("10,16 €", "10,17 €"),
                         ("54,35 €", "54,36 €")):
            with self.subTest(old=old, new=new), self.assertRaises(adapter.InvoiceParseError):
                self.parse_pages([page.replace(old, new, 1) for page in pages])

    def test_page_numbers_reject_missing_reordered_duplicated_or_inconsistent_pages(self):
        pages = discount_invoice_pages()
        cases = {
            "missing first": pages[1:],
            "missing middle": [pages[0], *pages[2:]],
            # Removing only this last zero-cost row leaves all monetary sums valid.
            "missing free item page": [pages[0], pages[1], pages[3]],
            "missing summary": pages[:3],
            "reordered": [pages[0], pages[2], pages[1], pages[3]],
            "duplicated": [pages[0], pages[1], pages[1], pages[2], pages[3]],
            "wrong total": [pages[0].replace("Strana 1 / 4", "Strana 1 / 5"), *pages[1:]],
            "missing footer": [pages[0].replace("Strana 1 / 4", ""), *pages[1:]],
            "repeated page number": [pages[0], pages[1].replace("Strana 2 / 4", "Strana 1 / 4"), *pages[2:]],
        }
        for name, texts in cases.items():
            with self.subTest(name=name), self.assertRaises(adapter.InvoiceParseError):
                self.parse_pages(texts)

    def test_unreadable_page_cannot_be_silently_omitted(self):
        for empty in (None, "", " \n\t"):
            with self.subTest(empty=empty), self.assertRaises(adapter.InvoiceParseError):
                # No page counters: a readable one-page invoice must not hide a
                # second scanned page whose text extractor returned no content.
                self.parse_pages([invoice_text(), empty])

    def test_rejects_missing_rows_incorrect_prices_taxes_totals_and_currency(self):
        bad = [
            invoice_text().replace("SK - PROFI BIKE", "Different issuer"),
            invoice_text().replace("Faktúra 9/26/00999", "Faktúra ../unsafe"),
            invoice_text().replace("01.02.2026", "31.02.2026"),
            invoice_text().replace("2 DEMO-B", "4 DEMO-B"),
            invoice_text().replace("1,00 Ks 0,03", "2,00 Ks 0,03", 1),
            invoice_text().replace("0,03 € 0,04 €", "0,03 € 0,05 €", 1),
            invoice_text().replace("0,03 23 %", "0,03 20 %", 1),
            invoice_text().replace("DPH 23 % 0,02", "DPH 23 % 0,03"),
            invoice_text().replace("Suma bez DPH 0,09", "Suma bez DPH 0,10"),
            invoice_text().replace("Celkom 0,11", "Celkom 0,12"),
            invoice_text().replace("€", "USD"),
            invoice_text().replace("1,00 Ks", "unreadable Ks", 1),
            invoice_text().replace("1,00 Ks", "1,0001 Ks", 1),
            invoice_text().replace("DEMO-A", "A" * 97),
            invoice_text().replace("3 DEMO-C Test product gamma 1,00 Ks 0,03 23 % 0,03 € 0,04 €", ""),
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(adapter.InvoiceParseError):
                adapter.parse_invoice_text(value)

    def test_pdf_errors_are_bounded_and_do_not_leak_reader_details(self):
        for content in (b"not a pdf", b"%PDF-" + b"x" * (20 * 1024 * 1024)):
            with self.assertRaises(adapter.InvoiceParseError):
                adapter.parse_invoice_pdf(content)
        with patch.object(adapter, "PdfReader", side_effect=ValueError("internal details")):
            with self.assertRaises(adapter.InvoiceParseError) as error:
                adapter.parse_invoice_pdf(b"%PDF-1.7 invalid")
        self.assertNotIn("internal details", str(error.exception))


class SkpInvoiceUploadTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('inventory_hub.services.invoice_archive.archive_after_upload',
                                AsyncMock(return_value={'status': 'completed'})))
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(folder)
        self.enterContext(patch.object(suppliers, "DATA_ROOT", self.root))
        self.enterContext(patch.object(config_io, "DATA_ROOT", self.root))
        config = self.root / "suppliers/sk-profi-bike/config.json"
        config.parent.mkdir(parents=True)
        config.write_text('{"product_code_prefix":"SKP-"}')
        self.invoice = adapter.parse_invoice_text(invoice_text())
        self.reader = self.enterContext(patch.object(adapter, "parse_invoice_pdf", return_value=self.invoice))
        app = FastAPI()
        app.include_router(suppliers.router)
        self.client = self.enterContext(TestClient(app))
        self.invoice_root = config.parent / "invoices"
        self.index_path = self.invoice_root / "index.latest.json"
        self.invoice_id = "sk-profi-bike:9_26_00999"

    def upload(self, filename="arbitrary-download-name.pdf", **kwargs):
        return self.client.post("/suppliers/sk-profi-bike/upload-invoice", files={
            "file": (filename, b"%PDF-1.7 synthetic", "application/pdf"),
        }, **kwargs)

    def test_upload_uses_invoice_identity_date_and_receiving_csv(self):
        result = self.upload()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["filename"], "9_26_00999.pdf")
        self.assertFalse(result.json()["replayed"])
        entry = json.loads(self.index_path.read_text())[self.invoice_id]
        self.assertEqual(entry["issue_date"], "2026-02-01")
        self.assertEqual(entry["original_number"], "9/26/00999")
        self.assertFalse(entry["vat_included"])
        self.assertEqual((self.root / entry["csv_path"]).read_bytes(), self.invoice.csv_bytes())
        self.assertEqual(entry["status"], "new")

    def test_duplicate_filename_and_retry_preserve_completed_status_and_notes(self):
        self.assertEqual(self.upload().status_code, 200)
        index = json.loads(self.index_path.read_text())
        index[self.invoice_id].update(status="processed", note="Already received", current_session_id="123")
        self.index_path.write_text(json.dumps(index))
        before = self.index_path.read_bytes()
        result = self.upload("renamed.pdf")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()["replayed"])
        self.assertEqual(self.index_path.read_bytes(), before)
        self.assertEqual(len(list((self.invoice_root / "csv").glob("*.csv"))), 1)

    def test_changed_same_number_rejects_without_overwriting(self):
        self.assertEqual(self.upload().status_code, 200)
        before = self.index_path.read_bytes()
        self.reader.return_value = adapter.parse_invoice_text(invoice_text().replace("Test product alpha", "Changed product"))
        response = self.upload()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.index_path.read_bytes(), before)

    def test_invalid_pdf_or_override_does_not_create_receiving_files(self):
        result = self.upload(params={"invoice_number": "OTHER"})
        self.assertEqual(result.status_code, 422, result.text)
        self.reader.side_effect = adapter.InvoiceParseError("Invalid document")
        result = self.upload()
        self.assertEqual(result.status_code, 422, result.text)
        self.assertFalse(self.invoice_root.exists())

    def test_recover_interrupted_upload_before_index(self):
        csv_path = self.invoice_root / "csv" / f"{self.invoice.key}.csv"
        csv_path.parent.mkdir(parents=True)
        csv_path.write_bytes(self.invoice.csv_bytes())
        response = self.upload()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["replayed"])
        self.assertEqual(len(json.loads(self.index_path.read_text())), 1)

    def test_interrupted_upload_cannot_pair_old_pdf_with_changed_rows(self):
        raw = self.invoice_root / "pdf" / f"{self.invoice.key}.pdf"
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b"%PDF-1.7 different prior document")
        prior = adapter.parse_invoice_text(invoice_text().replace("DEMO-A", "DEMO-DIFFERENT"))
        self.reader.side_effect = [self.invoice, prior]
        response = self.upload()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse((self.invoice_root / "csv").exists())
        self.assertFalse(self.index_path.exists())

    def test_concurrent_uploads_share_one_invoice(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: adapter.save_receiving_invoice(
                b"%PDF-1.7 synthetic", self.root, "sk-profi-bike"
            ), range(2)))
        self.assertEqual(sorted(row["replayed"] for row in responses), [False, True])
        self.assertEqual(list(json.loads(self.index_path.read_text())), [self.invoice_id])


if __name__ == "__main__":
    unittest.main()
