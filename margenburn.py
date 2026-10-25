"""
margenburn.py: Burn aus der Marge der $ODDS-Aufladungen. Seit 2.36.

Agenten zahlen in USDC, dort kauft und verbrennt der Server selbst (usdc.py).
Kunden laden mit $ODDS auf. Die $ODDS liegen in der Einzahlungs-Wallet, zu der der
Server bewusst keinen Schluessel hat. Darum rechnet der Server hier nur aus, was faellig
ist, und der Betreiber signiert den Burn auf admin.html mit seiner Wallet:

    Marge       je Konto: berechnet - Modellkosten, nur fuer verbrauchtes Guthaben
                (unverbrauchtes Guthaben gehoert noch dem Kunden), gewichtet mit dem Anteil,
                den das Konto mit $ODDS bezahlt hat (nicht USDC, nicht geschenkt).
    Faellig     MARGIN_BURN_PCT (50) % der Marge minus schon verbrannt, in Dollar,
                umgerechnet in $ODDS zum Kurs jetzt.
    Nachweis    Nach dem Senden traegt admin.html den Tx-Hash ein. Der Server prueft auf der
                Robinhood Chain: Transfer von $ODDS an 0x...dEaD. Erst dann zaehlt er.

Speicher: kv margin_burns (Liste der Burns mit Tx, Menge, Dollarwert, Zeit).
Oeffentlich unter /v1/burns/margin, damit jeder die Burns nachpruefen kann.
"""
import json
import os
import time

import holder
import storage
from logger import log

DEAD = "0x000000000000000000000000000000000000dead"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _pct():
    try:
        return max(0.0, min(100.0, float(os.getenv("MARGIN_BURN_PCT", "50")))) / 100.0
    except Exception:
        return 0.5


def burns():
    try:
        return json.loads(storage.kv_get("margin_burns") or "[]")
    except Exception:
        return []


def marge_usd():
    import ai
    pg = ai._pg()
    r = pg.execute("""
        WITH dep AS (SELECT credited_to AS a, sum(usd) AS u FROM ai_deposits
                     WHERE credited_to IS NOT NULL AND usd IS NOT NULL GROUP BY 1),
             m AS (SELECT address AS a, sum(charged - cost_or) AS mg FROM ai_usage
                   WHERE status = 'ok' AND charged IS NOT NULL AND cost_or IS NOT NULL GROUP BY 1)
        SELECT coalesce(sum(m.mg * LEAST(1, dep.u / NULLIF(acc.deposited, 0))), 0),
               coalesce(sum(m.mg), 0)
        FROM m JOIN dep ON dep.a = m.a JOIN ai_accounts acc ON acc.address = m.a""").fetchone()
    return float(r[0] or 0), float(r[1] or 0)


def lage():
    import ai
    marge, _ = marge_usd()
    pct = _pct()
    bs = burns()
    verbrannt_usd = sum(float(b.get("usd") or 0) for b in bs)
    verbrannt_odds = sum(float(b.get("odds") or 0) for b in bs)
    faellig_usd = max(0.0, marge * pct - verbrannt_usd)
    minimum = float(os.getenv("MARGIN_BURN_MIN_USD", "1") or 1)
    kurs, quelle = holder.kurs()
    info = holder.ketten_info() or {}
    return {"margin_usd": round(marge, 4), "burn_pct": round(pct * 100), "burn_target_usd": round(marge * pct, 4),
            "burned_usd": round(verbrannt_usd, 4), "burned_odds": round(verbrannt_odds, 4),
            "due_usd": round(faellig_usd, 4),
            "due_odds": round(faellig_usd / kurs, 2) if kurs else None,
            "min_usd": minimum, "ready": faellig_usd >= minimum,
            "price_usd": kurs, "price_source": quelle,
            "token": holder.mint(), "dead": DEAD, "from": ai.eingang(),
            "chain_id_hex": info.get("chain_id_hex"), "rpc": info.get("rpc"), "explorer": info.get("explorer"),
            "decimals": _dezimalen(),
            "burns": bs[-30:][::-1]}


def _dezimalen():
    try:
        return int(holder._evm_call("0x313ce567"), 16)
    except Exception:
        return int(os.getenv("ODDS_DECIMALS", "18") or 18)


def eintragen(tx):
    """Tx pruefen und als Burn eintragen. Nur Transfers von $ODDS an 0x...dEaD zaehlen."""
    tx = (tx or "").strip().lower()
    if not (tx.startswith("0x") and len(tx) == 66):
        return {"ok": False, "error": "That is not a transaction hash."}
    if any(b.get("tx") == tx for b in burns()):
        return {"ok": False, "error": "This burn is already counted."}
    try:
        rc = holder._evm_rpc("eth_getTransactionReceipt", [tx])
    except Exception as e:
        return {"ok": False, "error": "Chain not reachable: " + str(e)[:80]}
    if not rc:
        return {"ok": False, "error": "Not on chain yet. Wait a few seconds and try again."}
    if rc.get("status") not in ("0x1", 1):
        return {"ok": False, "error": "This transaction failed on chain."}
    token = (holder.mint() or "").lower()
    menge = 0
    for lg in rc.get("logs") or []:
        t = lg.get("topics") or []
        if (lg.get("address") or "").lower() == token and len(t) == 3 and t[0] == TRANSFER \
                and t[2][-40:].lower() == DEAD[2:]:
            menge += int(lg.get("data") or "0x0", 16)
    if not menge:
        return {"ok": False, "error": "No $ODDS went to the burn address in this transaction."}
    odds = menge / 10 ** _dezimalen()
    kurs, _ = holder.kurs()
    eintrag = {"tx": tx, "odds": round(odds, 6), "usd": round(odds * kurs, 6) if kurs else 0.0,
               "price": kurs, "ts": int(time.time()), "kind": "margin"}
    bs = burns() + [eintrag]
    storage.kv_set("margin_burns", json.dumps(bs))
    log.info(f"margenburn: {odds:,.2f} ODDS burned, tx {tx}")
    return {"ok": True, **eintrag}


def oeffentlich():
    bs = burns()
    return {"burns": bs[-50:][::-1], "burned_odds": round(sum(float(b.get("odds") or 0) for b in bs), 4),
            "burned_usd": round(sum(float(b.get("usd") or 0) for b in bs), 4), "burn_pct": round(_pct() * 100),
            "how": "Half of the margin from $ODDS top-ups gets burned. The operator signs each burn, every one is on chain."}
