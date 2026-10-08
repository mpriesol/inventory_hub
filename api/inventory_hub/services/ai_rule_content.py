"""Compile published source rules for the product-content API, without rewriting them.

Only reviewed document/module/revision combinations have a selection policy.
Unknown revisions and operator-written instructions remain intact. Selection is
structural, never a keyword filter or an AI summary. The source and result hashes
make each new job's selection inspectable; old frozen jobs are not recompiled.
"""
from __future__ import annotations

import hashlib
import re


VERSION = "content-v1"
_REVISIONS = {"01": "1.1 rev14", "03": "1.1 rev13", "04": "1.0 rev17",
              "user-request": "Pokyn používateľa 2026-10-07; bez číselnej revízie"}
_HEADER = re.compile(r"^Podklad: ([^|\n]+) \| ([^|\n]+) \| ([^|\n]+) \| ([^|\n]+) \| typ:([^|\n]+) \| stav:([^\n]+)\n")
_HEADINGS = re.compile(r"^#{2,6} ([0-9]+(?:\.[0-9]+)*[a-z]?)\.? [^\n]+\n", re.M)

# Digests identify the reviewed source modules, not mutable rule IDs. An edit
# keeps its complete text until its new version is explicitly reviewed.
_REVIEWED = {
    "01-intro": "835e803c740d06a7d6e2fb69496a4c04875b76db00be28c0512bf5ff3f2074ae",
    "01-1": "e2f233a4d6d02284df2df12e9ab94f3269069ab94ec72169d48f31fc968a2c2d",
    "01-2": "9db20fa1c3b4e8fc6d5ee7a9d3b7187328771e5241b2002482a29b7ed5c9ae88",
    "01-3": "8b38f11c4467ec4935b42facb3949748b9db5f0eb6bbcae7f13752eaf1ec991f",
    "01-4": "b5cdbe28d0bdaa443c624abb238f36cddef8b9308e104b74568460c5e2f07b63",
    "01-5": "8868050e2949f396945d88788c584fdc50904cb11a13c0c57aa4d7d91eb8804a",
    "01-6": "ef69b85caf1a3646106d35a884f87bcd95439223859a08c4a15f4cc86a8992ac",
    "01-7": "b0f2bd1ef5aeb276428722f5c63426b3a4117487a20ff6e89a4846d026e3a243",
    "01-8": "8c0fd6cf83749ce5ea4b345a3d1158c8c1449259654095dc0e349e37d7995879",
    "01-9": "9ad2748c3d8c8b80330bd8b4d7153560aaf7944f2f6ef2012d987c0b74b46001",
    "01-10": "5331792be82e91289b38781d4d48982de6b9c76b97678f60d94851d1622633d1",
    "01-11": "8895ff9d463930ae573a649df30b93268b9b697246ca74789a9f0783f3d2e144",
    "01-12": "d4acd4e6659ea35f9f81ea9e9f7921b486656583dbc3c8feb0383a36a34c95ab",
    "01-13": "6095e0a083e855b7261e2b9dde474581029f3351c8fb0f1968d361d6c99602c2",
    "01-14": "3944f38c1c9de7578378227925c430549838fb3b7c046c7c1a7a82117163ff73",
    "01-15": "5e157b3e103d7b8acf8be8f3d66f8f42f8b17b000a2f211c63c359ef0e18ac8b",
    "03-0": "7bc9d192bddd2b318107ed3898c7f45d071c7875e6ca4e02a0c721ec6429df2a",
    "03-1": "eaeeee6e64a632033325ae95f7172ecde4663039375972d95749cbe6e274f3fd",
    "03-2": "fb2f6ed6cb20e0d961f603fad249306ae1cf9b7053989a482609b6c8c8b07021",
    "03-2-1": "3f31c8e0794dcae536298ab281ca257371ff82db7df12cc938dd64a30861f1c5",
    "03-3": "410ca6e2ab1eae0fa1bc8235cf4c82275273b529a2e19ac5bde546df8d72f7ec",
    "03-3-1": "89af9385f834199d25fdb199386920185ba748a6ac60ec46560d341a9a031591",
    "03-4": "9c4a8516431768eb7b90150262b14284c3caee9581e6b83441ab818376469273",
    "03-5": "96c2cf277e4baf81b87f67ff4ab16f048660c40e8c50f2a8385d80a4fc8aa50e",
    "03-5-1": "37c5570a40fceee7e5a0258d3b18616ee6d3e9b7bd70de7da99e703cfe93f510",
    "03-5-2": "88e02a00c960a664569660494a7493f022f0fd0478838cff889bb7b01621c34a",
    "03-5-3": "10f6599e220132873275e85dd9faeff2136448f43fc9d27a47b21d5db1d5396e",
    "03-5-4": "18d481173c7ecd111fd51b836d72b221896146e6f1e02cc2e2eb328bd774af8e",
    "03-5-5": "8a20438a939088f4203c1705ab3bd27be3ad52b8879382e95f708a310c9a4c5a",
    "03-7": "c55a55d1a81e56231e795c03e1ae8881cd39b2517d9efa4696b6370f33a20f3d",
    "03-7-1": "0c9ae6123747dc4fa1ca7a71002d593421e5b6419291a45fd9e9da0ce729d106",
    "03-7-2": "87769d7b0ec97ec6ddd79daee286e4531b38fd9acebc3a7743a46fc2df924cd0",
    "03-7-3": "cfeaac00b77c5f9b03e7bf0da5f3fea2462bf8c018d7bc78111d25ad2fc13e7a",
    "03-7-4": "7d0df044c6ef415011119409ad80bf1f60f842943d9647bf4ae600fad77fc01f",
    "03-7-5": "5ebc3bd026b7280d70b061bfc8890bdd1c785f2d4c9147bc62105f83bc9bf5b8",
    "03-7-6": "e32e21d6563b6b62e0cff4908e11da7283e0a9952791375a11d25913aca8c976",
    "03-7-7": "e7b3e2ab65bfd4200046d66faa81621e51753f74b74b8b7a2b4665887d566420",
    "03-7-8": "b552f86a60c28126a7fcb25a9093e0aa906a3ae1de5910d820839a6dad836467",
    "03-7-9": "3a54006d677c8a6990fa2750ea9b28707d278c1564974f8b26483270016f58f5",
    "03-9": "48d8740ad015534a8945b204a66abc320bcaab3f7bf2596667e68d3d1f652858",
    "knowledge-common": "b919656bf933310eafff6c49f4d8ae8379ef432f4b90afb2f17fa4f0a2917789",
    "knowledge-usage": "dac5a6b9cad5fefb5e012eb9891dd0c280921a22665294fb70e112b7d08578bb",
    "user-request-2026-10-07": "faeaf3d3974d59989379b2ac4b8d4d80481f52c66916ac4a8ea9a54c6899da6f",
}
_LEGACY_COMMON = "db97c8cc6017c526bc96c061e189c777a140a6ae612c3255ad1c4ec88c82fa0d"

_API_CONTRACT = """Toto je obsahové API Hubu. Vytvor úplný zákaznícky obsah podľa priložených obsahových pravidiel a iba JSON podľa výstupnej schémy. Technické a konverzačné pokyny o XML, CDATA, súboroch, prihlasovaní, čakaní, spúšťaní skriptov a odovzdaní dokumentov sa na túto odpoveď nevzťahujú. Nežiadajú ďalšie výstupy ani nesmú ochudobniť texty, FAQ, tabuľky, SEO, H1 alebo doložené parametre. Nesľubuj vykonanie nepoužitých nástrojov. Dlhý popis je priamo statické HTML v long_description, bez CDATA a bez zástupných značiek; krátky popis, názov a SEO sú čistý text.
Rozsah produktu a variantov určuje výhradne dodané facts. Nepridávaj ani nevynechávaj SKU a nerozširuj produktovú rodinu podľa všeobecného postupu v zdrojovom dokumente. Parametre parent majú product_id=null aj pri samostatnom produkte. Číslo facts.id patrí do product_id iba pri skutočnom variante a existujúcej výberovej osi; scope=choice bez takej osi znamená parent.
Chýbajúci všeobecný bezpečnostný návod alebo ďalšie príslušenstvo samy nie sú missing_facts, pokiaľ nechýba rozhodujúci údaj pre bezpečné použitie alebo kompatibilitu. Nevymýšľaj chýbajúce údaje.
Ak podklady obsahujú presnú oficiálnu URL tohto modelu, najprv ju otvor. Použi aktuálny limit nástroja; ponechaj krok na otvorenie konkrétneho zdroja, nemíňaj všetky kroky opakovaným hľadaním. Jednoznačné dodané fakty netreba znova potvrdzovať."""
_TUBE_CONTRACT = """Pri dušiach zostav z doložených dvojíc a rozsahov jednu kompatibilitnú maticu a priamo z nej úplnú HTML tabuľku v long_description a zhodné hodnoty parametrov. Žiadny kartézsky súčin, domyslený rozmer ani prepočet palcov na ETRTO. API nemá samostatný generátor tejto tabuľky: požiadavka zdrojového návodu na skript nie je dôvod tabuľku vynechať alebo odovzdať značku na jej neskoršie doplnenie. Zachovaj kategóriové FAQ, užitočné vysvetlenia a schválený interný odkaz."""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _provenance(text: str):
    match = _HEADER.match(text)
    if not match:
        return None
    document, revision, section, module, kind, status = (v.strip() for v in match.groups())
    if (_REVISIONS.get(document) != revision or status != "active"
            or _REVIEWED.get(module) != _hash(text)):
        return None
    # A split chapter may start halfway through a paragraph. Never apply a
    # section policy to incomplete chunks (the package archive remains lossless).
    body = text[match.end():]
    part = re.match(r"Časť (\d+)/(\d+)\n", body)
    if part:
        if part.groups() != ("1", "1"):
            return None
        body = body[part.end():]
    return document, module, kind, text[:match.end()], body


def _without_sections(body: str, keys: set[str]) -> str:
    """Remove a numbered heading and its descendants, retaining exact other text."""
    headings = list(_HEADINGS.finditer(body))
    result, start, omitting = [], 0, None
    for heading in headings:
        key = heading.group(1)
        if omitting is not None and not (key == omitting or key.startswith(omitting + ".")):
            start, omitting = heading.start(), None
        if omitting is None and key in keys:
            result.append(body[start:heading.start()])
            omitting = key
    if omitting is None:
        result.append(body[start:])
    return "".join(result)


def _section(body: str, key: str) -> str | None:
    headings = list(_HEADINGS.finditer(body))
    for index, heading in enumerate(headings):
        if heading.group(1) != key:
            continue
        end = len(body)
        for following in headings[index + 1:]:
            if not following.group(1).startswith(key + "."):
                end = following.start()
                break
        return body[heading.start():end]
    return None


def _parameter_names(category: dict | None) -> set[str]:
    return {p["name"].casefold() for p in (category or {}).get("parameters", []) if p.get("approved", True)}


def _parameter_table(body: str, parameters: set[str]) -> str:
    # Exact table labels from the reviewed shared format chapter. Universal
    # boolean/count/number conventions and the prose below the table remain.
    optional = {"Hmotnosť (g)", "Hmotnosť (kg)", "Hmotnosť", "Nosnosť", "Objem", "Celkový objem",
                "Šírka plášťa", "Dĺžka ventilu", "Hrúbka steny", "Priemer kolesa",
                "Šírka plášťa v palcoch", "Rozmer ETRTO", "Produktový rad", "Generácia", "Rozmery", "Záruka"}
    result = []
    for line in body.splitlines(keepends=True):
        label = line.split("|", 2)[1].strip() if line.startswith("|") and line.count("|") >= 3 else None
        if label == "`<WEIGHT>` (systémové pole)":
            continue
        if label in optional and label.casefold() not in parameters:
            continue
        result.append(line)
    return "".join(result)


def _project_sections(body: str) -> str:
    # This imported ChatGPT project instruction repeats the editorial rules but
    # also asks an API call to log in, wait, run Python and hand over an XML file.
    # Retain the two full content chapters verbatim, not a shorter rewrite.
    headings = ("PLATNÉ PRAVIDLÁ", "KEDY A ČO POVINNE ČÍTAŤ", "FAKTY A VYSVETLENIA",
                "PRIRODZENÝ A UŽITOČNÝ TEXT", "PODKLADY, PRÍSTUP A EFEKTIVITA", "DOKONČENIE A KONTROLY")
    pattern = re.compile(r"^(?:" + "|".join(re.escape(v) for v in headings) + r")\n", re.M)
    matches = list(pattern.finditer(body))
    if {m.group().strip() for m in matches} != set(headings):
        return body
    return "".join(body[m.start():matches[i + 1].start() if i + 1 < len(matches) else len(body)]
                   for i, m in enumerate(matches) if m.group().strip() in
                   {"FAKTY A VYSVETLENIA", "PRIRODZENÝ A UŽITOČNÝ TEXT"})


def _select(text: str, category: dict | None) -> tuple[str, list[str]]:
    provenance = _provenance(text)
    if provenance is None:
        return text, ["unchanged"]
    document, module, kind, header, body = provenance
    if kind == "history":
        return "", ["history"]
    omit = {
        "01-intro": "workflow_only", "01-9": "unsupported_gallery_workflow",
        "01-10": "unsupported_relation_workflow", "01-13": "file_handover",
        "01-14": "rule_maintenance", "03-0": "rule_navigation", "03-1": "classification_only",
        "03-5": "empty_heading", "03-5-4": "unsupported_gallery_workflow",
        "03-9": "registry_authoring", "knowledge-usage": "rule_maintenance",
        "03-7-9": "unsupported_gallery_workflow",
    }
    if module in omit:
        return "", [omit[module]]
    parameters = _parameter_names(category)
    profile = (category or {}).get("id", "general")
    specialized = bool(category) and profile != "general"
    if document == "03" and specialized:
        required = {"03-5-1": {"objem", "celkový objem"}, "03-5-2": {"farba", "dominantná farba"}}
        if module in required and not parameters.intersection(required[module]):
            return "", ["other_category"]
        if module == "03-5-3" and not profile.startswith("clothing"):
            return "", ["other_category"]
    if module == "01-1":
        marker = "Pri rozpore rozhoduje predmet:\n"
        if marker not in body:
            return text, ["unchanged"]
        body = body[body.index(marker):]
        body = "".join(line for line in body.splitlines(keepends=True) if not line.startswith("- **XML syntax:**"))
        return header + body, ["rule_navigation", "content_precedence_preserved"]
    if module == "01-3":
        editorial = _section(body, "3.2")
        record = _section(body, "3.4")
        procedure = _section(body, "3.3")
        if editorial is None or record is None or procedure is None:
            return text, ["unchanged"]
        # Keep the content-quality and fact-record requirements. The numbered
        # tool/login/XML/delivery procedure belongs to the interactive workflow.
        review = re.search(r"^6\. \*\*Vytvor obsah individuálne\*\*[^\n]+\n", procedure, re.M)
        if review is None:
            return text, ["unchanged"]
        record = record.split("\n\nStav uvádzaj pravdivo:", 1)[0]
        return header + editorial + review.group() + "\n" + record, ["chat_and_file_workflow", "editorial_sections_preserved"]
    if module == "01-4":
        return header + _without_sections(body, {"4.7"}), ["login_workflow"]
    if module == "01-7":
        return header + _without_sections(body, {"7.6"}), ["unsupported_seo_url_and_image_titles"]
    if module == "01-8":
        weight = _section(body, "8.3")
        if weight is None or "\n\n**Dopravná hmotnosť**" not in weight:
            return text, ["unchanged"]
        return header + weight.split("\n\n**Dopravná hmotnosť**", 1)[0] + "\n", ["commercial_fields", "customer_weight_preserved"]
    if module == "01-11":
        # Preserve the one output-content distinction from the XML transport
        # chapter; all other content semantics are defined in 01/6–7 and 03.
        # The matched sentence is copied from the source, never synthesized.
        sentence = re.search(r"krátky popis, názov a SEO polia sú čistý text\.", body)
        if sentence is None:
            return text, ["unchanged"]
        return header + sentence.group() + "\n", ["technical_export", "content_format_preserved"]
    if module == "01-12":
        return header + _without_sections(body, {"12.1"}), ["validator_execution"]
    if module == "03-2-1":
        marker = "Znalostný blok je inšpirácia"
        if marker in body:
            return header + body[body.index(marker):], ["rule_navigation", "knowledge_scope_preserved"]
    if module == "03-3" and specialized:
        return header + _parameter_table(body, parameters), ["parameter_formats_for_profile"]
    if document == "user-request" and module == "user-request-2026-10-07":
        return header + _project_sections(body), ["chat_and_file_workflow", "editorial_sections_preserved"]
    return text, ["unchanged"]


def compile_instructions(instructions: list[dict], category: dict | None, shop: str) -> tuple[list[dict], dict]:
    """Pure selection for a new snapshot. Does not mutate any input or use facts."""
    selected, entries = [], []
    source_modules = {p[1] for row in instructions if (p := _provenance(row["text"]))}
    # A reviewed legacy digest may disappear only with all its content sources
    # present. The API-only differences are explicitly preserved below.
    replaces_digest = {"01-1", "01-2", "01-4", "01-5", "01-6", "01-7", "03-2", "03-3", "03-4"} <= source_modules
    compiled = shop == "biketrek" and bool(source_modules)
    for instruction in instructions:
        original = instruction["text"]
        value, reasons = _select(original, category) if shop == "biketrek" else (original, ["unchanged"])
        provenance = _provenance(original)
        if compiled and replaces_digest and (_hash(original) == _LEGACY_COMMON or
                (provenance and provenance[1] == "user-request-2026-10-07")):
            value, reasons = "", ["superseded_digest"]
        if value:
            selected.append({**instruction, "text": value})
        entries.append({"id": instruction["id"], "name": instruction["name"],
            "status": "omitted" if not value else "included" if value == original else "filtered",
            "source_characters": len(original), "selected_characters": len(value), "reasons": reasons,
            "source_sha256": _hash(original), "selected_sha256": _hash(value)})
    source_count = sum(e["source_characters"] for e in entries)
    retained_count = sum(e["selected_characters"] for e in entries)
    source_included = len(selected)
    runtime_text = _API_CONTRACT
    if (category or {}).get("id") == "inner_tubes" or "03-7-7" in source_modules:
        runtime_text += "\n" + _TUBE_CONTRACT
    if compiled:
        runtime = {"id": "hub_content_api_contract", "name": "Hub – rozsah obsahového API", "text": runtime_text}
        selected.append(runtime)
        entries.append({"id": runtime["id"], "name": runtime["name"], "status": "included",
            "source_characters": 0, "selected_characters": len(runtime_text), "reasons": ["runtime_contract"],
            "source_sha256": _hash(""), "selected_sha256": _hash(runtime_text)})
    added_count = len(runtime_text) if compiled else 0
    return selected, {"version": VERSION, "category_profile": (category or {}).get("id"),
        "source_characters": source_count, "selected_characters": retained_count + added_count,
        "omitted_characters": source_count - retained_count, "added_characters": added_count,
        "included_rules": len(selected), "omitted_rules": len(instructions) - source_included, "entries": entries}
