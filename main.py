"""
OddsEngine 2.0 — Worker

Scans every active Polymarket temperature market on an interval, evaluates
each one end to end, posts the ones that clear the gates, and writes every
evaluation (signal or not) to an append-only store.

    python main.py            # loop forever (Railway worker)
    python main.py --once     # single scan, then exit
    python main.py --dry      # scan, print, post nothing
"""
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import engine
import execution
import resolution
import storage
import telegram
import x_post
from config import cfg
from logger import log

# Bump on every change that alters behaviour. Printed at startup so a stale
# copy can never masquerade as a fresh one — that cost us two scans.
VERSION = "2.31.3  (2026-10-21  Papier-Test der Top-Staedte nur im Admin. Davor 2.31.2 MCP nur fuer $ODDS-Holder (MCP_MIN_HOLD), interne Kalibrierwerte nicht mehr im oeffentlichen Record. Davor 2.31.1 Polymarket-Wetterregeln auf METAR umgestellt: Station wieder aus ids= und (ICAO) gelesen, Denver jetzt KDEN. Davor 2.31.0 Agenten: Keys mit Tageslimit und Modell-Liste, Key per Wallet-Unterschrift, MCP unter /mcp, x402 mit $ODDS-Permit ueber eine Gas-Wallet, hinter den Schaltern agents und x402. Davor 2.30.0 Alle Modelle mit Rubriken und NEW, Holder-Staffel beim Aufschlag, Aufladen mit festem Betrag, Bild-Download. Davor 2.29.1 Chat wie ChatGPT: Verlauf gespeichert und verschluesselt, Bilder ansehen und erzeugen, einfache Preisuebersicht. Davor 2.29.0 OddsEngine AI: jedes Modell ueber einen Schluessel, bezahlt in $ODDS, Einzahlungen per Transfer erkannt, 8 % Aufschlag. Davor 2.27.2 Krypto-Engine v2: Chainlink-TWAP-Gewichte, Kopplung an den Markt, engine/late nur 15m/4h, sniper statt cheap, both heisst Gabagool. Davor 2.27.1 Schalter engine und ai fuer engine.oddsengine.live. Davor 2.27.0 Krypto-Engine als eigener Dienst crypto, Terminal-API /v1/crypto hinter dem Schalter crypto; Wetter-Worker unveraendert. Davor 2.26.0 Beat the Engine: taegliches Tippspiel fuer Holder gegen die Engine, hinter dem Schalter beat. Davor 2.25.2 Aktualisierung eines Gratis-Calls als Antwort unter den ersten Post statt zweitem Gratis-Post, Karte zeigt das Einzelfeld als Favorit. Davor 2.25.1 Stufen auch nach Tokenmenge (TIER_MODE, TIER_HOLDER_TOKENS), Feed-Zeile Staedte klarer. Davor 2.25.0 Ergebnisse von Holder-Calls einzeln auf X und im Kanal, Treffer wie Fehlschlag. Davor 2.24.0 Karte /v1/map fuer Landing und App. Davor 2.23.1 Engine Live feiner: Stationen erst ab 12 Messungen, Zeile ueber abgelehnte Maerkte. Davor 2.23 Engine Live, weitere Wallets per Transfer verknuepfen, Board ohne Gap-Werbung. Davor 2.22 Kalshi-Bilanz: Calls und Crowd-Picks auch zu Kalshi-Preisen gewertet. Davor 2.21 Schaltpult: Your Engine, Station Live, Engine vs Crowd und Kalshi fertig im Code, Freigabe per Knopf auf admin.html. Davor 2.20.1 Ehrliche Zahlen: Preis + k * (Modell - Preis), k gelernt aus allen Calls; Calls sind BET oder READ, READ ohne Einsatz, Agent und Autokauf nur BET. Davor 2.20.0 Your Engine: persoenlicher Agent je Holder in Telegram, Holder-Gruppe mit Einlass und Rauswurf, What I learned today. Davor 2.19.1 Telegram: Bot schweigt in Gruppen, /id zeigt die Chat-ID, neuer /start-Text. Davor 2026-10-13  $ODDS auf der Robinhood Chain: Holder-Check per EVM-Unterschrift, plus Kalibrierung aus 2.18)"

BANNER = r"""
+--------------------------------------------------+
|   OddsEngine 2.0  -  Weather Probability Engine  |
|   Dynamic discovery | Ensemble | Coverage-first  |
+--------------------------------------------------+
"""


def _bucket_reason(r):
    """Group reasons so the histogram is readable: numbers -> #."""
    r = re.sub(r"-?\d+(\.\d+)?", "#", r or "?")
    return r.split("(")[0].strip()


REGION = {
    "nyc": "NorthAmerica", "chicago": "NorthAmerica", "atlanta": "NorthAmerica",
    "miami": "NorthAmerica", "dallas": "NorthAmerica", "houston": "NorthAmerica",
    "austin": "NorthAmerica", "denver": "NorthAmerica", "los angeles": "NorthAmerica",
    "san francisco": "NorthAmerica", "seattle": "NorthAmerica",
    "toronto": "NorthAmerica", "mexico city": "NorthAmerica",
    "panama city": "LatinAmerica", "sao paulo": "LatinAmerica",
    "buenos aires": "LatinAmerica",
    "london": "Europe", "paris": "Europe", "munich": "Europe", "milan": "Europe",
    "madrid": "Europe", "warsaw": "Europe", "amsterdam": "Europe",
    "helsinki": "Europe", "moscow": "Europe", "istanbul": "Europe",
    "seoul": "Asia", "busan": "Asia", "tokyo": "Asia", "taipei": "Asia",
    "shanghai": "Asia", "beijing": "Asia", "guangzhou": "Asia",
    "shenzhen": "Asia", "chongqing": "Asia", "wuhan": "Asia",
    "qingdao": "Asia", "chengdu": "Asia", "singapore": "Asia",
    "kuala lumpur": "Asia", "manila": "Asia", "lucknow": "Asia",
    "jeddah": "MiddleEast", "tel aviv": "MiddleEast",
    "cape town": "Africa", "wellington": "Oceania",
    "karachi": "Asia", "ankara": "Europe", "hong kong": "Asia",
}
_MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


SCHREIBWEISE = {"nyc": "NYC", "sao paulo": "SaoPaulo",
                "kuala lumpur": "KualaLumpur", "tel aviv": "TelAviv",
                "los angeles": "LosAngeles", "san francisco": "SanFrancisco",
                "mexico city": "MexicoCity", "panama city": "PanamaCity",
                "buenos aires": "BuenosAires", "cape town": "CapeTown"}


def _tags(ev):
    """Anklickbare Filter unter jeder Nachricht."""
    stadt = (ev.get("city") or "").strip()
    t = ["#" + SCHREIBWEISE.get(stadt.lower(), stadt.title().replace(" ", ""))]
    d = (ev.get("date") or "").split("-")
    if len(d) == 3:
        t.append(f"#{_MON[int(d[1])-1]}{int(d[2])}")
    r = REGION.get(stadt.lower())
    if r:
        t.append("#" + r)
    return "  ".join(t)


def _offene_signale():
    """Was gerade laeuft — fuer die angeheftete Tafel."""
    from datetime import datetime, timezone
    heute = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    seen, out = set(), []
    for s in reversed(storage.load_all()):
        k = (s.get("city"), s.get("date"))
        if k in seen or not s.get("posted") or not s.get("legs"):
            continue
        if (s.get("date") or "") < heute:
            continue
        seen.add(k)
        out.append(s)
    out.sort(key=lambda s: (s.get("date") or "", s.get("lead_h") or 9e9))
    return out


# ── 2.14: Oeffentlichkeit verzoegert, Vorsprung gemessen ────────────────

def _blockpreis(snap, legs):
    """Was dieselben Beine in diesem Scan kosten. None, wenn eines fehlt."""
    preise = (snap or {}).get("market_prices") or {}
    werte = [preise.get(l) for l in (legs or [])]
    if not werte or any(not isinstance(w, (int, float)) for w in werte):
        return None
    return round(sum(werte), 4)


def _alter_min(ts):
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - t).total_seconds() / 60.0
    except Exception:
        return None


def _preisspur(jetzt):
    """Bei jedem Scan notieren, was die Bloecke der letzten vier Stunden jetzt
    kosten. Daraus rechnet /v1/headstart den Vorsprung der Holder. Laeuft
    unabhaengig von der Verzoegerung, misst also auch schon vor dem Launch."""
    neueste = {}
    for s in storage.load_all():
        if s.get("kind") or not (s.get("signal") and s.get("posted") and s.get("legs")):
            continue
        a = _alter_min(s.get("ts"))
        if a is None or a < 5 or a > 250:
            continue
        k = s.get("slug")
        if k and (k not in neueste or str(s.get("ts")) > str(neueste[k].get("ts"))):
            neueste[k] = s
    n = 0
    for k, s in neueste.items():
        jetzt_preis = _blockpreis(jetzt.get(k), s.get("legs"))
        if jetzt_preis is None or not isinstance(s.get("cost"), (int, float)):
            continue
        storage.save({"kind": "price_track", "slug": k, "city": s.get("city"),
                      "date": s.get("date"), "signal": False,
                      "ts": datetime.now(timezone.utc).isoformat(),
                      "signal_ts": s.get("ts"),
                      "age_min": round(_alter_min(s.get("ts")) or 0, 1),
                      "legs": s.get("legs"), "cost_signal": s.get("cost"),
                      "cost_now": jetzt_preis})
        n += 1
    if n:
        log.info(f"preisspur: {n} Bloecke nachgemessen")


_WS = "public_queue"


def _vormerken(snap, text, btns):
    roh = storage.kv_get(_WS)
    try:
        liste = json.loads(roh) if roh else []
    except Exception:
        liste = []
    liste.append({"slug": snap.get("slug"), "city": snap.get("city"),
                  "date": snap.get("date"), "ts": snap.get("ts"),
                  "legs": snap.get("legs"), "cost": snap.get("cost"),
                  "text": text, "buttons": [list(b) for b in (btns or [])],
                  "xtext": telegram.x_signal(snap)})
    ok = storage.kv_set(_WS, json.dumps(liste[-60:]))
    if ok:
        log.info(f"{snap.get('city')} {snap.get('date')}: in der App, "
                 f"Kanal und X in {cfg.public_delay_min} min")
    return ok


def _x_kuerzen(text, grenze=275):
    """X zaehlt Emojis doppelt. Lieber die Schlagwortzeile opfern als mitten
    im Satz abschneiden."""
    if len(text) <= grenze:
        return text
    zeilen = text.split("\n")
    zeilen = [z for z in zeilen if not z.strip().startswith("#")]
    text = "\n".join(zeilen).strip()
    return text[:grenze]


def _ist_gratis(snap):
    """Gratis, wenn dieser Markt heute schon gratis war (Aktualisierung)
    oder das Tageskontingent FREE_PER_DAY noch nicht voll ist."""
    heute = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    frei_heute = set()
    for s in storage.load_all():
        if s.get("kind") or not s.get("free"):
            continue
        if s.get("slug") == snap.get("slug"):
            # Seit 2.25.2: schon oeffentlich. Eine Aktualisierung ist KEIN
            # neues Gratis-Signal, sie geht als Antwort unter den ersten Post
            # (_update_senden). Vorher stand derselbe Markt zweimal als
            # "Free signal of the day" auf X.
            return False
        if str(s.get("ts") or "")[:10] == heute:
            frei_heute.add(s.get("slug"))
    return len(frei_heute) < max(0, cfg.free_per_day)


def _frei_vorher(slug):
    """Der erste oeffentliche Post zu diesem Markt, falls es einen gibt."""
    erst = None
    for s in storage.load_all():
        if s.get("kind") == "public_post" and s.get("slug") == slug:
            if erst is None or str(s.get("ts")) < str(erst.get("ts")):
                erst = s
    return erst


def _update_senden(vorher, snap, text, btns):
    """Aktualisierung eines schon oeffentlichen Calls: als Antwort darunter,
    in Telegram wie auf X. Kein neuer Post, kein zweites Gratis-Label.
    Gespeichert als public_update, damit das Ergebnis spaeter den ersten
    Post zitiert und nicht die Antwort."""
    alt = ", ".join(str(l) for l in (vorher.get("legs") or []))
    neu = ", ".join(str(l) for l in (snap.get("legs") or []))
    if alt == neu:
        log.info(f"update {snap.get('city')}: gleiche Felder, oeffentlich nichts Neues")
        return
    preis = snap.get("cost")
    p = f" at {preis * 100:.0f}c" if isinstance(preis, (int, float)) else ""
    zeile = f"🔄 Update on this call: block moved from {alt} to {neu}{p}."
    tg_ok = False
    try:
        refs = vorher.get("tg_refs") or []
        mid = refs[0][1] if refs and isinstance(refs[0], (list, tuple)) else None
        tg_ok = telegram.send(text + "\n\n" + zeile,
                              buttons=[tuple(b) for b in (btns or [])],
                              chat=cfg.tg_public or cfg.chat_id(), reply_to=mid)
    except Exception as e:
        log.debug(f"update tg: {e}")
    xid = None
    ziel = vorher.get("x_id")
    if ziel and x_post.bereit():
        xid = x_post.post(f"{zeile}\n\nResult goes under the first post when the station reports.",
                          reply_to=ziel)
    log.info(f"update {snap.get('city')}: telegram={'ok' if tg_ok else '-'} x={xid or '-'}")
    storage.save({"kind": "public_update", "slug": snap.get("slug"), "city": snap.get("city"),
                  "date": snap.get("date"), "signal": False,
                  "ts": datetime.now(timezone.utc).isoformat(),
                  "legs": snap.get("legs"), "cost": snap.get("cost"),
                  "x_id": xid, "reply_to": ziel})


def _oeffentlich_senden(e, snap_jetzt, gratis=False):
    c0 = e.get("cost")
    c1 = _blockpreis(snap_jetzt, e.get("legs"))
    alter = _alter_min(e.get("ts"))
    notiz = ""
    if gratis:
        notiz = ("🎁 Free signal of the day. $ODDS holders get every call live in the app."
                 if cfg.odds_mint else "🎁 Free signal of the day.")
    elif isinstance(c0, (int, float)) and alter is not None and alter >= 5:
        vorher = f"{c0 * 100:.0f}c"
        nun = f"{c1 * 100:.0f}c" if c1 is not None else "n/a"
        if cfg.odds_mint:
            notiz = (f"⏱ First out in the $ODDS app {alter:.0f} min ago at {vorher}. "
                     f"Block now {nun}.")
        else:
            notiz = f"⏱ Signal price {vorher}, {alter:.0f} min ago. Block now {nun}."
    tg_text = e.get("text") or ""
    if notiz:
        tg_text = tg_text + "\n\n" + notiz
    ok = telegram.send(tg_text, buttons=[tuple(b) for b in (e.get("buttons") or [])],
                       chat=cfg.tg_public or cfg.chat_id())
    tg_ref = telegram.letzte if ok else None
    xid = None
    xtext = e.get("xtext") or ""
    if xtext:
        if notiz:
            teile = xtext.split("\n\n")
            # Notiz vor die Schlagworte, damit sie beim Kuerzen bleibt.
            xtext = "\n\n".join(teile[:-1] + [notiz, teile[-1]]) if len(teile) > 1 \
                else xtext + "\n\n" + notiz
        xtext = _x_kuerzen(xtext)
        xid = x_post.post(xtext) if x_post.bereit() else None
        if not xid:
            telegram.send_x(xtext)
    log.info(f"oeffentlich {e.get('city')}: telegram={'ok' if ok else 'FEHLER'}  "
             f"x={'gepostet ' + str(xid) if xid else 'NICHT gepostet (' + x_post.status() + ')'}")
    storage.save({"kind": "public_post", "slug": e.get("slug"), "city": e.get("city"),
                  "date": e.get("date"), "signal": False,
                  "ts": datetime.now(timezone.utc).isoformat(),
                  "signal_ts": e.get("ts"), "legs": e.get("legs"),
                  "cost_signal": c0, "cost_public": c1,
                  "delay_min": round(alter or 0, 1),
                  "telegram_ok": bool(ok), "x_id": xid,
                  "tg_refs": [tg_ref] if tg_ref else []})
    return ok


def _warteschlange(jetzt):
    """Faellige Signale in Kanal und auf X bringen."""
    roh = storage.kv_get(_WS)
    if not roh:
        return
    try:
        liste = json.loads(roh)
    except Exception:
        storage.kv_set(_WS, "[]")
        return
    bleibt, raus = [], 0
    for e in liste:
        alter = _alter_min(e.get("ts"))
        if alter is None or alter > 24 * 60:
            continue                                   # verwaist, verwerfen
        if alter < cfg.public_delay_min:
            bleibt.append(e)
            continue
        snap_jetzt = jetzt.get(e.get("slug"))
        if snap_jetzt is None:
            # Markt nicht mehr gelistet, also zu. Ein Signal fuer einen
            # geschlossenen Markt hilft niemandem.
            log.info(f"warteschlange: {e.get('city')} {e.get('date')} "
                     f"verworfen, Markt geschlossen")
            continue
        _oeffentlich_senden(e, snap_jetzt)
        raus += 1
    storage.kv_set(_WS, json.dumps(bleibt))
    if raus:
        log.info(f"warteschlange: {raus} Signal(e) oeffentlich, {len(bleibt)} warten")


def scan(dry=False):
    # Seit 2.31.6: ohne Datenbank nichts posten. Ohne sie weiss der Worker nicht,
    # was schon draussen ist, und schickt alles noch einmal (Railway-Ausfall 22.10.2026).
    if not dry and cfg.database_url and not storage.kv_alive():
        log.warning("Datenbank nicht erreichbar: dieser Scan postet nichts")
        dry = True
    events = engine.discover()
    if not events:
        log.warning("no active temperature markets found")
        return 0, 0

    reasons = Counter()
    near_miss = []
    candidates = []
    jetzt = {}          # slug -> Schnappschuss dieses Scans, fuer Preisspur und Warteschlange

    # PASS 1 — evaluate everything. No posting, no cutting.
    #
    # The old loop walked events in date order and stopped at the cap, which
    # meant Toronto at +12pp lost its slot to Amsterdam simply because
    # Amsterdam came first. That is not selection, it is alphabetical luck.
    for ev in events:
        snap = engine.evaluate(ev)
        if not dry:
            # Seit 2.21: Crowd-Pick mitschreiben, unabhaengig vom Signal.
            try:
                import crowd
                crowd.merken(snap)
            except Exception as e:
                log.debug(f"crowd: {e}")
        if snap.get("slug"):
            jetzt[snap["slug"]] = snap
        if not snap["signal"]:
            reasons[_bucket_reason(snap.get("reason"))] += 1
            if dry:
                log.info(f"  no signal  {ev['city']:<15} {ev['date']}  {snap['reason']}")
            if "edge" in (snap.get("reason") or ""):
                near_miss.append((ev["city"], ev["date"], snap["reason"],
                                  snap.get("best_coverage"), snap.get("lead_h")))
            if snap.get("full_dist") is not None:
                storage.save(snap)
            continue
        candidates.append((ev, snap))

    # Seit 2.26: Beat the Engine. Runden waehlen, Engine-Tipp festschreiben.
    if not dry:
        try:
            import beat
            beat.runde(jetzt.values())
        except Exception as e:
            log.error(f"beat: {e}")

    # Seit 2.23: Engine Live. Was dieser Scan gesehen hat.
    if not dry:
        try:
            import feed
            alle_s = [s for s in jetzt.values() if s.get("full_dist")]
            st = [s for s in alle_s if s.get("metar_max_c") is not None]
            n_st = len(st)
            feed.add(f"Read {len(events)} markets in the {len({e.get('city') for e in events})} cities with a market open right now. "
                     f"{n_st} station{'s are' if n_st != 1 else ' is'} reporting from the ground "
                     f"for today's markets.", "scan")
            n_sig = len(candidates)
            feed.add(f"Checked every price against the forecast and its own record. "
                     f"Passed on {len(events) - n_sig} markets, most are priced about right."
                     + (f" {n_sig} worth a closer look." if n_sig else ""), "scan")
            # Nur Stationen, die heute schon eine Weile messen: kurz nach
            # Mitternacht ist "high so far" nichtssagend.
            tag = sorted([s for s in st if (s.get("metar_n") or 0) >= 12],
                         key=lambda s: -(s.get("metar_n") or 0))
            import random
            for s in random.sample(tag[:8], min(3, len(tag[:8]))):
                feed.add(f"{feed.stadt(s.get('city'))} · {s.get('station') or s.get('icao')} reads "
                         f"{feed.grad(s, s.get('metar_last_c'))}"
                         f"{' at ' + s['metar_last_at'] + ' local' if s.get('metar_last_at') else ''}, "
                         f"high so far {feed.grad(s, s.get('metar_max_c'))}.", "station",
                         stadt=s.get("city"))
        except Exception as e:
            log.debug(f"feed scan: {e}")

    # PASS 2 — best edge first, then the caps bite the weakest.
    # Seit 2.20.1: echte Wetten zuerst (nach ehrlichem Vorsprung), dann die
    # Einschaetzungen nach Preis. Ein hoher Preis heisst: der Markt ist sich
    # selbst sicher, und genau dort lag die Trefferquote am hoechsten.
    candidates.sort(key=lambda c: (c[1].get("grade") != "bet",
                                   -c[1]["edge_pp"], -(c[1].get("cost") or 0)))
    posted_today = storage.posted_today()

    signals = 0
    skipped = len(events) - len(candidates)
    for ev, snap in candidates:
        if posted_today + signals >= cfg.max_signals_per_day:
            snap["signal"] = False
            snap["reason"] = "daily signal cap reached"
            storage.save(snap)
            skipped += 1
            continue
        if storage.posted_recently(ev["slug"], cfg.repost_cooldown_h):
            # Innerhalb des Cooldowns nur erneut posten, wenn sich seit dem
            # letzten Mal etwas Wesentliches geaendert hat.
            alt = storage.last_posted(ev["slug"])
            grund = None
            if alt and storage.posted_recently(ev["slug"], cfg.repost_min_gap_h):
                grund = None                      # noch zu frisch, gar nicht pruefen
            elif alt:
                # Eine Aktualisierung ist nur dann eine, wenn die Wette sich
                # UEBERHAUPT NOCH LOHNT. Ein gefallener Preis kann daher
                # kommen, dass der Markt die Wette fuer schlechter haelt --
                # dann nachzuposten hiesse, einen Verlust nachzuschieben.
                # Kein Repost, wenn der Vorsprung nicht mehr ueber der
                # Schwelle liegt, egal wie sehr der Preis gefallen ist.
                # snap["signal"] ist genau dann True, wenn der Markt in
                # DIESEM Scan alle Tore bestanden hat, die Edge-Schwelle
                # eingeschlossen. Das ist die ehrlichste Pruefung: eine
                # Aktualisierung geht nur raus, wenn der Markt jetzt gerade
                # wieder ein gueltiges Signal waere.
                if not snap.get("signal"):
                    grund = None
                elif cfg.repost_on_shift and alt.get("legs") and snap.get("legs") \
                        and list(alt["legs"]) != list(snap["legs"]):
                    grund = (f"neuer Favorit: {' · '.join(alt['legs'])}"
                             f"  ->  {' · '.join(snap['legs'])}")
                elif snap.get("metar_in_block") and not alt.get("metar_in_block"):
                    grund = (f"Station misst bereits {snap.get('metar_max_c'):.1f}C "
                             f"im Block, Markt nimmt noch {snap['cost']*100:.0f}c")
                elif (alt.get("cost") is not None and snap.get("cost") is not None
                        and snap["cost"] <= cfg.repost_max_cost
                        and (alt["cost"] - snap["cost"]) * 100 >= cfg.repost_price_drop_c):
                    grund = (f"Preis gefallen: {alt['cost']*100:.0f}c"
                             f"  ->  {snap['cost']*100:.0f}c")
            if not grund:
                snap["reason"] = "cooldown"
                snap["posted"] = False
                storage.save(snap)
                skipped += 1
                continue
            snap["update"] = True
            snap["update_reason"] = grund
            log.info(f"{ev['city']} {ev['date']} AKTUALISIERUNG — {grund}")

        text = engine.render(snap)
        if snap.get("update"):
            text = ("\U0001F504 UPDATE — this market already had a signal today.\n"
                    f"   {snap.get('update_reason','')}\n"
                    "   The numbers below replace the earlier message.\n\n") + text
        btns = engine.links(snap)

        # Own wallet. Gated: off by default, needs a proven live record,
        # risk caps, kill switch. Never touches a subscriber's funds.
        if not dry:
            snap.update(execution.execute(snap))

        log.info(f"SIGNAL {ev['city']} {ev['date']} {snap.get('grade','bet').upper()} "
                 f"model={snap.get('edge_raw_pp', 0):+.1f}pp k={snap.get('trust_k')} "
                 f"edge={snap['edge_pp']:+.1f}pp "
                 f"cov={snap['coverage']:.0%} cost={snap['cost']*100:.1f}c")
        if dry:
            print("\n" + text + "\n")
            for t, u in btns:
                print(f"   [ {t} ]  {u}")
            print()
            snap["posted"] = False
        elif cfg.public_mode == "free":
            # Seit 2.15: das Signal steht in der App fuer alle Holder. Kanal
            # und X bekommen nur das Gratis-Signal des Tages.
            text += "\n\n" + _tags(ev)
            if cfg.paid_chat_id() and telegram.send_paid(text, buttons=btns):
                snap["tg_refs"] = [telegram.letzte] if telegram.letzte else []
            snap["posted"] = True
            vorher = _frei_vorher(snap.get("slug"))
            if vorher:
                snap["free"] = True
                _update_senden(vorher, snap, text, btns)
            gratis = False if vorher else _ist_gratis(snap)
            log.info(f"signal {ev['city']}: " + ("Gratis-Signal des Tages, geht in Kanal und auf X"
                     if gratis else "Aktualisierung, als Antwort unter den ersten Post" if vorher else "nur fuer Holder in der App, Gratis-Signal heute schon vergeben"))
            if gratis:
                snap["free"] = True
                _oeffentlich_senden({"text": text, "buttons": btns,
                                     "xtext": telegram.x_signal(snap),
                                     "slug": snap.get("slug"), "legs": snap.get("legs"),
                                     "cost": snap.get("cost"), "ts": snap.get("ts"),
                                     "city": snap.get("city"), "date": snap.get("date")},
                                    snap, gratis=True)
        elif cfg.public_mode == "delay" and cfg.public_delay_min > 0:
            # Seit 2.14: zuerst die App. Dort sehen es die Holder je nach
            # Stufe (api.py rechnet ab snap["ts"]). Kanal und X folgen nach
            # PUBLIC_DELAY_MIN ueber die Warteschlange unten.
            text += "\n\n" + _tags(ev)
            if cfg.paid_chat_id() and telegram.send_paid(text, buttons=btns):
                snap["tg_refs"] = [telegram.letzte] if telegram.letzte else []
            snap["posted"] = True
            snap["public_delay_min"] = cfg.public_delay_min
            if not _vormerken(snap, text, btns):
                # Ohne Datenbank keine Warteschlange: dann lieber sofort
                # posten als gar nicht.
                _oeffentlich_senden({"text": text, "buttons": btns,
                                     "xtext": telegram.x_signal(snap),
                                     "slug": snap.get("slug"), "legs": snap.get("legs"),
                                     "cost": snap.get("cost"), "ts": snap.get("ts"),
                                     "city": snap.get("city"), "date": snap.get("date")},
                                    snap)
        elif cfg.paywall_enabled and cfg.paid_chat_id():
            # The live signal is the product: subscribers only.
            # Schlagworte: Telegram macht daraus anklickbare Filter. Ein Tipp
            # auf #Seoul zeigt alle Seoul-Signale, #Jul24 alle eines Tages.
            # Das ist die Sortierung, die ein Kanal sonst nicht kann.
            text += "\n\n" + _tags(ev)
            snap["posted"] = telegram.send_paid(text, buttons=btns)
            if snap["posted"] and telegram.letzte:
                snap["tg_refs"] = [telegram.letzte]
        else:
            snap["posted"] = telegram.send(text, buttons=btns)
            if snap["posted"] and telegram.letzte:
                snap["tg_refs"] = [telegram.letzte]
        if not dry and snap.get("posted") and (cfg.public_mode == "instant"
                                               or (cfg.public_mode == "delay"
                                                   and cfg.public_delay_min <= 0)):
            xtext = telegram.x_signal(snap)
            xid = x_post.post(xtext) if x_post.bereit() else None
            if xid:
                snap["x_id"] = xid
            else:
                telegram.send_x(xtext)
        storage.save(snap)
        signals += 1
        # Seit 2.20: Your Engine. Jeder Holder bekommt den Call, wenn er zu
        # seinen Regeln passt, mit dem Split fuer seinen Einsatz.
        if not dry and snap.get("posted"):
            try:
                import agent
                agent.on_signal(snap)
            except Exception as e:
                log.warning(f"agent: {e}")
            try:
                import feed
                feed.add(f"{feed.stadt(snap.get('city'))} · new call for holders.", "call",
                         holder_text=(f"{feed.stadt(snap.get('city'))} · market "
                                      f"{snap.get('cost', 0)*100:.0f}%, model "
                                      f"{(snap.get('coverage_engine') or 0)*100:.0f}%, honest "
                                      f"{(snap.get('coverage') or 0)*100:.0f}% → "
                                      f"{str(snap.get('grade', 'bet')).upper()}"),
                         stadt=snap.get("city"))
            except Exception:
                pass
            # Seit 2.22: derselbe Call auch mit dem Kalshi-Preis festhalten.
            try:
                import kalshi_spur
                kalshi_spur.merken(snap, "engine")
            except Exception as e:
                log.debug(f"kalshi-spur: {e}")

    if not dry:
        try:
            _preisspur(jetzt)
        except Exception as e:
            log.warning(f"preisspur: {e}")
        try:
            if cfg.public_mode == "delay":
                _warteschlange(jetzt)
            elif storage.kv_get(_WS) not in (None, "", "[]"):
                # Aus dem alten Modell: wartende Signale NICHT mehr
                # veroeffentlichen, sie gehoeren jetzt den Holdern.
                storage.kv_set(_WS, "[]")
                log.info("warteschlange geleert (PUBLIC_MODE=free)")
        except Exception as e:
            log.warning(f"warteschlange: {e}")

    # The self-check. On a fair market, signals should be rare.
    if events and signals > len(events) * 0.4:
        log.warning("=" * 60)
        log.warning(f"{signals}/{len(events)} markets produced a signal (>40%).")
        log.warning("On a fair market that is implausible. Suspect DRESS_SIGMA_C")
        log.warning("too small or BUCKET_ROUNDING wrong. Do not trust these.")
        log.warning("=" * 60)

    log.info(f"scan done: {len(events)} markets | {signals} signals | {skipped} no-signal")

    # Score what we published. Without this the whole promise, publish before,
    # score after, has no code behind it: the API returns a hardcoded zero and
    # the calculator waits forever on numbers nobody produces.
    try:
        if not dry:
            resolution.run()
            resolution.tagesbilanz()
            try:
                import kalibrierung
                kalibrierung.lernen()
            except Exception as e:
                log.error(f"kalibrierung: {e}")
            try:
                import crowd
                crowd.tick()
            except Exception as e:
                log.error(f"crowd: {e}")
            try:
                import kalshi_engine
                kalshi_engine.tick()
            except Exception as e:
                log.error(f"kalshi_engine: {e}")
            try:
                import kalshi_spur
                kalshi_spur.tick()
            except Exception as e:
                log.error(f"kalshi-spur: {e}")
            try:
                import beat
                beat.tick()
            except Exception as e:
                log.error(f"beat tick: {e}")
            try:
                import tief
                tief.tick(events)
            except Exception as e:
                log.error(f"tief: {e}")
        st = resolution.stats()
        if st["signals_resolved"]:
            log.info(f"record: {st['signals_resolved']} scored | "
                     f"said {st['avg_said']:.0%} hit {st['hit_rate']:.0%} "
                     f"(off by {abs(st['mean_abs_error_pp']):.1f}) | "
                     f"net {st['net_pct']:+.1f}%")
            # The record post fires only when we cross a new 10-signal
            # milestone (10, 20, 30...), never on a thin count. Two looks dumb.
            every = cfg.record_post_every
            n = st["signals_resolved"]
            if (not dry and every > 0 and n >= every
                    and n // every > storage.last_record_n() // every):
                if telegram.send_record(st):
                    storage.save({"kind": "record_post", "slug": "record_post", "ts":
                                  datetime.now(timezone.utc).isoformat(),
                                  "signal": False,
                                  "n": n})
    except Exception as e:
        log.error(f"resolution failed: {e}")

    # Zero signals is a legitimate result. Zero signals with no idea why is a bug.
    log.info("why nothing fired:")
    for r, n in reasons.most_common():
        log.info(f"   {n:>4}x  {r}")

    if near_miss:
        log.info(f"closest calls ({len(near_miss)} reached the edge check):")
        for c, d, r, cov, lead in sorted(near_miss)[:12]:
            cv = f"cov={cov:.0%}" if cov else "cov=?"
            log.info(f"   {c:<15} {d}  lead={lead:>4.0f}h  {cv}  {r}")

    if not dry and cfg.tg_token:
        try:
            res = [r for r in storage.load_all()
                   if r.get("kind") == "resolution"]
            bilanz = None
            if res:
                treffer = sum(1 for r in res if r.get("outcome_hit"))
                bilanz = f"Record so far: {treffer} of {len(res)} hit"
            telegram.live_board(_offene_signale(), bilanz)
        except Exception as e:
            log.debug(f"live board: {e}")
        try:
            import agent
            agent.tick()
        except Exception as e:
            log.warning(f"agent tick: {e}")
    if not dry:
        try:
            import feed
            feed.flush()
        except Exception as e:
            log.debug(f"feed flush: {e}")

    return signals, skipped


def main():
    print(BANNER)
    log.info(f"version {VERSION}")
    errs = cfg.validate()
    if errs:
        for e in errs:
            log.error(f"config: {e}")
        sys.exit(1)

    log.info(f"signal_mode={cfg.signal_mode}  risk_mode={cfg.mode}")
    log.info(f"dress_sigma={cfg.dress_sigma_c}C  bucket_rounding={cfg.bucket_rounding}")
    log.info(f"wu: {'ON weight=' + str(cfg.wu_weight) if cfg.wu_enabled else 'OFF'}"
             f" (WU_ENABLED, reads the settlement source's own forecast)")
    log.info(f"gates: edge>={cfg.modes[cfg.mode][2]}pp depth>=${cfg.min_ask_depth_usd} "
             f"spread<={cfg.max_spread_bps}bps")
    if not cfg.is_live:
        log.info("OBSERVE MODE — measuring, not publishing. This is correct "
                 "until calibration.py says otherwise.")
    engine._load_city_params()
    ex = execution.status()
    log.info(f"execution: {'ON' if ex['can_trade'] else 'OFF'} — "
             f"{ex['reason'] or 'armed'} ({ex['scored_signals']}/{ex['needed']} scored)")
    log.info(f"paywall: {'ON' if cfg.paywall_enabled else 'OFF'}")
    log.info(f"oeffentlich: {cfg.public_mode} "
             f"({str(cfg.free_per_day) + ' Gratis-Signal(e) am Tag' if cfg.public_mode == 'free' else ''})"
             f"  |  $ODDS: {cfg.odds_mint or 'noch nicht live'}")
    log.info(f"x: {x_post.status()}")
    log.info(f"filter: block >= {cfg.min_field_cost*100:.0f}c, "
             f"<= {cfg.max_field_cost*100:.0f}c, Vorsprung < {cfg.max_edge_pp:.0f}pp, gesperrt: "
             f"{', '.join(sorted(cfg.blocked_cities)) or 'keine'}")
    if cfg.min_field_cost and cfg.max_field_cost <= cfg.min_field_cost:
        log.error("MIN_FIELD_COST >= MAX_FIELD_COST: so geht KEIN Signal raus. "
                  "MAX_FIELD_COST auf Railway hoeher setzen, z.B. 0.90.")

    once = "--once" in sys.argv
    dry = "--dry" in sys.argv

    while True:
        try:
            s, k = scan(dry=dry)
            # Only speaks when there is something to say.
            if not dry and cfg.tg_token:
                telegram.send_summary(s + k, s, k)
        except KeyboardInterrupt:
            log.info("stopped")
            return
        except Exception as e:
            log.error(f"scan failed: {e}", exc_info=True)

        if once or dry:
            return
        log.info(f"sleeping {cfg.scan_interval_min} min")
        time.sleep(cfg.scan_interval_min * 60)


if __name__ == "__main__":
    main()
