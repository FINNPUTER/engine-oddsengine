"""
feed.py: Engine Live. Seit 2.23.

Die Engine schreibt mit, was sie gerade tut: welche Maerkte sie liest, was die
Stationen melden, welche Calls rausgehen, was sie gelernt hat, was sich auf
Kalshi tut, was abgerechnet wurde. Jede Zeile kommt aus einer echten Handlung
des Workers, nichts ist gestellt.

Zeilen werden waehrend eines Scans gesammelt und am Ende in einem Rutsch in den
kv-Speicher geschrieben ("engine_feed", die letzten MAX Zeilen).

Jede Zeile hat einen oeffentlichen Text und optional einen fuer Holder. Wer
nicht Holder ist, sieht bei Calls nur, DASS einer rausging, nicht welcher.
"""
import json
import os
from datetime import datetime, timezone

import storage
from logger import log

MAX = int(os.getenv("FEED_MAX", "150"))
KV = "engine_feed"
_puffer = []


def add(text, art="info", holder_text=None, stadt=None):
    _puffer.append({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "kind": art, "text": str(text)[:220],
                    "holder_text": str(holder_text)[:220] if holder_text else None,
                    "city": stadt})


def flush():
    global _puffer
    if not _puffer:
        return
    try:
        roh = storage.kv_get(KV)
        alt = json.loads(roh) if roh else []
    except Exception:
        alt = []
    neu = (alt + _puffer)[-MAX:]
    if storage.kv_set(KV, json.dumps(neu)):
        _puffer = []
    else:
        _puffer = _puffer[-MAX:]


def lesen(n=60):
    try:
        roh = storage.kv_get(KV)
        return (json.loads(roh) if roh else [])[-n:]
    except Exception as e:
        log.debug(f"feed lesen: {e}")
        return []


def grad(snap, celsius):
    """Temperatur in der Einheit des Markts, ganzzahlig."""
    if celsius is None:
        return None
    f = any("°f" in str(l).lower() for l in (snap.get("full_dist") or {}))
    v = celsius * 9 / 5 + 32 if f else celsius
    return f"{int(round(v))}°{'F' if f else 'C'}"


def stadt(s):
    s = str(s or "")
    return {"nyc": "NYC", "la": "LA", "sf": "SF"}.get(s.lower(), s.title())
