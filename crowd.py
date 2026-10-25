"""
crowd.py: Engine vs Crowd. Seit 2.21.

Der Crowd-Pick ist der Block, den der Markt selbst fuer am wahrscheinlichsten
haelt: die drei nebeneinanderliegenden Stufen mit dem hoechsten Gesamtpreis.
Im Rueckblick ueber 636 Calls (15.10.2026) traf dieser Block bei einem Preis
von 75 bis 85c in 86 % der Faelle, in beiden Haelften der Daten. Das ist
nach Kosten ein kleiner Vorsprung, aber noch kein belegter. Darum wird er ab
jetzt live mitgeschrieben und bewertet, Markt fuer Markt.

Pro Markt wird genau EIN Crowd-Pick festgehalten: beim ersten Scan, in dem
der Block im Preisband liegt und der Markt noch mindestens CROWD_MIN_LEAD_H
Stunden offen ist. Danach wird nichts mehr veraendert, sonst waere es kein
Test.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import re

import storage
from logger import log


def _wert(label):
    """Sortierschluessel einer Stufe: '82-83°F' -> 82.5, '11°C or below' -> 10.5."""
    t = str(label or "").replace("\u2212", "-").lower()
    n = [float(x) for x in re.findall(r"(?:(?<=^)|(?<=\s))-?\d+(?:\.\d+)?|\d+(?:\.\d+)?", t)]
    if not n:
        return None
    if "below" in t or "lower" in t:
        return n[0] - 0.5
    if "higher" in t or "above" in t:
        return n[0] + 0.5
    return (n[0] + n[-1]) / 2

MIN_KOSTEN = float(os.getenv("CROWD_MIN_COST", "0.75"))
MAX_KOSTEN = float(os.getenv("CROWD_MAX_COST", "0.85"))
MIN_LEAD = float(os.getenv("CROWD_MIN_LEAD_H", "6"))
MAX_LEAD = float(os.getenv("CROWD_MAX_LEAD_H", "48"))
BREITE = int(os.getenv("CROWD_WIDTH", "3"))
KV_OFFEN = "crowd_open"
KV_FERTIG = "crowd_done"


def _laden(k):
    try:
        roh = storage.kv_get(k)
        return json.loads(roh) if roh else {}
    except Exception:
        return {}


def _speichern(k, d):
    storage.kv_set(k, json.dumps(d))


def block(preise, labels):
    """Die BREITE nebeneinanderliegenden Stufen mit dem hoechsten Gesamtpreis.
    labels in Reihenfolge der Stufen. Gibt (legs, asks, kosten) oder None."""
    if len(labels) < BREITE:
        return None
    best = None
    for i in range(len(labels) - BREITE + 1):
        teil = labels[i:i + BREITE]
        a = [preise.get(l) for l in teil]
        if any(not isinstance(x, (int, float)) or x <= 0 for x in a):
            continue
        s = sum(a)
        if best is None or s > best[2]:
            best = (teil, a, s)
    return best


def merken(snap):
    """Vom worker fuer jeden bewerteten Markt gerufen."""
    try:
        slug = snap.get("slug")
        dist = snap.get("full_dist") or {}
        preise = snap.get("market_prices") or {}
        lead = snap.get("lead_h")
        if not slug or not dist or not preise or not isinstance(lead, (int, float)):
            return
        if not (MIN_LEAD <= lead <= MAX_LEAD):
            return
        offen = _laden(KV_OFFEN)
        if slug in offen:
            return
        fertig = _laden(KV_FERTIG)
        if slug in fertig:
            return
        labels = [l for l in dist.keys() if _wert(l) is not None]
        labels.sort(key=_wert)
        b = block(preise, labels)
        if not b:
            return
        legs, asks, kosten = b
        if not (MIN_KOSTEN <= kosten <= MAX_KOSTEN):
            return
        modell = sum(float(dist.get(l) or 0) for l in legs)
        offen[slug] = {
            "slug": slug, "city": snap.get("city"), "date": snap.get("date"),
            "station": snap.get("station"), "ts": snap.get("ts"),
            "lead_h": round(float(lead), 1),
            "legs": legs, "asks": [round(a, 4) for a in asks],
            "cost": round(kosten, 4), "model": round(modell, 4),
            "engine_legs": snap.get("legs") if snap.get("signal") else None,
        }
        _speichern(KV_OFFEN, offen)
        log.info(f"crowd: {snap.get('city')} {snap.get('date')} "
                 f"{' / '.join(legs)} at {kosten*100:.0f}c gemerkt")
        try:
            import feed
            import features
            if features.an("crowd"):
                feed.add(f"{feed.stadt(snap.get('city'))} · crowd favourite locked: "
                         f"{' / '.join(legs)} at {kosten*100:.0f}c. Scored after the day.", "crowd")
        except Exception:
            pass
        # Seit 2.22: derselbe Pick auch mit dem Kalshi-Preis, wo moeglich.
        try:
            import kalshi_spur
            kalshi_spur.merken(offen[slug], "crowd")
        except Exception as e:
            log.debug(f"crowd kalshi: {e}")
    except Exception as e:
        log.debug(f"crowd merken: {e}")


def tick(max_fragen=25):
    """Bewertet offene Crowd-Picks, deren Tag vorbei ist."""
    offen = _laden(KV_OFFEN)
    if not offen:
        return 0
    from resolution import winning_bucket
    gestern = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    fertig = _laden(KV_FERTIG)
    n = 0
    for slug, p in list(offen.items()):
        if n >= max_fragen:
            break
        if str(p.get("date") or "9999") > gestern:
            continue
        n += 1
        lab, ok = winning_bucket(slug)
        if not ok or not lab:
            # Nach 5 Tagen ohne Ergebnis aufgeben, sonst waechst die Liste.
            try:
                alt = (datetime.now(timezone.utc).date()
                       - datetime.fromisoformat(str(p["date"])).date()).days
            except Exception:
                alt = 0
            if alt > 5:
                offen.pop(slug, None)
            continue
        hit = lab in (p.get("legs") or [])
        r = dict(p, kind="crowd_result", outcome_label=lab, outcome_hit=hit,
                 pnl_pct=round(((1 / p["cost"]) - 1) * 100 if hit else -100.0, 2),
                 signal=False, ts=datetime.now(timezone.utc).isoformat(),
                 picked_ts=p.get("ts"))
        storage.save(r)
        fertig[slug] = 1
        offen.pop(slug, None)
        log.info(f"crowd: {p.get('city')} {p.get('date')} "
                 f"{'HIT' if hit else 'MISS'} ({lab})")
    _speichern(KV_OFFEN, offen)
    # Nur die letzten 3000 Slugs behalten, mehr braucht die Doppelpruefung nicht.
    if len(fertig) > 3000:
        fertig = dict(list(fertig.items())[-3000:])
    _speichern(KV_FERTIG, fertig)
    return n


def offen():
    return list(_laden(KV_OFFEN).values())


def bilanz(rows):
    """Kennzahlen aus crowd_result-Zeilen."""
    xs = [r for r in rows if r.get("kind") == "crowd_result"
          and isinstance(r.get("cost"), (int, float)) and r["cost"] > 0]
    if not xs:
        return {"n": 0}
    n = len(xs)
    h = sum(1 for r in xs if r.get("outcome_hit"))
    roi = sum((1 / r["cost"] - 1) if r.get("outcome_hit") else -1 for r in xs) / n
    return {"n": n, "hits": h, "hit_rate": round(h / n, 4),
            "price_said": round(sum(r["cost"] for r in xs) / n, 4),
            "roi_pct": round(roi * 100, 2)}
