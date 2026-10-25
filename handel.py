"""
handel.py: Trading per Chat fuer Your Engine. Seit 2.38, seit 2.39 auf jeder Chain. Non-custodial.

Your Engine haelt keinen Schluessel und schickt nichts selbst. Ablauf:

    1. Der Nutzer schreibt "kauf mir fuer 50 $ von <CA>" oder "verkauf die Haelfte von $XYZ".
    2. vorschlag() erkennt Chain und Token, prueft was geht, holt ein Angebot und legt einen Auftrag an
       (kv trade_<id>, 15 Minuten gueltig). Your Engine antwortet mit der Zusammenfassung und
       einem Knopf "Confirm in wallet".
    3. Der Knopf oeffnet engine.oddsengine.live/#trade=<id>.
       EVM (Robinhood, Base, Ethereum, BNB, Arbitrum): seite() holt ein FRISCHES Angebot ueber Relay,
       die Wallet wechselt auf die richtige Chain und sendet selbst (Freigabe, dann Tausch).
       Solana: die Seite verbindet Phantom oder Solflare, sol_bauen() baut mit dem Schluessel der Wallet
       (nur die oeffentliche Adresse) eine frische Jupiter-Transaktion, die Wallet unterschreibt und sendet.
    4. gesendet() traegt die Tx ein und meldet das Ergebnis in Telegram.

Grenzen: TRADE_MAX_USD (Standard 1000) je Auftrag, kein Kauf ohne Angebot.
Gebuehr: keine eigene. Your Engine verdient an der Nachricht, nicht am Trade.
Variablen: RELAY_API, RELAY_API_KEY, JUPITER_API_KEY (optional, sonst lite-api.jup.ag), TRADE_MAX_USD.
"""
import json
import os
import re
import secrets
import time

import requests

import holder
import onchain
import storage
from logger import log

RELAY_API = os.getenv("RELAY_API", "https://api.relay.link").rstrip("/")
NATIVE = "0x0000000000000000000000000000000000000000"
SOL = "So11111111111111111111111111111111111111112"
GUELTIG = 15 * 60
SEITE = os.getenv("ENGINE_PUBLIC_URL", "https://engine.oddsengine.live").rstrip("/")
# Chain -> (Chain-Id, Explorer, oeffentlicher RPC fuer die Wallet, natives Kuerzel, Name)
EVM = {
    "robinhood": (4663, "https://robinhoodchain.blockscout.com", "https://rpc.mainnet.chain.robinhood.com", "ETH", "Robinhood Chain"),
    "base": (8453, "https://basescan.org", "https://mainnet.base.org", "ETH", "Base"),
    "ethereum": (1, "https://etherscan.io", "https://ethereum-rpc.publicnode.com", "ETH", "Ethereum"),
    "bsc": (56, "https://bscscan.com", "https://bsc-dataseed.bnbchain.org", "BNB", "BNB Chain"),
    "arbitrum": (42161, "https://arbiscan.io", "https://arb1.arbitrum.io/rpc", "ETH", "Arbitrum"),
}


def _f(k, d):
    try:
        return float(os.getenv(k, "") or d)
    except Exception:
        return d


def _evm(chain):
    if chain == "robinhood":
        k = holder.ketten_info()
        cid, exp, rpc, nat, name = EVM["robinhood"]
        return int(k.get("chain_id") or cid), k.get("explorer") or exp, k.get("rpc") or rpc, nat, name
    return EVM[chain]


def _headers():
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if os.getenv("RELAY_API_KEY", "").strip():
        h["Authorization"] = "Bearer " + os.getenv("RELAY_API_KEY").strip()
    return h


# ── Preise der nativen Muenzen ───────────────────────────────────────────

_NP = {}


def _eth_usd():
    try:
        p = (onchain.preise([onchain.WETH]).get(onchain.WETH) or {}).get("usd")
        if p:
            return float(p)
    except Exception:
        pass
    return _coinbase("ETH")


def _coinbase(sym):
    try:
        r = requests.get(f"https://api.coinbase.com/v2/prices/{sym}-USD/spot", timeout=8).json()
        return float(r["data"]["amount"])
    except Exception:
        return None


def nativ_usd(chain):
    c = _NP.get(chain)
    if c and time.time() - c[0] < 60:
        return c[1]
    if chain == "solana":
        p = None
        try:
            d = requests.get(_jup() + "/price/v3", params={"ids": SOL}, headers=_jh(), timeout=8).json()
            p = float((d.get(SOL) or {}).get("usdPrice") or 0) or None
        except Exception:
            p = None
        p = p or _coinbase("SOL")
    elif chain == "bsc":
        p = _coinbase("BNB")
        if not p:
            try:
                import scan
                p = scan.zahlen("bsc", "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c").get("price")
            except Exception:
                p = None
    else:
        p = _eth_usd()
    if p:
        _NP[chain] = (time.time(), p)
    return p


# ── EVM: Bestaende und Angebot ueber Relay ───────────────────────────────

def _rpc(chain, m, p):
    if chain == "robinhood":
        return holder._evm_rpc(m, p)
    import scan
    return scan._evm(chain, m, p)


def _nativ_saldo(chain, adr):
    try:
        return int(_rpc(chain, "eth_getBalance", [adr, "latest"]), 16)
    except Exception:
        return None


def _token_saldo(chain, token, adr):
    try:
        return int(_rpc(chain, "eth_call", [{"to": token, "data": "0x70a08231" + "0" * 24 + adr.lower()[2:]}, "latest"]), 16)
    except Exception:
        return None


def _dezimal(chain, token):
    try:
        return int(_rpc(chain, "eth_call", [{"to": token, "data": "0x313ce567"}, "latest"]), 16)
    except Exception:
        return 18


def _quote(adr, von, nach, menge_raw, slippage_bps=None, chain="robinhood"):
    cid = _evm(chain)[0]
    body = {"user": adr, "originChainId": cid, "destinationChainId": cid, "originCurrency": von,
            "destinationCurrency": nach, "amount": str(int(menge_raw)), "tradeType": "EXACT_INPUT",
            "recipient": adr, "refundTo": adr}
    if slippage_bps:
        body["slippageTolerance"] = str(int(slippage_bps))
    try:
        r = requests.post(RELAY_API + "/quote/v2", json=body, headers=_headers(), timeout=25)
    except Exception:
        return {"ok": False, "error": "The route service did not answer. Try again in a moment."}
    if r.status_code != 200:
        try:
            j = r.json()
            d = j.get("message") or j.get("error") or ""
        except Exception:
            d = r.text[:160]
        dl = d.lower()
        if "currency" in dl and ("unsupported" in dl or "invalid" in dl):
            return {"ok": False, "error": "No route for this token yet. It is usually too new or has no liquidity a router can reach."}
        if "insufficient" in dl and "liquidity" in dl:
            return {"ok": False, "error": "Not enough liquidity for that size. Try a smaller amount."}
        return {"ok": False, "error": d[:200] or f"route service said {r.status_code}"}
    q = r.json()
    txs, signatur = [], False
    for st in q.get("steps") or []:
        for it in st.get("items") or []:
            if it.get("status") == "complete":
                continue
            if st.get("kind") == "transaction":
                d = it.get("data") or {}
                if d.get("to"):
                    txs.append({"to": d["to"], "data": d.get("data") or "0x",
                                "value": hex(int(d.get("value") or 0)),
                                "gas": hex(int(d["gas"])) if d.get("gas") else None,
                                "step": st.get("id"), "label": st.get("action") or st.get("description") or st.get("id")})
            elif st.get("kind") == "signature":
                signatur = True
    if signatur or not txs:
        return {"ok": False, "error": "This route needs a step your wallet cannot do here. Try a different amount or token."}
    det = q.get("details") or {}
    aus = det.get("currencyOut") or {}
    ein = det.get("currencyIn") or {}
    return {"ok": True, "txs": txs, "out_raw": aus.get("amount"), "out_fmt": aus.get("amountFormatted"),
            "out_usd": aus.get("amountUsd"), "in_usd": ein.get("amountUsd"), "in_fmt": ein.get("amountFormatted"),
            "impact_pct": (det.get("totalImpact") or {}).get("percent"),
            "fees_usd": round(sum(float((v or {}).get("amountUsd") or 0) for v in (q.get("fees") or {}).values()), 4)}


# ── Solana: Angebot und Transaktion ueber Jupiter ────────────────────────

def _jup():
    return "https://api.jup.ag" if os.getenv("JUPITER_API_KEY", "").strip() else "https://lite-api.jup.ag"


def _jh():
    h = {"Accept": "application/json"}
    if os.getenv("JUPITER_API_KEY", "").strip():
        h["x-api-key"] = os.getenv("JUPITER_API_KEY").strip()
    return h


def _jup_quote(von, nach, menge_raw, slippage_bps=None):
    try:
        r = requests.get(_jup() + "/swap/v1/quote", params={"inputMint": von, "outputMint": nach, "amount": str(int(menge_raw)),
                                                             "slippageBps": str(int(slippage_bps or 300)),
                                                             "restrictIntermediateTokens": "true"},
                         headers=_jh(), timeout=20)
    except Exception:
        return {"ok": False, "error": "The Solana route service did not answer. Try again in a moment."}
    if r.status_code != 200:
        try:
            d = r.json().get("error") or r.text
        except Exception:
            d = r.text
        if "not tradable" in str(d).lower() or "no route" in str(d).lower() or "could not find any route" in str(d).lower():
            return {"ok": False, "error": "No route for this token yet. It may still be on its launch curve with no router support, or have no liquidity."}
        return {"ok": False, "error": str(d)[:200]}
    q = r.json()
    return {"ok": True, "quote": q, "out_raw": int(q.get("outAmount") or 0),
            "impact_pct": round(float(q.get("priceImpactPct") or 0) * 100, 3), "usd": q.get("swapUsdValue")}


def _sol_saldo(pubkey):
    try:
        return int(((holder._sol_rpc("getBalance", [pubkey]) or {}).get("value")) or 0)
    except Exception:
        return None


def _sol_token_saldo(mint, pubkey):
    try:
        import scan
        return scan.bestaende("solana", mint, [pubkey]).get(pubkey)
    except Exception:
        return None


def _sol_dezimal(mint):
    try:
        import scan
        d = scan.sicherheit_sol(mint).get("decimals")
        return int(d) if d is not None else 6
    except Exception:
        return 6


def _sol_wallet(adr):
    try:
        import motor
        return (motor.zustand(adr).get("prefs") or {}).get("sol_wallet")
    except Exception:
        return None


# ── Lesbar ───────────────────────────────────────────────────────────────

def _lesbar(x, nach=4):
    try:
        x = float(x)
    except Exception:
        return str(x)
    if x == 0:
        return "0"
    if abs(x) >= 1000:
        return f"{x:,.0f}"
    if abs(x) >= 1:
        return f"{x:,.2f}"
    return f"{x:.{nach}g}"


def _warnungen(impact):
    out = []
    try:
        imp = abs(float(impact or 0))
        if imp >= 10:
            out.append(f"Price impact {imp:.1f}%: you move the price a lot, the pool is thin.")
        elif imp >= 3:
            out.append(f"Price impact {imp:.1f}%.")
    except Exception:
        pass
    return out


# ── Auftrag anlegen ──────────────────────────────────────────────────────

def _ziel(token, alle):
    """(chain, adresse, symbol) oder Fehlertext."""
    if alle:
        import scan
        r = scan.analyse(token, deep=False)
        if not r.get("ok"):
            return None, r.get("error")
        if not r.get("market"):
            return None, "No market for this token yet, so nothing to trade against."
        return (r["chain"], r["address"], r.get("symbol") or r["address"][:6]), None
    t = onchain._suche(token)
    if not t:
        return None, "Token not found on Robinhood Chain. Give the contract address or the exact ticker."
    m = onchain._meta([t]).get(t, {})
    return ("robinhood", t, m.get("symbol") or t[:8]), None


def vorschlag(adr, side, token, amount=None, unit="usd", slippage_pct=None, alle=False):
    """Trade vorbereiten. side buy|sell, unit usd|native|eth|sol|token|percent. Nichts wird gesendet."""
    adr = holder.norm(adr)
    side = str(side or "buy").lower()
    unit = str(unit or ("usd" if side == "buy" else "percent")).lower()
    if unit in ("eth", "sol", "bnb"):
        unit = "native"
    ziel, fehler = _ziel(token, alle)
    if not ziel:
        return {"ok": False, "error": fehler}
    chain, t, sym = ziel
    try:
        menge = float(amount) if amount is not None else (100.0 if unit == "percent" else None)
    except Exception:
        menge = None
    if not menge or menge <= 0:
        return {"ok": False, "error": "How much? For a buy say a dollar amount or an amount of the chain's coin, for a sell a percent or a token amount."}
    slip = int(float(slippage_pct) * 100) if slippage_pct else None
    if chain == "solana":
        return _vorschlag_sol(adr, side, t, sym, menge, unit, slip)
    if chain not in EVM:
        return {"ok": False, "error": "Trading on this chain is not open yet."}
    cid, exp, rpc, nat, name = _evm(chain)
    dez = _dezimal(chain, t) if chain != "robinhood" else int(onchain._meta([t]).get(t, {}).get("decimals") or 18)
    np_ = nativ_usd(chain)
    if side == "buy":
        if unit in ("usd", "$", "dollar"):
            if not np_:
                return {"ok": False, "error": f"Could not read the {nat} price right now. Say the amount in {nat} instead."}
            n = menge / np_
            usd = menge
        else:
            n = menge
            usd = menge * np_ if np_ else None
        roh = int(n * 1e18)
        von, nach = NATIVE, t
        saldo = _nativ_saldo(chain, adr)
        if saldo is not None and saldo < roh:
            return {"ok": False, "error": f"Your wallet has {saldo / 1e18:.5f} {nat} on {name}, the buy needs {n:.5f} {nat} plus a little gas."}
        text_in = f"{n:.5f} {nat}" + (f" (about ${usd:,.2f})" if usd else "")
    else:
        saldo = _token_saldo(chain, t, adr)
        if not saldo:
            return {"ok": False, "error": f"Your wallet {adr[:6]}…{adr[-4:]} holds no {sym} on {name}."}
        tp = None
        try:
            import scan
            tp = scan.zahlen(chain, t).get("price")
        except Exception:
            tp = (onchain.preise([t]).get(t) or {}).get("usd") if chain == "robinhood" else None
        if unit in ("percent", "%", "pct"):
            roh = saldo * min(100.0, menge) // 100
        elif unit in ("usd", "$", "dollar"):
            if not tp:
                return {"ok": False, "error": f"No price for {sym} right now. Say a percent or a token amount."}
            roh = int(menge / tp * 10 ** dez)
        else:
            roh = int(menge * 10 ** dez)
        roh = int(min(roh, saldo))
        if roh <= 0:
            return {"ok": False, "error": "That amount is zero."}
        usd = (roh / 10 ** dez * tp) if tp else None
        von, nach = t, NATIVE
        text_in = f"{_lesbar(roh / 10 ** dez)} {sym}" + (f" (about ${usd:,.2f})" if usd else "")
    grenze = _f("TRADE_MAX_USD", 1000)
    if usd and usd > grenze:
        return {"ok": False, "error": f"Single trades are capped at ${grenze:,.0f} in the beta."}
    q = _quote(adr, von, nach, roh, slip, chain)
    if not q.get("ok"):
        return q
    tid = secrets.token_hex(8)
    auftrag = {"id": tid, "adr": adr, "chain": chain, "side": side, "token": t, "symbol": sym, "decimals": dez,
               "from": von, "to": nach, "amount_raw": str(roh), "slippage_bps": slip,
               "text_in": text_in, "ts": int(time.time()), "status": "pending"}
    storage.kv_set("trade_" + tid, json.dumps(auftrag))
    aus = (f"about {_lesbar(q['out_fmt'])} {sym}" if side == "buy" else f"about {_lesbar(q['out_fmt'], 5)} {nat}") \
        + (f" (${float(q['out_usd']):,.2f})" if q.get("out_usd") else "")
    return {"ok": True, "trade_id": tid, "chain": name, "side": side, "token": t, "symbol": sym, "you_pay": text_in, "you_get": aus,
            "route_fees_usd": q.get("fees_usd"), "price_impact_pct": q.get("impact_pct"), "warnings": _warnungen(q.get("impact_pct")),
            "steps": len(q["txs"]), "wallet": f"{adr[:6]}…{adr[-4:]}", "confirm_url": f"{SEITE}/#trade=" + tid,
            "valid_minutes": GUELTIG // 60,
            "note": "Nothing is sent yet. The user confirms in their own wallet with the button. The final amount is re-quoted when they open it."}


def _vorschlag_sol(adr, side, mint, sym, menge, unit, slip):
    dez = _sol_dezimal(mint)
    solp = nativ_usd("solana")
    pk = _sol_wallet(adr)
    hinweis = None
    if side == "buy":
        if unit in ("usd", "$", "dollar"):
            if not solp:
                return {"ok": False, "error": "Could not read the SOL price right now. Say the amount in SOL instead."}
            n, usd = menge / solp, menge
        else:
            n, usd = menge, (menge * solp if solp else None)
        roh = int(n * 1e9)
        text_in = f"{n:.4f} SOL" + (f" (about ${usd:,.2f})" if usd else "")
        if pk:
            s = _sol_saldo(pk)
            if s is not None and s < roh + 3_000_000:
                return {"ok": False, "error": f"Your Solana wallet {pk[:4]}…{pk[-4:]} has {s / 1e9:.4f} SOL, the buy needs {n:.4f} SOL plus about 0.003 SOL for fees."}
        q = _jup_quote(SOL, mint, roh, slip)
        if not q.get("ok"):
            return q
        aus = f"about {_lesbar(q['out_raw'] / 10 ** dez)} {sym}"
    else:
        usd = None
        roh = None
        if pk:
            saldo = _sol_token_saldo(mint, pk)
            if not saldo:
                return {"ok": False, "error": f"Your Solana wallet {pk[:4]}…{pk[-4:]} holds no {sym}. If you trade from another wallet, open the button and connect that one."}
            if unit in ("percent", "%", "pct"):
                roh = saldo * min(100.0, menge) // 100
            elif unit in ("usd", "$", "dollar"):
                try:
                    import scan
                    tp = scan.zahlen("solana", mint).get("price")
                except Exception:
                    tp = None
                if not tp:
                    return {"ok": False, "error": f"No price for {sym} right now. Say a percent or a token amount."}
                roh = int(menge / tp * 10 ** dez)
            else:
                roh = int(menge * 10 ** dez)
            roh = int(min(roh, saldo))
            if roh <= 0:
                return {"ok": False, "error": "That amount is zero."}
        if roh:
            q = _jup_quote(mint, SOL, roh, slip)
            if not q.get("ok"):
                return q
            usd = q.get("usd")
            text_in = f"{_lesbar(roh / 10 ** dez)} {sym}"
            aus = f"about {q['out_raw'] / 1e9:.4f} SOL" + (f" (${float(usd):,.2f})" if usd else "")
        else:
            q = {}
            text_in = (f"{menge:g}% of your {sym}" if unit in ("percent", "%", "pct") else
                       f"${menge:,.2f} of {sym}" if unit in ("usd", "$", "dollar") else f"{_lesbar(menge)} {sym}")
            aus = "the SOL amount is quoted when you connect your wallet"
            hinweis = "The amount is checked against your wallet when you open the button and connect it."
    try:
        usd = float(usd) if usd is not None else None
    except Exception:
        usd = None
    grenze = _f("TRADE_MAX_USD", 1000)
    if usd and usd > grenze:
        return {"ok": False, "error": f"Single trades are capped at ${grenze:,.0f} in the beta."}
    tid = secrets.token_hex(8)
    auftrag = {"id": tid, "adr": adr, "chain": "solana", "side": side, "token": mint, "symbol": sym, "decimals": dez,
               "unit": unit, "menge": menge, "amount_raw": str(roh) if roh else None, "slippage_bps": slip,
               "text_in": text_in, "ts": int(time.time()), "status": "pending"}
    storage.kv_set("trade_" + tid, json.dumps(auftrag))
    out = {"ok": True, "trade_id": tid, "chain": "Solana", "side": side, "token": mint, "symbol": sym, "you_pay": text_in,
           "you_get": aus, "price_impact_pct": q.get("impact_pct"), "warnings": _warnungen(q.get("impact_pct")),
           "wallet": (f"{pk[:4]}…{pk[-4:]}" if pk else "the Solana wallet they connect (Phantom or Solflare)"),
           "confirm_url": f"{SEITE}/#trade=" + tid, "valid_minutes": GUELTIG // 60,
           "note": "Nothing is sent yet. The user confirms in Phantom or Solflare with the button. The final amount is re-quoted when they confirm."}
    if hinweis:
        out["info"] = hinweis
    return out


# ── Widerruf von Freigaben (Wallet Guard, seit 2.47) ─────────────────────

def widerruf_auftrag(adr, chain, wallet, token, spender, art="erc20"):
    """Einen Widerruf als Bestaetigungs-Link vorbereiten. art: erc20 | all | permit2 | delegate (Solana).
    Die Wallet des Nutzers unterschreibt selbst, Your Engine sendet nichts."""
    if not adr:
        return None
    k = f"rv_{chain}_{wallet}_{token}_{spender}_{art}".lower()
    alt = storage.kv_get(k)
    if alt:
        a = _laden(alt)
        if a and a.get("status") == "pending":
            return alt
    tid = secrets.token_hex(8)
    a = {"id": tid, "kind": "revoke", "adr": adr, "chain": chain, "wallet": wallet, "token": token, "spender": spender,
         "art": art, "side": "revoke", "symbol": "", "text_in": "Revoke approval", "ts": int(time.time()), "status": "pending"}
    try:
        import wache
        if chain == "solana":
            a["symbol"] = wache._mint_name(token)
        else:
            a["symbol"] = wache._symbol(chain, token)[0]
    except Exception:
        pass
    storage.kv_set("trade_" + tid, json.dumps(a))
    storage.kv_set(k, tid)
    return tid


def _widerruf_seite(a):
    chain = a["chain"]
    sp = a["spender"]
    kurz = sp[:6] + "…" + sp[-4:]
    if chain == "solana":
        return {"ok": True, "id": a["id"], "kind": "revoke", "chain": "solana", "chain_name": "Solana", "side": "revoke",
                "symbol": a.get("symbol") or "", "token": a["token"], "spender": sp, "you_pay": f"Remove delegate {kurz}",
                "needs_wallet": True, "known_wallet": a.get("wallet"), "explorer": "https://solscan.io"}
    cid, exp, rpc, nat, name = _evm(chain)
    pad = lambda x: "0" * 24 + x[2:].lower()
    if a["art"] == "all":
        data = "0xa22cb465" + pad(sp) + "0" * 64
        to = a["token"]
    elif a["art"] == "permit2":
        data = "0x87517c45" + pad(a["token"]) + pad(sp) + "0" * 64 + "0" * 64
        to = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
    else:
        data = "0x095ea7b3" + pad(sp) + "0" * 64
        to = a["token"]
    return {"ok": True, "id": a["id"], "kind": "revoke", "chain": chain, "chain_name": name, "native": nat, "side": "revoke",
            "symbol": a.get("symbol") or "", "token": a["token"], "spender": sp, "wallet": a["wallet"],
            "you_pay": f"Revoke {a.get('symbol') or 'token'} approval for {kurz}", "you_get": "Spender can no longer move it",
            "txs": [{"to": to, "data": data, "value": "0x0"}], "chain_id_hex": hex(cid), "rpc": rpc, "explorer": exp}


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58d(s):
    n = 0
    for c in s:
        n = n * 58 + _B58.index(c)
    roh = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + roh


def _sol_widerruf_tx(owner, konto):
    """Legacy-Transaktion mit einer Revoke-Anweisung (SPL Token / Token-2022), unsigniert, base64."""
    import base64
    info = holder._sol_rpc("getAccountInfo", [konto, {"encoding": "jsonParsed"}]) or {}
    v = info.get("value") or {}
    prog = v.get("owner")
    if prog not in ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"):
        raise ValueError("not a token account")
    besitzer = (((v.get("data") or {}).get("parsed") or {}).get("info") or {}).get("owner")
    if besitzer != owner:
        raise ValueError("this token account belongs to another wallet")
    bh = (holder._sol_rpc("getLatestBlockhash", [{"commitment": "finalized"}]) or {}).get("value", {}).get("blockhash")
    keys = [_b58d(owner), _b58d(konto), _b58d(prog)]
    if any(len(k) != 32 for k in keys) or not bh:
        raise ValueError("could not build the transaction")
    msg = bytes([1, 0, 1, 3]) + b"".join(keys) + _b58d(bh) + bytes([1, 2, 2, 1, 0, 1, 5])
    return base64.b64encode(bytes([1]) + b"\x00" * 64 + msg).decode()


def _laden(tid):
    try:
        a = json.loads(storage.kv_get("trade_" + str(tid)) or "null")
    except Exception:
        a = None
    return a


def seite(tid):
    """Fuer die Bestaetigungsseite. EVM: frisches Angebot mit den Transaktionen. Solana: Auftrag, gebaut wird mit sol_bauen."""
    a = _laden(tid)
    if not a:
        return {"ok": False, "error": "This trade link does not exist."}
    if a["status"] != "pending":
        return {"ok": False, "error": "This trade was already sent." if a.get("kind") != "revoke" else "This approval was already revoked.",
                "status": a["status"], "hashes": a.get("hashes"),
                "explorer": _explorer(a.get("chain") or "robinhood"), "chain": a.get("chain") or "robinhood"}
    if a.get("kind") == "revoke":
        if time.time() - a["ts"] > 7 * 86400:
            return {"ok": False, "error": "This revoke link expired. Ask Your Engine to check your wallet again."}
        return _widerruf_seite(a)
    if time.time() - a["ts"] > GUELTIG:
        return {"ok": False, "error": "This trade link expired. Ask Your Engine again for a fresh one."}
    chain = a.get("chain") or "robinhood"
    if chain == "solana":
        return {"ok": True, "id": a["id"], "chain": "solana", "chain_name": "Solana", "side": a["side"], "symbol": a["symbol"],
                "token": a["token"], "you_pay": a["text_in"], "needs_wallet": True, "known_wallet": _sol_wallet(a["adr"]),
                "explorer": "https://solscan.io"}
    q = _quote(a["adr"], a["from"], a["to"], int(a["amount_raw"]), a.get("slippage_bps"), chain)
    if not q.get("ok"):
        return q
    cid, exp, rpc, nat, name = _evm(chain)
    aus = (f"{_lesbar(q['out_fmt'])} {a['symbol']}" if a["side"] == "buy" else f"{_lesbar(q['out_fmt'], 5)} {nat}")
    return {"ok": True, "id": a["id"], "chain": chain, "chain_name": name, "native": nat, "side": a["side"],
            "symbol": a["symbol"], "token": a["token"],
            "wallet": a["adr"], "you_pay": a["text_in"], "you_get": aus, "you_get_usd": q.get("out_usd"),
            "impact_pct": q.get("impact_pct"), "fees_usd": q.get("fees_usd"), "txs": q["txs"],
            "chain_id_hex": hex(cid), "rpc": rpc, "explorer": exp}


B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def sol_bauen(tid, pubkey):
    """Solana: mit der verbundenen Wallet (oeffentliche Adresse) eine frische Jupiter-Transaktion bauen."""
    a = _laden(tid)
    if not a or a.get("chain") != "solana":
        return {"ok": False, "error": "This trade link does not exist."}
    if a["status"] != "pending":
        return {"ok": False, "error": "This trade was already sent."}
    if a.get("kind") == "revoke":
        pk = str(pubkey or "").strip()
        if pk != a.get("wallet"):
            return {"ok": False, "error": f"Connect the wallet {a.get('wallet', '')[:4]}…{a.get('wallet', '')[-4:]}, that is the one with the delegate."}
        try:
            tx = _sol_widerruf_tx(pk, a["token"])
        except Exception as e:
            return {"ok": False, "error": f"Could not build the revoke: {str(e)[:120]}"}
        return {"ok": True, "tx": tx, "you_pay": "Remove delegate", "you_get": "Delegate removed", "warnings": [],
                "rpc": os.getenv("SOLANA_PUBLIC_RPC", "").strip() or "https://api.mainnet-beta.solana.com"}
    if time.time() - a["ts"] > GUELTIG:
        return {"ok": False, "error": "This trade link expired. Ask Your Engine again for a fresh one."}
    pk = str(pubkey or "").strip()
    if not B58.match(pk):
        return {"ok": False, "error": "That is not a Solana wallet address."}
    mint, dez, sym = a["token"], int(a.get("decimals") or 6), a["symbol"]
    if a["side"] == "buy":
        roh = int(a["amount_raw"])
        s = _sol_saldo(pk)
        if s is not None and s < roh + 3_000_000:
            return {"ok": False, "error": f"This wallet has {s / 1e9:.4f} SOL, the buy needs {roh / 1e9:.4f} SOL plus about 0.003 SOL for fees."}
        von, nach = SOL, mint
    else:
        saldo = _sol_token_saldo(mint, pk)
        if not saldo:
            return {"ok": False, "error": f"This wallet holds no {sym}."}
        if a.get("amount_raw"):
            roh = min(int(a["amount_raw"]), saldo)
        else:
            unit, menge = a.get("unit") or "percent", float(a.get("menge") or 100)
            if unit in ("percent", "%", "pct"):
                roh = saldo * min(100.0, menge) // 100
            elif unit in ("usd", "$", "dollar"):
                import scan
                tp = scan.zahlen("solana", mint).get("price")
                if not tp:
                    return {"ok": False, "error": f"No price for {sym} right now."}
                roh = min(int(menge / tp * 10 ** dez), saldo)
            else:
                roh = min(int(menge * 10 ** dez), saldo)
        if roh <= 0:
            return {"ok": False, "error": "That amount is zero."}
        von, nach = mint, SOL
    q = _jup_quote(von, nach, roh, a.get("slippage_bps"))
    if not q.get("ok"):
        return q
    try:
        usd = float(q.get("usd")) if q.get("usd") is not None else None
    except Exception:
        usd = None
    if usd and usd > _f("TRADE_MAX_USD", 1000):
        return {"ok": False, "error": f"Single trades are capped at ${_f('TRADE_MAX_USD', 1000):,.0f} in the beta."}
    try:
        r = requests.post(_jup() + "/swap/v1/swap", json={"quoteResponse": q["quote"], "userPublicKey": pk,
                                                         "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": True,
                                                         "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {
                                                             "maxLamports": 2_000_000, "priorityLevel": "high"}}},
                          headers=dict(_jh(), **{"Content-Type": "application/json"}), timeout=25)
        d = r.json()
    except Exception:
        return {"ok": False, "error": "The Solana route service did not answer. Try again in a moment."}
    tx = d.get("swapTransaction")
    if not tx:
        return {"ok": False, "error": str(d.get("error") or "Could not build the transaction.")[:200]}
    try:
        import motor
        z = motor.zustand(a["adr"])
        if z["prefs"].get("sol_wallet") != pk:
            z["prefs"]["sol_wallet"] = pk
            motor._zustand_speichern(a["adr"], z)
    except Exception:
        pass
    a["wallet"] = pk
    storage.kv_set("trade_" + tid, json.dumps(a))
    pay = f"{roh / 1e9:.4f} SOL" if a["side"] == "buy" else f"{_lesbar(roh / 10 ** dez)} {sym}"
    get = f"{_lesbar(q['out_raw'] / 10 ** dez)} {sym}" if a["side"] == "buy" else f"{q['out_raw'] / 1e9:.4f} SOL"
    return {"ok": True, "tx": tx, "you_pay": pay, "you_get": get, "you_get_usd": usd, "impact_pct": q.get("impact_pct"),
            "warnings": _warnungen(q.get("impact_pct")), "last_valid": d.get("lastValidBlockHeight"),
            "rpc": os.getenv("SOLANA_PUBLIC_RPC", "").strip() or "https://api.mainnet-beta.solana.com"}


def sol_status(sig):
    """Ist die Solana-Transaktion durch? {'state': confirmed|failed|pending}"""
    try:
        r = holder._sol_rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
        v = ((r or {}).get("value") or [None])[0]
        if not v:
            return {"state": "pending"}
        if v.get("err"):
            return {"state": "failed", "error": str(v.get("err"))[:120]}
        return {"state": "confirmed" if v.get("confirmationStatus") in ("confirmed", "finalized") else "pending"}
    except Exception:
        return {"state": "pending"}


def _explorer(chain):
    if chain == "solana":
        return "https://solscan.io"
    return _evm(chain)[1] if chain in EVM else EVM["robinhood"][1]


def gesendet(tid, hashes, ok=True, fehler=None):
    a = _laden(tid)
    if not a:
        return {"ok": False}
    chain = a.get("chain") or "robinhood"
    if chain == "solana":
        hs = [str(h)[:100] for h in (hashes or []) if B58.match(str(h)[:100]) or re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{60,100}", str(h))][:3]
    else:
        hs = [str(h)[:80] for h in (hashes or []) if str(h).startswith("0x")][:6]
    a.update(status="sent" if ok else "failed", hashes=hs, done=int(time.time()), error=(str(fehler)[:200] if fehler else None))
    storage.kv_set("trade_" + tid, json.dumps(a))
    exp = _explorer(chain)
    try:
        import motor
        z = motor.zustand(a["adr"])
        z.setdefault("trades", [])
        z["trades"] = (z["trades"] + [{"id": tid, "chain": chain, "side": a["side"], "symbol": a["symbol"], "in": a["text_in"],
                                        "ts": a["done"], "tx": hs[-1] if hs else None, "ok": ok,
                                        "link": f"{exp}/tx/{hs[-1]}" if hs else None}])[-30:]
        motor._zustand_speichern(a["adr"], z)
        if a.get("kind") == "revoke":
            if ok and hs:
                motor.senden(a["adr"], f"✅ Revoked. {a.get('symbol') or 'The token'} can no longer be moved by "
                                       f"{a['spender'][:6]}…{a['spender'][-4:]}.\n{exp}/tx/{hs[-1]}")
            return {"ok": True}
        if ok and hs:
            motor.senden(a["adr"], f"{'Bought' if a['side'] == 'buy' else 'Sold'} ${a['symbol']}: {a['text_in']} sent from your wallet.\n{exp}/tx/{hs[-1]}")
        elif not ok:
            motor.senden(a["adr"], f"The {a['side']} of ${a['symbol']} did not go through: {fehler or 'cancelled in the wallet'}.")
    except Exception as e:
        log.debug(f"handel gesendet: {e}")
    return {"ok": True}
