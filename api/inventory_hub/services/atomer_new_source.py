"""Build new xTrek parents from the two pinned private Atomer archives.

The CSV defines the complete hierarchy and visibility. The buyers XML supplies
rich descriptions and variant dimensions where exported. A physical balance is
never part of a catalog payload.
"""
from collections import Counter, defaultdict
import csv
from decimal import Decimal, InvalidOperation
from itertools import permutations
import hashlib
import html
import io
import re
import threading
from urllib.parse import urlsplit
import zlib

from lxml import etree
from lxml import html as lxml_html

from inventory_hub.adapters import atomer_content as xml_source
from inventory_hub.services import atomer_generations as gen, atomer_source_state as source
from inventory_hub.services.product_identity import verified_barcodes

FLAGS = ('Akcia', 'Novinka', 'Výpredaj', 'Odporúčaný')
MAX_ARCHIVE = 105_000_000
_CACHE_LOCK = threading.Lock()
_cache = None


def decimal_value(value):
    try:
        number = Decimal(str(value or '0').strip().replace(',', '.'))
    except InvalidOperation:
        gen.require(False, 'new_source_number_invalid')
    gen.require(number.is_finite() and number >= 0, 'new_source_number_invalid')
    return number


def grams(value):
    number = decimal_value(value) * 1000
    gen.require(number == number.to_integral_value(), 'generation_weight_precision')
    return int(number)


def named(row, field):
    values = {v for k, v in row if k == field and v.strip()}
    gen.require(len(values) <= 1, 'new_source_column_ambiguous')
    return next(iter(values), '')


def _csv(raw):
    try:
        raw.decode('utf-8-sig')
        encoding = 'utf-8-sig'
    except UnicodeDecodeError:
        encoding = 'cp1250'
    stream = io.TextIOWrapper(io.BytesIO(raw), encoding=encoding, newline='')
    delimiter = csv.Sniffer().sniff(stream.readline(), delimiters=';\t,').delimiter
    stream.seek(0)
    reader = csv.reader(stream, delimiter=delimiter, strict=True)
    head = next(reader)
    for required in ('Kat. číslo','Kat. číslo rodiča','Interné číslo','Viditeľný','Názov tovaru'):
        gen.require(head.count(required) == 1, 'new_admin_columns_invalid')
    for values in reader:
        if not values or not any(values):
            continue
        gen.require(len(values) == len(head), 'new_admin_columns_invalid')
        # Sparse storage matters: actual files have hundreds of mostly empty
        # characteristic/caption columns. Keep duplicate nonempty headings.
        yield tuple((k, v) for k, v in zip(head, values)
                    if v.strip() or v and k in ('Kat. číslo','Kat. číslo rodiča'))


def csv_rows(raw, wanted):
    index = Catalog(_csv(raw))
    result = {}
    for root in wanted:
        rows, _ = index.family(root)
        result.update(rows)
    return result


def hash_source(data, digest):
    decompressor = zlib.decompressobj()
    raw = decompressor.decompress(data, MAX_ARCHIVE + 1)
    gen.require(len(raw) <= MAX_ARCHIVE and decompressor.eof and not decompressor.unused_data
                and hashlib.sha256(raw).hexdigest() == digest, 'variant_source_changed')
    return raw


def _code(raw):
    return (raw or '').strip()


def _valid_key(key):
    return bool(key) and len(key) <= 255 and all(ord(c) >= 32 and ord(c) != 127 for c in key)


def _same_sellable(choices):
    def commercial(row):
        return [(k,v) for k,v in row if k not in ('Interné číslo','Obrázky') and not k.startswith('Popisy k obrázkom ')]
    first = choices[0]
    ean = _code(named(first,'EAN kód'))
    parent = _code(named(first,'Kat. číslo rodiča'))
    if parent in ('','0') or not ean or ean not in verified_barcodes([ean]):
        return None
    if any(commercial(r)!=commercial(first) for r in choices[1:]):
        return None
    images, captions = [], {}
    for row in choices:
        for index, url in enumerate(named(row,'Obrázky').split('|'),1):
            if not url:
                continue
            caption = named(row,'Popisy k obrázkom '+str(index))
            if caption and captions.get(url,caption)!=caption:
                return None
            if caption:
                captions[url]=caption
            if url not in images:
                images.append(url)
    combined = commercial(first)+[('Interné číslo',named(first,'Interné číslo')),('Obrázky','|'.join(images))]
    combined += [('Popisy k obrázkom '+str(i),captions[url]) for i,url in enumerate(images,1) if url in captions]
    return tuple(combined)


class FamilyRows(dict):
    """CSV rows for one or more selected families, with index-wide axis evidence."""
    def __init__(self, axis_values, owners=None, duplicate_ids=None, parent_aliases=None):
        super().__init__()
        self.axis_values = axis_values
        self.owners = owners or {}
        self.duplicate_ids = duplicate_ids or {}
        self.parent_aliases = parent_aliases or {}


class Catalog:
    """Indexed sparse CSV rows and serialized XML for both active and hidden families."""
    def __init__(self, rows):
        self._rows = {}
        self._xml = {}
        self._children = defaultdict(list)
        self._invalid = set()
        self._references = {}
        self._duplicate_ids = {}
        self._literal_children = {}
        self._unwitnessed = set()
        self._parent_aliases = {}
        self._axis_rows = []
        self.axis_values = defaultdict(set)
        self.category_paths = set()
        parents = {}
        duplicates = defaultdict(list)
        raw_parents = set()
        spellings = {}
        blank_roots = defaultdict(list)
        blank_parent_rows = []
        # Use the administrative native number only to locate a parent without
        # a catalog number, exactly as the migration inventory does.
        for row in rows:
            catalog_code = source.native_catalog_code(named(row,'Kat. číslo'),named(row,'Kat. číslo rodiča'),
                                                     named(row,'EAN kód'))
            native = _code(named(row, 'Interné číslo'))
            code = catalog_code or native
            parent = _code(named(row, 'Kat. číslo rodiča'))
            literal_code = next((v for k,v in row if k=='Kat. číslo'), '')
            literal_parent = next((v for k,v in row if k=='Kat. číslo rodiča'), '')
            if literal_code and not literal_code.strip() and parent == '0' and native:
                blank_roots[literal_code].append(native)
            if literal_parent and not parent and catalog_code:
                blank_parent_rows.append((code,literal_parent))
            if not _valid_key(code):
                if parent:
                    self._invalid.add(parent)
                continue
            if code in self._rows:
                if not duplicates[code]:
                    duplicates[code].append(self._rows[code])
                duplicates[code].append(row)
                self._invalid.add(code)
            if parent and parent != '0':
                raw_parents.add(named(row,'Kat. číslo rodiča'))
                spellings[code] = literal_parent
            for column,val in row:
                if column == 'Hlavna kategória' or column.startswith('Ďalšia kategória'):
                    self.category_paths.add(val)
            self._rows[code] = row
            parents[code] = parent if parent not in ('', '0', code) else ''
            if native:
                current = self._references.setdefault(native, code)
                if current != code:
                    self._references[native] = None
        for code, choices in duplicates.items():
            ids = [(next((v for k,v in r if k=='Kat. číslo'), ''),
                    next((v for k,v in r if k=='Kat. číslo rodiča'), ''),named(r,'Interné číslo')) for r in choices]
            separate = source.separate_parent(code,ids,self._rows)
            if separate and self._references.get(separate[1])==code:
                spelling,alias = separate
                for identity,row in zip(ids,choices):
                    if identity[2]==alias:
                        self._rows[alias]=row
                    else:
                        self._rows[code]=row
                self._references[alias]=alias
                parents[alias]=''
                parents[code]=alias
                self._parent_aliases[spelling]=alias
                self._invalid.remove(code)
                continue
            same = _same_sellable(choices)
            if same is not None:
                self._rows[code] = same
                self._duplicate_ids[code] = tuple(sorted({named(r,'Interné číslo') for r in choices}))
                self._invalid.remove(code)
                continue
            # Historical exports can contain a distinct unreferenced inactive
            # root whose literal SKU differs only by whitespace. Only one
            # literal parent may be referenced by children. A colliding
            # sellable or another referenced root still needs investigation.
            candidates = [r for r in choices if named(r,'Kat. číslo') in raw_parents]
            if (len(candidates)==1 and all(_code(named(r,'Kat. číslo rodiča')) in ('','0') for r in choices)
                    and all(source.flag(named(r,'Viditeľný')) is False for r in choices if r is not candidates[0])):
                self._rows[code] = candidates[0]
                self._invalid.remove(code)
        if self._parent_aliases:
            for code, spelling in spellings.items():
                if spelling in self._parent_aliases:
                    parents[code]=self._parent_aliases[spelling]
        for code, spelling in blank_parent_rows:
            candidates = blank_roots[spelling]
            if len(candidates)==1:
                parents[code] = candidates[0]
                self._literal_children[code] = candidates[0]
                self._unwitnessed.add(candidates[0])
            elif candidates:
                self._invalid.update([code,*candidates])
        for code, parent in parents.items():
            if not parent:
                continue
            root = self.canonical(parent)
            if root and root in self._rows and not parents.get(root):
                self._children[root].append(code)
                if code in self._invalid:
                    self._invalid.add(root)
            else:
                self._invalid.add(code)
                self._invalid.add(parent)
        self.roots = tuple(k for k in self._rows if not parents[k])
        self._owners = {c: r for r, children in self._children.items() for c in children}

    def canonical(self, ref):
        key = _code(ref)
        if key in self._rows:
            return key if self._references.get(key,key)==key else None
        return self._references.get(key, key)

    def parent_for(self, ref):
        key = self.canonical(ref)
        root = self._owners.get(key, key)
        return root if (root in self._rows and key not in self._invalid and root not in self._invalid
                        and root not in self._unwitnessed) else None

    def axis_inputs(self):
        """Buyers dimension values with their exact Atomer SKU, parent and barcode."""
        return tuple(self._axis_rows)

    def leaves(self, root):
        root = self.canonical(root)
        return tuple(self._children.get(root, [root])) if root in self._rows else ()

    def family(self, root):
        key = self.canonical(root)
        gen.require(key and key in self._rows, 'new_parent_source_missing')
        gen.require(key not in self._owners,'new_source_parent_is_sellable_child')
        gen.require(key not in self._invalid and key not in self._unwitnessed, 'new_parent_identity_ambiguous')
        codes = self.leaves(key)
        rows = FamilyRows(self.axis_values, self._owners, self._duplicate_ids, self._parent_aliases)
        rows[key] = self._rows[key]
        rows.update((code,self._rows[code]) for code in codes)
        parser = etree.XMLParser(no_network=True, resolve_entities=False)
        xml = {code: etree.fromstring(self._xml[code], parser) for code in codes if code in self._xml}
        return rows, xml

    def index_xml(self, raw):
        stream = io.BytesIO(raw)
        xml_source._preflight(stream)
        root = None
        count = 0
        for event, node in etree.iterparse(stream, events=('start', 'end'), load_dtd=False,
                                           no_network=True, resolve_entities=False, recover=False):
            if root is None:
                root = node
                gen.require(root.tag == 'PRODUCTS', 'new_source_xml_invalid')
            if event != 'end' or node.getparent() is not root:
                continue
            count += 1
            gen.require(node.tag == 'PRODUCT' and count <= xml_source.MAX_ROWS, 'new_source_xml_invalid')
            code = _code(xml_source._text(node, 'PRODUCT_ID'))
            parent_raw = _code(xml_source._text(node, 'PARENT_ID'))
            parent = self.canonical(parent_raw or code)
            expected_parent = self._owners.get(code, code)
            if self._parent_aliases and any(raw.strip()==parent_raw and alias==expected_parent
                                            for raw,alias in self._parent_aliases.items()):
                parent=expected_parent
            if parent != expected_parent or code not in self._rows:
                self._invalid.update((parent, expected_parent, code))
            elif code in self._xml:
                prior = etree.fromstring(self._xml[code])
                if code not in self._duplicate_ids or not _merge_xml_assets(prior, node):
                    self._invalid.update((parent,code))
                else:
                    self._xml[code] = etree.tostring(prior, with_tail=False, encoding='utf-8')
            elif _valid_key(code):
                self._xml[code] = etree.tostring(node, with_tail=False, encoding='utf-8')
                if self._literal_children.get(code)==parent:
                    self._unwitnessed.discard(parent)
                attrs = node.findall('VARIANT_PARAMETERS/*')
                axes = {a.tag:(a.text or '').strip() for a in attrs}
                if len(axes) != len(attrs) or not all(axes.values()):
                    self._invalid.add(parent)
                elif axes:
                    self._axis_rows.append(dict(code=code,parent=parent,ean=_code(node.findtext('EAN')),
                                                name=_code(node.findtext('PRODUCT_NAME')),axes=axes))
                    for tag, value in axes.items():
                        self.axis_values[tag].add(value)
            node.clear()
            while node.getprevious() is not None:
                del root[0]
        gen.require(count > 0, 'new_source_xml_invalid')


def _merge_xml_assets(prior, other):
    # Only a duplicate already proven commercially identical by the complete
    # administrative export may contribute additional XML image references.
    def commercial(node):
        return [(child.tag,etree.tostring(child, method='c14n')) for child in node
                if child.tag not in ('MAIN_IMAGE','ALTER_IMAGES')]
    if commercial(prior)!=commercial(other):
        return False
    def images(node):
        return [v for v in [node.findtext('MAIN_IMAGE'), *node.xpath('ALTER_IMAGES/IMAGE/text()')] if v]
    known = set(images(prior))
    extra = [v for v in dict.fromkeys(images(other)) if v not in known]
    if extra:
        group = prior.find('ALTER_IMAGES')
        if group is None:
            group = etree.SubElement(prior,'ALTER_IMAGES')
        for value in extra:
            etree.SubElement(group,'IMAGE').text = value
    return True


def read_catalog(archive, admin, source_sha, admin_sha):
    """One bounded index for immutable archives per process, invalidated by either digest."""
    global _cache
    key = (source_sha, admin_sha)
    with _CACHE_LOCK:
        if _cache is None or _cache[0] != key:
            rows = Catalog(_csv(hash_source(admin, admin_sha)))
            rows.index_xml(hash_source(archive, source_sha))
            _cache = (key, rows)
        return _cache[1]


def read_sources(archive, admin, selection):
    index = read_catalog(archive, admin, selection.source_sha, selection.admin_sha)
    rows, xml = FamilyRows(index.axis_values, index._owners, index._duplicate_ids, index._parent_aliases), {}
    for family in selection.families:
        grouped, nodes = index.family(family.parent)
        rows.update(grouped)
        xml[family.parent] = nodes
    return rows, xml


def checked_image(url):
    u = urlsplit(url)
    gen.require(u.scheme == 'https' and u.hostname in ('xtrek.sk','www.xtrek.sk') and not u.username and
                not u.password and u.port in (None,443) and not u.query and not u.fragment and
                u.path.startswith('/user/') and '/upload/stuff/' in u.path and '..' not in u.path,
                'generation_source_image')
    return url


def characteristics(row):
    attrs = defaultdict(list)
    for k, v in row:
        if k.startswith('Char. ') and v.strip() and v not in attrs[k[6:].strip()]:
            attrs[k[6:].strip()].append(v)
    return {k: values[0] if len(values) == 1 else values for k, values in attrs.items()}


def fields(row):
    return lambda k: named(row, k)


def pricing(row, pricelist, active=True):
    v = fields(row)
    gross = decimal_value(v('Bežná cena'))
    common = decimal_value(v('Stará cena'))
    gen.require(bool(v('Bežná cena')) and (gross > 0 or not active), 'generation_price_configuration')
    return [dict(language='sk', price_common=float(common),
                 pricelists=[dict(name=pricelist, price_original=float(gross))])]


def image_list(urls, captions=None):
    urls = list(dict.fromkeys(checked_image(u) for u in urls if u))
    result = []
    for i, url in enumerate(urls):
        image = dict(url=url, main_yn=i == 0, list_yn=i == 0, position=i+1)
        if captions and captions.get(url):
            image['titles'] = [{'language':'sk','title':captions[url]}]
        result.append(image)
    return result


def mapped_flags(row, registry):
    enabled = []
    for name in FLAGS:
        flag = source.flag(named(row,name))
        gen.require(flag is not None, 'new_flag_unknown')
        if flag:
            enabled.append(name)
    ids = [registry.get(name) for name in enabled]
    gen.require(all(type(i) is int and i > 0 for i in ids) and len(set(ids)) == len(ids),
                'new_flag_mapping_required')
    return enabled, [dict(label_id=i, active_yn=True) for i in ids]


def safe_html(value):
    """Allow ordinary original markup; refuse active content when publishing into a new shop."""
    if not value:
        return ''
    gen.require(len(value) <= xml_source.FIELD_LIMITS['long_description'], 'new_source_html_too_large')
    tree = lxml_html.fragment_fromstring(value, create_parent='div')
    for el in tree.iter():
        if not isinstance(el.tag,str):
            continue
        if el.tag.lower() == 'iframe':
            url = urlsplit(el.get('src') or '')
            gen.require(url.scheme=='https' and url.hostname in ('www.youtube.com','www.youtube-nocookie.com') and
                        not url.username and not url.password and url.port in (None,443) and
                        not url.query and not url.fragment and re.fullmatch(r'/embed/[A-Za-z0-9_-]{11}',url.path),
                        'new_active_html_requires_review')
        gen.require(el.tag.lower() not in {'script','embed','object','form','input','button','base','link','meta','style','svg','math'},
                    'new_active_html_requires_review')
        for name, value_ in el.attrib.items():
            key = name.lower()
            check = re.sub(r'[\x00-\x20]+', '', html.unescape(value_)).casefold()
            gen.require(not key.startswith('on') and key not in {'srcdoc','formaction','srcset'} and
                        not (key in {'src','href','xlink:href','action'} and check.startswith(('javascript:','vbscript:','data:'))) and
                        not (key == 'style' and any(s in check for s in ('expression(','url(','@import','behavior:','-moz-binding','\\'))),
                        'new_active_html_requires_review')
    return value


def _letters(text):
    return ''.join(text.replace('\xad','').replace('\u200b','').split())


def _one_text_edit(tree, column):
    # Formatted XML and current administrative text may differ in one
    # contiguous range. Only edit an unambiguously identified HTML text node.
    slots = tree.xpath('.//text()')
    offsets = [(i,j) for i,s in enumerate(slots) for j,ch in enumerate(s) if _letters(ch)]
    source_chars = [j for j,ch in enumerate(column) if _letters(ch)]
    old = ''.join(slots[i][j] for i,j in offsets)
    new = ''.join(column[j] for j in source_chars)
    if not old or not new:
        return None
    left = 0
    limit = min(len(old),len(new))
    while left<limit and old[left]==new[left]:
        left+=1
    right = 0
    while right<limit-left and old[-1-right]==new[-1-right]:
        right+=1
    before, after = offsets[left-1] if left else (0,-1), offsets[-right] if right else (len(slots)-1,len(slots[-1]))
    start = source_chars[left-1]+1 if left else 0
    end = source_chars[-right] if right else len(column)
    if before[0]==after[0]:
        item = slots[before[0]]
        revised = item[:before[1]+1]+column[start:end]+item[after[1]:]
    else:
        changed = offsets[left:len(old)-right]
        if not changed:
            # Insert between two formatted runs without altering their
            # original text or element layout. Prefer existing unstyled tail.
            slot,pos = after if slots[after[0]].is_tail else (before[0],before[1]+1)
            item = slots[slot]
            revised = item[:pos]+column[start:end]+item[pos:]
        elif changed[0][0]==changed[-1][0] and left+right==len(new):
            item = slots[changed[0][0]]
            revised = item[:changed[0][1]]+item[changed[-1][1]+1:]
        else:
            return None
    setattr(item.getparent(),'tail' if item.is_tail else 'text',revised)
    if _letters(''.join(tree.itertext()))!=new:
        return None
    return lxml_html.tostring(tree,encoding='unicode',method='html')[len('<div>'):-len('</div>')]


def _original(nodes, tag, column):
    versions = {n.findtext(tag) for n in nodes if n.findtext(tag)}
    plain = html.escape(column, quote=False).replace('\r\n','\n').replace('\r','\n').replace('\n','<br>\n')
    if not versions:
        return safe_html(plain),'admin_csv_rendered_text'
    if len(versions)==1 and not column:
        return safe_html(next(iter(versions))),'buyers_html'
    trees = [(v,lxml_html.fragment_fromstring(v,create_parent='div')) for v in versions]
    wanted = _letters(column)
    matching = [v for v,tree in trees if _letters(''.join(tree.itertext()))==wanted]
    if matching:
        gen.require(len(matching)==1,'new_variant_descriptions_differ')
        return safe_html(matching[0]),'buyers_html'
    gen.require(len(trees)==1,'new_variant_descriptions_differ')
    original,tree = trees[0]
    safe_html(original)
    updated = _one_text_edit(tree,column)
    if updated is not None:
        return safe_html(updated),'admin_csv_in_original_html'
    # A replacement crossing formatting boundaries cannot be styled from
    # evidence. Keep current text and line breaks; retain both raw archives.
    return safe_html(plain),'admin_csv_revised_text'


def _distinct_axes(axes, rows, origins):
    groups = defaultdict(list)
    for code, values in axes.items():
        groups[gen.key(values)].append(code)
    for codes in groups.values():
        if len(codes)<2:
            continue
        # Legacy hidden variants can have the same label or inherited buyer
        # dimensions. Their literal, distinct catalog identities stay visible
        # in the private plan and distinguish the native option. An active
        # duplicate combination needs proof of physical identity first.
        gen.require(all(source.flag(named(rows[c],'Viditeľný')) is False and 'Kód variantu' not in axes[c]
                        for c in codes), 'new_axis_collision')
        for code in codes:
            axes[code] = {**axes[code],'Kód variantu':code}
            origins[code] = 'hidden_duplicate_axes_with_source_sku'
    gen.require(len({gen.key(v) for v in axes.values()})==len(axes),'new_axis_collision')
    return axes,origins


def _axes(family, rows, xml_rows, vocabulary):
    """Keep known original axes; supply an exact source label for CSV-only variants."""
    resolved = {}
    suffixes = {}
    root_title = named(rows[family.parent], 'Názov tovaru').strip()
    for code, row in rows.items():
        if code == family.parent:
            continue
        title = named(row, 'Názov tovaru').strip()
        suffix = title[len(root_title):].strip(' -|,') if root_title and title.startswith(root_title) else title
        suffixes[code] = suffix or title
        xml = xml_rows.get(code)
        raw = {n.tag: (n.text or '').strip() for n in xml.findall('VARIANT_PARAMETERS/*')} if xml is not None else {}
        if raw:
            mapping = family.axis_names
            gen.require(all(tag in mapping and mapping[tag] for tag in raw), 'new_axis_names_required')
            axes = {mapping[tag]:value for tag,value in raw.items()}
            gen.require(len(axes) == len(raw) and all(axes.values()), 'new_axis_names_required')
            resolved[code] = axes
    if len(resolved) == len(suffixes):
        return _distinct_axes(resolved,rows,{})
    # The source often renders dimensions in a different order from the
    # numeric XML tags (e.g. size before colour). Derive that order from all
    # exported siblings. Decode hidden names only when exactly one complete
    # combination of independently observed source values fits the title.
    if resolved:
        keys = tuple(next(iter(resolved.values())))
        norm = lambda s: ' '.join(s.split())
        names_to_tags = {n:t for t,n in family.axis_names.items()}
        all_values = {name:{norm(v):v for v in vocabulary.get(names_to_tags.get(name),set())}
                      for name in keys}
        for axes in resolved.values():
            for name,value in axes.items():
                if name in all_values:
                    all_values[name][norm(value)] = value
        templates = []
        if len(keys)<=4 and all(set(a)==set(keys) for a in resolved.values()):
            for order in permutations(keys):
                if all(norm(suffixes[c])==norm(' '.join(a[k] for k in order)) for c,a in resolved.items()):
                    templates.append(order)
        for code, suffix in suffixes.items():
            if code in resolved:
                continue
            solutions = {0:{},1:{}}
            words = norm(suffix).split()
            if not words or len(words)>30:
                continue
            for order in templates:
                # Existing siblings determine both each axis and its position.
                # At most one previously unseen literal may be derived from a
                # name, only when the other independently known axes delimit it.
                prefixes = [({},0,0)]
                for i,name in enumerate(order):
                    following = []
                    for built,start,unknown in prefixes:
                        for end in range(start+1,len(words)-(len(order)-i-1)+1):
                            token = ' '.join(words[start:end])
                            known = all_values[name].get(token)
                            misses = unknown + int(known is None)
                            if misses<=1:
                                following.append(({**built,name:known or token},end,misses))
                    prefixes = following
                for axes,end,unknown in prefixes:
                    if end==len(words):
                        solutions[unknown][gen.key(axes)]=axes
            candidates=solutions[0] or solutions[1]
            if len(candidates)==1:
                resolved[code]=next(iter(candidates.values()))
    missing = set(suffixes) - set(resolved)
    if not missing:
        return _distinct_axes(resolved,rows,{})
    # Upgates permits a family to contain different sets of variant
    # parameters. Preserve every known named dimension. For leaves with no
    # proven numeric source dimension retain the original CSV variant label.
    # Literal SKU distinguishes two otherwise identical source labels.
    origins = {}
    counts = Counter(suffixes[code].casefold() for code in missing)
    used = {gen.key(a) for a in resolved.values()}
    for code in sorted(missing):
        suffix = suffixes[code]
        axes = {'Variant': suffix} if suffix else {}
        if not suffix or counts[suffix.casefold()] > 1 or gen.key(axes) in used:
            axes['Kód variantu'] = code
        gen.require(gen.key(axes) not in used,'new_axis_collision')
        resolved[code] = axes
        used.add(gen.key(axes))
        origins[code] = 'admin_csv_variant_name' if suffix else 'admin_csv_variant_code'
    return _distinct_axes(resolved,rows,origins)


def _display_properties(parent, rows, nodes, supplied_names):
    """Restore display units from XML without promoting leaf-specific values to the parent."""
    attrs = characteristics(parent)
    if not nodes:
        return attrs
    per_name = defaultdict(dict)
    for code, node in nodes.items():
        child = characteristics(rows[code])
        properties = list(node.findall('PROPERTIES/*'))
        assigned = set()
        for prop in properties:
            value = (prop.text or '').strip()
            if not value:
                continue
            label = supplied_names.get(prop.tag)
            if not label:
                # The CSV contains both the public characteristic name and its
                # unformatted value. The XML often appends a display unit.
                candidates=[]
                for k, raw in child.items():
                    variants=raw if isinstance(raw,list) else [raw]
                    if any(value==v or value.startswith(v+' ') and len(value)-len(v)<=12 for v in variants):
                        candidates.append(k)
                if len(candidates)==1:
                    label=candidates[0]
            if label and label not in assigned:
                per_name[label][code] = value
                assigned.add(label)
    for label, values in per_name.items():
        distinct=set(values.values())
        if len(distinct)!=1 or len(values)!=len(nodes):
            continue
        value=next(iter(distinct))
        current=attrs.get(label)
        if isinstance(current,list):
            continue
        if current:
            if value == current or value.startswith(current+' '):
                attrs[label]=value
        elif len(nodes)==len(rows) or len(nodes)==len(rows)-1:
            # Do not assign an XML-only property to unobserved hidden siblings.
            if all(code in nodes for code in rows if rows[code] is not parent):
                attrs[label]=value
    return attrs


def _params(attrs):
    return [dict(descriptions=[dict(language='sk',name=k)],
                 values=[dict(descriptions=[dict(language='sk',value=v)]) for v in
                         (values if isinstance(values,list) else [values])]) for k,values in attrs.items()]


def build(family, details, csv_source, xml_rows, options, native_codes):
    root = family.parent
    gen.require(root in csv_source and details, 'new_parent_source_missing')
    row = csv_source[root]
    v = fields(row)
    # During a shared preview csv_source can contain several families.
    owners = getattr(csv_source,'owners',{})
    children = {k:r for k,r in csv_source.items() if k != root and
                (owners.get(k)==root or _code(named(r,'Kat. číslo rodiča')) in {root,_code(v('Interné číslo'))}-{''})}
    codes = set(children) or {root}
    gen.require(set(details) == codes and set(xml_rows) <= codes, 'new_source_family_changed')
    rows = {root:row, **children}
    axes, axis_origins = _axes(family, rows, xml_rows, getattr(csv_source,'axis_values',{})) if children else ({}, {})
    visible = source.flag(v('Viditeľný'))
    gen.require(isinstance(visible,bool), 'generation_visibility_changed')
    nodes = list(xml_rows.values())
    desc = dict(language='sk',active_yn=visible,title=v('Názov tovaru'),
                seo_title=v('SEO titulka'),seo_description=v('SEO popis'))
    description_sources = {}
    for key,tag,column in [('short_description','DESCRIPTION_SHORT','Krátky popis'),
                           ('long_description','DESCRIPTION_LONG','Dlhý popis')]:
        desc[key],description_sources[key]=_original(nodes,tag,v(column))
    fallback_title = False
    if not desc['title']:
        gen.require(not visible, 'new_parent_source_missing')
        desc['title'] = root
        fallback_title = True
    brand = v('Výrobca').strip() or family.brand
    gen.require(not brand or v('Výrobca').strip() or desc['title'].split('|')[0].strip().casefold() == brand.casefold(),
                'new_brand_unconfirmed')
    paths = list(dict.fromkeys([v('Hlavna kategória')]+[value for key,value in row if key.startswith('Ďalšia kategória') and value]))
    main = paths[0]
    registry = options['categories']
    contexts = options.get('category_context',{})
    contextual = contexts.get(main,{})
    gen.require(main and type(registry.get(main)) is int and registry[main]>0, 'new_category_unmapped')
    mapped = [registry[main]]
    absent_categories=[]
    for path in paths[1:]:
        category = registry.get(path) or contextual.get(path)
        if not category:
            alternatives = {contexts[p][path] for p in paths if path in contexts.get(p,{})}
            category = alternatives.pop() if len(alternatives)==1 else None
        if type(category) is int and category>0:
            if category not in mapped:
                mapped.append(category)
        else:
            absent_categories.append(path)
    categories = [dict(category_id=category,main_yn=i==0) for i,category in enumerate(mapped)]
    source_images = [u.strip() for u in v('Obrázky').split('|') if u.strip()]
    captions = {u:v('Popisy k obrázkom '+str(i+1)) for i,u in enumerate(source_images)}
    if nodes and not children:
        source_images = gen.image_paths(nodes[0]) + source_images
    parent_vat = decimal_value(v('DPH'))
    vat = parent_vat
    if children:
        rates = {decimal_value(named(r,'DPH')) for r in children.values()}
        gen.require(len(rates)==1 and (vat == 0 or vat in rates), 'new_parent_variant_vat_conflict')
        # Non-selling placeholder parents in Atomer sometimes carry a zero
        # rate; Upgates stores the shared saleable rate at parent level.
        vat = rates.pop()
    product = dict(code=root,active_yn=visible,descriptions=[desc],categories=categories,
                   prices=pricing(row,options['pricelist'],active=visible and not children),vats={'sk':float(vat)},
                   weight=grams(v('Váha')),images=image_list(source_images,captions))
    if brand:
        product['manufacturer'] = brand
    attrs = _display_properties(row,rows,xml_rows,options.get('property_names',{}))
    if attrs:
        product['parameters'] = _params(attrs)
    readable, expected_flags, price_differences, corrected_barcodes, placeholders = {}, {}, {}, {}, {}
    if children and v('Dodacia doba').strip():
        parent_label = v('Dodacia doba').strip()
        gen.require(parent_label.casefold() in options['availability'], 'new_availability_unknown')
        product['availability_id'] = options['availability'][parent_label.casefold()]
        readable[root] = parent_label
    if v('Súbory na stiahnutie') or decimal_value(v('Body navyše')) or decimal_value(v('Min. obj. množstvo')) not in (0,1):
        gen.require(False, 'new_additional_source_fields')
    related = {c.strip() for c in v('Súvisiace tovary').split(',') if c.strip()}
    if not children and nodes:
        related.update(t.text.strip() for t in nodes[0].findall('RELATED_PRODUCTS/PRODUCT_ID') if t.text and t.text.strip())
    references = options.get('reference_codes', {})
    targets = {ref: references.get(ref, ref) for ref in related}
    product['related'] = sorted({target for target in targets.values() if target in native_codes and target!=root})
    absent_related = sorted(ref for ref, target in targets.items() if target not in native_codes)
    expected_flags[root], labels = mapped_flags(row,options.get('labels',{}))
    if labels:
        product['labels'] = labels
    default_child = None
    if children:
        default_child = next((code for code,r in children.items() if source.flag(named(r,'Viditeľný')) is True),next(iter(children)))
    for code, child in (children or {root:row}).items():
        cv = fields(child)
        d = details[code]
        n = xml_rows.get(code)
        ean = cv('EAN kód').strip()
        active = source.flag(cv('Viditeľný'))
        st = d['source_state']
        gen.require(isinstance(active,bool) and ean == _code(d.get('ean')) and st['product'] is visible and
                    (not children or st['variant'] is active and st['variant_origin']=='admin_csv')
                    and st['product_origin']=='admin_csv', 'new_leaf_identity_changed')
        published_ean=ean
        proof=(options.get('barcode_corrections') or {}).get(code)
        if proof or ean and ean not in verified_barcodes([ean]):
            if proof:
                revised=proof.get('ean')
                url=urlsplit(proof.get('manufacturer_or_supplier') or '')
                gen.require(ean not in verified_barcodes([ean]) and proof.get('before')==ean and proof.get('parent')==root and
                            (bool(ean) or type(proof.get('required_card_id')) is int and proof['required_card_id']>0) and
                            isinstance(revised,str) and revised in verified_barcodes([revised]) and
                            url.scheme=='https' and url.hostname and not url.username and not url.password and
                            len(str(proof.get('evidence') or ''))>=20, 'new_leaf_ean_unverified')
                published_ean=revised
                corrected_barcodes[code]=dict(proof)
            elif ean==code and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*',ean) and re.search(r'[A-Za-z]',ean):
                # An alphanumeric SKU repeated in the source barcode field is
                # an alternate spelling of the same inventory code, never a
                # GTIN. Keep the unedited cell in the private receipt.
                placeholders[code]=ean
                published_ean=''
            else:
                gen.require(False,'new_leaf_ean_unverified')
        # A missing GTIN is distinct from a malformed nonempty one. Keep an
        # empty source barcode absent, leaving exact scoped SKU identity intact.
        leaf = dict(code=code,active_yn=active,weight=grams(cv('Váha')),
                    prices=pricing(child,options['pricelist'],active=active and visible))
        if published_ean:
            leaf['ean'] = published_ean
        if n is not None:
            gen.require(_code(n.findtext('EAN')) == ean and source.flag(n.findtext('VISIBILITY')) is active and
                        decimal_value(n.findtext('VAT')) == decimal_value(cv('DPH')), 'new_leaf_source_conflict')
            gross = decimal_value(cv('Bežná cena'))
            buyers_gross = decimal_value(n.findtext('PRICE_VAT'))
            if gross!=buyers_gross:
                # The uploaded administrative export supplies current prices.
                # Confirm the tax arithmetic before superseding an older feed.
                net = decimal_value(cv('Bežná cena bez DPH'))
                gen.require(abs(net*(1+vat/100)-gross)<Decimal('0.011'), 'new_leaf_source_conflict')
                price_differences[code] = {'buyers':str(buyers_gross),'admin_csv':str(gross)}
        label = cv('Dodacia doba').strip()
        if label:
            gen.require(label.casefold() in options['availability'], 'new_availability_unknown')
            leaf['availability_id'] = options['availability'][label.casefold()]
            readable[code] = label
        registry = options.get('labels',{})
        expected_flags[code], labels = mapped_flags(child,registry)
        if children:
            labels.extend(dict(label_id=registry[name],active_yn=False) for name in expected_flags[root]
                          if name not in expected_flags[code])
        if labels:
            leaf['labels'] = labels
        if children:
            leaf.update(parameters=gen.parameters(axes[code]),main_yn=code==default_child)
            gallery = [u.strip() for u in cv('Obrázky').split('|') if u.strip()]
            child_captions = {u:cv('Popisy k obrázkom '+str(i+1)) for i,u in enumerate(gallery)}
            if n is not None:
                gallery = gen.image_paths(n) + gallery
            # The legacy writer uses //variants/ in XML and /variants/ in CSV.
            # Deduplicate by normalized resource path without changing source URLs.
            seen = set()
            unique = []
            for url in gallery:
                key = re.sub('/+', '/',urlsplit(checked_image(url)).path)
                if key not in seen:
                    unique.append(url)
                    seen.add(key)
            if unique:
                leaf['image'] = {'url':unique[0]}
                leaf['images'] = image_list(unique,child_captions)
            product.setdefault('variants',[]).append(leaf)
        else:
            product.update(leaf)
    return dict(payload=product,availability=readable,flags=expected_flags,related=sorted(related),
                missing_related=absent_related,seo_keywords=v('SEO kľúčové slová'),
                variant_axis_origins=axis_origins,missing_categories=absent_categories,
                admin_price_changes=price_differences,title_from_code=fallback_title,barcode_corrections=corrected_barcodes,
                source_barcode_placeholders=placeholders,
                description_sources=description_sources,
                variant_main_origin='first_visible_in_source_order' if default_child else None,
                source_duplicate_ids={code:list(ids) for code,ids in getattr(csv_source,'duplicate_ids',{}).items() if code in codes},
                source_parent_code_alias=next((dict(source_sku=raw,source_internal_id=alias,published_code=root)
                                              for raw,alias in getattr(csv_source,'parent_aliases',{}).items() if alias==root),None),
                parent_vat_from_leaves=({'admin_parent':str(parent_vat),'saleable':str(vat)} if parent_vat!=vat else None),
                managed_label_ids=sorted({value for key,value in options.get('labels',{}).items()
                                          if key in FLAGS and type(value) is int and value>0}),
                issues=[f'Chýba doplnková kategória: {path}' for path in absent_categories])
