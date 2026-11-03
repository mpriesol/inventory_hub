"""Exact source parameter values and presentation-only HTML adaptation.

Parameter names are an explicit reviewed import mapping. Values always come
from the original selected buyers row; commas and units are never split.
"""
from copy import deepcopy
import json
import re

import html5lib
from lxml import etree

from inventory_hub.adapters.atomer_content import AtomerSourceError, _html_tree


def fail(suffix):
    raise AtomerSourceError('atomer_source_' + suffix)


def canonical_parameters(rows):
    if not isinstance(rows, list) or len(rows) > 200:
        fail('invalid_parameters')
    result = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'descriptions', 'values'}:
            fail('invalid_parameters')
        def descriptions(items, field, limit):
            if not isinstance(items, list) or not items or len(items) > 30:
                fail('invalid_parameters')
            normalized, languages = [], set()
            for item in items:
                if (not isinstance(item, dict) or set(item) != {'language', field}
                        or not isinstance(item['language'], str)
                        or not re.fullmatch(r'[a-z]{2,3}(?:-[A-Za-z]{2,4})?', item['language'])
                        or item['language'] in languages
                        or not isinstance(item[field], str) or not item[field].strip()
                        or len(item[field]) > limit
                        or any(ord(char) < 32 for char in item[field])):
                    fail('invalid_parameters')
                languages.add(item['language']); normalized.append(deepcopy(item))
            return sorted(normalized, key=lambda value: value['language'])
        values = row['values']
        if not isinstance(values, list) or not values or len(values) > 100:
            fail('invalid_parameters')
        normalized_values = []
        for value in values:
            if not isinstance(value, dict) or set(value) != {'descriptions'}:
                fail('invalid_parameters')
            normalized_values.append({'descriptions': descriptions(value['descriptions'], 'value', 2000)})
        normalized_values.sort(key=lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True))
        if len({json.dumps(value, sort_keys=True) for value in normalized_values}) != len(normalized_values):
            fail('invalid_parameters')
        result.append({'descriptions': descriptions(row['descriptions'], 'name', 255), 'values': normalized_values})
    result.sort(key=lambda value: json.dumps(value['descriptions'], ensure_ascii=False, sort_keys=True))
    names = [slovak_name(row) for row in result if slovak_name(row) is not None]
    if len(set(names)) != len(names):
        fail('ambiguous_parameters')
    return result


def slovak_name(row):
    return next((item['name'].casefold() for item in row['descriptions'] if item['language'] == 'sk'), None)


def source_parameters(group, names, previous):
    rows = group['rows']
    if len(rows) != 1 or rows[0]['parent_code']:
        fail('parameters_standalone_required')
    try:
        root = etree.fromstring(rows[0]['raw_xml'].encode(), etree.XMLParser(
            load_dtd=False, no_network=True, resolve_entities=False))
    except (ValueError, etree.XMLSyntaxError):
        fail('invalid_parameters')
    properties = root.findall('PROPERTIES')
    if len(properties) != 1 or not len(properties[0]):
        fail('parameter_mapping_required')
    selected, source_ids, selected_names = [], set(), set()
    for prop in properties[0]:
        name = names.get(prop.tag)
        if (not isinstance(prop.tag, str) or not re.fullmatch(r'PROPERTY_\d+', prop.tag)
                or prop.tag in source_ids or len(prop) or not isinstance(name, str)
                or name.casefold() in selected_names):
            fail('parameter_mapping_required')
        source_ids.add(prop.tag); selected_names.add(name.casefold())
        selected.append({'descriptions': [{'language': 'sk', 'name': name}],
                         'values': [{'descriptions': [{'language': 'sk', 'value': prop.text or ''}]}]})
    previous = canonical_parameters(previous)
    for row in previous:
        if slovak_name(row) in selected_names and any(item['language'] != 'sk'
                for item in [*row['descriptions'], *(item for value in row['values'] for item in value['descriptions'])]):
            fail('parameter_translation_conflict')
    # Preserve every parameter outside the selected source names.
    return canonical_parameters([row for row in previous if slovak_name(row) not in selected_names] + selected)


def format_for_upgates(value):
    root, _ = _html_tree(value)
    text_before = list(root.itertext())
    keep_styles = {'font-weight', 'font-style', 'text-decoration', 'vertical-align'}
    for node in root.iter():
        node.attrib.pop('class', None)
        style = node.attrib.pop('style', '')
        kept = [part.strip() for part in style.split(';') if ':' in part
                and part.split(':', 1)[0].strip().lower() in keep_styles]
        if kept:
            node.set('style', '; '.join(kept))
    formatted = html5lib.serialize(root, tree='etree', quote_attr_values='always',
                                  omit_optional_tags=False, alphabetical_attributes=True)
    verified, _ = _html_tree(formatted)
    if list(verified.itertext()) != text_before:
        fail('formatting_text_changed')
    return formatted
