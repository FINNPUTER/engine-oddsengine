#!/usr/bin/env python3
"""
engine_backtest.py — was die Engine mit den JETZIGEN Einstellungen geliefert
haette.

Unterschied zu field_backtest.py: der spielt immer die drei Buckets zentriert
auf den Read. Dieser hier baut alle Bloecke wie engine.best_block(), zieht den
Haircut ab, faehrt Kostendeckel, Abdeckungs-Untergrenze samt Lead-Aware-Absenkung,
Edge-Schwelle und Whitelist, und nimmt dann den Block mit der groessten Edge.

Alle Schwellen kommen aus config.py, also aus den Railway-Variablen. Im Railway-
Terminal ausgefuehrt misst das exakt die laufende Konfiguration.

NICHT nachgestellt: Tiefe und Spread. Historische Orderbuecher gibt es nicht,
Polymarket archiviert nur Preise. Live sterben daran rund 18 Prozent der Maerkte,
die Signalzahl hier ist also entsprechend zu hoch. Trefferquote und ROI der
gewaehlten Bloecke sind davon nicht betroffen.

Aufruf im Railway-Terminal (Service worker, Tab Console):

    python3 engine_backtest.py --since 2026-07-01 --until 2026-07-21
    python3 engine_backtest.py --since 2026-07-01 --until 2026-07-21 --bias
"""
import argparse
import json
import time
from collections import defaultdict
from datetime import datetime, timedelta

import backtest_roi as bt
import engine
from config import cfg
from field_backtest import CITY_BIAS, _archive_range


def build_candidates(legs, q, prices, lead_h):
    """Alle zusammenhaengenden Fenster, wie best_block sie baut."""
    lo_n, hi_n, min_edge_pp, min_cov = cfg.modes[cfg.mode]

    if cfg.lead_aware_coverage and lead_h is not None and lead_h > cfg.cov_full_lead_h:
        span = max(cfg.max_lead_h - cfg.cov_full_lead_h, 1.0)
        t = min(1.0, (lead_h - cfg.cov_full_lead_h) / span)
        min_cov = max(cfg.cov_floor, min_cov - (min_cov - cfg.cov_floor) * t)

    n = len(legs)
    hc = cfg.coverage_haircut_pp / 100.0
    cands = []
    for size in range(lo_n, min(hi_n, n) + 1):
        for start in range(0, n - size + 1):
            rng = range(start, start + size)
            if any(prices[i] is None for i in rng):
                continue
            cost = sum(prices[i] for i in rng)
            if cost <= 0:
                continue
            cov_raw = sum(q[i] for i in rng)
            cov = max(cov_raw - hc, 0.0)
            cands.append({
                "idx": list(rng),
                "labels": [legs[i]["label"] for i in rng],
                "cost": cost,
                "coverage_raw": cov_raw,
                "coverage": cov,
                "edge_pp": (cov - cost) * 100,
            })
    return cands, min_cov, min_edge_pp


def pick(cands, min_cov, min_edge_pp):
    """Gates in derselben Reihenfolge wie in der Engine. Ohne Tiefe/Spread."""
    pool = [c for c in cands if c["cost"] <= cfg.max_field_cost]
    if not pool:
        return None, "cost cap"
    pool = [c for c in pool if c["coverage"] >= min_cov]
    if not pool:
        return None, "coverage"
    best = max(pool, key=lambda c: c["edge_pp"])
    if best["edge_pp"] < min_edge_pp:
        return None, "edge"
    return best, None


def run(since, until, limit, lead, bias):
    markets = bt.fetch_resolved(limit, since=since, until=until)

    by_station = defaultdict(list)
    for m in markets:
        d = (m.get("end") or "")[:10]
        if d:
            by_station[(m["icao"], m["lat"], m["lon"])].append((d, m))
    cache = {}
    for (icao, lat, lon), items in by_station.items():
        ds = [d for d, _ in items]
        try:
            for d, mx in _archive_range(lat, lon, min(ds), max(ds)).items():
                cache[(icao, d)] = mx
        except Exception:
            pass
        time.sleep(0.25)

    out = {"markets": len(markets), "signals": 0, "hit": 0,
           "staked": 0.0, "returned": 0.0,
           "rejected": defaultdict(int), "by_city": {}, "by_cost": {},
           "no_archive": 0, "no_price": 0, "picked_width": defaultdict(int)}

    for m in markets:
        date = (m.get("end") or "")[:10]
        maxima = cache.get((m["icao"], date))
        legs = m.get("legs") or []
        if not maxima:
            out["no_archive"] += 1
            continue
        if len(legs) < 3 or not m.get("winner"):
            continue

        city = (m.get("city") or "").lower()
        if cfg.signal_whitelist and city not in cfg.signal_whitelist:
            out["rejected"]["not whitelisted"] += 1
            continue

        shift = CITY_BIAS.get(city, 0.0) if bias else 0.0
        q = engine.distribution(maxima, legs, lead, bias=shift)
        if not q:
            out["rejected"]["no distribution"] += 1
            continue

        try:
            close = datetime.fromisoformat(m["end"].replace("Z", "+00:00"))
            ts = int((close - timedelta(hours=lead)).timestamp())
        except Exception:
            continue

        prices = []
        for l in legs:
            p = bt.price_at(l["token_id"], ts) if l.get("token_id") else None
            prices.append(float(p) if p is not None else None)
        if sum(1 for p in prices if p is not None) < 3:
            out["no_price"] += 1
            continue

        cands, min_cov, min_edge = build_candidates(legs, q, prices, lead)
        if not cands:
            out["rejected"]["no block"] += 1
            continue
        best, why = pick(cands, min_cov, min_edge)
        if not best:
            out["rejected"][why] += 1
            continue

        hit = m["winner"] in best["labels"]
        out["signals"] += 1
        out["hit"] += 1 if hit else 0
        out["staked"] += best["cost"]
        out["returned"] += 1.0 if hit else 0.0
        out["picked_width"][len(best["labels"])] += 1

        band = ("00-40c" if best["cost"] < .40 else "40-70c"
                if best["cost"] < .70 else "70-100c")
        b = out["by_cost"].setdefault(band, {"n": 0, "hit": 0, "stk": 0.0, "ret": 0.0})
        b["n"] += 1; b["hit"] += 1 if hit else 0
        b["stk"] += best["cost"]; b["ret"] += 1.0 if hit else 0.0

        c = out["by_city"].setdefault(m["city"], {"n": 0, "hit": 0, "stk": 0.0, "ret": 0.0})
        c["n"] += 1; c["hit"] += 1 if hit else 0
        c["stk"] += best["cost"]; c["ret"] += 1.0 if hit else 0.0

    for grp in (out["by_cost"], out["by_city"]):
        for v in grp.values():
            v["hit_rate"] = round(v["hit"] / v["n"], 3) if v["n"] else None
            v["roi"] = round((v["ret"] - v["stk"]) / v["stk"], 3) if v["stk"] else None
            v["stk"] = round(v["stk"], 2); v["ret"] = round(v["ret"], 2)

    if out["signals"]:
        out["hit_rate"] = round(out["hit"] / out["signals"], 3)
        out["roi"] = round((out["returned"] - out["staked"]) / out["staked"], 3)
        out["avg_cost_c"] = round(out["staked"] / out["signals"] * 100)
    out["rejected"] = dict(out["rejected"])
    out["picked_width"] = dict(out["picked_width"])
    out["config"] = {
        "risk_mode": cfg.mode,
        "max_field_cost": cfg.max_field_cost,
        "coverage_haircut_pp": cfg.coverage_haircut_pp,
        "lead_aware_coverage": cfg.lead_aware_coverage,
        "cov_floor": cfg.cov_floor,
        "dress_sigma_c": cfg.dress_sigma_c,
        "whitelist_size": len(cfg.signal_whitelist),
    }
    out["caveat"] = ("Tiefe und Spread nicht nachgestellt, historische "
                     "Orderbuecher existieren nicht. Signalzahl daher zu hoch.")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--lead", type=float, default=12.0)
    ap.add_argument("--bias", action="store_true")
    a = ap.parse_args()

    r = run(a.since, a.until, a.limit, a.lead, a.bias)

    print("\nKonfiguration:", json.dumps(r["config"], indent=1))
    print(f"\n{r['markets']} Maerkte geprueft")
    print(f"{r['signals']} Signale")
    if r.get("hit_rate") is not None:
        print(f"Trefferquote {r['hit_rate']*100:.1f} %   "
              f"ROI {r['roi']*100:+.1f} %   Schnittkosten {r['avg_cost_c']}c")
    print("\nAbgelehnt:")
    for k, v in sorted(r["rejected"].items(), key=lambda kv: -kv[1]):
        print(f"  {v:5}  {k}")
    print("\nGewaehlte Blockbreite:", r["picked_width"])
    print("\nNach Kostenband:")
    for k in sorted(r["by_cost"]):
        v = r["by_cost"][k]
        print(f"  {k}  {v['n']:4} Signale  {v['hit_rate']} Treffer  ROI {v['roi']}")
    print("\n" + r["caveat"])
    with open("engine_backtest.json", "w") as f:
        json.dump(r, f, indent=1, default=str)
    print("Rohdaten in engine_backtest.json")
