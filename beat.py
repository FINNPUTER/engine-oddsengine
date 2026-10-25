"""
beat.py: Beat the Engine. Seit 2.26.

Ein kostenloses Tippspiel fuer Holder, Mensch gegen Maschine, ohne Einsatz
und ohne Preise ausser Ruhm.

Ablauf je Markttag:
  1. Der worker waehlt fuer jeden Markttag drei Staedte aus (sobald die
     Maerkte des Tages auftauchen und noch lange genug offen sind).
  2. Holder tippen je Stadt ein Bracket, bis der Tipp schliesst
     (LOCK_H Stunden vor Marktschluss).
  3. Die Engine tippt dieselben Staedte: ihr wahrscheinlichstes Bracket aus
     dem letzten Scan VOR dem Schluss. Sichtbar erst nach dem Schluss.
  4. Nach dem Tag wird mit dem echten Ergebnis von Polymarket gewertet.

Punkte:  richtig = 1, richtig und die Engine lag falsch = 3,
         alle drei Staedte des Tages richtig = +2 Bonus.

Speicher (kv, Postgres), getrennt nach Schreiber, damit sich web und
worker nichts ueberschreiben:
  beat_games            worker   Spiele je Markt und Auswahl je Tag
  beat_scores           worker   Wochen- und Gesamtwertung
  beat_picks_<datum>    web      Tipps der Holder fuer einen Markttag
  beat_names            web      selbst gewaehlte Namen
"""
import hashlib
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone

import storage
from logger import log

STAEDTE = int(os.getenv("BEAT_CITIES", "3"))
LOCK_H = float(os.getenv("BEAT_LOCK_H", "14"))       # Tipp schliesst so viele Stunden vor Marktschluss
MIN_OFFEN_H = float(os.getenv("BEAT_MIN_OPEN_H", "8"))  # mindestens so lange muss getippt werden koennen
MAX_LEAD_H = float(os.getenv("BEAT_MAX_LEAD_H", "60"))
P_RICHTIG, P_SCHLAEGT, P_ALLE = 1, 3, 2

KV_SPIELE = "beat_games"
KV_STAND = "beat_scores"
KV_NAMEN = "beat_names"

_sperre = threading.Lock()


# ── Hilfen ───────────────────────────────────────────────────────────────

def _laden(k, leer=None):
    try:
        roh = storage.kv_get(k)
        return json.loads(roh) if roh else (leer if leer is not None else {})
    except Exception:
        return leer if leer is not None else {}


def _speichern(k, d):
    return storage.kv_set(k, json.dumps(d))


def _wert(label):
    """Sortierschluessel einer Stufe: '82-83°F' -> 82.5, '11°C or below' -> 10.5."""
    t = str(label or "").replace("−", "-").lower()
    n = [float(x) for x in re.findall(r"(?:(?<=^)|(?<=\s))-?\d+(?:\.\d+)?|\d+(?:\.\d+)?", t)]
    if not n:
        return None
    if "below" in t or "lower" in t:
        return n[0] - 0.5
    if "higher" in t or "above" in t:
        return n[0] + 0.5
    return (n[0] + n[-1]) / 2


def _jetzt():
    return datetime.now(timezone.utc)


def _zeit(s):
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def woche(datum):
    """ISO-Woche eines Markttags: '2026-W43'."""
    try:
        d = datetime.fromisoformat(str(datum)).date()
        j, w, _ = d.isocalendar()
        return f"{j}-W{w:02d}"
    except Exception:
        return "?"


def kurz(adresse):
    a = str(adresse or "")
    return a[:4] + "…" + a[-4:] if len(a) > 10 else a


def offen(spiel, jetzt=None):
    t = _zeit(spiel.get("lock_at"))
    return bool(t) and (jetzt or _jetzt()) < t


# ── worker: Auswahl, Engine-Tipp, Wertung ────────────────────────────────

def _brackets(snap):
    dist = snap.get("full_dist") or {}
    preise = snap.get("market_prices") or {}
    labels = [l for l in dist if _wert(l) is not None]
    labels.sort(key=_wert)
    return [{"label": l, "market": (round(float(preise[l]), 4)
                                    if isinstance(preise.get(l), (int, float)) else None)}
            for l in labels]


def _favorit(snap):
    dist = snap.get("full_dist") or {}
    beste = None
    for l, q in dist.items():
        if _wert(l) is None or not isinstance(q, (int, float)):
            continue
        if beste is None or q > beste[1]:
            beste = (l, q)
    return beste[0] if beste else None


def runde(snaps):
    """Einmal pro Scan vom worker, mit allen Schnappschuessen dieses Scans."""
    try:
        import features
        # Laeuft auch ausgeschaltet mit, damit beim Einschalten schon
        # Spiele bereitstehen. Ohne Datenbank gibt es nichts zu tun.
        _ = features
    except Exception:
        pass
    try:
        _runde(list(snaps))
    except Exception as e:
        log.error(f"beat runde: {e}")


def _runde(snaps):
    d = _laden(KV_SPIELE)
    spiele = d.setdefault("games", {})
    tage = d.setdefault("days", {})
    jetzt = _jetzt()
    geaendert = False
    nach_tag = {}
    for s in snaps:
        if not s.get("slug") or not s.get("full_dist") or not s.get("date"):
            continue
        nach_tag.setdefault(str(s["date"]), []).append(s)

    # 1. Auswahl: je Markttag einmal drei Staedte.
    for tag, xs in nach_tag.items():
        if tag in tage:
            continue
        kandidaten = []
        for s in xs:
            lead = s.get("lead_h")
            if not isinstance(lead, (int, float)):
                continue
            if not (LOCK_H + MIN_OFFEN_H <= lead <= MAX_LEAD_H):
                continue
            if len(_brackets(s)) < 4:
                continue
            kandidaten.append(s)
        # Erst waehlen, wenn genug Staedte des Tages da sind.
        if len(kandidaten) < STAEDTE:
            continue
        kandidaten.sort(key=lambda s: hashlib.sha1((tag + s["slug"]).encode()).hexdigest())
        gewaehlt, staedte = [], set()
        for s in kandidaten:
            if s.get("city") in staedte:
                continue
            gewaehlt.append(s)
            staedte.add(s.get("city"))
            if len(gewaehlt) >= STAEDTE:
                break
        for s in gewaehlt:
            t = _zeit(s.get("ts")) or jetzt
            lock = t + timedelta(hours=float(s["lead_h"]) - LOCK_H)
            labels = _brackets(s)
            f = any("°f" in str(b["label"]).lower() for b in labels)
            spiele[s["slug"]] = {
                "slug": s["slug"], "city": s.get("city"), "date": tag,
                "station": s.get("station"), "unit": "F" if f else "C",
                "lock_at": lock.isoformat(timespec="seconds"),
                "brackets": labels, "engine": None, "engine_draft": _favorit(s),
                "result": None, "scored": False,
            }
        tage[tag] = [s["slug"] for s in gewaehlt]
        geaendert = True
        log.info(f"beat: {tag} -> {', '.join(str(s.get('city')) for s in gewaehlt)}")
        try:
            import feed
            import features
            if features.an("beat"):
                feed.add(f"Beat the Engine · new round for {tag}: "
                         + ", ".join(feed.stadt(s.get("city")) for s in gewaehlt)
                         + ". Picks are open.", "info")
        except Exception:
            pass

    # 2. Laufende Spiele: Preise und Engine-Entwurf bis zum Schluss mitziehen,
    #    nach dem Schluss den Entwurf festschreiben.
    aktuell = {s["slug"]: s for s in snaps if s.get("slug")}
    gesperrt_neu = []
    for slug, g in spiele.items():
        if g.get("engine"):
            continue
        s = aktuell.get(slug)
        if offen(g, jetzt):
            if s:
                g["brackets"] = _brackets(s) or g["brackets"]
                fav = _favorit(s)
                if fav:
                    g["engine_draft"] = fav
                geaendert = True
        elif g.get("engine_draft"):
            g["engine"] = g["engine_draft"]
            g["locked_ts"] = jetzt.isoformat(timespec="seconds")
            gesperrt_neu.append(g)
            geaendert = True
    if gesperrt_neu:
        try:
            import feed
            import features
            if features.an("beat"):
                for g in gesperrt_neu:
                    feed.add(f"Beat the Engine · {feed.stadt(g.get('city'))}: picks closed. "
                             f"The engine went {g['engine']}.", "info")
        except Exception:
            pass

    # Alte Spiele aufraeumen (aelter als 21 Tage und gewertet).
    grenze = (jetzt - timedelta(days=21)).date().isoformat()
    for slug in [k for k, g in spiele.items() if g.get("scored") and str(g.get("date")) < grenze]:
        spiele.pop(slug, None)
        geaendert = True
    for tag in [t for t in tage if t < grenze]:
        tage.pop(tag, None)
        geaendert = True
    if geaendert:
        _speichern(KV_SPIELE, d)


def tick(max_fragen=12):
    """Ergebnisse holen und fertige Tage werten. Vom worker nach dem Scan."""
    try:
        return _tick(max_fragen)
    except Exception as e:
        log.error(f"beat tick: {e}")
        return 0


def _tick(max_fragen):
    from resolution import winning_bucket
    d = _laden(KV_SPIELE)
    spiele = d.get("games", {})
    tage = d.get("days", {})
    if not spiele:
        return 0
    gestern = (_jetzt() - timedelta(days=1)).date().isoformat()
    n = 0
    geaendert = False
    for slug, g in spiele.items():
        if n >= max_fragen:
            break
        if g.get("result") or g.get("scored") or str(g.get("date")) > gestern:
            continue
        n += 1
        lab, ok = winning_bucket(slug)
        if ok and lab:
            g["result"] = lab
            geaendert = True
        else:
            try:
                alt = (_jetzt().date() - datetime.fromisoformat(str(g["date"])).date()).days
            except Exception:
                alt = 0
            if alt > 5:
                g["result"] = "void"
                geaendert = True
    for tag, slugs in tage.items():
        gs = [spiele[s] for s in slugs if s in spiele]
        if not gs or any(g.get("scored") for g in gs):
            continue
        if not all(g.get("result") for g in gs):
            continue
        _werten(tag, gs)
        for g in gs:
            g["scored"] = True
        geaendert = True
    if geaendert:
        _speichern(KV_SPIELE, d)
    return n


def _leer():
    return {"pts": 0, "beats": 0, "hits": 0, "picks": 0, "days": 0, "perfect": 0}


def _werten(tag, gs):
    picks = _laden("beat_picks_" + tag)
    stand = _laden(KV_STAND)
    w = woche(tag)
    wochen = stand.setdefault("weeks", {})
    wo = wochen.setdefault(w, {})
    alle = stand.setdefault("all", {})
    eng = stand.setdefault("engine", {"weeks": {}, "all": {"hits": 0, "n": 0}})
    serie = stand.setdefault("streak", {})
    gewertet = [g for g in gs if g.get("result") not in (None, "void")]
    # Engine
    eh = sum(1 for g in gewertet if g.get("engine") and g["engine"] == g["result"])
    ew = eng["weeks"].setdefault(w, {"hits": 0, "n": 0})
    for z in (ew, eng["all"]):
        z["hits"] += eh
        z["n"] += len(gewertet)
    # Spieler
    schlaeger = 0
    for adr, tipps in (picks or {}).items():
        tipps = tipps or {}
        mein = [g for g in gewertet if tipps.get(g["slug"])]
        if not mein:
            continue
        pts = hits = beats = 0
        for g in mein:
            if tipps[g["slug"]] == g["result"]:
                hits += 1
                if g.get("engine") != g["result"]:
                    beats += 1
                    pts += P_SCHLAEGT
                else:
                    pts += P_RICHTIG
        perfekt = len(gewertet) >= STAEDTE and hits == len(gewertet)
        if perfekt:
            pts += P_ALLE
        if beats:
            schlaeger += 1
        for z in (wo.setdefault(adr, _leer()), alle.setdefault(adr, _leer())):
            z["pts"] += pts
            z["hits"] += hits
            z["beats"] += beats
            z["picks"] += len(mein)
            z["days"] += 1
            z["perfect"] += 1 if perfekt else 0
        s = serie.get(adr) or {"n": 0, "last": None}
        try:
            vorher = (datetime.fromisoformat(tag).date() - timedelta(days=1)).isoformat()
        except Exception:
            vorher = None
        s["n"] = s["n"] + 1 if s.get("last") == vorher else 1
        s["last"] = tag
        serie[adr] = s
    stand.setdefault("days", {})[tag] = {
        "engine_hits": eh, "n": len(gewertet), "players": len(picks or {}),
        "beat_it": schlaeger,
        "results": [{"city": g.get("city"), "result": g.get("result"),
                     "engine": g.get("engine")} for g in gs],
    }
    # Nur die letzten 12 Wochen und 30 Tage behalten.
    for k in sorted(wochen)[:-12]:
        wochen.pop(k, None)
    for k in sorted(eng["weeks"])[:-12]:
        eng["weeks"].pop(k, None)
    for k in sorted(stand["days"])[:-30]:
        stand["days"].pop(k, None)
    _speichern(KV_STAND, stand)
    log.info(f"beat: {tag} gewertet, engine {eh}/{len(gewertet)}, "
             f"{len(picks or {})} Spieler, {schlaeger} schlugen die Engine")
    try:
        import feed
        import features
        if features.an("beat") and gewertet:
            txt = (f"Beat the Engine · {tag} scored. Engine {eh}/{len(gewertet)}. "
                   + (f"{schlaeger} holder{'s' if schlaeger != 1 else ''} beat it."
                      if schlaeger else "Nobody beat it this time."))
            feed.add(txt, "result")
    except Exception:
        pass


# ── web: lesen, tippen, Namen ────────────────────────────────────────────

def namen():
    return _laden(KV_NAMEN)


def name_von(adresse, ns=None):
    ns = ns if ns is not None else namen()
    return ns.get(adresse) or kurz(adresse)


NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


def name_setzen(adresse, name):
    name = str(name or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("3 to 16 letters, digits or _")
    with _sperre:
        ns = namen()
        for a, n in ns.items():
            if a != adresse and n.lower() == name.lower():
                raise ValueError("that name is taken")
        ns[adresse] = name
        if not _speichern(KV_NAMEN, ns):
            raise RuntimeError("names need the database")
    return name


def tippen(adresse, slug, label):
    d = _laden(KV_SPIELE)
    g = d.get("games", {}).get(slug)
    if not g:
        raise ValueError("this city is not in today's game")
    if not offen(g):
        raise ValueError("picks for this city are closed")
    if label not in [b["label"] for b in g.get("brackets") or []]:
        raise ValueError("unknown bracket")
    k = "beat_picks_" + str(g["date"])
    with _sperre:
        picks = _laden(k)
        mein = picks.setdefault(adresse, {})
        mein[slug] = label
        if not _speichern(k, picks):
            raise RuntimeError("picks need the database")
    return {"slug": slug, "pick": label}


def _tabelle(z, ns, n=20):
    xs = sorted(z.items(), key=lambda kv: (-kv[1]["pts"], -kv[1]["beats"], -kv[1]["hits"]))
    return [{"rank": i + 1, "name": name_von(a, ns), "pts": v["pts"], "beats": v["beats"],
             "hits": v["hits"], "picks": v["picks"], "perfect": v.get("perfect", 0)}
            for i, (a, v) in enumerate(xs[:n])]


def _platz(z, adresse):
    xs = sorted(z.items(), key=lambda kv: (-kv[1]["pts"], -kv[1]["beats"], -kv[1]["hits"]))
    for i, (a, v) in enumerate(xs):
        if a == adresse:
            return dict(v, rank=i + 1, of=len(xs))
    return None


def lage(adresse=None):
    """Alles fuer die App: Spiele, eigene Tipps, Wertung."""
    d = _laden(KV_SPIELE)
    spiele = d.get("games", {})
    stand = _laden(KV_STAND)
    ns = namen()
    jetzt = _jetzt()
    ab = (jetzt - timedelta(days=2)).date().isoformat()
    xs = [g for g in spiele.values() if str(g.get("date")) >= ab]
    xs.sort(key=lambda g: (str(g.get("date")), str(g.get("lock_at"))))
    picks_cache = {}

    def mein_tipp(g):
        if not adresse:
            return None
        k = str(g.get("date"))
        if k not in picks_cache:
            picks_cache[k] = _laden("beat_picks_" + k)
        return (picks_cache[k].get(adresse) or {}).get(g["slug"])

    def zaehlen(g):
        k = str(g.get("date"))
        if k not in picks_cache:
            picks_cache[k] = _laden("beat_picks_" + k)
        return sum(1 for t in picks_cache[k].values() if (t or {}).get(g["slug"]))

    aus = []
    for g in xs:
        ist_offen = offen(g, jetzt)
        zustand = ("open" if ist_offen else "scored" if g.get("scored")
                   else "settling" if g.get("result") else "locked")
        aus.append({
            "slug": g["slug"], "city": g.get("city"), "date": g.get("date"),
            "station": g.get("station"), "unit": g.get("unit"),
            "lock_at": g.get("lock_at"), "state": zustand,
            "brackets": g.get("brackets") or [],
            "engine": None if ist_offen else g.get("engine"),
            "result": g.get("result") if g.get("result") != "void" else None,
            "void": g.get("result") == "void",
            "your_pick": mein_tipp(g), "players": zaehlen(g),
        })
    w = woche(jetzt.date().isoformat())
    wo = stand.get("weeks", {}).get(w, {})
    alle = stand.get("all", {})
    eng = stand.get("engine", {})
    ich = None
    if adresse:
        ich = {"name": ns.get(adresse), "short": kurz(adresse),
               "week": _platz(wo, adresse), "all": _platz(alle, adresse),
               "streak": (stand.get("streak", {}).get(adresse) or {}).get("n", 0)}
    tage = stand.get("days", {})
    letzte = sorted(tage.items(), reverse=True)[:7]
    return {
        "games": aus, "week": w,
        "board_week": _tabelle(wo, ns), "board_all": _tabelle(alle, ns),
        "engine_week": eng.get("weeks", {}).get(w, {"hits": 0, "n": 0}),
        "engine_all": eng.get("all", {"hits": 0, "n": 0}),
        "days": [dict(v, date=k) for k, v in letzte],
        "you": ich,
        "rules": {"hit": P_RICHTIG, "beat": P_SCHLAEGT, "all_three": P_ALLE,
                  "cities": STAEDTE, "lock_h": LOCK_H},
    }
