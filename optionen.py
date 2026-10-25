"""
optionen.py: was der Optionsmarkt ueber "BTC/ETH ueber X" weiss. Seit 2.35.

Polymarket ("Bitcoin above ___ on <Tag>?", Binance BTC/USDT, 1-Minuten-Kerze um 12:00 ET)
und Kalshi (KXBTCD/KXETHD, Schnitt der letzten 60 Sekunden des CF Benchmarks RTI)
fragen, ob BTC oder ETH zu einem festen Zeitpunkt ueber einem Kurs steht.

Am Optionsmarkt (Deribit) handeln Profis genau diese Frage mit viel mehr Geld. Aus der
impliziten Volatilitaet je Strike und Laufzeit folgt eine Wahrscheinlichkeit:

    sigma   aus Deribit mark_iv: je Verfall linear in ln(K/F), zwischen zwei Verfallen
            linear in der Gesamtvarianz sigma^2 * t
    F       Forward aus Deribit (underlying_price), auf Binance-USDT umgerechnet,
            wenn der Markt nach Binance BTC/USDT abrechnet
    P(ueber K) = N(d2),  d2 = (ln(F/K) - sigma^2 * tau / 2) / (sigma * sqrt(tau))

Liegt diese Zahl um mehr als OPT_EDGE ueber dem Ask (Gebuehr abgezogen), gibt es einen
Papier-Pick: 10 Anteile zum echten Ask, abgerechnet am echten Ergebnis. Es wird nichts
echt gehandelt. Dazu Kalibrierpunkte (24 h, 6 h, 1 h vor Schluss): Optionsmarkt gegen
Polymarket/Kalshi, Treffer und Brier-Wert nach dem Ausgang.

Speicher: kv optionen_live (Tafel), Postgres optionen_picks und optionen_cal.
Takt OPT_TICK (300 s), laeuft im Dienst crypto.
"""
import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

import storage
from logger import log

UA = {"User-Agent": "Mozilla/5.0 (OddsEngine options)", "Accept": "application/json"}
DERIBIT = "https://www.deribit.com/api/v2/public"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
BNV = "https://data-api.binance.vision/api/v3"

COINS = {"btc": ("BTC", "bitcoin", "KXBTCD"), "eth": ("ETH", "ethereum", "KXETHD")}
TAKT = float(os.getenv("OPT_TICK", "300"))
EDGE = float(os.getenv("OPT_EDGE", "0.05"))
FEE_RATE = float(os.getenv("OPT_FEE_RATE", "0.07"))
MIN_TAU_H = float(os.getenv("OPT_MIN_HOURS", "0.5"))
MAX_TAU_H = float(os.getenv("OPT_MAX_HOURS", "96"))
TAGE_VORAUS = 4
SHARES = 10.0
CHECKS_H = (24.0, 6.0, 1.0)
MONATE = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]

_LETZT = [0.0]
_SCHEMA = [False]
_FL = {}   # letzte Optionsflaeche je Waehrung, fuer lernen.py (seit 2.37)
_S = requests.Session()
_S.headers.update(UA)


def _get(url, **params):
    r = _S.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def _f(x, d=None):
    try:
        return float(x)
    except Exception:
        return d


def gebuehr(p):
    return FEE_RATE * p * (1 - p)


def _n(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# ── Deribit: Volatilitaetsflaeche ─────────────────────────────────────────

def _verfall(code):
    """'24OCT26' -> 2026-10-24 08:00 UTC (Deribit-Verfall)."""
    d = datetime.strptime(code, "%d%b%y").replace(hour=8, tzinfo=timezone.utc)
    return d.timestamp()


def flaeche(waehrung):
    """{verfall_ts: {"F": forward, "pts": [(k, iv), ...] sortiert}} aus allen Optionen."""
    d = _get(DERIBIT + "/get_book_summary_by_currency", currency=waehrung, kind="option")["result"]
    idx = _get(DERIBIT + "/get_index_price", index_name=waehrung.lower() + "_usd")["result"]["index_price"]
    exp = {}
    for x in d:
        teile = x["instrument_name"].split("-")
        if len(teile) != 4:
            continue
        try:
            t = _verfall(teile[1])
        except Exception:
            continue
        K = float(teile[2])
        iv = _f(x.get("mark_iv"))
        F = _f(x.get("underlying_price"))
        if not iv or iv <= 0 or not F:
            continue
        # aus dem Geld: Calls ueber F, Puts unter F (die liquideren Seiten)
        if (teile[3] == "C" and K < F * 0.995) or (teile[3] == "P" and K > F * 1.005):
            continue
        e = exp.setdefault(t, {"F": F, "pts": {}})
        k = math.log(K / F)
        e["pts"].setdefault(round(k, 5), []).append(iv / 100.0)
    out = {}
    for t, e in exp.items():
        pts = sorted((k, sum(v) / len(v)) for k, v in e["pts"].items())
        if len(pts) >= 3:
            out[t] = {"F": e["F"], "pts": pts}
    return {"index": idx, "exp": out}


def _iv_bei(pts, k):
    if k <= pts[0][0]:
        return pts[0][1]
    if k >= pts[-1][0]:
        return pts[-1][1]
    for (k1, v1), (k2, v2) in zip(pts, pts[1:]):
        if k1 <= k <= k2:
            return v1 + (v2 - v1) * (k - k1) / (k2 - k1) if k2 > k1 else v1
    return pts[-1][1]


def wahrscheinlichkeit(fl, K, T, kurs_faktor=1.0):
    """P(Kurs > K zum Zeitpunkt T). kurs_faktor rechnet den Deribit-USD-Forward auf die
    Abrechnungsquelle um (z. B. Binance BTC/USDT)."""
    jetzt = time.time()
    tau = (T - jetzt) / (365.25 * 86400)
    if tau <= 0:
        return None
    ts = sorted(fl["exp"])
    if not ts:
        return None
    vor = [t for t in ts if t <= T]
    nach = [t for t in ts if t >= T]

    def an(t):
        e = fl["exp"][t]
        F = e["F"] * kurs_faktor
        return F, _iv_bei(e["pts"], math.log(K / F))

    if not nach:
        F, iv = an(ts[-1])
    elif not vor or nach[0] == T:
        t2 = nach[0]
        F2, iv2 = an(t2)
        # vor dem ersten Verfall: Forward vom Index zum ersten Verfall interpolieren, IV des ersten Verfalls
        F0 = fl["index"] * kurs_faktor
        w = (T - jetzt) / max(1.0, t2 - jetzt)
        F = F0 + (F2 - F0) * min(1.0, w)
        iv = iv2
    else:
        t1, t2 = vor[-1], nach[0]
        F1, iv1 = an(t1)
        F2, iv2 = an(t2)
        a = (T - t1) / (t2 - t1)
        F = F1 + (F2 - F1) * a
        y1, y2 = (t1 - jetzt) / (365.25 * 86400), (t2 - jetzt) / (365.25 * 86400)
        w1, w2 = iv1 * iv1 * max(y1, 0), iv2 * iv2 * y2
        wv = w1 + (w2 - w1) * a
        iv = math.sqrt(max(wv, 1e-10) / tau)
    sd = iv * math.sqrt(tau)
    if sd <= 0:
        return None
    d2 = (math.log(F / K) - 0.5 * sd * sd) / sd
    return {"p": _n(d2), "iv": iv, "fwd": F, "tau_h": tau * 365.25 * 24}


# ── Maerkte lesen ────────────────────────────────────────────────────────

def polymarket(coin):
    _, name, _ = COINS[coin]
    out = []
    heute = datetime.now(timezone.utc)
    for i in range(TAGE_VORAUS + 1):
        tag = heute + timedelta(days=i)
        slug = f"{name}-above-on-{MONATE[tag.month - 1]}-{tag.day}-{tag.year}"
        try:
            e = _get(f"{GAMMA}/events/slug/{slug}")
        except Exception:
            continue
        if not isinstance(e, dict) or e.get("closed"):
            continue
        for m in e.get("markets", []):
            if m.get("closed") or not m.get("acceptingOrders"):
                continue
            K = _f(str(m.get("groupItemTitle", "")).replace(",", "").replace("$", ""))
            T = m.get("endDate") or e.get("endDate")
            if not K or not T:
                continue
            bb, ba = _f(m.get("bestBid")), _f(m.get("bestAsk"))
            if bb is None or ba is None:
                continue
            try:
                toks = json.loads(m.get("clobTokenIds") or "[]")
            except Exception:
                toks = []
            out.append({"venue": "polymarket", "coin": coin, "event": e.get("slug"), "title": e.get("title"),
                        "market": str(m.get("id")), "K": K,
                        "T": datetime.fromisoformat(T.replace("Z", "+00:00")).timestamp(),
                        "yes_bid": bb, "yes_ask": ba, "no_ask": round(1 - bb, 4), "no_bid": round(1 - ba, 4),
                        "tokens": toks, "source": "binance_usdt",
                        "url": f"https://polymarket.com/event/{e.get('slug')}"})
    return out


def kalshi(coin):
    serie = COINS[coin][2]
    out = []
    cur = None
    for _ in range(4):
        p = {"series_ticker": serie, "status": "open", "limit": 1000}
        if cur:
            p["cursor"] = cur
        j = _get(KALSHI + "/markets", **p)
        for m in j.get("markets", []):
            if m.get("strike_type") != "greater":
                continue
            K = _f(m.get("floor_strike"))
            ya, yb = _f(m.get("yes_ask_dollars")), _f(m.get("yes_bid_dollars"))
            na, nb = _f(m.get("no_ask_dollars")), _f(m.get("no_bid_dollars"))
            T = m.get("close_time")
            if not K or not T or ya is None or na is None:
                continue
            if ya >= 1 and na >= 1:
                continue
            out.append({"venue": "kalshi", "coin": coin, "event": m.get("event_ticker"), "title": m.get("title"),
                        "market": m.get("ticker"), "K": K,
                        "T": datetime.fromisoformat(T.replace("Z", "+00:00")).timestamp(),
                        "yes_bid": yb, "yes_ask": ya, "no_ask": na, "no_bid": nb, "source": "brti_usd",
                        "url": f"https://kalshi.com/markets/{serie.lower()}"})
        cur = j.get("cursor")
        if not cur:
            break
    return out


def _binance_faktor(coin, index):
    try:
        p = _f(_get(BNV + "/ticker/price", symbol=COINS[coin][0] + "USDT").get("price"))
        if p and index:
            f = p / index
            if 0.98 < f < 1.02:
                return f
    except Exception:
        pass
    return 1.0


def _clob_ask(token):
    """Bester Ask aus dem echten Orderbuch (Gamma kann ein paar Sekunden hinterher sein)."""
    try:
        b = _get(CLOB + "/book", token_id=token)
        asks = [float(a["price"]) for a in b.get("asks", [])]
        return min(asks) if asks else None
    except Exception:
        return None


# ── Speicher ─────────────────────────────────────────────────────────────

def _pg():
    pg = storage._init_pg()
    if not pg:
        return None
    if not _SCHEMA[0]:
        pg.execute("""CREATE TABLE IF NOT EXISTS optionen_picks (
            id TEXT PRIMARY KEY, venue TEXT, coin TEXT, event TEXT, market TEXT, strike NUMERIC,
            side TEXT, price NUMERIC, fee NUMERIC, p_model NUMERIC, iv NUMERIC, fwd NUMERIC,
            t_resolve TIMESTAMPTZ, created TIMESTAMPTZ DEFAULT now(),
            won BOOLEAN, pnl NUMERIC, settled TIMESTAMPTZ, data JSONB)""")
        pg.execute("""CREATE TABLE IF NOT EXISTS optionen_cal (
            id TEXT PRIMARY KEY, venue TEXT, coin TEXT, market TEXT, chk NUMERIC,
            p_model NUMERIC, p_market NUMERIC, t_resolve TIMESTAMPTZ, created TIMESTAMPTZ DEFAULT now(),
            yes BOOLEAN, settled TIMESTAMPTZ)""")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_opt_picks_open ON optionen_picks(settled)")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_opt_cal_open ON optionen_cal(settled)")
        _SCHEMA[0] = True
    return pg


def _ergebnis(venue, market):
    """True = Ja, False = Nein, None = noch offen."""
    try:
        if venue == "polymarket":
            m = _get(f"{GAMMA}/markets/{market}")
            if not m.get("closed"):
                return None
            pr = json.loads(m.get("outcomePrices") or "[]")
            if len(pr) == 2 and {float(pr[0]), float(pr[1])} == {0.0, 1.0}:
                return float(pr[0]) == 1.0
            return None
        m = _get(f"{KALSHI}/markets/{market}").get("market", {})
        r = (m.get("result") or "").lower()
        if r in ("yes", "no"):
            return r == "yes"
    except Exception as e:
        log.debug(f"optionen ergebnis {venue} {market}: {e}")
    return None


def _abrechnen(pg):
    for (pid, venue, market, side, price, fee) in pg.execute(
            "SELECT id, venue, market, side, price, fee FROM optionen_picks "
            "WHERE settled IS NULL AND t_resolve < now() - interval '5 minutes' LIMIT 60").fetchall():
        ja = _ergebnis(venue, market)
        if ja is None:
            continue
        won = ja if side == "yes" else not ja
        kosten = SHARES * (float(price) + float(fee))
        pnl = (SHARES if won else 0.0) - kosten
        pg.execute("UPDATE optionen_picks SET won=%s, pnl=%s, settled=now() WHERE id=%s", (won, pnl, pid))
    gesehen = {}
    for (cid, venue, market) in pg.execute(
            "SELECT id, venue, market FROM optionen_cal WHERE settled IS NULL "
            "AND t_resolve < now() - interval '5 minutes' LIMIT 300").fetchall():
        key = (venue, market)
        if key not in gesehen:
            gesehen[key] = _ergebnis(venue, market)
        ja = gesehen[key]
        if ja is None:
            continue
        pg.execute("UPDATE optionen_cal SET yes=%s, settled=now() WHERE id=%s", (ja, cid))


# ── Takt ─────────────────────────────────────────────────────────────────

def tick(force=False):
    if not force and time.time() - _LETZT[0] < TAKT:
        return None
    _LETZT[0] = time.time()
    pg = None
    try:
        pg = _pg()
    except Exception as e:
        log.debug(f"optionen pg: {e}")
    tafel = []
    for coin, (sym, _, _) in COINS.items():
        try:
            fl = flaeche(sym)
            _FL[sym] = dict(fl, _ts=time.time())
        except Exception as e:
            log.warning(f"optionen deribit {sym}: {e}")
            continue
        faktor_bn = _binance_faktor(coin, fl["index"])
        maerkte = []
        for fn in (polymarket, kalshi):
            try:
                maerkte += fn(coin)
            except Exception as e:
                log.debug(f"optionen {fn.__name__} {coin}: {e}")
        for m in maerkte:
            faktor = faktor_bn if m["source"] == "binance_usdt" else 1.0
            w = wahrscheinlichkeit(fl, m["K"], m["T"], faktor)
            if not w:
                continue
            p = w["p"]
            m.update({"p": round(p, 4), "iv": round(w["iv"], 4), "fwd": round(w["fwd"], 2),
                      "tau_h": round(w["tau_h"], 2), "index": fl["index"], "usdt_factor": round(faktor, 5)})
            m["mid"] = round((m["yes_bid"] + m["yes_ask"]) / 2, 4) if m["yes_bid"] is not None else m["yes_ask"]
            m["edge_yes"] = round(p - m["yes_ask"] - gebuehr(m["yes_ask"]), 4) if m["yes_ask"] < 1 else None
            m["edge_no"] = round((1 - p) - m["no_ask"] - gebuehr(m["no_ask"]), 4) if m["no_ask"] < 1 else None
            tafel.append(m)
            if pg:
                try:
                    _merken(pg, m)
                except Exception as e:
                    log.debug(f"optionen merken: {e}")
    if pg:
        try:
            _abrechnen(pg)
        except Exception as e:
            log.debug(f"optionen abrechnen: {e}")
    live = {"ts": time.time(), "edge": EDGE, "markets": tafel}
    try:
        storage.kv_set("optionen_live", json.dumps(live, default=str))
    except Exception as e:
        log.debug(f"optionen kv: {e}")
    return live


def _merken(pg, m):
    tau = m["tau_h"]
    # Kalibrierpunkte
    for chk in CHECKS_H:
        if chk - 0.5 <= tau <= chk + 0.5 and m.get("mid") is not None:
            cid = f"{m['venue']}|{m['market']}|{chk}"
            pg.execute("""INSERT INTO optionen_cal (id, venue, coin, market, chk, p_model, p_market, t_resolve)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s)) ON CONFLICT DO NOTHING""",
                       (cid, m["venue"], m["coin"], m["market"], chk, m["p"], m["mid"], m["T"]))
    # Papier-Pick
    if not (MIN_TAU_H <= tau <= MAX_TAU_H):
        return
    for side, edge, ask in (("yes", m.get("edge_yes"), m["yes_ask"]), ("no", m.get("edge_no"), m["no_ask"])):
        if edge is None or edge < EDGE or not (0.04 <= ask <= 0.96):
            continue
        if m["venue"] == "polymarket" and m.get("tokens"):
            echt = _clob_ask(m["tokens"][0 if side == "yes" else 1])
            if echt is None:
                continue
            p_side = m["p"] if side == "yes" else 1 - m["p"]
            if p_side - echt - gebuehr(echt) < EDGE:
                continue
            ask = echt
        pid = f"{m['venue']}|{m['market']}|{side}"
        pg.execute("""INSERT INTO optionen_picks (id, venue, coin, event, market, strike, side, price, fee,
                        p_model, iv, fwd, t_resolve, data)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s),%s) ON CONFLICT DO NOTHING""",
                   (pid, m["venue"], m["coin"], m["event"], m["market"], m["K"], side, ask, gebuehr(ask),
                    m["p"] if side == "yes" else 1 - m["p"], m["iv"], m["fwd"], m["T"],
                    json.dumps({"title": m.get("title"), "url": m.get("url"), "tau_h": tau}, default=str)))


# ── Fuer die API ─────────────────────────────────────────────────────────

def _q(rows):
    n = len(rows)
    if not n:
        return None
    win = sum(1 for r in rows if r["won"])
    cost = sum(r["cost"] for r in rows)
    pnl = sum(r["pnl"] for r in rows)
    return {"n": n, "wins": win, "win_rate": round(win / n, 4), "cost": round(cost, 2),
            "pnl": round(pnl, 2), "roi": round(pnl / cost, 4) if cost else 0.0}


def lage():
    try:
        live = json.loads(storage.kv_get("optionen_live") or "{}")
    except Exception:
        live = {}
    out = {"updated": live.get("ts"), "stale": time.time() - float(live.get("ts") or 0) > 900,
           "edge": live.get("edge", EDGE), "fee_rate": FEE_RATE, "shares": SHARES}
    ms = live.get("markets", [])
    gruppen = {}
    for m in ms:
        g = gruppen.setdefault((m["venue"], m["event"]), {
            "venue": m["venue"], "coin": m["coin"], "event": m["event"], "title": m.get("title"),
            "resolves": m["T"], "tau_h": m["tau_h"], "url": m.get("url"), "iv": m["iv"], "fwd": m["fwd"],
            "source": m["source"], "rows": []})
        g["rows"].append({k: m.get(k) for k in ("K", "p", "yes_bid", "yes_ask", "no_ask", "mid", "edge_yes", "edge_no")})
    for (venue, event), g in gruppen.items():
        nah = min((m for m in ms if m["venue"] == venue and m["event"] == event),
                  key=lambda m: abs(math.log(m["K"] / m["fwd"])) if m.get("fwd") else 9)
        g["iv"] = nah["iv"]
    ev = sorted(gruppen.values(), key=lambda g: (g["resolves"], g["coin"], g["venue"]))
    for g in ev:
        g["rows"].sort(key=lambda r: r["K"])
        # nur der spannende Bereich um den Kurs: Ja-Preise zwischen 3 und 97 Cent, plus Edges
        g["rows"] = [r for r in g["rows"] if (r.get("mid") is not None and 0.03 <= r["mid"] <= 0.97)
                     or (r.get("edge_yes") or -1) >= EDGE or (r.get("edge_no") or -1) >= EDGE]
    out["events"] = [g for g in ev if g["rows"]][:16]
    best = sorted([dict(m, side=s, edge=m.get("edge_" + s)) for m in ms for s in ("yes", "no")
                   if (m.get("edge_" + s) or -1) >= EDGE], key=lambda x: -x["edge"])[:8]
    out["top"] = [{k: b.get(k) for k in ("venue", "coin", "title", "K", "side", "edge", "p", "yes_ask", "no_ask",
                                          "tau_h", "url")} for b in best]
    try:
        pg = _pg()
        rows = pg.execute("""SELECT venue, coin, strike, side, price, fee, p_model, won, pnl, t_resolve, data
                             FROM optionen_picks WHERE settled IS NOT NULL ORDER BY settled DESC LIMIT 2000""").fetchall()
        res = [{"venue": r[0], "coin": r[1], "K": float(r[2]), "side": r[3], "price": float(r[4]),
                "p": float(r[6]), "won": r[7], "pnl": float(r[8]), "cost": SHARES * (float(r[4]) + float(r[5])),
                "end": r[9].timestamp(), "title": (r[10] or {}).get("title") if isinstance(r[10], dict) else None}
               for r in rows]
        out["record"] = {"all": _q(res), "polymarket": _q([r for r in res if r["venue"] == "polymarket"]),
                         "kalshi": _q([r for r in res if r["venue"] == "kalshi"])}
        out["recent"] = res[:25]
        offen = pg.execute("SELECT count(*) FROM optionen_picks WHERE settled IS NULL").fetchone()[0]
        out["open_picks"] = int(offen)
        cal = pg.execute("""SELECT chk, count(*),
                              avg(power(p_model - (CASE WHEN yes THEN 1 ELSE 0 END), 2)),
                              avg(power(p_market - (CASE WHEN yes THEN 1 ELSE 0 END), 2))
                            FROM optionen_cal WHERE settled IS NOT NULL GROUP BY chk ORDER BY chk DESC""").fetchall()
        out["calibration"] = [{"hours_before": float(c[0]), "n": int(c[1]), "options_brier": round(float(c[2]), 4),
                               "market_brier": round(float(c[3]), 4)} for c in cal]
    except Exception as e:
        log.debug(f"optionen lage: {e}")
        out.setdefault("record", {})
        out.setdefault("recent", [])
        out.setdefault("calibration", [])
    return out
