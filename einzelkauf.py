#!/usr/bin/env python3
"""einzelkauf.py — EIN einzelnes Temperaturfeld kaufen, nicht den ganzen Block.

    python3 einzelkauf.py --city qingdao --date 2026-07-27 --leg 34 --usd 2 --dry
    python3 einzelkauf.py --city qingdao --date 2026-07-27 --leg 34 --usd 2

    python3 einzelkauf.py --city qingdao --date 2026-07-27 --liste

WOFUER
place.py kauft immer den kompletten Dreierblock. Wenn ein Signal aber nur um
eine Stufe wandert und man zwei der drei Beine schon haelt, braucht man genau
das fehlende -- und place.py wuerde die vorhandenen doppelt kaufen.

WAS ES NICHT IST
Ein einzelnes Bein ist keine Dutch-Wette mehr, sondern eine Richtungswette:
trifft genau dieses Feld, zahlt es; jedes andere Ergebnis ist ein Totalverlust
auf diesen Betrag. Das Skript sagt vorher, wie hoch beides ausfaellt.

Preis, Slippage, Mindestorder und Tick-Rundung sind woertlich aus place.py
uebernommen -- dieselbe Mechanik, die dort erprobt ist.
"""

import argparse
import json
import math
import os
import subprocess
import sys

# Schluessel aus schluessel.sh laden, egal wo die Datei liegt.
try:
    import schluessel  # noqa: F401
except ImportError:
    pass

API = "https://web-production-6b61c.up.railway.app"
CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
MIN_ORDER = 1.0


def _mit_key(url):
    """Haengt die Zugangsschluessel an, falls welche gesetzt sind."""
    z = []
    k = os.getenv("ODDSENGINE_KEY", "").strip()
    a = os.getenv("ODDSENGINE_ADMIN", "").strip()
    if k:
        z.append("key=" + k)
    if a:
        z.append("admin=" + a)
    if not z:
        return url
    return url + ("&" if "?" in url else "?") + "&".join(z)


def get(url):
    url = _mit_key(url)
    r = subprocess.run(["curl", "-s", "--max-time", "45", url],
                       capture_output=True, text=True)
    try:
        return json.loads(r.stdout) if r.stdout.strip() else {}
    except ValueError:
        return {}


def signal_finden(stadt, datum):
    d = get("{}/v1/signals?hours=48".format(API))
    treffer = [s for s in (d.get("signals") or [])
               if str(s.get("city") or "").lower() == stadt.lower()
               and s.get("date") == datum and s.get("token_ids")]
    if not treffer:
        return None
    treffer.sort(key=lambda s: s.get("ts") or "", reverse=True)
    return treffer[0]


def brief(token_id):
    """Guenstigstes Verkaufsangebot im Buch."""
    b = get("{}/book?token_id={}".format(CLOB, token_id))
    asks = b.get("asks") or []
    if not asks:
        return None
    try:
        return min(float(x["price"]) for x in asks)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--leg", help="Temperaturstufe, z.B. 34")
    ap.add_argument("--usd", type=float, default=2.0, help="Einsatz in Dollar")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--liste", action="store_true", help="nur Beine zeigen")
    ap.add_argument("--slippage", type=int, default=100, help="in Basispunkten")
    a = ap.parse_args()

    sig = signal_finden(a.city, a.date)
    if not sig:
        sys.exit("  Kein Signal mit Token-IDs fuer {} am {}.".format(a.city, a.date))

    beine = list(zip(sig.get("legs") or [], sig.get("token_ids") or []))

    # Das Signal enthaelt nur die drei bis fuenf Felder des Blocks. Der Markt
    # hat aber mehr -- und genau die daneben sind interessant, wenn die Station
    # aus dem Block herauszulaufen droht. Deshalb den vollstaendigen Satz ueber
    # gamma nachladen und dazumischen.
    slug = sig.get("slug")
    if slug:
        d = get("{}/events?slug={}".format(GAMMA, slug))
        ev = d[0] if isinstance(d, list) and d else d
        vorhanden = {str(l).strip() for l, _ in beine}
        for m in ((ev or {}).get("markets") or []):
            lab = str(m.get("groupItemTitle") or "").strip()
            tok = m.get("clobTokenIds")
            if isinstance(tok, str):
                try:
                    tok = json.loads(tok)
                except ValueError:
                    tok = None
            if lab and tok and lab not in vorhanden:
                beine.append((lab, tok[0]))
                vorhanden.add(lab)

    if not beine:
        sys.exit("  Signal ohne Beine.")

    print("\n  {} · {}".format(str(sig.get("city")).title(), a.date))
    print("  {:<22}{:>9}{:>12}".format("Bein", "Brief", "je 1 $"))
    kurse = {}
    for lab, tok in beine:
        px = brief(tok)
        kurse[str(lab)] = (tok, px)
        print("  {:<22}{:>8}{:>12}".format(
            str(lab), "–" if px is None else "{:.1f}c".format(px * 100),
            "–" if not px else "{:.2f} Stk".format(1 / px)))

    if a.liste or not a.leg:
        print("\n  Mit --leg <Stufe> eines davon kaufen.\n")
        return

    passend = [k for k in kurse if a.leg in k]
    if len(passend) != 1:
        sys.exit("\n  '{}' passt auf {} Beine. Genauer angeben.\n"
                 .format(a.leg, len(passend)))
    lab = passend[0]
    tok, ask = kurse[lab]
    if not ask:
        sys.exit("\n  Fuer '{}' gibt es gerade kein Verkaufsangebot.\n".format(lab))

    einsatz = max(a.usd, MIN_ORDER * 1.03)
    stueck = einsatz / ask
    print("\n  KAUF   {}".format(lab))
    print("  Einsatz        ${:.2f}".format(einsatz))
    print("  Bei Treffer    ${:.2f}   ({:+.0f}%)"
          .format(stueck, (stueck / einsatz - 1) * 100))
    print("  Sonst          $0.00   (−100%)")
    print("\n  Das ist ein EINZELNES Feld. Nur genau {} zahlt aus,".format(lab))
    print("  jede andere Temperatur ist ein Totalverlust auf diesen Betrag.")

    if a.dry:
        print("\n  Trockenlauf. Nichts gesendet.\n")
        return

    if input("\n  Zum Bestaetigen {:.2f} eintippen: ".format(einsatz)).strip() \
            != "{:.2f}".format(einsatz):
        print("  Abgebrochen.\n")
        return

    try:
        from py_clob_client_v2 import ClobClient, OrderArgs, OrderType, Side
    except ImportError:
        sys.exit("  py-clob-client-v2 fehlt.  pip install py-clob-client-v2")
    key = os.getenv("POLY_PRIVATE_KEY", "").strip()
    funder = os.getenv("POLY_FUNDER_ADDRESS", "").strip()
    if not key or not funder:
        sys.exit("  POLY_PRIVATE_KEY oder POLY_FUNDER_ADDRESS fehlt.")
    styp = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))

    c = ClobClient(CLOB, 137, key=key, signature_type=styp, funder=funder)
    c.set_api_creds(c.create_or_derive_api_key())

    try:
        tick = float(c.get_tick_size(tok))
    except Exception:
        tick = 0.01
    px = ask * (1 + a.slippage / 10000)
    px = math.ceil(px / tick - 1e-9) * tick
    px = round(min(max(px, tick), 1 - tick), 6)
    size = math.ceil(einsatz / px * 100 - 1e-9) / 100
    while size * px < MIN_ORDER * 1.01:
        size = round(size + 0.01, 2)

    try:
        r = c.create_and_post_order(
            OrderArgs(token_id=tok, price=px, size=size, side=Side.BUY),
            order_type=OrderType.GTC)
        ok = bool(r.get("success")) if isinstance(r, dict) else bool(r)
        print("\n  {:<18}{:<11}{} @ {}  = ${:.2f}\n".format(
            lab, "GESENDET" if ok else "ABGELEHNT", size, round(px, 4),
            size * px))
        if not ok:
            print("      {}\n".format(str(r)[:200]))
    except Exception as e:
        print("\n  FEHLER  {}\n".format(str(e)[:200]))


if __name__ == "__main__":
    main()
