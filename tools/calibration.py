"""
The only report that matters.

Answers one question: when the engine says 90%, does it happen 90% of
the time? That number is the product. Not ROI, not win rate.

    python tools/calibration.py

Needs resolved markets, so run it after ~300 city-days have gone by.
It resolves every logged snapshot against Gamma and bins by predicted
coverage.
"""
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests

import storage

GAMMA = "https://gamma-api.polymarket.com"
BINS = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8),
        (0.8, 0.9), (0.9, 0.95), (0.95, 1.01)]


def resolved_winner(condition_id):
    """Gamma resolved+outcome flags only. Never infer from price >= 0.95."""
    try:
        r = requests.get(f"{GAMMA}/markets/{condition_id}", timeout=15)
        r.raise_for_status()
        m = r.json()
        if isinstance(m, list):
            m = m[0] if m else {}
        if not m.get("closed"):
            return None
        prices = m.get("outcomePrices")
        if isinstance(prices, str):
            prices = json.loads(prices)
        if not prices:
            return None
        return float(prices[0]) > 0.5          # YES token won
    except Exception:
        return None


def main():
    snaps = [s for s in storage.load_all() if s.get("signal")]
    if not snaps:
        print("No signals logged yet. Let the worker run.")
        return
    print(f"{len(snaps)} signals logged. Resolving against Gamma...\n")

    rows = []
    for s in snaps:
        cids = s.get("condition_ids") or []
        if not cids:
            continue
        results = [resolved_winner(c) for c in cids]
        if any(r is None for r in results):
            continue                            # not resolved yet
        rows.append({"pred": s["coverage"], "hit": any(results),
                     "edge": s.get("edge_pp", 0), "cost": s.get("cost", 0),
                     "payout": s.get("payout", 0), "budget": s.get("budget", 10),
                     "city": s.get("city"), "lead": s.get("lead_h", 0)})

    if not rows:
        print("Nothing resolved yet. Come back in a few days.")
        return

    print("=" * 62)
    print("  RELIABILITY  —  does the number mean what it says?")
    print("=" * 62)
    print(f"  {'predicted':<12}{'n':>5}{'said':>9}{'actual':>9}{'error':>9}")
    print("  " + "-" * 44)

    total_err = n_tot = 0
    for lo, hi in BINS:
        b = [r for r in rows if lo <= r["pred"] < hi]
        if not b:
            continue
        said = sum(r["pred"] for r in b) / len(b)
        act = sum(1 for r in b if r["hit"]) / len(b)
        err = (act - said) * 100
        flag = "  <-- overconfident" if err < -5 else ("  <-- timid" if err > 5 else "")
        print(f"  {lo:.0%}-{hi:.0%}{'':<4}{len(b):>5}{said:>8.0%}{act:>9.0%}{err:>+8.1f}pp{flag}")
        total_err += abs(err) * len(b)
        n_tot += len(b)

    print("  " + "-" * 44)
    if n_tot:
        print(f"  Mean absolute calibration error: {total_err/n_tot:.1f}pp")
        print(f"\n  Under 3pp -> the engine is honest. Sell it.")
        print(f"  Over 8pp  -> raise DRESS_SIGMA_C and rerun.")

    # Money, second. Calibration first.
    inv = sum(r["budget"] for r in rows)
    ret = sum(r["payout"] for r in rows if r["hit"])
    print("\n" + "=" * 62)
    print(f"  Signals resolved : {len(rows)}")
    print(f"  Hit rate         : {sum(1 for r in rows if r['hit'])/len(rows):.1%}")
    print(f"  Invested         : ${inv:.2f}")
    print(f"  Returned         : ${ret:.2f}")
    print(f"  Net              : ${ret-inv:+.2f}   ({(ret/inv-1)*100:+.1f}%)")
    print("=" * 62)
    print("\n  Calibration is the product. ROI on <100 samples is noise.")


if __name__ == "__main__":
    main()
