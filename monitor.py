#!/usr/bin/env python3
"""
Monitor javnih poziva za MSP u FBiH  (verzija 3)

Radi u tri koraka:
  1) Za svaki izvor trazi RSS feed (prvo feed kategorije, pa opsti), a ako ga nema
     ili nema relevantnih stavki - cita HTML stranicu.
  2) Filtrira linkove po kljucnim rijecima (javni poziv, konkurs, poticaj, subvencija...).
  3) Uporedjuje sa seen.json i salje obavjestenje SAMO za nove stavke.

Novi ili izmijenjeni izvor se prvi put upisuje "tiho" (bez obavjestenja),
pa dodavanje izvora u sources.yaml ne izaziva poplavu starih poziva.

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
    "poticaj", "podsticaj", "subvencij", "sufinansiranj", "sufinanciranj",
    "bespovratn", "grant", "kreditn", "refundacij", "potpore", "potpora",
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
            return None
        except Exception as e:
            if not quiet:
                log(f"     ! greska pri pristupu: {type(e).__name__}")
            return None
        if r.status_code == 200:
            return r
        if not quiet:
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
        best = ""
        for s in node.stripped_strings:
            if matches(s) and len(s) > len(best):
                best = s
        if best:
            return best
    return None


def from_html(url, soup, src):
    out, seen_links = [], set()
    context_mode = src.get("mode") == "context"
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if not matches(text):
            if not context_mode:
                continue
            text = context_title(a)
            if not text:
                continue
        link = urljoin(url, a["href"])
        if link in seen_links or link.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        seen_links.add(link)
        out.append(item(text, link, src, "html"))
    return out[:40]


def scan(src):
    """Vraca (stavke, dostupan). Izvor je 'dostupan' ako se stranica ili feed ucitao."""
    url = src["url"]
    log(f"  -> {src['name']}")
    r = get(url)
    soup = BeautifulSoup(r.content, "html.parser") if r else None

    feed_url, fp = discover_feed(url, soup)
    if fp:
        items = from_feed(fp, src)
        log(f"     RSS: {feed_url} ({len(items)} relevantnih)")
        if items:
            return items, True
    if soup:
        items = from_html(url, soup, src)
        log(f"     HTML ({len(items)} relevantnih)")
        return items, True
    if fp:
        return [], True
    log("     preskoceno (nedostupno)")
    return [], False


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


def write_page(new, found, when):
    parts = []
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


# ---------------- glavni tok ----------------

def main():
    when = dt.datetime.now().strftime("%d.%m.%Y %H:%M")
    test_mail = os.getenv("TEST_MAIL", "").strip().lower() == "true"

    with open(SOURCES_PATH, encoding="utf-8") as f:
        sources = yaml.safe_load(f)

    seen = load_seen()
    # Izvor je "upisan" tek kad u bazi ima bar jednu stavku iz njega. Izvor koji je
    # do sada vracao 0 (npr. dok mu parser nije popravljen) prvi put se upisuje tiho.
    url_by_name = {s["name"]: s["url"] for s in sources}
    initialized = set()
    for k, v in seen.items():
        if k == SRC_KEY or not isinstance(v, dict):
            continue
        u = v.get("src_url") or url_by_name.get(v.get("source"))
        if u:
            initialized.add(u)

    log(f"Skeniram {len(sources)} izvora...")
    found, reachable = [], []
    for src in sources:
        try:
            items, ok = scan(src)
            found += items
            if ok:
                reachable.append(src["url"])
        except Exception as e:
            log(f"  !! {src.get('name')}: {type(e).__name__}: {e}")
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
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=1)

    log("")
    log(f"Ukupno relevantnih: {len(found)} | Novih za obavjestenje: {len(new)}")
    if fresh:
        log(f"Novi/izmijenjeni izvori upisani bez obavjestenja: {len(fresh)} ({silent} stavki)")
    write_page(new, found, when)

    if test_mail:
        log("")
        log("PROBNO SLANJE (test_mail ukljucen):")
        sample = found[:5]
        heading = "PROBNO SLANJE – primjer obavještenja"
        if sample:
            plain, body = render(sample, when, heading)
        else:
            plain = body = f"<b>{heading}</b><br>Monitor radi, ali trenutno nema pronađenih stavki."
        ok = send_email(f"[TEST] Monitor javnih poziva – {when}", body)
        ok = send_telegram(plain) or ok
        if not ok:
            log("  NIJE POSLANO - nijedan kanal nije uspio (pogledaj poruke iznad).")

    if new:
        log("")
        log("Saljem obavjestenje o novim pozivima:")
        plain, body = render(new, when, f"Novi javni pozivi ({len(new)})")
        send_email(f"[Pozivi] {len(new)} novih javnih poziva – {when}", body)
        send_telegram(plain)
    elif not test_mail:
        log("Nema novih poziva.")


if __name__ == "__main__":
    main()
