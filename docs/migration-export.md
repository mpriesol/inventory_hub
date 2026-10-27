# Podklady na zjednotenie katalógov BIKETREK a xTrek

V prehľade **Obchody** je pri každom Upgates e-shope odkaz na ZIP export.
`GET /api/migration/catalog-export?shop_code=biketrek&refresh=true` vyžaduje
bežné operátorské prihlásenie. `shop_code` prijíma iba `biketrek` alebo `xtrek`.
Prípravný xTrek Upgates nie je pôvodný Atomer a export tohto pripojenia
nenahrádza Atomer XML.

Export obsahuje úplný produktový katalóg z vybraného Upgates pripojenia a
samostatné JSONL tabuľky všetkých lokálnych kariet, identifikátorov, variantov,
e-shopových a dodávateľských väzieb, dodávateľských katalógových identít,
historických bilancií, potvrdenia evidencie, značkových prefixov a opráv
obsahu. Identifikátory zostávajú textom, desatinné množstvá sa serializujú
presne. Súbor `manifest.json` uvádza verziu, časy pozorovania a počty záznamov.

Lokálne tabuľky sa čítajú v jednej `REPEATABLE READ READ ONLY` transakcii,
po dávkach bez skladania tabuľkového editora a bez opakovaného prehľadávania
variantov pokladničného rodiča. Upgates má vlastný čas pozorovania; nejde
o spoločný freeze. Výpadok vzdialeného čítania nevydá úspešný neúplný export.
Predvolené `refresh=false` využíva existujúcu krátkodobú katalógovú cache;
odkaz v UI vyžaduje čerstvé čítanie. Čítanie Upgates používa existujúci klient
a jeho API limity. Dočasný ZIP sa po stiahnutí uzavrie a neuchováva na serveri.

Export nie je záloha. Neobsahuje konfiguráciu, prihlasovacie údaje, zákazníkov,
objednávky, faktúry ani kompletnú pohybovú históriu. Surová bilancia sama
nepreukazuje potvrdený sklad. Dostupnosť a množstvo dodávateľa či e-shopu sú
samostatné pozorovania. Existujúce operátorské opravy sú v osobitnej tabuľke.

Samotné stiahnutie nezakladá karty, neprepája produkty, nemení zásoby,
neaktivuje synchronizáciu ani autoritu skladu. Atomer prevzatie, párovanie,
riešenie konfliktov a následné zápisy sú samostatné kroky migračnej analýzy.
