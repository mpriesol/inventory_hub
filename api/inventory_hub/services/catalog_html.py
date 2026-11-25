"""Small allowlist for displaying/importing supplier descriptions; originals stay in XML."""
from html import escape

from bs4 import BeautifulSoup, Comment

from inventory_hub.adapters.paul_lange_catalog import http_url


SHIMANO_MANUAL_INTRO = (
    "UPOZORNENIE",
    "Informácie o inštalácii, nastavení a výmene výrobkov",
    "Kvôli bezpečnosti si pred používaním",
    "Nasledujúce pokyny je potrebné vždy dodržiavať",
    "NEBEZPEČENSTVO - Nedodržanie pokynov",
    "VÝSTRAHA - Nedodržanie pokynov",
    "UPOZORNENIE - Nedodržanie pokynov",
)


def publication_safety(product, *, with_heading=False):
    """Leave the source intact and omit only the recognized Shimano manual preface."""
    safety = product.safety_information
    if not safety:
        return ""
    if product.supplier == "paul-lange" and (product.brand or "").strip().casefold() == "shimano":
        lines = safety.strip().splitlines()
        warnings = "\n".join(lines[len(SHIMANO_MANUAL_INTRO):]).lstrip()
        if warnings.startswith("VÝSTRAHA\n- ") and all(
                line.startswith(prefix) for line, prefix in zip(lines, SHIMANO_MANUAL_INTRO)):
            body = "<br>".join(escape(line) for line in warnings.splitlines())
            heading = "<h2>Pokyny výrobcu</h2>" if with_heading else ""
            return heading + "<p>" + body + "</p>"
    return "<h2>Bezpečnostné informácie</h2>" + clean_description(safety) if with_heading else safety


def clean_description(value: str) -> str:
    soup = BeautifulSoup(value or "", "html.parser")
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
    for element in list(soup.find_all(True)):
        if element.name is None or element.parent is None:
            continue
        if element.name in {"script", "style", "iframe", "object", "embed", "svg", "math", "form", "template"}:
            element.decompose()
        elif element.name not in {"p", "br", "strong", "b", "em", "i", "u", "ul", "ol", "li", "h2", "h3", "h4", "table", "thead", "tbody", "tr", "td", "th", "a", "blockquote"}:
            element.unwrap()
        else:
            href = element.get("href") if element.name == "a" else None
            element.attrs = {}
            if href and http_url(href):
                element["href"] = href
                element["rel"] = "noopener noreferrer"
    return str(soup)
