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

V rovnakom prehľade **Obchody** je aj tlačidlo **Stiahnuť z Upgates**. Otvára
existujúci produktový importer: najprv načíta náhľad, oddelí konflikty identity
a umožní vybrať skupiny na import do Hubu. Až potvrdenie importu vytvorí karty
alebo doplní overené väzby. Zachová existujúce kódy e-shopu a nemení fyzický sklad.
Voľba obnovy existujúcich údajov je predvolene vypnutá. Tento importer nespracúva
Atomer XML a nepovažuje pokladničné varianty za samostatne potvrdené zásoby.

## Rozšírené API podklady

`GET /api/migration/upgates-export?shop_code=xtrek&dataset=parameters`
stiahne samostatný ZIP jednej úplnej sekcie cez existujúce Upgates napojenie.
Vyžaduje rovnaké operátorské prihlásenie. Nevyžaduje funkčnú verejnú doménu
exportov. Pôvodný `catalog-export` ostáva kompatibilný, jeho základný zoznam
produktov však sám nepokrýva parametre, súbory ani súvisiace produkty.

Povolené `dataset`: `products`, `parameters`, `labels`, `files`, `related`,
`variants`, `categories`, `parameter_registry`, `availabilities`, `redirections`,
`config`. Produktové sekcie čítajú príslušné `products/...` API; varianty majú
vlastnú kolekciu a natívne ID. Číselník parametrov obsahuje aj hodnoty.
Dostupnosti sa čítajú výslovne pre všetky štyri typy, pretože API dokumentuje
predvolený filter `InStock`. Konfigurácia obsahuje len štyri dokumentované polia
DPH a limitu objednávania; nejde o export prístupov ani úplného nastavenia.

Každá stránka má v manifeste čas, počet a SHA-256. Desatinné čísla sa uchovajú
ako presné texty. Kontroluje sa číslo stránky, počet riadkov, stabilný celkový
počet a jedinečnosť natívnych ID. Chybná alebo neúplná odpoveď nedostane úspešný
ZIP. Limity sú 500 strán na sekciu, 16 MB odpoveď a 250 MB serializovaných strán.
Pri zamietnutí API rozpočtom alebo HTTP chybe sa volanie automaticky neopakuje.
Nadväzujúca analýza musí porovnať čas aj množinu ID medzi samostatnými sekciami;
export nepredstiera spoločný freeze a pri zmene katalógu treba vytvoriť nový beh.

Volania používajú spoločný API rozpočet a uložené prístupy. Nezapisujú katalóg,
sklad, rezervácie, FIFO ani nastavenia; bežná evidencia API rozpočtu zostáva
aktívna. Nevyžadujú databázovú migráciu. Nasadenie používa štandardný build;
rollback kódu odstráni len nový čítací endpoint, nezvracia žiadne obchodné dáta.

Referencie: [produktové API sekcie](https://docs.upgates.com/api-reference/produkty-seznamy),
[kategórie](https://docs.upgates.com/api-reference/kategorie),
[parametre](https://docs.upgates.com/api-reference/parametry),
[dostupnosti](https://docs.upgates.com/api-reference/dostupnosti).

Ak API vráti tú istú dostupnosť pri viacerých filtroch typu, totožné záznamy
zostávajú v pôvodných stránkach. Manifest rozlišuje počet pozorovaných riadkov
`count` a jedinečných identít `unique_count`; analýza ich nesmie sčítať.
Duplicitné ID v jednej filtrovanej kolekcii alebo zmenený obsah rovnakého ID
medzi filtrami export stále zastaví. Takto sa zachová dôkaz aj pri API, ktoré
vracia prekrývajúce sa výsledky filtrov.
