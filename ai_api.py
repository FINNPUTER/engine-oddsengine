"""
ai_api.py: Endpunkte fuer engine.oddsengine.live und OddsEngine AI. Seit 2.29.

Anmeldung wie beim Holder-Check: Wallet unterschreibt einen Text, dafuer gibt es
einen Sitzungsschluessel (X-OddsEngine-Key). API-Kunden nutzen eigene
Schluessel (Authorization: Bearer oe_live_...).

    GET  /v1/engine/nonce        Text zum Unterschreiben
    POST /v1/engine/login        Unterschrift -> Sitzungsschluessel
    GET  /v1/engine/me           Konto: Guthaben, Einzahlungen, Schluessel, Wallets
    POST /v1/engine/settings     Briefing und Gedaechtnis-Schalter (fuer Your Engine)
    POST /v1/engine/memory/delete
    POST /v1/engine/wallets      weitere oeffentliche Adresse merken
    POST /v1/engine/wallets/delete
    POST /v1/engine/telegram     Einmal-Link zum Bot
    GET  /v1/ai/models           Modelle mit unseren Preisen (oeffentlich)
    POST /v1/ai/keys             neuer API-Schluessel (einmal sichtbar)
    POST /v1/ai/keys/delete
    POST /v1/ai/chat             Playground (Sitzung), auf Wunsch als Stream
    POST /v1/ai/deposits/check   sofort nach Einzahlungen schauen
    POST /v1/ai/quote            Aufladen mit festem Betrag, Kurs 10 Minuten fest
    GET  /v1/admin/ai            Zahlen fuer den Betreiber
    GET  /v1/models              OpenAI-kompatibel
    POST /v1/chat/completions    OpenAI-kompatibel, mit oe_live_-Schluessel
"""
import json
import os
import re
import threading
import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

import asyncio

import agents
import ai
import holder
import storage

router = APIRouter()


def _admin_ok(request: Request):
    erwartet = os.getenv("ADMIN_KEY", "").strip()
    if not erwartet:
        return False
    key = (request.headers.get("X-OddsEngine-Admin")
           or request.query_params.get("admin") or "").strip()
    return bool(key) and key == erwartet


def _an(name):
    try:
        import features
        return features.an(name)
    except Exception:
        return False


def _offen(request, *namen):
    return _admin_ok(request) or any(_an(n) for n in namen)


def _sitzung(request: Request):
    key = (request.headers.get("X-OddsEngine-Key")
           or request.query_params.get("key") or "").strip()
    adr = holder.schluessel_adresse(key)
    if not adr:
        raise HTTPException(status_code=401, detail="session expired, connect your wallet again")
    return adr


def _sitzung_oder_none(request: Request):
    try:
        return _sitzung(request)
    except HTTPException:
        return None


async def _json(request):
    try:
        b = await request.json()
        return b if isinstance(b, dict) else {}
    except Exception:
        return {}


# ── einfache Bremse je Schluessel / Adresse ─────────────────────────────

_FENSTER = defaultdict(deque)
_BREMSE_LOCK = threading.Lock()


def _bremse(wer, pro_minute=None):
    pro_minute = pro_minute or int(os.getenv("AI_RATE_PER_MIN", "60") or 60)
    jetzt = time.time()
    with _BREMSE_LOCK:
        q = _FENSTER[wer]
        while q and q[0] < jetzt - 60:
            q.popleft()
        if len(q) >= pro_minute:
            return False
        q.append(jetzt)
        return True


# ── Anmeldung ────────────────────────────────────────────────────────────

@router.get("/v1/engine/nonce")
def engine_nonce(address: str = Query(..., min_length=32, max_length=44)):
    if not holder._geheim():
        raise HTTPException(status_code=503, detail="auth not configured (AUTH_SECRET)")
    if not holder.adresse_ok(address):
        raise HTTPException(status_code=400, detail="not a valid wallet address")
    address = holder.norm(address)
    n = holder.nonce(address)
    return {"address": address, "nonce": n, "message": holder.nachricht(address, n)}


@router.post("/v1/engine/login")
async def engine_login(request: Request):
    b = await _json(request)
    adr = str(b.get("address") or "").strip()
    sig = b.get("signature")
    if not adr or sig is None or not holder.adresse_ok(adr):
        raise HTTPException(status_code=400, detail="address and signature required")
    adr = holder.norm(adr)
    try:
        ok = holder.pruefe_unterschrift(adr, sig)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=401, detail="signature does not match this address")
    try:
        ai.konto(adr)
        ai.offene_zuordnen(adr)
    except Exception:
        pass
    return {"ok": True, "token": holder.schluessel(adr), "address": adr}


def _einstellungen(adr):
    try:
        return json.loads(storage.kv_get("engine_set_" + adr) or "{}")
    except Exception:
        return {}


def _telegram_verknuepft(adr):
    try:
        import agent
        for u in agent.alle():
            if u.get("wallet") == adr:
                return True
    except Exception:
        pass
    return False


@router.get("/v1/engine/me")
def engine_me(request: Request):
    adr = _sitzung(request)
    out = {"address": adr, "short": adr[:6] + "..." + adr[-4:]}
    try:
        ai.offene_zuordnen(adr)
        kt = ai.konto(adr) or {}
    except Exception:
        kt = {}
    kurs, _ = holder.kurs()
    credit = kt.get("credit", 0.0)
    out["balance"] = {"usd": round(credit, 4), "deposited": round(kt.get("deposited", 0.0), 4),
                      "spent": round(kt.get("spent", 0.0), 4),
                      "odds_equiv": round(credit / kurs, 0) if kurs else None,
                      "price": kurs}
    out["deposit"] = ai.eingang()
    out["token"] = holder.mint() or None
    out["chain"] = holder.ketten_info()
    try:
        out["deposits"] = ai.einzahlungen(adr)
        out["keys"] = ai.keys_liste(adr)
        out["usage"] = ai.verlauf(adr, 12)
    except Exception:
        out["deposits"], out["keys"], out["usage"] = [], [], []
    linked = holder.verknuepft(adr)
    s = _einstellungen(adr)
    extra = s.get("wallets") or []
    out["wallets"] = ([{"chain": "EVM", "address": adr, "main": True}]
                      + [{"chain": "EVM", "address": w, "linked": True} for w in linked]
                      + [w for w in extra if isinstance(w, dict)])
    out["linked"] = {"wallet": True, "telegram": _telegram_verknuepft(adr)}
    br = {"on": True, "parts": {"weather": True, "calendar": True, "mail": True, "commute": True, "news": True, "rewards": True, "calls": True, "crypto": True, "coins": True, "bags": True, "moving": False, "stocks": True}}
    gb = s.get("briefing") or {}
    br["on"] = gb.get("on", True)
    br["parts"].update(gb.get("parts") or {})
    mem = {"paused": False, "cats": {"wallets": True, "markets": True, "notes": True}, "items": []}
    gm = s.get("memory") or {}
    mem["paused"] = gm.get("paused", False)
    mem["cats"].update(gm.get("cats") or {})
    mem["items"] = gm.get("items") or []
    out["briefing"], out["memory"] = br, mem
    try:
        st = ai.holder_stufe(adr)
    except Exception:
        st = {}
    out["ai"] = {"live": _an("ai"), "markup": st.get("markup", ai.markup()), "base_markup": ai.markup(),
                 "holder": st, "configured": ai.bereit()}
    return out


@router.post("/v1/engine/settings")
async def engine_settings(request: Request):
    adr = _sitzung(request)
    b = await _json(request)
    s = _einstellungen(adr)
    br = s.setdefault("briefing", {"on": True, "parts": {}})
    mem = s.setdefault("memory", {"paused": False, "cats": {}, "items": []})
    for k, v in (b.get("briefing") or {}).items():
        if k == "on":
            br["on"] = bool(v)
        elif k in ("calls", "crypto", "bags", "moving", "weather", "commute", "news", "rewards", "calendar", "mail", "stocks", "coins"):
            br.setdefault("parts", {})[k] = bool(v)
    for k, v in (b.get("memory_cat") or {}).items():
        if k in ("wallets", "markets", "notes"):
            mem.setdefault("cats", {})[k] = bool(v)
    if "memory_paused" in b:
        mem["paused"] = bool(b["memory_paused"])
    storage.kv_set("engine_set_" + adr, json.dumps(s))
    import motor
    if "official_news" in b:
        motor.set_official_news(adr, bool(b["official_news"]))
    if "check_ins" in b:
        motor.set_check_ins(adr, bool(b["check_ins"]))
    if "voice_replies" in b:
        motor.set_voice_replies(adr, bool(b["voice_replies"]))
    if "smart_calls" in b:
        motor.set_smart_calls(adr, bool(b["smart_calls"]))
    if "guard" in b:
        import wache
        wache.bewachen(adr, bool(b["guard"]))
    if "language" in b:
        motor.set_language(adr, str(b.get("language") or "auto"))
    return {"ok": True}


@router.get("/v1/admin/announce")
def admin_announce_list(request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=401, detail="admin only")
    import motor
    return {"announcements": motor._ank_liste()[::-1], "engines": len(motor._nutzer())}


@router.post("/v1/admin/announce")
async def admin_announce(request: Request):
    """News an alle Your Engines (Telegram), ohne KI, kostenlos fuer die Nutzer."""
    if not _admin_ok(request):
        raise HTTPException(status_code=401, detail="admin only")
    b = await _json(request)
    import motor
    r = motor.ankuendigen(b.get("text"), "admin")
    if not r.get("ok"):
        return JSONResponse(status_code=400, content={"error": {"message": "Write something first."}})
    return r


@router.post("/v1/engine/memory/delete")
async def engine_memory_delete(request: Request):
    adr = _sitzung(request)
    b = await _json(request)
    s = _einstellungen(adr)
    mem = s.setdefault("memory", {"paused": False, "cats": {}, "items": []})
    if b.get("all"):
        mem["items"] = []
    else:
        mem["items"] = [i for i in mem.get("items", []) if str(i.get("id")) != str(b.get("id"))]
    storage.kv_set("engine_set_" + adr, json.dumps(s))
    return {"ok": True}


@router.post("/v1/engine/wallets")
async def engine_wallets(request: Request):
    adr = _sitzung(request)
    b = await _json(request)
    a = str(b.get("address") or "").strip()
    import re
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", a):
        kette, a = "EVM", a.lower()
    elif re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", a):
        kette = "SOL"
    else:
        raise HTTPException(status_code=400, detail="not a wallet address")
    s = _einstellungen(adr)
    ws = [w for w in s.get("wallets", []) if w.get("address") != a]
    if len(ws) >= 10:
        raise HTTPException(status_code=400, detail="up to 10 extra wallets")
    ws.append({"chain": kette, "address": a})
    s["wallets"] = ws
    storage.kv_set("engine_set_" + adr, json.dumps(s))
    return {"ok": True}


@router.post("/v1/engine/wallets/delete")
async def engine_wallets_delete(request: Request):
    adr = _sitzung(request)
    b = await _json(request)
    a = str(b.get("address") or "").strip()
    s = _einstellungen(adr)
    s["wallets"] = [w for w in s.get("wallets", []) if w.get("address") not in (a, a.lower())]
    storage.kv_set("engine_set_" + adr, json.dumps(s))
    return {"ok": True}


@router.post("/v1/engine/telegram")
def engine_telegram(request: Request):
    adr = _sitzung(request)
    import agent
    if not agent.AN:
        raise HTTPException(status_code=503, detail="Telegram linking is switched off")
    r = agent.link_code(adr)
    if not r:
        raise HTTPException(status_code=503, detail="telegram linking needs the database")
    return {"ok": True, "url": r[1]}


# ── Kalshi Edition (seit 2.34) ───────────────────────────────────────────

_KST = {"ts": 0.0, "d": None}


@router.get("/v1/kalshi/engine")
def kalshi_engine_api(request: Request):
    import kalshi_engine
    if not (kalshi_engine.oeffentlich() or _admin_ok(request)):
        raise HTTPException(status_code=403, detail="Kalshi Edition is not live yet")
    if _KST["d"] is None or time.time() - _KST["ts"] > 120:
        _KST.update(ts=time.time(), d=kalshi_engine.stand())
    return _KST["d"]


# ── Burns ueber Torch (seit 2.33) ───────────────────────────────────────
# Torch kauft mit seinen Gebuehren $ODDS und $TORCH zurueck und verbrennt sie. Die Zahlen
# kommen von Torch selbst (app.torchpad.fun/api/token/burned), 5 Minuten zwischengespeichert,
# damit die Burn-Seite sie ohne CORS-Probleme lesen kann.

_TORCHBURN = {"ts": 0.0, "d": None}


@router.get("/v1/burns/torch")
def burns_torch():
    if _TORCHBURN["d"] and time.time() - _TORCHBURN["ts"] < 300:
        return _TORCHBURN["d"]
    import requests as _rq
    out = {}
    for name, adr in (("odds", "0x9c87B4cfa21266d1638f437c96dBe1012CB81111"),
                      ("torch", "0x000050e18ffa3f8f7ad170eb9cef160eb3ed1111")):
        try:
            r = _rq.get("https://app.torchpad.fun/api/token/burned",
                        params={"chain": "robinhood", "address": adr}, timeout=10)
            d = r.json() if r.ok else {}
            b, oc = d.get("burned") or {}, d.get("onchain") or {}
            out[name] = {"buybacks": b.get("burns"), "usd_spent": b.get("usdSpent"),
                         "burned_by_buybacks": b.get("tokensBurned"), "last_burn": b.get("lastBurnAt"),
                         "burned_onchain": oc.get("tokensBurned"), "pct_of_supply": oc.get("pctOfSupply")}
        except Exception as e:
            out[name] = {"error": str(e)[:80]}
    out["source"] = "app.torchpad.fun"
    _TORCHBURN.update(ts=time.time(), d=out)
    return out


# ── Your Engine: der Agent (seit 2.33) ──────────────────────────────────

def _motor_offen(request, adr=None):
    import motor
    return _an("engine") or _admin_ok(request) or bool(adr and motor.beta(adr))


@router.post("/v1/engine/ask")
async def engine_ask(request: Request):
    adr = _sitzung(request)
    if not _motor_offen(request, adr):
        raise HTTPException(status_code=403, detail="Your Engine is not live yet")
    if not _bremse("motor:" + adr, 12):
        return JSONResponse(status_code=429, content={"error": {"message": "slow down a little", "type": "rate_limit"}})
    b = await _json(request)
    import motor
    try:
        bilder = [x for x in (b.get("images") or []) if isinstance(x, str) and x.startswith("data:image/") and len(x) < 7_000_000][:4]
        r = await asyncio.to_thread(motor.frage, adr, b.get("text") or "", "web", bilder)
    except motor.KeinGuthaben:
        return JSONResponse(status_code=402, content={"error": {"message": motor.LEER, "type": "insufficient_quota"}})
    except motor.NichtBereit as e:
        return JSONResponse(status_code=403, content={"error": {"message": str(e), "type": "setup_required"}})
    except ai.Fehler as e:
        if e.status == 402:
            return JSONResponse(status_code=402, content={"error": {"message": motor.LEER, "type": "insufficient_quota"}})
        return _fehler(e)
    kt = ai.konto(adr) or {}
    r["balance_usd"] = round(kt.get("credit", 0.0), 4)
    return r


@router.get("/v1/engine/agent")
def engine_agent(request: Request):
    adr = _sitzung(request)
    import motor
    z = motor.zustand(adr)
    inbox = z.get("inbox") or []
    if inbox:
        z["inbox"] = []
        motor._zustand_speichern(adr, z)
    out = motor.list_items(adr)
    out.update(history=motor.verlauf(adr), inbox=inbox, live=_motor_offen(request, adr),
               telegram=_telegram_verknuepft(adr), models={"fast": motor.FAST, "deep": motor.DEEP},
               defaults=motor.standard_modelle(adr),
               brain=motor.zustand(adr)["prefs"].get("brain") or "auto", brains=motor.koepfe(adr),
               name=motor._name_aus_gedaechtnis(adr),
               check_ins=motor.zustand(adr)["prefs"].get("checkins") is not False,
               language=motor.zustand(adr)["prefs"].get("lang"), languages=motor.SPRACHEN,
               voice_replies=motor.zustand(adr)["prefs"].get("voice") is True,
               smart_calls=motor.zustand(adr)["prefs"].get("smart") is True,
               guard=motor.zustand(adr)["prefs"].get("guard") is True,
               news_catalog=motor.news_katalog(adr), stocks=motor.zustand(adr)["prefs"].get("stocks") or [], coins=(motor.STANDARD_COINS if motor.zustand(adr)["prefs"].get("coins") is None else motor.zustand(adr)["prefs"]["coins"]),
               switches={"scan": motor._scan_an(adr), "voice": motor._schalter("voice", adr), "lang": motor._schalter("lang", adr),
                         "smart": motor._schalter("smart_calls", adr),
                         "guard": motor._schalter("guard", adr),
                         "trade": motor._handel_an(adr)},
               beta_label=os.getenv("MOTOR_BETA_LABEL", "on").strip().lower() != "off",
               access=motor.bereit(adr),
               feedback_url=os.getenv("MOTOR_FEEDBACK_URL", "").strip() or None)
    try:
        import postfach
        import kalender
        out.update(mail=postfach.status(adr), calendar=kalender.status(adr))
    except Exception:
        pass
    return out


@router.post("/v1/engine/agent/models")
async def engine_agent_models(request: Request):
    adr = _sitzung(request)
    b = await _json(request)
    import motor
    out = {}
    if "brain" in b:
        r = motor.kopf_setzen(adr, b.get("brain"))
        if not r.get("ok"):
            return JSONResponse(status_code=400, content={"error": {"message": r.get("error")}})
        out["brain"] = r["brain"]
    for art in ("text", "image"):
        if art + "_model" in b:
            r = motor.set_default_model(adr, art, b.get(art + "_model") or "reset")
            if not r.get("ok"):
                return JSONResponse(status_code=400, content={"error": {"message": r.get("error")}})
            out[art] = r.get("model")
    return {"ok": True, "defaults": motor.standard_modelle(adr), "brain": motor.zustand(adr)["prefs"].get("brain") or "auto"}


@router.post("/v1/engine/agent/remove")
async def engine_agent_remove(request: Request):
    adr = _sitzung(request)
    b = await _json(request)
    import motor
    return motor.remove(adr, str(b.get("kind") or ""), str(b.get("id") or ""))


@router.post("/v1/engine/agent/briefing")
async def engine_agent_briefing(request: Request):
    adr = _sitzung(request)
    b = await _json(request)
    import motor
    try:
        return motor.set_briefing(adr, b.get("hour"), b.get("utc_offset_hours"), b.get("on"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="hour 0-23, utc_offset_hours -12..14")


@router.post("/v1/engine/agent/briefing/now")
def engine_agent_briefing_now(request: Request):
    """Briefing sofort schicken: zum Ausprobieren. Kostet nichts, keine KI."""
    adr = _sitzung(request)
    if not _bremse("brief:" + adr, 3):
        raise HTTPException(status_code=429, detail="slow down a little")
    import motor
    t = motor.briefing_text(adr)
    ok = motor.senden(adr, t)
    return {"ok": True, "sent_to_telegram": ok, "text": t}


@router.post("/v1/engine/agent/mail")
async def engine_agent_mail(request: Request):
    """Postfach verbinden (IMAP mit App-Passwort), trennen oder die Mail-Wache schalten.
    Das Passwort kommt nur hier herein, nie ueber den Chat, und wird verschluesselt gespeichert."""
    adr = _sitzung(request)
    if not _bremse("mail:" + adr, 4):
        raise HTTPException(status_code=429, detail="slow down a little")
    b = await _json(request)
    import postfach
    if b.get("disconnect"):
        return postfach.trennen(adr)
    if "watch" in b and not b.get("password"):
        return postfach.wache(adr, b.get("watch"))
    if "headers_only" in b and not b.get("password"):
        return postfach.nur_kopf(adr, b.get("headers_only"))
    r = postfach.verbinden(adr, b.get("provider"), b.get("email"), b.get("password"))
    if not r.get("ok"):
        return JSONResponse(status_code=400, content={"error": {"message": r.get("error")}})
    return {**r, "mail": postfach.status(adr)}


@router.get("/v1/engine/trade/{tid}")
def engine_trade(tid: str):
    """Bestaetigungsseite: Auftrag plus frisches Angebot. Die Wallet des Nutzers sendet selbst."""
    if not re.fullmatch(r"[0-9a-f]{16}", tid or ""):
        raise HTTPException(status_code=404, detail="not found")
    if not _bremse("trade:" + tid, 2):
        raise HTTPException(status_code=429, detail="slow down a little")
    import handel
    r = handel.seite(tid)
    if not r.get("ok"):
        return JSONResponse(status_code=400, content={"error": {"message": r.get("error")}, "status": r.get("status"), "hashes": r.get("hashes")})
    return r


@router.post("/v1/engine/trade/{tid}/sent")
async def engine_trade_sent(tid: str, request: Request):
    if not re.fullmatch(r"[0-9a-f]{16}", tid or ""):
        raise HTTPException(status_code=404, detail="not found")
    b = await _json(request)
    import handel
    return handel.gesendet(tid, b.get("hashes") or [], bool(b.get("ok", True)), b.get("error"))


@router.post("/v1/engine/trade/{tid}/build")
async def engine_trade_build(tid: str, request: Request):
    """Solana (seit 2.39): mit der verbundenen Wallet eine frische Transaktion bauen. Nur die oeffentliche Adresse kommt herein."""
    if not re.fullmatch(r"[0-9a-f]{16}", tid or ""):
        raise HTTPException(status_code=404, detail="not found")
    if not _bremse("tradeb:" + tid, 6):
        raise HTTPException(status_code=429, detail="slow down a little")
    b = await _json(request)
    import handel
    r = await asyncio.to_thread(handel.sol_bauen, tid, b.get("pubkey"))
    if not r.get("ok"):
        return JSONResponse(status_code=400, content={"error": {"message": r.get("error")}})
    return r


@router.get("/v1/engine/trade/{tid}/status")
def engine_trade_status(tid: str, sig: str = ""):
    if not re.fullmatch(r"[0-9a-f]{16}", tid or "") or not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{60,100}", sig or ""):
        raise HTTPException(status_code=404, detail="not found")
    if not _bremse("trades:" + tid, 30):
        raise HTTPException(status_code=429, detail="slow down a little")
    import handel
    return handel.sol_status(sig)


@router.get("/v1/engine/scan")
def engine_scan(request: Request, q: str = ""):
    """Live-Seite fuer einen Coin (seit 2.39). Kein neuer Tiefen-Scan.
    Seit 2.44 auch ohne Anmeldung lesbar, wenn der Schalter 'scan' an ist (Link aus Telegram, Gruppen, geteilte Links):
    es sind oeffentliche Chain-Daten. Handeln und Recherche bleiben hinter der Anmeldung."""
    adr = _sitzung_oder_none(request)
    import motor
    import features
    if not (_admin_ok(request) or (motor._scan_an(adr) if adr else features.an("scan"))):
        raise HTTPException(status_code=403, detail="not live yet")
    if not _bremse("scan:" + (adr or "ip:" + _ip(request)), 30 if adr else 15):
        raise HTTPException(status_code=429, detail="slow down a little")
    import scan
    r = scan.seite(q)
    if not r.get("ok"):
        return JSONResponse(status_code=400, content={"error": {"message": r.get("error")}})
    r["can_trade"] = bool(adr) and motor._handel_an(adr)
    r["signed_in"] = bool(adr)
    return r


@router.get("/v1/engine/scan/trades")
def engine_scan_trades(request: Request, chain: str = "", pair: str = "", dev: str = ""):
    adr = _sitzung_oder_none(request)
    if not adr:
        import features
        if not features.an("scan"):
            raise HTTPException(status_code=401, detail="session expired, connect your wallet again")
    if not _bremse("scant:" + (adr or "ip:" + _ip(request)), 40 if adr else 20):
        raise HTTPException(status_code=429, detail="slow down a little")
    import scan
    if chain not in scan.KETTEN or not re.fullmatch(r"[0-9A-Za-z_]{20,80}", pair or ""):
        raise HTTPException(status_code=400, detail="bad pair")
    tr = scan.trades(chain, pair)
    d = (dev or "").lower()
    for t in tr:
        if d and str(t.get("wallet") or "").lower() == d:
            t["tag"] = "dev"
    m = scan.zahlen(chain, (request.query_params.get("token") or "")) if request.query_params.get("token") else {}
    return {"trades": tr, "market": m}


@router.post("/v1/engine/scan/research")
async def engine_scan_research(request: Request):
    """Projekt-Recherche von der Live-Seite (seit 2.39.2). Kostet vom Guthaben."""
    adr = _sitzung(request)
    import motor
    if not motor._scan_an(adr):
        raise HTTPException(status_code=403, detail="not live yet")
    if not _bremse("research:" + adr, 4):
        raise HTTPException(status_code=429, detail="slow down a little")
    b = await _json(request)
    import projekt
    if not motor._darf(adr):
        return JSONResponse(status_code=402, content={"error": {"message": motor.LEER}})
    d = await asyncio.to_thread(projekt.recherche, adr, str(b.get("q") or ""), bool(b.get("fresh")))
    try:
        import aktiv
        aktiv.merken(adr, "research", f"{d.get('chain')}:{d.get('token')} ${d.get('symbol') or ''} web", 0 if d.get("cached") else d.get("cost_usd"))
    except Exception:
        pass
    if d.get("error") or d.get("ok") is False:
        return JSONResponse(status_code=400, content={"error": {"message": d.get("error")}})
    return {k: v for k, v in d.items() if k != "links"} | {"links": d.get("links")}


@router.post("/v1/engine/scan/trade")
async def engine_scan_trade(request: Request):
    """Kauf/Verkauf direkt von der Live-Seite: legt denselben Auftrag an wie im Chat."""
    adr = _sitzung(request)
    import motor
    if not motor._handel_an(adr):
        raise HTTPException(status_code=403, detail="trading is not open yet")
    if not _bremse("scanbuy:" + adr, 10):
        raise HTTPException(status_code=429, detail="slow down a little")
    b = await _json(request)
    import handel
    r = await asyncio.to_thread(handel.vorschlag, adr, b.get("side") or "buy", b.get("token"), b.get("amount"),
                                b.get("unit"), b.get("slippage_pct"), True)
    if not r.get("ok"):
        return JSONResponse(status_code=400, content={"error": {"message": r.get("error")}})
    return r


@router.post("/v1/engine/agent/speak")
async def engine_agent_speak(request: Request):
    """Eine Antwort vorlesen (seit 2.39). MP3 zurueck, kostet VOICE_USD_PER_1K vom Guthaben."""
    adr = _sitzung(request)
    import motor
    if not motor._schalter("voice", adr):
        raise HTTPException(status_code=403, detail="voice is not open yet")
    if not _bremse("speak:" + adr, 8):
        raise HTTPException(status_code=429, detail="slow down a little")
    b = await _json(request)
    mp3, fehler = await asyncio.to_thread(motor.sprechen, adr, str(b.get("text") or "")[:4000])
    if not mp3:
        return JSONResponse(status_code=400, content={"error": {"message": fehler}})
    from fastapi.responses import Response
    return Response(content=mp3, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


@router.post("/v1/engine/agent/watch")
async def engine_agent_watch(request: Request):
    """'Watch it' von der Seite: Live-Wache wie im Chat."""
    adr = _sitzung(request)
    import motor
    if not motor._scan_an(adr):
        raise HTTPException(status_code=403, detail="not live yet")
    if not _bremse("watch:" + adr, 6):
        raise HTTPException(status_code=429, detail="slow down a little")
    b = await _json(request)
    r = await asyncio.to_thread(motor.watch_token, adr, str(b.get("q") or ""), b.get("minutes") or 60, b.get("every") or 5, b.get("move_pct") or 10)
    if not r.get("ok"):
        return JSONResponse(status_code=400, content={"error": {"message": r.get("why") or r.get("error")}})
    return r


@router.get("/v1/admin/intel")
def admin_intel(request: Request):
    """Stand der eigenen Wallet-Datenbank (seit 2.39.1)."""
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="admin only")
    import intel
    return intel.stand()


@router.post("/v1/engine/agent/news")
async def engine_agent_news(request: Request):
    """Nachrichtenquelle an/aus und sofort oder im Briefing (seit 2.41)."""
    adr = _sitzung(request)
    if not _bremse("news:" + adr, 30):
        raise HTTPException(status_code=429, detail="slow down a little")
    b = await _json(request)
    import motor
    import nachrichten
    q = str(b.get("source") or "").strip()
    slug = str(b.get("slug") or "").strip()
    if slug:
        such = next((s for k, _, s in nachrichten.KATALOG if k == slug), None)
        q = such or slug
    if not q:
        return JSONResponse(status_code=400, content={"error": {"message": "Which source?"}})
    if b.get("on") is False:
        r = await asyncio.to_thread(motor.unfollow_news, adr, q)
    else:
        r = await asyncio.to_thread(motor.follow_news, adr, q, b.get("keywords"), b.get("mode") or "briefing")
    if not r.get("ok"):
        return JSONResponse(status_code=400, content={"error": {"message": r.get("error") or "Could not change that."}})
    r["catalog"] = motor.news_katalog(adr)
    return r


@router.post("/v1/engine/agent/stocks")
async def engine_agent_stocks(request: Request):
    adr = _sitzung(request)
    if not _bremse("stocks:" + adr, 10):
        raise HTTPException(status_code=429, detail="slow down a little")
    b = await _json(request)
    import motor
    return await asyncio.to_thread(motor.set_stocks, adr, b.get("tickers") or "")


@router.post("/v1/engine/agent/coins")
async def engine_agent_coins(request: Request):
    adr = _sitzung(request)
    if not _bremse("coins:" + adr, 10):
        raise HTTPException(status_code=429, detail="slow down a little")
    b = await _json(request)
    import motor
    return await asyncio.to_thread(motor.set_coins, adr, b.get("coins") or "")


@router.get("/v1/admin/engine/users")
def admin_engine_users(request: Request, wallet: str = Query(None)):
    """Was die Nutzer von Your Engine tun: je Nutzer und die letzten Ereignisse (seit 2.40)."""
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="admin only")
    import aktiv
    if wallet:
        return {"feed": aktiv.feed(200, wallet.strip().lower() if wallet.startswith("0x") else wallet.strip())}
    return {"users": aktiv.uebersicht(), "feed": aktiv.feed(80)}


@router.get("/v1/admin/smart")
def admin_smart(request: Request):
    """Smart-Money-Calls: Liste, Runde, Sichtungen, Calls, Bilanz (seit 2.40)."""
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="admin only")
    import smart
    out = smart.stand()
    out["calls"] = smart.bilanz(30, 30)["calls"]
    return out


@router.get("/v1/engine/smart/record")
def engine_smart_record():
    """Oeffentliche Bilanz der verschickten Smart-Money-Calls, nur wenn der Schalter smart_calls an ist."""
    import features
    if not features.an("smart_calls"):
        raise HTTPException(status_code=404, detail="not live")
    import smart
    b = smart.bilanz(30, 50)
    b["calls"] = [c for c in b["calls"] if c.get("sent")]
    return b


@router.post("/v1/hooks/helius")
async def hook_helius(request: Request):
    """Helius schickt Kaeufe der beobachteten Smart-Money-Wallets (seit 2.45). Nur mit unserem Geheimnis."""
    import hmac
    import smart
    if not hmac.compare_digest(request.headers.get("authorization") or "", smart.hook_geheimnis()):
        raise HTTPException(status_code=401, detail="bad auth")
    try:
        daten = await request.json()
    except Exception:
        daten = []
    threading.Thread(target=smart.hook_empfangen, args=(daten,), daemon=True).start()
    return {"ok": True}


@router.post("/v1/admin/intel/import")
def admin_intel_import(request: Request):
    """Einmalige Uebernahme einer Wallet-Datenbank aus INTEL_IMPORT_URL (nur lesen). Laeuft im Hintergrund."""
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="admin only")
    url = os.getenv("INTEL_IMPORT_URL", "").strip()
    if not url:
        return JSONResponse(status_code=400, content={"error": {"message": "Set INTEL_IMPORT_URL on the web service first, then press import again."}})
    import intel
    threading.Thread(target=intel.importieren, args=(url,), daemon=True).start()
    return {"ok": True, "started": True}


@router.post("/v1/engine/agent/torch")
async def engine_agent_torch(request: Request):
    """Torch-Profilname fuer die Holder-Rewards speichern (oder mit remove entfernen)."""
    adr = _sitzung(request)
    if not _bremse("torch:" + adr, 3):
        raise HTTPException(status_code=429, detail="slow down a little")
    b = await _json(request)
    import motor
    if b.get("remove"):
        z = motor.zustand(adr)
        z["prefs"].pop("torch", None)
        motor._zustand_speichern(adr, z)
        return {"ok": True}
    r = motor.set_torch_profile(adr, b.get("name"))
    if not r.get("ok"):
        return JSONResponse(status_code=400, content={"error": {"message": r.get("error")}})
    return r


@router.post("/v1/engine/agent/calendar")
async def engine_agent_calendar(request: Request):
    adr = _sitzung(request)
    if not _bremse("cal:" + adr, 4):
        raise HTTPException(status_code=429, detail="slow down a little")
    b = await _json(request)
    import kalender
    if b.get("disconnect"):
        return kalender.trennen(adr)
    r = kalender.setzen(adr, b.get("url"))
    if not r.get("ok"):
        return JSONResponse(status_code=400, content={"error": {"message": r.get("error")}})
    return {**r, "calendar": kalender.status(adr)}


@router.post("/v1/engine/agent/clear")
def engine_agent_clear(request: Request):
    adr = _sitzung(request)
    import motor
    motor.verlauf_loeschen(adr)
    return {"ok": True}


# ── OddsEngine AI: Seite ─────────────────────────────────────────────────

@router.get("/v1/ai/models")
def ai_models(request: Request):
    liste = ai.modelle()
    return {"models": liste, "markup": ai.markup(), "count": len(liste),
            "live": _an("ai"), "unit": "USD per 1M tokens, OddsEngine fee included"}


@router.post("/v1/ai/keys")
async def ai_keys_new(request: Request):
    adr = _sitzung(request)
    if not _offen(request, "ai"):
        raise HTTPException(status_code=403, detail="OddsEngine AI is not live yet")
    b = await _json(request)
    try:
        if b.get("day_cap") or b.get("models"):
            return agents.key_mit_grenzen(adr, b.get("name"), b.get("day_cap"), b.get("models"))
        return ai.key_neu(adr, b.get("name"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/v1/ai/keys/limits")
async def ai_keys_limits(request: Request):
    """Tagesgrenze in Dollar und erlaubte Modelle fuer einen Key. Leer heisst ohne Grenze."""
    adr = _sitzung(request)
    b = await _json(request)
    try:
        return agents.grenzen_setzen(adr, str(b.get("id") or ""), b.get("day_cap"), b.get("models"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="day_cap must be a number of dollars")


@router.post("/v1/ai/keys/delete")
async def ai_keys_delete(request: Request):
    adr = _sitzung(request)
    b = await _json(request)
    ai.key_weg(adr, b.get("id"))
    return {"ok": True}


@router.post("/v1/ai/quote")
async def ai_quote(request: Request):
    """Aufladen mit festem Dollarbetrag: Kurs 10 Minuten halten."""
    adr = _sitzung(request)
    if not _bremse("quote:" + adr, 10):
        raise HTTPException(status_code=429, detail="slow down a little")
    b = await _json(request)
    try:
        return ai.angebot(adr, b.get("usd"))
    except ai.Fehler as e:
        return _fehler(e)


@router.post("/v1/ai/deposits/check")
def ai_deposit_check(request: Request):
    adr = _sitzung(request)
    if not _bremse("dep:" + adr, 4):
        raise HTTPException(status_code=429, detail="checking already, give it a few seconds")
    r = ai.scannen(erzwingen=True)
    neu = ai.offene_zuordnen(adr)
    kt = ai.konto(adr) or {}
    return {"ok": True, "scan": r, "credited_now": round(neu, 4),
            "balance_usd": round(kt.get("credit", 0.0), 4)}


def _fehler(e: "ai.Fehler"):
    return JSONResponse(status_code=e.status,
                        content={"error": {"message": e.text, "type": e.art, "code": e.status}})


@router.post("/v1/ai/chat")
async def ai_chat(request: Request):
    adr = _sitzung(request)
    if not _offen(request, "ai"):
        raise HTTPException(status_code=403, detail="OddsEngine AI is not live yet")
    if not _bremse("pg:" + adr, 30):
        return JSONResponse(status_code=429, content={"error": {"message": "slow down a little",
                                                                "type": "rate_limit"}})
    b = await _json(request)
    try:
        if b.get("stream"):
            return StreamingResponse(ai.strom(adr, None, b), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        return ai.anfrage(adr, None, b)
    except ai.Fehler as e:
        return _fehler(e)


@router.post("/v1/admin/ai/alert-test")
def admin_ai_alert_test(request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="admin only")
    ok = ai._an_betreiber("OddsEngine AI: Testwarnung. So sieht es aus, wenn bei OpenRouter 70 % "
                          "verbraucht sind. Alles in Ordnung, das war nur ein Test.")
    return {"ok": ok}


@router.get("/v1/admin/ai")
def admin_ai(request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="admin only")
    try:
        return ai.admin_lage()
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)[:200])


# ── OpenAI-kompatibel ────────────────────────────────────────────────────

@router.get("/v1/models")
def oa_models():
    return {"object": "list", "data": [
        {"id": m["id"], "object": "model", "owned_by": m["provider"],
         "created": m.get("created") or 0, "context_length": m.get("context"),
         "pricing": {"input_per_1m_usd": m["in"], "output_per_1m_usd": m["out"]}}
        for m in ai.modelle()]}


def _ip(request):
    return (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip() or \
        (request.client.host if request.client else "?")


@router.post("/v1/chat/completions")
async def oa_chat(request: Request):
    auth = request.headers.get("Authorization") or ""
    roh = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    b = await _json(request)
    zahlung = None
    if not roh:
        # x402: ohne Key, bezahlt pro Anfrage in $ODDS
        if not (_an("x402") and _an("ai")) and not _admin_ok(request):
            return _fehler(ai.Fehler(401, "missing API key. Create one on engine.oddsengine.live",
                                     "invalid_api_key"))
        import usdc as _usdc
        if not (agents.bereit() or _usdc.bereit()):
            return _fehler(ai.Fehler(503, "pay per request is not available right now, use an API key",
                                     "service_unavailable"))
        if not _bremse("x402:" + _ip(request), 20):
            return _fehler(ai.Fehler(429, "rate limit: 20 paid requests per minute", "rate_limit"))
        kopf = (request.headers.get("X-PAYMENT") or "").strip()
        neu_kopf = (request.headers.get("PAYMENT-SIGNATURE") or "").strip()
        ist_v2 = bool(neu_kopf and not kopf)
        kopf = kopf or neu_kopf
        try:
            if not kopf:
                ang = agents.angebot(b, str(request.url))
                v2 = ang.pop("_v2", None)
                return JSONResponse(status_code=402, content=ang,
                                    headers={"PAYMENT-REQUIRED": agents.angebot_kopf(v2)} if v2 else {})
            payer, zahlung = await asyncio.to_thread(agents.bezahlen, kopf, b)
        except ai.Fehler as e:
            return _fehler(e)
        except Exception as e:
            return _fehler(ai.Fehler(502, "payment could not be settled: " + str(e)[:120], "payment_failed"))
        adr, kid = payer, None
    else:
        try:
            wer = ai.key_pruefen(roh)
        except Exception:
            return _fehler(ai.Fehler(503, "service unavailable", "service_unavailable"))
        if not wer:
            return _fehler(ai.Fehler(401, "invalid API key. Create one on engine.oddsengine.live",
                                     "invalid_api_key"))
        adr, kid = wer
        if not _an("ai") and not _admin_ok(request):
            return _fehler(ai.Fehler(403, "OddsEngine AI is not live yet", "not_live"))
        if not _bremse("k:" + kid):
            return _fehler(ai.Fehler(429, "rate limit: 60 requests per minute per key", "rate_limit"))
    kopf_aus = {"X-PAYMENT-RESPONSE": agents.antwort_kopf(zahlung),
                "PAYMENT-RESPONSE": agents.antwort_kopf(zahlung, v2=True)} if zahlung else {}
    try:
        if kid:
            agents.pruefen(kid, b.get("model"))
        if b.get("stream"):
            return StreamingResponse(ai.strom(adr, kid, b), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                                              **kopf_aus})
        return JSONResponse(content=ai.anfrage(adr, kid, b), headers=kopf_aus)
    except ai.Fehler as e:
        r = _fehler(e)
        for k, v in kopf_aus.items():
            r.headers[k] = v
        return r


@router.get("/v1/x402/buybacks")
def x402_buybacks(request: Request):
    """Oeffentliches Protokoll: USDC-Zahlungen von Agenten, Rueckkaeufe von $ODDS, Burns."""
    if not _an("x402usdc") and not _admin_ok(request):
        raise HTTPException(status_code=404, detail="not live")
    try:
        import usdc as _usdc
        return _usdc.oeffentlich()
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)[:160])


@router.post("/v1/admin/buyback")
async def admin_buyback(request: Request):
    """Admin: Rueckkauf jetzt, z. B. {"usd": 2}. Die USDC muessen extra auf der Kasse liegen."""
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="admin only")
    b = await _json(request)
    import usdc as _usdc
    try:
        return await asyncio.to_thread(_usdc.jetzt_kaufen, float(b.get("usd") or 0))
    except Exception as e:
        return {"ok": False, "why": str(e)[:200]}


# ── Support-Postfach (seit 2.33) ─────────────────────────────────────────

@router.post("/v1/support")
async def support_web(request: Request):
    """Formular auf engine.oddsengine.live. Mit Sitzung wird die Wallet mitgespeichert."""
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0].strip()
    if not _bremse("support:" + ip, 6):
        return JSONResponse(status_code=429, content={"error": {"message": "Thanks, we got your messages. Give us a little time."}})
    b = await _json(request)
    text = str(b.get("text") or "").strip()
    if len(text) < 3 and not b.get("images"):
        return JSONResponse(status_code=400, content={"error": {"message": "Write a few words first."}})
    adr = None
    try:
        adr = _sitzung(request)
    except Exception:
        adr = None
    import support
    import base64 as _b64
    bilder = []
    for u in (b.get("images") or [])[:3]:
        m = re.match(r"^data:(image/(?:png|jpeg|jpg|webp));base64,([A-Za-z0-9+/=\s]+)$", str(u or ""))
        if m:
            try:
                d = _b64.b64decode(re.sub(r"\s", "", m.group(2)))
                if len(d) <= support.MAX_BILD:
                    bilder.append((m.group(1), d))
            except Exception:
                pass
    tid = support.neu("web", text, wallet=adr, name=str(b.get("contact") or "")[:80] or None,
                      context=f"Page: {str(b.get('page') or '')[:120]}", melden=True, bilder=bilder)
    return {"ok": bool(tid), "ticket": tid}


@router.get("/v1/admin/support")
def admin_support(request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="admin only")
    import support
    return support.liste(request.query_params.get("status") or "open", request.query_params.get("n") or 100)


@router.get("/v1/admin/support/file/{fid}")
def admin_support_file(fid: int, request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="admin only")
    import support
    from fastapi.responses import Response
    mime, d = support.datei(fid)
    if not d:
        raise HTTPException(status_code=404, detail="not found")
    return Response(content=d, media_type=mime or "image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/v1/admin/support/report")
async def admin_support_report(request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="admin only")
    import support
    try:
        return await asyncio.to_thread(support.bericht)
    except Exception as e:
        return JSONResponse(status_code=502, content={"error": {"message": str(e)[:200]}})


@router.post("/v1/admin/support/{tid}")
async def admin_support_set(tid: int, request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="admin only")
    b = await _json(request)
    import support
    if b.get("reply"):
        return await asyncio.to_thread(support.antworten, tid, b.get("reply"))
    return support.setzen(tid, b.get("status"), b.get("note"))


# ── Your Engine Beta: Tester und Startguthaben (seit 2.33) ──────────────

@router.get("/v1/admin/engine/beta")
def admin_engine_beta(request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="admin only")
    import motor
    import storage
    out = []
    for a in sorted(motor.beta_liste()):
        kt = ai.konto(a, anlegen=False) or {}
        out.append({"wallet": a, "credit": round(kt.get("credit", 0.0), 4), "spent": round(kt.get("spent", 0.0), 4),
                    "telegram": _telegram_verknuepft(a), "granted_usd": float(storage.kv_get("beta_grant_" + a) or 0),
                    "from_env": a in {holder.norm(x) for x in os.getenv("ENGINE_BETA", "").split(",") if x.strip()}})
    return {"testers": out, "count": len(out), "switch_on": _an("engine"),
            "granted_total_usd": round(sum(x["granted_usd"] for x in out), 2),
            "spent_total_usd": round(sum(x["spent"] for x in out), 4)}


@router.post("/v1/admin/engine/beta")
async def admin_engine_beta_set(request: Request):
    """{"add": ["0x..."], "remove": ["0x..."], "grant_usd": 1}. Startguthaben gibt es je Wallet nur einmal."""
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="admin only")
    import motor
    import storage
    from decimal import Decimal
    b = await _json(request)
    liste = {holder.norm(x) for x in json.loads(storage.kv_get("engine_beta_list") or "[]")}
    ergebnis = []
    grant = max(0.0, min(float(b.get("grant_usd") or 0), 10.0))
    for roh in (b.get("add") or [])[:100]:
        a = str(roh or "").strip()
        if not holder.adresse_ok(a) or not a.startswith("0x"):
            ergebnis.append({"wallet": a, "ok": False, "why": "not an EVM address"})
            continue
        a = holder.norm(a)
        liste.add(a)
        r = {"wallet": a, "ok": True}
        if grant > 0 and not storage.kv_get("beta_grant_" + a):
            ai.konto(a, anlegen=True)
            ai._pg().execute("UPDATE ai_accounts SET credit = credit + %s WHERE address=%s",
                             (Decimal(str(round(grant, 4))), a))
            storage.kv_set("beta_grant_" + a, str(grant))
            r["granted_usd"] = grant
        ergebnis.append(r)
    for roh in (b.get("remove") or [])[:100]:
        liste.discard(holder.norm(str(roh or "").strip()))
    if len(liste) > 300:
        return JSONResponse(status_code=400, content={"error": {"message": "too many testers"}})
    storage.kv_set("engine_beta_list", json.dumps(sorted(liste)))
    return {"ok": True, "results": ergebnis, "count": len(motor.beta_liste())}


# ── Agenten (seit 2.31) ──────────────────────────────────────────────────

@router.post("/v1/agents/key")
async def agents_key(request: Request):
    """API-Key per Wallet-Unterschrift, ohne Browser. Erst GET /v1/engine/nonce?address=...,
    die Nachricht mit personal_sign unterschreiben, dann hierher."""
    if not (_an("agents") and _an("ai")) and not _admin_ok(request):
        raise HTTPException(status_code=403, detail="agent keys are not live yet")
    b = await _json(request)
    adr = str(b.get("address") or "").strip()
    sig = b.get("signature")
    if not adr or sig is None or not holder.adresse_ok(adr):
        raise HTTPException(status_code=400, detail="address and signature required")
    adr = holder.norm(adr)
    if not _bremse("agk:" + adr, 5):
        raise HTTPException(status_code=429, detail="slow down a little")
    try:
        ok = holder.pruefe_unterschrift(adr, sig)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=401, detail="signature does not match this address")
    try:
        k = agents.key_mit_grenzen(adr, b.get("name") or "agent", b.get("day_cap"), b.get("models"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    kt = ai.konto(adr) or {}
    return {**k, "address": adr, "balance_usd": round(kt.get("credit", 0.0), 4),
            "base_url": "https://web-production-6b61c.up.railway.app/v1",
            "deposit_address": ai.eingang()}


@router.get("/mcp")
def mcp_get():
    return JSONResponse(status_code=405, content={"error": "use POST with JSON-RPC 2.0"},
                        headers={"Allow": "POST"})


@router.post("/mcp")
async def mcp_post(request: Request):
    if not (_an("agents") and _an("ai")) and not _admin_ok(request):
        return JSONResponse(status_code=403, content={"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32000, "message": "MCP is not live yet"}})
    auth = request.headers.get("Authorization") or ""
    roh = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    wer = None
    if roh:
        try:
            wer = ai.key_pruefen(roh)
        except Exception:
            wer = None
        if not wer:
            return JSONResponse(status_code=401, content={"jsonrpc": "2.0", "id": None,
                                "error": {"code": -32001, "message": "invalid API key"}})
    if not _bremse("mcp:" + (wer[1] if wer else _ip(request)), 60):
        return JSONResponse(status_code=429, content={"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32002, "message": "rate limit"}})
    try:
        b = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "parse error"}})
    if isinstance(b, list):
        out = [r for r in [await asyncio.to_thread(agents.mcp, x, wer) for x in b if isinstance(x, dict)] if r]
        return JSONResponse(content=out) if out else JSONResponse(status_code=202, content=None)
    if not isinstance(b, dict):
        return JSONResponse(status_code=400, content={"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32600, "message": "invalid request"}})
    r = await asyncio.to_thread(agents.mcp, b, wer)
    if r is None:
        return JSONResponse(status_code=202, content=None)
    return JSONResponse(content=r)


# ── Chat (seit 2.29.1) ───────────────────────────────────────────────────

import chat as _chat  # noqa: E402


def _chat_fehler(e):
    if isinstance(e, _chat.Fehler):
        return JSONResponse(status_code=e.status, content={"error": {"message": e.text}})
    if isinstance(e, ai.Fehler):
        return _fehler(e)
    return JSONResponse(status_code=503, content={"error": {"message": str(e)[:160]}})


def _chat_offen(request):
    if not _offen(request, "ai"):
        raise HTTPException(status_code=403, detail="OddsEngine AI is not live yet")


@router.get("/v1/chat/list")
def chat_list(request: Request):
    adr = _sitzung(request)
    _chat_offen(request)
    return {"chats": _chat.liste(adr)}


@router.post("/v1/chat/new")
async def chat_new(request: Request):
    adr = _sitzung(request)
    _chat_offen(request)
    b = await _json(request)
    return _chat.neu(adr, b.get("model"))


@router.get("/v1/chat/get/{cid}")
def chat_get(cid: str, request: Request):
    adr = _sitzung(request)
    _chat_offen(request)
    try:
        return _chat.laden(adr, cid)
    except Exception as e:
        return _chat_fehler(e)


@router.post("/v1/chat/rename/{cid}")
async def chat_rename(cid: str, request: Request):
    adr = _sitzung(request)
    b = await _json(request)
    try:
        _chat.umbenennen(adr, cid, b.get("title"))
        return {"ok": True}
    except Exception as e:
        return _chat_fehler(e)


@router.post("/v1/chat/delete/{cid}")
def chat_delete(cid: str, request: Request):
    adr = _sitzung(request)
    _chat.loeschen(adr, cid)
    return {"ok": True}


@router.post("/v1/chat/delete_all")
def chat_delete_all(request: Request):
    adr = _sitzung(request)
    _chat.alles_loeschen(adr)
    return {"ok": True}


@router.get("/v1/chat/img/{bid}")
def chat_img(bid: str, request: Request):
    adr = _sitzung(request)
    mime, daten = _chat.bild_laden(adr, bid)
    if not daten:
        raise HTTPException(status_code=404, detail="image not found or expired")
    from fastapi.responses import Response
    h = {"Cache-Control": "private, max-age=86400"}
    if request.query_params.get("dl"):
        endung = {"image/jpeg": "jpg", "image/webp": "webp"}.get(mime or "", "png")
        h["Content-Disposition"] = 'attachment; filename="oddsengine-{}.{}"'.format(bid[:8], endung)
    return Response(content=daten, media_type=mime or "image/png", headers=h)


@router.post("/v1/chat/send/{cid}")
async def chat_send(cid: str, request: Request):
    adr = _sitzung(request)
    _chat_offen(request)
    if not _bremse("chat:" + adr, 30):
        return JSONResponse(status_code=429, content={"error": {"message": "slow down a little"}})
    b = await _json(request)
    try:
        m, mode, msgs = _chat.vorbereiten(adr, cid, b)
    except Exception as e:
        return _chat_fehler(e)
    try:
        if mode == "image":
            return _chat.bild_erzeugen(adr, cid, m, msgs)
        gen = _chat.text_strom(adr, cid, m, msgs)
    except Exception as e:
        try:
            _chat.letzte_zuruecknehmen(adr, cid)
        except Exception:
            pass
        return _chat_fehler(e)
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def starten():
    threading.Thread(target=ai.waechter, daemon=True).start()
    try:
        import smart
        threading.Thread(target=smart.schleife, daemon=True).start()
        import wache
        threading.Thread(target=wache.schleife, daemon=True).start()
    except Exception as e:
        print(f"smart start: {e}")
