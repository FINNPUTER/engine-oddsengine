"""
postfach.py: E-Mail fuer Your Engine. Seit 2.36. Gleiches Prinzip wie bei HENRY.

Zugang ueber IMAP mit einem App-Passwort (Gmail, iCloud, Yahoo, GMX, web.de, Outlook wo erlaubt).
Kein Google-Pruefverfahren, keine Nutzergrenze.

BEWUSSTE GRENZEN, im Code und nicht nur im Prompt:
    Lesen:                           ja (immer BODY.PEEK, nichts wird als gelesen markiert)
    Entwurf in den Entwuerfe-Ordner: ja, der Nutzer schickt ihn selbst ab
    Senden, Loeschen, Verschieben:   NEIN. In dieser Datei gibt es keinen Aufruf dafuer.

Das App-Passwort wird mit AES-GCM verschluesselt gespeichert (Schluessel aus MAIL_KEY auf web).
Ohne MAIL_KEY laesst sich kein Postfach verbinden. Trennen loescht es sofort.
Verbunden wird nur ueber die Seite (engine.oddsengine.live), nie ueber den Chat, damit das
Passwort nie durch ein Modell oder ein Telegram-Protokoll laeuft.

Speicher: kv motor_mail_<adresse>.
"""
import base64
import email
import email.header
import email.policy
import email.utils
import hashlib
import html
import imaplib
import json
import os
import re
import secrets
import socket
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime

import storage
from logger import log

ANBIETER = {
    "gmail":   ("imap.gmail.com", 993, "https://myaccount.google.com/apppasswords"),
    "icloud":  ("imap.mail.me.com", 993, "https://account.apple.com"),
    "yahoo":   ("imap.mail.yahoo.com", 993, "https://login.yahoo.com/account/security"),
    "gmx":     ("imap.gmx.net", 993, "https://www.gmx.net"),
    "webde":   ("imap.web.de", 993, "https://web.de"),
    "outlook": ("outlook.office365.com", 993, "https://account.microsoft.com/security"),
}
WICHTIG = re.compile(r"\b(invoice|rechnung|mahnung|reminder|payment|zahlung|due|f[aä]llig|overdue|urgent|dringend|"
                     r"security|sicherheit|password|passwort|verify|verification|login|anmeldung|refund|erstattung|"
                     r"contract|vertrag|deadline|frist|termin|appointment|delivery|lieferung|paket|parcel|tax|steuer)\b", re.I)
WERBUNG = re.compile(r"(newsletter|unsubscribe|abmelden|no-?reply@.*(marketing|news)|% off|sale\b|angebot)", re.I)
socket.setdefaulttimeout(25)


# ── Verschluesselung ─────────────────────────────────────────────────────

def _schluessel():
    k = os.getenv("MAIL_KEY", "").strip()
    if not k:
        return None
    return hashlib.sha256(("oe-mail:" + k).encode()).digest()


def bereit():
    return _schluessel() is not None


def _zu(text):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    n = secrets.token_bytes(12)
    return base64.b64encode(n + AESGCM(_schluessel()).encrypt(n, text.encode(), None)).decode()


def _auf(blob):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    roh = base64.b64decode(blob)
    return AESGCM(_schluessel()).decrypt(roh[:12], roh[12:], None).decode()


# ── Speicher ─────────────────────────────────────────────────────────────

def _lesen(adr):
    try:
        r = storage.kv_get("motor_mail_" + adr)
        return json.loads(r) if r else None
    except Exception:
        return None


def _schreiben(adr, d):
    storage.kv_set("motor_mail_" + adr, json.dumps(d) if d else "")


def status(adr):
    d = _lesen(adr)
    if not d:
        return {"connected": False, "available": bereit()}
    return {"connected": True, "available": bereit(), "provider": d.get("provider"), "email": d.get("user"),
            "since": d.get("since"), "watch": d.get("watch", True), "error": d.get("error"),
            "headers_only": bool(d.get("headers_only"))}


# ── IMAP ─────────────────────────────────────────────────────────────────

def _verbindung(d):
    host, port = d["host"], int(d.get("port", 993))
    m = imaplib.IMAP4_SSL(host, port)
    m.login(d["user"], _auf(d["secret"]))
    return m


def verbinden(adr, provider, adresse, passwort):
    if not bereit():
        return {"ok": False, "error": "Email is not switched on yet. The operator has to set MAIL_KEY first."}
    provider = (provider or "gmail").lower().replace(".", "").replace(" ", "")
    if provider not in ANBIETER:
        return {"ok": False, "error": "Pick Gmail, iCloud, Yahoo, GMX, web.de or Outlook."}
    adresse = (adresse or "").strip()
    passwort = re.sub(r"\s+", "", passwort or "") if provider == "gmail" else (passwort or "").strip()
    if "@" not in adresse or len(passwort) < 6:
        return {"ok": False, "error": "Email address and app password are needed."}
    host, port, _ = ANBIETER[provider]
    d = {"provider": provider, "host": host, "port": port, "user": adresse, "secret": _zu(passwort),
         "since": int(time.time()), "watch": True}
    try:
        m = _verbindung(d)
        typ, daten = m.select("INBOX", readonly=True)
        uid_next = _uid_max(m)
        m.logout()
    except imaplib.IMAP4.error as e:
        return {"ok": False, "error": "Login failed. Use an app password, not your normal password. " + str(e)[:80]}
    except Exception as e:
        return {"ok": False, "error": "Could not reach the mailbox: " + str(e)[:100]}
    d["last_uid"] = uid_next
    _schreiben(adr, d)
    return {"ok": True, "email": adresse, "provider": provider}


def trennen(adr):
    _schreiben(adr, None)
    return {"ok": True}


def wache(adr, an):
    d = _lesen(adr)
    if not d:
        return {"ok": False, "error": "No mailbox connected."}
    d["watch"] = bool(an)
    _schreiben(adr, d)
    return {"ok": True, "watch": d["watch"]}


def nur_kopf(adr, an):
    """Nur Absender und Betreff: Your Engine liest nie den Text einer Mail."""
    d = _lesen(adr)
    if not d:
        return {"ok": False, "error": "No mailbox connected."}
    d["headers_only"] = bool(an)
    _schreiben(adr, d)
    return {"ok": True, "headers_only": d["headers_only"]}


def _uid_max(m):
    typ, daten = m.uid("search", None, "ALL")
    ids = (daten[0] or b"").split()
    return int(ids[-1]) if ids else 0


def _kopf(roh):
    try:
        return str(email.header.make_header(email.header.decode_header(roh or "")))
    except Exception:
        return roh or ""


def _text_aus(msg, n=6000):
    teile = []
    if msg.is_multipart():
        for p in msg.walk():
            ct = p.get_content_type()
            if p.get_content_disposition() == "attachment":
                continue
            if ct in ("text/plain", "text/html"):
                try:
                    teile.append((ct, p.get_content()))
                except Exception:
                    try:
                        teile.append((ct, p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8", "replace")))
                    except Exception:
                        pass
    else:
        try:
            teile.append((msg.get_content_type(), msg.get_content()))
        except Exception:
            teile.append(("text/plain", str(msg.get_payload())))
    txt = next((t for ct, t in teile if ct == "text/plain" and t and t.strip()), None)
    if not txt:
        h = next((t for ct, t in teile if ct == "text/html"), "") or ""
        h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
        h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", h)
        txt = html.unescape(re.sub(r"<[^>]+>", " ", h))
    txt = re.sub(r"[ \t\r\f\v]+", " ", txt or "")
    txt = re.sub(r"\n\s*\n+", "\n\n", txt).strip()
    return txt[:n]


def _eintrag(uid, msg, flags=b"", kopf=False):
    name, absender = parseaddr(_kopf(msg.get("From")))
    try:
        ts = parsedate_to_datetime(msg.get("Date")).timestamp()
    except Exception:
        ts = None
    betreff = _kopf(msg.get("Subject"))
    vorschau = "" if kopf else _text_aus(msg, 280).replace("\n", " ")
    return {"id": str(uid), "from": name or absender, "from_email": absender, "subject": betreff,
            "date": datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None,
            "unread": b"\\Seen" not in (flags or b""), "preview": vorschau,
            "important": bool(WICHTIG.search(betreff + " " + vorschau)) and not WERBUNG.search(betreff + " " + absender)}


def liste(adr, days=2, unread_only=False, query=None, count=15):
    d = _lesen(adr)
    if not d:
        return {"error": "No mailbox connected. The user connects it once on engine.oddsengine.live under Your Engine, Email."}
    count = max(1, min(int(count or 15), 30))
    seit = (datetime.now(timezone.utc) - timedelta(days=max(1, min(int(days or 2), 30)))).strftime("%d-%b-%Y")
    krit = ["SINCE", seit]
    ko = bool(d.get("headers_only"))
    if unread_only:
        krit.append("UNSEEN")
    try:
        m = _verbindung(d)
        m.select("INBOX", readonly=True)
        if query:
            q = str(query).replace('"', "")[:60]
            if ko:
                typ, daten = m.uid("search", "CHARSET", "UTF-8", *krit, "OR", "SUBJECT", f'"{q}"', "FROM", f'"{q}"')
            else:
                typ, daten = m.uid("search", "CHARSET", "UTF-8", *krit, "TEXT", f'"{q}"')
        else:
            typ, daten = m.uid("search", None, *krit)
        ids = (daten[0] or b"").split()[-count:]
        out = []
        for uid in reversed(ids):
            typ, roh = m.uid("fetch", uid, "(FLAGS BODY.PEEK[HEADER])" if ko else "(FLAGS BODY.PEEK[]<0.40000>)")
            msg_b, flags = None, b""
            for teil in roh:
                if isinstance(teil, tuple):
                    flags = teil[0]
                    msg_b = teil[1]
            if msg_b:
                out.append(_eintrag(uid.decode(), email.message_from_bytes(msg_b, policy=email.policy.default), flags, ko))
        m.logout()
    except Exception as e:
        _fehler(adr, d, e)
        return {"error": "Mailbox not reachable right now: " + str(e)[:100]}
    return {"email": d["user"], "messages": out, "unread_count": sum(1 for x in out if x["unread"]),
            "headers_only": ko,
            "note": ("Headers only: the user chose that Your Engine sees sender and subject, never the text. "
                     if ko else "") + "Read only. Your Engine never sends, deletes or moves mail."}


def lesen(adr, uid, intern=False):
    d = _lesen(adr)
    if not d:
        return {"error": "No mailbox connected."}
    ko = bool(d.get("headers_only"))
    try:
        m = _verbindung(d)
        m.select("INBOX", readonly=True)
        typ, roh = m.uid("fetch", str(int(uid)), "(FLAGS BODY.PEEK[HEADER])" if ko else "(FLAGS BODY.PEEK[]<0.300000>)")
        m.logout()
    except Exception as e:
        _fehler(adr, d, e)
        return {"error": "Mailbox not reachable right now: " + str(e)[:100]}
    for teil in roh or []:
        if isinstance(teil, tuple):
            msg = email.message_from_bytes(teil[1], policy=email.policy.default)
            e = _eintrag(str(uid), msg, teil[0], ko)
            e["text"] = None if ko else _text_aus(msg, 8000)
            if ko:
                e["note"] = "Headers only is on: Your Engine cannot read the text of this mail. The user can switch it off on engine.oddsengine.live."
            e["to"] = _kopf(msg.get("To"))
            e["message_id"] = msg.get("Message-ID")
            anh = [p.get_filename() for p in msg.walk() if p.get_content_disposition() == "attachment"]
            e["attachments"] = [a for a in anh if a][:10]
            return e
    return {"error": "Message not found."}


def _entwuerfe(m):
    typ, daten = m.list()
    for z in daten or []:
        s = z.decode(errors="replace")
        if "\\Drafts" in s:
            name = s.rsplit(' "/" ', 1)[-1].rsplit(' "." ', 1)[-1].strip()
            return name if name.startswith('"') else f'"{name}"'
    for kand in ('"[Gmail]/Drafts"', '"[Gmail]/Entw&APw-rfe"', "Drafts", "Entwurf", "Entw&APw-rfe"):
        typ, _ = m.select(kand, readonly=True)
        if typ == "OK":
            return kand
    return "Drafts"


def entwurf(adr, to, subject, body, reply_to_id=None):
    """Legt eine Antwort in den Entwuerfe-Ordner. Senden tut der Nutzer selbst."""
    d = _lesen(adr)
    if not d:
        return {"error": "No mailbox connected."}
    if not body:
        return {"error": "Nothing to write."}
    msg = EmailMessage()
    msg["From"] = d["user"]
    msg["Subject"] = subject or ""
    if to:
        msg["To"] = to
    if reply_to_id:
        orig = lesen(adr, reply_to_id, intern=True)
        if orig and not orig.get("error"):
            msg.replace_header("Subject", subject or ("Re: " + (orig.get("subject") or "")))
            if not to:
                msg["To"] = orig.get("from_email")
            if orig.get("message_id"):
                msg["In-Reply-To"] = orig["message_id"]
                msg["References"] = orig["message_id"]
    msg["Date"] = email.utils.formatdate(localtime=False)
    msg.set_content(body)
    try:
        m = _verbindung(d)
        ordner = _entwuerfe(m)
        typ, _ = m.append(ordner, "(\\Draft)", imaplib.Time2Internaldate(time.time()), msg.as_bytes(policy=email.policy.SMTP))
        m.logout()
    except Exception as e:
        _fehler(adr, d, e)
        return {"error": "Could not save the draft: " + str(e)[:100]}
    if typ != "OK":
        return {"error": "The mailbox refused the draft."}
    return {"ok": True, "saved_in": ordner.strip('"'), "to": msg.get("To"), "subject": msg.get("Subject"),
            "note": "Saved as a draft. Open your mail app to check it and press send yourself."}


def _fehler(adr, d, e):
    log.debug(f"postfach {adr}: {e}")
    if isinstance(e, imaplib.IMAP4.error) and "auth" in str(e).lower():
        d["error"] = "Login stopped working. Create a new app password and connect again."
        _schreiben(adr, d)


# ── Wache: neue wichtige Mails melden ────────────────────────────────────

def neue(adr):
    """Neue Mails seit dem letzten Blick. Gibt die wichtigen zurueck und merkt sich die UID."""
    d = _lesen(adr)
    if not d or not d.get("watch", True) or d.get("error"):
        return []
    try:
        m = _verbindung(d)
        m.select("INBOX", readonly=True)
        letzte = int(d.get("last_uid") or 0)
        typ, daten = m.uid("search", None, f"UID {letzte + 1}:*")
        ids = [int(x) for x in (daten[0] or b"").split() if int(x) > letzte][-20:]
        out = []
        for uid in ids:
            typ, roh = m.uid("fetch", str(uid), "(FLAGS BODY.PEEK[HEADER])" if d.get("headers_only") else "(FLAGS BODY.PEEK[]<0.30000>)")
            for teil in roh or []:
                if isinstance(teil, tuple):
                    e = _eintrag(str(uid), email.message_from_bytes(teil[1], policy=email.policy.default), teil[0], bool(d.get("headers_only")))
                    if e["important"]:
                        out.append(e)
        m.logout()
        if ids:
            d["last_uid"] = max(ids)
            _schreiben(adr, d)
        return out
    except Exception as e:
        _fehler(adr, d, e)
        return []


def briefing_zeilen(adr, n=5):
    d = _lesen(adr)
    if not d or d.get("error"):
        return []
    r = liste(adr, days=1, unread_only=True, count=25)
    if r.get("error"):
        return []
    ms = r["messages"]
    if not ms:
        return ["Email: inbox clear, nothing unread since yesterday."]
    wichtig = [x for x in ms if x["important"]]
    z = [f"Email: {len(ms)} unread" + (f", {len(wichtig)} look important" if wichtig else "")]
    for x in (wichtig or ms)[:n]:
        z.append(f"{x['from'][:28]}: {x['subject'][:70]}")
    return z
