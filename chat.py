"""
chat.py: der Chat auf engine.oddsengine.live. Seit 2.29.1.

Wie ChatGPT, nur mit $ODDS bezahlt. Jeder Chat gehoert genau einer Wallet.

Speicher (Postgres, legt sich selbst an):
    ai_chats   id, Adresse, Titel (verschluesselt), Modell, Zeiten
    ai_msgs    Nachrichten, Inhalt verschluesselt, Bild-IDs, Modell, Kosten
    ai_images  hochgeladene und erzeugte Bilder (verschluesselt), nach
               AI_IMAGE_DAYS Tagen (Standard 30) automatisch geloescht

Verschluesselung: AES-GCM, Schluessel aus AUTH_SECRET abgeleitet. Wer nur die
Datenbank sieht, liest nichts. Ohne AUTH_SECRET startet der Chat nicht.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import time
from decimal import Decimal

import ai
import holder
from logger import log

MAX_CHATS = int(os.getenv("AI_MAX_CHATS", "200") or 200)
MAX_BILDER = 4
MAX_BILD_BYTES = 4 * 1024 * 1024
VERLAUF = 30            # Nachrichten, die als Kontext mitgehen
BILD_KONTEXT = 2        # die letzten so vielen Nachrichten mit Bildern gehen mit Bild mit

_SCHEMA = False


def _schluessel():
    geheim = os.getenv("AUTH_SECRET", "").strip()
    if not geheim:
        raise RuntimeError("chat needs AUTH_SECRET")
    return hashlib.sha256(("oddsengine-chat:" + geheim).encode()).digest()


def _alte_schluessel():
    """Nach einem Wechsel von AUTH_SECRET bleiben alte Chats lesbar, wenn der
    alte Wert in AUTH_SECRET_OLD steht (mehrere mit Komma). Nur zum Lesen,
    geschrieben wird immer mit dem neuen Schluessel."""
    roh = os.getenv("AUTH_SECRET_OLD", "")
    return [hashlib.sha256(("oddsengine-chat:" + g.strip()).encode()).digest()
            for g in roh.split(",") if g.strip()]


def _entschluesseln(b):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    letzter = None
    for k in [_schluessel()] + _alte_schluessel():
        try:
            return AESGCM(k).decrypt(b[:12], b[12:], None)
        except Exception as e:
            letzter = e
    raise letzter


def _zu(text):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    n = secrets.token_bytes(12)
    return base64.b64encode(n + AESGCM(_schluessel()).encrypt(n, text.encode(), None)).decode()


def _auf(roh):
    if not roh:
        return ""
    try:
        b = base64.b64decode(roh)
        return _entschluesseln(b).decode()
    except Exception:
        return ""


def _zu_bytes(b):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    n = secrets.token_bytes(12)
    return n + AESGCM(_schluessel()).encrypt(n, b, None)


def _auf_bytes(b):
    return _entschluesseln(bytes(b))


def _pg():
    global _SCHEMA
    pg = ai._pg()
    if not _SCHEMA:
        pg.execute("""CREATE TABLE IF NOT EXISTS ai_chats (
            id TEXT PRIMARY KEY, address TEXT NOT NULL, title TEXT, model TEXT,
            created TIMESTAMPTZ DEFAULT now(), updated TIMESTAMPTZ DEFAULT now())""")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_ai_chats_addr ON ai_chats(address, updated)")
        pg.execute("""CREATE TABLE IF NOT EXISTS ai_msgs (
            id BIGSERIAL PRIMARY KEY, chat_id TEXT NOT NULL, address TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT, images JSONB, model TEXT,
            cost NUMERIC(20,8), ts TIMESTAMPTZ DEFAULT now())""")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_ai_msgs_chat ON ai_msgs(chat_id, id)")
        pg.execute("""CREATE TABLE IF NOT EXISTS ai_images (
            id TEXT PRIMARY KEY, address TEXT NOT NULL, chat_id TEXT, mime TEXT,
            data BYTEA, kind TEXT, ts TIMESTAMPTZ DEFAULT now())""")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_ai_images_ts ON ai_images(ts)")
        _SCHEMA = True
    return pg


class Fehler(Exception):
    def __init__(self, status, text):
        super().__init__(text)
        self.status, self.text = status, text


# ── Chats ────────────────────────────────────────────────────────────────

def liste(adresse):
    rows = _pg().execute("""SELECT id, title, model, updated FROM ai_chats WHERE address=%s
                            ORDER BY updated DESC LIMIT %s""",
                         (holder.norm(adresse), MAX_CHATS)).fetchall()
    return [{"id": r[0], "title": _auf(r[1]) or "New chat", "model": r[2],
             "updated": r[3].isoformat() if r[3] else None} for r in rows]


def neu(adresse, model=None):
    adresse = holder.norm(adresse)
    pg = _pg()
    n = pg.execute("SELECT count(*) FROM ai_chats WHERE address=%s", (adresse,)).fetchone()[0]
    if n >= MAX_CHATS:
        # aeltesten loeschen statt abzuweisen
        alt = pg.execute("SELECT id FROM ai_chats WHERE address=%s ORDER BY updated ASC LIMIT 1",
                         (adresse,)).fetchone()
        if alt:
            loeschen(adresse, alt[0])
    cid = "c_" + secrets.token_hex(8)
    pg.execute("INSERT INTO ai_chats (id, address, title, model) VALUES (%s,%s,%s,%s)",
               (cid, adresse, None, model))
    return {"id": cid, "title": "New chat", "model": model}


def _gehoert(adresse, cid):
    r = _pg().execute("SELECT id, model, title FROM ai_chats WHERE id=%s AND address=%s",
                      (str(cid), holder.norm(adresse))).fetchone()
    if not r:
        raise Fehler(404, "chat not found")
    return r


def laden(adresse, cid, key=""):
    r = _gehoert(adresse, cid)
    rows = _pg().execute("""SELECT id, role, content, images, model, cost, ts FROM ai_msgs
                            WHERE chat_id=%s ORDER BY id ASC""", (str(cid),)).fetchall()
    return {"id": r[0], "model": r[1], "title": _auf(r[2]) or "New chat",
            "messages": [{"id": x[0], "role": x[1], "content": _auf(x[2]),
                          "images": x[3] or [], "model": x[4],
                          "cost": float(x[5]) if x[5] is not None else None,
                          "ts": x[6].isoformat() if x[6] else None} for x in rows]}


def loeschen(adresse, cid):
    adresse = holder.norm(adresse)
    pg = _pg()
    if not pg.execute("SELECT 1 FROM ai_chats WHERE id=%s AND address=%s", (str(cid), adresse)).fetchone():
        return
    pg.execute("DELETE FROM ai_msgs WHERE chat_id=%s", (str(cid),))
    pg.execute("DELETE FROM ai_images WHERE chat_id=%s AND address=%s", (str(cid), adresse))
    pg.execute("DELETE FROM ai_chats WHERE id=%s", (str(cid),))


def alles_loeschen(adresse):
    adresse = holder.norm(adresse)
    pg = _pg()
    pg.execute("DELETE FROM ai_msgs WHERE address=%s", (adresse,))
    pg.execute("DELETE FROM ai_images WHERE address=%s", (adresse,))
    pg.execute("DELETE FROM ai_chats WHERE address=%s", (adresse,))


def umbenennen(adresse, cid, titel):
    _gehoert(adresse, cid)
    t = re.sub(r"\s+", " ", str(titel or "")).strip()[:80] or "New chat"
    _pg().execute("UPDATE ai_chats SET title=%s WHERE id=%s", (_zu(t), str(cid)))


# ── Bilder ───────────────────────────────────────────────────────────────

_DATAURL = re.compile(r"^data:(image/(?:png|jpeg|jpg|webp|gif));base64,([A-Za-z0-9+/=\s]+)$")


def _bild_speichern(adresse, cid, mime, daten, art):
    if len(daten) > MAX_BILD_BYTES:
        raise Fehler(413, "image too large, keep it under 4 MB")
    bid = "i_" + secrets.token_hex(10)
    _pg().execute("INSERT INTO ai_images (id, address, chat_id, mime, data, kind) VALUES (%s,%s,%s,%s,%s,%s)",
                  (bid, holder.norm(adresse), cid, mime, _zu_bytes(daten), art))
    return bid


def bild_laden(adresse, bid):
    r = _pg().execute("SELECT mime, data FROM ai_images WHERE id=%s AND address=%s",
                      (str(bid), holder.norm(adresse))).fetchone()
    if not r:
        return None, None
    try:
        return r[0], _auf_bytes(r[1])
    except Exception:
        return None, None


def _dataurl(adresse, bid):
    mime, b = bild_laden(adresse, bid)
    if not b:
        return None
    return "data:{};base64,{}".format(mime, base64.b64encode(b).decode())


def aufraeumen():
    tage = int(os.getenv("AI_IMAGE_DAYS", "30") or 30)
    try:
        _pg().execute("DELETE FROM ai_images WHERE ts < now() - make_interval(days => %s)", (tage,))
    except Exception as e:
        log.debug(f"chat aufraeumen {e}")


# ── Senden ───────────────────────────────────────────────────────────────

SYSTEM = ("You are OddsEngine AI, a helpful assistant inside engine.oddsengine.live. "
          "Answer clearly and directly. Use Markdown for structure when it helps. "
          "Never claim to be a different product than the model you are; if asked which "
          "model you are, say the model name you were given: {model}.")


def _kontext(adresse, cid, model_name):
    rows = _pg().execute("""SELECT role, content, images FROM ai_msgs WHERE chat_id=%s
                            ORDER BY id DESC LIMIT %s""", (str(cid), VERLAUF)).fetchall()
    rows = list(reversed(rows))
    msgs = [{"role": "system", "content": SYSTEM.format(model=model_name)}]
    mit_bild = [i for i, r in enumerate(rows) if r[2]]
    behalten = set(mit_bild[-BILD_KONTEXT:])
    for i, (rolle, inhalt, bilder) in enumerate(rows):
        text = _auf(inhalt)
        if bilder and rolle == "user" and i in behalten:
            teile = [{"type": "text", "text": text or " "}]
            for bid in bilder:
                u = _dataurl(adresse, bid)
                if u:
                    teile.append({"type": "image_url", "image_url": {"url": u}})
            msgs.append({"role": rolle, "content": teile})
        else:
            if bilder:
                text = (text + "\n" if text else "") + "[{} image{}]".format(
                    len(bilder), "" if len(bilder) == 1 else "s")
            msgs.append({"role": rolle, "content": text or " "})
    return msgs


def _speichern(adresse, cid, rolle, text, bilder, model, kosten):
    _pg().execute("""INSERT INTO ai_msgs (chat_id, address, role, content, images, model, cost)
                     VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                  (str(cid), holder.norm(adresse), rolle, _zu(text or ""),
                   json.dumps(bilder or []), model,
                   Decimal(str(round(kosten, 8))) if kosten is not None else None))
    _pg().execute("UPDATE ai_chats SET updated=now(), model=%s WHERE id=%s", (model, str(cid)))


def _titel_setzen(adresse, cid, text):
    r = _pg().execute("SELECT title FROM ai_chats WHERE id=%s", (str(cid),)).fetchone()
    if r and not r[0]:
        t = re.sub(r"\s+", " ", text or "").strip()
        t = (t[:60] + "…") if len(t) > 60 else t
        _pg().execute("UPDATE ai_chats SET title=%s WHERE id=%s", (_zu(t or "Image"), str(cid)))


def vorbereiten(adresse, cid, body):
    """Nachricht des Nutzers pruefen und speichern. Gibt (model, mode, messages) zurueck."""
    _gehoert(adresse, cid)
    text = str(body.get("content") or "").strip()
    bilder_roh = body.get("images") or []
    if not isinstance(bilder_roh, list):
        bilder_roh = []
    if len(bilder_roh) > MAX_BILDER:
        raise Fehler(400, f"up to {MAX_BILDER} images per message")
    if not text and not bilder_roh:
        raise Fehler(400, "write something first")
    if len(text) > 60000:
        raise Fehler(400, "message too long")
    model = str(body.get("model") or ai.AUTO_ID)
    m = ai.modell(model)
    if not m:
        raise Fehler(404, "model not available")
    mode = "image" if (body.get("mode") == "image" or m.get("kind") == "image") else "text"
    if mode == "image" and m.get("kind") != "image":
        raise Fehler(400, "pick an image model to create images")
    if bilder_roh and mode == "text" and "vision" not in (m.get("tags") or []) and model != ai.AUTO_ID:
        raise Fehler(400, "{} cannot see images. Pick a model with the vision tag.".format(m["name"]))
    ids = []
    for u in bilder_roh:
        mt = _DATAURL.match(str(u or ""))
        if not mt:
            raise Fehler(400, "images must be PNG, JPEG, WEBP or GIF")
        daten = base64.b64decode(re.sub(r"\s", "", mt.group(2)))
        ids.append(_bild_speichern(adresse, cid, mt.group(1).replace("jpg", "jpeg"), daten, "in"))
    _speichern(adresse, cid, "user", text, ids, model, None)
    _titel_setzen(adresse, cid, text)
    return m, mode, _kontext(adresse, cid, m["name"])


def letzte_zuruecknehmen(adresse, cid):
    """Nach einem Fehler vor der Antwort: die eben gespeicherte Nutzer-Nachricht
    wieder entfernen, damit ein erneuter Versuch nichts doppelt."""
    pg = _pg()
    r = pg.execute("""SELECT id, role, images FROM ai_msgs WHERE chat_id=%s AND address=%s
                      ORDER BY id DESC LIMIT 1""", (str(cid), holder.norm(adresse))).fetchone()
    if r and r[1] == "user":
        pg.execute("DELETE FROM ai_msgs WHERE id=%s", (r[0],))
        for bid in r[2] or []:
            pg.execute("DELETE FROM ai_images WHERE id=%s", (bid,))


def text_strom(adresse, cid, m, msgs):
    """Startet die Anfrage sofort (Fehler wie leeres Guthaben kommen so als
    normale Antwort zurueck) und gibt einen Generator fuer SSE zurueck."""
    quelle = ai.strom(adresse, None, {"model": m["id"], "messages": msgs})
    return _weiter(adresse, cid, m, quelle)


def _weiter(adresse, cid, m, quelle):
    teile, kosten, echt = [], None, None
    try:
        for roh in quelle:
            try:
                zeile = roh.decode()
                if zeile.startswith("data: ") and "[DONE]" not in zeile:
                    j = json.loads(zeile[6:])
                    for c in j.get("choices") or []:
                        d = (c.get("delta") or {}).get("content")
                        if d:
                            teile.append(d)
                    if j.get("usage"):
                        kosten = j["usage"].get("oddsengine_charged_usd")
                    echt = j.get("routed_model") or echt
            except Exception:
                pass
            yield roh
    finally:
        try:
            antwort = "".join(teile)
            if antwort or kosten:
                _speichern(adresse, cid, "assistant", antwort, [], m["id"], kosten)
        except Exception as e:
            log.warning(f"chat speichern {e}")


def bild_erzeugen(adresse, cid, m, msgs):
    """Bild-Modelle: ohne Stream, Bilder aus der Antwort speichern."""
    d = ai.anfrage(adresse, None, {"model": m["id"], "messages": msgs,
                                   "modalities": ["image", "text"], "max_tokens": 4096})
    msg = ((d.get("choices") or [{}])[0].get("message") or {})
    text = msg.get("content") or ""
    if isinstance(text, list):
        text = " ".join(str(t.get("text") or "") for t in text if isinstance(t, dict))
    ids = []
    for b in msg.get("images") or []:
        url = ((b or {}).get("image_url") or {}).get("url") or ""
        mt = _DATAURL.match(url)
        if mt:
            try:
                ids.append(_bild_speichern(adresse, cid, mt.group(1),
                                           base64.b64decode(re.sub(r"\s", "", mt.group(2))), "out"))
            except Fehler:
                continue
    kosten = (d.get("usage") or {}).get("oddsengine_charged_usd")
    if not ids and not text:
        text = "The model returned no image this time. Try a different prompt or model."
    _speichern(adresse, cid, "assistant", text, ids, m["id"], kosten)
    return {"role": "assistant", "content": text, "images": ids, "model": m["id"], "cost": kosten}
