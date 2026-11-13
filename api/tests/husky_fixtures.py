"""Synthetic Husky documents, never private portal HTML or production credentials."""
from inventory_hub.adapters.husky_b2b import GRID, LOGIN_PREFIX, SEARCH, SEARCH_BUTTON


def item(code='PUBLIC-1', mpn='MPN-1', ean='5901234123457', group='42', color='zelená'):
    return f'''<SHOPITEM><ID>123</ID><MASTER_PRODUCT_ID>{group}</MASTER_PRODUCT_ID>
<CODE>{code}</CODE><PRODUCTNO>{mpn}</PRODUCTNO><EAN>{ean}</EAN><EAN>{ean}</EAN>
<PRODUCTNAME>Testovací stan</PRODUCTNAME><PRODUCER>Husky</PRODUCER>
<PRICE_VAT>99.90</PRICE_VAT><COMMON_PRICE_VAT>123</COMMON_PRICE_VAT><VAT><VALUE>20</VALUE></VAT>
<DESCRIPTION>&lt;p&gt;Testovací popis.&lt;/p&gt;</DESCRIPTION><ANNOTATION>Krátky popis.</ANNOTATION>
<PARAM><PARAM_NAME>Farba</PARAM_NAME><VAL>{color}</VAL></PARAM><CATEGORYTEXT>Stany</CATEGORYTEXT>
<IMGURL>https://example.test/thumb.jpg</IMGURL><IMAGES><IMAGE><DOWNLOAD_FROM>https://example.test/original.jpg</DOWNLOAD_FROM></IMAGE></IMAGES>
<ATTRIBUTES><ATTRIBUTE><NAME>Hmotnosť</NAME><ATTRIBUTE_ITEMS><ATTRIBUTE_ITEM><VALUE>3 kg</VALUE></ATTRIBUTE_ITEM></ATTRIBUTE_ITEMS></ATTRIBUTE></ATTRIBUTES>
</SHOPITEM>'''


def stock(code='PUBLIC-1', flag='True'):
    return f'<SHOPITEM><KODVYROBCE>{code}</KODVYROBCE><SKLADEM>{flag}</SKLADEM></SHOPITEM>'


def hidden():
    return '<input type="hidden" name="__VIEWSTATE" value="synthetic-state"><input type="hidden" name="__CSRFTOKEN" value="synthetic-csrf">'


def login():
    return f'''<html><head><meta charset="utf-8"></head><form action="./login">{hidden()}<input name="{LOGIN_PREFIX}UserName">
<input type="password" name="{LOGIN_PREFIX}Password"><input type="submit" name="{LOGIN_PREFIX}LoginButton" value="Prihlásiť"></form></html>'''


def page(codes=('MPN-1',), current=1, next_page=None, stock_value='>10 ks', price='55,00'):
    pager = '' if next_page is None and current == 1 else '<tr><td colspan="7"><table><tr>' + f'<td><span>{current}</span></td>' + (
        f'''<td><a href="javascript:__doPostBack('{GRID}','Page${next_page}')">{next_page}</a></td>''' if next_page else '') + '</tr></table></td></tr>'
    rows = ''.join(f'''<tr><td></td><td>Testovací produkt</td><td>{code}</td><td>{stock_value}</td><td>100,00</td><td>{price}</td><td><input name="{GRID}$ctl03$txtObjednat" value="9"><input name="{GRID}$ctl03$btnAddToCart" type="image"></td></tr>''' for code in codes)
    return f'''<html><head><meta charset="utf-8"></head><form action="./objednavky">{hidden()}<a id="loginInfo_lvLoginView_lsLoginStatusLoggedIn">Odhlásiť</a>
<input type="checkbox" name="ctl00$LeftSiteBoxContent$catalog$trvCatalogTreen1CheckBox" checked>
<input type="checkbox" name="ctl00$TopSiteBoxContent$chckPouzeSkladem" checked>
<input name="{SEARCH}" value="old search"><input name="{SEARCH_BUTTON}" type="submit" value="OK">
<table id="MainContent_zbozi_grdList">{pager}<tr><th></th><th>Názov tovaru</th><th>Reg. číslo</th><th>Skladom</th><th>Cena (EUR)</th><th>Cena so zľavou (EUR)</th><th>Objednať</th></tr>{rows}{pager}</table></form></html>'''
