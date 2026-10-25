"""
kalshi_engine.py: Die Engine auf Kalshi, alle Staedte, Hoch- und Tiefstwert. Seit 2.34.

Kalshi fuehrt fuer gut 20 US-Staedte taegliche Maerkte auf den hoechsten und den
niedrigsten Wert des Tages. Abgerechnet wird nach dem amtlichen Tagesbericht des
National Weather Service (CLI) der jeweiligen Station, ueber The Weather Company,
in ganzen Grad Fahrenheit, je 2 Grad ein Fach.

Wichtig: Kalshi rechnet oft an einer ANDEREN Station ab als Polymarket.
    New York   Kalshi Central Park (KNYC)   Polymarket LaGuardia
    Chicago    Kalshi Midway (KMDW)         Polymarket O'Hare
Deshalb rechnet die Engine hier fuer genau die Kalshi-Station, mit eigener
Korrektur je Station und Art (Hoch/Tief), gelernt aus den Kalshi-Ergebnissen.

Ablauf je Durchlauf (aus main.py, nach dem Polymarket-Scan):
  1. Serien finden: alle taeglichen KXHIGH*/KXLOW*-Serien mit offenen Maerkten.
     Station aus dem Regeltext "(CLIxxx)".
  2. Je Tag und Stadt: Ensemble fuer die Station (Hoch oder Tief), Messwerte von
     heute einfalten, Verteilung ueber die Kalshi-Faecher.
  3. Preise: Kalshi-Ask plus Kalshi-Gebuehr (0,07 x P x (1 - P) je Kontrakt).
     Dieselbe Blockwahl und dieselben Schranken wie auf Polymarket (engine.best_block),
     dieselbe ehrliche Zahl (Preis + k x (Modell - Preis)).
  4. Der erste Call je Tag und Stadt wird eingefroren und spaeter gegen das
     Kalshi-Ergebnis gewertet. Kein automatisches Posten (KALSHI_POST=on schaltet es ein).

Variablen (worker): KALSHI_ENGINE (on), KALSHI_FEE (0.07), KALSHI_POST (off),
KALSHI_MAX_SERIES (60). Oeffentlich sichtbar nur mit Schalter "kalshi".
"""
import json
import math
import os
import re
import statistics
import time
from datetime import datetime, timedelta, timezone

import requests

import engine
import storage
from config import cfg
from logger import log

API = os.getenv("KALSHI_API_ENGINE", "https://api.elections.kalshi.com/trade-api/v2").rstrip("/")
UA = {"Accept": "application/json", "User-Agent": "OddsEngine/2.34"}

# CLI-Kuerzel -> (Stadt, Station, ICAO, Breite, Laenge)
STATIONEN = {
    "NYC": ("New York City", "Central Park", "KNYC", 40.7790, -73.9693),
    "MDW": ("Chicago", "Chicago Midway", "KMDW", 41.7861, -87.7524),
    "DEN": ("Denver", "Denver Intl", "KDEN", 39.8466, -104.6562),
    "MIA": ("Miami", "Miami Intl", "KMIA", 25.7881, -80.3169),
    "LAX": ("Los Angeles", "Los Angeles Intl", "KLAX", 33.9382, -118.3866),
    "AUS": ("Austin", "Austin-Bergstrom Intl", "KAUS", 30.1831, -97.6799),
    "PHL": ("Philadelphia", "Philadelphia Intl", "KPHL", 39.8683, -75.2311),
    "BOS": ("Boston", "Boston Logan", "KBOS", 42.3606, -71.0097),
    "ATL": ("Atlanta", "Atlanta Hartsfield-Jackson", "KATL", 33.6301, -84.4418),
    "SFO": ("San Francisco", "San Francisco Intl", "KSFO", 37.6197, -122.3647),
    "PHX": ("Phoenix", "Phoenix Sky Harbor", "KPHX", 33.4278, -112.0037),
    "MSY": ("New Orleans", "New Orleans Intl", "KMSY", 29.9934, -90.2580),
    "DCA": ("Washington DC", "Reagan National", "KDCA", 38.8483, -77.0342),
    "SAT": ("San Antonio", "San Antonio Intl", "KSAT", 29.5443, -98.4839),
    "HOU": ("Houston", "Houston Hobby", "KHOU", 29.6454, -95.2789),
    "IAH": ("Houston", "Houston Bush Intercontinental", "KIAH", 29.9844, -95.3414),
    "SEA": ("Seattle", "Seattle-Tacoma Intl", "KSEA", 47.4447, -122.3144),
    "MSP": ("Minneapolis", "Minneapolis-St Paul Intl", "KMSP", 44.8831, -93.2289),
    "OKC": ("Oklahoma City", "Will Rogers World", "KOKC", 35.3889, -97.6006),
    "DFW": ("Dallas", "Dallas-Fort Worth Intl", "KDFW", 32.8998, -97.0403),
    "DAL": ("Dallas", "Dallas Love Field", "KDAL", 32.8471, -96.8518),
    "SAN": ("San Diego", "San Diego Intl", "KSAN", 32.7336, -117.1831),
    "LAS": ("Las Vegas", "Harry Reid Intl", "KLAS", 36.0719, -115.1634),
    "EWR": ("Newark", "Newark Liberty Intl", "KEWR", 40.6825, -74.1694),
    "DTW": ("Detroit", "Detroit Metro", "KDTW", 42.2124, -83.3534),
    "ORD": ("Chicago", "Chicago O'Hare", "KORD", 41.9602, -87.9316),
    "LGA": ("New York City", "LaGuardia", "KLGA", 40.7794, -73.8803),
}

_MON = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
_SCHEMA = [False]
_SERIEN = {"ts": 0.0, "liste": []}
_FEHLT = set()
_LEER = {}


def _f(k, d):
    try:
        return float(os.getenv(k, "").strip() or d)
    except Exception:
        return float(d)


def an():
    return os.getenv("KALSHI_ENGINE", "on").strip().lower() not in ("0", "off", "false", "no")


def oeffentlich():
    try:
        import features
        return features.an("kalshi")
    except Exception:
        return False


# ── Speicher ─────────────────────────────────────────────────────────────

def _pg():
    pg = storage._init_pg()
    if not pg:
        return None
    if not _SCHEMA[0]:
        pg.execute("""CREATE TABLE IF NOT EXISTS kalshi_engine (
            event TEXT PRIMARY KEY, series TEXT, city TEXT, kind TEXT, date TEXT,
            snap JSONB, call JSONB, result JSONB, cal JSONB,
            updated TIMESTAMPTZ DEFAULT now())""")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_kalshi_engine_date ON kalshi_engine(date)")
        _SCHEMA[0] = True
    return pg


def _j(v):
    if v is None:
        return None
    return v if isinstance(v, (dict, list)) else json.loads(v)


def _zeile(pg, event):
    r = pg.execute("SELECT snap, call, result, cal FROM kalshi_engine WHERE event=%s", (event,)).fetchone()
    if not r:
        return None
    return {"snap": _j(r[0]), "call": _j(r[1]), "result": _j(r[2]), "cal": _j(r[3])}


def _schreiben(pg, ev, snap=None, call=None, result=None, cal=None):
    pg.execute("""INSERT INTO kalshi_engine (event, series, city, kind, date, snap, call, result, cal, updated)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                  ON CONFLICT (event) DO UPDATE SET
                    snap=COALESCE(EXCLUDED.snap, kalshi_engine.snap),
                    call=COALESCE(kalshi_engine.call, EXCLUDED.call),
                    result=COALESCE(EXCLUDED.result, kalshi_engine.result),
                    cal=COALESCE(kalshi_engine.cal, EXCLUDED.cal),
                    updated=now()""",
               (ev["event"], ev["series"], ev["city"], ev["kind"], ev["date"],
                json.dumps(snap, default=str) if snap is not None else None,
                json.dumps(call, default=str) if call is not None else None,
                json.dumps(result, default=str) if result is not None else None,
                json.dumps(cal, default=str) if cal is not None else None))


# ── Kalshi lesen ─────────────────────────────────────────────────────────

def _get(pfad, **params):
    for versuch in range(3):
        try:
            r = requests.get(API + pfad, params=params, headers=UA, timeout=15)
            if r.status_code == 429:
                time.sleep(1.5 * (versuch + 1))
                continue
            if r.status_code == 200:
                return r.json()
            return None
        except Exception:
            time.sleep(0.8)
    return None


def _datum(event_ticker):
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})$", str(event_ticker or "").upper())
    if not m or m.group(2) not in _MON:
        return None
    return f"20{m.group(1)}-{_MON[m.group(2)]:02d}-{m.group(3)}"


def serien():
    """Taegliche Hoch-/Tiefstwert-Serien von Kalshi, 6 Stunden Cache."""
    if _SERIEN["liste"] and time.time() - _SERIEN["ts"] < 6 * 3600:
        return _SERIEN["liste"]
    d = _get("/series", category="Climate and Weather", limit=1000) or {}
    out = []
    for s in d.get("series") or []:
        t = str(s.get("ticker") or "")
        if s.get("frequency") == "daily" and re.match(r"^KX(HIGH|LOW)", t):
            out.append(t)
    if out:
        _SERIEN.update(ts=time.time(), liste=sorted(out))
    return _SERIEN["liste"]


def _fach(m):
    """Ein Kalshi-Markt -> Fach mit Grenzen in Grad F (beide einschliesslich)."""
    art = str(m.get("strike_type") or "")
    lo, hi = m.get("floor_strike"), m.get("cap_strike")
    try:
        if art == "between" and lo is not None and hi is not None:
            a, b = int(round(float(lo))), int(round(float(hi)))
        elif art == "less" and hi is not None:
            a, b = None, int(round(float(hi))) - 1
        elif art == "less_or_equal" and hi is not None:
            a, b = None, int(round(float(hi)))
        elif art == "greater" and lo is not None:
            a, b = int(round(float(lo))) + 1, None
        elif art == "greater_or_equal" and lo is not None:
            a, b = int(round(float(lo))), None
        else:
            return None
    except Exception:
        return None

    def zahl(x):
        try:
            v = float(x)
            return v if 0 < v < 1 else None
        except Exception:
            return None
    label = m.get("yes_sub_title") or (f"{b}° or below" if a is None else f"{a}° or above" if b is None else f"{a}° to {b}°")
    return {"label": label, "lo_f": a, "hi_f": b,
            "lo_c": float("-inf") if a is None else engine._f_to_c(a - 0.5),
            "hi_c": float("inf") if b is None else engine._f_to_c(b + 0.5),
            "unit": "F", "ticker": m.get("ticker"),
            "ask": zahl(m.get("yes_ask_dollars")), "bid": zahl(m.get("yes_bid_dollars")),
            "last": zahl(m.get("last_price_dollars")),
            "volume": float(m.get("volume_fp") or m.get("volume") or 0),
            "result": str(m.get("result") or "").lower(), "status": m.get("status")}


def _events_aus(serie, maerkte):
    evs = {}
    for m in maerkte or []:
        et = m.get("event_ticker")
        d = _datum(et)
        f = _fach(m)
        if not d or not f:
            continue
        cli = re.search(r"\(CLI([A-Z]{3})\)", str(m.get("rules_primary") or ""))
        e = evs.setdefault(et, {"event": et, "series": serie, "date": d,
                                "kind": "high" if serie.startswith("KXHIGH") else "low",
                                "cli": cli.group(1) if cli else None,
                                "close_time": m.get("close_time"), "outcomes": []})
        if not e["cli"] and cli:
            e["cli"] = cli.group(1)
        e["outcomes"].append(f)
    out = []
    for e in evs.values():
        st = STATIONEN.get(e["cli"] or "")
        if not st:
            if e["cli"]:
                _FEHLT.add(e["cli"])
            continue
        e["city"], e["station"], e["icao"], e["lat"], e["lon"] = st
        e["outcomes"].sort(key=lambda o: -10 ** 6 if o["lo_f"] is None else o["lo_f"])
        # Faecher muessen lueckenlos sein, sonst stimmt die Verteilung nicht
        os_ = e["outcomes"]
        ok = len(os_) >= 3 and os_[0]["lo_f"] is None and os_[-1]["hi_f"] is None and all(
            os_[i]["hi_f"] is not None and os_[i + 1]["lo_f"] == os_[i]["hi_f"] + 1 for i in range(len(os_) - 1))
        if not ok:
            log.debug(f"kalshi_engine {e['event']}: faecher nicht lueckenlos")
            continue
        e["url"] = f"https://kalshi.com/markets/{serie.lower()}"
        out.append(e)
    return out


# ── Gelernte Korrektur je Station und Art ───────────────────────────────

_KORR = {"ts": 0.0, "d": {}}


def korrekturen():
    """Mittlerer Fehler Vorhersage minus Ergebnis je Serie, zur 0 geschrumpft."""
    if time.time() - _KORR["ts"] < 1800:
        return _KORR["d"]
    pg = _pg()
    out = {}
    if pg:
        ab = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        rows = pg.execute("SELECT series, cal FROM kalshi_engine WHERE date >= %s AND cal IS NOT NULL", (ab,)).fetchall()
        fehler = {}
        for s, cal in rows:
            c = _j(cal) or {}
            if c.get("value_c") is None or c.get("mean_c") is None:
                continue
            fehler.setdefault(s, []).append(float(c["mean_c"]) - float(c["value_c"]))
        k = _f("KALSHI_BIAS_SHRINK", 5)
        for s, v in fehler.items():
            n = len(v)
            m = statistics.mean(v)
            out[s] = {"n": n, "mean": round(m, 2), "bias": round(max(-3.0, min(3.0, m * n / (n + k))), 2)}
    _KORR.update(ts=time.time(), d=out)
    return out


# ── Startkorrektur aus den letzten 30 Tagen ─────────────────────────────
# Ohne eigene Ergebnisse waere die Korrektur 0, und das Modellgitter liegt an
# manchen Stationen 2 bis 3 Grad F daneben (Miami zu kalt, Los Angeles zu warm).
# Deshalb rechnet die Engine beim Start nach: die Vorhersage vom Vortag
# (Open-Meteo Previous Runs, dieselben drei Modelle) gegen das Kalshi-Ergebnis
# jedes Tages der letzten 30 Tage. Einmal am Tag je Serie, im kv gespeichert.

PREV = "https://previous-runs-api.open-meteo.com/v1/forecast"


def startkorrektur(e):
    k = "kalshi_boot_" + e["series"]
    try:
        alt = json.loads(storage.kv_get(k) or "null")
    except Exception:
        alt = None
    if alt and time.time() - float(alt.get("ts") or 0) < 24 * 3600:
        return alt
    werte = {}
    d = _get("/events", series_ticker=e["series"], status="settled", limit=40, with_nested_markets="true") or {}
    for ev in d.get("events") or []:
        dt = _datum(ev.get("event_ticker"))
        sieger = [f for f in (_fach(m) for m in ev.get("markets") or []) if f and f["result"] == "yes"]
        if dt and len(sieger) == 1:
            w = sieger[0]
            werte[dt] = (w["hi_f"] - 1 if w["lo_f"] is None else w["lo_f"] + 1 if w["hi_f"] is None
                         else (w["lo_f"] + w["hi_f"]) / 2)
    fehler = []
    if werte:
        try:
            r = requests.get(PREV, params={"latitude": e["lat"], "longitude": e["lon"],
                                           "hourly": "temperature_2m_previous_day1", "models": cfg.ens_models,
                                           "past_days": 31, "forecast_days": 1, "timezone": "auto",
                                           "temperature_unit": "fahrenheit"}, timeout=25)
            h = r.json().get("hourly") or {}
        except Exception:
            h = {}
        zeiten = h.get("time") or []
        fn = max if e["kind"] == "high" else min
        for dt, v in werte.items():
            idx = [i for i, t in enumerate(zeiten) if t.startswith(dt)]
            if len(idx) < 20:
                continue
            ms = []
            for key, arr in h.items():
                if key.startswith("temperature_2m"):
                    xs = [arr[i] for i in idx if i < len(arr) and arr[i] is not None]
                    if len(xs) >= 20:
                        ms.append(fn(xs))
            if ms:
                fehler.append(statistics.mean(ms) - v)
    n = len(fehler)
    out = {"ts": time.time(), "n": n}
    if n:
        m_f = statistics.mean(fehler)
        out.update(mean_f=round(m_f, 2), mae_f=round(statistics.mean(abs(x) for x in fehler), 2),
                   bias_c=round(max(-3.0, min(3.0, m_f * 5 / 9 * n / (n + 5))), 2))
    try:
        storage.kv_set(k, json.dumps(out))
    except Exception:
        pass
    return out


def _korrektur(e):
    """Eigene Ergebnisse, sobald genug da sind, vorher die Startkorrektur."""
    eigen = korrekturen().get(e["series"]) or {}
    if eigen.get("n", 0) >= int(_f("KALSHI_OWN_MIN_N", 15)):
        return eigen["bias"], f"own {eigen['n']} days"
    try:
        b = startkorrektur(e)
    except Exception as ex:
        log.debug(f"kalshi_engine startkorrektur {e['series']}: {ex}")
        b = {}
    if b.get("n", 0) >= 5 and b.get("bias_c") is not None:
        return b["bias_c"], f"last {b['n']} days"
    if eigen.get("n", 0) >= int(_f("KALSHI_BIAS_MIN_N", 3)):
        return eigen["bias"], f"own {eigen['n']} days"
    return 0.0, "none yet"


# ── Ein Tag, eine Stadt ──────────────────────────────────────────────────

def _offset_s(date, tz):
    try:
        from zoneinfo import ZoneInfo
        y, mo, d = (int(x) for x in date.split("-"))
        return int(datetime(y, mo, d, 12, tzinfo=ZoneInfo(tz)).utcoffset().total_seconds())
    except Exception:
        return 0


def gebuehr(p):
    return _f("KALSHI_FEE", 0.07) * p * (1 - p)


def bewerten(e, mode=None):
    """Wie engine.evaluate, nur fuer ein Kalshi-Event. Gibt immer einen Snapshot zurueck."""
    mode = mode or cfg.mode
    now = datetime.now(timezone.utc)
    snap = {"ts": now.isoformat(timespec="seconds"), "event": e["event"], "series": e["series"],
            "city": e["city"], "kind": e["kind"], "date": e["date"], "station": e["station"],
            "icao": e["icao"], "url": e["url"], "signal": False, "reason": None}
    try:
        ende = datetime.fromisoformat(str(e["close_time"]).replace("Z", "+00:00"))
        lead_h = (ende - now).total_seconds() / 3600
    except Exception:
        lead_h = 24.0
    snap["lead_h"] = round(lead_h, 1)
    snap["market"] = {o["label"]: o["ask"] for o in e["outcomes"]}
    if not (cfg.min_lead_h <= lead_h <= cfg.max_lead_h):
        snap["reason"] = f"lead {lead_h:.0f}h outside [{cfg.min_lead_h},{cfg.max_lead_h}]"
        return snap
    try:
        if e["kind"] == "high":
            werte, tz = engine.member_day_maxima(e["lat"], e["lon"], e["date"], key=e["icao"])
        else:
            import tief
            werte, tz = tief.member_day_minima(e["lat"], e["lon"], e["date"], key=e["icao"])
    except Exception as ex:
        snap["reason"] = f"forecast error: {str(ex)[:80]}"
        return snap
    if len(werte) < cfg.min_members:
        snap["reason"] = f"only {len(werte)} ensemble members"
        return snap
    snap["ens_mean"] = round(sum(werte) / len(werte), 2)
    snap["tz"] = tz

    # Was die Station heute schon gemessen hat: das Maximum kann nur steigen,
    # das Minimum nur fallen. Faecher, die damit unmoeglich sind, fallen raus.
    tot = set()
    mo = engine.metar_observed(e["icao"], e["date"], _offset_s(e["date"], tz)) if tz else None
    if mo and mo.get("metar_n"):
        if e["kind"] == "high" and mo.get("metar_max_c") is not None:
            mx = mo["metar_max_c"]
            snap["obs_c"] = mx
            werte = [max(w, mx) for w in werte]
            tot = {i for i, o in enumerate(e["outcomes"]) if o["hi_c"] < mx - 1e-9}
        elif e["kind"] == "low" and mo.get("metar_min_c") is not None:
            mn = mo["metar_min_c"]
            snap["obs_c"] = mn
            werte = [min(w, mn) for w in werte]
            tot = {i for i, o in enumerate(e["outcomes"]) if o["lo_c"] > mn + 1e-9}

    bias, quelle = _korrektur(e)
    snap["bias_c"] = bias
    snap["bias_from"] = quelle
    q = engine.distribution(werte, e["outcomes"], lead_h, sigma=cfg.dress_sigma_c, bias=bias)
    if not q:
        snap["reason"] = "distribution failed"
        return snap
    snap["full_dist"] = {o["label"]: round(p, 4) for o, p in zip(e["outcomes"], q)}

    books = []
    for i, o in enumerate(e["outcomes"]):
        a = o["ask"]
        if i in tot or a is None or a >= 0.99:
            books.append(None)
            continue
        b = o["bid"]
        mid = (a + b) / 2 if b else a
        books.append({"ask": round(a + gebuehr(a), 4), "ask_raw": a, "bid": b, "one_sided": b is None,
                      "spread_bps": round((a - b) / mid * 10000, 0) if b else 10000.0,
                      "ask_depth_usd": 1e6})
    if tot:
        snap["dead_legs"] = len(tot)
    if sum(b is not None for b in books) < 3:
        snap["reason"] = "not enough priced fields"
        return snap

    mitte = round(sum(werte) / len(werte) - bias, 2)
    snap["centre_c"] = mitte
    snap["centre_f"] = round(mitte * 9 / 5 + 32, 1)
    blk, why, diag = engine.best_block(e["outcomes"], q, books, mode, lead_h=lead_h, center_c=mitte)
    if not blk:
        snap["reason"] = why
        if diag:
            snap.update(diag)
        return snap
    try:
        import kalibrierung
        k = kalibrierung.vertrauen()
    except Exception:
        k = cfg.trust_start
    roh = blk.get("coverage_raw", blk["coverage"])
    cost = blk["cost"]
    ehrlich = max(0.0, min(1.0, cost + k * (roh - cost)))
    snap.update({
        "signal": True, "legs": [o["label"] for o in blk["legs"]],
        "tickers": [o["ticker"] for o in blk["legs"]],
        "asks": [round(a, 4) for a in blk["asks"]],
        "asks_raw": [books[e["outcomes"].index(o)]["ask_raw"] for o in blk["legs"]],
        "cost": round(cost, 4), "coverage_engine": round(roh, 4), "coverage": round(ehrlich, 4),
        "edge_pp": round((ehrlich - cost) * 100, 2), "edge_raw_pp": round((roh - cost) * 100, 2),
        "trust_k": round(k, 3), "payout": round(1 / cost, 3) if cost > 0 else None,
        "q": [round(x, 4) for x in blk["q"]],
    })
    snap["grade"] = "bet" if snap["edge_pp"] >= cfg.bet_min_edge_pp else "read"
    if cfg.min_field_cost and cost < cfg.min_field_cost:
        snap.update(signal=False, reason=f"block {cost*100:.0f}c < {cfg.min_field_cost*100:.0f}c floor")
    elif cfg.max_edge_pp and snap["edge_raw_pp"] >= cfg.max_edge_pp:
        snap.update(signal=False, reason=f"model edge {snap['edge_raw_pp']:+.1f}pp >= {cfg.max_edge_pp:.0f}pp cap")
    elif snap["grade"] == "read" and not cfg.publish_reads:
        snap.update(signal=False, reason="read only")
    return snap


# ── Ergebnis ────────────────────────────────────────────────────────────

def _ergebnis(event):
    d = _get(f"/events/{event}", with_nested_markets="true") or {}
    ms = (d.get("event") or {}).get("markets") or d.get("markets") or []
    if not ms:
        return None
    fs = [f for f in (_fach(m) for m in ms) if f]
    if not fs or any(f["result"] not in ("yes", "no") for f in fs):
        return None
    sieger = [f for f in fs if f["result"] == "yes"]
    if len(sieger) != 1:
        return None
    w = sieger[0]
    if w["lo_f"] is None:
        wert_f = w["hi_f"] - 1
    elif w["hi_f"] is None:
        wert_f = w["lo_f"] + 1
    else:
        wert_f = (w["lo_f"] + w["hi_f"]) / 2
    return {"label": w["label"], "value_f": wert_f, "value_c": round(engine._f_to_c(wert_f), 2)}


def _aufloesen(pg):
    heute = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = pg.execute("""SELECT event, series, city, kind, date, snap, call, cal FROM kalshi_engine
                         WHERE result IS NULL AND date < %s ORDER BY date LIMIT 80""", (heute,)).fetchall()
    for ev, s, c, k, d, snap, call, cal in rows:
        r = _ergebnis(ev)
        if not r:
            continue
        call = _j(call)
        cal = _j(cal)
        if call:
            r["hit"] = r["label"] in (call.get("legs") or [])
            cost = float(call.get("cost") or 0)
            r["pnl_per_usd"] = round((1 / cost - 1) if (r["hit"] and cost > 0) else -1.0, 4)
        neu_cal = None
        if cal and cal.get("mean_c") is not None:
            neu_cal = dict(cal, value_c=r["value_c"])
        pg.execute("UPDATE kalshi_engine SET result=%s, cal=COALESCE(%s, cal), updated=now() WHERE event=%s",
                   (json.dumps(r), json.dumps(neu_cal) if neu_cal else None, ev))
        time.sleep(0.15)


# ── Durchlauf ───────────────────────────────────────────────────────────

def tick():
    if not an():
        return
    pg = _pg()
    if not pg:
        return
    t0 = time.time()
    try:
        _aufloesen(pg)
    except Exception as ex:
        log.warning(f"kalshi_engine aufloesen: {ex}")
    n_ev = n_sig = 0
    for serie in serien()[:int(_f("KALSHI_MAX_SERIES", 120))]:
        if time.time() - _LEER.get(serie, 0) < 3 * 3600:
            continue
        d = _get("/markets", series_ticker=serie, status="open", limit=200)
        time.sleep(0.12)
        if not d:
            continue
        if not d.get("markets"):
            _LEER[serie] = time.time()
            continue
        for e in _events_aus(serie, d.get("markets") or []):
            try:
                snap = bewerten(e)
            except Exception as ex:
                log.warning(f"kalshi_engine {e['event']}: {ex}")
                continue
            n_ev += 1
            alt = _zeile(pg, e["event"]) or {}
            call = None
            if snap.get("signal") and not alt.get("call"):
                call = {k: snap.get(k) for k in ("ts", "legs", "tickers", "asks", "asks_raw", "cost", "coverage",
                                                  "coverage_engine", "edge_pp", "grade", "lead_h", "centre_f",
                                                  "obs_c", "payout")}
                n_sig += 1
                _post(e, call)
            cal = None
            if not alt.get("cal") and snap.get("ens_mean") is not None and 12 <= (snap.get("lead_h") or 0) <= 40:
                cal = {"mean_c": snap["ens_mean"], "lead_h": snap["lead_h"]}
            _schreiben(pg, e, snap=snap, call=call, cal=cal)
    if _FEHLT:
        log.info(f"kalshi_engine: unbekannte Stationen {sorted(_FEHLT)}")
    log.info(f"kalshi_engine: {n_ev} Maerkte, {n_sig} neue Calls, {time.time() - t0:.0f}s")


def _post(e, call):
    if os.getenv("KALSHI_POST", "off").strip().lower() not in ("1", "on", "true", "yes"):
        return
    try:
        import telegram
        art = "High" if e["kind"] == "high" else "Low"
        telegram.send(f"Kalshi call {e['city']} {art} {e['date']}\n"
                      f"{' / '.join(call['legs'])} for {call['cost']*100:.0f}c incl. fees, "
                      f"engine {call['coverage']*100:.0f}%\n{e['url']}")
    except Exception as ex:
        log.debug(f"kalshi_engine post: {ex}")


# ── Lesen fuer API, App und Your Engine ─────────────────────────────────

def stand(tage=21):
    pg = _pg()
    if not pg:
        return {"error": "no database"}
    heute = datetime.now(timezone.utc)
    ab = (heute - timedelta(days=tage)).strftime("%Y-%m-%d")
    rows = pg.execute("""SELECT event, series, city, kind, date, snap, call, result FROM kalshi_engine
                         WHERE date >= %s ORDER BY date DESC, city""", (ab,)).fetchall()
    offen, board, erledigt = [], [], []
    for ev, s, c, k, d, snap, call, res in rows:
        snap, call, res = _j(snap) or {}, _j(call), _j(res)
        basis = {"event": ev, "series": s, "city": c, "kind": k, "date": d, "station": snap.get("station"),
                 "url": snap.get("url")}
        if res is None:
            fav = None
            fd = snap.get("full_dist") or {}
            if fd:
                lab = max(fd, key=fd.get)
                fav = {"label": lab, "engine": fd[lab], "market": (snap.get("market") or {}).get(lab)}
            board.append(dict(basis, lead_h=snap.get("lead_h"), centre_f=snap.get("centre_f"),
                              obs_f=round(snap["obs_c"] * 9 / 5 + 32, 1) if snap.get("obs_c") is not None else None,
                              favourite=fav, reason=None if snap.get("signal") else snap.get("reason"),
                              correction_f=round(-(snap.get("bias_c") or 0) * 9 / 5, 1), correction_from=snap.get("bias_from"),
                              dist=fd, market=snap.get("market")))
            if call:
                offen.append(dict(basis, **call))
        elif call:
            erledigt.append(dict(basis, **call, result=res.get("label"), hit=res.get("hit"),
                                 pnl_per_usd=res.get("pnl_per_usd")))
    n = len(erledigt)
    treffer = sum(1 for x in erledigt if x.get("hit"))
    pnl = sum(x.get("pnl_per_usd") or 0 for x in erledigt)
    gesagt = statistics.mean([x["coverage"] for x in erledigt if x.get("coverage") is not None]) if n else None
    staedte = sorted({(b["city"], b["kind"]) for b in board})
    return {"open_calls": offen, "board": board, "results": erledigt[:60],
            "record": {"calls": n, "hits": treffer, "hit_rate": round(treffer / n, 3) if n else None,
                       "avg_said": round(gesagt, 3) if gesagt is not None else None,
                       "result_per_usd": round(pnl, 2), "days": tage},
            "cities": len({c for c, _ in staedte}), "markets": len(staedte),
            "corrections": korrekturen(),
            "note": "Kalshi settles on the NWS daily climate report of each station, in whole degrees F. "
                    "Prices include the Kalshi fee. Calls are frozen when first made and scored after settlement."}
