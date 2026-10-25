"""
ai.py: OddsEngine AI. Seit 2.29.

Jedes Modell ueber einen Schluessel, bezahlt in $ODDS. Dahinter steht EIN
OpenRouter-Konto (OPENROUTER_API_KEY auf dem Dienst web). Kunden bekommen
eigene Schluessel (oe_live_...), die API ist OpenAI-kompatibel.

Geld:
    Einzahlung  $ODDS auf der Robinhood Chain an AI_DEPOSIT_ADDRESS, von der
                bestaetigten Wallet oder einer verknuepften Wallet. Die Engine
                liest die Transfer-Logs des Tokens und schreibt den Dollarwert
                zum Kurs im Moment der Erkennung gut. Danach schwankt das
                Guthaben nicht mehr mit dem Kurs.
    Verbrauch   Was OpenRouter je Anfrage meldet (usage.cost), plus AI_MARKUP
                (Standard 0.08 = 8 %). Vor jeder Anfrage wird der hoechstmoegliche
                Betrag reserviert, danach auf den echten Betrag ausgeglichen.
                So kann niemand mit vielen Anfragen gleichzeitig ueberziehen.

Tabellen (Postgres, legen sich selbst an):
    ai_accounts   Guthaben je Adresse
    ai_keys       API-Schluessel, nur als SHA-256 gespeichert
    ai_usage      jede Anfrage: Modell, Tokens, Kosten OpenRouter, berechnet
    ai_deposits   jede erkannte Einzahlung, eindeutig je (tx, log index)

Variablen (Dienst web):
    OPENROUTER_API_KEY   Pflicht
    AI_DEPOSIT_ADDRESS   Empfangs-Wallet, Standard siehe unten
    AI_MARKUP            0.08
    AI_MODELS            leer oder "all" = alle bezahlten Modelle, "curated" = nur die Auswahl, oder eigene Liste
                         Liste "anbieter/modell,anbieter/modell"
    AI_SCAN_BACK         wie viele Bloecke beim ersten Lauf zurueck (200000)
    AI_MIN_CREDIT        Mindestguthaben fuer eine Anfrage in Dollar (0.01)
"""
import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
from decimal import Decimal

import requests

import holder
import storage
from logger import log

OR_BASE = os.getenv("AI_OR_BASE", "").strip() or "https://openrouter.ai/api/v1"
STANDARD_EINGANG = "0xaA0C5bA57c4Bd19b232D63828835267B36136907"

TOP_ANBIETER = ("openai/", "anthropic/", "google/", "x-ai/", "deepseek/", "moonshotai/", "qwen/", "z-ai/",
                "mistralai/", "meta-llama/")
KURATIERT = [
    # Reihenfolge = "Popular" auf der Seite
    "anthropic/claude-sonnet-5.5", "openai/gpt-6.1-sol", "google/gemini-4-argon",
    "anthropic/claude-opus-5.5", "openai/gpt-6-astra", "openai/gpt-6-astra-pro", "x-ai/grok-4.7", "deepseek/deepseek-v4-pro",
    "anthropic/claude-haiku-5.5", "openai/gpt-6.1-sol-pro", "google/gemini-3.8-flash",
    "openai/gpt-6-luna", "moonshotai/kimi-k3.1-pro", "moonshotai/kimi-k3", "qwen/qwen3.8-max-prime", "qwen/qwen3.8-max-0902",
    "z-ai/glm-5.3-prime",
    "deepseek/deepseek-v4.1-flash", "z-ai/glm-5.3", "mistralai/mistral-large-4-0",
    "meta-llama/llama-4-maverick", "anthropic/claude-fable-5.1", "x-ai/grok-build-0.1",
    "moonshotai/kimi-k2.7-code", "mistralai/devstral-2512", "qwen/qwen3.8-flash",
    "minimax/minimax-m3", "perplexity/sonar-pro", "perplexity/sonar-reasoning-pro",
    "mistralai/mistral-medium-3-5", "meta-llama/llama-3.3-70b-instruct",
    "nvidia/nemotron-3-ultra-550b-a55b", "cohere/command-a", "amazon/nova-pro-v1",
]
BILD_MODELLE = [
    "google/gemini-nano-banana-2.1", "openai/gpt-5.4-image-2", "google/gemini-3-pro-image",
    "google/gemini-3.1-flash-image", "openai/gpt-5-image-mini",
]
SCHREIBEN = {"anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5", "openai/gpt-6.1-sol",
             "google/gemini-4-argon", "anthropic/claude-fable-5.1", "mistralai/mistral-large-4-0",
             "moonshotai/kimi-k3"}
CODE = {"anthropic/claude-sonnet-5.5", "anthropic/claude-opus-5.5", "openai/gpt-6.1-sol",
        "moonshotai/kimi-k2.7-code", "mistralai/devstral-2512", "x-ai/grok-build-0.1",
        "deepseek/deepseek-v4-pro", "z-ai/glm-5.3"}
BILD_TOKENS = {"google": 1300, "openai": 4200}   # grobe Bild-Tokens je Bild, nur fuer die Anzeige
AUTO_ID = "oddsengine/auto"
AUTO_OR = "openrouter/auto"
ANBIETER = {
    "anthropic": "Anthropic", "openai": "OpenAI", "google": "Google", "x-ai": "xAI",
    "deepseek": "DeepSeek", "meta-llama": "Meta", "mistralai": "Mistral", "qwen": "Qwen",
    "moonshotai": "Moonshot", "z-ai": "Z.ai", "minimax": "MiniMax", "perplexity": "Perplexity",
    "nvidia": "NVIDIA", "cohere": "Cohere", "amazon": "Amazon", "microsoft": "Microsoft",
    "openrouter": "OpenRouter", "meta": "Meta", "bytedance-seed": "ByteDance", "bytedance": "ByteDance",
    "aion-labs": "Aion Labs", "ibm-granite": "IBM", "z-ai": "Z.ai", "moonshotai": "Moonshot",
    "thinkingmachines": "Thinking Machines", "inclusionai": "inclusionAI", "nousresearch": "Nous Research",
    "arcee-ai": "Arcee", "rekaai": "Reka", "stepfun": "StepFun", "xiaomi": "Xiaomi", "tencent": "Tencent",
}


def _f(k, d):
    try:
        return float(os.getenv(k, "").strip() or d)
    except Exception:
        return float(d)


def markup(adresse=None):
    """Aufschlag. Ohne Adresse der Grundpreis, mit Adresse die Holder-Stufe."""
    basis = max(0.0, min(1.0, _f("AI_MARKUP", 0.095)))
    if not adresse:
        return basis
    return holder_stufe(adresse)["markup"]


def stufen():
    """AI_HOLDER_TIERS = "250000:0.09,1000000:0.085,..." (ab Menge: Aufschlag).
    Nie unter AI_MARKUP_FLOOR, damit keine Anfrage Verlust macht."""
    roh = os.getenv("AI_HOLDER_TIERS", "250000:0.09,1000000:0.085,3000000:0.08,10000000:0.075").strip()
    boden = max(0.0, _f("AI_MARKUP_FLOOR", 0.075))
    basis = markup()
    out = []
    for teil in roh.split(","):
        try:
            menge, satz = teil.split(":")
            out.append((float(menge), max(boden, min(basis, float(satz)))))
        except Exception:
            continue
    return sorted(out)


_STUFE = {}


def holder_stufe(adresse):
    """Gehaltene $ODDS (verbundene plus verknuepfte Wallets) und der Aufschlag dazu.
    Fuenf Minuten gemerkt. Faellt die Abfrage aus, gilt der Grundpreis."""
    a = holder.norm(adresse)
    t = _STUFE.get(a)
    if t and time.time() - t[0] < 300:
        return t[1]
    menge = None
    try:
        menge = holder.guthaben(a)
        for b in holder.verknuepft(a):
            menge += holder.guthaben(b)
    except Exception as e:
        log.warning(f"ai: holdings {e}")
    satz, naechste = markup(), None
    for schwelle, s in stufen():
        if menge is not None and menge >= schwelle:
            satz = s
        elif naechste is None:
            naechste = {"hold": schwelle, "markup": s}
    erg = {"held": round(menge or 0.0, 2), "markup": satz, "next": naechste,
           "tiers": [{"hold": 0, "markup": markup()}] + [{"hold": x, "markup": y} for x, y in stufen()]}
    _STUFE[a] = (time.time(), erg)
    return erg


def or_key():
    return os.getenv("OPENROUTER_API_KEY", "").strip()


def eingang():
    a = os.getenv("AI_DEPOSIT_ADDRESS", "").strip() or STANDARD_EINGANG
    return a.lower()


def bereit():
    return bool(or_key())


# ── Datenbank ────────────────────────────────────────────────────────────

_SCHEMA_OK = False
_LOCK = threading.Lock()


def _pg():
    global _SCHEMA_OK
    pg = storage._init_pg()
    if not pg:
        raise RuntimeError("OddsEngine AI needs the database")
    if not _SCHEMA_OK:
        with _LOCK:
            if not _SCHEMA_OK:
                pg.execute("""CREATE TABLE IF NOT EXISTS ai_accounts (
                    address TEXT PRIMARY KEY,
                    credit NUMERIC(20,8) NOT NULL DEFAULT 0,
                    deposited NUMERIC(20,8) NOT NULL DEFAULT 0,
                    spent NUMERIC(20,8) NOT NULL DEFAULT 0,
                    created TIMESTAMPTZ DEFAULT now())""")
                pg.execute("""CREATE TABLE IF NOT EXISTS ai_keys (
                    id TEXT PRIMARY KEY,
                    address TEXT NOT NULL,
                    hash TEXT UNIQUE NOT NULL,
                    prefix TEXT NOT NULL,
                    name TEXT,
                    created TIMESTAMPTZ DEFAULT now(),
                    last_used TIMESTAMPTZ,
                    spent NUMERIC(20,8) NOT NULL DEFAULT 0,
                    requests BIGINT NOT NULL DEFAULT 0,
                    revoked BOOLEAN NOT NULL DEFAULT false)""")
                pg.execute("CREATE INDEX IF NOT EXISTS ix_ai_keys_addr ON ai_keys(address)")
                pg.execute("""CREATE TABLE IF NOT EXISTS ai_usage (
                    id BIGSERIAL PRIMARY KEY,
                    ts TIMESTAMPTZ DEFAULT now(),
                    address TEXT NOT NULL,
                    key_id TEXT,
                    model TEXT,
                    tokens_in INTEGER,
                    tokens_out INTEGER,
                    cost_or NUMERIC(20,8),
                    charged NUMERIC(20,8),
                    gen_id TEXT,
                    status TEXT)""")
                pg.execute("CREATE INDEX IF NOT EXISTS ix_ai_usage_addr ON ai_usage(address, ts)")
                pg.execute("""CREATE TABLE IF NOT EXISTS ai_deposits (
                    tx TEXT NOT NULL,
                    li INTEGER NOT NULL,
                    block BIGINT,
                    sender TEXT NOT NULL,
                    odds NUMERIC(38,8) NOT NULL,
                    usd NUMERIC(20,8),
                    price NUMERIC(30,12),
                    ts TIMESTAMPTZ DEFAULT now(),
                    credited_to TEXT,
                    credited_ts TIMESTAMPTZ,
                    PRIMARY KEY (tx, li))""")
                pg.execute("CREATE INDEX IF NOT EXISTS ix_ai_dep_sender ON ai_deposits(sender)")
                _SCHEMA_OK = True
    return pg


def _num(x):
    return float(x) if x is not None else 0.0


def konto(adresse, anlegen=True):
    adresse = holder.norm(adresse)
    pg = _pg()
    if anlegen:
        pg.execute("INSERT INTO ai_accounts (address) VALUES (%s) ON CONFLICT DO NOTHING", (adresse,))
    r = pg.execute("SELECT credit, deposited, spent FROM ai_accounts WHERE address=%s",
                   (adresse,)).fetchone()
    if not r:
        return None
    return {"credit": _num(r[0]), "deposited": _num(r[1]), "spent": _num(r[2])}


def _reservieren(adresse, betrag):
    """Betrag abziehen, nur wenn genug da ist. True/False, atomar."""
    r = _pg().execute("""UPDATE ai_accounts SET credit = credit - %s
                         WHERE address=%s AND credit >= %s RETURNING credit""",
                      (Decimal(str(round(betrag, 8))), holder.norm(adresse),
                       Decimal(str(round(betrag, 8))))).fetchone()
    return r is not None


def _ausgleichen(adresse, reserviert, echt):
    """Reservierung auf den echten Betrag bringen. Darf minimal ins Minus."""
    diff = Decimal(str(round(reserviert - echt, 8)))
    _pg().execute("""UPDATE ai_accounts SET credit = credit + %s, spent = spent + %s
                     WHERE address=%s""",
                  (diff, Decimal(str(round(echt, 8))), holder.norm(adresse)))


# ── Schluessel ───────────────────────────────────────────────────────────

MAX_KEYS = 10


def _hash(k):
    return hashlib.sha256(k.encode()).hexdigest()


def key_neu(adresse, name=None):
    adresse = holder.norm(adresse)
    konto(adresse)
    pg = _pg()
    n = pg.execute("SELECT count(*) FROM ai_keys WHERE address=%s AND NOT revoked",
                   (adresse,)).fetchone()[0]
    if n >= MAX_KEYS:
        raise ValueError(f"you can have up to {MAX_KEYS} keys, revoke one first")
    roh = "oe_live_" + secrets.token_urlsafe(30).replace("-", "x").replace("_", "y")
    kid = "k_" + secrets.token_hex(6)
    name = (str(name or "").strip()[:40]) or None
    pg.execute("INSERT INTO ai_keys (id, address, hash, prefix, name) VALUES (%s,%s,%s,%s,%s)",
               (kid, adresse, _hash(roh), roh[:12], name))
    return {"id": kid, "key": roh, "prefix": roh[:12], "name": name}


def keys_liste(adresse):
    rows = _pg().execute("""SELECT id, prefix, name, created, last_used, spent, requests
                            FROM ai_keys WHERE address=%s AND NOT revoked ORDER BY created DESC""",
                         (holder.norm(adresse),)).fetchall()
    out = [{"id": r[0], "prefix": r[1], "name": r[2],
             "created": r[3].strftime("%b %d") if r[3] else "",
             "last_used": r[4].isoformat() if r[4] else None,
             "spent": round(_num(r[5]), 4), "requests": int(r[6] or 0),
             "used": ("${:.2f}" if _num(r[5]) >= 0.01 or not _num(r[5]) else "${:.4f}").format(_num(r[5]))}
            for r in rows]
    try:
        import agents
        for k in out:
            cap, ms = agents.grenzen(k["id"])
            k["day_cap"], k["models"] = cap, ms
    except Exception:
        pass
    return out


def key_weg(adresse, kid):
    _pg().execute("UPDATE ai_keys SET revoked=true WHERE id=%s AND address=%s",
                  (str(kid), holder.norm(adresse)))


def key_pruefen(roh):
    """(address, key_id) fuer einen gueltigen Schluessel, sonst None."""
    if not roh or not roh.startswith("oe_live_") or len(roh) > 120:
        return None
    r = _pg().execute("SELECT address, id FROM ai_keys WHERE hash=%s AND NOT revoked",
                      (_hash(roh),)).fetchone()
    return (r[0], r[1]) if r else None


# ── Modelle ──────────────────────────────────────────────────────────────

_MODELLE = {"ts": 0.0, "roh": {}, "liste": []}


def _tags(m):
    tags = []
    arch = m.get("architecture") or {}
    ein = arch.get("input_modalities") or []
    sp = m.get("supported_parameters") or []
    if "image" in ein:
        tags.append("vision")
    if "reasoning" in sp or m.get("reasoning"):
        tags.append("reasoning")
    if "tools" in sp:
        tags.append("tools")
    if (m.get("context_length") or 0) >= 400000:
        tags.append("long context")
    mid = m.get("id", "")
    if "code" in mid or "devstral" in mid or "grok-build" in mid:
        tags.append("code")
    if "sonar" in mid:
        tags.append("web search")
    if "image" in (arch.get("output_modalities") or []):
        tags.insert(0, "image")
    return tags


def _preis(m, art):
    try:
        v = float((m.get("pricing") or {}).get(art) or 0)
    except Exception:
        v = 0.0
    return v


def _zeile(m, rang):
    mid = m["id"]
    pin, pout = _preis(m, "prompt"), _preis(m, "completion")
    k = 1 + markup()
    anbieter = mid.split("/")[0].lstrip("~")
    name = m.get("name") or mid
    if ": " in name:
        name = name.split(": ", 1)[1]
    bench = ((m.get("benchmarks") or {}).get("artificial_analysis") or {}).get("intelligence_index")
    dyn = pin < 0 or pout < 0
    gratis = (mid.endswith(":free") or mid == "openrouter/free") or (
        not dyn and pin == 0 and pout == 0 and _preis(m, "image_output") == 0)
    if dyn:
        pin = pout = 0.0
    z = {
        "id": mid, "name": name, "provider": ANBIETER.get(anbieter, anbieter.title()),
        "provider_id": anbieter, "context": m.get("context_length"),
        "max_output": (m.get("top_provider") or {}).get("max_completion_tokens"),
        "in": round(pin * 1e6 * k, 4), "out": round(pout * 1e6 * k, 4),
        "in_base": round(pin * 1e6, 4), "out_base": round(pout * 1e6, 4),
        "tags": _tags(m), "created": m.get("created"), "rank": rang,
        "intelligence": bench,
        "description": (m.get("description") or "")[:240],
        "per_msg": round((pin * 1000 + pout * 500) * k, 6),
        "tier": ("cheap" if (pin * 1000 + pout * 500) * k < 0.002 else
                 "standard" if (pin * 1000 + pout * 500) * k < 0.01 else "premium"),
        "kind": "image" if "image" in ((m.get("architecture") or {}).get("output_modalities") or []) else "text",
        "image_out_base": round(_preis(m, "image_output") * 1e6, 4) or None,
        "per_image": (round(_preis(m, "image_output") * BILD_TOKENS.get(anbieter, 2500) * k, 4)
                      if _preis(m, "image_output") else None),
    }
    if dyn:
        z.update({"in": None, "out": None, "in_base": None, "out_base": None, "per_msg": None, "tier": "auto"})
    if gratis:
        z.update({"in": 0.0, "out": 0.0, "in_base": 0.0, "out_base": 0.0, "per_msg": 0.0, "tier": "free",
                  "free": True})
        z["tags"].insert(0, "free")
    if mid.endswith(":batch"):
        z["tags"].append("batch")
    return z


def modelle(frisch=False):
    """Freigegebene Modelle mit unseren Preisen (je 1 Mio. Tokens, inkl. Aufschlag)."""
    if not frisch and _MODELLE["liste"] and time.time() - _MODELLE["ts"] < int(os.getenv("AI_MODELS_REFRESH_S", "600") or 600):
        return _MODELLE["liste"]
    try:
        r = requests.get(OR_BASE + "/models", timeout=15)
        r.raise_for_status()
        roh = {m["id"]: m for m in r.json().get("data", [])}
    except Exception as e:
        log.warning(f"ai: model list failed: {e}")
        if _MODELLE["liste"]:
            return _MODELLE["liste"]
        try:
            gesp = json.loads(storage.kv_get("ai_models") or "[]")
            if gesp:
                _MODELLE["liste"] = gesp
                return gesp
        except Exception:
            pass
        return []
    # Neue Top-Modelle landen von selbst unter "Popular" (seit 2.44.3): grosse Anbieter, die letzten AI_MODELS_NEW_DAYS
    # Tage, keine datierten Snapshots, Experimente oder Spezialvarianten. Platz direkt nach den ersten sechs.
    tage = int(os.getenv("AI_MODELS_NEW_DAYS", "21") or 21)
    neu_top = sorted([i for i, m in roh.items()
                      if i.startswith(TOP_ANBIETER) and i not in KURATIERT and ":" not in i
                      and (m.get("created") or 0) >= time.time() - tage * 86400
                      and not re.search(r"-(\d{4}|\d{8})$|exp|preview|image|vision|audio|tts|embed|guard|-free", i)
                      and ((m.get("architecture") or {}).get("output_modalities") or ["text"]) == ["text"]],
                     key=lambda i: -(roh[i].get("created") or 0))
    beliebt = KURATIERT[:6] + neu_top + KURATIERT[6:]
    wahl = os.getenv("AI_MODELS", "all").strip()
    if wahl.lower() == "curated":
        wahl = ""
    if wahl.lower() == "all":
        ids = sorted([i for i, m in roh.items()
                      if i != AUTO_OR
                      and "audio" not in ((m.get("architecture") or {}).get("output_modalities") or [])],
                     key=lambda i: (beliebt.index(i) if i in beliebt else 999,
                                    -(roh[i].get("created") or 0)))
    elif wahl:
        ids = [x.strip() for x in wahl.split(",") if x.strip() in roh]
    else:
        ids = [i for i in KURATIERT if i in roh]
    ids += [i for i in BILD_MODELLE if i in roh and i not in ids]
    liste = [_zeile(roh[i], n) for n, i in enumerate(ids)]
    frisch_ab = time.time() - 21 * 86400
    for z in liste:
        z["picked"] = z["id"] in KURATIERT or z["id"] in BILD_MODELLE or z["id"] in neu_top
        z["new"] = bool(z.get("created") and z["created"] >= frisch_ab)
        if z["id"] in SCHREIBEN and "writing" not in z["tags"]:
            z["tags"].append("writing")
        if z["id"] in CODE and "code" not in z["tags"]:
            z["tags"].append("code")
        if not z["picked"]:
            z["description"] = z["description"][:140]
    auto = {"id": AUTO_ID, "name": "OddsEngine auto", "provider": "OddsEngine",
            "provider_id": "oddsengine", "context": 2000000, "max_output": None,
            "in": None, "out": None, "in_base": None, "out_base": None,
            "tags": ["routes", "auto"], "created": None, "rank": -1, "intelligence": None,
            "per_msg": None, "tier": "auto", "kind": "text", "per_image": None, "image_out_base": None,
            "picked": True, "new": False,
            "description": "Picks a strong model for each prompt. You pay what that model "
                           "costs plus the OddsEngine fee."}
    liste.insert(0, auto)
    _MODELLE.update(ts=time.time(), roh=roh, liste=liste)
    try:
        storage.kv_set("ai_models", json.dumps(liste))
    except Exception:
        pass
    return liste


def modell(mid):
    for m in modelle():
        if m["id"] == mid:
            return m
    return None


# ── Anfragen ─────────────────────────────────────────────────────────────

class Fehler(Exception):
    def __init__(self, status, text, art="invalid_request_error"):
        super().__init__(text)
        self.status, self.text, self.art = status, text, art


def _schaetzung_tokens(body):
    """Eingabe-Tokens grob schaetzen. Bilder zaehlen pauschal, nicht nach Bytes."""
    try:
        n, bilder = 0, 0
        for msg in body.get("messages") or []:
            c = msg.get("content")
            if isinstance(c, list):
                for teil in c:
                    if (teil or {}).get("type") == "image_url":
                        bilder += 1
                    else:
                        n += len(json.dumps(teil))
            else:
                n += len(json.dumps(c))
        if body.get("prompt"):
            n += len(str(body.get("prompt")))
        return int(n / 3.2) + 1600 * bilder + 50
    except Exception:
        return 2000


MAX_OUT_STANDARD = 8192
AUTO_PREIS = (15.0, 75.0)   # $/1M ein/aus, zum Reservieren bei "auto"


def _mit_web(body):
    try:
        if str(body.get("model") or "").endswith(":online"):
            return True
        return any((p or {}).get("id") == "web" for p in (body.get("plugins") or []))
    except Exception:
        return False


def vorbereiten(adresse, body):
    """Body pruefen, Modell abbilden, Hoechstbetrag reservieren.
    Gibt (body_fuer_openrouter, reserviert, modell_zeile) zurueck."""
    if not bereit():
        raise Fehler(503, "OddsEngine AI is not configured yet", "service_unavailable")
    if not isinstance(body, dict):
        raise Fehler(400, "body must be a JSON object")
    mid = str(body.get("model") or AUTO_ID).strip()
    m = modell(mid)
    if not m:
        raise Fehler(404, f"model '{mid}' is not available on OddsEngine AI. "
                          "See GET /v1/models for the list.", "model_not_found")
    if not body.get("messages") and not body.get("prompt"):
        raise Fehler(400, "messages is required")
    if m.get("free"):
        return _gratis(adresse, body, m, mid)
    k = 1 + markup(adresse)
    pin = (m["in_base"] if m["in_base"] is not None else AUTO_PREIS[0]) / 1e6
    pout = (m["out_base"] if m["out_base"] is not None else AUTO_PREIS[1]) / 1e6
    kt = konto(adresse)
    guthaben = kt["credit"] if kt else 0.0
    mindest = _f("AI_MIN_CREDIT", 0.01)
    if guthaben < mindest:
        raise Fehler(402, "your OddsEngine AI balance is empty. Top up with $ODDS on "
                          "engine.oddsengine.live", "insufficient_quota")
    ein = _schaetzung_tokens(body)
    kosten_ein = ein * pin * k
    obergrenze = m.get("max_output") or 32768
    gewuenscht = body.get("max_tokens") or body.get("max_completion_tokens")
    leistbar = int(max(0.0, (guthaben - kosten_ein)) / (pout * k)) if pout > 0 else 10 ** 7
    if gewuenscht:
        try:
            gewuenscht = int(gewuenscht)
        except Exception:
            raise Fehler(400, "max_tokens must be a number")
        aus = min(gewuenscht, obergrenze)
        if aus > leistbar:
            raise Fehler(402, "not enough balance for max_tokens={}. You can afford about {} "
                              "output tokens with this model.".format(aus, max(0, leistbar)),
                         "insufficient_quota")
    else:
        aus = min(MAX_OUT_STANDARD, obergrenze, leistbar)
        if aus < 64:
            raise Fehler(402, "not enough balance for this model. Top up with $ODDS or pick "
                              "a cheaper model.", "insufficient_quota")
    reserve = kosten_ein + aus * pout * k
    if _mit_web(body):
        # Websuche: Suchgebuehr plus die Treffer als zusaetzliche Eingabe
        reserve += (_f("AI_WEB_RESERVE_USD", 0.03) + 20000 * pin) * k
    if m.get("kind") == "image" or "image" in (body.get("modalities") or []):
        bild = (m.get("image_out_base") or 60.0) / 1e6
        reserve += bild * 8000 * k
    if reserve > guthaben:
        raise Fehler(402, "not enough balance for this request, it can cost up to ${:.3f}. "
                          "Top up with $ODDS or pick a cheaper model.".format(reserve),
                     "insufficient_quota")
    if not _reservieren(adresse, reserve):
        raise Fehler(402, "not enough balance, other requests are still running",
                     "insufficient_quota")
    neu = dict(body)
    neu["model"] = AUTO_OR if mid == AUTO_ID else mid
    if "max_completion_tokens" in neu and "max_tokens" not in neu:
        neu["max_tokens"] = neu.pop("max_completion_tokens")
    neu["max_tokens"] = aus
    neu["usage"] = {"include": True}
    neu["user"] = hashlib.sha256(("oe:" + holder.norm(adresse)).encode()).hexdigest()[:24]
    for weg in ("api_key", "transforms_unsafe"):
        neu.pop(weg, None)
    return neu, reserve, m


def _gratis(adresse, body, m, mid):
    """Kostenlose Modelle: kein Guthaben noetig, aber Tageslimit je Wallet,
    weil OpenRouter die Gratis-Anfragen fuer das ganze Konto begrenzt."""
    tag = time.strftime("%Y%m%d", time.gmtime())
    k = "aifree_{}_{}".format(holder.norm(adresse), tag)
    try:
        n = int(storage.kv_get(k) or 0)
    except Exception:
        n = 0
    grenze = int(_f("AI_FREE_PER_DAY", 20))
    if n >= grenze:
        raise Fehler(429, "you used your {} free messages for today. Free models reset every day, "
                          "or top up to use any model.".format(grenze), "free_limit")
    storage.kv_set(k, str(n + 1))
    neu = dict(body)
    neu["model"] = mid
    neu["max_tokens"] = min(int(body.get("max_tokens") or MAX_OUT_STANDARD), MAX_OUT_STANDARD)
    neu.pop("max_completion_tokens", None)
    neu["usage"] = {"include": True}
    neu["user"] = hashlib.sha256(("oe:" + holder.norm(adresse)).encode()).hexdigest()[:24]
    return neu, 0.0, m


def _or_kopf():
    return {"Authorization": "Bearer " + or_key(), "Content-Type": "application/json",
            "HTTP-Referer": "https://engine.oddsengine.live", "X-Title": "OddsEngine AI"}


def _kosten(usage, m):
    """Echte Kosten bei OpenRouter in Dollar."""
    if not usage:
        return None
    c = usage.get("cost")
    if c is not None:
        try:
            return float(c)
        except Exception:
            pass
    ti, to = usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0
    if m.get("in_base") is None:
        return None
    return ti * m["in_base"] / 1e6 + to * m["out_base"] / 1e6


def _schaetzen(m, ein_tokens, aus_zeichen):
    """Kosten ohne Zahlen von OpenRouter (Abbruch mitten im Stream): grob ueber
    die gestreamten Zeichen. Verhindert Gratis-Nutzung durch Abbrechen."""
    pin = (m["in_base"] if m["in_base"] is not None else AUTO_PREIS[0]) / 1e6
    pout = (m["out_base"] if m["out_base"] is not None else AUTO_PREIS[1]) / 1e6
    return ein_tokens * pin + (aus_zeichen / 3.5) * pout


def abrechnen(adresse, key_id, m, reserve, usage, gen_id, status, modell_echt=None,
              schaetzung=None):
    """Reservierung ausgleichen und die Anfrage protokollieren. Gibt Betrag zurueck."""
    cost = _kosten(usage, m)
    if cost is None:
        cost = schaetzung or 0.0
    k = 1 + markup(adresse)
    if m.get("free"):
        cost = 0.0
    # Seit 2.32.3: immer die echten Kosten plus Gebuehr. Vorher war hier eine Obergrenze
    # (1,5 x Reservierung), die bei Websuche und langen Antworten zu wenig berechnet hat.
    # Nur eine reine Schaetzung (Abbruch ohne Zahlen von OpenRouter) bleibt gedeckelt.
    if _kosten(usage, m) is None and reserve > 0:
        cost = min(cost, reserve / k * 1.5)
    berechnet = cost * k
    try:
        _ausgleichen(adresse, reserve, berechnet)
        pg = _pg()
        pg.execute("""INSERT INTO ai_usage (address, key_id, model, tokens_in, tokens_out,
                      cost_or, charged, gen_id, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                   (holder.norm(adresse), key_id, modell_echt or m["id"],
                    int((usage or {}).get("prompt_tokens") or 0),
                    int((usage or {}).get("completion_tokens") or 0),
                    Decimal(str(round(cost, 8))), Decimal(str(round(berechnet, 8))),
                    gen_id, status))
        if key_id:
            pg.execute("""UPDATE ai_keys SET spent = spent + %s, requests = requests + 1,
                          last_used = now() WHERE id=%s""",
                       (Decimal(str(round(berechnet, 8))), key_id))
    except Exception as e:
        log.warning(f"ai: abrechnen {adresse} {e}")
    return berechnet


def anfrage(adresse, key_id, body):
    """Ohne Streaming. Gibt (status, json) zurueck."""
    neu, reserve, m = vorbereiten(adresse, body)
    neu["stream"] = False
    try:
        r = requests.post(OR_BASE + "/chat/completions", headers=_or_kopf(),
                          data=json.dumps(neu), timeout=600)
        d = r.json()
    except Exception as e:
        abrechnen(adresse, key_id, m, reserve, None, None, "error")
        raise Fehler(502, f"upstream error: {str(e)[:120]}", "upstream_error")
    if r.status_code >= 400 or "error" in d:
        abrechnen(adresse, key_id, m, reserve, d.get("usage"), d.get("id"), "error")
        err = d.get("error") or {}
        raise Fehler(r.status_code if r.status_code >= 400 else 502,
                     str(err.get("message") or "upstream error")[:300], "upstream_error")
    try:
        zeichen = len(json.dumps(d.get("choices") or ""))
    except Exception:
        zeichen = 0
    betrag = abrechnen(adresse, key_id, m, reserve, d.get("usage"), d.get("id"), "ok",
                       d.get("model"), _schaetzen(m, _schaetzung_tokens(body), zeichen))
    if body.get("model") in (None, "", AUTO_ID):
        d["routed_model"] = d.get("model")
        d["model"] = AUTO_ID
    d.setdefault("usage", {})["oddsengine_charged_usd"] = round(betrag, 8)
    d["usage"].pop("cost", None)
    d["usage"].pop("cost_details", None)
    return d


def strom(adresse, key_id, body):
    """Mit Streaming: Generator fuer SSE-Zeilen (bytes). Abrechnung am Ende."""
    neu, reserve, m = vorbereiten(adresse, body)
    neu["stream"] = True

    ein = _schaetzung_tokens(body)

    def gen():
        usage, gid, echt, status, zeichen = None, None, None, "error", 0
        try:
            with requests.post(OR_BASE + "/chat/completions", headers=_or_kopf(),
                               data=json.dumps(neu), timeout=600, stream=True) as r:
                if r.status_code >= 400:
                    try:
                        msg = (r.json().get("error") or {}).get("message")
                    except Exception:
                        msg = None
                    fehler = {"error": {"message": str(msg or f"upstream error {r.status_code}")[:300],
                                        "type": "upstream_error", "code": r.status_code}}
                    yield ("data: " + json.dumps(fehler) + "\n\n").encode()
                    yield b"data: [DONE]\n\n"
                    return
                for zeile in r.iter_lines(decode_unicode=True):
                    if zeile is None:
                        continue
                    if not zeile.startswith("data: "):
                        continue      # Kommentare (": OPENROUTER PROCESSING") weglassen
                    nutz = zeile[6:]
                    if nutz.strip() == "[DONE]":
                        status = "ok"
                        break
                    try:
                        j = json.loads(nutz)
                    except Exception:
                        continue
                    gid = gid or j.get("id")
                    for c in j.get("choices") or []:
                        dl = c.get("delta") or {}
                        zeichen += len(str(dl.get("content") or "")) + len(str(dl.get("reasoning") or ""))
                    echt = echt or j.get("model")
                    if j.get("usage"):
                        usage = j["usage"]
                        betrag_vorab = 0.0 if m.get("free") else (_kosten(usage, m) or 0) * (1 + markup(adresse))
                        j["usage"] = {k: v for k, v in usage.items()
                                      if k not in ("cost", "cost_details")}
                        j["usage"]["oddsengine_charged_usd"] = round(betrag_vorab, 8)
                    if body.get("model") in (None, "", AUTO_ID):
                        j["model"] = AUTO_ID
                    yield ("data: " + json.dumps(j) + "\n\n").encode()
                status = "ok"
                yield b"data: [DONE]\n\n"
        except GeneratorExit:
            status = "cancelled"
            raise
        except Exception as e:
            log.warning(f"ai: stream {e}")
            yield ("data: " + json.dumps({"error": {"message": "stream interrupted",
                                                    "type": "upstream_error"}}) + "\n\n").encode()
        finally:
            abrechnen(adresse, key_id, m, reserve, usage, gid, status, echt,
                      _schaetzen(m, ein, zeichen) if zeichen else 0.0)
    return gen()


def verlauf(adresse, n=20):
    rows = _pg().execute("""SELECT ts, model, tokens_in, tokens_out, charged, status
                            FROM ai_usage WHERE address=%s ORDER BY ts DESC LIMIT %s""",
                         (holder.norm(adresse), int(n))).fetchall()
    return [{"ts": r[0].isoformat(), "model": r[1], "in": r[2], "out": r[3],
             "usd": round(_num(r[4]), 6), "status": r[5]} for r in rows]


# ── Einzahlungen ─────────────────────────────────────────────────────────

_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
BESTAETIGUNGEN = 3
_SCAN = {"ts": 0.0, "laeuft": False}


def _besitzer(sender):
    """Wem gehoert diese Absender-Wallet? Bestaetigte Adresse oder verknuepft."""
    sender = holder.norm(sender)
    try:
        r = _pg().execute("SELECT 1 FROM ai_accounts WHERE address=%s", (sender,)).fetchone()
        if r:
            return sender
    except Exception:
        pass
    try:
        haupt = holder._kv("linkedto_" + sender)
        if haupt:
            return holder.norm(haupt)
    except Exception:
        pass
    return None


def _gutschreiben(tx, li, an):
    pg = _pg()
    r = pg.execute("SELECT usd, odds FROM ai_deposits WHERE tx=%s AND li=%s AND credited_to IS NULL",
                   (tx, li)).fetchone()
    if not r:
        return 0.0
    usd = r[0]
    if usd is None:
        kurs, _ = holder.kurs()
        if not kurs:
            return 0.0
        usd = Decimal(str(round(float(r[1]) * kurs, 8)))
        pg.execute("UPDATE ai_deposits SET usd=%s, price=%s WHERE tx=%s AND li=%s",
                   (usd, Decimal(str(kurs)), tx, li))
    z = pg.execute("""UPDATE ai_deposits SET credited_to=%s, credited_ts=now()
                      WHERE tx=%s AND li=%s AND credited_to IS NULL RETURNING usd""",
                   (an, tx, li)).fetchone()
    if not z:
        return 0.0
    konto(an)
    pg.execute("""UPDATE ai_accounts SET credit = credit + %s, deposited = deposited + %s
                  WHERE address=%s""", (z[0], z[0], an))
    log.info(f"ai: deposit {tx}:{li} credited {float(z[0]):.4f} USD to {an}")
    return float(z[0])


def scannen(erzwingen=False):
    """Transfer-Logs von $ODDS an die Empfangs-Wallet lesen und gutschreiben."""
    if not holder.mint() or not holder.evm():
        return {"ok": False, "why": "token not live"}
    if _SCAN["laeuft"] or (not erzwingen and time.time() - _SCAN["ts"] < 30):
        return {"ok": True, "skipped": True}
    _SCAN["laeuft"] = True
    neu = 0
    try:
        pg = _pg()
        jetzt = int(holder._evm_rpc("eth_blockNumber", []), 16) - BESTAETIGUNGEN
        roh = storage.kv_get("ai_scan_block")
        start = int(roh) + 1 if roh else max(0, jetzt - int(_f("AI_SCAN_BACK", 200000)))
        an = "0x" + eingang()[2:].rjust(64, "0")
        schritt = 20000
        dez = holder._dezimalen()
        while start <= jetzt:
            ende = min(jetzt, start + schritt)
            try:
                logs = holder._evm_rpc("eth_getLogs", [{
                    "address": holder.mint(), "fromBlock": hex(start), "toBlock": hex(ende),
                    "topics": [_TRANSFER, None, an]}])
            except RuntimeError:
                if schritt > 500:
                    schritt //= 4
                    continue
                raise
            for lg in logs or []:
                try:
                    sender = "0x" + lg["topics"][1][-40:]
                    try:
                        import agents as _ag
                        if _ag.gas_adresse() and holder.norm(sender) == holder.norm(_ag.gas_adresse()):
                            continue
                    except Exception:
                        pass
                    menge = int(lg["data"], 16) / (10 ** dez)
                    tx, li = lg["transactionHash"], int(lg["logIndex"], 16)
                    kurs, _ = holder.kurs()
                    wer0 = _besitzer(sender)
                    fest = _angebot_einloesen(wer0, menge) if wer0 else None
                    if fest:
                        kurs = fest
                    usd = Decimal(str(round(menge * kurs, 8))) if kurs else None
                    r = pg.execute("""INSERT INTO ai_deposits (tx, li, block, sender, odds, usd, price)
                                      VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                                      RETURNING tx""",
                                   (tx, li, int(lg["blockNumber"], 16), holder.norm(sender),
                                    Decimal(str(menge)), usd,
                                    Decimal(str(kurs)) if kurs else None)).fetchone()
                    if r:
                        neu += 1
                        wer = _besitzer(sender)
                        if wer:
                            _gutschreiben(tx, li, wer)
                except Exception as e:
                    log.warning(f"ai: deposit log {e}")
            storage.kv_set("ai_scan_block", str(ende))
            start = ende + 1
        return {"ok": True, "new": neu, "block": jetzt}
    except Exception as e:
        log.warning(f"ai: scan {e}")
        return {"ok": False, "why": str(e)[:160]}
    finally:
        _SCAN.update(laeuft=False, ts=time.time())


QUOTE_MIN = 10


def angebot(adresse, usd):
    """Aufladen mit festem Betrag: Kurs fuer QUOTE_MIN Minuten festhalten."""
    try:
        usd = float(usd)
    except Exception:
        raise Fehler(400, "amount must be a number")
    if not (1 <= usd <= 5000):
        raise Fehler(400, "pick an amount between $1 and $5,000")
    kurs, quelle = holder.kurs()
    if not kurs:
        raise Fehler(503, "the $ODDS price is not available right now, try again in a minute")
    dez = holder._dezimalen()
    menge = math.ceil(usd / kurs * 100) / 100
    q = {"usd": round(usd, 2), "odds": menge, "price": kurs, "exp": int(time.time()) + QUOTE_MIN * 60}
    storage.kv_set("aiq_" + holder.norm(adresse), json.dumps(q))
    roh = int(Decimal(str(menge)) * (Decimal(10) ** dez))
    daten = "0xa9059cbb" + eingang()[2:].lower().rjust(64, "0") + hex(roh)[2:].rjust(64, "0")
    k = holder.ketten_info()
    return dict(q, to=eingang(), token=holder.mint(), raw=str(roh), data=daten,
                chain_id=k.get("chain_id"), chain_id_hex=k.get("chain_id_hex"),
                rpc=k.get("rpc"), explorer=k.get("explorer"), minutes=QUOTE_MIN)


def _angebot_einloesen(adresse, menge):
    """Passt die Einzahlung zum offenen Angebot, gilt dessen Kurs. Dann verbraucht."""
    try:
        q = json.loads(storage.kv_get("aiq_" + holder.norm(adresse)) or "null")
    except Exception:
        q = None
    if not q:
        return None
    if time.time() > q["exp"] + 600:          # 10 Minuten Luft fuer die Erkennung
        return None
    if not (q["odds"] * 0.97 <= menge <= q["odds"] * 1.5):
        return None
    storage.kv_set("aiq_" + holder.norm(adresse), "null")
    return float(q["price"])


def offene_zuordnen(adresse):
    """Einzahlungen von dieser Adresse oder ihren verknuepften Wallets, die noch
    niemandem gutgeschrieben sind (z.B. vor dem ersten Login gesendet)."""
    adresse = holder.norm(adresse)
    wallets = [adresse] + list(holder.verknuepft(adresse))
    rows = _pg().execute("""SELECT tx, li FROM ai_deposits WHERE credited_to IS NULL
                            AND sender = ANY(%s)""", (wallets,)).fetchall()
    summe = 0.0
    for tx, li in rows:
        summe += _gutschreiben(tx, li, adresse)
    return summe


def einzahlungen(adresse, n=10):
    rows = _pg().execute("""SELECT tx, sender, odds, usd, ts FROM ai_deposits
                            WHERE credited_to=%s ORDER BY ts DESC LIMIT %s""",
                         (holder.norm(adresse), int(n))).fetchall()
    return [{"tx": r[0], "from": r[1], "odds": _num(r[2]), "usd": round(_num(r[3]), 4),
             "ts": r[4].isoformat() if r[4] else None} for r in rows]


def waechter():
    """Hintergrund: alle 45 s nach neuen Einzahlungen schauen."""
    time.sleep(20)
    while True:
        try:
            if storage._init_pg() and holder.mint():
                scannen()
            if storage._init_pg():
                try:
                    import usdc
                    usdc.tick()
                except Exception as e:
                    log.warning(f"ai: usdc tick {e}")
                try:
                    import motor
                    motor.tick()
                except Exception as e:
                    log.warning(f"ai: motor tick {e}")
            if bereit() and time.time() - _SCAN.get("warn", 0) > 600:
                _SCAN["warn"] = time.time()
                warnen()
            if storage._init_pg() and time.time() - _SCAN.get("aufr", 0) > 3600:
                _SCAN["aufr"] = time.time()
                import chat
                chat.aufraeumen()
        except Exception as e:
            log.debug(f"ai waechter {e}")
        time.sleep(45)


# ── Warnung an den Betreiber ────────────────────────────────────────────

def _an_betreiber(text):
    tok = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = (os.getenv("AI_ALERT_CHAT_ID", "") or os.getenv("TELEGRAM_PRIVATE_CHAT_ID", "")).strip()
    if not tok or not chat:
        log.warning("ai: no telegram chat for alerts: " + text[:80])
        return False
    try:
        r = requests.post("https://api.telegram.org/bot{}/sendMessage".format(tok),
                          json={"chat_id": chat, "text": text, "disable_web_page_preview": True}, timeout=15)
        return r.status_code == 200
    except Exception as e:
        log.warning(f"ai: alert {e}")
        return False


def or_stand():
    """Was bei OpenRouter noch da ist: Key-Limit und, falls lesbar, Kontoguthaben."""
    out = {}
    try:
        d = (requests.get(OR_BASE + "/key", headers=_or_kopf(), timeout=10).json() or {}).get("data") or {}
        if d.get("limit"):
            out["key_limit"] = float(d["limit"])
            out["key_left"] = float(d.get("limit_remaining") if d.get("limit_remaining") is not None
                                    else d["limit"] - float(d.get("usage") or 0))
    except Exception:
        pass
    try:
        r = requests.get(OR_BASE + "/credits", headers=_or_kopf(), timeout=10)
        d = (r.json() or {}).get("data") or {} if r.status_code == 200 else {}
        if d.get("total_credits") is not None:
            out["credits"] = float(d["total_credits"])
            out["credits_left"] = float(d["total_credits"]) - float(d.get("total_usage") or 0)
    except Exception:
        pass
    return out


def warnen():
    """Alle 10 Minuten: Ist bei OpenRouter 70 % (AI_ALERT_AT) oder 90 % verbraucht, oder
    halten Kunden mehr Guthaben als dort noch liegt? Dann eine Nachricht an den Betreiber,
    je Stufe nur einmal, bis wieder aufgeladen wurde."""
    st = or_stand()
    try:
        offen = float(_pg().execute("SELECT coalesce(sum(credit),0) FROM ai_accounts").fetchone()[0])
    except Exception:
        offen = 0.0
    grenze = _f("AI_ALERT_AT", 0.7)
    teile = []
    for art, gesamt, rest, name in (("key", st.get("key_limit"), st.get("key_left"), "Key-Limit"),
                                    ("cr", st.get("credits"), st.get("credits_left"), "OpenRouter-Guthaben")):
        if not gesamt:
            continue
        verbraucht = 1 - max(0.0, rest) / gesamt
        stufe = 2 if verbraucht >= 0.9 else 1 if verbraucht >= grenze else 0
        k = "ai_warn_" + art
        alt = int(storage.kv_get(k) or 0)
        if stufe > alt:
            teile.append("{}: {:.0f} % verbraucht, noch ${:.2f} von ${:.2f}.".format(
                name, verbraucht * 100, max(0.0, rest), gesamt))
        if stufe != alt:
            storage.kv_set(k, str(stufe))
    rest_min = min([x for x in (st.get("key_left"), st.get("credits_left")) if x is not None] or [None]) \
        if (st.get("key_left") is not None or st.get("credits_left") is not None) else None
    k = "ai_warn_offen"
    alt = int(storage.kv_get(k) or 0)
    jetzt = 1 if (rest_min is not None and offen > rest_min) else 0
    if jetzt > alt:
        teile.append("Kunden halten ${:.2f} Guthaben, bei OpenRouter sind nur noch ${:.2f} frei.".format(offen, rest_min))
    if jetzt != alt:
        storage.kv_set(k, str(jetzt))
    try:
        import agents
        import features
        if features.an("x402") and agents.gas_adresse():
            eth = agents.gas_eth()
            tief = 1 if (eth is not None and eth < agents._f("X402_GAS_MIN_ETH", 0.0005)) else 0
            alt = int(storage.kv_get("ai_warn_gas") or 0)
            if tief > alt:
                _an_betreiber("OddsEngine x402: Die Gas-Wallet {} hat nur noch {:.5f} ETH. Bitte ETH "
                              "auf Robinhood Chain nachschicken, sonst koennen Agenten nicht mehr "
                              "bezahlen.".format(agents.gas_adresse(), eth))
            if tief != alt:
                storage.kv_set("ai_warn_gas", str(tief))
    except Exception as e:
        log.warning(f"ai: gas check {e}")
    try:
        import usdc
        usdc.warnen()
    except Exception as e:
        log.warning(f"ai: base gas check {e}")
    if teile:
        _an_betreiber("OddsEngine AI, bitte aufladen\n\n" + "\n".join(teile) +
                      "\n\nKunden-Guthaben gesamt: ${:.2f}\nOpenRouter: openrouter.ai/settings/credits".format(offen))
    return {"stand": st, "offen": offen, "gesendet": bool(teile)}


# ── Admin ────────────────────────────────────────────────────────────────

def _lows():
    try:
        import tief
        return tief.stand()
    except Exception as e:
        log.warning(f"ai: lows {e}")
        return None


def _paper():
    try:
        import paper
        return paper.stand()
    except Exception as e:
        log.warning(f"ai: paper {e}")
        return None


def _x402_lage():
    try:
        import agents
        agents._tab()
        r = _pg().execute("""SELECT count(*), coalesce(sum(usd),0) FROM ai_x402
                             WHERE ts > date_trunc('day', now())""").fetchone()
        return {"gas_wallet": agents.gas_adresse(), "gas_eth": agents.gas_eth(),
                "payments_today": int(r[0]), "usd_today": round(float(r[1]), 4)}
    except Exception:
        return None


def _usdc_lage():
    try:
        import usdc
        return usdc.lage()
    except Exception:
        return None


def admin_lage():
    pg = _pg()
    def eins(sql, *a):
        r = pg.execute(sql, a).fetchone()
        return r[0] if r else None
    out = {
        "configured": bereit(), "deposit_address": eingang(), "markup": markup(),
        "accounts": eins("SELECT count(*) FROM ai_accounts"),
        "keys": eins("SELECT count(*) FROM ai_keys WHERE NOT revoked"),
        "x402": _x402_lage(),
        "x402_usdc": _usdc_lage(),
        "paper": _paper(),
        "lows": _lows(),
        "credit_open_usd": round(_num(eins("SELECT sum(credit) FROM ai_accounts")), 4),
        "deposited_usd": round(_num(eins("SELECT sum(usd) FROM ai_deposits WHERE credited_to IS NOT NULL")), 4),
        "deposited_odds": round(_num(eins("SELECT sum(odds) FROM ai_deposits")), 2),
        "unmatched": [{"tx": r[0], "from": r[1], "odds": _num(r[2])} for r in pg.execute(
            "SELECT tx, sender, odds FROM ai_deposits WHERE credited_to IS NULL ORDER BY ts DESC LIMIT 20").fetchall()],
        "requests_24h": eins("SELECT count(*) FROM ai_usage WHERE ts > now() - interval '24 hours'"),
        "cost_or_usd": round(_num(eins("SELECT sum(cost_or) FROM ai_usage")), 4),
        "charged_usd": round(_num(eins("SELECT sum(charged) FROM ai_usage")), 4),
        "top_models": [{"model": r[0], "n": r[1], "usd": round(_num(r[2]), 4)} for r in pg.execute(
            """SELECT model, count(*), sum(charged) FROM ai_usage WHERE ts > now() - interval '7 days'
               GROUP BY model ORDER BY 2 DESC LIMIT 8""").fetchall()],
        "scan_block": storage.kv_get("ai_scan_block"),
        "top_users": [{"address": r[0], "credit": round(_num(r[1]), 4), "deposited": round(_num(r[2]), 4),
                       "spent": round(_num(r[3]), 4), "requests": int(r[4] or 0),
                       "last": r[5].isoformat() if r[5] else None,
                       "since": r[6].isoformat() if r[6] else None,
                       "margin": round(_num(r[7]), 4)} for r in pg.execute(
            """SELECT a.address, a.credit, a.deposited, a.spent, u.n, u.last, a.created, u.marge
               FROM ai_accounts a LEFT JOIN (
                 SELECT address, count(*) AS n, max(ts) AS last, sum(charged) - sum(cost_or) AS marge
                 FROM ai_usage GROUP BY address) u ON u.address = a.address
               ORDER BY a.spent DESC, a.deposited DESC, u.n DESC NULLS LAST LIMIT 30""").fetchall()],
        "users_7d": eins("SELECT count(DISTINCT address) FROM ai_usage WHERE ts > now() - interval '7 days'"),
        "free_24h": eins("""SELECT count(*) FROM ai_usage WHERE ts > now() - interval '24 hours'
                            AND charged = 0 AND status = 'ok'"""),
    }
    out["margin_usd"] = round(out["charged_usd"] - out["cost_or_usd"], 4)
    try:
        r = requests.get(OR_BASE + "/credits", headers=_or_kopf(), timeout=10)
        d = (r.json() or {}).get("data") or {} if r.status_code == 200 else {}
        out["openrouter_left_usd"] = (round(_num(d.get("total_credits")) - _num(d.get("total_usage")), 4)
                                      if d.get("total_credits") is not None else None)
    except Exception:
        out["openrouter_left_usd"] = None
    try:
        r = requests.get(OR_BASE + "/key", headers=_or_kopf(), timeout=10)
        d = (r.json() or {}).get("data") or {}
        out["key_limit_left_usd"] = d.get("limit_remaining")
    except Exception:
        out["key_limit_left_usd"] = None
    return out
