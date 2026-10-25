"""
usdc.py: x402 in USDC auf Base, mit Rueckkauf und Burn von $ODDS. Seit 2.32.

Agenten zahlen mit dem Standard von x402 (scheme "exact", network "base"): sie
unterschreiben eine EIP-3009 TransferWithAuthorization fuer USDC, der Server reicht sie
mit der Gas-Wallet auf Base selbst ein. Kein Coinbase-Konto, kein fremder Facilitator.
Jeder Agent mit einer normalen x402-Bibliothek kann so ohne Anpassung bezahlen.

Geldfluss:
  1. USDC geht vom Agenten an die Gas-Wallet (auf Base ist sie die Kasse).
  2. Der Agent bekommt den Betrag minus Gasaufschlag als Guthaben in Dollar.
  3. Unsere Marge aus der Zahlung (Betrag * Aufschlag / (1 + Aufschlag)) kommt in den
     Rueckkauf-Topf. Ist der Topf groesser als BUYBACK_MIN_USD, kauft der Server damit
     $ODDS auf Robinhood Chain (Across, sonst Relay: USDC auf Base rein, $ODDS raus).
  4. Von den gekauften $ODDS gehen 50 % an 0x...dEaD (verbrannt), 50 % an die
     Einnahmen-Wallet. Jeder Rueckkauf steht oeffentlich unter GET /v1/x402/buybacks.
  5. Alles andere (Modellkosten und Guthaben der Agenten) schiebt der Server sofort als
     USDC an USDC_SWEEP_TO weiter. In der Gas-Wallet bleibt nur der offene Topf.

Sicherheit:
  - Hoechstens X402_USDC_DAY_USD Dollar pro Tag (Standard 100). Darueber antwortet die
    API "busy" und der Betreiber bekommt eine Telegram-Nachricht.
  - Jede Unterschrift genau einmal (Nonce auf der Chain und bei uns gemerkt).
  - Warnung bei zu wenig ETH auf Base.

Variablen (Service web):
    X402_USDC_DAY_USD   Tagesgrenze in Dollar, Standard 100
    BUYBACK_MIN_USD     ab diesem Topf wird gekauft, Standard 5
    BUYBACK_BURN_PCT    Anteil, der verbrannt wird, Standard 50
    USDC_SWEEP_TO       wohin die USDC weitergehen, Standard AI_DEPOSIT_ADDRESS
    BASE_RPC            Standard https://mainnet.base.org
    X402_BASE_MIN_ETH   Warnung darunter, Standard 0.0003
Schalter auf admin.html: "x402 in USDC (Base)".
"""
import base64  # noqa: F401
import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from decimal import Decimal

import ai
import holder
import storage
from logger import log

USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
BASE_ID = 8453
DEAD = "0x000000000000000000000000000000000000dead"
TWA_SEL = "e3ee160e"        # transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)
AUTH_STATE_SEL = "e94a0102"  # authorizationState(address,bytes32)
TRANSFER_SEL = "a9059cbb"
APPROVE_SEL = "095ea7b3"
BAL_SEL = "70a08231"


def _f(k, d):
    try:
        return float(os.getenv(k, "").strip() or d)
    except Exception:
        return float(d)


def _base_rpc_url():
    return os.getenv("BASE_RPC", "").strip() or "https://mainnet.base.org"


def _http(url, daten=None, timeout=20):
    kopf = {"User-Agent": "Mozilla/5.0 oddsengine", "Accept": "application/json"}
    if daten is not None:
        kopf["Content-Type"] = "application/json"
        daten = json.dumps(daten).encode()
    r = urllib.request.Request(url, data=daten, headers=kopf)
    with urllib.request.urlopen(r, timeout=timeout) as f:
        return json.loads(f.read().decode())


def _rpc(kette, methode, params):
    if kette == "base":
        urls = [u.strip() for u in _base_rpc_url().split(",") if u.strip()]
        letzter = None
        for u in urls:
            try:
                d = _http(u, {"jsonrpc": "2.0", "id": 1, "method": methode, "params": params}, 15)
                if "result" in d:
                    return d["result"]
                letzter = d.get("error")
            except Exception as e:
                letzter = str(e)
        raise RuntimeError("base rpc failed: {}".format(str(letzter)[:160]))
    return holder._evm_rpc(methode, params)


def _hex32(n):
    return hex(int(n))[2:].rjust(64, "0")


def _adr32(a):
    return holder.norm(a)[2:].rjust(64, "0")


# ── Tabellen ─────────────────────────────────────────────────────────────

_TAB = {"ok": False}


def _tab():
    if _TAB["ok"]:
        return
    pg = ai._pg()
    pg.execute("""CREATE TABLE IF NOT EXISTS ai_usdc (
        tx TEXT PRIMARY KEY, payer TEXT NOT NULL, usdc NUMERIC(20,6), credited NUMERIC(20,8),
        gas_usd NUMERIC(20,8), margin NUMERIC(20,8), ts TIMESTAMPTZ DEFAULT now())""")
    pg.execute("""CREATE TABLE IF NOT EXISTS ai_buyback (
        id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(), usd NUMERIC(20,6) NOT NULL,
        provider TEXT, bridge_tx TEXT, odds_before NUMERIC(38,8), odds_in NUMERIC(38,8),
        burned NUMERIC(38,8), to_revenue NUMERIC(38,8), burn_tx TEXT, revenue_tx TEXT,
        status TEXT NOT NULL, note TEXT, done_ts TIMESTAMPTZ)""")
    pg.execute("""CREATE TABLE IF NOT EXISTS ai_usdc_sweep (
        tx TEXT PRIMARY KEY, usd NUMERIC(20,6), an TEXT, ts TIMESTAMPTZ DEFAULT now())""")
    _TAB["ok"] = True


# ── Bereitschaft ─────────────────────────────────────────────────────────

def _konto():
    import agents
    return agents._gas_konto()


def kasse():
    k = _konto()
    return k.address if k else None


def an():
    try:
        import features
        return features.an("x402usdc") and features.an("x402") and features.an("ai")
    except Exception:
        return False


def bereit():
    return bool(an() and _konto() and ai.bereit())


def base_eth():
    a = kasse()
    if not a:
        return None
    try:
        return int(_rpc("base", "eth_getBalance", [a, "latest"]), 16) / 1e18
    except Exception:
        return None


def _usdc_stand(adr):
    return int(_rpc("base", "eth_call", [{"to": USDC, "data": "0x" + BAL_SEL + _adr32(adr)}, "latest"]), 16)


def _odds_stand(adr):
    return int(holder._evm_rpc("eth_call", [{"to": holder.mint(), "data": "0x" + BAL_SEL + _adr32(adr)},
                                            "latest"]), 16)


# ── 402-Angebot ──────────────────────────────────────────────────────────

def anforderung(usd, m, url):
    """Der Eintrag fuer accepts[]: Standard x402 exact auf Base."""
    roh = int((Decimal(str(usd)) * Decimal(10 ** 6)).to_integral_value(rounding="ROUND_UP"))
    return {"scheme": "exact", "network": "base", "maxAmountRequired": str(roh),
            "resource": url, "description": "OddsEngine AI, {}. Up to ${:.4f} in USDC. What is not "
            "used stays as credit on your wallet and pays your next requests.".format(m["name"], usd),
            "mimeType": "application/json", "payTo": holder.norm(kasse()), "maxTimeoutSeconds": 300,
            "asset": holder.norm(USDC), "outputSchema": None,
            "extra": {"name": "USD Coin", "version": "2"}}


def anforderung_v2(usd, m):
    """Derselbe Eintrag im Format von x402 v2 (Header PAYMENT-REQUIRED)."""
    roh = int((Decimal(str(usd)) * Decimal(10 ** 6)).to_integral_value(rounding="ROUND_UP"))
    return {"scheme": "exact", "network": "eip155:{}".format(BASE_ID), "amount": str(roh),
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "payTo": holder.norm(kasse()),
            "maxTimeoutSeconds": 300,
            "extra": {"name": "USD Coin", "version": "2", "assetTransferMethod": "eip3009"}}


# ── Zahlung einloesen ────────────────────────────────────────────────────

def _unterzeichner(a, sig):
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    msg = {
        "types": {
            "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                             {"name": "chainId", "type": "uint256"},
                             {"name": "verifyingContract", "type": "address"}],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}]},
        "primaryType": "TransferWithAuthorization",
        "domain": {"name": "USD Coin", "version": "2", "chainId": BASE_ID,
                   "verifyingContract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"},
        "message": {"from": a["from"], "to": a["to"], "value": int(a["value"]),
                    "validAfter": int(a["validAfter"]), "validBefore": int(a["validBefore"]),
                    "nonce": bytes.fromhex(a["nonce"][2:])}}
    return Account.recover_message(encode_typed_data(full_message=msg), signature=sig)


def _heute_usd():
    try:
        return float(ai._pg().execute(
            "SELECT coalesce(sum(usdc),0) FROM ai_usdc WHERE ts > date_trunc('day', now())").fetchone()[0])
    except Exception:
        return 0.0


_NONCE = {}


def _senden(kette, an_, data, wert=0, warten=True):
    """Transaktion von der Gas-Wallet auf base oder robinhood, wartet auf die Quittung."""
    from eth_account import Account
    from eth_utils import to_checksum_address
    k = _konto()
    cid = BASE_ID if kette == "base" else int(holder.ketten_info().get("chain_id") or 4663)
    nonce = int(_rpc(kette, "eth_getTransactionCount", [k.address, "pending"]), 16)
    # Oeffentliche Knoten liefern direkt nach einer Quittung manchmal die alte Nummer
    nonce = max(nonce, _NONCE.get(kette, -1) + 1)
    try:
        gas = int(_rpc(kette, "eth_estimateGas", [{"from": k.address, "to": an_, "data": data,
                                                    "value": hex(int(wert))}]), 16)
    except RuntimeError as e:
        raise ai.Fehler(402, "transaction would fail on chain: {}".format(str(e)[:140]), "payment_failed")
    preis = int(_rpc(kette, "eth_gasPrice", []), 16)
    tx = {"to": to_checksum_address(an_), "data": data, "value": int(wert), "nonce": nonce, "chainId": cid,
          "gas": int(gas * 1.3) + 10000, "maxFeePerGas": preis * 2 + 1,
          "maxPriorityFeePerGas": max(1, min(preis, 1000000) if kette == "base" else preis // 10),
          "type": 2}
    roh = Account.sign_transaction(tx, k.key)
    raw = getattr(roh, "raw_transaction", None) or getattr(roh, "rawTransaction")
    h = _rpc(kette, "eth_sendRawTransaction", ["0x" + bytes(raw).hex().replace("0x", "")])
    _NONCE[kette] = nonce
    if not warten:
        return h, None
    for _ in range(90):
        q = _rpc(kette, "eth_getTransactionReceipt", [h])
        if q:
            if int(q.get("status") or "0x0", 16) != 1:
                raise ai.Fehler(402, "transaction failed on chain", "payment_failed")
            return h, q
        time.sleep(1)
    raise ai.Fehler(504, "transaction is still pending on chain, try again shortly", "payment_pending")


def bezahlen(p, body, bedarf):
    """X-PAYMENT (scheme exact, network base) pruefen und einloesen. Gibt (payer, info)."""
    _tab()
    if not bereit():
        raise ai.Fehler(400, "USDC payments are not live, use the permit scheme with $ODDS", "invalid_payment")
    if p.get("network") not in (None, "base", "eip155:8453"):
        raise ai.Fehler(400, "only USDC on base is supported for the exact scheme", "invalid_payment")
    try:
        pl = p.get("payload") or {}
        a = pl["authorization"]
        a = {"from": holder.norm(a["from"]), "to": holder.norm(a["to"]), "value": int(str(a["value"])),
             "validAfter": int(str(a["validAfter"])), "validBefore": int(str(a["validBefore"])),
             "nonce": str(a["nonce"]).lower()}
        if not a["nonce"].startswith("0x") or len(a["nonce"]) != 66:
            raise ValueError("nonce")
        sig = str(pl["signature"])
        sig = sig if sig.startswith("0x") else "0x" + sig
    except Exception:
        raise ai.Fehler(400, "X-PAYMENT needs payload.signature and payload.authorization "
                             "(from, to, value, validAfter, validBefore, nonce)", "invalid_payment")
    if a["to"] != holder.norm(kasse()):
        raise ai.Fehler(402, "payTo does not match, pay to {}".format(holder.norm(kasse())), "invalid_payment")
    jetzt = time.time()
    if a["validBefore"] < jetzt + 30 or a["validAfter"] > jetzt + 5:
        raise ai.Fehler(400, "authorization is expired or not valid yet", "invalid_payment")
    try:
        wer = _unterzeichner(a, sig)
    except Exception:
        wer = None
    if not wer or holder.norm(wer) != a["from"]:
        raise ai.Fehler(402, "signature does not match authorization.from", "invalid_payment")
    merk = "x402u_" + hashlib.sha256((a["from"] + a["nonce"]).encode()).hexdigest()[:40]
    if storage.kv_get(merk):
        raise ai.Fehler(402, "this payment was already used, sign a new one", "invalid_payment")
    storage.kv_set(merk, "1")
    payer = a["from"]
    usd_noetig, mz = bedarf
    # Reicht das Guthaben, wird nichts eingezogen
    kt = ai.konto(payer) or {}
    if kt.get("credit", 0.0) >= mz["roh_usd"] * 1.05:
        return payer, {"success": True, "transaction": None, "network": "base", "payer": payer,
                       "paid_from": "credit", "credit_usd": round(kt.get("credit", 0.0), 6)}
    usd = a["value"] / 1e6
    if usd < usd_noetig * 0.97:
        raise ai.Fehler(402, "payment too small, this request needs about ${:.4f} in USDC".format(usd_noetig),
                        "payment_too_small")
    grenze = _f("X402_USDC_DAY_USD", 100)
    if _heute_usd() + usd > grenze:
        _alarm_tag(grenze)
        raise ai.Fehler(429, "x402 is busy today, try again tomorrow or use an API key", "x402_limit")
    gebraucht = int(_rpc("base", "eth_call", [{"to": USDC, "data": "0x" + AUTH_STATE_SEL + _adr32(payer)
                                               + a["nonce"][2:]}, "latest"]), 16)
    if gebraucht:
        raise ai.Fehler(402, "this authorization nonce was already used on chain", "invalid_payment")
    if _usdc_stand(payer) < a["value"]:
        raise ai.Fehler(402, "not enough USDC in the paying wallet", "insufficient_funds")
    s = sig[2:]
    r_, s_, v = s[:64], s[64:128], int(s[128:130], 16)
    if v < 27:
        v += 27
    data = ("0x" + TWA_SEL + _adr32(payer) + _adr32(a["to"]) + _hex32(a["value"]) + _hex32(a["validAfter"])
            + _hex32(a["validBefore"]) + a["nonce"][2:] + _hex32(v) + r_ + s_)
    tx, _q = _senden("base", USDC, data)
    gas_usd = _f("X402_GAS_USD", 0.01)
    gut = max(0.0, usd - gas_usd)
    m = ai.markup()
    marge = gut * m / (1 + m)
    pg = ai._pg()
    ai.konto(payer)
    pg.execute("""INSERT INTO ai_usdc (tx, payer, usdc, credited, gas_usd, margin)
                  VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING tx""",
               (tx, payer, Decimal(str(usd)), Decimal(str(round(gut, 8))), Decimal(str(gas_usd)),
                Decimal(str(round(marge, 8)))))
    pg.execute("""UPDATE ai_accounts SET credit = credit + %s, deposited = deposited + %s
                  WHERE address=%s""", (Decimal(str(round(gut, 8))), Decimal(str(round(gut, 8))), payer))
    log.info(f"x402 usdc: {payer} paid ${usd:.4f} tx {tx}")
    return payer, {"success": True, "transaction": tx, "network": "base", "payer": payer,
                   "credited_usd": round(gut, 6)}


# ── Rueckkauf und Burn ───────────────────────────────────────────────────

def topf():
    """Marge, die noch nicht zurueckgekauft wurde, in Dollar."""
    _tab()
    pg = ai._pg()
    rein = float(pg.execute("SELECT coalesce(sum(margin),0) FROM ai_usdc").fetchone()[0])
    raus = float(pg.execute("""SELECT coalesce(sum(usd),0) FROM ai_buyback WHERE status <> 'failed'
                               AND coalesce(note,'') NOT LIKE 'manual%%'""").fetchone()[0])
    return max(0.0, rein - raus)


def _quote_across(atom, adr):
    q = urllib.parse.urlencode({
        "tradeType": "exactInput", "amount": str(atom), "inputToken": USDC, "originChainId": BASE_ID,
        "outputToken": holder.mint(), "destinationChainId": int(holder.ketten_info().get("chain_id") or 4663),
        "depositor": adr, "recipient": adr})
    d = _http("https://app.across.to/api/swap/approval?" + q, None, 25)
    if not d.get("swapTx"):
        raise RuntimeError("across: no swapTx " + json.dumps(d)[:200])
    txs = []
    pruef = ((d.get("checks") or {}).get("allowance") or {})
    if int(pruef.get("actual") or 0) < int(pruef.get("expected") or atom):
        for t in d.get("approvalTxns") or []:
            txs.append({"to": t["to"], "data": t["data"], "value": int(t.get("value") or 0)})
    s = d["swapTx"]
    txs.append({"to": s["to"], "data": s["data"], "value": int(s.get("value") or 0)})
    return txs, int(d.get("expectedOutputAmount") or 0)


def _quote_relay(atom, adr):
    d = _http("https://api.relay.link/quote", {
        "user": adr, "recipient": adr, "originChainId": BASE_ID,
        "destinationChainId": int(holder.ketten_info().get("chain_id") or 4663),
        "originCurrency": USDC, "destinationCurrency": holder.mint(), "amount": str(atom),
        "tradeType": "EXACT_INPUT"}, 25)
    txs = []
    for st in d.get("steps") or []:
        if st.get("kind") != "transaction":
            raise RuntimeError("relay: step kind " + str(st.get("kind")))
        for it in st.get("items") or []:
            x = it.get("data") or {}
            if int(x.get("chainId") or BASE_ID) != BASE_ID:
                raise RuntimeError("relay: unexpected chain")
            txs.append({"to": x["to"], "data": x["data"], "value": int(x.get("value") or 0)})
    out = int(((d.get("details") or {}).get("currencyOut") or {}).get("amount") or 0)
    if not txs:
        raise RuntimeError("relay: no steps")
    return txs, out


_LETZTER = {"fehler": None}


def _kaufen(usd, manuell=False):
    adr = holder.norm(kasse())
    atom = int(usd * 1e6)
    vorher = _odds_stand(adr)
    fehler = []
    for name, f in (("across", _quote_across), ("relay", _quote_relay)):
        try:
            txs, erwartet = f(atom, adr)
            letzter = None
            for t in txs:
                letzter, _ = _senden("base", t["to"], t["data"], t["value"])
            pg = ai._pg()
            pg.execute("""INSERT INTO ai_buyback (usd, provider, bridge_tx, odds_before, status, note)
                          VALUES (%s,%s,%s,%s,'bridging',%s)""",
                       (Decimal(str(round(usd, 6))), name, letzter, Decimal(vorher) / Decimal(10 ** 18),
                        ("manual, " if manuell else "") + "expected {}".format(erwartet)))
            log.info(f"buyback: ${usd:.2f} via {name} tx {letzter}")
            return True
        except Exception as e:
            fehler.append("{}: {}".format(name, str(e)[:160]))
    log.warning("buyback: " + " | ".join(fehler))
    _LETZTER["fehler"] = " | ".join(fehler)[:400]
    return False


def _abschliessen(row):
    """$ODDS sind angekommen: Haelfte verbrennen, Haelfte an die Einnahmen."""
    bid, vorher = row[0], row[1]
    adr = holder.norm(kasse())
    dez = holder._dezimalen()
    jetzt = _odds_stand(adr)
    alt = int(Decimal(vorher) * (Decimal(10) ** dez))
    neu = jetzt - alt
    if neu <= 0:
        return False
    pct = max(0.0, min(100.0, _f("BUYBACK_BURN_PCT", 50)))
    weg = neu * int(pct * 100) // 10000
    rest = neu - weg
    pg = ai._pg()
    pg.execute("UPDATE ai_buyback SET status='burning', odds_in=%s WHERE id=%s",
               (Decimal(neu) / Decimal(10 ** dez), bid))
    btx = rtx = None
    if weg > 0:
        btx, _ = _senden("robinhood", holder.mint(), "0x" + TRANSFER_SEL + _adr32(DEAD) + _hex32(weg))
    if rest > 0:
        rtx, _ = _senden("robinhood", holder.mint(), "0x" + TRANSFER_SEL + _adr32(ai.eingang()) + _hex32(rest))
    pg.execute("""UPDATE ai_buyback SET status='done', burned=%s, to_revenue=%s, burn_tx=%s, revenue_tx=%s,
                  done_ts=now() WHERE id=%s""",
               (Decimal(weg) / Decimal(10 ** dez), Decimal(rest) / Decimal(10 ** dez), btx, rtx, bid))
    log.info(f"buyback {bid}: burned {weg / 10 ** dez:.2f} ODDS, revenue {rest / 10 ** dez:.2f}")
    return True


def jetzt_kaufen(usd):
    """Admin: Rueckkauf sofort mit USDC, die zusaetzlich auf der Kasse liegen (zaehlt nicht gegen den Topf)."""
    _tab()
    if not bereit():
        return {"ok": False, "why": "switch x402usdc off or gas key missing"}
    if ai._pg().execute("SELECT 1 FROM ai_buyback WHERE status IN ('bridging','burning')").fetchone():
        return {"ok": False, "why": "another buyback is still running"}
    frei = _usdc_stand(kasse()) / 1e6 - topf()
    if usd <= 0 or usd > frei:
        return {"ok": False, "why": "only ${:.2f} free USDC on the treasury".format(max(0.0, frei))}
    _TICK["ts"] = time.time()
    ok = _kaufen(round(usd, 2), manuell=True)
    return {"ok": ok} if ok else {"ok": False, "why": _LETZTER.get("fehler") or "bridge did not accept"}


def _fegen():
    """USDC ausser dem offenen Topf an USDC_SWEEP_TO weiterschieben."""
    ziel = holder.norm(os.getenv("USDC_SWEEP_TO", "").strip() or ai.eingang())
    adr = holder.norm(kasse())
    if not ziel or ziel == adr:
        return None
    stand = _usdc_stand(adr)
    halten = int(topf() * 1e6) + 10000
    # Nur was aus Zahlungen von Agenten kam (ohne Marge). Was du selbst fuer einen
    # Rueckkauf auf die Kasse legst, bleibt liegen.
    pg = ai._pg()
    rein = float(pg.execute("SELECT coalesce(sum(usdc - margin),0) FROM ai_usdc").fetchone()[0])
    raus = float(pg.execute("SELECT coalesce(sum(usd),0) FROM ai_usdc_sweep").fetchone()[0])
    mehr = min(stand - halten, int(max(0.0, rein - raus - 0.002) * 1e6))
    if mehr < int(_f("USDC_SWEEP_MIN", 2) * 1e6):
        return None
    tx, _ = _senden("base", USDC, "0x" + TRANSFER_SEL + _adr32(ziel) + _hex32(mehr))
    ai._pg().execute("INSERT INTO ai_usdc_sweep (tx, usd, an) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                     (tx, Decimal(mehr) / Decimal(10 ** 6), ziel))
    log.info(f"usdc: swept ${mehr / 1e6:.2f} to {ziel}")
    return tx


_TICK = {"ts": 0.0, "laeuft": False}


def tick():
    """Alle 2 Minuten aus ai.waechter: offene Rueckkaeufe abschliessen, neue starten, fegen."""
    if _TICK["laeuft"] or time.time() - _TICK["ts"] < 120 or not bereit():
        return
    _TICK.update(laeuft=True, ts=time.time())
    try:
        _tab()
        pg = ai._pg()
        offen = pg.execute("""SELECT id, odds_before, ts FROM ai_buyback
                              WHERE status = 'bridging' ORDER BY id LIMIT 1""").fetchone()
        haengt = pg.execute("SELECT id FROM ai_buyback WHERE status='burning' AND ts < now() - interval '30 minutes' "
                            "AND coalesce(note,'') NOT LIKE '%%alerted%%' LIMIT 1").fetchone()
        if haengt:
            pg.execute("UPDATE ai_buyback SET note=coalesce(note,'')||' alerted' WHERE id=%s", (haengt[0],))
            ai._an_betreiber("OddsEngine Rueckkauf {}: Burn oder Einnahmen-Ueberweisung ist mittendrin "
                             "stehengeblieben. Bitte /v1/x402/buybacks und die Gas-Wallet pruefen.".format(haengt[0]))
        if offen:
            try:
                fertig = _abschliessen(offen)
            except Exception as e:
                fertig = False
                log.warning(f"buyback finish: {e}")
            if not fertig and time.time() - offen[2].timestamp() > 3600:
                pg.execute("UPDATE ai_buyback SET status='stuck', note=coalesce(note,'')||' no ODDS after 1h' "
                           "WHERE id=%s AND status='bridging'", (offen[0],))
                ai._an_betreiber("OddsEngine Rueckkauf {}: nach einer Stunde sind keine $ODDS auf der Gas-Wallet "
                                 "angekommen. Bitte die Bruecke pruefen (Across/Relay).".format(offen[0]))
        elif topf() >= _f("BUYBACK_MIN_USD", 5):
            usd = round(topf(), 2)
            if _usdc_stand(kasse()) >= int(usd * 1e6):
                _kaufen(usd)
        _fegen()
    except Exception as e:
        log.warning(f"usdc tick: {e}")
    finally:
        _TICK["laeuft"] = False


def _alarm_tag(grenze):
    k = "x402u_alarm_" + time.strftime("%Y%m%d")
    if storage.kv_get(k):
        return
    storage.kv_set(k, "1")
    ai._an_betreiber("OddsEngine x402 USDC: Tagesgrenze von ${:.0f} erreicht, weitere USDC-Zahlungen sind "
                     "bis morgen pausiert. Hoeher setzen mit X402_USDC_DAY_USD auf web.".format(grenze))


def warnen():
    """Aus ai.warnen: zu wenig ETH auf Base."""
    if not bereit():
        return
    eth = base_eth()
    tief = 1 if (eth is not None and eth < _f("X402_BASE_MIN_ETH", 0.0003)) else 0
    alt = int(storage.kv_get("ai_warn_base") or 0)
    if tief > alt:
        ai._an_betreiber("OddsEngine x402 USDC: Die Gas-Wallet {} hat auf Base nur noch {:.5f} ETH. Bitte ETH "
                         "auf Base nachschicken, sonst koennen Agenten nicht in USDC bezahlen.".format(kasse(), eth))
    if tief != alt:
        storage.kv_set("ai_warn_base", str(tief))


# ── Anzeigen ─────────────────────────────────────────────────────────────

def oeffentlich():
    """Oeffentliches Protokoll: jede Zahlung zaehlt, jeder Rueckkauf mit Transaktionen."""
    _tab()
    pg = ai._pg()
    z = pg.execute("SELECT count(*), coalesce(sum(usdc),0), coalesce(sum(margin),0) FROM ai_usdc").fetchone()
    rows = pg.execute("""SELECT ts, usd, provider, bridge_tx, odds_in, burned, to_revenue, burn_tx, revenue_tx,
                                status, done_ts FROM ai_buyback ORDER BY id DESC LIMIT 100""").fetchall()
    b = pg.execute("""SELECT coalesce(sum(usd),0), coalesce(sum(odds_in),0), coalesce(sum(burned),0),
                             coalesce(sum(to_revenue),0) FROM ai_buyback WHERE status='done'""").fetchone()
    exp = holder.ketten_info().get("explorer") or "https://robinhoodchain.blockscout.com"
    return {
        "how": "Agents pay in USDC on Base (x402). Our margin buys $ODDS on Robinhood Chain. Half of every "
               "buy is burned (sent to 0x...dEaD), half goes to the OddsEngine revenue wallet.",
        "burn_pct": _f("BUYBACK_BURN_PCT", 50), "burn_address": DEAD, "token": holder.mint(),
        "payments": int(z[0]), "paid_usd": round(float(z[1]), 4), "margin_usd": round(float(z[2]), 4),
        "pending_usd": round(topf(), 4), "next_buy_at_usd": _f("BUYBACK_MIN_USD", 5),
        "bought_usd": round(float(b[0]), 4), "bought_odds": round(float(b[1]), 2),
        "burned_odds": round(float(b[2]), 2), "to_revenue_odds": round(float(b[3]), 2),
        "buybacks": [{
            "ts": r[0].isoformat() if r[0] else None, "usd": float(r[1]), "via": r[2],
            "bridge_tx": r[3], "bridge_link": "https://basescan.org/tx/{}".format(r[3]) if r[3] else None,
            "odds": float(r[4]) if r[4] is not None else None,
            "burned": float(r[5]) if r[5] is not None else None,
            "to_revenue": float(r[6]) if r[6] is not None else None,
            "burn_tx": r[7], "burn_link": "{}/tx/{}".format(exp, r[7]) if r[7] else None,
            "revenue_tx": r[8], "status": r[9],
            "done": r[10].isoformat() if r[10] else None} for r in rows]}


def lage():
    """Fuer admin.html."""
    try:
        _tab()
        pg = ai._pg()
        h = pg.execute("""SELECT count(*), coalesce(sum(usdc),0) FROM ai_usdc
                          WHERE ts > date_trunc('day', now())""").fetchone()
        usdc = None
        try:
            usdc = _usdc_stand(kasse()) / 1e6 if kasse() else None
        except Exception:
            pass
        return {"on": an(), "treasury": kasse(), "base_eth": base_eth(), "usdc_balance": usdc,
                "payments_today": int(h[0]), "usd_today": round(float(h[1]), 4),
                "day_limit_usd": _f("X402_USDC_DAY_USD", 100), "pending_buyback_usd": round(topf(), 4),
                "sweep_to": holder.norm(os.getenv("USDC_SWEEP_TO", "").strip() or ai.eingang())}
    except Exception as e:
        return {"error": str(e)[:160]}
