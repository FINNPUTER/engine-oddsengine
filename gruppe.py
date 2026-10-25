"""
gruppe.py: Your Engine in Telegram-Gruppen (seit 2.42). Jeder schickt einen Contract oder schreibt /scan <CA>,
der Bot antwortet im selben Thema mit der Report-Karte, kostenlos. Ist der Tiefen-Scan fertig, wird die Karte
in derselben Nachricht ergaenzt. Schalter 'group_scan'. GROUP_SCAN_PER_HOUR (60) je Gruppe.

Damit der Bot Nachrichten in der Gruppe sieht: BotFather /setprivacy -> Disable, oder den Bot zum Admin machen.
"""
import os
import threading
import time

from logger import log

_ZEIT = {}
_LETZTE = {}


def _an():
    try:
        import features
        return features.an("group_scan")
    except Exception:
        return False


def knoepfe(r):
    import agent
    out = [[("Full report", r["links"]["page"])]]
    zeile = []
    if r["links"].get("chart"):
        zeile.append(("Chart", r["links"]["chart"]))
    bot = None
    try:
        bot = agent.bot_name()
    except Exception:
        pass
    if bot:
        zeile.append(("Ask Your Engine", f"https://t.me/{bot}?start=scan"))
    out.append(zeile)
    return out


def nachricht(msg):
    """True, wenn die Gruppennachricht ein Scan war (beantwortet oder bewusst uebergangen)."""
    if not _an():
        return False
    import motor
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if text.split(" ")[0].split("@")[0] == "/scan":
        text = text.split(" ", 1)[1].strip() if " " in text else ""
    q = motor._nur_ca(text)
    if not q:
        return False
    chat = msg.get("chat") or {}
    cid = chat.get("id")
    jetzt = time.time()
    liste = [x for x in _ZEIT.get(cid, []) if jetzt - x < 3600]
    if len(liste) >= int(os.getenv("GROUP_SCAN_PER_HOUR", "60")):
        return True
    schon = _LETZTE.get((cid, q.split(":")[-1].lower()))
    if schon and jetzt - schon < 120:
        return True
    _ZEIT[cid] = liste + [jetzt]
    _LETZTE[(cid, q.split(":")[-1].lower())] = jetzt
    threading.Thread(target=_scannen, args=(cid, msg.get("message_id"), msg.get("message_thread_id"), q), daemon=True).start()
    return True


def _scannen(cid, antwort_auf, thema, q):
    import agent
    import scan
    try:
        agent._tg("sendChatAction", chat_id=cid, action="typing", **({"message_thread_id": thema} if thema else {}))
        r = scan.analyse(q)
        if not r.get("ok", True) or r.get("error"):
            return
        p = {"chat_id": cid, "text": r["card_html"], "parse_mode": "HTML", "disable_web_page_preview": True,
             "reply_markup": {"inline_keyboard": agent.knopfreihen(knoepfe(r))}}
        if antwort_auf:
            p["reply_parameters"] = {"message_id": antwort_auf, "allow_sending_without_reply": True}
        if thema:
            p["message_thread_id"] = thema
        d = agent._tg("sendMessage", **p) or {}
        mid = ((d.get("result") or {}).get("message_id"))
        try:
            import aktiv
            aktiv.merken(f"group:{cid}", "scan", f"{r.get('chain')}:{r.get('address')} ${r.get('symbol') or ''}")
        except Exception:
            pass
        if not mid or (r.get("deep") or {}).get("state") != "running":
            return
        # Tiefen-Scan abwarten und dieselbe Nachricht ergaenzen
        for _ in range(24):
            time.sleep(5)
            r2 = scan.analyse(f"{r['chain']}:{r['address']}", deep=False, frisch=True)
            if (r2.get("deep") or {}).get("state") == "ready":
                agent._tg("editMessageText", chat_id=cid, message_id=mid, text=r2["card_html"], parse_mode="HTML",
                          disable_web_page_preview=True, reply_markup={"inline_keyboard": agent.knopfreihen(knoepfe(r2))})
                return
    except Exception as e:
        log.warning(f"gruppe scan: {e}")
