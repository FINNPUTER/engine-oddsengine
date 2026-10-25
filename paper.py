"""
paper.py: Papier-Test einer festen Regel, nur fuer den Betreiber. Seit 2.31.3.

Regel, festgelegt am 21.10.2026 BEVOR die Ergebnisse feststehen:
    nur die Staedte in PAPER_CITIES, jeder Call der Engine ab PAPER_START,
    fester Einsatz PAPER_STAKE Dollar zum veroeffentlichten Preis des Blocks.
Kein Geld, kein Kauf, keine oeffentliche Seite. Sichtbar nur in /v1/admin/ai.
"""
import json
import os

import storage


def _f(k, d):
    try:
        return float(os.getenv(k, "").strip() or d)
    except Exception:
        return float(d)


def staedte():
    roh = os.getenv("PAPER_CITIES", "guangzhou,buenos aires,amsterdam,qingdao")
    return [s.strip().lower() for s in roh.split(",") if s.strip()]


def start():
    return os.getenv("PAPER_START", "2026-10-22").strip()


def stand():
    pg = storage._init_pg()
    if not pg:
        return None
    ab, ziel, einsatz = start(), set(staedte()), _f("PAPER_STAKE", 10)
    rows = pg.execute("SELECT payload FROM snapshots WHERE payload->>'kind' = 'resolution' "
                      "AND payload->>'date' >= %s ORDER BY ts ASC", (ab,)).fetchall()
    calls, gesehen = [], set()
    for (p,) in rows:
        r = p if isinstance(p, dict) else json.loads(p)
        stadt = str(r.get("city") or "").lower()
        if stadt not in ziel or not r.get("outcome_known"):
            continue
        k = r.get("slug") or (stadt + r.get("date", ""))
        if k in gesehen:
            continue
        gesehen.add(k)
        pnl = einsatz * float(r.get("pnl_pct") or 0) / 100.0
        calls.append({"date": r.get("date"), "city": stadt, "hit": bool(r.get("outcome_hit")),
                      "cost": r.get("cost"), "pnl": round(pnl, 2),
                      "said": r.get("said_legs"), "result": r.get("outcome_label")})
    n = len(calls)
    treffer = sum(1 for c in calls if c["hit"])
    summe = round(sum(c["pnl"] for c in calls), 2)
    return {"cities": sorted(ziel), "since": ab, "stake": einsatz, "calls": n, "hits": treffer,
            "hit_rate": round(treffer / n, 3) if n else None, "pnl": summe,
            "staked": round(n * einsatz, 2),
            "roi": round(summe / (n * einsatz), 4) if n else None,
            "last": list(reversed(calls[-12:]))}
