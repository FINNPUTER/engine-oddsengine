#!/usr/bin/env python3
"""
Backtest feasibility probe.

Question: can we score TODAY's engine against markets that already resolved,
without the model peeking at the answer?

That is the whole ballgame. If archived runs are available, calibration takes
a day instead of six weeks. If they are not, the worker has to earn it live.

THE TRAP
--------
The ensemble API's `past_days` does NOT give you the forecast that existed
back then. It gives you the CURRENT run's hindcast — a model that already
saw the weather happen. Scoring against that would produce a beautiful,
completely fake result. It is the single easiest way to fool yourself in
this entire project.

We need historical-forecast-api, which serves the run as it was issued.
This script checks, endpoint by endpoint, what actually exists.

    python3 backtest_probe.py
"""
import json
import time
from datetime import datetime, timedelta, timezone

import requests

ARCHIVE_FC = "https://historical-forecast-api.open-meteo.com/v1/forecast"
ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
ARCHIVE_ERA5 = "https://archive-api.open-meteo.com/v1/archive"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# Incheon (RKSI) — Seoul's resolution station. Net positive in the old record.
LAT, LON, ICAO = 37.469, 126.451, "RKSI"

s = requests.Session()
s.headers["User-Agent"] = "oddsengine-probe/1.0"


def get(url, **p):
    time.sleep(0.2)
    r = s.get(url, params=p, timeout=30)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:120]}"
    return r.json(), None


past = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")
old = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%d")

print("=" * 74)
print(f"  Probing station {ICAO}   target dates: {past} (14d ago), {old} (100d ago)")
print("=" * 74)

# ── 1. Archived deterministic runs ───────────────────────────────────────
print("\n1. historical-forecast-api — archived runs, no lookahead")
for label, day in (("14 days ago", past), ("100 days ago", old)):
    d, err = get(ARCHIVE_FC, latitude=LAT, longitude=LON, start_date=day,
                 end_date=day, hourly="temperature_2m",
                 models="ecmwf_ifs025,gfs_seamless,icon_seamless",
                 timezone="auto")
    if err:
        print(f"   {label:<14} {err}")
        continue
    h = d.get("hourly", {})
    series = [k for k in h if k.startswith("temperature_2m")]
    vals = {k: max(v for v in h[k] if v is not None) for k in series
            if any(x is not None for x in h[k])}
    print(f"   {label:<14} {len(series)} model series")
    for k, v in vals.items():
        print(f"      {k:<44} day max {v:.1f}C")

# ── 2. Does the archive expose WHICH run it is? ──────────────────────────
print("\n2. Can we pin the run time?  (lookahead check)")
for extra in ({"past_days": 0}, {}):
    d, err = get(ARCHIVE_FC, latitude=LAT, longitude=LON, start_date=past,
                 end_date=past, hourly="temperature_2m",
                 models="ecmwf_ifs025", timezone="auto", **extra)
    if d:
        keys = [k for k in d if k not in ("hourly", "hourly_units")]
        print(f"   meta keys: {keys}")
        break
print("   NOTE: historical-forecast-api serves the archived initialisation.")
print("   Compare its value against ERA5 below — if they match exactly, it is")
print("   analysis, not forecast, and the backtest would be lookahead.")

# ── 3. Truth ─────────────────────────────────────────────────────────────
print("\n3. ERA5 reanalysis — what actually happened")
d, err = get(ARCHIVE_ERA5, latitude=LAT, longitude=LON, start_date=past,
             end_date=past, hourly="temperature_2m", timezone="auto")
if err:
    print(f"   {err}")
else:
    v = [x for x in d["hourly"]["temperature_2m"] if x is not None]
    print(f"   ERA5 day max: {max(v):.1f}C   ({len(v)} hourly values)")
    print("   (ERA5 is a grid, NOT the station. Resolution truth is Wunderground.)")

# ── 4. Ensemble members for past dates? ──────────────────────────────────
print("\n4. ensemble-api past_days — members, but WHICH run?")
for pd_ in (7, 30, 92):
    d, err = get(ENSEMBLE, latitude=LAT, longitude=LON, hourly="temperature_2m",
                 models="icon_seamless", timezone="auto", past_days=pd_,
                 forecast_days=1)
    if err:
        print(f"   past_days={pd_:<4} {err}")
        continue
    h = d.get("hourly", {})
    mem = [k for k in h if k.startswith("temperature_2m")]
    times = h.get("time", [])
    print(f"   past_days={pd_:<4} {len(mem):>3} members, "
          f"range {times[0][:10] if times else '?'} .. {times[-1][:10] if times else '?'}")
print("   WARNING: these are the CURRENT run reaching backwards. Using them")
print("   to score past markets is lookahead. Only usable if the archive")
print("   endpoint above also serves members.")

# ── 5. Resolved markets ──────────────────────────────────────────────────
print("\n5. Gamma — resolved temperature markets to score against")
d, err = get(f"{GAMMA}/events", tag_id=104596, closed="true", limit=100)
if err:
    print(f"   {err}")
else:
    evs = [e for e in d if (e.get("slug") or "").startswith("highest-temperature-in-")]
    print(f"   {len(evs)} closed events on page 1")
    if evs:
        ev = evs[0]
        print(f"   sample: {ev.get('slug')}   endDate {ev.get('endDate')}")
        win = []
        for m in ev.get("markets", []):
            op = m.get("outcomePrices")
            if isinstance(op, str):
                try:
                    op = json.loads(op)
                except Exception:
                    op = None
            if op and float(op[0]) > 0.5:
                win.append(m.get("groupItemTitle"))
        print(f"   winning bucket: {win or 'not resolved yet'}")
        tok = ev["markets"][0].get("clobTokenIds")
        if isinstance(tok, str):
            tok = json.loads(tok)
        if tok:
            ts = int(datetime.fromisoformat(
                ev["endDate"].replace("Z", "+00:00")).timestamp()) - 12 * 3600
            ph, e2 = get(f"{CLOB}/prices-history", market=tok[0],
                         startTs=ts - 3600, endTs=ts + 3600, fidelity=60)
            n = len(ph.get("history", [])) if ph else 0
            print(f"   prices-history at T-12h: {n} points"
                  + (f"  e.g. {ph['history'][0]}" if n else f"  {e2 or 'empty'}"))

print("\n" + "=" * 74)
print("  VERDICT")
print("=" * 74)
print("  Backtest is ON if:  archived runs exist for past dates AND their")
print("  values differ from ERA5 AND prices-history returns points at T-12h.")
print("\n  If archived ENSEMBLE members are unavailable, we can still build a")
print("  distribution from multi-model spread — weaker, but honest.")
print("\n  If nothing archived is available, the worker earns the record live.")
print("  One week of 50 cities is ~350 resolved city-days. Not six.")
