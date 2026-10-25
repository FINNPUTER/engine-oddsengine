#!/usr/bin/env python3
"""
sim2 — $100, what would it have become?

Reads backtest_raw.json (finds it automatically) and replays the strategy
day by day against a real bankroll.

    python3 sim2.py
    python3 sim2.py --start 100 --sigma 1.4

WHAT CHANGED VS bankroll_sim.py
-------------------------------
The year fix. Slugs often omit it: ".../on-december-30" with no 2025.
Defaulting to the current year put RESOLVED markets in the FUTURE
(2026-12-31), which collapsed 7 months of trading into 23 phantom days and
made every concurrency and correlation number meaningless. Closed markets
are in the past by definition — if the parsed date is ahead of today, it
belongs to last year.

Also new: calibration measured on the TRADED subset. The 0.2pp figure was
measured across all 269 markets. Only ~43% clear the 3pp edge gate, and
selection shifts distributions. What the engine claims on the bets you
actually place is a different number, and it is the one that pays.

WHY NOT JUST 100 * 1.089^n
--------------------------
Concurrency: ten cities signal the same morning; you cannot stake 30% on
each. Correlation: the miss rate is not an independent coin flip per city —
when a heat wave arrives that the models underplayed, a dozen signals lose
the same afternoon. Kelly assumes independence. Weather does not care.

Max drawdown is the honest number here, not the final balance.
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date

VERSION = "sim2 / 2026-07-17 / year-fix + traded-subset calibration"

MONTHS = {m: i + 1 for i, m in enumerate(
    "january february march april may june july august september october "
    "november december".split())}
DATE_RE = re.compile(r"-on-(" + "|".join(MONTHS) + r")-(\d{1,2})(?:-(\d{4}))?$")
TODAY = date.today()


def find_raw():
    if os.path.exists("backtest_raw.json"):
        return "backtest_raw.json"
    try:
        out = subprocess.run(
            ["find", os.path.expanduser("~/Downloads"), "-maxdepth", "4",
             "-name", "backtest_raw.json"],
            capture_output=True, text=True, timeout=20).stdout.strip().split("\n")
        for p in out:
            if p and os.path.exists(p):
                return p
    except Exception:
        pass
    return None


def date_of(slug):
    m = DATE_RE.search(slug or "")
    if not m:
        return None
    mo, dy = MONTHS[m.group(1)], int(m.group(2))
    y = int(m.group(3)) if m.group(3) else TODAY.year
    if not m.group(3):
        try:
            if date(y, mo, dy) > TODAY:
                y -= 1
        except ValueError:
            return None
    return f"{y}-{mo:02d}-{dy:02d}"


def cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bounds(label, rounding=0.5):
    m = re.search(r"(-?\d+)\s*(?:-\s*(-?\d+))?\s*°?\s*([CF])"
                  r"\s*(or higher|or above|or below|or lower)?", label, re.I)
    if not m:
        return None
    lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
    unit, mod = m.group(3).upper(), (m.group(4) or "").lower()
    lo -= rounding
    hi += rounding
    if "high" in mod or "above" in mod:
        hi = float("inf")
    elif "below" in mod or "lower" in mod:
        lo = float("-inf")
    f = lambda t: (t - 32) * 5 / 9
    if unit == "F":
        lo = f(lo) if lo != float("-inf") else lo
        hi = f(hi) if hi != float("inf") else hi
    return lo, hi


ap = argparse.ArgumentParser()
ap.add_argument("--start", type=float, default=100.0)
ap.add_argument("--sigma", type=float, default=1.4)
ap.add_argument("--min-cov", type=float, default=0.70)
ap.add_argument("--min-edge", type=float, default=3.0)
a = ap.parse_args()

print(VERSION)
path = find_raw()
if not path:
    print("backtest_raw.json not found — run backtest_roi.py first.")
    sys.exit(1)
print(f"reading {path}\n")

rows = [r for r in json.load(open(path)) if r.get("prices")]

sigs = []
for r in rows:
    bs = [bounds(l) for l in r["buckets"]]
    if any(b is None for b in bs):
        continue
    q = []
    for lo, hi in bs:
        acc = sum((1.0 if hi == float("inf") else cdf((hi - m) / a.sigma))
                  - (0.0 if lo == float("-inf") else cdf((lo - m) / a.sigma))
                  for m in r["maxima"]) / len(r["maxima"])
        q.append(max(acc, 0.0))
    t = sum(q)
    if t <= 0:
        continue
    q = [x / t for x in q]
    p = [x if x else 0.0 for x in r["prices"]]

    best = None
    for sz in (3, 4, 5):
        for i in range(len(q) - sz + 1):
            if any(p[j] <= 0 for j in range(i, i + sz)):
                continue
            cost = sum(p[i:i + sz])
            cov = sum(q[i:i + sz])
            if cost >= 1.0 or cov < a.min_cov:
                continue
            edge = (cov - cost) * 100
            if not best or edge > best[0]:
                best = (edge, i, sz, cov, cost)
    if not best or best[0] < a.min_edge:
        continue
    edge, i, sz, cov, cost = best
    d = date_of(r["slug"])
    if not d:
        continue
    sigs.append({"d": d, "city": r["city"], "cost": cost, "cov": cov,
                 "edge": edge, "hit": i <= r["win_i"] < i + sz})

if len(sigs) < 20:
    print(f"only {len(sigs)} signals — too few. Rerun backtest with --n 500.")
    sys.exit(1)

sigs.sort(key=lambda x: x["d"])
by_day = defaultdict(list)
for s in sigs:
    by_day[s["d"]].append(s)
days = sorted(by_day)

n = len(sigs)
said = sum(s["cov"] for s in sigs) / n
hit = sum(1 for s in sigs if s["hit"]) / n
cost = sum(s["cost"] for s in sigs) / n
future = sum(1 for s in sigs if s["d"] > TODAY.isoformat())

print("=" * 64)
print(f"  {n} signals of {len(rows)} markets ({n/len(rows):.0%} fire the gate)")
print(f"  {len(days)} active days   {days[0]} .. {days[-1]}")
print(f"  {n/len(days):.2f} signals per active day")
print(f"  dated in the future: {future}   (must be 0)")
print("=" * 64)
print(f"  engine says : {said:.1%}")
print(f"  actually hit: {hit:.1%}      error {(hit-said)*100:+.1f}pp")
print(f"  avg cost    : {cost:.1%}  ->  EV {(hit/cost-1)*100:+.1f}% per signal")
print("=" * 64)
print("  This is calibration ON THE TRADED SUBSET. The 0.2pp figure was")
print("  measured across all markets. Selection shifts it. This is the one")
print("  that pays.")

multi = [(d, sum(1 for s in by_day[d] if s["hit"]) / len(by_day[d]), len(by_day[d]))
         for d in days if len(by_day[d]) >= 3]
wipe = [x for x in multi if x[1] == 0.0]
print(f"\n  days with 3+ signals : {len(multi)}")
if multi:
    print(f"  ...every signal lost : {len(wipe)}  ({len(wipe)/len(multi):.0%})")
for d, h, k in sorted(wipe, key=lambda x: -x[2])[:4]:
    print(f"     {d}   {k} signals, all lost")
miss = 1 - hit
if multi:
    print(f"\n  Independent misses would put a 4-signal wipeout at "
          f"{miss**4:.4f} per day.")
    print("  If the count above is higher, losses clump and Kelly overstates.")


def sim(size_fn):
    bank, peak, mdd, streak, worst = a.start, a.start, 0.0, 0, 0
    for d in days:
        todays = by_day[d]
        stakes = [size_fn(bank, len(todays), s) for s in todays]
        tot = sum(stakes)
        if tot > bank:
            stakes = [x * bank / tot for x in stakes]      # never lever
        for s, st in zip(todays, stakes):
            if st <= 0.01:
                continue
            bank -= st
            if s["hit"]:
                bank += st / s["cost"]
                streak = 0
            else:
                streak += 1
                worst = max(worst, streak)
        peak = max(peak, bank)
        mdd = max(mdd, (peak - bank) / peak)
    return bank, mdd, worst


def kelly_f(s, frac):
    return max(0.0, min((s["cov"] - s["cost"]) / (1 - s["cost"]) * frac, 0.9))


print("\n" + "=" * 64)
print(f"  ${a.start:.0f} START — sizing matters more than the edge")
print("=" * 64)
print(f"  {'rule':<20}{'final $':>12}{'x':>7}{'max DD':>9}{'streak':>9}")
print("  " + "-" * 57)
for name, fn in [
    ("flat $10", lambda b, k, s: 10.0),
    ("flat 5% of bank", lambda b, k, s: b * 0.05),
    ("flat 2% of bank", lambda b, k, s: b * 0.02),
    ("quarter Kelly / n", lambda b, k, s: b * kelly_f(s, 0.25) / max(k, 1)),
    ("half Kelly / n", lambda b, k, s: b * kelly_f(s, 0.5) / max(k, 1)),
    ("FULL Kelly / n", lambda b, k, s: b * kelly_f(s, 1.0) / max(k, 1)),
]:
    bank, mdd, ws = sim(fn)
    print(f"  {name:<20}{bank:>12.2f}{bank/a.start:>6.1f}x{mdd:>8.0%}{ws:>7} in a row")
print("  " + "-" * 57)
print("\n  Backtest. No order book depth, no slippage, and sigma was fitted on")
print("  these same markets. The live number is lower. It always is.")
