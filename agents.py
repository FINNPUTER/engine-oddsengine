"""
agents.py: OddsEngine AI fuer Agenten. Seit 2.31.

Drei Teile, jeder hinter einem Schalter auf admin.html:

  agents  Agent-Keys mit Tagesgrenze und erlaubten Modellen, Key per Wallet-Unterschrift
          ohne Browser (POST /v1/agents/key) und ein MCP-Server unter /mcp.
  x402    Bezahlen pro Anfrage ohne Konto: POST /v1/chat/completions ohne Key bekommt
          402 mit dem Preis in $ODDS. Der Agent unterschreibt ein EIP-2612 permit,
          der Server reicht es mit der Gas-Wallet ein und zieht die $ODDS direkt in die
          Einnahmen-Wallet. Der Betrag wird dem Agenten als Guthaben gutgeschrieben,
          die Anfrage daraus bezahlt, der Rest bleibt fuer die naechste.
          Seit 2.32 auch Standard-x402 in USDC auf Base (scheme exact), siehe usdc.py.

Die Gas-Wallet (X402_GAS_KEY) haelt nur ETH fuer Gebuehren. Sie bekommt nie $ODDS und
nie Kundengeld: transferFrom geht vom Agenten direkt an AI_DEPOSIT_ADDRESS.

Variablen (Service web):
    X402_GAS_KEY        privater Schluessel der Gas-Wallet (nur hier, nirgendwo sonst)
    X402_GAS_USD        Aufschlag pro Zahlung fuer das Gas, Standard 0.01
    X402_MIN_USD        kleinste Zahlung, Standard 0.05
    X402_MAX_TX_DAY     hoechstens so viele Zahlungen pro Tag, Standard 500
    X402_GAS_MIN_ETH    Warnung an den Betreiber darunter, Standard 0.0005
    ODDS_TOKEN_VERSION  EIP-712 version des Tokens, Standard "1"
"""
import base64
import json
import os
import time
from decimal import Decimal

import ai
import holder
import storage
from logger import log

PERMIT_SEL = "d505accf"
TRANSFER_FROM_SEL = "23b872dd"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _f(k, d):
    try:
        return float(os.getenv(k, "").strip() or d)
    except Exception:
        return float(d)


# ── Agent-Keys mit Grenzen ───────────────────────────────────────────────

def tabellen():
    pg = ai._pg()
    pg.execute("ALTER TABLE ai_keys ADD COLUMN IF NOT EXISTS day_cap NUMERIC(20,8)")
    pg.execute("ALTER TABLE ai_keys ADD COLUMN IF NOT EXISTS models TEXT")
    pg.execute("""CREATE TABLE IF NOT EXISTS ai_x402 (
        tx TEXT PRIMARY KEY, payer TEXT NOT NULL, odds NUMERIC(38,8), usd NUMERIC(20,8),
        gas_usd NUMERIC(20,8), permit_tx TEXT, ts TIMESTAMPTZ DEFAULT now())""")


_TAB = {"ok": False}


def _tab():
    if not _TAB["ok"]:
        tabellen()
        _TAB["ok"] = True


def grenzen_setzen(adresse, kid, day_cap=None, models=None):
    _tab()
    cap = None
    if day_cap not in (None, "", 0, "0"):
        cap = max(0.0, float(day_cap))
    ms = None
    if models:
        if isinstance(models, str):
            models = [m.strip() for m in models.split(",")]
        ms = ",".join(sorted({str(m).strip() for m in models if str(m).strip()}))[:2000] or None
    ai._pg().execute("UPDATE ai_keys SET day_cap=%s, models=%s WHERE id=%s AND address=%s",
                     (Decimal(str(cap)) if cap is not None else None, ms, str(kid), holder.norm(adresse)))
    return {"id": kid, "day_cap": cap, "models": ms.split(",") if ms else None}


def grenzen(kid):
    _tab()
    r = ai._pg().execute("SELECT day_cap, models FROM ai_keys WHERE id=%s", (kid,)).fetchone()
    if not r:
        return None, None
    return (float(r[0]) if r[0] is not None else None), (r[1].split(",") if r[1] else None)


def pruefen(kid, model):
    """Wirft ai.Fehler, wenn der Key das Modell nicht darf oder sein Tageslimit erreicht hat."""
    cap, ms = grenzen(kid)
    model = model or ai.AUTO_ID
    if ms and model not in ms:
        raise ai.Fehler(403, "this key may only use: " + ", ".join(ms), "model_not_allowed")
    if cap is not None:
        heute = float(ai._pg().execute(
            """SELECT coalesce(sum(charged),0) FROM ai_usage
               WHERE key_id=%s AND ts > date_trunc('day', now())""", (kid,)).fetchone()[0])
        if heute >= cap:
            raise ai.Fehler(429, "this key reached its daily limit of ${}".format(
                ("{:.2f}" if cap >= 0.01 else "{:.4f}").format(cap)), "key_limit")


def key_mit_grenzen(adresse, name, day_cap, models):
    k = ai.key_neu(adresse, name)
    g = grenzen_setzen(adresse, k["id"], day_cap, models)
    k.update(day_cap=g["day_cap"], models=g["models"])
    return k


# ── Gas-Wallet und Chain ─────────────────────────────────────────────────

def _gas_konto():
    roh = os.getenv("X402_GAS_KEY", "").strip()
    if not roh:
        return None
    from eth_account import Account
    try:
        return Account.from_key(roh if roh.startswith("0x") else "0x" + roh)
    except Exception:
        log.warning("x402: X402_GAS_KEY is not a valid private key")
        return None


def gas_adresse():
    k = _gas_konto()
    return k.address if k else None


def bereit():
    return bool(_gas_konto() and holder.mint() and holder.evm() and ai.bereit())


def _kette():
    return int(holder.ketten_info().get("chain_id") or 4663)


def _hex32(n):
    return hex(int(n))[2:].rjust(64, "0")


def _adr32(a):
    return holder.norm(a)[2:].rjust(64, "0")


def _call(data):
    return holder._evm_rpc("eth_call", [{"to": holder.mint(), "data": data}, "latest"])


def gas_eth():
    a = gas_adresse()
    if not a:
        return None
    try:
        return int(holder._evm_rpc("eth_getBalance", [a, "latest"]), 16) / 1e18
    except Exception:
        return None


_DOM = {}


def _domain():
    """EIP-712 Domain des Tokens, gegen DOMAIN_SEPARATOR auf der Chain geprueft."""
    if _DOM.get("d"):
        return _DOM["d"]
    from eth_account.messages import encode_typed_data  # noqa: F401
    from eth_utils import keccak
    roh = _call("0x06fdde03")
    try:
        b = bytes.fromhex(roh[2:])
        n = int.from_bytes(b[32:64], "big")
        name = b[64:64 + n].decode()
    except Exception:
        name = "OddsEngine"
    chain = _kette()
    soll = (_call("0x3644e515") or "").lower()
    for version in [os.getenv("ODDS_TOKEN_VERSION", "1"), "1", "2"]:
        typ = keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
        enc = (typ + keccak(text=name) + keccak(text=version) + int(chain).to_bytes(32, "big")
               + bytes.fromhex(_adr32(holder.mint())))
        if "0x" + keccak(enc).hex().replace("0x", "") == soll:
            _DOM["d"] = {"name": name, "version": version, "chainId": chain,
                         "verifyingContract": holder.mint()}
            return _DOM["d"]
    _DOM["d"] = {"name": name, "version": os.getenv("ODDS_TOKEN_VERSION", "1"), "chainId": chain,
                 "verifyingContract": holder.mint(), "unverified": True}
    return _DOM["d"]


def _permit_unterzeichner(owner, spender, value, nonce, deadline, sig):
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    d = _domain()
    msg = {
        "types": {
            "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                             {"name": "chainId", "type": "uint256"},
                             {"name": "verifyingContract", "type": "address"}],
            "Permit": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"},
                       {"name": "value", "type": "uint256"}, {"name": "nonce", "type": "uint256"},
                       {"name": "deadline", "type": "uint256"}]},
        "primaryType": "Permit",
        "domain": {k: d[k] for k in ("name", "version", "chainId", "verifyingContract")},
        "message": {"owner": owner, "spender": spender, "value": int(value), "nonce": int(nonce),
                    "deadline": int(deadline)}}
    return Account.recover_message(encode_typed_data(full_message=msg), signature=sig)


def _senden(data):
    """Transaktion an den Token von der Gas-Wallet, wartet auf die Quittung."""
    from eth_account import Account
    k = _gas_konto()
    chain = _kette()
    nonce = int(holder._evm_rpc("eth_getTransactionCount", [k.address, "pending"]), 16)
    try:
        gas = int(holder._evm_rpc("eth_estimateGas", [{"from": k.address, "to": holder.mint(),
                                                       "data": data}]), 16)
    except RuntimeError as e:
        raise ai.Fehler(402, "payment would fail on chain: {}".format(str(e)[:120]), "payment_failed")
    preis = int(holder._evm_rpc("eth_gasPrice", []), 16)
    from eth_utils import to_checksum_address
    tx = {"to": to_checksum_address(holder.mint()), "data": data, "value": 0, "nonce": nonce, "chainId": chain,
          "gas": int(gas * 1.3) + 10000, "maxFeePerGas": preis * 2 + 1, "maxPriorityFeePerGas": max(1, preis // 10),
          "type": 2}
    roh = Account.sign_transaction(tx, k.key)
    raw = getattr(roh, "raw_transaction", None) or getattr(roh, "rawTransaction")
    h = holder._evm_rpc("eth_sendRawTransaction", ["0x" + bytes(raw).hex().replace("0x", "")])
    for _ in range(60):
        q = holder._evm_rpc("eth_getTransactionReceipt", [h])
        if q:
            if int(q.get("status") or "0x0", 16) != 1:
                raise ai.Fehler(402, "payment transaction failed on chain", "payment_failed")
            return h, q
        time.sleep(1)
    raise ai.Fehler(504, "payment is still pending on chain, try again shortly", "payment_pending")


# ── x402 ─────────────────────────────────────────────────────────────────

def _bedarf_usd(body):
    """Hoechstbetrag fuer diese Anfrage in Dollar, wie ai.vorbereiten ihn reservieren wuerde,
    plus Gas. Ohne Konto rechnen wir mit dem Grundpreis."""
    mid = str(body.get("model") or ai.AUTO_ID).strip()
    m = ai.modell(mid)
    if not m:
        raise ai.Fehler(404, f"model '{mid}' is not available. See GET /v1/models", "model_not_found")
    k = 1 + ai.markup()
    pin = (m["in_base"] if m["in_base"] is not None else ai.AUTO_PREIS[0]) / 1e6
    pout = (m["out_base"] if m["out_base"] is not None else ai.AUTO_PREIS[1]) / 1e6
    ein = ai._schaetzung_tokens(body)
    aus = int(body.get("max_tokens") or body.get("max_completion_tokens") or 2048)
    aus = min(aus, m.get("max_output") or 32768)
    usd = (ein * pin + aus * pout) * k
    if ai._mit_web(body):
        usd += (ai._f("AI_WEB_RESERVE_USD", 0.03) + 20000 * pin) * k
    if m.get("kind") == "image" or "image" in (body.get("modalities") or []):
        usd += ((m.get("image_out_base") or 60.0) / 1e6) * 8000 * k
    m = dict(m, roh_usd=usd)
    return max(_f("X402_MIN_USD", 0.05), usd * 1.15) + _f("X402_GAS_USD", 0.01), m


def angebot(body, url):
    """Die 402-Antwort: was diese Anfrage hoechstens kostet, in $ODDS."""
    usd, m = _bedarf_usd(body)
    import usdc
    vorne = [usdc.anforderung(usd, m, url)] if usdc.bereit() else []
    kurs, _ = holder.kurs()
    v2 = {"x402Version": 2, "error": "payment required",
          "resource": {"url": url, "description": "OddsEngine AI, {}".format(m["name"]),
                       "mimeType": "application/json"},
          "accepts": [usdc.anforderung_v2(usd, m)] if vorne else []}
    if not kurs or not bereit():
        if vorne:
            return {"x402Version": 1, "error": "payment required", "accepts": vorne, "_v2": v2}
        raise ai.Fehler(503, "the $ODDS price is not available right now", "service_unavailable")
    dez = holder._dezimalen()
    menge = Decimal(str(usd / kurs)).quantize(Decimal("0.01"))
    roh = int(menge * (Decimal(10) ** dez))
    d = _domain()
    v2["accepts"].append({"scheme": "permit", "network": "eip155:{}".format(d["chainId"]), "amount": str(roh),
                          "asset": holder.mint(), "payTo": ai.eingang(), "maxTimeoutSeconds": 300,
                          "extra": {"token": "ODDS", "decimals": dez, "spender": gas_adresse(),
                                    "eip712": {k: d[k] for k in ("name", "version", "chainId",
                                                                  "verifyingContract")}}})
    return {"x402Version": 1, "error": "payment required", "_v2": v2,
            "accepts": vorne + [{
                "scheme": "permit", "network": "robinhood", "chainId": d["chainId"],
                "maxAmountRequired": str(roh), "asset": holder.mint(), "payTo": ai.eingang(),
                "resource": url, "mimeType": "application/json", "maxTimeoutSeconds": 300,
                "description": "OddsEngine AI, {}. Up to ${:.4f} in $ODDS. Whatever is not used "
                               "stays as credit on your wallet and pays your next requests: send a "
                               "fresh permit and we use the credit first, no new transfer."
                               .format(m["name"], usd),
                "extra": {"token": "ODDS", "decimals": dez, "spender": gas_adresse(),
                          "eip712": {k: d[k] for k in ("name", "version", "chainId", "verifyingContract")},
                          "usd": round(usd, 6), "price_usd": kurs,
                          "how": "Sign an EIP-2612 Permit(owner, spender, value, nonce, deadline) for at "
                                 "least maxAmountRequired, then repeat the request with header X-PAYMENT: "
                                 "base64 of {\"x402Version\":1,\"scheme\":\"permit\",\"network\":\"robinhood\","
                                 "\"payload\":{\"owner\",\"value\",\"deadline\",\"signature\"}}"}}]}


def _heute_n():
    try:
        return int(ai._pg().execute(
            "SELECT count(*) FROM ai_x402 WHERE ts > date_trunc('day', now())").fetchone()[0])
    except Exception:
        return 0


def bezahlen(kopf, body):
    """X-PAYMENT pruefen, auf der Chain einloesen, gutschreiben. Gibt (payer, info) zurueck."""
    _tab()
    if _heute_n() >= int(_f("X402_MAX_TX_DAY", 500)):
        raise ai.Fehler(429, "x402 is busy today, use an API key instead", "x402_limit")
    try:
        p = json.loads(base64.b64decode(kopf + "=" * (-len(kopf) % 4)).decode())
    except Exception:
        raise ai.Fehler(400, "X-PAYMENT is not valid base64 JSON", "invalid_payment")
    if isinstance(p, dict) and int(p.get("x402Version") or 1) >= 2 and isinstance(p.get("accepted"), dict):
        acc = p["accepted"]
        netz = str(acc.get("network") or "")
        p = {"x402Version": 2, "scheme": acc.get("scheme"),
             "network": "base" if netz in ("eip155:8453", "base") else
             ("robinhood" if netz.startswith("eip155:") else netz),
             "payload": p.get("payload") or {}}
    if isinstance(p, dict) and p.get("scheme") == "exact":
        import usdc
        return usdc.bezahlen(p, body, _bedarf_usd(body))
    if not bereit():
        raise ai.Fehler(400, "the permit scheme is not available right now, pay with scheme exact (USDC on base)",
                        "invalid_payment")
    try:
        pl = p.get("payload") or {}
        owner = holder.norm(pl["owner"])
        value = int(str(pl["value"]))
        deadline = int(pl["deadline"])
        sig = str(pl["signature"])
        sig = sig if sig.startswith("0x") else "0x" + sig
    except Exception:
        raise ai.Fehler(400, "X-PAYMENT is not valid base64 JSON with payload owner, value, deadline, "
                             "signature", "invalid_payment")
    if p.get("scheme") not in (None, "permit"):
        raise ai.Fehler(400, "supported schemes: exact (USDC on base) and permit ($ODDS)", "invalid_payment")
    if deadline < time.time() + 60:
        raise ai.Fehler(400, "permit deadline is too close or already passed", "invalid_payment")
    spender = gas_adresse()
    nonce = int(_call("0x7ecebe00" + _adr32(owner)), 16)
    try:
        wer = _permit_unterzeichner(owner, spender, value, nonce, deadline, sig)
    except Exception:
        wer = None
    if not wer or holder.norm(wer) != owner:
        raise ai.Fehler(402, "permit signature does not match the owner", "invalid_payment")
    # Jede Unterschrift genau einmal, auch wenn sie nur Guthaben freigibt
    import hashlib
    merk = "x402s_" + hashlib.sha256(sig.lower().encode()).hexdigest()[:40]
    if storage.kv_get(merk):
        raise ai.Fehler(402, "this payment was already used, sign a new permit", "invalid_payment")
    storage.kv_set(merk, "1")
    usd_noetig, mz = _bedarf_usd(body)
    gas_usd = _f("X402_GAS_USD", 0.01)
    # Reicht das Guthaben vom letzten Mal, zahlt der Agent nichts neu und spart das Gas
    kt = ai.konto(owner) or {}
    if kt.get("credit", 0.0) >= mz["roh_usd"] * 1.05:
        return owner, {"success": True, "transaction": None, "network": "robinhood", "payer": owner,
                       "paid_from": "credit", "credit_usd": round(kt.get("credit", 0.0), 6)}
    kurs, _ = holder.kurs()
    dez = holder._dezimalen()
    menge = value / (10 ** dez)
    if not kurs or menge * kurs < usd_noetig * 0.97:
        raise ai.Fehler(402, "payment too small, this request needs about ${:.4f} in $ODDS".format(usd_noetig),
                        "payment_too_small")
    stand = int(_call("0x70a08231" + _adr32(owner)), 16)
    if stand < value:
        raise ai.Fehler(402, "not enough $ODDS in the paying wallet", "insufficient_funds")
    s = sig[2:]
    r_, s_, v = s[:64], s[64:128], int(s[128:130], 16)
    if v < 27:
        v += 27
    permit = ("0x" + PERMIT_SEL + _adr32(owner) + _adr32(spender) + _hex32(value) + _hex32(deadline)
              + _hex32(v) + r_ + s_)
    ptx, _ = _senden(permit)
    zug = "0x" + TRANSFER_FROM_SEL + _adr32(owner) + _adr32(ai.eingang()) + _hex32(value)
    ttx, quit = _senden(zug)
    li = 0
    an = "0x" + _adr32(ai.eingang())
    for lg in quit.get("logs") or []:
        t = lg.get("topics") or []
        if len(t) == 3 and t[0].lower() == TRANSFER_TOPIC and t[2].lower().endswith(an[-40:].lower()):
            li = int(lg.get("logIndex") or "0x0", 16)
            break
    usd = max(0.0, menge * kurs - gas_usd)
    pg = ai._pg()
    pg.execute("""INSERT INTO ai_deposits (tx, li, block, sender, odds, usd, price)
                  VALUES (%s,%s,%s,%s,%s,%s,%s)
                  ON CONFLICT (tx, li) DO UPDATE SET usd=EXCLUDED.usd, price=EXCLUDED.price
                  WHERE ai_deposits.credited_to IS NULL""",
               (ttx, li, int(quit.get("blockNumber") or "0x0", 16), owner, Decimal(str(menge)),
                Decimal(str(round(usd, 8))), Decimal(str(kurs))))
    ai.konto(owner)
    ai._gutschreiben(ttx, li, owner)
    pg.execute("""INSERT INTO ai_x402 (tx, payer, odds, usd, gas_usd, permit_tx)
                  VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
               (ttx, owner, Decimal(str(menge)), Decimal(str(round(usd, 8))), Decimal(str(gas_usd)), ptx))
    log.info(f"x402: {owner} paid {menge:.2f} ODDS (${usd:.4f}) tx {ttx}")
    return owner, {"success": True, "transaction": ttx, "network": "robinhood", "payer": owner,
                   "credited_usd": round(usd, 6)}


def antwort_kopf(info, v2=False):
    if v2:
        info = dict(info)
        info["network"] = {"base": "eip155:8453", "robinhood": "eip155:4663"}.get(info.get("network"),
                                                                             info.get("network"))
    return base64.b64encode(json.dumps(info).encode()).decode()


def angebot_kopf(v2):
    return base64.b64encode(json.dumps(v2).encode()).decode()


# ── MCP (Streamable HTTP, JSON-RPC 2.0) ─────────────────────────────────

WERKZEUGE = [
    {"name": "list_models",
     "description": "Search the 460+ models on OddsEngine AI. Returns id, name, maker, price per "
                    "typical message in USD and what it is good at.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "words to search in name, id or maker"},
         "category": {"type": "string", "enum": ["picked", "new", "free", "code", "writing", "reasoning",
                                                 "vision", "image", "long context", "cheap", "web search"]},
         "limit": {"type": "integer", "default": 20}}}},
    {"name": "estimate_cost",
     "description": "What one request costs on a model before you run it, in USD, fee included.",
     "inputSchema": {"type": "object", "required": ["model"], "properties": {
         "model": {"type": "string"}, "input_tokens": {"type": "integer", "default": 1000},
         "output_tokens": {"type": "integer", "default": 500}}}},
    {"name": "chat",
     "description": "Ask a model. Paid from the balance of this key's wallet.",
     "inputSchema": {"type": "object", "required": ["model"], "properties": {
         "model": {"type": "string", "description": "model id, or oddsengine/auto"},
         "prompt": {"type": "string"},
         "messages": {"type": "array", "items": {"type": "object"}},
         "max_tokens": {"type": "integer"}}}},
    {"name": "compare",
     "description": "Send one prompt to 2 or 3 models at once and get every answer with its cost.",
     "inputSchema": {"type": "object", "required": ["models", "prompt"], "properties": {
         "models": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3},
         "prompt": {"type": "string"}, "max_tokens": {"type": "integer", "default": 800}}}},
    {"name": "balance",
     "description": "Balance in USD, your fee and your $ODDS holdings.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _text(obj, fehler=False):
    t = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return {"content": [{"type": "text", "text": t}], "isError": bool(fehler)}


def _passt(m, cat):
    tags = m.get("tags") or []
    return {"picked": m.get("picked"), "new": m.get("new"), "free": m.get("free"),
            "image": m.get("kind") == "image", "cheap": m.get("tier") == "cheap",
            "vision": "vision" in tags}.get(cat, cat in tags)


def mcp_min_hold():
    """So viele $ODDS muss die Wallet hinter einem Key halten, um MCP zu nutzen.
    Verbundene Wallets (z. B. Torch) zaehlen mit. 0 schaltet die Sperre ab."""
    return _f("MCP_MIN_HOLD", 250000)


def mcp_sperre(wer):
    """None, wenn der Key MCP nutzen darf, sonst der Text fuer den Agenten."""
    if not wer:
        return ("OddsEngine MCP is for $ODDS holders. Create an API key with a wallet that holds "
                "at least {:,.0f} $ODDS (linked wallets count) and send it as "
                "Authorization: Bearer oe_live_...".format(mcp_min_hold()))
    mindest = mcp_min_hold()
    if mindest <= 0:
        return None
    try:
        held = float(ai.holder_stufe(wer[0]).get("held") or 0)
    except Exception:
        held = 0.0
    if held < mindest:
        return ("OddsEngine MCP is for $ODDS holders. This wallet holds {:,.0f} $ODDS, it needs "
                "{:,.0f}. Linked wallets count too.".format(held, mindest))
    return None


def werkzeug(name, args, wer):
    args = args or {}
    sperre = mcp_sperre(wer)
    if sperre:
        return _text(sperre, True)
    if name == "list_models":
        q = str(args.get("query") or "").lower()
        cat = args.get("category")
        out = []
        for m in ai.modelle():
            if q and q not in (m["name"] + " " + m["id"] + " " + (m.get("provider") or "")).lower():
                continue
            if cat and not _passt(m, cat):
                continue
            out.append({"id": m["id"], "name": m["name"], "maker": m.get("provider"),
                        "per_message_usd": m.get("per_msg"), "per_image_usd": m.get("per_image"),
                        "free": bool(m.get("free")), "new": bool(m.get("new")),
                        "tags": m.get("tags"), "context": m.get("context")})
            if len(out) >= int(args.get("limit") or 20):
                break
        return _text(out)
    if name == "estimate_cost":
        m = ai.modell(str(args.get("model") or ""))
        if not m:
            return _text("unknown model, use list_models", True)
        k = 1 + (ai.markup(wer[0]) if wer else ai.markup())
        if m.get("in_base") is None:
            return _text({"model": m["id"], "note": "price depends on the model it routes to"})
        usd = (int(args.get("input_tokens") or 1000) * m["in_base"]
               + int(args.get("output_tokens") or 500) * m["out_base"]) / 1e6 * k
        return _text({"model": m["id"], "usd": round(usd, 6), "fee": round(k - 1, 4)})
    if not wer:
        return _text("this tool needs an API key: Authorization: Bearer oe_live_...", True)
    adr, kid = wer
    if name == "balance":
        kt = ai.konto(adr) or {}
        st = ai.holder_stufe(adr)
        return _text({"usd": round(kt.get("credit", 0.0), 4), "fee": st.get("markup"),
                      "holding_odds": st.get("held"), "next_tier": st.get("next")})
    if name == "chat":
        msgs = args.get("messages") or [{"role": "user", "content": str(args.get("prompt") or "")}]
        body = {"model": args.get("model") or ai.AUTO_ID, "messages": msgs}
        if args.get("max_tokens"):
            body["max_tokens"] = int(args["max_tokens"])
        try:
            pruefen(kid, body["model"])
            d = ai.anfrage(adr, kid, body)
        except ai.Fehler as e:
            return _text(e.text if hasattr(e, "text") else str(e), True)
        msg = ((d.get("choices") or [{}])[0].get("message") or {})
        return _text({"model": d.get("model"), "content": msg.get("content"),
                      "cost_usd": (d.get("usage") or {}).get("oddsengine_charged_usd")})
    if name == "compare":
        ms = [str(x) for x in (args.get("models") or [])][:3]
        if len(ms) < 2:
            return _text("give 2 or 3 models", True)
        out = []
        for mid in ms:
            try:
                pruefen(kid, mid)
                d = ai.anfrage(adr, kid, {"model": mid, "max_tokens": int(args.get("max_tokens") or 800),
                                          "messages": [{"role": "user", "content": str(args.get("prompt") or "")}]})
                msg = ((d.get("choices") or [{}])[0].get("message") or {})
                out.append({"model": mid, "content": msg.get("content"),
                            "cost_usd": (d.get("usage") or {}).get("oddsengine_charged_usd")})
            except ai.Fehler as e:
                out.append({"model": mid, "error": getattr(e, "text", str(e))})
        return _text(out)
    return _text("unknown tool " + str(name), True)


def mcp(nachricht, wer):
    """Eine JSON-RPC-Nachricht beantworten. None bei Notifications."""
    mid = nachricht.get("id")
    meth = nachricht.get("method")
    if mid is None:
        return None
    def ok(r):
        return {"jsonrpc": "2.0", "id": mid, "result": r}
    if meth == "initialize":
        v = ((nachricht.get("params") or {}).get("protocolVersion")) or "2025-06-18"
        return ok({"protocolVersion": v, "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "oddsengine-ai", "version": "2.31.0"},
                   "instructions": "Every major AI model behind one key, paid in $ODDS. Use list_models "
                                   "to find a model, estimate_cost before big requests, chat or compare to run."})
    if meth == "ping":
        return ok({})
    if meth == "tools/list":
        return ok({"tools": WERKZEUGE})
    if meth == "tools/call":
        p = nachricht.get("params") or {}
        try:
            return ok(werkzeug(p.get("name"), p.get("arguments"), wer))
        except Exception as e:
            log.warning(f"mcp tool {e}")
            return ok(_text("tool failed: " + str(e)[:160], True))
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
