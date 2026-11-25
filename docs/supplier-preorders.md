# Predobjednávky u dodávateľov

Po potvrdení objednávky dodávateľovi otvorte **Predobjednávky** v hlavnej navigácii. Zvoľte dodávateľa, jeho číslo objednávky, dátum a položky. Každá položka má množstvo v jednotke skladovej karty a vlastný očakávaný termín. Kartu vyhľadajte podľa jej presného SKU. Novú kartu možno založiť cez rovnaké overenie značky, výrobného kódu a voliteľného EAN ako v sklade. Novo založená karta je neaktívna.

Predobjednávka sa uloží po označení schválenia. Úspešné uloženie sa pri opakovaní toho istého UUID obnoví a súbežné požiadavky nemôžu zapísať rovnaké číslo pre toho istého dodávateľa dvakrát. Pridanie objednávky ani prepojenie faktúry nevytvára pohyb skladu a neodosiela objednávku do B2B.

V detaile objednávky vyhľadajte a pripojte jednotlivé faktúry. Faktúra je priradená jednej predobjednávke, aby sa jej dodávka nezapočítala dvakrát. Pri každej položke sa ukážu potvrdené skladové pohyby z pripojených príjemiek, dátum zaúčtovania, prijaté množstvo a zostávajúci počet. Evidencia obsahuje aj vybavené objednávky. Otvorené objednávky sú viditeľné na skladovej karte a po dohľadaní produktu. Očakávaný termín možno upraviť po jednotlivých položkách.

Migrácia 061 pridáva samostatné tabuľky a odkazy. Fyzické príjmy, zásoby, nákupné ceny, FIFO a existujúce predajné kanály využívajú svoj bežný postup.

Po zrušení a opätovnom potvrdení príjmu sa v dodanom množstve započíta nový príjem. Kompenzovaný pohyb zostáva v skladovej histórii.
