# Prepojenie existujúcich skladových kariet s e-shopom

Prepojenie priradí existujúcej karte Hubu konkrétny existujúci produkt alebo variant v BIKETREK či xTrek. Nevytvára produkt v e-shope, nemení jeho ceny ani zásoby a nezakladá ďalšiu kartu v Hube. Potvrdenie fyzického skladu zostáva samostatným príjmom alebo inventúrnou opravou.

## Postup obsluhy

1. Otvorte **Sklad**. Pre karty bez potvrdeného príjmu vyberte **Skladová evidencia → Všetky produkty** alebo **Nepotvrdené položky**.
2. Otvorte detail jednej karty alebo označte konkrétne riadky v tabuľke a zvoľte prepojenie s existujúcim produktom e-shopu. Najprv uložte alebo zahoďte rozpracované úpravy buniek.
3. Vyberte cieľový e-shop. Jedna dávka obsahuje 1 až 100 vybraných kariet; ďalší e-shop má vlastný náhľad a výsledok.
4. Pripravte náhľad. Presné spoločné SKU umožňuje automatické nájdenie. Jednoznačný overený EAN môže pomôcť pri kandidátovi známom z uložených údajov e-shopu; nejde o hromadné prehľadanie celého vzdialeného katalógu podľa EAN.
5. Ak sa automatická zhoda nenájde, zadajte konkrétny existujúci kód cieľového produktu alebo variantu a podľa potreby jeho rodiča. Skontrolujte názov, kód, EAN, rodiča a typ položky. Rodič s variantmi nie je samostatná fyzická skladová položka; označenie hlavného variantu na tom nič nemení.
6. Skontrolujte náhľad, označte pripravené riadky a potvrďte prepojenie. Riadky s konfliktom alebo bez nájdeného cieľa sa nemôžu uložiť. Už správne prepojené položky zostanú zachované.

Chýbajúca farba alebo veľkosť v odpovedi znamená neznámy údaj, nie dôkaz nesprávneho variantu. Rozhodujú overené identifikátory a explicitný výber; dostupné protichodné údaje sa nesmú ignorovať. Názov, podobnosť názvu ani cena samy neautorizujú prepojenie. Predajné ceny oboch e-shopov sa môžu líšiť.

Ak sa stratí odpoveď pri ukladaní, obnovte výsledok tej istej dávky. Nevytvárajte náhradnú dávku, kým nie je známy výsledok pôvodnej. Opakovanie rovnakého uloženia nezdvojuje väzby; zmena výberu už dokončenej dávky sa odmietne. Pri zmene identity alebo vypršaní náhľadu treba najprv overiť stav pôvodnej dávky a až potom pripraviť nový náhľad.

Pri jednoznačnom odmietnutí platného uloženia server pod spoločným zámkom najprv vráti prípadný už dokončený výsledok alebo trvalo ukončí platnosť náhľadu. Až potom označí chybu `known_unapplied: true`. Rozhranie tak môže povoliť nový náhľad aj po skoršej stratenej odpovedi; oneskorený starý pokus už nemôže zapísať väzby. Samotný stav `prepared` ani sieťová chyba tento dôkaz neposkytujú.

## Kontrakt API

Všetky cesty vyžadujú operátorské prihlásenie, bežnú CSRF ochranu pre cookie relácie a odpovede `Cache-Control: no-store`.

| Cesta | Účinok |
| --- | --- |
| `POST /api/product-links/candidates` | Ponúkne obmedzený zoznam kandidátov z uložených údajov; samotný kandidát ešte neoprávňuje zápis. |
| `POST /api/product-links/preview` | Uloží náhľad vybraných kariet pre jeden e-shop pod klientskym `request_id`. Kontroluje živú vzdialenú identitu cez existujúci limitovaný Upgates klient. |
| `GET /api/product-links/{id}` | Vráti uložený náhľad alebo dokončený výsledok na obnovu po výpadku. |
| `POST /api/product-links/{id}/apply` | Potvrdí presný náhľad a vybranú podmnožinu pripravených riadkov. Väzby zapisuje v jednej transakcii. |

Náhľad uchováva konkrétne SKU, typ produktu/variantu, rodiča, natívne vzdialené ID a identifikačné dôkazy. Pred zápisom sa kontroluje aktuálne spojenie s e-shopom, vzdialená identita aj lokálne vlastníctvo cez spoločný `product_identity` resolver. Zápis používa `IDENTITY_WRITE_LOCK`; existujúca väzba sa potichu nepresmeruje na iný produkt. Konflikt SKU, overených čiarových kódov alebo vlastníkov zastaví zápis.

Vyhľadávanie používa filtrované čítanie vybraných kódov. Upgates rozlišuje filter `codes` pre produkty a `variant_codes` pre varianty. Bežný `/products` nemusí obsahovať variantové parametre; presné varianty sa čítajú cez samostatný `/products/variants` vrátane ich natívneho rodiča. Produktový zoznam môže vynechať obsah variantov, ale samostatná fyzická položka sa prijme iba pri explicitnom potvrdení, že produkt nemá varianty ani nie je súpravou. Chýbajúce parametre sa nedopĺňajú odhadom z rodiča. Jedna fáza používa najviac osem požiadaviek a rešpektuje spoločný API rozpočet; náhľad ani prepojenie neposielajú externý zápis.

Migrácia `027_product_links.sql` pridáva iba trvalé náhľady a výsledky do `product_link_previews`. Nespúšťa párovanie historických produktov ani nepovoľuje automatizáciu zásob. Nasadenie obsahuje migráciu pred reštartom API. Pri návrate na kompatibilný starší kód možno tabuľku ponechať; nemažte uložené výsledky potrebné na obnovu.

Implementačné a syntetické testy nie sú dôkazom ostrého prepojenia konkrétneho produktu. Výsledok živej dávky sa overuje v jej uloženom výsledku a na karte v Hube.
