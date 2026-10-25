"""
tief.py: Tiefstwert-Maerkte ("Lowest temperature in ..."). Seit 2.31.5.

Polymarket fuehrt seit Oktober 2026 zu fast jeder Hoechstwert-Stadt auch einen
Tiefstwert-Markt. Gleiche Station, gleiche METAR-Regel, nur der niedrigste
statt des hoechsten Werts des lokalen Tages.

Zwei Stufen:
  * immer (auch mit Schalter aus): die Engine rechnet fuer jeden Tiefstwert-
    Markt einen Call (3er-Block um die Vorhersage), friert ihn zu Beginn des
    lokalen Tages ein und wertet ihn danach aus. Dazu der Fehler der
    Vorhersage gegen die Station, je Stadt. Sichtbar nur fuer den Betreiber
    (/v1/admin/lows und die Operator-Ansicht auf der Engine-Seite).
  * Schalter "lows" an: dieselben Calls gehen an den Holder-Kanal, Ergebnisse
    ebenso, und /v1/lows zeigt offene Calls und die Bilanz oeffentlich.

Eigene Tabelle "tief". Die Hoechstwert-Bilanz wird nie angefasst.
"""
import json
import os
import statistics
import time
from datetime import datetime, timedelta, timezone

import engine
import stations
import storage
from config import cfg
from logger import log

try:
    from zoneinfo import ZoneInfo
except Exception:          # pragma: no cover
    ZoneInfo = None

_SCHEMA = [False]
_ev_cache = {}             # low slug -> (ts, event dict or None)
_ens_cache = {}            # (icao, date) -> (ts, minima, tz)
_metar_cache = {}          # (icao, date) -> (ts, result)


def _i(k, d):
    try:
        return int(float(os.getenv(k, "").strip() or d))
    except Exception:
        return d


def _f(k, d):
    try:
        return float(os.getenv(k, "").strip() or d)
    except Exception:
        return d


def an():
    try:
        import features
        return features.an("lows")
    except Exception:
        return False


# ── Speicher ─────────────────────────────────────────────────────────────

def _pg():
    pg = storage._init_pg()
    if not pg:
        return None
    if not _SCHEMA[0]:
        pg.execute("""CREATE TABLE IF NOT EXISTS tief (
            slug TEXT PRIMARY KEY, city TEXT, date TEXT, data JSONB,
            updated TIMESTAMPTZ DEFAULT now())""")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_tief_date ON tief(date)")
        _SCHEMA[0] = True
    return pg


def _laden(pg, slug):
    r = pg.execute("SELECT data FROM tief WHERE slug=%s", (slug,)).fetchone()
    if not r:
        return None
    return r[0] if isinstance(r[0], dict) else json.loads(r[0])


def _speichern(pg, d):
    pg.execute("""INSERT INTO tief (slug, city, date, data, updated) VALUES (%s,%s,%s,%s,now())
                  ON CONFLICT (slug) DO UPDATE SET data=EXCLUDED.data, updated=now()""",
               (d["slug"], d["city"], d["date"], json.dumps(d, default=str)))


# ── Markt holen ──────────────────────────────────────────────────────────

def low_slug(high_slug):
    if not high_slug or not high_slug.startswith("highest-temperature-in-"):
        return None
    return "lowest-" + high_slug[len("highest-"):]


def _event(slug):
    """Tiefstwert-Event zu einem Slug, 3 h im Speicher. None, wenn es keins gibt."""
    hit = _ev_cache.get(slug)
    if hit and time.time() - hit[0] < 3 * 3600:
        return hit[1]
    ev = None
    try:
        d = engine._get(f"{engine.GAMMA}/events", slug=slug)
        ev = d[0] if isinstance(d, list) and d else None
    except Exception as e:
        log.debug(f"tief: {slug}: {e}")
        return None            # Fehler nicht cachen, naechster Lauf versucht es erneut
    _ev_cache[slug] = (time.time(), ev)
    if len(_ev_cache) > 600:
        for k in sorted(_ev_cache, key=lambda x: _ev_cache[x][0])[:300]:
            _ev_cache.pop(k, None)
    return ev


def _parsen(ev_low, ev_high):
    """Gleiche Pruefungen wie engine.discover(): Rundung aus den Regeln,
    Station aus der Regel-URL, mindestens drei lesbare Felder."""
    if not ev_low or ev_low.get("closed"):
        return None
    desc = ev_low.get("description") or ""
    mkts = ev_low.get("markets", [])
    if not desc and mkts:
        desc = mkts[0].get("description") or ""
    rounding = stations.rounding_for(desc)
    if rounding is None:
        return None
    st = stations.coords_for(engine._url_in(desc), ev_high["city"])
    if not st:
        return None
    outs = []
    for mk in mkts:
        b = engine.parse_bucket(mk.get("groupItemTitle", ""), rounding)
        tok = engine._loads(mk.get("clobTokenIds"), [])
        if b and tok:
            b["token_id"] = tok[0]
            b["market_slug"] = mk.get("slug", "")
            outs.append(b)
    if len(outs) < 3:
        return None
    outs.sort(key=lambda x: x["sort"])
    return {"slug": ev_low.get("slug"), "city": ev_high["city"], "date": ev_high["date"],
            "lat": st[0], "lon": st[1], "station": st[2], "icao": st[3], "outcomes": outs}


# ── Vorhersage ───────────────────────────────────────────────────────────

def member_day_minima(lat, lon, target_date, key=None):
    """Tiefstwert je Ensemble-Mitglied ueber den LOKALEN Tag, aus Stundenwerten.
    Gleiche Quelle wie die Hoechstwerte (engine._ensemble_holen)."""
    ck = (key, target_date)
    hit = _ens_cache.get(ck)
    if key and hit and time.time() - hit[0] < cfg.ens_cache_min * 60:
        return hit[1], hit[2]
    data = engine._ensemble_holen(lat, lon)
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    idx = [i for i, t in enumerate(times) if t.startswith(target_date)]
    if len(idx) < 20:
        return [], None
    minima = []
    for k, series in hourly.items():
        if k == "time" or not k.startswith("temperature_2m"):
            continue
        vals = [series[i] for i in idx if i < len(series) and series[i] is not None]
        if len(vals) >= 20:
            minima.append(min(vals))
    tz = data.get("timezone", "UTC")
    if key and minima:
        _ens_cache[ck] = (time.time(), minima, tz)
        if len(_ens_cache) > 400:
            for k in sorted(_ens_cache, key=lambda x: _ens_cache[x][0])[:200]:
                _ens_cache.pop(k, None)
    return minima, tz


def _tagesbeginn(date, tz):
    """Lokale Mitternacht des Markttags als UTC-Zeitpunkt."""
    y, m, d = (int(x) for x in date.split("-"))
    if ZoneInfo and tz:
        try:
            lokal = datetime(y, m, d, tzinfo=ZoneInfo(tz))
            return lokal.astimezone(timezone.utc)
        except Exception:
            pass
    return datetime(y, m, d, tzinfo=timezone.utc)


def _offset(date, tz):
    if ZoneInfo and tz:
        try:
            y, m, d = (int(x) for x in date.split("-"))
            return int(datetime(y, m, d, 12, tzinfo=ZoneInfo(tz)).utcoffset().total_seconds())
        except Exception:
            pass
    return 0


# ── Station ──────────────────────────────────────────────────────────────

def metar_min(icao, date, tz):
    """Niedrigster Wert der Station am lokalen Tag, nach der Polymarket-Regel:
    nur routinemaessige Meldungen (kein SPECI), Zehntelwert aus der T-Gruppe,
    Zeit = echte Beobachtungszeit in Ortszeit. 48 h zurueck, damit der ganze
    Tag auch am Folgetag noch drin ist. 30 Minuten Cache."""
    ck = (icao, date)
    hit = _metar_cache.get(ck)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    try:
        data = engine._get(engine.METAR, ids=icao, format="json", hours=48)
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    off = _offset(date, tz)
    werte = []
    for m in data:
        roh = str(m.get("rawOb") or "")
        if str(m.get("metarType") or "").upper() == "SPECI" or roh.startswith("SPECI"):
            continue
        t, ot = m.get("temp"), m.get("obsTime")
        if t is None or not ot:
            continue
        try:
            lokal = datetime.fromtimestamp(int(ot), tz=timezone.utc) + timedelta(seconds=off)
        except Exception:
            continue
        if lokal.strftime("%Y-%m-%d") == date:
            werte.append(float(t))
    res = {"min_c": min(werte) if werte else None, "n": len(werte)}
    _metar_cache[ck] = (time.time(), res)
    if len(_metar_cache) > 400:
        for k in sorted(_metar_cache, key=lambda x: _metar_cache[x][0])[:200]:
            _metar_cache.pop(k, None)
    return res


def _rund(x):
    """Polymarket: auf ganze Grad, genau .5 nach oben (16.5 -> 17, -8.5 -> -8)."""
    import math
    return math.floor(x + 0.5)


def _label_fuer(outcomes, wert_c):
    """Welches Feld enthaelt den gerundeten Wert? Fahrenheit-Felder werden
    ueber ihre Grenzen in Celsius geprueft."""
    if wert_c is None:
        return None
    r = _rund(wert_c)
    ninf, pinf = float("-inf"), float("inf")
    outcomes = [dict(o, lo_c=ninf if o.get("lo_c") is None else o["lo_c"],
                     hi_c=pinf if o.get("hi_c") is None else o["hi_c"]) for o in outcomes]
    for o in outcomes:
        if o.get("unit") == "F":
            f = _rund(wert_c * 9 / 5 + 32)
            lo = float("-inf") if o["lo_c"] == float("-inf") else round(o["lo_c"] * 9 / 5 + 32 + 0.5)
            hi = float("inf") if o["hi_c"] == float("inf") else round(o["hi_c"] * 9 / 5 + 32 - 0.5)
            if lo <= f <= hi:
                return o["label"]
        elif o["lo_c"] <= r < o["hi_c"]:
            return o["label"]
    return None


# ── Lernen: Fehler je Stadt ──────────────────────────────────────────────

def _bias_je_stadt(pg):
    """Mittlerer Fehler (Vorhersage minus Station) je Stadt, zur 0 geschrumpft
    solange es wenige Tage sind. Positiv = Vorhersage zu warm."""
    rows = pg.execute("SELECT city, data FROM tief WHERE data->>'obs_min_c' IS NOT NULL").fetchall()
    fehler = {}
    for c, d in rows:
        d = d if isinstance(d, dict) else json.loads(d)
        if d.get("late"):
            continue
        f = (d.get("final") or {}).get("fc_median")
        if f is None or d.get("obs_min_c") is None:
            continue
        fehler.setdefault(c, []).append(float(f) - float(d["obs_min_c"]))
    k = _f("LOWS_BIAS_SHRINK", 5)
    out = {}
    for c, v in fehler.items():
        n = len(v)
        out[c] = {"n": n, "mean": round(statistics.mean(v), 2),
                  "mae": round(statistics.mean(abs(x) for x in v), 2),
                  "bias": round(statistics.mean(v) * n / (n + k), 2)}
    return out


# ── Ein Markt ────────────────────────────────────────────────────────────

def _call(m, minima, tz, bias):
    """3er-Block um die Vorhersage. Liefert den Call oder None."""
    now = datetime.now(timezone.utc)
    lead_h = (_tagesbeginn(m["date"], tz) - now).total_seconds() / 3600
    med_raw = statistics.median(minima)
    mitte = med_raw - bias
    outs = m["outcomes"]
    q = engine.distribution(minima, outs, max(lead_h, 0), sigma=None, bias=bias)
    if not q:
        return None
    ci = None
    for i, o in enumerate(outs):
        if o["lo_c"] <= mitte < o["hi_c"]:
            ci = i
            break
    if ci is None:
        ci = 0 if mitte < outs[0]["hi_c"] else len(outs) - 1
    a = max(0, min(ci - 1, len(outs) - 3))
    blk = list(range(a, a + 3))
    asks, kosten = [], 0.0
    for i in blk:
        b = engine.orderbook(outs[i]["token_id"])
        if not b:
            asks.append(None)
            kosten = None
            continue
        asks.append(b["ask"])
        if kosten is not None:
            kosten += b["ask"]
    # Frische Tiefstwert-Maerkte haben oft noch kein echtes Angebot: dann steht
    # nur eine Platzhalter-Order bei 99c im Buch. Das ist kein Preis.
    if kosten is not None and (kosten >= 1.0 or any(a is None or a >= 0.95 for a in asks)):
        kosten = None
    srt = sorted(minima)
    q_blk = sum(q[i] for i in blk)
    # Seit 2.37.2: Einstieg nur mit Vorsprung. Drei Felder zusammen zahlen hoechstens 1 $ aus.
    # Kostet der Block mehr als die Engine-Chance minus LOWS_EDGE, wird nicht gekauft.
    kein_kauf = None
    if kosten is not None and q_blk - kosten < _f("LOWS_EDGE", 0.05):
        kein_kauf = "price too high for the engine's chance"
        kosten = None
    return {"ts": now.isoformat(timespec="seconds"), "lead_h": round(lead_h, 1), "no_trade": kein_kauf,
            "fc_median": round(med_raw, 2), "fc_p10": round(srt[int(len(srt) * .1)], 2),
            "fc_p90": round(srt[min(len(srt) - 1, int(len(srt) * .9))], 2),
            "members": len(minima), "bias": bias,
            "block": [outs[i]["label"] for i in blk],
            "block_slugs": [outs[i].get("market_slug") for i in blk],
            "q_block": round(sum(q[i] for i in blk), 3),
            "asks": asks, "cost": round(kosten, 3) if kosten is not None else None}


def _post_call(d):
    blk = " / ".join(x.replace("°C", "").replace("°F", "") for x in d["final"]["block"])
    einheit = "°F" if "°F" in "".join(d["final"]["block"]) else "°C"
    cost = d["final"].get("cost")
    txt = (f"LOW · {d['city'].upper()} · {_tag(d['date'])}\n\n"
           f"Lowest temperature of the day at {d.get('station') or d.get('icao')}\n"
           f"Our block: {blk}{einheit}\n"
           + (f"Entry {round(cost * 100)}c\n" if cost else "")
           + f"Engine says {round(d['final']['q_block'] * 100)}%")
    btn = [("Open on Polymarket", f"https://polymarket.com/event/{d['slug']}")]
    try:
        import telegram
        telegram.send_paid(txt, btn)
        return True
    except Exception as e:
        log.warning(f"tief: post {d['slug']}: {e}")
        return False


def _post_result(d):
    f = d["final"]
    blk = " / ".join(x.replace("°C", "").replace("°F", "") for x in f["block"])
    einheit = "°F" if "°F" in "".join(f["block"]) else "°C"
    hit = d.get("hit")
    kopf = "✅ HIT" if hit else "❌ MISS"
    cost = f.get("cost")
    txt = (f"{kopf} · LOW · {d['city'].upper()} · {_tag(d['date'])}\n\n"
           f"Low came in at {d.get('result_label') or '?'}\n"
           f"Our block: {blk}{einheit}"
           + (f"\nEntry {round(cost * 100)}c" + (f" · paid {1 / cost:.2f}x" if hit and cost else "") if cost else ""))
    try:
        import telegram
        telegram.send_paid(txt)
        return True
    except Exception as e:
        log.warning(f"tief: result {d['slug']}: {e}")
        return False


def _tag(date):
    try:
        return datetime.strptime(date, "%Y-%m-%d").strftime("%b %-d")
    except Exception:
        return date


# ── Lauf ─────────────────────────────────────────────────────────────────

def tick(events):
    """Nach jedem Scan des Workers. events = die Hoechstwert-Maerkte aus
    engine.discover(); zu jedem wird der passende Tiefstwert-Markt gesucht."""
    if os.getenv("LOWS_ENABLED", "true").strip().lower() in ("0", "false", "no", "off"):
        return
    pg = _pg()
    if not pg:
        return
    bias_tab = _bias_je_stadt(pg)
    min_n = _i("LOWS_BIAS_MIN_N", 3)
    post_lead = _f("LOWS_POST_LEAD_H", 20)
    oeffentlich = an()
    neu = upd = 0
    for ev in events or []:
        ls = low_slug(ev.get("slug"))
        if not ls:
            continue
        d = _laden(pg, ls)
        if d and d.get("frozen"):
            continue
        low = _event(ls)
        m = _parsen(low, ev)
        if not m:
            continue
        try:
            minima, tz = member_day_minima(m["lat"], m["lon"], m["date"], key=m["icao"])
        except Exception as e:
            log.debug(f"tief: ensemble {m['city']}: {e}")
            continue
        if not minima:
            continue
        b = bias_tab.get(m["city"], {})
        bias = b.get("bias", 0.0) if b.get("n", 0) >= min_n else 0.0
        call = _call(m, minima, tz, bias)
        if not call:
            continue
        if not d:
            d = {"slug": ls, "city": m["city"], "date": m["date"], "icao": m["icao"],
                 "station": m["station"], "tz": tz, "first": call, "posted": False,
                 "outcomes": [{"label": o["label"], "unit": o.get("unit"),
                               "lo_c": None if o["lo_c"] == float("-inf") else o["lo_c"],
                               "hi_c": None if o["hi_c"] == float("inf") else o["hi_c"]}
                              for o in m["outcomes"]]}
            if call["lead_h"] <= 0:
                d["late"] = True     # erst nach Tagesbeginn gesehen: zaehlt nicht in die Bilanz
            neu += 1
        else:
            upd += 1
        if call["lead_h"] <= 0:
            # Lokaler Tag hat begonnen. Der letzte Call VOR Mitternacht bleibt stehen,
            # ab jetzt zaehlt nur noch die Station.
            d["frozen"] = True
            d.setdefault("final", call)
        else:
            d["final"] = call
        if (oeffentlich and not d.get("posted") and 0 < call["lead_h"] <= post_lead
                and call.get("cost")):
            d["posted"] = _post_call(d)
        _speichern(pg, d)
    aufgeloest = _aufloesen(pg, oeffentlich)
    if neu or upd or aufgeloest:
        log.info(f"tief: {neu} neu, {upd} aktualisiert, {aufgeloest} ausgewertet"
                 f"{' (oeffentlich)' if oeffentlich else ' (intern)'}")


def _aufloesen(pg, oeffentlich):
    """Eingefrorene Calls auswerten: offizielles Feld von Polymarket, sobald
    es da ist; bis dahin die eigene METAR-Auswertung (fuer den Fehler je Stadt)."""
    import resolution
    rows = pg.execute("SELECT data FROM tief WHERE (data->>'frozen')::boolean IS TRUE "
                      "AND (data->>'official') IS DISTINCT FROM 'true' "
                      "AND date >= %s", ((datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d"),)
                      ).fetchall()
    n = 0
    jetzt = datetime.now(timezone.utc)
    for (d,) in rows:
        d = d if isinstance(d, dict) else json.loads(d)
        ende = _tagesbeginn(d["date"], d.get("tz")) + timedelta(days=1)
        if jetzt < ende + timedelta(minutes=30):
            continue                  # Tag noch nicht vorbei
        geaendert = False
        if d.get("obs_min_c") is None and jetzt < ende + timedelta(hours=40):
            r = metar_min(d["icao"], d["date"], d.get("tz"))
            if r and r["min_c"] is not None and r["n"] >= _i("LOWS_MIN_REPORTS", 18):
                d["obs_min_c"] = r["min_c"]
                d["obs_n"] = r["n"]
                d["est_label"] = _label_fuer(d.get("outcomes") or [], r["min_c"])
                geaendert = True
        label, ok = resolution.winning_bucket(d["slug"])
        if ok and label:
            d["result_label"] = label
            d["official"] = True
            geaendert = True
        elif d.get("est_label") and not d.get("result_label"):
            d["result_label"] = d["est_label"]
        if d.get("result_label"):
            hit = d["result_label"] in (d.get("final") or {}).get("block", [])
            if d.get("hit") != hit:
                d["hit"] = hit
                geaendert = True
        if d.get("official") and oeffentlich and d.get("posted") and not d.get("result_posted"):
            d["result_posted"] = _post_result(d)
            geaendert = True
        if geaendert:
            _speichern(pg, d)
            n += 1
    return n


# ── Auswertung ───────────────────────────────────────────────────────────

def _zeilen(pg, ab=None):
    q = "SELECT data FROM tief"
    args = ()
    if ab:
        q += " WHERE date >= %s"
        args = (ab,)
    q += " ORDER BY date DESC, city ASC LIMIT 2000"
    return [r[0] if isinstance(r[0], dict) else json.loads(r[0]) for r in pg.execute(q, args).fetchall()]


def stand(oeffentlich=False):
    """Bilanz der Tiefstwert-Calls. oeffentlich=True blendet die Interna
    (Vorhersagewerte, Fehler je Stadt, Bias) aus."""
    pg = _pg()
    if not pg:
        return None
    rows = _zeilen(pg, os.getenv("LOWS_START", "").strip() or None)
    rows = [d for d in rows if not d.get("late")]
    fertig = [d for d in rows if d.get("result_label") is not None and d.get("final")]
    offen = [d for d in rows if not d.get("result_label") and d.get("final")]
    einsatz = _f("LOWS_STAKE", 10)
    treffer = [d for d in fertig if d.get("hit")]
    # Nur echte Einstiege: Preis unter 1 $ und mit Vorsprung gegen die Engine-Chance. Aeltere Calls
    # von vor 2.37.2 ohne diese Pruefung werden hier nachtraeglich gleich behandelt.
    rand = _f("LOWS_EDGE", 0.05)
    mit_preis = [d for d in fertig if 0 < (d["final"].get("cost") or 0) < 1
                 and (d["final"].get("q_block") or 0) - d["final"]["cost"] >= rand]
    pnl = sum((einsatz / d["final"]["cost"] - einsatz) if d.get("hit") else -einsatz for d in mit_preis)

    def kurz(d):
        f = d["final"]
        out = {"city": d["city"], "date": d["date"], "block": f["block"], "cost": f.get("cost"),
               "said": f.get("q_block"), "result": d.get("result_label"), "hit": d.get("hit"),
               "official": bool(d.get("official")), "slug": d["slug"]}
        if not oeffentlich:
            out.update({"fc_median": f.get("fc_median"), "obs_min": d.get("obs_min_c"),
                        "err": (round(f["fc_median"] - d["obs_min_c"], 2)
                                if d.get("obs_min_c") is not None and f.get("fc_median") is not None else None),
                        "bias_used": f.get("bias"), "lead_h": f.get("lead_h")})
        return out

    out = {"calls": len(fertig), "hits": len(treffer), "entries": len(mit_preis),
           "entry_hits": sum(1 for d in mit_preis if d.get("hit")),
           "skipped_price": len(fertig) - len(mit_preis),
           "hit_rate": round(len(treffer) / len(fertig), 3) if fertig else None,
           "avg_cost": round(statistics.mean(d["final"]["cost"] for d in mit_preis), 3) if mit_preis else None,
           "avg_said": round(statistics.mean(d["final"]["q_block"] for d in fertig), 3) if fertig else None,
           "stake": einsatz, "pnl": round(pnl, 2),
           "roi": round(pnl / (len(mit_preis) * einsatz), 4) if mit_preis else None,
           "open": [kurz(d) for d in offen[:40]],
           "last": [kurz(d) for d in fertig[:30]]}
    if not oeffentlich:
        out["cities"] = _bias_je_stadt(pg)
        out["switch_on"] = an()
        out["tracked"] = len(rows)
    return out
