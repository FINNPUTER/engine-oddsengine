"""
boersen.py: was an den grossen Boersen passiert. Seit 2.35.

Polymarket zeigt, worauf die Leute wetten. Entschieden wird der Kurs aber an
den Boersen. Deshalb liest die Engine jede Minute fuer BTC, ETH, SOL, XRP,
DOGE, BNB und HYPE:

    Funding        Hyperliquid und OKX (Perpetuals). Positiv: Longs zahlen, die Masse ist long.
    Open Interest  Hyperliquid und OKX, dazu die Aenderung ueber 15 Minuten und 1 Stunde.
    Long/Short     OKX, Verhaeltnis der Konten long zu short (5 Minuten).
    Taker-Flow     OKX, Anteil der aggressiven Kaeufe am Volumen der letzten 5 und 15 Minuten.
    Orderbuch      OKX, Coinbase und Kraken: Geld auf Kauf- gegen Verkaufsseite
                   innerhalb von 0,1 % und 0,5 % um den Mittelkurs.
    Liquidationen  OKX, liquidierte Longs und Shorts der letzten 15 und 60 Minuten in Dollar.
    Basis          Perp gegen Spot, und der USDT-Aufschlag (Binance-Spiegel gegen Coinbase).

Alles oeffentliche Schnittstellen ohne Schluessel. Binance-Futures und Bybit
sperren US-Server, darum OKX, Hyperliquid, Coinbase, Kraken und der offizielle
Binance-Datenspiegel (data-api.binance.vision).

Speicher:
    kv boersen_live     letzter Stand je Coin (fuer Terminal und /v1/sentiment)
    boersen_snap        Postgres, eine Zeile je Coin und Minute, 45 Tage. Damit pruefen wir
                        spaeter, welches Signal den Ausgang der Fenster wirklich vorhersagt,
                        bevor irgendetwas davon in die Engine-Zahl einfliesst.

Laeuft im Dienst crypto (krypto_main.py), Takt BOERSEN_TICK (60 s).
"""
import json
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import requests

import storage
from logger import log

UA = {"User-Agent": "Mozilla/5.0 (OddsEngine markets)", "Accept": "application/json"}
OKX = "https://www.okx.com"
HL = "https://api.hyperliquid.xyz/info"
CB = "https://api.exchange.coinbase.com"
KR = "https://api.kraken.com/0/public"
BNV = "https://data-api.binance.vision/api/v3"

COINS = ("btc", "eth", "sol", "xrp", "doge", "bnb", "hype")
HL_NAME = {"btc": "BTC", "eth": "ETH", "sol": "SOL", "xrp": "XRP", "doge": "DOGE", "bnb": "BNB", "hype": "HYPE"}
CB_PAIR = {"btc": "BTC-USD", "eth": "ETH-USD", "sol": "SOL-USD", "xrp": "XRP-USD", "doge": "DOGE-USD"}
KR_PAIR = {"btc": "XBTUSD", "eth": "ETHUSD", "sol": "SOLUSD", "xrp": "XRPUSD", "doge": "XDGUSD"}
TAKT = float(os.getenv("BOERSEN_TICK", "60"))
HALTEN_TAGE = int(os.getenv("BOERSEN_KEEP_DAYS", "45"))

_VERLAUF = {c: deque(maxlen=90) for c in COINS}   # (ts, oi_usd) fuer die Aenderungen
_LETZT = [0.0]
_SCHEMA = [False]
_SITZUNG = requests.Session()
_SITZUNG.headers.update(UA)


def _get(url, **params):
    r = _SITZUNG.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def _f(x, d=None):
    try:
        return float(x)
    except Exception:
        return d


# ── Quellen ──────────────────────────────────────────────────────────────

def hyperliquid():
    r = _SITZUNG.post(HL, json={"type": "metaAndAssetCtxs"}, timeout=10)
    r.raise_for_status()
    meta, ctxs = r.json()
    out = {}
    namen = {u["name"]: i for i, u in enumerate(meta["universe"])}
    for c, n in HL_NAME.items():
        i = namen.get(n)
        if i is None:
            continue
        x = ctxs[i]
        mark = _f(x.get("markPx"))
        oi = _f(x.get("openInterest"))
        out[c] = {"funding_1h": _f(x.get("funding")), "oi_usd": oi * mark if oi and mark else None,
                  "mark": mark, "oracle": _f(x.get("oraclePx")), "premium": _f(x.get("premium")),
                  "vol_24h_usd": _f(x.get("dayNtlVlm"))}
    return out


def _okx_buch(c):
    d = _get(OKX + "/api/v5/market/books", instId=f"{c.upper()}-USDT", sz="400")["data"][0]
    return [(float(p), float(q)) for p, q, *_ in d["bids"]], [(float(p), float(q)) for p, q, *_ in d["asks"]]


def _cb_buch(c):
    d = _get(f"{CB}/products/{CB_PAIR[c]}/book", level="2")
    return [(float(p), float(q)) for p, q, _ in d["bids"]], [(float(p), float(q)) for p, q, _ in d["asks"]]


def _kr_buch(c):
    d = _get(KR + "/Depth", pair=KR_PAIR[c], count="500")["result"]
    v = next(iter(d.values()))
    return [(float(p), float(q)) for p, q, _ in v["bids"]], [(float(p), float(q)) for p, q, _ in v["asks"]]


def druck(bids, asks):
    """Ungleichgewicht im Buch: (Kauf - Verkauf) / (Kauf + Verkauf) in Dollar, je Band um den Mittelkurs."""
    if not bids or not asks:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2
    out = {"mid": mid, "spread_bp": (asks[0][0] - bids[0][0]) / mid * 1e4}
    for band, name in ((0.001, "b10"), (0.005, "b50")):
        kb = sum(p * q for p, q in bids if p >= mid * (1 - band))
        kv = sum(p * q for p, q in asks if p <= mid * (1 + band))
        out[name] = (kb - kv) / (kb + kv) if kb + kv > 0 else None
        out[name + "_usd"] = kb + kv
    return out


def okx(c):
    inst = f"{c.upper()}-USDT-SWAP"
    out = {}
    try:
        out["funding_8h"] = _f(_get(OKX + "/api/v5/public/funding-rate", instId=inst)["data"][0]["fundingRate"])
    except Exception:
        pass
    try:
        out["oi_usd"] = _f(_get(OKX + "/api/v5/public/open-interest", instType="SWAP", instId=inst)["data"][0]["oiUsd"])
    except Exception:
        pass
    try:
        d = _get(OKX + "/api/v5/rubik/stat/contracts/long-short-account-ratio", ccy=c.upper(), period="5m")["data"]
        out["long_short"] = _f(d[0][1])
        out["long_short_1h"] = _f(d[12][1]) if len(d) > 12 else None
    except Exception:
        pass
    try:
        d = _get(OKX + "/api/v5/rubik/stat/taker-volume", ccy=c.upper(), instType="CONTRACTS", period="5m")["data"]
        # [ts, sell, buy]
        def anteil(rows):
            s = sum(float(r[1]) for r in rows)
            b = sum(float(r[2]) for r in rows)
            return b / (b + s) if b + s > 0 else None
        out["taker_buy_5m"] = anteil(d[:1])
        out["taker_buy_15m"] = anteil(d[:3])
        out["taker_buy_1h"] = anteil(d[:12])
    except Exception:
        pass
    try:
        d = _get(OKX + "/api/v5/public/liquidation-orders", instType="SWAP", uly=f"{c.upper()}-USDT", state="filled")["data"]
        jetzt = time.time() * 1000
        liq = {"long_15m": 0.0, "short_15m": 0.0, "long_1h": 0.0, "short_1h": 0.0}
        ct = _ct_wert(c)
        for blk in d:
            for x in blk.get("details", []):
                alter = jetzt - float(x["ts"])
                usd = float(x["sz"]) * ct * float(x["bkPx"])
                ps = x.get("posSide")
                seite = ps if ps in ("long", "short") else ("long" if x.get("side") == "sell" else "short")
                if alter <= 3600e3:
                    liq[seite + "_1h"] += usd
                if alter <= 900e3:
                    liq[seite + "_15m"] += usd
        out["liq"] = {k: round(v) for k, v in liq.items()}
    except Exception:
        pass
    try:
        b, a = _okx_buch(c)
        out["book"] = druck(b, a)
    except Exception:
        pass
    return out


_CT = {}


def _ct_wert(c):
    """Kontraktgroesse der OKX-Perps (z. B. 0.01 BTC je Kontrakt)."""
    if c not in _CT:
        try:
            d = _get(OKX + "/api/v5/public/instruments", instType="SWAP", instId=f"{c.upper()}-USDT-SWAP")["data"][0]
            _CT[c] = float(d["ctVal"])
        except Exception:
            return 0.0
    return _CT[c]


def spot_buecher(c):
    out = {}
    for name, fn, tab in (("coinbase", _cb_buch, CB_PAIR), ("kraken", _kr_buch, KR_PAIR)):
        if c not in tab:
            continue
        try:
            out[name] = druck(*fn(c))
        except Exception:
            pass
    return out


def binance_spot(c):
    try:
        d = _get(BNV + "/ticker/price", symbol=f"{c.upper()}USDT")
        return _f(d.get("price"))
    except Exception:
        return None


# ── Zusammenfassen ───────────────────────────────────────────────────────

def _aenderung(c, oi, sek):
    v = _VERLAUF[c]
    jetzt = time.time()
    alt = None
    for ts, x in v:
        if jetzt - ts >= sek:
            alt = x
    if alt and oi:
        return oi / alt - 1
    return None


def _mittel(werte):
    w = [x for x in werte if x is not None]
    return sum(w) / len(w) if w else None


def coin(c, hl):
    o = okx(c)
    sp = spot_buecher(c)
    h = hl.get(c, {})
    oi = (h.get("oi_usd") or 0) + (o.get("oi_usd") or 0) or None
    if oi:
        _VERLAUF[c].append((time.time(), oi))
    bnb = binance_spot(c)
    cb_mid = (sp.get("coinbase") or {}).get("mid")
    buecher = {"okx": o.get("book"), **sp}
    druck10 = _mittel([(b or {}).get("b10") for b in buecher.values()])
    druck50 = _mittel([(b or {}).get("b50") for b in buecher.values()])
    f_hl = h.get("funding_1h")
    f_okx = o.get("funding_8h")
    funding_jahr = _mittel([f_hl * 24 * 365 if f_hl is not None else None,
                            f_okx * 3 * 365 if f_okx is not None else None])
    return {
        "coin": c, "ts": time.time(),
        "price": h.get("mark") or (buecher.get("okx") or {}).get("mid"),
        "funding": {"hyperliquid_1h": f_hl, "okx_8h": f_okx, "annual": funding_jahr},
        "oi": {"usd": oi, "hyperliquid_usd": h.get("oi_usd"), "okx_usd": o.get("oi_usd"),
               "chg_15m": _aenderung(c, oi, 900), "chg_1h": _aenderung(c, oi, 3600)},
        "long_short": {"now": o.get("long_short"), "1h_ago": o.get("long_short_1h")},
        "taker_buy": {"5m": o.get("taker_buy_5m"), "15m": o.get("taker_buy_15m"), "1h": o.get("taker_buy_1h")},
        "book": {"pressure_10bp": druck10, "pressure_50bp": druck50, "venues": buecher},
        "liquidations": o.get("liq"),
        "basis": {"perp_premium": h.get("premium"),
                  "usdt_premium": (bnb / cb_mid - 1) if bnb and cb_mid else None,
                  "binance_usdt": bnb},
        "vol_24h_usd": h.get("vol_24h_usd"),
    }


def stimmung(d):
    """Ein Wert von -1 (klar Down) bis +1 (klar Up) aus den Boersendaten, rein beschreibend.
    Gewichte sind bewusst einfach und werden NICHT fuer Handelsentscheidungen genutzt,
    bis die Auswertung zeigt, was traegt."""
    teile = []
    b = d["book"].get("pressure_10bp")
    if b is not None:
        teile.append(max(-1, min(1, b * 2)))
    t = d["taker_buy"].get("15m")
    if t is not None:
        teile.append(max(-1, min(1, (t - 0.5) * 6)))
    liq = d.get("liquidations") or {}
    lo, sh = liq.get("long_15m", 0), liq.get("short_15m", 0)
    if lo + sh > 0:
        teile.append((sh - lo) / (sh + lo) * 0.6)
    if not teile:
        return None
    return round(sum(teile) / len(teile), 3)


def _pg():
    pg = storage._init_pg()
    if not pg:
        return None
    if not _SCHEMA[0]:
        pg.execute("""CREATE TABLE IF NOT EXISTS boersen_snap (
            ts TIMESTAMPTZ NOT NULL, coin TEXT NOT NULL, data JSONB,
            PRIMARY KEY (coin, ts))""")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_boersen_ts ON boersen_snap(ts)")
        _SCHEMA[0] = True
    return pg


def tick(force=False):
    if not force and time.time() - _LETZT[0] < TAKT:
        return None
    _LETZT[0] = time.time()
    try:
        hl = hyperliquid()
    except Exception as e:
        log.debug(f"boersen hyperliquid: {e}")
        hl = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        daten = list(ex.map(lambda c: coin(c, hl), COINS))
    for d in daten:
        d["mood"] = stimmung(d)
    live = {"ts": time.time(), "coins": {d["coin"]: d for d in daten}}
    try:
        storage.kv_set("boersen_live", json.dumps(live, default=str))
    except Exception as e:
        log.debug(f"boersen kv: {e}")
    try:
        pg = _pg()
        if pg:
            for d in daten:
                pg.execute("INSERT INTO boersen_snap (ts, coin, data) VALUES (now(), %s, %s) ON CONFLICT DO NOTHING",
                           (d["coin"], json.dumps(d, default=str)))
            if int(time.time()) % 3600 < TAKT:
                pg.execute("DELETE FROM boersen_snap WHERE ts < now() - make_interval(days => %s)", (HALTEN_TAGE,))
    except Exception as e:
        log.debug(f"boersen pg: {e}")
    return live


def lage():
    try:
        return json.loads(storage.kv_get("boersen_live") or "{}")
    except Exception:
        return {}


def sentiment():
    """Fuer /v1/sentiment: Boersen, Polymarket-Masse und Engine je Coin nebeneinander."""
    b = lage()
    try:
        k = json.loads(storage.kv_get("krypto_live") or "{}")
    except Exception:
        k = {}
    maerkte = k.get("markets", []) or []
    coins = {}
    spaltung = []
    for c in COINS:
        d = (b.get("coins") or {}).get(c) or {}
        tfs = {}
        for m in maerkte:
            if m.get("a") != c:
                continue
            crowd, eng = m.get("mid_up"), m.get("p_up")
            tfs[m["tf"]] = {"crowd_up": crowd, "engine_up": eng,
                            "gap": round(eng - crowd, 4) if crowd is not None and eng is not None else None,
                            "slug": m.get("slug"), "end": m.get("end")}
            if crowd is not None and eng is not None:
                spaltung.append({"coin": c, "tf": m["tf"], "crowd_up": crowd, "engine_up": eng,
                                 "gap": round(eng - crowd, 4), "slug": m.get("slug"), "end": m.get("end")})
        liq = d.get("liquidations") or {}
        coins[c] = {
            "price": d.get("price"), "mood": d.get("mood"),
            "funding_annual": (d.get("funding") or {}).get("annual"),
            "oi_usd": (d.get("oi") or {}).get("usd"),
            "oi_chg_15m": (d.get("oi") or {}).get("chg_15m"), "oi_chg_1h": (d.get("oi") or {}).get("chg_1h"),
            "long_short": (d.get("long_short") or {}).get("now"),
            "long_short_1h_ago": (d.get("long_short") or {}).get("1h_ago"),
            "taker_buy_5m": (d.get("taker_buy") or {}).get("5m"),
            "taker_buy_15m": (d.get("taker_buy") or {}).get("15m"),
            "taker_buy_1h": (d.get("taker_buy") or {}).get("1h"),
            "book_pressure": (d.get("book") or {}).get("pressure_10bp"),
            "book_pressure_wide": (d.get("book") or {}).get("pressure_50bp"),
            "book_venues": sorted(k2 for k2, v in ((d.get("book") or {}).get("venues") or {}).items() if v),
            "liq_long_1h": liq.get("long_1h"), "liq_short_1h": liq.get("short_1h"),
            "liq_long_15m": liq.get("long_15m"), "liq_short_15m": liq.get("short_15m"),
            "usdt_premium": (d.get("basis") or {}).get("usdt_premium"),
            "polymarket": tfs,
        }
    crowd_all = {}
    for tf in ("5m", "15m", "4h"):
        w = [m.get("mid_up") for m in maerkte if m.get("tf") == tf and m.get("mid_up") is not None]
        e = [m.get("p_up") for m in maerkte if m.get("tf") == tf and m.get("p_up") is not None]
        crowd_all[tf] = {"crowd_up": round(sum(w) / len(w), 4) if w else None,
                         "engine_up": round(sum(e) / len(e), 4) if e else None, "n": len(w)}
    moods = [v["mood"] for v in coins.values() if v.get("mood") is not None]
    spaltung.sort(key=lambda x: -abs(x["gap"]))
    return {"updated": b.get("ts"), "stale": time.time() - float(b.get("ts") or 0) > 300,
            "crypto_updated": k.get("ts"), "coins": coins, "crowd": crowd_all,
            "exchange_mood": round(sum(moods) / len(moods), 3) if moods else None,
            "splits": spaltung[:10],
            "sources": ["Hyperliquid", "OKX", "Coinbase", "Kraken", "Binance spot", "Polymarket"]}
