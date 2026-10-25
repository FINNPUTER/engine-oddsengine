"""
$ODDS Holder-Pruefung.

Seit 2.19 laeuft $ODDS auf der Robinhood Chain (EVM, Chain ID 4663, Launch
ueber Torch). Der Kunde beweist mit einer Wallet-Unterschrift (MetaMask, Rabby,
Phantom EVM: personal_sign), dass ihm eine Adresse gehoert. ODDS_CHAIN=solana
schaltet auf den alten Solana-Weg zurueck (Phantom signMessage, Helius). Wir lesen das $ODDS-Guthaben dieser Adresse von der
Kette, rechnen es mit dem aktuellen Kurs in Dollar um und leiten daraus die
Stufe ab. Kein Konto, kein Passwort, keine Datenbank.

Stufen (Dollarwert der gehaltenen $ODDS, per Variable einstellbar), seit 2.15:

    public   0                         nur die Gratis-Signale (FREE_PER_DAY, Standard 1)
    holder   TIER_HOLDER_USD (100)     alle Signale sofort
    pro      TIER_PRO_USD    (1000)    dazu das Engine-Board (spaeter auch per Pass)
    whale    TIER_WHALE_USD  (5000)    wie pro; fuer HOLDER_ALLOW und API

Kulanz (TIER_GRACE, 0.70): einmal erreichte Stufe bleibt, solange mindestens
70 % der Grenze gehalten werden.

Der Schluessel, den die App bekommt, ist aus Adresse und Ablauf per HMAC
abgeleitet. Die Stufe steckt NICHT im Schluessel: sie wird bei jeder Anfrage
aus dem Guthaben bestimmt (zehn Minuten zwischengespeichert). Wer verkauft,
faellt damit von selbst zurueck, ohne dass irgendwo etwas geloescht wird.

Noetige Variablen auf Railway (Dienst web):
    ODDS_MINT         Vertragsadresse von $ODDS (0x..., nach dem Launch)
    ODDS_CHAIN        robinhood (Standard) oder solana
    ODDS_RPC          eigener RPC-Knoten, optional. Standard ist der offizielle
                      https://rpc.mainnet.chain.robinhood.com
    AUTH_SECRET       gibt es schon, signiert auch diese Schluessel
Optional:
    HOLDER_ALLOW      "adresse:whale,adresse:pro" fuer Team und Tests
    ODDS_PRICE_USD    fester Kurs, falls DexScreener und Jupiter ausfallen
"""
import hashlib
import hmac
import json
import os
import time
import urllib.request

STUFEN = ("public", "holder", "pro", "whale")
RANG = {s: i for i, s in enumerate(STUFEN)}

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


# ── Einstellungen ────────────────────────────────────────────────────────

def mint():
    m = os.getenv("ODDS_MINT", "").strip()
    return m.lower() if evm() else m


def kette():
    return (os.getenv("ODDS_CHAIN", "robinhood").strip().lower() or "robinhood")


def evm():
    return kette() != "solana"


KETTEN = {
    # name: (chain_id, offizieller RPC, Explorer, DexScreener-Kennung)
    "robinhood": (4663, "https://rpc.mainnet.chain.robinhood.com",
                  "https://robinhoodchain.blockscout.com", "robinhood"),
}


def ketten_info():
    if not evm():
        return {"chain": "solana", "type": "solana"}
    cid, rpc, exp, _ = KETTEN.get(kette(), KETTEN["robinhood"])
    cid = int(_f("ODDS_CHAIN_ID", cid))
    return {"chain": kette(), "type": "evm", "chain_id": cid,
            "chain_id_hex": hex(cid), "rpc": os.getenv("ODDS_RPC", "").strip() or rpc,
            "explorer": os.getenv("ODDS_EXPLORER", "").strip() or exp,
            "buy_url": os.getenv("ODDS_BUY_URL", "").strip() or
            ("https://app.torchpad.fun/trade/{}/{}".format(kette(), mint()) if mint() else None),
            "ref_url": os.getenv("ODDS_REF_URL", "").strip() or "https://app.torchpad.fun/r/REF026455"}


def _f(k, d):
    try:
        return float(os.getenv(k, "") or d)
    except ValueError:
        return float(d)


def schwellen():
    return {"holder": _f("TIER_HOLDER_USD", 100),
            "pro": _f("TIER_PRO_USD", 1000),
            "whale": _f("TIER_WHALE_USD", 5000)}


# Kulanz: wer eine Stufe einmal erreicht hat, behaelt sie, solange er
# mindestens diesen Anteil der Grenze haelt. Sonst fliegt ein Holder bei jedem
# Kursrutsch raus und wieder rein.
KULANZ = _f("TIER_GRACE", 0.70)


def public_delay_min():
    """Seit 2.15 gibt es keine Zeitstufen mehr. Holder sehen alles sofort,
    die Oeffentlichkeit nur die Gratis-Signale. Bleibt fuer alte Aufrufer."""
    return 0


def verzoegerung(stufe):
    return 0


def sieht_alles(stufe):
    return RANG.get(stufe, 0) >= RANG["holder"]


def stufen_info():
    s = schwellen()
    out = [
        {"tier": "public", "min_usd": 0, "signals": "free",
         "free_per_day": int(_f("FREE_PER_DAY", 1)), "board": False},
        {"tier": "holder", "min_usd": s["holder"], "signals": "all", "board": False},
        {"tier": "pro", "min_usd": s["pro"], "signals": "all", "board": True,
         "pass": "soon"},
    ]
    ts = token_schwellen()
    if ts and os.getenv("TIER_MODE", "").strip().lower() == "tokens":
        for z in out:
            if z["tier"] in ts:
                z["min_tokens"] = ts[z["tier"]]
    return out


# ── Base58 ───────────────────────────────────────────────────────────────

def b58decode(s):
    n = 0
    for ch in s:
        i = _B58.find(ch)
        if i < 0:
            raise ValueError("not base58")
        n = n * 58 + i
    roh = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    null = len(s) - len(s.lstrip("1"))
    return b"\x00" * null + roh


def adresse_ok(a):
    if evm():
        import re
        return bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", str(a or "").strip()))
    try:
        return len(b58decode(a)) == 32
    except Exception:
        return False


def norm(a):
    """EVM-Adressen immer klein, sonst zaehlt dieselbe Wallet doppelt."""
    a = str(a or "").strip()
    return a.lower() if evm() else a


def _sig_bytes(sig):
    """Phantom liefert Uint8Array. Die App schickt base58; hex, base64 und
    eine Zahlenliste werden aber auch angenommen, damit es nicht an der
    Schreibweise scheitert."""
    import base64
    if isinstance(sig, list):
        return bytes(int(x) & 0xFF for x in sig)
    s = str(sig).strip()
    if len(s) == 128:
        try:
            return bytes.fromhex(s)
        except ValueError:
            pass
    try:
        b = b58decode(s)
        if len(b) == 64:
            return b
    except Exception:
        pass
    try:
        b = base64.b64decode(s + "=" * (-len(s) % 4))
        if len(b) == 64:
            return b
    except Exception:
        pass
    raise ValueError("signature format not recognised")


# ── Ed25519 ──────────────────────────────────────────────────────────────

def _ed25519_rein(pk, msg, sig):
    """Pruefung nach RFC 8032 in reinem Python. Nur Rueckfall, falls das
    Paket cryptography fehlt. Langsam (Millisekunden), aber korrekt."""
    p = 2 ** 255 - 19
    L = 2 ** 252 + 27742317777372353535851937790883648493
    d = -121665 * pow(121666, p - 2, p) % p
    I = pow(2, (p - 1) // 4, p)

    def inv(x):
        return pow(x, p - 2, p)

    def xrecover(y):
        xx = (y * y - 1) * inv(d * y * y + 1)
        x = pow(xx, (p + 3) // 8, p)
        if (x * x - xx) % p != 0:
            x = (x * I) % p
        if x % 2 != 0:
            x = p - x
        return x

    def add(P, Q):
        x1, y1, z1, t1 = P
        x2, y2, z2, t2 = Q
        A = (y1 - x1) * (y2 - x2) % p
        B = (y1 + x1) * (y2 + x2) % p
        C = t1 * 2 * d * t2 % p
        D = z1 * 2 * z2 % p
        E, F, G, H = B - A, D - C, D + C, B + A
        return (E * F % p, G * H % p, F * G % p, E * H % p)

    def mul(s, P):
        Q = (0, 1, 1, 0)
        while s > 0:
            if s & 1:
                Q = add(Q, P)
            P = add(P, P)
            s >>= 1
        return Q

    def decode(b):
        y = int.from_bytes(b, "little")
        sign = y >> 255
        y &= (1 << 255) - 1
        if y >= p:
            raise ValueError("bad point")
        x = xrecover(y)
        if (x * x * -1 + y * y - 1 - d * x * x * y * y) % p != 0:
            raise ValueError("not on curve")
        if (x & 1) != sign:
            x = p - x
        return (x, y, 1, x * y % p)

    def gleich(P, Q):
        x1, y1, z1, _ = P
        x2, y2, z2, _ = Q
        return (x1 * z2 - x2 * z1) % p == 0 and (y1 * z2 - y2 * z1) % p == 0

    By = 4 * inv(5) % p
    Bx = xrecover(By)
    B = (Bx, By, 1, Bx * By % p)
    if len(sig) != 64 or len(pk) != 32:
        return False
    try:
        A = decode(pk)
        R = decode(sig[:32])
    except ValueError:
        return False
    S = int.from_bytes(sig[32:], "little")
    if S >= L:
        return False
    h = int.from_bytes(hashlib.sha512(sig[:32] + pk + msg).digest(), "little") % L
    return gleich(mul(S, B), add(R, mul(h, A)))


def _evm_gueltig(adresse, text, sig):
    from eth_account import Account
    from eth_account.messages import encode_defunct
    s = str(sig or "").strip()
    if not s.startswith("0x"):
        s = "0x" + s
    if len(s) != 132:
        raise ValueError("signature format not recognised")
    wer = Account.recover_message(encode_defunct(text=text), signature=s)
    return wer.lower() == norm(adresse)


def signatur_gueltig(adresse, text, sig):
    if evm():
        return _evm_gueltig(adresse, text, sig)
    pk = b58decode(adresse)
    sb = _sig_bytes(sig)
    msg = text.encode("utf-8")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        try:
            Ed25519PublicKey.from_public_bytes(pk).verify(sb, msg)
            return True
        except Exception:
            return False
    except ImportError:
        return _ed25519_rein(pk, msg, sb)


# ── Einmaltext und Schluessel ────────────────────────────────────────────

def _geheim():
    return os.getenv("AUTH_SECRET", "").strip()


def _hmac(text):
    return hmac.new(_geheim().encode(), text.encode(), hashlib.sha256).hexdigest()[:24]


def nonce(adresse, fenster=0):
    return _hmac("{}nonce:{}:{}".format(_praefix(), norm(adresse), int(time.time() // 600) - fenster))


def nachricht(adresse, n):
    return ("OddsEngine holder check\n"
            "Address: {}\n"
            "Nonce: {}\n\n"
            "Signing is free. It sends no transaction and moves no tokens."
            ).format(norm(adresse), n)


def pruefe_unterschrift(adresse, sig):
    """Zwei Zeitfenster, damit niemand an der Zehn-Minuten-Grenze scheitert."""
    for fenster in (0, 1):
        if signatur_gueltig(adresse, nachricht(adresse, nonce(adresse, fenster)), sig):
            return True
    return False


SITZUNG_TAGE = 7


def _praefix():
    return "evm" if evm() else "sol"


def schluessel(adresse):
    adresse = norm(adresse)
    bis = int(time.time()) + SITZUNG_TAGE * 86400
    p = _praefix()
    return "{}.{}.{}.{}".format(p, adresse, bis, _hmac("{}sess:{}:{}".format(p, adresse, bis)))


def schluessel_adresse(key):
    """Adresse aus einem gueltigen Schluessel, sonst None."""
    p = _praefix()
    if not key or not key.startswith(p + ".") or not _geheim():
        return None
    try:
        _, adr, bis, sig = key.split(".")
        if int(bis) < time.time():
            return None
        if not hmac.compare_digest(sig, _hmac("{}sess:{}:{}".format(p, adr, bis))):
            return None
        return adr
    except Exception:
        return None


# ── Kette und Kurs ───────────────────────────────────────────────────────

def _post(url, daten, timeout=12):
    r = urllib.request.Request(url, data=json.dumps(daten).encode(),
                               headers={"Content-Type": "application/json",
                                        "User-Agent": "oddsengine/2.14"})
    with urllib.request.urlopen(r, timeout=timeout) as f:
        return json.loads(f.read().decode())


def _get(url, timeout=10):
    r = urllib.request.Request(url, headers={"User-Agent": "oddsengine/2.14",
                                             "Accept": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as f:
        return json.loads(f.read().decode())


def _rpcs():
    out = []
    k = os.getenv("HELIUS_API_KEY", "").strip()
    if k:
        out.append("https://mainnet.helius-rpc.com/?api-key=" + k)
    eigen = os.getenv("SOLANA_RPC", "").strip()
    if eigen:
        out.append(eigen)
    out.append("https://api.mainnet-beta.solana.com")
    return out


def _sol_rpc(methode, params):
    letzter = None
    for u in _rpcs():
        try:
            d = _post(u, {"jsonrpc": "2.0", "id": 1, "method": methode, "params": params})
            if "result" in d:
                return d["result"]
            letzter = d.get("error")
        except Exception as e:
            letzter = str(e)
    raise RuntimeError("solana rpc failed: {}".format(str(letzter)[:120]))


_GUTHABEN = {}


def _evm_rpcs():
    out = []
    eigen = os.getenv("ODDS_RPC", "").strip()
    if eigen:
        out += [u.strip() for u in eigen.split(",") if u.strip()]
    out.append(KETTEN.get(kette(), KETTEN["robinhood"])[1])
    return out


def _evm_call(daten):
    letzter = None
    for u in _evm_rpcs():
        try:
            d = _post(u, {"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                          "params": [{"to": mint(), "data": daten}, "latest"]})
            if "result" in d:
                return d["result"]
            letzter = d.get("error")
        except Exception as e:
            letzter = str(e)
    raise RuntimeError("chain rpc failed: {}".format(str(letzter)[:120]))


_DEZIMAL = {}


def _dezimalen():
    m = mint()
    if m in _DEZIMAL:
        return _DEZIMAL[m]
    try:
        d = int(_evm_call("0x313ce567"), 16)   # decimals()
    except Exception:
        d = int(_f("ODDS_DECIMALS", 18))
    _DEZIMAL[m] = d
    return d


def _evm_guthaben(adresse):
    roh = _evm_call("0x70a08231" + norm(adresse)[2:].rjust(64, "0"))   # balanceOf
    return int(roh or "0x0", 16) / (10 ** _dezimalen())


def guthaben(adresse):
    """$ODDS dieser Adresse, in ganzen Token. Zwei Minuten zwischengespeichert."""
    m = mint()
    if not m:
        return 0.0
    adresse = norm(adresse)
    t = _GUTHABEN.get(adresse)
    if t and time.time() - t[0] < 120:
        return t[1]
    if evm():
        summe = _evm_guthaben(adresse)
        _GUTHABEN[adresse] = (time.time(), summe)
        return summe
    res = _sol_rpc("getTokenAccountsByOwner",
                   [adresse, {"mint": m}, {"encoding": "jsonParsed"}])
    summe = 0.0
    for acc in (res or {}).get("value", []):
        try:
            ta = acc["account"]["data"]["parsed"]["info"]["tokenAmount"]
            summe += float(ta.get("uiAmountString") or ta.get("uiAmount") or 0)
        except Exception:
            continue
    _GUTHABEN[adresse] = (time.time(), summe)
    return summe


_KURS = {"ts": 0.0, "usd": None, "quelle": None}


def kurs():
    """Dollarkurs von $ODDS. DexScreener, dann Jupiter, dann ODDS_PRICE_USD.
    Fuenf Minuten zwischengespeichert; der letzte gute Wert bleibt stehen,
    falls beide Quellen gleichzeitig ausfallen."""
    m = mint()
    if not m:
        return None, None
    if _KURS["usd"] and time.time() - _KURS["ts"] < 300:
        return _KURS["usd"], _KURS["quelle"]
    usd, quelle = None, None
    try:
        d = _get("https://api.dexscreener.com/latest/dex/tokens/" + m)
        paare = [p for p in (d.get("pairs") or [])
                 if str((p.get("baseToken") or {}).get("address") or "").lower() == m.lower()
                 and p.get("priceUsd")]
        paare.sort(key=lambda p: -float((p.get("liquidity") or {}).get("usd") or 0))
        if paare:
            usd, quelle = float(paare[0]["priceUsd"]), "dexscreener"
    except Exception:
        pass
    if not usd and evm():
        # GeckoTerminal kennt viele EVM-Ketten frueher als DexScreener.
        for netz in [x.strip() for x in os.getenv(
                "GECKO_NETWORK", "robinhood,robinhood-chain").split(",") if x.strip()]:
            try:
                d = _get("https://api.geckoterminal.com/api/v2/simple/networks/{}/token_price/{}"
                         .format(netz, m))
                preise = ((d.get("data") or {}).get("attributes") or {}).get("token_prices") or {}
                p = preise.get(m) or preise.get(m.lower())
                if p:
                    usd, quelle = float(p), "geckoterminal"
                    break
            except Exception:
                continue
    if not usd and not evm():
        try:
            d = _get("https://lite-api.jup.ag/price/v3?ids=" + m)
            x = d.get(m) or (d.get("data") or {}).get(m) or {}
            p = x.get("usdPrice") or x.get("price")
            if p:
                usd, quelle = float(p), "jupiter"
        except Exception:
            pass
    if not usd:
        fest = _f("ODDS_PRICE_USD", 0)
        if fest > 0:
            usd, quelle = fest, "fixed"
    if usd:
        _KURS.update(ts=time.time(), usd=usd, quelle=quelle)
        return usd, quelle
    return _KURS["usd"], _KURS["quelle"]


# ── Weitere Wallets verknuepfen (seit 2.23) ─────────────────────────────
#
# Wer sich bei Torch mit MetaMask anmeldet, bekommt dort eine EIGENE, in Torch
# eingebettete Wallet. Die kann auf unserer Seite nicht unterschreiben. Darum
# der Beweis ueber die Kette: aus der Torch-Wallet (B) irgendeine Menge $ODDS an
# die bestaetigte Wallet (A) schicken. Senden kann nur, wer B kontrolliert. Die
# Engine sieht den Transfer im Log des Tokens und verknuepft B mit A. Die Stufe
# zaehlt danach das Guthaben von A und allen verknuepften Wallets zusammen.

_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
LINK_MIN = int(_f("LINK_WINDOW_MIN", 60))
LINK_MAX = int(_f("LINK_MAX_WALLETS", 5))


def _evm_rpc(methode, params):
    letzter = None
    for u in _evm_rpcs():
        try:
            d = _post(u, {"jsonrpc": "2.0", "id": 1, "method": methode, "params": params})
            if "result" in d:
                return d["result"]
            letzter = d.get("error")
        except Exception as e:
            letzter = str(e)
    raise RuntimeError("chain rpc failed: {}".format(str(letzter)[:120]))


def _kv(k):
    try:
        import storage
        roh = storage.kv_get(k)
        return json.loads(roh) if roh else None
    except Exception:
        return None


def _kv_setzen(k, v):
    import storage
    return storage.kv_set(k, json.dumps(v))


def verknuepft(adresse):
    """Weitere Wallets, die zu dieser Adresse gehoeren."""
    if not evm():
        return []
    return [norm(x) for x in (_kv("links_" + norm(adresse)) or []) if adresse_ok(x)]


def link_start(adresse, andere):
    adresse, andere = norm(adresse), norm(andere)
    if not evm():
        raise ValueError("linking works on the EVM chain only")
    if not adresse_ok(andere):
        raise ValueError("that is not a valid wallet address")
    if andere == adresse:
        raise ValueError("that is the wallet you already verified")
    if not mint():
        raise ValueError("token not live yet")
    if andere in verknuepft(adresse):
        raise ValueError("already linked")
    if len(verknuepft(adresse)) >= LINK_MAX:
        raise ValueError("you can link up to {} wallets".format(LINK_MAX))
    block = int(_evm_rpc("eth_blockNumber", []), 16)
    anfrage = {"b": andere, "block": block, "bis": time.time() + LINK_MIN * 60}
    if not _kv_setzen("linkreq_" + adresse, anfrage):
        raise RuntimeError("linking needs the database")
    return {"from": andere, "to": adresse, "token": mint(), "expires_min": LINK_MIN,
            "how": "Send any amount of $ODDS from {} to {}. One token is enough.".format(andere, adresse)}


def _log_gefunden(von, an, ab_block):
    jetzt = int(_evm_rpc("eth_blockNumber", []), 16)
    schritt = 50000
    start = ab_block
    t1 = "0x" + von[2:].rjust(64, "0")
    t2 = "0x" + an[2:].rjust(64, "0")
    while start <= jetzt:
        ende = min(jetzt, start + schritt)
        try:
            logs = _evm_rpc("eth_getLogs", [{"address": mint(), "fromBlock": hex(start),
                                              "toBlock": hex(ende),
                                              "topics": [_TRANSFER, t1, t2]}])
        except RuntimeError:
            if schritt > 2000:
                schritt //= 5
                continue
            raise
        if logs:
            return True
        start = ende + 1
    return False


def link_pruefen(adresse):
    adresse = norm(adresse)
    a = _kv("linkreq_" + adresse)
    if not a:
        return {"status": "none"}
    if a.get("bis", 0) < time.time():
        return {"status": "expired", "from": a.get("b")}
    if not _log_gefunden(a["b"], adresse, int(a["block"])):
        return {"status": "waiting", "from": a["b"], "to": adresse,
                "expires_in_min": max(0, int((a["bis"] - time.time()) / 60))}
    andere = a["b"]
    # Eine Wallet gehoert genau einer bestaetigten Adresse.
    alt = _kv("linkedto_" + andere)
    if alt and alt != adresse:
        _kv_setzen("links_" + alt, [x for x in (_kv("links_" + alt) or []) if norm(x) != andere])
        _STUFE.pop(alt, None)
    liste = verknuepft(adresse)
    if andere not in liste:
        liste.append(andere)
    _kv_setzen("links_" + adresse, liste)
    _kv_setzen("linkedto_" + andere, adresse)
    _kv_setzen("linkreq_" + adresse, None)
    _STUFE.pop(adresse, None)
    return {"status": "linked", "linked": liste}


def link_loesen(adresse, andere):
    adresse, andere = norm(adresse), norm(andere)
    liste = [x for x in verknuepft(adresse) if x != andere]
    _kv_setzen("links_" + adresse, liste)
    if _kv("linkedto_" + andere) == adresse:
        _kv_setzen("linkedto_" + andere, None)
    _STUFE.pop(adresse, None)
    return {"linked": liste}


# ── Stufe ────────────────────────────────────────────────────────────────

def _erlaubt():
    out = {}
    for teil in os.getenv("HOLDER_ALLOW", "").split(","):
        teil = teil.strip()
        if not teil:
            continue
        adr, _, st = teil.rpartition(":") if teil.count(":") else (teil, "", "whale")
        st = (st or "whale").strip().lower()
        if st in RANG:
            out[norm(adr)] = st
    return out


_STUFE = {}
_GEMERKT = {}


def _gemerkt(adresse):
    if adresse in _GEMERKT:
        return _GEMERKT[adresse]
    try:
        import storage
        v = storage.kv_get("tier_" + adresse)
        _GEMERKT[adresse] = v
        return v
    except Exception:
        return None


def _merken(adresse, stufe):
    if _GEMERKT.get(adresse) == stufe:
        return
    _GEMERKT[adresse] = stufe
    try:
        import storage
        storage.kv_set("tier_" + adresse, stufe)
    except Exception:
        pass


def token_schwellen():
    """Seit 2.25.1: Stufen nach Anzahl Token statt Dollarwert. Greift, wenn
    TIER_MODE=tokens gesetzt ist, oder als Rueckfall, wenn kein Kurs da ist
    und TIER_HOLDER_TOKENS gesetzt ist. Gut fuer den Launch, solange $ODDS
    noch auf keiner Kursseite steht."""
    try:
        h = float(os.getenv("TIER_HOLDER_TOKENS", "") or 0)
    except ValueError:
        h = 0
    if h <= 0:
        return None
    return {"holder": h, "pro": _f("TIER_PRO_TOKENS", h * 10),
            "whale": _f("TIER_WHALE_TOKENS", h * 50)}


def stufe_nach_token(menge):
    s = token_schwellen() or {}
    for k in ("whale", "pro", "holder"):
        if k in s and menge >= s[k]:
            return k
    return "public"


def stufe_von(usd):
    s = schwellen()
    if usd >= s["whale"]:
        return "whale"
    if usd >= s["pro"]:
        return "pro"
    if usd >= s["holder"]:
        return "holder"
    return "public"


def status(adresse, frisch=False):
    """Alles, was die App ueber diese Adresse wissen muss."""
    adresse = norm(adresse)
    frei = _erlaubt().get(adresse)
    t = _STUFE.get(adresse)
    if t and not frisch and time.time() - t[0] < 120:
        return t[1]
    out = {"address": adresse, "mint": mint() or None, "chain": kette(),
           "balance": None, "price_usd": None, "value_usd": None,
           "tier": "public", "error": None}
    if frei:
        out["tier"] = frei
        out["note"] = "allow list"
    if not mint():
        out["error"] = "token not live yet"
    else:
        try:
            out["balance"] = guthaben(adresse)
            # Seit 2.23: verknuepfte Wallets zaehlen mit.
            weitere = []
            for b in verknuepft(adresse):
                try:
                    g = guthaben(b)
                except Exception:
                    g = 0.0
                weitere.append({"address": b, "balance": g})
            if weitere:
                out["own_balance"] = out["balance"]
                out["linked"] = weitere
                out["balance"] = out["balance"] + sum(w["balance"] for w in weitere)
            usd, quelle = kurs()
            out["price_usd"], out["price_source"] = usd, quelle
            nach_token = token_schwellen() and (
                os.getenv("TIER_MODE", "").strip().lower() == "tokens" or usd is None)
            if nach_token:
                auto = stufe_nach_token(out["balance"])
                out["tier_by"] = "tokens"
                if usd is not None:
                    out["value_usd"] = round(out["balance"] * usd, 2)
                _merken(adresse, auto)
                if RANG[auto] > RANG[out["tier"]]:
                    out["tier"] = auto
            elif usd is not None:
                out["value_usd"] = round(out["balance"] * usd, 2)
                auto = stufe_von(out["value_usd"])
                # Kulanz: frueher erreichte Stufe halten, solange noch
                # KULANZ x Grenze da ist.
                vorher = _gemerkt(adresse)
                if vorher and RANG.get(vorher, 0) > RANG[auto] and vorher in schwellen() \
                        and out["value_usd"] >= KULANZ * schwellen()[vorher]:
                    auto = vorher
                    out["grace"] = True
                _merken(adresse, auto)
                if RANG[auto] > RANG[out["tier"]]:
                    out["tier"] = auto
            else:
                out["error"] = "price unavailable"
        except Exception as e:
            out["error"] = str(e)[:140]
    s = schwellen()
    naechste = [k for k in ("holder", "pro", "whale") if RANG[k] > RANG[out["tier"]]]
    if naechste:
        k = naechste[0]
        out["next_tier"] = k
        out["next_tier_usd"] = s[k]
        ts = token_schwellen()
        if out.get("tier_by") == "tokens" and ts:
            out["next_tier_tokens"] = ts[k]
            if out["balance"] is not None:
                out["missing_tokens"] = round(max(0.0, ts[k] - out["balance"]), 2)
        if out["value_usd"] is not None:
            out["missing_usd"] = round(max(0.0, s[k] - out["value_usd"]), 2)
    out["delay_min"] = 0
    out["all_signals"] = sieht_alles(out["tier"])
    out["board"] = RANG[out["tier"]] >= RANG["pro"]
    # Fehler nicht zwischenspeichern, sonst haengt ein Aussetzer zehn Minuten.
    if not out["error"] or frei:
        _STUFE[adresse] = (time.time(), out)
    return out
