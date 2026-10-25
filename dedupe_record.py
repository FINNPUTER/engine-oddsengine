#!/usr/bin/env python3
"""
dedupe_record.py — die doppelt gezaehlten Aufloesungen bereinigen.

Ein Markt kann mehrfach veroeffentlicht worden sein. Bis heute Nacht schrieb
die Aufloesung dann fuer jede Veroeffentlichung eine eigene Zeile, und ein
Treffer zaehlte doppelt. Der Fehler ist behoben, aber die bereits
geschriebenen Zeilen stehen noch in der Datenbank.

Behalten wird die ZULETZT geschriebene Zeile je Markt. Sie gehoert zur
zuletzt gesagten Fassung, und die hat die vorherige ersetzt.

Standard ist Trockenlauf. Es wird nichts geloescht, bevor --loeschen gesetzt
ist, und auch dann erst nach einer Rueckfrage.

    In der Railway-Konsole beim Dienst worker:

    /opt/venv/bin/python dedupe_record.py
    /opt/venv/bin/python dedupe_record.py --loeschen
"""
import argparse
import os
import sys
from collections import defaultdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loeschen", action="store_true",
                    help="wirklich loeschen, sonst nur anzeigen")
    a = ap.parse_args()

    url = os.getenv("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL fehlt — das hier gehoert in die Railway-Konsole.")
    try:
        import psycopg
    except ImportError:
        sys.exit("psycopg fehlt.")

    con = psycopg.connect(url, autocommit=True)

    # Nach NAMEN lesen, nicht nach Position. Beim Erweitern der Abfrage sind
    # die Nummern sonst still verrutscht, und dann steht die Rendite in der
    # Spalte fuer die Beinzahl.
    cur = con.execute("""
        SELECT id, slug, ts,
               payload->>'city'          AS city,
               payload->>'date'          AS market_date,
               payload->>'outcome_hit'   AS hit,
               payload->>'cost'          AS cost,
               payload->>'pnl_pct'       AS pnl,
               payload->'said_legs'      AS legs,
               payload->>'outcome_label' AS label,
               payload->>'published_ts'  AS pub
        FROM snapshots
        WHERE payload->>'kind' = 'resolution'
        ORDER BY slug, ts
    """)
    spalten = [d[0] for d in cur.description]
    rows = [dict(zip(spalten, r)) for r in cur.fetchall()]

    if not rows:
        print("\n  keine Aufloesungen gefunden\n")
        return

    je_markt = defaultdict(list)
    for r in rows:
        je_markt[r["slug"]].append(r)

    doppelt = {k: v for k, v in je_markt.items() if len(v) > 1}
    print(f"\n  {len(rows)} Aufloesungen, {len(je_markt)} verschiedene Maerkte")
    if not doppelt:
        print("  keine Doppelten.")
        if a.loeschen:
            nachrechnen(con)
        else:
            print("  Mit --loeschen werden die Renditen nachgerechnet.\n")
        return

    weg = []
    print(f"  {len(doppelt)} Markt/Maerkte mehrfach vorhanden:\n")
    for slug, v in doppelt.items():
        # Entscheidend ist, wann das Signal VEROEFFENTLICHT wurde, nicht wann
        # es aufgeloest wurde. Beide Aufloesungen entstehen im selben Lauf und
        # tragen denselben Zeitstempel — danach zu sortieren waere Zufall.
        # Behalten wird die zuletzt gesagte Fassung, sie hat die frueheren
        # ersetzt.
        v = sorted(v, key=lambda r: (r["pub"] or "", str(r["ts"])))
        behalten = v[-1]
        print(f"  {v[0]['city']} {v[0]['market_date']}   ({slug})")
        for r in v:
            marke = "  <- behalten" if r["id"] == behalten["id"] else "  <- weg"
            kosten = f"{float(r['cost'])*100:.1f}c" if r["cost"] else "?"
            beine = len(r["legs"]) if r["legs"] else "?"
            print(f"      id {r['id']:<8}"
                  f"veroeff. {str(r['pub'] or '?')[:16]:<18}"
                  f"Preis {kosten:<8}{beine} Beine  "
                  f"Rendite {r['pnl'] or '?':>7}%{marke}")
            if r["id"] != behalten["id"]:
                weg.append(r["id"])
        print()

    print(f"  Zu loeschen: {len(weg)} Zeile(n)")

    if not a.loeschen:
        print("\n  Trockenlauf. Nichts geaendert. Mit --loeschen ausfuehren.\n")
        return

    print("\n  Zum Loeschen tippe LOESCHEN und Enter.")
    if input("  > ").strip() != "LOESCHEN":
        print("  Abgebrochen.\n")
        return

    con.execute("DELETE FROM snapshots WHERE id = ANY(%s)", (weg,))
    rest = con.execute("""SELECT count(*) FROM snapshots
                          WHERE payload->>'kind' = 'resolution'""").fetchone()[0]
    print(f"\n  {len(weg)} Zeile(n) geloescht. {rest} Aufloesungen verbleiben.")
    nachrechnen(con)


def nachrechnen(con):
    """Alte Zeilen auf die echte Aufteilung umrechnen.

    Bis heute stand dort 1 geteilt durch Blockpreis, also eine perfekt nach
    Preis verteilte Aufteilung. Tatsaechlich zwingt die 1-Dollar-Mindestorder
    mehr Geld auf billige Randbeine. Trifft so eines, ist die Auszahlung ein
    Vielfaches. Die Einzelpreise stehen in der Signalzeile, also holen wir sie
    von dort.
    """
    einsatz = float(os.getenv("EXAMPLE_BANKROLL", "100")) * \
              float(os.getenv("STAKE_PCT", "5")) / 100.0
    mindest = float(os.getenv("MIN_ORDER_USD", "1"))

    rows = con.execute("""
        SELECT r.id, r.slug,
               r.payload->'said_legs'      AS legs,
               r.payload->>'outcome_label' AS label,
               r.payload->>'outcome_hit'   AS hit,
               r.payload->>'pnl_pct'       AS alt
        FROM snapshots r
        WHERE r.payload->>'kind' = 'resolution'
    """).fetchall()
    if not rows:
        return

    print(f"\n  rechne {len(rows)} Renditen mit ${einsatz:.2f} Einsatz nach\n")
    geaendert = ergaenzt = 0
    for rid, slug, legs, label, hit, alt in rows:
        sig = con.execute("""
            SELECT payload->'asks' FROM snapshots
            WHERE slug = %s AND payload->>'signal' = 'true'
                  AND payload->'asks' IS NOT NULL
            ORDER BY ts DESC LIMIT 1""", (slug,)).fetchone()
        if not sig or not sig[0] or not legs:
            continue
        asks = [float(x) for x in sig[0]]
        if len(asks) != len(legs):
            continue
        k = sum(asks)
        if not k:
            continue
        st = [max(a / k * einsatz, mindest) for a in asks]
        ges = sum(st)
        if str(hit).lower() == "true" and label in legs:
            i = legs.index(label)
            zur = st[i] / asks[i] if asks[i] else 0.0
        else:
            zur = 0.0
        neu = round((zur / ges - 1) * 100, 2)
        # Auch schreiben, wenn sich die Rendite nicht aendert. Sonst fehlen
        # genau den Fehlschlaegen (immer -100) die Felder budget und payout,
        # und jede spaetere Auswertung rechnet dann nur mit den Treffern.
        unveraendert = alt is not None and abs(float(alt) - neu) < 0.5
        con.execute("""UPDATE snapshots
                       SET payload = payload
                           || jsonb_build_object('pnl_pct', %s::numeric,
                                                 'payout',  %s::numeric,
                                                 'budget',  %s::numeric)
                       WHERE id = %s""",
                    (neu, round(zur, 2), round(ges, 2), rid))
        if not unveraendert:
            print(f"    {slug[:46]:<48}{float(alt or 0):>+8.0f}% -> {neu:>+8.0f}%")
            geaendert += 1
        else:
            ergaenzt += 1
    print(f"\n  {geaendert} Zeile(n) angepasst, "
          f"{ergaenzt} nur um Einsatz und Auszahlung ergaenzt.\n")


if __name__ == "__main__":
    main()
