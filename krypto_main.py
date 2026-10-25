"""
krypto_main.py: eigener Railway-Dienst fuer die Krypto-Engine. Seit 2.27.

    Procfile:  crypto: python krypto_main.py

In Railway einen neuen Dienst aus demselben Repo anlegen und ihn `crypto`
nennen. Er bekommt dieselbe DATABASE_URL wie web und worker (Variable
referenzieren), sonst sieht die API nichts. Der Wetter-Worker bleibt
unberuehrt.

    python krypto_main.py           # Dauerschleife
    python krypto_main.py --once    # ein Durchlauf, Ausgabe, Ende
"""
import json
import os
import sys
import time

import threading

import boersen
import krypto
import optionen
from logger import log

VERSION = "2.37.0  (2026-10-23  Lernlabor: Merkmale je Fenster, Walk-forward-Training, Schattentrades, nur intern). Davor 2.35.0  (Boersen-Rekorder und Optionen gegen BTC/ETH ueber X). Davor 2.27.2  (2026-10-20  Engine v2: Chainlink-TWAP-Gewichte, Startpreis 60-s-Schnitt, Kopplung an den Markt, sniper statt cheap. Davor 2.27.0 Krypto-Engine: Up/Down 5m/15m/4h fuer BTC ETH SOL XRP DOGE BNB HYPE, TWAP-Modell, fuenf Papier-Module, Kalibrierung gegen den Markt)"
TAKT = float(os.getenv("CRYPTO_TICK", "5"))


def _schleife(name, fn, takt):
    """Langsame Nebenaufgaben im eigenen Faden, damit der 5-Sekunden-Takt der Krypto-Engine nie wartet."""
    while True:
        t0 = time.time()
        try:
            fn(force=True)
        except Exception as e:
            log.warning(f"crypto {name}: {e}")
        time.sleep(max(5.0, takt - (time.time() - t0)))


def main():
    log.info(f"OddsEngine crypto {VERSION}")
    if "--once" not in sys.argv:
        threading.Thread(target=_schleife, args=("boersen", boersen.tick, boersen.TAKT), daemon=True).start()
        threading.Thread(target=_schleife, args=("optionen", optionen.tick, optionen.TAKT), daemon=True).start()
        import lernen
        threading.Thread(target=lernen.schleife, daemon=True).start()
    if "--once" in sys.argv:
        z = krypto.tick()
        for m in z["markets"]:
            log.info(f"{m['sym']:5} {m['tf']:4} p_up={m['p_up']} ask_up={m['ask_up']} "
                     f"ask_down={m['ask_down']} signal={m['signal']}")
        return
    fehler = 0
    while True:
        t0 = time.time()
        try:
            z = krypto.tick()
            fehler = 0
            if int(t0) % 300 < TAKT:
                log.info(f"crypto: {len(z['markets'])} markets live, "
                         f"{len(krypto._OFFEN)} windows open")
        except Exception as e:
            fehler += 1
            log.warning(f"crypto tick: {e}")
            if fehler > 20:
                time.sleep(30)
        time.sleep(max(0.5, TAKT - (time.time() - t0)))


if __name__ == "__main__":
    main()
