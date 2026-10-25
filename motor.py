"""
motor.py: Your Engine, der persoenliche Agent. Seit 2.33.

Ein Kopf fuer den Tab auf engine.oddsengine.live und fuer Telegram, mit demselben
Gedaechtnis und demselben Guthaben.

  frage(adr, text)   Ein Gespraechsschritt: das Modell denkt, ruft Werkzeuge, antwortet.
  tick()             Alle 45 s aus ai.waechter: Erinnerungen, Preisalarme, beobachtete Seiten,
                     Morgen-Briefing zur Uhrzeit des Nutzers. Niemand muss vorher schreiben.

Modell: die Engine waehlt selbst. Normal MOTOR_FAST (Claude Haiku), fuer Analysen MOTOR_DEEP
(Claude Sonnet). Websuche nur ueber das Werkzeug web_search.

Geld: Jede KI-Anfrage laeuft ueber ai.anfrage, also aus dem vorher aufgeladenen Guthaben,
zu den echten Kosten plus Gebuehr. Ohne Guthaben antwortet nur der feste Hinweis.
Zugang seit 2.33: Wallet, verknuepftes Telegram und Guthaben (MOTOR_MIN_CREDIT, 0.05 $).
Briefing, Erinnerungen, Preisalarme und beobachtete Seiten sind ohne KI und laufen, solange
Guthaben da ist. Ist es leer, kommt einmal ein Hinweis, dann pausiert alles bis zum Aufladen.

Alle Modelle: generate_image (Bilder, auch per Sprachnachricht), ask_model (Frage an ein
bestimmtes Modell), Standardmodelle fuer Chat und Bilder je Nutzer. Jede Anfrage wird wie im
Chat zu den echten Kosten plus Gebuehr vom Guthaben abgezogen.

Variablen (web): MOTOR_FAST, MOTOR_DEEP, MOTOR_IMAGE, MOTOR_MAX_WATCH (10), MOTOR_WATCH_MIN (30)
"""
import hashlib
import html
import ipaddress
import json
import os
import re
import secrets
import socket
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

import ai
import holder
import onchain
import storage
from logger import log

FAST = os.getenv("MOTOR_FAST", "").strip() or "anthropic/claude-haiku-5.5"
DEEP = os.getenv("MOTOR_DEEP", "").strip() or "anthropic/claude-sonnet-5.5"
BILD_STD = os.getenv("MOTOR_IMAGE", "").strip() or "google/gemini-3.1-flash-image"
MAXI = os.getenv("MOTOR_MAX", "").strip() or "anthropic/claude-opus-5.5"
# Wie Your Engine denkt, waehlt der Kunde. Nur Modelle, die Werkzeuge sicher bedienen.
KOEPFE = [("auto", "Auto", "Fast for normal questions, Smart for analysis"),
          ("fast", "Fast", "Quick answers, lowest cost"),
          ("smart", "Smart", "Deeper answers for everything"),
          ("max", "Max", "The strongest model, highest cost")]


def _kopf_modell(kopf, text=""):
    if kopf == "fast":
        return FAST
    if kopf == "smart":
        return DEEP
    if kopf == "max":
        return MAXI
    return _modell(text)


def koepfe(adr=None):
    """Die Auswahl mit geschaetzten Kosten je Antwort (inkl. Gebuehr)."""
    try:
        k = 1 + ai.markup(adr)
    except Exception:
        k = 1.1
    def schaetzen(mid):
        m = ai.modell(mid) or {}
        pin, pout = (m.get("in_base") or 0) / 1e6, (m.get("out_base") or 0) / 1e6
        return {"low": round((pin * 5000 + pout * 200) * k, 4), "typical": round((pin * 12000 + pout * 500) * k, 4),
                "high": round((pin * 30000 + pout * 1500) * k, 4)}
    out = []
    for key, label, info in KOEPFE:
        if key == "auto":
            f, d = schaetzen(FAST), schaetzen(DEEP)
            est = {"low": f["low"], "typical": f["typical"], "high": d["high"]}
            modelle = [FAST, DEEP]
        else:
            mid = _kopf_modell(key)
            est, modelle = schaetzen(mid), [mid]
        out.append({"key": key, "label": label, "info": info, "models": modelle,
                    "names": [((ai.modell(x) or {}).get("name") or x) for x in modelle], "usd_per_answer": est})
    return out


def kopf_setzen(adr, kopf):
    kopf = str(kopf or "auto").lower()
    if kopf not in [k for k, _, _ in KOEPFE]:
        return {"ok": False, "error": "Pick auto, fast, smart or max."}
    z = zustand(adr)
    z["prefs"]["brain"] = kopf
    _zustand_speichern(adr, z)
    return {"ok": True, "brain": kopf}


# ── Feedback privat an OddsEngine (seit 2.33) ───────────────────────────

def send_feedback(adr, text, letzte_antwort=None):
    t = str(text or "").strip()[:1500]
    if not t:
        return {"ok": False, "error": "empty feedback"}
    tag = time.strftime("%Y%m%d", time.gmtime())
    k = f"motor_fb_{adr}_{tag}"
    try:
        n = int(storage.kv_get(k) or 0)
    except Exception:
        n = 0
    if n >= 20:
        return {"ok": False, "error": "Thanks, that is a lot of feedback for today. Send more tomorrow."}
    storage.kv_set(k, str(n + 1))
    tgu = _tg_von(adr) or {}
    wer = (_name_aus_gedaechtnis(adr) or tgu.get("name") or "?") + (f" (@{tgu['handle']})" if tgu.get("handle") else "")
    hist = verlauf(adr)[-4:]
    kontext = "\n".join(f"{h.get('role')}: {str(h.get('content') or '')[:300]}" for h in hist)
    import support
    tid = support.neu("engine", t, tg_id=tgu.get("tg"), wallet=adr, name=_name_aus_gedaechtnis(adr) or tgu.get("name"),
                      handle=tgu.get("handle"), context=("Last messages:\n" + kontext) if kontext else None, melden=True)
    return {"ok": bool(tid), "ticket": tid, "note": "Saved as a support ticket for the OddsEngine team. Nobody else sees it. The team answers in Telegram."}


# ── Wie der Nutzer heisst ───────────────────────────────────────────────

def _name_aus_gedaechtnis(adr):
    items, _ = _gedaechtnis(adr)
    for i in reversed(items):
        t = str(i.get("text") or "")
        if t.lower().startswith("name: "):
            return t[6:].strip()
    return None


def set_name(adr, name):
    n = re.sub(r"\s+", " ", str(name or "")).strip()[:40]
    if not n:
        return {"ok": False, "error": "empty name"}
    s = _einst(adr)
    mem = s.setdefault("memory", {"paused": False, "cats": {}, "items": []})
    mem["items"] = [i for i in mem.get("items") or [] if not str(i.get("text", "")).lower().startswith("name: ")]
    _einst_speichern(adr, s)
    r = _merken(adr, "notes", "Name: " + n)
    if not r.get("ok"):
        return {"ok": False, "why": r.get("why") or "could not save", "note": "Use the name in this chat anyway."}
    return {"ok": True, "name": n}
MAX_RUNDEN = 7
HIST = 16
TIEF = re.compile(r"\b(analy[sz]|compare|vergleich|strateg|why|warum|explain|erkl[aä]r|plan|risk|risiko|"
                  r"research|recherch|deep|ausf[uü]hrlich|in detail|detailed|think)", re.I)


def _f(k, d):
    try:
        return float(os.getenv(k, "").strip() or d)
    except Exception:
        return float(d)


# ── Speicher ─────────────────────────────────────────────────────────────

def _j(k, std):
    try:
        r = storage.kv_get(k)
        return json.loads(r) if r else std
    except Exception:
        return std


def _set(k, v):
    storage.kv_set(k, json.dumps(v, default=str))


def zustand(adr):
    z = _j("motor_s_" + adr, {})
    z.setdefault("reminders", [])
    z.setdefault("alerts", [])
    z.setdefault("watches", [])
    z.setdefault("inbox", [])
    z.setdefault("prefs", {})
    z.setdefault("routes", [])
    z.setdefault("news", [])
    z.setdefault("signals", [])
    z.setdefault("followups", [])
    z.setdefault("tokwatch", [])
    z["prefs"].setdefault("hour", 8)
    return z


def _zustand_speichern(adr, z):
    _set("motor_s_" + adr, z)
    ids = set(_j("motor_users", []))
    if adr not in ids:
        ids.add(adr)
        _set("motor_users", sorted(ids))


def _einst(adr):
    return _j("engine_set_" + adr, {})


def _einst_speichern(adr, s):
    _set("engine_set_" + adr, s)


def verlauf(adr):
    return _j("motor_h_" + adr, [])


def verlauf_loeschen(adr):
    storage.kv_set("motor_h_" + adr, "[]")


def _tg_von(adr):
    try:
        import agent
        for u in agent.alle():
            if u.get("wallet") == adr:
                return u
    except Exception:
        pass
    return None


def _tz(adr):
    u = _tg_von(adr)
    z = zustand(adr)
    if z["prefs"].get("tz") is not None:
        return float(z["prefs"]["tz"])
    if u:
        return float((u.get("rules") or {}).get("tz") or 0)
    return 0.0


def _lokal(adr):
    return datetime.now(timezone.utc) + timedelta(hours=_tz(adr))


def senden(adr, text, buttons=None, html=False):
    """An Telegram, wenn verknuepft. Sonst in den Posteingang fuer den Web-Tab.
    Seit 2.39 in der Sprache des Nutzers (Schalter 'lang'). html=True: Telegram-HTML, wird nicht uebersetzt."""
    if not html:
        try:
            text = _uebersetzen(adr, text)
        except Exception:
            pass
    u = _tg_von(adr)
    if u:
        try:
            import agent
            if agent._send(u["tg"], text, buttons, html=html):
                return True
        except Exception as e:
            log.warning(f"motor senden: {e}")
    z = zustand(adr)
    z["inbox"] = (z["inbox"] + [{"ts": int(time.time()), "text": re.sub(r"</?(b|code|i)>", "", text) if html else text}])[-20:]
    _zustand_speichern(adr, z)
    return False


# ── Gedaechtnis ──────────────────────────────────────────────────────────

def _gedaechtnis(adr):
    s = _einst(adr)
    mem = s.get("memory") or {}
    cats = {"wallets": True, "markets": True, "notes": True}
    cats.update(mem.get("cats") or {})
    return [i for i in (mem.get("items") or []) if cats.get(i.get("cat"), True)], mem.get("paused", False)


def _merken(adr, cat, text):
    cat = cat if cat in ("wallets", "markets", "notes") else "notes"
    s = _einst(adr)
    mem = s.setdefault("memory", {"paused": False, "cats": {}, "items": []})
    if mem.get("paused"):
        return {"ok": False, "why": "the user paused memory, nothing new is saved"}
    if (mem.get("cats") or {}).get(cat) is False:
        return {"ok": False, "why": f"the user switched off memory for {cat}"}
    text = str(text or "").strip()[:240]
    if not text:
        return {"ok": False}
    items = mem.setdefault("items", [])
    if any(i.get("text", "").lower() == text.lower() for i in items):
        return {"ok": True, "note": "already saved"}
    i = {"id": secrets.token_hex(4), "cat": cat, "text": text, "ts": int(time.time())}
    items.append(i)
    mem["items"] = items[-60:]
    _einst_speichern(adr, s)
    return {"ok": True, "id": i["id"]}


def _vergessen(adr, mid):
    s = _einst(adr)
    mem = s.setdefault("memory", {"items": []})
    vor = len(mem.get("items") or [])
    mem["items"] = [i for i in mem.get("items") or [] if str(i.get("id")) != str(mid)]
    _einst_speichern(adr, s)
    return {"ok": len(mem["items"]) < vor}


def _wallets(adr):
    s = _einst(adr)
    out = [adr]
    try:
        out += list(holder.verknuepft(adr))
    except Exception:
        pass
    out += [w.get("address") for w in s.get("wallets") or [] if w.get("chain") == "EVM"]
    return [w for i, w in enumerate(out) if w and w not in out[:i]]


# ── Werkzeuge: Markt und Engine ──────────────────────────────────────────

def _stufe(adr):
    try:
        return holder.status(adr).get("tier", "public")
    except Exception:
        return "public"


def engine_calls(adr, city=None):
    import api
    stufe = _stufe(adr)
    # Seit 2.36.3: Your Engine haengt am Guthaben, nicht am Halten. Wer zahlt, sieht alles.
    if not holder.sieht_alles(stufe):
        try:
            if float((ai.konto(adr, anlegen=False) or {}).get("credit") or 0) >= _min():
                stufe = "holder"
        except Exception:
            pass
    rows = [r for r in api._rows(36) if r.get("signal")]
    seen, out = set(), []
    for r in rows:
        if r.get("slug") in seen:
            continue
        seen.add(r.get("slug"))
        if city and city.lower() not in str(r.get("city") or "").lower():
            continue
        if not api._sichtbar(r, stufe):
            continue
        out.append({"city": (r.get("city") or "").title(), "date": r.get("date"), "kind": r.get("kind") or "high",
                    "grade": r.get("grade", "bet"), "block": r.get("legs"),
                    "price_cents": round((r.get("cost") or 0) * 100),
                    "honest_odds_pct": round((r.get("coverage") or 0) * 100),
                    "posted": r.get("ts"),
                    "market": ("https://polymarket.com/event/" + r["event_slug"]) if r.get("event_slug") else None})
    voll = holder.sieht_alles(stufe)
    return {"calls": out[:15], "user_sees_all_calls": voll,
            "note": None if voll else "Without balance the user only sees the free call. With a top up on engine.oddsengine.live they see every call live."}


def kalshi_calls(city=None):
    import kalshi_engine
    if not kalshi_engine.oeffentlich():
        return {"error": "Kalshi Edition is not live yet."}
    d = kalshi_engine.stand()
    c = str(city or "").strip().lower()
    def passt(x):
        return not c or c in str(x.get("city") or "").lower()
    brett = []
    for b in d.get("board") or []:
        if passt(b):
            fav = b.get("favourite") or {}
            brett.append({"city": b["city"], "kind": b["kind"], "date": b["date"], "station": b.get("station"),
                          "engine_centre_f": b.get("centre_f"), "measured_so_far_f": b.get("obs_f"),
                          "engine_favourite": fav.get("label"), "engine_pct": round((fav.get("engine") or 0) * 100),
                          "kalshi_price_c": round((fav.get("market") or 0) * 100) if fav.get("market") else None,
                          "link": b.get("url")})
    calls = [{k: x.get(k) for k in ("city", "kind", "date", "legs", "cost", "coverage", "edge_pp", "grade", "url")}
             for x in d.get("open_calls") or [] if passt(x)]
    return {"open_calls": calls, "board": brett[:30], "record": d.get("record"),
            "page": "https://public.oddsengine.live/#kalshi",
            "note": "Kalshi settles on the NWS daily climate report, whole degrees F. Costs include the Kalshi fee."}


def engine_record():
    import api
    res = api._resolutions()
    n = len(res)
    res = [r for r in res if r.get("outcome_hit") is not None]
    n = len(res)
    hits = sum(1 for r in res if r.get("outcome_hit"))
    return {"scored_calls": n, "hits": hits, "hit_rate_pct": round(hits / n * 100, 1) if n else None,
            "full_record": "https://public.oddsengine.live (Record tab)"}


def crypto_markets():
    k = _j("krypto_live", {})
    ms = k.get("markets") or []
    mit = [m for m in ms if m.get("signal")]
    return {"prices_usd": k.get("assets"),
            "edges_now": [{"coin": m.get("sym"), "window": m.get("tf"), "side": m["signal"].get("side"),
                           "engine_up_pct": round((m.get("p_up") or 0) * 100), "ask_up": m.get("ask_up"),
                           "ask_down": m.get("ask_down"), "ends": m.get("end")} for m in mit][:10],
            "terminal": "https://terminal.oddsengine.live",
            "stale": time.time() - float(k.get("ts") or 0) > 120}


_CG = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple", "DOGE": "dogecoin",
       "BNB": "binancecoin", "HYPE": "hyperliquid", "USDC": "usd-coin", "ADA": "cardano", "AVAX": "avalanche-2",
       "LINK": "chainlink", "SUI": "sui", "TON": "the-open-network", "TRX": "tron", "PEPE": "pepe"}
_PC = {}


def preis(asset):
    """Dollarpreis: grosse Coins (Krypto-Dienst, sonst CoinGecko), Robinhood-Tokens (onchain)."""
    a = str(asset or "").strip().lstrip("$")
    au = a.upper()
    k = (_j("krypto_live", {}).get("assets") or {})
    for kid, v in k.items():
        if kid.upper() == au and v:
            return {"asset": au, "usd": v, "source": "Chainlink feed (Crypto Edition)"}
    if au in _CG:
        c = _PC.get(au)
        if c and time.time() - c[0] < 60:
            return {"asset": au, "usd": c[1], "source": "CoinGecko"}
        try:
            d = requests.get("https://api.coingecko.com/api/v3/simple/price",
                             params={"ids": _CG[au], "vs_currencies": "usd", "include_24hr_change": "true"},
                             timeout=12).json()
            v = d[_CG[au]]["usd"]
            _PC[au] = (time.time(), v)
            return {"asset": au, "usd": v, "change_24h_pct": d[_CG[au]].get("usd_24h_change"), "source": "CoinGecko"}
        except Exception:
            pass
    # Jeder andere Coin: ueber den ganzen Markt (alle Chains), die meiste Liquiditaet gewinnt
    try:
        import scan
        f = scan.ticker_finden(a) if not a.lower().startswith("0x") else None
        if f and f.get("address") and f.get("chain") != "robinhood":
            ps = [x for x in scan._paare(f["address"]) if str((x.get("baseToken") or {}).get("address") or "").lower() == f["address"].lower()]
            ps.sort(key=lambda x: -scan._liq(x))
            if ps and ps[0].get("priceUsd"):
                x = ps[0]
                return {"asset": (x.get("baseToken") or {}).get("symbol") or au, "token": f["address"], "chain": scan.KETTEN[f["chain"]][0],
                        "usd": float(x["priceUsd"]), "change_24h_pct": (x.get("priceChange") or {}).get("h24"),
                        "market_cap_usd": x.get("marketCap") or x.get("fdv"), "source": "DexScreener"}
        if f and f.get("address") and f.get("chain") == "robinhood":
            a = f["address"]
    except Exception as e:
        log.warning(f"motor preis markt: {e}")
    t = onchain._suche(a)
    if t:
        p = onchain.preise([t]).get(t) or {}
        if p.get("usd"):
            m = onchain._meta([t]).get(t, {})
            return {"asset": m.get("symbol") or au, "token": t, "usd": p["usd"], "market_cap_usd": p.get("fdv"),
                    "source": "GeckoTerminal (Robinhood Chain)"}
    return {"error": f"no price found for {asset}"}


def odds_info():
    out = {"token": holder.mint(), "chain": "Robinhood Chain"}
    try:
        out.update({k: v for k, v in onchain.burned("odds").items() if k != "token"})
    except Exception:
        pass
    try:
        import usdc
        o = usdc.oeffentlich()
        out["agent_buybacks"] = {k: o.get(k) for k in ("payments", "bought_odds", "burned_odds", "pending_usd",
                                                         "next_buy_at_usd")}
    except Exception:
        pass
    out["burn_page"] = "https://public.oddsengine.live/burn"
    ki = holder.ketten_info() or {}
    out["buy"] = ki.get("buy_url")
    out["torch_signup"] = ki.get("ref_url")
    out["buy_note"] = "New to Torch: sign up through torch_signup first, then buy $ODDS at buy."
    return out


def polymarket(query):
    try:
        d = requests.get("https://gamma-api.polymarket.com/public-search",
                         params={"q": query, "limit_per_type": 5, "events_status": "active"}, timeout=15).json()
    except Exception as e:
        return {"error": f"polymarket search failed: {str(e)[:80]}"}
    out = []
    for ev in (d.get("events") or [])[:5]:
        ms = []
        for m in (ev.get("markets") or [])[:6]:
            try:
                pr = json.loads(m.get("outcomePrices") or "[]")
                oc = json.loads(m.get("outcomes") or "[]")
            except Exception:
                pr, oc = [], []
            ms.append({"question": m.get("question") or m.get("groupItemTitle"),
                       "prices": {o: round(float(p) * 100, 1) for o, p in zip(oc, pr)} if pr else None,
                       "volume_usd": m.get("volume")})
        out.append({"event": ev.get("title"), "ends": ev.get("endDate"), "markets": ms,
                    "url": "https://polymarket.com/event/" + str(ev.get("slug") or "")})
    return {"results": out, "prices_are": "cents per share (= implied chance in %)"}


# ── Werkzeuge: Web ───────────────────────────────────────────────────────

def _sicher(url):
    try:
        u = urlparse(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return False
        if u.hostname in ("localhost",) or u.hostname.endswith(".internal") or u.hostname.endswith(".local"):
            return False
        for info in socket.getaddrinfo(u.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:
        return False


def _holen(url, max_bytes=2_000_000):
    for _ in range(4):
        if not _sicher(url):
            raise ValueError("this address is not allowed")
        r = requests.get(url, timeout=15, allow_redirects=False, stream=True,
                         headers={"User-Agent": "Mozilla/5.0 (OddsEngine Your Engine)"})
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("Location"):
            url = requests.compat.urljoin(url, r.headers["Location"])
            continue
        roh = b""
        for teil in r.iter_content(65536):
            roh += teil
            if len(roh) > max_bytes:
                break
        return r.status_code, r.headers.get("Content-Type", ""), roh.decode(r.encoding or "utf-8", errors="ignore"), url
    raise ValueError("too many redirects")


def _text(roh):
    titel = re.search(r"<title[^>]*>(.*?)</title>", roh, re.S | re.I)
    t = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", roh)
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr)>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t).strip()
    return (html.unescape(titel.group(1)).strip() if titel else None), t


def read_url(url):
    try:
        st, typ, roh, end = _holen(url)
    except Exception as e:
        return {"error": str(e)[:160]}
    if "html" in typ or roh.lstrip().startswith("<"):
        titel, t = _text(roh)
    else:
        titel, t = None, roh
    return {"url": end, "status": st, "title": titel, "text": t[:12000], "truncated": len(t) > 12000}


_YT = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})")


def youtube_video(url, languages=None):
    """Was in einem YouTube-Video gesagt wird (Untertitel), dazu Titel und Kanal.
    Das Modell fasst danach zusammen. Bilder im Video sieht es nicht, nur den gesprochenen Text.
    Cloud-Server werden von YouTube oft gesperrt: dann hilft YT_PROXY (http://user:pass@host:port)."""
    m = _YT.search(str(url or ""))
    if not m:
        return {"error": "That is not a YouTube video link."}
    vid = m.group(1)
    out = {"video_id": vid, "url": "https://www.youtube.com/watch?v=" + vid}
    try:
        o = requests.get("https://www.youtube.com/oembed", params={"url": out["url"], "format": "json"}, timeout=10).json()
        out["title"], out["channel"] = o.get("title"), o.get("author_name")
    except Exception:
        pass
    sprachen = [x for x in (languages or [])] + ["en", "de", "es", "fr", "pt", "it", "nl", "tr", "ru", "ja", "ko", "zh-Hans", "hi"]
    try:
        import youtube_transcript_api as yta
        proxy = os.getenv("YT_PROXY", "").strip()
        teile = None
        if hasattr(yta.YouTubeTranscriptApi, "fetch") or hasattr(yta.YouTubeTranscriptApi(), "fetch"):
            kw = {}
            if proxy:
                from youtube_transcript_api.proxies import GenericProxyConfig
                kw["proxy_config"] = GenericProxyConfig(http_url=proxy, https_url=proxy)
            api_ = yta.YouTubeTranscriptApi(**kw)
            try:
                f = api_.fetch(vid, languages=sprachen)
            except Exception:
                liste = api_.list(vid)
                tr = next(iter(liste))
                f = tr.fetch()
            teile = [(x.start, x.text) for x in f]
            out["language"] = getattr(f, "language_code", None)
        else:
            kw = {"proxies": {"http": proxy, "https": proxy}} if proxy else {}
            teile = [(x["start"], x["text"]) for x in yta.YouTubeTranscriptApi.get_transcript(vid, languages=sprachen, **kw)]
        text, letzte = [], -999
        for start, t in teile:
            t = re.sub(r"\s+", " ", str(t)).strip()
            if not t or t in ("[Music]", "[Musik]"):
                continue
            if start - letzte >= 60:
                text.append(f"\n[{int(start // 60)}:{int(start % 60):02d}] ")
                letzte = start
            text.append(t + " ")
        voll = "".join(text).strip()
        out["transcript"] = voll[:40000]
        out["truncated"] = len(voll) > 40000
        out["note"] = ("This is what is SAID in the video (captions), with minute marks. Summarize from it. "
                       "You cannot see the picture: if the video mostly shows instead of explains, say so.")
        return out
    except Exception as e:
        msg = type(e).__name__
        if "Block" in msg or "IpBlocked" in msg or "TooManyRequests" in msg:
            out["error"] = "YouTube blocked the request from our server right now. Try again later."
            log.warning("motor youtube: blocked, set YT_PROXY on web")
        elif "Disabled" in msg or "NoTranscript" in msg or "NotFound" in msg:
            out["error"] = "This video has no captions, so there is no spoken text to read."
        else:
            out["error"] = "Could not read this video: " + msg
        return out


def web_search(adr, query):
    body = {"model": FAST, "max_tokens": 900, "plugins": [{"id": "web", "max_results": 5}],
            "messages": [{"role": "system", "content": "Search the web and answer the question with current facts. "
                                                      "Be specific: names, numbers, dates. Say when the results do not "
                                                      "answer it. Keep it under 200 words."},
                         {"role": "user", "content": str(query)[:500]}]}
    d = ai.anfrage(adr, None, body)
    msg = ((d.get("choices") or [{}])[0].get("message") or {})
    quellen = []
    for a in msg.get("annotations") or []:
        u = (a.get("url_citation") or {})
        if u.get("url") and u["url"] not in [q["url"] for q in quellen]:
            quellen.append({"title": u.get("title"), "url": u["url"]})
    return {"answer": msg.get("content"), "sources": quellen[:6],
            "cost_usd": (d.get("usage") or {}).get("oddsengine_charged_usd")}


def chart_ta(adr, asset, timeframe=None):
    """Chart-Lesung aus echten Kerzen (ta.py). Kostenlos."""
    import ta
    try:
        return ta.lesen(asset, timeframe)
    except Exception as e:
        log.warning(f"motor chart_ta: {e}")
        return {"error": "Chart data could not be loaded right now."}


def x_sentiment(adr, topic, hours=24):
    """Was auf X ueber ein Thema gesagt wird. Echte X-Suche mit XAI_API_KEY, sonst Websuche (klar gekennzeichnet)."""
    import xai
    t = str(topic or "").strip()[:200]
    if not t:
        return {"error": "Which coin, stock or topic?"}
    try:
        h = max(1, min(168, int(hours or 24)))
    except Exception:
        h = 24
    if xai.bereit():
        d = xai.stimmung(adr, t, h)
        if not d.get("error"):
            return d
        log.warning(f"motor x_sentiment: {d.get('error')}")
    w = web_search(adr, f"What are people posting on X (Twitter) about {t} in the last {h} hours? Name accounts and link posts.")
    w["source"] = "web search (no live X access, results are thin)"
    w["how_to_report"] = "Say clearly that this is from web results, not a live read of X, and give no bullish or bearish split."
    return w


# ── Werkzeuge: Erinnerungen, Alarme, Beobachten ──────────────────────────

def _zeitpunkt(adr, when):
    """'in 30 minutes', '2026-10-23 09:00' (Ortszeit), 'tomorrow 8:00' -> Unix-Zeit."""
    s = str(when or "").strip().lower()
    jetzt = time.time()
    m = re.match(r"in\s+(\d+(?:\.\d+)?)\s*(m|min|minute|minutes|h|hr|hour|hours|d|day|days)\b", s)
    if m:
        n = float(m.group(1))
        f = {"m": 60, "min": 60, "minute": 60, "minutes": 60, "h": 3600, "hr": 3600, "hour": 3600,
             "hours": 3600, "d": 86400, "day": 86400, "days": 86400}[m.group(2)]
        return jetzt + n * f
    tz = _tz(adr)
    lok = _lokal(adr)
    m = re.match(r"(today|tomorrow)?\s*(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if m:
        h, mi = int(m.group(2)), int(m.group(3) or 0)
        if m.group(4) == "pm" and h < 12:
            h += 12
        if m.group(4) == "am" and h == 12:
            h = 0
        ziel = lok.replace(hour=h, minute=mi, second=0, microsecond=0)
        if m.group(1) == "tomorrow" or (not m.group(1) and ziel <= lok):
            ziel += timedelta(days=1)
        return (ziel - timedelta(hours=tz)).timestamp()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s[:16], fmt).replace(tzinfo=timezone.utc) - timedelta(hours=tz)
            return d.timestamp()
        except Exception:
            continue
    return None


_WIEDER = {"daily": list(range(7)), "weekdays": [0, 1, 2, 3, 4], "weekends": [5, 6], "weekly": None}


def set_reminder(adr, when, text, repeat=None, follow_up=False, duration_minutes=None):
    rep_ = (repeat or "").strip().lower() or None
    tage_ = None
    if rep_ and rep_ not in _WIEDER:
        tage_ = _tage(rep_)
        if not tage_:
            return {"ok": False, "why": "repeat: daily, weekdays, weekends, weekly or days like 'mon,wed,fri'"}
    ts = _zeitpunkt(adr, when)
    if not ts or ts < time.time() - 60:
        return {"ok": False, "why": "could not read the time. Use 'in 30 minutes', 'tomorrow 9:00' or 'YYYY-MM-DD HH:MM'"}
    z = zustand(adr)
    if len(z["reminders"]) >= 50:
        return {"ok": False, "why": "up to 50 open reminders"}
    r = {"id": secrets.token_hex(3), "at": int(ts), "text": str(text)[:300]}
    if follow_up:
        r["follow_up"] = True
        try:
            r["dur"] = max(10, min(int(duration_minutes or 90), 600))
        except Exception:
            r["dur"] = 90
    if rep_:
        r["repeat"] = rep_
        if tage_:
            r["days"] = tage_
    z["reminders"].append(r)
    _zustand_speichern(adr, z)
    lok = datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=_tz(adr))
    return {"ok": True, "id": r["id"], "local_time": lok.strftime("%a %b %d, %H:%M"), "repeat": rep_ or "once",
            "check_in_after": bool(r.get("follow_up")),
            "delivery": "Telegram" if _tg_von(adr) else "the Your Engine tab (link Telegram to get it on your phone)"}


def _naechster(adr, r):
    """Naechster Termin einer wiederkehrenden Erinnerung, gleiche Uhrzeit in Ortszeit."""
    tz = _tz(adr)
    t = datetime.fromtimestamp(r["at"], timezone.utc) + timedelta(hours=tz)
    if r.get("repeat") == "weekly":
        n = t + timedelta(days=7)
    else:
        tage_ = r.get("days") or _WIEDER.get(r.get("repeat")) or list(range(7))
        n = t + timedelta(days=1)
        for _ in range(8):
            if n.weekday() in tage_:
                break
            n += timedelta(days=1)
    ts = (n - timedelta(hours=tz)).replace(tzinfo=timezone.utc).timestamp() if n.tzinfo is None else (n - timedelta(hours=tz)).timestamp()
    while ts <= time.time():
        ts += 86400 if r.get("repeat") != "weekly" else 7 * 86400
    return int(ts)


# ── Nachfragen (seit 2.36.4) ─────────────────────────────────────────────
# Your Engine merkt sich, was der Nutzer vorhat (Training, Arzt, Meeting, Pruefung) und fragt
# danach kurz nach, wie es war. Quellen: plan_follow_up (aus dem Gespraech), Erinnerungen mit
# follow_up, Kalendertermine. Hoechstens MOTOR_CHECKINS_DAY (3) am Tag, nie zwischen 22 und 8 Uhr
# Ortszeit. Abschaltbar mit set_check_ins.

def _nachfrage_neu(adr, about, ts, quelle="chat", context=None):
    z = zustand(adr)
    if z["prefs"].get("checkins") is False:
        return {"ok": False, "why": "the user switched check-ins off"}
    about = str(about or "").strip()[:160]
    if not about:
        return {"ok": False, "why": "what should I ask about?"}
    if any(f["about"].lower() == about.lower() and abs(f["at"] - ts) < 6 * 3600 for f in z["followups"]):
        return {"ok": True, "note": "already planned"}
    f = {"id": secrets.token_hex(3), "at": int(ts), "about": about, "src": quelle,
         "ctx": str(context or "")[:240] or None}
    z["followups"] = (z["followups"] + [f])[-30:]
    _zustand_speichern(adr, z)
    lok = datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=_tz(adr))
    return {"ok": True, "id": f["id"], "ask_at_local": lok.strftime("%a %b %d, %H:%M")}


def plan_follow_up(adr, about, when, context=None):
    ts = _zeitpunkt(adr, when)
    if not ts or ts < time.time() - 60:
        return {"ok": False, "why": "could not read the time. Use 'in 2 hours', 'today 19:30' or 'tomorrow 12:00'"}
    return _nachfrage_neu(adr, about, ts, "chat", context)


def set_check_ins(adr, on):
    z = zustand(adr)
    z["prefs"]["checkins"] = bool(on)
    if not on:
        z["followups"] = []
    _zustand_speichern(adr, z)
    return {"ok": True, "check_ins": bool(on)}


# ── Sprache und Stimme (seit 2.39, Schalter "lang" und "voice") ─────────

SPRACHEN = {"en": "English", "de": "German", "es": "Spanish", "fr": "French", "it": "Italian", "pt": "Portuguese",
            "nl": "Dutch", "pl": "Polish", "tr": "Turkish", "ru": "Russian", "uk": "Ukrainian", "ar": "Arabic",
            "hi": "Hindi", "id": "Indonesian", "vi": "Vietnamese", "th": "Thai", "ja": "Japanese", "ko": "Korean",
            "zh": "Chinese", "fil": "Filipino", "sv": "Swedish", "da": "Danish", "no": "Norwegian", "fi": "Finnish",
            "cs": "Czech", "ro": "Romanian", "el": "Greek", "he": "Hebrew", "fa": "Persian", "ms": "Malay",
            "hr": "Croatian", "sr": "Serbian", "bs": "Bosnian", "sl": "Slovenian", "sk": "Slovak", "hu": "Hungarian",
            "bg": "Bulgarian", "sq": "Albanian", "lt": "Lithuanian", "lv": "Latvian", "et": "Estonian", "ca": "Catalan"}


def _schalter(name, adr):
    try:
        import features
        return features.an(name) or beta(adr)
    except Exception:
        return False


def _sprache(adr):
    """Gespeicherte Sprache als Name ('German') oder None. Nur mit Schalter 'lang'."""
    if not _schalter("lang", adr):
        return None
    l = (zustand(adr).get("prefs") or {}).get("lang")
    return SPRACHEN.get(l, l) if l else None


def set_language(adr, language):
    t = str(language or "").strip()
    if not t:
        return {"ok": False, "error": "Which language?"}
    code = next((k for k, v in SPRACHEN.items() if t.lower() in (k, v.lower())), None)
    if not code and t.lower() in ("auto", "reset", "default"):
        code = None
    elif not code:
        code = t[:30]
    z = zustand(adr)
    if code:
        z["prefs"]["lang"] = code
    else:
        z["prefs"].pop("lang", None)
    _zustand_speichern(adr, z)
    return {"ok": True, "language": SPRACHEN.get(code, code) if code else "the language the user writes in",
            "note": "Everything from now on is in this language: answers, briefing, alerts, check-ins, OddsEngine news and voice replies."}


def set_voice_replies(adr, on):
    import stimme
    z = zustand(adr)
    z["prefs"]["voice"] = bool(on)
    _zustand_speichern(adr, z)
    return {"ok": True, "voice_replies": bool(on), "ready": stimme.bereit(),
            "how": "When the user sends a voice note in Telegram, the answer comes back as a voice note with the text below it. "
                   "On engine.oddsengine.live every answer has a play button. Voice costs a little more than text, from the balance."}


def set_smart_calls(adr, on):
    z = zustand(adr)
    z["prefs"]["smart"] = bool(on)
    _zustand_speichern(adr, z)
    return {"ok": True, "smart_calls": bool(on),
            "how": "When several wallets that our own scoring rates highly buy the same coin within a short window, "
                   "the user gets the call in Telegram with the full token report and buttons. Free. Not financial advice."}


def smart_calls_record(adr, days=30):
    import smart
    b = smart.bilanz(days or 30, 25)
    b["calls"] = [c for c in b["calls"] if c.get("sent")]
    return b


def _stimme_an(adr):
    return _schalter("voice", adr) and (zustand(adr).get("prefs") or {}).get("voice") is True


_UEB = {}


def _uebersetzen(adr, text):
    """Proaktive Nachrichten in der Sprache des Nutzers. Adressen, Links, Zahlen und Ticker bleiben."""
    sp = _sprache(adr)
    if not sp or sp == "English" or not text or len(text) < 3:
        return text
    key = (sp, hashlib.sha256(text.encode()).hexdigest())
    if key in _UEB:
        return _UEB[key]
    try:
        d = ai.anfrage(adr, None, {"model": FAST, "max_tokens": 1500, "messages": [
            {"role": "system", "content": f"Translate the message into {sp}. Keep it natural, short and in the same layout and line breaks. "
                                          "Keep EXACTLY as they are: links, wallet and contract addresses, numbers, prices, percentages, "
                                          "$TICKERS, emojis and the bar characters. No comments, output only the translation. No em dashes."},
            {"role": "user", "content": text[:6000]}]})
        out = (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    except Exception as e:
        log.debug(f"motor uebersetzen: {e}")
        out = ""
    if not out:
        return text
    out = _striche(out)
    if len(_UEB) > 500:
        _UEB.clear()
    _UEB[key] = out
    return out


def _ruhezeit(adr, ts):
    """Nie nachts fragen: zwischen 22 und 8 Uhr Ortszeit auf 8:30 schieben."""
    tz = _tz(adr)
    lok = datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=tz)
    if lok.hour >= 22:
        lok = (lok + timedelta(days=1)).replace(hour=8, minute=30)
    elif lok.hour < 8:
        lok = lok.replace(hour=8, minute=30)
    else:
        return ts
    return int((lok - timedelta(hours=tz)).timestamp())


def _nachfrage_text(adr, f):
    name = _name_aus_gedaechtnis(adr)
    lok = _lokal(adr)
    vorlage = (f"Hey{(' ' + name) if name else ''}, how did it go: {f['about']}?")
    body = {"model": FAST, "max_tokens": 120, "messages": [
        {"role": "system", "content": "You write ONE short, warm check-in message (max 2 sentences) from a personal assistant to the user, "
                                      "asking how something went. Sound like a friend who remembered, not a bot. No emojis spam (one is fine), "
                                      "no markdown, no em dashes. For health or doctor visits ask gently and never about details. "
                                      "Write in the language the user uses with you (default English)."},
        {"role": "user", "content": f"User name: {name or 'unknown'}. Local time now: {lok.strftime('%A %H:%M')}. "
                                    f"Ask how this went: {f['about']}." + (f" Context: {f['ctx']}" if f.get("ctx") else "")
                                    + " Recent chat language sample: " + " | ".join((h.get("content") or "")[:80] for h in verlauf(adr)[-4:] if h.get("role") == "user")}]}
    try:
        d = ai.anfrage(adr, None, body)
        t = (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        return _striche(t) if t else vorlage
    except Exception:
        return vorlage


def _nachfragen():
    jetzt = time.time()
    tag_max = int(_f("MOTOR_CHECKINS_DAY", 3))
    for adr in _nutzer():
        z = zustand(adr)
        if not z.get("followups") or z["prefs"].get("checkins") is False or not _darf(adr):
            continue
        faellig = [f for f in z["followups"] if f["at"] <= jetzt]
        if not faellig:
            continue
        heute = _lokal(adr).strftime("%Y-%m-%d")
        log_ = [x for x in (z["prefs"].get("checkin_log") or []) if x.get("day") == heute]
        rest, gesendet = [], []
        for f in z["followups"]:
            if f["at"] > jetzt:
                rest.append(f)
                continue
            if jetzt - f["at"] > 36 * 3600:
                continue  # zu alt, nicht mehr nachfragen
            neu = _ruhezeit(adr, f["at"])
            if neu > jetzt + 60:
                rest.append(dict(f, at=neu))
                continue
            if len(log_) + len(gesendet) >= tag_max:
                rest.append(dict(f, at=int(jetzt + 3 * 3600)))
                continue
            gesendet.append(f)
        z["followups"] = rest
        verlauf_neu = []
        for f in gesendet:
            text = _nachfrage_text(adr, f)
            senden(adr, text)
            verlauf_neu.append({"role": "assistant", "content": text})
            log_.append({"day": heute, "about": f["about"], "ts": int(jetzt)})
        alt = [x for x in (z["prefs"].get("checkin_log") or []) if x.get("day") != heute][-20:]
        z["prefs"]["checkin_log"] = alt + log_
        _zustand_speichern(adr, z)
        if verlauf_neu:
            _set("motor_h_" + adr, (verlauf(adr) + verlauf_neu)[-HIST:])


# ── OddsEngine-News an alle Engines (seit 2.36.5) ───────────────────────
# Was der Betreiber im offiziellen Telegram-Kanal postet (oder im Admin-Panel schreibt), geht an
# jede Your Engine mit verknuepftem Telegram. Kostenlos fuer den Nutzer, ohne KI. Abschaltbar:
# /news off im Bot, Schalter auf engine.oddsengine.live oder im Chat ("keine News mehr").
# Nachts (22 bis 8 Uhr Ortszeit) wartet die Nachricht bis zum Morgen. Aelter als 36 Stunden: verfaellt.

def _ank_liste():
    return list(_j("motor_ann", []))


def ankuendigen(text, quelle="admin", kanal=None, msg_id=None):
    text = str(text or "").strip()
    if not text and not msg_id:
        return {"ok": False, "why": "empty"}
    ls = _ank_liste()
    if msg_id and any(a.get("chat") == kanal and a.get("mid") == msg_id for a in ls):
        return {"ok": True, "note": "already queued"}
    a = {"id": secrets.token_hex(4), "ts": int(time.time()), "text": text[:3800], "src": quelle,
         "chat": kanal, "mid": msg_id}
    _set("motor_ann", (ls + [a])[-30:])
    _T["ann"] = 0.0
    return {"ok": True, "id": a["id"], "engines": len(_nutzer())}


def set_official_news(adr, on):
    z = zustand(adr)
    z["prefs"]["official"] = bool(on)
    _zustand_speichern(adr, z)
    return {"ok": True, "official_news": bool(on)}


def _ankuendigungen():
    ls = _ank_liste()
    if not ls:
        return
    import agent
    jetzt = time.time()
    frisch = [a for a in ls if jetzt - a["ts"] < 36 * 3600]
    if not frisch:
        return
    for u in agent.alle():
        adr = u.get("wallet")
        if not adr or not offen(adr):
            continue
        z = zustand(adr)
        if z["prefs"].get("official") is False:
            continue
        stand = int(z["prefs"].get("ann_ts") or 0)
        if not stand:
            # Wer neu dazukommt, bekommt nur News der letzten 6 Stunden, kein altes Archiv.
            stand = int(jetzt - 6 * 3600)
            z["prefs"]["ann_ts"] = stand
            _zustand_speichern(adr, z)
        seen = set(z["prefs"].get("ann_seen") or [])
        offen_ = [a for a in frisch if a["ts"] >= stand and a["id"] not in seen]
        if not offen_:
            continue
        lok = _lokal(adr)
        if lok.hour >= 22 or lok.hour < 8:
            continue
        gesendet = []
        for a in offen_:
            ok = False
            if a.get("chat") and a.get("mid"):
                r = agent._tg("copyMessage", chat_id=u["tg"], from_chat_id=a["chat"], message_id=a["mid"])
                ok = bool(r and r.get("ok"))
            if not ok and a.get("text"):
                ok = bool(agent._send(u["tg"], "OddsEngine news\n\n" + a["text"]))
            if ok:
                gesendet.append(a)
            time.sleep(0.05)
        z = zustand(adr)
        z["prefs"]["ann_seen"] = (list(z["prefs"].get("ann_seen") or []) + [a["id"] for a in offen_])[-40:]
        _zustand_speichern(adr, z)
        if gesendet:
            _set("motor_h_" + adr, (verlauf(adr) + [{"role": "assistant", "content": "[OddsEngine announcement] " + (a.get("text") or "")[:1500]}
                                                    for a in gesendet if a.get("text")])[-HIST:])


def _bewusstsein(adr):
    """Was Your Engine gerade 'weiss': heutige Erinnerungen, geplante und letzte Nachfragen."""
    z = zustand(adr)
    tz = _tz(adr)
    jetzt = time.time()
    lok = lambda ts: (datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=tz)).strftime("%a %H:%M")
    z_ = []
    bald = sorted([r for r in z["reminders"] if r["at"] - jetzt < 36 * 3600], key=lambda r: r["at"])[:6]
    if bald:
        z_.append("Coming up (reminders): " + "; ".join(f"{lok(r['at'])} {r['text'][:60]}" for r in bald))
    plan = sorted(z.get("followups") or [], key=lambda f: f["at"])[:5]
    if plan:
        z_.append("Check-ins you planned: " + "; ".join(f"{lok(f['at'])} ask about {f['about'][:60]}" for f in plan))
    letzte = (z["prefs"].get("checkin_log") or [])[-4:]
    if letzte:
        z_.append("You recently asked how these went: " + "; ".join(f"{x['about'][:60]} ({lok(x['ts'])})" for x in letzte))
    return z_


def set_alert(adr, asset, above=None, below=None):
    p = preis(asset)
    if p.get("error"):
        return p
    if above is None and below is None:
        return {"ok": False, "why": "give above or below"}
    z = zustand(adr)
    if len(z["alerts"]) >= 30:
        return {"ok": False, "why": "up to 30 price alerts"}
    a = {"id": secrets.token_hex(3), "asset": p["asset"], "token": p.get("token"),
         "above": float(above) if above is not None else None, "below": float(below) if below is not None else None,
         "set_at_price": p["usd"]}
    z["alerts"].append(a)
    _zustand_speichern(adr, z)
    return {"ok": True, "id": a["id"], "now_usd": p["usd"]}


def mein_konto(adr):
    b = bereit(adr)
    out = {"balance_usd": b.get("credit"), "telegram_linked": b.get("telegram"), "ready": b.get("ready"),
           "free_trial_messages_left": b.get("trial_left"), "min_balance_usd": b.get("min_usd")}
    try:
        kt = ai.konto(adr, anlegen=False) or {}
        out["spent_usd"] = round(float(kt.get("spent") or 0), 4)
        out["deposited_usd"] = round(float(kt.get("deposited") or 0), 4)
    except Exception:
        pass
    try:
        out["recent_top_ups"] = [{"odds": float(d.get("odds") or 0), "usd": float(d.get("usd") or 0), "when": str(d.get("ts"))}
                                 for d in (ai.einzahlungen(adr, 5) or [])]
    except Exception:
        pass
    try:
        out["deposit_address"] = ai.eingang()
    except Exception:
        pass
    out["how_to_top_up"] = ("engine.oddsengine.live, Your Engine, Top up: pick an amount, the wallet opens with the transfer "
                            "in $ODDS on Robinhood Chain. Credited in dollars at the live price, usually within a minute.")
    return out


_COINS = ("btc", "eth", "sol", "xrp", "doge", "bnb", "hype")


def set_signal_alert(adr, kind, coin=None, timeframe=None, threshold=None):
    """Geld-Alarme (seit 2.36), ohne KI:
    split  Polymarket-Masse und Engine liegen bei einem Up/Down-Fenster mindestens 'threshold' Punkte auseinander.
    edge   Der Optionsmarkt sieht bei "BTC/ETH ueber X" auf Polymarket oder Kalshi mindestens 'threshold' Cent Vorsprung."""
    kind = (kind or "").lower()
    coin = (coin or "").lower().replace("$", "") or None
    if kind not in ("split", "edge"):
        return {"ok": False, "why": "kind is split or edge"}
    if coin and coin not in _COINS:
        return {"ok": False, "why": "coins: " + ", ".join(c.upper() for c in _COINS)}
    if kind == "edge" and coin and coin not in ("btc", "eth"):
        return {"ok": False, "why": "edge alerts exist for BTC and ETH"}
    tf = (timeframe or "").lower() or None
    if tf and tf not in ("5m", "15m", "4h"):
        return {"ok": False, "why": "timeframe 5m, 15m or 4h"}
    t = float(threshold if threshold is not None else (15 if kind == "split" else 5))
    t = max(5.0, min(60.0, t)) if kind == "split" else max(2.0, min(40.0, t))
    z = zustand(adr)
    if len(z["signals"]) >= 12:
        return {"ok": False, "why": "up to 12 signal alerts"}
    a = {"id": secrets.token_hex(3), "kind": kind, "coin": coin, "tf": tf if kind == "split" else None,
         "threshold": t, "seen": []}
    z["signals"].append(a)
    _zustand_speichern(adr, z)
    return {"ok": True, "id": a["id"], "kind": kind, "coin": coin or "all", "threshold": t,
            "note": ("You get a Telegram message when the Polymarket crowd and the engine are this many points apart."
                     if kind == "split" else
                     "You get a Telegram message when the options market sees at least this many cents of edge, fee included.")}


def _signale():
    import json as _json
    try:
        k = _json.loads(storage.kv_get("krypto_live") or "{}")
    except Exception:
        k = {}
    try:
        o = _json.loads(storage.kv_get("optionen_live") or "{}")
    except Exception:
        o = {}
    if time.time() - float(k.get("ts") or 0) > 120:
        k = {}
    if time.time() - float(o.get("ts") or 0) > 900:
        o = {}
    for adr in _nutzer():
        z = zustand(adr)
        if not z.get("signals") or not _darf(adr):
            continue
        geaendert = False
        for a in z["signals"]:
            zeilen = []
            if a["kind"] == "split":
                for m in k.get("markets", []) or []:
                    if (a.get("coin") and m.get("a") != a["coin"]) or (a.get("tf") and m.get("tf") != a["tf"]):
                        continue
                    if m.get("mid_up") is None or m.get("p_up") is None:
                        continue
                    gap = (m["p_up"] - m["mid_up"]) * 100
                    if abs(gap) < a["threshold"] or m.get("slug") in a["seen"]:
                        continue
                    a["seen"] = (a["seen"] + [m.get("slug")])[-40:]
                    zeilen.append(f"{m['a'].upper()} {m['tf']}: crowd {m['mid_up'] * 100:.0f}% Up, engine {m['p_up'] * 100:.0f}% "
                                  f"({gap:+.0f} pts). polymarket.com/event/{m.get('slug')}")
                if zeilen:
                    zeilen.insert(0, "Crowd and engine split")
            else:
                for m in o.get("markets", []) or []:
                    if a.get("coin") and m.get("coin") != a["coin"]:
                        continue
                    for seite in ("yes", "no"):
                        e = m.get("edge_" + seite)
                        if e is None or e * 100 < a["threshold"]:
                            continue
                        key = f"{m.get('market')}|{seite}"
                        if key in a["seen"]:
                            continue
                        a["seen"] = (a["seen"] + [key])[-60:]
                        p = m["p"] if seite == "yes" else 1 - m["p"]
                        preis_ = m["yes_ask"] if seite == "yes" else m["no_ask"]
                        zeilen.append(f"{m['coin'].upper()} above ${m['K']:,.0f}, {('Kalshi' if m['venue'] == 'kalshi' else 'Polymarket')}: "
                                      f"{seite.upper()} at {preis_ * 100:.0f}c, options say {p * 100:.0f}% (+{e * 100:.0f}c). {m.get('url') or ''}")
                if zeilen:
                    zeilen.insert(0, "Options market edge")
            if zeilen:
                geaendert = True
                senden(adr, "\n".join(zeilen[:6]) + "\n\nPaper signal, not financial advice.")
        if geaendert:
            z2 = zustand(adr)
            neu = {a["id"]: a["seen"] for a in z["signals"]}
            for a in z2.get("signals", []):
                if a["id"] in neu:
                    a["seen"] = neu[a["id"]]
            _zustand_speichern(adr, z2)


def watch_page(adr, url, note=""):
    z = zustand(adr)
    if len(z["watches"]) >= int(_f("MOTOR_MAX_WATCH", 10)):
        return {"ok": False, "why": "watch list is full"}
    r = read_url(url)
    if r.get("error"):
        return {"ok": False, "why": r["error"]}
    w = {"id": secrets.token_hex(3), "url": r["url"], "title": r.get("title"), "note": str(note)[:200],
         "hash": hashlib.sha256(r["text"].encode()).hexdigest(), "checked": int(time.time()), "changes": 0}
    storage.kv_set("motor_w_" + w["id"], r["text"][:20000])
    z["watches"].append(w)
    _zustand_speichern(adr, z)
    return {"ok": True, "id": w["id"], "title": w["title"],
            "checks_every_min": int(_f("MOTOR_WATCH_MIN", 30)),
            "cost": "checking is free. Only when the page changes, a short summary is written from the balance."}


def remove(adr, kind, item_id):
    z = zustand(adr)
    feld = {"reminder": "reminders", "alert": "alerts", "watch": "watches", "commute": "routes", "news": "news",
            "signal": "signals", "check_in": "followups", "token_watch": "tokwatch"}.get(kind)
    if not feld:
        return {"ok": False}
    vor = len(z[feld])
    z[feld] = [x for x in z[feld] if str(x.get("id")) != str(item_id)]
    _zustand_speichern(adr, z)
    return {"ok": len(z[feld]) < vor}


def list_items(adr):
    z = zustand(adr)
    tz = _tz(adr)
    return {"reminders": [{"id": r["id"], "text": r["text"], "repeat": r.get("repeat") or "once",
                           "local_time": (datetime.fromtimestamp(r["at"], timezone.utc) + timedelta(hours=tz)).strftime("%a %b %d, %H:%M")}
                          for r in z["reminders"]],
            "alerts": z["alerts"], "watches": [{k: w.get(k) for k in ("id", "url", "title", "note")} for w in z["watches"]],
            "commutes": [{"id": r["id"], "name": r["name"], "to": r["to"]["label"], "from": r["from"]["label"],
                          "time": r["time"], "days": r["days"], "mode": r.get("mode")} for r in z["routes"]],
            "home": (z["prefs"].get("home") or {}).get("label"), "traffic": orte.verkehr_da(),
            "news": [{"id": n["id"], "name": n["name"], "mode": n.get("mode"), "keywords": n.get("words") or []} for n in z["news"]],
            "signals": [{k_: s_.get(k_) for k_ in ("id", "kind", "coin", "tf", "threshold")} for s_ in z.get("signals", [])],
            "check_ins": {"on": z["prefs"].get("checkins") is not False,
                          "planned": [{"id": f["id"], "about": f["about"],
                                       "local_time": (datetime.fromtimestamp(f["at"], timezone.utc) + timedelta(hours=tz)).strftime("%a %b %d, %H:%M")}
                                      for f in z.get("followups", [])]},
            "official_news": z["prefs"].get("official") is not False,
            "token_watches": [{"id": w["id"], "symbol": w["sym"], "every_min": w["every"],
                               "until_local": (datetime.fromtimestamp(w["until"], timezone.utc) + timedelta(hours=tz)).strftime("%H:%M")}
                              for w in z.get("tokwatch", []) if w["until"] > time.time()],
            "torch_profile": (z["prefs"].get("torch") or {}).get("name"),
            "briefing_local_hour": z["prefs"].get("hour"), "utc_offset_hours": tz}


BRIEF_TEILE = ("weather", "calendar", "mail", "commute", "news", "rewards", "calls", "crypto", "coins", "bags", "moving", "stocks")


def _preis_txt(v):
    v = float(v)
    if v >= 10000:
        return f"${v:,.0f}"
    if v >= 1:
        return f"${v:,.2f}"
    if v >= 0.01:
        return f"${v:.4f}"
    return f"${v:.8f}".rstrip("0")


def coin_zeile(sym):
    """'TORCH $0.0123 (+4.2% 24h)' fuer jeden Coin: grosse Coins und Robinhood-Tokens."""
    p = preis(sym)
    if not p.get("usd"):
        return None
    chg = p.get("change_24h_pct")
    if chg is None and p.get("token"):
        try:
            chg = float(((onchain._dex(p["token"]) or {}).get("priceChange") or {}).get("h24"))
        except Exception:
            chg = None
    return f"{p.get('asset') or str(sym).upper()} {_preis_txt(p['usd'])}" + (f" ({chg:+.1f}% 24h)" if chg is not None else "")


STANDARD_COINS = ["ODDS", "TORCH"]


def set_coins(adr, coins):
    """Die Coins, deren Preis der Nutzer im Briefing (und auf Nachfrage) sehen will."""
    roh = coins if isinstance(coins, list) else re.split(r"[,;\s]+", str(coins or ""))
    liste = []
    for c in roh:
        c = str(c).strip().lstrip("$")
        if c and c.upper() not in [x.upper() for x in liste]:
            liste.append(c if c.lower().startswith("0x") or len(c) > 20 else c.upper())
    z = zustand(adr)
    if z["prefs"].get("coins") is None and not any(str(x).upper() in ("ODDS", "TORCH") for x in liste) and liste:
        liste = STANDARD_COINS + liste  # erstes Speichern: ODDS und TORCH bleiben drin
    liste = liste[:12]
    z["prefs"]["coins"] = liste
    _zustand_speichern(adr, z)
    s = _einst(adr)
    br = s.setdefault("briefing", {"on": True, "parts": {}})
    if br.get("parts"):
        br["parts"]["coins"] = True
        _einst_speichern(adr, s)
    jetzt, fehlt = [], []
    for c in liste:
        zl = coin_zeile(c)
        (jetzt if zl else fehlt).append(zl or c)
    out = {"ok": True, "coins": liste, "now": jetzt, "in_briefing": True}
    if fehlt:
        out["not_found"] = fehlt
        out["hint"] = "No price found for these. Ask for the contract address."
    return out


def set_briefing(adr, hour=None, utc_offset_hours=None, on=None, parts=None, coins=None):
    if coins:
        set_coins(adr, coins)
        if isinstance(parts, list) and "coins" not in parts:
            parts = parts + ["coins"]
    z = zustand(adr)
    z["prefs"]["onboarded"] = True
    if hour is not None:
        z["prefs"]["hour"] = max(0, min(23, int(hour)))
    if utc_offset_hours is not None:
        z["prefs"]["tz"] = max(-12, min(14, float(utc_offset_hours)))
    _zustand_speichern(adr, z)
    if on is not None or parts:
        s = _einst(adr)
        br = s.setdefault("briefing", {"on": True, "parts": {}})
        if on is not None:
            br["on"] = bool(on)
        if parts:
            # parts: Liste der gewuenschten Teile (alles andere aus) oder {teil: true/false}
            if isinstance(parts, list):
                gew = {str(x).strip().lower() for x in parts}
                br["parts"] = {t: (t in gew) for t in BRIEF_TEILE}
            elif isinstance(parts, dict):
                br.setdefault("parts", {}).update({k: bool(v) for k, v in parts.items() if k in BRIEF_TEILE})
        _einst_speichern(adr, s)
    br = (_einst(adr).get("briefing") or {})
    out = {"ok": True, "hour": z["prefs"]["hour"], "utc_offset_hours": _tz(adr), "on": br.get("on", True),
           "parts": br.get("parts") or "default (all)", "coins": zustand(adr)["prefs"].get("coins") or []}
    if (br.get("parts") or {}).get("rewards", True) and not (zustand(adr)["prefs"].get("torch") or _torch_auto(adr)):
        out["needs"] = "Torch rewards are in the briefing but no Torch profile is known yet. " + TORCH_HILFE + " Ask for it now."
    if (br.get("parts") or {}).get("weather", True) and not zustand(adr)["prefs"].get("home"):
        out["needs_home"] = "Weather needs their city: ask and call set_home."
    return out


def onboarding_fertig(adr):
    z = zustand(adr)
    z["prefs"]["onboarded"] = True
    _zustand_speichern(adr, z)
    return {"ok": True}


# ── Orte, Wetter, Wege (seit 2.33) ──────────────────────────────────────

import orte

_TAGE = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
         "mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6}


def _tage(x):
    t = str(x if not isinstance(x, list) else ",".join(map(str, x))).lower()
    if not t or "daily" in t or "every day" in t or "taeglich" in t or "täglich" in t:
        return list(range(7))
    if "weekday" in t or "werktag" in t:
        return [0, 1, 2, 3, 4]
    if "weekend" in t or "wochenende" in t:
        return [5, 6]
    out = sorted({v for k, v in _TAGE.items() if re.search(r"\b" + k, t)})
    return out or list(range(7))


def _uhrzeit(x):
    m = re.search(r"(\d{1,2})(?::|\.|h)?(\d{2})?\s*(am|pm)?", str(x or "").lower())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3) == "pm" and h < 12:
        h += 12
    if m.group(3) == "am" and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return f"{h:02d}:{mi:02d}"


def _gebuehr(adr, usd):
    """Fester Betrag vom Guthaben (z. B. Verkehrsabfrage). False, wenn nicht genug da ist."""
    try:
        if not ai._reservieren(adr, usd):
            return False
        ai._ausgleichen(adr, usd, usd)
        return True
    except Exception as e:
        log.warning(f"motor gebuehr: {e}")
        return False


def _ort(adr, q):
    """'home' oder ein gespeicherter Name, sonst Suche."""
    z = zustand(adr)
    t = str(q or "").strip()
    if not t or t.lower() in ("home", "zuhause", "zu hause", "my place", "mein zuhause"):
        return z["prefs"].get("home")
    for r in z["routes"]:
        if r["name"].lower() == t.lower():
            return r["to"]
    return orte.suchen(t)


def set_home(adr, place):
    o = orte.suchen(place)
    if not o:
        return {"ok": False, "error": f"Could not find '{place}'."}
    z = zustand(adr)
    z["prefs"]["home"] = o
    _zustand_speichern(adr, z)
    return {"ok": True, "home": o["label"]}


def add_commute(adr, name, destination, time_, days=None, origin=None, mode="car"):
    z = zustand(adr)
    if len(z["routes"]) >= int(_f("MOTOR_MAX_ROUTES", 6)):
        return {"ok": False, "error": "Too many saved trips. Remove one first."}
    zu = orte.suchen(destination)
    if not zu:
        return {"ok": False, "error": f"Could not find '{destination}'."}
    von = orte.suchen(origin) if origin else z["prefs"].get("home")
    if not von:
        return {"ok": False, "error": "I need a starting point. Ask the user where they live (set_home) or pass origin."}
    uhr = _uhrzeit(time_)
    if not uhr:
        return {"ok": False, "error": f"Could not read the time '{time_}'."}
    r = {"id": secrets.token_hex(3), "name": str(name or destination)[:40], "from": von, "to": zu,
         "time": uhr, "days": _tage(days), "mode": mode or "car"}
    z["routes"].append(r)
    _zustand_speichern(adr, z)
    return {"ok": True, "trip": {"name": r["name"], "from": von["label"], "to": zu["label"], "leaving": uhr,
                                  "days": r["days"]},
            "note": f"I check traffic {int(_f('MOTOR_ROUTE_LEAD_MIN', 45))} min before leaving and message the user. "
                    + ("" if orte.verkehr_da() else "Traffic is not set up yet on the server, tell the user it comes soon.")}


def get_route(adr, origin, destination, mode="car"):
    a, b = _ort(adr, origin), _ort(adr, destination)
    if not a or not b:
        return {"error": "Could not find one of the places. Ask for a clearer address."}
    if not orte.verkehr_da():
        return {"error": "Live traffic is not set up yet.", "km_straight_line": round(orte.km(a, b), 1)}
    if not _gebuehr(adr, _f("MOTOR_ROUTE_USD", 0.002)):
        return {"error": "Balance too low for a traffic check."}
    r = orte.route(a, b, mode)
    r.update({"from": a["label"], "to": b["label"], "cost_usd": _f("MOTOR_ROUTE_USD", 0.002)})
    return r


def get_weather(adr, place=None):
    o = _ort(adr, place)
    if not o:
        return {"error": "Which place? Ask the user, or save their home with set_home."}
    w = orte.wetter(o["lat"], o["lon"], _tz(adr))
    w["place"] = o["label"]
    return w


def _pendeln():
    """Vor jeder gespeicherten Fahrt: Verkehr pruefen und Bescheid geben."""
    vorlauf = _f("MOTOR_ROUTE_LEAD_MIN", 45)
    for adr in _nutzer():
        z = zustand(adr)
        if not z["routes"] or not _darf(adr):
            continue
        lok = _lokal(adr)
        tag = lok.strftime("%Y-%m-%d")
        geaendert = False
        for r in z["routes"]:
            if lok.weekday() not in r.get("days", []) or r.get("sent") == tag:
                continue
            h, m = map(int, r["time"].split(":"))
            ab = lok.replace(hour=h, minute=m, second=0, microsecond=0)
            bis = (ab - lok).total_seconds() / 60
            if not (0 <= bis <= vorlauf):
                continue
            r["sent"] = tag
            geaendert = True
            if not orte.verkehr_da() or not _gebuehr(adr, _f("MOTOR_ROUTE_USD", 0.002)):
                continue
            zeile = orte.route_zeile(r["name"], r["time"], orte.route(r["from"], r["to"], r.get("mode")))
            if zeile:
                senden(adr, "On the road\n\n" + zeile + ".")
        if geaendert:
            z2 = zustand(adr)
            sent = {r["id"]: r.get("sent") for r in z["routes"]}
            for r in z2["routes"]:
                if sent.get(r["id"]):
                    r["sent"] = sent[r["id"]]
            _zustand_speichern(adr, z2)


def _post():
    """Neue wichtige Mails (Rechnungen, Zahlungen, Fristen, Sicherheit) sofort melden. Ohne KI, kostet nichts."""
    import postfach
    for adr in _nutzer():
        st = postfach.status(adr)
        if not st.get("connected") or not st.get("watch") or st.get("error") or not _darf(adr):
            continue
        neu = postfach.neue(adr)
        if neu:
            zeilen = ["New mail that looks important"] + [f"{x['from'][:30]}: {x['subject'][:80]}" for x in neu[:5]]
            zeilen.append("Ask me to summarize or draft a reply.")
            senden(adr, "\n".join(zeilen))


def _termine():
    """Erinnerung vor Kalenderterminen, mit Fahrzeit bei Ort und gespeichertem Zuhause.
    Seit 2.36.4 auch: nach Terminen von mindestens 30 Minuten eine Nachfrage einplanen (hoechstens 2 am Tag)."""
    import kalender
    vorlauf = _f("MOTOR_CAL_LEAD_MIN", 30)
    for adr in _nutzer():
        st = kalender.status(adr)
        if not st.get("connected") or not st.get("remind", True) or not _darf(adr):
            continue
        z = zustand(adr)
        if z["prefs"].get("checkins") is not False:
            try:
                tag = kalender.termine(adr, tage=1, tz_h=_tz(adr))
                gemerkt = z["prefs"].get("cal_fu") or []
                heute = _lokal(adr).strftime("%Y-%m-%d")
                schon = sum(1 for g in gemerkt if g.startswith(heute))
                for e in tag.get("events") or []:
                    if schon >= 2:
                        break
                    if e["all_day"] or not e.get("end") or e["end"] - e["start"] < 1800 or e["end"] < time.time() - 3600:
                        continue
                    key = heute + "|" + e["uid"]
                    if key in gemerkt:
                        continue
                    _nachfrage_neu(adr, e["title"], e["end"] + 30 * 60, "calendar", e.get("location"))
                    gemerkt.append(key)
                    schon += 1
                z["prefs"]["cal_fu"] = gemerkt[-40:]
                _zustand_speichern(adr, z)
                z = zustand(adr)
            except Exception as ex:
                log.debug(f"motor cal followup: {ex}")
        gesendet = z["prefs"].get("cal_sent") or []
        bald = kalender.bald(adr, vorlauf + 60, _tz(adr))
        neu = []
        for e in bald:
            if e["uid"] in gesendet:
                continue
            fahrt = None
            heim = z["prefs"].get("home")
            if e.get("location") and heim and orte.verkehr_da():
                try:
                    ziel = orte.suchen(e["location"])
                    if ziel and _gebuehr(adr, _f("MOTOR_ROUTE_USD", 0.002)):
                        fahrt = orte.route(heim, ziel, "car")
                except Exception:
                    fahrt = None
            minuten = (e["start"] - time.time()) / 60
            weg = (fahrt or {}).get("minutes_now") or 0
            if minuten > vorlauf + weg:
                continue
            text = f"Coming up: {e['title']} at {e['local'][-5:]}" + (f"\n{e['location']}" if e.get("location") else "")
            if fahrt and fahrt.get("minutes_now"):
                los = max(0, int(minuten - fahrt["minutes_now"]))
                text += (f"\nDriving takes {fahrt['minutes_now']} min right now"
                         + (f", {fahrt['minutes_traffic_delay']} min of that is traffic" if fahrt.get("minutes_traffic_delay", 0) >= 3 else "")
                         + f". Leave in about {los} min.")
            senden(adr, text)
            neu.append(e["uid"])
        if neu:
            z2 = zustand(adr)
            z2["prefs"]["cal_sent"] = ((z2["prefs"].get("cal_sent") or []) + neu)[-60:]
            _zustand_speichern(adr, z2)


# ── Torch Holder-Rewards (seit 2.33) ────────────────────────────────────
# Torch zahlt Holdern mancher Coins einen Teil der Kaufsteuer aus, in der Paarwaehrung
# (ETH oder TORCH). Lesbar ueber das oeffentliche Torch-Profil des Nutzers.

TORCH_API = "https://app.torchpad.fun/api/exec/"


def _torch_get(pfad):
    r = requests.get(TORCH_API + pfad, timeout=12, headers={"Accept": "application/json"})
    return r.json() if r.ok else {"error": (r.json() if "json" in r.headers.get("content-type", "") else {}).get("error") or r.status_code}


def set_torch_profile(adr, name):
    n = str(name or "").strip().lstrip("@")
    if not re.match(r"^[A-Za-z0-9_.\-]{2,64}$", n):
        return {"ok": False, "error": "That does not look like a Torch profile name."}
    try:
        p = (_torch_get("profile/" + requests.utils.quote(n)) or {}).get("profile")
    except Exception:
        p = None
    if not p:
        return {"ok": False, "error": f"No public Torch profile called '{n}'. The profile must be public on app.torchpad.fun."}
    z = zustand(adr)
    z["prefs"]["torch"] = {"id": p.get("userId"), "name": p.get("displayName") or n}
    _zustand_speichern(adr, z)
    return {"ok": True, "profile": p.get("displayName") or n}


TORCH_HILFE = ("Torch rewards need the user's Torch profile name once. Where to find it: open app.torchpad.fun, "
               "tap the profile picture top right, the name shown there is it. The profile must be public. "
               "They can tell you in the chat or save it on engine.oddsengine.live under Your Engine, Morning briefing.")


def _torch_auto(adr):
    """Profil ueber die verknuepften Wallets finden, falls Torch das Profil auch unter der Adresse fuehrt."""
    for w in _wallets(adr)[:4]:
        try:
            p = (_torch_get("profile/" + w) or {}).get("profile")
        except Exception:
            p = None
        if p and p.get("userId"):
            z = zustand(adr)
            z["prefs"]["torch"] = {"id": p.get("userId"), "name": p.get("displayName") or w[:8], "via": "wallet"}
            _zustand_speichern(adr, z)
            return z["prefs"]["torch"]
    return None


def torch_rewards(adr, name=None):
    if name:
        r = set_torch_profile(adr, name)
        if not r.get("ok"):
            return r
    tp = zustand(adr)["prefs"].get("torch") or _torch_auto(adr)
    if not tp:
        return {"error": "No Torch profile found for the user's wallets.", "ask_user": TORCH_HILFE}
    try:
        d = _torch_get(f"profile/{tp['id']}/holder-rewards")
    except Exception as e:
        return {"error": f"Torch did not answer: {str(e)[:80]}"}
    if d.get("error"):
        return {"error": f"Torch: {d['error']}"}
    eth_usd = float(d.get("ethUsd") or 0)
    torch_usd = None
    out, summe = [], 0.0
    for t in d.get("tokens") or []:
        dez = int(t.get("assetDecimals") or 18)
        offen = int(t.get("claimable") or 0) / 10 ** dez
        verdient = int(t.get("earned") or 0) / 10 ** dez
        sym = t.get("assetSymbol") or "ETH"
        if sym == "ETH":
            usd = offen * eth_usd
        else:
            if torch_usd is None:
                torch_usd = float((preis("TORCH") or {}).get("usd") or 0)
            usd = offen * torch_usd if sym == "TORCH" else None
        summe += usd or 0
        out.append({"coin": t.get("symbol"), "claimable": offen, "earned_total": verdient, "paid_in": sym,
                    "claimable_usd": round(usd, 2) if usd is not None else None})
    out.sort(key=lambda x: -(x["claimable_usd"] or 0))
    return {"profile": tp["name"], "rewards": out, "claimable_usd_total": round(summe, 2),
            "claim_at": "https://app.torchpad.fun/rewards",
            "how": "New rounds about every 2 hours, claimable 2 hours later, never expire. One claim covers every coin."}


# ── Schlagzeilen aus Telegram-Kanaelen und RSS (seit 2.33) ──────────────

import nachrichten


def follow_news(adr, source, keywords=None, mode="instant"):
    q = nachrichten.quelle(source)
    if not q:
        return {"ok": False, "error": f"I do not know '{source}'. Give me a public Telegram channel (@name or t.me link) or an RSS link."}
    d = nachrichten.holen(q)
    if d is None:
        return {"ok": False, "error": f"I cannot read {q['name']}. Only public Telegram channels with web preview and RSS feeds work."}
    z = zustand(adr)
    if len(z["news"]) >= int(_f("MOTOR_MAX_NEWS", 15)):
        return {"ok": False, "error": "Too many news sources. Remove one first."}
    woerter = [w.strip() for w in re.split(r"[,;]", keywords) if w.strip()] if isinstance(keywords, str) else (keywords or [])
    z["news"] = [n for n in z["news"] if n["key"] != q["key"]]
    z["news"].append({"id": secrets.token_hex(3), "key": q["key"], "name": q["name"], "url": q["url"],
                      "kind": q["kind"], "where": q["where"], "words": woerter[:10],
                      "mode": "briefing" if str(mode).lower().startswith("brief") else "instant"})
    _zustand_speichern(adr, z)
    _gesehen_setzen(q["key"], d)
    return {"ok": True, "source": q["name"], "keywords": woerter, "mode": z["news"][-1]["mode"],
            "latest": (d[-1]["text"][:200] if d and q["kind"] == "tg" else (d[0]["text"][:200] if d else None)),
            "note": "New posts go to the user's Telegram as they come in" if z["news"][-1]["mode"] == "instant"
                    else "The newest headlines go into the morning briefing"}


def unfollow_news(adr, source):
    q = nachrichten.quelle(source)
    z = zustand(adr)
    vorher = len(z["news"])
    z["news"] = [n for n in z["news"] if not (q and n["key"] == q["key"]) and n.get("id") != source]
    _zustand_speichern(adr, z)
    return {"ok": True, "removed": vorher - len(z["news"])}


def news_katalog(adr):
    """Die fertige Auswahl fuer die Schalter auf der Seite, mit dem Stand des Nutzers."""
    meine = {n["key"]: n for n in zustand(adr)["news"]}
    out = []
    for slug, gruppe, such in nachrichten.KATALOG:
        q = nachrichten.quelle(such)
        if not q:
            continue
        n = meine.get(q["key"])
        out.append({"slug": slug, "group": gruppe, "name": q["name"], "on": bool(n), "mode": (n or {}).get("mode") or "briefing",
                    "keywords": (n or {}).get("words") or []})
    eigene = {q["key"] for q in (nachrichten.quelle(s) for _, _, s in nachrichten.KATALOG) if q}
    extra = [{"slug": n["id"], "group": "Yours", "name": n["name"], "on": True, "mode": n.get("mode"), "keywords": n.get("words") or []}
             for n in zustand(adr)["news"] if n["key"] not in eigene]
    return out + extra


def set_stocks(adr, tickers):
    import aktien
    roh = str(tickers or "")
    liste = tickers if isinstance(tickers, list) else (re.split(r"[,;]+", roh) if re.search(r"[,;]", roh) else roh.split())
    syms = [s for s in (aktien.norm(t) for t in liste) if s][:12]
    z = zustand(adr)
    z["prefs"]["stocks"] = syms
    _zustand_speichern(adr, z)
    ks = aktien.kurse(syms) if syms else []
    return {"ok": True, "stocks": syms, "now": [aktien.zeile(k) for k in ks],
            "note": "They show up in the morning briefing (part 'stocks'). Prices are slightly delayed."}


def stock_quotes(adr, tickers=None):
    import aktien
    liste = tickers or zustand(adr)["prefs"].get("stocks") or ["^GSPC", "^IXIC", "^DJI"]
    if isinstance(liste, str):
        liste = re.split(r"[,;]+", liste) if re.search(r"[,;]", liste) else liste.split()
    return {"quotes": aktien.kurse(liste), "note": "Yahoo Finance, slightly delayed. Not financial advice."}


def news_headlines(adr, source=None, query=None, count=5):
    subs = zustand(adr)["news"]
    qs = [nachrichten.quelle(source)] if source else [nachrichten.quelle(n["where"]) or None for n in subs] or [nachrichten.quelle("watcher guru")]
    out = []
    for q in [x for x in qs if x][:5]:
        d = nachrichten.holen(q) or []
        d = d[::-1] if q["kind"] == "tg" else d
        for it in d:
            if nachrichten.passt(it["text"], [query] if query else None):
                out.append({"source": q["name"], "text": it["text"][:400], "time": it.get("ts"), "link": it.get("link")})
            if len([o for o in out if o["source"] == q["name"]]) >= int(count or 5):
                break
    return {"headlines": out[:20]}


def _gesehen(key):
    return set(_j("news_seen_" + key, []))


def _gesehen_setzen(key, d):
    ids = [str(x["id"]) for x in (d or [])]
    alt = _j("news_seen_" + key, [])
    _set("news_seen_" + key, (alt + [i for i in ids if i not in alt])[-200:])


def _nachrichten():
    nutzer = [(a, zustand(a)) for a in _nutzer()]
    nutzer = [(a, z) for a, z in nutzer if z["news"]]
    if not nutzer:
        return
    quellen = {}
    for a, z in nutzer:
        for n in z["news"]:
            quellen[n["key"]] = n
    neu = {}
    for key, n in quellen.items():
        d = nachrichten.holen(nachrichten.quelle(n["where"]))
        if d is None:
            continue
        alt = _gesehen(key)
        frisch = [x for x in d if str(x["id"]) not in alt]
        if alt:
            neu[key] = frisch[-10:] if n["kind"] == "tg" else frisch[:10][::-1]
        _gesehen_setzen(key, d)
        puf = _j("news_buf_" + key, [])
        puf = (puf + [dict(x, seen=int(time.time())) for x in (neu.get(key) or [])])[-60:]
        _set("news_buf_" + key, puf)
    tag = time.strftime("%Y%m%d", time.gmtime())
    grenze = int(_f("MOTOR_NEWS_MAX_DAY", 30))
    for a, z in nutzer:
        if not _darf(a):
            continue
        zahl = z["prefs"].get("news_n") if z["prefs"].get("news_day") == tag else 0
        for n in z["news"]:
            if n.get("mode") != "instant":
                continue
            for it in neu.get(n["key"]) or []:
                if zahl >= grenze or not nachrichten.passt(it["text"], n.get("words")):
                    continue
                senden(a, f"{n['name']}\n\n{it['text']}\n\n{it.get('link') or n['url']}")
                zahl += 1
        if zahl:
            z2 = zustand(a)
            z2["prefs"]["news_day"], z2["prefs"]["news_n"] = tag, zahl
            _zustand_speichern(a, z2)


def _news_fuer_briefing(adr, n_max=5):
    z = zustand(adr)
    out = []
    grenze = time.time() - 86400
    for n in z["news"]:
        puf = [x for x in _j("news_buf_" + n["key"], []) if x.get("seen", 0) >= grenze
               and nachrichten.passt(x["text"], n.get("words"))]
        for x in puf[-3:][::-1]:
            out.append(f"{n['name']}: {x['text'].splitlines()[0][:160]}")
    return out[:n_max]


# ── Alle Modelle von OddsEngine AI (seit 2.33) ──────────────────────────

def _modelle_suchen(q, art=None, n=8):
    q = (q or "").strip().lower()
    woerter = [w for w in re.split(r"[\s/:_,]+", q) if w]
    out = []
    for m in ai.modelle():
        if m.get("id") == ai.AUTO_ID:
            continue
        if art and m.get("kind") != art:
            continue
        heu = (str(m.get("id")) + " " + str(m.get("name") or "")).lower()
        if all(w in heu for w in woerter):
            out.append(m)
    # erst die ausgewaehlten Modelle, dann der Rest in Listenreihenfolge
    out.sort(key=lambda m: 0 if m.get("picked") else 1)
    return out[:n]


def _modell_finden(q, art=None):
    q = str(q or "").strip()
    if not q:
        return None
    m = ai.modell(q)
    if m and m.get("id") != ai.AUTO_ID and (not art or m.get("kind") == art):
        return m
    r = _modelle_suchen(q, art, 1)
    return r[0] if r else None


def _kurz(m):
    return {"id": m.get("id"), "name": m.get("name"), "kind": m.get("kind"),
            "usd_per_m_in": m.get("in"), "usd_per_m_out": m.get("out"),
            "usd_per_image": m.get("per_image"), "free": bool(m.get("free"))}


def standard_modelle(adr):
    p = zustand(adr)["prefs"]
    return {"text_model": p.get("text_model"), "image_model": p.get("image_model") or BILD_STD}


def list_models(query=None, kind=None):
    art = kind if kind in ("text", "image") else None
    ms = _modelle_suchen(query, art, 10)
    if not ms and query:
        return {"models": [], "note": f"No model matches '{query}'."}
    return {"models": [_kurz(m) for m in ms]}


def set_default_model(adr, kind, model):
    art = "image" if str(kind or "").lower().startswith("im") else "text"
    z = zustand(adr)
    if str(model or "").strip().lower() in ("", "none", "reset", "default", "auto"):
        z["prefs"].pop(art + "_model", None)
        _zustand_speichern(adr, z)
        return {"ok": True, "kind": art, "model": None if art == "text" else BILD_STD}
    m = _modell_finden(model, art)
    if not m:
        return {"ok": False, "error": f"No {art} model matches '{model}'.",
                "suggestions": [x["id"] for x in _modelle_suchen("", art, 6)]}
    z["prefs"][art + "_model"] = m["id"]
    _zustand_speichern(adr, z)
    return {"ok": True, "kind": art, "model": m["id"], "name": m.get("name")}


def _inhalt(msg):
    t = msg.get("content") or ""
    if isinstance(t, list):
        t = " ".join(str(x.get("text") or "") for x in t if isinstance(x, dict))
    return str(t)


def ask_model(adr, model, prompt):
    """Eine Frage an ein bestimmtes Modell, abgerechnet wie im Chat."""
    if model:
        m = _modell_finden(model, "text")
        if not m:
            return {"error": f"No chat model matches '{model}'.",
                    "suggestions": [x["id"] for x in _modelle_suchen("", "text", 6)]}
    else:
        m = ai.modell(standard_modelle(adr)["text_model"] or "")
        if not m:
            return {"error": "The user has no default chat model. Answer yourself."}
    d = ai.anfrage(adr, None, {"model": m["id"], "max_tokens": 2500,
                               "messages": [{"role": "user", "content": str(prompt or "")[:12000]}]})
    msg = ((d.get("choices") or [{}])[0].get("message") or {})
    return {"model": m["id"], "name": m.get("name"), "answer": _inhalt(msg)[:12000],
            "cost_usd": (d.get("usage") or {}).get("oddsengine_charged_usd")}


def generate_image(adr, prompt, model, ctx):
    """Bild ueber ein Bild-Modell von OddsEngine AI. Das Bild geht direkt an den Nutzer
    (Web: in den Chat, Telegram: als Foto), das Modell bekommt nur die Bestaetigung."""
    import base64
    import chat
    if model:
        m = _modell_finden(model, "image")
        if not m:
            return {"error": f"No image model matches '{model}'.",
                    "image_models": [x["id"] for x in _modelle_suchen("", "image", 8)]}
    else:
        m = (ai.modell(standard_modelle(adr)["image_model"] or "")
             or ai.modell(BILD_STD) or (_modelle_suchen("", "image", 1) or [None])[0])
        if not m:
            return {"error": "No image model is available right now."}
    d = ai.anfrage(adr, None, {"model": m["id"], "modalities": ["image", "text"], "max_tokens": 4096,
                               "messages": [{"role": "user", "content": str(prompt or "")[:4000]}]})
    msg = ((d.get("choices") or [{}])[0].get("message") or {})
    ids = []
    for b in msg.get("images") or []:
        url = ((b or {}).get("image_url") or {}).get("url") or ""
        mt = chat._DATAURL.match(url)
        if not mt:
            continue
        try:
            ids.append(chat._bild_speichern(adr, "motor", mt.group(1),
                                            base64.b64decode(re.sub(r"\s", "", mt.group(2))), "out"))
        except Exception as e:
            # Speichern ging nicht: das Bild geht trotzdem raus, bezahlt ist bezahlt
            log.warning(f"motor bild speichern: {e}")
            if ctx is not None:
                ctx.setdefault("data", []).append(url)
            ids.append(None)
    ids_ok = [i for i in ids if i]
    kosten = (d.get("usage") or {}).get("oddsengine_charged_usd")
    if ctx is not None:
        ctx.setdefault("images", []).extend(ids_ok)
    if not ids:
        return {"ok": False, "model": m["id"], "cost_usd": kosten,
                "error": "The model returned no image. Try a different prompt or another image model.",
                "model_said": _inhalt(msg)[:400]}
    return {"ok": True, "images": len(ids), "model": m["id"], "name": m.get("name"), "cost_usd": kosten,
            "note": "The image is already in front of the user. Do not paste links, just say a short line."}


# ── Werkzeugliste fuers Modell ───────────────────────────────────────────

def _fn(name, desc, props=None, req=None):
    return {"type": "function", "function": {"name": name, "description": desc,
                                             "parameters": {"type": "object", "properties": props or {},
                                                            "required": req or []}}}


S = {"type": "string"}
N = {"type": "number"}
WERKZEUGE = [
    _fn("remember", "Save a durable fact about the user (wallets they care about, markets they follow, how they like answers). Never save secrets.",
        {"category": {"type": "string", "enum": ["wallets", "markets", "notes"]}, "text": S}, ["category", "text"]),
    _fn("forget", "Delete one memory line by id.", {"id": S}, ["id"]),
    _fn("engine_calls", "OddsEngine's current weather calls on Polymarket (what the user is allowed to see).", {"city": S}),
    _fn("engine_record", "OddsEngine's public track record: scored calls and hit rate."),
    _fn("kalshi_calls", "OddsEngine Kalshi Edition: the engine's calls and its view on Kalshi weather markets (daily high and low, 20+ US cities, Kalshi's own stations), with the Kalshi record. Optional city filter.", {"city": S}),
    _fn("crypto_markets", "OddsEngine Crypto Edition: live coin prices and the Up/Down windows where the engine sees an edge now."),
    _fn("price", "Current USD price of a coin (BTC, ETH, SOL...) or a Robinhood Chain token ($ODDS, $TORCH, a Torch ticker or 0x address).", {"asset": S}, ["asset"]),
    _fn("odds_info", "$ODDS facts: price, supply, total burned, agent buybacks, burn page."),
    _fn("wallet_holdings", "What a wallet holds on Robinhood Chain (ETH, $ODDS, $TORCH, every Torch token) with USD value, plus native coins on Base, Ethereum, BNB and more.", {"address": S}, ["address"]),
    _fn("wallet_activity", "Recent buys, sells and transfers of a wallet on Robinhood Chain, optionally for one token.", {"address": S, "token": S}, ["address"]),
    _fn("token_info", "Everything about a Robinhood Chain token: launch time and age, creator (dev wallet) and how much the creator still holds, supply, burned, price, market cap, liquidity, volume and buys/sells for 5m/1h/6h/24h, price change, holders, top-10 wallets' share, Torch graduation progress, chart and trade links. Call it as soon as the user pastes a 0x address or a ticker.", {"token": S}, ["token"]),
    _fn("watch_token", "Live view of a token in Telegram: an update every 'every' minutes (default 5) for 'minutes' (default 60, max 240) with price, change since start, market cap, volume, buys/sells, plus an instant message when the price moves 'move_pct' percent (default 10). Offer it after a token report.", {"token": S, "minutes": N, "every": N, "move_pct": N}, ["token"]),
    _fn("trade", "Buy or sell a Robinhood Chain token from the user's OWN wallet. Non-custodial: you only prepare it, the user confirms in their wallet with the button that is attached to your answer automatically. side 'buy' or 'sell'. Buy: amount with unit 'usd' (default) or 'eth'. Sell: unit 'percent' (default 100) or 'token' or 'usd'. Before a buy of a coin you have not checked in this chat, call token_info and mention the main risk in one line. Then show: you pay, you get (estimate), route fees, price impact and warnings, and say they confirm with the button. Never say a trade happened before the user confirmed.", {"side": {"type": "string", "enum": ["buy", "sell"]}, "token": S, "amount": N, "unit": {"type": "string", "enum": ["usd", "eth", "token", "percent"]}, "slippage_pct": N}, ["side", "token"]),
    _fn("my_trades", "The user's recent trades sent through Your Engine, with transaction links."),
    _fn("torch_launches", "The latest launches on the Torch launchpad, newest first, with price and market cap.", {"count": N}),
    _fn("torch_market", "What is happening on Torch right now: list 'new' (newest coins), 'trending' (most 24h volume), 'top' (biggest market cap), 'graduated' (coins that reached graduation, most recent first), 'graduating' (closest to graduation, with progress %).", {"list": {"type": "string", "enum": ["new", "trending", "top", "graduated", "graduating"]}, "count": N}),
    _fn("polymarket_search", "Search open Polymarket markets and their current prices.", {"query": S}, ["query"]),
    _fn("web_search", "Search the web for current information. Costs a little from the balance, use it only when the question needs fresh facts.", {"query": S}, ["query"]),
    _fn("read_url", "Read the text of a web page or document link the user gives you.", {"url": S}, ["url"]),
    _fn("youtube_video", "Get what is said in a YouTube video (captions with minute marks), plus title and channel. Use it whenever the user sends a YouTube link or asks about a video: summarize, pull out steps, numbers, claims, or answer questions about it. It reads the spoken words, not the picture. languages: optional preferred caption languages like ['de'].", {"url": S, "languages": {"type": "array", "items": S}}, ["url"]),
    _fn("set_reminder", "Remind the user later. 'when' like 'in 30 minutes', 'tomorrow 9:00', '2026-10-25 18:00' (user's local time). For something that repeats use ONE reminder with repeat ('daily', 'weekdays', 'weekends', 'weekly' or days like 'mon,wed,fri'), never many single ones. follow_up=true for personal plans (training, lesson, meeting, doctor, exam): after it (duration_minutes, default 90) you check in and ask how it went.", {"when": S, "text": S, "repeat": S, "follow_up": {"type": "boolean"}, "duration_minutes": {"type": "integer"}}, ["when", "text"]),
    _fn("plan_follow_up", "Plan a short check-in to ask the user how something went. Use it when they mention a personal plan or appointment (training, doctor, job interview, meeting, exam, date, trip, a game) without needing a reminder. 'when' is when to ask, shortly after it ends, like 'today 20:30' or 'in 3 hours'. Tell them in a few words that you will check in. Skip trivial things.", {"about": S, "when": S, "context": S}, ["about", "when"]),
    _fn("set_check_ins", "Turn your check-ins (asking how things went) on or off for the user.", {"on": {"type": "boolean"}}, ["on"]),
    _fn("set_language", "Save the language for everything you send the user: answers, briefing, alerts, check-ins, news, voice. Use a language name like 'German' or 'Spanish', or 'auto' to follow the language they write in.", {"language": S}, ["language"]),
    _fn("set_smart_calls", "Turn smart money calls on or off for this user: a Telegram message with the token report whenever several highly rated wallets buy the same coin within a short window.", {"on": {"type": "boolean"}}, ["on"]),
    _fn("smart_calls_record", "Track record of every smart money call: market cap at the call, peak since, multiple, hit rate. Use it when the user asks how good the calls are.", {"days": N}),
    _fn("set_voice_replies", "Turn voice replies on or off: when on, a voice note from the user gets a voice note back (with the text below), and the web chat can read answers out loud.", {"on": {"type": "boolean"}}, ["on"]),
    _fn("set_official_news", "Turn OddsEngine news (what the team posts in the official channel and on X) in Telegram on or off for the user.", {"on": {"type": "boolean"}}, ["on"]),
    _fn("set_price_alert", "Alert the user when a price goes above or below a level.", {"asset": S, "above": N, "below": N}, ["asset"]),
    _fn("set_signal_alert", "Money alerts, no AI cost. kind 'split': message the user when the Polymarket crowd and the OddsEngine crypto engine are at least 'threshold' points apart on an Up/Down window (coin, timeframe 5m/15m/4h optional). kind 'edge': message when the options market (Deribit) sees at least 'threshold' cents of edge on Polymarket or Kalshi 'BTC/ETH above X' markets.", {"kind": {"type": "string", "enum": ["split", "edge"]}, "coin": S, "timeframe": S, "threshold": N}, ["kind"]),
    _fn("watch_page", "Watch a web page and tell the user when it changes.", {"url": S, "note": S}, ["url"]),
    _fn("list_items", "List the user's reminders, price alerts, watched pages and briefing time."),
    _fn("remove_item", "Remove a reminder, alert, watched page, saved trip, followed news source, signal alert, planned check-in or live token watch.", {"kind": {"type": "string", "enum": ["reminder", "alert", "watch", "commute", "news", "signal", "check_in", "token_watch"]}, "id": S}, ["kind", "id"]),
    _fn("set_briefing", "Set the morning briefing: local hour (0-23), the user's UTC offset in hours, on/off, and parts: the list of what they want in it, from weather, calendar, mail, commute, news, rewards, calls (weather market calls), crypto (BTC, ETH, SOL prices and terminal edges), coins (ODDS and TORCH by default, plus any coins the user names: pass the full list in 'coins'), bags (their tokens), moving (Torch movers), stocks (their saved stocks, see set_stocks). The user decides: when they say they only want certain things, pass exactly that list, everything else goes off. A list switches everything else off.", {"hour": N, "utc_offset_hours": N, "on": {"type": "boolean"}, "parts": {"type": "array", "items": {"type": "string", "enum": list(BRIEF_TEILE)}}, "coins": {"type": "array", "items": S}}),
    _fn("set_coins", "Save the coins whose price the user wants every morning in the briefing (tickers like TORCH, ODDS, BTC, SOL or contract addresses). Replaces the old list. Always call it when the user names coins they want to see.", {"coins": {"type": "array", "items": S}}, ["coins"]),
    _fn("finish_onboarding", "Call when the user skips or postpones the first setup of the briefing, so you stop asking."),
    _fn("torch_rewards", "Holder rewards the user can claim on Torch (paid in ETH or TORCH), per coin, with USD value. Needs the user's Torch profile name once (pass it as 'profile'); if unknown, explain where they find it: app.torchpad.fun, profile picture top right, the name shown there, profile must be public.", {"profile": S}),
    _fn("follow_news", "Follow a news source and forward new posts to the user's Telegram (mode 'instant') or put them into the morning briefing (mode 'briefing'). Source: a public Telegram channel (@name or t.me link, e.g. Watcher Guru, Whale Alert, Cointelegraph), 'Federal Reserve', 'SEC', or any RSS link. keywords (comma separated) to only get matching posts, e.g. 'BTC, Fed, rate'.", {"source": S, "keywords": S, "mode": {"type": "string", "enum": ["instant", "briefing"]}}, ["source"]),
    _fn("unfollow_news", "Stop following a news source (name, @channel or link).", {"source": S}, ["source"]),
    _fn("set_stocks", "Save the stocks and indices the user wants to follow (tickers like AAPL, TSLA, NVDA, or S&P 500, Nasdaq, DAX, Gold). They appear in the morning briefing.", {"tickers": S}, ["tickers"]),
    _fn("wallet_guard", "Turn Wallet Guard on or off for the user, optionally adding a wallet to protect (0x or Solana). Alerts in Telegram within seconds for risky approvals, Solana delegates or owner changes, tokens taken with an old approval, and tokens leaving without a swap, each with a Revoke button their own wallet signs. Use it when the user asks for protection, security, drain or scam safety.", {"on": {"type": "boolean"}, "wallet": S}),
    _fn("wallet_check", "Check a wallet for open approvals and Solana delegates: who can move which tokens, with risk and a revoke link for each. Empty wallet = the user's own wallets. Use it for 'check my wallet', 'am I safe', 'revoke', 'approvals'.", {"wallet": S}),
    _fn("chart_ta", "Technical chart reading computed from real candles: trend (EMA 20/50/200), RSI, MACD, volatility (ATR), support and resistance, volume, per timeframe. Works for coins (SOL, BTC, ETH...), any contract address or chart link, and stocks. ALWAYS call it when the user asks about a chart, TA, trend, bullish or bearish, levels or momentum. Never answer TA from memory.", {"asset": S, "timeframe": {"type": "string", "enum": ["1h", "4h", "1d"]}}, ["asset"]),
    _fn("x_sentiment", "Live read of what people post on X (Twitter) about a coin, stock or topic: how many posts, mood split, main topics, loudest accounts, shilling or bot signs, with post links. Costs a little from the balance. Use it for sentiment, hype, 'what is CT saying', 'what does X think'.", {"topic": S, "hours": N}, ["topic"]),
    _fn("stock_quotes", "Current stock and index prices (slightly delayed), for given tickers or the user's saved list.", {"tickers": S}),
    _fn("news_headlines", "Latest headlines from the user's followed sources or one given source, optionally filtered by a word.", {"source": S, "query": S, "count": N}),
    _fn("send_feedback", "Send feedback, a bug report or an idea privately to the OddsEngine team. Use it when the user says something is wrong, broken, missing, or says 'feedback'. Include what they said and what went wrong.", {"text": S}, ["text"]),
    _fn("set_name", "Save how the user wants to be called. Call it as soon as they tell you their name or nickname.", {"name": S}, ["name"]),
    _fn("set_home", "Save where the user lives (address, area or city). Used for weather and as the start of trips.", {"place": S}, ["place"]),
    _fn("add_commute", "Save a regular trip, e.g. 'every weekday at 5:00 to the gym'. Before each trip Your Engine checks live traffic and messages the user. time like '05:00' or '7:30am', days like 'weekdays', 'daily', 'mon,wed,fri'. origin empty = home.", {"name": S, "destination": S, "time": S, "days": S, "origin": S, "mode": {"type": "string", "enum": ["car", "bike", "walk", "motorcycle"]}}, ["destination", "time"]),
    _fn("get_route", "Travel time right now with live traffic between two places ('home' works). Costs a little from the balance.", {"origin": S, "destination": S, "mode": {"type": "string", "enum": ["car", "bike", "walk", "motorcycle"]}}, ["destination"]),
    _fn("get_weather", "Weather now and today for a place (empty = the user's home). Worldwide.", {"place": S}),
    _fn("generate_image", "Create an image with an OddsEngine AI image model and show it to the user. Use it whenever the user asks for a picture, image, logo, meme, artwork, by text or voice. 'model' only if the user names one, otherwise their default image model is used.", {"prompt": S, "model": S}, ["prompt"]),
    _fn("ask_model", "Pass a question to one specific AI model of OddsEngine AI and get its answer. Use it when the user names a model (\"ask GPT\", \"with Grok\", \"what does Gemini say\") or when the user has a default chat model and the question needs no live data. Leave 'model' empty to use the user's default chat model.", {"model": S, "prompt": S}, ["prompt"]),
    _fn("list_models", "Find AI models on OddsEngine AI by name and their prices. kind is 'text' or 'image'.", {"query": S, "kind": {"type": "string", "enum": ["text", "image"]}}),
    _fn("my_account", "The user's own Your Engine account: balance in dollars, spent so far, deposited, recent top-ups, free trial messages left, Telegram linked, the deposit address and how to top up. Use it whenever they ask about their balance, credit, costs or top-ups."),
    _fn("mail_status", "Is the user's mailbox connected, which address, and whether new important mail is forwarded."),
    _fn("mail_inbox", "List the user's recent emails (read only): sender, subject, preview, unread, 'important' flag (bills, payments, deadlines, security). days back (default 2), unread_only, an optional search word. Summarize them for the user in plain words.", {"days": N, "unread_only": {"type": "boolean"}, "query": S, "count": N}),
    _fn("mail_read", "Read one email in full by its id from mail_inbox.", {"id": S}, ["id"]),
    _fn("mail_draft", "Write an email reply or a new email and save it as a DRAFT in the user's mailbox. Your Engine never sends mail: tell the user it is in their drafts and they send it themselves. reply_to_id = id of the mail being answered.", {"to": S, "subject": S, "body": S, "reply_to_id": S}, ["body"]),
    _fn("calendar_events", "The user's calendar events (read only) for today and the next days, in their local time.", {"days": N}),
    _fn("calendar_connect", "Save the secret iCal address of the user's calendar (Google: Settings, the calendar, 'Secret address in iCal format'; Apple iCloud: share calendar as public link; Outlook: Publish calendar, ICS link).", {"url": S}, ["url"]),
    _fn("set_default_model", "Save the user's default model for chat answers (kind 'text') or for images (kind 'image'). model 'reset' goes back to the standard.", {"kind": {"type": "string", "enum": ["text", "image"]}, "model": S}, ["kind", "model"]),
]


def _ausfuehren(adr, name, a, ctx=None):
    a = a or {}
    try:
        if name == "torch_rewards":
            return torch_rewards(adr, a.get("profile"))
        if name == "follow_news":
            return follow_news(adr, a.get("source"), a.get("keywords"), a.get("mode") or "instant")
        if name == "news_headlines":
            return news_headlines(adr, a.get("source"), a.get("query"), a.get("count") or 5)
        if name == "send_feedback":
            return send_feedback(adr, a.get("text"))
        if name == "set_name":
            return set_name(adr, a.get("name"))
        if name == "set_home":
            return set_home(adr, a.get("place"))
        if name == "add_commute":
            return add_commute(adr, a.get("name"), a.get("destination"), a.get("time"), a.get("days"),
                               a.get("origin"), a.get("mode") or "car")
        if name == "get_route":
            return get_route(adr, a.get("origin") or "home", a.get("destination"), a.get("mode") or "car")
        if name == "get_weather":
            return get_weather(adr, a.get("place"))
        if name == "generate_image":
            return generate_image(adr, a.get("prompt"), a.get("model"), ctx)
        if name == "ask_model":
            return ask_model(adr, a.get("model"), a.get("prompt"))
        if name == "list_models":
            return list_models(a.get("query"), a.get("kind"))
        if name == "set_default_model":
            return set_default_model(adr, a.get("kind"), a.get("model"))
        if name == "my_account":
            return mein_konto(adr)
        if name == "mail_status":
            import postfach
            st = postfach.status(adr)
            if not st["connected"]:
                st["how"] = ("Connect once on engine.oddsengine.live: Your Engine, Email. You need an app password "
                             "(Gmail: myaccount.google.com/apppasswords, needs 2-step verification). Never ask for it in the chat.")
            return st
        if name == "mail_inbox":
            import postfach
            return postfach.liste(adr, a.get("days") or 2, bool(a.get("unread_only")), a.get("query"), a.get("count") or 15)
        if name == "mail_read":
            import postfach
            return postfach.lesen(adr, a.get("id"))
        if name == "mail_draft":
            import postfach
            return postfach.entwurf(adr, a.get("to"), a.get("subject"), a.get("body"), a.get("reply_to_id"))
        if name == "calendar_events":
            import kalender
            return kalender.termine(adr, a.get("days") or 1, _tz(adr))
        if name == "calendar_connect":
            import kalender
            return kalender.setzen(adr, a.get("url"))
        if name == "remember":
            return _merken(adr, a.get("category"), a.get("text"))
        if name == "forget":
            return _vergessen(adr, a.get("id"))
        if name == "engine_calls":
            return engine_calls(adr, a.get("city"))
        if name == "kalshi_calls":
            return kalshi_calls(a.get("city"))
        if name == "engine_record":
            return engine_record()
        if name == "crypto_markets":
            return crypto_markets()
        if name == "price":
            return preis(a.get("asset"))
        if name == "odds_info":
            return odds_info()
        if name == "wallet_holdings":
            return onchain.holdings(a.get("address"))
        if name == "wallet_activity":
            return onchain.activity(a.get("address"), a.get("token"))
        if name == "project_research":
            return projekt_recherche(adr, a.get("token"), ctx)
        if name == "wallet_intel":
            import intel
            return intel.wallet(a.get("address"))
        if name == "scanner_signals":
            import intel
            return intel.laeufer(a.get("hours") or 72, a.get("count") or 10)
        if name == "token_info":
            if _scan_an(adr):
                return token_report(adr, a.get("token"), ctx)
            return onchain.token(a.get("token"))
        if name == "trade":
            return trade(adr, a.get("side"), a.get("token"), a.get("amount"), a.get("unit"), a.get("slippage_pct"), ctx)
        if name == "my_trades":
            return my_trades(adr)
        if name == "watch_token":
            return watch_token(adr, a.get("token"), a.get("minutes"), a.get("every"), a.get("move_pct"))
        if name == "torch_market":
            return onchain.torch_markt(a.get("list") or "new", a.get("count") or 8)
        if name == "torch_launches":
            return onchain.latest_launches(int(a.get("count") or 5))
        if name == "polymarket_search":
            return polymarket(a.get("query"))
        if name == "web_search":
            return web_search(adr, a.get("query"))
        if name == "read_url":
            if _YT.search(str(a.get("url") or "")):
                return youtube_video(a.get("url"))
            return read_url(a.get("url"))
        if name == "youtube_video":
            return youtube_video(a.get("url"), a.get("languages"))
        if name == "set_reminder":
            return set_reminder(adr, a.get("when"), a.get("text"), a.get("repeat"), a.get("follow_up"), a.get("duration_minutes"))
        if name == "plan_follow_up":
            return plan_follow_up(adr, a.get("about"), a.get("when"), a.get("context"))
        if name == "set_official_news":
            return set_official_news(adr, a.get("on", True))
        if name == "set_check_ins":
            return set_check_ins(adr, a.get("on", True))
        if name == "set_language":
            return set_language(adr, a.get("language"))
        if name == "set_voice_replies":
            return set_voice_replies(adr, a.get("on", True))
        if name == "unfollow_news":
            return unfollow_news(adr, a.get("source"))
        if name == "set_stocks":
            return set_stocks(adr, a.get("tickers"))
        if name == "stock_quotes":
            return stock_quotes(adr, a.get("tickers"))
        if name == "set_coins":
            return set_coins(adr, a.get("coins"))
        if name == "wallet_guard":
            import wache
            return wache.bewachen(adr, a.get("on", True), a.get("wallet"))
        if name == "wallet_check":
            import wache
            r = wache.pruefen(adr, a.get("wallet"))
            for x in (r.get("approvals") or [])[:4]:
                if x.get("revoke_link") and x.get("risk") != "ok":
                    ctx.setdefault("buttons", []).append((f"Revoke {x['token']}"[:30], x["revoke_link"]))
            return r
        if name == "chart_ta":
            return chart_ta(adr, a.get("asset"), a.get("timeframe"))
        if name == "x_sentiment":
            return x_sentiment(adr, a.get("topic"), a.get("hours", 24))
        if name == "set_smart_calls":
            return set_smart_calls(adr, a.get("on", True))
        if name == "smart_calls_record":
            return smart_calls_record(adr, a.get("days") or 30)
        if name == "set_price_alert":
            return set_alert(adr, a.get("asset"), a.get("above"), a.get("below"))
        if name == "set_signal_alert":
            return set_signal_alert(adr, a.get("kind"), a.get("coin"), a.get("timeframe"), a.get("threshold"))
        if name == "watch_page":
            return watch_page(adr, a.get("url"), a.get("note") or "")
        if name == "list_items":
            return list_items(adr)
        if name == "remove_item":
            return remove(adr, a.get("kind"), a.get("id"))
        if name == "set_briefing":
            return set_briefing(adr, a.get("hour"), a.get("utc_offset_hours"), a.get("on"), a.get("parts"), a.get("coins"))
        if name == "finish_onboarding":
            return onboarding_fertig(adr)
    except ai.Fehler:
        raise
    except Exception as e:
        log.warning(f"motor werkzeug {name}: {e}")
        return {"error": f"{name} failed: {str(e)[:160]}"}
    return {"error": "unknown tool"}


# ── Das Gespraech ────────────────────────────────────────────────────────

def _system(adr, quelle):
    items, pausiert = _gedaechtnis(adr)
    lok = _lokal(adr)
    ws = _wallets(adr)
    rufname = _name_aus_gedaechtnis(adr)
    tgu = _tg_von(adr) or {}
    if rufname:
        name_zeile = f"The user wants to be called {rufname}. Use the name now and then, naturally, not in every message."
    else:
        tgname = tgu.get("name") or (("@" + tgu["handle"]) if tgu.get("handle") else None)
        name_zeile = ((f"Their Telegram name is {tgname}. " if tgname else "")
                      + "You do not know yet how they want to be called. If you have not asked in the history above, "
                        "greet them" + (f" as {tgname}" if tgname else "") + " and ask once, in one short line at the end, "
                        "how they would like to be called. When they tell you, call set_name.")
    zeilen = [
        "You are Your Engine, the personal agent of OddsEngine. You work for one person.",
        name_zeile,
        "$ODDS is the token of OddsEngine and of you, Your Engine. It trades on Torch (Robinhood Chain), paired with $TORCH. Every user gets the ODDS and TORCH price in the morning briefing by default (part 'coins'); when they name more coins, set_coins with ODDS and TORCH plus their coins, unless they say they do not want them.",
        "OddsEngine calls Polymarket weather markets, runs the Crypto Edition terminal, and gives access to 460+ AI models paid in $ODDS on Robinhood Chain.",
        "Rules:",
        "- Facts about prices, wallets, tokens, burns, launches, calls and markets come ONLY from your tools. Never guess numbers. If a tool fails, say so.",
        "- Use the tools whenever the question touches live data. Call several tools if needed.",
        "- Be short and clear, like a sharp friend. Plain text, no markdown tables, no headings. Short lines work best on a phone.",
        "- Give the explorer or market link when it helps.",
        "- No financial advice, no price predictions, no promises. Show what the data says.",
        "- Use web_search only when fresh outside facts are needed, it costs the user money.",
        "- Wallet safety: for protection, drains, scam links or approvals use wallet_guard and wallet_check. Be clear: Your Engine never holds keys and cannot stop a transaction; it warns in seconds and gives a revoke button. If a private key or seed phrase is exposed, the only fix is moving funds to a fresh wallet. NEVER ask for a seed phrase or private key; if the user pastes one, tell them to treat that wallet as compromised and move funds now.",
        "- Charts and TA: always call chart_ta (default gives 1h and 1d). Report per timeframe: trend label, price vs EMA 20/50/200, RSI with zone, MACD state, nearest support and resistance with numbers, volume vs average. Then one line: what would change the picture (a level to lose or reclaim). Say 'the chart reads bullish/bearish/mixed', never 'will go up'. If timeframes disagree, say so.",
        "- Sentiment on X: call x_sentiment. Report only what it found: posts read, the mood split, main topics, loud accounts, warning signs, with links. If it found few posts or came from web search, say that plainly and give no split. Never invent counts.",
        "- When the user asks for sentiment AND TA, call both tools in the same turn and give two short blocks.",
        "- If token_info says ambiguous: several real coins share that ticker. Do not pick one. List the candidates (chain, short address, market cap) and ask the user for the contract address.",
        "- Tickers: the whole market counts, not only Torch. A ticker means the coin with that exact ticker and the most liquidity on any chain we cover. If the report has same_ticker_elsewhere, say in one line that other coins use the same ticker and which chain you picked, and offer to check another one by contract. Never swap in a coin with a different ticker.",
        "- Finish every answer. Never stop mid sentence. If the answer gets long, cut detail, not the ending.",
        "- Briefing wishes: when the user names coins they want in the morning (e.g. 'TORCH and ODDS price'), call set_coins with exactly those, then confirm with the prices from the tool. That only adds them, it does not switch other parts off. Use set_briefing parts only when they say what else to keep or drop. Never answer 'I am not sure if the briefing includes X': you set it, then you know.",
        "- Save durable facts with remember (their wallets, coins and cities they follow, how they like answers). Not passing details.",
        "- Answer in the language the user writes in.",
        "- You can see pictures: users send photos and screenshots in Telegram or in the web chat (charts, wallets, receipts, documents, errors, anything). Use what you see.",
        "- If the user reports a bug, a wrong answer or has an idea, send it with send_feedback and tell them the team got it privately.",
        "- You can use every model of OddsEngine AI. Pictures: call generate_image, also when the request came as a voice note. Pass the user's description faithfully and complete, add only what makes the image clearer, never change what they asked for.",
        "- When the user names a model for an answer, use ask_model with that model and give its answer, say which model answered. When they say to use a model from now on, call set_default_model.",
        "- If the user has a default chat model, send general questions that need no live data to ask_model without a model name. Live data (chain, prices, calls, markets, web) you always get with your own tools.",
        "- YouTube: when the user sends a YouTube link, call youtube_video and give a tight summary: what it is about in one line, then the key points with minute marks, then anything they should check. You read the spoken words, not the picture; say so only if the captions are thin. Offer to save the key points to memory.\n"
        "- Email: you can read the user's mailbox and save drafts, never send, delete or move. If it is not connected, explain they connect it once on engine.oddsengine.live with an app password. NEVER ask for or accept a password in the chat; if they paste one, tell them to delete it and use the page.",
        "- Tokens: when the user pastes a 0x address or names a ticker, call token_info right away and give a tight launch report: name and ticker, where and when it launched (age), dev wallet and what the dev still holds, market cap, liquidity, volume and buys vs sells (5m and 1h), holders and the top-10 wallets' share (pools and the Torch curve excluded), burned, Torch graduation progress, price change, then the chart and trade links. Flag risks plainly (dev holds a lot, top-10 hold most, tiny liquidity, sells dominate) without advice. Then offer a live view with watch_token.\n"
        "- Trading: you can prepare buys and sells on Robinhood Chain from the user's own wallet with trade. You never hold keys and never send anything; the user confirms in their wallet. If they ask to trade and the tool is not available, say trading by chat opens soon.\n"
        "- Calendar: read only. Use calendar_events for plans and timing; combine with get_route when a place is given.",
        "- You remember the user's life, not just their questions. When they mention something personal that is coming up (training, a lesson, doctor, interview, meeting, exam, trip), plan a check-in with plan_follow_up or a reminder with follow_up=true, and say so in a few words. When they answer one of your check-ins, react like a friend in one or two lines, and save anything lasting with remember (e.g. 'karate on Tuesdays with daughter'). Bring up earlier things naturally when it fits, e.g. ask about the result of something they told you before. Never pry into health details. If they say stop asking, call set_check_ins with on=false.",
        "- Messages marked [OddsEngine announcement] in the history are official news from the team that you delivered. Answer questions about them from that text. If the user does not want them, call set_official_news with on=false.",
        "- If asked whether you are conscious or have feelings: be honest, you are an AI that remembers and follows up, not a person.",
        f"Now: {lok.strftime('%A %Y-%m-%d %H:%M')} user local time (UTC{_tz(adr):+g}).",
        f"User's wallets: {', '.join(ws)}",
        f"User tier: {_stufe(adr)}. Channel: {'Telegram' if quelle == 'tg' else 'web'}.",
        f"User's home: {(zustand(adr)['prefs'].get('home') or {}).get('label') or 'not saved yet (ask when weather or trips come up, then set_home)'}.",
        f"User's default chat model: {standard_modelle(adr)['text_model'] or 'none, you answer yourself'}. "
        f"Default image model: {standard_modelle(adr)['image_model']}.",
    ]
    zp = zustand(adr)["prefs"]
    sp = _sprache(adr)
    if sp:
        zeilen.append(f"LANGUAGE: always answer in {sp}, whatever language the user writes in, unless they ask you to switch "
                      "(then call set_language). This overrides the language rule above.")
    elif _schalter("lang", adr):
        zeilen.append("LANGUAGE: no language saved yet. If the user writes in a language other than English, call set_language "
                      "once with that language so your briefing, alerts and news reach them in it too. Do not mention that you saved it.")
    if _schalter("voice", adr):
        zeilen.append("VOICE: " + ("voice replies are ON: when the user sent a voice note, your answer is also sent as a voice note, so write "
                                   "it to sound good spoken: short sentences, no lists of numbers, say addresses as 'the address'."
                                   if zp.get("voice") else "you can talk back with voice notes. If the user asks you to speak, answer by "
                                   "voice or turn it on, call set_voice_replies with on=true."))
    if not zp.get("onboarded"):
        zeilen.append(
            "NEW USER SETUP (do this before anything else, one step per message, friendly and short): "
            "1) If you do not know how they want to be called, greet them and ask that first" + (
                " and, in the same short message, which language they want you to use (save it with set_language). "
                if _schalter("lang", adr) and not zp.get("lang") else ". ") +
            "2) Then ask what they want in their morning briefing and when. Offer the options as short lines: "
            "weather where they live, their calendar, important mail, traffic for a regular trip, news they follow, "
            "Torch rewards they can claim (needs their Torch profile name, ask for it if they pick this), today's weather market calls, crypto prices and edges, their bags, top Torch movers. "
            "Ask for the time and their city (for the time zone and the weather; save the city with set_home). "
            "3) Then call set_briefing with hour, utc_offset_hours and parts, and confirm in one line what they will get. "
            "If they ask something else first, answer it, then come back to the setup once. If they skip, call finish_onboarding.")
    try:
        wissen = _bewusstsein(adr)
    except Exception:
        wissen = []
    if wissen:
        zeilen.append("What is going on in the user's life right now:")
        zeilen += ["- " + w for w in wissen]
    if _scan_an(adr):
        zeilen.append("- Project research: when the user wants the project checked (website, team, socials, X talk, GitHub), call project_research. "
                      "Offer it after a token report when the coin is young.")
        zeilen.append("- Tokens on ANY chain (this replaces the token rule above): when the user pastes a contract address, a Solana mint, "
                      "a chart link or names a ticker, call token_info at once. The report card with the verdict and buttons is shown "
                      "to them automatically, so your answer adds at most two short lines: the one thing that matters most, then offer "
                      "'Watch it' (live alerts on price moves, dev sells, LP pulls, top holder dumps) or a buy with trade. If a deep scan "
                      "is still running, say the rest arrives in about a minute. Never paste the numbers of the card again.")
    if pausiert:
        zeilen.append("Memory is paused by the user: do not call remember.")
    if items:
        zeilen.append("What you know about the user (memory ids in brackets):")
        zeilen += [f"- [{i.get('id')}] ({i.get('cat')}) {i.get('text')}" for i in items[-40:]]
    return "\n".join(zeilen)


def _modell(text):
    if len(text) > 600 or TIEF.search(text or ""):
        return DEEP
    return FAST


class KeinGuthaben(Exception):
    pass


class NichtBereit(Exception):
    pass


def _min():
    return _f("MOTOR_MIN_CREDIT", 0.05)


def bereit(adr):
    """Your Engine laeuft nur mit Wallet (Sitzung), verknuepftem Telegram und Guthaben in $ODDS.
    Alles, was Your Engine tut, geht von diesem Guthaben ab."""
    adr = holder.norm(adr)
    tg = _tg_von(adr) is not None
    try:
        kt = ai.konto(adr, anlegen=False) or {}
        credit = float(kt.get("credit") or 0.0)
        eingezahlt = float(kt.get("deposited") or 0.0)
    except Exception:
        credit, eingezahlt = 0.0, 0.0
    fehlt = []
    if not tg:
        fehlt.append("telegram")
    if credit < _min():
        fehlt.append("balance")
    return {"ready": not fehlt, "telegram": tg, "credit": round(credit, 4), "paid_before": eingezahlt > 0,
            "min_usd": _min(), "missing": fehlt,
            "trial_left": probe_rest(adr) if (tg and "balance" in fehlt) else 0,
            "trial_total": int(_f("MOTOR_TRIAL", 10))}


# Seit 2.39 (Schalter "scan"): token_info und trade fuer jede Chain.
TOKEN_ALLE = _fn("token_info", "Full report on any token on any chain: Solana (pump.fun and all), Base, Ethereum, BNB Chain, Arbitrum, Robinhood Chain. Takes a contract address, a mint, a chart link (DexScreener, pump.fun, Birdeye...) or a ticker. Returns a verdict, market (price, MC, liquidity, volume, buys/sells), safety (honeypot, taxes, mint/freeze, LP lock, owner), supply (top 10, bundles, fresh wallets, same-funder clusters, snipers, holders), dev wallet and their earlier launches, smart wallets among the first buyers. The report card with buttons is shown to the user automatically. Call it as soon as the user pastes an address or link.", {"token": S}, ["token"])
WALLET_INTEL = _fn("wallet_intel", "Our record on any wallet (Solana or EVM): its score (0-99, how often and how early it was in coins that ran), rank among all scored wallets, and which coins it was early in with how far they went. Use it when the user asks if a wallet is smart money, a good trader, a dev or worth following.", {"address": S}, ["address"])
SIGNALE = _fn("scanner_signals", "Coins that people checked with Your Engine and that went on to run, with the multiple from the first check to the peak. Use it when the user asks what ran, what is hot, or what Your Engine caught early. Not buy calls.", {"hours": N, "count": N})
PROJEKT = _fn("project_research", "Deep research on a token's project: reads its website and docs (domain age, whether the contract on the site matches, terms, dead links, copied site text, partners that name it back), checks the claims on the site against the chain, the X account (followers, age) and what people on X are saying right now, the Telegram group, the GitHub (does it exist, age, last change, commits, a copy?) and paid DexScreener visibility. Takes 20 to 60 seconds and costs a little from the balance. Use it when the user asks to research, check the project, the team, the socials, the website, GitHub or what X says about a coin. The result card is shown to the user automatically.", {"token": S}, ["token"])
TRADE_ALLE = _fn("trade", "Buy or sell a token from the user's OWN wallet on Solana, Base, Ethereum, BNB Chain, Arbitrum or Robinhood Chain. Non-custodial: you only prepare it, the user confirms in their own wallet (Phantom or Solflare on Solana, MetaMask, Rabby or any EVM wallet elsewhere) with the button that is attached to your answer automatically. side 'buy' or 'sell'. Buy: amount with unit 'usd' (default) or 'native' (SOL, ETH or BNB). Sell: unit 'percent' (default 100), 'token' or 'usd'. token: contract address or mint (best), or a ticker you got from token_info. Before a buy of a coin you have not checked in this chat, call token_info first and mention the main risk in one line. Then show: you pay, you get (estimate), fees, price impact and warnings, and say they confirm with the button. Never say a trade happened before the user confirmed.", {"side": {"type": "string", "enum": ["buy", "sell"]}, "token": S, "amount": N, "unit": {"type": "string", "enum": ["usd", "native", "eth", "sol", "token", "percent"]}, "slippage_pct": N}, ["side", "token"])

# ── Probenachrichten (seit 2.33) ─────────────────────────────────────────
# Jedes Telegram-Konto bekommt MOTOR_TRIAL Nachrichten (Standard 10) ohne Guthaben.
# Gezaehlt je Telegram-Konto, nicht je Wallet: neue Wallets kosten nichts, Telegram-Konten
# brauchen eine Telefonnummer. Nur der schnelle Kopf mit den Daten-Werkzeugen, keine Bilder,
# keine Websuche, keine anderen Modelle, keine Erinnerungen. Tagesdeckel fuer alle zusammen:
# MOTOR_TRIAL_DAY_USD (Standard 3 $). Die Kosten traegt OddsEngine.

PROBE_WEG = {"web_search", "x_sentiment", "generate_image", "ask_model", "set_default_model", "set_reminder", "get_route", "add_commute", "follow_news",
             "set_price_alert", "watch_page", "remove_item", "set_signal_alert", "mail_draft", "watch_token", "trade", "my_trades", "plan_follow_up", "set_check_ins"}


def _tg_id(adr):
    u = _tg_von(adr)
    return str(u.get("tg")) if u and u.get("tg") is not None else None


def probe_rest(adr):
    tid = _tg_id(adr)
    if not tid:
        return 0
    try:
        n = int(storage.kv_get("motor_trial_tg_" + tid) or 0)
    except Exception:
        n = 0
    return max(0, int(_f("MOTOR_TRIAL", 10)) - n)


def _probe_topf_frei():
    tag = time.strftime("%Y%m%d", time.gmtime())
    try:
        heute = float(storage.kv_get("motor_trial_usd_" + tag) or 0)
    except Exception:
        heute = 0.0
    return heute < _f("MOTOR_TRIAL_DAY_USD", 3.0)


def _probe_buchen(adr, usd):
    tid = _tg_id(adr)
    tag = time.strftime("%Y%m%d", time.gmtime())
    try:
        if tid:
            n = int(storage.kv_get("motor_trial_tg_" + tid) or 0)
            storage.kv_set("motor_trial_tg_" + tid, str(n + 1))
        k = "motor_trial_usd_" + tag
        storage.kv_set(k, str(round(float(storage.kv_get(k) or 0) + usd, 6)))
    except Exception as e:
        log.warning(f"motor probe buchen: {e}")


def _probe_kosten_nur(usd):
    tag = time.strftime("%Y%m%d", time.gmtime())
    try:
        k = "motor_trial_usd_" + tag
        storage.kv_set(k, str(round(float(storage.kv_get(k) or 0) + usd, 6)))
    except Exception:
        pass


def _roh(body):
    """Anfrage auf Kosten von OddsEngine (nur Probe). Gibt (antwort, kosten_usd) zurueck."""
    neu = dict(body)
    neu["usage"] = {"include": True}
    neu["stream"] = False
    r = requests.post(ai.OR_BASE + "/chat/completions", headers=ai._or_kopf(),
                      data=json.dumps(neu), timeout=120)
    d = r.json()
    if r.status_code >= 400 or "error" in d:
        raise ai.Fehler(502, str((d.get("error") or {}).get("message") or "upstream error")[:200],
                        "upstream_error")
    try:
        kosten = float((d.get("usage") or {}).get("cost") or 0)
    except Exception:
        kosten = 0.0
    return d, kosten


TG_FEHLT = ("Link your Telegram first. That is how Your Engine knows it is you and how it reaches you "
            "with briefings and alerts. Tap Link Telegram on engine.oddsengine.live.")
PAUSE = ("Your balance is empty, so I paused your briefing, reminders, alerts and watched pages. "
         "Top up with $ODDS on engine.oddsengine.live and everything starts again on its own.")


def _klartext(t):
    """Telegram zeigt Markdown nicht an: **fett**, __x__, # Ueberschriften und `code` werden zu Klartext."""
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t or "", flags=re.S)
    t = re.sub(r"__(.+?)__", r"\1", t, flags=re.S)
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", t)
    t = re.sub(r"`([^`\n]+)`", r"\1", t)
    t = re.sub(r"(?m)^(\s*)[*]\s+", r"\1- ", t)
    return t


def _striche(t):
    t = re.sub(r"(?<=\d)[—–](?=\d)", "-", t)
    t = re.sub(r"\s*[—–]\s*", ", ", t)
    return t.replace(" ,", ",")


def frage(adr, text, quelle="web", bilder=None):
    """Ein Gespraechsschritt. Gibt {"text", "cost_usd", "model", "tools"} zurueck.
    bilder: Liste von data:-URLs (Fotos, Screenshots). Seit 2.39.1 versteht Your Engine Bilder."""
    adr = holder.norm(adr)
    text = str(text or "").strip()[:4000]
    bilder = [b for b in (bilder or []) if isinstance(b, str) and b.startswith("data:image/")][:4]
    if bilder and not text:
        text = "Look at this and tell me what matters."
    if not text:
        return {"text": "Say something and I am on it."}
    r = bereit(adr)
    if "telegram" in r["missing"]:
        raise NichtBereit(TG_FEHLT)
    if not bilder and _scan_an(adr):
        frei = _freier_scan(adr, text, quelle)
        if frei:
            return frei
    probe = False
    if "balance" in r["missing"]:
        if r.get("trial_left", 0) > 0 and _probe_topf_frei():
            probe = True
        else:
            raise KeinGuthaben()
    modell = FAST if probe else _kopf_modell(zustand(adr)["prefs"].get("brain") or "auto", text)
    if bilder and not probe:
        # Bilder: immer ein starkes Modell mit Augen (Standard wie Smart), "max" bleibt max
        modell = MAXI if (zustand(adr)["prefs"].get("brain") == "max") else (os.getenv("MOTOR_VISION", "").strip() or DEEP)
    werkzeuge = [w for w in WERKZEUGE if w["function"]["name"] not in PROBE_WEG] if probe else WERKZEUGE
    if not _handel_an(adr):
        werkzeuge = [w for w in werkzeuge if w["function"]["name"] not in ("trade", "my_trades")]
    if not _schalter("lang", adr):
        werkzeuge = [w for w in werkzeuge if w["function"]["name"] != "set_language"]
    if not _schalter("voice", adr):
        werkzeuge = [w for w in werkzeuge if w["function"]["name"] != "set_voice_replies"]
    if not _schalter("smart_calls", adr):
        werkzeuge = [w for w in werkzeuge if w["function"]["name"] not in ("set_smart_calls", "smart_calls_record")]
    if not _schalter("guard", adr):
        werkzeuge = [w for w in werkzeuge if w["function"]["name"] not in ("wallet_guard", "wallet_check")]
    if _scan_an(adr):
        werkzeuge = [TOKEN_ALLE if w["function"]["name"] == "token_info" else
                     TRADE_ALLE if w["function"]["name"] == "trade" else w for w in werkzeuge]
        werkzeuge = werkzeuge + [WALLET_INTEL, SIGNALE] + ([] if probe else [PROJEKT])
    hist = verlauf(adr)[-HIST:]
    rein = []
    for h in hist:
        inhalt = h.get("content") or ""
        for c in h.get("cards") or []:
            zl = [z for z in str(c.get("text") or "").splitlines() if z.strip()]
            if zl:
                inhalt += f"\n[Report card shown to the user: {zl[0]} on {zl[1] if len(zl) > 1 else ''}, address {zl[-1]}]"
        rein.append({"role": h.get("role"), "content": inhalt})
    system = _system(adr, quelle)
    if probe:
        system += ("\nFREE TRIAL: the user has no balance yet and uses free trial messages. You can read "
                   "the chain, prices, markets and the calls. Images, web search, other AI models, reminders, "
                   "alerts and watched pages unlock after a top up with $ODDS on "
                   "engine.oddsengine.live. If they ask for one of those, say that in one line. You can set up their morning "
                   "briefing now (set_briefing); tell them it starts arriving once they top up.")
    if bilder:
        system += ("\nIMAGES: the user sent " + ("a picture" if len(bilder) == 1 else f"{len(bilder)} pictures") +
                   ". Look closely and do what they ask: read text, numbers, charts, tickers, contract addresses, wallet "
                   "screens, receipts, documents, errors, objects, places, food, plants, anything. If you see a contract "
                   "address or a ticker of a coin, call token_info with it. If you see a chart, say what it shows (trend, "
                   "levels, volume) without predicting prices. If it is a screenshot of a bug, say what went wrong and how to "
                   "fix it. Be concrete, start with what matters most.")
        inhalt = [{"type": "text", "text": text}] + [{"type": "image_url", "image_url": {"url": b}} for b in bilder]
    else:
        inhalt = text
    if str(modell).startswith("anthropic/"):
        # Prompt-Cache: der lange Systemtext wird in jeder Werkzeug-Runde nur einmal voll bezahlt
        sys_msg = {"role": "system", "content": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]}
    else:
        sys_msg = {"role": "system", "content": system}
    msgs = [sys_msg] + rein + [{"role": "user", "content": inhalt}]
    kosten, benutzt, probe_kosten = 0.0, [], 0.0
    ctx = {"images": [], "data": [], "buttons": [], "cards": []}
    antwort = None
    for runde in range(MAX_RUNDEN):
        body = {"model": modell, "messages": msgs, "max_tokens": 900 if probe else int(os.getenv("MOTOR_MAX_TOKENS", "2200")), "tools": werkzeuge}
        if runde == MAX_RUNDEN - 1 or (probe and runde == 4):
            body.pop("tools")
        if probe:
            d, k_roh = _roh(body)
            probe_kosten += k_roh
        else:
            d = ai.anfrage(adr, None, body)
            kosten += float((d.get("usage") or {}).get("oddsengine_charged_usd") or 0)
        wahl = (d.get("choices") or [{}])[0]
        msg = (wahl.get("message") or {})
        calls = msg.get("tool_calls") or []
        if not calls:
            antwort = msg.get("content") or ""
            if wahl.get("finish_reason") == "length" and antwort and not probe:
                # Abgeschnitten: einmal fortsetzen lassen statt mitten im Satz aufzuhoeren
                try:
                    d2 = ai.anfrage(adr, None, {"model": modell, "max_tokens": 1200, "messages": msgs + [
                        {"role": "assistant", "content": antwort},
                        {"role": "user", "content": "Continue exactly where you stopped and finish the answer briefly. Do not repeat anything."}]})
                    kosten += float((d2.get("usage") or {}).get("oddsengine_charged_usd") or 0)
                    mehr = (((d2.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                    if mehr:
                        antwort = antwort.rstrip() + (" " if not antwort.endswith("\n") else "") + mehr
                except Exception as e:
                    log.warning(f"motor fortsetzen: {e}")
            elif wahl.get("finish_reason") == "length" and antwort:
                antwort = re.sub(r"[^.!?\n]*$", "", antwort).rstrip() or antwort
            break
        msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for c in calls[:6]:
            fn = (c.get("function") or {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            r = _ausfuehren(adr, fn.get("name"), args, ctx)
            if isinstance(r, dict) and r.get("cost_usd"):
                kosten += float(r["cost_usd"] or 0)
            benutzt.append(fn.get("name"))
            msgs.append({"role": "tool", "tool_call_id": c.get("id"), "content": json.dumps(r, default=str)[:int(os.getenv("MOTOR_TOOL_CHARS", "9000"))]})
    antwort = _striche(antwort or "I could not finish that one. Try asking a bit differently.")
    rest = None
    if probe:
        _probe_buchen(adr, probe_kosten)
        rest = probe_rest(adr)
        if rest <= 3:
            antwort += ("\n\n" + (f"{rest} free message{'s' if rest != 1 else ''} left." if rest else "That was your last free message.")
                        + " Top up with $ODDS on engine.oddsengine.live to keep going and unlock images, web search and every AI model.")
    a_eintrag = {"role": "assistant", "content": antwort}
    if ctx.get("cards"):
        a_eintrag["cards"] = [{"text": c["text"], "buttons": c.get("buttons")} for c in ctx["cards"]][:3]
    if ctx["images"]:
        a_eintrag["images"] = ctx["images"]
    hist = (hist + [{"role": "user", "content": (f"[sent {len(bilder)} photo{'s' if len(bilder) > 1 else ''}] " if bilder else "") + text},
                    a_eintrag])[-HIST:]
    _set("motor_h_" + adr, hist)
    try:
        import aktiv
        aktiv.merken(adr, "chat", f"{quelle} · {modell} · {', '.join(benutzt[:5])} · {text[:120]}", kosten)
    except Exception:
        pass
    return {"text": antwort, "cost_usd": round(kosten, 6), "model": modell, "tools": benutzt,
            "images": ctx["images"], "image_data": ctx["data"], "trial": probe, "trial_left": rest,
            "buttons": ctx.get("buttons") or [], "cards": ctx.get("cards") or [],
            "open_report": ctx.get("open_report"), "deep_running": bool(ctx.get("deep_running"))}


_FREI = {}
_SCAN_WORTE = {"check", "scan", "ca", "token", "coin", "this", "what", "about", "pls", "please", "look",
               "report", "analyse", "analyze", "is", "it", "the", "and", "gm", "hey", "yo", "bro", "was", "ist", "das",
               "pruef", "pruefen", "schau", "checken", "mal", "bitte", "den", "die", "der", "dieser", "diesen"}


def _nur_ca(text):
    """Ist die Nachricht nur ein Contract, ein Mint oder ein Chart-Link (hoechstens ein, zwei Worte dazu)?"""
    import scan
    t = str(text or "").strip()
    if not t or len(t) > 300:
        return None
    m = re.fullmatch(r"(?:(?:scan|check)\s+)?\$([A-Za-z][A-Za-z0-9]{1,14})", t, re.I)
    if m:
        return m.group(1)  # "$PONS" oder "scan $WIF": Ticker ueber den ganzen Markt
    hinweis, a = scan.lesen(t)
    if not a or not (scan.EVM_RE.fullmatch(a) or scan.SOL_RE.fullmatch(a)):
        return None
    rest = re.sub(r"https?://\S+", " ", t)
    rest = re.sub(re.escape(a), " ", rest, flags=re.I)
    rest = re.sub(r"\b(?:" + "|".join(scan.NAMEN) + r"):", " ", rest, flags=re.I)
    worte = {w.lower() for w in re.findall(r"[A-Za-z\u00c0-\u024f]{2,}", rest)}
    if worte - _SCAN_WORTE:
        return None
    return (hinweis + ":" + a) if hinweis else a


def _freier_scan(adr, text, quelle):
    """CA-Scans sind kostenlos (seit 2.40): Karte direkt, ohne Sprachmodell. Hoechstens SCAN_FREE_PER_HOUR je Stunde."""
    import scan
    q = _nur_ca(text)
    if not q:
        return None
    grenze = int(os.getenv("SCAN_FREE_PER_HOUR", "40"))
    jetzt = time.time()
    liste = [x for x in _FREI.get(adr, []) if jetzt - x < 3600]
    if len(liste) >= grenze:
        return None
    _FREI[adr] = liste + [jetzt]
    r = scan.analyse(q)
    if r.get("ambiguous"):
        # Mehrere echte Coins mit dem Ticker: nicht raten, nach dem Contract fragen (kostenlos, ohne Modell)
        zeilen = [f"There are several coins called ${r['symbol']}. Which one do you mean? Send me the contract address."]
        for c in r["candidates"]:
            zeilen.append(f"{c['chain']}: {c['address']} (MC {scan.usd(c['mcap_usd'])}, 24h vol {scan.usd(c['volume_24h_usd'])})")
        antwort = "\n".join(zeilen)
        _set("motor_h_" + adr, (verlauf(adr) + [{"role": "user", "content": text}, {"role": "assistant", "content": antwort}])[-HIST:])
        return {"text": antwort, "cost_usd": 0.0, "model": None, "tools": ["scan"], "images": [], "image_data": [],
                "trial": False, "trial_left": None, "buttons": [], "cards": [], "free": True}
    if not r.get("ok", True) or r.get("error"):
        return None
    try:
        scan.nachfassen_merken(adr, r)
    except Exception:
        pass
    tief = (r.get("deep") or {}).get("state") == "running"
    antwort = ("Deep scan is running, it shows up here and in the full report in about a minute. " if tief else "")
    antwort += "Ask me anything about it, or tap Full report for the chart and the holder map."
    antwort = _striche(antwort)
    karte = {"text": r["card"], "html": r.get("card_html"), "buttons": scan.knoepfe(r)}
    hist = (verlauf(adr) + [{"role": "user", "content": text}, {"role": "assistant", "content": antwort, "cards": [karte]}])[-HIST:]
    _set("motor_h_" + adr, hist)
    try:
        import aktiv
        aktiv.merken(adr, "scan", f"{r.get('chain')}:{r.get('address')} ${r.get('symbol') or ''} {quelle}")
    except Exception:
        pass
    return {"text": antwort, "cost_usd": 0.0, "model": None, "tools": ["scan"], "images": [], "image_data": [],
            "trial": False, "trial_left": None, "buttons": [], "cards": [karte], "free": True,
            "open_report": f"{r['chain']}:{r['address']}", "deep_running": tief}


LEER = ("Your balance is empty, so I cannot answer right now. Everything I do comes off your "
        "balance. Top up with $ODDS on engine.oddsengine.live (Account), then write me again.")


# ── Hintergrund ──────────────────────────────────────────────────────────

_T = {"alarm": 0.0, "watch": 0.0, "brief": 0.0, "weg": 0.0, "news": 0.0}


def _nutzer():
    return list(_j("motor_users", []))


_BEREIT = {}


def _darf(adr):
    """Hintergrund laeuft nur mit Telegram und Guthaben. Leer: einmal Bescheid, dann Pause."""
    if not offen(adr):
        return False
    c = _BEREIT.get(adr)
    if c and time.time() - c[0] < 30:
        return c[1]
    r = bereit(adr)
    z = zustand(adr)
    if r["ready"]:
        if z["prefs"].get("pause_info"):
            z["prefs"].pop("pause_info", None)
            _zustand_speichern(adr, z)
    elif (r["telegram"] and "balance" in r["missing"] and r.get("paid_before")
          and not z["prefs"].get("pause_info")):
        z["prefs"]["pause_info"] = int(time.time())
        _zustand_speichern(adr, z)
        senden(adr, PAUSE)
    _BEREIT[adr] = (time.time(), r["ready"])
    return r["ready"]


def _erinnerungen():
    jetzt = time.time()
    for adr in _nutzer():
        if not _darf(adr):
            continue
        z = zustand(adr)
        faellig = [r for r in z["reminders"] if r["at"] <= jetzt]
        if not faellig:
            continue
        z["reminders"] = [r for r in z["reminders"] if r["at"] > jetzt]
        for r in faellig:
            if r.get("repeat"):
                z["reminders"].append(dict(r, at=_naechster(adr, r)))
        _zustand_speichern(adr, z)
        for r in faellig:
            senden(adr, "Reminder\n\n" + r["text"])
            if r.get("follow_up"):
                _nachfrage_neu(adr, r["text"], r["at"] + int(r.get("dur") or 90) * 60, "reminder")


def _alarme():
    for adr in _nutzer():
        if not _darf(adr):
            continue
        z = zustand(adr)
        if not z["alerts"]:
            continue
        bleiben, geaendert = [], False
        for a in z["alerts"]:
            p = preis(a.get("token") or a["asset"])
            v = p.get("usd")
            if v is None:
                bleiben.append(a)
                continue
            if (a.get("above") is not None and v >= a["above"]) or (a.get("below") is not None and v <= a["below"]):
                grenze = a["above"] if a.get("above") is not None else a["below"]
                richtung = "above" if a.get("above") is not None else "below"
                senden(adr, f"Price alert\n\n{a['asset']} is at ${v:,.8g}, {richtung} your ${grenze:,.8g}.")
                geaendert = True
            else:
                bleiben.append(a)
        if geaendert:
            z = zustand(adr)
            z["alerts"] = [a for a in z["alerts"] if a["id"] in {b["id"] for b in bleiben}]
            _zustand_speichern(adr, z)


# ── Live-Uebersicht eines Tokens in Telegram (seit 2.37.1) ─────────────
# "Zeig mir den Coin live": alle N Minuten eine kurze Zeile mit Preis, Aenderung seit Start,
# Marktwert, Volumen, Kaeufe/Verkaeufe, Holder. Bei einer Bewegung ab move_pct sofort.
# Ohne KI, hoechstens 3 gleichzeitig, hoechstens 4 Stunden.

def _tok_zahlen(t):
    p = onchain._dex(t) or {}
    tx = (p.get("txns") or {}).get("m5") or {}
    try:
        px = float(p.get("priceUsd")) if p.get("priceUsd") else None
    except Exception:
        px = None
    return {"price": px, "mc": p.get("marketCap") or p.get("fdv"), "vol_h1": (p.get("volume") or {}).get("h1"),
            "buys5": tx.get("buys"), "sells5": tx.get("sells"), "chg_h1": (p.get("priceChange") or {}).get("h1"),
            "url": p.get("url"), "sym": ((p.get("baseToken") or {}).get("symbol"))}


def _usd_kurz(v):
    try:
        v = float(v)
    except Exception:
        return "n/a"
    return f"${v/1e6:.2f}M" if v >= 1e6 else f"${v/1e3:.1f}K" if v >= 1e3 else f"${v:,.0f}"


def _px(v):
    if v is None:
        return "n/a"
    return f"${v:.8f}".rstrip("0") if v < 0.01 else f"${v:,.4f}"


def watch_token(adr, token, minutes=60, every=5, move_pct=10):
    """Live-Wache. Seit 2.39 fuer jede Chain, mit Sofort-Alarm bei Dev-Verkauf, LP-Abzug und Top-Holder-Dump."""
    if _scan_an(adr):
        import scan
        r = scan.analyse(token, deep=False)
        if not r.get("ok"):
            return r
        chain, t, sym = r["chain"], r["address"], r.get("symbol") or scan.kurz(r["address"])
        zn = scan.zahlen(chain, t)
        dev = (r.get("dev") or {}).get("address")
        tops = [x["address"] for x in (r.get("supply") or {}).get("top") or []
                if x.get("address") and not x.get("pool") and not x.get("locked") and not x.get("account")
                and str(x["address"]).lower() != str(dev or "").lower()][:3]
        chart = r["links"].get("chart") or r["links"].get("page")
    else:
        r = onchain.token(token)
        if r.get("error"):
            return r
        chain, t, sym, dev, tops = "robinhood", r["token"], r.get("symbol") or r["token"][:8], None, []
        zn = _tok_zahlen(t)
        chart = zn.get("url") or r.get("chart") or r.get("trade")
    z = zustand(adr)
    z["tokwatch"] = [w for w in z["tokwatch"] if w["until"] > time.time() and w.get("token") != t]
    if len(z["tokwatch"]) >= 3:
        return {"ok": False, "why": "up to 3 live watches at once. Stop one first (remove_item kind token_watch)."}
    try:
        minutes = max(5, min(int(minutes or 60), 240))
        every = max(1, min(int(every or 5), 60))
        move_pct = max(3.0, min(float(move_pct or 10), 80.0))
    except Exception:
        minutes, every, move_pct = 60, 5, 10.0
    w = {"id": secrets.token_hex(3), "chain": chain, "token": t, "sym": sym,
         "start_px": zn.get("price"), "last_px": zn.get("price"),
         "start": int(time.time()), "until": int(time.time() + minutes * 60), "every": every, "move": move_pct,
         "next": int(time.time() + every * 60), "chart": chart, "liq_ref": zn.get("liq"),
         "dev": dev, "tops": tops, "bal": {}, "bal_ts": 0}
    if _scan_an(adr) and (dev or tops):
        try:
            import scan
            b = scan.bestaende(chain, t, [x for x in [dev] + tops if x])
            w["bal"] = {k: v for k, v in b.items()}
            w["bal_ts"] = int(time.time())
        except Exception:
            pass
    z["tokwatch"].append(w)
    _zustand_speichern(adr, z)
    wache = ["price moves of " + f"{move_pct:g}%"]
    if w.get("liq_ref"):
        wache.append("liquidity pulls")
    if dev and w["bal"].get(dev):
        wache.append("dev sells")
    if tops:
        wache.append("top holder dumps")
    return {"ok": True, "id": w["id"], "symbol": sym, "chain": chain, "updates_every_min": every, "for_min": minutes,
            "instant_alerts_on": wache, "chart": chart,
            "delivery": "Telegram" if _tg_von(adr) else "the Your Engine tab"}


def _wache_alarme(w, zn):
    """Sofort-Alarme ausser Preis: LP-Abzug, Dev-Verkauf, Top-Holder-Dump. Gibt Textzeilen zurueck."""
    out = []
    liq = zn.get("liq")
    ref = w.get("liq_ref")
    if liq is not None and ref and ref > 1000 and liq < ref * 0.6:
        out.append(f"Liquidity fell from {_usd_kurz(ref)} to {_usd_kurz(liq)}. Possible LP pull.")
        w["liq_ref"] = liq
    elif liq and (not ref or liq > ref):
        w["liq_ref"] = liq
    if not (w.get("dev") or w.get("tops")) or time.time() - (w.get("bal_ts") or 0) < 55:
        return out
    try:
        import scan
        jetzt = scan.bestaende(w.get("chain") or "robinhood", w["token"], [x for x in [w.get("dev")] + (w.get("tops") or []) if x])
    except Exception:
        return out
    w["bal_ts"] = int(time.time())
    sup = jetzt.get("_supply") or w.get("bal", {}).get("_supply") or 0
    alt = w.get("bal") or {}
    for wer in [x for x in [w.get("dev")] + (w.get("tops") or []) if x]:
        a, n = alt.get(wer), jetzt.get(wer)
        if a is None or n is None or a <= 0 or n >= a:
            if n is not None:
                alt[wer] = max(n, a or 0) if a else n
            continue
        weg = a - n
        anteil = weg / sup * 100 if sup else None
        if wer == w.get("dev"):
            if n < a * 0.9 and (anteil is None or anteil >= 0.2):
                out.append(f"Dev just sold {100 - n / a * 100:.0f}% of their bag" + (f" ({anteil:.1f}% of supply)" if anteil else "")
                           + (f", now holds {n / sup * 100:.2f}%." if sup else "."))
                alt[wer] = n
        elif n < a * 0.5:
            out.append(f"Top holder {wer[:4]}…{wer[-4:]} just dumped {100 - n / a * 100:.0f}% of their bag"
                       + (f" ({anteil:.1f}% of supply)." if anteil else "."))
            alt[wer] = n
    alt["_supply"] = sup or alt.get("_supply")
    w["bal"] = alt
    return out


def _token_wache():
    jetzt = time.time()
    for adr in _nutzer():
        z = zustand(adr)
        if not z.get("tokwatch") or not _darf(adr):
            continue
        geaendert = False
        rest = []
        for w in z["tokwatch"]:
            ende = jetzt >= w["until"]
            if w.get("chain") and _scan_an(adr):
                import scan
                zn = scan.zahlen(w["chain"], w["token"])
                alarme = _wache_alarme(w, zn)
                geaendert = True
            else:
                zn = _tok_zahlen(w["token"])
                alarme = []
            px = zn.get("price")
            sprung = (px and w.get("last_px") and abs(px / w["last_px"] - 1) * 100 >= w["move"])
            if alarme:
                senden(adr, f"⚠️ ${w['sym']}\n" + "\n".join(alarme) + (f"\nPrice {_px(px)} · MC {_usd_kurz(zn.get('mc'))}" if px else "")
                       + (f"\n{w['chart']}" if w.get("chart") else ""), [[("Stop watching", f"cb:x:{w['id']}")]])
            if ende or sprung or jetzt >= w["next"]:
                seit = (px / w["start_px"] - 1) * 100 if px and w.get("start_px") else None
                min_in = int((jetzt - w["start"]) / 60)
                kopf = (f"${w['sym']} moved {((px / w['last_px'] - 1) * 100):+.1f}% just now" if sprung and not ende
                        else f"${w['sym']} live, final update after {min_in} min" if ende
                        else f"${w['sym']} live, {min_in} min in")
                zeilen = [kopf, f"Price {_px(px)}" + (f" ({seit:+.1f}% since you started)" if seit is not None else ""),
                          f"MC {_usd_kurz(zn.get('mc'))} · Liq {_usd_kurz(zn.get('liq'))} · Vol 1h {_usd_kurz(zn.get('vol_h1'))}"
                          if zn.get("liq") is not None else f"MC {_usd_kurz(zn.get('mc'))} · Vol 1h {_usd_kurz(zn.get('vol_h1'))}"]
                if zn.get("buys5") is not None:
                    zeilen.append(f"Last 5 min: {zn['buys5']} buys, {zn['sells5']} sells")
                if w.get("chart"):
                    zeilen.append(w["chart"])
                if ende:
                    zeilen.append("Live watch ended. Say the word to start it again.")
                senden(adr, "\n".join(zeilen), None if ende else [[("Stop watching", f"cb:x:{w['id']}")]])
                w["last_px"] = px or w.get("last_px")
                w["next"] = int(jetzt + w["every"] * 60)
                geaendert = True
            if not ende:
                rest.append(w)
            else:
                geaendert = True
        if geaendert:
            z2 = zustand(adr)
            ids = {w["id"]: w for w in rest}
            z2["tokwatch"] = [ids[w["id"]] for w in z2.get("tokwatch", []) if w["id"] in ids]
            _zustand_speichern(adr, z2)


# ── Trading per Chat (seit 2.38, non-custodial, siehe handel.py) ───────

def _handel_an(adr):
    try:
        import features
        return features.an("trade") or beta(adr)
    except Exception:
        return False


def trade(adr, side, token, amount=None, unit=None, slippage_pct=None, ctx=None):
    import handel
    r = handel.vorschlag(adr, side, token, amount, unit, slippage_pct, alle=_scan_an(adr))
    if r.get("ok") and ctx is not None:
        ctx.setdefault("buttons", []).append(("Confirm in wallet", r["confirm_url"]))
    return r


def my_trades(adr):
    return {"trades": (zustand(adr).get("trades") or [])[-15:][::-1]}


# ── CA-Report fuer jede Chain (seit 2.39, siehe scan.py) ─────────────────

def _scan_an(adr):
    try:
        import features
        return features.an("scan") or beta(adr)
    except Exception:
        return False


def token_report(adr, token, ctx=None):
    """Der Report als Karte (mit Knoepfen) direkt vor der Antwort; das Modell bekommt die Zahlen kompakt."""
    import scan
    r = scan.analyse(token)
    if not r.get("ok"):
        if r.get("ambiguous") or r.get("major"):
            return r
        if not str(token or "").strip().startswith(("0x", "http")) and len(str(token or "")) < 20:
            alt = onchain.token(token)
            if not alt.get("error"):
                return alt
        return r
    if ctx is not None:
        ctx.setdefault("cards", []).append({"text": r["card"], "html": r.get("card_html"), "buttons": scan.knoepfe(r)})
        ctx["open_report"] = f"{r['chain']}:{r['address']}"
        ctx["deep_running"] = (r.get("deep") or {}).get("state") == "running"
    try:
        scan.nachfassen_merken(adr, r)
    except Exception as e:
        log.debug(f"motor nachfassen merken: {e}")
    return scan.fuer_modell(r)


def projekt_recherche(adr, token, ctx=None):
    import projekt
    if not _darf(adr):
        return {"error": "No balance for research. Top up first."}
    d = projekt.recherche(adr, token)
    if d.get("ok") is False or d.get("error"):
        return d
    try:
        import aktiv
        aktiv.merken(adr, "research", f"{d.get('chain')}:{d.get('token')} ${d.get('symbol') or ''}", d.get("cost_usd") if not d.get("cached") else 0)
    except Exception:
        pass
    if ctx is not None:
        ctx.setdefault("cards", []).append({"text": projekt.karte(d), "buttons": []})
    out = projekt.fuer_modell(d)
    if d.get("cost_usd") and not d.get("cached"):
        out["cost_usd"] = d["cost_usd"]
    return out


def _intel_verfolgen():
    import intel
    intel.verfolgen()
    try:
        if time.time() - float(storage.kv_get("iw_rescored") or 0) >= 3600:
            storage.kv_set("iw_rescored", str(time.time()))
            intel.neu_bewerten()
    except Exception as e:
        log.warning(f"motor neu bewerten: {e}")


def _tief_in_verlauf(adr, r):
    """Fertiger Tiefen-Scan auch in den Chat-Verlauf, damit er im Web-Chat erscheint (seit 2.44)."""
    import scan
    karte = {"text": r["card"], "buttons": scan.knoepfe(r)}
    eintrag = {"role": "assistant", "content": f"Deep scan for ${r.get('symbol') or ''} is in. Bundles, clusters and the holder map are ready in the full report.",
               "cards": [karte], "deep_for": f"{r['chain']}:{r['address']}"}
    _set("motor_h_" + adr, (verlauf(adr) + [eintrag])[-HIST:])


def _scan_nachfassen():
    import scan
    scan.nachfassen(senden, _tief_in_verlauf)


def knopf(adr, chat_id, data):
    """Telegram-Knoepfe: r:<id> Report neu, w:<id> live beobachten, x:<watch> Wache stoppen. Ohne KI-Kosten."""
    import agent
    import scan
    teile = str(data or "").split(":")
    art, wert = teile[0], (teile[1] if len(teile) > 1 else "")
    try:
        if art == "x":
            z = zustand(adr)
            vorher = len(z.get("tokwatch") or [])
            z["tokwatch"] = [w for w in z.get("tokwatch") or [] if w.get("id") != wert]
            _zustand_speichern(adr, z)
            agent._send(chat_id, "Stopped watching." if len(z["tokwatch"]) < vorher else "That watch already ended.")
            return
        if not _darf(adr):
            agent._send(chat_id, LEER)
            return
        info = scan.aus_id(wert)
        if not info:
            agent._send(chat_id, "That button expired. Paste the address again.")
            return
        q = f"{info['chain']}:{info['address']}"
        if art == "r":
            agent._tg("sendChatAction", chat_id=chat_id, action="typing")
            r = scan.analyse(q, deep=False, frisch=True)
            if r.get("ok"):
                agent._send(chat_id, r.get("card_html") or r["card"], scan.knoepfe(r), html=bool(r.get("card_html")))
            else:
                agent._send(chat_id, r.get("error") or "Could not refresh that one.")
        elif art == "p":
            import projekt
            agent._send(chat_id, f"Researching ${info.get('symbol') or ''}: website, docs, X, Telegram, GitHub. Takes up to a minute.")
            agent._tg("sendChatAction", chat_id=chat_id, action="typing")
            d = projekt.recherche(adr, q)
            agent._send(chat_id, projekt.karte(d) if not d.get("error") else d["error"])
        elif art == "w":
            res = watch_token(adr, q)
            if res.get("ok"):
                agent._send(chat_id, f"Watching ${res['symbol']} live: an update every {res['updates_every_min']} min for "
                                     f"{res['for_min']} min, and instantly on " + ", ".join(res["instant_alerts_on"]) + ".",
                            [[("Stop watching", f"cb:x:{res['id']}")]])
            else:
                agent._send(chat_id, res.get("why") or res.get("error") or "Could not start the watch.")
    except Exception as e:
        log.warning(f"motor knopf {art}: {e}")
        agent._send(chat_id, "Something went wrong on my side. Try again in a minute.")


def _beobachten():
    jetzt = time.time()
    takt = _f("MOTOR_WATCH_MIN", 30) * 60
    for adr in _nutzer():
        if not _darf(adr):
            continue
        z = zustand(adr)
        neu = False
        for w in z["watches"]:
            if jetzt - w.get("checked", 0) < takt:
                continue
            w["checked"] = int(jetzt)
            neu = True
            r = read_url(w["url"])
            if r.get("error"):
                continue
            h = hashlib.sha256(r["text"].encode()).hexdigest()
            if h == w.get("hash"):
                continue
            alt = storage.kv_get("motor_w_" + w["id"]) or ""
            w["hash"] = h
            w["changes"] = int(w.get("changes") or 0) + 1
            storage.kv_set("motor_w_" + w["id"], r["text"][:20000])
            alte = set(alt.splitlines())
            dazu = [z_ for z_ in r["text"].splitlines() if z_.strip() and z_ not in alte][:80]
            if not dazu:
                continue
            kurz = None
            try:
                d = ai.anfrage(adr, None, {"model": FAST, "max_tokens": 250, "messages": [
                    {"role": "system", "content": "A watched web page changed. Say in 2 to 4 short plain sentences what is new. No markdown."},
                    {"role": "user", "content": f"Page: {w.get('title') or w['url']}\nUser note: {w.get('note')}\nNew lines:\n" + "\n".join(dazu)[:6000]}]})
                kurz = ((d.get("choices") or [{}])[0].get("message") or {}).get("content")
            except Exception:
                kurz = None
            senden(adr, f"Page changed: {w.get('title') or w['url']}\n\n{kurz or chr(10).join(dazu[:8])[:900]}\n\n{w['url']}")
        if neu:
            z2 = zustand(adr)
            ids = {w["id"]: w for w in z["watches"]}
            z2["watches"] = [ids.get(w["id"], w) for w in z2["watches"]]
            _zustand_speichern(adr, z2)


def briefing_text(adr):
    s = _einst(adr)
    br = s.get("briefing") or {}
    teile = {"weather": True, "calendar": True, "mail": True, "commute": True, "news": True, "rewards": True,
             "calls": True, "crypto": True, "coins": True, "bags": True, "moving": False, "stocks": True}
    teile.update(br.get("parts") or {})
    lok = _lokal(adr)
    z = [f"Good morning. Your Engine, {lok.strftime('%A %b %d')}."]
    zz0 = zustand(adr)
    heim = zz0["prefs"].get("home")
    if teile.get("weather") and heim:
        try:
            zeile = orte.wetter_zeile(orte.wetter(heim["lat"], heim["lon"], _tz(adr)))
            if zeile:
                z += ["", "Weather: " + zeile]
        except Exception:
            pass
    if teile.get("calendar"):
        try:
            import kalender
            kz = kalender.briefing_zeilen(adr, _tz(adr))
            if kz:
                z += [""] + kz
        except Exception:
            pass
    if teile.get("mail"):
        try:
            import postfach
            mz = postfach.briefing_zeilen(adr)
            if mz:
                z += [""] + mz
        except Exception:
            pass
    if teile.get("commute"):
        heute_wege = [r for r in zz0["routes"] if lok.weekday() in r.get("days", [])]
        if heute_wege:
            z += ["", "Your trips today"]
            for r in heute_wege[:3]:
                zeile = None
                h_, m_ = map(int, r["time"].split(":"))
                bald = 0 <= ((lok.replace(hour=h_, minute=m_) - lok).total_seconds() / 3600) <= 2
                if bald and orte.verkehr_da() and _gebuehr(adr, _f("MOTOR_ROUTE_USD", 0.002)):
                    zeile = orte.route_zeile(r["name"], r["time"], orte.route(r["from"], r["to"], r.get("mode")))
                z.append(zeile or f"{r['name']} at {r['time']}. I check traffic before you leave.")
    if teile.get("calls"):
        try:
            c = engine_calls(adr)
            if c["calls"]:
                z += ["", "Engine calls"]
                for x in c["calls"][:5]:
                    z.append(f"{x['city']}: {' / '.join(x['block'] or [])} at {x['price_cents']}c, honest {x['honest_odds_pct']}%"
                             + ("" if x["grade"] == "bet" else " (read)"))
            elif not c["user_sees_all_calls"]:
                z += ["", "Engine calls: hold $ODDS to see every call live."]
            else:
                z += ["", "Engine calls: none open right now. The engine stays quiet when prices look fair."]
        except Exception:
            pass
    if teile.get("crypto"):
        try:
            k = crypto_markets()
            pr = k.get("prices_usd") or {}
            if pr:
                z += ["", "Crypto: " + "  ".join(f"{a.upper()} ${v:,.0f}" if v and v > 10 else f"{a.upper()} ${v:,.4f}"
                                                for a, v in list(pr.items())[:5] if v)]
            if k.get("edges_now"):
                z.append(f"{len(k['edges_now'])} window(s) with an edge now on terminal.oddsengine.live")
        except Exception:
            pass
    eigene_coins = zz0["prefs"].get("coins")
    if eigene_coins is None:
        eigene_coins = STANDARD_COINS  # ODDS (Token der Engine) und TORCH (das Paar) hat jeder automatisch
    if teile.get("coins") and eigene_coins:
        zl = []
        for c in eigene_coins[:12]:
            try:
                x = coin_zeile(c)
            except Exception:
                x = None
            if x:
                zl.append(x)
        if zl:
            z += ["", "Your coins"] + zl
    if teile.get("stocks") and zz0["prefs"].get("stocks"):
        try:
            import aktien
            ks = aktien.kurse(zz0["prefs"]["stocks"])
            if ks:
                z += ["", "Stocks: " + "  ".join(aktien.zeile(k) for k in ks)]
        except Exception:
            pass
    if teile.get("bags"):
        try:
            summe, top = 0.0, []
            for w in _wallets(adr)[:4]:
                h = onchain.holdings(w)
                summe += float(h.get("tokens_value_usd") or 0)
                top += h.get("tokens") or []
            gestern = s.get("_bags_prev")
            z += ["", f"Your bags on Robinhood Chain: ${summe:,.2f}"
                  + (f" ({(summe - gestern) / gestern * 100:+.1f}% since yesterday)" if gestern else "")]
            for t in sorted(top, key=lambda x: -(x["usd"] or 0))[:3]:
                if t.get("usd"):
                    z.append(f"{t['symbol']}: {t['amount']:,.0f} = ${t['usd']:,.2f}")
            s["_bags_prev"] = summe
            _einst_speichern(adr, s)
        except Exception:
            pass
    if teile.get("moving"):
        try:
            ls = onchain.launches()[-150:]
            pr = onchain.preise([l["token"] for l in ls])
            meta = onchain._meta([l["token"] for l in ls])
            top = sorted(((t, p) for t, p in pr.items() if p.get("vol24")), key=lambda x: -x[1]["vol24"])[:3]
            if top:
                z += ["", "Moving on Torch (24h volume)"]
                for t, p in top:
                    z.append(f"{meta.get(t, {}).get('symbol')}: ${p['vol24']:,.0f} volume, mcap ${p.get('fdv') or 0:,.0f}")
        except Exception:
            pass
    if teile.get("news", True):
        try:
            nz = _news_fuer_briefing(adr)
            if nz:
                z += ["", "Headlines"] + nz
        except Exception:
            pass
    if teile.get("rewards", True) and zustand(adr)["prefs"].get("torch"):
        try:
            tr = torch_rewards(adr)
            if tr.get("rewards"):
                offen = [x for x in tr["rewards"] if x["claimable"] > 0]
                if offen:
                    z += ["", f"Torch rewards to claim: about ${tr['claimable_usd_total']:,.2f}"]
                    for x in offen[:3]:
                        z.append(f"{x['coin']}: {x['claimable']:,.6g} {x['paid_in']}")
                    z.append("Claim on app.torchpad.fun/rewards")
        except Exception:
            pass
    zz = zustand(adr)
    heute = [r for r in zz["reminders"] if r["at"] - time.time() < 86400]
    if heute:
        z += ["", "Today: " + "; ".join(r["text"] for r in heute[:4])]
    z += ["", "Write me anytime. Not financial advice."]
    return "\n".join(z)


def _briefings():
    try:
        import agent
        nutzer = list(agent.alle())
    except Exception:
        return
    for u in nutzer:
        adr = u.get("wallet")
        if not adr or not _darf(adr):
            continue
        s = _einst(adr)
        if not (s.get("briefing") or {}).get("on", True):
            continue
        z = zustand(adr)
        lok = _lokal(adr)
        stunde = int(z["prefs"].get("hour", 8))
        tag = lok.strftime("%Y-%m-%d")
        if z["prefs"].get("sent") == tag or not (stunde <= lok.hour < stunde + 3):
            continue
        z["prefs"]["sent"] = tag
        _zustand_speichern(adr, z)
        try:
            text = briefing_text(adr)
            import agent as ag
            ag._send(u["tg"], text)
        except Exception as e:
            log.warning(f"motor briefing {adr}: {e}")


def beta_liste():
    """Tester aus admin.html (kv engine_beta_list) plus ENGINE_BETA auf web."""
    aus_env = {holder.norm(x) for x in os.getenv("ENGINE_BETA", "").split(",") if x.strip()}
    return aus_env | {holder.norm(x) for x in _j("engine_beta_list", [])}


def beta(adr):
    """Wallets, die Your Engine schon vor dem Schalter nutzen duerfen."""
    return holder.norm(adr) in beta_liste()


def offen(adr=None):
    try:
        import features
        if features.an("engine"):
            return True
    except Exception:
        pass
    return bool(adr and beta(adr))


def tick():
    """Aus ai.waechter, etwa alle 45 Sekunden."""
    try:
        import features
        if not features.an("engine") and not beta_liste():
            return
    except Exception:
        return
    jetzt = time.time()
    for name, f, takt in (("erinnerung", _erinnerungen, 0), ("alarm", _alarme, 120),
                          ("watch", _beobachten, 300), ("brief", _briefings, 60), ("weg", _pendeln, 60),
                          ("news", _nachrichten, 120), ("post", _post, 600), ("kal", _termine, 120), ("nachfrage", _nachfragen, 60), ("ann", _ankuendigungen, 60), ("tokwatch", _token_wache, 20), ("scan", _scan_nachfassen, 20), ("intel", _intel_verfolgen, 1800),
                          ("signal", _signale, 60)):
        if takt and jetzt - _T.get(name, 0) < takt:
            continue
        _T[name] = jetzt
        try:
            f()
        except Exception as e:
            log.warning(f"motor {name}: {e}")


# ── Telegram ─────────────────────────────────────────────────────────────

STT = os.getenv("MOTOR_STT", "").strip() or "google/gemini-3.5-flash-lite"


def transkribieren(adr, daten, fmt="ogg", probe=False):
    import base64
    body = {"model": STT, "max_tokens": 600, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "Transcribe this voice note exactly, in its original language. Output only the words."},
        {"type": "input_audio", "input_audio": {"data": base64.b64encode(daten).decode(), "format": fmt}}]}]}
    if probe:
        d, k = _roh(body)
        _probe_kosten_nur(k)
    else:
        d = ai.anfrage(adr, None, body)
    return (((d.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()


def _teilen(text, n=3800):
    out = []
    while len(text) > n:
        cut = text.rfind("\n", 0, n)
        cut = cut if cut > n // 2 else n
        out.append(text[:cut])
        text = text[cut:].lstrip()
    return out + [text]


def _foto(chat_id, adr, bid, dataurl=None):
    """Erzeugtes Bild als Foto in den Telegram-Chat (zu gross: als Datei)."""
    import base64
    import chat
    from config import cfg
    try:
        if dataurl:
            mt = chat._DATAURL.match(dataurl)
            mime, daten = (mt.group(1), base64.b64decode(re.sub(r"\s", "", mt.group(2)))) if mt else (None, None)
        else:
            mime, daten = chat.bild_laden(adr, bid)
        if not daten:
            return False
        endung = {"image/jpeg": "jpg", "image/webp": "webp"}.get(mime or "", "png")
        r = requests.post(f"https://api.telegram.org/bot{cfg.tg_token}/sendPhoto", data={"chat_id": chat_id},
                          files={"photo": (f"oddsengine.{endung}", daten, mime or "image/png")}, timeout=60)
        if not (r.ok and r.json().get("ok")):
            r = requests.post(f"https://api.telegram.org/bot{cfg.tg_token}/sendDocument", data={"chat_id": chat_id},
                              files={"document": (f"oddsengine.{endung}", daten, mime or "image/png")}, timeout=60)
        return bool(r.ok)
    except Exception as e:
        log.warning(f"motor foto: {e}")
        return False


def kostenzeile_setzen(adr, an):
    z = zustand(adr)
    z["prefs"]["cost_line"] = bool(an)
    _zustand_speichern(adr, z)
    return {"ok": True, "cost_line": bool(an)}


def _kostenzeile(adr, r):
    """Kleine Zeile unter jeder Telegram-Antwort: was sie gekostet hat und das Guthaben danach.
    Dasselbe Konto wie auf engine.oddsengine.live. /cost off schaltet sie ab."""
    if zustand(adr)["prefs"].get("cost_line") is False:
        return None
    if r.get("free"):
        return "Free scan"
    if r.get("trial"):
        n = r.get("trial_left")
        return f"Free message · {n} left" if n is not None else "Free message"
    try:
        credit = float((ai.konto(adr, anlegen=False) or {}).get("credit") or 0)
    except Exception:
        return None
    k = float(r.get("cost_usd") or 0)
    kosten = f"${k:,.4f}" if 0 < k < 0.01 else f"${k:,.2f}"
    zeile = f"{kosten} · Balance ${credit:,.2f}"
    if credit < _f("MOTOR_LOW_BALANCE", 0.5):
        zeile += "\nBalance low. Top up with $ODDS on engine.oddsengine.live"
    return zeile


def sprechen(adr, text):
    """Text -> MP3 fuer die Seite. Kostet VOICE_USD_PER_1K vom Guthaben. (bytes, None) oder (None, fehler)."""
    import stimme
    if not stimme.bereit():
        return None, "Voice is not set up yet."
    t = stimme.vorlesbar(text)
    k = stimme.kosten(len(t))
    if not _gebuehr(adr, k):
        return None, "Your balance is too low for voice."
    mp3, n = stimme.sprechen(t)
    if not mp3:
        return None, f"Could not speak right now ({n})."
    return mp3, None


def _sprachnachricht(chat_id, adr, text):
    """Antwort als Telegram-Sprachnachricht. True, wenn sie raus ist."""
    import stimme
    from config import cfg
    mp3, fehler = sprechen(adr, text)
    if not mp3:
        return False
    import agent
    agent._tg("sendChatAction", chat_id=chat_id, action="record_voice")
    og = stimme.ogg(mp3)
    if og:
        r = requests.post(f"https://api.telegram.org/bot{cfg.tg_token}/sendVoice", data={"chat_id": chat_id},
                          files={"voice": ("answer.ogg", og, "audio/ogg")}, timeout=60)
    else:
        r = requests.post(f"https://api.telegram.org/bot{cfg.tg_token}/sendAudio", data={"chat_id": chat_id, "title": "Your Engine"},
                          files={"audio": ("answer.mp3", mp3, "audio/mpeg")}, timeout=60)
    return bool(r.ok)


def telegram_foto(chat_id, adr, file_id, caption="", mime="image/jpeg"):
    """Foto oder Bild-Datei an den Bot: Your Engine schaut es sich an. Laeuft in einem eigenen Thread."""
    import base64
    import agent
    from config import cfg
    try:
        agent._tg("sendChatAction", chat_id=chat_id, action="typing")
        f = agent._tg("getFile", file_id=file_id) or {}
        pfad = (f.get("result") or {}).get("file_path")
        if not pfad:
            agent._send(chat_id, "I could not open that picture. Try again.")
            return
        daten = requests.get(f"https://api.telegram.org/file/bot{cfg.tg_token}/{pfad}", timeout=30).content
        if len(daten) > 8_000_000:
            agent._send(chat_id, "That picture is too big. Send it as a photo, not as a file.")
            return
        url = f"data:{mime or 'image/jpeg'};base64," + base64.b64encode(daten).decode()
        telegram(chat_id, adr, caption or "", bilder=[url])
    except Exception as e:
        log.warning(f"motor foto rein: {e}")
        agent._send(chat_id, "Something went wrong with that picture. Try again in a minute.")


def telegram(chat_id, adr, text, voice_id=None, bilder=None):
    """Nachricht aus dem privaten Chat mit dem Bot. Laeuft in einem eigenen Thread."""
    import agent
    from config import cfg
    try:
        agent._tg("sendChatAction", chat_id=chat_id, action="typing")
        probe = False
        if voice_id:
            kt = ai.konto(adr) or {}
            if kt.get("credit", 0.0) < _min():
                if probe_rest(adr) > 0 and _probe_topf_frei():
                    probe = True
                else:
                    raise KeinGuthaben()
            f = agent._tg("getFile", file_id=voice_id) or {}
            pfad = (f.get("result") or {}).get("file_path")
            if not pfad:
                agent._send(chat_id, "I could not open that voice note. Try again or type it.")
                return
            daten = requests.get(f"https://api.telegram.org/file/bot{cfg.tg_token}/{pfad}", timeout=30).content
            if len(daten) > 8_000_000:
                agent._send(chat_id, "That voice note is too long. Keep it under a few minutes.")
                return
            text = transkribieren(adr, daten, "ogg", probe)
            if not text:
                agent._send(chat_id, "I could not understand that voice note.")
                return
        if voice_id:
            agent._tg("sendChatAction", chat_id=chat_id, action="typing")
        r = frage(adr, text, "tg", bilder=bilder)
        for c in r.get("cards") or []:
            agent._send(chat_id, c.get("html") or c["text"], c.get("buttons"), html=bool(c.get("html")))
        for bid in r.get("images") or []:
            _foto(chat_id, adr, bid)
        for url in r.get("image_data") or []:
            _foto(chat_id, adr, None, url)
        teile = _teilen(_klartext(r["text"]))
        try:
            fuss = _kostenzeile(adr, r)
        except Exception:
            fuss = None
        if fuss and teile:
            if len(teile[-1]) + len(fuss) < 3900:
                teile[-1] += "\n\n" + fuss
            else:
                teile.append(fuss)
        knoepfe = r.get("buttons") or None
        stimme_ok = bool(voice_id) and _stimme_an(adr) and not r.get("trial")
        if stimme_ok:
            try:
                stimme_ok = _sprachnachricht(chat_id, adr, r["text"])
            except Exception as e:
                log.warning(f"motor stimme: {e}")
                stimme_ok = False
        for i, teil in enumerate(teile):
            agent._send(chat_id, teil, knoepfe if (knoepfe and i == len(teile) - 1) else None, silent=bool(stimme_ok))
    except KeinGuthaben:
        agent._send(chat_id, LEER)
    except NichtBereit as e:
        agent._send(chat_id, str(e))
    except ai.Fehler as e:
        agent._send(chat_id, LEER if e.status == 402 else "Something went wrong on my side: " + str(e.text)[:200])
    except Exception as e:
        log.warning(f"motor telegram: {e}")
        agent._send(chat_id, "Something went wrong on my side. Try again in a minute.")
