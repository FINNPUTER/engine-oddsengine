"""
scan.py: der CA-Report fuer jede Chain. Seit 2.39.

Ein Coin, egal wo: Solana, Base, Ethereum, BNB Chain, Arbitrum, Robinhood Chain.

  analyse(q)        Chain erkennen, alles einsammeln, Urteil bilden, Karte fuer Telegram bauen
  trades(...)       die letzten Kaeufe und Verkaeufe eines Paars (fuer die Live-Seite)
  nachfassen()      aus motor.tick: fertige Tiefen-Scans nachliefern

Schichten:
  Markt       DexScreener (jede Chain, ohne Schluessel): Preis, MC, Liquiditaet, Volumen,
              Kaeufe/Verkaeufe je Fenster, Alter, Boerse, Chart
  Sicherheit  EVM: GoPlus (Honeypot, Steuern, Mint, Owner, LP-Lock, Holder, Creator).
              Solana: Mint- und Freeze-Rechte direkt vom Mint-Konto (RPC, HELIUS_API_KEY wenn da).
              Robinhood: onchain.token (Torch, Dev, Burn, Top 10).
  Tiefe       intel.py, eigene Erkennung: erste Kaeufer, Bundles, Sniper, frische Wallets, Cluster,
              Dev-Historie, bewertete Kaeufer aus der eigenen Wallet-Datenbank. Kein fremder Dienst.
"""
import json
import os
import re
import time

import requests

import storage
from logger import log

UA = {"User-Agent": "Mozilla/5.0 oddsengine", "Accept": "application/json"}
SEITE = os.getenv("ENGINE_PUBLIC_URL", "https://engine.oddsengine.live").rstrip("/")

# key: (Name, DexScreener-Id, GeckoTerminal-Netz, GoPlus-Chain-Id, Explorer, Token-Pfad, Adress-Pfad,
#       Tx-Pfad, Nativ, EVM-Chain-Id, Tiefen-Name)
KETTEN = {
    "solana":    ("Solana", "solana", "solana", None, "https://solscan.io", "/token/", "/account/", "/tx/", "SOL", None, "solana"),
    "ethereum":  ("Ethereum", "ethereum", "eth", "1", "https://etherscan.io", "/token/", "/address/", "/tx/", "ETH", 1, "ethereum"),
    "base":      ("Base", "base", "base", "8453", "https://basescan.org", "/token/", "/address/", "/tx/", "ETH", 8453, "base"),
    "bsc":       ("BNB Chain", "bsc", "bsc", "56", "https://bscscan.com", "/token/", "/address/", "/tx/", "BNB", 56, None),
    "arbitrum":  ("Arbitrum", "arbitrum", "arbitrum", "42161", "https://arbiscan.io", "/token/", "/address/", "/tx/", "ETH", 42161, None),
    "robinhood": ("Robinhood Chain", "robinhood", "robinhood", None, "https://robinhoodchain.blockscout.com", "/token/", "/address/", "/tx/", "ETH", 4663, "robinhood"),
}
DEX_ZU = {v[1]: k for k, v in KETTEN.items()}
# Eigene Coins (ODDS, TORCH): nie als Nachahmer markieren. OWN_TOKENS (Komma-Liste) erweitert das.
EIGENE = {"0x9c87b4cfa21266d1638f437c96dbe1012cb81111", "0x000050e18ffa3f8f7ad170eb9cef160eb3ed1111"} | {
    x.strip().lower() for x in os.getenv("OWN_TOKENS", "").split(",") if x.strip()}
NAMEN = {"sol": "solana", "solana": "solana", "eth": "ethereum", "ethereum": "ethereum", "mainnet": "ethereum",
         "base": "base", "bsc": "bsc", "bnb": "bsc", "binance": "bsc", "arb": "arbitrum", "arbitrum": "arbitrum",
         "robinhood": "robinhood", "rh": "robinhood", "torch": "robinhood"}
DEX_NAMEN = {"pumpfun": "pump.fun curve", "pumpswap": "PumpSwap", "raydium": "Raydium", "meteora": "Meteora",
             "orca": "Orca", "uniswap": "Uniswap", "aerodrome": "Aerodrome", "pancakeswap": "PancakeSwap",
             "sushiswap": "SushiSwap", "camelot": "Camelot", "launchlab": "LaunchLab", "moonshot": "Moonshot",
             "boop": "boop", "bonk": "letsbonk", "believe": "Believe", "meteoradbc": "Meteora DBC"}

SOL_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
EVM_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
GRUEN, GELB, ROT = "🟢", "🟠", "🔴"
AMPEL = {"green": GRUEN, "amber": GELB, "red": ROT}


def _f(x):
    try:
        if x is None or x == "":
            return None
        return float(x)
    except Exception:
        return None


def _ff(k, d):
    try:
        return float(os.getenv(k, "") or d)
    except Exception:
        return float(d)


# ── Zahlen lesbar ────────────────────────────────────────────────────────

def usd(v):
    v = _f(v)
    if v is None:
        return "n/a"
    a = abs(v)
    if a >= 1e9:
        return f"${v/1e9:.2f}B"
    if a >= 1e6:
        return f"${v/1e6:.2f}M"
    if a >= 1e3:
        return f"${v/1e3:.1f}K"
    if a == 0:
        return "$0"
    return f"${v:,.0f}" if a >= 10 else f"${v:,.2f}"


def preis(v):
    v = _f(v)
    if v is None:
        return "n/a"
    if v >= 1:
        return f"${v:,.4f}".rstrip("0").rstrip(".")
    if v == 0:
        return "$0"
    s = f"{v:.12f}".rstrip("0")
    # 0.000001234 -> $0.0₅1234 waere huebsch, liest aber nicht jeder. Vier gueltige Stellen reichen.
    nach = s.split(".")[1]
    nullen = len(nach) - len(nach.lstrip("0"))
    return "$" + f"{v:.{nullen + 4}f}".rstrip("0")


def pct(v, plus=True):
    v = _f(v)
    if v is None:
        return "n/a"
    return (f"{v:+.1f}%" if plus else f"{v:.1f}%").replace(".0%", "%")


def zahl(v):
    """12.345 -> '12.3', 2.0 -> '2', 0.04 -> '0.04'."""
    v = _f(v)
    if v is None:
        return "?"
    if abs(v) < 1 and v != 0:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{v:.1f}".rstrip("0").rstrip(".")


def balken(p, n=10):
    p = max(0.0, min(100.0, _f(p) or 0.0))
    voll = int(round(p / 100 * n))
    return "▰" * voll + "▱" * (n - voll)


def kurz(a):
    a = str(a or "")
    return a[:4] + "…" + a[-4:] if len(a) > 12 else a


def alter_text(sek):
    if sek is None:
        return None
    sek = max(0, sek)
    if sek < 3600:
        return f"{int(sek // 60)} min"
    if sek < 172800:
        return f"{sek / 3600:.1f} h".replace(".0 h", " h")
    if sek < 86400 * 60:
        return f"{int(sek // 86400)} days"
    return f"{int(sek // (86400 * 30))} months"


def explorer(chain, art, a):
    k = KETTEN.get(chain)
    if not k or not a:
        return None
    pfad = {"token": k[5], "address": k[6], "tx": k[7]}[art]
    return k[4] + pfad + a


# ── Eingabe verstehen ────────────────────────────────────────────────────

def lesen(q):
    """Aus Text, Link oder Ticker: (chain_hinweis, adresse_oder_ticker)."""
    q = str(q or "").strip()
    hinweis = None
    m = re.search(r"dexscreener\.com/([a-z0-9]+)/", q, re.I) or re.search(r"geckoterminal\.com/([a-z0-9_]+)/", q, re.I)
    if m:
        hinweis = NAMEN.get(m.group(1).lower()) or DEX_ZU.get(m.group(1).lower())
    if re.search(r"pump\.fun|solscan|birdeye|photon-sol|bullx|axiom\.trade|gmgn\.ai/sol", q, re.I):
        hinweis = "solana"
    if re.search(r"basescan|/base/", q, re.I):
        hinweis = hinweis or "base"
    if re.search(r"etherscan\.io", q, re.I):
        hinweis = hinweis or "ethereum"
    if re.search(r"bscscan", q, re.I):
        hinweis = hinweis or "bsc"
    if re.search(r"torchpad|robinhoodchain", q, re.I):
        hinweis = hinweis or "robinhood"
    if ":" in q and not q.lower().startswith("http"):
        a, b = q.split(":", 1)
        if a.lower() in NAMEN:
            hinweis, q = NAMEN[a.lower()], b.strip()
    m = EVM_RE.search(q)
    if m:
        return hinweis, m.group(0).lower()
    m = SOL_RE.search(q)
    if m and not re.fullmatch(r"[a-z]+", m.group(0)):
        return "solana", m.group(0)
    return hinweis, q.lstrip("$").strip()


# ── Markt: DexScreener ───────────────────────────────────────────────────

_DEX = {}


def _paare(adr):
    c = _DEX.get(adr)
    if c and time.time() - c[0] < 20:
        return c[1]
    ps = []
    try:
        d = requests.get("https://api.dexscreener.com/latest/dex/tokens/" + adr, headers=UA, timeout=12).json()
        ps = d.get("pairs") or []
    except Exception as e:
        log.debug(f"scan dex {adr}: {e}")
        if c:
            return c[1]
    _DEX[adr] = (time.time(), ps)
    return ps


def _liq(p):
    return float((p.get("liquidity") or {}).get("usd") or 0)


def chain_waehlen(adr, paare, hinweis=None):
    """Auf welcher Chain lebt der Coin? Hinweis gewinnt, sonst die Chain mit der meisten Liquiditaet,
    bei 0x ohne Paar: Robinhood (Torch-Kurve), bei base58 immer Solana."""
    eigene = [p for p in paare if str(p.get("baseToken", {}).get("address", "")).lower() == adr.lower()] or paare
    if hinweis and hinweis in KETTEN:
        return hinweis
    if not adr.startswith("0x"):
        return "solana"
    summe = {}
    for p in eigene:
        k = DEX_ZU.get(p.get("chainId"))
        if k:
            summe[k] = summe.get(k, 0) + _liq(p)
    if summe:
        return max(summe, key=summe.get)
    return "robinhood"


def markt(adr, chain, paare):
    ps = [p for p in paare if DEX_ZU.get(p.get("chainId")) == chain]
    eigene = [p for p in ps if str(p.get("baseToken", {}).get("address", "")).lower() == adr.lower()]
    ps = sorted(eigene or ps, key=lambda p: -_liq(p))
    if not ps:
        return {}
    p = ps[0]
    alt = min([x.get("pairCreatedAt") for x in ps if x.get("pairCreatedAt")] or [None]) if ps else None
    info = p.get("info") or {}
    links = {}
    for w in info.get("websites") or []:
        if w.get("url"):
            links.setdefault("website", w["url"])
    for s in info.get("socials") or []:
        if s.get("url") and s.get("type"):
            links.setdefault(s["type"], s["url"])
    # Volumen und Kaeufe/Verkaeufe ueber ALLE Pools des Coins auf dieser Chain (seit 2.44), nicht nur das groesste Paar
    zaehl = eigene or [p]
    vol, tx = {}, {}
    for k in ("m5", "h1", "h6", "h24"):
        vs = [_f((x.get("volume") or {}).get(k)) for x in zaehl]
        vol[k] = round(sum(v for v in vs if v), 2) if any(v is not None for v in vs) else None
        ts = [(x.get("txns") or {}).get(k) for x in zaehl if (x.get("txns") or {}).get(k)]
        if ts:
            tx[k] = {"buys": sum(int(t.get("buys") or 0) for t in ts), "sells": sum(int(t.get("sells") or 0) for t in ts)}
    return {"price": _f(p.get("priceUsd")), "mcap": _f(p.get("marketCap")) or _f(p.get("fdv")), "fdv": _f(p.get("fdv")),
            "liq": _liq(p) or None, "vol": vol, "txns": tx,
            "change": {k: _f(v) for k, v in (p.get("priceChange") or {}).items() if k in ("m5", "h1", "h6", "h24")},
            "pair": p.get("pairAddress"), "url": p.get("url"), "dex": p.get("dexId"),
            "quote": (p.get("quoteToken") or {}).get("symbol"),
            "symbol": (p.get("baseToken") or {}).get("symbol"), "name": (p.get("baseToken") or {}).get("name"),
            "image": info.get("imageUrl"), "created_ms": alt, "links": links, "pairs": len(ps),
            "liq_all": round(sum(_liq(x) for x in ps), 2)}


# ── Sicherheit EVM: GoPlus ───────────────────────────────────────────────

_GP = {}


def goplus(chain, adr):
    gid = KETTEN.get(chain, (None,) * 4)[3]
    if not gid:
        return None
    c = _GP.get((chain, adr))
    if c and time.time() - c[0] < 120:
        return c[1]
    r = None
    try:
        d = requests.get(f"https://api.gopluslabs.io/api/v1/token_security/{gid}",
                         params={"contract_addresses": adr}, headers=UA, timeout=15).json()
        res = d.get("result") or {}
        r = res.get(adr.lower()) or (list(res.values())[0] if res else None)
    except Exception as e:
        log.debug(f"scan goplus {adr}: {e}")
    _GP[(chain, adr)] = (time.time(), r)
    return r


def _ja(v):
    return str(v) == "1"


def sicherheit_evm(g):
    if not g:
        return {}
    out = {"honeypot": _ja(g.get("is_honeypot")) if g.get("is_honeypot") not in (None, "") else None,
           "buy_tax": _f(g.get("buy_tax")), "sell_tax": _f(g.get("sell_tax")),
           "mintable": _ja(g.get("is_mintable")), "open_source": _ja(g.get("is_open_source")) if g.get("is_open_source") not in (None, "") else None,
           "proxy": _ja(g.get("is_proxy")), "take_back": _ja(g.get("can_take_back_ownership")),
           "hidden_owner": _ja(g.get("hidden_owner")), "pausable": _ja(g.get("transfer_pausable")),
           "blacklist": _ja(g.get("is_blacklisted")), "cannot_sell_all": _ja(g.get("cannot_sell_all")),
           "slippage_mod": _ja(g.get("slippage_modifiable")),
           "owner": g.get("owner_address") or None, "creator": g.get("creator_address") or None,
           "creator_pct": (_f(g.get("creator_percent")) or 0) * 100 if g.get("creator_percent") not in (None, "") else None,
           "holders": int(_f(g.get("holder_count")) or 0) or None}
    for k in ("buy_tax", "sell_tax"):
        if out[k] is not None:
            out[k] = round(out[k] * 100, 2)
    tot = 0.0
    for lp in g.get("lp_holders") or []:
        p = _f(lp.get("percent")) or 0
        a = str(lp.get("address") or "").lower()
        if str(lp.get("is_locked")) == "1" or a.endswith("dead") or a == "0x" + "0" * 40:
            tot += p
    if g.get("lp_holders"):
        out["lp_locked_pct"] = round(min(1.0, tot) * 100, 1)
    out["owner_renounced"] = (not out["owner"]) or out["owner"] in ("0x" + "0" * 40, "0x000000000000000000000000000000000000dead")
    top = []
    for h in g.get("holders") or []:
        top.append({"address": str(h.get("address") or "").lower(), "pct": round((_f(h.get("percent")) or 0) * 100, 2),
                    "pool": bool(h.get("is_contract")) and not str(h.get("is_locked")) == "1",
                    "locked": str(h.get("is_locked")) == "1", "label": (h.get("tag") or "")[:30] or None})
    out["top"] = top[:15]
    return out


# ── Sicherheit Solana: Mint-Konto ────────────────────────────────────────

_SM = {}


def sicherheit_sol(adr):
    c = _SM.get(adr)
    if c and time.time() - c[0] < 300:
        return c[1]
    out = {}
    try:
        import holder
        r = holder._sol_rpc("getAccountInfo", [adr, {"encoding": "jsonParsed"}])
        v = (r or {}).get("value") or {}
        d = (v.get("data") or {})
        info = ((d.get("parsed") or {}).get("info") or {}) if isinstance(d, dict) else {}
        if info:
            out = {"mint_revoked": info.get("mintAuthority") is None, "freeze_revoked": info.get("freezeAuthority") is None,
                   "decimals": info.get("decimals"), "supply_raw": info.get("supply"),
                   "token2022": (d.get("program") == "spl-token-2022")}
            for e in info.get("extensions") or []:
                art = e.get("extension")
                st = e.get("state") or {}
                if art == "transferFeeConfig":
                    bps = ((st.get("newerTransferFee") or {}).get("transferFeeBasisPoints")
                           or (st.get("olderTransferFee") or {}).get("transferFeeBasisPoints") or 0)
                    out["transfer_fee_pct"] = round(float(bps) / 100, 2)
                elif art == "permanentDelegate" and st.get("delegate"):
                    out["permanent_delegate"] = True
                elif art == "nonTransferable":
                    out["non_transferable"] = True
                elif art == "transferHook" and st.get("programId"):
                    out["transfer_hook"] = True
                elif art == "tokenMetadata":
                    out["symbol"] = st.get("symbol")
                    out["name"] = st.get("name")
                    out["uri"] = st.get("uri")
    except Exception as e:
        log.debug(f"scan sol mint {adr}: {e}")
    _SM[adr] = (time.time(), out)
    return out


def top_sol(adr, supply_raw):
    """Ohne Tiefen-Schicht: die groessten Token-Konten (Pools eingeschlossen)."""
    try:
        import holder
        r = holder._sol_rpc("getTokenLargestAccounts", [adr])
        acc = (r or {}).get("value") or []
        sup = float(supply_raw or 0)
        if not sup:
            return []
        return [{"address": a.get("address"), "pct": round(float(a.get("amount") or 0) / sup * 100, 2), "account": True}
                for a in acc[:12]]
    except Exception:
        return []


# ── Urteil ───────────────────────────────────────────────────────────────

def urteil(r):
    gut, schlecht, kritisch = [], [], []
    s, sp, m = r.get("safety") or {}, r.get("supply") or {}, r.get("market") or {}
    if s.get("honeypot"):
        kritisch.append("Honeypot: buys work, sells fail")
    if s.get("cannot_sell_all"):
        kritisch.append("You cannot sell your whole bag")
    st, bt = s.get("sell_tax"), s.get("buy_tax")
    if st is not None and st >= 25:
        kritisch.append(f"Sell tax {zahl(st)}%")
    elif (st or 0) >= 6 or (bt or 0) >= 6:
        schlecht.append(f"Tax {zahl(bt or 0)}% buy / {zahl(st or 0)}% sell")
    if s.get("freeze_revoked") is False:
        kritisch.append("Freeze authority is still active (your tokens can be frozen)")
    if s.get("permanent_delegate"):
        kritisch.append("Permanent delegate: someone can move your tokens")
    if s.get("take_back") or s.get("hidden_owner"):
        kritisch.append("Owner can take control back" if s.get("take_back") else "Hidden owner in the contract")
    if s.get("mint_revoked") is False or s.get("mintable"):
        schlecht.append("Supply can still be minted")
    if s.get("pausable") or s.get("blacklist"):
        schlecht.append("Owner can pause or blacklist transfers")
    if (s.get("transfer_fee_pct") or 0) >= 3:
        schlecht.append(f"Transfer fee {zahl(s['transfer_fee_pct'])}%")
    if s.get("open_source") is False:
        schlecht.append("Contract not verified")
    lp = s.get("lp_locked_pct")
    jung = (r.get("age_sec") or 0) < 30 * 86400 or (m.get("mcap") or 0) < 5e6
    if lp is not None and m.get("liq") and lp < 50 and r.get("stage_curve") is not True and jung:
        schlecht.append(f"Only {zahl(lp)}% of LP locked or burned")
    b = sp.get("bundled_pct")
    if b is not None:
        if b >= 25:
            kritisch.append(f"{zahl(b)}% of supply sits in bundled wallets")
        elif b >= 10:
            schlecht.append(f"{zahl(b)}% bundled")
        elif b < 3:
            gut.append("No real bundle")
    fr = sp.get("fresh_pct")
    if fr is not None and fr >= 30:
        schlecht.append(f"{zahl(fr)}% of early buyers are fresh wallets")
    sf = sp.get("same_funder") or 0
    cl = sp.get("clusters") or 0
    if sf >= 2:
        schlecht.append(f"{sf} wallet groups funded by the same source")
    elif cl >= 2:
        schlecht.append(f"{cl} wallet groups that bought together")
    if (sp.get("snipers") or 0) >= 5:
        schlecht.append(f"{sp['snipers']} snipers in the first block")
    t10 = sp.get("top10_pct")
    if t10 is not None:
        if t10 >= 50:
            schlecht.append(f"Top 10 wallets hold {zahl(t10)}%")
        elif t10 <= 20:
            gut.append(f"Top 10 hold only {zahl(t10)}%")
    lg = sp.get("largest_pct")
    if lg is not None and lg >= 10 and not sp.get("largest_locked"):
        schlecht.append(f"One wallet holds {zahl(lg)}%")
    dv = r.get("dev") or {}
    if (dv.get("holds_pct") or 0) >= 5:
        schlecht.append(f"Dev still holds {zahl(dv['holds_pct'])}%")
    elif dv.get("holds_pct") is not None and dv["holds_pct"] < 0.5 and dv.get("address"):
        gut.append("Dev holds nothing")
    if (dv.get("launches_seen") or 0) >= 3 and not dv.get("launches_ran"):
        schlecht.append(f"Dev launched {dv['launches_seen']} coins before, none ran")
    elif dv.get("launches_ran"):
        gut.append(f"Dev had {dv['launches_ran']} earlier coin{'s' if dv['launches_ran'] != 1 else ''} that ran")
    if s.get("mint_revoked") and s.get("freeze_revoked"):
        gut.append("Mint and freeze revoked")
    if s.get("honeypot") is False and (st or 0) < 1 and (bt or 0) < 1 and not s.get("mintable"):
        gut.append("No honeypot, no tax")
    if lp is not None and lp >= 90:
        gut.append(f"LP {zahl(lp)}% locked or burned")
    if (sp.get("locked_pct") or 0) >= 1:
        bis = sp.get("unlock_ts")
        if bis and bis - time.time() < 7 * 86400:
            schlecht.append(f"{zahl(sp['locked_pct'])}% of supply unlocks {('in ' + str(max(0, int((bis - time.time()) // 86400))) + ' days') if bis > time.time() else 'now'}")
        else:
            gut.append(f"{zahl(sp['locked_pct'])}% of supply locked" + (f" until {time.strftime('%d %b %Y', time.gmtime(bis))}" if bis else ""))
    # Weiterleitungen (seit 2.46): Tokens ohne Verkauf an andere Wallets geschickt
    fw = r.get("forwards") or {}
    if fw.get("count"):
        dfp = fw.get("dev_forwarded_pct") or 0
        if fw.get("from_dev") and dfp >= 1:
            (kritisch if dfp >= 5 else schlecht).append(f"Dev sent {zahl(dfp)}% of supply to other wallets instead of selling")
        if (fw.get("still_held_pct") or 0) >= 3:
            schlecht.append(f"Hidden holders: wallets that got forwarded tokens still hold {zahl(fw['still_held_pct'])}%")
        if fw.get("collectors_elsewhere"):
            n_, mx = fw["collectors_elsewhere"], fw.get("collectors_elsewhere_max") or 0
            schlecht.append(f"{n_} wallet{'s' if n_ != 1 else ''} collecting forwarded supply did the same on other coins"
                            + (f" (one on {mx})" if mx > 1 else ""))
        if (fw.get("to_fresh") or 0) >= 3:
            schlecht.append(f"Tokens forwarded to {fw['to_fresh']} fresh wallets")
    elif fw.get("checked") and fw.get("count") == 0:
        gut.append("No tokens forwarded to hidden wallets")
    sm = r.get("smart") or {}
    if (sm.get("found") or 0) > 0:
        gut.append(f"{sm['found']} smart wallet{'s' if sm['found'] != 1 else ''} among the first buyers")
    liq = m.get("liq")
    if liq is not None and liq < 5000 and r.get("stage_curve") is not True:
        schlecht.append(f"Thin liquidity ({usd(liq)})")
    h1 = (m.get("txns") or {}).get("h1") or {}
    kb, vk = h1.get("buys") or 0, h1.get("sells") or 0
    if kb + vk >= 20:
        if vk > kb * 1.6:
            schlecht.append(f"Sells dominate the last hour ({vk} vs {kb} buys)")
        elif kb > vk * 1.6:
            gut.append(f"Buyers lead the last hour ({kb} vs {vk})")
    tw = r.get("twins") or {}
    if tw.get("likely_copy") and str(r.get("address") or "").lower() not in EIGENE:
        schlecht.append(f"Copycat risk: an older, bigger ${r.get('symbol')} exists")
    if r.get("renamed"):
        schlecht.append("Name on chain differs from the listing")
    sc = r.get("risk_score")
    lv = str(r.get("risk_level") or "").upper()
    for n in r.get("notes") or []:
        nl = n.lower()
        if "bundle pattern" in nl or "forwarded" in nl or "sniper pattern" in nl:
            schlecht.append(n[0].upper() + n[1:])
            break
    if lv in ("HIGH", "CRITICAL", "DANGER") and not kritisch:
        kritisch.append("Our scanner rates it high risk")
    if kritisch:
        stufe = "red"
    elif len(schlecht) >= 3 or (sc is not None and sc < 50):
        stufe = "red" if len(schlecht) >= 5 else "amber"
    elif schlecht or (sc is not None and sc < 70) or lv == "MEDIUM":
        stufe = "amber"
    else:
        stufe = "green"
    titel = {"green": "Looks clean so far", "amber": "Mixed, check the flags", "red": "High risk"}[stufe]
    vorne = kritisch + schlecht
    if stufe == "green":
        grund = ", ".join(gut[:3]) or "Nothing stands out in the data"
    elif gut and vorne:
        grund = f"{', '.join(gut[:2])}. But {vorne[0][0].lower() + vorne[0][1:]}" + (f", and {vorne[1][0].lower() + vorne[1][1:]}" if len(vorne) > 1 else "")
    else:
        grund = ", ".join(vorne[:3])
    return {"level": stufe, "title": titel, "reason": grund + ".", "good": gut, "bad": vorne}


# ── Alles zusammen ───────────────────────────────────────────────────────

_CACHE = {}


def analyse(q, deep=None, frisch=False):
    """Der Report. deep=None: Tiefen-Scan automatisch fuer junge oder kleine Coins starten."""
    hinweis, a = lesen(q)
    if not a:
        return {"ok": False, "error": "Give me a contract address, a chart link or a ticker."}
    gleichnamige = None
    if not (a.startswith("0x") or SOL_RE.fullmatch(a)):
        f = ticker_finden(a, hinweis)
        if f and f.get("ambiguous"):
            return {"ok": False, "ambiguous": True, "symbol": f["symbol"], "candidates": f["candidates"],
                    "error": f"There are several coins called ${f['symbol']}. Send the contract address of the one you mean."}
        if f and f.get("major"):
            return {"ok": False, "major": True, "error": f"{f['symbol']} is a major coin with its own chain, not a token contract. Use the price and chart tools for it."}
        if not f:
            return {"ok": False, "error": f"I could not find '{a}'. Paste the contract address (works for Solana, Base, Ethereum, BNB Chain, Arbitrum and Robinhood Chain)."}
        a, hinweis, gleichnamige = f["address"], f["chain"], f.get("others")
    ck = (hinweis, a)
    c = _CACHE.get(ck)
    if c and not frisch and time.time() - c[0] < 25 and deep is not True:
        return c[1]
    paare = _paare(a)
    chain = chain_waehlen(a, paare, hinweis)
    mk = markt(a, chain, paare)
    k = KETTEN[chain]
    r = {"ok": True, "chain": chain, "chain_name": k[0], "address": a, "symbol": mk.get("symbol"), "name": mk.get("name"),
         "image": mk.get("image"), "market": mk, "safety": {}, "supply": {}, "dev": {}, "smart": {},
         "links": {"chart": mk.get("url"), "explorer": explorer(chain, "token", a),
                   "page": f"{SEITE}/#token={chain}:{a}"}}
    if gleichnamige:
        r["same_ticker_elsewhere"] = gleichnamige
    weitere = sorted({DEX_ZU.get(p.get("chainId")) for p in paare if DEX_ZU.get(p.get("chainId")) and DEX_ZU.get(p.get("chainId")) != chain})
    if weitere:
        r["also_on"] = [KETTEN[x][0] for x in weitere]
    if mk.get("created_ms"):
        r["age_sec"] = int(time.time() - mk["created_ms"] / 1000)
    dex = mk.get("dex")
    if dex:
        r["venue"] = DEX_NAMEN.get(dex, dex.replace("-", " ").title() if dex.islower() else dex)
        r["stage_curve"] = dex in ("pumpfun", "launchlab", "moonshot", "meteoradbc", "boop", "bonk", "believe")
    # Sicherheit je Chain
    if chain == "solana":
        s = sicherheit_sol(a)
        r["safety"].update({x: s.get(x) for x in ("mint_revoked", "freeze_revoked", "transfer_fee_pct",
                                                   "permanent_delegate", "non_transferable", "transfer_hook") if s.get(x) is not None})
        r["symbol"] = r["symbol"] or s.get("symbol")
        r["name"] = r["name"] or s.get("name")
        sol_supply = s.get("supply_raw")
    elif chain == "robinhood":
        try:
            import onchain
            t = onchain.token(a)
        except Exception as e:
            t = {"error": str(e)}
        if not t.get("error"):
            r["symbol"] = r["symbol"] or t.get("symbol")
            r["name"] = r["name"] or t.get("name")
            if t.get("creator"):
                r["dev"].update(address=t["creator"], holds_pct=t.get("creator_holds_pct"),
                                explorer=t.get("creator_explorer"))
            if t.get("holders") is not None:
                r["supply"]["holders"] = t["holders"]
            if t.get("top10_wallets_pct") is not None:
                r["supply"]["top10_pct"] = t["top10_wallets_pct"]
                r["supply"]["largest_pct"] = t.get("largest_holder_pct")
            if t.get("top_list"):
                r["supply"]["top"] = t["top_list"][:15]
            for lk in t.get("locked") or []:
                tops = r["supply"].setdefault("top", [])
                if not any(x.get("address") == lk["address"] for x in tops):
                    tops.append({"address": lk["address"], "pct": lk["pct"], "pool": False, "locked": True, "locker": lk["locker"],
                                 "unlock_ts": lk.get("unlock_ts")})
                else:
                    for x in tops:
                        if x.get("address") == lk["address"] and lk.get("unlock_ts"):
                            x.update(locked=True, locker=lk["locker"], unlock_ts=lk["unlock_ts"], pool=False)
            if t.get("burned_pct_of_supply"):
                r["burned_pct"] = t["burned_pct_of_supply"]
            if t.get("torch"):
                r["torch"] = t["torch"]
                if not t["torch"].get("graduated"):
                    r["stage_curve"] = True
                    r["venue"] = "Torch curve"
                elif not r.get("venue"):
                    r["venue"] = "Torch, graduated"
            if t.get("launched_on_torch") and t.get("launched_utc") and not r.get("age_sec"):
                try:
                    import calendar
                    r["age_sec"] = int(time.time() - calendar.timegm(time.strptime(t["launched_utc"], "%Y-%m-%d %H:%M UTC")))
                except Exception:
                    pass
            if not mk.get("price") and t.get("price_usd"):
                mk.update(price=t.get("price_usd"), mcap=t.get("market_cap_usd"), liq=t.get("liquidity_usd"))
            r["links"]["trade_torch"] = t.get("trade")
            if t.get("creator"):
                try:
                    import onchain
                    frueher = [l for l in onchain.launches() if l.get("creator") == t["creator"] and l["token"] != a]
                    r["dev"]["launches_seen"] = len(frueher)
                except Exception:
                    pass
    else:
        g = sicherheit_evm(goplus(chain, a))
        if g:
            r["safety"].update({x: g.get(x) for x in ("honeypot", "buy_tax", "sell_tax", "mintable", "open_source", "proxy",
                                                       "take_back", "hidden_owner", "pausable", "blacklist", "cannot_sell_all",
                                                       "lp_locked_pct", "owner_renounced") if g.get(x) is not None})
            if g.get("holders"):
                r["supply"]["holders"] = g["holders"]
            if g.get("creator"):
                r["dev"].update(address=g["creator"], holds_pct=round(g["creator_pct"], 3) if g.get("creator_pct") is not None else None,
                                explorer=explorer(chain, "address", g["creator"]))
            if g.get("top"):
                r["supply"]["top"] = g["top"]
    # Tiefe: eigene Erkennung (intel.py), Ergebnis aus dem Cache oder im Hintergrund
    import intel
    starten = deep is True or (deep is None and (r.get("age_sec") or 0) < 14 * 86400 and (mk.get("mcap") or 0) < 100e6)
    it = intel.tiefe(chain, a, mk.get("mcap"), r.get("symbol"), starten=starten)
    r["deep"] = {"state": it.get("state"), "age_min": it.get("age_min")}
    if it.get("state") == "ready" and it.get("measured"):
        sp = r["supply"]
        for x, y in (("bundled_supply_pct", "bundled_pct"), ("fresh_wallet_pct", "fresh_pct"), ("clusters", "clusters"),
                     ("same_funder_groups", "same_funder"), ("snipers", "snipers"), ("cluster_supply_pct", "cluster_pct"),
                     ("still_holding_pct", "early_holding_pct")):
            if it.get(x) is not None and not (str(it.get("basis") or "").startswith("top") and y in ("bundled_pct", "snipers", "early_holding_pct")):
                sp[y] = it[x]
        if it.get("dev"):
            if not r["dev"].get("address"):
                r["dev"].update(address=it["dev"], explorer=explorer(chain, "address", it["dev"]))
            if it.get("dev_holds_pct") is not None and r["dev"].get("holds_pct") is None:
                r["dev"]["holds_pct"] = it["dev_holds_pct"]
        dh = it.get("dev_history") or {}
        if dh.get("seen") is not None:
            r["dev"]["launches_seen"] = max(r["dev"].get("launches_seen") or 0, dh["seen"])
            r["dev"]["launches_ran"] = dh.get("ran")
        rb = it.get("rated") or {}
        if rb.get("checked"):
            r["smart"] = {"found": rb.get("found") or 0, "checked": rb["checked"], "best": rb.get("best")}
        hm = it.get("holders_map") or []
        if it.get("holders_count") and not sp.get("holders"):
            sp["holders"] = it["holders_count"]
        if hm:
            echt = [x for x in hm if not x.get("pool") and not x.get("locked")]
            r["supply"]["top"] = hm[:15]
            r["supply"]["top10_pct"] = round(sum(x["pct"] for x in echt[:10]), 2)
            r["supply"]["largest_pct"] = echt[0]["pct"] if echt else None
            r["supply"].pop("top_note", None)
        if chain == "solana" and not sp.get("holders"):
            try:
                hz = intel.holder_zahl_sol(a)
                if hz:
                    sp["holders"] = hz
            except Exception:
                pass
        fw = it.get("forwards") or {}
        if fw.get("checked"):
            r["forwards"] = fw
        cl = it.get("cluster_map") or []
        von = {}
        for c in cl:
            for mm in c.get("members") or []:
                von.setdefault(str(mm).lower(), c.get("id"))
        basis = [dict(x) for x in (r["supply"].get("top") or [])]
        for x in basis:
            if von.get(str(x.get("address")).lower()):
                x["cluster"] = von[str(x["address"]).lower()]
        r["supply"]["top"] = basis or r["supply"].get("top")
        r["map"] = {"holders": basis[:40] + [{"address": b["wallet"], "pct": b["pct"], "pool": False, "cluster": b["cluster"]}
                                             for b in it.get("buyers") or [] if b.get("cluster") and b["pct"] > 0
                                             and str(b["wallet"]).lower() not in {str(x.get("address")).lower() for x in basis}][:25],
                    "clusters": [{"id": c.get("id"), "kind": "same funder" if c.get("type") == "funding" else "bought together",
                                  "supply_pct": c.get("supply_pct"), "wallets": c.get("wallets"), "funder": c.get("funder"),
                                  "exchange_like": False, "members": (c.get("members") or [])[:30]} for c in cl]}
        n = []
        if str(it.get("basis") or "").startswith("top"):
            n.append("Launch is too far back to read bundles; clusters and fresh wallets are checked on the top holders")
        if (it.get("snipers") or 0) >= 3:
            n.append(f"{it['snipers']} wallets bought in the launch block or right after")
        if (it.get("cluster_supply_pct") or 0) >= 5:
            n.append(f"Wallets in clusters still hold {zahl(it['cluster_supply_pct'])}% of supply")
        if it.get("still_holding_pct") is not None and it["still_holding_pct"] <= 25:
            n.append(f"Only {zahl(it['still_holding_pct'])}% of the first {it.get('early_buyers')} buyers still hold")
        if dh.get("ran"):
            n.append(f"Dev's earlier coins that ran: {', '.join(dh.get('examples') or [])}".rstrip(": "))
        r["notes"] = n
    # Solana ohne Tiefe: groesste Konten
    if chain == "solana" and not r["supply"].get("top") and locals().get("sol_supply"):
        top = top_sol(a, sol_supply)
        if top:
            r["supply"]["top"] = top
            r["supply"]["top_note"] = "token accounts, pools included"
    if r["supply"].get("top") and r["supply"].get("top10_pct") is None and chain != "solana":
        echt = [x for x in r["supply"]["top"] if not x.get("pool") and not x.get("locked")]
        r["supply"]["top10_pct"] = round(sum(x["pct"] for x in echt[:10]), 2)
        r["supply"]["largest_pct"] = echt[0]["pct"] if echt else None
    gesp = [x for x in r["supply"].get("top") or [] if x.get("locked")]
    if gesp:
        namen = sorted({str(x.get("locker") or x.get("label") or "").strip() for x in gesp} - {""})
        r["supply"]["locked_pct"] = round(sum(float(x.get("pct") or 0) for x in gesp), 2)
        r["supply"]["locked_by"] = ", ".join(namen)[:60] or None
        ub = [x.get("unlock_ts") for x in gesp if x.get("unlock_ts")]
        if ub:
            r["supply"]["unlock_ts"] = min(ub)
    if r["dev"].get("address") and not r["dev"].get("explorer"):
        r["dev"]["explorer"] = explorer(chain, "address", r["dev"]["address"])
    if not mk and not r.get("symbol"):
        return {"ok": False, "error": f"No market found for {a} on any chain I read. It may be brand new or not tradable yet."}
    r["age"] = alter_text(r.get("age_sec"))
    try:
        zw = zwillinge(r)
        if zw:
            r["twins"] = zw
    except Exception as e:
        log.debug(f"scan zwillinge: {e}")
    try:
        nv = narrativ(r)
        if nv:
            r["narrative"] = nv
    except Exception as e:
        log.debug(f"scan narrativ: {e}")
    if chain == "solana":
        alt_name = (sicherheit_sol(a) or {}).get("name")
        if alt_name and mk.get("name") and alt_name.strip().lower() != str(mk.get("name")).strip().lower() \
                and alt_name.strip().lower() not in str(mk.get("name")).lower():
            r["renamed"] = {"onchain": alt_name, "listed": mk.get("name")}
    r["verdict"] = urteil(r)
    r["card"] = karte(r)
    r["card_html"] = karte(r, html=True)
    _CACHE[ck] = (time.time(), r)
    if (r.get("deep") or {}).get("state") == "running":
        r["deep_note"] = "The deep scan (bundles, clusters, early buyers) is running and arrives in about a minute."
    return r


# ── Karte fuer Telegram (Klartext, kurze Zeilen) ─────────────────────────

# ── Gleicher Name, andere Adresse; Narrative ─────────────────────────────

_SUCHE = {}
STOP = {"the", "a", "of", "and", "coin", "token", "inu", "on", "to", "in", "for", "by", "is", "it", "my", "official",
        "sol", "solana", "eth", "base", "bnb", "ai", "x", "new", "fun", "pump", "meme", "robinhood", "torch"}


def _suche(q):
    q = str(q or "").strip()
    c = _SUCHE.get(q.lower())
    if c and time.time() - c[0] < 600:
        return c[1]
    try:
        ps = requests.get("https://api.dexscreener.com/latest/dex/search", params={"q": q}, headers=UA, timeout=10).json().get("pairs") or []
    except Exception:
        ps = []
    _SUCHE[q.lower()] = (time.time(), ps)
    return ps


CG_KETTEN = {"solana": "solana", "ethereum": "ethereum", "base": "base", "binance-smart-chain": "bsc",
             "arbitrum-one": "arbitrum", "robinhood": "robinhood"}


def _coingecko(sym):
    """Etablierte Coins: CoinGecko kennt den echten Coin zu einem Ticker (nach Marktkapitalisierung).
    -> {"id", "rank", "chain", "address"} oder {"id", "rank", "major": True} (eigene Chain, kein Contract) oder None.
    24 h im kv-Speicher, damit das kostenlose Limit reicht. COINGECKO_API_KEY (Demo-Key) optional."""
    key = "cgt_" + sym.lower()
    try:
        c = json.loads(storage.kv_get(key) or "null")
        if c and time.time() - c.get("ts", 0) < 86400:
            return c.get("d")
    except Exception:
        pass
    h = dict(UA)
    if os.getenv("COINGECKO_API_KEY", "").strip():
        h["x-cg-demo-api-key"] = os.getenv("COINGECKO_API_KEY").strip()
    try:
        d = requests.get("https://api.coingecko.com/api/v3/search", params={"query": sym}, headers=h, timeout=8).json()
        if "coins" not in d:
            raise ValueError("coingecko limit")
        cs = [x for x in d.get("coins") or [] if str(x.get("symbol") or "").lower() == sym.lower() and x.get("market_cap_rank")]
        if not cs:
            out = None
        else:
            cs.sort(key=lambda x: x["market_cap_rank"])
            cid, rank = cs[0]["id"], cs[0]["market_cap_rank"]
            i = requests.get(f"https://api.coingecko.com/api/v3/coins/{cid}", headers=h, timeout=8,
                             params={"localization": "false", "tickers": "false", "market_data": "false",
                                     "community_data": "false", "developer_data": "false"}).json()
            if i.get("id") != cid:
                raise ValueError("coingecko limit")  # Limit-Antwort nie als 'grosser Coin ohne Contract' werten
            pl = {CG_KETTEN[k]: v for k, v in (i.get("platforms") or {}).items() if k in CG_KETTEN and v}
            heim = CG_KETTEN.get(i.get("asset_platform_id") or "")
            if heim and pl.get(heim):
                out = {"id": cid, "rank": rank, "chain": heim, "address": pl[heim]}
            elif pl:
                k = next(k for k in ("solana", "ethereum", "base", "bsc", "arbitrum", "robinhood") if k in pl)
                out = {"id": cid, "rank": rank, "chain": k, "address": pl[k]}
            elif not i.get("asset_platform_id"):
                out = {"id": cid, "rank": rank, "major": True}
            else:
                out = None
    except Exception as e:
        log.debug(f"scan coingecko {sym}: {e}")
        return None  # Fehler nicht cachen
    try:
        storage.kv_set(key, json.dumps({"ts": time.time(), "d": out}))
    except Exception:
        pass
    return out


def ticker_finden(sym, chain=None):
    """Ticker -> der echte Coin, ueber den ganzen Markt, nicht nur Torch.
    1. ODDS und TORCH fest. 2. CoinGecko (etablierte Coins, nach Marktkapitalisierung). 3. DexScreener: exakter
    Ticker, echtes Volumen und echte Trades gewinnen (Liquiditaet allein ist faelschbar). 4. Torch-Kurve (onchain).
    Gleichnamige Coins woanders kommen als 'others' mit."""
    q = str(sym or "").strip().lstrip("$")
    if not q:
        return None
    import onchain
    if q.lower() in ("odds", "torch"):
        t = onchain._suche(q) or {"odds": "0x9c87b4cfa21266d1638f437c96dbe1012cb81111",
                                  "torch": "0x000050e18ffa3f8f7ad170eb9cef160eb3ed1111"}[q.lower()]
        return {"chain": "robinhood", "address": t}
    toks = {}
    for p in _suche(q):
        b = p.get("baseToken") or {}
        k = DEX_ZU.get(p.get("chainId"))
        if not k or str(b.get("symbol") or "").strip().lstrip("$").lower() != q.lower():
            continue
        if chain and k != chain:
            continue
        adr = str(b.get("address") or "")
        if not adr:
            continue
        t = toks.setdefault((k, adr.lower()), {"chain": k, "address": adr if k == "solana" else adr.lower(),
                                               "liq": 0.0, "vol24": 0.0, "tx24": 0, "mcap": 0.0})
        t["liq"] += _liq(p)
        t["vol24"] += float((p.get("volume") or {}).get("h24") or 0)
        tx = (p.get("txns") or {}).get("h24") or {}
        t["tx24"] += int(tx.get("buys") or 0) + int(tx.get("sells") or 0)
        t["mcap"] = max(t["mcap"], float(p.get("marketCap") or p.get("fdv") or 0))

    def andere(ohne):
        echt = [t for t in toks.values() if t["address"].lower() != ohne.lower() and t["liq"] >= 1000
                and (not t["mcap"] or t["liq"] <= t["mcap"] * 1.5)]
        echt.sort(key=lambda t: (-t["vol24"], -t["tx24"]))
        return [{"chain": KETTEN[t["chain"]][0], "address": t["address"], "liquidity_usd": round(t["liq"]),
                 "mcap_usd": round(t["mcap"]), "volume_24h_usd": round(t["vol24"])} for t in echt[:5]]

    # Echtes Geschehen: Liquiditaet ueber der Marktkapitalisierung ist ein Fake-Pool, ohne Trades zaehlt nichts
    plausibel = [t for t in toks.values() if t["liq"] >= 2000 and t["tx24"] >= 20 and (not t["mcap"] or t["liq"] <= t["mcap"] * 1.5)]
    best = max(plausibel, key=lambda t: (t["vol24"], t["tx24"])) if plausibel else None
    def mehrdeutig(liste):
        """Mehrere echte Coins mit diesem Ticker und keiner klar vorne: nicht raten, nach dem Contract fragen."""
        kand = sorted(liste, key=lambda t: -t["vol24"])[:5]
        return {"ambiguous": True, "symbol": q.upper(), "candidates": [
            {"chain": KETTEN[t["chain"]][0], "address": t["address"], "mcap_usd": round(t["mcap"]),
             "liquidity_usd": round(t["liq"]), "volume_24h_usd": round(t["vol24"])} for t in kand]}

    cg = _coingecko(q)
    if cg and cg.get("address") and (not chain or cg["chain"] == chain):
        a = cg["address"] if cg["chain"] == "solana" else cg["address"].lower()
        cg_vol = sum(t["vol24"] for t in toks.values() if t["address"].lower() == a.lower())
        # Ein kleiner CoinGecko-Coin verliert gegen einen Coin mit deutlich mehr echtem Handel (z. B. LOUD auf Torch)
        if not (best and cg["rank"] > 500 and best["vol24"] > max(cg_vol, 1) * 3 and best["address"].lower() != a.lower()):
            # Etablierter Coin (Top 500) ist eindeutig. Ein kleiner mit ernsthafter Konkurrenz gleichen Namens nicht.
            rivalen = [t for t in plausibel if t["address"].lower() != a.lower() and t["vol24"] >= max(cg_vol, 1) * 0.25]
            if cg["rank"] > 500 and rivalen:
                eigen = toks.get((cg["chain"], a.lower())) or {"chain": cg["chain"], "address": a, "liq": 0.0, "vol24": cg_vol, "mcap": 0.0}
                return mehrdeutig([eigen] + rivalen)
            return {"chain": cg["chain"], "address": a, "coingecko_rank": cg["rank"], "others": andere(a)}
    elif cg and cg.get("major") and not chain:
        return {"major": True, "symbol": q.upper(), "coingecko_id": cg["id"]}
    if best:
        # Zweiter Coin mit mindestens einem Viertel des Handels des ersten: zu nah dran, also nachfragen
        rivalen = [t for t in plausibel if t is not best and t["vol24"] >= best["vol24"] * 0.25]
        if rivalen:
            return mehrdeutig([best] + rivalen)
        out = {"chain": best["chain"], "address": best["address"], "others": andere(best["address"])}
        return out
    t = onchain._suche(q)
    if t:
        return {"chain": "robinhood", "address": t}
    return None


def zwillinge(r):
    """Andere Coins mit demselben Ticker oder Namen. Wer war zuerst da, wer ist groesser?"""
    sym = str(r.get("symbol") or "").strip().lstrip("$")
    if len(sym) < 2:
        return None
    eigen = str(r["address"]).lower()
    toks = {}
    for p in _suche(sym):
        b = p.get("baseToken") or {}
        if str(b.get("symbol") or "").strip().lstrip("$").lower() != sym.lower():
            continue
        a = str(b.get("address") or "")
        if not a or a.lower() == eigen:
            continue
        k = DEX_ZU.get(p.get("chainId")) or p.get("chainId")
        t = toks.setdefault(a.lower(), {"address": a, "chain": k, "liq": 0.0, "mcap": 0.0, "created_ms": None})
        t["liq"] += _liq(p)
        t["mcap"] = max(t["mcap"], float(p.get("marketCap") or p.get("fdv") or 0))
        if p.get("pairCreatedAt"):
            t["created_ms"] = min(t["created_ms"] or p["pairCreatedAt"], p["pairCreatedAt"])
    if not toks:
        return None
    andere = sorted(toks.values(), key=lambda t: -t["mcap"])
    mein_alter = (r.get("market") or {}).get("created_ms")
    aelter = [t for t in andere if t["created_ms"] and mein_alter and t["created_ms"] < mein_alter]
    groesser = [t for t in andere if t["mcap"] > ((r.get("market") or {}).get("mcap") or 0)]
    gleiche_chain = [t for t in andere if t["chain"] == r["chain"]]
    out = {"count": len(andere), "same_chain": len(gleiche_chain), "older": len(aelter), "bigger": len(groesser),
           "top": [{"address": t["address"], "chain": KETTEN.get(t["chain"], (t["chain"],))[0], "mcap": t["mcap"],
                    "age": alter_text(time.time() - t["created_ms"] / 1000) if t["created_ms"] else None} for t in andere[:3]]}
    out["likely_copy"] = bool(aelter and groesser and any(t in groesser for t in aelter))
    return out


def narrativ(r):
    """Laeuft das Thema dieses Coins gerade? Neue Coins mit demselben Wort in 3 Tagen, wie viele davon laufen."""
    woerter = re.findall(r"[a-z]{3,}", f"{r.get('name') or ''} {r.get('symbol') or ''}".lower())
    woerter = [w for w in dict.fromkeys(woerter) if w not in STOP][:2]
    if not woerter:
        return None
    best = None
    for w in woerter:
        jetzt = time.time() * 1000
        neu, gelaufen, gesehen = 0, 0, set()
        for p in _suche(w):
            b = p.get("baseToken") or {}
            a = str(b.get("address") or "").lower()
            if not a or a in gesehen or a == str(r["address"]).lower():
                continue
            if w not in f"{b.get('name') or ''} {b.get('symbol') or ''}".lower():
                continue
            if not p.get("pairCreatedAt") or jetzt - p["pairCreatedAt"] > 3 * 86400 * 1000:
                continue
            gesehen.add(a)
            neu += 1
            mc = float(p.get("marketCap") or p.get("fdv") or 0)
            if mc >= 1_000_000 or float((p.get("priceChange") or {}).get("h24") or 0) >= 200:
                gelaufen += 1
        if neu >= 3 and (best is None or (gelaufen, neu) > (best["ran"], best["new"])):
            best = {"word": w.upper(), "new": neu, "ran": gelaufen, "hot": gelaufen >= 2}
    return best


def karte(r, html=False):
    """Die Karte fuer Telegram und den Web-Chat. Seit 2.41.1 mit klarer Gliederung; html=True fuer Telegram
    (fett, CA zum Antippen kopierbar)."""
    import html as H
    e = (lambda x: H.escape(str(x), quote=False)) if html else (lambda x: str(x))
    fett = (lambda x: f"<b>{e(x)}</b>") if html else (lambda x: str(x))
    m, s, sp, dv, v = r.get("market") or {}, r.get("safety") or {}, r.get("supply") or {}, r.get("dev") or {}, r.get("verdict") or {}
    sym = r.get("symbol") or kurz(r["address"])
    kopf = fett(f"${sym}") + (f"  {e(r['name'])}" if r.get("name") and str(r["name"]).lower() != str(sym).lower() else "")
    ort = " · ".join(x for x in (r.get("chain_name"), r.get("venue"), (r.get("age") + " old") if r.get("age") else None) if x)
    z = [kopf, e(ort), ""]
    # Urteil mit den wichtigsten Punkten
    z.append(f"{AMPEL[v.get('level', 'amber')]} " + fett(v.get("title") or ""))
    schlecht, gut = list(v.get("bad") or []), list(v.get("good") or [])
    for x in schlecht[:3]:
        z.append("⚠️ " + e(x))
    for x in gut[:max(2, 5 - min(3, len(schlecht)))]:
        z.append("✅ " + e(x))
    if not schlecht and not gut and v.get("reason"):
        z.append(e(v["reason"]))
    # Markt
    z.append("")
    z.append(f"💰 MC {usd(m.get('mcap'))} · Liq {usd(m.get('liq'))} · {preis(m.get('price'))}")
    ch = m.get("change") or {}
    if ch:
        z.append("📈 " + " · ".join(f"{n} {pct(ch[k])}" for k, n in (("m5", "5m"), ("h1", "1h"), ("h24", "24h")) if ch.get(k) is not None))
    h1 = (m.get("txns") or {}).get("h1") or {}
    if h1.get("buys") is not None:
        h24 = (m.get("txns") or {}).get("h24") or {}
        z.append(f"🔁 1h {h1['buys']} buys / {h1['sells']} sells · Vol {usd((m.get('vol') or {}).get('h1'))}"
                 + (f" · 24h {h24['buys']}/{h24['sells']} · Vol {usd((m.get('vol') or {}).get('h24'))}" if h24.get("buys") is not None else ""))
    if r.get("torch"):
        tt = r["torch"]
        z.append("🎓 Torch: graduated" if tt.get("graduated") else f"🎓 Torch curve: {tt.get('progress_to_graduation_pct')}% to graduation")
    # Sicherheit
    teile = []
    if s.get("honeypot") is not None:
        teile.append("Honeypot!" if s["honeypot"] else "No honeypot")
    if s.get("buy_tax") is not None or s.get("sell_tax") is not None:
        teile.append(f"Tax {zahl(s.get('buy_tax') or 0)}/{zahl(s.get('sell_tax') or 0)}")
    if s.get("mint_revoked") is not None:
        teile.append("Mint revoked" if s["mint_revoked"] else "Mint ACTIVE")
    elif s.get("mintable") is not None:
        teile.append("Mintable" if s["mintable"] else "Not mintable")
    if s.get("freeze_revoked") is not None:
        teile.append("Freeze revoked" if s["freeze_revoked"] else "Freeze ACTIVE")
    if s.get("lp_locked_pct") is not None:
        teile.append(f"LP {zahl(s['lp_locked_pct'])}% locked")
    elif s.get("lp_locked") is not None:
        teile.append("LP locked" if s["lp_locked"] else "LP not locked")
    if s.get("owner_renounced") is not None:
        teile.append("Owner renounced" if s["owner_renounced"] else "Owner active")
    lock = None
    if (sp.get("locked_pct") or 0) >= 0.5:
        lock = (f"🔒 {zahl(sp['locked_pct'])}% of supply locked"
                + (f" until {time.strftime('%d %b %Y', time.gmtime(sp['unlock_ts']))}" if sp.get("unlock_ts") else "")
                + (f" ({sp['locked_by']})" if sp.get("locked_by") else ""))
    if teile or lock:
        z += ["", "🛡 " + fett("Safety")]
        if teile:
            z.append(e(" · ".join(teile)))
        if lock:
            z.append(e(lock))
    # Holder
    sz = []
    kurzinfo = []
    if sp.get("holders"):
        kurzinfo.append(f"{int(sp['holders']):,} holders")
    if sp.get("top10_pct") is not None:
        kurzinfo.append(f"top 10 {zahl(sp['top10_pct'])}%")
    if sp.get("largest_pct") is not None:
        kurzinfo.append(f"largest {zahl(sp['largest_pct'])}%")
    if kurzinfo:
        sz.append(" · ".join(kurzinfo))
    if sp.get("bundled_pct") is not None:
        sz.append(f"{balken(sp['bundled_pct'])} {zahl(sp['bundled_pct'])}% bundled")
    if sp.get("fresh_pct") is not None:
        sz.append(f"{balken(sp['fresh_pct'])} {zahl(sp['fresh_pct'])}% fresh wallets")
    if any(sp.get(x) is not None for x in ("clusters", "same_funder", "snipers")):
        sz.append(f"Clusters {sp.get('clusters') or 0} · same funder {sp.get('same_funder') or 0} · snipers {sp.get('snipers') or 0}")
    fw = r.get("forwards") or {}
    if fw.get("count"):
        t = f"📤 Forwards: {fw['count']} from {fw['senders']} wallet{'s' if fw['senders'] != 1 else ''}"
        if fw.get("to_fresh"):
            t += f" ({fw['to_fresh']} to fresh wallets)"
        if fw.get("still_held_pct"):
            t += f" · receivers still hold {zahl(fw['still_held_pct'])}%"
        sz.append(t)
        if fw.get("collectors_elsewhere"):
            sz.append(f"📥 Collection wallets: {fw['collectors_elsewhere']} also collected on other coins"
                      + (f" (one on {fw['collectors_elsewhere_max']})" if (fw.get("collectors_elsewhere_max") or 0) > 1 else ""))
    elif fw.get("checked"):
        sz.append("📤 Forwards: none")
    if r.get("burned_pct"):
        sz.append(f"🔥 Burned {zahl(r['burned_pct'])}% of supply")
    sm = r.get("smart") or {}
    if sm.get("checked"):
        sz.append(f"🧠 Smart wallets: {sm['found']} of the first {sm['checked']} buyers" + (f" (best score {sm['best']})" if sm.get("best") else ""))
    if sz:
        z += ["", "👥 " + fett("Holders")] + [e(x) for x in sz]
    # Dev
    if dv.get("address"):
        d = f"{kurz(dv['address'])}" + (f" · holds {zahl(dv['holds_pct'])}%" if dv.get("holds_pct") is not None else "")
        if dv.get("launches_seen"):
            d += (f" · {dv['launches_seen']} earlier launch{'es' if dv['launches_seen'] != 1 else ''}"
                  + (f", {dv['launches_ran']} ran" if dv.get("launches_ran") else ", none ran" if dv.get("launches_ran") == 0 else ""))
        z += ["", "🧑‍💻 " + fett("Dev") + " " + e(d)]
    doppelt = ("top 10", "largest wallet", "fresh wallet", "multiple minor", "low mcap", "price dropped", "price rose")
    fl = [n for n in (r.get("notes") or []) if not any(d in n.lower() for d in doppelt)][:3]
    if fl:
        z += [""] + ["• " + e(n) for n in fl]
    # Name und Narrativ
    tw, nv = r.get("twins") or {}, r.get("narrative")
    extra = []
    if tw.get("count"):
        if tw.get("likely_copy"):
            extra.append(f"⚠️ Copycat: {tw['count']} other ${r.get('symbol')}, {tw['older']} older and bigger. "
                         f"Biggest on {tw['top'][0]['chain']} at MC {usd(tw['top'][0]['mcap'])}")
        else:
            extra.append(f"🪞 Same name: {tw['count']} other ${r.get('symbol')}" + (f", {tw['same_chain']} on this chain" if tw.get("same_chain") else "")
                         + (". This one is the oldest" if not tw.get("older") else ""))
    if r.get("renamed"):
        extra.append(f"⚠️ Name on chain is \"{r['renamed']['onchain']}\", listed as \"{r['renamed']['listed']}\"")
    if nv:
        extra.append(f"📣 Narrative {nv['word']}: {nv['new']} new coins in 3 days, {nv['ran']} running" + (" (hot)" if nv.get("hot") else ""))
    if r.get("also_on"):
        extra.append(f"Also trades on {', '.join(r['also_on'])}")
    if extra:
        z += [""] + [e(x) for x in extra]
    if (r.get("deep") or {}).get("state") == "running":
        z += ["", e("⏳ Deep scan running, bundles and first buyers follow shortly.")]
    z += ["", f"<code>{e(r['address'])}</code>" if html else r["address"]]
    return "\n".join(z)


def knoepfe(r):
    """Telegram-Knoepfe unter der Karte. ('text', url) oder ('text', 'cb:...')."""
    out = [[("Full report", r["links"]["page"])]]
    zeile = []
    if r["links"].get("chart"):
        zeile.append(("Chart", r["links"]["chart"]))
    zeile.append(("Watch it", f"cb:w:{_kurzid(r)}"))
    zeile.append(("Refresh", f"cb:r:{_kurzid(r)}"))
    out.append(zeile)
    out.append([("Project research", f"cb:p:{_kurzid(r)}")])
    return out


def _kurzid(r):
    """Callback-Daten sind auf 64 Bytes begrenzt: Chain und Adresse unter kurzer Id merken."""
    import hashlib
    i = hashlib.sha1(f"{r['chain']}:{r['address']}".encode()).hexdigest()[:12]
    try:
        storage.kv_set("scan_id_" + i, json.dumps({"chain": r["chain"], "address": r["address"], "symbol": r.get("symbol")}))
    except Exception:
        pass
    return i


def aus_id(i):
    try:
        return json.loads(storage.kv_get("scan_id_" + re.sub(r"[^0-9a-f]", "", str(i))[:12]) or "null")
    except Exception:
        return None


# ── Fuer das Modell: kompakt, ohne Karte doppelt ─────────────────────────

def fuer_modell(r):
    if not r.get("ok"):
        return r
    aus = {k: r.get(k) for k in ("chain_name", "symbol", "name", "address", "venue", "age", "also_on", "risk_score", "twins", "narrative", "renamed",
                                 "safety", "dev", "smart", "notes", "deep_note", "burned_pct", "torch")}
    fw = r.get("forwards") or {}
    if fw.get("checked"):
        aus["forwards"] = {k: fw.get(k) for k in ("count", "senders", "receivers", "from_early", "from_dev", "dev_forwarded_pct",
                                                  "to_fresh", "forwarded_pct", "still_held_pct", "collectors_elsewhere",
                                                  "collectors_elsewhere_max")}
        aus["forwards"]["meaning"] = ("Forwards are plain wallet to wallet transfers of this coin, no sale. Early buyers or the dev "
                                      "moving tokens to new wallets can hide how much they really hold.")
    aus["market"] = {k: (r.get("market") or {}).get(k) for k in ("price", "mcap", "liq", "change", "txns", "vol")}
    aus["supply"] = {k: v for k, v in (r.get("supply") or {}).items() if k != "top"}
    aus["verdict"] = r.get("verdict")
    aus["links"] = r.get("links")
    aus["shown_to_user"] = ("The full report card with these numbers, the verdict and buttons (Full report, Chart, "
                            "Watch it, Refresh) is ALREADY shown to the user above your answer. Do not repeat the numbers. "
                            "Add at most two short lines: the one thing that matters most here, and what they can do next "
                            "(watch it, or buy with the trade tool). If they ask questions, answer from this data.")
    return {k: v for k, v in aus.items() if v not in (None, {}, [])}


# ── Live-Handel fuer die Seite: GeckoTerminal ────────────────────────────

_TR = {}


def trades(chain, pair, n=40):
    netz = KETTEN.get(chain, (None,) * 3)[2]
    if not netz or not pair:
        return []
    c = _TR.get((chain, pair))
    if c and time.time() - c[0] < 8:
        return c[1]
    out = []
    try:
        d = requests.get(f"https://api.geckoterminal.com/api/v2/networks/{netz}/pools/{pair}/trades",
                         headers=UA, timeout=12).json()
        for x in (d.get("data") or [])[:n]:
            a = x.get("attributes") or {}
            kauf = a.get("kind") == "buy"
            out.append({"ts": a.get("block_timestamp"), "side": "buy" if kauf else "sell",
                        "usd": _f(a.get("volume_in_usd")), "wallet": a.get("tx_from_address"),
                        "tx": a.get("tx_hash"),
                        "amount": _f(a.get("to_token_amount") if kauf else a.get("from_token_amount")),
                        "price": _f(a.get("price_to_in_usd") if kauf else a.get("price_from_in_usd"))})
    except Exception as e:
        log.debug(f"scan trades {pair}: {e}")
        if c:
            return c[1]
    _TR[(chain, pair)] = (time.time(), out)
    return out


def seite(q):
    """Alles fuer die Live-Seite auf engine.oddsengine.live. Startet keinen neuen Tiefen-Scan."""
    r = analyse(q, deep=False)
    if not r.get("ok"):
        return r
    m = r.get("market") or {}
    tr = trades(r["chain"], m.get("pair"))
    dev = str((r.get("dev") or {}).get("address") or "").lower()
    top = {str(x.get("address") or "").lower(): x for x in (r.get("supply") or {}).get("top") or []}
    for t in tr:
        w = str(t.get("wallet") or "").lower()
        if dev and w == dev:
            t["tag"] = "dev"
        elif w in top and not top[w].get("pool"):
            t["tag"] = "top holder"
    out = dict(r)
    out["trades"] = tr
    out["explorer_base"] = KETTEN[r["chain"]][4]
    out["tx_path"] = KETTEN[r["chain"]][7]
    out["addr_path"] = KETTEN[r["chain"]][6]
    netz = KETTEN.get(r["chain"], (None,) * 3)[2]
    if netz and m.get("pair"):
        out["embed"] = (f"https://www.geckoterminal.com/{netz}/pools/{m['pair']}?embed=1&info=0&swaps=0&grayscale=0"
                        "&light_chart=0&chart_type=market_cap&resolution=5m&bg_color=0b0f0d")
    elif m.get("url"):
        out["embed"] = m["url"] + "?embed=1&loadChartSettings=0&trades=0&tabs=0&info=0&chartLeftToolbar=0&chartTheme=dark&theme=dark&chartStyle=1&chartType=usd&interval=5"
    out["tradable"] = True
    return out


# ── Tiefen-Scan nachliefern (aus motor.tick) ─────────────────────────────

def nachfassen_merken(adr, r):
    if (r.get("deep") or {}).get("state") != "running":
        return
    try:
        l = json.loads(storage.kv_get("scan_follow") or "[]")
    except Exception:
        l = []
    l = [x for x in l if not (x["adr"] == adr and x["address"] == r["address"])]
    l.append({"adr": adr, "chain": r["chain"], "address": r["address"], "symbol": r.get("symbol"),
              "at": int(time.time() + 70), "n": 0})
    storage.kv_set("scan_follow", json.dumps(l[-200:]))


def nachfassen(senden, verlauf=None):
    try:
        l = json.loads(storage.kv_get("scan_follow") or "[]")
    except Exception:
        l = []
    if not l:
        return
    jetzt, rest = time.time(), []
    for x in l:
        if x["at"] > jetzt:
            rest.append(x)
            continue
        r = analyse(f"{x['chain']}:{x['address']}", deep=False, frisch=True)
        st = (r.get("deep") or {})
        if r.get("ok") and st.get("state") == "ready" and (st.get("age_min") or 0) <= 15:
            text = f"🔎 <b>Deep scan is in</b>\n\n" + r.get("card_html", "")
            try:
                senden(x["adr"], text, knoepfe(r), html=True)
            except Exception as e:
                log.debug(f"scan nachfassen senden: {e}")
            if verlauf:
                try:
                    verlauf(x["adr"], r)
                except Exception as e:
                    log.debug(f"scan nachfassen verlauf: {e}")
            continue
        x["n"] = x.get("n", 0) + 1
        if x["n"] < 4:
            x["at"] = int(jetzt + 45)
            rest.append(x)
    storage.kv_set("scan_follow", json.dumps(rest))


# ── Fuer die Live-Wache (motor._token_wache) ─────────────────────────────

RPC = {"ethereum": "https://ethereum-rpc.publicnode.com", "base": "https://mainnet.base.org",
       "bsc": "https://bsc-dataseed.bnbchain.org", "arbitrum": "https://arb1.arbitrum.io/rpc"}


def zahlen(chain, adr):
    """Kurze Marktzahlen fuer die Wache: Preis, MC, Liq, Vol 1h, Kaeufe/Verkaeufe 5m, Chart."""
    m = markt(adr, chain, _paare(adr))
    tx = (m.get("txns") or {}).get("m5") or {}
    return {"price": m.get("price"), "mc": m.get("mcap"), "liq": m.get("liq"), "vol_h1": (m.get("vol") or {}).get("h1"),
            "buys5": tx.get("buys"), "sells5": tx.get("sells"), "chg_h1": (m.get("change") or {}).get("h1"),
            "url": m.get("url"), "sym": m.get("symbol")}


def _evm(chain, methode, params):
    if chain == "robinhood":
        import holder
        return holder._evm_rpc(methode, params)
    url = os.getenv("RPC_" + chain.upper(), "").strip() or RPC.get(chain)
    d = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": methode, "params": params}, headers=UA, timeout=12).json()
    if "result" not in d:
        raise RuntimeError(str(d.get("error"))[:100])
    return d["result"]


def bestaende(chain, token, wallets):
    """Rohe Bestaende {wallet: int} plus '_supply'. Fehlende Werte fehlen einfach."""
    out = {}
    if chain == "solana":
        import holder
        try:
            s = holder._sol_rpc("getTokenSupply", [token])
            out["_supply"] = int(((s or {}).get("value") or {}).get("amount") or 0)
        except Exception:
            pass
        for w in wallets:
            try:
                r = holder._sol_rpc("getTokenAccountsByOwner", [w, {"mint": token}, {"encoding": "jsonParsed"}])
                out[w] = sum(int((((x.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info", {}).get("tokenAmount", {}).get("amount") or 0)
                             for x in ((r or {}).get("value") or []))
            except Exception:
                pass
        return out
    try:
        out["_supply"] = int(_evm(chain, "eth_call", [{"to": token, "data": "0x18160ddd"}, "latest"]), 16)
    except Exception:
        pass
    for w in wallets:
        try:
            out[w] = int(_evm(chain, "eth_call", [{"to": token, "data": "0x70a08231" + "0" * 24 + w.lower().replace("0x", "")}, "latest"]), 16)
        except Exception:
            pass
    return out
