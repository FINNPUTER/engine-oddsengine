"""
orte.py: Orte, Wetter und Wege fuer Your Engine. Seit 2.33.

  suchen(q)            Ort oder Adresse -> {label, lat, lon}. TomTom, sonst OpenStreetMap.
  wetter(lat, lon)     Heute und jetzt, weltweit, ueber MET Norway (frei, auch kommerziell,
                       Quelle: MET Norway).
  route(a, b, art)     Fahrzeit mit aktuellem Verkehr ueber TomTom (TOMTOM_API_KEY auf web,
                       derselbe Schluessel wie bei HENRY). Ohne Schluessel: kein Verkehr.

Kosten: Routen kosten uns bei TomTom ab dem Freikontingent etwas, deshalb geht jede
Verkehrsabfrage mit MOTOR_ROUTE_USD (Standard 0,002 $) vom Guthaben des Nutzers ab.
Wetter kostet nichts.
"""
import math
import os
import time

import requests

from logger import log

UA = {"User-Agent": "OddsEngine-YourEngine/1.0 (engine.oddsengine.live)"}
_CACHE = {}


def _key():
    return os.getenv("TOMTOM_API_KEY", "").strip()


def verkehr_da():
    return bool(_key())


def _merk(k, ttl, f):
    c = _CACHE.get(k)
    if c and time.time() - c[0] < ttl:
        return c[1]
    v = f()
    if v is not None:
        _CACHE[k] = (time.time(), v)
        if len(_CACHE) > 2000:
            for kk in sorted(_CACHE, key=lambda x: _CACHE[x][0])[:500]:
                _CACHE.pop(kk, None)
    return v


def suchen(q):
    q = str(q or "").strip()[:200]
    if not q:
        return None

    def hol():
        if _key():
            try:
                r = requests.get("https://api.tomtom.com/search/2/geocode/" + requests.utils.quote(q) + ".json",
                                 params={"key": _key(), "limit": 1}, timeout=10)
                res = (r.json().get("results") or []) if r.ok else []
                if res:
                    p = res[0].get("position") or {}
                    a = res[0].get("address") or {}
                    return {"label": a.get("freeformAddress") or q, "lat": p.get("lat"), "lon": p.get("lon"),
                            "country": a.get("countryCode")}
            except Exception as e:
                log.debug(f"orte tomtom geocode: {e}")
        try:
            r = requests.get("https://nominatim.openstreetmap.org/search",
                             params={"format": "jsonv2", "limit": 1, "q": q}, headers=UA, timeout=10)
            res = r.json() if r.ok else []
            if res:
                return {"label": res[0].get("display_name", q)[:120], "lat": float(res[0]["lat"]),
                        "lon": float(res[0]["lon"]), "country": None}
        except Exception as e:
            log.debug(f"orte nominatim: {e}")
        return None
    return _merk("g:" + q.lower(), 7 * 86400, hol)


_SYM = {"clearsky": "clear", "fair": "mostly clear", "partlycloudy": "partly cloudy", "cloudy": "cloudy",
        "fog": "fog", "lightrain": "light rain", "rain": "rain", "heavyrain": "heavy rain",
        "lightrainshowers": "light showers", "rainshowers": "showers", "heavyrainshowers": "heavy showers",
        "lightsleet": "light sleet", "sleet": "sleet", "lightsnow": "light snow", "snow": "snow",
        "heavysnow": "heavy snow", "snowshowers": "snow showers", "rainandthunder": "rain and thunder",
        "heavyrainandthunder": "thunderstorms", "rainshowersandthunder": "showers and thunder"}


def _sym(code):
    c = str(code or "").split("_")[0]
    return _SYM.get(c, c.replace("and", " and ") or "n/a")


def wetter(lat, lon, utc_offset_h=0.0):
    """Jetzt plus Rest des Tages (lokal). Quelle: MET Norway."""
    lat, lon = round(float(lat), 3), round(float(lon), 3)

    def hol():
        r = requests.get("https://api.met.no/weatherapi/locationforecast/2.0/compact",
                         params={"lat": lat, "lon": lon}, headers=UA, timeout=12)
        r.raise_for_status()
        return r.json()
    try:
        d = _merk(f"w:{lat},{lon}", 1800, hol)
    except Exception as e:
        return {"error": f"weather unavailable: {str(e)[:80]}"}
    reihe = ((d or {}).get("properties") or {}).get("timeseries") or []
    if not reihe:
        return {"error": "weather unavailable"}
    from datetime import datetime, timedelta, timezone
    jetzt_l = datetime.now(timezone.utc) + timedelta(hours=utc_offset_h)
    temps, regen, symbole = [], 0.0, []
    erste = reihe[0]
    for t in reihe[:30]:
        ts = datetime.fromisoformat(t["time"].replace("Z", "+00:00")) + timedelta(hours=utc_offset_h)
        if ts.date() != jetzt_l.date():
            continue
        det = t["data"]["instant"]["details"]
        temps.append(det.get("air_temperature"))
        n1 = (t["data"].get("next_1_hours") or {})
        regen += float((n1.get("details") or {}).get("precipitation_amount") or 0)
        if n1.get("summary"):
            symbole.append(n1["summary"].get("symbol_code"))
    det0 = erste["data"]["instant"]["details"]
    s0 = ((erste["data"].get("next_1_hours") or erste["data"].get("next_6_hours") or {}).get("summary") or {})
    temps = [x for x in temps if x is not None]
    haeufig = max(set(symbole), key=symbole.count) if symbole else s0.get("symbol_code")
    return {"now_c": det0.get("air_temperature"), "now": _sym(s0.get("symbol_code")),
            "wind_ms": det0.get("wind_speed"),
            "today_min_c": min(temps) if temps else None, "today_max_c": max(temps) if temps else None,
            "today": _sym(haeufig), "rain_mm_rest_of_day": round(regen, 1),
            "source": "MET Norway"}


def wetter_zeile(w):
    if not w or w.get("error"):
        return None
    t = f"{w['now']}, {w['now_c']:.0f}°C now"
    if w.get("today_max_c") is not None:
        t += f", {w['today_min_c']:.0f} to {w['today_max_c']:.0f}°C today"
    if w.get("rain_mm_rest_of_day", 0) >= 0.5:
        t += f", {w['rain_mm_rest_of_day']} mm rain later"
    return t


def route(a, b, art="car"):
    """a, b: {lat, lon}. Gibt Fahrzeit mit Verkehr, davon Stau, ohne Verkehr, Strecke."""
    if not _key():
        return {"error": "traffic is not set up (TOMTOM_API_KEY missing on web)"}
    modus = {"car": "car", "bike": "bicycle", "bicycle": "bicycle", "walk": "pedestrian",
             "pedestrian": "pedestrian", "motorcycle": "motorcycle"}.get(str(art or "car").lower(), "car")
    try:
        r = requests.get("https://api.tomtom.com/routing/1/calculateRoute/"
                         f"{a['lat']},{a['lon']}:{b['lat']},{b['lon']}/json",
                         params={"key": _key(), "traffic": "true", "travelMode": modus,
                                 "computeTravelTimeFor": "all", "routeType": "fastest"}, timeout=15)
        if not r.ok:
            return {"error": f"route failed ({r.status_code})"}
        s = ((r.json().get("routes") or [{}])[0].get("summary") or {})
    except Exception as e:
        return {"error": f"route failed: {str(e)[:80]}"}
    if not s:
        return {"error": "no route found"}
    mit = s.get("travelTimeInSeconds") or 0
    stau = s.get("trafficDelayInSeconds") or 0
    ohne = s.get("noTrafficTravelTimeInSeconds") or max(0, mit - stau)
    normal = s.get("historicTrafficTravelTimeInSeconds") or ohne
    return {"minutes_now": round(mit / 60), "minutes_traffic_delay": round(stau / 60),
            "minutes_no_traffic": round(ohne / 60), "minutes_usual": round(normal / 60),
            "km": round((s.get("lengthInMeters") or 0) / 1000, 1), "mode": modus}


def route_zeile(name, abfahrt, r):
    if not r or r.get("error"):
        return None
    t = f"{name}: {r['minutes_now']} min right now"
    extra = r["minutes_now"] - r["minutes_usual"]
    if r["minutes_traffic_delay"] >= 3:
        t += f", {r['minutes_traffic_delay']} min of that is traffic"
    elif extra <= 1:
        t += ", roads are clear"
    if abfahrt:
        t += f" (leaving {abfahrt})"
    if extra >= 8:
        t += f". About {extra} min slower than usual, leave a bit earlier"
    return t


def km(a, b):
    p = math.pi / 180
    x = (b["lon"] - a["lon"]) * p * math.cos((a["lat"] + b["lat"]) / 2 * p)
    y = (b["lat"] - a["lat"]) * p
    return 6371 * math.hypot(x, y)
