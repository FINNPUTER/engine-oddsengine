"""
kalender.py: Kalender fuer Your Engine. Seit 2.36.

Nur lesend, ueber die geheime iCal-Adresse, die Google, Apple und Outlook anbieten.
Kein Login, kein Pruefverfahren. Wiederholungen (RRULE), Ausnahmen und Zeitzonen
rechnet recurring-ical-events.

    setzen(adr, url)        Adresse pruefen und speichern (webcal:// wird zu https://)
    termine(adr, tage)      Termine ab jetzt, in der Ortszeit des Nutzers
    bald(adr, minuten)      Termine, die in den naechsten Minuten beginnen (fuer die Erinnerung)

Speicher: kv motor_cal_<adresse>. Abruf hoechstens alle 10 Minuten je Nutzer.
"""
import json
import time
from datetime import date, datetime, timedelta, timezone

import requests

import storage
from logger import log

UA = {"User-Agent": "Mozilla/5.0 (OddsEngine YourEngine calendar)"}
_CACHE = {}


def _lesen(adr):
    try:
        r = storage.kv_get("motor_cal_" + adr)
        return json.loads(r) if r else None
    except Exception:
        return None


def _schreiben(adr, d):
    storage.kv_set("motor_cal_" + adr, json.dumps(d) if d else "")


def _url(u):
    u = (u or "").strip()
    if u.lower().startswith("webcal://"):
        u = "https://" + u[9:]
    return u


def _holen(url):
    import motor
    if not motor._sicher(url):
        raise ValueError("That link is not allowed.")
    r = requests.get(url, headers=UA, timeout=20, stream=True, allow_redirects=False)
    if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
        ziel = r.headers.get("Location", "")
        if not motor._sicher(ziel):
            raise ValueError("The calendar link redirects somewhere not allowed.")
        r = requests.get(ziel, headers=UA, timeout=20, stream=True, allow_redirects=False)
    r.raise_for_status()
    roh = b""
    for teil in r.iter_content(65536):
        roh += teil
        if len(roh) > 6_000_000:
            raise ValueError("Calendar too large.")
    if b"BEGIN:VCALENDAR" not in roh[:2000]:
        raise ValueError("That link is not an iCal calendar. Use the secret address in iCal format.")
    return roh


def _kalender(adr, url=None):
    url = url or (_lesen(adr) or {}).get("url")
    if not url:
        return None
    c = _CACHE.get(adr)
    if c and c[0] == url and time.time() - c[1] < 600:
        return c[2]
    import icalendar
    cal = icalendar.Calendar.from_ical(_holen(url))
    _CACHE[adr] = (url, time.time(), cal)
    return cal


def setzen(adr, url):
    url = _url(url)
    if not url.startswith("https://"):
        return {"ok": False, "error": "Paste the secret iCal address (it starts with https:// or webcal://)."}
    try:
        cal = _kalender(adr, url)
        n = len(list(cal.walk("VEVENT")))
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}
    name = str(cal.get("X-WR-CALNAME") or "Calendar")
    _schreiben(adr, {"url": url, "name": name, "since": int(time.time()), "remind": True})
    return {"ok": True, "name": name, "events_in_file": n}


def trennen(adr):
    _schreiben(adr, None)
    _CACHE.pop(adr, None)
    return {"ok": True}


def status(adr):
    d = _lesen(adr)
    if not d:
        return {"connected": False}
    return {"connected": True, "name": d.get("name"), "since": d.get("since"), "remind": d.get("remind", True)}


def _zeit(v, tz_h):
    """DTSTART als UTC-Zeitstempel; ganztaegig = Mitternacht Ortszeit."""
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone(timedelta(hours=tz_h)))
        return v.timestamp(), False
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=timezone(timedelta(hours=tz_h))).timestamp(), True
    return None, False


def termine(adr, tage=1, tz_h=0.0, ab=None, n=25):
    try:
        cal = _kalender(adr)
    except Exception as e:
        return {"error": "Calendar not reachable: " + str(e)[:120]}
    if cal is None:
        return {"error": "No calendar connected. The user adds the secret iCal link once on engine.oddsengine.live under Your Engine, Calendar."}
    import recurring_ical_events
    lokal = timezone(timedelta(hours=tz_h))
    start = datetime.fromtimestamp(ab or time.time(), lokal)
    if not ab:
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    ende = start + timedelta(days=max(1, min(int(tage or 1), 14)))
    out = []
    for ev in recurring_ical_events.of(cal).between(start, ende):
        ts, ganz = _zeit(ev.get("DTSTART").dt if ev.get("DTSTART") else None, tz_h)
        if ts is None:
            continue
        te, _ = _zeit(ev.get("DTEND").dt, tz_h) if ev.get("DTEND") else (None, False)
        if str(ev.get("STATUS") or "").upper() == "CANCELLED":
            continue
        lt = datetime.fromtimestamp(ts, lokal)
        out.append({"title": str(ev.get("SUMMARY") or "(no title)")[:140], "start": ts, "end": te, "all_day": ganz,
                    "local": lt.strftime("%a %d %b") + ("" if ganz else lt.strftime(" %H:%M")),
                    "location": str(ev.get("LOCATION") or "")[:160] or None,
                    "uid": str(ev.get("UID") or "") + "|" + str(int(ts))})
    out.sort(key=lambda x: x["start"])
    return {"events": out[:n], "calendar": (_lesen(adr) or {}).get("name")}


def bald(adr, minuten, tz_h=0.0):
    r = termine(adr, tage=1, tz_h=tz_h, ab=time.time())
    if r.get("error"):
        return []
    jetzt = time.time()
    return [e for e in r["events"] if not e["all_day"] and 0 < e["start"] - jetzt <= minuten * 60]


def briefing_zeilen(adr, tz_h=0.0):
    if not _lesen(adr):
        return []
    r = termine(adr, tage=1, tz_h=tz_h)
    if r.get("error"):
        return []
    ev = [e for e in r["events"] if (e["end"] or e["start"]) > time.time() or e["all_day"]]
    if not ev:
        return ["Calendar: nothing planned today."]
    z = ["Today in your calendar"]
    for e in ev[:6]:
        z.append(("All day" if e["all_day"] else e["local"][-5:]) + f": {e['title']}" + (f" ({e['location'][:50]})" if e["location"] else ""))
    return z
