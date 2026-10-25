"""
kalshi_spur.py: die Bilanz auf Kalshi. Seit 2.22.

Jeder Call der Engine und jeder Crowd-Pick in einer Stadt, die auch Kalshi
handelt, wird im selben Moment auch mit dem Kalshi-Preis festgehalten: welche
Kalshi-Maerkte genau diese Stufen abdecken und was sie gerade kosten. Nach dem
Tag fragt der Worker bei Kalshi nach, welche Stufe gewonnen hat. Kalshi
rechnet auf seiner EIGENEN Station ab, deshalb zaehlt hier Kalshis Ergebnis,
nicht das von Polymarket.

Festgehalten wird nur, wenn JEDE Stufe des Blocks genau einem Kalshi-Markt
entspricht. Passt eine Stufe nicht (andere Gradpaare), gibt es keinen
Kalshi-Eintrag, statt einen schiefen Block zu werten.

Gespeichert: offene Eintraege im kv "kalshi_open", fertige als Zeilen mit
kind="kalshi_result" in derselben Tabelle wie alles andere.
"""
import json
import time
from datetime import datetime, timedelta, timezone

import requests

import kalshi
import storage
from logger import log

KV_OFFEN = "kalshi_open"


def _laden():
    try:
        roh = storage.kv_get(KV_OFFEN)
        return json.loads(roh) if roh else {}
    except Exception:
        return {}


def _speichern(d):
    storage.kv_set(KV_OFFEN, json.dumps(d))


def merken(snap, quelle):
    """snap braucht city, date, slug, legs. quelle: 'engine' oder 'crowd'."""
    try:
        stadt = str(snap.get("city") or "").lower()
        if not kalshi.AN or stadt not in kalshi.SERIES or not snap.get("legs"):
            return None
        schluessel = f"{quelle}|{snap.get('slug')}"
        offen = _laden()
        if schluessel in offen:
            return None
        tag = kalshi.fuer(stadt, snap.get("date"))
        if not tag or not tag.get("buckets"):
            return None
        nach_grenzen = {(b["lo"], b["hi"]): b for b in tag["buckets"]}
        beine = []
        for lab in snap["legs"]:
            g = kalshi.grenzen(lab)
            b = nach_grenzen.get(g) if g else None
            if not b or not b.get("ticker") or not b.get("ask"):
                return None
            beine.append({"label": lab, "kalshi_label": b["label"],
                          "ticker": b["ticker"], "ask": b["ask"]})
        kosten = round(sum(x["ask"] for x in beine), 4)
        if not (0 < kosten < 1):
            return None
        offen[schluessel] = {
            "source": quelle, "slug": snap.get("slug"), "city": stadt,
            "date": snap.get("date"), "legs": snap["legs"], "kalshi": beine,
            "cost": kosten, "same_station": bool(tag.get("same_station")),
            "kalshi_station": tag.get("station"), "series": tag.get("series"),
            "ts": datetime.now(timezone.utc).isoformat(),
            "polymarket_cost": snap.get("cost"),
        }
        _speichern(offen)
        try:
            import feed
            import features
            if features.an("kalshi") and snap.get("cost"):
                feed.add(f"{feed.stadt(stadt)} · same block on Kalshi {kosten*100:.0f}c, "
                         f"on Polymarket {float(snap['cost'])*100:.0f}c.", "kalshi")
        except Exception:
            pass
        log.info(f"kalshi-spur: {quelle} {stadt} {snap.get('date')} "
                 f"auch auf Kalshi festgehalten, {kosten*100:.0f}c")
        return offen[schluessel]
    except Exception as e:
        log.debug(f"kalshi-spur merken: {e}")
        return None


def _ergebnis(ticker):
    """'yes' / 'no' sobald Kalshi abgerechnet hat, sonst None."""
    for base in kalshi.BASES:
        try:
            r = requests.get(f"{base}/markets/{ticker}", timeout=kalshi.TIMEOUT,
                             headers={"Accept": "application/json"})
            if r.status_code != 200:
                continue
            m = (r.json() or {}).get("market") or {}
            if str(m.get("status")) in ("finalized", "settled") and m.get("result") in ("yes", "no"):
                return m["result"]
            return None
        except Exception:
            continue
    return None


def tick(max_maerkte=12):
    offen = _laden()
    if not offen:
        return 0
    gestern = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    n = 0
    for k, p in list(offen.items()):
        if n >= max_maerkte:
            break
        if str(p.get("date") or "9999") > gestern:
            continue
        n += 1
        res = [_ergebnis(b["ticker"]) for b in p.get("kalshi") or []]
        time.sleep(0.1)
        if any(r is None for r in res):
            try:
                alt = (datetime.now(timezone.utc).date()
                       - datetime.fromisoformat(str(p["date"])).date()).days
            except Exception:
                alt = 0
            if alt > 7:
                offen.pop(k, None)
            continue
        hit = any(r == "yes" for r in res)
        gewinner = next((b["kalshi_label"] for b, r in zip(p["kalshi"], res) if r == "yes"), None)
        storage.save(dict(p, kind="kalshi_result", outcome_hit=hit, outcome_label=gewinner,
                          signal=False, picked_ts=p.get("ts"),
                          ts=datetime.now(timezone.utc).isoformat(),
                          pnl_pct=round(((1 / p["cost"]) - 1) * 100 if hit else -100.0, 2)))
        offen.pop(k, None)
        log.info(f"kalshi-spur: {p['source']} {p['city']} {p['date']} {'HIT' if hit else 'MISS'}")
    _speichern(offen)
    return n


def offen():
    return list(_laden().values())


def bilanz(rows, quelle=None):
    xs = [r for r in rows if r.get("kind") == "kalshi_result"
          and (quelle is None or r.get("source") == quelle)
          and isinstance(r.get("cost"), (int, float)) and r["cost"] > 0]
    if not xs:
        return {"n": 0}
    n = len(xs)
    h = sum(1 for r in xs if r.get("outcome_hit"))
    roi = sum((1 / r["cost"] - 1) if r.get("outcome_hit") else -1 for r in xs) / n
    return {"n": n, "hits": h, "hit_rate": round(h / n, 4),
            "price_said": round(sum(r["cost"] for r in xs) / n, 4),
            "roi_pct": round(roi * 100, 2)}
