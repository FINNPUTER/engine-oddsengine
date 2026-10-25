"""
Field backtest with REAL prices -> the money number.

For every resolved market we can reach: centre a 3-bracket field on our ensemble
read, fetch what those 3 brackets actually COST at signal time, and settle it
against what happened. Then the only question that matters: did the field return
more than it cost, after the misses?

Hit rate alone lies (a field on the favourite hits often but costs ~ its hit
rate = zero). Only cost-vs-return says "money".

Honest limits: ENSEMBLE field only (no archived WU -> the blend is forward-only);
prices are the mid at `lead` hours before close, fills/slippage not modelled.
"""
import re
import time
import threading
from datetime import datetime, timedelta
from collections import defaultdict

import backtest_roi as bt


def _archive_range(lat, lon, start, end):
    d = bt.get(bt.ARCHIVE, latitude=lat, longitude=lon, start_date=start,
               end_date=end, hourly="temperature_2m", models=bt.MODELS,
               timezone="auto")
    out = {}
    if not d:
        return out
    h = d.get("hourly", {})
    times = h.get("time", [])
    series = {k: v for k, v in h.items()
              if k.startswith("temperature_2m") and k != "time"}
    by_date = defaultdict(list)
    for i, t in enumerate(times):
        by_date[t[:10]].append(i)
    for date, idxs in by_date.items():
        maxima = []
        for vals in series.values():
            day = [vals[i] for i in idxs if i < len(vals) and vals[i] is not None]
            if len(day) >= 20:
                maxima.append(max(day))
        if maxima:
            out[date] = maxima
    return out


CITY_BIAS = {   # gemessen (unser Read - Ist), nur Konsistenz >= 0.80
    "seoul": -2.62, "shanghai": -1.97, "guangzhou": -1.83, "taipei": -1.53,
    "atlanta": -1.32, "chongqing": -1.31, "munich": -1.27, "los angeles": 1.15,
    "kuala lumpur": -1.12, "singapore": -1.00,
}


def _actual_c(label):
    """Representative Celsius temperature of a winning bucket label."""
    if not label:
        return None
    is_f = "F" in label
    nums = re.findall(r"\d+", label)
    if not nums:
        return None
    val = (int(nums[0]) + int(nums[1])) / 2.0 if len(nums) >= 2 else int(nums[0])
    return (val - 32.0) * 5.0 / 9.0 if is_f else float(val)


def _center(leg):
    lo, hi = leg["lo_c"], leg["hi_c"]
    if lo == float("-inf"):
        return hi - 1.0
    if hi == float("inf"):
        return lo + 1.0
    return (lo + hi) / 2.0


def run(limit=150, maxcost=0.70, lead=12.0, prices=True, cities=None, since=None, until=None, biascorrect=False):
    markets = bt.fetch_resolved(limit, since=since, until=until)
    if cities:
        want = {c.strip().lower() for c in cities.split(",")}
        markets = [m for m in markets if (m.get("city") or "").lower() in want]
    by_station = defaultdict(list)
    for m in markets:
        date = (m.get("end") or "")[:10]
        if date:
            by_station[(m["icao"], m["lat"], m["lon"])].append((date, m))
    cache = {}
    for (icao, lat, lon), items in by_station.items():
        dates = [d for d, _ in items]
        try:
            for d, mx in _archive_range(lat, lon, min(dates), max(dates)).items():
                cache[(icao, d)] = mx
        except Exception:
            pass
        time.sleep(0.25)

    out = {"limit": limit, "maxcost": maxcost, "biascorrect": biascorrect,
           "until": until, "markets": len(markets),
           "bets": 0, "hit": 0, "staked": 0.0, "returned": 0.0,
           "no_archive": 0, "no_price": 0, "too_expensive": 0,
           "arch_scored": 0, "one_temp_hit": 0, "three_temp_hit": 0,
           "dates": [], "by_city": {}, "by_cost": {}, "bias": {}}
    for m in markets:
        date = (m.get("end") or "")[:10]
        maxima = cache.get((m["icao"], date))
        if not maxima:
            out["no_archive"] += 1
            continue
        legs = m.get("legs") or []
        if len(legs) < 3 or not m.get("winner"):
            continue
        mean = sum(maxima) / len(maxima)
        _shift = CITY_BIAS.get((m.get("city") or "").lower(), 0.0) if biascorrect else 0.0
        if _shift:
            mean = mean - _shift
            _dbg = out.setdefault("_shift_debug", {})
            _d = _dbg.setdefault(m["city"], {"markets": 0, "total_shift": 0.0})
            _d["markets"] += 1; _d["total_shift"] = round(_d["total_shift"] + _shift, 2)
        ci = min(range(len(legs)), key=lambda i: abs(_center(legs[i]) - mean))
        lo = max(0, min(ci - 1, len(legs) - 3))
        field_legs = legs[lo:lo + 3]
        labels = {l["label"] for l in field_legs}
        hit = m["winner"] in labels
        # 1-vs-3 comparison, over EVERY archived market (independent of price):
        out["arch_scored"] += 1
        if legs[ci]["label"] == m["winner"]:
            out["one_temp_hit"] += 1
        if hit:
            out["three_temp_hit"] += 1
        # BIAS: our read (Celsius) vs the temperature that actually happened.
        # Positive = we ran hot, negative = we ran cool. Aggregated per city so
        # a consistent offset (a real, correctable bias) stands out from noise.
        actual = _actual_c(m["winner"])
        if actual is not None:
            bias = mean - actual
            b = out["bias"].setdefault(m["city"], {"n": 0, "sum": 0.0,
                                                    "abs": 0.0, "hot": 0})
            b["n"] += 1
            b["sum"] += bias
            b["abs"] += abs(bias)
            if bias > 0.5:
                b["hot"] += 1
            elif bias < -0.5:
                b["hot"] -= 1
        if not prices:
            continue  # hit-rate only mode: skip the (slow) price fetch
        try:
            close = datetime.fromisoformat(m["end"].replace("Z", "+00:00"))
            ts = int((close - timedelta(hours=lead)).timestamp())
        except Exception:
            continue
        prices_l = [bt.price_at(l["token_id"], ts) if l.get("token_id") else None
                    for l in field_legs]
        if any(p is None for p in prices_l):
            out["no_price"] += 1
            continue
        cost = sum(float(p) for p in prices_l)
        if cost <= 0:
            out["no_price"] += 1
            continue
        # Preisklassen-Aufschlüsselung ueber ALLE Felder (auch teure):
        band = ("00-10c" if cost < .10 else "10-20c" if cost < .20 else
                "20-40c" if cost < .40 else "40-70c" if cost < .70 else "70-100c")
        cb = out["by_cost"].setdefault(band, {"n": 0, "hit": 0, "ret": 0.0, "stk": 0.0})
        cb["n"] += 1; cb["hit"] += 1 if hit else 0
        cb["ret"] += 1.0 if hit else 0.0; cb["stk"] += cost
        if cost > maxcost:
            out["too_expensive"] += 1
            continue
        out["bets"] += 1
        out["dates"].append(date)
        out["staked"] += cost
        out["returned"] += 1.0 if hit else 0.0
        out["hit"] += 1 if hit else 0
        bc = out["by_city"].setdefault(m["city"], {"n": 0, "hit": 0,
                                                    "staked": 0.0, "returned": 0.0})
        bc["n"] += 1
        bc["hit"] += 1 if hit else 0
        bc["staked"] += cost
        bc["returned"] += 1.0 if hit else 0.0

    for city, b in out["bias"].items():
        if b["n"]:
            b["mean_bias_c"] = round(b["sum"] / b["n"], 2)
            b["avg_miss_c"] = round(b["abs"] / b["n"], 2)
            b["consistency"] = round(abs(b["hot"]) / b["n"], 2)
            b["direction"] = ("runs HOT" if b["mean_bias_c"] > 0.4 else
                              "runs COOL" if b["mean_bias_c"] < -0.4 else "centered")
        b.pop("sum", None); b.pop("abs", None); b.pop("hot", None)
    out["bias"] = dict(sorted(out["bias"].items(),
                              key=lambda kv: -abs(kv[1].get("mean_bias_c") or 0)))
    if out["arch_scored"]:
        out["one_temp_rate"] = round(out["one_temp_hit"] / out["arch_scored"], 3)
        out["three_temp_rate"] = round(out["three_temp_hit"] / out["arch_scored"], 3)
    if out["dates"]:
        ds = sorted(out["dates"])
        out["date_from"], out["date_to"] = ds[0], ds[-1]
        try:
            from datetime import date as _d
            a = _d.fromisoformat(ds[0]); b = _d.fromisoformat(ds[-1])
            out["span_days"] = (b - a).days + 1
        except Exception:
            pass
    out.pop("dates", None)
    for band, v in out["by_cost"].items():
        v["hit_rate"] = round(v["hit"] / v["n"], 3) if v["n"] else None
        v["roi"] = round((v["ret"] - v["stk"]) / v["stk"], 3) if v["stk"] else None
        v["stk"] = round(v["stk"], 2); v["ret"] = round(v["ret"], 2)
    if out["bets"]:
        out["hit_rate"] = round(out["hit"] / out["bets"], 3)
        out["avg_cost_c"] = round(out["staked"] / out["bets"] * 100)
        out["roi"] = round((out["returned"] - out["staked"]) / out["staked"], 3)
        out["on_100_flat"] = round(100 * (out["returned"] / out["staked"]), 2)
        out["verdict"] = ("MAKES money after cost" if out["roi"] > 0.02
                          else "LOSES after cost" if out["roi"] < -0.02
                          else "roughly break-even (the market's margin)")
        for c, v in out["by_city"].items():
            v["roi"] = (round((v["returned"] - v["staked"]) / v["staked"], 2)
                        if v["staked"] else None)
            v["staked"] = round(v["staked"], 2)
            v["returned"] = round(v["returned"], 2)
        out["by_city"] = dict(sorted(out["by_city"].items(),
                                     key=lambda kv: -(kv[1].get("roi") or -9)))
        out["staked"] = round(out["staked"], 2)
        out["returned"] = round(out["returned"], 2)
        out["note"] = ("Ensemble field, no WU. Prices = mid ~%gh before close; "
                       "fills/slippage not modelled." % lead)
    else:
        out["note"] = "No priced bets -- price history may be unavailable."
    return out


_fb = {"status": "idle", "started": None, "params": None, "result": None, "gen": 0}


def _worker(gen, limit, maxcost, lead, prices=True, cities=None, since=None, until=None, biascorrect=False):
    if gen != _fb["gen"]:
        return                      # ein neuerer Lauf hat uns bereits ersetzt
    _fb.update(status="running", started=time.strftime("%H:%M:%S"),
               params={"limit": limit, "maxcost": maxcost, "lead": lead,
                       "until": until, "since": since, "cities": cities,
                       "biascorrect": biascorrect})
    try:
        res = run(limit, maxcost, lead, prices, cities, since, until, biascorrect)
        if gen == _fb["gen"]:       # nur der neueste Lauf darf schreiben
            _fb["result"] = res; _fb["status"] = "done"
    except Exception as e:
        if gen == _fb["gen"]:
            _fb["result"] = {"error": f"{type(e).__name__}: {e}"}; _fb["status"] = "error"


def start(limit=150, maxcost=0.70, lead=12.0, prices=True, cities=None, since=None, until=None, biascorrect=False, restart=False):
    if _fb["status"] == "running" and not restart:
        return {"status": "running", "started": _fb["started"],
                "params": _fb["params"], "note": "poll ?poll=true in ~3-5 min"}
    _fb["gen"] += 1
    # Ein neuer Lauf loescht das alte Ergebnis. Sonst liefert poll=true
    # waehrend der Rechnung weiter die Zahlen des vorherigen Laufs.
    _fb["result"] = None
    threading.Thread(target=_worker, args=(_fb["gen"], limit, maxcost, lead, prices, cities, since, until, biascorrect),
                     daemon=True).start()
    return {"status": "started", "limit": limit, "maxcost": maxcost,
            "note": "poll ?poll=true in ~3-5 min for the money number"}


def status():
    return {"status": _fb["status"], "started": _fb["started"],
            "params": _fb["params"], "result": _fb["result"]}
