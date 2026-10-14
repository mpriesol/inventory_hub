# Používateľské účty a prihlásenie

V **Nastavenia → Používateľské účty** alebo cez meno v spodnej časti navigácie sa používateľ prihlási menom a heslom. Prihlásenie odomkne AI obsah aj ostatné funkcie, ktoré používali spoločný operátorský token. Pretrvá obnovenie stránky, nový panel aj zatvorenie prehliadača. **Odhlásiť sa** zruší konkrétnu reláciu na serveri aj v prehliadači.

Pri prvom použití správca zvolí **Prvé prihlásenie / prístupový token** a zadá existujúci Hub token. Takto prihlásený správca môže vytvoriť vlastný pomenovaný účet s rolou **Správca** a heslom aspoň 12 znakov, prípadne účty **Obsluha** pre kolegov. Heslá sa nezobrazujú ani neposielajú späť klientovi. Pri nasadení sa aj úspešná autorizovaná požiadavka zo starého otvoreného panela automaticky zmení na trvalú reláciu; token netreba zadávať znovu. Staršie tokenové formuláre ostávajú kompatibilné.

Správca môže vytvárať účty a deaktivovať/aktivovať iné účty. Deaktivácia okamžite ruší všetky relácie účtu. Vlastný aktuálne prihlásený účet nemožno deaktivovať. Obsluha používa rovnaké chránené prevádzkové funkcie ako pôvodný token, bez správy účtov. Všetky prevádzkové API, konfigurácie, dokumenty a logy vyžadujú prihlásenie. Správca navyše spravuje integračné konfigurácie, ciele a prihlasovacie údaje; obsluha môže vykonávať bežnú skladovú prácu. Nejde o všeobecné rolové rozdelenie jednotlivých skladových operácií. Zmena/zabudnuté heslo zatiaľ nemá samostatný samoobslužný postup; správca môže deaktivovať starý a vytvoriť nový účet.

## Relácie a ochrany

- Náhodný token relácie je iba v cookie `__Host-hub_session` s `Secure`, `HttpOnly`, `SameSite=Strict` a cestou `/`. JavaScript ani localStorage/sessionStorage ho neobsahujú. Nasadenie vyžaduje HTTPS.
- Server uchováva SHA-256 odtlačok relácie a heslá so samostatnou náhodnou soľou cez scrypt (`N=32768`, `r=8`, `p=3`). Relácia má platnosť 365 dní od obnovenia cez `/auth/session`; bežné používanie teda nevyžaduje opakované prihlásenie. Vymazanie cookies, dlhá neaktivita, deaktivácia alebo odhlásenie ju ukončí.
- Zápisy s cookie vyžadujú `X-Hub-Request: 1`, odmietajú cudzí `Origin` a `Sec-Fetch-Site: cross-site`. Autorizované serverové integrácie môžu naďalej používať pôvodný bearer token.
- Päť neúspešných pokusov na používateľské meno alebo 30 na pripojenie počas piatich minút obmedzí ďalšie prihlásenie; počítadlá pretrvajú reštart. Neodlišujú neznáme meno od chybného hesla. Za proxy sa klientsky limit môže zdieľať podľa sieťového nastavenia proxy.
- Rotácia `AI_CONTENT_ACCESS_TOKEN` zneplatní relácie vytvorené tokenom. Účty s heslom majú samostatné relácie. Tajomstvá nesmú ísť do Git, logov ani validačných chýb.

## API a nasadenie

Jediné verejné cesty (s prefixom `/api` cez Caddy): `GET/HEAD /health`, `GET /auth/session`, `POST /auth/login`, `POST /auth/logout` (iba uvedené metódy a presné cesty). `POST /auth/token-session` vyžaduje pôvodný token. Správca používa `GET/POST /auth/users` a `PUT /auth/users/{id}` s poľom `active`. Odpovede autentizácie majú `Cache-Control: no-store`.

Aditívna, opakovateľná migrácia `020_user_sessions.sql` vytvára `hub_users`, `hub_sessions` a `hub_login_limits`, bez predvoleného mena či hesla. Je pribalená v API image a workflow spúšťa `python -m inventory_hub.user_sessions_migrate` po migrácii 019 pred reštartom API. Nové premenné prostredia netreba. Pri návrate aplikácie tabuľky ponechaj; stará verzia opäť používa operátorský token a nebude čítať nové relácie. Skladové pohyby, FIFO ani shop nastavenia migrácia nemení.

Automatické testy: `test_user_sessions.py`, `test_user_sessions_db.py` (izolovaný localhost PostgreSQL s povinným testovacím názvom) a `frontend/tests/accounts-ui.cjs`. Pokrývajú prihlásenie, obnovenie relácie, odhlásenie, zneplatnenie, roly, pôvod požiadavky, limity a neprítomnosť tajomstiev v odpovediach.


Kontrakt skrytých prihlasovacích údajov a ich výmeny: [configuration-secrets.md](configuration-secrets.md).

## Uzavretie súkromného API (14. október 2026)

Globálna hranica autentizácie chráni aj staršie routre bez vlastnej dependency, `/docs`, `/openapi.json`, `/files/*` a `/data/*`. Je zapnutá aj pri legacy úložisku; tam sa používa existujúci bearer prístup. Každý cookie zápis kontroluje rovnaký pôvod a `X-Hub-Request: 1`. CORS preflight spracuje CORS middleware bez vykonania prevádzkovej operácie. Chyby validácie nevracajú vstupné hodnoty ani kontext s heslami. Odpovede sú `no-store` a `nosniff`.

Frontend pred načítaním súkromných stránok obnoví reláciu alebo zobrazí prihlásenie. Spoločný transport pridáva potrebné hlavičky iba k vlastnému API; na iné domény neposiela Hub token. Sťahovanie používa cookie, nikdy token v URL. Starý otvorený panel treba po tomto nasadení raz obnoviť. Prihlásenie sa tým nestratí. Klientske logy ukladajú iba cestu bez query parametrov; aj staré uložené záznamy sa očistia.

Prihlásenie samo nedovoľuje stiahnuť ľubovoľný súbor. Povolené sú konkrétne oblasti faktúr, feedov, importov a logov; konfigurácie, histórie konfigurácií, browser storage, cookies, skryté cesty, traversal a symlinky sú odmietnuté. HTML/XML/JSON sa nesmú vykonávať v pôvode Hubu; odpovede používajú text/prílohu a sandbox CSP. Lokálne feedy musia ležať v povolenej oblasti `suppliers/<code>/feeds/`. Staršie obyčajné diagnostické logy môžu obsahovať citlivé údaje; táto zmena spätne neprepisuje ich obsah.

Deployment spúšťa `python -m inventory_hub.security_check`: čítaním verejného HTTPS overí zdravú DB, odmietnutie anonymného skladu/konfigurácie/súboru, úspešný autentizovaný stav AI a odmietnutie raw konfiguračného súboru aj po prihlásení. Kľúč načíta iba vo vnútri kontajnera, nezapisuje ho do logu ani argumentov. Kontrola nevykonáva prevádzkové zápisy a nenasleduje presmerovania. `/health` vracia pri nezdravej DB HTTP 503 bez detailov pripojenia.

Nevzniká migrácia ani aktivácia skladovej synchronizácie. Bezpečnostnú opravu nevracať na predchádzajúcu verejne dostupnú verziu; pri probléme použiť doprednú opravu. Po uzavretí verejného prístupu vymeniť potenciálne sprístupnené integračné kľúče/heslá u poskytovateľov a bezpečne aktualizovať uložené hodnoty. Záloha/obnova a dôkaz absencie historického zneužitia sú samostatné prevádzkové úlohy, nie výsledok testov tejto opravy.
