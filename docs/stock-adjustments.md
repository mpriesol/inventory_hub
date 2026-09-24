# Ručná oprava fyzického množstva

V jednotnej tabuľke Sklad klikni na fyzické množstvo alebo otvor detail produktu a záložku opravy. Vyber správny sklad, zadaj skutočne spočítané celé kusy, obsluhu, referenciu a dôvod. Najprv sa vytvorí náhľad; druhé potvrdenie zaúčtuje jeho rozdiel.

Pri produkte bez overeného fyzického stavu možno výslovne potvrdiť aj **0 kusov**. Zostane uložený doklad počítania a prázdny overený sklad; nevznikne fiktívny príjem, pohyb ani nákupná cena. Takto môže pravidelný prenos bezpečne prenášať dodávateľskú dostupnosť produktu, ktorý sme nikdy fyzicky nemali. Neznámy stav sa na nulu nemení automaticky.

Prírastok vytvorí nákupnú vrstvu. Cena je v EUR bez DPH; ak ju nepoznáš, zostáva neznáma, nie nulová. Úbytok spotrebuje najstaršie dostupné príjmy a zachová ich pôvodný náklad. Nejde o tržbu ani zákaznícku vratku. V histórii vznikne `ADJUSTMENT_IN` alebo `ADJUSTMENT_OUT`; predchádzajúce pohyby sa nemenia.

Spočítané množstvo nemôže klesnúť pod rezervované kusy plus karanténu. Tieto záväzky vyrieš cez príslušnú objednávku alebo karanténu. Voľné množstvo, rezervácie z objednávok a celková nákupná hodnota sa neprepisujú nezávislými číslami v tabuľke. Nákupné ceny sa opravujú v časti Nákupné ceny. Staršie zásoby pred korekciou vyžadujú zdokumentovaný prechod nákupných vrstiev.

Ak sa sklad po náhľade zmení, zaúčtovanie sa odmietne a treba nový náhľad. Platnosť je 30 minút. Po strate odpovede použi **Obnoviť pôvodnú požiadavku**; prehliadač uchováva jej pôvodné UUID a obsah. Server vráti už uložený výsledok bez ďalšieho pohybu. Zatvorenie detailu nie je zrušenie už odoslanej operácie.

## Vývojársky kontrakt

- Chránené a `no-store` cesty: `POST /stock-adjustments/preview`, `GET /stock-adjustments/{uuid}`, `POST /stock-adjustments/{uuid}/apply`.
- Náhľad prijíma SKU, sklad, absolútny fyzický počet ako reťazec, čas počítania, dôvod/referenciu/obsluhu a klasifikovanú cenu prírastku. Záporné, desatinné a nečíselné počty sa odmietajú.
- Náhľad má transakčný zámok UUID ešte pred zámkami skladu/produktu. Rovnaké UUID s iným obsahom je konflikt. Zaúčtovanie používa existujúce zámky bilancií a kontrolu údržbového uzáveru, hash celého náhľadu aj aktuálnej skladovej snímky.
- Pohyb, nákupná vrstva alebo alokácia výdaja, bilancia a uložený výsledok sa zapisujú v jednej transakcii. Prvé potvrdenie nuly zapisuje iba audit a prázdny stav nákupných vrstiev; výsledok má `initial_zero_count:true`, `movement_id:null`, `delta:"0"`. Čítacie projekcie uznávajú iba dokončený audit a aktuálne nulové fyzické/rezervované/karanténne množstvá. Interné služby `fifo` ostávajú spoločnou implementáciou oceňovania.
- Migrácia `017_stock_adjustments.sql` iba pridáva tabuľku náhľadov/výsledkov a index. Nemení existujúce zásoby. Je zabalená v API obraze a spúšťa sa po `016` pred reštartom.
- Tento postup neodosiela požiadavky do Upgates. Samostatne aktivovaný pravidelný prenos zahrnie nový lokálny stav pri ďalšom prechode.

Pri návrate aplikácie sa tabuľka ani pohyby nemažú. Použi kompatibilný kód podporujúci nákupné vrstvy a údržbové uzávery; chybu oprav doprednou zmenou.
