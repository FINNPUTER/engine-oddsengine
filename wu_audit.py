#!/usr/bin/env python3
"""
wu_audit.py — lag Wunderground richtiger als das Ensemble? Und waren die
Verwerfungen berechtigt?

/v1/readcheck schaut nur auf aufgeloeste SIGNALE. Faelle wie Tokyo, wo WU um
7,6 Grad abwich und deshalb verworfen wurde, werden nie zum Signal und tauchen
dort nie auf. Genau die sind aber die interessanten: dort trennt sich, ob die
Sicherung schuetzt oder gute Information wegwirft.

Dieses Skript nimmt JEDEN gespeicherten Snapshot mit einem WU-Wert, egal ob
verwendet oder verworfen, holt das tatsaechliche Ergebnis vom Markt und misst,
wer naeher dran war.

Im Railway-Terminal (Service worker, Tab Console):

    python wu_audit.py
    python wu_audit.py --city tokyo
    python wu_audit.py --min-dev 4.0
"""
import argparse
import json
from collections import defaultdict

import storage
import resolution
from config import cfg
from field_backtest import _actual_c


def latest_per_slug(rows):
    """Der letzte Snapshot je Markt: dort steckt die meiste Beobachtung drin."""
    best = {}
    for r in rows:
        slug = r.get("slug")
        if not slug or r.get("kind") == "resolution":
            continue
        if r.get("ens_mean") is None:
            continue
        if r.get("wu_high_c") is None and r.get("wu_high_c_rejected") is None:
            continue
        prev = best.get(slug)
        if prev is None or (r.get("ts") or "") > (prev.get("ts") or ""):
            best[slug] = r
    return list(best.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default=None)
    ap.add_argument("--min-dev", type=float, default=None,
                    help="nur Faelle mit mindestens dieser Abweichung WU zu Ensemble")
    ap.add_argument("--out", default="wu_audit.json")
    a = ap.parse_args()

    rows = latest_per_slug(storage.load_all())
    if a.city:
        rows = [r for r in rows if a.city.lower() in (r.get("city") or "").lower()]
    print(f"{len(rows)} Maerkte mit WU-Wert im Speicher")

    w = cfg.wu_weight
    groups = {"WU verwendet": [], "WU verworfen": []}
    per_city = defaultdict(lambda: {"n": 0, "wu_besser": 0})
    detail = []
    unresolved = 0

    for r in rows:
        label, resolved = resolution.winning_bucket(r["slug"])
        if not resolved or not label:
            unresolved += 1
            continue
        actual = _actual_c(label)
        if actual is None:
            continue

        ens = float(r["ens_mean"])
        rejected = r.get("wu_high_c") is None
        wu = float(r.get("wu_high_c") if not rejected else r["wu_high_c_rejected"])
        dev = abs(wu - ens)
        if a.min_dev is not None and dev < a.min_dev:
            continue

        blend = ens + w * (wu - ens)
        e_ens, e_wu, e_blend = abs(ens - actual), abs(wu - actual), abs(blend - actual)

        key = "WU verworfen" if rejected else "WU verwendet"
        groups[key].append((e_ens, e_wu, e_blend))
        c = per_city[r.get("city") or "?"]
        c["n"] += 1
        c["wu_besser"] += 1 if e_wu < e_ens else 0

        detail.append({
            "city": r.get("city"), "date": r.get("date"), "slug": r["slug"],
            "verworfen": rejected, "abweichung_c": round(dev, 1),
            "ist_c": round(actual, 1), "ensemble_c": round(ens, 1), "wu_c": round(wu, 1),
            "fehler_ensemble": round(e_ens, 2), "fehler_wu": round(e_wu, 2),
            "fehler_blend": round(e_blend, 2),
            "naeher": "WU" if e_wu < e_ens else "Ensemble",
        })

    def mean(v):
        return round(sum(v) / len(v), 2) if v else None

    print(f"{unresolved} noch nicht aufgeloest, uebersprungen\n")
    head = f"{'Gruppe':<16}{'n':>5}{'Fehler Ens':>12}{'Fehler WU':>11}{'Fehler Blend':>14}{'WU naeher':>11}"
    print(head)
    print("-" * len(head))
    for name, vals in groups.items():
        if not vals:
            print(f"{name:<16}{0:>5}")
            continue
        ens_e = [x[0] for x in vals]
        wu_e = [x[1] for x in vals]
        bl_e = [x[2] for x in vals]
        closer = sum(1 for x in vals if x[1] < x[0])
        print(f"{name:<16}{len(vals):>5}{mean(ens_e):>12}{mean(wu_e):>11}"
              f"{mean(bl_e):>14}{closer}/{len(vals):>8}")

    rej = groups["WU verworfen"]
    if rej:
        wrong = sum(1 for x in rej if x[1] < x[0])
        print(f"\nVon {len(rej)} Verwerfungen waren {wrong} falsch, "
              f"dort lag WU naeher am Ist als das Ensemble.")
        print("Ist das ein grosser Anteil, ist WU_MAX_DEV_C zu streng gesetzt.")

    print("\nJe Stadt (WU naeher / gesamt):")
    for city, v in sorted(per_city.items(), key=lambda kv: -kv[1]["n"]):
        if v["n"] >= 3:
            print(f"  {city:<16} {v['wu_besser']:>3} / {v['n']:<4}")

    with open(a.out, "w") as f:
        json.dump({"wu_weight": w, "wu_max_dev_c": cfg.wu_max_dev_c,
                   "rows": detail}, f, indent=1, default=str)
    print(f"\nEinzelfaelle in {a.out}")


if __name__ == "__main__":
    main()
