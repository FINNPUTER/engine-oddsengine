"""
support.py: Support-Postfach. Seit 2.33.

Jede Anfrage landet als Ticket in der Datenbank, damit nichts verloren geht:
  telegram   jemand schreibt dem Bot (Empfang), auch /support <text>
  engine     Your Engine meldet Feedback, Fehler oder Ideen (send_feedback)
  web        Formular "Support" auf engine.oddsengine.live

Der Betreiber sieht alles in admin.html (Support inbox), kann antworten (geht per
Telegram an den Nutzer, wenn bekannt) und Tickets erledigen. Antwortet er im Telegram
auf die weitergeleitete Nachricht, wird das Ticket automatisch als beantwortet markiert.

Screenshots: Fotos an den Bot und Bilder im Formular werden am Ticket gespeichert.
KI-Sortierung: jedes neue Ticket bekommt von einem guenstigen Modell Bereich, Art,
Dringlichkeit und einen kurzen Titel (SUPPORT_AI_MODEL, Standard Gemini Flash Lite, sieht
auch die Screenshots). bericht() fasst alle offenen Tickets zu gebuendelten Baustellen
zusammen (SUPPORT_REPORT_MODEL, Standard Claude Sonnet). Die Kosten traegt OddsEngine,
ein paar Zehntel Cent pro Ticket. Aus: SUPPORT_AI=off
"""
import base64
import json
import os
import re
import threading
import time

from logger import log

_BEREIT = {"ok": False}


def _pg():
    import ai
    pg = ai._pg()
    if not _BEREIT["ok"]:
        pg.execute("""CREATE TABLE IF NOT EXISTS support_tickets (
            id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT now(),
            source TEXT NOT NULL, tg_id TEXT, wallet TEXT, name TEXT, handle TEXT,
            text TEXT NOT NULL, context TEXT, status TEXT NOT NULL DEFAULT 'open',
            reply TEXT, replied_at TIMESTAMPTZ, note TEXT)""")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_support_status ON support_tickets(status, id)")
        for col in ("ai_category", "ai_area", "ai_severity", "ai_title", "ai_summary"):
            pg.execute(f"ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS {col} TEXT")
        pg.execute("""CREATE TABLE IF NOT EXISTS support_files (
            id BIGSERIAL PRIMARY KEY, ticket_id BIGINT NOT NULL, mime TEXT, data BYTEA,
            ts TIMESTAMPTZ NOT NULL DEFAULT now())""")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_support_files_t ON support_files(ticket_id)")
        _BEREIT["ok"] = True
    return pg


MAX_BILD = 5_000_000


def neu(source, text, tg_id=None, wallet=None, name=None, handle=None, context=None, melden=False, bilder=None):
    t = str(text or "").strip()[:4000]
    if not t and bilder:
        t = "[screenshot]"
    if not t:
        return None
    try:
        r = _pg().execute("""INSERT INTO support_tickets (source, tg_id, wallet, name, handle, text, context)
                             VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                          (source, str(tg_id) if tg_id is not None else None, wallet, (name or "")[:80],
                           (handle or "")[:80], t, (context or "")[:4000] or None)).fetchone()
        tid = r[0] if r else None
        for mime, daten in (bilder or [])[:5]:
            if daten and len(daten) <= MAX_BILD:
                _pg().execute("INSERT INTO support_files (ticket_id, mime, data) VALUES (%s,%s,%s)",
                              (tid, mime or "image/jpeg", daten))
    except Exception as e:
        log.warning(f"support neu: {e}")
        tid = None
    if tid and os.getenv("SUPPORT_AI", "on").strip().lower() not in ("0", "off", "false", "no"):
        threading.Thread(target=klassifizieren, args=(tid,), daemon=True).start()
    if melden:
        try:
            import ai
            wer = (name or "?") + (f" (@{handle})" if handle else "")
            ai._an_betreiber(f"Support #{tid} ({source})\nFrom: {wer}" + (f"  #id{tg_id}" if tg_id else "")
                             + (f"\nWallet: {wallet}" if wallet else "") + (f"\n{len(bilder)} screenshot(s)" if bilder else "")
                             + f"\n\n{t}" + (f"\n\n{context}" if context else "")
                             + ("\n\nReply to this message to answer them, or use admin.html." if tg_id else "\n\nAnswer in admin.html, Support inbox."))
        except Exception:
            pass
    return tid


SPALTEN = ("id, ts, source, tg_id, wallet, name, handle, text, context, status, reply, replied_at, note, "
           "ai_category, ai_area, ai_severity, ai_title, ai_summary")


def _zeile(r):
    return {"id": r[0], "ts": r[1].isoformat() if r[1] else None, "source": r[2], "tg_id": r[3], "wallet": r[4],
            "name": r[5], "handle": r[6], "text": r[7], "context": r[8], "status": r[9], "reply": r[10],
            "replied_at": r[11].isoformat() if r[11] else None, "note": r[12],
            "ai": {"category": r[13], "area": r[14], "severity": r[15], "title": r[16], "summary": r[17]}}


def _dateien(ids):
    if not ids:
        return {}
    rows = _pg().execute("SELECT id, ticket_id FROM support_files WHERE ticket_id = ANY(%s) ORDER BY id",
                         (list(ids),)).fetchall()
    out = {}
    for fid, tid in rows:
        out.setdefault(tid, []).append(fid)
    return out


def datei(fid):
    r = _pg().execute("SELECT mime, data FROM support_files WHERE id=%s", (int(fid),)).fetchone()
    return (r[0], bytes(r[1])) if r else (None, None)


def liste(status="open", n=100):
    sql = "SELECT " + SPALTEN + " FROM support_tickets"
    args = []
    if status and status != "all":
        sql += " WHERE status=%s"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT %s"
    args.append(max(1, min(int(n or 100), 500)))
    rows = _pg().execute(sql, tuple(args)).fetchall()
    zahlen = dict(_pg().execute("SELECT status, count(*) FROM support_tickets GROUP BY status").fetchall())
    ts = [_zeile(r) for r in rows]
    fs = _dateien([t["id"] for t in ts])
    for t in ts:
        t["files"] = fs.get(t["id"], [])
    try:
        import storage
        rep_ = json.loads(storage.kv_get("support_report") or "null")
    except Exception:
        rep_ = None
    return {"tickets": ts, "counts": {k: int(v) for k, v in zahlen.items()}, "report": rep_}


# ── KI: sortieren und buendeln ───────────────────────────────────────────

BEREICHE = ["your_engine", "ai_chat", "balance_topup", "agents_x402", "weather_calls", "crypto_terminal",
            "burn_page", "telegram_bot", "wallet_holder", "website", "other"]


def _ki(model, messages, max_tokens=600):
    import ai
    import requests
    r = requests.post(ai.OR_BASE + "/chat/completions", headers=ai._or_kopf(), timeout=90,
                      data=json.dumps({"model": model, "messages": messages, "max_tokens": max_tokens,
                                       "usage": {"include": True}}))
    d = r.json()
    if r.status_code >= 400 or "error" in d:
        raise RuntimeError(str((d.get("error") or {}).get("message") or r.status_code)[:200])
    c = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    if isinstance(c, list):
        c = " ".join(str(x.get("text") or "") for x in c if isinstance(x, dict))
    return c


def klassifizieren(tid):
    try:
        r = _pg().execute("SELECT source, text, context FROM support_tickets WHERE id=%s", (int(tid),)).fetchone()
        if not r:
            return
        inhalt = [{"type": "text", "text":
                   "You sort support tickets for OddsEngine (weather and crypto calls on Polymarket, OddsEngine AI chat "
                   "paid in $ODDS, Your Engine personal agent in Telegram, x402 agent payments, burn page, $ODDS token on "
                   "Torch / Robinhood Chain). Read the ticket and any screenshots. Answer ONLY with JSON: "
                   '{"category": "bug|payment|question|feature|feedback|spam|other", '
                   '"area": "' + "|".join(BEREICHE) + '", "severity": "high|medium|low", '
                   '"title": "max 8 words, what is wrong or wanted", '
                   '"summary": "one or two sentences: what happened, what the user expects, anything seen in the screenshot"}. '
                   "high = money, balance, payments, or something broken for many people. English only.\n\n"
                   f"Source: {r[0]}\nTicket: {r[1]}\n" + (f"Context: {r[2][:1500]}" if r[2] else "")}]
        for fid, mime, daten in _pg().execute(
                "SELECT id, mime, data FROM support_files WHERE ticket_id=%s ORDER BY id LIMIT 3", (int(tid),)).fetchall():
            inhalt.append({"type": "image_url", "image_url": {
                "url": f"data:{mime or 'image/jpeg'};base64," + base64.b64encode(bytes(daten)).decode()}})
        txt = _ki(os.getenv("SUPPORT_AI_MODEL", "").strip() or "google/gemini-3.5-flash-lite",
                  [{"role": "user", "content": inhalt}], 400)
        m = re.search(r"\{.*\}", txt, re.S)
        j = json.loads(m.group(0)) if m else {}
        _pg().execute("""UPDATE support_tickets SET ai_category=%s, ai_area=%s, ai_severity=%s, ai_title=%s,
                         ai_summary=%s WHERE id=%s""",
                      (str(j.get("category") or "")[:20], str(j.get("area") or "")[:30],
                       str(j.get("severity") or "")[:10], str(j.get("title") or "")[:120],
                       str(j.get("summary") or "")[:600], int(tid)))
    except Exception as e:
        log.warning(f"support klassifizieren #{tid}: {e}")


def bericht():
    """Alle offenen Tickets zu Baustellen buendeln. Ergebnis in kv support_report."""
    rows = _pg().execute("SELECT " + SPALTEN + " FROM support_tickets WHERE status='open' ORDER BY id DESC LIMIT 200").fetchall()
    ts = [_zeile(r) for r in rows]
    if not ts:
        out = {"ts": int(time.time()), "open": 0, "text": "No open tickets. Nothing to fix right now."}
    else:
        zeilen = []
        for t in ts:
            a = t["ai"] or {}
            zeilen.append(f"#{t['id']} [{a.get('severity') or '?'}] [{a.get('area') or '?'}] [{a.get('category') or '?'}] "
                          f"{a.get('title') or ''} | {(a.get('summary') or t['text'] or '')[:300]}")
        txt = _ki(os.getenv("SUPPORT_REPORT_MODEL", "").strip() or "anthropic/claude-sonnet-5.5", [{"role": "user", "content":
                  "You are the support lead of OddsEngine. Below are all open support tickets. Group them into work items: "
                  "tickets about the same problem go together. Order by urgency (money and balance first, then things broken "
                  "for many people, then single bugs, then questions, then ideas). For each work item write:\n"
                  "N. [HIGH|MEDIUM|LOW] Area: short name of the problem\n"
                  "Tickets: #ids (count)\nWhat users see: one or two sentences\nLikely cause and what to check or fix: one or two sentences\n"
                  "Then a last section 'Questions to answer' listing tickets that only need a reply, with a one line suggested answer each. "
                  "Plain text, no markdown tables, no dashes as separators. Be concrete and short.\n\n" + "\n".join(zeilen)}], 2500)
        out = {"ts": int(time.time()), "open": len(ts), "text": txt.strip()}
    try:
        import storage
        storage.kv_set("support_report", json.dumps(out))
    except Exception:
        pass
    return out


def setzen(tid, status=None, note=None):
    if status not in (None, "open", "answered", "done"):
        return {"ok": False, "error": "status must be open, answered or done"}
    pg = _pg()
    if status:
        pg.execute("UPDATE support_tickets SET status=%s WHERE id=%s", (status, int(tid)))
    if note is not None:
        pg.execute("UPDATE support_tickets SET note=%s WHERE id=%s", (str(note)[:1000], int(tid)))
    return {"ok": True}


def antworten(tid, text):
    t = str(text or "").strip()[:3500]
    if not t:
        return {"ok": False, "error": "empty reply"}
    pg = _pg()
    r = pg.execute("SELECT tg_id, wallet FROM support_tickets WHERE id=%s", (int(tid),)).fetchone()
    if not r:
        return {"ok": False, "error": "ticket not found"}
    tg_id, wallet = r
    if not tg_id and wallet:
        try:
            import agent
            for u in agent.alle():
                if u.get("wallet") == wallet:
                    tg_id = str(u.get("tg"))
                    break
        except Exception:
            pass
    gesendet = False
    if tg_id:
        try:
            import requests
            tok = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            rr = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                               json={"chat_id": tg_id, "text": "OddsEngine support\n\n" + t,
                                     "disable_web_page_preview": True}, timeout=15)
            gesendet = rr.status_code == 200 and rr.json().get("ok")
        except Exception as e:
            log.warning(f"support antworten: {e}")
    pg.execute("UPDATE support_tickets SET reply=%s, replied_at=now(), status='answered' WHERE id=%s", (t, int(tid)))
    return {"ok": True, "sent_to_telegram": bool(gesendet),
            "note": None if gesendet else "No Telegram for this person. The reply is saved, answer them where they wrote."}


def beantwortet_per_telegram(tg_id, text):
    """Betreiber hat im Telegram auf die weitergeleitete Nachricht geantwortet."""
    try:
        pg = _pg()
        r = pg.execute("""SELECT id FROM support_tickets WHERE tg_id=%s AND status='open'
                          ORDER BY id DESC LIMIT 1""", (str(tg_id),)).fetchone()
        if r:
            pg.execute("UPDATE support_tickets SET reply=%s, replied_at=now(), status='answered' WHERE id=%s",
                       (str(text or "")[:3500], r[0]))
    except Exception as e:
        log.debug(f"support beantwortet: {e}")
