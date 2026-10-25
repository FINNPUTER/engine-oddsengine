"""
Resolution stations — the exact point each market resolves on.

Built from the URLs inside the market rules themselves. Every WU rule links
to /history/daily/<cc>/<city>/<ICAO>, and every NOAA rule to
timeseries?site=<ICAO>. The ICAO code is the station's identity, so we parse
it out of the rules rather than guessing from the city name.

This matters more than it looks. Geocoding "nyc" lands in Manhattan;
the market resolves on LaGuardia, out on the water in Queens. In summer
that is regularly 1-2C apart — one to two whole buckets. The engine would
have been forecasting the wrong place, confidently, every single day.

Coordinates are the published airport reference points. Open-Meteo snaps to
a ~2-11 km grid, so sub-kilometre precision buys nothing; being at the right
airport buys everything.

  python3 stations.py            # audit the table against resolution_rules.json
"""

import json
import os
import re

# ICAO -> (lat, lon, name)
STATIONS = {
    # ── North America ────────────────────────────────────────────────
    "KLGA": (40.777, -73.872, "LaGuardia Airport"),              # nyc
    "KORD": (41.979, -87.904, "Chicago O'Hare Intl"),            # chicago
    "KATL": (33.640, -84.427, "Hartsfield-Jackson Intl"),        # atlanta
    "KMIA": (25.795, -80.290, "Miami Intl"),                     # miami
    "KDAL": (32.847, -96.852, "Dallas Love Field"),              # dallas
    "KHOU": (29.645, -95.279, "William P. Hobby"),               # houston
    "KAUS": (30.194, -97.670, "Austin-Bergstrom Intl"),          # austin
    "KBKF": (39.702, -104.751, "Buckley Space Force Base"),      # denver bis 22.10.2026
    "KDEN": (39.847, -104.656, "Denver Intl"),                   # denver ab 23.10.2026
    "KLAX": (33.942, -118.408, "Los Angeles Intl"),              # los angeles
    "KSFO": (37.619, -122.375, "San Francisco Intl"),            # san francisco
    "KSEA": (47.449, -122.309, "Seattle-Tacoma Intl"),           # seattle
    "CYYZ": (43.677, -79.631, "Toronto Pearson Intl"),           # toronto
    "MMMX": (19.436, -99.072, "Benito Juarez Intl"),             # mexico city
    "MPMG": (8.973, -79.556, "Marcos A. Gelabert Intl"),         # panama city

    # ── South America ────────────────────────────────────────────────
    "SAEZ": (-34.822, -58.536, "Minister Pistarini Intl"),       # buenos aires
    "SBGR": (-23.432, -46.470, "Sao Paulo-Guarulhos Intl"),      # sao paulo

    # ── Europe ───────────────────────────────────────────────────────
    "EGLC": (51.505, 0.055, "London City Airport"),              # london
    "LFPB": (48.969, 2.441, "Paris-Le Bourget"),                 # paris
    "EHAM": (52.309, 4.764, "Amsterdam Schiphol"),               # amsterdam
    "EDDM": (48.354, 11.786, "Munich Airport"),                  # munich
    "LIMC": (45.630, 8.723, "Milan Malpensa Intl"),              # milan
    "LEMD": (40.472, -3.561, "Adolfo Suarez Madrid-Barajas"),    # madrid
    "EPWA": (52.166, 20.967, "Warsaw Chopin"),                   # warsaw
    "EFHK": (60.317, 24.963, "Helsinki-Vantaa"),                 # helsinki
    "UUWW": (55.599, 37.261, "Vnukovo Intl"),                    # moscow  (NOAA)
    "LTFM": (41.262, 28.742, "Istanbul Airport"),                # istanbul (NOAA)
    "LTAC": (40.128, 32.995, "Esenboga Intl"),                   # ankara

    # ── Asia ─────────────────────────────────────────────────────────
    "RKSI": (37.469, 126.451, "Incheon Intl"),                   # seoul
    "RKPK": (35.180, 128.938, "Gimhae Intl"),                    # busan
    "RJTT": (35.553, 139.781, "Tokyo Haneda"),                   # tokyo
    "RCSS": (25.069, 121.552, "Taipei Songshan"),                # taipei
    "ZBAA": (40.080, 116.585, "Beijing Capital Intl"),           # beijing
    "ZSPD": (31.143, 121.805, "Shanghai Pudong Intl"),           # shanghai
    "ZGGG": (23.392, 113.299, "Guangzhou Baiyun Intl"),          # guangzhou
    "ZGSZ": (22.639, 113.811, "Shenzhen Bao'an Intl"),           # shenzhen
    "ZUCK": (29.719, 106.642, "Chongqing Jiangbei Intl"),        # chongqing
    "ZUUU": (30.578, 103.947, "Chengdu Shuangliu Intl"),         # chengdu
    "ZHHH": (30.784, 114.208, "Wuhan Tianhe Intl"),              # wuhan
    "ZSQD": (36.362, 120.375, "Qingdao Jiaodong Intl"),          # qingdao
    "ZSJN": (36.857, 117.216, "Jinan Yaoqiang Intl"),            # jinan
    "ZHCC": (34.520, 113.841, "Zhengzhou Xinzheng Intl"),        # zhengzhou
    "WSSS": (1.359, 103.989, "Singapore Changi"),                # singapore
    "WMKK": (2.746, 101.710, "Kuala Lumpur Intl"),               # kuala lumpur
    "RPLL": (14.509, 121.020, "Ninoy Aquino Intl"),              # manila
    "VILK": (26.761, 80.889, "Chaudhary Charan Singh Intl"),     # lucknow
    "OPMR": (24.894, 66.939, "Masroor Airbase"),                 # karachi
    "OPKC": (24.907, 67.161, "Karachi/Jinnah Intl"),             # karachi, seit 30.09. in den Regeln
    "OEJN": (21.680, 39.157, "King Abdulaziz Intl"),             # jeddah
    "LLBG": (32.011, 34.887, "Ben Gurion Intl"),                 # tel aviv (NOAA)

    # ── Africa / Oceania ─────────────────────────────────────────────
    "FACT": (-33.965, 18.602, "Cape Town Intl"),                 # cape town
    "NZWN": (-41.327, 174.805, "Wellington Intl"),               # wellington
}

# Hong Kong is the one market that does NOT resolve on an airport. It uses
# the Hong Kong Observatory's "Absolute Daily Max (deg. C)" at ONE DECIMAL
# PLACE, while all 50 others round to whole degrees. Different rounding
# convention means a different bucket geometry, and the rules never say
# where the edge sits. Excluded until measured against resolved markets.
# It was also net negative in the historical track record.
HK_OBSERVATORY = (22.302, 114.174, "Hong Kong Observatory")

EXCLUDED_CITIES = {"hong kong"}

# ICAO appears in both rule flavours. Note the segment count differs:
#   WU non-US .../history/daily/gb/london/EGLC            (country/city/ICAO)
#   WU US     .../history/daily/us/ny/new-york-city/KLGA  (country/STATE/city/ICAO)
#   NOAA      .../timeseries?site=UUWW
# Matching a fixed number of segments silently drops every US city.
ICAO_FROM_URL = [
    re.compile(r"/history/daily/(?:[a-z0-9\-]+/)+([A-Za-z]{4})/?\s*$", re.I),
    re.compile(r"/history/daily/(?:[a-z0-9\-]+/)+([A-Za-z]{4})\b", re.I),
    re.compile(r"[?&]site=([A-Za-z]{4})\b", re.I),
    # METAR-Regeln seit 23.10.2026: aviationweather.gov/api/data/metar?ids=EGLC
    re.compile(r"[?&]ids=([A-Za-z]{4})\b", re.I),
]


def icao_from_url(url):
    for rx in ICAO_FROM_URL:
        m = rx.search(url or "")
        if m:
            return m.group(1).upper()
    return None


def coords_for(url, city=None):
    """
    (lat, lon, name, icao) for a market, or None.

    The URL is authoritative — it is inside the rules the market resolves on.
    Never fall back to geocoding the city: that is how you end up forecasting
    Manhattan for a market that settles at LaGuardia.
    """
    if city and city.lower() in EXCLUDED_CITIES:
        return None
    icao = icao_from_url(url)
    if icao and icao in STATIONS:
        lat, lon, name = STATIONS[icao]
        return lat, lon, name, icao
    if icao:
        # Neue Stadt oder neue Station bei Polymarket. Bis 2.13 fiel der Markt
        # hier still heraus. Jetzt wird die Station bei aviationweather.gov
        # nachgeschlagen (amtliche ICAO-Daten, keine Geokodierung der Stadt)
        # und fuer den Rest des Laufs gemerkt. Gemeldet wird sie im privaten
        # Chat, damit jemand sie in STATIONS eintraegt.
        st = _nachschlagen(icao)
        if st:
            NEU[icao] = (city, st[2])
            return st[0], st[1], st[2], icao
        FEHLT[icao] = city
    elif city:
        FEHLT["?" + city] = city
    return None


# Stationen, die zur Laufzeit dazukamen oder fehlen. engine.discover()
# meldet beides einmal am Tag in den privaten Chat.
NEU = {}
FEHLT = {}
_NACHGESCHLAGEN = {}


def _nachschlagen(icao):
    if icao in _NACHGESCHLAGEN:
        return _NACHGESCHLAGEN[icao]
    st = None
    try:
        import json as _json
        import urllib.request as _u
        r = _u.Request("https://aviationweather.gov/api/data/stationinfo?ids={}&format=json"
                       .format(icao), headers={"User-Agent": "oddsengine/2.14"})
        with _u.urlopen(r, timeout=12) as f:
            d = _json.loads(f.read().decode() or "[]")
        if isinstance(d, dict):
            d = d.get("features") or d.get("data") or [d]
        for x in d or []:
            props = x.get("properties", x) if isinstance(x, dict) else {}
            lat = props.get("lat") if props.get("lat") is not None else props.get("latitude")
            lon = props.get("lon") if props.get("lon") is not None else props.get("longitude")
            if lat is None and isinstance(x, dict) and x.get("geometry"):
                lon, lat = x["geometry"]["coordinates"][:2]
            if lat is not None and lon is not None:
                name = props.get("site") or props.get("name") or icao
                st = (round(float(lat), 3), round(float(lon), 3), str(name))
                break
    except Exception:
        st = None
    _NACHGESCHLAGEN[icao] = st
    return st


def rounding_for(description):
    """
    Half-bucket width, straight from the rules.

    "whole degrees Celsius (eg, 9C)"  -> source rounds itself, so the bucket
        labelled 33 is exactly {T : round(T) == 33} = [32.5, 33.5). 0.5.
    "one decimal place (eg, 9.1C)"    -> the boundary is NOT specified
        anywhere in the rules. Only Hong Kong. Returns None -> skip.
    """
    d = (description or "").lower()
    if "whole degree" in d:
        return 0.5
    if "one decimal place" in d:
        return None
    return None


if __name__ == "__main__":
    path = "resolution_rules.json"
    if not os.path.exists(path):
        print(f"{path} not found — run rules_audit.py first.")
        raise SystemExit(1)

    rules = json.load(open(path))
    ok = missing = excluded = 0
    print(f"{'city':<15}{'icao':<7}{'round':<7}station")
    print("-" * 72)
    for city, d in sorted(rules.items()):
        url = d.get("url") or ""
        desc = d.get("description") or ""
        r = rounding_for(desc)
        c = coords_for(url, city)
        if city.lower() in EXCLUDED_CITIES:
            excluded += 1
            print(f"{city:<15}{'-':<7}{'-':<7}EXCLUDED (0.1C precision, boundary unknown)")
            continue
        if not c:
            missing += 1
            icao = icao_from_url(url) or "?"
            print(f"{city:<15}{icao:<7}{str(r):<7}!! NO COORDS — add {icao} to STATIONS")
            continue
        ok += 1
        print(f"{city:<15}{c[3]:<7}{str(r):<7}{c[2]}")

    print("-" * 72)
    print(f"{ok} usable · {missing} missing coords · {excluded} excluded")
    if missing:
        print("\nAdd the missing ICAOs to STATIONS before running the engine —"
              "\nthose cities are skipped, silently, otherwise.")
