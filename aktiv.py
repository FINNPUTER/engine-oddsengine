"""
aktiv.py: was die Nutzer von Your Engine tun, fuer das Admin-Dashboard. Seit 2.40.

  merken(adr, art, detail, kosten)   ein Ereignis (chat, scan, research, watch, image, voice, trade)
  uebersicht()                       je Nutzer: Telegram, Guthaben, zuletzt aktiv, Nachrichten, Scans, Ausgaben
  feed(n)                            die letzten Ereignisse aller Nutzer
"""
import time

import storage
from logger import log

_S = [False]


def _pg():
    pg = storage._init_pg()
    if pg and not _S[0]:
        pg.execute("""CREATE TABLE IF NOT EXISTS eng_activity (
            id BIGSERIAL PRIMARY KEY, adr TEXT NOT NULL, ts BIGINT NOT NULL, art TEXT NOT NULL,
            detail TEXT, kosten DOUBLE PRECISION DEFAULT 0)""")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_eng_activity_adr_ts ON eng_activity(adr, ts DESC)")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_eng_activity_ts ON eng_activity(ts DESC)")
        _S[0] = True
    return pg


def merken(adr, art, detail=None, kosten=0.0):
    try:
        pg = _pg()
        if pg:
            pg.execute("INSERT INTO eng_activity (adr, ts, art, detail, kosten) VALUES (%s,%s,%s,%s,%s)",
                       (str(adr), int(time.time()), str(art)[:20], (str(detail) if detail else None) and str(detail)[:300], float(kosten or 0)))
    except Exception as e:
        log.debug(f"aktiv: {e}")


def uebersicht():
    pg = _pg()
    if not pg:
        return []
    jetzt = int(time.time())
    rows = pg.execute("""SELECT adr, MAX(ts),
        COUNT(*) FILTER (WHERE art='chat' AND ts >= %s), COUNT(*) FILTER (WHERE art='chat' AND ts >= %s),
        COUNT(*) FILTER (WHERE art='scan' AND ts >= %s), COUNT(*) FILTER (WHERE art='research' AND ts >= %s),
        COALESCE(SUM(kosten) FILTER (WHERE ts >= %s),0), COALESCE(SUM(kosten),0), MIN(ts)
        FROM eng_activity GROUP BY adr ORDER BY MAX(ts) DESC LIMIT 300""",
                      (jetzt - 86400, jetzt - 7 * 86400, jetzt - 7 * 86400, jetzt - 7 * 86400, jetzt - 7 * 86400)).fetchall()
    tg = {}
    try:
        import agent
        for u in agent.alle():
            if u.get("wallet"):
                tg[u["wallet"]] = u.get("username") or u.get("name") or str(u.get("tg") or "")
    except Exception:
        pass
    out = []
    for r in rows:
        try:
            import ai
            k = ai.konto(r[0], anlegen=False) or {}
        except Exception:
            k = {}
        out.append({"wallet": r[0], "telegram": tg.get(r[0]), "last_ts": r[1], "first_ts": r[8],
                    "chats_24h": r[2], "chats_7d": r[3], "scans_7d": r[4], "research_7d": r[5],
                    "spent_7d": round(r[6], 4), "spent_all": round(r[7], 4), "balance": round(k.get("credit", 0.0), 4) if k else None})
    return out


def feed(n=120, adr=None):
    pg = _pg()
    if not pg:
        return []
    if adr:
        rows = pg.execute("SELECT adr, ts, art, detail, kosten FROM eng_activity WHERE adr=%s ORDER BY ts DESC LIMIT %s", (adr, int(n))).fetchall()
    else:
        rows = pg.execute("SELECT adr, ts, art, detail, kosten FROM eng_activity ORDER BY ts DESC LIMIT %s", (int(n),)).fetchall()
    return [{"wallet": r[0], "ts": r[1], "kind": r[2], "detail": r[3], "cost": round(r[4] or 0, 5)} for r in rows]
