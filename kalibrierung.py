"""
kalibrierung.py: die Engine lernt aus ihren eigenen Fehlern nach. Seit 2.18.

Zwei Dinge waren fest eingestellt und sind inzwischen falsch:

1. Der Versatz pro Stadt (city_params.json) wurde im Juli und August gemessen.
   Im Herbst liegt er woanders: Seoul verfehlt seitdem fast nur nach UNTEN
   (Korrektur zu warm), Manila und Chengdu nur nach OBEN (zu kalt).
   Jetzt: einmal am Tag wird pro Stadt nachgerechnet, wie weit die
   Modellmitte in den letzten KALIB_TAGE Tagen vom echten Stationswert lag,
   und zwar ueber ALLE beobachteten Maerkte, nicht nur unsere Calls.

2. Der Abschlag fuer Selbstueberschaetzung (COVERAGE_HAIRCUT_PP 8.8) war zu
   klein: gesagt 76 %, getroffen 62 %. Jetzt: der Abschlag folgt dem, was
   die letzten Calls wirklich getroffen haben.

Beides wird geschrumpft (wenige Messungen ziehen wenig), begrenzt und im Log
gezeigt. Aus: KALIB_ENABLED=false, dann gilt wieder alles Feste.
"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import storage
from config import cfg
from logger import log

AN = os.getenv("KALIB_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
TAGE = int(os.getenv("KALIB_TAGE", "21"))
MIN_N = int(os.getenv("KALIB_MIN_N", "8"))
SCHRUMPF = float(os.getenv("KALIB_SCHRUMPF", "6"))
BIAS_MAX = float(os.getenv("KALIB_BIAS_MAX_C", "3.0"))
HC_MAX = float(os.getenv("KALIB_HAIRCUT_MAX_PP", "20"))
HC_FENSTER = int(os.getenv("KALIB_HAIRCUT_FENSTER", "80"))
# Seit 2.20.1: wie sehr die Engine dem Marktpreis etwas voraus hat.
VT_FENSTER = int(os.getenv("KALIB_TRUST_FENSTER", "150"))
VT_MIN_N = int(os.getenv("KALIB_TRUST_MIN_N", "40"))
VT_SCHRUMPF = float(os.getenv("KALIB_TRUST_SCHRUMPF", "50"))
VT_START = float(os.getenv("KALIB_TRUST_START", "0.25"))
MAX_ABFRAGEN = int(os.getenv("KALIB_MAX_ABFRAGEN", "250"))

_stand = {"ts": 0.0, "daten": None}


def label_c(label):
    """'17°C' -> 17.0 · '90-91°F' -> 32.5 · Randfelder ('or below/higher') -> None,
    weil dort der echte Wert unbekannt ist."""
    t = str(label or "").replace("−", "-").replace("–", "-").lower()
    if re.search(r"or (below|lower|less|above|higher|more)|<|>|≤|≥", t):
        return None
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*\u00b0?\s*[fc]?\s*(?:-|to)\s*(-?\d+(?:\.\d+)?)", t)
    if m:
        mitte = (float(m.group(1)) + float(m.group(2))) / 2
    else:
        e = re.search(r"-?\d+(?:\.\d+)?", t)
        if not e:
            return None
        mitte = float(e.group())
    if "f" in t:
        mitte = (mitte - 32) * 5 / 9
    return round(mitte, 2)


def rand_c(label):
    """'16°C or higher' -> 16.0 · '60°F or below' -> 15.56. Sonst None."""
    t = str(label or "").replace("−", "-").replace("–", "-").lower()
    if not re.search(r"or (below|lower|less|above|higher|more)", t):
        return None
    e = re.search(r"-?\d+(?:\.\d+)?", t)
    if not e:
        return None
    v = float(e.group())
    return round((v - 32) * 5 / 9 if "f" in t else v, 2)


def _lead_score(r):
    """Gesucht ist die Vorhersage rund 24 h vor Schluss: weit genug weg, dass
    noch nichts gemessen ist, nah genug, dass sie der Call-Lage entspricht."""
    try:
        return abs(float(r.get("lead_h")) - 24.0)
    except Exception:
        return 99.0


def lernen(erzwingen=False):
    """Einmal am Tag (oder erzwungen). Gibt das Ergebnis zurueck."""
    if not AN:
        return None
    heute = datetime.now(timezone.utc).date()
    if not erzwingen and storage.kv_get("kalib_day") == heute.isoformat() \
            and (_laden() or {}).get("vertrauen") is not None \
            and (_laden() or {}).get("staedte"):
        return _laden()
    try:
        if not erzwingen and time.time() - float(storage.kv_get("kalib_versuch") or 0) < 3600:
            return _laden()
        storage.kv_set("kalib_versuch", str(time.time()))
    except Exception:
        pass
    ab = (heute - timedelta(days=TAGE)).isoformat()
    rows = storage.load_all()

    # Gewinner, die wir schon kennen: aus Ergebnissen und frueheren Abfragen.
    sieger = {}
    for r in rows:
        if r.get("kind") in ("resolution", "outcome") and r.get("outcome_label"):
            sieger[r.get("slug")] = r.get("outcome_label")

    # Je Markt die Vorhersage, die am naechsten an 24 h vor Schluss lag.
    beste = {}
    for r in rows:
        if r.get("kind") or r.get("ens_mean") is None or not r.get("slug"):
            continue
        d = str(r.get("date") or "")
        if not d or d < ab or d >= heute.isoformat():
            continue
        try:
            if not (12.0 <= float(r.get("lead_h")) <= 40.0):
                continue
        except Exception:
            continue
        alt = beste.get(r["slug"])
        if alt is None or _lead_score(r) < _lead_score(alt):
            beste[r["slug"]] = r

    # Fehlende Gewinner bei Polymarket nachfragen, gedeckelt.
    fehlend = [s for s in beste if s not in sieger][:MAX_ABFRAGEN]
    if fehlend:
        from resolution import winning_bucket
        neu = 0
        for slug in fehlend:
            lab, fertig = winning_bucket(slug)
            if fertig and lab:
                sieger[slug] = lab
                r = beste[slug]
                storage.save({"kind": "outcome", "slug": slug, "city": r.get("city"),
                              "date": r.get("date"), "signal": False,
                              "ts": datetime.now(timezone.utc).isoformat(),
                              "outcome_label": lab})
                neu += 1
            time.sleep(0.15)
        log.info(f"kalibrierung: {neu} von {len(fehlend)} Gewinnern nachgeladen")

    # Rest je Stadt: echter Wert minus die Mitte, die die Engine WIRKLICH benutzt (seit 2.45.1).
    # Vorher wurde gegen die rohe Ensemble-Mitte gelernt, die Engine schiebt die Mitte aber zusaetzlich
    # zur WU-Vorhersage (wu_bias_c). Lag WU zu kalt, blieb die Mitte zu kalt und der Ausgang landete
    # ueberwiegend im obersten Block oder darueber (Oktober 2026: im Schnitt +0,7 Bloecke).
    rest = {}
    for slug, r in beste.items():
        wert = label_c(sieger.get(slug))
        if wert is None:
            # Randfeld ("16°C or higher"): frueher verworfen. Gerade die heissen Ausreisser fehlten so beim Lernen.
            # Jetzt zaehlt der Randwert selbst als vorsichtige Untergrenze bzw. Obergrenze.
            wert = rand_c(sieger.get(slug))
        if wert is None:
            continue
        mitte = float(r["ens_mean"]) - float(r.get("wu_bias_c") or 0.0)
        rest.setdefault(str(r.get("city")).lower(), []).append(wert - mitte)

    staedte = {}
    for stadt, xs in rest.items():
        n = len(xs)
        m = sum(xs) / n
        sd = (sum((x - m) ** 2 for x in xs) / (n - 1)) ** 0.5 if n > 1 else None
        # distribution() rechnet mm = m - bias. Liegt die Station im Schnitt
        # WAERMER als das Modell (m > 0), muss bias negativ werden.
        b = -m * n / (n + SCHRUMPF)
        b = max(-BIAS_MAX, min(BIAS_MAX, b))
        staedte[stadt] = {"n": n, "rest_c": round(m, 2),
                          "sd_c": round(sd, 2) if sd is not None else None,
                          "bias_c": round(b, 2), "aktiv": n >= MIN_N}

    # Abschlag: was die Calls roh behauptet haben gegen das, was sie trafen.
    res = [r for r in rows if r.get("kind") == "resolution" and r.get("outcome_known")
           and isinstance(r.get("said_coverage"), (int, float))]
    res = sorted(res, key=lambda r: str(r.get("ts")))[-HC_FENSTER:]
    hc = cfg.coverage_haircut_pp
    hc_info = {"n": len(res), "haircut_pp": hc}
    if len(res) >= 20:
        roh = [r.get("said_coverage_raw") if isinstance(r.get("said_coverage_raw"), (int, float))
               else r["said_coverage"] + (r.get("haircut_pp") or cfg.coverage_haircut_pp) / 100
               for r in res]
        trefferquote = sum(1 for r in res if r.get("outcome_hit")) / len(res)
        noetig = (sum(roh) / len(roh) - trefferquote) * 100
        gewicht = len(res) / (len(res) + 20)
        hc = cfg.coverage_haircut_pp + max(noetig - cfg.coverage_haircut_pp, 0) * gewicht
        hc = round(min(max(hc, cfg.coverage_haircut_pp), HC_MAX), 1)
        hc_info = {"n": len(res), "roh_gesagt": round(sum(roh) / len(roh) * 100, 1),
                   "getroffen": round(trefferquote * 100, 1),
                   "noetig_pp": round(noetig, 1), "haircut_pp": hc}

    vt_info = vertrauen_lernen(rows)

    daten = {"ts": datetime.now(timezone.utc).isoformat(), "tage": TAGE,
             "staedte": staedte, "haircut": hc_info, "vertrauen": vt_info}
    try:
        alt = json.loads(storage.kv_get("kalibrierung") or "null")
    except Exception:
        alt = None
    # Seit 2.33: kommt nichts zurueck (Datenbank oder Polymarket gerade nicht
    # erreichbar), bleiben die alten Werte stehen, kein Lern-Post, und es wird
    # spaeter am Tag nochmal versucht.
    if not staedte:
        log.warning(f"kalibrierung: keine Daten ({len(rows)} Zeilen, {len(beste)} Maerkte), "
                    "alte Werte bleiben, kein Lern-Post, neuer Versuch in einer Stunde")
        if alt:
            _stand["daten"], _stand["ts"] = alt, time.time()
        return alt
    storage.kv_set("kalibrierung", json.dumps(daten))
    # Seit 2.20: was sich geaendert hat, als "What I learned today".
    try:
        import agent
        agent.lern_post(alt, daten)
        import feed
        for z in agent.lern_text(alt, daten).split("\n"):
            z = z.strip()
            if z and not z.startswith("What I learned") and "public.oddsengine.live" not in z:
                feed.add("Learned: " + z, "learn")
    except Exception as e:
        log.warning(f"kalibrierung: lern-post: {e}")
    storage.kv_set("kalib_day", heute.isoformat())
    _stand["daten"], _stand["ts"] = daten, time.time()

    log.info(f"kalibrierung: {len(beste)} Maerkte der letzten {TAGE} Tage, "
             f"{sum(len(v) for v in rest.values())} mit bekanntem Wert")
    log.info(f"kalibrierung: Abschlag {hc_info}")
    log.info(f"kalibrierung: Vertrauen {vt_info}")
    try:
        from engine import _CITY_PARAMS
    except Exception:
        _CITY_PARAMS = {}
    for stadt, s in sorted(staedte.items(), key=lambda kv: -abs(kv[1]["rest_c"])):
        fest = (_CITY_PARAMS.get(stadt) or {}).get("bias_c", 0.0)
        log.info(f"   {stadt:<14} n={s['n']:>2}  Station minus Modell {s['rest_c']:+.2f}C"
                 f"  -> Korrektur {s['bias_c']:+.2f} (fest war {fest:+.2f})"
                 f"{'' if s['aktiv'] else '  [zu wenig, fest bleibt]'}")
    return daten


def _laden():
    if _stand["daten"] is not None and time.time() - _stand["ts"] < 1800:
        return _stand["daten"]
    try:
        roh = storage.kv_get("kalibrierung")
        _stand["daten"] = json.loads(roh) if roh else None
    except Exception:
        _stand["daten"] = None
    _stand["ts"] = time.time()
    return _stand["daten"]


def bias(stadt):
    """Gelernte Korrektur fuer eine Stadt oder None (dann gilt die feste)."""
    if not AN:
        return None
    d = _laden() or {}
    s = (d.get("staedte") or {}).get(str(stadt or "").lower())
    if s and s.get("aktiv"):
        return float(s["bias_c"])
    return None


def haircut_pp():
    if not AN:
        return cfg.coverage_haircut_pp
    d = _laden() or {}
    try:
        return float((d.get("haircut") or {}).get("haircut_pp", cfg.coverage_haircut_pp))
    except Exception:
        return cfg.coverage_haircut_pp


# ── Vertrauen (seit 2.20.1) ──────────────────────────────────────────────
#
# Die ehrliche Frage ist nicht "wie sicher ist die Engine", sondern "wie viel
# weiss die Engine, was der Preis nicht schon weiss". Gemessen an allen
# bewerteten Calls:
#
#     ehrlich = Preis + k * (Engine - Preis)
#
# k = 0: der Preis allein sagt das Ergebnis genauso gut voraus. Dann hat die
#        Engine keinen Vorsprung, egal was sie behauptet.
# k = 1: die Engine hat voll recht, der Preis liegt daneben.
#
# k ist die Kleinste-Quadrate-Loesung (minimaler Brier-Score) ueber die
# letzten VT_FENSTER Calls, geschrumpft bei wenigen Daten und auf [0, 1]
# begrenzt. Am 15.10.2026 ergab das ueber 636 Calls k = 0,0 in BEIDEN
# Haelften der Daten. Darum steht ab jetzt in jedem Call, was die Zahlen
# hergeben, und nicht, was das Modell behauptet.

def vertrauen_lernen(rows):
    res = [r for r in rows if r.get("kind") == "resolution" and r.get("outcome_known")
           and isinstance(r.get("cost"), (int, float)) and r["cost"] > 0]
    res = sorted(res, key=lambda r: str(r.get("published_ts") or r.get("ts")))[-VT_FENSTER:]
    paare = []
    for r in res:
        roh = r.get("said_coverage_raw")
        if not isinstance(roh, (int, float)):
            roh = r.get("said_coverage")
        if not isinstance(roh, (int, float)):
            continue
        paare.append((float(roh) - r["cost"], (1.0 if r.get("outcome_hit") else 0.0) - r["cost"]))
    n = len(paare)
    if n < VT_MIN_N:
        return {"n": n, "k": VT_START, "k_roh": None, "start": True}
    nenner = sum(d * d for d, _ in paare)
    k_roh = (sum(d * y for d, y in paare) / nenner) if nenner > 0 else 0.0
    k = max(0.0, min(1.0, k_roh)) * n / (n + VT_SCHRUMPF)
    # Brier mit und ohne Engine, damit das Log zeigt, ob sie etwas beitraegt.
    b_preis = sum(y * y for _, y in paare) / n
    b_engine = sum((y - d) ** 2 for d, y in paare) / n
    return {"n": n, "k": round(k, 3), "k_roh": round(k_roh, 3),
            "brier_preis": round(b_preis, 4), "brier_engine": round(b_engine, 4)}


def vertrauen():
    """Faktor k in [0, 1]. Ohne Daten der Startwert."""
    if not AN:
        return VT_START
    d = _laden() or {}
    try:
        return float((d.get("vertrauen") or {}).get("k", VT_START))
    except Exception:
        return VT_START
