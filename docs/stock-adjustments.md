# Ručná oprava fyzického množstva

Fyzický počet je celé číslo pre `pcs`, `pair` a `pack`; karta `m` prijíma najviac tri desatinné miesta. Služby nemožno inventarizovať. Typ/jednotka sú súčasťou kontroly náhľadu; existujúce nemenné pohyby sa neprepisujú. [Typy a jednotky](stock-units.md).

V jednotnej tabuľke Sklad klikni na fyzické množstvo alebo otvor detail produktu a záložku opravy. Vyber správny sklad, zadaj skutočne spočítané množstvo v jednotke karty, obsluhu, referenciu a dôvod. Najprv sa vytvorí náhľad; druhé potvrdenie zaúčtuje jeho rozdiel.

Pri produkte bez overeného fyzického stavu možno výslovne potvrdiť aj **0 kusov**. Zostane uložený doklad počítania a prázdny overený sklad; nevznikne fiktívny príjem, pohyb ani nákupná cena. Takto môže pravidelný prenos bezpečne prenášať dodávateľskú dostupnosť produktu, ktorý sme nikdy fyzicky nemali. Neznámy stav sa na nulu nemení automaticky.

Prírastok vytvorí nákupnú vrstvu. Cena je v EUR bez DPH; ak ju nepoznáš, zostáva neznáma, nie nulová. Úbytok spotrebuje najstaršie dostupné príjmy a zachová ich pôvodný náklad. Nejde o tržbu ani zákaznícku vratku. V histórii vznikne `ADJUSTMENT_IN` alebo `ADJUSTMENT_OUT`; predchádzajúce pohyby sa nemenia.

Spočítané množstvo nemôže klesnúť pod rezervované množstvo plus karanténu. Tieto záväzky vyrieš cez príslušnú objednávku alebo karanténu. Voľné množstvo, rezervácie z objednávok a celková nákupná hodnota sa neprepisujú nezávislými číslami v tabuľke. Nákupné ceny sa opravujú v časti Nákupné ceny. Staršie zásoby pred korekciou vyžadujú zdokumentovaný prechod nákupných vrstiev.

Ak sa sklad po náhľade zmení, zaúčtovanie sa odmietne a treba nový náhľad. Platnosť je 30 minút. Po strate odpovede použi **Obnoviť pôvodnú požiadavku**; prehliadač uchováva jej pôvodné UUID a obsah. Server vráti už uložený výsledok bez ďalšieho pohybu. Zatvorenie detailu nie je zrušenie už odoslanej operácie.

## Vývojársky kontrakt

- Chránené a `no-store` cesty: `POST /stock-adjustments/preview`, `GET /stock-adjustments/{uuid}`, `POST /stock-adjustments/{uuid}/apply`.
- Náhľad prijíma SKU, sklad, absolútny fyzický počet ako reťazec, čas počítania, dôvod/referenciu/obsluhu a klasifikovanú cenu prírastku. Záporné, desatinné a nečíselné počty sa odmietajú.
- Náhľad má transakčný zámok UUID ešte pred zámkami skladu/produktu. Rovnaké UUID s iným obsahom je konflikt. Zaúčtovanie používa existujúce zámky bilancií a kontrolu údržbového uzáveru, hash celého náhľadu aj aktuálnej skladovej snímky.
- Pohyb, nákupná vrstva alebo alokácia výdaja, bilancia a uložený výsledok sa zapisujú v jednej transakcii. Prvé potvrdenie nuly zapisuje iba audit a prázdny stav nákupných vrstiev; výsledok má `initial_zero_count:true`, `movement_id:null`, `delta:"0"`. Čítacie projekcie uznávajú iba dokončený audit a aktuálne nulové fyzické/rezervované/karanténne množstvá. Interné služby `fifo` ostávajú spoločnou implementáciou oceňovania.
- Migrácia `017_stock_adjustments.sql` iba pridáva tabuľku náhľadov/výsledkov a index. Nemení existujúce zásoby. Je zabalená v API obraze a spúšťa sa po `016` pred reštartom.
- Tento postup neodosiela požiadavky do Upgates. Samostatne aktivovaný pravidelný prenos zahrnie nový lokálny stav pri ďalšom prechode.

Pri návrate aplikácie sa tabuľka ani pohyby nemažú. Použi kompatibilný kód podporujúci nákupné vrstvy a údržbové uzávery; chybu oprav doprednou zmenou.

## Jednorazové prevzatie existujúcich zásob

Vlastník 9. 11. 2026 rozhodol prevziať existujúce vlastné zásoby ako prevádzkový
počiatočný stav a neskôr ich opravovať inventúrou. Výslovný vstup
`evidence_kind: "owner_adoption"` používa rovnaký náhľad, skladové zámky,
nemenné opravné pohyby a FIFO. Predvolený vstup zostáva `physical_count`.
Prevzatie vyžaduje zdrojovú referenciu, čas pozorovania v poli `counted_at`,
obsluhu a dôvod; nie je tvrdením, že kusy niekto fyzicky spočítal. Nový register
má `source_type: stock_adoption`, výsledok a nová FIFO vrstva nesú tento pôvod.
Neznáma cena zostáva `NULL`. Nula musí byť výslovne uvedená a potvrdená.

Tento vstup nevytvára trvalý osobitný skladový režim. Ďalšie príjmy a inventúry
pokračujú od prevzatého množstva. Už potvrdené položky sa korigujú rozdielom;
staré faktúry ani príjemky sa znovu neprehrávajú. Výber množstva a prípadný
konflikt zdrojov musí byť vysvetlený v podklade konkrétnej operácie. Samotný
import alebo migrácia SQL žiadne zásoby nepotvrdí.

Operátorské `POST /stock-adjustments/batch-preview` a `batch-apply` prijímajú
`entries` s najviac 100 bežnými požiadavkami. Každá má svoje pôvodné UUID,
náhľad a výsledok. Náhľad nemení sklad. Zaúčtovanie potvrdzuje položky postupne
v samostatných transakciách a pri známej chybe zastaví ďalšie položky; odpoveď
rozlíši `completed`, `blocked` a `not_attempted`. Dávka nie je jedna spoločná
transakcia. Pri strate odpovede treba cez `GET /stock-adjustments/{uuid}`
obnoviť výsledok každej pôvodnej položky. Nevytvárať náhradné UUID a nepredpokladať,
že celá dávka zlyhala. Opakovanie dokončeného UUID nepridá ďalšie kusy.
