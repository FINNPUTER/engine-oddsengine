"""
intel.py: die eigene Tiefe von Your Engine. Seit 2.39.1. Kein fremder Dienst.

  tiefe(chain, token, markt)   Tiefen-Scan aus dem Cache oder im Hintergrund starten
  scannen(chain, token, ...)   der Scan selbst: erste Kaeufer, Bundles, Sniper, frische Wallets,
                               gleiche Geldgeber (Cluster), Anteile, Halter, Dev, bewertete Kaeufer
  wallet(adr)                  was wir ueber ein Wallet wissen
  laeufer(stunden)             Coins aus unseren Reports, die danach gelaufen sind
  verfolgen()                  aus motor.tick: Coins nachverfolgen, Gewinner-Wallets bewerten
  importieren(url)             einmalige Uebernahme einer bestehenden Wallet-Datenbank (nur lesen)

Solana: normaler RPC (HELIUS_API_KEY, sonst SOLANA_RPC, sonst oeffentlich). Etwa 150 bis 250 Abfragen
je Tiefen-Scan, mit dem kostenlosen Helius-Plan also einige tausend Scans im Monat.
EVM (Robinhood zuerst, Base, Ethereum, Arbitrum): Transfer-Logs ab dem Launch ueber den RPC, Geldgeber
ueber den freien Blockscout der Chain. Kein Schluessel noetig.

Eigene Tabellen: iw_wallets (Bewertung), iw_early (fruehe Kaeufer je Coin), iw_tokens (verfolgte Coins),
iw_devs (wer was gelauncht hat).
"""
import json
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import storage
import weiter
from logger import log

UA = {"User-Agent": "Mozilla/5.0 oddsengine", "Accept": "application/json"}
SYSTEM = "11111111111111111111111111111111"
TOKEN_PROGS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
FRUEH = int(os.getenv("INTEL_EARLY_BUYERS", "40"))
CACHE_S = int(os.getenv("INTEL_CACHE_MIN", "30")) * 60
LAUF_X = float(os.getenv("INTEL_RUNNER_X", "5"))
BLOCKSCOUT = {"robinhood": "https://robinhoodchain.blockscout.com", "base": "https://base.blockscout.com",
              "ethereum": "https://eth.blockscout.com", "arbitrum": "https://arbitrum.blockscout.com"}
# Token-Locker auf Solana (Programm-Ids aus den Doku-Seiten der Anbieter)
LOCKER_SOL = {"strmRqUCoQUgGUan5YhzUZa6KqdzwX5L6FpUxfmKg5m": "Streamflow", "strmdbLr6w7QNmsiEXyFwWg3VSfg1GiELgU27P8aCGw": "Streamflow",
              "strn1sS2qKxs7SgJ1xx4trPKSWdqxFim6HFG9ETXiCL": "Streamflow", "9qWEUss4PnL8kTPrnG96BpMqWZTrRo38RsqKapuSsmJK": "Streamflow",
              "aSTRM2NKoKxNnkmLWk9sz3k74gKBk9t7bpPrTGxMszH": "Streamflow", "strmTRD9k8gA3vCgADVR6rg4h9KyTY5fsjvbT8R3vnP": "Streamflow",
              "STAKEvGqQTtzJZH6BWDcbpzXXn2BBerPAgQ3EGLN2GH": "Streamflow staking",
              "LocpQgucEQHbqNABEYvBvwoxCPsSbG91A1QaQhQQqjn": "Jupiter Lock"}
# Token-Locker auf EVM-Ketten: Adresse -> Name. Der Robinhood-Locker meldet Locked(id, owner, token, amount, unlockTime).
LOCKER_EVM = {"robinhood": {"0xd0f7d8c6e9f6d80c297bebe4f7fd1b9c8125c32f": "Token locker"}}
LOCK_TOPICS = {"0x1cb39d6ecc8f823fa6183785ee795ddcb2873e92effbff9b9a377ef761035676"}
TOTE = {"0x000000000000000000000000000000000000dead", "0x0000000000000000000000000000000000000000"}
BOERSEN_EVM = {"0x28c6c06298d514db089934071355e5743bf21d60", "0x21a31ee1afc51d94c2efccaa2092ad1028285549",
               "0xdfd5293d8e347dfe59e90efd55b2956a1343963d", "0x56eddb7aa87536c09ccc2793473599fd21a8b17f",
               "0x9696f59e4d72e237be84ffd425dcad154bf96976", "0x4976a4a02f38326660d17bf34b431dc6e2eb2327",
               "0x71660c4005ba85c37ccec55d0c4493e66fe775d3", "0x503828976d22510aad0201ac7ec88293211d23da",
               "0xddfabcdc4d8ffc6d5beaf154f18b778f892a0740", "0x3cd751e6b0078be393132286c442345e5dc49699",
               "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43", "0x77696bb39917c91a0c3908d577d5e322095425ca",
               "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b", "0x236f9f97e0e62388479bf9e5ba4889e46b0273c3",
               "0x2910543af39aba0cd09dbb2d50200b3e800a63d2", "0xa83b11093c858c86321fbc4c20fe82cdbd58e09e",
               "0xf89d7b9c864f589bbf53a82105107622b35eaa40", "0x0d0707963952f2fba59dd06f2b425ace40b492fe",
               "0x2b5634c42055806a59e9107ed44d43c426e58258", "0x876eabf441b2ee5b5b0554fd502a8e0600950cfa",
               "0x6262998ced04146fa42253a5c0af90ca02dfd2a3", "0x8731d54e9d02c286767d56ac03e8037c07e01e98",
               "0x3154cf16ccdb4c6d922629664174b904d80f2c35", "0x49048044d57e1c92a77f79988d21fa8faf74e97e",
               "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad", "0x1111111254eeb25477b68fb85ed929f73a960582",
               "0xdef1c0ded9bec7f1a1670819833240f027b25eff"}
BOERSEN_SOL = {"5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9", "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM",
               "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S", "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS",
               "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2", "u6PJ8DtQuPFnfmwHbGFULQ4u4EgjDiyYKjVEsynXq2w",
               "GJRs4FwHtemZ5ZE9x3FNvJ8TMwitKTh21yxdRPqn7npE", "6QJzieMYfp7yr3EdrePaQoG3Ghxs2wM98xSLRu8Xh56U",
               "39BEQuKehPQZDbqjWpsxDsL1sD6mrPnPQQMkkjcy7BLQ"}


# ── Datenbank ────────────────────────────────────────────────────────────

_SCHEMA = [False]
_LOCK = threading.Lock()


def _pg():
    pg = storage._init_pg()
    if not pg:
        return None
    if not _SCHEMA[0]:
        pg.execute("""CREATE TABLE IF NOT EXISTS iw_wallets (
            wallet TEXT NOT NULL, chain TEXT NOT NULL, score INTEGER NOT NULL DEFAULT 0, label TEXT,
            winners INTEGER DEFAULT 0, calls INTEGER DEFAULT 0, best_x DOUBLE PRECISION DEFAULT 0,
            total_x DOUBLE PRECISION DEFAULT 0, win_rate DOUBLE PRECISION, avg_entry_mcap DOUBLE PRECISION,
            source TEXT DEFAULT 'own', updated BIGINT NOT NULL, PRIMARY KEY (wallet, chain))""")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_iw_wallets_score ON iw_wallets(score DESC)")
        pg.execute("""CREATE TABLE IF NOT EXISTS iw_early (
            chain TEXT NOT NULL, token TEXT NOT NULL, wallet TEXT NOT NULL, rank INTEGER, buy_ts BIGINT,
            entry_mcap DOUBLE PRECISION, PRIMARY KEY (chain, token, wallet))""")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_iw_early_wallet ON iw_early(wallet)")
        pg.execute("""CREATE TABLE IF NOT EXISTS iw_tokens (
            chain TEXT NOT NULL, token TEXT NOT NULL, symbol TEXT, first_mcap DOUBLE PRECISION,
            peak_mcap DOUBLE PRECISION, multiple DOUBLE PRECISION, first_seen BIGINT, checked BIGINT,
            credited BOOLEAN DEFAULT FALSE, PRIMARY KEY (chain, token))""")
        # Seit 2.45: Herkunft und Markierung je frueher Kauf, Import-Score getrennt vom eigenen
        for sql in ("ALTER TABLE iw_early ADD COLUMN IF NOT EXISTS flag TEXT",
                    "ALTER TABLE iw_early ADD COLUMN IF NOT EXISTS src TEXT",
                    "ALTER TABLE iw_wallets ADD COLUMN IF NOT EXISTS import_score INTEGER"):
            pg.execute(sql)
        pg.execute("UPDATE iw_early SET src = CASE WHEN entry_mcap IS NOT NULL THEN 'import' ELSE 'own' END WHERE src IS NULL")
        pg.execute("UPDATE iw_wallets SET import_score = score WHERE source = 'import' AND import_score IS NULL")
        pg.execute("""CREATE TABLE IF NOT EXISTS iw_devs (
            chain TEXT NOT NULL, dev TEXT NOT NULL, token TEXT NOT NULL, ts BIGINT, PRIMARY KEY (chain, dev, token))""")
        _SCHEMA[0] = True
    return pg


def _q(sql, args=(), eins=False):
    pg = _pg()
    if not pg:
        return None if eins else []
    try:
        with _LOCK:
            cur = pg.execute(sql, args)
            if cur.description is None:
                return None
            return cur.fetchone() if eins else cur.fetchall()
    except Exception as e:
        log.debug(f"intel sql: {e}")
        return None if eins else []


def _k(chain, a):
    return a if chain == "solana" else str(a).lower()


# ── Solana RPC ───────────────────────────────────────────────────────────

def _sol_url():
    k = os.getenv("HELIUS_API_KEY", "").strip()
    if k:
        return "https://mainnet.helius-rpc.com/?api-key=" + k
    return os.getenv("SOLANA_RPC", "").strip() or "https://api.mainnet-beta.solana.com"


def _sol(m, p, timeout=20):
    for versuch in range(3):
        try:
            d = requests.post(_sol_url(), json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p}, headers=UA, timeout=timeout).json()
            if "result" in d:
                return d["result"]
            if "429" in str(d.get("error")) or "rate" in str(d.get("error")).lower():
                time.sleep(0.6 * (versuch + 1))
                continue
            return None
        except Exception:
            time.sleep(0.4)
    return None


PAR = int(os.getenv("INTEL_PARALLEL", "8"))


def _sol_batch(calls, n=20):
    """[(methode, params)] -> [result|None], in Paketen. Seit 2.40.2 laufen die Pakete parallel (INTEL_PARALLEL)."""
    out = [None] * len(calls)

    def paket(i):
        teil = calls[i:i + n]
        for versuch in range(3):
            try:
                d = requests.post(_sol_url(), json=[{"jsonrpc": "2.0", "id": i + j, "method": m, "params": p}
                                                    for j, (m, p) in enumerate(teil)], headers=UA, timeout=30).json()
                if isinstance(d, list):
                    for x in d:
                        if isinstance(x, dict) and "result" in x:
                            out[int(x["id"])] = x["result"]
                    break
            except Exception:
                pass
            time.sleep(0.6 * (versuch + 1))
    with ThreadPoolExecutor(max(1, PAR)) as ex:
        list(ex.map(paket, range(0, len(calls), n)))
    return out


def _start_sigs(mint, max_seiten=int(os.getenv("INTEL_SIG_PAGES", "30"))):
    """Signaturen vom Launch an (aelteste zuerst) oder None, wenn der Coin zu gross ist."""
    vor, letzte, davor = None, [], []
    for _ in range(max_seiten):
        opt = {"limit": 1000}
        if vor:
            opt["before"] = vor
        r = _sol("getSignaturesForAddress", [mint, opt])
        if r is None:
            return None
        if not r:
            break
        davor, letzte = letzte, r
        vor = r[-1].get("signature")
        if len(r) < 1000:
            break
    else:
        return None
    alle = [x for x in (davor + letzte) if x.get("signature") and not x.get("err")]
    return list(reversed(alle))


def _tx(sig):
    return ("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": int(os.getenv("SOL_TX_VERSION", "1")), "commitment": "confirmed"}])


def _schluessel(tx):
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    return [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]


def _token_delta(tx, mint):
    """{owner: rohe Aenderung} fuer diesen Mint in dieser Transaktion."""
    meta = (tx or {}).get("meta") or {}
    vor, nach = {}, {}
    for liste, ziel in ((meta.get("preTokenBalances") or [], vor), (meta.get("postTokenBalances") or [], nach)):
        for b in liste:
            if b.get("mint") != mint or not b.get("owner"):
                continue
            ziel[b["owner"]] = ziel.get(b["owner"], 0) + int(((b.get("uiTokenAmount") or {}).get("amount")) or 0)
    return {o: nach.get(o, 0) - vor.get(o, 0) for o in set(vor) | set(nach)}


def _geldgeber_aus(tx, wallet):
    """Wer hat diesem Wallet in dieser Transaktion SOL geschickt? (System-Transfer oder Bilanz)."""
    if not isinstance(tx, dict):
        return None
    meta = tx.get("meta") or {}
    msg = (tx.get("transaction") or {}).get("message") or {}
    gruppen = [msg.get("instructions") or []] + [i.get("instructions") or [] for i in meta.get("innerInstructions") or []]
    for g in gruppen:
        for ix in g:
            p = (ix or {}).get("parsed") or {}
            if not isinstance(p, dict) or p.get("type") not in ("transfer", "transferChecked", "createAccount"):
                continue
            info = p.get("info") or {}
            src, dst = info.get("source"), info.get("destination") or info.get("newAccount")
            if dst == wallet and src and src != wallet and int(info.get("lamports") or 0) > 5_000_000:
                return src
    namen = _schluessel(tx)
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if wallet not in namen or len(pre) != len(namen) or len(post) != len(namen):
        return None
    i = namen.index(wallet)
    if post[i] - pre[i] <= 5_000_000:
        return None
    best, verlust = None, 0
    for j, n in enumerate(namen):
        if j != i and pre[j] - post[j] > verlust:
            best, verlust = n, pre[j] - post[j]
    return best if verlust > 5_000_000 else None


def _boerse_sol(adr):
    if adr in BOERSEN_SOL:
        return True
    r = _sol("getSignaturesForAddress", [adr, {"limit": 200}])
    if isinstance(r, list) and len(r) >= 200:
        zeiten = [x.get("blockTime") for x in r if x.get("blockTime")]
        if zeiten and max(zeiten) - min(zeiten) < 6 * 3600:
            return True
    return False


def _scan_sol(mint):
    t0 = time.time()
    sigs = _start_sigs(mint)
    ohne_start = not sigs
    txs = []
    if sigs:
        txs = [t for t in _sol_batch([_tx(s["signature"]) for s in sigs[:140]], 20) if t]
        ohne_start = not txs
    if ohne_start:
        # Alter oder grosser Coin: der Launch liegt zu weit zurueck. Dann die groessten Halter pruefen
        # (frisch? gleicher Geldgeber?), ohne Bundles und Sniper.
        sup = int((((_sol("getTokenSupply", [mint]) or {}).get("value")) or {}).get("amount") or 0)
        halter = _halter_sol(mint, sup)
        jetzt = int(time.time())
        kaeufer = [{"wallet": h["address"], "slot": 0, "ts": jetzt, "raw": 0} for h in halter if not h.get("pool")][:20]
        if not kaeufer:
            return {"measured": False, "why": "history too long and no holders read"}
        bestand = {h["address"]: int(h["pct"] / 100 * sup) for h in halter}
        _alter_sol(kaeufer, juenger_als=7 * 86400)
        erg = _auswerten("solana", mint, None, 0, kaeufer, bestand, sup, halter, set(),
                         boerse=_boerse_sol, slot_key="slot", sniper_spanne=-1, ohne_start=True)
        return erg
    txs.sort(key=lambda t: (t.get("slot") or 0))
    erste = txs[0]
    dev = (_schluessel(erste) or [None])[0]
    start_slot = erste.get("slot") or 0
    kaeufer, verkaeufer = [], set()
    gesehen = set()
    for t in txs:
        for o, d in _token_delta(t, mint).items():
            if d < 0:
                verkaeufer.add(o)
            elif d > 0 and o not in gesehen:
                gesehen.add(o)
                kaeufer.append({"wallet": o, "slot": t.get("slot") or 0, "ts": t.get("blockTime") or 0, "raw": d,
                                "payer": (_schluessel(t) or [None])[0], "konto": weiter.konto_aus_tx(t, mint, o)})
    # Programme (Kurve, Pools) heraus: echte Wallets gehoeren dem System-Programm
    kand = [k["wallet"] for k in kaeufer[:FRUEH * 2]]
    besitzer = {}
    for i in range(0, len(kand), 100):
        r = _sol("getMultipleAccounts", [kand[i:i + 100], {"encoding": "base64"}]) or {}
        for a, v in zip(kand[i:i + 100], r.get("value") or []):
            besitzer[a] = (v or {}).get("owner")
    kaeufer = [k for k in kaeufer if besitzer.get(k["wallet"]) in (SYSTEM, None) and k["wallet"] not in (mint, dev)][:FRUEH]
    if not kaeufer:
        return {"measured": False, "why": "no early buyers found"}
    t1 = time.time()
    # Geldgeber und Alter der Kaeufer parallel zu Bestaenden und Haltern (seit 2.40.2)
    sup = int((((_sol("getTokenSupply", [mint]) or {}).get("value")) or {}).get("amount") or 0)
    wer = [k["wallet"] for k in kaeufer] + ([dev] if dev else [])
    with ThreadPoolExecutor(3) as ex:
        f_alter = ex.submit(_alter_sol, kaeufer)
        f_best = ex.submit(_sol_batch, [("getTokenAccountsByOwner", [w, {"mint": mint}, {"encoding": "jsonParsed"}]) for w in wer], 20)
        f_halter = ex.submit(_halter_sol, mint, sup)
        f_alter.result()
        best = f_best.result()
        halter = f_halter.result()
    bestand = {}
    for w, r in zip(wer, best):
        try:
            bestand[w] = sum(int(x["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for x in (r or {}).get("value") or [])
        except Exception:
            bestand[w] = None
    log.info(f"intel sol {mint[:8]}: history {t1 - t0:.1f}s, wallets {time.time() - t1:.1f}s")
    erg = _auswerten("solana", mint, dev, start_slot, kaeufer, bestand, sup, halter, verkaeufer,
                     boerse=_boerse_sol, slot_key="slot", sniper_spanne=2)
    # Weiterleitungen (seit 2.46): Tokens ohne Verkauf an andere Wallets geschickt
    t2 = time.time()
    try:
        dev_konto = next((weiter.konto_aus_tx(t, mint, dev) for t in txs[:5] if weiter.konto_aus_tx(t, mint, dev)), None) if dev else None
        erg["forwards"] = weiter.sol(mint, kaeufer, dev, dev_konto, sup, halter)
    except Exception as e:
        log.warning(f"intel forwards sol {mint[:8]}: {e}")
        erg["forwards"] = {"checked": False}
    log.info(f"intel sol {mint[:8]}: forwards {time.time() - t2:.1f}s")
    return erg


def _alter_sol(kaeufer, juenger_als=2 * 86400):
    """Je Kaeufer: wie alt ist das Wallet beim Kauf (frisch?), und wer hat es zuerst mit SOL gefuellt."""
    def alter(k):
        r = _sol("getSignaturesForAddress", [k["wallet"], {"limit": 100}]) or []
        aelteste = r[-1] if r else None
        zeit = (aelteste or {}).get("blockTime")
        k["tx_count"] = len(r)
        k["fresh"] = bool(len(r) < 100 and zeit and k["ts"] and (k["ts"] - zeit) < juenger_als)
        k["first_sig"] = (aelteste or {}).get("signature") if len(r) < 100 else None
        return k
    with ThreadPoolExecutor(max(6, PAR * 2)) as ex:
        kaeufer[:] = list(ex.map(alter, kaeufer))
    erste_tx = _sol_batch([_tx(k["first_sig"]) for k in kaeufer if k.get("first_sig")], 20)
    i = 0
    for k in kaeufer:
        if k.get("first_sig"):
            k["funder"] = _geldgeber_aus(erste_tx[i], k["wallet"])
            i += 1


def _halter_sol(mint, sup):
    r = _sol("getTokenLargestAccounts", [mint]) or {}
    konten = [x for x in (r.get("value") or []) if int(x.get("amount") or 0) > 0][:20]
    if not konten or not sup:
        return []
    info = _sol("getMultipleAccounts", [[k["address"] for k in konten], {"encoding": "jsonParsed"}]) or {}
    owner = []
    for v in info.get("value") or []:
        try:
            owner.append(v["data"]["parsed"]["info"]["owner"])
        except Exception:
            owner.append(None)
    progs = {}
    eigner = [o for o in owner if o]
    if eigner:
        r2 = _sol("getMultipleAccounts", [eigner, {"encoding": "base64"}]) or {}
        for o, v in zip(eigner, r2.get("value") or []):
            progs[o] = (v or {}).get("owner")
    sum_, konto = {}, {}
    for k, o in zip(konten, owner):
        if not o:
            continue
        sum_[o] = sum_.get(o, 0) + int(k.get("amount") or 0)
        konto.setdefault(o, k["address"])
    gesperrt = _locker_sol(sum_, konto, progs, sup)
    out = []
    for o, v in sorted(sum_.items(), key=lambda x: -x[1]):
        x = {"address": o, "pct": round(v / sup * 100, 3), "pool": progs.get(o) not in (SYSTEM, None), "konto": konto.get(o)}
        if gesperrt.get(o):
            x.update(locked=True, locker=gesperrt[o], pool=False)
        out.append(x)
    return out[:20]


def _locker_sol(sum_, konto, progs, sup):
    """{owner: 'Streamflow'|'Jupiter Lock'} fuer Bestaende in einem Token-Locker.
    Jupiter Lock: das Escrow-Konto gehoert dem Lock-Programm. Streamflow: der Eigentuemer ist eine PDA ohne
    eigenes Konto, dann zeigt die erste Transaktion des Token-Kontos, welches Programm es angelegt hat."""
    out = {}
    pruefen = []
    for o, v in sum_.items():
        p = progs.get(o)
        if p in LOCKER_SOL:
            out[o] = LOCKER_SOL[p]
        elif p is None and sup and v / sup >= 0.005:
            pruefen.append(o)
    if not pruefen:
        return out
    sigs = _sol_batch([("getSignaturesForAddress", [konto[o], {"limit": 50}]) for o in pruefen[:8]], 8)
    erste = []
    for o, r in zip(pruefen, sigs):
        if isinstance(r, list) and r and len(r) < 50:
            erste.append((o, r[-1].get("signature")))
    txs = _sol_batch([_tx(sg) for _, sg in erste], 8) if erste else []
    for (o, _), tx in zip(erste, txs):
        keys = set(_schluessel(tx))
        for ix in (((tx or {}).get("transaction") or {}).get("message") or {}).get("instructions") or []:
            if isinstance(ix, dict) and ix.get("programId"):
                keys.add(ix["programId"])
        for pid, name in LOCKER_SOL.items():
            if pid in keys:
                out[o] = name
                break
    return out


def holder_zahl_sol(mint):
    """Nur mit Helius (DAS). Bis 3000 genau, darueber '3000+'."""
    if not os.getenv("HELIUS_API_KEY", "").strip():
        return None
    eigner = set()
    for seite in range(1, 4):
        r = _sol("getTokenAccounts", {"mint": mint, "limit": 1000, "page": seite}) or {}
        ks = r.get("token_accounts") or []
        for k in ks:
            if int(k.get("amount") or 0) > 0:
                eigner.add(k.get("owner"))
        if len(ks) < 1000:
            return len(eigner)
    return f"{len(eigner)}+"


# ── EVM ──────────────────────────────────────────────────────────────────

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _evm(chain, m, p):
    import scan
    return scan._evm(chain, m, p)


def _evm_batch(chain, calls):
    if chain == "robinhood":
        import onchain
        return onchain._rpc_batch(calls)
    import scan
    url = os.getenv("RPC_" + chain.upper(), "").strip() or scan.RPC.get(chain)
    out = [None] * len(calls)
    for i in range(0, len(calls), 40):
        teil = calls[i:i + 40]
        for versuch in range(3):
            try:
                d = requests.post(url, json=[{"jsonrpc": "2.0", "id": i + j, "method": m, "params": p} for j, (m, p) in enumerate(teil)],
                                  headers=UA, timeout=30).json()
                if isinstance(d, list):
                    for x in d:
                        if "result" in x:
                            out[int(x["id"])] = x["result"]
                    break
            except Exception:
                pass
            time.sleep(0.6 * (versuch + 1))
    return out


BS_IDS = {"robinhood": 4663, "base": 8453, "ethereum": 1, "arbitrum": 42161}


def _bs(chain, pfad, params=None):
    """Blockscout. Mit BLOCKSCOUT_API_KEY ueber Blockscout Pro (api.blockscout.com, hoehere Grenzen,
    ein Schluessel fuer alle Ketten), sonst ueber den freien Explorer der Kette."""
    p = dict(params or {})
    key = os.getenv("BLOCKSCOUT_API_KEY", "").strip()
    cid = BS_IDS.get(chain)
    if key and cid:
        p["apikey"] = key
        if pfad == "/api":
            url, p["chain_id"] = "https://api.blockscout.com/v2/api", cid
        else:
            url = f"https://api.blockscout.com/{cid}{pfad}"
    else:
        b = BLOCKSCOUT.get(chain)
        if not b:
            return None
        url = b + pfad
    try:
        return requests.get(url, params=p, headers=UA, timeout=15).json()
    except Exception:
        return None


def _start_evm(chain, token):
    """(Launch-Block, Ersteller) ueber Torch-Launches oder den Explorer."""
    if chain == "robinhood":
        try:
            import onchain
            l = next((x for x in onchain.launches() if x["token"] == token), None)
            if l:
                return l["block"], l["creator"]
        except Exception:
            pass
    d = _bs(chain, f"/api/v2/addresses/{token}") or {}
    tx = d.get("creation_transaction_hash") or d.get("creation_tx_hash")
    dev = str(d.get("creator_address_hash") or "").lower() or None
    if tx:
        try:
            r = _evm(chain, "eth_getTransactionReceipt", [tx])
            return int(r["blockNumber"], 16), dev
        except Exception:
            pass
    return None, dev


def _logs(chain, token, von, max_logs=40000):
    """Alle Transfer-Logs ab dem Launch bis jetzt (seit 2.40 bis zum aktuellen Block, fuer Holder und Locks).
    Gibt (logs, vollstaendig) zurueck."""
    fenster = 500_000 if chain == "robinhood" else 50_000
    try:
        top = int(_evm(chain, "eth_blockNumber", []), 16)
    except Exception:
        return [], False
    out, b, fehler = [], von, 0
    while b <= top:
        bis = min(top, b + fenster)
        try:
            r = _evm(chain, "eth_getLogs", [{"address": token, "fromBlock": hex(b), "toBlock": hex(bis), "topics": [TRANSFER]}]) or []
        except Exception:
            fenster //= 4
            fehler += 1
            if fenster < 500 or fehler > 12:
                return out, False
            continue
        out += r
        if len(out) >= max_logs:
            return out, False
        b = bis + 1
    return out, True


def _geldgeber_evm(chain, w):
    d = _bs(chain, "/api", {"module": "account", "action": "txlist", "address": w, "sort": "asc", "page": 1, "offset": 5}) or {}
    liste = d.get("result") if isinstance(d.get("result"), list) else []
    erste = liste[0] if liste else None
    funder = None
    for x in liste:
        if str(x.get("to") or "").lower() == w and int(x.get("value") or 0) > 0:
            funder = str(x.get("from") or "").lower()
            break
    return (int(erste["timeStamp"]) if erste and erste.get("timeStamp") else None), funder


def _boerse_evm(chain, adr):
    if adr in BOERSEN_EVM:
        return True
    d = _bs(chain, f"/api/v2/addresses/{adr}/counters") or {}
    try:
        return int(d.get("transactions_count") or 0) > 5000
    except Exception:
        return False


def _entsperrung(chain, locker, txs):
    """Fruehestes Entsperrdatum aus den Lock-Ereignissen der Transaktionen, die Tokens zum Locker gebracht haben."""
    zeiten = []
    for tx in list(txs)[:6]:
        try:
            rc = _evm(chain, "eth_getTransactionReceipt", [tx]) or {}
        except Exception:
            continue
        for lg in rc.get("logs") or []:
            if str(lg.get("address") or "").lower() != locker or not lg.get("topics") or lg["topics"][0] not in LOCK_TOPICS:
                continue
            roh = (lg.get("data") or "0x")[2:]
            for i in range(0, len(roh), 64):
                try:
                    v = int(roh[i:i + 64], 16)
                except Exception:
                    continue
                if time.time() - 400 * 86400 < v < time.time() + 30 * 365 * 86400:
                    zeiten.append(v)
    return min(zeiten) if zeiten else None


def _scan_evm(chain, token):
    token = token.lower()
    block, dev = _start_evm(chain, token)
    if not block:
        return {"measured": False, "why": "launch not found"}
    logs, voll = _logs(chain, token, block)
    if not logs:
        return {"measured": False, "why": "no transfers read"}
    try:
        sup = int(_evm(chain, "eth_call", [{"to": token, "data": "0x18160ddd"}, "latest"]), 16)
    except Exception:
        sup = 0
    if not sup:
        try:
            sup = int((_evm_batch(chain, [("eth_call", [{"to": token, "data": "0x18160ddd"}, "latest"])]) or [None])[0] or "0x0", 16)
        except Exception:
            sup = 0
    klein = sup * 0.0002 if sup else 0
    infra = {token} | TOTE
    if chain == "robinhood":
        try:
            import onchain
            infra |= {onchain.FABRIK.lower()} | {a.lower() for a in onchain._pool_adressen()}
        except Exception:
            pass
    ts_block, netto, zum_locker = {}, {}, {}
    lock = LOCKER_EVM.get(chain, {})
    infra |= set(lock)
    kaeufer, verkaeufer, gesehen = [], set(), set()
    for l in logs:
        try:
            von = "0x" + l["topics"][1][-40:]
            an = "0x" + l["topics"][2][-40:]
            menge = int(l.get("data") or "0x0", 16)
            b = int(l["blockNumber"], 16)
        except Exception:
            continue
        netto[von] = netto.get(von, 0) - menge
        netto[an] = netto.get(an, 0) + menge
        if an in lock:
            zum_locker.setdefault(an, set()).add(l.get("transactionHash"))
        if von not in TOTE:
            verkaeufer.add(von)
        if len(kaeufer) >= FRUEH * 3 or an in infra or menge <= klein or an in gesehen:
            continue
        gesehen.add(an)
        kaeufer.append({"wallet": an, "slot": b, "raw": menge, "tx": l.get("transactionHash")})
    # Vertraege (Pools, Kurve, Router, Locker) heraus. Unklare Antworten einzeln nachfragen, nie als Wallet zaehlen.
    codes = _evm_batch(chain, [("eth_getCode", [k["wallet"], "latest"]) for k in kaeufer])
    for i, (k, c) in enumerate(zip(kaeufer, codes)):
        if c is None:
            try:
                codes[i] = _evm(chain, "eth_getCode", [k["wallet"], "latest"])
            except Exception:
                codes[i] = "?"
    kaeufer = [k for k, c in zip(kaeufer, codes) if c in ("0x", "0x0") and k["wallet"] != dev][:FRUEH]
    if not kaeufer:
        return {"measured": False, "why": "no early buyers found"}
    bl = sorted({k["slot"] for k in kaeufer} | {block})
    zeiten = _evm_batch(chain, [("eth_getBlockByNumber", [hex(b), False]) for b in bl])
    for b, z in zip(bl, zeiten):
        if z and z.get("timestamp"):
            ts_block[b] = int(z["timestamp"], 16)
    for k in kaeufer:
        k["ts"] = ts_block.get(k["slot"], 0)

    def info(k):
        erst, funder = _geldgeber_evm(chain, k["wallet"])
        k["fresh"] = bool(erst and k["ts"] and (k["ts"] - erst) < 2 * 86400)
        k["funder"] = funder
        return k
    if BLOCKSCOUT.get(chain):
        with ThreadPoolExecutor(6) as ex:
            kaeufer = list(ex.map(info, kaeufer))
    else:
        noncen = _evm_batch(chain, [("eth_getTransactionCount", [k["wallet"], "latest"]) for k in kaeufer])
        for k, n in zip(kaeufer, noncen):
            k["fresh"] = bool(n and int(n, 16) <= 3)
    # Bestaende: aus den Logs, wenn sie vollstaendig sind, sonst einzeln abfragen
    wer = [k["wallet"] for k in kaeufer] + ([dev] if dev else [])
    if voll:
        bestand = {w: max(0, netto.get(w, 0)) for w in wer}
    else:
        best = _evm_batch(chain, [("eth_call", [{"to": token, "data": "0x70a08231" + "0" * 24 + w[2:]}, "latest"]) for w in wer])
        bestand = {w: (int(b, 16) if b else None) for w, b in zip(wer, best)}
    # Holder-Karte und Anzahl aus den Logs
    halter, anzahl = [], None
    if voll and sup:
        positiv = {a: v for a, v in netto.items() if v > 0 and a not in TOTE}
        anzahl = len(positiv)
        top = sorted(positiv.items(), key=lambda x: -x[1])[:25]
        cs = _evm_batch(chain, [("eth_getCode", [a, "latest"]) for a, _ in top])
        for (a, v), c in zip(top, cs):
            x = {"address": a, "pct": round(v / sup * 100, 3), "pool": bool(c and c not in ("0x", "0x0")) and a not in lock}
            if a in lock:
                x.update(locked=True, locker=lock[a])
                u = _entsperrung(chain, a, zum_locker.get(a, set()))
                if u:
                    x["unlock_ts"] = u
            halter.append(x)
    erg = _auswerten(chain, token, dev, block, kaeufer, bestand, sup, halter, verkaeufer,
                     boerse=lambda a: _boerse_evm(chain, a), slot_key="slot", sniper_spanne=2)
    if anzahl:
        erg["holders_count"] = anzahl
    try:
        erg["forwards"] = weiter.evm(chain, token, logs, infra, sup, netto, [k["wallet"] for k in kaeufer], dev, voll)
    except Exception as e:
        log.warning(f"intel forwards {chain} {token[:10]}: {e}")
        erg["forwards"] = {"checked": False}
    return erg


# ── Auswertung (fuer beide Welten gleich) ────────────────────────────────

def _auswerten(chain, token, dev, start, kaeufer, bestand, sup, halter, verkaeufer, boerse, slot_key, sniper_spanne, ohne_start=False):
    pct = lambda w: (bestand.get(w) or 0) / sup * 100 if sup else 0.0
    # Bundles: mehrere fruehe Kaeufer im selben Block/Slot; Sniper: im Launch-Block oder direkt danach
    je_slot = {}
    for k in kaeufer if not ohne_start else []:
        je_slot.setdefault(k[slot_key], []).append(k["wallet"])
    gebuendelt = {w for ws in je_slot.values() if len(ws) >= 2 for w in ws}
    sniper = {k["wallet"] for k in kaeufer if k[slot_key] - start <= sniper_spanne} if not ohne_start else set()
    gebuendelt |= sniper
    # Gleiche Geldgeber
    gruppen = {}
    for k in kaeufer:
        f = k.get("funder")
        if f and f != k["wallet"]:
            gruppen.setdefault(f, []).append(k["wallet"])
    if dev:
        for f, ws in gruppen.items():
            if f == dev:
                ws.append(dev) if dev not in ws else None
    cluster, boersen = [], set()
    kandidaten = [f for f, ws in gruppen.items() if len(ws) >= 2]

    def pruef(f):
        try:
            return f, bool(boerse(f))
        except Exception:
            return f, False
    with ThreadPoolExecutor(max(1, min(8, len(kandidaten) or 1))) as ex:
        ist_b = dict(ex.map(pruef, kandidaten))
    for f, ws in sorted(gruppen.items(), key=lambda x: -len(x[1])):
        if len(ws) < 2:
            continue
        ist_boerse = ist_b.get(f, False)
        if ist_boerse:
            boersen.add(f)
            continue
        cluster.append({"type": "funding", "funder": f, "members": ws, "wallets": len(ws),
                        "supply_pct": round(sum(pct(w) for w in ws), 2)})
    for s, ws in sorted(je_slot.items()):
        if len(ws) >= 3 and not any(set(ws) <= set(c["members"]) for c in cluster):
            cluster.append({"type": "timing", "funder": None, "members": ws, "wallets": len(ws),
                            "supply_pct": round(sum(pct(w) for w in ws), 2)})
    for i, c in enumerate(cluster[:12], 1):
        c["id"] = i
    cluster = cluster[:12]
    in_cluster = {w for c in cluster for w in c["members"]}
    frisch = [k for k in kaeufer if k.get("fresh")]
    bewertet = _bewertete([k["wallet"] for k in kaeufer], chain)
    dev_hist = _dev_historie(chain, dev, token) if dev else {}
    out = {
        "measured": True, "chain": chain, "token": token, "ts": int(time.time()), "dev": dev,
        "basis": "top holders (launch too far back)" if ohne_start else "first buyers",
        "dev_holds_pct": round(pct(dev), 3) if dev and bestand.get(dev) is not None else None,
        "early_buyers": len(kaeufer),
        "bundled_supply_pct": round(sum(pct(w) for w in gebuendelt), 2),
        "fresh_wallet_pct": round(len(frisch) / len(kaeufer) * 100, 1),
        "snipers": len(sniper), "clusters": len(cluster),
        "same_funder_groups": sum(1 for c in cluster if c["type"] == "funding"),
        "cluster_supply_pct": round(sum(pct(w) for w in in_cluster), 2),
        "still_holding_pct": round(sum(1 for k in kaeufer if (bestand.get(k["wallet"]) or 0) > 0) / len(kaeufer) * 100, 1),
        "cluster_map": cluster, "holders_map": halter or [],
        "rated": bewertet, "dev_history": dev_hist,
        "buyers": [{"wallet": k["wallet"], "slot": k[slot_key], "ts": k.get("ts"), "fresh": bool(k.get("fresh")),
                    "pct": round(pct(k["wallet"]), 3), "bundled": k["wallet"] in gebuendelt,
                    "cluster": next((c["id"] for c in cluster if k["wallet"] in c["members"]), None)} for k in kaeufer],
    }
    return out


def _bewertete(wallets, chain):
    if not wallets:
        return {"found": 0, "checked": 0}
    ks = [_k(chain, w) for w in wallets]
    rows = _q("SELECT wallet, score, winners, best_x FROM iw_wallets WHERE chain=%s AND wallet = ANY(%s) AND score > 0 ORDER BY score DESC",
              (chain, ks)) or []
    return {"found": len(rows), "checked": len(wallets), "best": rows[0][1] if rows else None,
            "wallets": [{"wallet": r[0], "score": r[1], "winners": r[2], "best_x": r[3]} for r in rows[:8]]}


def _dev_historie(chain, dev, token):
    rows = _q("SELECT d.token, t.multiple, t.symbol FROM iw_devs d LEFT JOIN iw_tokens t ON t.chain=d.chain AND t.token=d.token "
              "WHERE d.chain=%s AND d.dev=%s AND d.token<>%s", (chain, _k(chain, dev), _k(chain, token))) or []
    gelaufen = [r for r in rows if (r[1] or 0) >= LAUF_X]
    out = {"seen": len(rows), "ran": len(gelaufen), "examples": [r[2] for r in rows if r[2]][:5]}
    if chain == "robinhood":
        try:
            import onchain
            n = sum(1 for l in onchain.launches() if l.get("creator") == _k(chain, dev) and l["token"] != _k(chain, token))
            out["seen"] = max(out["seen"], n)
        except Exception:
            pass
    return out


def _merken(chain, token, ergebnis, mcap, symbol):
    """Fruehe Kaeufer, Dev und den Coin speichern: daraus wird spaeter die Bewertung."""
    if not ergebnis.get("measured"):
        return
    jetzt = int(time.time())
    tk = _k(chain, token)
    _q("INSERT INTO iw_tokens (chain, token, symbol, first_mcap, peak_mcap, multiple, first_seen, checked) "
       "VALUES (%s,%s,%s,%s,%s,1,%s,%s) ON CONFLICT (chain, token) DO NOTHING",
       (chain, tk, symbol, mcap, mcap, jetzt, jetzt))
    if ergebnis.get("dev"):
        _q("INSERT INTO iw_devs (chain, dev, token, ts) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
           (chain, _k(chain, ergebnis["dev"]), tk, jetzt))
    dev = _k(chain, ergebnis.get("dev") or "")
    dev_gruppe = {_k(chain, w) for c in ergebnis.get("cluster_map") or [] if c.get("funder") == ergebnis.get("dev")
                  for w in c.get("members") or []}
    for i, b in enumerate(ergebnis.get("buyers") or [], 1):
        w = _k(chain, b["wallet"])
        if dev and w == dev:
            continue
        # Bundles und Sniper (gleicher Block, Launch-Block) und die Gruppe des Devs bekommen nie Punkte
        flag = "bundle" if b.get("bundled") else ("dev_group" if w in dev_gruppe else None)
        _q("INSERT INTO iw_early (chain, token, wallet, rank, buy_ts, entry_mcap, flag, src) VALUES (%s,%s,%s,%s,%s,%s,%s,'own') "
           "ON CONFLICT DO NOTHING", (chain, tk, w, i, b.get("ts"), None, flag))


# ── Oeffentliche Wege ────────────────────────────────────────────────────

_LAEUFT = {}


def _cache_key(chain, token):
    return "iw_scan_" + chain + "_" + _k(chain, token)


def aus_cache(chain, token):
    try:
        d = json.loads(storage.kv_get(_cache_key(chain, token)) or "null")
    except Exception:
        d = None
    return d


def scannen(chain, token, mcap=None, symbol=None):
    t0 = time.time()
    try:
        if chain == "solana":
            erg = _scan_sol(token)
        elif chain in ("robinhood", "base", "ethereum", "arbitrum", "bsc"):
            erg = _scan_evm(chain, token)
        else:
            erg = {"measured": False, "why": "chain not covered"}
    except Exception as e:
        log.warning(f"intel scan {chain} {token[:10]}: {e}")
        erg = {"measured": False, "why": "scan failed"}
    erg["took_s"] = round(time.time() - t0, 1)
    log.info(f"intel scan {chain} {token[:10]}: {erg['took_s']}s, measured={erg.get('measured')}")
    erg["ts"] = int(time.time())
    try:
        storage.kv_set(_cache_key(chain, token), json.dumps(erg, default=str))
    except Exception:
        pass
    try:
        _merken(chain, token, erg, mcap, symbol)
    except Exception as e:
        log.debug(f"intel merken: {e}")
    return erg


def tiefe(chain, token, mcap=None, symbol=None, starten=True):
    """Fertiger Scan aus dem Cache ({'state':'ready', ...}), sonst im Hintergrund starten ({'state':'running'})."""
    c = aus_cache(chain, token)
    if c and time.time() - (c.get("ts") or 0) < CACHE_S:
        return dict(c, state="ready" if c.get("measured") else "unavailable", age_min=int((time.time() - c["ts"]) / 60))
    key = (chain, _k(chain, token))
    if key in _LAEUFT and time.time() - _LAEUFT[key] < 180:
        return {"state": "running"}
    if not starten:
        return {"state": "none"}
    _LAEUFT[key] = time.time()

    def lauf():
        try:
            scannen(chain, token, mcap, symbol)
        finally:
            _LAEUFT.pop(key, None)
    threading.Thread(target=lauf, daemon=True).start()
    return {"state": "running"}


def wallet(adr):
    a = str(adr or "").strip()
    if not a:
        return {"error": "Which wallet?"}
    chain_k = a.lower() if a.startswith("0x") else a
    rows = _q("SELECT chain, score, label, winners, calls, best_x, total_x, win_rate, avg_entry_mcap, source FROM iw_wallets "
              "WHERE wallet=%s ORDER BY score DESC", (chain_k,)) or []
    frueh = _q("SELECT e.chain, e.token, t.symbol, t.multiple, e.rank FROM iw_early e LEFT JOIN iw_tokens t "
               "ON t.chain=e.chain AND t.token=e.token WHERE e.wallet=%s ORDER BY COALESCE(t.multiple,0) DESC LIMIT 15", (chain_k,)) or []
    if not rows and not frueh:
        return {"wallet": a, "known": False,
                "meaning": "Not in our scored set. We have not seen it early in a coin that ran. That does not make it bad."}
    rang = None
    if rows:
        r = _q("SELECT COUNT(*) FROM iw_wallets WHERE score > %s", (rows[0][1],), eins=True)
        gesamt = _q("SELECT COUNT(*) FROM iw_wallets", (), eins=True)
        rang = {"rank": (r[0] + 1) if r else None, "of": gesamt[0] if gesamt else None}
    return {"wallet": a, "known": bool(rows), "standing": rang,
            "scores": [{"chain": x[0], "score": x[1], "label": x[2], "winners": x[3], "calls": x[4], "best_x": x[5],
                        "win_rate": x[7]} for x in rows],
            "early_in": [{"chain": x[0], "token": x[1], "symbol": x[2], "token_multiple": round(x[3], 1) if x[3] else None,
                          "buyer_rank": x[4]} for x in frueh]}


def laeufer(stunden=72, n=10):
    seit = int(time.time() - float(stunden or 72) * 3600)
    rows = _q("SELECT chain, token, symbol, first_mcap, peak_mcap, multiple, first_seen FROM iw_tokens "
              "WHERE first_seen >= %s AND multiple >= 2 ORDER BY multiple DESC LIMIT %s", (seit, int(n or 10))) or []
    return {"runners": [{"chain": r[0], "token": r[1], "symbol": r[2], "mcap_when_first_checked": r[3], "peak_mcap": r[4],
                         "multiple": round(r[5], 1) if r[5] else None} for r in rows],
            "meaning": "Coins someone checked with Your Engine that went on to run, with the multiple from the first check to the peak."}


# ── Nachverfolgen und bewerten ───────────────────────────────────────────

def _punkte(winners, best_x, total_x, calls):
    """0 bis 99: oft frueh in Gewinnern, und wie gross die Gewinner waren."""
    if not winners:
        return 0
    s = 35 + 16 * math.log2(1 + winners) + 9 * math.log10(max(1.0, best_x)) + 4 * math.log10(max(1.0, total_x))
    if calls and winners / calls < 0.15:
        s -= 10
    return int(max(1, min(99, round(s))))


HALBWERT_TAGE = float(os.getenv("INTEL_HALF_LIFE_DAYS", "30"))


def _punkte2(a_w, w_w, wins, best_x):
    """Seit 2.45: echte Trefferquote statt nur Treffer. a_w und w_w sind zeitgewichtete Versuche und Treffer
    (alte zaehlen weniger). Ein einzelner Glueckstreffer bleibt unter 36, erst wiederholte Treffer zaehlen."""
    if wins < 1 or a_w <= 0:
        return 0
    quote = (w_w + 1) / (a_w + 6)          # vorsichtige Quote: wenige Versuche zaehlen nicht als 100 %
    s = 20 + 250 * (quote - 0.15) + 5 * math.log2(1 + w_w) + 6 * math.log10(max(1.0, best_x))
    if wins < 2 or w_w < 1:
        s = min(s, 35)  # ein Treffer oder nur alte Treffer: nie auf der Beobachtungsliste
    return int(max(1, min(99, round(s))))


def neu_bewerten():
    """Alle eigenen Bewertungen neu berechnen (stuendlich): Versuche = jeder Coin, bei dem das Wallet unter den
    ersten 30 Kaeufern war und dessen Ausgang feststeht (aelter als 24 h). Nieten zaehlen mit, Bundles, Sniper und
    die Gruppe des Devs zaehlen nie, alte Coins verlieren mit der Halbwertszeit INTEL_HALF_LIFE_DAYS an Gewicht.
    Importierte Wallets behalten ihren Import-Score, bis genug eigene Daten da sind (5 Versuche)."""
    jetzt = int(time.time())
    rows = _q("""SELECT e.chain, e.wallet,
                   SUM(power(0.5, GREATEST(0, %s - t.first_seen) / 86400.0 / %s)),
                   SUM(CASE WHEN t.multiple >= %s THEN power(0.5, GREATEST(0, %s - t.first_seen) / 86400.0 / %s) ELSE 0 END),
                   COUNT(*), COUNT(*) FILTER (WHERE t.multiple >= %s), MAX(t.multiple)
                 FROM iw_early e JOIN iw_tokens t ON t.chain = e.chain AND t.token = e.token
                 WHERE e.rank <= 30 AND e.flag IS NULL AND COALESCE(e.src, 'own') = 'own' AND t.first_seen < %s
                 GROUP BY e.chain, e.wallet
                 HAVING COUNT(*) FILTER (WHERE t.multiple >= %s) >= 1
                    OR e.wallet IN (SELECT wallet FROM iw_wallets WHERE score > 0)""",
              (jetzt, HALBWERT_TAGE, LAUF_X, jetzt, HALBWERT_TAGE, LAUF_X, jetzt - 86400, LAUF_X)) or []
    n = 0
    for chain, w, a_w, w_w, a, wins, best in rows:
        eigen = _punkte2(float(a_w or 0), float(w_w or 0), int(wins or 0), float(best or 0))
        alt = _q("SELECT import_score FROM iw_wallets WHERE wallet=%s AND chain=%s", (w, chain), eins=True)
        imp = int(alt[0] or 0) if alt and alt[0] is not None else None
        neu = eigen if (imp is None or a >= 5) else max(eigen, imp)
        _q("INSERT INTO iw_wallets (wallet, chain, score, winners, calls, best_x, win_rate, updated) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
           "ON CONFLICT (wallet, chain) DO UPDATE SET score=EXCLUDED.score, winners=EXCLUDED.winners, calls=EXCLUDED.calls, "
           "best_x=GREATEST(iw_wallets.best_x, EXCLUDED.best_x), win_rate=EXCLUDED.win_rate, updated=EXCLUDED.updated",
           (w, chain, neu, int(wins or 0), int(a or 0), float(best or 0), round(int(wins or 0) / max(1, int(a or 0)), 3), jetzt))
        n += 1
    log.info(f"intel: rescored {n} wallets")
    return n


def verfolgen(max_n=60):
    """Coins aus den letzten 14 Tagen nachsehen; ab LAUF_X die fruehen Kaeufer bewerten."""
    seit = int(time.time() - 14 * 86400)
    rows = _q("SELECT chain, token, first_mcap, peak_mcap, credited FROM iw_tokens WHERE first_seen >= %s "
              "AND first_mcap > 0 ORDER BY COALESCE(checked,0) ASC LIMIT %s", (seit, int(max_n))) or []
    import scan
    je_chain = {}
    for r in rows:
        je_chain.setdefault(r[0], []).append(r)
    for chain, liste in je_chain.items():
        dex = scan.KETTEN.get(chain, (None, None))[1]
        for i in range(0, len(liste), 30):
            teil = liste[i:i + 30]
            try:
                d = requests.get(f"https://api.dexscreener.com/tokens/v1/{dex}/" + ",".join(x[1] for x in teil), headers=UA, timeout=15).json()
            except Exception:
                continue
            mc = {}
            for p in d if isinstance(d, list) else []:
                a = _k(chain, (p.get("baseToken") or {}).get("address") or "")
                v = float(p.get("marketCap") or p.get("fdv") or 0)
                mc[a] = max(mc.get(a, 0), v)
            jetzt = int(time.time())
            for (c, tok, first, peak, credited) in teil:
                v = mc.get(tok)
                if not v:
                    _q("UPDATE iw_tokens SET checked=%s WHERE chain=%s AND token=%s", (jetzt, c, tok))
                    continue
                peak = max(peak or 0, v)
                mult = peak / first if first else 1
                _q("UPDATE iw_tokens SET peak_mcap=%s, multiple=%s, checked=%s WHERE chain=%s AND token=%s",
                   (peak, mult, jetzt, c, tok))
                if mult >= LAUF_X and not credited:
                    _gutschreiben(c, tok, mult)


def _gutschreiben(chain, token, mult):
    frueh = _q("SELECT wallet FROM iw_early WHERE chain=%s AND token=%s AND rank <= 30 AND flag IS NULL", (chain, token)) or []
    jetzt = int(time.time())
    for (w,) in frueh:
        alt = _q("SELECT winners, calls, best_x, total_x, score FROM iw_wallets WHERE wallet=%s AND chain=%s", (w, chain), eins=True)
        win, calls, best, total, score = alt if alt else (0, 0, 0.0, 0.0, 0)
        win, calls, best, total = (win or 0) + 1, (calls or 0) + 1, max(best or 0, mult), (total or 0) + mult
        neu = max(score or 0, _punkte(win, best, total, calls))
        _q("INSERT INTO iw_wallets (wallet, chain, score, winners, calls, best_x, total_x, updated) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
           "ON CONFLICT (wallet, chain) DO UPDATE SET score=EXCLUDED.score, winners=EXCLUDED.winners, calls=EXCLUDED.calls, "
           "best_x=EXCLUDED.best_x, total_x=EXCLUDED.total_x, updated=EXCLUDED.updated",
           (w, chain, neu, win, calls, best, total, jetzt))
    _q("UPDATE iw_tokens SET credited=TRUE WHERE chain=%s AND token=%s", (chain, token))
    log.info(f"intel: {chain} {token[:10]} ran {mult:.1f}x, {len(frueh)} early wallets credited")


# ── Einmalige Uebernahme einer Wallet-Datenbank ─────────────────────────

def importieren(url):
    """Liest wallet_scores, runners und smart_wallets einer anderen Postgres-Datenbank (nur lesen) und
    uebernimmt sie in die eigenen Tabellen. Fortschritt im kv unter iw_import."""
    import psycopg
    stand = {"start": int(time.time()), "wallets": 0, "tokens": 0, "early": 0, "done": False, "error": None}

    def melden():
        storage.kv_set("iw_import", json.dumps(stand))
    melden()
    try:
        src = psycopg.connect(url, autocommit=True, connect_timeout=10)
        src.execute("SET default_transaction_read_only = on")
        jetzt = int(time.time())
        cur = src.cursor(name="iw_w")
        cur.execute("SELECT wallet, COALESCE(chain,'solana'), puter_score, label, winners, calls, best_call, total_call, "
                    "win_rate, avg_entry_mcap FROM wallet_scores")
        while True:
            rows = cur.fetchmany(2000)
            if not rows:
                break
            for r in rows:
                ch = r[1] or "solana"
                _q("INSERT INTO iw_wallets (wallet, chain, score, label, winners, calls, best_x, total_x, win_rate, avg_entry_mcap, source, updated, import_score) "
                   "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'import',%s,%s) ON CONFLICT (wallet, chain) DO UPDATE SET "
                   "import_score=EXCLUDED.import_score, score=GREATEST(iw_wallets.score, EXCLUDED.score), winners=GREATEST(iw_wallets.winners, EXCLUDED.winners), "
                   "calls=GREATEST(iw_wallets.calls, EXCLUDED.calls), best_x=GREATEST(iw_wallets.best_x, EXCLUDED.best_x), "
                   "total_x=GREATEST(iw_wallets.total_x, EXCLUDED.total_x), win_rate=EXCLUDED.win_rate, "
                   "avg_entry_mcap=EXCLUDED.avg_entry_mcap, label=EXCLUDED.label, updated=EXCLUDED.updated",
                   (_k(ch, r[0]), ch, int(r[2] or 0), r[3], r[4] or 0, r[5] or 0, r[6] or 0, r[7] or 0, r[8], r[9], jetzt, int(r[2] or 0)))
            stand["wallets"] += len(rows)
            melden()
        cur.close()
        cur = src.cursor(name="iw_t")
        cur.execute("SELECT mint, COALESCE(chain,'solana'), symbol, first_mcap, peak_mcap, multiple, detected_at FROM runners")
        while True:
            rows = cur.fetchmany(2000)
            if not rows:
                break
            for r in rows:
                ch = r[1] or "solana"
                _q("INSERT INTO iw_tokens (chain, token, symbol, first_mcap, peak_mcap, multiple, first_seen, checked, credited) "
                   "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,TRUE) ON CONFLICT (chain, token) DO NOTHING",
                   (ch, _k(ch, r[0]), r[2], r[3], r[4], r[5], r[6], jetzt))
            stand["tokens"] += len(rows)
            melden()
        cur.close()
        try:
            cur = src.cursor(name="iw_e")
            cur.execute("SELECT sw.wallet, sw.mint, COALESCE(r.chain,'solana'), sw.rank, sw.buy_time, sw.entry_mcap "
                        "FROM smart_wallets sw LEFT JOIN runners r ON r.mint = sw.mint")
            while True:
                rows = cur.fetchmany(5000)
                if not rows:
                    break
                for r in rows:
                    ch = r[2] or "solana"
                    _q("INSERT INTO iw_early (chain, token, wallet, rank, buy_ts, entry_mcap, src) VALUES (%s,%s,%s,%s,%s,%s,'import') "
                       "ON CONFLICT DO NOTHING", (ch, _k(ch, r[1]), _k(ch, r[0]), r[3], r[4], r[5]))
                stand["early"] += len(rows)
                melden()
            cur.close()
        except Exception as e:
            stand["early_note"] = str(e)[:120]
        src.close()
        stand["done"] = True
    except Exception as e:
        stand["error"] = str(e)[:200]
    stand["end"] = int(time.time())
    melden()
    return stand


def stand():
    try:
        imp = json.loads(storage.kv_get("iw_import") or "null")
    except Exception:
        imp = None
    w = _q("SELECT COUNT(*), COUNT(*) FILTER (WHERE score > 0), COUNT(*) FILTER (WHERE source='own') FROM iw_wallets", (), eins=True)
    t = _q("SELECT COUNT(*), COUNT(*) FILTER (WHERE multiple >= %s) FROM iw_tokens", (LAUF_X,), eins=True)
    e = _q("SELECT COUNT(*) FROM iw_early", (), eins=True)
    return {"wallets": w[0] if w else 0, "scored": w[1] if w else 0, "own": w[2] if w else 0,
            "tokens": t[0] if t else 0, "runners": t[1] if t else 0, "early_rows": e[0] if e else 0,
            "import": imp, "helius": bool(os.getenv("HELIUS_API_KEY", "").strip())}
