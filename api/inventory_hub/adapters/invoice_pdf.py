"""One dispatch for validated supplier PDF originals across upload and receiving."""
from inventory_hub.adapters import ariga_invoice, sk_profi_bike_invoice, zookee_invoice


PARSERS = {"ariga": ariga_invoice, "sk-profi-bike": sk_profi_bike_invoice, "zookee": zookee_invoice}


def parse_invoice_pdf(supplier, content):
    return PARSERS[supplier].parse_invoice_pdf(content)
