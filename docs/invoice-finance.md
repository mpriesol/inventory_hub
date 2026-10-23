# Úhrady a finančný prehľad faktúr

Hub eviduje doklady vystavené dodávateľom alebo e-shopom. Platby v tejto evidencii sú ručne zaznamenané účtovné skutočnosti; neposielajú peniaze, nevystavujú faktúru a nemenia príjem ani sklad. Pôvodný súbor, ceny a položky zdrojového dokladu zostávajú v archíve.

## Sumy a stav

Finančný prehľad používa sumu vrátane DPH a skutočný zostávajúci zostatok. Čiastočná úhrada 30 EUR na faktúre 100 EUR znamená 70 EUR zostávajúcich na úhradu. Splatnosť sa posudzuje iba pri známom nenulovom zostatku. Neznáma cena, mena alebo doterajšia úhrada zostáva neznáma; nenahrádza sa nulou, EUR ani predvolenou sadzbou DPH.

Sumy sa počítajú cez `Decimal`. Nové finančné API vracia desatinné reťazce a odlišuje `"0.00"` od `null`. Pri ručnej oprave celkovej sumy, sadzby alebo základu DPH na samostatne nahratom doklade sa odvodený základ, daň a suma s DPH prepočítajú spolu s presnosťou na cent. Prijaté doklady zo synchronizácie dodávateľa alebo e-shopu nemenia obchodné údaje ručnou úpravou; upraviť možno poznámku a splatnosť. Po prvom ručnom zázname platby je finančný základ uzamknutý aj na samostatne nahratom doklade.

Dobropis má vo finančnom prehľade záporný účinok bez ohľadu na to, či ho vystaviteľ zapísal kladnými alebo zápornými sumami. Pôvodné hodnoty zostávajú zachované. Vyrovnanie dobropisu zapisuje zápornú uhradenú sumu; vrátenie tohto vyrovnania má opačné znamienko. Zostatok je vedený samostatne pre každý doklad. Hub automaticky nezapočítava dobropis proti inej faktúre.

Prehľady, mesačný graf a rozdelenie podľa splatnosti oddeľujú jednotlivé meny a nákupné/predajné doklady. Bez výslovného kurzu sa nič neprepočítava. Spoločná skalárna suma je `null`, ak výber obsahuje viac mien alebo neznámu menu. `unknown_amount_count` a `unknown_outstanding_count` označujú neúplné finančné údaje; súčet príslušnej skupiny je vtedy neznámy. Dobropisy znižujú čistý súčet v danej mene, nemenia však stav úhrady inej faktúry.

## Záznamy platieb

`invoice_payments` obsahuje nemenné záznamy typu `opening`, `payment`, `refund` a `reversal`. Prvá ručná operácia zmrazí známu zdrojovú úhradu ako počiatočný stav. Ak zdroj alebo staršia evidencia neurčuje doteraz zaplatenú sumu, obsluha ju musí výslovne uviesť v `opening_paid_amount`. Samotná stará predvolená značka `unpaid` nie je dôkazom nulovej úhrady. Výslovný zdrojový údaj o nulovej úhrade použiteľný je.

Ďalšie obnovy zdroja zaznamenajú svoje vlastné údaje `source_paid_amount`, `source_payment_status` a `source_paid_at`, ale nemenia ručnú históriu ani zmrazený počiatočný stav. Kým ručná história neexistuje, zmena zdrojovej úhrady zvýši revíziu, aby starší otvorený formulár nemohol nepozorovane vychádzať z iného počiatočného stavu.

Oprava platby pridá opačný záznam s väzbou `reverses_payment_id`. Pôvodný záznam zostáva čitateľný. Ten istý záznam možno opraviť iba raz; počiatočný stav a samotnú opravu nemožno týmto tlačidlom zrušiť. Každá operácia musí zachovať uhradený zostatok medzi nulou a sumou príslušného dokladu. Preplatky, cudzie meny a automatické párovanie platieb z banky nie sú súčasťou tejto evidencie.

## API a opakovanie

Všetky cesty `/invoices` vyžadujú existujúci prístup obsluhy a vracajú `Cache-Control: no-store`. Finančné mutácie používajú aktuálnu `payment_revision`; kontrola a zápis prebehnú pod zámkom toho istého dokladu.

| Metóda a cesta | Význam |
|---|---|
| `POST /invoices/{id}/payments` | `request_id`, `expected_revision`, `kind` (`payment`/`refund`), kladné `amount` ako reťazec, `paid_at` s časovým pásmom, poznámka a prípadný počiatočný stav. Znamienko zápisu určí typ dokladu a operácie. |
| `POST /invoices/{id}/payments/{payment_id}/reverse` | Nové UUID, aktuálna revízia a poznámka; pridá opačný záznam. |
| `GET /invoices/{id}/payment-operations/{request_id}` | Pôvodná uložená odpoveď dokončenej operácie. |
| `PATCH /invoices/{id}` | Revíziou chránená oprava povolených metadát. |
| `GET /invoices/{id}` | `finance`: sumy, odvodený stav, splatnosť, revízia, neistoty a história úhrad. |
| `GET /invoices` a `/invoices/stats` | Filter `direction=purchase|sale`, skupiny `totals_by_currency`, mesačné údaje `monthly` a zostatky `aging`. Predvolený smer je nákup. |

UUID označuje jednu konkrétnu operáciu. Presné zopakovanie vracia pôvodnú odpoveď aj po ďalších úhradách, bez ďalšieho zápisu. Zmenený obsah s rovnakým UUID skončí konfliktom. Pri strate odpovede klient zachová UUID aj obsah; najskôr načíta uložený výsledok a až potom môže výslovne zopakovať tú istú operáciu. Chýbajúci výsledok čítania sám osebe neoprávňuje na nové UUID.

`known_unapplied=true` označuje preukázané odmietnutie aktuálnej operácie pred zápisom, napríklad zmenenú revíziu. Pri konflikte UUID sa tento príznak neuvádza ako pravdivý, pretože pod daným UUID už mohla byť uložená iná operácia. Staršie priame prepínanie `PATCH /invoices/{id}/payment` vracia `invoice_payment_ledger_required`; nemôže obísť históriu úhrad.

## Overenie

`test_invoice_finance.py` pokrýva centové zaokrúhlenie, nulové hodnoty, neznámu sadzbu/úhradu, viaceré meny, čiastočné úhrady a oba zdrojové zápisy dobropisu. `test_invoice_finance_db.py` overuje PostgreSQL transakcie, súbeh, UUID, revízie, zmrazenie počiatočného stavu, opravy a zákaz úprav histórie. `test_invoice_routes.py` overuje prístup obsluhy, bezpečné chybové odpovede a rozhrania. Databázové prípady vyžadujú izolovanú lokálnu databázu `*_catalog_test`; ich preskočenie nie je potvrdením funkčnosti migrácie.
