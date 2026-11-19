# Návrh zberu dodávateľských faktúr z e-mailu

Stav je návrh. Prvým zdrojom by boli PDF faktúry Northfinder, ktoré už prichádzajú e-mailom.

1. Na existujúcej pošte vytvoriť samostatnú schránku pre faktúry Hubu. Pravidlo v pôvodnej schránke pošle kópiu správy, keď súhlasí overený odosielateľ aj typ správy s faktúrou. Pôvodný e-mail ostane v bežnej schránke. Na rovnakú adresu môže obsluha doposlať jednotlivý doklad ručne.
2. Hub každých päť minút načíta nové správy zo samostatnej schránky. Zvoliť konektor podľa poskytovateľa. Gmail API s čítacím prístupom k tejto schránke, Microsoft Graph s čítaním obmedzeným jej rozsahom alebo TLS IMAP v režime čítania, ak ho poskytovateľ podporuje. Použiť chránené serverové úložisko prihlasovania.
3. Zobrať PDF prílohy, overiť typ súboru a dodávateľa podľa pravidla správy aj dôkazu v doklade. Obsah e-mailu a názov prílohy používať len na orientáciu. Prílohu odovzdať spoločnej službe `invoice_upload` s pôvodnými bajtmi a uložiť identitu správy, prílohy a výsledného dokladu.
4. Cursor v schránke uložiť až po trvalom zaznamenaní výsledku každej prílohy. Opakované doručenie s rovnakými bajtmi má ten istý výsledok. Pri tom istom dodávateľovi a čísle dokladu s odlišnými faktúrovými údajmi poslať prípad na kontrolu. Parser z názvu prílohy nikdy nevytvorí faktúrové údaje.
5. Úspešný doklad sa zobrazí vo Faktúrach. Nepodporovaný alebo rozporuplný doklad ponechá originál a dôvod na kontrolu. Výpadok schránky sa rieši opakovaným čítaním od posledného uloženého výsledku. Existujúci blok **Vyžaduje pozornosť** môže upozorniť na zlyhanie čítania alebo spracovania. Sklad sa účtuje až pri potvrdení príjmu obsluhou.

Pilot pre Northfinder má overiť jednu novú správu s PDF, rovnakú správu doručenú znova a chybnú prílohu. Treba potvrdiť dostupnosť originálu, stav spracovania a príjem za cenu po zľave z tovarového riadka. Overený odosielateľ a reálne pravidlo pošty sa nastavia podľa existujúcej schránky.

Oficiálna dokumentácia: [filtre Gmailu](https://support.google.com/mail/answer/6579), [Gmail API pre prílohy](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages.attachments/get) a [obmedzenie schránok pre aplikáciu v Exchange Online](https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac).
