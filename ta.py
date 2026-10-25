"""
ta.py: Chart-Lesung fuer Your Engine (seit 2.43). Rechnet die Kennzahlen selbst aus echten Kerzen, das Modell
erklaert sie nur. Keine Vorhersage.

  lesen(was, rahmen)  was: SOL/BTC/ETH..., ein Contract oder Chart-Link, oder eine Aktie (AAPL, HOOD, S&P 500)
                      rahmen: "1h", "4h", "1d" (Standard: 1h und 1d)
Quellen: Coinbase (grosse Coins), GeckoTerminal (jeder Coin mit Pool), Yahoo Finance (Aktien).
"""
import time

import requests

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
MAJORS = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LINK", "DOT", "LTC", "BCH", "SHIB", "PEPE", "SUI", "APT",
          "NEAR", "UNI", "AAVE", "HBAR", "XLM", "ATOM", "ARB", "OP", "INJ", "TIA", "SEI", "BONK", "WIF", "POL", "HYPE"}
_C = {}


def _kerzen_coinbase(sym, rahmen):
    g = {"1h": 3600, "4h": 21600, "1d": 86400}[rahmen]  # 4h gibt es nicht, 6h ist der naechste
    d = requests.get(f"https://api.exchange.coinbase.com/products/{sym}-USD/candles", params={"granularity": g}, headers=UA, timeout=10).json()
    if not isinstance(d, list):
        return None
    return [(x[0], x[3], x[2], x[1], x[4], x[5]) for x in sorted(d)]  # t, o, h, l, c, v


def _kerzen_gecko(netz, pool, rahmen):
    art, agg = {"1h": ("hour", 1), "4h": ("hour", 4), "1d": ("day", 1)}[rahmen]
    d = requests.get(f"https://api.geckoterminal.com/api/v2/networks/{netz}/pools/{pool}/ohlcv/{art}",
                     params={"aggregate": agg, "limit": 300}, headers=UA, timeout=12).json()
    l = (((d.get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
    return [tuple(x[:6]) for x in sorted(l)] or None


def _kerzen_yahoo(sym, rahmen):
    rng, iv = {"1h": ("1mo", "60m"), "4h": ("3mo", "60m"), "1d": ("1y", "1d")}[rahmen]
    d = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}", params={"range": rng, "interval": iv}, headers=UA, timeout=10).json()
    r = ((d.get("chart") or {}).get("result") or [{}])[0] or {}
    t, q = r.get("timestamp") or [], (((r.get("indicators") or {}).get("quote") or [{}])[0])
    out = [(t[i], q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i] or 0) for i in range(len(t))
           if q.get("close") and q["close"][i] is not None]
    if rahmen == "4h":
        out = [(g[0][0], g[0][1], max(x[2] for x in g), min(x[3] for x in g), g[-1][4], sum(x[5] for x in g))
               for g in (out[i:i + 4] for i in range(0, len(out), 4)) if g]
    return out or None


def _ema(w, n):
    if len(w) < n:
        return None
    k, e = 2 / (n + 1), sum(w[:n]) / n
    for x in w[n:]:
        e = x * k + e * (1 - k)
    return e


def _ema_reihe(w, n):
    if len(w) < n:
        return []
    k, e, out = 2 / (n + 1), sum(w[:n]) / n, []
    for x in w[n:]:
        e = x * k + e * (1 - k)
        out.append(e)
    return out


def _rsi(w, n=14):
    if len(w) <= n:
        return None
    g = [max(0, w[i] - w[i - 1]) for i in range(1, len(w))]
    v = [max(0, w[i - 1] - w[i]) for i in range(1, len(w))]
    ag, av = sum(g[:n]) / n, sum(v[:n]) / n
    for i in range(n, len(g)):
        ag, av = (ag * (n - 1) + g[i]) / n, (av * (n - 1) + v[i]) / n
    return 100.0 if av == 0 else 100 - 100 / (1 + ag / av)


def auswerten(k):
    if not k or len(k) < 30:
        return {"error": "not enough candles"}
    c = [x[4] for x in k]
    h, l, v = [x[2] for x in k], [x[3] for x in k], [x[5] or 0 for x in k]
    p = c[-1]
    e20, e50, e200 = _ema(c, 20), _ema(c, 50), _ema(c, 200)
    r = _rsi(c)
    m12, m26 = _ema_reihe(c, 12), _ema_reihe(c, 26)
    macd = [a - b for a, b in zip(m12[-len(m26):], m26)] if m26 else []
    sig = _ema_reihe(macd, 9) if len(macd) >= 9 else []
    hist = (macd[-1] - sig[-1]) if sig else None
    hist_vor = (macd[-2] - sig[-2]) if len(sig) >= 2 else None
    tr = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, len(k))]
    atr = sum(tr[-14:]) / 14 if len(tr) >= 14 else None
    hoch, tief = max(h[-50:]), min(l[-50:])
    # Unterstuetzung und Widerstand: lokale Tiefs und Hochs der letzten 60 Kerzen
    piv_t = sorted({round(l[i], 12) for i in range(max(2, len(l) - 60), len(l) - 2) if l[i] == min(l[i - 2:i + 3]) and l[i] < p})
    piv_h = sorted({round(h[i], 12) for i in range(max(2, len(h) - 60), len(h) - 2) if h[i] == max(h[i - 2:i + 3]) and h[i] > p})
    vol20 = sum(v[-21:-1]) / 20 if len(v) > 21 else None
    punkte = 0
    for bed in ((e20 and p > e20), (e20 and e50 and e20 > e50), (e50 and e200 and e50 > e200), (r and r > 50), (hist is not None and hist > 0)):
        punkte += 1 if bed else -1
    trend = "bullish" if punkte >= 3 else "bearish" if punkte <= -3 else "mixed"
    def pc(a, b):
        return round((a - b) / b * 100, 2) if a and b else None
    return {
        "price": p, "trend": trend, "score": punkte,
        "vs_ema20_pct": pc(p, e20), "vs_ema50_pct": pc(p, e50), "vs_ema200_pct": pc(p, e200),
        "ema20_above_ema50": (e20 > e50) if e20 and e50 else None, "ema50_above_ema200": (e50 > e200) if e50 and e200 else None,
        "rsi14": round(r, 1) if r is not None else None,
        "rsi_zone": None if r is None else ("overbought" if r >= 70 else "oversold" if r <= 30 else "neutral"),
        "macd": None if hist is None else ("bullish cross" if hist > 0 and (hist_vor or 0) <= 0 else "bearish cross" if hist < 0 and (hist_vor or 0) >= 0
                                           else "above signal" if hist > 0 else "below signal"),
        "atr_pct": round(atr / p * 100, 2) if atr and p else None,
        "range_50": {"high": hoch, "low": tief, "from_high_pct": pc(p, hoch), "from_low_pct": pc(p, tief)},
        "support": piv_t[-2:][::-1], "resistance": piv_h[:2],
        "volume_vs_avg20": round(v[-1] / vol20, 2) if vol20 else None,
        "change_pct": {"last_candle": pc(c[-1], c[-2]), "10_candles": pc(c[-1], c[-11]), "30_candles": pc(c[-1], c[-31])},
        "candles": len(k),
    }


def lesen(was, rahmen=None):
    import aktien
    import scan
    q = str(was or "").strip()
    rahmen_liste = [rahmen] if rahmen in ("1h", "4h", "1d") else ["1h", "1d"]
    sym = q.upper().lstrip("$").replace("-USD", "").replace("/USD", "")
    quelle, name, hole = None, q, None
    if sym in MAJORS:
        quelle, name = "Coinbase", sym
        hole = lambda rh: _kerzen_coinbase(sym, rh)
    else:
        hinweis, a = scan.lesen(q)
        if a and (scan.EVM_RE.fullmatch(a) or scan.SOL_RE.fullmatch(a)):
            r = scan.analyse(f"{hinweis}:{a}" if hinweis else a, deep=False)
            m = r.get("market") or {}
            netz = scan.KETTEN.get(r.get("chain"), (None,) * 3)[2]
            if not (netz and m.get("pair")):
                return {"error": "No pool with a chart found for this coin."}
            quelle, name = "GeckoTerminal", "$" + str(r.get("symbol") or a[:6])
            hole = lambda rh: _kerzen_gecko(netz, m["pair"], rh)
        else:
            s = aktien.norm(q)
            if not s:
                return {"error": "Give me a coin (SOL, BTC), a contract address or a stock ticker."}
            quelle, name = "Yahoo Finance", s
            hole = lambda rh: _kerzen_yahoo(s, rh)
    out = {"asset": name, "source": quelle, "timeframes": {},
           "how_to_read": "Computed from real candles. Describe what the chart shows (trend, momentum, levels, volume). "
                          "Never predict a price or say buy or sell. Not financial advice."}
    for rh in rahmen_liste:
        key = (name, rh)
        c = _C.get(key)
        if c and time.time() - c[0] < 120:
            out["timeframes"][rh] = c[1]
            continue
        try:
            erg = auswerten(hole(rh))
        except Exception as e:
            erg = {"error": str(e)[:100]}
        if quelle == "Coinbase" and rh == "4h":
            erg["note"] = "6h candles (Coinbase has no 4h)"
        _C[key] = (time.time(), erg)
        out["timeframes"][rh] = erg
    return out
