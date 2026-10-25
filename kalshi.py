"""
kalshi.py: liest die Tageshoch-Maerkte von Kalshi, NUR lesend.

Seit 2.17. Kein Konto, kein Schluessel: die Marktdaten von Kalshi sind
oeffentlich (GET /markets). Wir kaufen dort nichts und geben dort keine
Signale. Das Board zeigt die Kalshi-Preise neben Polymarket, mehr nicht.

Wichtig, und deshalb steht es an jeder Zeile im Board:
Kalshi loest NICHT auf derselben Station auf wie Polymarket.
    New York   Kalshi Central Park      Polymarket LaGuardia
    Chicago    Kalshi Midway            Polymarket O'Hare
    Denver     Kalshi Denver Intl       Polymarket Buckley
Dazwischen liegen oft 1 bis 2 Grad, also ein ganzes Fach. Nur wo die Station
dieselbe ist (same_station), darf man die Engine direkt daneben halten.

Weitere Staedte ohne neues ZIP: KALSHI_SERIES="stadt:TICKER:Station:0|1,..."
    z.B.  KALSHI_SERIES="seattle:KXHIGHTSEA:Seattle-Tacoma Intl:1"
Aus: KALSHI_ENABLED=false
"""
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from logger import log

BASES = [b.strip().rstrip("/") for b in os.getenv(
    "KALSHI_API",
    "https://api.elections.kalshi.com/trade-api/v2,"
    "https://external-api.kalshi.com/trade-api/v2").split(",") if b.strip()]
AN = os.getenv("KALSHI_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
CACHE_S = int(os.getenv("KALSHI_CACHE_S", "120"))
TIMEOUT = float(os.getenv("KALSHI_TIMEOUT_S", "6"))

# stadt (wie in unseren Snapshots) -> (Serie, Kalshi-Station, dieselbe Station wie Polymarket)
SERIES = {
    "nyc":         ("KXHIGHNY",  "Central Park",           False),
    "chicago":     ("KXHIGHCHI", "Chicago Midway",         False),
    "los angeles": ("KXHIGHLAX", "Los Angeles Intl",       True),
    "austin":      ("KXHIGHAUS", "Austin-Bergstrom Intl",  True),
    "denver":      ("KXHIGHDEN", "Denver Intl",            False),
    "miami":       ("KXHIGHMIA", "Miami Intl",             True),
}
for _teil in os.getenv("KALSHI_SERIES", "").split(","):
    _f = [x.strip() for x in _teil.split(":")]
    if len(_f) >= 2 and _f[0] and _f[1]:
        SERIES[_f[0].lower()] = (_f[1].upper(), _f[2] if len(_f) > 2 else "",
                                 (_f[3] if len(_f) > 3 else "0") in ("1", "true", "yes"))

_MON = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
_cache = {}
_lock = threading.Lock()


def _zahl(x):
    try:
        v = float(x)
        return v if v > 0 else None
    except Exception:
        return None


def _datum(event_ticker):
    """KXHIGHNY-26SEP09 -> 2026-09-09"""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})$", str(event_ticker or "").upper())
    if not m or m.group(2) not in _MON:
        return None
    return f"20{m.group(1)}-{_MON[m.group(2)]:02d}-{m.group(3)}"


def grenzen(text):
    """'84° to 85°' -> (84, 85) · '83° or below' -> (None, 83) · '93° or above' -> (93, None)
    Geht auch fuer Polymarket: '86-87°F', '71°F or below', '90°F or higher'."""
    t = str(text or "").replace("\u2212", "-").replace("\u2013", "-").lower()
    erste = re.search(r"-?\d+", t)
    if not erste:
        return None
    if re.search(r"or (below|lower|less)|<|\u2264", t):
        return (None, int(erste.group()))
    if re.search(r"or (above|higher|more)|>|\u2265", t):
        return (int(erste.group()), None)
    m = re.search(r"(-?\d+)\s*\u00b0?\s*[fc]?\s*(?:-|to)\s*(-?\d+)", t)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (int(erste.group()), int(erste.group()))


def _fach(m):
    titel = m.get("yes_sub_title") or m.get("subtitle") or ""
    g = grenzen(titel)
    if g is None:
        art = str(m.get("strike_type") or "")
        lo, hi = m.get("floor_strike"), m.get("cap_strike")
        try:
            if art == "between" and lo is not None and hi is not None:
                g = (int(lo), int(hi))
            elif art in ("less", "less_or_equal") and hi is not None:
                g = (None, int(hi) - (1 if art == "less" else 0))
            elif art in ("greater", "greater_or_equal") and lo is not None:
                g = (int(lo) + (1 if art == "greater" else 0), None)
        except Exception:
            g = None
    if g is None:
        return None
    lo, hi = g
    if not titel:
        titel = (f"{hi}° or below" if lo is None else
                 f"{lo}° or above" if hi is None else f"{lo}° to {hi}°")
    return {"label": titel, "lo": lo, "hi": hi,
            "ask": _zahl(m.get("yes_ask_dollars")),
            "bid": _zahl(m.get("yes_bid_dollars")),
            "last": _zahl(m.get("last_price_dollars")),
            "ticker": m.get("ticker")}


def _holen(serie):
    letzter = None
    for base in BASES:
        try:
            r = requests.get(f"{base}/markets",
                             params={"series_ticker": serie, "status": "open", "limit": 200},
                             timeout=TIMEOUT, headers={"Accept": "application/json"})
            if r.status_code == 200:
                return r.json().get("markets") or []
            letzter = f"{r.status_code} {r.text[:120]}"
        except Exception as e:
            letzter = str(e)[:160]
    log.debug(f"kalshi {serie}: {letzter}")
    return None


def serie_laden(stadt, roh=False):
    """Alle offenen Tage einer Stadt: {datum: {...}}. Mit Cache."""
    if not AN:
        return {}
    eintrag = SERIES.get(str(stadt or "").lower())
    if not eintrag:
        return {}
    serie, station, gleich = eintrag
    jetzt = time.time()
    with _lock:
        c = _cache.get(serie)
        if c and jetzt - c[0] < CACHE_S and not roh:
            return c[1]
    maerkte = _holen(serie)
    if maerkte is None:
        # Kalshi nicht erreichbar: lieber den alten Stand als gar nichts.
        with _lock:
            c = _cache.get(serie)
        return c[1] if c else {}
    tage = {}
    for m in maerkte:
        d = _datum(m.get("event_ticker"))
        f = _fach(m)
        if not d or not f:
            continue
        t = tage.setdefault(d, {
            "series": serie, "event": m.get("event_ticker"),
            "station": station, "same_station": bool(gleich),
            "url": f"https://kalshi.com/markets/{serie.lower()}",
            "close_time": m.get("close_time"), "buckets": []})
        t["buckets"].append(f)
    for t in tage.values():
        t["buckets"].sort(key=lambda b: (-10 ** 6 if b["lo"] is None else b["lo"]))
    with _lock:
        _cache[serie] = (jetzt, tage)
    if roh:
        return {"parsed": tage, "raw_count": len(maerkte),
                "raw_sample": maerkte[0] if maerkte else None}
    return tage


def fuer(stadt, datum):
    return serie_laden(stadt).get(str(datum))


def vorladen(staedte):
    """Mehrere Serien parallel holen, damit das Board nicht wartet."""
    ziel = sorted({str(s).lower() for s in staedte if str(s).lower() in SERIES})
    if not (AN and ziel):
        return
    try:
        with ThreadPoolExecutor(max_workers=min(6, len(ziel))) as ex:
            list(ex.map(serie_laden, ziel))
    except Exception as e:
        log.debug(f"kalshi vorladen: {e}")


def neben_polymarket(k, faecher, gleich):
    """Haengt an jedes Kalshi-Fach den Polymarket-Preis und (nur bei gleicher
    Station) die Engine-Wahrscheinlichkeit, sofern die Fachgrenzen passen."""
    pm = {}
    for f in faecher or []:
        g = grenzen(f.get("label"))
        if g:
            pm[g] = f
    aus = []
    for b in k.get("buckets") or []:
        z = dict(b)
        p = pm.get((b["lo"], b["hi"]))
        if p:
            z["polymarket"] = p.get("market")
            if gleich and isinstance(p.get("engine"), (int, float)):
                z["engine"] = p["engine"]
                if z.get("ask"):
                    z["gap_pp"] = round((p["engine"] - z["ask"]) * 100, 1)
        aus.append(z)
    k = dict(k)
    k["buckets"] = aus
    k["aligned"] = sum(1 for z in aus if "polymarket" in z)
    return k
