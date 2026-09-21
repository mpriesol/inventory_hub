"""Small allowlist for displaying/importing supplier descriptions; originals stay in XML."""
from bs4 import BeautifulSoup, Comment

from inventory_hub.adapters.paul_lange_catalog import http_url


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
