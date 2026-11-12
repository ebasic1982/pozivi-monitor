#!/usr/bin/env python3
"""
Monitor javnih poziva za MSP u FBiH  (verzija 3.3.2)

Radi u tri koraka:
  1) Za svaki izvor trazi RSS feed (prvo feed kategorije, pa opsti), a ako ga nema
     ili nema relevantnih stavki - cita HTML stranicu.
  2) Filtrira linkove po kljucnim rijecima (javni poziv, konkurs, poticaj, subvencija...).
  3) Uporedjuje sa seen.json i salje obavjestenje SAMO za nove stavke.

Novi ili izmijenjeni izvor se prvi put upisuje "tiho" (bez obavjestenja),
pa dodavanje izvora u sources.yaml ne izaziva poplavu starih poziva.

Pracenje ispravnosti izvora (od verzije 3.2):
  - NEDOSTUPAN: stranica vraca gresku DOWN_DAYS dana zaredom -> upozorenje
  - BEZ STAVKI: stranica radi, ali nijedan poziv nije prepoznat SILENT_DAYS dana
    zaredom (najcesce redizajn sajta) -> upozorenje
  - Kad izvor ponovo proradi -> obavjestenje o oporavku
  - Svakog ponedjeljka: sedmicni izvjestaj o stanju svih izvora

Obavjestenja: email (SMTP) i/ili Telegram. Podesavanje preko GitHub secreta.
Probno slanje: pokretanje workflowa sa ukljucenom opcijom "test_mail".
"""

import os
import re
import sys
import json
import time
import html
import smtplib
import warnings
import unicodedata
import datetime as dt
from email.message import EmailMessage
from urllib.parse import urljoin, urlparse

import requests
import feedparser
import urllib3
import yaml
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ROOT = os.path.dirname(os.path.abspath(__file__))
SEEN_PATH = os.path.join(ROOT, "seen.json")
SOURCES_PATH = os.path.join(ROOT, "sources.yaml")
PAGE_PATH = os.path.join(ROOT, "docs", "index.html")
SRC_KEY = "__sources__"   # spisak izvora koji su vec inicijalizovani
HEALTH_KEY = "__health__" # stanje ispravnosti po izvoru
META_KEY = "__meta__"     # datum zadnjeg sedmicnog izvjestaja i sl.

DOWN_DAYS = 3     # upozorenje kad je izvor nedostupan ovoliko dana zaredom
SILENT_DAYS = 7   # upozorenje kad izvor radi, ali bez ijednog poziva, ovoliko dana
REPORT_WEEKDAY = 1  # ponedjeljak (ISO: 1 = ponedjeljak ... 7 = nedjelja)

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Sarajevo")
except Exception:          # ako vremenska zona nije dostupna, koristi se UTC
    TZ = None

LAST_ERR = {"msg": ""}     # razlog zadnje greske pri pristupu (za upozorenja)


def now_local():
    return dt.datetime.now(TZ) if TZ else dt.datetime.utcnow()

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "bs,hr;q=0.9,sr;q=0.8,en;q=0.7",
}
TIMEOUT = 25

# Kljucne rijeci (bez dijakritike - tekst se normalizuje prije poredjenja)
KEYWORDS = [
    "javni poziv", "javni konkurs", "javni natjecaj", "javnog poziva",
    "poziv za dostav", "konkurs za", "natjecaj za",
    "poziv za projekt", "poziv za podnosenj", "poziv za prijav", "poziv na dostav",
    "poticaj", "podsticaj", "subvencij", "sufinansiranj", "sufinanciranj",
    "bespovratn", "grant", "kreditn", "refundacij", "potpore", "potpora",
    # EU i donatorski pozivi na engleskom
    "call for proposal", "call for application", "call for expression",
    "public call", "funding opportunit", "challenge fund",
]

# Ako se pojavi bilo koja od ovih - preskoci (rezultati, nabavke, oglasi za posao)
NEGATIVE = [
    "javna nabav", "javne nabav", "nabavka", "tender za",
    "odluka o izboru", "odluka o dodjeli", "odluka o odobravanju",
    "rang lista", "rang-lista", "rang listu", "rang-listu",
    "preliminarna lista", "lista korisnika", "lista odobrenih",
    "rezultati", "obavijest o dodjeli ugovora",
    "prodaja stalnih sredstava", "oglas za prijem", "konkurs za prijem",
    "javni oglas za popunu", "za prijem", "zakup prostora", "osposobljavanj",
    "odluka o raspodjeli", "odluke o raspodjeli", "odluka o konacnoj",
    "konacna lista", "konacnu listu", "call for tender", "procurement",
    # pozivi gradjanima (cesti na stranicama opcina, nisu za firme)
    "stipendi", "u zakup", "volonter", "priznanja", "ogrjev", "liga osnovnih",
    "dodjelu stan", "zamjenskih stan", "lijecenj",
]


def log(msg=""):
    print(msg, flush=True)


def strip_dia(s):
    s = s.replace("đ", "d").replace("Đ", "D")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def norm(s):
    return re.sub(r"\s+", " ", strip_dia(html.unescape(s or "")).lower()).strip()


def matches(text):
    t = norm(text)
    if len(t) < 12:
        return False
    if any(n in t for n in NEGATIVE):
        return False
    return any(k in t for k in KEYWORDS)


def get(url, quiet=False):
    """GET sa browserskim zaglavljima. Ako SSL certifikat ne valja, pokusa bez provjere
    (citamo samo javne stranice, ne saljemo nikakve podatke)."""
    for verify in (True, False):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, verify=verify)
        except requests.exceptions.SSLError:
            if verify:
                if not quiet:
                    log("     ! SSL certifikat neispravan - pokusavam bez provjere")
                continue
            if not quiet:
                LAST_ERR["msg"] = "SSL greska"
            return None
        except Exception as e:
            if not quiet:
                LAST_ERR["msg"] = f"greska pri pristupu ({type(e).__name__})"
                log(f"     ! greska pri pristupu: {type(e).__name__}")
            return None
        if r.status_code == 200:
            return r
        if not quiet:
            LAST_ERR["msg"] = f"HTTP {r.status_code}"
            log(f"     ! server vratio HTTP {r.status_code}")
        return None
    return None


def discover_feed(url, soup):
    """Redoslijed: sam URL -> feed kategorije (/feed/ na putanji) -> <link rel=alternate>
    -> opsti feedovi sajta."""
    p = urlparse(url)
    root = f"{p.scheme}://{p.netloc}"
    cands = [url]
    if not p.query:
        base = url if url.endswith("/") else url + "/"
        cands.append(base + "feed/")
    if soup:
        for ln in soup.find_all("link", rel=lambda v: v and "alternate" in v):
            typ = ln.get("type") or ""
            if ("rss" in typ or "atom" in typ) and ln.get("href"):
                cands.append(urljoin(url, ln["href"]))
    cands += [root + "/feed/", root + "/rss", root + "/?feed=rss2"]

    for c in dict.fromkeys(cands):
        r = get(c, quiet=True)
        if not r:
            continue
        fp = feedparser.parse(r.content)
        if not fp.bozo and fp.entries:
            return c, fp
    return None, None


def item(title, link, src, via):
    return {"title": title[:300], "link": link, "source": src["name"],
            "tier": src.get("tier", ""), "src_url": src["url"], "via": via}


def from_feed(fp, src):
    out = []
    for e in fp.entries[:60]:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if title and link and matches(title):
            out.append(item(title, link, src, "rss"))
    return out


def context_title(a):
    """Za stranice gdje naslov poziva nije u samom linku (npr. link je samo 'Detalji'
    ili putanja): trazi naslov u najblizem redu/bloku oko linka."""
    node = a
    for _ in range(5):
        node = node.parent
        if node is None or node.name in ("body", "html"):
            break
        if len(node.get_text(" ", strip=True)) > 700:
            break
        best, best_path = "", ""
        for s in node.stripped_strings:
            if not matches(s):
                continue
            if looks_like_path(s):          # putanja fajla nije naslov - samo rezerva
                if len(s) > len(best_path):
                    best_path = s
            elif len(s) > len(best):
                best = s
        if best:
            return best
        if best_path:
            return slug_title(best_path)
    return None


def looks_like_path(t):
    """Tekst linka koji je zapravo putanja/adresa fajla, a ne naslov."""
    t = t.strip()
    if t.startswith(("/", "http://", "https://", "www.")):
        return True
    return " " not in t and len(t) > 30 and ("-" in t or "/" in t or "_" in t)


def slug_title(t):
    """'/documents/download/95165/javni-poziv-za-dodjelu...' -> 'Javni poziv za dodjelu...'"""
    parts = [p for p in re.split(r"[/?#]", t) if p]
    s = parts[-1] if parts else t
    s = re.sub(r"\.(pdf|docx?|html?|php)$", "", s, flags=re.I)
    s = re.sub(r"^\d+[-_]+", "", s)
    s = re.sub(r"[-_]+", " ", s).strip()
    return s[:1].upper() + s[1:] if s else t


def link_text(a):
    """Naslov linka:
    - ako je cijela kartica jedan link (slika + datum + naslov + sazetak), uzima se
      samo naslov iz zaglavlja (h1-h6) unutar kartice
    - ako link nema teksta (npr. samo slika), uzima se title ili alt slike"""
    h = a.find(["h1", "h2", "h3", "h4", "h5", "h6"])
    if h:
        t = h.get_text(" ", strip=True)
        if len(t) >= 5:
            return t
    t = a.get_text(" ", strip=True)
    if t:
        return t
    if a.get("title"):
        return a["title"].strip()
    img = a.find("img", alt=True)
    return img["alt"].strip() if img else ""


def from_html(url, soup, src):
    """Vadi pozive iz HTML stranice.
    - standardno: link ciji tekst sadrzi kljucnu rijec
    - mode: context  -> naslov se trazi i u redu/bloku oko linka
    - link_obrazac   -> prihvata svaki link cija adresa odgovara obrascu (za agregatore
                        ciji naslovi ne sadrze nase kljucne rijeci, npr. na engleskom)
    Isti poziv sa vise linkova (poziv, obrazac, pravilnik) javlja se samo jednom."""
    out, seen_links, seen_titles = [], set(), set()
    context_mode = src.get("mode") == "context"
    rx = re.compile(src["link_obrazac"]) if src.get("link_obrazac") else None
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        link = urljoin(url, href)
        text = link_text(a)
        if text and looks_like_path(text):
            text = (context_title(a) if context_mode else None) or slug_title(text)

        if rx:
            if not rx.search(link) or len(text) < 5 or any(n in norm(text) for n in NEGATIVE):
                continue
        elif not matches(text):
            if not context_mode:
                continue
            text = context_title(a)
            if not text:
                continue

        nt = norm(text)
        if link in seen_links or nt in seen_titles:
            continue
        seen_links.add(link)
        seen_titles.add(nt)
        out.append(item(text, link, src, "html"))
    return out[:40]


def scan(src):
    """Vraca (stavke, dostupan, razlog). Izvor je 'dostupan' ako se stranica ili feed
    ucitao; 'razlog' opisuje gresku kad nije."""
    url = src["url"]
    log(f"  -> {src['name']}")
    LAST_ERR["msg"] = ""
    r = get(url)
    soup = BeautifulSoup(r.content, "html.parser") if r else None

    # Izvori sa obrascem linka (agregatori) citaju se iskljucivo iz HTML-a
    use_feed = not src.get("link_obrazac") and src.get("rss", True) is not False
    feed_url, fp = discover_feed(url, soup) if use_feed else (None, None)
    if fp:
        items = from_feed(fp, src)
        log(f"     RSS: {feed_url} ({len(items)} relevantnih)")
        if items:
            return items, True, ""
    if soup:
        items = from_html(url, soup, src)
        log(f"     HTML ({len(items)} relevantnih)")
        return items, True, ""
    if fp:
        return [], True, ""
    log("     preskoceno (nedostupno)")
    return [], False, LAST_ERR["msg"] or "nepoznata greska"


def key(it):
    return it["link"].split("#")[0].rstrip("/")


def load_seen():
    """Ucitava bazu. Stari format (bez spiska izvora) se odbacuje, pa se svi izvori
    ponovo inicijalizuju tiho - bez obavjestenja."""
    if os.path.exists(SEEN_PATH):
        try:
            with open(SEEN_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and SRC_KEY in data:
                return data
            log("Baza je u starom formatu - izvori ce se ponovo upisati bez obavjestenja.")
        except Exception:
            log("Baza nije citljiva - pravim novu.")
    return {SRC_KEY: []}


# ---------------- obavjestenja ----------------

SMTP_VARS = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "MAIL_TO"]


def _smtp_attempt(host, port, user, pwd, msg):
    """Jedan pokusaj slanja. Vraca (uspjeh, faza_greske, opis)."""
    stage = "povezivanje"
    try:
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT)
        else:
            s = smtplib.SMTP(host, port, timeout=TIMEOUT)
            stage = "TLS"
            s.starttls()
        try:
            stage = "prijava"
            s.login(user, pwd)
            stage = "slanje"
            s.send_message(msg)
        finally:
            try:
                s.quit()
            except Exception:
                pass
        return True, None, None
    except smtplib.SMTPAuthenticationError as e:
        return False, "auth", f"kod {e.smtp_code}"
    except Exception as e:
        return False, stage, f"{type(e).__name__}: {e}"


def send_email(subject, body_html):
    missing = [v for v in SMTP_VARS if not (os.getenv(v) or "").strip()]
    if missing:
        if len(missing) < len(SMTP_VARS):
            log(f"  Email: nedostaju secreti: {', '.join(missing)}")
        return False

    host = os.getenv("SMTP_HOST").strip()
    user = os.getenv("SMTP_USER").strip()
    to = os.getenv("MAIL_TO").strip()
    pwd = re.sub(r"\s+", "", os.getenv("SMTP_PASS"))
    try:
        port = int(os.getenv("SMTP_PORT").strip())
    except ValueError:
        log("  Email: SMTP_PORT nije broj - koristim standardni STARTTLS port")
        port = 587

    # Provjere bez otkrivanja vrijednosti (GitHub ionako maskira secrete u logu)
    log("  Email provjera: "
        f"host je Gmail server: {'DA' if host.lower() == 'smtp.gmail.com' else 'NE'} | "
        f"port standardan: {'DA' if port in (587, 465) else 'NE'} | "
        f"SMTP_USER ima @: {'DA' if '@' in user else 'NE'} | "
        f"SMTP_PASS ima 16 znakova: {'DA' if len(pwd) == 16 else 'NE'}")

    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = user
    m["To"] = to
    m.set_content("Novi javni pozivi - pogledaj HTML verziju poruke.")
    m.add_alternative(body_html, subtype="html")

    ports = [port] + [p for p in (587, 465) if p != port]
    for p in ports:
        mode = "SSL" if p == 465 else "STARTTLS"
        ok, stage, err = _smtp_attempt(host, p, user, pwd, m)
        if ok:
            log(f"  Email: POSLANO (nacin: {mode})")
            return True
        if stage == "auth":
            log(f"  Email: PRIJAVA ODBIJENA ({err}) - provjeri SMTP_USER i App Password (SMTP_PASS)")
            return False
        log(f"  Email: nije uspjelo preko {mode}, faza '{stage}' - {err}")
    log("  Email: NIJE POSLANO ni na jedan nacin.")
    return False


def send_telegram(text):
    tok, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            json={"chat_id": chat, "text": text[:4000],
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=TIMEOUT)
        log("  Telegram: " + ("POSLANO" if r.status_code == 200 else f"greska {r.status_code}"))
        return r.status_code == 200
    except Exception as e:
        log(f"  Telegram: greska - {e}")
        return False


def render(items, when, heading):
    by = {}
    for it in items:
        by.setdefault(it["tier"] or "Ostalo", []).append(it)
    plain = [f"<b>{html.escape(heading)}</b> — {when}\n"]
    h = [f"<h2 style='color:#1F4E79'>{html.escape(heading)}</h2><p><i>{when}</i></p>"]
    for tier, lst in by.items():
        plain.append(f"\n<b>{html.escape(tier)}</b>")
        h.append(f"<h3 style='color:#2E75B6;margin-bottom:4px'>{html.escape(tier)}</h3><ul>")
        for it in lst:
            plain.append(f"• <a href=\"{html.escape(it['link'])}\">{html.escape(it['title'][:120])}</a>"
                         f"\n  <i>{html.escape(it['source'])}</i>")
            h.append(f"<li style='margin:6px 0'><a href=\"{html.escape(it['link'])}\">"
                     f"{html.escape(it['title'])}</a><br><small style='color:#777'>"
                     f"{html.escape(it['source'])}</small></li>")
        h.append("</ul>")
    h.append("<hr><p><small style='color:#777'>Automatsko obavještenje. "
             "Uslove uvijek provjeriti u originalnom tekstu poziva.</small></p>")
    return "\n".join(plain), "".join(h)


def write_page(new, found, when, seen=None, sources=None):
    parts = []
    if seen is not None and sources:
        parts.append("<h2 style='color:#1F4E79'>Stanje izvora</h2>" + health_table(seen, sources))
    if new:
        parts.append(render(new, when, f"Novo u zadnjem prolazu ({len(new)})")[1])
    else:
        parts.append("<p>Nema novih poziva u zadnjem prolazu.</p>")
    if found:
        parts.append(render(found, when, f"Trenutno pronađeno na izvorima ({len(found)})")[1])
    os.makedirs(os.path.dirname(PAGE_PATH), exist_ok=True)
    with open(PAGE_PATH, "w", encoding="utf-8") as f:
        f.write(f"""<!doctype html><html lang="bs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Monitor javnih poziva – MSP FBiH</title>
<style>body{{font-family:system-ui,Arial,sans-serif;max-width:860px;margin:2rem auto;
padding:0 1rem;color:#222;line-height:1.5}}a{{color:#1F4E79}}</style></head><body>
<h1 style="color:#1F4E79">Monitor javnih poziva – MSP u FBiH</h1>
<p><small>Zadnje ažuriranje: {html.escape(when)}</small></p>{''.join(parts)}
</body></html>""")


# ---------------- ispravnost izvora ----------------

STATE_LABEL = {"ok": "Ispravan", "down": "Nedostupan", "silent": "Bez stavki",
               "off": "Praćenje isključeno"}
STATE_COLOR = {"ok": "#2E7D32", "down": "#C62828", "silent": "#C55A11", "off": "#777777"}


def _d(s):
    return dt.date.fromisoformat(s) if s else None


def _fmt(s):
    d = _d(s)
    return d.strftime("%d.%m.%Y") if d else "—"


def update_health(seen, sources, results, today):
    """Azurira stanje svakog izvora i vraca (upozorenja, oporavci).
    results: {url: {"count": n, "ok": bool, "reason": str}} za ovaj prolaz.
    Dani se broje kalendarski, pa vise rucnih pokretanja u istom danu ne ubrzava prag."""
    health = seen.setdefault(HEALTH_KEY, {})
    urls = {s["url"] for s in sources}
    for u in list(health):                      # izvori uklonjeni iz sources.yaml
        if u not in urls:
            del health[u]

    t = today.isoformat()
    alerts, recoveries = [], []
    for src in sources:
        u = src["url"]
        h = health.setdefault(u, {})
        h["name"] = src["name"]
        h["url"] = u

        if src.get("health") is False:
            h.update(state="off", since=h.get("since") or t, days=0, reason="", count=0)
            h["alerted"] = None
            continue

        # Poznat kvar: smatra se da je korisnik vec obavijesten (nema upozorenja),
        # ali kad izvor proradi - stize obavjestenje o oporavku.
        if src.get("poznat_kvar") and "alerted" not in h:
            h["alerted"] = "down"

        r = results.get(u) or {"count": 0, "ok": False, "reason": "greska u skripti"}
        if r["ok"] and r["count"] > 0:
            state = "ok"
        elif r["ok"]:
            state = "silent"
        else:
            state = "down"

        if state != h.get("state"):
            h["state"] = state
            h["since"] = t
        h["days"] = (today - _d(h["since"])).days + 1
        h["count"] = r["count"]
        h["reason"] = r["reason"] if state == "down" else ""
        if r["ok"]:
            h["last_reachable"] = t
        if state == "ok":
            h["last_items"] = t

        alerted = h.get("alerted")
        if state == "ok":
            if alerted:
                recoveries.append(u)
            continue
        limit = DOWN_DAYS if state == "down" else int(src.get("tihi_dani", SILENT_DAYS))
        if h["days"] >= limit and alerted != state:
            alerts.append(u)
    return alerts, recoveries


def health_table(seen, sources):
    health = seen.get(HEALTH_KEY, {})
    rows = []
    for src in sources:
        h = health.get(src["url"], {})
        st = h.get("state", "ok")
        label = STATE_LABEL.get(st, st)
        if st in ("down", "silent") and h.get("days"):
            label += f" ({h['days']}. dan)"
        if st == "down" and h.get("reason"):
            label += f" – {h['reason']}"
        if src.get("poznat_kvar") and st == "down":
            label += " – poznat kvar"
        rows.append(
            f"<tr><td style='padding:4px 8px;border-bottom:1px solid #eee'>"
            f"<a href=\"{html.escape(src['url'])}\">{html.escape(src['name'])}</a></td>"
            f"<td style='padding:4px 8px;border-bottom:1px solid #eee;"
            f"color:{STATE_COLOR.get(st, '#222')};font-weight:bold'>{html.escape(label)}</td>"
            f"<td style='padding:4px 8px;border-bottom:1px solid #eee;text-align:center'>"
            f"{h.get('count', '—')}</td>"
            f"<td style='padding:4px 8px;border-bottom:1px solid #eee'>"
            f"{_fmt(h.get('last_items'))}</td></tr>")
    return ("<table style='border-collapse:collapse;font-size:14px'>"
            "<tr style='background:#1F4E79;color:#fff'>"
            "<th style='padding:6px 8px;text-align:left'>Izvor</th>"
            "<th style='padding:6px 8px;text-align:left'>Stanje</th>"
            "<th style='padding:6px 8px'>Pronađeno danas</th>"
            "<th style='padding:6px 8px;text-align:left'>Zadnji put sa pozivima</th></tr>"
            + "".join(rows) + "</table>")


def health_counts(seen, sources):
    health = seen.get(HEALTH_KEY, {})
    c = {"ok": 0, "down": 0, "silent": 0, "off": 0}
    for src in sources:
        c[health.get(src["url"], {}).get("state", "ok")] += 1
    return c


def send_health_alert(seen, alerts, recoveries, when):
    """Jedan mail za sva upozorenja i oporavke iz ovog prolaza.
    Stanje 'alerted' se upisuje SAMO ako je slanje uspjelo - inace se ponavlja sutra."""
    health = seen[HEALTH_KEY]
    parts = []
    if alerts:
        parts.append(f"<h2 style='color:#C62828'>Izvori koji ne rade ({len(alerts)})</h2><ul>")
        for u in alerts:
            h = health[u]
            if h["state"] == "down":
                what = (f"Stranica je nedostupna već <b>{h['days']} dana</b> "
                        f"(razlog: {html.escape(h.get('reason') or 'nepoznat')}). "
                        "Najčešće je to privremeni pad servera ili blokada automatskih posjeta.")
            else:
                what = (f"Stranica radi, ali već <b>{h['days']} dana</b> nije prepoznat nijedan "
                        "poziv. Najčešće to znači da je sajt redizajniran pa skripta više ne "
                        "prepoznaje objave; rjeđe da institucija zaista nema objava.")
            parts.append(f"<li style='margin:8px 0'><b><a href=\"{html.escape(u)}\">"
                         f"{html.escape(h['name'])}</a></b><br>{what}</li>")
        parts.append("</ul><p><b>Šta uraditi:</b> otvori stranicu ručno. Ako ima novih poziva "
                     "koje monitor nije javio, izvoru treba popraviti pravilo čitanja. "
                     "Dok se ne popravi, taj izvor prati ručno.</p>")
    if recoveries:
        parts.append(f"<h2 style='color:#2E7D32'>Izvori koji ponovo rade ({len(recoveries)})</h2><ul>")
        for u in recoveries:
            parts.append(f"<li><a href=\"{html.escape(u)}\">{html.escape(health[u]['name'])}</a>"
                         " – ponovo se uredno čita.</li>")
        parts.append("</ul>")
    body = (f"<p><i>{when}</i></p>" + "".join(parts) +
            "<hr><p><small style='color:#777'>Automatsko upozorenje monitora javnih poziva. "
            "Upozorenje za isti kvar stiže samo jednom.</small></p>")

    if alerts:
        names = ", ".join(health[u]["name"] for u in alerts[:2])
        more = f" i još {len(alerts) - 2}" if len(alerts) > 2 else ""
        subject = f"[Upozorenje] Izvor ne radi: {names}{more}"
    else:
        names = ", ".join(health[u]["name"] for u in recoveries[:2])
        subject = f"[Monitor] Izvor ponovo radi: {names}"

    plain = re.sub(r"<(?!/?(b|a|i)\b)[^>]+>", "\n", body)
    ok = send_email(subject, body)
    ok = send_telegram(plain) or ok
    if ok:
        for u in alerts:
            health[u]["alerted"] = health[u]["state"]
        for u in recoveries:
            health[u]["alerted"] = None
        log("  Upozorenje o izvorima: POSLANO")
    else:
        log("  Upozorenje o izvorima NIJE poslano - pokusat ce se ponovo u sljedecem prolazu.")
    return ok


def weekly_due(seen, today):
    """Izvjestaj ide ponedjeljkom, jednom sedmicno. Ako ponedjeljak bude preskocen
    (GitHub ponekad preskoci zakazano pokretanje), ide prvi sljedeci dan."""
    last = _d(seen.get(META_KEY, {}).get("last_weekly"))
    if last and last.isocalendar()[:2] == today.isocalendar()[:2]:
        return False
    if today.isoweekday() == REPORT_WEEKDAY:
        return True
    return last is None or (today - last).days >= 8


def recent_notified(seen, today, days=7):
    out = []
    for k, v in seen.items():
        if k.startswith("__") or not isinstance(v, dict) or not v.get("notified_on"):
            continue
        if (today - _d(v["notified_on"])).days < days:
            out.append({"title": v.get("title", k), "link": k, "source": v.get("source", ""),
                        "date": v["notified_on"]})
    return sorted(out, key=lambda x: x["date"], reverse=True)


def send_weekly_report(seen, sources, today, when):
    c = health_counts(seen, sources)
    problems = c["down"] + c["silent"]
    if problems == 0:
        status = (f"<p style='color:#2E7D32'><b>Monitor radi ispravno.</b> "
                  f"Svi aktivni izvori ({c['ok']}) se uredno čitaju.</p>")
    else:
        status = (f"<p><b>Monitor radi.</b> Ispravno: {c['ok']} · "
                  f"<span style='color:#C62828'>nedostupno: {c['down']}</span> · "
                  f"<span style='color:#C55A11'>bez stavki: {c['silent']}</span></p>")

    rec = recent_notified(seen, today)
    if rec:
        lst = "".join(
            f"<li style='margin:4px 0'>{_fmt(r['date'])} – <a href=\"{html.escape(r['link'])}\">"
            f"{html.escape(r['title'])}</a><br><small style='color:#777'>"
            f"{html.escape(r['source'])}</small></li>" for r in rec[:30])
        calls = f"<h3 style='color:#2E75B6'>Novi pozivi javljeni u zadnjih 7 dana ({len(rec)})</h3><ul>{lst}</ul>"
    else:
        calls = ("<h3 style='color:#2E75B6'>Novi pozivi javljeni u zadnjih 7 dana</h3>"
                 "<p>Ove sedmice nije bilo novih poziva na praćenim izvorima.</p>")

    body = (f"<h2 style='color:#1F4E79'>Sedmični izvještaj – monitor javnih poziva</h2>"
            f"<p><i>{when}</i></p>{status}"
            f"<h3 style='color:#2E75B6'>Stanje izvora</h3>{health_table(seen, sources)}"
            f"{calls}<hr><p><small style='color:#777'>Ovaj izvještaj stiže svakog ponedjeljka. "
            "Ako ga jednog ponedjeljka ne dobiješ, monitor vjerovatno nije radio – "
            "provjeri tab Actions u GitHubu.</small></p>")
    subject = f"[Sedmični izvještaj] Monitor javnih poziva – {today.strftime('%d.%m.%Y')}"
    plain = (f"<b>Sedmični izvještaj</b> – {when}\nIspravno: {c['ok']}, nedostupno: "
             f"{c['down']}, bez stavki: {c['silent']}\nNovih poziva u 7 dana: {len(rec)}")
    ok = send_email(subject, body)
    ok = send_telegram(plain) or ok
    if ok:
        seen.setdefault(META_KEY, {})["last_weekly"] = today.isoformat()
        log("  Sedmicni izvjestaj: POSLANO")
    else:
        log("  Sedmicni izvjestaj NIJE poslan - pokusat ce se ponovo u sljedecem prolazu.")
    return ok


# ---------------- glavni tok ----------------

def main():
    now = now_local()
    when = now.strftime("%d.%m.%Y %H:%M")
    today = now.date()
    test_mail = os.getenv("TEST_MAIL", "").strip().lower() == "true"

    with open(SOURCES_PATH, encoding="utf-8") as f:
        sources = yaml.safe_load(f)

    seen = load_seen()
    # Izvor je "upisan" tek kad u bazi ima bar jednu stavku iz njega. Izvor koji je
    # do sada vracao 0 (npr. dok mu parser nije popravljen) prvi put se upisuje tiho.
    url_by_name = {s["name"]: s["url"] for s in sources}
    initialized = set()
    for k, v in seen.items():
        if k.startswith("__") or not isinstance(v, dict):
            continue
        u = v.get("src_url") or url_by_name.get(v.get("source"))
        if u:
            initialized.add(u)

    log(f"Skeniram {len(sources)} izvora...")
    found, results = [], {}
    for src in sources:
        try:
            items, ok, reason = scan(src)
            found += items
            results[src["url"]] = {"count": len(items), "ok": ok, "reason": reason}
        except Exception as e:
            log(f"  !! {src.get('name')}: {type(e).__name__}: {e}")
            results[src["url"]] = {"count": 0, "ok": False,
                                   "reason": f"greska u skripti ({type(e).__name__})"}
        time.sleep(1)

    new, silent = [], 0
    for it in found:
        k = key(it)
        if k in seen:
            continue
        seen[k] = {"title": it["title"], "source": it["source"],
                   "src_url": it["src_url"], "first_seen": when}
        if it["src_url"] in initialized:
            new.append(it)
        else:
            silent += 1

    with_items = {it["src_url"] for it in found}
    fresh = [u for u in with_items if u not in initialized]
    seen[SRC_KEY] = sorted(initialized | with_items)

    alerts, recoveries = update_health(seen, sources, results, today)

    log("")
    log(f"Ukupno relevantnih: {len(found)} | Novih za obavjestenje: {len(new)}")
    if fresh:
        log(f"Novi/izmijenjeni izvori upisani bez obavjestenja: {len(fresh)} ({silent} stavki)")
    c = health_counts(seen, sources)
    log(f"Stanje izvora: ispravno {c['ok']} | nedostupno {c['down']} | "
        f"bez stavki {c['silent']} | iskljuceno {c['off']}")
    for src in sources:
        h = seen[HEALTH_KEY].get(src["url"], {})
        if h.get("state") in ("down", "silent"):
            limit = DOWN_DAYS if h["state"] == "down" else int(src.get("tihi_dani", SILENT_DAYS))
            log(f"  - {src['name']}: {STATE_LABEL[h['state']]}, {h['days']}. dan "
                f"(upozorenje od {limit}. dana){' – ' + h['reason'] if h.get('reason') else ''}")
    write_page(new, found, when, seen, sources)

    if test_mail:
        log("")
        log("PROBNO SLANJE (test_mail ukljucen):")
        sample = found[:5]
        heading = "PROBNO SLANJE – primjer obavještenja"
        if sample:
            plain, body = render(sample, when, heading)
        else:
            plain = body = f"<b>{heading}</b><br>Monitor radi, ali trenutno nema pronađenih stavki."
        body += ("<h3 style='color:#2E75B6'>Trenutno stanje izvora</h3>"
                 + health_table(seen, sources))
        ok = send_email(f"[TEST] Monitor javnih poziva – {when}", body)
        ok = send_telegram(plain) or ok
        if not ok:
            log("  NIJE POSLANO - nijedan kanal nije uspio (pogledaj poruke iznad).")

    if new:
        log("")
        log("Saljem obavjestenje o novim pozivima:")
        plain, body = render(new, when, f"Novi javni pozivi ({len(new)})")
        ok = send_email(f"[Pozivi] {len(new)} novih javnih poziva – {when}", body)
        ok = send_telegram(plain) or ok
        if ok:
            for it in new:
                seen[key(it)]["notified_on"] = today.isoformat()
        else:
            # Ne upisujemo ih kao vidjene - pokusat ce se ponovo u sljedecem prolazu
            for it in new:
                seen.pop(key(it), None)
            log(f"  Obavjestenje NIJE poslano - {len(new)} novih poziva ostaje za sljedeci prolaz.")
    elif not test_mail:
        log("Nema novih poziva.")

    if alerts or recoveries:
        log("")
        log(f"Saljem upozorenje o izvorima (ne rade: {len(alerts)}, oporavljeni: {len(recoveries)}):")
        send_health_alert(seen, alerts, recoveries, when)

    if weekly_due(seen, today):
        log("")
        log("Saljem sedmicni izvjestaj:")
        send_weekly_report(seen, sources, today, when)

    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
