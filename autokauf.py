#!/usr/bin/env python3
"""autokauf.py — kauft selbst, nach Regeln die aus den eigenen Zahlen kommen.

    python3 autokauf.py --stake 5 --dry            einmal durchsehen, nichts kaufen
    python3 autokauf.py --stake 5                  einmal durchsehen und kaufen
    caffeinate -i python3 autokauf.py --stake 5 --dauer 20     laeuft durch

Vorher:
    export POLY_PRIVATE_KEY=0x...
    export POLY_FUNDER_ADDRESS=0xe8f31cabd6f045f7e1fc3c3781f925a9f1a75659

WAS SICH GEGENUEBER DER ERSTEN FASSUNG GEAENDERT HAT -- und warum

1. GEFILTERT WIRD AUF DEN SCHLECHTESTEN FALL, NICHT AUF DEN ERWARTUNGSWERT.
   beine.py hat gemessen: das billigste Bein trifft in 6 % der Bloecke, die
   Engine gibt ihm 18-24 %. Jede Kennzahl, die diese Wahrscheinlichkeiten
   benutzt -- edge_pp, ev_pct, auch mein eigener "echter EV" -- bevorzugt
   deshalb genau die Signale, die am staerksten ueberschaetzt sind. Der
   schlechteste Fall dagegen ist reine Preisrechnung: was kommt zurueck, wenn
   das TEUERSTE Bein trifft. Diese Zahl stimmt unabhaengig von jeder Prognose.

2. ER ZIEHT NACH, WENN DIE ENGINE IHREN BLOCK VERSCHIEBT.
   Am 27. und 28. gingen fuenf Positionen verloren, weil die Engine den Block
   nach oben verschob und die alte Fassung im Depot blieb: Guangzhou, Qingdao,
   Tokyo, Seoul, Chengdu. Alle fuenf in dieselbe Richtung -- es wurde waermer.
   Der Kaeufer prueft deshalb bei jedem Durchgang, ob das aktuelle Signal
   Felder enthaelt, die im Depot fehlen, und kauft die einzeln nach.

3. KEINE WEISSE LISTE MEHR.
   Am 27. hat der Nutzer 9 von 16 Signalen gesetzt; von den 7 ausgelassenen
   trafen 6. Die Auswahl war das Problem, nicht die Staedte.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

# Schluessel aus schluessel.sh laden, egal wo die Datei liegt.
try:
    import schluessel  # noqa: F401
except ImportError:
    pass

API = "https://web-production-6b61c.up.railway.app"
DATA = "https://data-api.polymarket.com"
EOA = "0xe8f31cabd6f045f7e1fc3c3781f925a9f1a75659"
PROXY = "0x21368F7BA724F8cB222937EEA69d8c283ECFdd0c"
MERKE = "autokauf.json"
MON = {1:"jan",2:"feb",3:"mar",4:"apr",5:"may",6:"jun",
       7:"jul",8:"aug",9:"sep",10:"oct",11:"nov",12:"dec"}


def jetzt():
    return datetime.now().strftime("%H:%M")


def get(u):
    k = os.getenv("ODDSENGINE_KEY", "").strip()
    if k:
        u += ("&" if "?" in u else "?") + "key=" + k
    r = subprocess.run(["curl", "-s", "--max-time", "60", u],
                       capture_output=True, text=True)
    try:
        return json.loads(r.stdout) if r.stdout.strip() else {}
    except ValueError:
        return {}


def merke_lesen():
    try:
        with open(MERKE) as f:
            d = json.load(f)
    except Exception:
        d = {}
    heute = datetime.now().strftime("%Y-%m-%d")
    if d.get("tag") != heute:
        d = {"tag": heute, "gesetzt": [], "beine": [], "summe": 0.0}
    for k, v in (("gesetzt", []), ("beine", []), ("summe", 0.0)):
        d.setdefault(k, v)
    return d


def merke_schreiben(d):
    try:
        with open(MERKE, "w") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass


def norm(bein):
    """Beinbezeichnung vereinheitlichen.

    Im Markttitel steht "between 86-87F", im Signal steht "86-87F". Ohne
    Angleichung findet der Vergleich das Bein nie wieder, haelt es fuer
    fehlend und kauft es erneut -- bei Denver am 28.07. viermal denselben
    Block. Betrifft alle Staedte mit Fahrenheit-Baendern.
    """
    t = str(bein or "").strip().lower()
    t = t.replace("between ", "").replace("°", "").replace(" ", "")
    return t


def depot():
    """Was ich halte: {stadt|datum: set(beine)}"""
    da = {}
    for adr in (EOA, PROXY):
        for p in get(DATA + "/positions?user=" + adr + "&sizeThreshold=0") or []:
            if float(p.get("size") or 0) <= 0:
                continue
            m = re.search(r"temperature in (.+?) be (.+?) on ([A-Za-z]+)\s+(\d+)",
                          str(p.get("title") or ""), re.I)
            if not m:
                continue
            mo = {v: k for k, v in MON.items()}.get(m.group(3)[:3].lower())
            if not mo:
                continue
            k = "{}|2026-{:02d}-{:02d}".format(m.group(1).strip().lower(),
                                               mo, int(m.group(4)))
            da.setdefault(k, set()).add(norm(m.group(2)))
    return da


def schlechtester(asks, stake, mindest=1.0):
    """Was kommt zurueck, wenn das teuerste Bein trifft -- in Prozent.

    Reine Preisrechnung, unabhaengig von jeder Wahrscheinlichkeit. Das ist der
    Grund, warum hier danach gefiltert wird und nicht nach edge oder EV.
    """
    k = sum(asks or [])
    if not k or any(x <= 0 for x in asks):
        return None, None
    roh = [stake * a / k for a in asks]
    echt = [max(r, mindest) for r in roh]
    aus = sum(echt)
    zahlt = [e / a for e, a in zip(echt, asks)]
    return round((min(zahlt) / aus - 1) * 100), round(aus, 2)


def place(args, eingabe=None, sek=240):
    return subprocess.run([sys.executable, "place.py"] + args, input=eingabe,
                          capture_output=True, text=True, timeout=sek)


def kaufe_block(stadt, datum, stake, a):
    basis = ["--city", stadt, "--date", datum, "--stake", str(stake)]
    p = place(basis + ["--dry"])
    aus = p.stdout + p.stderr
    g = re.search(r"Gesamteinsatz\s+\$([\d.]+)", aus)
    b = re.search(r"Blockpreis\s+([\d.]+)c", aus)
    if not g or not b:
        return None, "kein handelbarer Block"
    betrag, preis = float(g.group(1)), float(b.group(1)) / 100.0
    s = re.search(r"schlechtester\s+\$([\d.]+)", aus)
    if not s:
        return None, "schlechtester Fall nicht lesbar"
    schlecht = round((float(s.group(1)) / betrag - 1) * 100)
    if preis > a.max_preis:
        return None, "Block {:.0f}c > {:.0f}c".format(preis * 100, a.max_preis * 100)
    # Billige Bloecke NICHT aussperren, sondern kleiner kaufen.
    #
    # Ueber 54 Bloecke trafen die unter 60 Cent nur zu 33 % und kosteten
    # -34,2 %. Das ist ein Hinweis, kein Beweis -- die abgelehnte Gruppe war
    # zwoelf Bloecke gross. Wer sie ganz aussperrt, sammelt nie wieder Daten
    # darueber und kann die Regel nie pruefen; sie bestaetigt sich dann nur
    # selbst. Ein Drittel Einsatz begrenzt den Schaden und haelt die Messung
    # am Laufen.
    if preis < a.min_preis:
        klein = round(stake * a.klein_anteil, 2)
        if klein < 3.0:
            return None, "Block {:.0f}c < {:.0f}c, Restbetrag zu klein".format(
                preis * 100, a.min_preis * 100)
        p = place(["--city", stadt, "--date", datum, "--stake", str(klein), "--dry"])
        aus = p.stdout + p.stderr
        g = re.search(r"Gesamteinsatz\s+\$([\d.]+)", aus)
        if not g:
            return None, "Block {:.0f}c, kleiner Versuch nicht handelbar".format(preis * 100)
        betrag = float(g.group(1))
        if a.dry:
            return {"betrag": betrag, "schlecht": schlecht, "klein": True,
                    "dry": True}, None
        p2 = place(["--city", stadt, "--date", datum, "--stake", str(klein), "--ja"])
        ok = "Beinen gesendet" in (p2.stdout + p2.stderr)
        return ({"betrag": betrag, "schlecht": schlecht, "klein": True,
                 "ok": ok}, None) if ok \
            else (None, "Block {:.0f}c, kleiner Kauf fehlgeschlagen".format(preis * 100))
    if betrag > a.max_markt:
        return None, "Einsatz ${:.2f} > ${:.2f} je Markt".format(betrag, a.max_markt)
    # Ein Feld ueber der Grenze macht den ganzen Block unattraktiv: bei 98 Cent
    # bleiben zwei Prozent, und das Geld liegt fest bis zur Aufloesung.
    teuerstes = 0.0
    for m in re.finditer(r"^\s+\S.*?\s+(\d+(?:\.\d+)?)c\s", aus, re.M):
        teuerstes = max(teuerstes, float(m.group(1)) / 100.0)
    if teuerstes > a.max_bein:
        return None, "Feld zu {:.0f}c > {:.0f}c (kaum Ertrag)".format(
            teuerstes * 100, a.max_bein * 100)
    if schlecht < a.min_schlecht:
        return None, "schlechtester Fall {:+d}% < {:+d}%".format(schlecht, a.min_schlecht)
    if a.dry:
        return {"betrag": betrag, "schlecht": schlecht, "dry": True}, None
    # Mit --ja rechnet place.py selbst nach und setzt den aktuellen Betrag.
    # Vorher tippte der Kaeufer den Betrag aus dem Trockenlauf ein; bewegte
    # sich der Preis dazwischen, stimmte die Eingabe nicht und nichts wurde
    # gesendet -- am 28.07. vier Stunden lang, ohne dass es auffiel.
    p2 = place(basis + ["--ja"])
    aus2 = p2.stdout + p2.stderr
    # place.py schreibt "0 von 3 Beinen gesendet." auch dann, wenn KEIN Bein
    # durchging. Der frühere Test auf die blosse Zeichenkette meldete deshalb
    # GEKAUFT, waehrend alle Orders abgelehnt wurden -- am 28.07. viermal, ohne
    # dass auf der Kette etwas geschah. Jetzt zaehlt die Zahl davor.
    m = re.search(r"(\d+)\s+von\s+(\d+)\s+Beinen gesendet", aus2)
    if not m or int(m.group(1)) == 0:
        grund = "keine Order angenommen"
        f = re.search(r"ABGELEHNT\s+(.{0,90})", aus2)
        e = re.search(r"FEHLER\s+(.{0,90})", aus2)
        if f:
            grund = "abgelehnt: " + f.group(1).strip()
        elif e:
            grund = "Fehler: " + e.group(1).strip()
        return None, grund
    teil = int(m.group(1)) < int(m.group(2))
    # GESENDET ist nicht GEKAUFT. place.py meldet, dass die Orders beim
    # Handelsplatz angekommen sind -- ob jemand dagegen handelt, entscheidet
    # sich danach. Am 28.07. stand viermal GEKAUFT im Protokoll, waehrend auf
    # der Kette nichts geschah. Deshalb kurz warten und im Depot nachsehen.
    gefuellt = None
    try:
        time.sleep(6)
        k = stadt.lower() + "|" + str(datum)
        gefuellt = k in depot()
    except Exception:
        pass
    return {"betrag": betrag, "schlecht": schlecht, "ok": True,
            "teil": teil, "gefuellt": gefuellt}, None


def kaufe_bein(stadt, datum, bein, usd, dry):
    args = [sys.executable, "einzelkauf.py", "--city", stadt, "--date", datum,
            "--leg", bein, "--usd", str(usd)]
    if dry:
        args.append("--dry")
        r = subprocess.run(args, capture_output=True, text=True, timeout=180)
        return "KAUF" in r.stdout, r.stdout.strip()[-200:]
    r = subprocess.run(args, input="{:.2f}\n".format(max(usd, 1.03)),
                       capture_output=True, text=True, timeout=180)
    aus = r.stdout + r.stderr
    return "GESENDET" in aus, aus.strip()[-200:]


def guthaben():
    """Freies pUSD. Ohne Deckung laufen Orders ins Leere und der Kaeufer
    wuerde stundenlang gegen eine leere Wallet arbeiten."""
    key = os.getenv("POLY_PRIVATE_KEY", "").strip()
    if not key:
        return None
    try:
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        sig = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
        c = ClobClient("https://clob.polymarket.com", chain_id=137, key=key,
                       signature_type=sig,
                       funder=os.getenv("POLY_FUNDER_ADDRESS", EOA))
        c.set_api_creds(c.create_or_derive_api_key())
        p = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL,
                                   signature_type=sig)
        try:
            c.update_balance_allowance(p)
        except Exception:
            pass
        r = c.get_balance_allowance(p)
        return float(r.get("balance", 0)) / 1e6 if isinstance(r, dict) else None
    except Exception:
        return None


def einloesen(a):
    """Gewonnenes holen, bevor gekauft wird.

    Der Kaeufer gibt aus, holt aber nichts zurueck -- ueber Nacht steht er
    sonst vor einer leeren Wallet, waehrend vierzig oder sechzig Dollar zum
    Einloesen bereitliegen. Deshalb vor jedem Durchgang einmal nachsehen.
    """
    if a.dry or not a.einloesen:
        return
    if not os.path.exists("redeem.py"):
        return
    try:
        p = subprocess.run([sys.executable, "redeem.py", "--dry"],
                           capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return
    m = re.search(r"SUMME einloesbar\s+([\d.]+)", p.stdout)
    if not m or float(m.group(1)) < a.min_einloesen:
        return
    betrag = float(m.group(1))
    print("  {}  {:.2f} einloesbar -- hole sie".format(jetzt(), betrag))
    try:
        r = subprocess.run([sys.executable, "redeem.py", "--ja"],
                           capture_output=True, text=True, timeout=900)
        n = len(re.findall(r"\bOK\b|erfolgreich|bestaetigt", r.stdout))
        print("  {}  eingeloest ({} Transaktion(en))".format(jetzt(), n or "?"))
    except subprocess.TimeoutExpired:
        print("  {}  Einloesen dauerte zu lange -- weiter".format(jetzt()))


def durchgang(a):
    d = merke_lesen()
    einloesen(a)
    if not a.dry:
        g = guthaben()
        if g is not None:
            print("  {}  Guthaben {:.2f}".format(jetzt(), g))
            if g < max(a.boden, a.stake * 1.2):
                print("  {}  zu wenig Guthaben -- warte. redeem.py laeuft?"
                      .format(jetzt()))
                return
    if d["summe"] >= a.tageslimit:
        print("  {}  Tageslimit {:.2f} erreicht.".format(jetzt(), a.tageslimit))
        return

    hab = depot()
    sig = get(API + "/v1/signals?hours=12").get("signals") or []
    neu = {}
    for s in sorted(sig, key=lambda x: x.get("ts") or ""):
        # Seit 2.20.1: nur echte Wetten. Ein READ hat nach der eigenen
        # Bilanz keinen Vorsprung vor dem Preis.
        if s.get("posted") and s.get("legs") and s.get("grade", "bet") == "bet":
            neu[(str(s.get("city") or "").lower(), s.get("date"))] = s

    # ── 1. Nachziehen: fehlende Beine in Maerkten, die ich schon halte ──
    if a.nachziehen:
        for (stadt, datum), s in neu.items():
            k = stadt + "|" + str(datum)
            if k not in hab:
                continue
            # Fehlende Beine ergaenzen. Wichtig: ein Markt gilt NICHT als
            # erledigt, nur weil irgendein Bein daraus im Depot liegt.
            # Verschiebt die Engine den Block, hielt der Kaeufer den Markt
            # sonst fuer gesetzt und liess die neuen Felder liegen -- am
            # 29.07. blieben so Manila, Warsaw und Helsinki ungekauft,
            # obwohl sie in luecken.py als kaufbar standen.
            fehlt = [l for l in (s.get("legs") or []) if norm(l) not in hab[k]]
            alle = s.get("legs") or []
            # Fehlt MEHR ALS DIE HAELFTE, ist es kein Nachziehen mehr, sondern
            # ein neuer Block. Einzelne Beine zu 1,50 zu stueckeln ergibt dann
            # eine schiefe Position statt einer gleichmaessigen Aufteilung --
            # am 29.07. blieben Manila, Warsaw und Helsinki so haengen, weil
            # der Markt als "gehalten" galt und nur getroepfelt wurde.
            if alle and len(fehlt) > len(alle) / 2.0:
                if k in d["gesetzt"]:
                    continue
                r, grund = kaufe_block(stadt, str(datum), a.stake, a)
                print("  {}  {:<13}{}  Block verschoben, neu gekauft  {}".format(
                    jetzt(), stadt.title()[:13], datum,
                    grund or ("$%.2f" % r["betrag"] if r else "")))
                if r and not a.dry:
                    d["gesetzt"].append(k)
                    d["summe"] += r["betrag"]
                    merke_schreiben(d)
                continue
            for bein in fehlt:
                mk = k + "|" + str(bein)
                if mk in d["beine"] or (s.get("lead_h") or 0) <= 0:
                    continue
                if a.bein_usd > a.max_markt:
                    continue
                ok, txt = kaufe_bein(stadt, str(datum), str(bein), a.bein_usd, a.dry)
                print("  {}  {:<13}{}  Bein {:<10} {}".format(
                    jetzt(), stadt.title()[:13], datum, str(bein)[:10],
                    ("wuerde nachziehen" if a.dry else
                     ("Bein gesendet" if ok else "fehlgeschlagen"))))
                if ok and not a.dry:
                    d["beine"].append(mk)
                    d["summe"] += a.bein_usd
                    merke_schreiben(d)

    # ── 2. Neue Bloecke ────────────────────────────────────────────────
    gekauft = 0
    kandidaten = [(st, dt, s) for (st, dt), s in neu.items()
                  if (st + "|" + str(dt)) not in hab
                  and (st + "|" + str(dt)) not in d["gesetzt"]
                  and (s.get("lead_h") or 0) > 0]

    def schluessel(x):
        sc = schlechtester(x[2].get("asks") or [], a.stake)[0]
        return -(sc if sc is not None else -999)
    kandidaten.sort(key=schluessel)

    for stadt, datum, s in kandidaten:
        if gekauft >= a.max_blocks:
            print("  {}  {} Bloecke je Durchgang erreicht.".format(jetzt(), a.max_blocks))
            break
        if d["summe"] + a.stake > a.tageslimit:
            print("  {}  Tageslimit erreicht.".format(jetzt()))
            break
        try:
            r, grund = kaufe_block(stadt, str(datum), a.stake, a)
        except subprocess.TimeoutExpired:
            r, grund = None, "Zeitueberschreitung"
        if r is None:
            print("  {}  {:<13}{}  gelassen: {}".format(
                jetzt(), stadt.title()[:13], datum, grund))
            continue
        print("  {}  {:<13}{}  schlechtester {:+d}%  Einsatz ${:.2f}  {}{}".format(
            jetzt(), stadt.title()[:13], datum, r["schlecht"], r["betrag"],
            "wuerde kaufen" if a.dry else
            ("GEKAUFT" + (" (nur teilweise)" if r.get("teil") else "")
             if r.get("gefuellt") else
             ("GESENDET, noch nicht gefuellt" if r.get("gefuellt") is False
              else "GESENDET")),
            "  (klein, Block unter {:.0f}c)".format(a.min_preis * 100)
            if r.get("klein") else ""))
        if not a.dry:
            d["gesetzt"].append(stadt + "|" + str(datum))
            d["summe"] += r["betrag"]
            merke_schreiben(d)
            gekauft += 1

    if not kandidaten:
        print("  {}  nichts Neues.".format(jetzt()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stake", type=float, required=True)
    ap.add_argument("--dry", action="store_true")
    # regeltest.py ueber 48 aufgeloeste Bloecke: alles kaufen ergab 75 %
    # Treffer und +2,4 %. Die Filterregel liess nur 10 durch, davon trafen 50 %,
    # Ergebnis -12,3 %. Aussortiert wurden 38 Bloecke mit 82 % Treffern und
    # +6,3 %. Jede Auswahl war bisher schlechter als keine -- die des Nutzers
    # (56 %) wie meine (50 %). Deshalb stehen die Filter jetzt offen und sind
    # nur noch da, falls eine spaetere Messung eine echte Regel findet.
    ap.add_argument("--min-schlecht", type=int, default=-999, dest="min_schlecht",
                    help="schlechtester Fall in Prozent (aus: -999)")
    ap.add_argument("--max-preis", type=float, default=1.0, dest="max_preis",
                    help="hoechster Blockpreis (aus: 1.0)")
    # Ueber 54 aufgeloeste Bloecke: ab 60 Cent trafen 36 von 42 (86 %) und
    # brachten +14,8 %. Unter 60 Cent trafen 4 von 12 (33 %) und kosteten
    # -34,2 %. Billige Bloecke sind nicht guenstig, sie sind unwahrscheinlich --
    # der Markt hat dort recht, und die Engine ueberschaetzt genau diese Raender.
    # Bei 70 Cent faellt das Ergebnis wieder auf +5,7 %, weil zu viele gute
    # Bloecke wegfallen. 60 ist die Schwelle, nicht "je hoeher desto besser".
    ap.add_argument("--min-preis", type=float, default=0.60, dest="min_preis",
                    help="niedrigster Blockpreis (Standard 0.60, aus: 0)")
    # Zwei harte Obergrenzen. Am 28.07. gingen 44,95 $ in ein Feld zu 98,5
    # Cent -- das zahlt 45,63 zurueck, also anderthalb Prozent, und bindet ein
    # Fuenftel des Kapitals bis zur Aufloesung. Wie der Betrag zustande kam,
    # liess sich nicht klaeren; diese Grenzen greifen unabhaengig davon.
    ap.add_argument("--max-bein-preis", type=float, default=0.90,
                    dest="max_bein", help="kein Feld ueber diesem Preis kaufen")
    ap.add_argument("--max-je-markt", type=float, default=25.0,
                    dest="max_markt", help="Obergrenze je Markt und Tag")
    ap.add_argument("--klein-anteil", type=float, default=0.34, dest="klein_anteil",
                    help="Anteil des Einsatzes fuer Bloecke unter min-preis "
                         "(0 = gar nicht kaufen)")
    ap.add_argument("--max-blocks", type=int, default=6, dest="max_blocks")
    ap.add_argument("--tageslimit", type=float, default=60.0)
    ap.add_argument("--mindest-guthaben", type=float, default=6.0,
                    dest="boden", help="unter diesem Guthaben wird nicht gekauft")
    ap.add_argument("--bein-usd", type=float, default=1.5, dest="bein_usd",
                    help="Einsatz beim Nachziehen einzelner Beine")
    ap.add_argument("--kein-nachzug", action="store_false", dest="nachziehen")
    ap.add_argument("--kein-einloesen", action="store_false", dest="einloesen",
                    help="nicht automatisch einloesen")
    ap.add_argument("--min-einloesen", type=float, default=3.0,
                    dest="min_einloesen",
                    help="erst ab diesem Betrag einloesen (Standard 3)")
    ap.add_argument("--dauer", type=int, metavar="MINUTEN")
    a = ap.parse_args()

    for f in ("place.py", "einzelkauf.py"):
        if not os.path.exists(f):
            sys.exit("  {} fehlt in diesem Ordner.".format(f))
    if not a.dry and not os.getenv("POLY_PRIVATE_KEY", "").strip():
        sys.exit("  POLY_PRIVATE_KEY fehlt.")

    print("\n  AUTOKAUF" + ("  (Trockenlauf)" if a.dry else ""))
    if a.min_schlecht <= -999 and a.max_preis >= 1.0 and a.min_preis <= 0:
        print("  Kauft JEDES Signal, ohne Auswahl.")
    elif a.min_schlecht <= -999 and a.max_preis >= 1.0:
        print("  Kauft jeden Block. Ab {:.0f}c mit vollem Einsatz, darunter mit"
              .format(a.min_preis * 100))
        print("  {:.0f} % davon -- so bleibt die Messung fuer billige Bloecke"
              .format(a.klein_anteil * 100))
        print("  am Laufen, bei begrenztem Schaden.")
    else:
        print("  Kauft, wenn der schlechteste Fall >= {:+d}% und der Block <= {:.0f}c."
              .format(a.min_schlecht, a.max_preis * 100))
    print("  Zieht fehlende Beine mit ${:.2f} nach.".format(a.bein_usd)
          if a.nachziehen else "  Kein Nachziehen.")
    print("  ${:.2f} je Block · {} je Durchgang · ${:.2f} am Tag"
          .format(a.stake, a.max_blocks, a.tageslimit))
    print("  Löst vor jedem Durchgang ein, sobald ${:.2f} bereitliegen.\n"
          .format(a.min_einloesen) if a.einloesen else "  Kein Einlösen.\n")

    if not a.dauer:
        durchgang(a)
        return
    print("  Alle {} Minuten. Beenden mit Strg+C.\n".format(a.dauer))
    while True:
        try:
            durchgang(a)
            time.sleep(a.dauer * 60)
        except KeyboardInterrupt:
            print("\n  Beendet.\n")
            return
        except Exception as e:
            print("  {}  Durchgang fehlgeschlagen: {}".format(jetzt(), str(e)[:120]))
            time.sleep(60)


if __name__ == "__main__":
    main()
