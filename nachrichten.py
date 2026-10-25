"""
nachrichten.py: Schlagzeilen fuer Your Engine. Seit 2.33.

Quellen:
  Telegram  oeffentliche Kanaele ueber die Web-Vorschau t.me/s/<kanal> (z. B. Watcher Guru).
            Kein Bot im Kanal noetig, geht nur bei Kanaelen mit Vorschau.
  RSS       jede RSS- oder Atom-Adresse, z. B. die Pressemitteilungen der Federal Reserve.

  quelle(q)      Name, @kanal, t.me-Link oder RSS-Adresse -> {"key", "name", "url", "kind"}
  holen(q)       neueste Beitraege [{id, ts, text, link}], 2 Minuten zwischengespeichert
"""
import html
import re
import time
import xml.etree.ElementTree as ET

import requests

from logger import log

UA = {"User-Agent": "Mozilla/5.0 (compatible; OddsEngine-YourEngine/1.0; +https://engine.oddsengine.live)"}

BEKANNT = {
    "watcher guru": ("tg", "WatcherGuru", "Watcher Guru"),
    "watcherguru": ("tg", "WatcherGuru", "Watcher Guru"),
    "federal reserve": ("rss", "https://www.federalreserve.gov/feeds/press_all.xml", "Federal Reserve"),
    "fed": ("rss", "https://www.federalreserve.gov/feeds/press_all.xml", "Federal Reserve"),
    "the fed": ("rss", "https://www.federalreserve.gov/feeds/press_all.xml", "Federal Reserve"),
    "sec": ("rss", "https://www.sec.gov/news/pressreleases.rss", "SEC"),
    "whale alert": ("tg", "whale_alert_io", "Whale Alert"),
    "cointelegraph": ("tg", "cointelegraph", "Cointelegraph"),
    "bbc": ("rss", "https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World"),
    "bbc world": ("rss", "https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World"),
    "al jazeera": ("rss", "https://www.aljazeera.com/xml/rss/all.xml", "Al Jazeera"),
    "guardian": ("rss", "https://www.theguardian.com/world/rss", "The Guardian World"),
    "the guardian": ("rss", "https://www.theguardian.com/world/rss", "The Guardian World"),
    "npr": ("rss", "https://feeds.npr.org/1001/rss.xml", "NPR News"),
    "cnbc": ("rss", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", "CNBC Top News"),
    "marketwatch": ("rss", "https://feeds.content.dowjones.io/public/rss/mw_topstories", "MarketWatch"),
}
# Fertige Auswahl fuer die Schalter auf der Seite (seit 2.41): (slug, Gruppe, Suchname fuer BEKANNT)
KATALOG = [
    ("watcherguru", "Crypto", "watcher guru"), ("whalealert", "Crypto", "whale alert"), ("cointelegraph", "Crypto", "cointelegraph"),
    ("bbc", "World", "bbc world"), ("aljazeera", "World", "al jazeera"), ("guardian", "World", "guardian"), ("npr", "World", "npr"),
    ("cnbc", "Markets", "cnbc"), ("marketwatch", "Markets", "marketwatch"),
    ("fed", "Central banks", "federal reserve"), ("sec", "Central banks", "sec"),
]
_CACHE = {}


def quelle(q):
    t = str(q or "").strip()
    if not t:
        return None
    b = BEKANNT.get(t.lower())
    if b:
        art, wo, name = b
    else:
        m = re.match(r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]{4,64})/?$", t)
        if m or re.match(r"^@?[A-Za-z][A-Za-z0-9_]{3,63}$", t):
            art, wo = "tg", (m.group(1) if m else t.lstrip("@"))
            name = wo
        elif re.match(r"^https?://", t):
            art, wo, name = "rss", t, re.sub(r"^https?://(www\.)?", "", t).split("/")[0]
        else:
            return None
    url = f"https://t.me/s/{wo}" if art == "tg" else wo
    return {"key": f"{art}:{wo.lower()}", "name": name, "url": url, "kind": art, "where": wo}


def _tg(kanal):
    r = requests.get(f"https://t.me/s/{kanal}", headers=UA, timeout=15)
    if not r.ok:
        return None
    s = r.text
    out = []
    for blk in re.split(r'(?=<div class="tgme_widget_message_wrap)', s)[1:]:
        pid = re.search(r'data-post="([^"]+)"', blk)
        txt = re.search(r'<div class="tgme_widget_message_text[^>]*>(.*?)</div>', blk, re.S)
        ts = re.search(r'<time datetime="([^"]+)"', blk)
        if not pid or not txt:
            continue
        t = re.sub(r"<br\s*/?>", "\n", txt.group(1))
        t = html.unescape(re.sub(r"<[^>]+>", "", t))
        t = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", t)).strip()
        t = re.sub(r"\s*@\w+\s*$", "", t).strip()
        if not t:
            continue
        num = int(pid.group(1).split("/")[-1]) if pid.group(1).split("/")[-1].isdigit() else 0
        out.append({"id": num, "ts": ts.group(1) if ts else None, "text": t[:900],
                    "link": "https://t.me/" + pid.group(1)})
    if not out and "tgme_channel_info" not in s:
        return None
    return out


def _rss(url):
    r = requests.get(url, headers=UA, timeout=15)
    if not r.ok:
        return None
    root = ET.fromstring(r.content)
    out = []
    items = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    for it in items[:30]:
        def f(*tags):
            for tg in tags:
                x = it.find(tg)
                if x is not None:
                    return (x.text or x.get("href") or "").strip()
            return ""
        titel = html.unescape(re.sub(r"<[^>]+>", "", f("title", "{http://www.w3.org/2005/Atom}title")))
        link = f("link", "{http://www.w3.org/2005/Atom}link")
        ts = f("pubDate", "{http://www.w3.org/2005/Atom}updated", "{http://www.w3.org/2005/Atom}published")
        gid = f("guid", "{http://www.w3.org/2005/Atom}id") or link or titel
        if titel:
            out.append({"id": gid, "ts": ts, "text": titel[:400], "link": link})
    return out


def holen(q):
    """q: Ergebnis von quelle(). Neueste zuletzt (Telegram) bzw. in Feed-Reihenfolge (RSS)."""
    if not q:
        return None
    c = _CACHE.get(q["key"])
    if c and time.time() - c[0] < 110:
        return c[1]
    try:
        d = _tg(q["where"]) if q["kind"] == "tg" else _rss(q["where"])
    except Exception as e:
        log.debug(f"nachrichten {q['key']}: {e}")
        d = None
    if d is not None:
        _CACHE[q["key"]] = (time.time(), d)
    return d


def passt(text, woerter):
    if not woerter:
        return True
    t = (text or "").lower()
    return any(w.lower() in t for w in woerter if w)
