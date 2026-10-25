"""
agent.py: Your Engine. Seit 2.20.

Jeder Holder bekommt im Telegram-Bot seinen eigenen Agenten. Der Agent
verwaltet KEIN Geld und KEIN Wallet. Er weiss nur, welches Wallet zu welchem
Telegram-Nutzer gehoert (bewiesen per Unterschrift in der App), und:

  1. laesst Holder in die private Gruppe und nimmt Verkaeufer wieder raus,
  2. schickt jedem die Calls, die zu seinen eigenen Regeln passen, mit dem
     Split fuer seinen eigenen Einsatz,
  3. fuehrt fuer jeden Buch, wie seine Picks ausgegangen sind,
  4. schickt sonntags eine Wochenbilanz und schlaegt bessere Regeln vor,
  5. postet (falls eingeschaltet) taeglich, was die Engine an sich selbst
     korrigiert hat.

Verknuepfen:
  App: Verify -> "Link Telegram" -> POST /v1/holder/telegram liefert
  t.me/<bot>?start=<code>. Der Bot liest /start <code> und merkt sich
  Telegram-ID <-> Wallet. Der Code gilt 30 Minuten und nur einmal.

Speicher: alles im kv-Speicher (Postgres), ein Eintrag je Nutzer.

Schalter (Railway, Service web UND worker):
  AGENT_ENABLED   true (Standard) | false
  LEARN_POST      preview (Standard: nur an den Betreiber) | on | off
  TG_BOT_USERNAME optional, sonst per getMe
"""
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import requests

import features
import holder
import storage
from config import cfg
from logger import log

API = "https://api.telegram.org/bot{token}/{method}"
POLY = "https://polymarket.com/event"

AN = os.getenv("AGENT_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")
LERN_POST = os.getenv("LEARN_POST", "preview").strip().lower()
SAFE_COV = float(os.getenv("AGENT_SAFE_COVERAGE", "0.72"))
CHECK_MIN = int(os.getenv("AGENT_CHECK_MIN", "30"))
CODE_MIN = 30
MAX_PICKS = 300
STANDARD = {"stake": 10.0, "cities": None, "risk": "all", "tz": 0, "quiet": None, "on": True,
            "reads": False}
_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ── Telegram ─────────────────────────────────────────────────────────────

def _tg(method, **p):
    if not cfg.tg_token:
        return None
    try:
        r = requests.post(API.format(token=cfg.tg_token, method=method), json=p, timeout=15)
        return r.json()
    except Exception as e:
        log.debug(f"agent tg {method}: {e}")
        return None


def _clean(text):
    return (text.replace("—", ",").replace("–", "-"))


def _send(chat, text, buttons=None, silent=False, html=False):
    p = {"chat_id": chat, "text": _clean(text), "disable_web_page_preview": True}
    if html:
        p["parse_mode"] = "HTML"
    if silent:
        p["disable_notification"] = True
    if buttons:
        p["reply_markup"] = {"inline_keyboard": knopfreihen(buttons, privat=str(chat).lstrip("-").isdigit() and not str(chat).startswith("-"))}
    r = _tg("sendMessage", **p)
    return bool(r and r.get("ok"))


def knopfreihen(buttons, privat=False):
    """[(text, url)] -> eine Reihe je Knopf. [[(text, url), ...], ...] -> Reihen wie gegeben.
    Ein Ziel 'cb:...' wird ein Rueckruf-Knopf (callback_data), sonst ein Link.
    privat=True (Chat mit dem Bot): Links auf den Live-Report oeffnen als Mini App direkt in Telegram (seit 2.44)."""
    seite = os.getenv("ENGINE_PUBLIC_URL", "https://engine.oddsengine.live").rstrip("/")

    def k(t, u):
        u = str(u or "")
        if u.startswith("cb:"):
            return {"text": t, "callback_data": u[3:64 + 3]}
        if privat and u.startswith(seite) and "#token=" in u and os.getenv("TG_REPORT_MINIAPP", "1") != "0":
            return {"text": t, "web_app": {"url": u}}
        return {"text": t, "url": u}
    reihen = []
    for b in buttons or []:
        if isinstance(b, (list, tuple)) and b and isinstance(b[0], (list, tuple)):
            reihen.append([k(t, u) for t, u in b])
        elif isinstance(b, (list, tuple)) and len(b) == 2:
            reihen.append([k(b[0], b[1])])
    return reihen


_BOT = {"name": None}


def bot_name():
    if _BOT["name"]:
        return _BOT["name"]
    n = os.getenv("TG_BOT_USERNAME", "").strip().lstrip("@")
    if not n:
        r = _tg("getMe")
        if r and r.get("ok"):
            n = r["result"].get("username") or ""
    _BOT["name"] = n or "OddsEnginePublicBot"
    return _BOT["name"]


# ── Speicher ─────────────────────────────────────────────────────────────

def _ids():
    try:
        return [str(x) for x in json.loads(storage.kv_get("agent_users") or "[]")]
    except Exception:
        return []


def _ids_setzen(ids):
    storage.kv_set("agent_users", json.dumps(sorted(set(ids))))


def laden(tg):
    try:
        raw = storage.kv_get("agent_u_" + str(tg))
        if not raw:
            return None
        u = json.loads(raw)
        r = dict(STANDARD)
        r.update(u.get("rules") or {})
        u["rules"] = r
        u.setdefault("picks", [])
        return u
    except Exception:
        return None


def speichern(u):
    storage.kv_set("agent_u_" + str(u["tg"]), json.dumps(u, default=str))
    ids = _ids()
    if str(u["tg"]) not in ids:
        ids.append(str(u["tg"]))
        _ids_setzen(ids)


def alle():
    for tg in _ids():
        u = laden(tg)
        if u:
            yield u


# ── Verknuepfen ──────────────────────────────────────────────────────────

def link_code(adresse):
    """Einmal-Code fuer t.me/<bot>?start=<code>. None ohne Datenbank."""
    code = secrets.token_hex(8)
    ok = storage.kv_set("agent_code_" + code,
                        json.dumps({"a": holder.norm(adresse), "bis": time.time() + CODE_MIN * 60}))
    if not ok:
        return None
    return code, f"https://t.me/{bot_name()}?start={code}"


def _stufe(adr, frisch=False):
    try:
        st = holder.status(adr, frisch=frisch)
        return st.get("tier") or "public", st
    except Exception as e:
        log.debug(f"agent stufe {adr}: {e}")
        return None, {}


def ist_holder(t):
    return holder.RANG.get(t or "public", 0) >= holder.RANG["holder"]


def _im_gruppe(tg):
    if not cfg.tg_paid:
        return False
    r = _tg("getChatMember", chat_id=cfg.tg_paid, user_id=int(tg))
    if not (r and r.get("ok")):
        return False
    return r["result"].get("status") in ("member", "administrator", "creator", "restricted")


def _einladung(u):
    if not cfg.tg_paid:
        return None
    r = _tg("createChatInviteLink", chat_id=cfg.tg_paid, member_limit=1,
            expire_date=int(time.time()) + 3600, name=("engine " + str(u["tg"]))[:32])
    if r and r.get("ok"):
        return r["result"]["invite_link"]
    log.warning(f"agent: Einladung fehlgeschlagen: {str(r)[:160]}")
    return None


def _entfernen(u):
    if not cfg.tg_paid or str(u["tg"]) == str(cfg.tg_private):
        return False
    r = _tg("banChatMember", chat_id=cfg.tg_paid, user_id=int(u["tg"]),
            until_date=int(time.time()) + 60)
    _tg("unbanChatMember", chat_id=cfg.tg_paid, user_id=int(u["tg"]), only_if_banned=True)
    return bool(r and r.get("ok"))


def _binden(chat_id, von, code):
    raw = storage.kv_get("agent_code_" + code)
    d = None
    try:
        d = json.loads(raw) if raw else None
    except Exception:
        d = None
    if not d or d.get("bis", 0) < time.time():
        _send(chat_id, "This link has expired. Open public.oddsengine.live, "
                       "verify your wallet and tap Link Telegram again.")
        return
    storage.kv_set("agent_code_" + code, "")
    adr = d["a"]

    # Ein Wallet gehoert genau einem Telegram-Konto.
    for alt in alle():
        if alt.get("wallet") == adr and str(alt["tg"]) != str(chat_id):
            alt["wallet"] = None
            speichern(alt)
            if _im_gruppe(alt["tg"]):
                _entfernen(alt)
            _send(alt["tg"], "Your wallet was just linked to another Telegram account, "
                             "so it was unlinked here.")

    u = laden(chat_id) or {"tg": str(chat_id), "rules": dict(STANDARD), "picks": []}
    u["wallet"] = adr
    u["name"] = von.get("first_name") or ""
    u["handle"] = von.get("username") or ""
    u["linked"] = datetime.now(timezone.utc).isoformat()
    t, st = _stufe(adr, frisch=True)
    u["tier"] = t or "public"
    speichern(u)

    kurz = adr[:6] + "..." + adr[-4:]
    # Seit 2.36.2: Your Engine (motor) haengt am Guthaben, nicht an der Holder-Stufe.
    try:
        import motor
        if motor.offen(adr):
            b = motor.bereit(adr)
            if b.get("ready"):
                geld = f"Your balance: ${b.get('credit', 0):,.2f}. Every answer comes off it, you see the cost on engine.oddsengine.live."
            elif b.get("trial_left"):
                geld = (f"You have {b['trial_left']} free messages to try me. After that you top up with $ODDS "
                        "on engine.oddsengine.live, no subscription.")
            else:
                geld = "Top up with $ODDS on engine.oddsengine.live to start, no subscription."
            link = _einladung(u) if ist_holder(u["tier"]) else None
            _send(chat_id, f"Linked to {kurz}. This is Your Engine.\n\n"
                           "Just write to me like you would to a person, or send a voice note. Try:\n"
                           "what are today's calls\n"
                           "what's the weather where I live\n"
                           "make me an image of a green robot\n"
                           "tell me when crowd and engine split on BTC\n\n"
                           f"{geld}\n\nLet's set you up in two quick steps. First: what should I call you?",
                  [("Join OddsEngine Holders", link)] if link else None)
            return
    except Exception as e:
        log.warning(f"agent binden motor: {e}")
    # Seit 2.36.3: kein Holder-Gate mehr. Your Engine haengt nur am Guthaben.
    link = _einladung(u) if ist_holder(u["tier"]) else None
    _send(chat_id, f"Linked to {kurz}.\n\n"
                   "Your Engine opens here very soon. You will hear it from me first. "
                   "It runs on balance: top up with $ODDS on engine.oddsengine.live, "
                   "every message shows what it cost.",
          [("Join OddsEngine Holders", link)] if link else None)


# ── Regeln ───────────────────────────────────────────────────────────────

def _regeln_text(u):
    r = u["rules"]
    staedte = ", ".join(c.title() for c in r["cities"]) if r.get("cities") else "all cities"
    risiko = (f"safe only (engine at {SAFE_COV*100:.0f}% or more)"
              if r.get("risk") == "safe" else "every call")
    ruhe = (f"{r['quiet'][0]:02d}:00 to {r['quiet'][1]:02d}:00 (UTC{r['tz']:+d}), silent"
            if r.get("quiet") else "off")
    status_ = "on" if r.get("on", True) else "paused"
    lesen = "on, you also get READ calls (no bet)" if r.get("reads") else "off, bets only"
    return ("Your rules\n"
            f"Stake per call: ${r['stake']:,.2f}\n"
            f"Cities: {staedte}\n"
            f"Risk: {risiko}\n"
            f"Quiet hours: {ruhe}\n"
            f"Reads: {lesen}\n"
            f"Status: {status_}\n\n"
            "Change them anytime:\n"
            "/stake 25\n"
            "/cities paris, london, seoul  (or /cities all)\n"
            "/risk safe  (or /risk all)\n"
            "/quiet 23-7  (or /quiet off)   /tz +2\n"
            "/reads on  (or /reads off)\n"
            "/pause   /resume   /me")


def _norm_stadt(s):
    return re.sub(r"\s+", " ", s.replace("_", " ").strip().lower())


HILFE = ("Your Engine\n\n"
         "/balance  your Your Engine balance and last top ups\n"
         "/cost on|off  show the cost under every answer\n"
         "/news on|off  OddsEngine news from the team\n"
         "/voice on|off  answer voice notes with a voice note\n"
         "/language German  the language for everything I send\n"
         "/me  your rules and record\n"
         "/stake 25  stake per call in dollars\n"
         "/cities paris, seoul  or  /cities all\n"
         "/drop_seoul  /add_paris\n"
         "/risk safe  or  /risk all\n"
         "/quiet 23-7  or  /quiet off\n"
         "/tz +2  your time zone for quiet hours\n"
         "/reads on  also send READ calls (no bet, just the engine's view)\n"
         "/pause  /resume\n"
         "/join  a fresh invite to the holder group\n"
         "/unlink  forget my wallet\n\n"
         "Anything else you write goes to a human.")


def befehl(chat_id, von, text):
    """Private Nachricht an den Bot. True, wenn hier erledigt."""
    if not AN:
        return False
    text = (text or "").strip()
    if not text.startswith("/"):
        return False
    roh = text.split(None, 1)
    cmd = roh[0].split("@")[0].lower()
    arg = roh[1].strip() if len(roh) > 1 else ""

    if cmd == "/start":
        if arg and re.fullmatch(r"[0-9a-f]{16}", arg):
            _binden(chat_id, von, arg)
            return True
        u = laden(chat_id)
        if u and u.get("wallet"):
            try:
                import motor
                if motor.offen(u["wallet"]):
                    _send(chat_id, "Your Engine is here. Just write to me or send a voice note.")
                    return True
            except Exception:
                pass
            _send(chat_id, "Your Engine is running.\n\n" + _me_text(u))
            return True
        _send(chat_id, (
            "OddsEngine.\n\n"
            "The engine reads the weather for about 50 cities every day and calls "
            "the Polymarket temperature markets. Each call says if it is a BET or just a READ. Every call "
            "is posted before the station reports and scored after. Hits and "
            "misses, all of them.\n\n"
            "Free: one call a day and every result on @OddsEngine.\n\n"
            "Your Engine: your own AI right here. Calls, prices, alerts, reminders, "
            "email and calendar. Top up with $ODDS, pay per message, no subscription.\n\n"
            "To start: open the app, verify your wallet, tap Link Telegram."),
            [("Open the app", "https://public.oddsengine.live"),
             ("How it works", "https://docs.oddsengine.live")])
        return True

    if cmd in ("/help", "/commands"):
        _send(chat_id, HILFE)
        return True

    u = laden(chat_id)
    if not u or not u.get("wallet"):
        _send(chat_id, "Link your wallet first: open the app, verify, tap Link Telegram.",
              [("Open the app", "https://public.oddsengine.live")])
        return True
    r = u["rules"]

    # Solange Your Engine nicht freigeschaltet ist, gehen nur Konto und
    # Gruppe. Die Regeln selbst warten auf den Schalter.
    if cmd in ("/balance", "/credit", "/guthaben"):
        _send(chat_id, _guthaben_text(u["wallet"]),
              [("Top up", "https://engine.oddsengine.live")])
        return True

    if cmd == "/news":
        try:
            import motor
            an = arg.lower() not in ("off", "aus", "0")
            motor.set_official_news(u["wallet"], an)
            _send(chat_id, "OddsEngine news on. What the team posts reaches you here." if an
                  else "OddsEngine news off. /news on brings it back.")
        except Exception as e:
            log.warning(f"agent news: {e}")
            _send(chat_id, "Could not change that right now.")
        return True

    if cmd == "/voice":
        try:
            import motor
            if not motor._schalter("voice", u["wallet"]):
                _send(chat_id, "Voice replies are not open yet.")
                return True
            an = arg.lower() not in ("off", "aus", "0")
            r = motor.set_voice_replies(u["wallet"], an)
            _send(chat_id, ("Voice replies on. Send me a voice note and I answer with one, the text comes below it."
                            + ("" if r.get("ready") else " (The voice service is still being set up, text answers until then.)"))
                  if an else "Voice replies off. /voice on brings them back.")
        except Exception as e:
            log.warning(f"agent voice: {e}")
            _send(chat_id, "Could not change that right now.")
        return True

    if cmd in ("/smart", "/calls"):
        try:
            import motor
            if not motor._schalter("smart_calls", u["wallet"]):
                _send(chat_id, "Smart money calls are not open yet.")
                return True
            an = arg.lower() not in ("off", "aus", "0")
            motor.set_smart_calls(u["wallet"], an)
            _send(chat_id, "Smart money calls on. When several wallets we rate highly buy the same coin, you get the report here. /smart off stops them."
                  if an else "Smart money calls off. /smart on brings them back.")
        except Exception as e:
            log.warning(f"agent smart: {e}")
            _send(chat_id, "Could not change that right now.")
        return True

    if cmd in ("/language", "/lang", "/sprache"):
        try:
            import motor
            if not motor._schalter("lang", u["wallet"]):
                _send(chat_id, "I answer in the language you write in.")
                return True
            if not arg:
                _send(chat_id, "Which language? For example /language German, or /language auto to follow how you write.")
                return True
            r = motor.set_language(u["wallet"], arg)
            _send(chat_id, f"Done. Everything from me is now in {r.get('language')}." if r.get("ok") else r.get("error") or "Which language?")
        except Exception as e:
            log.warning(f"agent language: {e}")
            _send(chat_id, "Could not change that right now.")
        return True

    if cmd == "/cost":
        try:
            import motor
            an = arg.lower() not in ("off", "aus", "0")
            motor.kostenzeile_setzen(u["wallet"], an)
            _send(chat_id, "Every answer now ends with what it cost and your balance." if an
                  else "Cost line off. /balance shows your balance anytime, /cost on brings it back.")
        except Exception as e:
            log.warning(f"agent cost: {e}")
            _send(chat_id, "Could not change that right now.")
        return True

    if not features.an("agent") and cmd not in ("/me", "/rules", "/status", "/join", "/unlink"):
        _send(chat_id, "Your Engine switches on soon. Your wallet is linked and "
                       "you keep your place in the holder group. /join for a fresh invite.")
        return True

    if cmd in ("/me", "/rules", "/status"):
        t, _ = _stufe(u["wallet"], frisch=True)
        if t:
            u["tier"] = t
            speichern(u)
        _send(chat_id, _me_text(u))
        return True

    if cmd == "/stake":
        try:
            v = float(arg.replace("$", "").replace(",", "."))
            if not 1 <= v <= 100000:
                raise ValueError
            r["stake"] = round(v, 2)
            speichern(u)
            _send(chat_id, f"Stake set to ${v:,.2f} per call.")
        except Exception:
            _send(chat_id, "Write it like this: /stake 25")
        return True

    if cmd == "/cities":
        if not arg or arg.lower() in ("all", "alle", "any"):
            r["cities"] = None
            speichern(u)
            _send(chat_id, "You get calls for all cities.")
        else:
            liste = [_norm_stadt(x) for x in re.split(r"[,;]", arg) if _norm_stadt(x)]
            r["cities"] = sorted(set(liste))
            speichern(u)
            _send(chat_id, "You get calls for: " + ", ".join(c.title() for c in r["cities"]))
        return True

    m = re.fullmatch(r"/(drop|add)_?(.*)", cmd + ("_" + arg if arg else ""))
    if m and cmd.startswith(("/drop", "/add")):
        stadt = _norm_stadt(m.group(2))
        if not stadt:
            _send(chat_id, "Write it like this: /drop_seoul or /add_paris")
            return True
        if m.group(1) == "drop":
            if r.get("cities"):
                r["cities"] = [c for c in r["cities"] if c != stadt]
            else:
                r["blocked"] = sorted(set((r.get("blocked") or []) + [stadt]))
            speichern(u)
            _send(chat_id, f"{stadt.title()} dropped. You will not get its calls anymore.")
        else:
            r["blocked"] = [c for c in (r.get("blocked") or []) if c != stadt]
            if r.get("cities") is not None:
                r["cities"] = sorted(set(r["cities"] + [stadt]))
            speichern(u)
            _send(chat_id, f"{stadt.title()} is in.")
        return True

    if cmd == "/risk":
        a = arg.lower()
        if a in ("safe", "all"):
            r["risk"] = a
            speichern(u)
            _send(chat_id, "Only the calls the engine is most sure about."
                  if a == "safe" else "Every call that fits your cities.")
        else:
            _send(chat_id, "Write /risk safe or /risk all")
        return True

    if cmd == "/reads":
        a = arg.lower()
        if a in ("on", "off"):
            r["reads"] = a == "on"
            speichern(u)
            _send(chat_id, "You also get READ calls now. They come without a stake: "
                  "the engine's view, no bet." if r["reads"] else
                  "Bets only. READ calls stay in the app and the group.")
        else:
            _send(chat_id, "Write /reads on or /reads off")
        return True

    if cmd == "/tz":
        try:
            v = int(arg.replace("UTC", "").replace("utc", "").replace("+", "") or "x")
            if not -12 <= v <= 14:
                raise ValueError
            r["tz"] = v
            speichern(u)
            _send(chat_id, f"Time zone set to UTC{v:+d}.")
        except Exception:
            _send(chat_id, "Write it like this: /tz +2 (Germany in summer), /tz -5 (New York)")
        return True

    if cmd == "/quiet":
        if arg.lower() in ("off", "aus", ""):
            r["quiet"] = None
            speichern(u)
            _send(chat_id, "Quiet hours off.")
            return True
        mm = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", arg)
        if mm and 0 <= int(mm.group(1)) <= 23 and 0 <= int(mm.group(2)) <= 23:
            r["quiet"] = [int(mm.group(1)), int(mm.group(2))]
            speichern(u)
            _send(chat_id, f"Between {r['quiet'][0]:02d}:00 and {r['quiet'][1]:02d}:00 "
                           f"(UTC{r['tz']:+d}) calls arrive without a sound. Set your zone with /tz.")
        else:
            _send(chat_id, "Write it like this: /quiet 23-7")
        return True

    if cmd in ("/pause", "/resume"):
        r["on"] = cmd == "/resume"
        speichern(u)
        _send(chat_id, "Your Engine is back on." if r["on"] else
              "Paused. No calls until you write /resume.")
        return True

    if cmd == "/join":
        t, _ = _stufe(u["wallet"], frisch=True)
        u["tier"] = t or u.get("tier")
        speichern(u)
        if not ist_holder(u["tier"]):
            _send(chat_id, "The holder group needs $100 in $ODDS. /me shows where you stand.")
        elif _im_gruppe(chat_id):
            _send(chat_id, "You are already in the holder group.")
        else:
            link = _einladung(u)
            _send(chat_id, "Here is your personal invite. It works once and for one hour.",
                  [("Join OddsEngine Holders", link)] if link else None)
        return True

    if cmd == "/unlink":
        if _im_gruppe(chat_id):
            _entfernen(u)
        u["wallet"] = None
        speichern(u)
        _send(chat_id, "Unlinked. Your wallet is forgotten here. Link again from the app anytime.")
        return True

    _send(chat_id, HILFE)
    return True


# ── Bilanz ───────────────────────────────────────────────────────────────

def _bilanz(picks):
    fertig = [p for p in picks if p.get("hit") is not None]
    n = len(fertig)
    h = sum(1 for p in fertig if p.get("hit"))
    pnl = round(sum(p.get("pnl") or 0 for p in fertig), 2)
    einsatz = sum(p.get("stake") or 0 for p in fertig)
    return n, h, pnl, einsatz


def _geld(v):
    return ("+" if v >= 0 else "-") + f"${abs(v):,.2f}"


def _me_text(u):
    picks = u.get("picks") or []
    n, h, pnl, einsatz = _bilanz(picks)
    offen = sum(1 for p in picks if p.get("hit") is None)
    adr = u.get("wallet") or ""
    zeilen = [f"Wallet {adr[:6]}...{adr[-4:]}", ""]
    if n:
        roi = (pnl / einsatz * 100) if einsatz else 0
        zeilen.append(f"Your record: {n} pick{'s' if n != 1 else ''}, {h} hit{'s' if h != 1 else ''}, {_geld(pnl)} ({roi:+.0f}%)")
    else:
        zeilen.append("Your record: no settled picks yet.")
    if offen:
        zeilen.append(f"Open right now: {offen}")
    if features.an("agent"):
        zeilen += ["", _regeln_text(u)]
    else:
        zeilen += ["", "Your Engine switches on soon. /join for a fresh group invite."]
    try:
        zeilen += ["", _guthaben_text(adr, kurz=True)]
    except Exception:
        pass
    return "\n".join(zeilen)


def _guthaben_text(adr, kurz=False):
    """Das echte Guthaben des Engine-Kontos (dasselbe wie auf engine.oddsengine.live)."""
    import motor
    k = motor.mein_konto(adr)
    if k.get("error"):
        return "Balance: not available right now."
    z = [f"Your Engine balance: ${k.get('balance_usd', 0):,.2f}"]
    if k.get("free_trial_messages_left"):
        z.append(f"Free messages left: {k['free_trial_messages_left']}")
    if kurz:
        return "\n".join(z)
    if k.get("spent_usd") is not None:
        z.append(f"Spent so far: ${k['spent_usd']:,.2f}")
    tops = k.get("recent_top_ups") or []
    if tops:
        z += ["", "Last top ups"]
        for t in tops[:5]:
            z.append(f"{str(t.get('when') or '')[:10]}  ${float(t.get('usd') or 0):,.2f}  ({float(t.get('odds') or 0):,.0f} $ODDS)")
    z += ["", "Top up with $ODDS on engine.oddsengine.live. No subscription, you pay per message."]
    return "\n".join(z)


# ── Calls ────────────────────────────────────────────────────────────────

def _tag(datum):
    try:
        d = datetime.strptime(str(datum)[:10], "%Y-%m-%d")
        return f"{_MON[d.month - 1]} {d.day}"
    except Exception:
        return str(datum or "")


def _passt(u, snap):
    r = u["rules"]
    stadt = _norm_stadt(str(snap.get("city") or ""))
    if r.get("cities") and stadt not in r["cities"]:
        return False
    if stadt in (r.get("blocked") or []):
        return False
    if r.get("risk") == "safe" and (snap.get("coverage") or 0) < SAFE_COV:
        return False
    if snap.get("grade", "bet") != "bet" and not r.get("reads"):
        return False
    return True


def _ruhig(u):
    q = u["rules"].get("quiet")
    if not q:
        return False
    h = (datetime.now(timezone.utc) + timedelta(hours=u["rules"].get("tz") or 0)).hour
    a, b = q
    return (a <= h < b) if a < b else (h >= a or h < b)


def _lesen(u, snap):
    """READ: die Sicht der Engine, ohne Einsatz. Seit 2.20.1."""
    legs = list(snap.get("legs") or [])
    kosten = snap.get("cost") or 0
    z = ["Engine read. No bet.", "",
         f"{(snap.get('city') or '').title()}  ·  {_tag(snap.get('date'))}  ·  daily high",
         f"Block {' / '.join(legs)} at {kosten*100:.0f}c",
         f"Market {kosten*100:.0f}%  ·  model {(snap.get('coverage_engine') or 0)*100:.0f}%  ·  "
         f"honest {(snap.get('coverage') or 0)*100:.0f}%", "",
         "The price already holds what the model sees, so there is no stake here. "
         "Not tracked in your record. /reads off to stop these."]
    ev = snap.get("event_slug") or ""
    btns = [("Open market", f"{POLY}/{ev}")] if ev else []
    return "\n".join(z), btns


def _call(u, snap):
    if snap.get("grade", "bet") != "bet":
        return _lesen(u, snap)
    einsatz = float(u["rules"]["stake"])
    legs = list(snap.get("legs") or [])
    stakes = list(snap.get("stakes") or [])
    slugs = list(snap.get("market_slugs") or [])
    tot = sum(stakes) or 0
    teile = ([s / tot * einsatz for s in stakes] if tot and len(stakes) == len(legs)
             else [einsatz / max(len(legs), 1)] * len(legs))
    kosten = snap.get("cost") or 0
    zahlt = einsatz / kosten if kosten else None
    kopf = ("Your Engine updated a call." if snap.get("update")
            else "Your Engine found one for you.")
    z = [kopf, "",
         f"{(snap.get('city') or '').title()}  ·  {_tag(snap.get('date'))}  ·  daily high",
         f"Block {' / '.join(legs)} at {kosten*100:.0f}c",
         f"Honest odds {(snap.get('coverage') or 0)*100:.0f}%  ·  price {kosten*100:.0f}c", "",
         f"Your ${einsatz:,.2f} split:"]
    for lab, w in zip(legs, teile):
        z.append(f"{lab}  ${w:,.2f}")
    if zahlt:
        z.append(f"Pays about ${zahlt:,.2f} if the high lands in the block.")
    z += ["", "Tracked in your record either way. /me for your stats."]
    ev = snap.get("event_slug") or ""
    btns = []
    for lab, sl, w in zip(legs, slugs, teile):
        if ev and sl:
            btns.append((f"{lab}  ·  ${w:,.2f}", f"{POLY}/{ev}/{sl}"))
    if ev:
        btns.append(("Open full market", f"{POLY}/{ev}"))
    return "\n".join(z), btns


def on_signal(snap):
    """Vom worker gerufen, sobald ein Call veroeffentlicht ist."""
    if not AN or not cfg.tg_token or not snap.get("slug"):
        return
    if not features.an("agent"):
        return
    n = 0
    for u in alle():
        try:
            if not u.get("wallet") or not u["rules"].get("on", True):
                continue
            if not ist_holder(u.get("tier")) or not _passt(u, snap):
                continue
            text, btns = _call(u, snap)
            if not _send(u["tg"], text, btns, silent=_ruhig(u)):
                continue
            if snap.get("grade", "bet") != "bet":
                n += 1
                continue
            if True:
                u["picks"] = [p for p in u["picks"] if p.get("slug") != snap["slug"]]
                u["picks"].append({"slug": snap["slug"], "city": snap.get("city"),
                                   "date": snap.get("date"), "legs": snap.get("legs"),
                                   "cost": snap.get("cost"), "stake": u["rules"]["stake"],
                                   "ts": datetime.now(timezone.utc).isoformat(),
                                   "hit": None, "pnl": None})
                u["picks"] = u["picks"][-MAX_PICKS:]
                speichern(u)
                n += 1
            time.sleep(0.05)
        except Exception as e:
            log.warning(f"agent on_signal {u.get('tg')}: {e}")
    if n:
        log.info(f"agent: {snap.get('city')} an {n} Holder geschickt")
        try:
            import feed
            feed.add(f"Your Engine sent {feed.stadt(snap.get('city'))} to {n} "
                     f"holder{'s' if n != 1 else ''} whose rules it fits.", "agent")
        except Exception:
            pass


# ── Takt: Ergebnisse, Mitgliedschaft, Wochenbilanz ───────────────────────

def _ergebnisse():
    res = {}
    for r in storage.load_all():
        if r.get("kind") == "resolution" and r.get("outcome_known") and r.get("slug"):
            res[r["slug"]] = r
    for u in alle():
        neu = []
        for p in u["picks"]:
            if p.get("hit") is not None or p.get("slug") not in res:
                continue
            r = res[p["slug"]]
            hit = bool(r.get("outcome_hit"))
            pct = r.get("pnl_pct")
            st = float(p.get("stake") or 0)
            if isinstance(pct, (int, float)):
                pnl = round(st * pct / 100, 2)
            elif hit and p.get("cost"):
                pnl = round(st * (1 / p["cost"] - 1), 2)
            else:
                pnl = -st
            p.update(hit=hit, pnl=pnl, label=r.get("outcome_label"))
            neu.append(p)
        if not neu:
            continue
        speichern(u)
        for p in neu:
            z = (f"{'✅ HIT' if p['hit'] else '❌ MISS'}  ·  {(p.get('city') or '').title()} "
                 f"{_tag(p.get('date'))}\n"
                 f"The high came in at {p.get('label') or '?'}. "
                 f"You had {' / '.join(p.get('legs') or [])}.\n"
                 f"Your ${p.get('stake'):,.2f}: {_geld(p['pnl'])}")
            _send(u["tg"], z, silent=_ruhig(u))
            time.sleep(0.05)


def _mitgliedschaft():
    jetzt = time.time()
    try:
        zuletzt = float(storage.kv_get("agent_check_ts") or 0)
    except Exception:
        zuletzt = 0
    if jetzt - zuletzt < CHECK_MIN * 60:
        return
    storage.kv_set("agent_check_ts", jetzt)
    for u in alle():
        if not u.get("wallet"):
            continue
        t, st = _stufe(u["wallet"], frisch=True)
        # Bei Kettenfehlern oder fehlendem Kurs nie jemanden rauswerfen.
        if not t or (st.get("error") and not st.get("note")):
            continue
        alt = u.get("tier")
        u["tier"] = t
        speichern(u)
        if ist_holder(alt) and not ist_holder(t):
            if _im_gruppe(u["tg"]):
                _entfernen(u)
            _send(u["tg"], "Your $ODDS dropped below the holder line, so you left the holder "
                           "group. Your Engine keeps running on your balance. /join to come back.")
        elif not ist_holder(alt) and ist_holder(t):
            link = None if _im_gruppe(u["tg"]) else _einladung(u)
            _send(u["tg"], "You are a holder now. Here is your invite to the holder group.",
                  [("Join OddsEngine Holders", link)] if link else None)
        time.sleep(0.1)


def _woche():
    if not features.an("agent"):
        return
    jetzt = datetime.now(timezone.utc)
    if jetzt.weekday() != 6 or jetzt.hour < 12:
        return
    kw = jetzt.strftime("%G-%V")
    if storage.kv_get("agent_week") == kw:
        return
    storage.kv_set("agent_week", kw)
    seit7 = (jetzt - timedelta(days=7)).isoformat()
    seit30 = (jetzt - timedelta(days=30)).isoformat()
    for u in alle():
        if not u.get("wallet"):
            continue
        woche = [p for p in u["picks"] if str(p.get("ts")) >= seit7 and p.get("hit") is not None]
        if not woche:
            continue
        n, h, pnl, _ = _bilanz(woche)
        z = [f"Your Engine, week {jetzt.strftime('%V')}", "",
             f"{n} pick{'s' if n != 1 else ''}, {h} hit{'s' if h != 1 else ''}, {_geld(pnl)} on your ${u['rules']['stake']:,.0f} stakes."]
        je = {}
        for p in u["picks"]:
            if str(p.get("ts")) >= seit30 and p.get("hit") is not None:
                c = _norm_stadt(str(p.get("city") or ""))
                a = je.setdefault(c, [0, 0])
                a[0] += 1
                a[1] += 1 if p.get("hit") else 0
        gut = sorted(((v[1] / v[0], v[0], c) for c, v in je.items() if v[0] >= 2), reverse=True)
        if gut and gut[0][0] >= 0.6:
            q, m, c = gut[0]
            z.append(f"Best city this month: {c.title()}, {int(q*m+0.5)} of {m}.")
        schlecht = [(c, v) for c, v in je.items() if v[0] >= 3 and v[1] / v[0] <= 0.34]
        for c, v in schlecht[:2]:
            z.append(f"{c.title()} hit only {v[1]} of {v[0]} for you this month. "
                     f"Tap /drop_{c.replace(' ', '_')} to stop getting it.")
        z += ["", "/me for your full record."]
        _send(u["tg"], "\n".join(z))
        time.sleep(0.1)


def _freigabe():
    """Einmal, wenn der Betreiber Your Engine freischaltet: allen verknuepften
    Holdern Bescheid geben. Seit 2.21."""
    if not features.an("agent") or storage.kv_get("agent_released") == "1":
        return
    n = 0
    for u in alle():
        if u.get("wallet") and ist_holder(u.get("tier")):
            if _send(u["tg"], "Your Engine is live.\n\nFrom now on I send you the "
                              "calls that fit your rules, with the split for your own "
                              "stake, and keep score of every one.\n\n" + _regeln_text(u)):
                n += 1
            time.sleep(0.05)
    storage.kv_set("agent_released", "1")
    log.info(f"agent: Freigabe an {n} Holder gemeldet")


def tick():
    """Am Ende jedes Scans vom worker gerufen."""
    if not AN or not cfg.tg_token:
        return
    for name, f in (("ergebnisse", _ergebnisse), ("mitgliedschaft", _mitgliedschaft),
                    ("woche", _woche), ("freigabe", _freigabe)):
        try:
            f()
        except Exception as e:
            log.warning(f"agent {name}: {e}")


# ── Die Engine lernt oeffentlich ─────────────────────────────────────────

def lern_text(alt, neu):
    """Was die Engine heute an sich korrigiert hat. Kurz, menschlich."""
    alt = alt or {}
    a_st = alt.get("staedte") or {}
    zeilen = []
    kand = []
    for stadt, s in (neu.get("staedte") or {}).items():
        if not s.get("aktiv"):
            continue
        vor = (a_st.get(stadt) or {})
        diff = abs(s.get("bias_c", 0) - (vor.get("bias_c", 0) if vor.get("aktiv") else 0))
        if diff >= 0.3:
            kand.append((abs(s.get("rest_c", 0)), stadt, s))
    for _, stadt, s in sorted(kand, reverse=True)[:3]:
        rest = s.get("rest_c", 0)
        richtung = "too warm" if rest < 0 else "too cold"
        zeilen.append(f"{stadt.title()}: I ran {abs(rest):.1f}°C {richtung} over the last "
                      f"{neu.get('tage', 21)} days. Correcting by {abs(s.get('bias_c', 0)):.1f}°C.")
    v_neu = (neu.get("vertrauen") or {})
    v_alt = (alt.get("vertrauen") or {})
    if isinstance(v_neu.get("k"), (int, float)) and v_neu.get("k_roh") is not None:
        k_a = v_alt.get("k")
        if k_a is None or abs(v_neu["k"] - k_a) >= 0.05:
            if v_neu["k"] < 0.05:
                zeilen.append(f"Checked my last {v_neu.get('n', 0)} calls against the prices. "
                              "The market called them as well as I did. So no BET until I "
                              "beat the price again, only READs.")
            elif k_a is None or v_neu["k"] > k_a:
                zeilen.append(f"My last {v_neu.get('n', 0)} calls added something the price "
                              "did not have. Trusting my model a bit more.")
            else:
                zeilen.append(f"My last {v_neu.get('n', 0)} calls were closer to the price "
                              "than to my model. Trusting the market more.")
    if not zeilen:
        n = sum(s.get("n", 0) for s in (neu.get("staedte") or {}).values())
        return ("What I learned today:\n"
                f"Checked {n} of my forecasts against the weather stations. "
                "Nothing needed fixing today.\n\n"
                "Every call and every miss: public.oddsengine.live")
    return ("What I learned today:\n\n" + "\n".join(zeilen) +
            "\n\nEvery call and every miss: public.oddsengine.live")


def lern_post(alt, neu):
    if LERN_POST == "off" or not neu:
        return
    text = lern_text(alt, neu)
    log.info("lern-post:\n" + text)
    if LERN_POST == "on":
        try:
            import telegram
            telegram.send_free(text)
        except Exception as e:
            log.warning(f"lern-post telegram: {e}")
        try:
            import x_post
            if x_post.bereit():
                kurz = text if len(text) <= 280 else text[:270].rsplit("\n", 1)[0]
                x_post.post(kurz)
        except Exception as e:
            log.warning(f"lern-post x: {e}")
    elif cfg.tg_private:
        _send(cfg.tg_private, "PREVIEW, not posted. Set LEARN_POST=on in Railway "
                              "(worker) to publish this every day.\n\n" + text)
