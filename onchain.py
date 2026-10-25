"""
onchain.py: Your Engine liest die Chain. Seit 2.33.

Alles live von Robinhood Chain ueber den oeffentlichen RPC (plus Multicall3), Preise von
GeckoTerminal und DexScreener. Kein fremdes Konto, kein Schluessel.

  launches()        jeder Launch auf dem Torch-Launchpad (Event der Torch-Fabrik), mit Cache
  holdings(w)       was ein Wallet haelt: ETH, $ODDS, $TORCH, USDG, WETH und jeder Torch-Token,
                    mit Dollarwert; dazu ETH auf Base, Ethereum, Arbitrum, BNB (ueber Torch)
  activity(w)       die letzten Kaeufe, Verkaeufe und Transfers dieses Wallets
  token(q)          Name, Supply, verbrannt, Preis, Marktwert, Liquiditaet, Launch, Ersteller
  burned(q)         verbrannte Menge eines Tokens (0x...dEaD und 0x0)

Grenzen des oeffentlichen RPC: eth_getLogs ueber 10 Mio. Bloecke nur mit EINER Adresse.
Darum: Bestaende per balanceOf ueber alle bekannten Tokens, Verlauf je Token.
Tokens von anderen Launchpads auf Robinhood Chain sieht holdings() nur, wenn man die
Adresse nennt (token(q) und activity(w, token)).
"""
import json
import os
import time

import requests

import holder
import storage
from logger import log

TORCH = "0x000050e18ffa3f8f7ad170eb9cef160eb3ed1111"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
FABRIK = "0x7891ea889899928afe637e4cca8106188427776a"
LAUNCH_TOPIC = "0xea6baf14ec1f4c08d18114c1eea0e6a2a018eef994f9db19ea8a41526e49d28c"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
MULTICALL = "0xcA11bde05977b3631167028862bE2a173976CA11"
DEAD = "0x000000000000000000000000000000000000dead"
NULL = "0x0000000000000000000000000000000000000000"
START_BLOCK = 0
FENSTER = 9_900_000
EXP = "https://robinhoodchain.blockscout.com"
TORCH_TRADE = "https://app.torchpad.fun/trade/robinhood/"


def _rpc(m, p):
    return holder._evm_rpc(m, p)


def _rpc_batch(calls, n=40):
    """JSON-RPC-Batch, Ergebnisliste in derselben Reihenfolge (None bei Fehler).
    Seit 2.40 in Paketen zu 40: der Robinhood-RPC lehnt groessere Pakete mit 429 ab, dann war alles None."""
    out = [None] * len(calls)
    for s in range(0, len(calls), n):
        teil = calls[s:s + n]
        for url in holder._evm_rpcs():
            ok = False
            for versuch in range(4):
                try:
                    r = requests.post(url, json=[{"jsonrpc": "2.0", "id": s + i, "method": c[0], "params": c[1]}
                                                 for i, c in enumerate(teil)],
                                      headers={"User-Agent": "Mozilla/5.0 oddsengine"}, timeout=30)
                    d = r.json()
                    if isinstance(d, list):
                        for x in d:
                            if isinstance(x, dict) and "result" in x:
                                out[int(x["id"])] = x["result"]
                        ok = True
                        break
                    if r.status_code == 429 or "429" in str(d)[:200]:
                        time.sleep(0.6 * (versuch + 1))
                        continue
                    break
                except Exception:
                    time.sleep(0.3)
            if ok:
                break
    return out


def _adr(a):
    a = str(a or "").strip().lower()
    return a if a.startswith("0x") and len(a) == 42 else None


def _top():
    return int(_rpc("eth_blockNumber", []), 16)


def _ts_bloecke(bloecke):
    bloecke = sorted(set(bloecke))
    res = _rpc_batch([("eth_getBlockByNumber", [hex(b), False]) for b in bloecke])
    out = {}
    for b, r in zip(bloecke, res):
        if not r:
            try:
                r = _rpc("eth_getBlockByNumber", [hex(b), False])
            except Exception:
                r = None
        out[b] = int(r["timestamp"], 16) if r else None
    return out


def _vertraege(adressen):
    """Welche Adressen sind Vertraege (Pool, Router, Hook)? Cache im Speicher."""
    adressen = [a for a in set(adressen) if a not in _CODE]
    if adressen:
        res = _rpc_batch([("eth_getCode", [a, "latest"]) for a in adressen])
        for a, r in zip(adressen, res):
            if r is None:
                try:
                    r = _rpc("eth_getCode", [a, "latest"])
                except Exception:
                    r = "0x"
            _CODE[a] = bool(r and r not in ("0x", "0x0"))
    return _CODE


_CODE = {}


# ── Multicall ────────────────────────────────────────────────────────────

def _multicall(aufrufe):
    """aufrufe: [(ziel, calldata_hex)] -> [bytes|None]."""
    from eth_abi import decode, encode
    out = []
    for i in range(0, len(aufrufe), 250):
        teil = aufrufe[i:i + 250]
        daten = "0x82ad56cb" + encode(["(address,bool,bytes)[]"],
                                      [[(z, True, bytes.fromhex(c[2:])) for z, c in teil]]).hex()
        try:
            roh = _rpc("eth_call", [{"to": MULTICALL, "data": daten}, "latest"])
            erg = decode(["(bool,bytes)[]"], bytes.fromhex(roh[2:]))[0]
            out += [d if ok else None for ok, d in erg]
        except Exception as e:
            log.warning(f"onchain multicall: {e}")
            out += [None] * len(teil)
    return out


def _u(b):
    return int.from_bytes(b, "big") if b and len(b) >= 32 else None


def _s(b):
    if not b:
        return None
    try:
        from eth_abi import decode
        return decode(["string"], b)[0]
    except Exception:
        try:
            return b.rstrip(b"\x00").decode(errors="ignore") or None
        except Exception:
            return None


def _pad(a):
    return a[2:].rjust(64, "0")


def _meta(tokens):
    """Symbol, Name, Dezimalen, Supply je Token (Cache 1 Tag)."""
    out, fehlt = {}, []
    for t in tokens:
        c = storage.kv_get("ocm_" + t)
        if c:
            try:
                out[t] = json.loads(c)
                continue
            except Exception:
                pass
        fehlt.append(t)
    if fehlt:
        auf = []
        for t in fehlt:
            auf += [(t, "0x95d89b41"), (t, "0x06fdde03"), (t, "0x313ce567"), (t, "0x18160ddd")]
        r = _multicall(auf)
        for i, t in enumerate(fehlt):
            sym, name, dez, sup = r[4 * i:4 * i + 4]
            m = {"symbol": _s(sym) or "?", "name": _s(name) or "?", "decimals": _u(dez) if _u(dez) is not None else 18,
                 "supply_raw": str(_u(sup) or 0)}
            out[t] = m
            try:
                storage.kv_set("ocm_" + t, json.dumps(m))
            except Exception:
                pass
    return out


# ── Preise ───────────────────────────────────────────────────────────────

_PREIS = {}


def preise(tokens):
    """{token: {usd, fdv, vol24, liq, chg24}} ueber GeckoTerminal, 2 Minuten Cache."""
    jetzt = time.time()
    out, fehlt = {}, []
    for t in tokens:
        c = _PREIS.get(t)
        if c and jetzt - c[0] < 120:
            out[t] = c[1]
        else:
            fehlt.append(t)
    for i in range(0, len(fehlt), 30):
        teil = fehlt[i:i + 30]
        try:
            d = requests.get("https://api.geckoterminal.com/api/v2/networks/robinhood/tokens/multi/" + ",".join(teil),
                             headers={"Accept": "application/json"}, timeout=15).json()
            for x in d.get("data") or []:
                a = x.get("attributes") or {}
                t = str(a.get("address") or "").lower()
                p = {"usd": float(a["price_usd"]) if a.get("price_usd") else None,
                     "fdv": float(a["fdv_usd"]) if a.get("fdv_usd") else None,
                     "vol24": float((a.get("volume_usd") or {}).get("h24") or 0),
                     "liq": float(a.get("total_reserve_in_usd") or 0)}
                out[t] = p
                _PREIS[t] = (jetzt, p)
        except Exception as e:
            log.warning(f"onchain preise: {e}")
    return out


_DEX = {}


def _dex(token):
    """Bestes Paar auf DexScreener: Preis, Aenderung, Kaeufe/Verkaeufe, Volumen je Zeitfenster. 30 s Cache."""
    c = _DEX.get(token)
    if c and time.time() - c[0] < 30:
        return c[1]
    p = None
    try:
        d = requests.get("https://api.dexscreener.com/latest/dex/tokens/" + token, timeout=12).json()
        ps = sorted(d.get("pairs") or [], key=lambda p: -float((p.get("liquidity") or {}).get("usd") or 0))
        p = ps[0] if ps else None
    except Exception:
        p = None
    _DEX[token] = (time.time(), p)
    return p


def _veraenderung(token):
    p = _dex(token)
    if p:
        return (p.get("priceChange") or {}), p.get("url")
    return {}, None


_ENTS = {}


def _entsperrung_rh(token, locker, ab_block=None):
    """Entsperrdatum eines Locks: Transfer zum Locker suchen, im selben Beleg das Lock-Ereignis lesen. 6 h im Speicher."""
    c = _ENTS.get((token, locker))
    if c and time.time() - c[0] < 6 * 3600:
        return c[1]
    import intel
    txs = set()
    try:
        top = _top()
        b = int(ab_block or max(0, top - 4_000_000))
        while b <= top and len(txs) < 6:
            bis = min(top, b + 500_000)
            r = _rpc("eth_getLogs", [{"address": token, "fromBlock": hex(b), "toBlock": hex(bis),
                                      "topics": [TRANSFER, None, "0x" + "0" * 24 + locker[2:]]}]) or []
            txs |= {x.get("transactionHash") for x in r}
            b = bis + 1
    except Exception:
        pass
    u = intel._entsperrung("robinhood", locker, txs) if txs else None
    _ENTS[(token, locker)] = (time.time(), u)
    return u


def _holder_zahl(token):
    """Holder laut Blockscout und die groessten Halter (ohne Burn-Adressen)."""
    out = {}
    import intel

    def bs(pfad):
        # Seit 2.40 zuerst Blockscout Pro (BLOCKSCOUT_API_KEY): der freie Explorer zeigt Servern oft eine Cloudflare-Pruefung
        d = intel._bs("robinhood", pfad)
        if isinstance(d, dict) and d:
            return d
        return requests.get(EXP + pfad, timeout=10, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"}).json()
    try:
        d = bs(f"/api/v2/tokens/{token}")
        if d.get("holders_count") or d.get("holders"):
            out["holders"] = int(d.get("holders_count") or d.get("holders"))
    except Exception:
        pass
    try:
        d = bs(f"/api/v2/tokens/{token}/holders")
        top = []
        for x in d.get("items") or []:
            a = str(((x.get("address") or {}).get("hash")) or "").lower()
            if a in (DEAD, NULL):
                continue
            top.append({"address": a, "raw": int(x.get("value") or 0),
                        "is_contract": bool((x.get("address") or {}).get("is_contract")),
                        "name": (x.get("address") or {}).get("name")})
        out["top"] = top[:20]
    except Exception:
        pass
    return out


# ── Torch-Launches ───────────────────────────────────────────────────────

_LAUNCH = {"ts": 0.0, "liste": None}


def launches(frisch=False):
    """Alle Torch-Launches, aelteste zuerst: [{token, creator, block}]. Cache im kv, inkrementell."""
    if _LAUNCH["liste"] is not None and not frisch and time.time() - _LAUNCH["ts"] < 90:
        return _LAUNCH["liste"]
    stand = {"block": START_BLOCK - 1, "items": []}
    try:
        roh = storage.kv_get("oc_launches")
        if roh:
            stand = json.loads(roh)
    except Exception:
        pass
    top = _top()
    a = stand["block"] + 1
    neu = []
    while a <= top:
        b = min(top, a + FENSTER)
        try:
            for l in _rpc("eth_getLogs", [{"address": FABRIK, "fromBlock": hex(a), "toBlock": hex(b),
                                            "topics": [LAUNCH_TOPIC]}]) or []:
                neu.append({"token": "0x" + l["topics"][1][-40:], "creator": "0x" + l["topics"][2][-40:],
                            "block": int(l["blockNumber"], 16), "tx": l["transactionHash"]})
        except Exception as e:
            log.warning(f"onchain launches {a}-{b}: {e}")
            break
        a = b + 1
    if a > stand["block"] + 1:
        stand["items"] += neu
        stand["block"] = a - 1
        try:
            storage.kv_set("oc_launches", json.dumps(stand))
        except Exception:
            pass
    _LAUNCH.update(ts=time.time(), liste=stand["items"])
    return stand["items"]


def latest_launches(n=5):
    ls = launches()[-max(1, min(int(n), 20)):][::-1]
    toks = [l["token"] for l in ls]
    meta = _meta(toks)
    pr = preise(toks)
    ts = _ts_bloecke([l["block"] for l in ls])
    out = []
    for l in ls:
        m, p = meta.get(l["token"], {}), pr.get(l["token"], {})
        out.append({"symbol": m.get("symbol"), "name": m.get("name"), "token": l["token"],
                    "creator": l["creator"], "launched_utc": _zeit(ts.get(l["block"])),
                    "price_usd": p.get("usd"), "market_cap_usd": p.get("fdv"), "volume_24h_usd": p.get("vol24"),
                    "trade": TORCH_TRADE + l["token"], "tx": EXP + "/tx/" + l["tx"]})
    return {"total_launches_on_torch": len(launches()), "latest": out}


def _zeit(ts):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)) if ts else None


# ── Bestaende ────────────────────────────────────────────────────────────

def _bekannte():
    basis = [t for t in (holder.mint() and holder.mint().lower(), TORCH, USDG, WETH) if t]
    return basis + [l["token"] for l in launches() if l["token"] not in basis]


def holdings(wallet):
    w = _adr(wallet)
    if not w:
        return {"error": "need an 0x wallet address"}
    toks = _bekannte()
    r = _multicall([(t, "0x70a08231" + _pad(w)) for t in toks])
    da = [(t, _u(b)) for t, b in zip(toks, r) if _u(b)]
    meta = _meta([t for t, _ in da])
    pr = preise([t for t, _ in da])
    pos = []
    for t, roh in da:
        m = meta.get(t, {})
        menge = roh / 10 ** int(m.get("decimals") or 18)
        p = (pr.get(t) or {}).get("usd")
        pos.append({"symbol": m.get("symbol"), "token": t, "amount": round(menge, 6),
                    "usd": round(menge * p, 2) if p else None, "price_usd": p})
    pos.sort(key=lambda x: -(x["usd"] or 0))
    eth = None
    try:
        eth = int(_rpc("eth_getBalance", [w, "latest"]), 16) / 1e18
    except Exception:
        pass
    andere = {}
    try:
        d = requests.get("https://app.torchpad.fun/api/portfolio?address=" + w, timeout=15).json()
        for b in d.get("balances") or []:
            if b.get("chain") != "robinhood" and b.get("balance") not in (None, "0"):
                andere[b.get("chainName") or b.get("chain")] = {
                    "symbol": b.get("nativeSymbol"),
                    "amount": round(int(b["balance"]) / 10 ** int(b.get("nativeDecimals") or 18), 6)}
    except Exception:
        pass
    summe = sum(x["usd"] or 0 for x in pos)
    return {"wallet": w, "chain": "Robinhood Chain", "eth": round(eth, 6) if eth is not None else None,
            "tokens": pos[:40], "tokens_value_usd": round(summe, 2),
            "other_chains_native": andere,
            "covered": "ETH, $ODDS, $TORCH, USDG, WETH and every token launched on Torch. "
                       "Tokens from other launchpads: ask with the token address.",
            "explorer": EXP + "/address/" + w}


def activity(wallet, token=None, n=10):
    """Letzte Bewegungen: je Token (ueber 10 Mio. Bloecke). Ohne token: die groessten Bestaende + $ODDS/$TORCH."""
    w = _adr(wallet)
    if not w:
        return {"error": "need an 0x wallet address"}
    if token:
        toks = [_adr(token) or _suche(token)]
        toks = [t for t in toks if t]
    else:
        h = holdings(w)
        toks = [x["token"] for x in (h.get("tokens") or [])[:8]]
        for t in (holder.mint() and holder.mint().lower(), TORCH):
            if t and t not in toks:
                toks.append(t)
    top = _top()
    ereig = []
    for t in toks:
        for richtung, topics in (("in", [TRANSFER, None, "0x" + _pad(w)]), ("out", [TRANSFER, "0x" + _pad(w)])):
            try:
                ls = _rpc("eth_getLogs", [{"address": t, "fromBlock": hex(max(0, top - FENSTER)), "toBlock": hex(top),
                                           "topics": topics}]) or []
            except Exception:
                ls = []
            for l in ls[-n:]:
                gegen = "0x" + (l["topics"][1] if richtung == "in" else l["topics"][2])[-40:]
                ereig.append({"token": t, "dir": richtung, "raw": int(l["data"], 16), "other": gegen,
                              "block": int(l["blockNumber"], 16), "tx": l["transactionHash"]})
    ereig.sort(key=lambda e: -e["block"])
    ereig = ereig[:n]
    meta = _meta(list({e["token"] for e in ereig}))
    pr = preise(list({e["token"] for e in ereig}))
    ts = _ts_bloecke([e["block"] for e in ereig])
    pools = _pool_adressen()
    vertrag = _vertraege([e["other"] for e in ereig if e["other"] not in (DEAD, NULL)])
    out = []
    for e in ereig:
        m = meta.get(e["token"], {})
        menge = e["raw"] / 10 ** int(m.get("decimals") or 18)
        p = (pr.get(e["token"]) or {}).get("usd")
        art = ("burn" if e["other"] in (DEAD, NULL) and e["dir"] == "out" else
               "mint" if e["other"] == NULL else
               ("buy" if e["dir"] == "in" else "sell") if (e["other"] in pools or vertrag.get(e["other"])) else
               ("received" if e["dir"] == "in" else "sent"))
        out.append({"when_utc": _zeit(ts.get(e["block"])), "type": art, "symbol": m.get("symbol"),
                    "amount": round(menge, 4), "usd_now": round(menge * p, 2) if p else None,
                    "counterparty": e["other"], "tx": EXP + "/tx/" + e["tx"]})
    return {"wallet": w, "recent": out,
            "note": "buy/sell = tokens came from or went to a contract (pool, router, launchpad). received/sent = from or to a normal wallet. Window: about the last 11 days."}


def _pool_adressen():
    """Adressen, ueber die auf Robinhood Chain getauscht wird (Uniswap v4 PoolManager, Router)."""
    s = {"0x8366a39cc670b4001a1121b8f6a443a643e40951", "0x340278a0ef7c7e83418771504871619453bd0fa6"}
    for x in os.getenv("ONCHAIN_POOLS", "").split(","):
        if _adr(x):
            s.add(_adr(x))
    return s


# ── Token ────────────────────────────────────────────────────────────────

def _suche(q):
    q = str(q or "").strip().lstrip("$").lower()
    if not q:
        return None
    if _adr(q):
        return _adr(q)
    feste = {"odds": holder.mint() and holder.mint().lower(), "torch": TORCH, "usdg": USDG, "weth": WETH}
    if q in feste:
        return feste[q]
    ls = launches()[-400:][::-1]
    meta = _meta([l["token"] for l in ls])
    for l in ls:
        if (meta.get(l["token"], {}).get("symbol") or "").lower() == q:
            return l["token"]
    # Nicht auf Torch gestartet (z. B. PONS): exakter Ticker auf Robinhood Chain, das Paar mit der meisten Liquiditaet
    try:
        import scan
        beste, liq = None, -1.0
        for p in scan._suche(q):
            b_ = p.get("baseToken") or {}
            if p.get("chainId") != "robinhood" or str(b_.get("symbol") or "").strip().lstrip("$").lower() != q:
                continue
            l_ = float((p.get("liquidity") or {}).get("usd") or 0)
            if l_ > liq:
                beste, liq = str(b_.get("address") or "").lower(), l_
        if beste:
            return beste
    except Exception as e:
        log.warning(f"onchain suche dex: {e}")
    # Zum Schluss nur der exakte Name, nie ein Teilwort (sonst wird aus PONS ein PONSDOG)
    for l in ls:
        if (meta.get(l["token"], {}).get("name") or "").strip().lower() == q:
            return l["token"]
    return None


def token(q):
    t = _suche(q)
    if not t:
        return {"error": "token not found. Give the contract address or the exact ticker of a Torch launch."}
    m = _meta([t]).get(t, {})
    dez = int(m.get("decimals") or 18)
    sup = int(m.get("supply_raw") or 0) / 10 ** dez
    b = _multicall([(t, "0x70a08231" + _pad(DEAD)), (t, "0x70a08231" + _pad(NULL))])
    weg = sum((_u(x) or 0) for x in b) / 10 ** dez
    p = preise([t]).get(t, {})
    chg, url = _veraenderung(t)
    l = next((x for x in launches() if x["token"] == t), None)
    if l is None and t.endswith("1111"):
        l = next((x for x in launches(frisch=True) if x["token"] == t), None)
    lt = _ts_bloecke([l["block"]]).get(l["block"]) if l else None
    extra = {}
    # Alter des Launches
    if lt:
        alter = max(0, time.time() - lt)
        extra["age"] = (f"{int(alter // 60)} min" if alter < 3600 else f"{alter / 3600:.1f} h" if alter < 172800
                        else f"{alter / 86400:.0f} days")
        extra["just_launched"] = alter < 6 * 3600
    # Handel: Kaeufe, Verkaeufe, Volumen je Fenster
    dp = _dex(t)
    if dp:
        tx = dp.get("txns") or {}
        extra["trades"] = {k: {"buys": (tx.get(k) or {}).get("buys"), "sells": (tx.get(k) or {}).get("sells")}
                           for k in ("m5", "h1", "h6", "h24") if tx.get(k)}
        extra["volume_usd"] = {k: v for k, v in (dp.get("volume") or {}).items() if k in ("m5", "h1", "h6", "h24")}
        if not p.get("usd") and dp.get("priceUsd"):
            p = dict(p, usd=float(dp["priceUsd"]), fdv=dp.get("fdv") or dp.get("marketCap"),
                     liq=(dp.get("liquidity") or {}).get("usd"))
    # Holder und Verteilung
    hz = _holder_zahl(t)
    if hz.get("holders") is not None:
        extra["holders"] = hz["holders"]
    if hz.get("top") and sup:
        pools = _pool_adressen()
        # Pools, Torch-Kurve und andere Vertraege zaehlen nicht als Halter, nur Wallets
        echte = [x for x in hz["top"] if x["address"] not in pools and x["address"] != FABRIK and not x.get("is_contract")]
        extra["top10_wallets_pct"] = round(sum(x["raw"] for x in echte[:10]) / 10 ** dez / sup * 100, 2)
        extra["largest_holder_pct"] = round(echte[0]["raw"] / 10 ** dez / sup * 100, 2) if echte else None
        try:
            import intel
            lock = intel.LOCKER_EVM.get("robinhood", {})
        except Exception:
            lock = {}
        extra["top_list"] = [{"address": x["address"], "pct": round(x["raw"] / 10 ** dez / sup * 100, 3),
                              "pool": (x.get("is_contract") or x["address"] in pools or x["address"] == FABRIK) and x["address"] not in lock,
                              "locked": x["address"] in lock, "locker": lock.get(x["address"]),
                              "label": (x.get("name") or "")[:30] or None} for x in hz["top"]]
    # Gesperrte Tokens in bekannten Lockern, auch ohne Holder-Liste
    if sup:
        try:
            import intel
            lock = intel.LOCKER_EVM.get("robinhood", {})
            if lock:
                lb = _multicall([(t, "0x70a08231" + _pad(a)) for a in lock])
                extra["locked"] = [{"address": a, "locker": n, "pct": round((_u(v) or 0) / 10 ** dez / sup * 100, 3)}
                                   for (a, n), v in zip(lock.items(), lb) if (_u(v) or 0) > 0]
                for lk in extra["locked"]:
                    u = _entsperrung_rh(t, lk["address"], l["block"] if l else None)
                    if u:
                        lk["unlock_ts"] = u
        except Exception:
            pass
    # Dev: was haelt der Ersteller noch
    if l and sup:
        try:
            dev = _u(_multicall([(t, "0x70a08231" + _pad(l["creator"]))])[0]) or 0
            extra["creator_holds_pct"] = round(dev / 10 ** dez / sup * 100, 3)
        except Exception:
            pass
    # Torchs eigene Sicht: Fortschritt bis zur Graduation
    try:
        tl = next((x for x in _torch_liste() if str(x.get("address") or "").lower() == t), None)
        if tl:
            extra["torch"] = {"graduated": bool(tl.get("graduated")),
                              "progress_to_graduation_pct": 100 if tl.get("graduated") else round((tl.get("progress") or 0) * 100, 1),
                              "market_cap_usd": tl.get("marketCapUsd"), "volume_24h_usd": tl.get("volume24hUsd")}
    except Exception:
        pass
    return {**extra, "symbol": m.get("symbol"), "name": m.get("name"), "token": t, "chain": "Robinhood Chain",
            "total_supply": round(sup, 2), "burned": round(weg, 2),
            "burned_pct_of_supply": round(weg / sup * 100, 4) if sup else None,
            "price_usd": p.get("usd"), "market_cap_usd": p.get("fdv"), "liquidity_usd": p.get("liq"),
            "volume_24h_usd": p.get("vol24"), "price_change_pct": chg or None,
            "launched_on_torch": bool(l), "launched_utc": _zeit(lt), "creator": l["creator"] if l else None,
            "creator_explorer": EXP + "/address/" + l["creator"] if l else None,
            "launch_tx": EXP + "/tx/" + l["tx"] if l and l.get("tx") else None,
            "chart": url, "trade": TORCH_TRADE + t if l or t in (TORCH,) else None,
            "explorer": EXP + "/token/" + t}


# ── Torch-Markt aus Torchs eigener Uebersicht (seit 2.33) ────────────────
# app.torchpad.fun/api/discover/venue liefert alle Torch-Coins mit Fortschritt bis zur
# Graduation und dem Flag graduated. Wann ein Coin graduiert ist, sagt Torch nicht, deshalb
# merken wir uns den ersten Moment, in dem wir ihn als graduiert sehen.

_TM = {"ts": 0.0, "d": None}


def _torch_liste():
    if _TM["d"] is not None and time.time() - _TM["ts"] < 90:
        return _TM["d"]
    r = requests.get("https://app.torchpad.fun/api/discover/venue", params={"venue": "torch", "list": "new"},
                     timeout=15, headers={"Accept": "application/json"})
    r.raise_for_status()
    d = r.json().get("tokens") or []
    _TM.update(ts=time.time(), d=d)
    try:
        import storage
        alt = json.loads(storage.kv_get("torch_grad_seen") or "null")
        erst = alt is None
        alt = alt or {}
        jetzt = int(time.time())
        neu = False
        for x in d:
            a = str(x.get("address") or "").lower()
            if x.get("graduated") and a and a not in alt:
                alt[a] = None if erst else jetzt
                neu = True
        if neu or erst:
            storage.kv_set("torch_grad_seen", json.dumps(alt))
        _TM["grad"] = alt
    except Exception:
        _TM["grad"] = _TM.get("grad") or {}
    return d


def torch_markt(liste="new", n=8):
    """new, trending, top, graduated (zuletzt graduiert), graduating (kurz davor)."""
    try:
        d = _torch_liste()
    except Exception as e:
        return {"error": f"Torch did not answer: {str(e)[:80]}"}
    liste = str(liste or "new").lower()
    grad = _TM.get("grad") or {}
    if liste == "trending":
        w = sorted(d, key=lambda x: -(x.get("volume24hUsd") or 0))
    elif liste == "top":
        w = sorted(d, key=lambda x: -(x.get("marketCapUsd") or 0))
    elif liste == "graduated":
        w = [x for x in d if x.get("graduated")]
        w.sort(key=lambda x: (grad.get(str(x.get("address")).lower()) or 0, str(x.get("createdAt") or "")), reverse=True)
    elif liste == "graduating":
        w = sorted([x for x in d if not x.get("graduated") and (x.get("progress") or 0) > 0],
                   key=lambda x: -(x.get("progress") or 0))
    else:
        liste = "new"
        w = sorted(d, key=lambda x: str(x.get("createdAt") or ""), reverse=True)
    out = []
    for x in w[:max(1, min(int(n or 8), 20))]:
        a = str(x.get("address") or "")
        g = grad.get(a.lower())
        out.append({"symbol": x.get("symbol"), "name": x.get("name"), "token": a,
                    "market_cap_usd": round(x.get("marketCapUsd") or 0), "volume_24h_usd": round(x.get("volume24hUsd") or 0),
                    "change_24h_pct": x.get("change24hPercent"), "launched_utc": x.get("createdAt"),
                    "graduated": bool(x.get("graduated")),
                    "progress_to_graduation_pct": round((x.get("progress") or 0) * 100, 1) if not x.get("graduated") else 100,
                    "graduated_seen_utc": _zeit(g) if g else None,
                    "trade": TORCH_TRADE + a})
    return {"list": liste, "coins_on_torch": len(d), "graduated_total": sum(1 for x in d if x.get("graduated")),
            "coins": out, "source": "app.torchpad.fun",
            "note": "Graduation is Torch's milestone for a coin. graduated_seen_utc is when OddsEngine first saw it graduated."}


def burned(q):
    r = token(q)
    if r.get("error"):
        return r
    return {k: r.get(k) for k in ("symbol", "token", "total_supply", "burned", "burned_pct_of_supply",
                                  "price_usd")} | {"burn_addresses": [DEAD, NULL],
                                                    "burned_usd": round((r.get("burned") or 0) * (r.get("price_usd") or 0), 2)}
