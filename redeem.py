#!/usr/bin/env python3
"""redeem.py — gewonnene Positionen einloesen, direkt von der EOA aus.

    python3 redeem.py --dry     zeigt JEDE Transaktion, sendet NICHTS
    python3 redeem.py           loest wirklich ein (fragt vorher)
    python3 redeem.py --usdce   Auszahlung in USDC.e statt pUSD

Vorher im selben Terminal:
    export POLY_PRIVATE_KEY=0xDEIN_SCHLUESSEL

Einmalig zu installieren:
    pip3 install web3 --break-system-packages

WIE ES FUNKTIONIERT
Deine gewonnenen Anteile sind Tokens auf Polygon. Einloesen heisst: einen
Vertrag aufrufen, der die Anteile verbrennt und dir die Sicherheit auszahlt.
Seit dem 28.04.2026 laeuft Polymarket auf pUSD; die Auszahlung geht deshalb
ueber einen vorgelagerten Adapter, der intern USDC.e holt und daraus pUSD
macht. Damit dieser Adapter deine Anteile anfassen darf, braucht es EINMAL
pro Wallet eine Freigabe (setApprovalForAll). Das erledigt das Skript selbst
und nur, wenn sie fehlt.

Gas zahlst du in POL. Ohne POL auf der Wallet passiert gar nichts -- das
Skript prueft das zuerst und bricht mit einer klaren Meldung ab.
"""

import argparse
import json
import os
import subprocess
import sys

# Schluessel aus schluessel.sh laden, egal wo die Datei liegt.
try:
    import schluessel  # noqa: F401
except ImportError:
    pass

EOA = "0xe8f31cabd6f045f7e1fc3c3781f925a9f1a75659"
DATA = "https://data-api.polymarket.com"

# Oeffentliche Polygon-Knoten. Der erste, der antwortet, wird genommen.
# Einzelne drosseln oder sind zeitweise tot -- deshalb eine Liste statt
# einer festen Adresse. Eigenen Knoten notfalls mit --rpc uebergeben.
RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.llamarpc.com",
    "https://polygon-rpc.com",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
    "https://polygon.drpc.org",
]

# Vertragsadressen auf Polygon (nachgeschlagen, nicht geraten)
CTF          = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK     = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
CTF_ADAPTER  = "0xAdA100Db00Ca00073811820692005400218FcE1f"   # -> pUSD
NEG_ADAPTER  = "0xadA2005600Dec949baf300f4C6120000bDB6eAab"   # -> pUSD
USDCE        = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

ABI_REDEEM4 = [{"name": "redeemPositions", "type": "function",
                "stateMutability": "nonpayable", "outputs": [],
                "inputs": [{"name": "collateralToken", "type": "address"},
                           {"name": "parentCollectionId", "type": "bytes32"},
                           {"name": "conditionId", "type": "bytes32"},
                           {"name": "indexSets", "type": "uint256[]"}]}]
ABI_REDEEM2 = [{"name": "redeemPositions", "type": "function",
                "stateMutability": "nonpayable", "outputs": [],
                "inputs": [{"name": "conditionId", "type": "bytes32"},
                           {"name": "amounts", "type": "uint256[]"}]}]
ABI_CTF = [{"name": "setApprovalForAll", "type": "function",
            "stateMutability": "nonpayable", "outputs": [],
            "inputs": [{"name": "operator", "type": "address"},
                       {"name": "approved", "type": "bool"}]},
           {"name": "isApprovedForAll", "type": "function",
            "stateMutability": "view",
            "outputs": [{"name": "", "type": "bool"}],
            "inputs": [{"name": "account", "type": "address"},
                       {"name": "operator", "type": "address"}]}]


def hole(url):
    r = subprocess.run(["curl", "-s", "--max-time", "45", url],
                       capture_output=True, text=True)
    try:
        return json.loads(r.stdout) if r.stdout.strip() else None
    except ValueError:
        return None


def kurz(t):
    import re
    t = re.sub(r".*temperature in ", "", str(t or ""))
    return re.sub(r"\?.*", "", t)[:42]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="nur zeigen, nichts senden")
    ap.add_argument("--usdce", action="store_true", help="in USDC.e statt pUSD")
    ap.add_argument("--rpc", help="eigenen Polygon-Knoten verwenden")
    ap.add_argument("--ja", action="store_true",
                    help="ohne Rueckfrage einloesen (fuer autokauf.py)")
    ap.add_argument("--min", type=float, default=0.05, metavar="BETRAG",
                    help="wertlose Positionen unter diesem Betrag ueberspringen "
                         "(Standard 0.05). --min 0 loest wirklich alles ein.")
    a = ap.parse_args()
    nach_pusd = not a.usdce

    try:
        from web3 import Web3
    except ImportError:
        sys.exit("  web3 fehlt.  pip3 install web3 --break-system-packages")

    key = os.getenv("POLY_PRIVATE_KEY", "").strip()
    if not key:
        sys.exit("  POLY_PRIVATE_KEY fehlt.  export POLY_PRIVATE_KEY=0x...")

    w3 = None
    for u in ([a.rpc] if a.rpc else RPCS):
        try:
            k = Web3(Web3.HTTPProvider(u, request_kwargs={"timeout": 12}))
            if k.is_connected() and k.eth.block_number > 0:
                w3 = k
                print("\n  Knoten: " + u)
                break
        except Exception:
            pass
        print("  Knoten antwortet nicht: " + u)
    if w3 is None:
        sys.exit("\n  Kein Polygon-Knoten erreichbar.\n"
                 "  Pruefe: laeuft ein VPN? Dann kurz abschalten und neu starten.\n"
                 "  Oder einen eigenen Knoten angeben:  --rpc https://...\n")
    konto = w3.eth.account.from_key(key)
    ich = Web3.to_checksum_address(konto.address)
    if ich.lower() != EOA.lower():
        sys.exit("  Der Schluessel gehoert zu {} , erwartet war {}".format(ich, EOA))

    pol = w3.eth.get_balance(ich) / 1e18
    print("  Wallet {}   {:.4f} POL".format(ich[:8] + "..." + ich[-4:], pol))
    if pol < 0.05 and not a.dry:
        sys.exit("  Zu wenig POL fuer Gas. Lade etwas POL auf diese Wallet "
                 "und starte neu.")

    pos = hole(DATA + "/positions?user=" + EOA + "&redeemable=true&sizeThreshold=0")
    if not isinstance(pos, list):
        sys.exit("  Positionen nicht abrufbar.")
    pos = [p for p in pos if float(p.get("size") or 0) > 0]
    if not pos:
        sys.exit("  Nichts einzuloesen.")

    print("  {} einloesbare Position(en), Auszahlung in {}\n"
          .format(len(pos), "pUSD" if nach_pusd else "USDC.e"))

    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF), abi=ABI_CTF)
    plan = []
    freigaben = []
    uebersprungen = [0, 0.0]

    for p in pos:
        cid = str(p.get("conditionId") or p.get("condition_id") or "")
        if not cid:
            continue
        if not cid.startswith("0x"):
            cid = "0x" + cid
        neg = p.get("negativeRisk")
        if neg is None:
            neg = p.get("negRisk")
        wert = float(p.get("size") or 0) * float(p.get("curPrice") or 0)
        # Verlorene Beine halten weiter Tokens. Sie einzuloesen zahlt nichts
        # aus, kostet aber je eine Transaktion und Wartezeit. Deshalb raus,
        # solange nicht ausdruecklich --min 0 gesetzt ist.
        if wert < a.min:
            uebersprungen[0] += 1
            uebersprungen[1] += wert
            continue

        if nach_pusd:
            ziel = NEG_ADAPTER if neg else CTF_ADAPTER
            c = w3.eth.contract(address=Web3.to_checksum_address(ziel),
                                abi=ABI_REDEEM4)
            fn = c.functions.redeemPositions(
                Web3.to_checksum_address(USDCE), b"\x00" * 32,
                bytes.fromhex(cid[2:]), [1, 2])
            if ziel not in freigaben:
                try:
                    ok = ctf.functions.isApprovedForAll(
                        ich, Web3.to_checksum_address(ziel)).call()
                except Exception:
                    ok = False
                if not ok:
                    freigaben.append(ziel)
        elif neg:
            groesse = int(float(p.get("size") or 0) * 1e6)
            idx = int(p.get("outcomeIndex") or 0)
            betraege = [0, 0]
            betraege[idx] = groesse
            c = w3.eth.contract(address=Web3.to_checksum_address(NEG_RISK),
                                abi=ABI_REDEEM2)
            fn = c.functions.redeemPositions(bytes.fromhex(cid[2:]), betraege)
        else:
            c = w3.eth.contract(address=Web3.to_checksum_address(CTF),
                                abi=ABI_REDEEM4)
            fn = c.functions.redeemPositions(
                Web3.to_checksum_address(USDCE), b"\x00" * 32,
                bytes.fromhex(cid[2:]), [1, 2])

        # Probelauf gegen die Kette: estimate_gas fuehrt den Aufruf simuliert
        # aus. Scheitert er, liefert die Ausnahme den Grund -- und zwar OHNE
        # dass eine Transaktion gesendet und Gas verbrannt wird.
        try:
            gas = int(fn.estimate_gas({"from": ich}) * 1.35)
            grund = None
        except Exception as e:
            gas, grund = None, str(e)[:120]
        plan.append((kurz(p.get("title")), wert, cid, bool(neg), fn, gas, grund))

    for ziel in freigaben:
        print("  FREIGABE   einmalig fuer Adapter {}".format(ziel[:10] + "..."))
    for name, wert, cid, neg, _, gas, grund in plan:
        print("  {:<10} {:<44}{:>8.2f}   {}".format(
            "EINLOESEN" if grund is None else "GEHT NICHT",
            name, wert, "negRisk" if neg else "standard"))
        if grund:
            print("             -> {}".format(grund))
    machbar = [x for x in plan if x[6] is None]
    kaputt = [x for x in plan if x[6] is not None]
    print("  {:<55}{:>8.2f}".format("SUMME einloesbar", sum(x[1] for x in machbar)))
    if kaputt:
        print("  {:<55}{:>8.2f}".format(
            "davon abgelehnt, nicht gesendet", sum(x[1] for x in kaputt)))
    plan = machbar
    if uebersprungen[0]:
        print("  {} wertlose Position(en) uebersprungen (zusammen {:.2f}). "
              "Mit --min 0 kaemen sie mit.".format(*uebersprungen))
    if not plan:
        sys.exit("\n  Nichts oberhalb von {:.2f} zu holen.\n".format(a.min))

    if a.dry:
        print("\n  Trockenlauf. Es wurde nichts gesendet.\n")
        return

    print("\n  {} Transaktion(en) werden gesendet und kosten POL."
          .format(len(freigaben) + len(plan)))
    # --ja ueberspringt die Rueckfrage. Nur fuer den Autokauf gedacht, der
    # nachts laeuft und niemanden zum Antworten hat. Von Hand immer ohne.
    if not a.ja and input("  Wirklich einloesen?  ja/nein: ").strip().lower() not in ("ja", "j"):
        print("  Abgebrochen.\n")
        return

    nonce = w3.eth.get_transaction_count(ich)

    def senden(fn, was, gas=None):
        nonlocal nonce
        try:
            tx = fn.build_transaction({
                "from": ich, "nonce": nonce, "chainId": 137,
                "gas": gas or 900000,
                "maxFeePerGas": w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": w3.to_wei(30, "gwei")})
            sig = w3.eth.account.sign_transaction(tx, key)
            roh = getattr(sig, "raw_transaction", None) or sig.rawTransaction
            h = w3.eth.send_raw_transaction(roh)
            nonce += 1
            q = w3.eth.wait_for_transaction_receipt(h, timeout=180)
            print("  {}  {}   {}".format(
                "OK    " if q.status == 1 else "FEHLER",
                was, w3.to_hex(h)))
            return q.status == 1
        except Exception as e:
            print("  FEHLER  {}   {}".format(was, str(e)[:150]))
            return False

    for ziel in freigaben:
        senden(ctf.functions.setApprovalForAll(
            Web3.to_checksum_address(ziel), True), "Freigabe " + ziel[:10])

    gut = 0
    for name, wert, cid, neg, fn, gas, grund in plan:
        if senden(fn, name, gas):
            gut += 1

    print("\n  {} von {} eingeloest. Guthaben pruefen: python3 status.py\n"
          .format(gut, len(plan)))


if __name__ == "__main__":
    main()
