#!/usr/bin/env python3
"""
Monitor javnih poziva za MSP u FBiH.

Radi u tri koraka:
  1) Za svaki izvor pokusa naci RSS feed (auto-discovery), pa padne na HTML parsiranje.
  2) Filtrira linkove po kljucnim rijecima (javni poziv, konkurs, poticaj, subvencija...).
  3) Uporedi sa seen.json -> salje notifikaciju SAMO za nove stavke.

Notifikacije: Telegram i/ili email (SMTP). Konfiguracija preko env varijabli.
"""

import os
import re
import json
import sys
import time
import html
import smtplib
import unicodedata
import datetime as dt
from email.message import EmailMessage
from urllib.parse import urljoin, urlparse

import warnings

import requests
import feedparser
import yaml
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

ROOT = os.path.dirname(os.path.abspath(__file__))
SEEN_PATH = os.path.join(ROOT, "seen.json")
SOURCES_PATH = os.path.join(ROOT, "sources.yaml")
PAGE_PATH = os.path.join(ROOT, "docs", "index.html")

UA = "Mozilla/5.0 (compatible; PoziviMonitor/1.0; +poslovni monitoring javnih poziva)"
TIMEOUT = 25

# Kljucne rijeci (bez dijakritike - tekst se normalizuje prije poredjenja)
KEYWORDS = [
    "javni poziv", "javni konkurs", "javni natjecaj", "javnog poziva",
    "poziv za dostav", "konkurs za", "natjecaj za",
    "poticaj", "podsticaj", "subvencij", "sufinansiranj", "sufinanciranj",
    "bespovratn", "grant", "kreditn", "refundacij", "potpore", "potpora",
]

# Ako se pojavi bilo koja od ovih - preskoci (smanjuje sum)
NEGATIVE = [
    "javna nabav", "nabavka", "tender za", "odluka o izboru ponudjaca",
    "prodaja stalnih sredstava", "oglas za prijem", "konkurs za prijem",
    "javni oglas za popunu", "rezultati", "obavijest o dodjeli ugovora",
]


def strip_dia(s: str) -> str:
    """Ukloni dijakritike: natjecaj == natjecaj, poziv == poziv."""
    s = s.replace("đ", "d").replace("Đ", "D")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", strip_dia(html.unescape(s or "")).lower()).strip()


def matches(text: str) -> bool:
    t = norm(text)
    if len(t) < 12:
        return False
    if any(n in t for n in NEGATIVE):
        return False
    return any(k in t for k in KEYWORDS)


def get(url: str):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, verify=True)
        if r.status_code == 200:
            return r
    except Exception as e:
        print(f"    ! greska: {type(e).__name__}: {e}", file=sys.stderr)
    return None


def discover_feed(url: str, soup: BeautifulSoup | None):
    """Nadji RSS: prvo <link rel=alternate>, pa uobicajene putanje (WP /feed/, Drupal /rss)."""
    cands = [url]  # sam URL moze vec biti feed
    if soup:
        for ln in soup.find_all("link", rel=lambda v: v and "alternate" in v):
            if "rss" in (ln.get("type") or "") or "atom" in (ln.get("type") or ""):
                if ln.get("href"):
                    cands.append(urljoin(url, ln["href"]))
    base = url if url.endswith("/") else url + "/"
    root = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    cands += [base + "feed/", base + "rss", root + "/rss", root + "/feed/",
              root + "/?feed=rss2"]

    for c in dict.fromkeys(cands):
        r = get(c)
        if not r:
            continue
        fp = feedparser.parse(r.content)
        if not fp.bozo and fp.entries:
            return c, fp
    return None, None


def from_feed(fp, src) -> list:
    out = []
    for e in fp.entries[:60]:
        title = (e.get("title") or "").strip()
        link = (e.get("link") or "").strip()
        if not title or not link or not matches(title):
            continue
        out.append({"title": title[:300], "link": link, "source": src["name"],
                    "tier": src.get("tier", ""), "via": "rss"})
    return out


def from_html(url: str, soup: BeautifulSoup, src) -> list:
    out, seen_links = [], set()
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if not matches(text):
            continue
        link = urljoin(url, a["href"])
        if link in seen_links or link.startswith(("mailto:", "tel:", "#")):
            continue
        seen_links.add(link)
        out.append({"title": text[:300], "link": link, "source": src["name"],
                    "tier": src.get("tier", ""), "via": "html"})
    return out[:40]


def scan(src) -> list:
    url = src["url"]
    print(f"  -> {src['name']}")
    r = get(url)
    soup = BeautifulSoup(r.content, "html.parser") if r else None

    feed_url, fp = discover_feed(url, soup)
    if fp:
        items = from_feed(fp, src)
        print(f"     RSS: {feed_url} ({len(items)} relevantnih)")
        if items:
            return items
    if soup:
        items = from_html(url, soup, src)
        print(f"     HTML fallback ({len(items)} relevantnih)")
        return items
    print("     preskoceno (nedostupno)")
    return []


def load_seen() -> dict:
    if os.path.exists(SEEN_PATH):
        try:
            with open(SEEN_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def key(it) -> str:
    return norm(it["source"]) + "|" + it["link"].split("?")[0].rstrip("/")


# ---------- notifikacije ----------

def send_telegram(text: str) -> bool:
    tok, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            json={"chat_id": chat, "text": text[:4000],
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=TIMEOUT)
        print("  Telegram:", "OK" if r.status_code == 200 else f"greska {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        print("  Telegram greska:", e, file=sys.stderr)
        return False


def send_email(subject: str, body_html: str) -> bool:
    host, user = os.getenv("SMTP_HOST"), os.getenv("SMTP_USER")
    pwd, to = os.getenv("SMTP_PASS"), os.getenv("MAIL_TO")
    if not all([host, user, pwd, to]):
        return False
    try:
        m = EmailMessage()
        m["Subject"] = subject
        m["From"] = user
        m["To"] = to
        m.set_content("Novi javni pozivi - pogledaj HTML verziju.")
        m.add_alternative(body_html, subtype="html")
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=TIMEOUT) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(m)
        print("  Email: OK")
        return True
    except Exception as e:
        print("  Email greska:", e, file=sys.stderr)
        return False


def render(items, when) -> tuple:
    by = {}
    for it in items:
        by.setdefault(it["tier"] or "Ostalo", []).append(it)

    plain = [f"<b>Novi javni pozivi ({len(items)})</b> — {when}\n"]
    h = [f"<h2>Novi javni pozivi ({len(items)})</h2><p><i>{when}</i></p>"]
    for tier, lst in by.items():
        plain.append(f"\n<b>{html.escape(tier)}</b>")
        h.append(f"<h3>{html.escape(tier)}</h3><ul>")
        for it in lst:
            plain.append(f"• <a href=\"{html.escape(it['link'])}\">{html.escape(it['title'][:120])}</a>"
                         f"\n  <i>{html.escape(it['source'])}</i>")
            h.append(f"<li><a href=\"{html.escape(it['link'])}\">{html.escape(it['title'])}</a>"
                     f"<br><small>{html.escape(it['source'])}</small></li>")
        h.append("</ul>")
    return "\n".join(plain), "".join(h)


def write_page(items, when):
    _, body = render(items, when) if items else ("", "<p>Nema novih poziva u zadnjem prolazu.</p>")
    os.makedirs(os.path.dirname(PAGE_PATH), exist_ok=True)
    with open(PAGE_PATH, "w", encoding="utf-8") as f:
        f.write(f"""<!doctype html><html lang="bs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Monitor javnih poziva – MSP FBiH</title>
<style>body{{font-family:system-ui,Arial,sans-serif;max-width:840px;margin:2rem auto;padding:0 1rem;
color:#222;line-height:1.5}}h1{{color:#1F4E79}}h3{{color:#2E75B6;margin-bottom:.3rem}}
li{{margin:.4rem 0}}small{{color:#777}}a{{color:#1F4E79}}</style></head><body>
<h1>Monitor javnih poziva – MSP u FBiH</h1>
<p><small>Zadnje ažuriranje: {html.escape(when)}</small></p>{body}
<hr><p><small>Automatski generisano. Uvijek provjeriti uslove u originalnom tekstu poziva.</small></p>
</body></html>""")


def main():
    when = dt.datetime.now().strftime("%d.%m.%Y %H:%M")
    with open(SOURCES_PATH, encoding="utf-8") as f:
        sources = yaml.safe_load(f)

    print(f"Skeniram {len(sources)} izvora...")
    found = []
    for src in sources:
        try:
            found += scan(src)
        except Exception as e:
            print(f"  !! {src['name']}: {e}", file=sys.stderr)
        time.sleep(1)

    seen = load_seen()
    first_run = not seen
    new = []
    for it in found:
        k = key(it)
        if k not in seen:
            seen[k] = {"title": it["title"], "first_seen": when}
            new.append(it)

    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=1)

    print(f"\nUkupno relevantnih: {len(found)} | Novih: {len(new)}")
    write_page(new if new else found, when)

    if first_run:
        print("Prvi prolaz – baza inicijalizovana, notifikacije se ne salju.")
        return
    if not new:
        print("Nema novih poziva.")
        return

    plain, body = render(new, when)
    send_telegram(plain)
    send_email(f"[Pozivi] {len(new)} novih javnih poziva – {when}", body)


if __name__ == "__main__":
    main()
