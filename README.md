# Monitor javnih poziva – MSP u FBiH

Automatski prati stranice federalnih, kantonalnih i općinskih institucija i javlja
**samo kad se pojavi novi javni poziv**. Radi sam, bez servera i bez mjesečnih troškova.

## Kako radi

1. Za svaki izvor pokušava pronaći RSS feed (auto-discovery: `<link rel=alternate>`, pa
   `/feed/`, `/rss`, `?feed=rss2`). Ako ga nema — parsira HTML i vadi linkove.
2. Filtrira po ključnim riječima (*javni poziv, konkurs, poticaj, subvencija, kredit,
   grant, refundacija...*), uz negativni filter koji izbacuje šum (javne nabavke,
   odluke o izboru ponuđača, oglasi za prijem).
3. Poredi sa `seen.json` i šalje notifikaciju **samo za nove stavke**.
4. Generiše i `docs/index.html` — stranicu s pregledom (može se objaviti preko GitHub Pages).

Dijakritika se normalizuje, pa `natječaj` i `natjecaj` jednako prolaze.

## Postavljanje (10–15 min)

1. Napravi **privatni** GitHub repo i ubaci ove fajlove.
2. Uključi Actions: tab **Actions** → *I understand my workflows, go ahead and enable them*.
3. Podesi notifikacije u **Settings → Secrets and variables → Actions → New repository secret**.

### Telegram (najlakše, instant, besplatno)
- U Telegramu piši `@BotFather` → `/newbot` → dobiješ token.
- Pošalji svom botu bilo koju poruku, pa otvori:
  `https://api.telegram.org/bot<TOKEN>/getUpdates` → pronađi `"chat":{"id":...}`.
- Dodaj secrets: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.

### Email (opcionalno)
Dodaj: `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USER`, `SMTP_PASS`, `MAIL_TO`.
Za Gmail obavezno koristi *App Password*, ne običnu lozinku.

Dovoljno je podesiti **jedan** kanal — skripta preskače onaj koji nije konfigurisan.

4. Pokreni ručno prvi put: **Actions → Monitor javnih poziva → Run workflow**.
   Prvi prolaz samo puni bazu i **ne šalje** notifikacije (da te ne zaspe sa 100 starih poziva).
   Od drugog prolaza javlja samo novo.

## Podešavanje

- **Izvori**: `sources.yaml` — dodaj/ukloni institucije slobodno.
- **Učestalost**: `.github/workflows/monitor.yml`, polje `cron`.
  Dnevno `0 6 * * *` · sedmično `0 6 * * 1` · mjesečno `0 6 1 * *`.
- **Ključne riječi**: `KEYWORDS` i `NEGATIVE` u `monitor.py`.

## Stranica (opcionalno)

Settings → Pages → Source: *Deploy from a branch* → `main` / `/docs`.
Dobiješ javni URL s pregledom zadnjih poziva.

## Ograničenja — pročitati

- **Prvo pokretanje treba nadzor.** Izvori su testirani logički, ali ne i protiv svake
  žive stranice. Neka institucija može imati zaštitu, JS-generisan sadržaj ili
  netipičnu strukturu — tada taj izvor vraća 0 i treba mu doraditi pravilo.
- **Filter nije savršen.** Radije propušta previše nego premalo — bolje da pregledaš
  jedan suvišan link nego da propustiš poziv. Ako ima previše šuma, dopuni `NEGATIVE`.
- **Ovo je detekcija, ne procjena.** Skripta javlja *da se nešto pojavilo*. Da li klijent
  ispunjava uslove i da li se traži biznis plan — ostaje na tebi (i to je dio koji se naplaćuje).
- Neke općine objavljuju pozive samo u PDF-u ili na Facebooku — to ovaj monitor neće uhvatiti.
