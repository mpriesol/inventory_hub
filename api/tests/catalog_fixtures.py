"""Synthetic feed and API data only; never production fixtures."""
from copy import deepcopy
from contextlib import asynccontextmanager

from inventory_hub.catalog_types import CatalogProduct, CatalogPrices


def xml_item(code="A-001", name="Červená prilba", extra="", ean="00012345"):
    return f"""<SHOPITEM><ITEM_ID>{code}</ITEM_ID><PRODUCT>{name}</PRODUCT>
    <EAN>{ean}</EAN><MANUFACTURER>TEST</MANUFACTURER><MANUFACTURER_CODE>M-{code}</MANUFACTURER_CODE>
    <DESCRIPTION><![CDATA[<p>Popis <strong>produktu</strong></p>]]></DESCRIPTION>
    <IMGURL>https://images.example.com/{code}.jpg</IMGURL><IMAGES><IMGURL>https://images.example.com/{code}-2.jpg</IMGURL></IMAGES>
    <PRICE>100</PRICE><PRICE_VAT>123</PRICE_VAT><PRICE_VOC>60</PRICE_VOC><PRICE_VOC_VAT>73.80</PRICE_VOC_VAT>
    <STOCK>6+</STOCK><STOCK_EXTERNAL>ano</STOCK_EXTERNAL><DELIVERY>skladom</DELIVERY>
    <STA_PARAMS><PRODUCT_NAME>Prilba</PRODUCT_NAME><UNUSED>original</UNUSED></STA_PARAMS>
    <DYN_PARAMS><PARAM><DESC>Farba</DESC><VAL>červená</VAL></PARAM><PARAM><DESC>Veľkosť</DESC><VAL>M</VAL></PARAM></DYN_PARAMS>
    {extra}</SHOPITEM>"""


def product(id=1, **changes):
    return CatalogProduct(id=id, supplier="paul-lange", code=f"A-{id:03}", shop_code=f"PL-A-{id:03}",
                          name="Červená prilba", brand="TEST", eans=[f"0000000{id}"], images=["https://images.example.com/item.jpg"],
                          source_hash=f"hash-{id}", prices=CatalogPrices(currency="EUR", vat_percent=23,
                          retail_net=100, retail_gross=123, purchase_net=60, purchase_gross="73.80"), **changes)


def northfinder_variant(code="N-RED-M", ean="000001", size="M", purchase="10", retail="24.60"):
    return f"""<variant><id_variant>{ean}</id_variant><reference>{code}</reference><ean13>{ean}</ean13>
    <price>{purchase}</price><recomended_retail_price>{retail}</recomended_retail_price><quantity>3</quantity>
    <attributes><attribute><group>FARBA</group><value>red</value></attribute><attribute><group>VEĽKOSŤ</group><value>{size}</value></attribute></attributes>
    <images><image>b2b.northfinder.com/synthetic-test.jpg</image></images><extra>preserve variant</extra></variant>"""


def northfinder_product(variants=None, purchase="10", retail="24.60"):
    return f"""<product><id_product>1</id_product><reference>N</reference><name>Technical name</name><name_b2c>Červená bunda TEST</name_b2c>
    <description><![CDATA[<p>Popis bundy</p>]]></description><currency>EUR</currency><ean13>0</ean13>
    <categories_b2c><category>Oblečenie &gt; Bundy</category></categories_b2c><price>{purchase}</price><recomended_retail_price>{retail}</recomended_retail_price>
    <features><feature><group>Farba</group><value>red</value></feature><feature><group>Farba</group><value>blue</value></feature><feature><group>Materiál</group><value>Polyester</value></feature></features>
    <variants>{variants if variants is not None else northfinder_variant()}</variants><extra>preserve parent</extra></product>"""


@asynccontextmanager
async def dummy_session():
    yield object()


class FakeUpgates:
    """Implements documented responses, including products.inserted_yn (not created_yn)."""
    def __init__(self):
        self.products = {}
        self.sent = []
        self.with_vat = True
        self.has_field = True
        self.timeout = False
        self.accept = True
        self.omit_variant = False

    def get(self, path, params=None):
        params = params or {}
        if path == "config":
            return {"config": {"prices_with_vat_yn": self.with_vat}}
        if path == "languages":
            return {"languages": [{"language_id": "sk", "currency_id": "EUR", "active_yn": True, "default_yn": True}]}
        if path == "pricelists":
            return {"pricelists": [{"name": "Predvolené", "default_yn": True}]}
        if path == "categories":
            return {"categories": [{"code": "K-TEST", "descriptions": [{"language": "sk", "name": "Test"}]}]}
        if path == "metas":
            return {"metas": [{"key": "validation_required", "category": "products", "type": "checkbox", "common_languages_value_yn": True}] if self.has_field else []}
        if path in ("products", "products/simple"):
            rows = list(self.products.values())
            if "codes" in params:
                rows = [p for p in rows if p["code"] in params["codes"].split(";")]
            if "variant_codes" in params:
                rows = [p for p in rows if any(v["code"] in params["variant_codes"].split(";") for v in p.get("variants", []))]
            return {"products": deepcopy(rows), "number_of_pages": 1, "number_of_items": len(rows)}
        raise AssertionError(path)

    def post(self, path, payload):
        from inventory_hub.services.upgates import UpgatesError
        self.sent.append((path, deepcopy(payload)))
        if path == "metas":
            self.has_field = True
            return {"metas": [{"key": "validation_required", "created_yn": True}]}
        responses = []
        for p in payload["products"]:
            remote = deepcopy(p)
            remote["product_id"] = 700
            for i, variant in enumerate(remote.get("variants", [])):
                variant["variant_id"] = 800 + i
            if self.omit_variant:
                remote["variants"] = remote.get("variants", [])[:1]
            if self.accept:
                self.products[p["code"]] = remote
            responses.append({"code": p["code"], "inserted_yn": self.accept,
                              "variants": [{"code": v["code"], "inserted_yn": True} for v in remote.get("variants", [])]})
        if self.timeout:
            raise UpgatesError("Simulated timeout after send")
        return {"products": responses}
