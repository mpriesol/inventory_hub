"""Conservative cleanup of newly generated copy, preserving technical facts."""
import re
import unicodedata

from bs4 import BeautifulSoup


def _key(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split()).rstrip(':')


def concise_title(title, facts):
    # The approved format separates the model from its short descriptor. Never
    # trim a model name/code or infer a missing parameter from the title itself.
    if ' - ' not in title:
        return title
    model, descriptor = title.split(' - ', 1)
    endings = []
    for fact in facts:
        for param in [*fact.get('parameters', []), *fact.get('variant_attributes', [])]:
            name, value = _key(param['name']), param['value'].strip()
            if name in {'farba', 'barva', 'color', 'colour'} and value:
                prefix = r'(?:(?:farba|farby|vo farbe|color|colour)\s*:?\s*)?'
            elif name in {'veľkosť', 'velkost', 'velikost', 'size'} and re.fullmatch(r'(?:[2-6]?X{0,3}[SML]|[SML]/[SML]|UNI|ONE SIZE)', value, re.I):
                prefix = r'(?:(?:vo? veľkosti|veľkosť|velkost|velikost|size)\s*:?\s*)?'
            else:
                continue
            endings.append(re.compile(r'[\s,;/]+(?:' + prefix + re.escape(value) + r')\s*$', re.I))
    while True:
        previous = descriptor
        for pattern in endings:
            shortened = pattern.sub('', descriptor).rstrip(' ,;/')
            if shortened:
                descriptor = shortened
        if descriptor == previous:
            return model + ' - ' + descriptor


def without_duplicate_parameter_tables(html, parameters):
    if not parameters or '<table' not in html.lower():
        return html
    pairs = {(_key(p['name']), _key(value)) for p in parameters for value in p['values']}
    soup = BeautifulSoup(html, 'html.parser')
    changed = False
    headings = {'parametre', 'technické parametre', 'špecifikácia', 'technická špecifikácia', 'parameters', 'specifications'}
    for table in list(soup.find_all('table')):
        # Compatibility/size charts, embedded media and links carry information
        # beyond a plain parameter/value table and must remain intact.
        if table.find(['table', 'a', 'img']) or table.find(attrs={'colspan': True}) or table.find(attrs={'rowspan': True}):
            continue
        caption = table.find('caption')
        if caption and _key(caption.get_text(' ', strip=True)) not in headings:
            continue
        heading = table.find_previous_sibling()
        if heading and heading.name in {'h2', 'h3', 'h4'} and re.search(
                r'kompatibil|compatibil|veľkostn|velikostn|size chart', _key(heading.get_text(' ', strip=True))):
            continue
        rows = []
        for row in table.find_all('tr'):
            cells = row.find_all(['td', 'th'], recursive=False)
            text = tuple(_key(c.get_text(' ', strip=True)) for c in cells)
            if text in {('parameter', 'hodnota'), ('parametre', 'hodnota'), ('parameter', 'value'), ('parametr', 'hodnota')}:
                continue
            rows.append(text)
        if rows and all(len(row) == 2 and row in pairs for row in rows):
            if heading and heading.name in {'h2', 'h3', 'h4'} and _key(heading.get_text(' ', strip=True)) in headings:
                heading.decompose()
            table.decompose()
            changed = True
    result = str(soup) if changed else html
    return result if soup.get_text(strip=True) else html


def normalize_generated_content(content, context):
    if context.get('prompt_version', 1) < 4:
        return content
    from inventory_hub.services.ai_content_validation import has_active_html
    if has_active_html(content['long_description']):
        return content  # Never hide a validation failure by deleting its table.
    return {**content, 'title': concise_title(content['title'], context.get('facts', [])),
            'long_description': without_duplicate_parameter_tables(content['long_description'], content['parameters'])}
