import os
"""
OddsEngine 2.0 — Core Engine
============================

Polymarket temperature markets -> real probability distribution -> best
contiguous 3-5 outcome block by EDGE.

Read-only. Nothing here touches a wallet.

Pipeline
--------
1. discover()            Gamma -> every active highest-temperature-in-* event
2. member_day_maxima()   Open-Meteo Ensemble -> hourly temps, ~100 members,
                         24h max computed per member over the market's LOCAL
                         calendar day. NOT the model's daily-max field —
                         that misses warm-front nights (the Toronto bug).
3. distribution()        Ensemble dressing -> smooth q over the buckets
4. orderbook()           CLOB /book -> real executable asks + depth
5. best_block()          every contiguous 3..5 window, scored by
                         edge = q_block - sum(asks). NOT by coverage.

THE ASSUMPTION THAT CAN INVALIDATE EVERYTHING
---------------------------------------------
BUCKET_ROUNDING. Is "33C" the interval [32.5, 33.5) or the rounded station
reading? Unverified. Half a bucket of error shifts every number here.
Verify against 5 real resolutions before trusting output.
"""

import json
import math
import re
import time
from datetime import datetime, timezone, timedelta

import requests

import stations
from config import cfg
from logger import log

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
GEO = "https://geocoding-api.open-meteo.com/v1/search"
ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
FORECAST = "https://api.open-meteo.com/v1/forecast"

SLUG_RE = re.compile(
    r"^highest-temperature-in-(?P<city>[a-z0-9-]+?)-on-"
    r"(?P<month>january|february|march|april|may|june|july|august|september"
    r"|october|november|december)-(?P<day>\d{1,2})(?:-(?P<year>\d{4}))?$"
)
BUCKET_RE = re.compile(
    r"(?P<lo>-?\d+)\s*(?:-\s*(?P<hi>-?\d+))?\s*°?\s*(?P<unit>[CF])"
    r"\s*(?P<mod>or higher|or above|or below|or lower)?", re.I)

MONTHS = {m: i + 1 for i, m in enumerate(
    "january february march april may june july august september october "
    "november december".split())}

_sess = requests.Session()
_sess.headers["User-Agent"] = "oddsengine/2.0"
_geo_cache = {}
_last_call = [0.0]
# (icao, date) -> (fetched_at, maxima, tz). ICON/GFS rerun every 6h, ECMWF
# every 12h. Re-fetching hourly costs 6x the API calls for identical numbers
# and would blow Open-Meteo's free daily quota within a week.
_ens_cache = {}
_obs_cache = {}
_front_cache = {}
_taf_cache = {}
_metar_cache = {}
_wu_cache = {}          # (icao, date) -> (fetched_at, high_c)
TAF = "https://aviationweather.gov/api/data/taf"
METAR = "https://aviationweather.gov/api/data/metar"
WU_HOURLY = "https://www.wunderground.com/hourly/{path}/date/{date}"

# Fallback WU paths by ICAO, used only when the market description carries no
# /history/daily/ link. Confirmed paths only — a wrong path gives wrong data,
# which is worse than None. Extend as stations are verified.
_WU_STATIONS = {
    "EDDM": "de/oberding/EDDM",     # Munich
    "RKSI": "kr/incheon/RKSI",      # Seoul
    "SAEZ": "ar/ezeiza/SAEZ",       # Buenos Aires
    "RCSS": "tw/taipei/RCSS",       # Taipei (aus alten Regeln, vor METAR)
    "ZSJN": "cn/jinan/ZSJN",        # Jinan
    "ZHCC": "cn/zhengzhou/ZHCC",    # Zhengzhou
}
_WU_PATH_RE = re.compile(
    r"wunderground\.com/history/daily/((?:[a-z0-9\-]+/)+[A-Za-z]{4})", re.I)

# Per-city bias and sigma, from tools/city_calibration.py. Absent -> global.
# One sigma for fifty cities was always a placeholder: Singapore is tropical
# and boring, Chicago has fronts, and LaGuardia sits on water the model's grid
# point does not. Cities with thin history are shrunk toward the global value
# by the calibration tool, so a city with two markets simply gets the default.
_CITY_PARAMS = {}


def _load_city_params():
    global _CITY_PARAMS
    try:
        import os
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "city_params.json")
        if os.path.exists(p):
            with open(p) as f:
                d = json.load(f)
            _CITY_PARAMS = d.get("cities", {})
            log.info(f"city_params.json: {len(_CITY_PARAMS)} cities calibrated")
        else:
            log.info("no city_params.json — global sigma for all cities. "
                     "Run tools/city_calibration.py --write once there is history.")
    except Exception as e:
        log.warning(f"city_params load failed, using global: {e}")


def city_params(city):
    """(sigma, bias) for a city. Falls back to the global fit."""
    p = _CITY_PARAMS.get((city or "").lower()) or {}
    sg, b = p.get("sigma_c", cfg.dress_sigma_c), p.get("bias_c", 0.0)
    # Seit 2.18: gelernte Korrektur aus den letzten Wochen schlaegt die feste.
    try:
        import kalibrierung
        live = kalibrierung.bias(city)
        if live is not None:
            b = live
    except Exception:
        pass
    return sg, b


def _get(url, retries=3, **params):
    """Throttled GET with retry. Gamma throws SSL EOF under load."""
    for attempt in range(retries):
        wait = cfg.throttle_s - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        try:
            r = _sess.get(url, params=params or None, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    return None


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _f_to_c(f):
    return (f - 32.0) * 5.0 / 9.0


def _loads(v, d):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return d
    return v if v is not None else d


# ── 1. Market discovery ──────────────────────────────────────────────────

def parse_bucket(label, rounding=None):
    """
    '33C' / '58-59F' / '29C or higher' -> bounds in Celsius.

    `rounding` is the half-bucket width, taken from the market's own rules
    via stations.rounding_for(). For the 50 cities whose source "measures
    temperatures to whole degrees", it is 0.5: the source rounds before we
    ever see the number, so the bucket labelled 33 is exactly
    {T : round(T) == 33} = [32.5, 33.5). Confirmed against the rule text,
    not assumed.
    """
    if not label:
        return None
    if rounding is None:
        rounding = cfg.bucket_rounding
    m = BUCKET_RE.search(label)
    if not m:
        return None
    lo_raw = int(m.group("lo"))
    hi_raw = int(m.group("hi") or m.group("lo"))
    unit = m.group("unit").upper()
    mod = (m.group("mod") or "").lower()

    lo = lo_raw - rounding
    hi = hi_raw + rounding
    if "higher" in mod or "above" in mod:
        hi = float("inf")
    elif "below" in mod or "lower" in mod:
        lo = float("-inf")

    if unit == "F":
        if lo != float("-inf"):
            lo = _f_to_c(lo)
        if hi != float("inf"):
            hi = _f_to_c(hi)

    return {"label": label, "unit": unit, "lo_c": lo, "hi_c": hi,
            "sort": lo_raw if lo != float("-inf") else -999}


def station_hint(desc):
    """
    The resolution authority named in the rules.

    NOT always an airport and NOT always METAR. Hong Kong resolves on the
    Hong Kong Observatory's "Absolute Daily Max (deg. C)" from its Daily
    Extract. Every city names its own source. Airport-only matching misses
    most of them.
    """
    if not desc:
        return None
    for pat in [
        r"recorded by the ([A-Z][A-Za-z\s\.\'()-]{3,58}?)(?: in degrees| on | as |,|\.)",
        r"information from the ([A-Z][A-Za-z\s\.\'()-]{3,58}?)(?:,|\.| specifically)",
        r"reported by ([A-Z][A-Za-z\s\.\'()-]{3,58}?)(?:,|\.| for )",
        r"according to ([A-Z][A-Za-z\s\.\'()-]{3,58}?)(?:,|\.| for )",
        r"at\s+([A-Z][A-Za-z\s\.]+?(?:Airport|Airfield|Intl|International))",
    ]:
        m = re.search(pat, desc)
        if m:
            return " ".join(m.group(1).split()).strip(" .,")
    return None


# Gamma tag for "Highest temperature". Measured 100/100 hit rate.
# Do NOT go back to paging /events without a tag: Gamma orders by volume and
# caps offset at ~2100 (HTTP 422). Temperature markets are tiny and sit far
# past that, so plain pagination returns zero of them no matter the params.
# Fallbacks, also measured: 103040 daily-temperature (87/100), 84 weather (54/100).
# The param is tag_id. Plain `tag` silently returns unfiltered results.
TAG_HIGHEST_TEMPERATURE = 104596


def discover(city_filter=None):
    """Every ACTIVE temperature event on Polymarket. No hardcoded city list."""
    out, offset = [], 0
    while offset < cfg.max_discovery_offset:
        try:
            page = _get(f"{GAMMA}/events", tag_id=cfg.tag_id, closed="false",
                        limit=100, offset=offset)
        except Exception as e:
            log.warning(f"gamma offset={offset}: {e}")
            break
        if not page:
            break

        for ev in page:
            m = SLUG_RE.match(ev.get("slug", "") or "")
            if not m:
                continue
            city = m.group("city").replace("-", " ")
            if city_filter and city_filter.lower() not in city:
                continue

            desc = ev.get("description") or ""
            mkts = ev.get("markets", [])
            if not desc and mkts:
                desc = mkts[0].get("description") or ""

            # Rounding comes from the rules, per market. Hong Kong resolves at
            # 0.1C precision and never states where its bucket edge sits, so
            # rounding_for() returns None and we skip rather than guess.
            rounding = stations.rounding_for(desc)
            if rounding is None:
                log.debug(f"skip {city}: rounding convention not stated")
                continue

            # The station is parsed out of the URL inside the rules (ICAO).
            # Never geocode the city — "nyc" is Manhattan, the market settles
            # at LaGuardia, and that is one to two whole buckets apart.
            st = stations.coords_for(_url_in(desc), city)
            if not st:
                log.debug(f"skip {city}: no station coords")
                continue

            outs = []
            for mk in mkts:
                b = parse_bucket(mk.get("groupItemTitle", ""), rounding)
                tok = _loads(mk.get("clobTokenIds"), [])
                if b and tok:
                    b["token_id"] = tok[0]      # index 0 = YES
                    b["condition_id"] = mk.get("conditionId", "")
                    # Per-outcome slug, e.g. ...-on-july-17-2026-33c. This is
                    # what makes a one-tap deep link possible: no command, no
                    # copy-paste, no wallet handed to anyone.
                    b["market_slug"] = mk.get("slug", "")
                    outs.append(b)
            if len(outs) < 3:
                continue
            outs.sort(key=lambda x: x["sort"])

            year = int(m.group("year")) if m.group("year") else datetime.now().year
            out.append({
                "slug": ev.get("slug"),
                "title": ev.get("title", ""),
                "city": city,
                "outcomes": outs,
                "date": f"{year}-{MONTHS[m.group('month')]:02d}-{int(m.group('day')):02d}",
                "end_date": ev.get("endDate", ""),
                "lat": st[0], "lon": st[1], "station": st[2], "icao": st[3],
                "rounding": rounding,
                "description": desc[:2000],
            })

        if len(page) < 100:
            break
        offset += 100

    log.info(f"discovered {len(out)} active temperature events")
    _stationen_melden()
    return out


def _stationen_melden():
    """Neue oder fehlende Stationen einmal am Tag in den privaten Chat."""
    if not (stations.NEU or stations.FEHLT):
        return
    try:
        import storage
        import telegram
        heute = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        schluessel = "stationen_" + heute
        stand = "|".join(sorted(list(stations.NEU) + list(stations.FEHLT)))
        if storage.kv_get(schluessel) == stand:
            return
        z = ["🛰 Stationen"]
        for icao, (city, name) in sorted(stations.NEU.items()):
            z.append(f"NEU  {city} → {icao} {name}: nachgeschlagen, wird bewertet. "
                     f"Bitte in stations.py eintragen.")
        for icao, city in sorted(stations.FEHLT.items()):
            z.append(f"FEHLT  {city} ({icao.lstrip('?') or 'kein ICAO im Regeltext'}): "
                     f"nicht bewertet.")
        for zeile in z[1:]:
            log.warning(zeile)
        if cfg.tg_token and cfg.tg_private:
            telegram.send("\n".join(z), chat=cfg.tg_private)
        storage.kv_set(schluessel, stand)
    except Exception as e:
        log.debug(f"stationen melden: {e}")


# ── 2. Forecast ──────────────────────────────────────────────────────────

def _url_in(text):
    """Die Quell-URL mit der Station aus den Regeln.
    Seit den Maerkten vom 23.10.2026 stehen die Regeln auf METAR (aviationweather.gov,
    ids=EGLC) und die erste URL im Text ist ein PDF mit der Leseanleitung. Deshalb
    nehmen wir die erste URL, aus der sich eine Station lesen laesst, und sonst den
    ICAO-Code in Klammern, z. B. "London City Airport (EGLC)"."""
    t = text or ""
    urls = re.findall(r"(https?://[^\s\)]+)", t)
    for u in urls:
        if stations.icao_from_url(u):
            return u
    m = re.search(r"\(([A-Z]{4})\)", t)
    if m:
        return "https://aviationweather.gov/api/data/metar?ids=" + m.group(1)
    return urls[0] if urls else ""


def geocode(city):
    """Deprecated. City centre != resolution station. Kept for tools only."""
    if city in _geo_cache:
        return _geo_cache[city]
    try:
        r = _get(GEO, name=city, count=1, language="en", format="json")
        res = (r.get("results") or [None])[0]
        if res:
            _geo_cache[city] = (res["latitude"], res["longitude"])
            return _geo_cache[city]
    except Exception as e:
        log.warning(f"geocode {city}: {e}")
    _geo_cache[city] = None
    return None



def _ensemble_holen(lat, lon):
    """Seit 2.17.6: Faellt bei Open-Meteo EIN Modell aus, liefert die
    gemeinsame Abfrage aller Modelle einen 500er und die Engine war blind
    (12.10.2026, ICON). Dann jedes Modell einzeln holen und zusammenlegen.
    Gerechnet wird nur, wenn mindestens ENS_MIN_MODELS Modelle da sind."""
    try:
        return _get(ENSEMBLE, latitude=lat, longitude=lon,
                    hourly="temperature_2m", models=cfg.ens_models,
                    timezone="auto", forecast_days=7, past_days=1)
    except Exception as gesamt:
        modelle = [m.strip() for m in str(cfg.ens_models).split(",") if m.strip()]
        if len(modelle) < 2:
            raise
        zusammen, tz, ok, fehlt = {}, None, [], []
        for m in modelle:
            try:
                d = _get(ENSEMBLE, latitude=lat, longitude=lon,
                         hourly="temperature_2m", models=m,
                         timezone="auto", forecast_days=7, past_days=1)
            except Exception:
                fehlt.append(m)
                continue
            h = d.get("hourly", {})
            if not h.get("time"):
                fehlt.append(m)
                continue
            if "time" not in zusammen:
                zusammen["time"] = h["time"]
                tz = d.get("timezone", "UTC")
            elif h["time"] != zusammen["time"]:
                fehlt.append(m)
                continue
            for k, v in h.items():
                if k.startswith("temperature_2m"):
                    zusammen[f"{k}__{m}"] = v
            ok.append(m)
        mindest = int(os.getenv("ENS_MIN_MODELS", "2"))
        if len(ok) < mindest:
            raise gesamt
        log.warning(f"ensemble: Sammelabfrage gescheitert, einzeln geholt: "
                    f"ok {','.join(ok)} | fehlt {','.join(fehlt) or '-'}")
        return {"hourly": zusammen, "timezone": tz}


def member_day_maxima(lat, lon, target_date, key=None):
    """
    Max temperature per ensemble member over the market's LOCAL calendar day.

    We take HOURLY series and build the 24h max ourselves. Trusting a model's
    temperature_2m_max field misses warm-front nights, which is exactly how
    Toronto resolved against us.

    Cached per (station, date) for cfg.ens_cache_min. The models simply do
    not produce new numbers faster than that.

    Returns (list_of_member_maxima, timezone_string).
    """
    ck = (key, target_date)
    if key:
        hit = _ens_cache.get(ck)
        if hit and (time.time() - hit[0]) < cfg.ens_cache_min * 60:
            return hit[1], hit[2]

    data = _ensemble_holen(lat, lon)
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    if not times:
        return [], None

    idx = [i for i, t in enumerate(times) if t.startswith(target_date)]
    if len(idx) < 20:
        return [], None

    maxima = []
    for key, series in hourly.items():
        if key == "time" or not key.startswith("temperature_2m"):
            continue
        vals = [series[i] for i in idx if i < len(series) and series[i] is not None]
        if len(vals) >= 20:
            maxima.append(max(vals))
    tz = data.get("timezone", "UTC")
    if key and maxima:
        _ens_cache[ck] = (time.time(), maxima, tz)
        # Bounded: ~50 stations x 3 dates. Never let a long-running worker
        # turn its cache into a memory leak.
        if len(_ens_cache) > 400:
            for k in sorted(_ens_cache, key=lambda x: _ens_cache[x][0])[:200]:
                _ens_cache.pop(k, None)
    return maxima, tz


def observed_max_today(lat, lon, target_date, key=None):
    """
    Highest temperature actually MEASURED at the station so far on the
    market's local day. Open-Meteo's forecast endpoint backfills elapsed
    hours with observations, so this is the ground the market has already
    stood on -- a hard floor the day's max cannot fall below.

    Returns (obs_max_c, n_hours). (None, 0) when the market's day is not
    today locally (nothing observed yet) or the call fails, in which case
    the caller keeps the raw ensemble unchanged -- no regression.

    Cached 20 min: observations refresh hourly, so a long cache would hand
    back a stale floor exactly when it matters most, near close.
    """
    ck = ("obs", key, target_date)
    hit = _obs_cache.get(ck)
    if hit and (time.time() - hit[0]) < 1200:
        return hit[1]
    try:
        data = _get(FORECAST, latitude=lat, longitude=lon,
                    hourly="temperature_2m", timezone="auto",
                    past_days=1, forecast_days=1)
    except Exception:
        return (None, 0)
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    off = data.get("utc_offset_seconds", 0)
    now_iso = (datetime.now(timezone.utc)
               + timedelta(seconds=off)).strftime("%Y-%m-%dT%H:%M")
    vals = [temps[i] for i, t in enumerate(times)
            if t.startswith(target_date) and t <= now_iso
            and i < len(temps) and temps[i] is not None]
    res = (round(max(vals), 2), len(vals)) if vals else (None, 0)
    _obs_cache[ck] = (time.time(), res)
    if len(_obs_cache) > 400:
        for k in sorted(_obs_cache, key=lambda x: _obs_cache[x][0])[:200]:
            _obs_cache.pop(k, None)
    return res


def front_signal(lat, lon, target_date, key=None):
    """
    Peak-window front risk: precipitation probability and cloud cover during the
    hours the day's max is expected. A front arriving AT the peak suppresses the
    high -- the classic 'cheap low bucket hits' setup traded by hand.

    NOTE: the ensemble temperature ALREADY reflects rain (wet members run
    cooler), so the low buckets already carry probability. This makes the front
    EXPLICIT and visible in logs/snapshots; it is same-model precip, so it adds
    no independent skill for fronts the models miss -- that needs a nowcast/radar
    source. Returns {peak_hour, peak_precip_pct, peak_cloud_pct} or None. 30-min cache.
    """
    ck = ("front", key, target_date)
    hit = _front_cache.get(ck)
    if hit and (time.time() - hit[0]) < 1800:
        return hit[1]
    try:
        data = _get(FORECAST, latitude=lat, longitude=lon,
                    hourly="temperature_2m,precipitation_probability,cloud_cover",
                    timezone="auto", forecast_days=4)
    except Exception:
        return None
    h = data.get("hourly", {})
    times = h.get("time", []); temps = h.get("temperature_2m", [])
    precs = h.get("precipitation_probability", []); clouds = h.get("cloud_cover", [])
    idx = [i for i, t in enumerate(times)
           if t.startswith(target_date) and i < len(temps) and temps[i] is not None]
    if not idx:
        return None
    # The daily TEMPERATURE peak is a daytime event. On flat, overcast/rainy days
    # the diurnal range collapses and the raw argmax can land at night by noise,
    # so search the daytime window (10-18 local) for the peak; fall back to the
    # full day only if that window is missing. (Resolution still uses the full-day
    # max via member_day_maxima -- night maxima are not lost, only the front timing.)
    day = [i for i in idx if 10 <= int(times[i][11:13]) <= 18] or idx
    ph = max(day, key=lambda i: temps[i])           # warmest daytime hour = peak
    win = [i for i in idx if abs(i - ph) <= 2]       # +-2h around the peak
    def _mx(a):
        v = [a[i] for i in win if i < len(a) and a[i] is not None]
        return max(v) if v else None
    def _mean(a):
        v = [a[i] for i in win if i < len(a) and a[i] is not None]
        return round(sum(v) / len(v)) if v else None
    res = {"peak_hour": times[ph][11:16], "peak_precip_pct": _mx(precs),
           "peak_cloud_pct": _mean(clouds),
           "utc_offset": data.get("utc_offset_seconds", 0)}
    _front_cache[ck] = (time.time(), res)
    if len(_front_cache) > 400:
        for k in sorted(_front_cache, key=lambda x: _front_cache[x][0])[:200]:
            _front_cache.pop(k, None)
    return res


def taf_front(icao, target_date, peak_hour, utc_offset):
    """
    INDEPENDENT front check from the airport's aviation forecast (TAF). The
    resolution stations ARE airports, so the TAF is for exactly the point that
    resolves the market -- and it does NOT come from the Open-Meteo temperature
    ensemble, so it can flag a front the models under-called. Reads the forecast
    period covering the expected peak and flags rain/thunder/heavy cloud that
    would suppress the max. This is the source traded by hand.

    Returns {taf_wx, taf_cloud_pct, taf_front} or None. 60-min cache.
    NOTE: TAF is a point aviation forecast refreshed every ~6h, not live radar;
    and this parser is UNTESTED against live TAF in this build -- ships as a
    logged diagnostic, verify against reality before wiring it into bets.
    """
    if not icao or not peak_hour:
        return None
    ck = ("taf", icao, target_date)
    hit = _taf_cache.get(ck)
    if hit and (time.time() - hit[0]) < 3600:
        return hit[1]
    try:
        data = _get(TAF, ids=icao, format="json")
    except Exception:
        return None
    if not isinstance(data, list) or not data:
        return None
    fcsts = data[0].get("fcsts") or []
    if not fcsts:
        return None
    try:
        hh, mm = peak_hour.split(":")
        local = datetime.strptime(target_date, "%Y-%m-%d").replace(
            hour=int(hh), minute=int(mm), tzinfo=timezone.utc)
        peak_epoch = (local - timedelta(seconds=utc_offset)).timestamp()
    except Exception:
        return None
    period = None
    for f in fcsts:
        tf, tt = f.get("timeFrom"), f.get("timeTo")
        if tf is not None and tt is not None and tf <= peak_epoch <= tt:
            period = f
            break
    if period is None:
        return None   # peak not covered by any TAF period -> no signal, don't guess
    wx = (period.get("wxString") or "").strip()
    cover = {"SKC": 0, "CLR": 0, "NSC": 0, "FEW": 20, "SCT": 40, "BKN": 75, "OVC": 100}
    cloud_pct = max([cover.get((c.get("cover") or "").upper(), 0)
                     for c in (period.get("clouds") or [])] or [0])
    wet = any(code in wx for code in ("RA", "SHRA", "TSRA", "TS", "DZ", "SN", "GR", "GS"))
    # TX/TN: die amtlich vorhergesagte Hoechst- und Tiefsttemperatur der Station.
    # Nur ein Teil der Stationen haengt diese Gruppen an (gemessen 19 von 46).
    # Wird NUR mitgeschrieben, nicht gewichtet — genau der Fehler, den
    # WU_WEIGHT=0.7 war. Erst messen, dann entscheiden.
    raw = data[0].get("rawTAF") or ""
    tx = tn = tx_time = None
    m = re.search(r"\bTX(M?)(\d{1,2})/(\d{2})(\d{2})Z", raw, re.I)
    if m:
        tx = (-1 if m.group(1) else 1) * int(m.group(2))
        tx_time = f"{m.group(3)}T{m.group(4)}Z"
    m = re.search(r"\bTN(M?)(\d{1,2})/(\d{2})(\d{2})Z", raw, re.I)
    if m:
        tn = (-1 if m.group(1) else 1) * int(m.group(2))

    res = {"taf_wx": wx or "-", "taf_cloud_pct": cloud_pct,
           "taf_front": bool(wet or cloud_pct >= 75),
           "taf_tx_c": tx, "taf_tn_c": tn, "taf_tx_time": tx_time}
    _taf_cache[ck] = (time.time(), res)
    if len(_taf_cache) > 400:
        for k in sorted(_taf_cache, key=lambda x: _taf_cache[x][0])[:200]:
            _taf_cache.pop(k, None)
    return res


def metar_observed(icao, target_date, utc_offset):
    """
    Was hat die Station bis JETZT gemessen? Das ist die Quelle, gegen die
    abgerechnet wird — keine Modellrechnung und keine private Wetterstation.

    Das Tagesmaximum kann nur steigen. Liegt es bereits ueber einem Block, ist
    die Wette endgueltig verloren, oft Stunden vor der Abrechnung. Hier wird es
    nur mitgeschrieben; ob daraus eine Sperre wird, entscheidet der Record.

    Returns {metar_max_c, metar_last_c, metar_n, metar_last_at} oder None.
    15-Minuten-Cache, weil METAR halbstuendlich aktualisiert.
    """
    if not icao or not target_date:
        return None
    ck = ("metar", icao, target_date)
    hit = _metar_cache.get(ck)
    if hit and (time.time() - hit[0]) < 900:
        return hit[1]
    try:
        data = _get(METAR, ids=icao, format="json", hours=26)
    except Exception:
        return None
    if not isinstance(data, list) or not data:
        return None
    werte = []
    for m in data:
        # Regeln ab 23.10.2026: nur routinemaessige Meldungen zaehlen, SPECI nicht.
        # "temp" ist bei aviationweather.gov schon der Zehntelwert aus der T-Gruppe.
        roh = str(m.get("rawOb") or "")
        if str(m.get("metarType") or "").upper() == "SPECI" or roh.startswith("SPECI"):
            continue
        t, rt, ot = m.get("temp"), m.get("reportTime"), m.get("obsTime")
        if t is None or not (rt or ot):
            continue
        try:
            # obsTime ist die echte Beobachtungszeit (z. B. 23:51). reportTime ist auf
            # die volle Stunde gerundet und kann eine Meldung in den naechsten Tag schieben.
            if ot:
                ts = datetime.fromtimestamp(int(ot), tz=timezone.utc)
            else:
                ts = datetime.fromisoformat(str(rt).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        lokal = ts + timedelta(seconds=utc_offset or 0)
        if lokal.strftime("%Y-%m-%d") == target_date:
            werte.append((lokal, float(t)))
    if not werte:
        res = {"metar_max_c": None, "metar_min_c": None, "metar_last_c": None,
               "metar_n": 0, "metar_last_at": None}
    else:
        werte.sort()
        res = {"metar_max_c": max(v for _, v in werte),
               "metar_min_c": min(v for _, v in werte),
               "metar_last_c": werte[-1][1],
               "metar_n": len(werte),
               "metar_last_at": werte[-1][0].strftime("%H:%M")}
    _metar_cache[ck] = (time.time(), res)
    if len(_metar_cache) > 400:
        for k in sorted(_metar_cache, key=lambda x: _metar_cache[x][0])[:200]:
            _metar_cache.pop(k, None)
    return res


def wu_forecast(description, icao, target_date):
    """
    Wunderground's own forecast high for the resolution station, in Celsius.

    WU is the settlement source; its own forecast predicts its own resolution
    better than an Open-Meteo proxy. The daily "High XX F" is in the server-
    rendered summary (the hourly table is JS-only and not needed). The WU URL
    is derived from the market's resolution description (its /history/daily/
    link) and rewritten to the /hourly/.../date/ forecast page. Cached per
    (station, day). Defensive: any failure returns None and the engine falls
    back to the ensemble alone -- WU never takes the scan down.
    """
    if not cfg.wu_enabled:
        return None
    ck = (icao or "", target_date)
    hit = _wu_cache.get(ck)
    if hit and (time.time() - hit[0]) < cfg.wu_cache_min * 60:
        return hit[1]
    try:
        # 1) path from the market description; 2) fall back to a known station
        #    map so a missing/edited description doesn't kill WU for that city.
        m = _WU_PATH_RE.search(description or "")
        path = m.group(1) if m else _WU_STATIONS.get((icao or "").upper())
        if not path:
            log.debug(f"wu: no path for {icao} — description had none, no fallback")
            return None
        url = WU_HOURLY.format(path=path, date=target_date)
        html = None
        for attempt in range(2):  # one retry; WU/Cloudflare is flaky
            try:
                r = requests.get(url, timeout=6, headers={
                    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/120.0.0.0 Safari/537.36"),
                    "Accept-Language": "en-US,en;q=0.9"})
                if r.status_code == 200 and r.text:
                    html = r.text
                    break
            except Exception:
                pass
            time.sleep(0.2)
        if not html:
            log.debug(f"wu: fetch failed for {icao} {target_date}")
            return None
        # ⚠ 03.08.: alle drei Muster liefen in DIESELBE Fahrenheit-Umrechnung.
        # Das zweite Muster passt auf `[°F]`, also auf das Gradzeichen ODER
        # das F: es greift damit auch bei "Hi 30°", und das dritte liest
        # temperatureMax ganz ohne Einheit. Liefert Wunderground einmal
        # Celsius, werden 30 °C als 30 °F gelesen, also als -1,1 °C.
        #
        # Fuer US-Staedte kam bisher immer Fahrenheit, deshalb ist es nie
        # aufgefallen. Fuer die uebrigen dreissig Staedte ist es eine
        # tickende Uhr. Jetzt wird die Einheit MITGELESEN und nur dann
        # umgerechnet, wenn sie wirklich F lautet.
        m_hi = re.search(r"High\s+(\d+)\s*(F|C)\b", html)
        if m_hi:
            wert, einheit = int(m_hi.group(1)), m_hi.group(2)
        else:
            m_hi = re.search(r"\bHi\b[^0-9]{0,6}(\d+)\s*°?\s*(F|C)?", html)
            if m_hi:
                wert = int(m_hi.group(1))
                einheit = m_hi.group(2)
            else:
                m_hi = re.search(r'"temperatureMax"\s*:\s*"?(-?\d+)', html)
                if not m_hi:
                    log.debug(f"wu: no High in page for {icao} {target_date}")
                    return None
                wert, einheit = int(m_hi.group(1)), None

        if einheit is None:
            # Keine Einheit im Text. Wunderground liefert je nach Ort
            # verschieden, also am Wert entscheiden: ueber 50 kann in diesen
            # Staedten nur Fahrenheit sein, unter 50 nur Celsius.
            einheit = "F" if wert >= 50 else "C"

        high_c = (round((wert - 32) * 5.0 / 9.0, 1) if einheit == "F"
                  else float(wert))
        _wu_cache[ck] = (time.time(), high_c)
        if len(_wu_cache) > 400:
            for k in sorted(_wu_cache, key=lambda x: _wu_cache[x][0])[:200]:
                _wu_cache.pop(k, None)
        return high_c
    except Exception as e:
        log.debug(f"wu_forecast error {icao}: {e}")
        return None


def distribution(maxima, outcomes, lead_h, sigma=None, bias=0.0):
    """
    Ensemble dressing: each member is a Gaussian kernel, not a point.

        q(bucket) = mean over members of P(kernel lands in bucket)

    Raw member counts are overconfident. Ensembles are underdispersed and
    none of them know this station's local bias. The dressing width is what
    keeps the tails honest — it is the single most important number in this
    file and it is currently GUESSED, not measured.
    """
    if not maxima:
        return None
    base = cfg.dress_sigma_c if sigma is None else sigma
    sigma = math.sqrt(base ** 2 + (cfg.sigma_per_day_c * max(lead_h, 0) / 24.0) ** 2)

    q = []
    for o in outcomes:
        acc = 0.0
        for m in maxima:
            # Bias corrects the station, not the bucket: the grid point the
            # model reports is not the thermometer the market settles on.
            mm = m - bias
            hi = 1.0 if o["hi_c"] == float("inf") else _norm_cdf((o["hi_c"] - mm) / sigma)
            lo = 0.0 if o["lo_c"] == float("-inf") else _norm_cdf((o["lo_c"] - mm) / sigma)
            acc += max(hi - lo, 0.0)
        q.append(acc / len(maxima))

    tot = sum(q)
    return [x / tot for x in q] if tot > 0 else None   # buckets are exhaustive


# ── 3. Real executable prices ────────────────────────────────────────────

def _spanne(books, rng):
    """Groesste Spanne im Block -- einseitige Felder ausgenommen.

    Ein Feld ohne Gebot hat keine echte Spanne; wir setzen dort 10000 an, damit
    niemand versehentlich damit rechnet. Fuer das Gate zaehlt es aber nicht mit:
    die Spanne schuetzt vor einem schlechten AUSSTIEG, und diese Bloecke werden
    bis zur Aufloesung gehalten und danach eingeloest. Ein Feld deswegen zu
    verwerfen, hiesse die billigen Aussenbeine wegzuwerfen -- also genau die,
    die bei der 1-Dollar-Mindestorder den groessten Teil der Auszahlung tragen.
    """
    echte = [books[i]["spread_bps"] for i in rng
             if not books[i].get("one_sided")]
    return max(echte) if echte else 0.0


def _einseitig(books, rng):
    return sum(1 for i in rng if books[i].get("one_sided"))


def orderbook(token_id):
    """Real asks + depth. The displayed Gamma price is not tradeable."""
    try:
        b = _get(f"{CLOB}/book", retries=2, token_id=token_id)
    except Exception:
        return None
    bids, asks = b.get("bids", []), b.get("asks", [])
    # Frueher stand hier "if not bids or not asks: return None". Damit fiel
    # jedes Feld raus, auf das niemand BIETET -- obwohl man dort einwandfrei
    # KAUFEN kann, solange Verkaufsangebote vorliegen. Betroffen sind vor
    # allem die billigen Aussenfelder zu zwei, drei Cent: dort sitzen Verkaeufer,
    # aber keine Kaeufer. Genau diese Felder tragen bei der 1-Dollar-Mindest-
    # order den groessten Teil der Auszahlung. Sie wurden also systematisch
    # verworfen, und faellt dadurch die Zahl lesbarer Felder unter drei, wird
    # der ganze Markt als "orderbooks unavailable" abgeschrieben.
    if not asks:
        return None
    try:
        best_ask = min(float(a["price"]) for a in asks)
    except (ValueError, KeyError):
        return None
    if not 0 < best_ask < 1:
        return None

    best_bid = None
    if bids:
        try:
            b2 = max(float(x["price"]) for x in bids)
            if 0 < b2 < 1:
                best_bid = b2
        except (ValueError, KeyError):
            best_bid = None

    depth = sum(float(a["size"]) * float(a["price"])
                for a in asks if float(a["price"]) <= best_ask * 1.10)

    if best_bid is None:
        # Ohne Gegenseite laesst sich keine echte Spanne bilden. Statt zu raten
        # wird der volle Abstand zur Null angesetzt -- das ist die ehrliche
        # obere Schranke. Wer eine enge Spanne verlangt, filtert das Feld
        # weiterhin heraus; wer nur kaufen will, sieht es jetzt ueberhaupt.
        return {"ask": best_ask, "bid": None, "one_sided": True,
                "spread_bps": 10000.0,
                "ask_depth_usd": round(depth, 2)}

    mid = (best_ask + best_bid) / 2
    return {"ask": best_ask, "bid": best_bid, "one_sided": False,
            "spread_bps": round((best_ask - best_bid) / mid * 10000, 0),
            "ask_depth_usd": round(depth, 2)}


# ── 4. Block selection ───────────────────────────────────────────────────

def _bucket_center_c(o):
    """Repraesentative Mitte eines Buckets in Grad Celsius."""
    lo, hi = o["lo_c"], o["hi_c"]
    if lo == float("-inf"):
        return hi - 1.0
    if hi == float("inf"):
        return lo + 1.0
    return (lo + hi) / 2.0


def best_block(outcomes, q, books, mode, lead_h=None, center_c=None, front=False):
    """
    Pick the best contiguous 3-5 outcome block.

    ORDER MATTERS, and getting it wrong is why v1 of this function returned
    nothing on 63 of 93 markets:

      1. Build every contiguous window.
      2. FILTER on the hard gates — coverage, depth, spread, cost.
      3. Only among the survivors, maximise edge.
      4. Then check the edge threshold.

    v1 did argmax(edge) first and gated afterwards. That kills a market
    outright whenever the highest-edge block happens to fail a gate, even
    though the second-best block would have passed both.

    And the reason the top-edge block kept failing is worse: absolute edge
    in percentage points systematically prefers cheap tail blocks.

        tail:    coverage 17%, cost  5c  ->  edge +12pp   (EV +240%)
        centre:  coverage 95%, cost 92c  ->  edge  +3pp   (EV   +3%)

    The tail wins on both. That is exactly the behaviour that burned $46 on
    35 sub-1c bets for zero hits. Coverage-first is not a nicety here — it is
    what makes this a coverage product instead of a lottery ticket machine.

    Returns (block, None) or (None, reason).
    """
    lo_n, hi_n, min_edge_pp, min_cov = cfg.modes[mode]
    # Lead-aware coverage: ease the requirement further from resolution, where
    # the edge is beating the market rather than being certain. Hard floor at
    # cov_floor so cheap 17%-tail blocks never win (see tail/centre note above).
    if cfg.lead_aware_coverage and lead_h is not None and lead_h > cfg.cov_full_lead_h:
        span = max(cfg.max_lead_h - cfg.cov_full_lead_h, 1.0)
        t = min(1.0, (lead_h - cfg.cov_full_lead_h) / span)
        min_cov = max(cfg.cov_floor, min_cov - (min_cov - cfg.cov_floor) * t)
    n = len(outcomes)
    cands = []

    # ── Zentrierter Modus (CENTERED_FIELD=true) ──────────────────────────
    # Die Strategie des Nutzers: Ensemble mit WU kombinieren, Stadt-Bias
    # abziehen, und genau die drei Buckets um diesen Wert kaufen. Keine Suche
    # nach dem groessten Vorsprung, also auch keine Auswahlverzerrung — und
    # damit faellt der Optimizer-Haircut hier weg. Die Abdeckung ist dann
    # Ergebnis, kein Filter: sie wird berichtet, aber nicht geprueft.
    if cfg.centered_field and center_c is not None and n >= cfg.centered_width:
        base = cfg.centered_width
        # Eine Front zur Spitzenstunde drueckt das Tagesmaximum. Statt eine
        # Gradzahl zu raten, die wir nicht kennen, kaufen wir ein Band MEHR,
        # und zwar nach unten. Das deckt genau die Richtung ab, in die das
        # Risiko zeigt. Ausgeloest wird es von der Flughafenmeldung, nicht vom
        # Modellregen: der steckt bereits in der Ensemble-Temperatur und waere
        # doppelt gezaehlt.
        extra = 1 if (front and cfg.front_widen) else 0
        w = min(base + extra, n)
        ci = min(range(n), key=lambda i: abs(_bucket_center_c(outcomes[i]) - center_c))
        start = max(0, min(ci - (base // 2) - extra, n - w))
        rng = range(start, start + w)
        if any(books[i] is None for i in rng):
            return None, "orderbook missing on the centred field", None
        asks = [books[i]["ask"] for i in rng]
        cost = sum(asks)
        if cost <= 0:
            return None, "no price on the centred field", None
        cov = sum(q[i] for i in rng)
        stakes = [cfg.budget * a / cost for a in asks]
        blk = {
            "legs": [outcomes[i] for i in rng],
            "asks": asks,
            "cost": cost,
            "min_stake_usd": cost / min(asks) * cfg.min_order_usd if min(asks) > 0 else 0.0,
            "coverage": cov,
            "coverage_raw": cov,
            "edge_pp": (cov - cost) * 100,
            "ev_pct": (cov * (cfg.budget / cost) / cfg.budget - 1) * 100,
            "payout": cfg.budget / cost,
            "stakes": stakes,
            "depth_ratio": min((books[i]["ask_depth_usd"] / st if st > 0 else 999)
                               for i, st in zip(rng, stakes)),
            "min_depth": min(books[i]["ask_depth_usd"] for i in rng),
            "max_spread": _spanne(books, rng),
            "one_sided_legs": _einseitig(books, rng),
            "q": [q[i] for i in rng],
            "centred": True,
            "front_widened": bool(extra),
        }
        # Robuster Vorsprung: derselbe Block ohne sein billigstes Bein.
        # Immer wieder haengt der gesamte ausgewiesene Vorsprung an einem
        # 2-Cent-Randbein, dem wir 16-22% geben und der Markt 2%. Genau dort
        # blaeht eine zu breite Verteilung die Raender auf. Diese Zahl zeigt,
        # was uebrig bleibt, wenn man dieses eine Bein nicht mitzaehlt.
        _ci = min(range(len(asks)), key=lambda i: asks[i])
        _rc, _rk = cov - blk["q"][_ci], cost - asks[_ci]
        blk["robust_edge_pp"] = (_rc - _rk) * 100 if _rk > 0 else 0.0
        blk["thin_leg"] = outcomes[list(rng)[_ci]]["label"]
        diag = {"diag_legs": [o["label"] for o in blk["legs"]],
                "diag_coverage_raw": round(cov, 4),
                "diag_coverage": round(cov, 4),
                "diag_edge_pp": round(blk["edge_pp"], 2),
                "diag_cost": round(cost, 4),
                "diag_min_depth_usd": round(blk["min_depth"], 2),
                "diag_depth_ratio": round(blk["depth_ratio"], 1),
                "diag_max_spread_bps": blk["max_spread"],
                "diag_robust_edge_pp": round(blk["robust_edge_pp"], 2)}
        if cov < cfg.centered_min_coverage:
            return None, (f"centred coverage {cov:.0%} < "
                          f"{cfg.centered_min_coverage:.0%} floor"), diag
        if blk["robust_edge_pp"] < cfg.min_robust_edge_pp:
            return None, (f"robust edge {blk['robust_edge_pp']:+.1f}pp < "
                          f"{cfg.min_robust_edge_pp}pp (alles haengt am "
                          f"{blk['thin_leg']}-Bein)"), diag
        if cost > cfg.max_field_cost:
            return None, (f"centred field {cost*100:.0f}c > "
                          f"{cfg.max_field_cost*100:.0f}c cap"), diag
        if blk["depth_ratio"] < cfg.min_depth_ratio:
            return None, f"book thinner than {cfg.min_depth_ratio:.0f}x the order", diag
        if blk["min_depth"] < cfg.min_ask_depth_usd:
            return None, f"depth < ${cfg.min_ask_depth_usd:.0f} floor", diag
        if blk["max_spread"] > cfg.max_spread_bps:
            return None, f"spread > {cfg.max_spread_bps:.0f}bps", diag
        if blk["edge_pp"] < min_edge_pp:
            return None, (f"edge {blk['edge_pp']:+.1f}pp < {min_edge_pp}pp "
                          f"(cov {cov:.0%}, cost {cost*100:.0f}c)"), diag
        return blk, None, diag

    for size in range(lo_n, min(hi_n, n) + 1):
        for start in range(0, n - size + 1):
            rng = range(start, start + size)
            if any(books[i] is None for i in rng):
                continue
            asks = [books[i]["ask"] for i in rng]
            cost = sum(asks)
            cov = sum(q[i] for i in rng)
            payout = cfg.budget / cost if cost > 0 else 0.0

            stakes = [cfg.budget * a / cost for a in asks] if cost > 0 else []
            # Worst leg measured as depth-per-dollar-staked, not raw dollars.
            depth_ratio = min(
                (books[i]["ask_depth_usd"] / st if st > 0 else 999)
                for i, st in zip(rng, stakes)) if stakes else 0

            cands.append({
                "legs": [outcomes[i] for i in rng],
                "asks": asks,
                "cost": cost,
                # Smallest total stake at which every leg clears the exchange
                # minimum. The cheapest leg binds, because the split is
                # proportional to price.
                "min_stake_usd": (cost / min(asks) * cfg.min_order_usd
                                  if asks and min(asks) > 0 else 0.0),
                "coverage": cov,
                "edge_pp": (cov - cost) * 100,
                "ev_pct": (cov * payout / cfg.budget - 1) * 100 if cost > 0 else 0.0,
                "payout": payout,
                "stakes": stakes,
                "depth_ratio": depth_ratio,
                "min_depth": min(books[i]["ask_depth_usd"] for i in rng),
                "max_spread": _spanne(books, rng),
                "one_sided_legs": _einseitig(books, rng),
                "q": [q[i] for i in rng],
            })

    # Optimizer's curse haircut. Applied BEFORE the gates and before we
    # publish, so a signal never claims more than the selection has earned.
    try:
        import kalibrierung
        hc = kalibrierung.haircut_pp() / 100.0
    except Exception:
        hc = cfg.coverage_haircut_pp / 100.0
    for c in cands:
        c["coverage_raw"] = c["coverage"]
        c["coverage"] = max(c["coverage"] - hc, 0.0)
        c["edge_pp"] = (c["coverage"] - c["cost"]) * 100
        c["ev_pct"] = ((c["coverage"] * (cfg.budget / c["cost"]) / cfg.budget - 1) * 100
                       if c["cost"] > 0 else 0.0)

    if not cands:
        return None, "no viable block (books missing)", None

    # Diagnostics: the strongest block by edge across ALL candidates, attached to
    # every reject so a no-signal market still shows how close it came and on which
    # gate it died. Pure observability -- changes no gate and no threshold.
    def _diag(c):
        return {"diag_legs": [o["label"] for o in c["legs"]],
                "diag_coverage_raw": round(c["coverage_raw"], 4),
                "diag_coverage": round(c["coverage"], 4),
                "diag_edge_pp": round(c["edge_pp"], 2),
                "diag_cost": round(c["cost"], 4),
                "diag_min_depth_usd": round(c["min_depth"], 2),
                "diag_depth_ratio": round(c["depth_ratio"], 1),
                "diag_max_spread_bps": c["max_spread"]}
    diag = _diag(max(cands, key=lambda c: c["edge_pp"]))

    # Each gate reports the best value among the blocks that were still alive
    # when it ran, not among all of them. Reporting the global maximum produced
    # nonsense like "best coverage 76% < 70%": that 76% block had already been
    # killed by the cost gate, and the real best among affordable blocks was
    # below the line.
    def _fmt(pool_now, key, fmt):
        return fmt(max(c[key] for c in pool_now)) if pool_now else "n/a"

    pool = cands
    stages = [
        ("cost", lambda c: c["cost"] <= cfg.max_field_cost,
         lambda p: f"cheapest block {min(c['cost'] for c in p)*100:.0f}c > {cfg.max_field_cost*100:.0f}c cap"),
        ("coverage", lambda c: c["coverage"] >= min_cov,
         lambda p: f"best coverage {_fmt(p, 'coverage', lambda v: f'{v:.0%}')} "
                   f"< {min_cov:.0%}"),
        ("depth_ratio", lambda c: c["depth_ratio"] >= cfg.min_depth_ratio,
         lambda p: f"book thinner than {cfg.min_depth_ratio:.0f}x the order"),
        ("min_depth", lambda c: c["min_depth"] >= cfg.min_ask_depth_usd,
         lambda p: f"depth < ${cfg.min_ask_depth_usd:.0f} floor"),
        ("max_spread", lambda c: c["max_spread"] <= cfg.max_spread_bps,
         lambda p: f"spread > {cfg.max_spread_bps:.0f}bps"),
    ]
    for _, keep, why in stages:
        nxt = [c for c in pool if keep(c)]
        if not nxt:
            return None, why(pool), diag
        pool = nxt

    best = max(pool, key=lambda c: c["edge_pp"])
    if best["edge_pp"] < min_edge_pp:
        return None, (f"edge {best['edge_pp']:+.1f}pp < {min_edge_pp}pp "
                      f"(cov {best['coverage']:.0%}, cost {best['cost']*100:.0f}c)"), diag
    return best, None, diag


# ── 5. Full evaluation of one market ─────────────────────────────────────

def evaluate(ev, mode=None):
    """
    Run the whole pipeline on one event.
    Returns a snapshot dict — always, signal or not. Never raises.
    """
    mode = mode or cfg.mode
    now = datetime.now(timezone.utc)
    snap = {"ts": now.isoformat(), "slug": ev["slug"], "city": ev["city"],
            "date": ev["date"], "station": ev.get("station"),
            "icao": ev.get("icao"), "lat": ev.get("lat"), "lon": ev.get("lon"),
            "rounding": ev.get("rounding"), "mode": mode,
            "signal": False, "reason": None}

    try:
        end = datetime.fromisoformat(ev["end_date"].replace("Z", "+00:00"))
        lead_h = (end - now).total_seconds() / 3600
    except Exception:
        lead_h = 24.0
    snap["lead_h"] = round(lead_h, 1)

    if not (cfg.min_lead_h <= lead_h <= cfg.max_lead_h):
        snap["reason"] = f"lead {lead_h:.0f}h outside [{cfg.min_lead_h},{cfg.max_lead_h}]"
        return snap

    try:
        maxima, tz = member_day_maxima(ev["lat"], ev["lon"], ev["date"],
                                       key=ev.get("icao"))
    except Exception as e:
        snap["reason"] = f"forecast error: {e}"
        return snap
    if len(maxima) < cfg.min_members:
        snap["reason"] = f"only {len(maxima)} ensemble members"
        return snap

    snap.update({"ens_members": len(maxima),
                 "ens_mean": round(sum(maxima) / len(maxima), 2),
                 "ens_min": round(min(maxima), 2),
                 "ens_max": round(max(maxima), 2),
                 "dress_sigma": cfg.dress_sigma_c, "tz": tz})

    # -- Wunderground: the settlement source's OWN forecast for this station.
    # A better predictor of what WU will report than the Open-Meteo proxy.
    if cfg.wu_enabled:
        wu_high = wu_forecast(ev.get("description"), ev.get("icao"), ev["date"])
        if wu_high is not None:
            dev = abs(wu_high - snap["ens_mean"])
            if dev > cfg.wu_max_dev_c:
                # Wild outlier: almost certainly the feels-like value or the
                # wrong day scraped, not a real forecast disagreement. Do NOT
                # fold it -- record it for inspection and fall back to ensemble.
                snap["wu_high_c_rejected"] = wu_high
                snap["wu_reject_dev_c"] = round(dev, 1)
                log.info(f"{ev['city']} {ev['date']} WU {wu_high}C REJECTED: "
                         f"{dev:.1f}C off ensemble {snap['ens_mean']}C "
                         f"(>{cfg.wu_max_dev_c}C) -- likely feels-like/wrong-day, not folded")
            else:
                snap["wu_high_c"] = wu_high

    # -- Observation folding -------------------------------------------
    # Near resolution the models still argue about a peak the station has
    # already recorded. Floor every member at the temperature measured so
    # far today: no member may claim a day-max below what already happened.
    # Collapses the low tail on markets whose peak has passed; next-day
    # markets (no elapsed hours) are returned untouched.
    if cfg.obs_fold_enabled:
        obs_max, obs_n = observed_max_today(ev["lat"], ev["lon"], ev["date"],
                                            key=ev.get("icao"))
        if obs_max is not None and obs_n >= 3:
            spread_before = round(max(maxima) - min(maxima), 2)
            maxima = [max(m, obs_max) for m in maxima]
            snap["obs_max_c"] = obs_max
            snap["obs_hours"] = obs_n
            snap["ens_min_folded"] = round(min(maxima), 2)
            log.info(f"{ev['city']} {ev['date']} obs-fold: {obs_n}h measured, "
                     f"max_so_far {obs_max:.1f}C, spread {spread_before:.1f} "
                     f"-> {round(max(maxima) - min(maxima), 2):.1f}C")

    # -- Front signal: rain/cloud at the expected peak suppresses the high, which
    # is where the cheap low buckets pay. Visible in snapshot + log; already
    # reflected in the ensemble temperature, made explicit here.
    if cfg.front_signal_enabled:
        fs = front_signal(ev["lat"], ev["lon"], ev["date"], key=ev.get("icao"))
        if fs:
            snap["peak_hour"] = fs["peak_hour"]
            snap["peak_precip_pct"] = fs["peak_precip_pct"]
            snap["peak_cloud_pct"] = fs["peak_cloud_pct"]
            if (fs["peak_precip_pct"] or 0) >= cfg.front_precip_pct:
                log.info(f"{ev['city']} {ev['date']} FRONT RISK at peak {fs['peak_hour']}: "
                         f"precip {fs['peak_precip_pct']}%, cloud {fs['peak_cloud_pct']}% "
                         f"-> suppressed max likely, watch the low buckets")
            if cfg.taf_enabled:
                tf = taf_front(ev.get("icao"), ev["date"], fs["peak_hour"], fs.get("utc_offset", 0))
                if tf:
                    snap["taf_wx"] = tf["taf_wx"]
                    snap["taf_cloud_pct"] = tf["taf_cloud_pct"]
                    snap["taf_front"] = tf["taf_front"]
                    # Amtliche Temperaturvorhersage der Station, falls die
                    # Station sie ueberhaupt anhaengt. Nur mitschreiben.
                    snap["taf_tx_c"] = tf.get("taf_tx_c")
                    snap["taf_tn_c"] = tf.get("taf_tn_c")
                    snap["taf_tx_time"] = tf.get("taf_tx_time")
                    if tf.get("taf_tx_c") is not None:
                        log.info(f"{ev['city']} {ev['date']} TAF TX "
                                 f"{tf['taf_tx_c']}C vs unser Read "
                                 f"{snap.get('ens_mean')}C")
                    if tf["taf_front"]:
                        log.info(f"{ev['city']} {ev['date']} TAF FRONT ({ev.get('icao')}): "
                                 f"wx '{tf['taf_wx']}' cloud {tf['taf_cloud_pct']}% at peak "
                                 f"{fs['peak_hour']} -> INDEPENDENT suppression signal")

                mo = metar_observed(ev.get("icao"), ev["date"],
                                    fs.get("utc_offset", 0))
                if mo:
                    snap["metar_max_c"] = mo["metar_max_c"]
                    snap["metar_last_c"] = mo["metar_last_c"]
                    snap["metar_n"] = mo["metar_n"]
                    snap["metar_last_at"] = mo["metar_last_at"]

    sg, bias = city_params(ev["city"])
    # WU nudges the ensemble centre toward the settlement source's own forecast.
    # distribution() shifts warmer for a negative bias (mm = m - bias), so a WU
    # high above our mean yields a negative nudge = warmer, correcting the
    # proxy's cool lean. Weight 0..1; the ensemble keeps its full spread.
    if cfg.wu_enabled and snap.get("wu_high_c") is not None:
        wu_bias = round(cfg.wu_weight * (snap["ens_mean"] - snap["wu_high_c"]), 3)
        snap["wu_bias_c"] = wu_bias
        bias = round(bias + wu_bias, 3)
    # Confident tight bets: if the peak is already measured (obs folded) and we
    # are near resolution, shrink the dressing toward sigma_min so a sure 1-2
    # bucket call can clear coverage. Far-out markets keep the full sigma.
    if cfg.tight_near_resolution and snap.get("obs_max_c") is not None and lead_h is not None:
        shrink = min(1.0, max(lead_h, 0.0) / cfg.sigma_full_lead_h)
        sg = round(cfg.sigma_min + max(sg - cfg.sigma_min, 0.0) * shrink, 3)
        snap["sigma_eff"] = sg
    snap["city_sigma"] = sg
    snap["city_bias_c"] = bias
    q = distribution(maxima, ev["outcomes"], lead_h, sigma=sg, bias=bias)
    if not q:
        snap["reason"] = "distribution failed"
        return snap
    snap["full_dist"] = {o["label"]: round(p, 4) for o, p in zip(ev["outcomes"], q)}

    books = [orderbook(o["token_id"]) for o in ev["outcomes"]]

    # Ein Tagesmaximum kann nur STEIGEN. Hat die Station heute schon 18.3
    # gemeldet, ist jedes Feld, das bei 18.0 endet, mit Sicherheit verloren --
    # nicht unwahrscheinlich, sondern ausgeschlossen. Bisher blieben solche
    # Beine im Block und wurden mitbezahlt; der Markt nimmt dafuer weiter Geld,
    # weil er den Messwert nicht so schnell verarbeitet wie wir. Genau daraus
    # entsteht der Vorsprung -- also raus damit, bevor der Block gebaut wird.
    tote = 0
    _mm = snap.get("metar_max_c")
    if _mm is not None and cfg.metar_gate:
        for i, o in enumerate(ev["outcomes"]):
            hi = o.get("hi_c")
            if books[i] is not None and hi is not None and hi < 900 and hi < _mm - 1e-9:
                books[i] = None
                tote += 1
    if tote:
        snap["metar_dead_legs"] = tote

    if sum(b is not None for b in books) < 3:
        snap["reason"] = ("orderbooks unavailable" if not tote else
                          f"only {sum(b is not None for b in books)} fields left "
                          f"above the measured {_mm:.1f}C")
        return snap
    snap["market_prices"] = {o["label"]: (b["ask"] if b else None)
                             for o, b in zip(ev["outcomes"], books)}

    snap["best_coverage"] = round(max(sum(q[i] for i in range(s0, s0 + sz))
                                      for sz in (3, 4, 5)
                                      for s0 in range(0, max(len(q) - sz + 1, 1))
                                      if s0 + sz <= len(q)), 4) if len(q) >= 3 else None

    center_c = round(sum(maxima) / len(maxima) - bias, 2)
    snap["centre_c"] = center_c
    blk, why, diag = best_block(ev["outcomes"], q, books, mode,
                                lead_h=lead_h, center_c=center_c,
                                front=bool(snap.get("taf_front")))
    if not blk:
        snap["reason"] = why
        if diag:
            snap.update(diag)
        return snap

    # ── Was die Station bis jetzt gemessen hat ────────────────────────────
    # Das Tagesmaximum kann nur STEIGEN. Daraus folgen zwei harte Regeln, und
    # zwar aus einer Messung, nicht aus einer Prognose:
    #
    #   liegt das Maximum schon UEBER dem Block  -> die Wette kann nicht mehr
    #       gewinnen, also gar nicht erst veroeffentlichen
    #   liegt es schon IM Block                  -> ein guter Teil der Frage
    #       ist beantwortet, waehrend der Markt noch den vollen Preis nimmt.
    #       Das ist der groesste Informationsvorsprung des Tages.
    mmax = snap.get("metar_max_c")
    if mmax is not None and cfg.metar_gate:
        # Randbuckets sind offen: "30C or higher" deckt alles darueber ab.
        # Enthaelt der Block so ein Bein, gibt es nach oben keine Grenze —
        # sonst wuerde ein heisser Messwert faelschlich als tot gelten.
        los = [o.get("lo_c") for o in blk["legs"] if o.get("lo_c") is not None]
        his = [o.get("hi_c") for o in blk["legs"] if o.get("hi_c") is not None]
        lo = float("-inf") if any(x <= -900 for x in los) else min(los)
        hi = float("inf") if any(x >= 900 for x in his) else max(his)
        if mmax > hi + 1e-9:
            snap["reason"] = (f"station already at {mmax:.1f}C, above the block "
                              f"({lo:.1f}-{hi:.1f}C) — cannot win any more")
            snap["metar_dead"] = True
            if diag:
                snap.update(diag)
            return snap
        if mmax >= lo and blk["cost"] <= cfg.metar_load_max_cost:
            snap["metar_in_block"] = True
            log.info(f"{ev['city']} {ev['date']} STATION SCHON IM BLOCK: "
                     f"{mmax:.1f}C in {lo:.1f}-{hi:.1f}C, Block kostet "
                     f"{blk['cost']*100:.0f}c")

    snap.update({
        "signal": True,
        "front_widened": bool(blk.get("front_widened")),
        "robust_edge_pp": round(blk.get("robust_edge_pp", 0.0), 2),
        "thin_leg": blk.get("thin_leg"),
        "legs": [o["label"] for o in blk["legs"]],
        "token_ids": [o["token_id"] for o in blk["legs"]],
        "condition_ids": [o["condition_id"] for o in blk["legs"]],
        "market_slugs": [o.get("market_slug", "") for o in blk["legs"]],
        "event_slug": ev["slug"],
        "q": [round(x, 4) for x in blk["q"]],
        "asks": [round(a, 4) for a in blk["asks"]],
        "cost": round(blk["cost"], 4),
        "coverage": round(blk["coverage"], 4),
        "coverage_raw": round(blk.get("coverage_raw", blk["coverage"]), 4),
        "haircut_pp": round((blk.get("coverage_raw", blk["coverage"]) - blk["coverage"]) * 100, 2),
        "edge_pp": round(blk["edge_pp"], 2),
        "ev_pct": round(blk["ev_pct"], 2),
        "payout": round(blk["payout"], 2),
        "stakes": [round(s, 2) for s in blk["stakes"]],
        # Derived from the ROUNDED stakes, so the number we publish and the
        # split we print can never disagree by a rounding cent.
        "min_stake_usd": (
            math.ceil(cfg.min_order_usd * sum(round(s, 2) for s in blk["stakes"])
                      / min(round(s, 2) for s in blk["stakes"]) * 100) / 100
            if blk.get("stakes") and min(round(s, 2) for s in blk["stakes"]) > 0
            else 0.0),
        "min_depth_usd": blk["min_depth"],
        "depth_ratio": round(blk.get("depth_ratio", 0), 1),
        "max_spread_bps": blk["max_spread"],
        "budget": cfg.budget,
    })

    # ── Ehrliche Zahlen (seit 2.20.1) ────────────────────────────────────
    # Bis 2.20.0 stand in jedem Call die Abdeckung des Modells, im
    # zentrierten Modus sogar ganz ohne Abschlag. Gesagt 79 %, getroffen 67 %.
    # Ab jetzt: Preis + k * (Modell - Preis), mit k aus kalibrierung.py, also
    # gelernt aus allen bewerteten Calls. Das Modell bleibt sichtbar
    # (coverage_engine), aber gerechnet und gepostet wird die ehrliche Zahl.
    # Ein Call ist nur dann eine Wette ("bet"), wenn die ehrliche Zahl den
    # Preis um BET_MIN_EDGE_PP schlaegt. Sonst ist er eine Einschaetzung
    # ("read"): gezeigt, gemessen, aber ohne Einsatzvorschlag.
    try:
        import kalibrierung
        k = kalibrierung.vertrauen()
    except Exception:
        k = cfg.trust_start
    roh = snap["coverage_raw"]
    cost = snap["cost"]
    ehrlich = max(0.0, min(1.0, cost + k * (roh - cost)))
    snap["coverage_engine"] = roh
    snap["edge_raw_pp"] = round((roh - cost) * 100, 2)
    snap["trust_k"] = round(k, 3)
    snap["coverage"] = round(ehrlich, 4)
    snap["haircut_pp"] = round((roh - ehrlich) * 100, 2)
    snap["edge_pp"] = round((ehrlich - cost) * 100, 2)
    snap["ev_pct"] = round((ehrlich / cost - 1) * 100, 2) if cost > 0 else 0.0
    snap["grade"] = "bet" if snap["edge_pp"] >= cfg.bet_min_edge_pp else "read"

    # Signal whitelist: a fully-qualified signal on a non-whitelisted city is
    # kept and measured, but not published or traded. Proven cities only.
    if cfg.signal_whitelist and (ev.get("city") or "").lower() not in cfg.signal_whitelist:
        snap["signal"] = False
        snap["reason"] = "city not whitelisted (measured, not published)"
    elif (ev.get("city") or "").lower() in cfg.blocked_cities:
        snap["signal"] = False
        snap["reason"] = "city blocked (measured, not published)"
    elif cfg.min_field_cost and isinstance(snap.get("cost"), (int, float)) \
            and snap["cost"] < cfg.min_field_cost:
        snap["signal"] = False
        snap["reason"] = (f"block {snap['cost']*100:.0f}c < "
                          f"{cfg.min_field_cost*100:.0f}c floor (measured, not published)")
    elif cfg.max_edge_pp and isinstance(snap.get("edge_raw_pp"), (int, float)) \
            and snap["edge_raw_pp"] >= cfg.max_edge_pp:
        snap["signal"] = False
        snap["reason"] = (f"model edge {snap['edge_raw_pp']:+.1f}pp >= {cfg.max_edge_pp:.0f}pp cap: "
                          f"market disagrees too much (measured, not published)")
    elif snap.get("grade") == "read" and not cfg.publish_reads:
        snap["signal"] = False
        snap["reason"] = (f"read only: honest edge {snap['edge_pp']:+.1f}pp < "
                          f"{cfg.bet_min_edge_pp:.0f}pp (measured, not published)")
    return snap


POLY = "https://polymarket.com/event"


def links(snap):
    """
    One deep link per leg, plus the event itself.

    A signal that ends in "go find this market yourself" is a signal most
    people never act on. The subscriber taps, Polymarket opens on the exact
    outcome, they buy with their own wallet. We never hold anyone's keys.
    """
    ev = snap.get("event_slug") or ""
    out = []
    tot = sum(snap.get("stakes", [])) or 1.0
    lesen = snap.get("grade", "bet") != "bet"
    for lab, slug, stake in zip(snap.get("legs", []),
                                snap.get("market_slugs", []),
                                snap.get("stakes", [])):
        url = f"{POLY}/{ev}/{slug}" if (ev and slug) else (f"{POLY}/{ev}" if ev else None)
        if url:
            out.append((lab if lesen else f"{lab}  ·  {stake/tot*100:.0f}%", url))
    if ev:
        out.append(("Open full market", f"{POLY}/{ev}"))
    return out


def _floored(stake, weights, asks, floor):
    """What the exchange actually lets you do at a given total stake.

    Legs below the minimum order get raised to it, which inflates the total and
    skews the payouts. Returns (per-leg stakes, total actually spent, payout per
    leg). Uneven payouts are fine -- but a leg that returns LESS than the total
    spent means you can be right and still lose, so callers must check that."""
    tot = sum(weights) or 1.0
    real = [max(stake * w / tot, floor) for w in weights]
    spent = sum(real)
    pays = [(s / a if a > 0 else 0.0) for s, a in zip(real, asks)]
    return real, spent, pays


def _profitable_from(weights, asks, floor, cap=500.0):
    """Smallest total stake at which EVERY leg pays back more than was staked in
    total. Below this the minimum-order floor can make a winning leg a losing
    trade. Returns 0.0 if the block never clears it under the cap."""
    s = floor
    while s <= cap:
        _, spent, pays = _floored(s, weights, asks, floor)
        if pays and min(pays) > spent:
            return round(s, 2)
        s += 0.25
    return 0.0


def render(snap):
    """Signal text, Spec section 9 format, plus the two lines it was missing."""
    D = "──────────────"
    kind = "BET" if snap.get("grade", "bet") == "bet" else "READ"
    L = [f"ODDSENGINE · {snap['city'].upper()} · {kind}", "",
         f"Highest temperature in {snap['city'].title()}"]
    if snap.get("station"):
        L.append(snap["station"])
    L += [snap["date"], "", D, "",
          "🎯 Selected range",
          "   " + " · ".join(snap["legs"])]
    if snap.get("front_widened"):
        L += ["", "🌧 A front is forecast at the hottest hour, which tends to",
              "   cap the day's high. One extra bracket was added below."]
    # Seit 2.20.1: drei Zahlen statt "Edge". Was der Markt sagt, was das
    # Modell sagt, und was nach unserer eigenen Bilanz davon uebrig bleibt.
    bet = snap.get("grade", "bet") == "bet"
    eng = snap.get("coverage_engine", snap["coverage"])
    L += ["", "🔢 Weather model per bracket"]
    for lab, p in zip(snap["legs"], snap["q"]):
        L.append(f"   {lab} — {p:.0%}")
    L += ["", "🏷 Current prices"]
    for lab, a in zip(snap["legs"], snap["asks"]):
        L.append(f"   {lab} — {a*100:.1f}¢")
    L += ["", "📊 Odds for the whole block",
          f"   Market price · {snap['cost']*100:.1f}¢  (the market says {snap['cost']:.0%})",
          f"   Weather model · {eng:.0%}",
          f"   Honest odds · {snap['coverage']:.0%}  (model checked against our scored record)"]
    if bet:
        L += ["", f"🟢 BET · honest odds beat the price by {snap['edge_pp']:+.1f} pts", "", D, ""]
    else:
        L += ["", "👀 READ ONLY · no bet",
              "   The price already holds what the model sees. Our record says",
              "   the market is the better guide on calls like this, so there is",
              "   nothing to bet here. Posted so you can follow the engine.",
              "", D, "",
              f"Outside-range risk · {1-snap['coverage']:.0%}",
              f"Lead time · {snap['lead_h']:.0f}h",
              f"Published · {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
              "", D, "",
              "Disclaimer: Probabilities and prices reflect the data available "
              "when this was published. This is information, not financial advice."]
        return "\n".join(L)

    # Percentages, not dollars. The split is identical whether the reader
    # stakes $5 or $5,000, and we have no business implying we know which.
    L += ["💰 Suggested split of your stake"]
    tot = sum(snap["stakes"]) or 1.0
    for lab, st in zip(snap["legs"], snap["stakes"]):
        L.append(f"   {lab} — {st/tot*100:.1f}%")

    ex = cfg.example_bankroll * cfg.stake_pct / 100.0
    minst = snap.get("min_stake_usd") or 0.0
    mult = snap["payout"] / snap["budget"]
    floor = cfg.min_order_usd

    real, spent, pays = _floored(ex, snap["stakes"], snap["asks"], floor)
    raw = [ex * st / tot for st in snap["stakes"]]
    uneven = any(r < floor for r in raw)
    safe = bool(pays) and min(pays) > spent
    okfrom = _profitable_from(snap["stakes"], snap["asks"], floor) if not safe else 0.0

    L += ["", f"🧮 Example — ${ex:.2f} staked:"]
    for lab, s, p in zip(snap["legs"], real, pays):
        mark = "" if p > spent else "  ⚠"
        L.append(f"   {lab} — ${s:.2f}  →  ${p:.2f} back{mark}")
    if uneven:
        L.append(f"   Total ${spent:.2f}. Legs under ${floor:.0f} were raised to "
                 f"the ${floor:.0f} minimum, so the payouts above are uneven.")
    if not safe:
        L.append(f"   ⚠ A leg marked above returns less than the ${spent:.2f} "
                 f"total — winning it still loses money.")
        if okfrom:
            L.append(f"   Every leg clears the total from ${okfrom:.2f} up.")
        else:
            L.append("   No stake size fixes this block. Skip it or drop the "
                     "cheapest leg.")
    elif uneven and minst:
        L.append(f"   Even payout from ${minst:.2f} up.")
    # "Covered" is jargon. Say what actually happens: the split is built so
    # the payout is identical whichever of these lands. That is the whole
    # point of dutching and it should not need decoding.
    rng = " or ".join(snap["legs"])
    if uneven:
        L += ["", f"If ANY of {rng} is the day's high, you get the amount shown "
                  "next to that leg above.",
              "", "If it lands outside that range:",
              "the stake is gone.",
              "", f"On the ${spent:.2f} above:",
              f"   best case  →  ${max(pays):.2f}",
              f"   worst case →  ${min(pays):.2f}",
              f"   miss       →  $0.00"]
        # Dieselbe Zahl, die spaeter im Ergebnisbeitrag steht. So kann jeder
        # vorher sehen, was ein Treffer wert waere, und hinterher nachpruefen,
        # ob es gestimmt hat.
        # Die alte Fassung rechnete 1/Blockpreis und unterstellte damit
        # gleiche Auszahlung auf jedem Bein. Durch die 1-Dollar-Mindestorder
        # sind die Auszahlungen aber UNGLEICH -- die Zahl war schlicht falsch
        # und hat das Signal schlechter dargestellt als es ist. Jetzt aus den
        # echten Betraegen: was zurueckkommt und was noetig ist, jeweils als
        # Spanne, weil es davon abhaengt welches Bein trifft.
        if spent and pays and min(pays) > 0:
            _lo = (min(pays) / spent - 1) * 100
            _hi = (max(pays) / spent - 1) * 100
            _be_lo = spent / max(pays) * 100
            _be_hi = spent / min(pays) * 100
            L += ["", f"A hit returns {_lo:+.0f}% to {_hi:+.0f}% on the "
                      f"${spent:.2f} above, depending on which leg lands.",
                  f"To break even the block has to land {_be_lo:.0f}% to "
                  f"{_be_hi:.0f}% of the time."]
    else:
        L += ["", f"If ANY of {rng} is the day's high:",
              f"you get {mult:.2f}x your stake — the same, whichever one it is.",
              "", "If it lands outside that range:",
              "the stake is gone.",
              "", f"On the ${spent:.2f} example:",
              f"   hit  →  ${spent*mult:.2f}   (+${spent*(mult-1):.2f})",
              f"   miss →  $0.00        (-${spent:.2f})"]
    L += ["", D, "",
          f"Honest expected value · {snap['ev_pct']:+.1f}%",
          f"Outside-range risk · {1-snap['coverage']:.0%}",
          f"Lead time · {snap['lead_h']:.0f}h",
          f"Published · {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
          "", D, "",
          "Disclaimer: Probabilities, prices and suggested allocations reflect "
          "the data available when this signal was published. Weather models, "
          "market prices and liquidity may change after publication. This is "
          "information, not financial advice."]
    return "\n".join(L)
