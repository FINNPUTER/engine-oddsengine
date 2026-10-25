"""
krypto.py: die Engine liest Krypto. Seit 2.27.

Polymarket hat Up/Down-Maerkte fuer BTC, ETH, SOL, XRP, DOGE, BNB und HYPE in
Fenstern von 5 Minuten, 15 Minuten und 4 Stunden. Seit 2026 entscheidet nicht
mehr der Preis am Ende, sondern der Durchschnitt ueber das ganze Fenster
(Chainlink TWAP): Up, wenn der Durchschnitt >= Startpreis.

Das macht den Ausgang mitten im Fenster gut rechenbar:

    Endschnitt = f * A + (1 - f) * Mittel(rest)
    f  = vergangener Anteil, A = Schnitt bisher, P = Preis jetzt
    Mittel(rest) ~ Normal(P, P * sigma * sqrt(tau / 3))      (Brownsche Bewegung)
    P(Up) = Phi((f*A + (1-f)*P - S0) / sd)

Seit 2.27.2 (Engine v2): Startpreis und Schnitt wie bei Chainlink (60-s-TWAP-Stream,
also Trapez-Gewichte statt flach), Basis Hyperliquid/Chainlink realistisch, und die
Engine-Zahl wird an den Markt gekoppelt: P = Markt + k * (Modell - Markt), k je
Zeitfenster gelernt aus den abgerechneten Fenstern. engine und late nur noch dort,
wo sie tragen (CRYPTO_ENGINE_TFS, CRYPTO_LATE_TFS), late nur, wenn auch der Markt
den Favoriten sieht. cheap ersetzt durch sniper (strenger lag), both heisst Gabagool.

Fuenf Module laufen als Papierhandel mit, jedes mit eigener oeffentlicher Bilanz.
Gekauft wird immer zum echten Ask aus dem Orderbuch, die Taker-Gebuehr
(0.07 * p * (1 - p) je Anteil) geht ab. Es wird NICHTS echt gehandelt.

    lag      starke Bewegung an der Boerse, Orderbuch hinkt hinterher (alter crypto_lag v3)
    engine   Modell gegen Markt: Einstieg, wenn Modell - Ask - Gebuehr >= EDGE
    both     beide Seiten, wenn Ask(Up) + Ask(Down) + Gebuehren < 1
    sniper   wie lag, aber strenger, nur 15 min und 4 h (ersetzt cheap seit 2.27.2)
    late     Favorit kurz vor Schluss, Modell >= 90 %, Ask <= 95 Cent

Dazu die Kalibrierung: an festen Punkten im Fenster (25, 50, 75, 90 %) wird die
Engine-Wahrscheinlichkeit und der Marktpreis festgehalten und nach dem Ausgang
gewertet. So sieht jeder, ob die Engine besser liegt als der Markt.

Laeuft als eigener Railway-Dienst `crypto` (Procfile: crypto: python krypto_main.py),
damit der Wetter-Worker unberuehrt bleibt. Speicher: kv-Tabelle (Postgres).

    krypto_live    Momentaufnahme aller Maerkte, alle paar Sekunden
    krypto_open    offene Fenster mit Einstiegen und Kalibrierpunkten
    krypto_book    Bilanzen, Kalibrierung, Tage, letzte Trades
"""
import bisect
import json
import math
import os
import time
from collections import deque
from datetime import datetime, timezone

import requests

import storage
from logger import log

UA = {"User-Agent": "Mozilla/5.0 (OddsEngine crypto)", "Accept": "application/json"}
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
HL = "https://api.hyperliquid.xyz/info"

ASSETS = {"btc": "BTC", "eth": "ETH", "sol": "SOL", "xrp": "XRP",
          "doge": "DOGE", "bnb": "BNB", "hype": "HYPE"}
NAMES = {"btc": "Bitcoin", "eth": "Ethereum", "sol": "Solana", "xrp": "XRP",
         "doge": "Dogecoin", "bnb": "BNB", "hype": "Hyperliquid"}
TFS = {"5m": 300, "15m": 900, "4h": 14400}

FEE_RATE = float(os.getenv("CRYPTO_FEE_RATE", "0.07"))
EDGE = float(os.getenv("CRYPTO_EDGE", "0.05"))            # engine: Mindestvorsprung je Anteil
SHARES = 10.0                                              # Papiergroesse je Einstieg
BASIS = float(os.getenv("CRYPTO_BASIS", "0.0003"))         # Abweichung Referenz zu Chainlink
MODEL_V = "2.27.2"                                          # Engine v2, fuer das interne Aenderungslog
K_DEFAULT = float(os.getenv("CRYPTO_K", "0.35"))           # Kopplung an den Markt, bis gelernt
K_MIN_N = int(os.getenv("CRYPTO_K_MIN_N", "150"))
ENGINE_TFS = [x.strip() for x in os.getenv("CRYPTO_ENGINE_TFS", "15m,4h").split(",") if x.strip()]
LATE_TFS = [x.strip() for x in os.getenv("CRYPTO_LATE_TFS", "15m,4h").split(",") if x.strip()]
CHECKS = (0.25, 0.5, 0.75, 0.9)
LATE = {"5m": 60, "15m": 120, "4h": 900}                   # Sekunden vor Schluss fuer "late"
MODULES = ("engine", "lag", "sniper", "both", "late")
MODULE_INFO = {
    "engine": ("Engine edge", "Buys the side the engine rates at least 5 cents higher than the "
               "ask after fees. Once per window."),
    "both":   ("Gabagool", "Buys Up and Down together when both asks plus fees cost less "
               "than one dollar. Locks in the gap whatever happens."),
    "sniper": ("Lag sniper", "Same idea as Crypto lag, only stricter: a bigger move, confirmed "
               "over five minutes, and the book at least 4 cents behind. 15 minute and 4 hour "
               "windows only."),
    "cheap":  ("Cheap side", "Buys the side that trades at 10 cents or less. Small stake, "
               "big payout when it lands."),
    "lag":    ("Crypto lag", "The coin moves hard on the exchange, the Polymarket price has not "
               "caught up yet. Buys the side of the move while the book still lags. Once per window."),
    "late":   ("Late favourite", "In the last stretch of the window, buys the side the engine "
               "rates 90 % or higher when the ask is 95 cents or less."),
}

_s = requests.Session()
_s.headers.update(UA)

# Speicher im Prozess
PREISE = {a: deque(maxlen=4000) for a in ASSETS}            # (ts, preis) alle paar Sekunden
_MKT = {}       # slug -> {"up": token, "down": token, "cid": .., "ok": bool, "ts": abruf}
_OFFEN = {}     # slug -> Fensterzustand (siehe _fenster)
_BUCH = None    # Bilanz, aus kv geladen


# ── kleine Helfer ──────────────────────────────────────────────────────

def gebuehr(p):
    """Taker-Gebuehr je Anteil zum Preis p."""
    p = max(0.0, min(1.0, float(p)))
    return FEE_RATE * p * (1 - p)


def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _j(url, **kw):
    r = _s.get(url, timeout=kw.pop("timeout", 8), **kw)
    r.raise_for_status()
    return r.json()


def _jetzt():
    return time.time()


def _tag(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


# ── Preise (Hyperliquid, ein Aufruf fuer alle) ─────────────────────────

def preise_holen():
    """Mittelkurse aller sieben Coins. Hyperliquid liefert alle in einem Aufruf."""
    try:
        r = _s.post(HL, json={"type": "allMids"}, timeout=6)
        r.raise_for_status()
        mids = r.json()
    except Exception as e:
        log.debug(f"krypto: allMids {e}")
        return {}
    out = {}
    for a, coin in ASSETS.items():
        try:
            v = float(mids.get(coin))
            if v > 0:
                out[a] = v
        except Exception:
            pass
    return out


def kerzen(a, start_ms, ende_ms):
    """1-Minuten-Kerzen fuer den Rueckblick nach einem Neustart."""
    try:
        r = _s.post(HL, json={"type": "candleSnapshot", "req": {
            "coin": ASSETS[a], "interval": "1m",
            "startTime": int(start_ms), "endTime": int(ende_ms)}}, timeout=8)
        r.raise_for_status()
        return [(int(k["t"]) / 1000.0, float(k["o"]), float(k["c"])) for k in r.json()]
    except Exception as e:
        log.debug(f"krypto: kerzen {a} {e}")
        return []


# Crypto lag (aus dem alten Bot, crypto_lag v3): starke Bewegung ueber 60 s,
# bestaetigt durch 5 Minuten, deutlich ueber dem Rauschen, und das Orderbuch
# hinkt noch hinterher.
LAG_MOVE_60 = {"5m": 0.0012, "15m": 0.0008, "4h": 0.0008}
LAG_MOVE_300 = 0.0010
LAG_Z = {"5m": 2.0, "15m": 1.5, "4h": 1.5}
LAG_EDGE = float(os.getenv("CRYPTO_LAG_EDGE", "0.02"))


def rendite(a, sek, jetzt=None):
    """Log-Rendite der letzten sek Sekunden, None ohne Daten."""
    jetzt = jetzt or _jetzt()
    pts = PREISE[a]
    if len(pts) < 3:
        return None
    alt = [p for p in pts if p[0] <= jetzt - sek]
    if not alt or jetzt - sek - alt[-1][0] > 20:
        return None
    return math.log(pts[-1][1] / alt[-1][1])


def sigma(a, fenster_s=1800):
    """Schwankung je Sekunde (Log-Rendite) aus den letzten 30 Minuten."""
    pts = [p for p in PREISE[a] if p[0] >= _jetzt() - fenster_s]
    if len(pts) < 20:
        return {"btc": 2.2e-4, "eth": 2.8e-4}.get(a, 3.5e-4)
    rs, dts = [], []
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        dt = t1 - t0
        if dt <= 0 or dt > 60:
            continue
        rs.append(math.log(p1 / p0) / math.sqrt(dt))
    if len(rs) < 10:
        return 3e-4
    m = sum(rs) / len(rs)
    v = sum((r - m) ** 2 for r in rs) / (len(rs) - 1)
    return max(math.sqrt(v), 2e-5)


# ── Maerkte und Orderbuecher ───────────────────────────────────────────

def slug_von(a, tf, start):
    return f"{a}-updown-{tf}-{int(start)}"


def markt(slug):
    """Token-IDs eines Fensters. Einmal je Fenster, Fehlschlag 60 s gemerkt."""
    c = _MKT.get(slug)
    if c and (c["ok"] or _jetzt() - c["ts"] < 60):
        return c if c["ok"] else None
    try:
        ev = _j(f"{GAMMA}/events", params={"slug": slug})
        m = ev[0]["markets"][0]
        toks = json.loads(m["clobTokenIds"])
        outs = [o.lower() for o in json.loads(m.get("outcomes") or '["Up","Down"]')]
        up = toks[outs.index("up")] if "up" in outs else toks[0]
        dn = toks[outs.index("down")] if "down" in outs else toks[1]
        c = {"up": up, "down": dn, "cid": m.get("conditionId"), "ok": True, "ts": _jetzt(),
             "title": ev[0].get("title") or ""}
    except Exception as e:
        log.debug(f"krypto: markt {slug} {e}")
        c = {"ok": False, "ts": _jetzt()}
    _MKT[slug] = c
    if len(_MKT) > 600:
        for k in sorted(_MKT, key=lambda k: _MKT[k]["ts"])[:200]:
            _MKT.pop(k, None)
    return c if c["ok"] else None


def buecher(tokens):
    """Bestes Bid/Ask mit Menge fuer viele Tokens in einem Aufruf."""
    out = {}
    if not tokens:
        return out
    try:
        r = _s.post(f"{CLOB}/books", json=[{"token_id": t} for t in tokens], timeout=8)
        r.raise_for_status()
        for b in r.json():
            bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids") or []]
            asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks") or []]
            bb = max(bids, default=(None, 0))
            ba = min(asks, default=(None, 0))
            out[str(b.get("asset_id"))] = {"bid": bb[0], "bid_size": bb[1],
                                           "ask": ba[0], "ask_size": ba[1]}
    except Exception as e:
        log.debug(f"krypto: books {e}")
    return out


# ── Modell ──────────────────────────────────────────────────────────────

def _gewicht(u, S, E):
    """Gewicht eines Zeitpunkts im Chainlink-Schnitt: Mittel des 60-s-TWAP-Streams
    ueber [S, E] = Preis ueber [S-60, E] mit Rampe am Anfang und am Ende."""
    if u < S - 60 or u > E:
        return 0.0
    if u < S:
        return (u - (S - 60)) / 60.0
    if u > E - 60:
        return (E - u) / 60.0
    return 1.0


def _preis_bei(ts, ps, u):
    i = bisect.bisect_left(ts, u)
    if i <= 0:
        return ps[0]
    if i >= len(ts):
        return ps[-1]
    t0, t1 = ts[i - 1], ts[i]
    if t1 <= t0:
        return ps[i]
    return ps[i - 1] + (ps[i] - ps[i - 1]) * (u - t0) / (t1 - t0)


def modell(a, start, dauer, s0, jetzt=None):
    """P(Up) fuer ein Fenster (roh, ohne Markt). None, wenn Daten fehlen."""
    jetzt = jetzt or _jetzt()
    S, E = start, start + dauer
    now = min(max(jetzt, S), E)
    pts = [p for p in PREISE[a] if S - 75 <= p[0] <= now]
    if s0 is None or not pts:
        return None
    ts = [p[0] for p in pts]
    ps = [p[1] for p in pts]
    p_now = ps[-1]
    # bekannter Teil
    t0 = S - 60
    n1 = max(1, int((now - t0) / max(1.0, (now - t0) / 600)))
    h1 = (now - t0) / n1 if now > t0 else 0.0
    bek, wk = 0.0, 0.0
    for i in range(n1):
        u = t0 + (i + 0.5) * h1
        g = _gewicht(u, S, E)
        if g:
            bek += g * _preis_bei(ts, ps, u) * h1
            wk += g * h1
    # offener Teil: Erwartung p_now, Varianz ueber W(s) = Gewicht ab s bis Ende
    tau = E - now
    wf, var_int = 0.0, 0.0
    if tau > 0:
        n2 = max(1, int(tau / max(1.0, tau / 600)))
        h2 = tau / n2
        gs = [_gewicht(now + (i + 0.5) * h2, S, E) for i in range(n2)]
        W = 0.0
        for g in reversed(gs):
            W += g * h2
            var_int += W * W * h2
        wf = W
    D = wk + wf
    if D <= 0:
        return None
    mu = (bek + p_now * wf) / D
    sd_rest = p_now * sigma(a) * math.sqrt(var_int) / D
    sd = math.sqrt(sd_rest ** 2 + (p_now * BASIS) ** 2)
    avg = bek / wk if wk > 0 else p_now
    pu = _phi((mu - s0) / sd) if sd > 0 else (1.0 if mu >= s0 else 0.0)
    return {"p_up": max(0.003, min(0.997, pu)), "avg": avg, "now": p_now,
            "f": (now - S) / dauer, "mu": mu, "sd": sd}


_K = {"ts": 0.0, "k": {}}


def k_wert(tf):
    """Kopplung an den Markt je Zeitfenster, gelernt aus den abgerechneten Fenstern:
    das k aus 0..1, mit dem Markt + k * (Modell - Markt) den kleinsten Brier hat."""
    if _jetzt() - _K["ts"] > 600:
        neu = {}
        for t, rows in (buch().get("fit") or {}).items():
            if len(rows) < K_MIN_N:
                continue
            best = None
            for i in range(21):
                k = i / 20
                br = sum((m + k * (r - m) - y) ** 2 for r, m, y in rows) / len(rows)
                if best is None or br < best[1]:
                    best = (k, br)
            neu[t] = best[0]
        _K["k"], _K["ts"] = neu, _jetzt()
    return _K["k"].get(tf, K_DEFAULT)


def gekoppelt(roh, mid, tf):
    if mid is None:
        return roh
    return max(0.003, min(0.997, mid + k_wert(tf) * (roh - mid)))


# ── Buchfuehrung ────────────────────────────────────────────────────────

def _leer_buch():
    return {"stats": {}, "days": {}, "calib": {}, "trades": [], "windows": 0,
            "resolved": 0, "since": _jetzt(), "fit": {}, "changes": []}


def _aenderung_vermerken():
    """Internes Aenderungslog: wann welche Engine-Version live ging (nicht oeffentlich)."""
    b = buch()
    ch = b.setdefault("changes", [])
    if not any(c.get("v") == MODEL_V for c in ch):
        ch.append({"v": MODEL_V, "ts": round(_jetzt()), "windows": b.get("windows", 0),
                   "resolved": b.get("resolved", 0),
                   "note": "Engine v2: Chainlink-TWAP-Gewichte, Startpreis 60-s-Schnitt, "
                           "Basis 3 bp, Kopplung an den Markt, engine/late nur "
                           + ",".join(ENGINE_TFS) + ", late nur mit Markt, cheap -> sniper, "
                           "both -> Gabagool"})
        log.info(f"krypto: Engine {MODEL_V} live, im Aenderungslog vermerkt")


def buch():
    global _BUCH
    if _BUCH is None:
        try:
            roh = storage.kv_get("krypto_book")
            _BUCH = json.loads(roh) if roh else _leer_buch()
        except Exception:
            _BUCH = _leer_buch()
        try:
            roh = storage.kv_get("krypto_open")
            for k, v in (json.loads(roh) if roh else {}).items():
                _OFFEN.setdefault(k, v)
        except Exception:
            pass
    return _BUCH


def _einstieg(w, modul, seite, ask, p_modell=None, groesse=SHARES):
    """Papier-Einstieg, einmal je Modul und Fenster (both: zwei Seiten)."""
    w["trades"].append({"m": modul, "side": seite, "price": round(ask, 4), "v": MODEL_V,
                        "shares": groesse, "fee": round(gebuehr(ask) * groesse, 5),
                        "p": None if p_modell is None else round(p_modell, 4),
                        "t": round(_jetzt() - w["start"])})
    w["done"].append(modul)


def _werten(w, sieger, quelle):
    """Fenster abschliessen: Trades und Kalibrierung ins Buch."""
    b = buch()
    b["resolved"] = b.get("resolved", 0) + 1
    tag = _tag(w["start"] + w["dauer"])
    for t in w["trades"]:
        kosten = t["price"] * t["shares"] + t["fee"]
        ertrag = t["shares"] if t["side"] == sieger else 0.0
        pnl = ertrag - kosten
        keys = [f"{t['m']}|{w['tf']}|{w['a']}", f"{t['m']}|{w['tf']}|*", f"{t['m']}|*|*"]
        if t.get("v") == MODEL_V:
            keys += ["v2:" + k for k in keys]
        for key in keys:
            s = b["stats"].setdefault(key, {"n": 0, "win": 0, "cost": 0.0, "pnl": 0.0, "fee": 0.0})
            s["n"] += 1
            s["win"] += 1 if ertrag > 0 else 0
            s["cost"] = round(s["cost"] + kosten, 4)
            s["pnl"] = round(s["pnl"] + pnl, 4)
            s["fee"] = round(s["fee"] + t["fee"], 4)
        d = b["days"].setdefault(tag, {})
        for dk in ([t["m"], "v2:" + t["m"]] if t.get("v") == MODEL_V else [t["m"]]):
            dm = d.setdefault(dk, {"n": 0, "pnl": 0.0, "cost": 0.0})
            dm["n"] += 1
            dm["pnl"] = round(dm["pnl"] + pnl, 4)
            dm["cost"] = round(dm["cost"] + kosten, 4)
        b["trades"].insert(0, {"m": t["m"], "a": w["a"], "tf": w["tf"], "side": t["side"],
                               "price": t["price"], "p": t["p"], "won": ertrag > 0,
                               "pnl": round(pnl, 3), "cost": round(kosten, 3),
                               "end": int(w["start"] + w["dauer"]), "src": quelle,
                               "v": t.get("v", "2.27.0")})
    del b["trades"][150:]
    for k, c in w["calib"].items():
        ck = f"{w['tf']}|{k}"
        s = b["calib"].setdefault(ck, {"n": 0, "eng_hit": 0, "mkt_hit": 0,
                                        "eng_brier": 0.0, "mkt_brier": 0.0})
        y = 1.0 if sieger == "up" else 0.0
        s["n"] += 1
        s["eng_hit"] += 1 if (c["e"] >= 0.5) == (y == 1.0) else 0
        s["mkt_hit"] += 1 if (c["m"] >= 0.5) == (y == 1.0) else 0
        s["eng_brier"] = round(s["eng_brier"] + (c["e"] - y) ** 2, 5)
        s["mkt_brier"] = round(s["mkt_brier"] + (c["m"] - y) ** 2, 5)
        if "r" in c:
            fit = b.setdefault("fit", {}).setdefault(w["tf"], [])
            fit.append([c["r"], c["m"], y])
            del fit[:-3000]
    for alt in sorted(b["days"])[:-90]:
        b["days"].pop(alt, None)


def ausgang(slug):
    """'up' / 'down' sobald Polymarket aufgeloest hat, sonst None."""
    try:
        ev = _j(f"{GAMMA}/events", params={"slug": slug})
        m = ev[0]["markets"][0]
        preise = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        outs = [o.lower() for o in json.loads(m.get("outcomes") or '["Up","Down"]')]
        if len(preise) == 2 and max(preise) >= 0.99 and min(preise) <= 0.01:
            return outs[preise.index(max(preise))]
    except Exception as e:
        log.debug(f"krypto: ausgang {slug} {e}")
    return None


# ── ein Durchlauf ───────────────────────────────────────────────────────

def _fenster(a, tf, start):
    slug = slug_von(a, tf, start)
    w = _OFFEN.get(slug)
    if w is None:
        w = {"slug": slug, "a": a, "tf": tf, "start": start, "dauer": TFS[tf],
             "s0": None, "full": False, "trades": [], "done": [], "calib": {},
             "min_sum": None}
        _OFFEN[slug] = w
        buch()["windows"] = buch().get("windows", 0) + 1
    return w


def _startpreis(a, w):
    """Startpreis wie bei Chainlink: der 60-s-TWAP zum Fensterbeginn, also der
    Schnitt ueber die Minute davor. Eigene Messung, wenn wir dabei waren, sonst Kerze."""
    if w["s0"] is not None:
        return w["s0"]
    S = w["start"]
    nah = [p for p in PREISE[a] if S - 62 <= p[0] <= S + 2]
    if len(nah) >= 4 and nah[0][0] <= S - 45 and nah[-1][0] >= S - 8:
        fl, zt = 0.0, 0.0
        for (t0, p0), (t1, p1) in zip(nah, nah[1:]):
            fl += (p0 + p1) / 2 * (t1 - t0)
            zt += t1 - t0
        w["s0"] = fl / zt if zt > 0 else nah[-1][1]
        w["full"] = True
        return w["s0"]
    if _jetzt() - S > 8:
        ks = kerzen(a, (S - 60) * 1000, _jetzt() * 1000)
        if ks:
            vor = [k for k in ks if abs(k[0] - (S - 60)) < 1]
            w["s0"] = (vor[0][1] + vor[0][2]) / 2 if vor else ks[0][1]
            vorhanden = {round(p[0]) for p in PREISE[a]}
            for t, o, c in ks:
                if round(t + 59) not in vorhanden and t + 59 < _jetzt() - 5:
                    PREISE[a].append((t + 59, c))
            PREISE[a] = deque(sorted(PREISE[a]), maxlen=4000)
            w["full"] = False
    return w["s0"]


def _spark(a, start, n=60):
    pts = [round(p[1], 6) for p in PREISE[a] if p[0] >= start]
    schritt = max(1, len(pts) // n)
    out = pts[::schritt]
    if pts and out[-1] != pts[-1]:
        out.append(pts[-1])
    return out[-(n + 1):]


def _rueckblick(a, jetzt):
    """Nach einem Neustart: die letzten 4 Stunden aus Minutenkerzen nachladen."""
    ks = kerzen(a, (jetzt - TFS["4h"] - 120) * 1000, jetzt * 1000)
    for t, o, c in ks:
        if t + 59 < jetzt - 5:
            PREISE[a].append((t + 59, c))


def tick():
    """Ein Durchlauf: Preise, Fenster, Orderbuecher, Modell, Module, Auswertung."""
    jetzt = _jetzt()
    _aenderung_vermerken()
    px = preise_holen()
    for a, v in px.items():
        if not PREISE[a]:
            _rueckblick(a, jetzt)
        PREISE[a].append((jetzt, v))

    aktiv = []
    for a in ASSETS:
        for tf, dauer in TFS.items():
            start = int(jetzt - jetzt % dauer)
            mk = markt(slug_von(a, tf, start))
            if not mk:
                continue
            w = _fenster(a, tf, start)
            aktiv.append((a, tf, w, mk))

    bk = buecher([t for _, _, _, mk in aktiv for t in (mk["up"], mk["down"])])
    live = []
    for a, tf, w, mk in aktiv:
        up, dn = bk.get(str(mk["up"]), {}), bk.get(str(mk["down"]), {})
        s0 = _startpreis(a, w)
        m = modell(a, w["start"], w["dauer"], s0, jetzt) if a in px else None
        rest = w["start"] + w["dauer"] - jetzt
        ask_u, ask_d = up.get("ask"), dn.get("ask")
        bid_u, bid_d = up.get("bid"), dn.get("bid")
        mid_u = None
        if bid_u is not None and ask_u is not None:
            mid_u = (bid_u + ask_u) / 2
        elif ask_d is not None and bid_d is not None:
            mid_u = 1 - (bid_d + ask_d) / 2

        signal = None
        pr = None
        if m:
            pr = m["p_up"]
            m["p_up"] = gekoppelt(pr, mid_u, tf)
        if m and ask_u and ask_d:
            pu = m["p_up"]
            f = m["f"]
            edge_u = pu - ask_u - gebuehr(ask_u)
            edge_d = (1 - pu) - ask_d - gebuehr(ask_d)
            if edge_u >= edge_d and edge_u > 0:
                signal = {"side": "up", "edge": round(edge_u, 4)}
            elif edge_d > 0:
                signal = {"side": "down", "edge": round(edge_d, 4)}
            # Lernlabor (seit 2.37, intern): Merkmale an festen Punkten, Schattentrades
            try:
                import lernen
                lernen.punkt(w, a, tf, m, pr, mid_u, ask_u, ask_d, bid_u, jetzt)
            except Exception as e:
                log.debug(f"krypto lernen: {e}")
            # Kalibrierpunkte
            for c in CHECKS:
                k = str(c)
                if k not in w["calib"] and f >= c and mid_u is not None and w["full"]:
                    w["calib"][k] = {"e": round(pu, 4), "r": round(pr, 4), "m": round(mid_u, 4)}
            summe = ask_u + ask_d + gebuehr(ask_u) + gebuehr(ask_d)
            w["min_sum"] = round(summe if w["min_sum"] is None else min(w["min_sum"], summe), 4)

            if w["full"]:
                # engine
                ask_sig = signal and (ask_u if signal["side"] == "up" else ask_d)
                if "engine" not in w["done"] and tf in ENGINE_TFS and 0.2 <= f <= 0.95 and signal \
                        and signal["edge"] >= EDGE and 0.05 <= ask_sig <= 0.95:
                    ask = ask_sig
                    _einstieg(w, "engine", signal["side"], ask,
                              pu if signal["side"] == "up" else 1 - pu)
                # both
                if "both" not in w["done"] and summe < 0.995 and f <= 0.95:
                    n = max(1.0, min(SHARES, up.get("ask_size") or 0, dn.get("ask_size") or 0))
                    _einstieg(w, "both", "up", ask_u, None, n)
                    w["done"].remove("both")
                    _einstieg(w, "both", "down", ask_d, None, n)
                # lag
                if "lag" not in w["done"] and 0.1 <= f <= 0.9:
                    r60, r300 = rendite(w["a"], 60, jetzt), rendite(w["a"], 300, jetzt)
                    if r60 is not None and r300 is not None and r60 * r300 > 0:
                        z = abs(r60) / (sigma(w["a"]) * math.sqrt(60))
                        if abs(r60) >= LAG_MOVE_60[tf] and abs(r300) >= LAG_MOVE_300 \
                                and z >= LAG_Z[tf]:
                            seite = "up" if r60 > 0 else "down"
                            p = pr if seite == "up" else 1 - pr
                            ask = ask_u if seite == "up" else ask_d
                            if 0.05 <= ask <= 0.92 and p - ask - gebuehr(ask) >= LAG_EDGE:
                                _einstieg(w, "lag", seite, ask, p)
                # sniper: strenger lag, nur 15 min und 4 h
                if "sniper" not in w["done"] and tf in ("15m", "4h") and 0.1 <= f <= 0.85:
                    r60, r300 = rendite(w["a"], 60, jetzt), rendite(w["a"], 300, jetzt)
                    if r60 is not None and r300 is not None and r60 * r300 > 0:
                        z = abs(r60) / (sigma(w["a"]) * math.sqrt(60))
                        if abs(r60) >= 2 * LAG_MOVE_60[tf] and abs(r300) >= 1.5 * LAG_MOVE_300 \
                                and z >= LAG_Z[tf] + 1.0:
                            seite = "up" if r60 > 0 else "down"
                            p = pr if seite == "up" else 1 - pr
                            ask = ask_u if seite == "up" else ask_d
                            if 0.05 <= ask <= 0.90 and p - ask - gebuehr(ask) >= 2 * LAG_EDGE:
                                _einstieg(w, "sniper", seite, ask, p)
                # late
                if "late" not in w["done"] and tf in LATE_TFS and 0 < rest <= LATE[tf] \
                        and mid_u is not None:
                    for seite, p, ask, mk_s in (("up", pu, ask_u, mid_u),
                                                ("down", 1 - pu, ask_d, 1 - mid_u)):
                        if p >= 0.90 and mk_s >= 0.80 and ask <= 0.95 \
                                and p - ask - gebuehr(ask) >= 0.01:
                            _einstieg(w, "late", seite, ask, p)
                            break

        live.append({
            "slug": w["slug"], "a": a, "name": NAMES[a], "sym": ASSETS[a], "tf": tf,
            "start": w["start"], "end": w["start"] + w["dauer"], "s0": s0,
            "now": px.get(a), "avg": m and round(m["avg"], 6),
            "p_up": m and round(m["p_up"], 4), "p_raw": None if pr is None else round(pr, 4),
            "f": m and round(m["f"], 4),
            "ask_up": ask_u, "ask_down": ask_d, "bid_up": bid_u, "bid_down": bid_d,
            "mid_up": None if mid_u is None else round(mid_u, 4),
            "signal": signal, "full": w["full"],
            "spark": _spark(a, w["start"]),
            "entries": [{"m": t["m"], "side": t["side"], "price": t["price"]} for t in w["trades"]],
        })

    # abgelaufene Fenster werten
    for slug, w in list(_OFFEN.items()):
        ende = w["start"] + w["dauer"]
        if jetzt < ende + 45:
            continue
        if not w["trades"] and not w["calib"] and not w.get("lern") and not w.get("ml"):
            _OFFEN.pop(slug, None)
            continue
        if w.get("next_try", 0) > jetzt:
            continue
        sieger = ausgang(slug)
        quelle = "polymarket"
        if sieger is None and jetzt > ende + 1800:
            # Notfall: eigener Schnitt, als Schaetzung markiert
            m = modell(w["a"], w["start"], w["dauer"], w["s0"], ende)
            sieger = None if not m else ("up" if m["mu"] >= w["s0"] else "down")
            quelle = "estimate"
        if sieger is None:
            if jetzt > ende + 7200:
                _OFFEN.pop(slug, None)
            else:
                w["next_try"] = jetzt + 30
            continue
        _werten(w, sieger, quelle)
        if quelle == "polymarket":
            try:
                import lernen
                lernen.aufloesen(w, sieger)
            except Exception as e:
                log.debug(f"krypto lernen aufloesen: {e}")
        _OFFEN.pop(slug, None)
        log.info(f"krypto: {slug} -> {sieger} ({quelle}), {len(w['trades'])} trades")

    zustand = {"ts": jetzt, "markets": live, "assets": {a: px.get(a) for a in ASSETS}}
    storage.kv_set("krypto_live", json.dumps(zustand, separators=(",", ":")))
    storage.kv_set("krypto_open", json.dumps(_OFFEN, separators=(",", ":")))
    storage.kv_set("krypto_book", json.dumps(buch(), separators=(",", ":")))
    return zustand


# ── fuer die API ────────────────────────────────────────────────────────

def _quote(s):
    if not s or not s.get("n"):
        return None
    return {"n": s["n"], "wins": s["win"], "win_rate": round(s["win"] / s["n"], 4),
            "cost": round(s["cost"], 2), "pnl": round(s["pnl"], 2),
            "roi": round(s["pnl"] / s["cost"], 4) if s["cost"] else 0.0,
            "fees": round(s.get("fee", 0.0), 2)}


def lage():
    """Alles fuer das Terminal in einem Aufruf."""
    try:
        live = json.loads(storage.kv_get("krypto_live") or "{}")
    except Exception:
        live = {}
    try:
        b = json.loads(storage.kv_get("krypto_book") or "{}") or _leer_buch()
    except Exception:
        b = _leer_buch()
    st = b.get("stats", {})
    seit_v2 = next((c["ts"] for c in b.get("changes", []) if c.get("v") == MODEL_V), None)

    def minus(a_, b_):
        if not a_:
            return None
        if not b_:
            return a_
        return {k: a_.get(k, 0) - b_.get(k, 0) for k in ("n", "win", "cost", "pnl", "fee")}

    mods = []
    for m in MODULES:
        name, info = MODULE_INFO[m]
        tage = sorted(b.get("days", {}).items())
        kurve, summe = [], 0.0
        for tag, d in tage:
            if "v2:" + m in d:
                summe += d["v2:" + m]["pnl"]
            kurve.append({"day": tag, "pnl": round(summe, 2)})
        mods.append({"id": m, "name": name, "info": info, "since": seit_v2,
                     "all": _quote(st.get(f"v2:{m}|*|*")),
                     "prev": _quote(minus(st.get(f"{m}|*|*"), st.get(f"v2:{m}|*|*"))),
                     "tf": {tf: _quote(st.get(f"v2:{m}|{tf}|*")) for tf in TFS},
                     "assets": {}, "curve": kurve[-60:]})
    # pro Coin ueber alle tf, seit v2
    for mod in mods:
        for a in ASSETS:
            agg = {"n": 0, "win": 0, "cost": 0.0, "pnl": 0.0, "fee": 0.0}
            for tf in TFS:
                s = st.get(f"v2:{mod['id']}|{tf}|{a}")
                if s:
                    for k in agg:
                        agg[k] += s.get(k, 0)
            mod["assets"][a] = _quote(agg)
    picks = sum(v.get("n", 0) for k, v in st.items() if k.endswith("|*|*") and not k.startswith("v2:"))
    calib = {}
    for k, s in b.get("calib", {}).items():
        tf, c = k.split("|")
        if not s["n"]:
            continue
        calib.setdefault(tf, {})[c] = {
            "n": s["n"], "engine_hit": round(s["eng_hit"] / s["n"], 4),
            "market_hit": round(s["mkt_hit"] / s["n"], 4),
            "engine_brier": round(s["eng_brier"] / s["n"], 4),
            "market_brier": round(s["mkt_brier"] / s["n"], 4)}
    alter = time.time() - float(live.get("ts") or 0)
    return {"updated": live.get("ts"), "stale": alter > 60,
            "markets": live.get("markets", []), "prices": live.get("assets", {}),
            "modules": mods, "calibration": calib, "trades": b.get("trades", [])[:40],
            "windows": b.get("windows", 0), "resolved": b.get("resolved", 0),
            "since": b.get("since"), "since_v2": seit_v2, "picks": picks,
            "fee_rate": FEE_RATE, "edge": EDGE,
            "assets": [{"id": a, "sym": ASSETS[a], "name": NAMES[a]} for a in ASSETS],
            "timeframes": list(TFS)}
