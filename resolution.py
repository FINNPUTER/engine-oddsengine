"""
Resolution: score what we published.

The gap this fills was embarrassing. The worker published signals and stored
them, and then nothing ever looked again. `signals_resolved` was a hardcoded
zero in the API, `outcome_known` was a field the dashboard filtered on that
nothing ever set, and the calculator waited forever for numbers that had no
producer.

Every claim on the site rests on "we publish before and score after". This is
the "after".

APPEND-ONLY, SO NO EDITS
------------------------
Snapshots are never modified, spec §12. A resolution is therefore a NEW record
pointing back at the signal, not a rewrite of it. The API joins them. That way
what we said stays exactly as it was said, forever, next to what happened.

TRUTH
-----
Gamma's resolved flag and outcome, nothing else. Never a price threshold: a
bracket can trade at 0.97 and still lose, and the old bot's CHANGELOG has the
scar tissue to prove it.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import requests

import storage
from config import cfg
from logger import log

GAMMA = "https://gamma-api.polymarket.com"

_sess = requests.Session()
_sess.headers["User-Agent"] = "oddsengine/2.3"


def _loads(v, d):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return d
    return v if v is not None else d


def winning_bucket(event_slug):
    """
    (winning_label, resolved) for an event.

    resolved=False means it has not settled yet, which is not an error.
    """
    try:
        r = _sess.get(f"{GAMMA}/events", params={"slug": event_slug}, timeout=25)
        r.raise_for_status()
        d = r.json()
        ev = d[0] if isinstance(d, list) and d else d
        if not ev or not ev.get("closed"):
            return None, False
        for m in ev.get("markets", []):
            prices = _loads(m.get("outcomePrices"), [])
            # Gamma reports the settled outcome. Index 0 is YES.
            if prices and float(prices[0]) > 0.5:
                return m.get("groupItemTitle"), True
        return None, False        # closed but no winner reported yet
    except Exception as e:
        log.debug(f"resolve {event_slug}: {e}")
        return None, False


def run(max_check=40):
    """
    Score every published signal whose day is over and which we have not
    scored yet. Cheap: one Gamma call per unscored signal, and each is only
    ever asked once.
    """
    all_rows = storage.load_all()

    # Seit 2.14 kommt das X-Signal spaeter (Warteschlange). Seine ID steht
    # dann im public_post-Eintrag, nicht im Signal selbst.
    x_ids = {r.get("slug"): r.get("x_id") for r in all_rows
             if r.get("kind") == "public_post" and r.get("x_id")}

    # Wo steht das Signal in Telegram? Je Chat die JUENGSTE Nachricht, denn
    # gewertet wird die letzte Fassung. Das Ergebnis kommt als Antwort genau
    # darunter, statt lose im Kanal zu stehen.
    tg_refs = {}
    for r in all_rows:
        refs = r.get("tg_refs") or []
        if not refs or not (r.get("kind") == "public_post"
                            or (r.get("signal") and r.get("posted"))):
            continue
        for ref in refs:
            try:
                chat, mid = str(ref[0]), int(ref[1])
            except Exception:
                continue
            d = tg_refs.setdefault(r.get("slug"), {})
            alt = d.get(chat)
            if alt is None or str(r.get("ts")) >= alt[0]:
                d[chat] = (str(r.get("ts")), mid)

    published, done = [], set()
    for s in all_rows:
        if s.get("kind") == "resolution":
            done.add(s.get("slug"))
        elif s.get("signal") and s.get("posted"):
            published.append(s)

    now = datetime.now(timezone.utc)
    todo = []
    for s in published:
        if s.get("slug") in done:
            continue
        try:
            # Give the station time to report before asking.
            ts = datetime.fromisoformat(s["ts"])
            lead = float(s.get("lead_h") or 0)
            if now < ts + timedelta(hours=lead + 6):
                continue
        except Exception:
            pass
        todo.append(s)

    # Ein Markt kann mehrfach veroeffentlicht worden sein: als Aktualisierung,
    # wenn sich der Favorit verschoben hat oder der Preis gefallen ist. Fuer
    # den Record zaehlt er trotzdem EINMAL, und zwar mit der zuletzt gesagten
    # Fassung — die hat die vorherige ersetzt. Ohne das hier stuende jeder
    # aktualisierte Markt mehrfach in der Bilanz und ein Treffer wuerde
    # doppelt zaehlen.
    alle_sig = list(todo)
    je_markt = {}
    erste = {}
    for s in todo:
        k = s.get("slug")
        alt = je_markt.get(k)
        if alt is None or (s.get("ts") or "") > (alt.get("ts") or ""):
            je_markt[k] = s
        # Die ERSTE Fassung getrennt merken. Sie ist die, auf die ein Abonnent
        # reagieren konnte, als er das Signal zum ersten Mal las -- und sie kann
        # anders ausgehen als die letzte. Bei Istanbul am 27.07. sagten 16
        # Fassungen ueber 34 Stunden 30/31/32, erst drei Stunden vor Schluss
        # kippte es auf 29/30/31. Der Tag wurde 32: die erste Ansage traf, die
        # letzte nicht. Wer nur die letzte wertet, schreibt einem Abonnenten
        # einen Fehlschlag zu, den er gar nicht hatte.
        a = erste.get(k)
        if a is None or (s.get("ts") or "") < (a.get("ts") or ""):
            erste[k] = s
    if len(je_markt) < len(todo):
        log.info(f"resolve: {len(todo) - len(je_markt)} Mehrfachveroeffentlichungen "
                 f"zusammengefasst")
    todo = sorted(je_markt.values(), key=lambda x: x.get("ts") or "")

    if not todo:
        return 0

    log.info(f"resolving {min(len(todo), max_check)} of {len(todo)} unscored signals")
    n = 0
    for s in todo[:max_check]:
        if s.get("slug") in done:
            continue
        label, resolved = winning_bucket(s.get("event_slug") or s.get("slug"))
        if not resolved:
            continue
        done.add(s.get("slug"))

        legs = s.get("legs") or []
        hit = label in legs
        payout = s.get("payout", 0) or 0
        budget = s.get("budget", cfg.budget) or cfg.budget
        # Die ehrliche Rendite rechnet mit der Aufteilung, die im Signal STAND,
        # nicht mit einer gedachten. Polymarket nimmt keine Order unter einem
        # Dollar an, deshalb bekommen billige Randbeine mehr Geld als ihnen
        # rechnerisch zusteht. Trifft ausgerechnet so ein Bein, faellt die
        # Auszahlung viel hoeher aus als "1 geteilt durch Blockpreis".
        # Bei Seoul am 23.07. waren das 55,56 auf 5,76 statt der glatten 165 %.
        # Derselbe Einsatz wie im Beispiel der Signalnachricht, sonst stimmt
        # der Vergleich nie: dort stehen 5 Dollar, hier stand bisher 10.
        beispiel = cfg.example_bankroll * cfg.stake_pct / 100.0
        asks = s.get("asks") or []
        kosten = sum(a for a in asks if a) if asks else None
        pnl_pct = None
        if kosten and len(asks) == len(legs) and beispiel:
            einsaetze = [max(a / kosten * beispiel, cfg.min_order_usd)
                         for a in asks]
            gesamt = sum(einsaetze)
            if hit and label in legs:
                i = legs.index(label)
                zurueck = einsaetze[i] / asks[i] if asks[i] else 0.0
            else:
                zurueck = 0.0
            pnl_pct = (zurueck / gesamt - 1) * 100 if gesamt else -100.0
            payout = round(zurueck, 2)
            budget = round(gesamt, 2)
        if pnl_pct is None:
            pnl_pct = ((payout / budget - 1) * 100) if hit and budget else -100.0

        storage.save({
            "kind": "resolution",
            "ts": now.isoformat(),
            "slug": s.get("slug"),
            "city": s.get("city"),
            "date": s.get("date"),
            "station": s.get("station"),
            "signal": False,
            "outcome_known": True,
            "outcome_label": label,
            "outcome_hit": hit,
            # Copied so a scorer never has to join back to say what we claimed.
            "said_coverage": s.get("coverage"),
            "said_coverage_raw": s.get("coverage_raw"),
            "haircut_pp": s.get("haircut_pp"),
            "grade": s.get("grade"),
            "trust_k": s.get("trust_k"),
            "said_coverage_engine": s.get("coverage_engine"),
            "lead_h": s.get("lead_h"),
            "metar_in_block": s.get("metar_in_block"),
            "said_legs": legs,
            "first_legs": (erste.get(s.get("slug")) or {}).get("legs"),
            "first_ts": (erste.get(s.get("slug")) or {}).get("ts"),
            "first_hit": ((label in ((erste.get(s.get("slug")) or {}).get("legs") or []))
                          if label else None),
            "versions": sum(1 for x in alle_sig if x.get("slug") == s.get("slug")),
            # Ohne die Einzelpreise laesst sich die Rendite spaeter nicht
            # nachrechnen. Sie entscheidet ueber die Aufteilung und damit
            # darueber, wieviel auf dem Gewinnerbein lag.
            "said_asks": s.get("asks"),
            "cost": s.get("cost"),
            "edge_pp": s.get("edge_pp"),
            "pnl_pct": round(pnl_pct, 2),
            "payout": round(payout, 2) if payout else 0.0,
            "budget": round(budget, 2) if budget else None,
            "published_ts": s.get("ts"),
            # Stand der Call oeffentlich (Kanal oder X)? Dann kam das Ergebnis
            # schon als Antwort darunter und gehoert nicht in die Tagesbilanz.
            "public": bool(s.get("free") or s.get("x_id") or x_ids.get(s.get("slug"))
                           or str(cfg.tg_public or "") in (tg_refs.get(s.get("slug")) or {})
                           or _holder_einzeln()),
        })
        n += 1
        try:
            import feed
            feed.add(f"Scored {feed.stadt(s.get('city'))}: {'HIT' if hit else 'MISS'}, "
                     f"the high came in at {label}.", "result", stadt=s.get("city"))
        except Exception:
            pass
        # Seit 2.17.4: ein Ergebnis steht NUR dort, wo auch der Call stand,
        # als Antwort direkt darunter. Calls, die nur in der App waren, kommen
        # einmal am Tag gesammelt in die Tagesbilanz (tagesbilanz unten).
        # So sieht niemand ein Ergebnis zu einem Call, den er nie gesehen hat,
        # und trotzdem wird jedes Ergebnis veroeffentlicht.
        bil = bilanz_ab_regeln()
        res_info = {
            "city": s.get("city"), "date": s.get("date"),
            "said_legs": legs, "cost": s.get("cost"),
            "published_ts": s.get("ts"), "free": s.get("free"),
            "outcome_label": label, "outcome_hit": hit}
        try:
            import telegram
            text = telegram.x_result(res_info, bil, link=True)
            for chat, (_, mid) in (tg_refs.get(s.get("slug")) or {}).items():
                telegram.send(text, chat=chat, reply_to=mid)
        except Exception as e:
            log.debug(f"result post: {e}")
        try:
            import telegram
            import x_post
            ziel = s.get("x_id") or x_ids.get(s.get("slug"))
            if ziel:
                xtext = telegram.x_result(res_info, bil)
                xid = None
                if x_post.bereit():
                    # Seit 2.17.5 als Zitat-Post, nicht als Antwort (X_RESULT_AS=reply schaltet zurueck).
                    if os.getenv("X_RESULT_AS", "quote").lower() == "reply":
                        xid = x_post.post(xtext, reply_to=ziel)
                    else:
                        xid = x_post.post(xtext, quote=ziel) or x_post.post(xtext, reply_to=ziel)
                if not xid:
                    telegram.send_x(xtext)
            elif _holder_einzeln():
                # Seit 2.25: Ergebnis eines Holder-Calls, einzeln, Treffer wie
                # Fehlschlag. Der Call wird erst nach dem Ergebnis oeffentlich.
                xtext = telegram.x_result(res_info, bil, holder=True)
                xid = x_post.post(xtext) if x_post.bereit() else None
                if not xid:
                    telegram.send_x(xtext)
                kanal = str(cfg.tg_public or "")
                if kanal and kanal not in (tg_refs.get(s.get("slug")) or {}):
                    telegram.send(telegram.x_result(res_info, bil, link=True, holder=True), chat=kanal)
        except Exception as e:
            log.debug(f"x result: {e}")
        log.info(f"  {s.get('city'):<14} {s.get('date')}  said {legs} "
                 f"({(s.get('coverage') or 0):.0%})  ->  {label}  "
                 f"{'HIT' if hit else 'MISS'}")

    if n:
        log.info(f"scored {n} signals")
    return n


def _holder_einzeln():
    """Seit 2.25: Ergebnisse von Holder-Calls einzeln auf X und im Kanal
    statt gesammelt in der Tagesbilanz. X_HOLDER_RESULTS=false schaltet zurueck."""
    return os.getenv("X_HOLDER_RESULTS", "true").lower() not in ("0", "false", "no", "off")


def bilanz_ab_regeln():
    """Signale seit RULES_SINCE: Anzahl, Treffer, Ergebnis bei gleichem Einsatz."""
    seit = cfg.rules_since
    n = hits = 0
    roi = 0.0
    gesehen = set()
    for x in storage.load_all():
        if x.get("kind") != "resolution" or not x.get("outcome_known"):
            continue
        if str(x.get("published_ts") or "")[:10] < seit or x.get("slug") in gesehen:
            continue
        c = x.get("cost")
        if not isinstance(c, (int, float)) or c <= 0:
            continue
        gesehen.add(x.get("slug"))
        n += 1
        if x.get("outcome_hit"):
            hits += 1
            roi += 1 / c - 1
        else:
            roi -= 1
    return {"n": n, "hits": hits, "roi": (roi / n * 100) if n else 0.0}


def stats():
    """
    What the API and the calculator need. Empty until signals actually resolve,
    which is the honest state and not a failure.
    """
    rows = [r for r in storage.load_all() if r.get("kind") == "resolution"]
    if not rows:
        return {"signals_resolved": 0, "hit_rate": None, "avg_cost": None,
                "mean_abs_error_pp": None, "longest_losing_streak": None,
                "net_pct": None}

    rows.sort(key=lambda r: r.get("published_ts") or r.get("ts") or "")
    hits = [bool(r.get("outcome_hit")) for r in rows]
    said = [r.get("said_coverage") for r in rows if r.get("said_coverage") is not None]
    cost = [r.get("cost") for r in rows if r.get("cost")]

    streak = worst = 0
    for h in hits:
        streak = 0 if h else streak + 1
        worst = max(worst, streak)

    hit_rate = sum(hits) / len(hits)
    avg_said = sum(said) / len(said) if said else None
    avg_cost = sum(cost) / len(cost) if cost else None

    # Die Rendite auf DIESELBE Weise wie Seite und Kanal: aus dem echten
    # Einsatz und der echten Auszahlung je Signal, die die 1-Dollar-
    # Mindestorder enthalten. Die alte Formel hit_rate/avg_cost unterstellte
    # eine perfekte Aufteilung und wich stark ab, sobald ein billiges Bein
    # traf. Tragen nicht alle Zeilen budget, faellt sie auf den
    # gleichgewichteten Mittelwert der Einzelrenditen zurueck.
    voll = [r for r in rows if r.get("budget") and r.get("payout") is not None]
    if len(voll) == len(rows) and rows:
        stake = sum(r["budget"] for r in rows)
        back = sum(r["payout"] for r in rows)
        net_pct = (back / stake - 1) * 100 if stake else None
    else:
        pnl = [r.get("pnl_pct") for r in rows if r.get("pnl_pct") is not None]
        net_pct = sum(pnl) / len(pnl) if pnl else None

    return {
        "signals_resolved": len(rows),
        "hit_rate": round(hit_rate, 4),
        "avg_said": round(avg_said, 4) if avg_said else None,
        "avg_cost": round(avg_cost, 4) if avg_cost else None,
        # The number the whole site rests on: we said X, X happened.
        "mean_abs_error_pp": round(abs(hit_rate - avg_said) * 100, 2) if avg_said else None,
        "longest_losing_streak": worst,
        "net_pct": round(net_pct, 2) if net_pct is not None else None,
    }


def _recap_text(rows, bilanz, link=False, platz=280):
    import telegram
    rows = sorted(rows, key=lambda r: (str(r.get("date")), str(r.get("city"))))
    hits = sum(1 for r in rows if r.get("outcome_hit"))
    kopf = ["📊 App calls settled since yesterday", ""]
    fuss = ["", f"{len(rows)} call{'s' if len(rows) != 1 else ''} · {hits} hit{'s' if hits != 1 else ''}",
            "Every call goes live in the app before the market closes. Every result lands here."]
    if bilanz and bilanz.get("n"):
        fuss += ["", f"Since Sep 25: {bilanz['n']} calls · {bilanz['hits']} hits "
                     f"({bilanz['hits'] * 100 // bilanz['n']}%)"]
    if link:
        fuss += ["", "oddsengine.live"]
    zeilen = [f"{'✅' if r.get('outcome_hit') else '❌'} {str(r.get('city') or '').upper()} "
              f"{telegram._tag(r.get('date'))}" for r in rows]
    # Auf X ist bei 280 Zeichen Schluss: lieber "+N more" als abgeschnitten.
    for k in range(len(zeilen), -1, -1):
        mitte = zeilen[:k] + ([f"+{len(zeilen) - k} more in the app"] if k < len(zeilen) else [])
        text = telegram._ohne_striche("\n".join(kopf + mitte + fuss))
        if link or len(text) <= platz:
            return text
    return text


def tagesbilanz(dry=False):
    """Einmal am Tag ab RECAP_HOUR_UTC: alle Ergebnisse seit der letzten
    Bilanz, deren Call NICHT oeffentlich stand, gesammelt in einem Post.
    Auf X ohne Link (kostet sonst 0,20 $), im Telegram-Kanal mit Link."""
    now = datetime.now(timezone.utc)
    stunde = int(os.getenv("RECAP_HOUR_UTC", "12"))
    heute = now.date().isoformat()
    if now.hour < stunde or storage.kv_get("recap_day") == heute:
        return 0
    seit = storage.kv_get("recap_ts")
    if not seit:
        # Erster Lauf nach dem Update: die alten Ergebnisse standen schon
        # einzeln draussen. Ab jetzt sammeln, erste Bilanz am naechsten Tag.
        storage.kv_set("recap_ts", now.isoformat())
        storage.kv_set("recap_day", heute)
        log.info("tagesbilanz: ab jetzt wird gesammelt, erste Bilanz morgen")
        return 0
    rows, gesehen = [], set()
    for r in storage.load_all():
        if (r.get("kind") != "resolution" or not r.get("outcome_known")
                or r.get("public") or str(r.get("ts") or "") <= seit
                or r.get("slug") in gesehen):
            continue
        # Alte Eintraege vor 2.17.4 haben kein "public": nur ab Regelstart zaehlen.
        if str(r.get("published_ts") or "")[:10] < cfg.rules_since:
            continue
        gesehen.add(r.get("slug"))
        rows.append(r)
    if dry:
        return len(rows)
    storage.kv_set("recap_day", heute)
    storage.kv_set("recap_ts", now.isoformat())
    if not rows:
        log.info("tagesbilanz: keine App-Calls abgerechnet, nichts gepostet")
        return 0
    bil = bilanz_ab_regeln()
    try:
        import telegram
        kanal = str(cfg.tg_public or cfg.chat_id() or "")
        if kanal:
            telegram.send(_recap_text(rows, bil, link=True), chat=kanal)
    except Exception as e:
        log.debug(f"tagesbilanz telegram: {e}")
    try:
        import telegram
        import x_post
        xtext = _recap_text(rows, bil)
        xid = x_post.post(xtext) if x_post.bereit() else None
        if not xid:
            telegram.send_x(xtext)
    except Exception as e:
        log.debug(f"tagesbilanz x: {e}")
    log.info(f"tagesbilanz: {len(rows)} App-Calls gesammelt gepostet")
    return len(rows)
