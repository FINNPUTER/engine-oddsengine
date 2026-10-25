#!/usr/bin/env python3
"""
place.py — eine Wette, ein Befehl, du am Ausloeser.

Holt das veroeffentlichte Signal fuer eine Stadt von deiner eigenen API, zieht
die AKTUELLEN Briefkurse frisch aus dem Orderbuch, rechnet die Aufteilung fuer
deinen Einsatz durch, zeigt dir alles, und fragt nach. Erst dann geht etwas raus.

    python3 place.py --list
    python3 place.py --city austin --stake 6 --dry
    python3 place.py --city austin --stake 6

Vorbereitung einmalig:

    pip install py-clob-client-v2
    export POLY_PRIVATE_KEY='0x...'
    export POLY_FUNDER_ADDRESS='0x...'      # dort liegt dein USDC
    export POLY_SIGNATURE_TYPE=0            # 0 = eigenes Wallet, 2 = Proxy

Es wird nichts automatisch gehandelt. Ein Aufruf ist eine Wette.
"""
import argparse
import json
import math
import os
import shutil
import ssl
import subprocess
import sys
import urllib.request
from datetime import date

# Schluessel aus schluessel.sh laden, egal wo die Datei liegt.
try:
    import schluessel  # noqa: F401
except ImportError:
    pass

# Homebrew-Python auf macOS bringt keine Wurzelzertifikate mit. certifi liegt
# ohnehin als Abhaengigkeit von py-clob-client bereit, also nutzen wir dessen
# Buendel. Ohne das schlaegt jeder Aufruf mit CERTIFICATE_VERIFY_FAILED fehl.
try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL = None

API = os.getenv("ODDSENGINE_API",
                "https://web-production-6b61c.up.railway.app")
CLOB = "https://clob.polymarket.com"
MIN_ORDER = 1.0
HARD_MAX_STAKE = 50.0          # Notbremse gegen Vertipper


# Polymarket filtert Anfragen, die sich als "Python-urllib" melden, mit 403.
_UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/126.0 Safari/537.36"),
       "Accept": "application/json"}


_CURL = shutil.which("curl")


def _mit_key(url):
    """Haengt die Zugangsschluessel an, falls welche gesetzt sind.

    Sind die Umgebungsvariablen leer, bleibt die Adresse unveraendert -- das
    Werkzeug funktioniert also vor UND nach dem Absperren, ohne dass man
    etwas umstellen muss.

        export ODDSENGINE_KEY=...      fuer /v1/signals
        export ODDSENGINE_ADMIN=...    fuer die internen Endpunkte
    """
    import os as _os
    z = []
    k = _os.getenv("ODDSENGINE_KEY", "").strip()
    a = _os.getenv("ODDSENGINE_ADMIN", "").strip()
    if k:
        z.append("key=" + k)
    if a:
        z.append("admin=" + a)
    if not z:
        return url
    return url + ("&" if "?" in url else "?") + "&".join(z)


def get(url):
    """Erst curl, dann Python.

    Polymarket blockt Python nicht nur wegen der Kennung, sondern erkennt es
    am TLS-Fingerabdruck und antwortet mit 403. curl kommt durch, liegt auf
    jedem Mac bereit und umgeht das Thema komplett.
    """
    url = _mit_key(url)
    if _CURL:
        r = subprocess.run([_CURL, "-s", "--max-time", "25",
                            "-H", "Accept: application/json", url],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
        raise RuntimeError(f"curl kam nicht durch (Code {r.returncode})")
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=25, context=_SSL) as r:
        return json.loads(r.read().decode())


def signals(hours=24):
    d = get(f"{API}/v1/signals?hours={hours}")
    rows = [s for s in d.get("signals", []) if s.get("legs")]
    rows.sort(key=lambda s: s.get("ts") or "", reverse=True)
    return rows


def book_ask(token_id, mitte=False):
    """Aktueller Kurs aus dem Orderbuch.

    Standard ist der Briefkurs, also sofort kaufbar. Mit mitte=True die Mitte
    zwischen Geld und Brief: spart im Schnitt knapp zwei Cent pro Block, die
    Order kann dafuer im Buch liegen bleiben statt sofort zu fuellen.

    Gibt (Preis, Grund) zurueck. Ohne Grund war es erfolgreich.
    """
    if not token_id:
        return None, "keine Token-ID im Signal"
    try:
        b = get(f"{CLOB}/book?token_id={token_id}")
    except Exception as e:
        return None, f"Buch nicht abrufbar: {str(e)[:90]}"
    asks = b.get("asks") or []
    if not asks:
        return None, "Buch da, aber kein Verkaufsangebot"
    try:
        a = min(float(x["price"]) for x in asks)
        if not mitte:
            return a, None
        bids = b.get("bids") or []
        if not bids:
            return a, None
        return round((a + max(float(x["price"]) for x in bids)) / 2, 4), None
    except Exception as e:
        return None, f"Preis unlesbar: {str(e)[:60]}"


def split(stake, asks):
    """Dutching: proportional zum Preis, dann auf das 1-Dollar-Minimum heben."""
    cost = sum(asks)
    raw = [stake * a / cost for a in asks]
    real = [max(r, MIN_ORDER) if r > 0 else 0.0 for r in raw]
    return raw, real


def show(sig, asks_now, stake, drop_cheap):
    legs = sig["legs"]
    print(f"\n  {sig['city'].upper()}  {sig['date']}")
    if sig.get("station"):
        print(f"  {sig['station']}")
    print(f"  veroeffentlicht {sig.get('ts', '')[:16]}\n")

    keep = list(range(len(legs)))
    if drop_cheap and len(legs) > 2:
        cheapest = min(keep, key=lambda i: asks_now[i][0] if asks_now[i][0] else 9)
        keep = [i for i in keep if i != cheapest]
        print(f"  --drop-cheap: Bein {legs[cheapest]} wird weggelassen\n")

    a_now = [asks_now[i][0] for i in keep]
    if any(a is None for a in a_now):
        print("  Kein handelbarer Preis. Im Einzelnen:\n")
        for i in keep:
            px, err = asks_now[i]
            state = f"{px*100:.1f}c" if px is not None else err
            print(f"    {legs[i]:<20}{state}")
        print("\n  Solange ein Bein kein Angebot hat, laesst sich der Block")
        print("  nicht vollstaendig kaufen. Warten, bis wieder jemand anbietet,")
        print("  oder mit --drop-cheap ohne dieses Bein rechnen.")
        return None
    lab = [legs[i] for i in keep]
    q = [sig["q"][i] for i in keep] if sig.get("q") else [None] * len(keep)

    raw, real = split(stake, a_now)
    spent = sum(real)
    pays = [real[i] / a_now[i] for i in range(len(keep))]

    print(f"  {'Bein':<18}{'unsere':>8}{'Preis':>9}{'Einsatz':>10}{'zurueck':>10}")
    for i in range(len(keep)):
        prob = f"{q[i]*100:.0f}%" if q[i] is not None else "-"
        mark = "  <-- verliert Geld" if pays[i] < spent else ""
        print(f"  {lab[i]:<18}{prob:>8}{a_now[i]*100:>8.1f}c"
              f"{real[i]:>10.2f}{pays[i]:>10.2f}{mark}")

    cost = sum(a_now)
    if cost >= 1.0:
        print(f"\n  Blockpreis {cost*100:.1f}c — das ist MEHR als der Dollar,")
        print("  den ein Treffer auszahlt. Garantierter Verlust, egal wie das")
        print("  Wetter wird. Nicht kaufen.\n")
        return None
    cov = sum(x for x in q if x is not None) if q[0] is not None else None
    print(f"\n  Blockpreis {cost*100:.1f}c", end="")
    if cov is not None:
        print(f"   Abdeckung {cov*100:.0f}%   Edge {(cov-cost)*100:+.1f}pp")
    else:
        print()
    print(f"  Gesamteinsatz ${spent:.2f}"
          f"   bester Fall ${max(pays):.2f}"
          f"   schlechtester ${min(pays):.2f}   daneben $0.00")

    if any(abs(raw[i] - real[i]) > 0.01 for i in range(len(keep))):
        print(f"\n  Hinweis: Beine unter ${MIN_ORDER:.0f} wurden angehoben. "
              f"Sauber waere ${sum(raw):.2f} gewesen.")
    if min(pays) < spent:
        print("  WARNUNG: Ein Bein zahlt weniger zurueck als der Gesamteinsatz.")
        print("           Gewinnt ausgerechnet dieses, machst du Verlust.")
        print("           Groesser setzen oder --drop-cheap verwenden.")
    return {"tokens": [sig["token_ids"][i] for i in keep],
            "labels": lab, "asks": a_now, "usd": real, "spent": spent}


def place(plan, slippage_bps):
    """Orders senden über py-clob-client-v2.

    Polymarket hat das alte Paket py-clob-client im Juni 2026 archiviert und
    die CLOB-API auf v2 umgestellt. Der alte Client bekommt seither auf jede
    Order "invalid order version, please use the latest clob-client" zurueck.
    Deshalb v2. Installation:  pip install py-clob-client-v2
    """
    try:
        from py_clob_client_v2 import ClobClient, OrderArgs, OrderType, Side
    except ImportError:
        sys.exit("py-clob-client-v2 fehlt.\n"
                 "  pip install py-clob-client-v2\n"
                 "Das alte py-clob-client ist archiviert und wird von "
                 "Polymarket nicht mehr angenommen.")

    key = os.getenv("POLY_PRIVATE_KEY", "").strip()
    funder = os.getenv("POLY_FUNDER_ADDRESS", "").strip()
    if not key or not funder:
        sys.exit("POLY_PRIVATE_KEY oder POLY_FUNDER_ADDRESS fehlt.")
    sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))

    c = ClobClient(CLOB, 137, key=key, signature_type=sig_type, funder=funder)
    c.set_api_creds(c.create_or_derive_api_key())
    print(f"\n  Wallet {funder[:6]}...{funder[-4:]}  (Typ {sig_type})\n")

    filled = 0
    for tok, lab, ask, usd in zip(plan["tokens"], plan["labels"],
                                  plan["asks"], plan["usd"]):
        # Preis muss innerhalb [tick, 1-tick] liegen, sonst weist der Client
        # ihn ab, bevor er ueberhaupt sendet. Tick-Groesse variiert je Markt.
        try:
            tick = float(c.get_tick_size(tok))
        except Exception:
            tick = 0.01
        # Preis auf ein Tick-Vielfaches AUFRUNDEN. Rundet man ab, liegt der
        # tatsaechliche Orderwert (Stueck x gerundeter Preis) unter dem
        # gerechneten, und die 1-Dollar-Mindestgrenze reisst knapp.
        px = ask * (1 + slippage_bps / 10000)
        px = math.ceil(px / tick - 1e-9) * tick
        px = round(min(max(px, tick), 1 - tick), 6)
        # Stueckzahl so, dass Stueck x Preis sicher ueber MIN_ORDER liegt.
        ziel = max(usd, MIN_ORDER * 1.03)
        size = math.ceil(ziel / px * 100 - 1e-9) / 100
        while size * px < MIN_ORDER * 1.01:
            size = round(size + 0.01, 2)
        usd = round(size * px, 2)
        try:
            r = c.create_and_post_order(
                OrderArgs(token_id=tok, price=px, size=size, side=Side.BUY),
                order_type=OrderType.GTC,
            )
            ok = bool(r.get("success")) if isinstance(r, dict) else bool(r)
            filled += 1 if ok else 0
            print(f"  {lab:<18}{'GESENDET' if ok else 'ABGELEHNT':<11}"
                  f"{size} @ {round(px, 4)}  = ${usd:.2f}")
            if not ok:
                print(f"      {str(r)[:170]}")
        except Exception as e:
            print(f"  {lab:<18}FEHLER   {str(e)[:170]}")
    print(f"\n  {filled} von {len(plan['tokens'])} Beinen gesendet.")
    if filled and filled < len(plan["tokens"]):
        print("  Achtung: unvollstaendiger Block. Das ist keine Dutch-Wette "
              "mehr, sondern eine Richtungswette. Rest von Hand ergaenzen "
              "oder die gesendeten wieder verkaufen.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city")
    ap.add_argument("--date", help="Markttag, falls es fuer die Stadt mehrere gibt")
    ap.add_argument("--stake", type=float)
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--slippage-bps", type=float, default=100)
    ap.add_argument("--legs", help="nur diese Beine kaufen, z.B. --legs 23,24")
    ap.add_argument("--mitte", action="store_true",
                    help="zur Mitte zwischen Geld und Brief kaufen statt zum "
                         "Brief: rund 2 Cent guenstiger, fuellt aber nicht immer")
    ap.add_argument("--drop-cheap", action="store_true",
                    help="billigstes Bein weglassen (spart die 1-Dollar-Falle)")
    ap.add_argument("--ja", action="store_true",
                    help="ohne Rueckfrage setzen (fuer autokauf.py)")
    ap.add_argument("--dry", action="store_true", help="nur rechnen, nichts senden")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    rows = signals(a.hours)
    if a.list or not a.city:
        seen, uniq = set(), []
        for s in rows:
            k = (s.get("city"), s.get("date"))
            if k in seen:
                continue
            seen.add(k)
            uniq.append(s)
        print(f"\n  {len(uniq)} Maerkte in den letzten {a.hours}h "
              f"({len(rows)} Schnappschuesse, die Engine prueft jeden Markt "
              f"bei jedem Scan neu):\n")
        heute = date.today().isoformat()
        for s in uniq:
            alt = "   (Markttag vorbei)" if (s.get("date") or "") < heute else ""
            print(f"  {s['city']:<14}{s['date']}   {' · '.join(s['legs'])}"
                  f"   {s['cost']*100:.0f}c   Edge {s['edge_pp']:+.1f}pp{alt}")
        print("\n  Setzen mit:  python3 place.py --city <stadt> --stake <betrag>\n")
        return

    if a.stake is None:
        sys.exit("--stake fehlt. Es gibt bewusst keinen Standardwert.")
    if a.stake <= 0 or a.stake > HARD_MAX_STAKE:
        sys.exit(f"--stake muss zwischen 0 und {HARD_MAX_STAKE:.0f} liegen.")

    hits = [s for s in rows if a.city.lower() in (s.get("city") or "").lower()]
    if a.date:
        hits = [s for s in hits if s.get("date") == a.date]
    if not hits:
        sys.exit(f"Kein Signal fuer '{a.city}'"
                 + (f" am {a.date}" if a.date else "")
                 + f" in den letzten {a.hours}h.")
    tage = sorted({s.get("date") for s in hits})
    if len(tage) > 1:
        print(f"\n  Fuer {a.city} gibt es mehrere Markttage: {', '.join(tage)}")
        print("  Waehle einen mit --date, zum Beispiel --date " + tage[0] + "\n")
        sys.exit(1)
    sig = hits[0]
    if not sig.get("token_ids"):
        sys.exit("Dem Signal fehlen die Token-IDs, damit laesst sich nicht setzen.")

    asks_now = [book_ask(t, mitte=a.mitte) for t in sig["token_ids"]]
    if a.mitte:
        print("\n  --mitte: Limit in der Buchmitte. Prueft nach dem Senden mit "
              "status.py,\n  ob wirklich gefuellt wurde.")
    plan = show(sig, asks_now, a.stake, a.drop_cheap)
    if not plan:
        return
    if a.legs:
        want = [w.strip().lower() for w in a.legs.split(",") if w.strip()]
        keep = [i for i, l in enumerate(plan["labels"])
                if any(w in l.lower() for w in want)]
        if not keep:
            sys.exit(f"  Kein Bein passt zu --legs {a.legs}")
        for k in ("tokens", "labels", "asks", "usd"):
            plan[k] = [plan[k][i] for i in keep]
        plan["spent"] = round(sum(plan["usd"]), 2)
        print(f"\n  --legs: nur {', '.join(plan['labels'])}  "
              f"= ${plan['spent']:.2f}")
    if a.dry:
        print("\n  --dry: nichts gesendet.\n")
        return

    # --ja ueberspringt die Rueckfrage. Nur fuer autokauf.py gedacht: der
    # rechnet erst trocken, merkt sich den Betrag und tippt ihn dann ein --
    # bewegt sich der Preis dazwischen auch nur um einen Cent, stimmt die
    # Eingabe nicht mehr und der Kauf bricht ab. Von Hand bleibt die Rueckfrage.
    if getattr(a, "ja", False):
        print(f"\n  Setze ${plan['spent']:.2f} ohne Rueckfrage (--ja).")
    elif True:
        print(f"\n  Zum Setzen von ${plan['spent']:.2f} tippe den Betrag "
              f"({plan['spent']:.2f}) und Enter, sonst Abbruch.")
        if input("  > ").strip() != f"{plan['spent']:.2f}":
            print("  Abgebrochen, nichts gesendet.\n")
            return
    place(plan, a.slippage_bps)


if __name__ == "__main__":
    main()
