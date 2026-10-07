# Používateľské účty a prihlásenie

V **Nastavenia → Používateľské účty** alebo cez meno v spodnej časti navigácie sa používateľ prihlási menom a heslom. Prihlásenie odomkne AI obsah aj ostatné funkcie, ktoré používali spoločný operátorský token. Pretrvá obnovenie stránky, nový panel aj zatvorenie prehliadača. **Odhlásiť sa** zruší konkrétnu reláciu na serveri aj v prehliadači.

Pri prvom použití správca zvolí **Prvé prihlásenie / prístupový token** a zadá existujúci Hub token. Takto prihlásený správca môže vytvoriť vlastný pomenovaný účet s rolou **Správca** a heslom aspoň 12 znakov, prípadne účty **Obsluha** pre kolegov. Heslá sa nezobrazujú ani neposielajú späť klientovi. Pri nasadení sa aj úspešná autorizovaná požiadavka zo starého otvoreného panela automaticky zmení na trvalú reláciu; token netreba zadávať znovu. Staršie tokenové formuláre ostávajú kompatibilné.

Správca môže vytvárať účty a deaktivovať/aktivovať iné účty. Deaktivácia okamžite ruší všetky relácie účtu. Vlastný aktuálne prihlásený účet nemožno deaktivovať. Obsluha používa rovnaké chránené prevádzkové funkcie ako pôvodný token, bez správy účtov. Nejde o úplné rolové rozdelenie skladu, dodávateľov a čítacích stránok; existujúce verejné cesty sa touto zmenou neuzatvárajú. Zmena/zabudnuté heslo zatiaľ nemá samostatný samoobslužný postup; správca môže deaktivovať starý a vytvoriť nový účet.

## Relácie a ochrany

- Náhodný token relácie je iba v cookie `__Host-hub_session` s `Secure`, `HttpOnly`, `SameSite=Strict` a cestou `/`. JavaScript ani localStorage/sessionStorage ho neobsahujú. Nasadenie vyžaduje HTTPS.
- Server uchováva SHA-256 odtlačok relácie a heslá so samostatnou náhodnou soľou cez scrypt (`N=32768`, `r=8`, `p=3`). Relácia má platnosť 365 dní od obnovenia cez `/auth/session`; bežné používanie teda nevyžaduje opakované prihlásenie. Vymazanie cookies, dlhá neaktivita, deaktivácia alebo odhlásenie ju ukončí.
- Zápisy s cookie vyžadujú `X-Hub-Request: 1`, odmietajú cudzí `Origin` a `Sec-Fetch-Site: cross-site`. Autorizované serverové integrácie môžu naďalej používať pôvodný bearer token.
- Päť neúspešných pokusov na používateľské meno alebo 30 na pripojenie počas piatich minút obmedzí ďalšie prihlásenie; počítadlá pretrvajú reštart. Neodlišujú neznáme meno od chybného hesla. Za proxy sa klientsky limit môže zdieľať podľa sieťového nastavenia proxy.
- Rotácia `AI_CONTENT_ACCESS_TOKEN` zneplatní relácie vytvorené tokenom. Účty s heslom majú samostatné relácie. Tajomstvá nesmú ísť do Git, logov ani validačných chýb.

## API a nasadenie

Verejné cesty (s prefixom `/api` cez Caddy): `GET /auth/session`, `POST /auth/login`, `POST /auth/logout`. `POST /auth/token-session` vyžaduje pôvodný token. Správca používa `GET/POST /auth/users` a `PUT /auth/users/{id}` s poľom `active`. Odpovede autentizácie majú `Cache-Control: no-store`.

Aditívna, opakovateľná migrácia `020_user_sessions.sql` vytvára `hub_users`, `hub_sessions` a `hub_login_limits`, bez predvoleného mena či hesla. Je pribalená v API image a workflow spúšťa `python -m inventory_hub.user_sessions_migrate` po migrácii 019 pred reštartom API. Nové premenné prostredia netreba. Pri návrate aplikácie tabuľky ponechaj; stará verzia opäť používa operátorský token a nebude čítať nové relácie. Skladové pohyby, FIFO ani shop nastavenia migrácia nemení.

Automatické testy: `test_user_sessions.py`, `test_user_sessions_db.py` (izolovaný localhost PostgreSQL s povinným testovacím názvom) a `frontend/tests/accounts-ui.cjs`. Pokrývajú prihlásenie, obnovenie relácie, odhlásenie, zneplatnenie, roly, pôvod požiadavky, limity a neprítomnosť tajomstiev v odpovediach.
