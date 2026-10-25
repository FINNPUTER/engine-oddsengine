#!/usr/bin/env python3
"""
Per-city calibration — spec §3, finally.

One sigma for fifty cities is a placeholder, not a model. Singapore is tropical
and boring; Chicago has fronts. LaGuardia sits on the water and the model's grid
point does not. Each station has its own bias and its own spread, and the engine
currently ignores both.

This measures them from markets that already resolved and writes city_params.json,
which engine.py picks up. Cities without enough history keep the global value.

    python3 tools/city_calibration.py --n 1500
    python3 tools/city_calibration.py --n 1500 --write

WHY SHRINKAGE, NOT PER-CITY AVERAGES
------------------------------------
50 cities x 11 brackets is a lot of parameters and there are not many resolved
markets per city. Fitting each city on 6 samples fits noise and ships it as
knowledge — the exact mistake the 8.8pp haircut exists to undo.

So each city's estimate is pulled toward the global one, hard when the sample is
thin and less as it grows:

    weight = n / (n + K)        K = SHRINK_K

At n=5 a city keeps ~17% of its own estimate. At n=100, ~83%. Nothing has to be
decided about "enough data" — the formula handles it, and a city with two markets
simply gets the global number.

TRUTH
-----
The resolved market itself. Gamma reports the winning bracket, which Polymarket
read off the resolution source. We do not re-derive it.

FORECASTS
---------
historical-forecast-api — the runs as they were issued. Never the ensemble API's
past_days, which reaches backwards with today's run and already knows the answer.
"""

import argparse
import json
import math
import os
import re
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import requests

import stations
from engine import parse_bucket

GAMMA = "https://gamma-api.polymarket.com"
ARCHIVE = "https://historical-forecast-api.open-meteo.com/v1/forecast"
TAG = 104596
MODELS = ("ecmwf_ifs025,ecmwf_aifs025,gfs_seamless,gfs_global,icon_seamless,"
          "icon_global,icon_eu,jma_seamless,gem_seamless,meteofrance_seamless,"
          "ukmo_seamless")

SIGMA_GRID = [0.6, 0.8, 1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 2.4, 2.8, 3.2]
SHRINK_K = 25.0          # samples at which a city is trusted ~50/50 vs global
GLOBAL_SIGMA = 1.4       # measured on 269 markets, all cities pooled
MIN_CITY_N = 3

s = requests.Session()
s.headers["User-Agent"] = "oddsengine-citycal/1.0"
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
        except Exception:
            pass
        if a < retries:
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


def fetch_resolved(limit):
    """Every resolved temperature market we can reach, with its winning bracket."""
    out, offset = [], 0
    while len(out) < limit and offset < 2000:
        page = get(f"{GAMMA}/events", tag_id=TAG, closed="true", limit=100, offset=offset)
        if not page:
            break
        for ev in page:
            slug = ev.get("slug") or ""
            if not slug.startswith("highest-temperature-in-"):
                continue
            city = slug[len("highest-temperature-in-"):].split("-on-")[0].replace("-", " ")
            if city.lower() in stations.EXCLUDED_CITIES:
                continue

            mkts = ev.get("markets") or []
            desc = ev.get("description") or (mkts[0].get("description") if mkts else "")
            rounding = stations.rounding_for(desc)
            if rounding is None:
                continue
            m = re.search(r"(https?://[^\s\)]+)", desc or "")
            st = stations.coords_for(m.group(1) if m else "", city)
            if not st:
                continue

            legs, win = [], None
            for mk in mkts:
                b = parse_bucket(mk.get("groupItemTitle", ""), rounding)
                if not b:
                    continue
                pr = loads(mk.get("outcomePrices"), [])
                b["won"] = bool(pr and float(pr[0]) > 0.5)
                if b["won"]:
                    win = b
                legs.append(b)
            if len(legs) < 5 or win is None:
                continue
            legs.sort(key=lambda x: x["sort"])
            out.append({"city": city, "date": (ev.get("endDate") or "")[:10],
                        "legs": legs, "win": win, "icao": st[3],
                        "lat": st[0], "lon": st[1]})
            if len(out) >= limit:
                break
        if len(page) < 100:
            break
        offset += 100
    return out


_fc = {}


def archived(lat, lon, date, icao):
    ck = (icao, date)
    if ck in _fc:
        return _fc[ck]
    d = get(ARCHIVE, latitude=lat, longitude=lon, start_date=date, end_date=date,
            hourly="temperature_2m", models=MODELS, timezone="auto")
    out = []
    if d:
        for k, series in (d.get("hourly") or {}).items():
            if k == "time" or not k.startswith("temperature_2m"):
                continue
            v = [x for x in series if x is not None]
            if len(v) >= 20:
                out.append(max(v))
    _fc[ck] = out
    return out


def dist(maxima, legs, sigma, bias=0.0):
    q = []
    for o in legs:
        acc = 0.0
        for m in maxima:
            mm = m - bias                       # correct the station, not the bucket
            hi = 1.0 if o["hi_c"] == float("inf") else norm_cdf((o["hi_c"] - mm) / sigma)
            lo = 0.0 if o["lo_c"] == float("-inf") else norm_cdf((o["lo_c"] - mm) / sigma)
            acc += max(hi - lo, 0.0)
        q.append(acc / len(maxima))
    t = sum(q)
    return [x / t for x in q] if t > 0 else None


def brier(rows, sigma, bias):
    tot = 0.0
    n = 0
    for r in rows:
        q = dist(r["maxima"], r["legs"], sigma, bias)
        if not q:
            continue
        w = next(i for i, l in enumerate(r["legs"]) if l["won"])
        tot += sum((qi - (1.0 if j == w else 0.0)) ** 2 for j, qi in enumerate(q))
        n += 1
    return tot / n if n else 9e9


ap = argparse.ArgumentParser()
ap.add_argument("--n", type=int, default=1500)
ap.add_argument("--write", action="store_true", help="write city_params.json")
a = ap.parse_args()

print(f"Fetching resolved markets (target {a.n})...")
mk = fetch_resolved(a.n)
print(f"  {len(mk)} usable\n")
if len(mk) < 100:
    print("Too few to calibrate anything. Raise --n.")
    raise SystemExit(1)

rows = []
for i, m in enumerate(mk, 1):
    if i % 100 == 0:
        print(f"  forecasts {i}/{len(mk)}...")
    mx = archived(m["lat"], m["lon"], m["date"], m["icao"])
    if len(mx) < 3:
        continue
    # Point estimate of the day's high = the models' consensus.
    m["maxima"] = mx
    m["consensus"] = sum(mx) / len(mx)
    # Truth to the resolution of the bracket: its midpoint. Open-ended
    # brackets have no midpoint, so they cannot inform a bias estimate.
    lo, hi = m["win"]["lo_c"], m["win"]["hi_c"]
    m["actual"] = None if (lo == float("-inf") or hi == float("inf")) else (lo + hi) / 2
    rows.append(m)

print(f"\n  {len(rows)} with archived forecasts")
by_city = defaultdict(list)
for r in rows:
    by_city[r["city"]].append(r)
print(f"  {len(by_city)} cities · median {sorted(len(v) for v in by_city.values())[len(by_city)//2]} markets each\n")

# ── Global bias, then per-city, shrunk toward it ──
gb = [r["consensus"] - r["actual"] for r in rows if r["actual"] is not None]
global_bias = sum(gb) / len(gb) if gb else 0.0
print("=" * 74)
print(f"  GLOBAL   bias {global_bias:+.2f}°C   sigma {GLOBAL_SIGMA}   (n={len(gb)})")
print("=" * 74)
print(f"  {'city':<15}{'n':>4}{'raw bias':>10}{'shrunk':>9}{'sigma':>7}{'w':>6}  {'hit@4':>6}")
print("  " + "-" * 60)

params = {}
for city in sorted(by_city, key=lambda c: -len(by_city[c])):
    rs = by_city[city]
    n = len(rs)
    if n < MIN_CITY_N:
        continue

    bs = [r["consensus"] - r["actual"] for r in rs if r["actual"] is not None]
    raw_bias = sum(bs) / len(bs) if bs else global_bias
    # Shrinkage: a city with 5 markets keeps ~17% of its own estimate.
    # Without this, 5 samples of noise get shipped as a station constant.
    w = len(bs) / (len(bs) + SHRINK_K) if bs else 0.0
    bias = w * raw_bias + (1 - w) * global_bias

    best_s, best_b = GLOBAL_SIGMA, 9e9
    for sg in SIGMA_GRID:
        b = brier(rs, sg, bias)
        if b < best_b:
            best_b, best_s = b, sg
    sigma = w * best_s + (1 - w) * GLOBAL_SIGMA

    hits = 0
    for r in rs:
        q = dist(r["maxima"], r["legs"], sigma, bias)
        if not q:
            continue
        top4 = sorted(range(len(q)), key=lambda j: -q[j])[:4]
        if next(i for i, l in enumerate(r["legs"]) if l["won"]) in top4:
            hits += 1
    hit = hits / n

    params[city] = {"n": n, "bias_c": round(bias, 3), "sigma_c": round(sigma, 2),
                    "raw_bias_c": round(raw_bias, 3), "weight": round(w, 2),
                    "hit_at_4": round(hit, 3), "icao": rs[0]["icao"]}
    flag = "  <-- thin" if w < 0.3 else ""
    print(f"  {city:<15}{n:>4}{raw_bias:>+9.2f}°{bias:>+8.2f}°{sigma:>7.2f}{w:>6.2f}{hit:>7.0%}{flag}")

print("  " + "-" * 60)
print(f"  {len(params)} cities fitted · {sum(1 for p in params.values() if p['weight'] >= .3)} "
      f"with enough data to move off the global value")

warm = sorted(params.items(), key=lambda x: -x[1]["bias_c"])[:4]
cold = sorted(params.items(), key=lambda x: x[1]["bias_c"])[:4]
print("\n  Models run HOT here (forecast above what settled) — we subtract:")
for c, p in warm:
    print(f"    {c:<15} {p['bias_c']:+.2f}°C  (n={p['n']})")
print("\n  Models run COLD here — we add:")
for c, p in cold:
    print(f"    {c:<15} {p['bias_c']:+.2f}°C  (n={p['n']})")

hard = sorted(params.items(), key=lambda x: -x[1]["sigma_c"])[:4]
easy = sorted(params.items(), key=lambda x: x[1]["sigma_c"])[:4]
print("\n  Hardest to forecast (wide sigma):")
for c, p in hard:
    print(f"    {c:<15} sigma {p['sigma_c']:.2f}  hit@4 {p['hit_at_4']:.0%}")
print("\n  Easiest:")
for c, p in easy:
    print(f"    {c:<15} sigma {p['sigma_c']:.2f}  hit@4 {p['hit_at_4']:.0%}")

out = {"_meta": {"global_bias_c": round(global_bias, 3), "global_sigma_c": GLOBAL_SIGMA,
                 "shrink_k": SHRINK_K, "markets": len(rows), "cities": len(params)},
       "cities": params}
path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "city_params.json")
if a.write:
    json.dump(out, open(path, "w"), indent=2)
    print(f"\nWrote city_params.json — engine.py picks it up on next start")
else:
    print(f"\n(dry run — add --write to save city_params.json)")
