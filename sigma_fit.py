#!/usr/bin/env python3
"""
sigma_fit.py — misst die Dressing-Breite je Stadt, statt sie zu raten.

Die Engine baut ihre Verteilung so:

    Gesamtbreite^2  =  Streuung der Modelle untereinander  +  sigma^2

Bekannt war bisher nur die Gesamtbreite (der Fehler unseres Reads gegen die
Station). Dieses Skript misst zusaetzlich die Modellstreuung und loest damit
nach sigma auf. Ohne diesen Schritt wuerde man die Modellstreuung doppelt
zaehlen und die Verteilung zu breit machen.

Zusaetzlich beruecksichtigt es den Vorlauf-Term, den distribution() aufschlaegt:

    sigma_eff^2 = sigma^2 + (SIGMA_PER_DAY_C * lead_h / 24)^2

Aufruf (Railway-Terminal, Service worker, Tab Console):

    python sigma_fit.py --since 2026-07-01 --until 2026-07-22
    python sigma_fit.py --since 2026-07-01 --until 2026-07-22 --write

Ohne --write wird nur gerechnet und angezeigt. Mit --write entsteht
city_params.json neu, mit bias_c und sigma_c je Stadt.
"""
import argparse
import json
import math
import statistics as st
import time
from collections import defaultdict

import backtest_roi as bt
from config import cfg
from field_backtest import _archive_range, _actual_c

# Unter diesen Wert geht es nicht: eine Verteilung ohne jede Breite behauptet
# Gewissheit, die 20 Messtage niemals belegen koennen.
SIGMA_FLOOR = 0.35
SIGMA_CEIL = 3.00
MIN_DAYS = 12          # weniger Tage, keine eigene Zahl
BIAS_MIN_CONSISTENCY = 0.80
BIAS_MIN_ABS = 0.50


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--until", required=True)
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--lead", type=float, default=12.0)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--out", default="city_params.json")
    a = ap.parse_args()

    markets = bt.fetch_resolved(a.limit, since=a.since, until=a.until)
    print(f"{len(markets)} aufgeloeste Maerkte im Fenster\n")

    by_station = defaultdict(list)
    for m in markets:
        d = (m.get("end") or "")[:10]
        if d:
            by_station[(m["icao"], m["lat"], m["lon"])].append(d)

    cache = {}
    for (icao, lat, lon), ds in by_station.items():
        try:
            for d, mx in _archive_range(lat, lon, min(ds), max(ds)).items():
                cache[(icao, d)] = mx
        except Exception:
            pass
        time.sleep(0.25)

    per_city = defaultdict(lambda: {"err": [], "spread": []})
    for m in markets:
        d = (m.get("end") or "")[:10]
        maxima = cache.get((m["icao"], d))
        if not maxima or len(maxima) < 3 or not m.get("winner"):
            continue
        actual = _actual_c(m["winner"])
        if actual is None:
            continue
        city = (m.get("city") or "").lower()
        per_city[city]["err"].append(sum(maxima) / len(maxima) - actual)
        per_city[city]["spread"].append(st.pstdev(maxima))

    lead_term = cfg.sigma_per_day_c * max(a.lead, 0.0) / 24.0

    rows = []
    for city, v in per_city.items():
        n = len(v["err"])
        if n < MIN_DAYS:
            continue
        bias = st.fmean(v["err"])                    # unser Read minus Ist
        resid = st.pstdev(v["err"])                  # Breite NACH Bias-Abzug
        ens = st.fmean(v["spread"])                  # Streuung der Modelle
        pos = sum(1 for e in v["err"] if e > 0) / n
        cons = round(abs(2 * pos - 1), 2)

        # Gesamtbreite^2 = Modellstreuung^2 + sigma^2 + Vorlauf^2
        inner = resid ** 2 - ens ** 2 - lead_term ** 2
        sigma = math.sqrt(inner) if inner > 0 else SIGMA_FLOOR
        sigma = round(min(max(sigma, SIGMA_FLOOR), SIGMA_CEIL), 2)

        use_bias = (cons >= BIAS_MIN_CONSISTENCY and abs(bias) >= BIAS_MIN_ABS)
        rows.append({"city": city, "n": n, "bias": round(bias, 2),
                     "cons": cons, "resid": round(resid, 2),
                     "ens": round(ens, 2), "sigma": sigma,
                     "use_bias": use_bias,
                     "capped": inner <= 0})

    rows.sort(key=lambda r: r["sigma"])
    print(f"Vorlauf-Aufschlag bei {a.lead:.0f}h: {lead_term:.2f} C")
    print(f"Bisher global fuer alle Staedte: {cfg.dress_sigma_c:.2f} C\n")
    head = (f"{'Stadt':<15}{'n':>4}{'Bias':>7}{'Kons':>6}"
            f"{'Gesamt':>8}{'Modelle':>9}{'sigma':>8}")
    print(head)
    print("-" * len(head))
    for r in rows:
        mark = " *" if r["use_bias"] else ""
        flag = "  <- Untergrenze" if r["capped"] else ""
        print(f"{r['city']:<15}{r['n']:>4}{r['bias']:>7.2f}{r['cons']:>6.2f}"
              f"{r['resid']:>8.2f}{r['ens']:>9.2f}{r['sigma']:>8.2f}{mark}{flag}")
    print("\n* = Bias wird gesetzt (Konsistenz >= "
          f"{BIAS_MIN_CONSISTENCY}, Betrag >= {BIAS_MIN_ABS} C)")
    print(f"{sum(1 for r in rows if r['capped'])} von {len(rows)} Staedten "
          f"auf die Untergrenze {SIGMA_FLOOR} gesetzt: dort ist die gemessene "
          "Breite kleiner als die Modellstreuung, also nicht aufloesbar.")

    if not a.write:
        print("\nNur gerechnet. Mit --write wird city_params.json geschrieben.")
        return

    cities = {}
    for r in rows:
        e = {"sigma_c": r["sigma"], "consistency": r["cons"], "n": r["n"]}
        if r["use_bias"]:
            e["bias_c"] = r["bias"]
        cities[r["city"]] = e

    doc = {"_meta": {
        "note": ("sigma_c gemessen, nicht geraten: Gesamtbreite des Readfehlers "
                 "abzueglich der Modellstreuung und des Vorlauf-Terms. bias_c nur "
                 f"bei Konsistenz >= {BIAS_MIN_CONSISTENCY} und Betrag >= "
                 f"{BIAS_MIN_ABS} C. distribution() rechnet mm = m - bias_c, "
                 "ein negativer Wert schiebt den Read also waermer."),
        "source": f"sigma_fit.py {a.since}..{a.until}, lead {a.lead}h",
        "sigma_floor": SIGMA_FLOOR, "min_days": MIN_DAYS,
        "mode": ("erst in Paper messen; kein echtes Geld auf eine neu "
                 "kalibrierte Stadt, bevor ihr eigener Live-Record gruen ist")},
        "cities": cities}
    with open(a.out, "w") as f:
        json.dump(doc, f, indent=2)
    print(f"\n{len(cities)} Staedte nach {a.out} geschrieben.")
    print("Datei ins Repo laden, dann liest die Engine sie beim naechsten Start.")


if __name__ == "__main__":
    main()
