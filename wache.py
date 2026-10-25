"""
wache.py: Wallet Guard von Your Engine, seit 2.47.

Ein laufender Drain laesst sich nicht stoppen. Was sich abfangen laesst, ist der Moment davor: die Freigabe.
Fast jeder Drain ueber einen Scam-Link beginnt mit einem Approve, Permit oder (Solana) Delegate an einen fremden
Empfaenger. Wallet Guard meldet so etwas in Sekunden in Telegram, mit einem Knopf zum Widerrufen, den die eigene
Wallet des Nutzers unterschreibt. Dazu: Abfluss-Alarm, wenn Tokens die Wallet ohne Swap verlassen, und ein
Wallet-Check, der alle offenen Freigaben zeigt.

  EVM (Robinhood, Base, Ethereum, BNB, Arbitrum): ein eth_getLogs je Kette und Runde auf Approval, ApprovalForAll,
      Permit2 (Approval, Permit) und Transfer, gefiltert auf die bewachten Wallets (topic1).
  Solana: die bewachten Wallets haengen am Helius-Webhook (smart.hook_sync), ohne Webhook wird abgefragt.
      Erkannt werden Approve/ApproveChecked (Delegate), SetAuthority (Kontobesitzer wechselt), Abfluesse ohne Swap
      und Abfluesse, die jemand anderes als der Besitzer ausloest.

  schleife()                aus ai_api.starten (Schalter 'guard')
  bewachen(adr, on, wallet) Nutzer schaltet Wallet Guard an oder aus, fuegt eine Wallet hinzu
  pruefen(adr, wallet)      Wallet-Check: offene Freigaben mit Risiko und Widerruf-Links
  sol_tx(tx_roh)            aus smart.hook_empfangen fuer Transaktionen bewachter Solana-Wallets
"""
import json
import os
import re
import threading
import time

import requests

import storage
from logger import log

SEITE = os.getenv("ENGINE_PUBLIC_URL", "https://engine.oddsengine.live").rstrip("/")
TAKT = int(os.getenv("GUARD_POLL_SEC", "20"))
# Live-Wache braucht eth_getLogs ohne Vertragsadresse. Robinhood (bis 30000 Bloecke) und Arbitrum koennen das mit dem
# oeffentlichen Knoten, Base, Ethereum und BNB nur mit eigenem Knoten (RPC_BASE, RPC_ETHEREUM, RPC_BSC, z. B. Alchemy).
KETTEN_EVM = [k.strip() for k in os.getenv("GUARD_CHAINS", "robinhood,arbitrum").split(",") if k.strip()] + \
    [k for k in ("base", "ethereum", "bsc") if os.getenv("RPC_" + k.upper(), "").strip() and
     k not in os.getenv("GUARD_CHAINS", "robinhood,arbitrum")]
FENSTER_MAX = {"robinhood": 25000, "arbitrum": 2000, "base": 500, "ethereum": 100, "bsc": 300}

T_APPROVAL = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
T_ALL = "0x17307eab39ab6107e8899845ad3d59bd9653f200f220920489ca2b5937696c31"
T_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
T_P2_APPROVAL = "0xda9fa7c1b00402c17d0161b249b1ab8bbec047c5a52207b9c112deffd817036b"
T_P2_PERMIT = "0xc6a377bfc4eb120024a8ac08eef205be16b817020812c73223e81d1bdb9708ec"
PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"
UNBEGRENZT = 2 ** 200

# Bekannte, etablierte Router und Vertraege: Freigaben an sie sind normal (Handel), kein Alarm.
BEKANNT = {
    PERMIT2: "Permit2 (Uniswap)",
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad": "Uniswap Universal Router",
    "0x66a9893cc07d91d95644aedd05d03f95e1dba8af": "Uniswap Universal Router",
    "0x6ff5693b99212da76ad316178a184ab56d299b43": "Uniswap Universal Router",
    "0x198ef79f1f515f02dfe9e3115ed9fc07183f02fc": "Uniswap Universal Router",
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d": "Uniswap V2 Router",
    "0xe592427a0aece92de3edee1f18e0157c05861564": "Uniswap V3 Router",
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45": "Uniswap V3 Router 2",
    "0x1111111254eeb25477b68fb85ed929f73a960582": "1inch Router",
    "0x111111125421ca6dc452d289314280a0f8842a65": "1inch Router",
    "0xdef1c0ded9bec7f1a1670819833240f027b25eff": "0x Exchange",
    "0x10ed43c718714eb63d5aa57b78b54704e256024e": "PancakeSwap Router",
    "0x13f4ea83d0bd40e75c8222255bc855a974568dd4": "PancakeSwap Smart Router",
    "0xcf5540fffcdc3d510b18bfca6d2b9987b0772559": "Odos Router",
    "0xdef171fe48cf0115b1d80b88dc8eab59176fee57": "ParaSwap",
    "0x6a000f20005980200259b80c5102003040001068": "ParaSwap",
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b5": "KyberSwap",
    "0x7891ea889899928afe637e4cca8106188427776a": "Torch",
    "0x8366a39cc670b4001a1121b8f6a443a643e40951": "Uniswap V4 Pool Manager",
}
GOPLUS_ID = {"ethereum": "1", "bsc": "56", "arbitrum": "42161", "base": "8453"}
SYSTEM = "11111111111111111111111111111111"
TOKEN_PROGS = {"TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"}

_T = {}
_LOCK = threading.Lock()
_SYM = {}
_EINSTUFUNG = {}


# ── Wer wird bewacht ─────────────────────────────────────────────────────

def _an():
    try:
        import features
        return features.an("guard")
    except Exception:
        return False


def bewachte():
    """{wallet_lower_or_solana: [adr, ...]} fuer alle Nutzer mit Wallet Guard an."""
    import motor
    out = {}
    for adr in motor._nutzer():
        try:
            p = motor.zustand(adr)["prefs"]
            if p.get("guard") is not True:
                continue
            ws = set(w.lower() for w in motor._wallets(adr) if str(w).startswith("0x"))
            ws |= {w for w in (p.get("guard_wallets") or []) if w}
            if p.get("sol_wallet"):
                ws.add(p["sol_wallet"])
            for w in ws:
                k = w.lower() if str(w).startswith("0x") else w
                out.setdefault(k, []).append(adr)
        except Exception as e:
            log.debug(f"wache nutzer {adr[:8]}: {e}")
    return out


def sol_wallets():
    return sorted(w for w in bewachte() if not w.startswith("0x"))


def bewachen(adr, on=True, wallet=None):
    import motor
    z = motor.zustand(adr)
    p = z["prefs"]
    p["guard"] = bool(on)
    w = str(wallet or "").strip()
    if w:
        if re.fullmatch(r"0x[0-9a-fA-F]{40}", w):
            w = w.lower()
        elif not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", w):
            return {"ok": False, "error": "That is not a wallet address (0x… or a Solana address)."}
        p["guard_wallets"] = sorted(set(p.get("guard_wallets") or []) | {w})[:10]
    motor._zustand_speichern(adr, z)
    ws = [x for x in motor._wallets(adr) if str(x).startswith("0x")] + list(p.get("guard_wallets") or []) + \
         ([p["sol_wallet"]] if p.get("sol_wallet") else [])
    return {"ok": True, "guard": bool(on), "wallets": sorted(set(ws)),
            "what": "Your Engine messages you in Telegram within seconds when one of these wallets approves a spender we do "
                    "not know, gives a Solana delegate or owner change, or when tokens leave it without a swap. Every alert "
                    "has a Revoke button your own wallet signs. Your Engine never holds keys and cannot stop a transaction.",
            "live": _an()}


# ── Einstufung von Spendern ──────────────────────────────────────────────

def _evm_rpc(chain):
    if chain == "robinhood":
        import holder
        return lambda m, p: holder._evm_rpc(m, p)
    import scan
    url = os.getenv("RPC_" + chain.upper(), "").strip() or scan.RPC.get(chain)

    def f(m, p):
        r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p}, timeout=20).json()
        if "error" in r:
            raise RuntimeError(str(r["error"])[:160])
        return r.get("result")
    return f


def einstufen(chain, spender):
    """-> {"level": "ok"|"high"|"critical", "name", "why"} fuer einen Spender. 6 h im Speicher."""
    s = str(spender or "").lower()
    k = (chain, s)
    c = _EINSTUFUNG.get(k)
    if c and time.time() - c[0] < 6 * 3600:
        return c[1]
    if s in BEKANNT:
        out = {"level": "ok", "name": BEKANNT[s], "why": "known router"}
    else:
        out = {"level": "high", "name": None, "why": "unknown contract"}
        try:
            code = _evm_rpc(chain)("eth_getCode", [s, "latest"])
            if code in ("0x", "0x0", None):
                out = {"level": "critical", "name": None, "why": "a plain wallet, not a contract (classic drainer pattern)"}
        except Exception:
            pass
        if out["level"] == "high" and chain in GOPLUS_ID:
            try:
                d = requests.get(f"https://api.gopluslabs.io/api/v1/approval_security/{GOPLUS_ID[chain]}",
                                 params={"contract_addresses": s}, timeout=10).json()
                r = (d.get("result") or {})
                r = r.get(s) or r
                if str(r.get("malicious_address")) == "1" or r.get("malicious_behavior"):
                    out = {"level": "critical", "name": r.get("contract_name"), "why": "flagged as malicious"}
                elif str(r.get("trust_list")) == "1":
                    out = {"level": "ok", "name": r.get("contract_name") or r.get("tag"), "why": "trusted contract"}
                else:
                    out["name"] = r.get("contract_name") or r.get("tag") or None
            except Exception:
                pass
        if out["level"] == "high" and chain == "robinhood":
            try:
                import intel
                d = intel._bs("robinhood", f"/api/v2/addresses/{s}") or {}
                cnt = intel._bs("robinhood", f"/api/v2/addresses/{s}/counters") or {}
                n = int(cnt.get("transactions_count") or 0)
                out["name"] = d.get("name") or None
                if n >= 2000 and d.get("is_verified"):
                    out = {"level": "ok", "name": d.get("name"), "why": "established verified contract"}
                elif not d.get("is_verified"):
                    out["why"] = "unknown, unverified contract"
            except Exception:
                pass
    _EINSTUFUNG[k] = (time.time(), out)
    return out


def _symbol(chain, token):
    k = (chain, token)
    if k in _SYM:
        return _SYM[k]
    sym, dez = token[:6] + "…", 18
    try:
        rpc = _evm_rpc(chain)
        r = rpc("eth_call", [{"to": token, "data": "0x95d89b41"}, "latest"]) or "0x"
        b = bytes.fromhex(r[2:])
        if len(b) >= 96:
            n = int.from_bytes(b[32:64], "big")
            sym = b[64:64 + n].decode("utf-8", "ignore") or sym
        elif len(b) == 32:
            sym = b.rstrip(b"\x00").decode("utf-8", "ignore") or sym
        dz = rpc("eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
        dez = int(dz, 16) if dz and dz != "0x" else 18
    except Exception:
        pass
    _SYM[k] = (sym, dez)
    return _SYM[k]


def _menge(raw, dez):
    if raw >= UNBEGRENZT:
        return "unlimited"
    v = raw / 10 ** dez
    return f"{v:,.0f}" if v >= 100 else f"{v:,.4f}".rstrip("0").rstrip(".")


def _kurz(a):
    a = str(a)
    return a[:6] + "…" + a[-4:]


# ── Melden ───────────────────────────────────────────────────────────────

def _einmal(schluessel, sek=6 * 3600):
    import hashlib
    k = "guard_seen_" + hashlib.sha1(schluessel.encode()).hexdigest()[:24]
    try:
        if storage.kv_get(k):
            return False
        storage.kv_set(k, str(int(time.time())))
    except Exception:
        pass
    seen = _T.setdefault("seen", {})
    if schluessel in seen and time.time() - seen[schluessel] < sek:
        return False
    seen[schluessel] = time.time()
    return True


def _melden(wallet, text, knoepfe):
    import motor
    for adr in bewachte().get(wallet, []):
        try:
            motor.senden(adr, text, knoepfe)
        except Exception as e:
            log.warning(f"wache senden: {e}")
        try:
            import aktiv
            aktiv.merken(adr, "guard", text.splitlines()[0][:160])
        except Exception:
            pass


def _widerruf_knopf(wallet, chain, token, spender, art):
    try:
        import handel
        adr = (bewachte().get(wallet) or [None])[0]
        tid = handel.widerruf_auftrag(adr, chain, wallet, token, spender, art)
        return ("Revoke now", f"{SEITE}/#trade={tid}") if tid else None
    except Exception as e:
        log.warning(f"wache widerruf: {e}")
        return None


# ── EVM: eine Runde ──────────────────────────────────────────────────────

def _pad(a):
    return "0x" + "0" * 24 + a[2:].lower()


def _runde_evm(chain, wallets):
    rpc = _evm_rpc(chain)
    top = int(rpc("eth_blockNumber", []), 16)
    k = "guard_blk_" + chain
    try:
        von = int(storage.kv_get(k) or 0)
    except Exception:
        von = 0
    if not von or top - von > FENSTER_MAX.get(chain, 500) * 4:
        von = top - min(FENSTER_MAX.get(chain, 500), 200)
    if von >= top:
        return 0
    bis = min(top, von + FENSTER_MAX.get(chain, 500))
    logs = []
    ws = sorted(wallets)
    for i in range(0, len(ws), 100):
        logs += rpc("eth_getLogs", [{"fromBlock": hex(von + 1), "toBlock": hex(bis),
                                     "topics": [[T_APPROVAL, T_ALL, T_TRANSFER, T_P2_APPROVAL, T_P2_PERMIT],
                                                [_pad(w) for w in ws[i:i + 100]]]}]) or []
    storage.kv_set(k, str(bis))
    if logs:
        auswerten_evm(chain, logs, rpc)
    return len(logs)


def auswerten_evm(chain, logs, rpc):
    import handel
    exp = handel._explorer(chain)
    je_tx = {}
    for l in logs:
        je_tx.setdefault(l.get("transactionHash"), []).append(l)
    for txh, ls in je_tx.items():
        info = None
        abfluss = [l for l in ls if l["topics"][0] == T_TRANSFER and len(l["topics"]) >= 3]
        if abfluss:
            try:
                info = rpc("eth_getTransactionByHash", [txh]) or {}
                rc = rpc("eth_getTransactionReceipt", [txh]) or {}
            except Exception:
                info, rc = {}, {}
            for l in abfluss:
                w = "0x" + l["topics"][1][-40:]
                an = "0x" + l["topics"][2][-40:]
                token = l["address"].lower()
                roh = int(l.get("data") or "0x0", 16)
                if not roh:
                    continue
                rein = any(x.get("topics") and x["topics"][0] == T_TRANSFER and len(x["topics"]) >= 3
                           and "0x" + x["topics"][2][-40:] == w for x in rc.get("logs") or [])
                sym, dez = _symbol(chain, token)
                absender = str(info.get("from") or "").lower()
                ziel_tx = str(info.get("to") or "").lower()
                if absender and absender != w:
                    # Ein bekannter Router oder Filler fuehrt einen vom Nutzer unterschriebenen Auftrag aus (z. B. UniswapX)
                    if einstufen(chain, ziel_tx)["level"] == "ok" or einstufen(chain, absender)["level"] == "ok":
                        continue
                    if not _einmal(f"drain:{chain}:{txh}:{w}"):
                        continue
                    kn = [b for b in [_widerruf_knopf(w, chain, token, absender, "erc20")] if b]
                    _melden(w, f"🚨 Tokens are being taken from your wallet {_kurz(w)}\n\n{_menge(roh, dez)} {sym} were moved out by "
                               f"{_kurz(absender)} using an approval you gave earlier. This was not signed by your wallet.\n\n"
                               f"Revoke now and move what is left to a safe wallet.\n{exp}/tx/{txh}",
                            kn + [("Transaction", f"{exp}/tx/{txh}")])
                elif not rein and ziel_tx == token:
                    # Reine Ueberweisung: die Transaktion ging direkt an den Token-Vertrag (transfer), kein Router, kein Swap
                    if not _einmal(f"out:{chain}:{txh}:{w}:{token}"):
                        continue
                    _melden(w, f"📤 {_menge(roh, dez)} {sym} left your wallet {_kurz(w)}\n\nSent to {_kurz(an)}, no swap. "
                               f"If this was not you, your key may be exposed: move what is left to a fresh wallet now.\n{exp}/tx/{txh}",
                            [("Transaction", f"{exp}/tx/{txh}")])
        for l in ls:
            t0 = l["topics"][0]
            if t0 == T_TRANSFER:
                continue
            try:
                if t0 == T_APPROVAL and len(l["topics"]) >= 3:
                    w, spender, token = "0x" + l["topics"][1][-40:], "0x" + l["topics"][2][-40:], l["address"].lower()
                    roh, art = int(l.get("data") or "0x0", 16), "erc20"
                    if abfluss and any("0x" + a["topics"][1][-40:] == w for a in abfluss):
                        continue  # Approval-Ereignis beim Ausgeben einer Freigabe, nicht eine neue Freigabe
                elif t0 == T_ALL and len(l["topics"]) >= 3:
                    w, spender, token = "0x" + l["topics"][1][-40:], "0x" + l["topics"][2][-40:], l["address"].lower()
                    roh, art = (UNBEGRENZT if int(l.get("data") or "0x0", 16) else 0), "all"
                elif t0 in (T_P2_APPROVAL, T_P2_PERMIT) and l["address"].lower() == PERMIT2 and len(l["topics"]) >= 4:
                    w, token, spender = "0x" + l["topics"][1][-40:], "0x" + l["topics"][2][-40:], "0x" + l["topics"][3][-40:]
                    roh, art = int((l.get("data") or "0x0")[2:66] or "0", 16), "permit2"
                else:
                    continue
            except Exception:
                continue
            if not roh:
                continue
            e = einstufen(chain, spender)
            if e["level"] == "ok":
                continue
            if not _einmal(f"appr:{chain}:{w}:{token}:{spender}"):
                continue
            sym, dez = _symbol(chain, token)
            was = ("all your NFTs of this collection" if art == "all" else f"{_menge(roh, dez)} {sym}")
            kopf = "🚨 Dangerous approval" if e["level"] == "critical" else "⚠️ New approval to an unknown spender"
            kn = [b for b in [_widerruf_knopf(w, chain, token, spender, art)] if b]
            _melden(w, f"{kopf} on {_kurz(w)}\n\nSpender {_kurz(spender)}{' (' + e['name'] + ')' if e.get('name') else ''}: "
                       f"{e['why']}.\nIt can now move {was}{' via Permit2' if art == 'permit2' else ''}.\n\n"
                       f"If you did not mean to do this, revoke it now, before anything is taken.\n{exp}/tx/{txh}",
                    kn + [("Transaction", f"{exp}/tx/{txh}")])


# ── Solana ───────────────────────────────────────────────────────────────

def _ix_alle(tx):
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    meta = (tx or {}).get("meta") or {}
    ixs = list(msg.get("instructions") or [])
    for g in meta.get("innerInstructions") or []:
        ixs += g.get("instructions") or []
    return [ix for ix in ixs if isinstance(ix, dict)]


def sol_tx(tx_roh):
    """Transaktion aus dem Webhook: fuer jede bewachte Wallet darin pruefen (geparst nachgeladen)."""
    if not _an():
        return 0
    import intel
    w_alle = set(sol_wallets())
    keys = intel._schluessel(tx_roh)
    betroffen = [k for k in keys if k in w_alle]
    if not betroffen:
        return 0
    sig = ((tx_roh.get("transaction") or {}).get("signatures") or [""])[0]
    tx = tx_roh
    if not _ix_alle(tx_roh) or not any("parsed" in ix for ix in _ix_alle(tx_roh)):
        tx = intel._sol(*intel._tx(sig)) or tx_roh
    for w in betroffen:
        try:
            sol_pruefen(tx, w, sig)
        except Exception as e:
            log.warning(f"wache sol {w[:6]}: {e}")
    return len(betroffen)


def _mint_name(mint):
    k = ("solana", mint)
    if k in _SYM:
        return _SYM[k][0]
    name = mint[:4] + "…" + mint[-4:]
    try:
        import scan
        ps = scan._paare(mint)
        for p in ps:
            if (p.get("baseToken") or {}).get("address") == mint:
                name = "$" + str(p["baseToken"].get("symbol") or name)
                break
    except Exception:
        pass
    _SYM[k] = (name, 0)
    return name


def sol_pruefen(tx, w, sig):
    import intel
    import weiter
    meta = (tx or {}).get("meta") or {}
    if meta.get("err"):
        return
    ixs = _ix_alle(tx)
    zahler = (intel._schluessel(tx) or [None])[0]
    konten = {}
    for b in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
        i = b.get("accountIndex")
        keys = intel._schluessel(tx)
        if isinstance(i, int) and i < len(keys):
            konten[keys[i]] = (b.get("owner"), b.get("mint"))
    link = f"https://solscan.io/tx/{sig}"
    for ix in ixs:
        p = ix.get("parsed") if isinstance(ix.get("parsed"), dict) else None
        if not p or ix.get("programId") not in TOKEN_PROGS:
            continue
        typ, info = p.get("type"), p.get("info") or {}
        if typ in ("approve", "approveChecked") and info.get("owner") == w and info.get("delegate") != w:
            konto = info.get("source")
            mint = info.get("mint") or (konten.get(konto) or (None, None))[1]
            if not _einmal(f"sol:appr:{sig}:{konto}"):
                continue
            kn = [b for b in [_widerruf_knopf(w, "solana", konto, info.get("delegate"), "delegate")] if b]
            _melden(w, f"⚠️ New delegate on your Solana wallet {_kurz(w)}\n\n{_kurz(info.get('delegate'))} can now move "
                       f"{_mint_name(mint) if mint else 'a token'} from your wallet. Normal swaps do not need this.\n\n"
                       f"If you did not mean to do this, revoke it now.\n{link}", kn + [("Transaction", link)])
        elif typ == "setAuthority" and info.get("authority") == w and str(info.get("authorityType")) in ("accountOwner", "AccountOwner"):
            if not _einmal(f"sol:auth:{sig}:{info.get('account')}"):
                continue
            _melden(w, f"🚨 Ownership of a token account in {_kurz(w)} was handed to {_kurz(info.get('newAuthority'))}\n\n"
                       f"Those tokens are no longer yours to move. If this was not you, your wallet is compromised: move "
                       f"everything else to a fresh wallet now.\n{link}", [("Transaction", link)])
        elif typ in ("transfer", "transferChecked"):
            quelle = info.get("source")
            besitzer, mint = konten.get(quelle) or (None, info.get("mint"))
            if besitzer != w:
                continue
            roh = int(((info.get("tokenAmount") or {}).get("amount")) or info.get("amount") or 0)
            if not roh:
                continue
            ziel_besitzer = (konten.get(info.get("destination")) or (None, None))[0] or info.get("destination")
            auth = info.get("authority") or info.get("multisigAuthority")
            if auth and auth != w:
                if not _einmal(f"sol:drain:{sig}:{quelle}"):
                    continue
                kn = [b for b in [_widerruf_knopf(w, "solana", quelle, auth, "delegate")] if b]
                _melden(w, f"🚨 Tokens are being taken from your wallet {_kurz(w)}\n\n{_mint_name(mint)} was moved out by "
                           f"{_kurz(auth)} using a delegate you gave earlier. Your wallet did not sign this.\n\nRevoke now and "
                           f"move what is left to a safe wallet.\n{link}", kn + [("Transaction", link)])
            elif weiter._nur_uebertragung(tx) and zahler == w:
                if not _einmal(f"sol:out:{sig}:{quelle}"):
                    continue
                _melden(w, f"📤 {_mint_name(mint)} left your wallet {_kurz(w)}\n\nSent to {_kurz(ziel_besitzer)}, no swap. If this "
                           f"was not you, your key may be exposed: move what is left to a fresh wallet now.\n{link}",
                        [("Transaction", link)])
    # SOL selbst: reine Ueberweisung ueber 0.05 SOL
    if weiter._nur_uebertragung(tx):
        for ix in ixs:
            p = ix.get("parsed") if isinstance(ix.get("parsed"), dict) else None
            if not p or ix.get("programId") != SYSTEM or p.get("type") != "transfer":
                continue
            info = p.get("info") or {}
            if info.get("source") == w and int(info.get("lamports") or 0) >= 50_000_000:
                if not _einmal(f"sol:sol:{sig}"):
                    continue
                _melden(w, f"📤 {int(info['lamports']) / 1e9:,.3f} SOL left your wallet {_kurz(w)}\n\nSent to "
                           f"{_kurz(info.get('destination'))}. If this was not you, move what is left to a fresh wallet now.\n{link}",
                        [("Transaction", link)])


def _runde_sol_abfrage(wallets):
    """Ohne Webhook: neue Signaturen je Wallet abfragen (Sicherheitsnetz)."""
    import intel
    for w in wallets[:50]:
        k = "guard_sig_" + w
        letzte = storage.kv_get(k)
        opt = {"limit": 20}
        if letzte:
            opt["until"] = letzte
        sigs = intel._sol("getSignaturesForAddress", [w, opt]) or []
        if not isinstance(sigs, list) or not sigs:
            continue
        storage.kv_set(k, sigs[0]["signature"])
        if not letzte:
            continue
        neu = [s for s in sigs if not s.get("err")][:10]
        txs = intel._sol_batch([intel._tx(s["signature"]) for s in neu], 10)
        for s, tx in zip(neu, txs):
            if isinstance(tx, dict):
                try:
                    sol_pruefen(tx, w, s["signature"])
                except Exception as e:
                    log.debug(f"wache sol abfrage: {e}")


# ── Schleife ─────────────────────────────────────────────────────────────

def tick():
    if not _an():
        return
    if not _LOCK.acquire(blocking=False):
        return
    try:
        b = bewachte()
        evm = sorted(w for w in b if w.startswith("0x"))
        if evm:
            for chain in KETTEN_EVM:
                try:
                    _runde_evm(chain, evm)
                except Exception as e:
                    _T.setdefault("fehler", {})[chain] = str(e)[:160]
                    log.debug(f"wache {chain}: {e}")
        sol = sorted(w for w in b if not w.startswith("0x"))
        if sol:
            import smart
            if not smart.hook_aktiv() and time.time() - _T.get("sol", 0) >= 60:
                _T["sol"] = time.time()
                _runde_sol_abfrage(sol)
            if time.time() - _T.get("hook", 0) >= 600:
                _T["hook"] = time.time()
                try:
                    smart.hook_sync()
                except Exception as e:
                    log.debug(f"wache hook: {e}")
        _T["runde"] = {"ts": int(time.time()), "evm_wallets": len(evm), "sol_wallets": len(sol)}
    finally:
        _LOCK.release()


def schleife():
    time.sleep(40)
    while True:
        try:
            tick()
        except Exception as e:
            log.warning(f"wache tick: {e}")
        time.sleep(TAKT)


def stand():
    return {"on": _an(), "last_round": _T.get("runde"), "errors": _T.get("fehler") or {}, "chains": KETTEN_EVM}


# ── Wallet-Check ─────────────────────────────────────────────────────────

def _robinhood_freigaben(w):
    """Freigaben einer Wallet auf Robinhood: Blockscout-Logs (Approval mit owner = w), dann aktuelle Hoehe."""
    import intel
    paare = set()
    d = intel._bs("robinhood", "/api", {"module": "logs", "action": "getLogs", "fromBlock": "0", "toBlock": "latest",
                                         "topic0": T_APPROVAL, "topic1": _pad(w), "topic0_1_opr": "and"}) or {}
    for l in (d.get("result") if isinstance(d.get("result"), list) else []):
        try:
            paare.add((str(l["address"]).lower(), "0x" + l["topics"][2][-40:]))
        except Exception:
            pass
    if not paare:
        rpc = _evm_rpc("robinhood")
        top = int(rpc("eth_blockNumber", []), 16)
        for i in range(int(os.getenv("GUARD_CHECK_WINDOWS", "40"))):
            bis = top - i * 25000
            try:
                ls = rpc("eth_getLogs", [{"fromBlock": hex(max(0, bis - 24999)), "toBlock": hex(bis),
                                          "topics": [T_APPROVAL, _pad(w)]}]) or []
            except Exception:
                break
            for l in ls:
                paare.add((str(l["address"]).lower(), "0x" + l["topics"][2][-40:]))
    rpc = _evm_rpc("robinhood")
    out = []
    for token, spender in list(paare)[:80]:
        try:
            r = rpc("eth_call", [{"to": token, "data": "0xdd62ed3e" + _pad(w)[2:] + _pad(spender)[2:]}, "latest"])
            roh = int(r or "0x0", 16)
        except Exception:
            continue
        if roh:
            sym, dez = _symbol("robinhood", token)
            out.append({"chain": "robinhood", "token": token, "symbol": sym, "spender": spender, "amount": _menge(roh, dez), "art": "erc20"})
    return out


def _goplus_freigaben(chain, w):
    try:
        d = requests.get(f"https://api.gopluslabs.io/api/v2/token_approval_security/{GOPLUS_ID[chain]}",
                         params={"addresses": w}, timeout=15).json()
    except Exception:
        return []
    out = []
    for t in d.get("result") or []:
        for a in t.get("approved_list") or []:
            sp = str(a.get("approved_contract") or "").lower()
            if not sp:
                continue
            ai = a.get("address_info") or {}
            out.append({"chain": chain, "token": str(t.get("token_address") or "").lower(), "symbol": t.get("token_symbol"),
                        "spender": sp, "amount": a.get("approved_amount"), "art": "erc20",
                        "goplus_bad": str(ai.get("malicious_behavior") or "") not in ("", "[]") or str(ai.get("doubt_list")) == "1"})
    return out


def pruefen(adr, wallet=None):
    """Wallet-Check: alle offenen Freigaben mit Risiko und Widerruf-Knopf (als Trade-Link)."""
    import motor
    p = motor.zustand(adr)["prefs"]
    if wallet:
        ws = [wallet.lower() if wallet.startswith("0x") else wallet]
    else:
        ws = [w.lower() for w in motor._wallets(adr) if str(w).startswith("0x")] + list(p.get("guard_wallets") or []) + \
             ([p["sol_wallet"]] if p.get("sol_wallet") else [])
    ws = list(dict.fromkeys(ws))[:5]
    if not ws:
        return {"ok": False, "error": "No wallet to check. Send me the address."}
    funde = []
    for w in ws:
        if w.startswith("0x"):
            try:
                funde += [dict(x, wallet=w) for x in _robinhood_freigaben(w)]
            except Exception as e:
                log.debug(f"wache check rh: {e}")
            for chain in GOPLUS_ID:
                funde += [dict(x, wallet=w) for x in _goplus_freigaben(chain, w)]
        else:
            import intel
            for prog in TOKEN_PROGS:
                r = intel._sol("getTokenAccountsByOwner", [w, {"programId": prog}, {"encoding": "jsonParsed"}]) or {}
                for x in r.get("value") or []:
                    try:
                        info = x["account"]["data"]["parsed"]["info"]
                    except Exception:
                        continue
                    if info.get("delegate"):
                        funde.append({"chain": "solana", "wallet": w, "token": x["pubkey"], "mint": info.get("mint"),
                                      "symbol": _mint_name(info.get("mint")), "spender": info["delegate"],
                                      "amount": (info.get("delegatedAmount") or {}).get("uiAmountString"), "art": "delegate"})
    aus = []
    for f in funde[:40]:
        if f["chain"] == "solana":
            e = {"level": "high", "name": None, "why": "a delegate can move these tokens"}
        else:
            e = einstufen(f["chain"], f["spender"])
            if f.get("goplus_bad"):
                e = dict(e, level="critical", why="flagged by GoPlus")
        tid = None
        try:
            import handel
            tid = handel.widerruf_auftrag(adr, f["chain"], f["wallet"], f["token"], f["spender"], f["art"])
        except Exception:
            pass
        aus.append({"chain": f["chain"], "wallet": _kurz(f["wallet"]), "token": f.get("symbol") or _kurz(f["token"]),
                    "spender": f["spender"], "spender_name": e.get("name"), "risk": e["level"], "why": e["why"],
                    "amount": f.get("amount"), "revoke_link": f"{SEITE}/#trade={tid}" if tid else None})
    aus.sort(key=lambda x: {"critical": 0, "high": 1, "ok": 2}.get(x["risk"], 3))
    return {"ok": True, "wallets": [_kurz(w) for w in ws], "approvals": aus,
            "risky": sum(1 for x in aus if x["risk"] != "ok"),
            "how_to_report": "List risky approvals first (token, spender, why) and say the revoke link opens their own wallet to "
                             "sign. Approvals to known routers are normal for trading. If there are none, say the wallet is clean. "
                             "Never ask for a seed phrase or private key."}
