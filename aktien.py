"""
aktien.py: Aktien- und Indexkurse fuer Your Engine (seit 2.41). Kostenlos ueber Yahoo Finance (leicht verzoegert).

  kurse(["AAPL", "TSLA", "^GSPC"]) -> [{"symbol", "name", "price", "change_pct", "currency", "market_time"}]
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

UA = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
NAMEN = {"S&P 500": "^GSPC", "SP500": "^GSPC", "SPX": "^GSPC", "NASDAQ": "^IXIC", "DOW": "^DJI", "DAX": "^GDAXI",
         "NIKKEI": "^N225", "FTSE": "^FTSE", "GOLD": "GC=F", "OIL": "CL=F", "VIX": "^VIX"}
_C = {}


def norm(t):
    t = str(t or "").strip().upper().lstrip("$")
    return NAMEN.get(t, t) if re.fullmatch(r"[\^A-Z0-9.=&\- ]{1,12}", t) else None


def _eins(sym):
    c = _C.get(sym)
    if c and time.time() - c[0] < 60:
        return c[1]
    out = None
    for host in ("query1", "query2"):
        try:
            d = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}", params={"range": "1d", "interval": "1d"},
                             headers=UA, timeout=8).json()
            m = (((d.get("chart") or {}).get("result") or [{}])[0] or {}).get("meta") or {}
            p = m.get("regularMarketPrice")
            vor = m.get("chartPreviousClose") or m.get("previousClose")
            if p is None:
                continue
            out = {"symbol": sym, "name": m.get("shortName") or m.get("longName") or sym, "price": p,
                   "change_pct": round((p - vor) / vor * 100, 2) if vor else None, "currency": m.get("currency"),
                   "market_time": m.get("regularMarketTime")}
            break
        except Exception:
            continue
    _C[sym] = (time.time(), out)
    return out


def kurse(liste):
    syms = [s for s in (norm(x) for x in liste or []) if s][:15]
    with ThreadPoolExecutor(6) as ex:
        return [x for x in ex.map(_eins, syms) if x]


def zeile(k):
    p = k["price"]
    ch = k.get("change_pct")
    zurueck = {v: n for n, v in NAMEN.items() if " " in n or n in ("GOLD", "OIL", "DAX", "NASDAQ", "DOW", "VIX", "FTSE", "NIKKEI")}
    name = zurueck.get(k["symbol"], k["symbol"].lstrip("^"))
    name = name.title() if name.isupper() and len(name) > 4 and name not in ("NASDAQ",) else name
    return f"{name} {p:,.2f}" + (f" ({ch:+.1f}%)" if ch is not None else "")
