"""
weiter.py: Token-Weiterleitungen (Forwards) im Tiefen-Scan, seit 2.46.

Frage: Haben fruehe Kaeufer oder der Dev ihre Tokens nicht verkauft, sondern einfach an andere Wallets
geschickt? So wird Angebot versteckt: die Top-Holder-Liste sieht sauber aus, aber dieselben Leute halten
ueber neue Wallets weiter. Erkannt wird nur eine reine Uebertragung von Wallet zu Wallet, kein Verkauf:
  EVM (Robinhood, Base, Ethereum, BNB, Arbitrum): aus allen Transfer-Logs des Coins. Absender und Empfaenger
      sind beide normale Wallets (kein Vertrag), und in derselben Transaktion bewegt kein Pool, Router oder
      Vertrag diesen Coin.
  Solana: aus den Transaktionen der Token-Konten der fruehen Kaeufer und des Devs. Der Absender verliert
      Tokens, ein normales Wallet (System-Konto) bekommt sie, und der Absender bekommt dafuer kein SOL.
Dazu: frische Empfaenger, was die Empfaenger heute noch halten, Sammel-Wallets (bekommen von mehreren
Absendern) und Sammel-Wallets, die das schon bei anderen Coins getan haben (Tabelle iw_fwd).

  evm(chain, token, logs, infra, sup, netto, frueh, dev, codes)   -> dict
  sol(mint, kaeufer, dev, dev_konto, sup)                         -> dict
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor

from logger import log

MIN_ANTEIL = float(os.getenv("FWD_MIN_SUPPLY_PCT", "0.02"))   # kleinere Uebertragungen sind Staub
MAX_TX_SOL = int(os.getenv("FWD_MAX_TX_SOL", "320"))
SYSTEM = "11111111111111111111111111111111"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
NULL = "0x0000000000000000000000000000000000000000"


# ── Speicher: Sammel-Wallets ueber alle Coins ─────────────────────────────

def _q(sql, args=(), eins=False):
    import intel
    pg = intel._pg()
    if not pg:
        return None
    if not _q.__dict__.get("schema"):
        pg.execute("""CREATE TABLE IF NOT EXISTS iw_fwd (chain TEXT NOT NULL, token TEXT NOT NULL, src TEXT NOT NULL,
                      dst TEXT NOT NULL, pct DOUBLE PRECISION, ts BIGINT, PRIMARY KEY (chain, token, src, dst))""")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_iw_fwd_dst ON iw_fwd(chain, dst)")
        _q.schema = True
    return intel._q(sql, args, eins)


def _speichern(chain, token, liste):
    jetzt = int(time.time())
    for f in liste:
        try:
            _q("INSERT INTO iw_fwd (chain, token, src, dst, pct, ts) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (chain, token, src, dst) "
               "DO UPDATE SET pct=EXCLUDED.pct, ts=EXCLUDED.ts", (chain, token, f["from"], f["to"], f["pct"], jetzt))
        except Exception as e:
            log.debug(f"weiter speichern: {e}")


def _anderswo(chain, token, ziele):
    """Wie viele ANDERE Coins hat jedes Ziel-Wallet schon gesammelt? {wallet: anzahl}"""
    if not ziele:
        return {}
    try:
        rows = _q("SELECT dst, COUNT(DISTINCT token) FROM iw_fwd WHERE chain=%s AND dst = ANY(%s) AND token <> %s GROUP BY dst",
                  (chain, list(ziele), token)) or []
        return {r[0]: int(r[1]) for r in rows}
    except Exception:
        return {}


def _zusammenfassen(chain, token, liste, halten, frisch, frueh, dev, sup):
    """liste: [{from, to, raw, tx}] -> Ergebnis fuer Report, Karte und Bewertung."""
    if not liste:
        return {"checked": True, "count": 0}
    pct = lambda raw: round(raw / sup * 100, 3) if sup else None
    # gleiche Paare zusammenfassen
    paare = {}
    for f in liste:
        k = (f["from"], f["to"])
        p = paare.setdefault(k, {"from": f["from"], "to": f["to"], "raw": 0, "n": 0})
        p["raw"] += f["raw"]
        p["n"] += 1
    paare = list(paare.values())
    for p in paare:
        p["pct"] = pct(p["raw"])
        p["fresh"] = bool(frisch.get(p["to"]))
        p["holds_pct"] = pct(halten[p["to"]]) if halten.get(p["to"]) is not None else None
        p["src"] = "dev" if dev and p["from"] == dev else ("early buyer" if p["from"] in frueh else "holder")
    ziele = {}
    for p in paare:
        ziele.setdefault(p["to"], set()).add(p["from"])
    andere = _anderswo(chain, token, set(ziele))
    sammler = []
    for z, quellen in ziele.items():
        if len(quellen) >= 2 or andere.get(z):
            sammler.append({"wallet": z, "from_wallets": len(quellen), "other_tokens": andere.get(z, 0),
                            "holds_pct": pct(halten[z]) if halten.get(z) is not None else None, "fresh": bool(frisch.get(z))})
    sammler.sort(key=lambda s: (-s["other_tokens"], -s["from_wallets"]))
    # Was die Empfaenger heute noch halten (jeder nur einmal), ohne Wallets, die selbst fruehe Kaeufer sind
    gehalten = sum(halten.get(z) or 0 for z in ziele if z not in frueh and z != dev)
    paare.sort(key=lambda p: -(p["raw"]))
    out = {
        "checked": True, "count": sum(p["n"] for p in paare), "pairs": len(paare),
        "senders": len({p["from"] for p in paare}), "receivers": len(ziele),
        "from_early": len({p["from"] for p in paare if p["src"] == "early buyer"}),
        "from_dev": any(p["src"] == "dev" for p in paare),
        "dev_forwarded_pct": round(sum(p["pct"] or 0 for p in paare if p["src"] == "dev"), 3),
        "to_fresh": len({p["to"] for p in paare if p["fresh"]}),
        "forwarded_pct": round(sum(p["pct"] or 0 for p in paare), 3),
        "still_held_pct": pct(gehalten) if sup else None,
        "list": [{k: p[k] for k in ("from", "to", "pct", "fresh", "holds_pct", "src", "n")} for p in paare[:25]],
        "collectors": sammler[:10],
        "collectors_elsewhere": sum(1 for s in sammler if s["other_tokens"]),
        "collectors_elsewhere_max": max([s["other_tokens"] for s in sammler] or [0]),
    }
    _speichern(chain, token, paare)
    return out


# ── EVM ──────────────────────────────────────────────────────────────────

def evm(chain, token, logs, infra, sup, netto, frueh, dev, voll):
    """Alle Weiterleitungen aus den Transfer-Logs. netto: Bestaende aus den Logs (wenn voll)."""
    import intel
    if not logs or not sup:
        return {"checked": False}
    klein = sup * MIN_ANTEIL / 100
    je_tx, kand = {}, []
    for l in logs:
        try:
            von = "0x" + l["topics"][1][-40:]
            an = "0x" + l["topics"][2][-40:]
            menge = int(l.get("data") or "0x0", 16)
        except Exception:
            continue
        tx = l.get("transactionHash")
        je_tx.setdefault(tx, []).append((von, an))
        if von in infra or an in infra or von == NULL or an == NULL or von == an or menge < klein:
            continue
        kand.append({"from": von, "to": an, "raw": menge, "tx": tx})
    if not kand:
        return {"checked": True, "count": 0}
    # Wer ist ein Vertrag? Alle Adressen der Kandidaten und ihrer Transaktionen
    adressen = sorted({a for k in kand for a in (k["from"], k["to"])} |
                      {a for k in kand for p in je_tx.get(k["tx"], []) for a in p if a not in infra and a != NULL})[:900]
    codes = {}
    for i in range(0, len(adressen), 120):
        teil = adressen[i:i + 120]
        res = intel._evm_batch(chain, [("eth_getCode", [a, "latest"]) for a in teil])
        for a, c in zip(teil, res):
            codes[a] = c
    eoa = lambda a: codes.get(a) in ("0x", "0x0")
    liste = []
    for k in kand:
        if not (eoa(k["from"]) and eoa(k["to"])):
            continue
        # In derselben Transaktion bewegt ein Vertrag oder Pool diesen Coin: dann ist es Teil eines Swaps
        if any(a in infra or (a != NULL and not eoa(a)) for p in je_tx.get(k["tx"], []) for a in p):
            continue
        liste.append(k)
    if not liste:
        return {"checked": True, "count": 0}
    ziele = sorted({k["to"] for k in liste})[:200]
    noncen = intel._evm_batch(chain, [("eth_getTransactionCount", [z, "latest"]) for z in ziele])
    frisch = {z: (n is not None and int(n, 16) == 0) for z, n in zip(ziele, noncen)}
    if voll and netto is not None:
        halten = {z: max(0, netto.get(z, 0)) for z in ziele}
    else:
        best = intel._evm_batch(chain, [("eth_call", [{"to": token, "data": "0x70a08231" + "0" * 24 + z[2:]}, "latest"]) for z in ziele])
        halten = {z: (int(b, 16) if b else None) for z, b in zip(ziele, best)}
    return _zusammenfassen(chain, token, liste, halten, frisch, set(frueh), dev, sup)


# ── Solana ───────────────────────────────────────────────────────────────

def konto_aus_tx(tx, mint, owner):
    """Das Token-Konto (Adresse) eines Besitzers fuer diesen Mint aus einer Transaktion."""
    import intel
    keys = intel._schluessel(tx)
    meta = (tx or {}).get("meta") or {}
    for b in (meta.get("postTokenBalances") or []) + (meta.get("preTokenBalances") or []):
        if b.get("mint") == mint and b.get("owner") == owner:
            i = b.get("accountIndex")
            if isinstance(i, int) and i < len(keys):
                return keys[i]
    return None


# Eine reine Uebertragung ruft nur diese Programme auf. Ist ein DEX, eine Kurve oder ein Aggregator dabei,
# ist es ein Swap und keine Weiterleitung.
EINFACH = {SYSTEM, "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
           "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL", "ComputeBudget111111111111111111111111111111",
           "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr", "Memo1UhkJRfHyvLMcVucJwxXeuD728EqVDDwQDxFMNo"}


def _nur_uebertragung(tx):
    msg = ((tx or {}).get("transaction") or {}).get("message") or {}
    meta = (tx or {}).get("meta") or {}
    ixs = list(msg.get("instructions") or [])
    for g in meta.get("innerInstructions") or []:
        ixs += g.get("instructions") or []
    progs = {ix.get("programId") for ix in ixs if isinstance(ix, dict) and ix.get("programId")}
    return bool(progs) and progs <= EINFACH


def _sol_aenderung(tx, wallet):
    import intel
    meta = (tx or {}).get("meta") or {}
    keys = intel._schluessel(tx)
    if wallet not in keys:
        return 0
    i = keys.index(wallet)
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    if i >= len(pre) or i >= len(post):
        return 0
    return post[i] - pre[i]


def sol(mint, kaeufer, dev, dev_konto, sup, halter=None):
    """kaeufer: [{wallet, konto, ...}] (fruehe Kaeufer mit ihrem Token-Konto aus dem Kauf).
    halter: die groessten Halter mit Token-Konto. Ihre Konten zeigen, wie sie an die Tokens kamen: kam es per
    reiner Uebertragung, ist der Halter ein Empfaenger einer Weiterleitung (versteckter Halter)."""
    import intel
    if not sup:
        return {"checked": False}
    quellen = [(k["wallet"], k.get("konto")) for k in kaeufer if k.get("konto")]
    if dev and dev_konto:
        quellen.append((dev, dev_konto))
    schon = {kt for _, kt in quellen}
    for h in halter or []:
        if h.get("konto") and not h.get("pool") and not h.get("locked") and h["konto"] not in schon:
            quellen.append((h["address"], h["konto"]))
    if not quellen:
        return {"checked": False}
    sigs = intel._sol_batch([("getSignaturesForAddress", [kt, {"limit": 40}]) for _, kt in quellen], 20)
    alle = []
    for (w, kt), r in zip(quellen, sigs):
        for s in (r if isinstance(r, list) else []):
            if not s.get("err") and s.get("signature"):
                alle.append((w, s["signature"]))
    gesehen, eindeutig = set(), []
    for w, sg in alle:
        if sg not in gesehen:
            gesehen.add(sg)
            eindeutig.append((w, sg))
    eindeutig = eindeutig[:MAX_TX_SOL]
    txs = intel._sol_batch([intel._tx(sg) for _, sg in eindeutig], 20)
    klein = sup * MIN_ANTEIL / 100
    roh = []
    for (w, sg), tx in zip(eindeutig, txs):
        if not isinstance(tx, dict) or (tx.get("meta") or {}).get("err"):
            continue
        if not _nur_uebertragung(tx):
            continue
        d = intel._token_delta(tx, mint)
        for von, minus in d.items():
            if minus >= 0:
                continue
            empf = [(o, v) for o, v in d.items() if v > 0 and o != von]
            if not empf:
                continue
            # Verkauf: der Absender bekommt SOL (mehr als Gebuehren). Dann ist es keine Weiterleitung.
            if _sol_aenderung(tx, von) > 2_000_000:
                continue
            for o, v in empf:
                if v >= klein:
                    roh.append({"from": von, "to": o, "raw": v, "tx": sg})
    if not roh:
        return {"checked": True, "count": 0}
    # Nur echte Wallets als Empfaenger (System-Konten); Pools und Programme fallen raus
    ziele = sorted({f["to"] for f in roh} | {f["from"] for f in roh})
    besitzer = {}
    for i in range(0, len(ziele), 100):
        r = intel._sol("getMultipleAccounts", [ziele[i:i + 100], {"encoding": "base64"}]) or {}
        for a, v in zip(ziele[i:i + 100], r.get("value") or []):
            besitzer[a] = (v or {}).get("owner")
    liste = [f for f in roh if besitzer.get(f["to"]) in (SYSTEM, None) and besitzer.get(f["from"]) in (SYSTEM, None)]
    if not liste:
        return {"checked": True, "count": 0}
    empfaenger = sorted({f["to"] for f in liste})[:60]

    def frisch_von(a):
        r = intel._sol("getSignaturesForAddress", [a, {"limit": 25}])
        if not isinstance(r, list):
            return a, False
        zeiten = [x.get("blockTime") for x in r if x.get("blockTime")]
        return a, len(r) < 25 and bool(zeiten) and (time.time() - min(zeiten)) < 7 * 86400

    with ThreadPoolExecutor(8) as ex:
        frisch = dict(ex.map(frisch_von, empfaenger))
    best = intel._sol_batch([("getTokenAccountsByOwner", [z, {"mint": mint}, {"encoding": "jsonParsed"}]) for z in empfaenger], 20)
    halten = {}
    for z, r in zip(empfaenger, best):
        try:
            halten[z] = sum(int(x["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"]) for x in (r or {}).get("value") or [])
        except Exception:
            halten[z] = None
    return _zusammenfassen("solana", mint, liste, halten, frisch, {k["wallet"] for k in kaeufer}, dev, sup)
