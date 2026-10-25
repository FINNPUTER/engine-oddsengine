"""
smart.py: Smart-Money-Calls von Your Engine. Seit 2.40.

Frage: kaufen gerade mehrere Wallets, die unsere eigene Bewertung (iw_wallets) unabhaengig voneinander
hoch einstuft, denselben Coin? Wenn SMART_MIN_WALLETS davon innerhalb von SMART_WINDOW_SEC auf einem Coin
landen, ist das der Call. Die Wallets haben nichts miteinander zu tun, ausser dass unsere Bewertung sie
in dieselbe Liga gesetzt hat. Gleicher Geldgeber zaehlt nur einmal.

  tick()        aus ai.waechter: Runde ueber die Beobachtungsliste, Gruppen pruefen, Calls nachverfolgen
  stand()       Admin: Liste, Runde, Sichtungen, Calls, Abfragen heute
  bilanz()      jeder Call mit Multiple seit dem Call (Trefferquote)
  setzen(adr)   Nutzer schaltet Calls fuer sich an oder aus (motor)

Schalter: 'smart' liest im Hintergrund mit und schreibt jeden Call in die Bilanz (auch bevor Your Engine
live ist, nur der Admin sieht es). 'smart_calls' schickt die Calls an Nutzer, die sie angeschaltet haben,
und an SMART_CHAT_ID (optional, ein Telegram-Kanal).

Kosten: Solana ueber HELIUS_API_KEY (eine Signaturabfrage je Wallet und Runde, eine Transaktion je neuem
Swap), Robinhood ueber BLOCKSCOUT_API_KEY. Taegliche Obergrenze SMART_DAILY_RPC.
"""
import calendar
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import storage
from logger import log

MIN_SCORE = int(os.getenv("SMART_MIN_SCORE", "36"))
LISTE_N = int(os.getenv("SMART_WATCHLIST", "150"))
FENSTER = int(os.getenv("SMART_WINDOW_SEC", "900"))
# Seit 2.45: wie oft jedes Wallet geprueft wird, unabhaengig vom Fenster. Mit Helius-Webhook kommen Solana-Kaeufe
# sofort, dann reicht fuer Solana eine langsame Sicherheitsrunde.
ABFRAGE = int(os.getenv("SMART_POLL_SEC", "120"))
ABFRAGE_MIT_HOOK = int(os.getenv("SMART_POLL_WITH_HOOK_SEC", "900"))
MIN_W = int(os.getenv("SMART_MIN_WALLETS", "3"))
MIN_SOL = float(os.getenv("SMART_MIN_SOL", "0.05"))
MIN_MCAP = float(os.getenv("SMART_MIN_MCAP", "0"))
MAX_MCAP = float(os.getenv("SMART_MAX_MCAP", "0"))
TAKT = int(os.getenv("SMART_INTERVAL_SEC", "60"))
TAGES_RPC = int(os.getenv("SMART_DAILY_RPC", "150000"))
MARGE = 0.85
KETTEN = ("solana", "robinhood")
STUFEN = (2, 3, 5, 10, 25, 50, 100)

WSOL = "So11111111111111111111111111111111111111112"
STABIL_SOL = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
              "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB", "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo", WSOL}
STABIL_EVM = {"0x5fc5360d0400a0fd4f2af552add042d716f1d168", "0x0bd7d308f8e1639fab988df18a8011f41eacad73"}

_T = {}
_ZEIGER = [0]
_LETZTE = {}
_GEPRUEFT = {}
_HOOK = {"ok_bis": 0, "events": 0, "letztes": 0, "fehler": None, "adressen": 0}
_LISTE = {"ts": 0, "w": []}
_SCHEMA = [False]
_LAEUFT = threading.Lock()


# ── Datenbank ────────────────────────────────────────────────────────────

def _q(sql, args=(), eins=False):
    import intel
    if not _SCHEMA[0] and intel._pg():
        intel._q("""CREATE TABLE IF NOT EXISTS iw_sight (
            chain TEXT NOT NULL, token TEXT NOT NULL, wallet TEXT NOT NULL, score INTEGER, native DOUBLE PRECISION,
            sig TEXT NOT NULL, ts BIGINT NOT NULL, funder TEXT, PRIMARY KEY (chain, wallet, sig, token))""")
        intel._q("CREATE INDEX IF NOT EXISTS ix_iw_sight_ts ON iw_sight(ts)")
        intel._q("""CREATE TABLE IF NOT EXISTS iw_calls (
            chain TEXT NOT NULL, token TEXT NOT NULL, ts BIGINT NOT NULL, symbol TEXT, wallets TEXT, n INTEGER,
            score_sum INTEGER, mcap0 DOUBLE PRECISION, peak DOUBLE PRECISION, last DOUBLE PRECISION, checked BIGINT,
            stufe INTEGER DEFAULT 0, level TEXT, sent BOOLEAN DEFAULT FALSE, recipients TEXT, PRIMARY KEY (chain, token, ts))""")
        intel._q("CREATE INDEX IF NOT EXISTS ix_iw_calls_ts ON iw_calls(ts)")
        _SCHEMA[0] = True
    return intel._q(sql, args, eins)


def _rpc_zaehlen(n=1):
    tag = time.strftime("%Y%m%d", time.gmtime())
    if _T.get("rpc_tag") != tag:
        _T["rpc_tag"], _T["rpc"] = tag, 0
    _T["rpc"] = _T.get("rpc", 0) + n


def _rpc_frei(n):
    tag = time.strftime("%Y%m%d", time.gmtime())
    if _T.get("rpc_tag") != tag:
        return True
    return _T.get("rpc", 0) + n <= TAGES_RPC


# ── Beobachtungsliste ────────────────────────────────────────────────────

def liste():
    if time.time() - _LISTE["ts"] < 600 and _LISTE["w"]:
        return _LISTE["w"]
    rows = _q("SELECT wallet, chain, score FROM iw_wallets WHERE score >= %s AND chain IN ('solana','robinhood') "
              "ORDER BY score DESC, best_x DESC LIMIT %s", (MIN_SCORE, LISTE_N)) or []
    _LISTE.update(ts=time.time(), w=[{"wallet": r[0], "chain": r[1], "score": int(r[2] or 0)} for r in rows])
    return _LISTE["w"]


def _runde_groesse(n):
    """So viele je Runde, dass die ganze Liste innerhalb des Fensters einmal drankommt (mit Reserve)."""
    if n <= 0:
        return 0
    runden = max(1, int(FENSTER * MARGE // max(1, TAKT)))
    return max(1, -(-n // runden))


# ── Kaeufe lesen ─────────────────────────────────────────────────────────

def _swaps_sol(w):
    """Neue Kaeufe eines Solana-Wallets seit der letzten Runde: [(token, sol, sig, ts)]."""
    import intel
    seit = int(time.time()) - FENSTER
    opt = {"limit": 15}
    if _LETZTE.get(w):
        opt["until"] = _LETZTE[w]
    _rpc_zaehlen()
    sigs = intel._sol("getSignaturesForAddress", [w, opt]) or []
    if not isinstance(sigs, list):
        return []
    if sigs:
        _LETZTE[w] = sigs[0].get("signature")
    neu = [s for s in sigs if not s.get("err") and (s.get("blockTime") or 0) >= seit]
    if not neu:
        return []
    _rpc_zaehlen(len(neu))
    txs = intel._sol_batch([intel._tx(s["signature"]) for s in neu], 10)
    out = []
    for s, tx in zip(neu, txs):
        out += _kauf_aus_tx(tx, w, s["signature"], s.get("blockTime") or 0)
    return out


def _kauf_aus_tx(tx, w, sig, ts):
    if not isinstance(tx, dict):
        return []
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return []
    vor, nach = {}, {}
    for liste_, ziel in ((meta.get("preTokenBalances") or [], vor), (meta.get("postTokenBalances") or [], nach)):
        for b in liste_:
            if b.get("owner") != w or not b.get("mint"):
                continue
            ziel[b["mint"]] = ziel.get(b["mint"], 0) + float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
    delta = {m: nach.get(m, 0) - vor.get(m, 0) for m in set(vor) | set(nach)}
    import intel
    keys = intel._schluessel(tx)
    sol = 0.0
    if w in keys:
        i = keys.index(w)
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if i < len(pre) and i < len(post):
            sol = (pre[i] - post[i]) / 1e9
    sol += max(0.0, -delta.get(WSOL, 0))
    usd = sum(max(0.0, -delta.get(m, 0)) for m in STABIL_SOL if m != WSOL)
    gekauft = [m for m, d in delta.items() if d > 0 and m not in STABIL_SOL]
    if len(gekauft) != 1:
        return []
    einsatz = sol if sol > 0 else (usd / 150.0 if usd else 0)
    return [(gekauft[0], round(einsatz, 4), sig, ts)]


def _swaps_evm(chain, w):
    import intel
    seit = int(time.time()) - FENSTER
    _rpc_zaehlen()
    d = intel._bs(chain, f"/api/v2/addresses/{w}/token-transfers", {"type": "ERC-20", "filter": "to"}) or {}
    out = []
    for it in d.get("items") or []:
        to = ((it.get("to") or {}).get("hash") or "").lower()
        tok = (it.get("token") or {})
        adr = str(tok.get("address_hash") or tok.get("address") or "").lower()
        if to != w.lower() or not adr or adr in STABIL_EVM:
            continue
        try:
            ts = calendar.timegm(time.strptime(str(it.get("timestamp"))[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            continue
        if ts < seit:
            continue
        if not (it.get("from") or {}).get("is_contract"):
            continue
        sig = it.get("transaction_hash") or it.get("tx_hash") or ""
        out.append((adr, 0.0, sig, ts))
    return out


def _runde():
    w = liste()
    if not w:
        _T["leer"] = time.time()
        return 0
    jetzt = time.time()
    hook = hook_aktiv()

    def intervall(x):
        return ABFRAGE_MIT_HOOK if (hook and x["chain"] == "solana") else ABFRAGE
    faellig = sorted([x for x in w if jetzt - _GEPRUEFT.get(x["wallet"], 0) >= intervall(x)],
                     key=lambda x: _GEPRUEFT.get(x["wallet"], 0))
    # Hoechstens so viele, wie das Tagesbudget im Schnitt hergibt (2 Abfragen je Wallet), mindestens 5
    je_runde = max(5, int(TAGES_RPC / 2 / max(1, 86400 / TAKT)))
    teil = faellig[:je_runde]
    if not teil:
        return 0
    if not _rpc_frei(len(teil) * 2):
        _T["budget_stop"] = time.time()
        return 0
    for x in teil:
        _GEPRUEFT[x["wallet"]] = jetzt

    def eins(x):
        try:
            swaps = _swaps_sol(x["wallet"]) if x["chain"] == "solana" else _swaps_evm(x["chain"], x["wallet"])
        except Exception as e:
            log.debug(f"smart {x['wallet'][:8]}: {e}")
            return 0
        neu = 0
        for tok, einsatz, sig, ts in swaps:
            if x["chain"] == "solana" and einsatz < MIN_SOL:
                continue
            _q("INSERT INTO iw_sight (chain, token, wallet, score, native, sig, ts) VALUES (%s,%s,%s,%s,%s,%s,%s) "
               "ON CONFLICT DO NOTHING", (x["chain"], tok, x["wallet"], x["score"], einsatz, sig, ts))
            neu += 1
        return neu
    with ThreadPoolExecutor(5) as ex:
        gesehen = sum(ex.map(eins, teil))
    sol_n = sum(1 for x in w if x["chain"] == "solana")
    _T["runde"] = {"ts": int(time.time()), "wallets": len(teil), "of": len(w), "sightings": gesehen, "due": len(faellig),
                   "check_every_min": round(ABFRAGE / 60, 1),
                   "solana_every_min": round((ABFRAGE_MIT_HOOK if hook else ABFRAGE) / 60, 1), "solana_wallets": sol_n}
    return gesehen


# ── Gruppen und Calls ────────────────────────────────────────────────────

def _geldgeber(chain, wallets):
    """Wer hat jedes Wallet zuerst finanziert? Gleicher Geldgeber zaehlt als ein Wallet."""
    if chain != "solana":
        return {w: w for w in wallets}
    import intel
    ks = [{"wallet": w, "ts": int(time.time())} for w in wallets]
    try:
        intel._alter_sol(ks, juenger_als=0)
    except Exception:
        pass
    return {k["wallet"]: k.get("funder") or k["wallet"] for k in ks}


def gruppen():
    seit = int(time.time()) - FENSTER
    return _q("SELECT chain, token, COUNT(DISTINCT wallet), SUM(score), MIN(ts), MAX(ts) FROM iw_sight WHERE ts >= %s "
              "GROUP BY chain, token HAVING COUNT(DISTINCT wallet) >= %s", (seit, MIN_W)) or []


def _pruefen():
    import scan
    for chain, token, n, ssum, t0, t1 in gruppen():
        if _q("SELECT 1 FROM iw_calls WHERE chain=%s AND token=%s AND ts >= %s", (chain, token, int(time.time()) - 86400), eins=True):
            continue
        seit = int(time.time()) - FENSTER
        rows = _q("SELECT wallet, MAX(score), SUM(native), MIN(ts) FROM iw_sight WHERE chain=%s AND token=%s AND ts >= %s "
                  "GROUP BY wallet ORDER BY MAX(score) DESC", (chain, token, seit)) or []
        geld = _geldgeber(chain, [r[0] for r in rows])
        unabh = {}
        for r in rows:
            unabh.setdefault(geld.get(r[0], r[0]), r)
        if len(unabh) < MIN_W:
            continue
        try:
            r = scan.analyse(f"{chain}:{token}", deep=False)
        except Exception as e:
            log.warning(f"smart analyse {token[:10]}: {e}")
            continue
        if not r.get("ok", True) or r.get("error"):
            continue
        m = r.get("market") or {}
        mc = float(m.get("mcap") or 0)
        lvl = (r.get("verdict") or {}).get("level")
        jetzt = int(time.time())
        wl = [{"wallet": x[0], "score": int(x[1] or 0), "sol": round(float(x[2] or 0), 3), "ts": int(x[3] or 0)} for x in unabh.values()]
        notiz = None
        if lvl == "red":
            notiz = "red"
        elif MIN_MCAP and mc and mc < MIN_MCAP:
            notiz = "small"
        elif MAX_MCAP and mc > MAX_MCAP:
            notiz = "big"
        _q("INSERT INTO iw_calls (chain, token, ts, symbol, wallets, n, score_sum, mcap0, peak, last, checked, level) "
           "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
           (chain, token, jetzt, r.get("symbol"), json.dumps(wl), len(wl), sum(x["score"] for x in wl), mc, mc, mc, jetzt,
            notiz or lvl))
        if notiz:
            log.info(f"smart: {r.get('symbol')} {len(wl)} wallets, not sent ({notiz})")
            continue
        _senden_call(chain, token, jetzt, r, wl, t0)


def _text(r, wl, t0):
    import scan
    mins = max(1, round((max(x["ts"] for x in wl) - min(x["ts"] for x in wl)) / 60))
    import html as H
    z = [f"🟢 <b>Smart money:</b> {len(wl)} rated wallets bought ${H.escape(str(r.get('symbol') or scan.kurz(r['address'])))} within {mins} min", ""]
    for x in sorted(wl, key=lambda x: -x["score"])[:6]:
        z.append(f"{scan.kurz(x['wallet'])}  score {x['score']}" + (f" · {x['sol']} SOL" if x.get("sol") else ""))
    z += ["", "Different funders, no shared history we could find. Our own scoring rated each of them for being early in coins that ran.", ""]
    return "\n".join(z) + scan.karte(r, html=True)


def _senden_call(chain, token, ts, r, wl, t0):
    import features
    if not features.an("smart_calls"):
        return
    import motor
    import scan
    text, knoepfe = _text(r, wl, t0), scan.knoepfe(r)
    an = []
    for adr in motor._nutzer():
        try:
            if not motor.offen(adr) or (motor.zustand(adr).get("prefs") or {}).get("smart") is not True:
                continue
            if motor.senden(adr, text, knoepfe, html=True):
                an.append(adr)
        except Exception as e:
            log.debug(f"smart senden {adr[:8]}: {e}")
    kanal = os.getenv("SMART_CHAT_ID", "").strip()
    if kanal:
        try:
            import agent
            agent._send(kanal, text, knoepfe, html=True)
            an.append("chat:" + kanal)
        except Exception as e:
            log.warning(f"smart kanal: {e}")
    _q("UPDATE iw_calls SET sent=TRUE, recipients=%s WHERE chain=%s AND token=%s AND ts=%s", (json.dumps(an), chain, token, ts))


# ── Nachverfolgen und Bilanz ─────────────────────────────────────────────

def _nachverfolgen():
    import scan
    rows = _q("SELECT chain, token, ts, symbol, mcap0, peak, stufe, sent, recipients FROM iw_calls WHERE ts >= %s AND mcap0 > 0",
              (int(time.time()) - 7 * 86400,)) or []
    je = {}
    for r in rows:
        je.setdefault(r[0], []).append(r)
    for chain, liste_ in je.items():
        dex = scan.KETTEN.get(chain, (None, None))[1]
        for i in range(0, len(liste_), 30):
            teil = liste_[i:i + 30]
            try:
                d = requests.get(f"https://api.dexscreener.com/tokens/v1/{dex}/" + ",".join(x[1] for x in teil), timeout=15).json()
            except Exception:
                continue
            mc = {}
            for p in d if isinstance(d, list) else []:
                a = (p.get("baseToken") or {}).get("address") or ""
                a = a if chain == "solana" else a.lower()
                mc[a] = max(mc.get(a, 0), float(p.get("marketCap") or p.get("fdv") or 0))
            for (c, tok, ts, sym, m0, peak, stufe, sent, emp) in teil:
                v = mc.get(tok)
                if not v:
                    continue
                peak = max(peak or 0, v)
                x = peak / m0
                neu = max([s for s in STUFEN if x >= s] or [0])
                _q("UPDATE iw_calls SET peak=%s, last=%s, checked=%s, stufe=%s WHERE chain=%s AND token=%s AND ts=%s",
                   (peak, v, int(time.time()), max(stufe or 0, neu), c, tok, ts))
                if sent and neu > (stufe or 0):
                    _nachricht(c, tok, sym, m0, peak, neu, emp)


def _nachricht(chain, token, sym, m0, peak, stufe, emp):
    import scan
    text = f"🟢 ${sym or scan.kurz(token)} is {stufe}x since the smart money call (MC {scan.usd(m0)} to {scan.usd(peak)})."
    try:
        empf = json.loads(emp or "[]")
    except Exception:
        empf = []
    import motor
    for a in empf:
        try:
            if a.startswith("chat:"):
                import agent
                agent._send(a[5:], text)
            else:
                motor.senden(a, text)
        except Exception:
            pass


def bilanz(tage=30, n=50):
    seit = int(time.time()) - int(tage) * 86400
    rows = _q("SELECT chain, token, ts, symbol, n, mcap0, peak, last, level, sent FROM iw_calls WHERE ts >= %s ORDER BY ts DESC LIMIT %s",
              (seit, int(n))) or []
    calls = [{"chain": r[0], "token": r[1], "ts": r[2], "symbol": r[3], "wallets": r[4], "mcap_at_call": r[5],
              "peak_mcap": r[6], "mcap_now": r[7], "peak_x": round(r[6] / r[5], 2) if r[5] else None,
              "now_x": round(r[7] / r[5], 2) if r[5] else None, "verdict": r[8], "sent": bool(r[9])} for r in rows]
    gezaehlt = [c for c in calls if c["peak_x"] is not None and c["verdict"] not in ("red", "small", "big")]
    xs = sorted(c["peak_x"] for c in gezaehlt)
    return {"calls": calls, "counted": len(gezaehlt),
            "hit_2x": round(sum(1 for x in xs if x >= 2) / len(xs) * 100) if xs else None,
            "hit_5x": round(sum(1 for x in xs if x >= 5) / len(xs) * 100) if xs else None,
            "median_peak_x": xs[len(xs) // 2] if xs else None,
            "meaning": "Every smart money call with the market cap at the call and the highest since. Nothing is removed."}


# ── Helius-Webhook: Solana-Kaeufe in Sekunden statt per Abfrage (seit 2.45) ──────────

def _hook_url():
    u = os.getenv("SMART_HOOK_URL", "").strip()
    if u:
        return u
    dom = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip()
    return f"https://{dom}/v1/hooks/helius" if dom else None


def hook_geheimnis():
    import hashlib
    import hmac
    k = (os.getenv("AUTH_SECRET", "") or os.getenv("HELIUS_API_KEY", "")).encode()
    return hmac.new(k, b"oddsengine-helius-hook", hashlib.sha256).hexdigest()[:40]


def hook_aktiv():
    """Webhook steht und ist frisch abgeglichen. Dann wird Solana nur noch langsam nachgeprueft."""
    return time.time() < _HOOK["ok_bis"]


def hook_sync():
    """Webhook bei Helius anlegen oder auf die aktuelle Beobachtungsliste bringen (alle 10 Minuten)."""
    key = os.getenv("HELIUS_API_KEY", "").strip()
    url = _hook_url()
    if not key or not url or os.getenv("SMART_HOOK", "1") == "0":
        _HOOK["ok_bis"] = 0
        return
    adressen = set()
    try:
        import features
        if features.an("smart"):
            adressen |= {x["wallet"] for x in liste() if x["chain"] == "solana"}
    except Exception:
        pass
    try:
        import wache
        if wache._an():
            adressen |= set(wache.sol_wallets())  # Wallet Guard haengt am selben Webhook
    except Exception:
        pass
    adressen = sorted(adressen)
    if not adressen:
        return
    basis = os.getenv("HELIUS_API_BASE", "https://api.helius.xyz").rstrip("/")
    body = {"webhookURL": url, "transactionTypes": ["ANY"], "accountAddresses": adressen,
            "webhookType": "raw", "txnStatus": "success", "authHeader": hook_geheimnis()}
    alle = requests.get(f"{basis}/v0/webhooks", params={"api-key": key}, timeout=15).json()
    vorhanden = [h for h in (alle if isinstance(alle, list) else []) if h.get("webhookURL") == url]
    if vorhanden:
        h = vorhanden[0]
        if sorted(h.get("accountAddresses") or []) != adressen or h.get("webhookType") != "raw":
            r = requests.put(f"{basis}/v0/webhooks/{h['webhookID']}", params={"api-key": key}, json=body, timeout=20)
            r.raise_for_status()
    else:
        r = requests.post(f"{basis}/v0/webhooks", params={"api-key": key}, json=body, timeout=20)
        r.raise_for_status()
    _HOOK.update(ok_bis=time.time() + 1500, adressen=len(adressen), fehler=None)


def hook_empfangen(daten):
    """Rohe Transaktionen von Helius: Kaeufe der beobachteten Wallets sofort als Sichtung speichern und pruefen."""
    txs = daten if isinstance(daten, list) else [daten]
    try:
        import wache
        if wache._an():
            for tx in txs:
                if isinstance(tx, dict):
                    wache.sol_tx(tx)
    except Exception as e:
        log.warning(f"smart hook wache: {e}")
    beob = {x["wallet"]: x for x in liste() if x["chain"] == "solana"}
    neu = 0
    for tx in txs:
        if not isinstance(tx, dict):
            continue
        import intel
        keys = intel._schluessel(tx)
        sig = ((tx.get("transaction") or {}).get("signatures") or [""])[0]
        ts = int(tx.get("blockTime") or time.time())
        for w in [k for k in keys if k in beob]:
            for tok, einsatz, s_, t_ in _kauf_aus_tx(tx, w, sig, ts):
                if einsatz < MIN_SOL:
                    continue
                _q("INSERT INTO iw_sight (chain, token, wallet, score, native, sig, ts) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                   "ON CONFLICT DO NOTHING", ("solana", tok, w, beob[w]["score"], einsatz, s_, t_))
                neu += 1
    _HOOK["events"] += len(txs)
    _HOOK["letztes"] = int(time.time())
    if neu and _LAEUFT.acquire(blocking=False):
        try:
            _pruefen()
        except Exception as e:
            log.warning(f"smart hook pruefen: {e}")
        finally:
            _LAEUFT.release()
    return neu


def stand():
    w = liste()
    s = _q("SELECT COUNT(*), COUNT(DISTINCT token) FROM iw_sight WHERE ts >= %s", (int(time.time()) - 86400,), eins=True)
    c = _q("SELECT COUNT(*), COUNT(*) FILTER (WHERE sent) FROM iw_calls", (), eins=True)
    try:
        import features
        an, senden_ = features.an("smart"), features.an("smart_calls")
    except Exception:
        an, senden_ = False, False
    return {"on": an, "sending": senden_, "watchlist": len(w), "min_score": MIN_SCORE, "window_min": FENSTER // 60,
            "min_wallets": MIN_W, "last_round": _T.get("runde"), "rpc_today": _T.get("rpc", 0), "rpc_cap": TAGES_RPC,
            "budget_stopped": bool(_T.get("budget_stop") and time.time() - _T["budget_stop"] < 300),
            "sightings_24h": s[0] if s else 0, "tokens_24h": s[1] if s else 0,
            "calls": c[0] if c else 0, "calls_sent": c[1] if c else 0,
            "helius": bool(os.getenv("HELIUS_API_KEY", "").strip()),
            "webhook": {"active": hook_aktiv(), "events": _HOOK["events"], "last_event": _HOOK["letztes"] or None,
                        "addresses": _HOOK["adressen"], "error": _HOOK["fehler"]},
            "blockscout": bool(os.getenv("BLOCKSCOUT_API_KEY", "").strip()),
            "record": {k: v for k, v in bilanz().items() if k != "calls"},
            "hint": None if w else f"No wallet scores {MIN_SCORE} or more yet. Run the wallet import in the admin, or lower SMART_MIN_SCORE."}


def tick():
    try:
        import features
        if not features.an("smart"):
            return
    except Exception:
        return
    if not _LAEUFT.acquire(blocking=False):
        return
    try:
        jetzt = time.time()
        if jetzt - _T.get("r", 0) >= TAKT:
            _T["r"] = jetzt
            _runde()
            _pruefen()
        if jetzt - _T.get("h", 0) >= 600:
            _T["h"] = jetzt
            try:
                hook_sync()
            except Exception as e:
                _HOOK["fehler"] = str(e)[:160]
                log.warning(f"smart hook sync: {e}")
        if jetzt - _T.get("f", 0) >= 600:
            _T["f"] = jetzt
            _nachverfolgen()
            _q("DELETE FROM iw_sight WHERE ts < %s", (int(jetzt) - 3 * 86400,))
    except Exception as e:
        log.warning(f"smart tick: {e}")
    finally:
        _LAEUFT.release()


def schleife():
    """Eigener Takt (aus ai_api beim Start), damit eine Runde wirklich alle SMART_INTERVAL_SEC laeuft."""
    time.sleep(30)
    while True:
        tick()
        time.sleep(max(15, TAKT // 4))
