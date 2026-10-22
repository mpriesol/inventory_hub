# Identita produktov a automatické SKU

Jedna konkrétna predajná jednotka má jednu kartu Hubu s interným `products.id`.
Toto ID a cudzie kľúče spájajú sklad, dodávateľské ponuky, e-shopy a doklady.
Pre nové karty systém automaticky zostaví SKU ako
`<registrovaná skratka značky>-<presný kód predajnej položky>`. Obsluha nemusí
vymýšľať jedinečné SKU ani ručne dopĺňať prefix. Napríklad rovnaký kód `00042`
pod značkami Topeak a Shimano vytvorí návrhy `TP-00042` a `SH-00042`.

Existujúca jednoznačne identifikovaná karta si vždy ponechá pôvodné SKU.
Pridanie dodávateľa, úprava názvu značky ani nové pravidlo neprečíslujú históriu.

## Kód konkrétnej položky

MPN nie je povinný. Použiť možno skutočný dodávateľský kód konkrétnej predajnej
položky v rozsahu daného dodávateľa, overený GTIN alebo výslovne zadaný kód
konkrétneho výrobku. Rodičovský kód spoločný viacerým variantom nie je skladovou
identitou. Názvy polí vo feede nie sú automatickým dôkazom overeného MPN.

Bežná príprava z katalógu alebo príjemky automaticky použije značku a dodávateľský
kód vybraného riadka. Pri chýbajúcej značke alebo kóde ich treba doplniť.
Neznáma značka potrebuje jednorazovú registráciu skratky alebo priradenie aliasu.
Register garantuje jedinečnosť dvojpísmenovej skratky; nepovoľuje dve značky pod
rovnakou skratkou. Kód zostáva textom vrátane úvodných núl a veľkosti písmen.

Systém pred uložením kontroluje existujúce SKU, overené EAN/UPC, dodávateľsky
obmedzené identifikátory a aktuálne databázové väzby. Rozporné dôkazy sú konflikt.
Rovnaký kód u dvoch dodávateľov sám osebe nepreukazuje rovnaký výrobok.
Prefix znižuje kolízie medzi značkami, ale nenahrádza kontrolu jedinečnosti.
Pri skutočnej kolízii sa karta nevytvorí a systém nepridá odhadnutú príponu ani
poradové číslo; treba opraviť značku, kód alebo výslovné prepojenie existujúcej karty.

## Bežný postup

1. Vyber položky z katalógu/príjemky alebo otvor **Nová skladová karta**.
2. Skontroluj značku a kód konkrétneho výrobku. Systém zobrazí pridelené SKU alebo
   existujúcu kartu. Nepovinný MPN ani text dôkazu nie sú podmienkou základnej karty.
3. Ulož kartu do Hubu. Nevyžaduje hotový e-shopový popis, predajnú cenu ani DPH
   a nevytvára zásobu.
4. Potvrď fyzický príjem alebo samostatné publikovanie do vybraných e-shopov.

Služba musí mať výslovné `item_type=service`; prefix ani kategória tento typ
nenahrádzajú. Môže používať bežnú značku alebo registrovaný priestor služby.
Priestor služby sa odmietne pri karte typu `stock`. Jednotky a prepočty balenia
sú súčasťou skladového modelu, nie pravidla číslovania.

## API a dôkazy

`POST /api/sku-policy/preview` prijíma `sku_identity` so značkou, presným
`source_code`, `code_kind` a pri dodávateľskom kóde aj `supplier_id`. Prijíma tiež
`item_type`, čiarové kódy a dodávateľský kontext. Vracia stav `new`, `existing`,
`blocked` alebo `conflict`, výsledné SKU a ID existujúcej karty. Samotné `sku`
bez `sku_identity` slúži na vyhľadanie existujúcej karty, nie na obídenie prideľovania.

`source_ref` je nepovinný pôvod údaja a `leaf_confirmed` je predvolene `false`.
Zadanie prevádzkového kódu preto nepredstiera overenie MPN. Existujúce výslovne
potvrdené dôkazy sa naďalej kontrolujú a uchovávajú; nový záznam v
`product_sku_identities` vznikne iba pri `leaf_confirmed=true` s neprázdnym
`source_ref`. Bežné väzby držia karta, overené identifikátory a cudzie kľúče.

Ručné založenie a import zopakujú kontrolu pod spoločným `IDENTITY_WRITE_LOCK`.
UUID chráni opakované uloženie po výpadku. Dodávateľský alias a skutočná ponuka
sa prepájajú atomicky cez `product_supply_sources`. Faktúrový alias bez ponuky
nevytvára vymyslený produkt feedu. Založenie karty nemení pohyby, FIFO ani e-shop.

Upgates používa jedinečný `code` pre produkty aj varianty; platí to pre POST aj
PUT. Hub preto overuje obsadenosť kódov vrátane rodičov samostatne v BIKETREK
aj xTrek a opakuje kontrolu pred odoslaním. [Oficiálna referencia](https://docs.upgates.com/api-reference/produkty).
Interné databázové ID túto požiadavku API nenahrádza.
