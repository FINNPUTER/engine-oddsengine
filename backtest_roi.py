#!/usr/bin/env python3
"""
OddsEngine — Backtest, Sigma Calibration & ROI
==============================================

Runs TODAY's engine against markets that already resolved, using the forecast
runs as they existed back then, and answers the only question that matters:

    When the engine says 90%, does it happen 90% of the time?

And it fits DRESS_SIGMA_C from the data instead of me guessing it. The probe
showed ECMWF 26.5 / GFS 22.2 / ICON 27.1 for one Seoul day — a 4.9C spread.
0.9 was optimistic. This measures what it should be.

    pip install requests
    python3 backtest.py                    # ~200 markets, all cities
    python3 backtest.py --n 400 --lead 12
    python3 backtest.py --city seoul

NO LOOKAHEAD
------------
Forecasts come from historical-forecast-api, which serves the archived run.
Verified: on 2026-07-03 at RKSI it returned GFS 22.2C while ERA5 (truth) was
26.0C. A model that had seen the answer does not miss by 3.8 degrees.

The ensemble API's past_days is NOT used. That reaches backwards with the
CURRENT run, which already ate the observations. Scoring against it produces
a gorgeous, entirely fake result.

TRUTH
-----
The resolved market itself. Gamma reports the winning bucket, which Polymarket
derived from Wunderground. No scraping, no second guess.
"""

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, ".")
try:
    import stations
except ImportError:
    print("Run this from inside oddsengine-platform/ (needs stations.py)")
    raise SystemExit(1)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
ARCHIVE = "https://historical-forecast-api.open-meteo.com/v1/forecast"
TAG = 104596

# A dozen deterministic archived models. More sources = a better spread proxy.
# Open-Meteo does not archive ensemble members, so the multi-model spread is
# our stand-in. Weaker than 100 members, but it is real and it is honest.
MODELS = ("ecmwf_ifs025,ecmwf_aifs025,gfs_seamless,gfs_global,icon_seamless,"
          "icon_global,icon_eu,jma_seamless,gem_seamless,meteofrance_seamless,"
          "ukmo_seamless")

SIGMA_GRID = [0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 2.3, 2.6, 3.0]
BINS = [(0.0, .5), (.5, .6), (.6, .7), (.7, .8), (.8, .9), (.9, .95), (.95, 1.01)]

s = requests.Session()
s.headers["User-Agent"] = "oddsengine-backtest/1.0"
_t = [0.0]


def get(url, retries=2, **p):
    for a in range(retries + 1):
        w = 0.15 - (time.time() - _t[0])
        if w > 0:
            time.sleep(w)
        _t[0] = time.time()
        try:
            r = s.get(url, params=p, timeout=30)
            if r.status_code == 200:
                return r.json()
            if a == retries:
                return None
        except Exception:
            if a == retries:
                return None
            time.sleep(1)
    return None


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def loads(v, d):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return d
    return v if v is not None else d


# ── Resolved markets ─────────────────────────────────────────────────────

def fetch_resolved(limit, city=None, since=None, until=None):
    out, offset = [], 0
    extra = {"order": "endDate", "ascending": "false"}
    if since:
        extra["end_date_min"] = since
    if until:
        extra["end_date_max"] = until
    while len(out) < limit and offset < 1000:
        page = get(f"{GAMMA}/events", tag_id=TAG, closed="true",
                   limit=100, offset=offset, **extra)
        if not page:
            break
        for ev in page:
            slug = ev.get("slug") or ""
            if not slug.startswith("highest-temperature-in-"):
                continue
            cty = slug[len("highest-temperature-in-"):].split("-on-")[0].replace("-", " ")
            if city and city.lower() not in cty:
                continue
            if cty.lower() in stations.EXCLUDED_CITIES:
                continue

            desc = ev.get("description") or ""
            mkts = ev.get("markets") or []
            if not desc and mkts:
                desc = mkts[0].get("description") or ""
            rounding = stations.rounding_for(desc)
            if rounding is None:
                continue
            url = ""
            import re as _re
            m = _re.search(r"(https?://[^\s\)]+)", desc)
            if m:
                url = m.group(1)
            st = stations.coords_for(url, cty)
            if not st:
                continue

            legs, winner = [], None
            for mk in mkts:
                b = None
                lab = mk.get("groupItemTitle", "")
                try:
                    from engine import parse_bucket
                    b = parse_bucket(lab, rounding)
                except Exception:
                    pass
                if not b:
                    continue
                tok = loads(mk.get("clobTokenIds"), [])
                prices = loads(mk.get("outcomePrices"), [])
                b["token_id"] = tok[0] if tok else None
                won = bool(prices and float(prices[0]) > 0.5)
                b["won"] = won
                if won:
                    winner = lab
                legs.append(b)

            if len(legs) < 5 or winner is None:
                continue
            legs.sort(key=lambda x: x["sort"])
            out.append({"slug": slug, "city": cty, "legs": legs,
                        "winner": winner, "end": ev.get("endDate", ""),
                        "lat": st[0], "lon": st[1], "icao": st[3]})
            if len(out) >= limit:
                break
        if len(page) < 100:
            break
        offset += 100
    return out


# ── Archived forecast ────────────────────────────────────────────────────

_fc_cache = {}


def archived_maxima(lat, lon, date, icao):
    """Day maxima per model, from the run as it was issued. No lookahead."""
    ck = (icao, date)
    if ck in _fc_cache:
        return _fc_cache[ck]
    d = get(ARCHIVE, latitude=lat, longitude=lon, start_date=date,
            end_date=date, hourly="temperature_2m", models=MODELS,
            timezone="auto")
    out = []
    if d:
        h = d.get("hourly", {})
        for k, series in h.items():
            if k == "time" or not k.startswith("temperature_2m"):
                continue
            vals = [v for v in series if v is not None]
            if len(vals) >= 20:
                out.append(max(vals))
    _fc_cache[ck] = out
    return out


def dist(maxima, legs, sigma):
    q = []
    for o in legs:
        acc = 0.0
        for m in maxima:
            hi = 1.0 if o["hi_c"] == float("inf") else norm_cdf((o["hi_c"] - m) / sigma)
            lo = 0.0 if o["lo_c"] == float("-inf") else norm_cdf((o["lo_c"] - m) / sigma)
            acc += max(hi - lo, 0.0)
        q.append(acc / len(maxima))
    t = sum(q)
    return [x / t for x in q] if t > 0 else None


def price_at(token_id, ts):
    d = get(f"{CLOB}/prices-history", market=token_id,
            startTs=ts - 5400, endTs=ts + 5400, fidelity=60)
    h = (d or {}).get("history") or []
    if not h:
        return None
    return min(h, key=lambda x: abs(x["t"] - ts))["p"]


# ── Main ─────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--lead", type=float, default=12, help="hours before close")
    ap.add_argument("--city")
    ap.add_argument("--no-prices", action="store_true", help="skip market comparison")
    a = ap.parse_args()

    print(f"Fetching resolved markets (target {a.n})...")
    mkts = fetch_resolved(a.n, a.city)
    print(f"  {len(mkts)} usable\n")
    if len(mkts) < 20:
        print("Too few to say anything. Widen --n or drop --city.")
        raise SystemExit(1)

    rows = []
    for i, mk in enumerate(mkts, 1):
        if i % 25 == 0:
            print(f"  {i}/{len(mkts)}...")
        date = mk["end"][:10]
        maxima = archived_maxima(mk["lat"], mk["lon"], date, mk["icao"])
        if len(maxima) < 3:
            continue
        prices = None
        if not a.no_prices:
            try:
                close = datetime.fromisoformat(mk["end"].replace("Z", "+00:00"))
                ts = int((close - timedelta(hours=a.lead)).timestamp())
                prices = [price_at(l["token_id"], ts) if l["token_id"] else None
                          for l in mk["legs"]]
            except Exception:
                prices = None
        rows.append({"mk": mk, "maxima": maxima, "prices": prices,
                     "win_i": next(i for i, l in enumerate(mk["legs"]) if l["won"])})

    print(f"\n  {len(rows)} markets with archived forecasts")
    print(f"  models per market: {sum(len(r['maxima']) for r in rows)/len(rows):.1f}")
    sp = [max(r["maxima"]) - min(r["maxima"]) for r in rows]
    print(f"  model spread: median {sorted(sp)[len(sp)//2]:.1f}C  mean {sum(sp)/len(sp):.1f}C")

    # ── Fit sigma: which dressing width scores best? ─────────────────────────
    print("\n" + "=" * 66)
    print("  FITTING DRESS_SIGMA_C  (multi-category Brier, lower is better)")
    print("=" * 66)
    print(f"  {'sigma':<8}{'brier':>9}{'logscore':>11}{'hit@top4':>10}")
    print("  " + "-" * 38)

    best_sigma, best_brier = None, 9e9
    for sg in SIGMA_GRID:
        briers, logs, hits = [], [], []
        for r in rows:
            q = dist(r["maxima"], r["mk"]["legs"], sg)
            if not q:
                continue
            w = r["win_i"]
            briers.append(sum((qi - (1.0 if j == w else 0.0)) ** 2
                              for j, qi in enumerate(q)))
            logs.append(-math.log(max(q[w], 1e-6)))
            order = sorted(range(len(q)), key=lambda j: -q[j])[:4]
            hits.append(1.0 if w in order else 0.0)
        if not briers:
            continue
        b = sum(briers) / len(briers)
        lg = sum(logs) / len(logs)
        h = sum(hits) / len(hits)
        flag = ""
        if b < best_brier:
            best_brier, best_sigma = b, sg
            flag = "  <--"
        print(f"  {sg:<8.1f}{b:>9.4f}{lg:>11.4f}{h:>9.0%}{flag}")

    print("  " + "-" * 38)
    print(f"  BEST: DRESS_SIGMA_C = {best_sigma}   (currently shipping 0.9)")

    # ── Reliability at the fitted sigma ──────────────────────────────────────
    print("\n" + "=" * 66)
    print(f"  RELIABILITY at sigma={best_sigma} — the product, in one table")
    print("=" * 66)
    print(f"  {'block says':<13}{'n':>5}{'said':>9}{'happened':>11}{'error':>9}")
    print("  " + "-" * 47)

    buckets = defaultdict(list)
    for r in rows:
        q = dist(r["maxima"], r["mk"]["legs"], best_sigma)
        if not q:
            continue
        # best contiguous 4-block by probability — the shape we actually publish
        n = len(q)
        bi, bcov = 0, -1
        for st in range(n - 3):
            c = sum(q[st:st + 4])
            if c > bcov:
                bcov, bi = c, st
        hit = bi <= r["win_i"] < bi + 4
        buckets[next((lo, hi) for lo, hi in BINS if lo <= bcov < hi)].append((bcov, hit))

    tot_err = tot_n = 0
    for lo, hi in BINS:
        b = buckets.get((lo, hi))
        if not b:
            continue
        said = sum(x[0] for x in b) / len(b)
        act = sum(1 for x in b if x[1]) / len(b)
        err = (act - said) * 100
        tag = "  overconfident" if err < -5 else ("  timid" if err > 5 else "")
        print(f"  {lo:.0%}-{hi:.0%}{'':<5}{len(b):>5}{said:>8.0%}{act:>11.0%}{err:>+8.1f}pp{tag}")
        tot_err += abs(err) * len(b)
        tot_n += len(b)
    print("  " + "-" * 47)
    if tot_n:
        mae = tot_err / tot_n
        print(f"  Mean absolute calibration error: {mae:.1f}pp")
        print("  under 3pp -> honest. over 8pp -> the sigma model is wrong shape.")

    # ── Engine vs market ─────────────────────────────────────────────────────
    withp = [r for r in rows if r["prices"] and sum(1 for p in r["prices"] if p) >= 5]
    if withp:
        print("\n" + "=" * 66)
        print(f"  ENGINE vs MARKET  ({len(withp)} markets with prices at T-{a.lead:.0f}h)")
        print("=" * 66)
        eb, mb = [], []
        for r in withp:
            q = dist(r["maxima"], r["mk"]["legs"], best_sigma)
            p = [x if x else 0.0 for x in r["prices"]]
            tp = sum(p)
            p = [x / tp for x in p] if tp > 0 else None
            if not q or not p:
                continue
            w = r["win_i"]
            eb.append(sum((qi - (1.0 if j == w else 0.0)) ** 2 for j, qi in enumerate(q)))
            mb.append(sum((pi - (1.0 if j == w else 0.0)) ** 2 for j, pi in enumerate(p)))
        if eb:
            e, m = sum(eb) / len(eb), sum(mb) / len(mb)
            wins = sum(1 for x, y in zip(eb, mb) if x < y)
            print(f"  Engine Brier : {e:.4f}")
            print(f"  Market Brier : {m:.4f}")
            print(f"  Engine better on {wins}/{len(eb)} markets ({wins/len(eb):.0%})")
            print()
            if e < m:
                print(f"  Engine beats the market by {(m-e)/m:.1%}. That is the claim.")
            else:
                print(f"  MARKET WINS by {(e-m)/e:.1%}.")
                print("  No signal product here. Fix calibration or sell data, not picks.")
    else:
        print("\n  (no price history pulled — rerun without --no-prices for the "
              "engine-vs-market comparison)")

    json.dump([{"slug": r["mk"]["slug"], "city": r["mk"]["city"],
                "winner": r["mk"]["winner"], "maxima": r["maxima"],
                "prices": r["prices"]} for r in rows],
              open("backtest_raw.json", "w"), indent=1)
    print(f"\nWrote backtest_raw.json ({len(rows)} markets)")


    # ═══════════════════════════════════════════════════════════════════════
    #  ROI — what the strategy would actually have made
    # ═══════════════════════════════════════════════════════════════════════
    #
    # Brier scores every outcome of every market. The strategy trades almost
    # none of them: only when edge, coverage, and depth all clear. A worse
    # average forecast can still pay inside that narrow slice — or not. This
    # measures it instead of inferring it.
    #
    # What this simulation does NOT model, and it matters:
    #   - depth. prices-history gives a price, not a book. Real fills are worse.
    #   - the archived engine is weaker (9 deterministic runs, no ensemble members)
    #   - Gamma returns the most-traded markets first -> the hardest ones
    #
    # So treat the number as an optimistic upper bound, not a promise.

    MODES_BT = {           # (min_n, max_n, min_edge_pp, min_cov)
        "safe":       (4, 5, 4.0, 0.85),
        "balanced":   (3, 5, 3.0, 0.70),
        "aggressive": (3, 3, 2.0, 0.50),
    }
    BUDGET = 10.0


    def sim_block(q, p, legs, mode):
        """Coverage-first: filter on gates, then max edge. Same order as live."""
        lo_n, hi_n, min_edge, min_cov = MODES_BT[mode]
        n = len(q)
        cands = []
        for size in range(lo_n, min(hi_n, n) + 1):
            for st in range(0, n - size + 1):
                rng = range(st, st + size)
                if any(p[i] is None or p[i] <= 0 for i in rng):
                    continue
                cost = sum(p[i] for i in rng)
                if cost >= 1.0:
                    continue
                cov = sum(q[i] for i in rng)
                cands.append({"i0": st, "n": size, "cost": cost, "cov": cov,
                              "edge": (cov - cost) * 100})
        pool = [c for c in cands if c["cov"] >= min_cov]
        if not pool:
            return None
        b = max(pool, key=lambda c: c["edge"])
        return b if b["edge"] >= min_edge else None


    def baseline_block(p, legs, size=4):
        """Control: just buy the market's own cheapest viable 4-block. No engine.
        If the engine cannot beat this, the engine is decoration."""
        n = len(p)
        best = None
        for st in range(0, n - size + 1):
            rng = range(st, st + size)
            if any(p[i] is None or p[i] <= 0 for i in rng):
                continue
            cost = sum(p[i] for i in rng)
            if cost >= 1.0:
                continue
            if best is None or cost < best["cost"]:
                best = {"i0": st, "n": size, "cost": cost}
        return best


    print("\n" + "=" * 66)
    print(f"  ROI SIMULATION at sigma={best_sigma}, ${BUDGET:.0f}/signal, T-{a.lead:.0f}h")
    print("=" * 66)

    pr = [r for r in rows if r["prices"] and sum(1 for x in r["prices"] if x) >= 5]
    print(f"  {len(pr)} of {len(rows)} markets have usable prices\n")

    if not pr:
        print("  no price data — rerun without --no-prices")
    else:
        print(f"  {'mode':<12}{'signals':>8}{'hit':>7}{'in $':>9}{'out $':>9}{'ROI':>9}")
        print("  " + "-" * 54)
        per_city = defaultdict(lambda: [0, 0.0, 0.0])
        for mode in ("safe", "balanced", "aggressive"):
            inv = ret = 0.0
            n_sig = n_hit = 0
            for r in pr:
                q = dist(r["maxima"], r["mk"]["legs"], best_sigma)
                if not q:
                    continue
                b = sim_block(q, r["prices"], r["mk"]["legs"], mode)
                if not b:
                    continue
                n_sig += 1
                inv += BUDGET
                hit = b["i0"] <= r["win_i"] < b["i0"] + b["n"]
                if hit:
                    n_hit += 1
                    ret += BUDGET / b["cost"]
                if mode == "balanced":
                    c = per_city[r["mk"]["city"]]
                    c[0] += 1
                    c[1] += BUDGET
                    c[2] += (BUDGET / b["cost"]) if hit else 0.0
            roi = (ret / inv - 1) * 100 if inv else 0
            hr = n_hit / n_sig if n_sig else 0
            print(f"  {mode:<12}{n_sig:>8}{hr:>6.0%}{inv:>9.2f}{ret:>9.2f}{roi:>8.1f}%")

        # control
        inv = ret = n = 0
        for r in pr:
            b = baseline_block(r["prices"], r["mk"]["legs"])
            if not b:
                continue
            n += 1
            inv += BUDGET
            if b["i0"] <= r["win_i"] < b["i0"] + b["n"]:
                ret += BUDGET / b["cost"]
        print("  " + "-" * 54)
        print(f"  {'MARKET-only':<12}{n:>8}{'':>7}{inv:>9.2f}{ret:>9.2f}"
              f"{((ret/inv-1)*100 if inv else 0):>8.1f}%   <- no engine, control")

        print("\n  If MARKET-only matches or beats the engine modes, the engine")
        print("  adds nothing and the honest product is data, not picks.")

        if per_city:
            print("\n  BY CITY (balanced)")
            print(f"  {'city':<16}{'sig':>5}{'in $':>9}{'out $':>9}{'net $':>9}")
            print("  " + "-" * 48)
            for c, (k, i_, o_) in sorted(per_city.items(), key=lambda x: x[1][2]-x[1][1],
                                         reverse=True):
                if k:
                    print(f"  {c:<16}{k:>5}{i_:>9.2f}{o_:>9.2f}{o_-i_:>+9.2f}")

    json.dump([{"slug": r["mk"]["slug"], "city": r["mk"]["city"],
                "winner": r["mk"]["winner"], "win_i": r["win_i"],
                "maxima": r["maxima"], "prices": r["prices"],
                "buckets": [l["label"] for l in r["mk"]["legs"]]} for r in rows],
              open("backtest_raw.json", "w"), indent=1)
    print(f"\nWrote backtest_raw.json ({len(rows)} markets)")
