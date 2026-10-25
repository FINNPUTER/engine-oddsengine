"""
OddsEngine — Public read API

Same repo as the worker, same Postgres, different start command. On Railway
this is a second service pointed at this file:

    uvicorn api:app --host 0.0.0.0 --port $PORT

Read-only. It never writes, never trades, holds no keys. The worker produces
snapshots; this hands them out.

    GET /                      health + version
    GET /v1/markets            latest evaluation per live market
    GET /v1/markets/{slug}     one market, full detail
    GET /v1/signals            recent signals only
    GET /v1/calibration        the reliability table — the actual product
    GET /v1/stations           the 50 resolution stations
    GET /v1/tiers              $ODDS tiers, delays, price
    GET /v1/holder/nonce       text for the wallet to sign (EVM personal_sign since 2.19)
    POST /v1/holder/verify     signature -> key + tier
    GET /v1/holder/me          tier for a key
    GET /v1/board              engine vs market for every open market (pro+)
    GET /v1/headstart          how far prices move after a signal

Since 2.29 also OddsEngine AI and engine.oddsengine.live, see ai_api.py.
"""

import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

import holder
import kalshi
import resolution
import stations as station_mod
import storage
from config import cfg

VERSION = "2.47.0"

app = FastAPI(
    title="OddsEngine API",
    description="Probability distributions for Polymarket temperature markets. "
                "Published before resolution, scored after.",
    version=VERSION,
    # Die Auto-Doku listete jeden Endpunkt samt Parametern. Wer die
    # Railway-Adresse kannte, hatte damit den kompletten Bauplan.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# The landing page and dashboard are on different origins (GitHub Pages).
@app.middleware("http")
async def _nicht_zwischenspeichern(request: Request, call_next):
    """Antworten unter /v1/ nie zwischenspeichern lassen.

    Ohne diesen Kopf darf der Browser -- und jeder Zwischenspeicher auf dem
    Weg -- eine alte Antwort wiederverwenden. Das Dashboard laedt zwar jede
    Minute neu, bekam aber unter Umstaenden dieselben Zahlen zurueck und sah
    deshalb eingefroren aus, obwohl der Dienst laengst neue Daten hatte.
    """
    antwort = await call_next(request)
    if request.url.path.startswith("/v1/"):
        antwort.headers["Cache-Control"] = "no-store, must-revalidate"
        antwort.headers["Pragma"] = "no-cache"
    return antwort


# Seit 2.31.2: Werkzeug-Endpunkte mit Kalibrierung, Backtests und Rohdaten nur mit
# Admin-Key. Keine oeffentliche Seite nutzt sie; sie zeigen, wie die Engine rechnet.
_NUR_ADMIN = ("/v1/backtest", "/v1/fieldbacktest", "/v1/fieldperf", "/v1/wutest",
              "/v1/calibration", "/v1/readcheck", "/v1/viertesband", "/v1/regeln",
              "/v1/positions", "/v1/reads")


@app.middleware("http")
async def _kern_schuetzen(request: Request, call_next):
    pfad = request.url.path.rstrip("/")
    if pfad in _NUR_ADMIN and not _admin_ok(request):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=403, content={"detail": "admin only"})
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    # POST wird fuer die Wallet-Anmeldung gebraucht, OPTIONS fuer den
    # Vorabflug des Browsers. Ohne beides blockt der Browser die Anfrage,
    # bevor sie ueberhaupt rausgeht -- sichtbar als "NetworkError".
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

import ai_api  # noqa: E402  (2.29: OddsEngine AI und engine.oddsengine.live)
app.include_router(ai_api.router)

BINS = [(0.0, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8),
        (0.8, 0.9), (0.9, 0.95), (0.95, 1.01)]


def _rows(since_h=72):
    """Snapshots from Postgres if wired, else the JSONL fallback."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_h)
    pg = storage._init_pg()
    if pg:
        try:
            cur = pg.execute(
                "SELECT payload FROM snapshots WHERE ts > %s ORDER BY ts DESC LIMIT 5000",
                (cutoff,))
            return [r[0] if isinstance(r[0], dict) else json.loads(r[0])
                    for r in cur.fetchall()]
        except Exception:
            pass
    out = []
    for s in storage.load_all():
        try:
            if datetime.fromisoformat(s["ts"]) > cutoff:
                out.append(s)
        except Exception:
            continue
    return list(reversed(out))


def _latest_per_market(rows):
    seen, out = set(), []
    for r in rows:                      # already newest-first
        # Eintraege mit einer Art (resolution, price_track, public_post)
        # tragen auch einen slug, sind aber keine Marktbewertung. Ohne diesen
        # Filter verdraengt die Preisspur den eigentlichen Schnappschuss.
        if r.get("kind"):
            continue
        k = r.get("slug")
        if k and k not in seen:
            seen.add(k)
            out.append(r)
    return out


@app.get("/")
def root():
    return {
        "service": "oddsengine",
        "version": VERSION,
        "signal_mode": cfg.signal_mode,
        "risk_mode": cfg.mode,
        "stations": len(station_mod.STATIONS),
        "status": "in calibration",
        "note": "Probabilities are published before resolution and scored "
                "against the resolution station afterwards. No performance "
                "figure is claimed until the reliability table has data.",
        "docs": "/docs",
    }


@app.get("/v1/markets")
def markets(request: Request, city: str | None = None, signals_only: bool = False,
            hours: int = Query(72, ge=1, le=336)):
    alle = _rows(hours)
    rows = _latest_per_market(alle)
    if city:
        rows = [r for r in rows if city.lower() in (r.get("city") or "")]
    if signals_only:
        rows = [r for r in rows if r.get("signal")]
    # Bis 2.13 stand hier alles offen: jeder konnte mit ?signals_only=true
    # die Signale lesen, an der Sperre von /v1/signals vorbei. Jetzt gilt
    # dieselbe Stufe wie ueberall. Unter pro nur der Schaufensterblick.
    stufe = _stufe(request)
    gesperrt = _gesperrte_maerkte(alle, stufe)
    if holder.RANG.get(stufe, 0) < holder.RANG["pro"] or gesperrt:
        return {"count": len(rows), "tier": stufe,
                "locked": holder.RANG.get(stufe, 0) < holder.RANG["pro"],
                "markets": [
                    (_schaufenster(r, stufe) if (holder.RANG.get(stufe, 0) < holder.RANG["pro"]
                                                 or r.get("slug") in gesperrt)
                     else _voll_markt(r))
                    for r in rows]}
    return {"count": len(rows), "tier": stufe, "locked": False, "markets": [
        {k: r.get(k) for k in
         ("slug", "city", "date", "station", "icao", "lead_h", "signal",
          "reason", "coverage", "cost", "edge_pp", "ev_pct", "legs", "q",
          "asks", "stakes", "payout", "best_coverage", "coverage_raw",
          "haircut_pp", "min_depth_usd", "depth_ratio", "max_spread_bps",
          "diag_legs", "diag_coverage_raw", "diag_coverage", "diag_edge_pp",
          "diag_cost", "diag_min_depth_usd", "diag_depth_ratio", "diag_max_spread_bps",
          "full_dist", "market_prices",
          "ens_members", "ens_mean", "wu_high_c", "wu_bias_c", "wu_high_c_rejected",
          "peak_hour", "peak_precip_pct", "peak_cloud_pct",
          "taf_wx", "taf_cloud_pct", "taf_front",
          # Ohne centre_c laesst sich nicht pruefen, ob unser Read bei den
          # ABGELEHNTEN Maerkten anders danebenliegt als bei den
          # veroeffentlichten — also ob wir falsch rechnen oder falsch
          # auswaehlen. Das Feld wird laengst gespeichert, es fehlte nur hier.
          "centre_c", "robust_edge_pp", "thin_leg", "front_widened",
          "grade", "trust_k", "coverage_engine", "edge_raw_pp",
          "taf_tx_c", "taf_tn_c", "taf_tx_time",
          "metar_max_c", "metar_last_c", "metar_n", "metar_last_at",
          "metar_dead", "metar_in_block", "posted", "ts")}
        for r in rows]}


_MARKT_FELDER = (
    "slug", "city", "date", "station", "icao", "lead_h", "signal",
    "reason", "coverage", "cost", "edge_pp", "ev_pct", "legs", "q",
    "asks", "stakes", "payout", "best_coverage", "coverage_raw",
    "haircut_pp", "min_depth_usd", "depth_ratio", "max_spread_bps",
    "diag_legs", "diag_coverage_raw", "diag_coverage", "diag_edge_pp",
    "diag_cost", "diag_min_depth_usd", "diag_depth_ratio", "diag_max_spread_bps",
    "full_dist", "market_prices",
    "ens_members", "ens_mean", "wu_high_c", "wu_bias_c", "wu_high_c_rejected",
    "peak_hour", "peak_precip_pct", "peak_cloud_pct",
    "taf_wx", "taf_cloud_pct", "taf_front",
    "centre_c", "robust_edge_pp", "thin_leg", "front_widened",
    "grade", "trust_k", "coverage_engine", "edge_raw_pp",
    "taf_tx_c", "taf_tn_c", "taf_tx_time",
    "metar_max_c", "metar_last_c", "metar_n", "metar_last_at",
    "metar_dead", "metar_in_block", "posted", "ts", "token_ids")


def _voll_markt(r):
    return {k: r.get(k) for k in _MARKT_FELDER}


@app.get("/v1/markets/{slug}")
def market(slug: str, request: Request):
    stufe = _stufe(request)
    alle = _rows(336)
    for r in alle:
        if r.get("slug") == slug:
            if (holder.RANG.get(stufe, 0) >= holder.RANG["pro"]
                    and slug not in _gesperrte_maerkte(alle, stufe)):
                return r
            return _schaufenster(r, stufe)
    raise HTTPException(404, "not found")


# ── Backtest ─────────────────────────────────────────────────────────────
# Runs backtest_roi.py server-side (the server can reach Open-Meteo; the
# sandbox that wrote this could not). Background thread so it never blocks the
# HTTP worker; poll the same URL for the result. Read-only, places no orders.
_bt = {"status": "idle", "output": "", "started": None, "n": None}


def _run_backtest(n, lead):
    _bt.update(status="running", output="",
               started=datetime.now(timezone.utc).isoformat(), n=n)
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        proc = subprocess.run(
            [sys.executable, "backtest_roi.py", "--n", str(n), "--lead", str(lead)],
            cwd=here, capture_output=True, text=True, timeout=1500)
        out = proc.stdout or ""
        if proc.stderr:
            out += "\n[stderr]\n" + proc.stderr[-2000:]
        _bt.update(status="done", output=out or "(no output)")
    except subprocess.TimeoutExpired:
        _bt.update(status="timeout", output="backtest exceeded 25 min — try a smaller --n")
    except Exception as e:
        _bt.update(status="error", output=f"{type(e).__name__}: {e}")


@app.get("/v1/backtest")
def backtest(request: Request, n: int = Query(60, ge=20, le=400),
             lead: float = Query(12, ge=1, le=48),
             restart: bool = False):
    """
    Replays the engine against ALREADY-RESOLVED Polymarket temperature markets,
    using the archived forecast from that time (no lookahead), and scores it
    against what actually happened. This is the same script that produced the
    261-market calibration. First call starts it in the background; poll the
    same URL again in 1-2 min for the result.
    """
    _intern(request)
    if _bt["status"] == "running":
        return {"status": "running", "started": _bt["started"], "n": _bt["n"],
                "note": "poll this URL again in ~1-2 min"}
    if restart or _bt["status"] == "idle":
        threading.Thread(target=_run_backtest, args=(n, lead), daemon=True).start()
        return {"status": "started", "n": n, "lead": lead,
                "note": "poll this URL again in ~1-2 min for the result"}
    return {"status": _bt["status"], "started": _bt["started"], "n": _bt["n"],
            "output": _bt["output"]}


# ── WU probe ─────────────────────────────────────────────────────────────
# Does THIS server reach Wunderground's forecast, or does Cloudflare block it?
# One dated URL, read-only, no orders. If the daily "High" comes back we can
# build the full per-city WU forecast module; if it's a block page, we know
# before building anything.
_WU_TEST = {
    "munich":       "https://www.wunderground.com/hourly/de/oberding/EDDM/date/{d}",
    "seoul":        "https://www.wunderground.com/hourly/kr/incheon/RKSI/date/{d}",
    "buenos aires": "https://www.wunderground.com/hourly/ar/ezeiza/SAEZ/date/{d}",
}


@app.get("/v1/fieldbacktest")
def fieldbacktest(request: Request, limit: int = 150, maxcost: float = 0.70, lead: float = 12.0, prices: bool = True, cities: str = None, since: str = None, until: str = None, biascorrect: bool = False, restart: bool = False, poll: bool = False):
    """Big-sample HIT-RATE backtest of the 3-field-around-ensemble over every
    resolved market we can reach. Batched archive (per-station date range) so
    hundreds of markets are feasible. First call starts it; poll the same URL."""
    _intern(request)
    import field_backtest
    try:
        if poll:
            return field_backtest.status()
        return field_backtest.start(limit=limit, maxcost=maxcost, lead=lead, prices=prices, cities=cities, since=since, until=until, biascorrect=biascorrect, restart=restart)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@app.get("/v1/fieldperf")
def fieldperf(request: Request, max: int = 50, maxcost: float = 0.80, corrected: bool = True):
    """Shadow-mode: score the ensemble+WU blend 3-bracket field against every
    resolved market we snapshotted. The forward test of the field strategy."""
    _intern(request)
    import field_scorer
    try:
        return field_scorer.run(max_markets=max, maxcost=maxcost, corrected=corrected)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@app.get("/v1/wutest")
def wutest(request: Request, city: str = "munich", path: str = "", find: str = ""):
    """Probe whether this server can read a Wunderground forecast page."""
    _intern(request)
    import requests
    d = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    if path:
        url = f"https://www.wunderground.com/hourly/{path.strip('/')}/date/{d}"
    else:
        tmpl = _WU_TEST.get(city.lower())
        if not tmpl:
            return {"error": f"no test url for {city!r}", "have": list(_WU_TEST)}
        url = tmpl.format(d=d)
    headers = {
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        r = requests.get(url, headers=headers, timeout=20)
        html = r.text or ""
    except Exception as e:
        return {"url": url, "error": f"{type(e).__name__}: {e}"}
    hi = re.search(r"High\s+(\d+)\s*F", html)
    lo = re.search(r"Low\s+(\d+)\s*F", html)
    all_highs = [{"F": int(m.group(1)),
                  "ctx": html[max(0, m.start() - 45):m.start() + 12]}
                 for m in re.finditer(r"High\s+(\d+)\s*F", html)][:8]
    find_hits = []
    if find:
        for m in list(re.finditer(re.escape(find), html))[:6]:
            find_hits.append(html[max(0, m.start() - 30):m.start() + 220])
    low_html = html.lower()
    blocked = any(s in low_html for s in
                  ("cf-browser-verification", "attention required",
                   "cloudflare", "captcha", "access denied"))
    return {
        "url": url,
        "http_status": r.status_code,
        "high_F": int(hi.group(1)) if hi else None,
        "high_C": round((int(hi.group(1)) - 32) * 5 / 9, 1) if hi else None,
        "low_F": int(lo.group(1)) if lo else None,
        "looks_blocked": blocked,
        "js_wall_no_high": ("enable JavaScript" in html and not hi),
        "all_high_matches": all_highs,
        "find_hits": find_hits,
        "html_len": len(html),
        "snippet": html[:500],
    }


# Felder, die den Handel ueberhaupt erst moeglich machen. Ohne sie sieht ein
# Besucher, DASS ein Signal laeuft und in welcher Stadt -- aber nicht, welche
# Temperaturfelder zu welchem Preis. Genau das ist das Produkt.
_BEZAHLT = ("legs", "asks", "q", "coverage", "edge_pp", "robust_edge_pp",
            "coverage_engine", "edge_raw_pp",
            "cost", "stakes", "payout", "budget", "token_ids", "ev_pct",
            "min_stake_usd", "thin_leg", "centre_c", "ens_mean", "wu_high_c")


def _intern(request: Request):
    """Schranke fuer die Forschungs- und Betriebsendpunkte.

    Diese Endpunkte legen die Methode offen: Backtests, Bias je Stadt,
    Prognosefehler, die eigenen Positionen. Nichts davon braucht ein Besucher,
    alles davon ist das Kapital. Solange ADMIN_KEY leer ist, bleibt es offen
    wie bisher -- der Deploy ist also gefahrlos und die Schranke wird spaeter
    per Variable eingeschaltet.
    """
    erwartet = os.getenv("ADMIN_KEY", "").strip()
    if not erwartet:
        return
    key = (request.headers.get("X-OddsEngine-Admin")
           or request.query_params.get("admin") or "").strip()
    if key != erwartet:
        raise HTTPException(status_code=404, detail="Not found")


# ── Anmeldung per Wallet ────────────────────────────────────────────────
#
# Kein Konto, kein Passwort. Der Kunde beweist mit einer Unterschrift, dass
# ihm eine Adresse gehoert; steht die Adresse auf der Abonnentenliste, bekommt
# er einen Sitzungsschluessel. Der Schluessel ist aus der Adresse ableitbar
# (HMAC mit dem Servergeheimnis) und muss deshalb nirgends gespeichert werden
# -- keine Datenbank, kein Zustand, nichts das verlorengehen kann.

def _geheim() -> str:
    return os.getenv("AUTH_SECRET", "").strip()


def _abonnenten() -> set:
    return {a.strip().lower()
            for a in os.getenv("SUBSCRIBERS", "").split(",") if a.strip()}


def _hmac(text: str) -> str:
    import hashlib
    import hmac as _h
    return _h.new(_geheim().encode(), text.encode(), hashlib.sha256).hexdigest()[:24]


def _nonce(adresse: str, fenster: int = 0) -> str:
    """Zeitgebundene Einmalzahl, gueltig fuer rund zehn Minuten.

    Ueber die Zeitscheibe statt ueber eine gespeicherte Liste, damit der Dienst
    zustandslos bleibt und ein Neustart niemanden aussperrt.
    """
    import time
    return _hmac("nonce:{}:{}".format(adresse.lower(), int(time.time() // 600) - fenster))


# ── Bezahlen: Zugang aus Zahlungseingaengen auf der Kette ───────────────
#
# Kein Konto, keine Datenbank, kein Zahlungsdienstleister. Der Kunde schickt
# USDC an eine feste Adresse; die API liest die Ueberweisungen direkt von
# Polygon und rechnet daraus die Tage aus. Jede Zahlung verlaengert -- wer
# zweimal zahlt, hat zwei Wochen.
#
# Noetige Variablen:
#   PAY_ADDRESS      wohin die Kunden zahlen
#   PAY_TOKEN        Vertragsadresse des Zahlungstokens (Standard: USDC.e)
#   PAY_PRO_WOCHE    Preis je Woche (Standard 25)
#   PAY_DEZIMAL      Nachkommastellen des Tokens (Standard 6)
# SUBSCRIBERS bleibt daneben bestehen -- fuer Freunde, Tests und Freiplaetze.

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
RPCS = ["https://polygon-bor-rpc.publicnode.com", "https://polygon.llamarpc.com",
        "https://polygon-rpc.com", "https://1rpc.io/matic"]
_ZAHL_CACHE = {}


def _rpc(methode, params):
    import urllib.request
    daten = json.dumps({"jsonrpc": "2.0", "id": 1,
                        "method": methode, "params": params}).encode()
    for u in RPCS:
        try:
            r = urllib.request.Request(u, data=daten,
                                       headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(r, timeout=15) as f:
                d = json.loads(f.read().decode())
            if "result" in d:
                return d["result"]
        except Exception:
            continue
    return None


def bezahlt_bis(adresse: str):
    """Bis wann hat diese Adresse bezahlt? Gibt einen Zeitstempel zurueck.

    Liest alle Ueberweisungen des Tokens von dieser Adresse an PAY_ADDRESS
    und rechnet je bezahltem Wochenpreis sieben Tage dazu. Zahlungen
    verlaengern einander, statt sich zu ueberschreiben.
    """
    import time as _t
    ziel = os.getenv("PAY_ADDRESS", "").strip().lower()
    if not ziel or not ziel.startswith("0x"):
        return None
    token = os.getenv("PAY_TOKEN",
                      "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174").strip().lower()
    preis = float(os.getenv("PAY_PRO_WOCHE", "25") or 25)
    dez = int(os.getenv("PAY_DEZIMAL", "6") or 6)
    adr = adresse.lower()

    # Fuenf Minuten zwischenspeichern. Ohne das fragt jede Anmeldung die
    # Kette ab, und bei mehreren Kunden wird das schnell zum Engpass.
    treffer = _ZAHL_CACHE.get(adr)
    if treffer and _t.time() - treffer[0] < 300:
        return treffer[1]

    kopf = _rpc("eth_blockNumber", [])
    if not kopf:
        return None
    jetzt = int(kopf, 16)
    # Rund 90 Tage zurueck; laenger lohnt bei einem Wochenabo nicht.
    von = max(0, jetzt - 90 * 43200)

    thema_von = "0x" + "0" * 24 + adr[2:]
    thema_zu = "0x" + "0" * 24 + ziel[2:]
    zahlungen = []
    b = von
    while b <= jetzt:
        bis = min(b + 100000, jetzt)
        logs = _rpc("eth_getLogs", [{"address": token,
                                     "fromBlock": hex(b), "toBlock": hex(bis),
                                     "topics": [TRANSFER, thema_von, thema_zu]}])
        for l in (logs or []):
            try:
                betrag = int(l.get("data") or "0x0", 16) / (10.0 ** dez)
                blk = int(l.get("blockNumber") or "0x0", 16)
                zahlungen.append((blk, betrag))
            except Exception:
                continue
        b = bis + 1

    if not zahlungen:
        _ZAHL_CACHE[adr] = (_t.time(), None)
        return None

    # Blocknummer in Zeit: Polygon macht rund einen Block alle zwei Sekunden.
    ende = 0.0
    for blk, betrag in sorted(zahlungen):
        wann = _t.time() - (jetzt - blk) * 2.0
        tage = (betrag / preis) * 7.0 * 86400.0
        ende = max(ende, wann) + tage
    _ZAHL_CACHE[adr] = (_t.time(), ende)
    return ende


def hat_zugang(adresse: str):
    """Freiplatz oder bezahlt? Gibt (bool, bis_wann_oder_None) zurueck."""
    import time as _t
    if adresse.lower() in _abonnenten():
        return True, None
    bis = bezahlt_bis(adresse)
    if bis and bis > _t.time():
        return True, bis
    return False, bis


def _sitzungsschluessel(adresse: str) -> str:
    return "{}.{}".format(adresse.lower(), _hmac("session:" + adresse.lower()))


def _sitzung_gueltig(key: str) -> bool:
    if not _geheim() or "." not in key:
        return False
    adr, sig = key.rsplit(".", 1)
    # Freiplatz ODER bezahlt. Laeuft die Zahlung aus, wird der Schluessel
    # von selbst ungueltig -- ohne dass irgendwo etwas geloescht werden muss.
    if not hat_zugang(adr)[0]:
        return False
    import hmac as _h
    return _h.compare_digest(sig, _hmac("session:" + adr.lower()))


@app.get("/v1/auth/nonce")
def auth_nonce(address: str = Query(..., min_length=42, max_length=42)):
    """Den Text liefern, den die Wallet unterschreiben soll."""
    if not _geheim():
        raise HTTPException(status_code=503, detail="auth not configured")
    n = _nonce(address)
    # Verraet nur, ob GENAU DIESE Adresse ein Abo hat -- das erfaehrt man beim
    # Anmelden ohnehin. Die Liste selbst bleibt drin. Spart beim Einrichten das
    # Unterschreiben zum Ausprobieren.
    return {"address": address,
            "subscribed": hat_zugang(address)[0],
            "subscriber_count": len(_abonnenten()),
            "pay_to": os.getenv("PAY_ADDRESS", ""),
            "price_per_week": float(os.getenv("PAY_PRO_WOCHE", "25") or 25),
            "nonce": n,
            "message": ("OddsEngine login\n"
                        "Address: {}\n"
                        "Nonce: {}\n\n"
                        "Signing costs nothing and sends no transaction."
                        ).format(address, n)}


@app.post("/v1/auth/verify")
async def auth_verify(request: Request):
    """Unterschrift pruefen und -- falls abonniert -- den Schluessel ausgeben."""
    if not _geheim():
        raise HTTPException(status_code=503, detail="auth not configured")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="bad body")
    adresse = str(body.get("address") or "").strip()
    unterschrift = str(body.get("signature") or "").strip()
    if not adresse or not unterschrift:
        raise HTTPException(status_code=400, detail="address and signature required")

    try:
        from eth_account import Account
        from eth_account.messages import encode_defunct
    except ImportError:
        raise HTTPException(status_code=503, detail="eth_account missing")

    # Zwei Zeitfenster zulassen, damit eine Unterschrift nicht genau an der
    # Zehn-Minuten-Grenze ungueltig wird, waehrend der Nutzer noch tippt.
    #
    # Und zwei Schreibweisen der Adresse: Wallets liefern sie mal mit
    # Grossbuchstaben nach EIP-55, mal komplett klein. Die Seite fragt die
    # Einmalzahl mit der einen Fassung ab und schickt beim Pruefen womoeglich
    # die andere -- dann steht im unterschriebenen Text eine andere Zeichenkette
    # als beim Nachbauen, und das Zurueckrechnen scheitert, obwohl alles stimmt.
    versuche = []
    for fenster in (0, 1):
        for schreib in (adresse, adresse.lower()):
            text = ("OddsEngine login\n"
                    "Address: {}\n"
                    "Nonce: {}\n\n"
                    "Signing costs nothing and sends no transaction."
                    ).format(schreib, _nonce(adresse, fenster))
            try:
                wer = Account.recover_message(encode_defunct(text=text),
                                              signature=unterschrift)
            except Exception as e:
                versuche.append("recover: " + str(e)[:60])
                continue
            versuche.append(wer)
            # Bis 2.13 stand diese Pruefung NACH den Schleifen und sah nur
            # den letzten Versuch. Passte ein frueherer, scheiterte die
            # Anmeldung trotzdem.
            if wer.lower() != adresse.lower():
                continue
            ok, bis = hat_zugang(adresse)
            if not ok:
                return {"ok": False, "subscribed": False,
                        "address": adresse,
                        "pay_to": os.getenv("PAY_ADDRESS", ""),
                        "price_per_week": float(os.getenv("PAY_PRO_WOCHE", "25") or 25),
                        "detail": "signature valid, no active subscription"}
            return {"ok": True, "subscribed": True, "address": adresse,
                    "paid_through": bis,
                    "key": _sitzungsschluessel(adresse)}
    # Nicht nur "passt nicht" melden, sondern WAS herauskam. Ohne diese Angabe
    # laesst sich von aussen nicht unterscheiden, ob die Unterschrift falsch war
    # oder der Text beim Nachbauen anders aussah.
    raise HTTPException(
        status_code=401,
        detail="signature does not match. expected {} got {}".format(
            adresse, " / ".join(dict.fromkeys(versuche)) or "nothing"))


# ── Stufen: wer sieht was, und ab wann ──────────────────────────────────
#
# Seit 2.14 ist $ODDS der Schluessel. Jede Anfrage bekommt eine Stufe
# (public, holder, pro, whale). Signale werden nicht fuer immer gesperrt,
# sondern ZEITVERSETZT freigegeben: whale sofort, pro nach 30, holder nach
# 60, alle anderen nach PUBLIC_DELAY_MIN (120) Minuten. Gemessen wird ab dem
# Zeitstempel der jeweiligen Fassung. Eine Aktualisierung startet ihre eigene
# Uhr, damit ein neuer Favorit nicht an der Warteschlange vorbeilaeuft.

_OFFEN = ("slug", "city", "date", "station", "icao", "lead_h", "ts",
          "posted", "signal", "update", "kind")


def _stufe(request: Request) -> str:
    admin = os.getenv("ADMIN_KEY", "").strip()
    akey = (request.headers.get("X-OddsEngine-Admin")
            or request.query_params.get("admin") or "").strip()
    if admin and akey and akey == admin:
        return "whale"
    key = (request.headers.get("X-OddsEngine-Key")
           or request.query_params.get("key") or "").strip()
    if not key:
        return "public"
    erlaubt = [k.strip() for k in os.getenv("SIGNAL_KEYS", "").split(",") if k.strip()]
    if key in erlaubt:
        return "whale"
    adr = holder.schluessel_adresse(key)
    if adr:
        return holder.status(adr).get("tier", "public")
    # Alte Wochenabos (USDC auf Polygon) laufen bis zu ihrem Ende als pro weiter.
    try:
        if _sitzung_gueltig(key):
            return "pro"
    except Exception:
        pass
    return "public"


def _ts(r):
    try:
        return datetime.fromisoformat(str(r.get("ts")).replace("Z", "+00:00"))
    except Exception:
        return None


def _frei_ab(r, stufe):
    t = _ts(r)
    if t is None:
        return None
    return t + timedelta(minutes=holder.verzoegerung(stufe))


def _sichtbar(r, stufe):
    """Seit 2.15: Holder sehen alles sofort, alle anderen nur die
    Gratis-Signale, die der Worker als free markiert."""
    return holder.sieht_alles(stufe) or bool(r.get("free"))


def _freigaben(r):
    return {s: (_frei_ab(r, s).isoformat() if _frei_ab(r, s) else None)
            for s in ("whale", "pro", "holder", "public")}


def _schaufenster(r, stufe):
    out = {k: r.get(k) for k in _OFFEN if k in r}
    out["locked"] = True
    if r.get("signal") and r.get("posted"):
        out["needs"] = "holder"
    return out


def _gesperrte_maerkte(rows, stufe):
    """Maerkte, deren NEUESTE veroeffentlichte Fassung diese Stufe noch nicht
    sehen darf. Das Board zeigt sie gesperrt, sonst verriete die Verteilung
    das Signal vor der Zeit."""
    neueste = {}
    for r in rows:
        if not (r.get("signal") and r.get("posted")):
            continue
        k = r.get("slug")
        if k and (k not in neueste or str(r.get("ts")) > str(neueste[k].get("ts"))):
            neueste[k] = r
    return {k for k, r in neueste.items() if not _sichtbar(r, stufe)}


def _zugang(request: Request) -> bool:
    """Alte Schnittstelle, noch von einzelnen Seiten benutzt."""
    return holder.RANG.get(_stufe(request), 0) >= holder.RANG["holder"]


@app.get("/v1/signals")
def signals(request: Request, hours: int = Query(168, ge=1, le=720)):
    stufe = _stufe(request)
    rows = [r for r in _rows(hours) if r.get("signal")]
    aus, zu = [], 0
    for r in rows:
        if _sichtbar(r, stufe):
            x = dict(r)
            x["locked"] = False
            aus.append(x)
        else:
            aus.append(_schaufenster(r, stufe))
            zu += 1
    return {"count": len(aus), "signals": aus, "tier": stufe,
            "all_signals": holder.sieht_alles(stufe),
            "free_per_day": int(float(os.getenv("FREE_PER_DAY", "1") or 1)),
            # "locked" bleibt fuer die alten Seiten: wahr, sobald etwas fehlt.
            "locked": zu > 0 and not any(not a.get("locked") for a in aus),
            "locked_count": zu}


# ── $ODDS Anmeldung (Robinhood Chain, EVM) ──────────────────────────────────────────

def _ca_sichtbar(request=None):
    """Die CA geht erst raus, wenn der Torch-Schalter an ist (seit 2.25.2).
    So kann ODDS_MINT vorher gesetzt und getestet werden, ohne dass sie leakt."""
    try:
        import features
        if features.an("torch"):
            return True
    except Exception:
        pass
    return bool(request is not None and _admin_ok(request))


def _kette_oeffentlich(sichtbar):
    k = dict(holder.ketten_info())
    if not sichtbar:
        k["buy_url"] = None
    return k


@app.get("/v1/tiers")
def tiers(request: Request):
    usd, quelle = (holder.kurs() if holder.mint() else (None, None))
    sichtbar = _ca_sichtbar(request)
    try:
        import features
        torch_an = features.an("torch")
    except Exception:
        torch_an = False
    return {"mint": (holder.mint() or None) if sichtbar else None,
            "live": bool(holder.mint()) and sichtbar, "torch": torch_an,
            "price_usd": usd, "price_source": quelle,
            "chain": _kette_oeffentlich(sichtbar),
            "tiers": holder.stufen_info()}


@app.get("/v1/holder/nonce")
def holder_nonce(address: str = Query(..., min_length=32, max_length=44)):
    if not holder._geheim():
        raise HTTPException(status_code=503, detail="auth not configured (AUTH_SECRET)")
    if not holder.adresse_ok(address):
        raise HTTPException(status_code=400, detail="not a valid wallet address for this chain")
    address = holder.norm(address)
    n = holder.nonce(address)
    return {"address": address, "nonce": n, "message": holder.nachricht(address, n),
            "mint": (holder.mint() or None) if _ca_sichtbar() else None,
            "chain": _kette_oeffentlich(_ca_sichtbar())}


@app.post("/v1/holder/verify")
async def holder_verify(request: Request):
    if not holder._geheim():
        raise HTTPException(status_code=503, detail="auth not configured (AUTH_SECRET)")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="bad body")
    adr = str(body.get("address") or "").strip()
    sig = body.get("signature")
    if not adr or sig is None or not holder.adresse_ok(adr):
        raise HTTPException(status_code=400, detail="address and signature required")
    adr = holder.norm(adr)
    try:
        ok = holder.pruefe_unterschrift(adr, sig)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=401, detail="signature does not match this address")
    st = holder.status(adr, frisch=True)
    out = dict(st)
    if not _ca_sichtbar(request):
        out["mint"] = None
    out["ok"] = True
    out["key"] = holder.schluessel(adr)
    out["tiers"] = holder.stufen_info()
    return out


@app.post("/v1/holder/telegram")
def holder_telegram(request: Request):
    """Seit 2.20: Einmal-Link, der dieses Wallet mit Telegram verknuepft.
    Der Bot liest /start <code> und weiss danach, welcher Telegram-Nutzer
    dieses Wallet besitzt. Kein Zugriff aufs Wallet, nur die Zuordnung."""
    key = (request.headers.get("X-OddsEngine-Key")
           or request.query_params.get("key") or "").strip()
    adr = holder.schluessel_adresse(key)
    if not adr:
        raise HTTPException(status_code=401, detail="session expired, sign in again")
    import agent
    if not agent.AN:
        raise HTTPException(status_code=503, detail="Telegram linking is switched off")
    r = agent.link_code(adr)
    if not r:
        raise HTTPException(status_code=503, detail="telegram linking needs the database")
    code, link = r
    return {"ok": True, "link": link, "bot": "@" + agent.bot_name(),
            "expires_min": agent.CODE_MIN}


@app.get("/v1/holder/me")
def holder_me(request: Request, fresh: bool = False):
    key = (request.headers.get("X-OddsEngine-Key")
           or request.query_params.get("key") or "").strip()
    adr = holder.schluessel_adresse(key)
    if not adr:
        raise HTTPException(status_code=401, detail="session expired, sign in again")
    out = dict(holder.status(adr, frisch=fresh))
    if not _ca_sichtbar(request):
        out["mint"] = None
    out["tiers"] = holder.stufen_info()
    return out


def _holder_adresse(request: Request):
    key = (request.headers.get("X-OddsEngine-Key")
           or request.query_params.get("key") or "").strip()
    adr = holder.schluessel_adresse(key)
    if not adr:
        raise HTTPException(status_code=401, detail="session expired, sign in again")
    return adr


@app.post("/v1/holder/link")
async def holder_link_start(request: Request):
    """Seit 2.23: eine weitere Wallet (z.B. die eingebettete Torch-Wallet)
    verknuepfen. Beweis per $ODDS-Transfer von dort an die bestaetigte Wallet."""
    adr = _holder_adresse(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        return holder.link_start(adr, str(body.get("address") or ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)[:160])


@app.get("/v1/holder/link")
def holder_link_check(request: Request):
    adr = _holder_adresse(request)
    try:
        return holder.link_pruefen(adr)
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)[:160])


@app.post("/v1/holder/unlink")
async def holder_unlink(request: Request):
    adr = _holder_adresse(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    return holder.link_loesen(adr, str(body.get("address") or ""))


# ── Engine-Board: Engine gegen Markt, fuer jeden offenen Markt ──────────

def _board_zeile(r):
    dist = r.get("full_dist") or {}
    preise = r.get("market_prices") or {}
    faecher = []
    for lab, q in dist.items():
        m = preise.get(lab)
        faecher.append({"label": lab, "engine": q, "market": m,
                        "gap_pp": (round((q - m) * 100, 1)
                                   if isinstance(q, (int, float)) and isinstance(m, (int, float))
                                   else None)})
    zeile = {k: r.get(k) for k in ("slug", "city", "date", "station", "lead_h",
                                    "ts", "signal", "posted", "centre_c",
                                    "ens_mean", "wu_high_c", "metar_max_c",
                                    "metar_last_c", "metar_last_at")}
    zeile["buckets"] = faecher
    if r.get("signal") and r.get("legs"):
        zeile["block"] = {k: r.get(k) for k in ("legs", "asks", "q", "cost",
                                                "coverage", "edge_pp", "token_ids")}
    gaps = [f["gap_pp"] for f in faecher if f["gap_pp"] is not None]
    zeile["top_gap_pp"] = max(gaps) if gaps else None
    return zeile


def _offen_markt(r):
    """Markttag noch nicht vorbei (grob, ueber lead_h ab dem Scan)."""
    t = _ts(r)
    try:
        lead = float(r.get("lead_h") or 0)
    except Exception:
        lead = 0
    return t is None or t + timedelta(hours=lead) > datetime.now(timezone.utc)


@app.get("/v1/board")
def board(request: Request, hours: int = Query(36, ge=1, le=96)):
    stufe = _stufe(request)
    alle = _rows(hours)
    rows = [r for r in _latest_per_market(alle)
            if r.get("full_dist") and _offen_markt(r)]
    zeilen = [_board_zeile(r) for r in rows]
    # Das juengste VEROEFFENTLICHTE Signal je Markt. Der letzte Scan kann
    # "kein Signal" sagen (Cooldown, Preis gelaufen), das Signal steht aber.
    sig = {}
    for x in alle:
        if x.get("kind") or not (x.get("signal") and x.get("posted")):
            continue
        k = x.get("slug")
        if k and (k not in sig or str(x.get("ts")) > str(sig[k].get("ts"))):
            sig[k] = x
    offen = {z["slug"] for z in zeilen}
    if holder.RANG.get(stufe, 0) < holder.RANG["pro"]:
        return {"locked": True, "tier": stufe, "needs": "pro",
                "markets": len(zeilen),
                "cities": len({z["city"] for z in zeilen}),
                "signals_live": len(offen & set(sig)),
                "stations_now": sum(1 for z in zeilen if z.get("metar_max_c") is not None)}
    aus = []
    for r, z in zip(rows, zeilen):
        s_ = sig.get(r.get("slug"))
        if s_ is not None:
            z["signal"] = True
            z["signal_ts"] = s_.get("ts")
            if _sichtbar(s_, stufe):
                z["block"] = {k: s_.get(k) for k in ("legs", "asks", "q", "cost",
                                                     "coverage", "edge_pp", "token_ids")}
            else:
                z = {k: z.get(k) for k in ("slug", "city", "date", "station", "lead_h", "ts")}
                z["signal"] = True
                z["locked"] = True
                z["needs"] = "holder"
        elif not r.get("signal"):
            z.pop("block", None)
        aus.append(z)
    aus.sort(key=lambda z: (str(z.get("date")), str(z.get("city"))))
    # Seit 2.17: Kalshi-Preise daneben, nur lesend. Faellt Kalshi aus, fehlt
    # das Feld einfach, das Board selbst haengt nicht davon ab.
    try:
        kalshi.vorladen([z.get("city") for z in aus if not z.get("locked")])
        for z in aus:
            if z.get("locked"):
                continue
            k = kalshi.fuer(z.get("city"), z.get("date"))
            if k and k.get("buckets"):
                z["kalshi"] = kalshi.neben_polymarket(k, z.get("buckets"),
                                                      k.get("same_station"))
    except Exception:
        pass
    return {"locked": False, "tier": stufe, "count": len(aus), "markets": aus,
            "kalshi": sum(1 for z in aus if z.get("kalshi"))}


@app.get("/v1/kalshi")
def kalshi_pruefen(request: Request, city: str = Query("nyc"), raw: int = Query(0)):
    """Zum Nachsehen, was von Kalshi ankommt. Nur Pro und hoeher."""
    if holder.RANG.get(_stufe(request), 0) < holder.RANG["pro"]:
        raise HTTPException(403, "pro only")
    return {"enabled": kalshi.AN, "cities": sorted(kalshi.SERIES),
            "city": city.lower(),
            "data": kalshi.serie_laden(city, roh=bool(raw))}


# ── Schaltpult und die Updates dahinter (seit 2.21) ─────────────────────
#
# Jedes Update laeuft im Hintergrund schon mit. Sichtbar wird es, wenn der
# Betreiber es auf der Admin-Seite freigibt. Mit Admin-Schluessel sieht man
# alles vorab, damit man pruefen kann, bevor die Holder es sehen.

def _admin_ok(request: Request):
    erwartet = os.getenv("ADMIN_KEY", "").strip()
    if not erwartet:
        return False
    key = (request.headers.get("X-OddsEngine-Admin")
           or request.query_params.get("admin") or "").strip()
    return bool(key) and key == erwartet


def _freigegeben(request: Request, name):
    import features
    return features.an(name) or _admin_ok(request)


@app.get("/v1/features")
def features_lesen(request: Request):
    import features
    out = {"features": features.alle()}
    if _admin_ok(request):
        out["admin"] = True
        out["list"] = features.beschreibung()
    return out


@app.post("/v1/admin/features")
async def features_schalten(request: Request):
    if not os.getenv("ADMIN_KEY", "").strip():
        raise HTTPException(status_code=403, detail="set ADMIN_KEY on the web service first")
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="wrong admin key")
    try:
        body = await request.json()
    except Exception:
        body = {}
    import features
    name = str(body.get("name") or "")
    if name not in features.LISTE:
        raise HTTPException(status_code=400, detail="unknown feature")
    ok, werte = features.setzen(name, bool(body.get("on")))
    if not ok:
        raise HTTPException(status_code=503, detail="switches need the database")
    print(f"features: {name} -> {'on' if werte.get(name) else 'off'}", flush=True)
    return {"ok": True, "features": werte, "list": features.beschreibung()}


def _stufen_zahl(label):
    t = str(label or "").replace("−", "-").lower()
    n = [float(x) for x in re.findall(r"(?:(?<=^)|(?<=\s))-?\d+(?:\.\d+)?|\d+(?:\.\d+)?", t)]
    if not n:
        return None
    if "below" in t or "lower" in t:
        return (-999.0, n[0])
    if "higher" in t or "above" in t:
        return (n[0], 999.0)
    return (n[0], n[-1])


@app.get("/v1/station")
def station_live(request: Request):
    """Station Live: was die Abrechnungsstation heute schon gemessen hat.
    Das Tageshoch kann nur steigen. Stufen unter dem bisherigen Hoch sind
    damit raus, egal was noch kommt."""
    if not _freigegeben(request, "station"):
        return {"enabled": False}
    rows = [r for r in _latest_per_market(_rows(30))
            if r.get("metar_max_c") is not None and _offen_markt(r)]
    aus = []
    for r in rows:
        labels = list((r.get("full_dist") or {}).keys())
        if not labels:
            continue
        f = any("°f" in str(l).lower() or "f" == str(l).strip()[-1:].lower() for l in labels)
        def umr(c):
            if c is None:
                return None
            v = c * 9 / 5 + 32 if f else c
            return int(v + 0.5) if v >= 0 else -int(-v + 0.5)
        hoch = umr(r.get("metar_max_c"))
        zuletzt = umr(r.get("metar_last_c"))
        preise = r.get("market_prices") or {}
        stufen = []
        for l in sorted(labels, key=lambda x: (_stufen_zahl(x) or (0, 0))[0]):
            g = _stufen_zahl(l)
            if not g:
                continue
            if g[1] < hoch:
                zust = "out"
            elif g[0] <= hoch <= g[1]:
                zust = "live"
            else:
                zust = "open"
            stufen.append({"label": l, "state": zust, "market": preise.get(l)})
        aus.append({"slug": r.get("slug"), "city": r.get("city"), "date": r.get("date"),
                    "station": r.get("station"), "icao": r.get("icao"),
                    "unit": "F" if f else "C", "high_so_far": hoch, "last": zuletzt,
                    "last_at": r.get("metar_last_at"), "readings": r.get("metar_n"),
                    "lead_h": r.get("lead_h"), "scan_ts": r.get("ts"),
                    "out": sum(1 for s in stufen if s["state"] == "out"),
                    "brackets": stufen})
    aus.sort(key=lambda z: (z.get("lead_h") or 99))
    return {"enabled": True, "count": len(aus), "stations": aus,
            "note": "Station readings via aviationweather.gov METAR, local time. "
                    "The day's high can only go up, so brackets below it are out."}


@app.get("/v1/crowd")
def crowd_lesen(request: Request):
    """Engine vs Crowd: der Lieblingsblock des Markts gegen die Calls der
    Engine, beide ab demselben Tag bewertet."""
    if not _freigegeben(request, "crowd"):
        return {"enabled": False}
    import crowd
    erg = sorted(_art("crowd_result", 365), key=lambda r: str(r.get("date")), reverse=True)
    seit = min((str(r.get("date")) for r in erg), default=None)
    eng = [r for r in _art("resolution", 365)
           if r.get("outcome_known") and seit and str(r.get("date")) >= seit
           and isinstance(r.get("cost"), (int, float)) and r["cost"] > 0]
    def kurz(xs):
        n = len(xs)
        if not n:
            return {"n": 0}
        h = sum(1 for r in xs if r.get("outcome_hit"))
        roi = sum((1 / r["cost"] - 1) if r.get("outcome_hit") else -1 for r in xs) / n
        return {"n": n, "hits": h, "hit_rate": round(h / n, 4),
                "price_said": round(sum(r["cost"] for r in xs) / n, 4),
                "roi_pct": round(roi * 100, 2)}
    offen = sorted(crowd.offen(), key=lambda p: str(p.get("date")))
    felder = ("city", "date", "station", "legs", "cost", "model", "engine_legs",
              "outcome_label", "outcome_hit", "lead_h")
    return {"enabled": True, "since": seit, "crowd": crowd.bilanz(erg), "engine": kurz(eng),
            "open": [{k: p.get(k) for k in felder} for p in offen[:40]],
            "recent": [{k: r.get(k) for k in felder} for r in erg[:30]],
            "band": [crowd.MIN_KOSTEN, crowd.MAX_KOSTEN]}


# ── Beat the Engine (seit 2.26, hinter dem Schalter "beat") ─────────────

def _beat_spieler(request: Request):
    """Adresse des angemeldeten Holders oder None. Wirft nicht."""
    key = (request.headers.get("X-OddsEngine-Key")
           or request.query_params.get("key") or "").strip()
    try:
        return holder.schluessel_adresse(key) if key else None
    except Exception:
        return None


def _beat_darf(adr):
    try:
        st = holder.status(adr)
        return holder.RANG.get(st.get("tier"), 0) >= holder.RANG["holder"]
    except Exception:
        return False


@app.get("/v1/beat")
def beat_lesen(request: Request):
    if not _freigegeben(request, "beat"):
        return {"enabled": False}
    import beat
    adr = _beat_spieler(request)
    out = beat.lage(adr)
    out["enabled"] = True
    out["can_play"] = bool(adr) and _beat_darf(adr)
    return out


@app.post("/v1/beat/pick")
async def beat_tipp(request: Request):
    if not _freigegeben(request, "beat"):
        raise HTTPException(status_code=403, detail="Beat the Engine is not live yet")
    adr = _holder_adresse(request)
    if not _beat_darf(adr):
        raise HTTPException(status_code=403, detail="hold $ODDS at holder level to play")
    try:
        body = await request.json()
    except Exception:
        body = {}
    import beat
    try:
        return beat.tippen(adr, str(body.get("slug") or ""), str(body.get("label") or ""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)[:160])


@app.post("/v1/beat/name")
async def beat_name(request: Request):
    if not _freigegeben(request, "beat"):
        raise HTTPException(status_code=403, detail="Beat the Engine is not live yet")
    adr = _holder_adresse(request)
    if not _beat_darf(adr):
        raise HTTPException(status_code=403, detail="hold $ODDS at holder level to play")
    try:
        body = await request.json()
    except Exception:
        body = {}
    import beat
    try:
        return {"name": beat.name_setzen(adr, body.get("name"))}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)[:160])


@app.get("/v1/feed")
def engine_feed(request: Request, n: int = Query(60, ge=1, le=150)):
    """Engine Live: was die Engine zuletzt getan hat. Holder sehen bei Calls
    die Zahlen, alle anderen nur, dass einer rausging."""
    import feed
    holder_ = holder.RANG.get(_stufe(request), 0) >= holder.RANG["holder"] or _admin_ok(request)
    aus = []
    for z in feed.lesen(n):
        aus.append({"ts": z.get("ts"), "kind": z.get("kind"), "city": z.get("city"),
                    "text": (z.get("holder_text") if holder_ and z.get("holder_text") else z.get("text"))})
    return {"lines": aus, "holder": bool(holder_)}


@app.get("/v1/map")
def engine_map(request: Request):
    """Seit 2.24: jede Stadt als Punkt auf der Karte. Oeffentlich: Station,
    naechster Markttag, Favorit des Markts, Stationswerte, ob gerade ein Call
    offen ist (ohne Inhalt)."""
    alle = _rows(48)
    rows = [r for r in _latest_per_market(alle) if r.get("full_dist") and _offen_markt(r)
            and isinstance(r.get("lat"), (int, float)) and isinstance(r.get("lon"), (int, float))]
    sig = {x.get("slug") for x in alle if not x.get("kind") and x.get("signal") and x.get("posted")}
    je_stadt = {}
    for r in rows:
        k = str(r.get("city")).lower()
        if k not in je_stadt or (r.get("lead_h") or 99) < (je_stadt[k].get("lead_h") or 99):
            je_stadt[k] = r
    aus = []
    for k, r in je_stadt.items():
        labels = list((r.get("full_dist") or {}).keys())
        preise = r.get("market_prices") or {}
        f = any("°f" in str(l).lower() for l in labels)
        def umr(c):
            if c is None:
                return None
            v = c * 9 / 5 + 32 if f else c
            return int(v + 0.5) if v >= 0 else -int(-v + 0.5)
        # Favorit = das einzelne Feld mit dem hoechsten Preis. Frueher stand
        # hier ein Dreierblock, der kostet fast immer ~1$ und sagt nichts.
        # Unter 2c ist das Buch leer oder der Markt schon entschieden: dann
        # zeigen wir keinen Favoriten statt "0c".
        fav = None
        try:
            kand = [(l, float(preise[l])) for l in labels
                    if isinstance(preise.get(l), (int, float))]
            if kand:
                l, p = max(kand, key=lambda t: t[1])
                if 0.02 <= p <= 0.99:
                    fav = {"legs": [l], "cost": round(p, 3)}
        except Exception:
            fav = None
        aus.append({"city": r.get("city"), "station": r.get("station"), "icao": r.get("icao"),
                    "lat": round(float(r["lat"]), 3), "lon": round(float(r["lon"]), 3),
                    "date": r.get("date"), "lead_h": r.get("lead_h"), "unit": "F" if f else "C",
                    "high_so_far": umr(r.get("metar_max_c")), "last": umr(r.get("metar_last_c")),
                    "last_at": r.get("metar_last_at"), "favourite": fav,
                    "call": r.get("slug") in sig})
    aus.sort(key=lambda z: z["lon"])
    return {"count": len(aus), "cities": aus,
            "reporting": sum(1 for z in aus if z["high_so_far"] is not None),
            "calls": sum(1 for z in aus if z["call"])}


@app.get("/v1/kalshi/record")
def kalshi_bilanz(request: Request):
    """Seit 2.22: Engine-Calls und Crowd-Picks, gewertet zu Kalshi-Preisen
    und nach Kalshis eigener Abrechnung."""
    if not _freigegeben(request, "kalshi"):
        return {"enabled": False}
    import kalshi_spur
    erg = sorted(_art("kalshi_result", 365), key=lambda r: str(r.get("date")), reverse=True)
    felder = ("source", "city", "date", "legs", "cost", "polymarket_cost", "same_station",
              "kalshi_station", "outcome_label", "outcome_hit")
    offen = sorted(kalshi_spur.offen(), key=lambda p: str(p.get("date")))
    return {"enabled": True,
            "since": min((str(r.get("date")) for r in erg), default=None),
            "engine": kalshi_spur.bilanz(erg, "engine"),
            "crowd": kalshi_spur.bilanz(erg, "crowd"),
            "open": [{k: p.get(k) for k in felder} for p in offen[:30]],
            "recent": [{k: r.get(k) for k in felder} for r in erg[:30]],
            "note": "Scored on Kalshi's own settlement, at the Kalshi ask when the call "
                    "went out, before Kalshi trading fees."}


@app.get("/v1/kalshi/compare")
def kalshi_vergleich(request: Request):
    """Kalshi neben Polymarket, fuer jede Stadt, die beide handeln."""
    if not _freigegeben(request, "kalshi"):
        return {"enabled": False}
    if not kalshi.AN:
        return {"enabled": True, "markets": [], "note": "Kalshi feed switched off"}
    rows = {(str(r.get("city")).lower(), str(r.get("date"))): r
            for r in _latest_per_market(_rows(48)) if r.get("full_dist") and _offen_markt(r)}
    kalshi.vorladen(list(kalshi.SERIES))
    aus = []
    for stadt, (serie, kst, gleich) in kalshi.SERIES.items():
        for datum, k in sorted((kalshi.serie_laden(stadt) or {}).items()):
            r = rows.get((stadt, datum))
            if r is None:
                continue
            z = _board_zeile(r)
            v = kalshi.neben_polymarket(k, z.get("buckets"), gleich)
            if not v.get("aligned"):
                continue
            aus.append({"city": stadt, "date": datum, "polymarket_station": r.get("station"),
                        "kalshi_station": kst, "same_station": bool(gleich),
                        "kalshi_url": k.get("url"), "polymarket_slug": r.get("slug"),
                        "buckets": [{kk: b.get(kk) for kk in ("label", "lo", "hi", "ask", "bid",
                                                               "polymarket")}
                                    for b in v.get("buckets") or []]})
    aus.sort(key=lambda z: (z["date"], z["city"]))
    return {"enabled": True, "count": len(aus), "markets": aus,
            "note": "Kalshi ask next to the Polymarket ask for the same bracket. "
                    "Only where both settle on the same station is it the same question."}


# ── Vorsprung messen: wie weit laeuft der Preis nach dem Signal? ────────

def _art(kind, tage=90):
    pg = storage._init_pg()
    if pg:
        try:
            cur = pg.execute(
                "SELECT payload FROM snapshots WHERE payload->>'kind' = %s "
                "AND ts > now() - (%s || ' days')::interval ORDER BY ts DESC",
                (kind, str(tage)))
            return [r[0] if isinstance(r[0], dict) else json.loads(r[0])
                    for r in cur.fetchall()]
        except Exception:
            pass
    return [r for r in storage.load_all() if r.get("kind") == kind]


@app.get("/v1/headstart")
def headstart():
    """Der Beweis fuer den Vorsprung. Nach jedem Signal notiert der Worker bei
    jedem Scan, was derselbe Block JETZT kostet. Steigt der Preis, hat wer
    frueher kaufte einen besseren Einstieg gehabt. Faellt er, auch das steht
    hier. Oeffentlich, weil es die Aussage ist, mit der der Token steht."""
    spur = _art("price_track")
    stufen = [(0, 45, "~30 min"), (45, 75, "~60 min"), (75, 105, "~90 min"),
              (105, 150, "~120 min"), (150, 240, "~3 h")]
    aus = []
    for lo, hi, name in stufen:
        b = [x for x in spur if lo <= (x.get("age_min") or -1) < hi
             and isinstance(x.get("cost_signal"), (int, float))
             and isinstance(x.get("cost_now"), (int, float))]
        if not b:
            aus.append({"after": name, "n": 0})
            continue
        dz = [(x["cost_now"] - x["cost_signal"]) * 100 for x in b]
        aus.append({"after": name, "n": len(b),
                    "avg_move_c": round(sum(dz) / len(dz), 2),
                    "share_up": round(sum(1 for d in dz if d > 0.5) / len(dz), 3),
                    "share_down": round(sum(1 for d in dz if d < -0.5) / len(dz), 3)})
    pub = [x for x in _art("public_post")
           if isinstance(x.get("cost_signal"), (int, float))
           and isinstance(x.get("cost_public"), (int, float))]
    dp = [(x["cost_public"] - x["cost_signal"]) * 100 for x in pub]
    return {"note": "Price of the same block after the signal, in cents. "
                    "Positive means whoever bought at signal time got in cheaper.",
            "by_age": aus,
            "public_posts": {"n": len(pub),
                             "avg_move_c": round(sum(dp) / len(dp), 2) if dp else None},
            "since": min((str(x.get("ts")) for x in spur), default=None)}


@app.get("/v1/calibration")
def calibration():
    """
    The only claim worth making: when we say 90%, does it happen 90%?

    Bins stay empty until signals resolve. That is deliberate. An empty table
    is honest; a number with no producer behind it is what the old dashboard
    did, and it said "SYSTEM ACTIVE" for six days after the server was gone.
    """
    st = resolution.stats()
    res = [r for r in _rows(24 * 90) if r.get("kind") == "resolution"]

    bins = []
    for lo, hi in BINS:
        b = [r for r in res if lo <= (r.get("said_coverage") or 0) < hi]
        obs = (sum(1 for r in b if r.get("outcome_hit")) / len(b)) if b else None
        pred = (sum(r["said_coverage"] for r in b) / len(b)) if b else None
        bins.append({"range": f"{lo:.0%}-{hi:.0%}", "n": len(b),
                     "predicted": round(pred, 4) if pred is not None else None,
                     "observed": round(obs, 4) if obs is not None else None,
                     "error_pp": round((obs - pred) * 100, 2)
                     if (obs is not None and pred is not None) else None})

    out = {"status": "live" if st["signals_resolved"] >= 100 else "in calibration",
           "signals_published": sum(1 for r in _rows(24 * 90) if r.get("signal")),
           "bins": bins,
           "note": "observed frequencies appear here as markets resolve"}
    out.update(st)
    return out


def _resolutions():
    """Alle Aufloesungen, ohne das allgemeine Zeilenlimit.

    _rows() holt die juengsten 5000 Schnappschuesse. Bei rund 120 gescannten
    Maerkten pro Stunde reicht das keine zwei Tage zurueck — aeltere
    Aufloesungen fielen still aus dem Record, und die Bilanz wurde mit jedem
    Tag unvollstaendiger. Deshalb hier gezielt nach der Art filtern.
    """
    pg = storage._init_pg()
    if pg:
        try:
            cur = pg.execute(
                "SELECT payload FROM snapshots "
                "WHERE payload->>'kind' = 'resolution' ORDER BY ts DESC")
            return [r[0] if isinstance(r[0], dict) else json.loads(r[0])
                    for r in cur.fetchall()]
        except Exception:
            pass
    return [r for r in storage.load_all() if r.get("kind") == "resolution"]


@app.get("/v1/reads")
def reads(request: Request):
    """Fuer JEDEN gescannten Markt-Tag der letzte Read — ohne Kappung.

    /v1/markets ist auf 5000 Zeilen gedeckelt und reicht damit keine zwei Tage
    zurueck. Fuer die Stadt-Genauigkeit ueber ALLE gescannten Maerkte (auch die
    nie gesetzten) brauchen wir aber die volle Historie. Deshalb hier direkt aus
    der Datenbank, je (Stadt, Datum) der spaeteste Snapshot mit einem Read.
    """
    _intern(request)
    best = {}
    pg = storage._init_pg()
    if pg:
        try:
            cur = pg.execute(
                "SELECT payload FROM snapshots "
                "WHERE payload->>'centre_c' IS NOT NULL "
                "AND payload->>'kind' IS DISTINCT FROM 'resolution' "
                "ORDER BY ts ASC")
            src = (r[0] if isinstance(r[0], dict) else json.loads(r[0])
                   for r in cur.fetchall())
        except Exception:
            src = (r for r in storage.load_all())
    else:
        src = (r for r in storage.load_all())
    for s in src:
        if s.get("centre_c") is None or s.get("kind") == "resolution":
            continue
        k = f"{(s.get('city') or '').lower()}|{s.get('date')}"
        best[k] = {
            "city": s.get("city"), "date": s.get("date"),
            "centre_c": s.get("centre_c"), "ens_mean": s.get("ens_mean"),
            "legs": s.get("legs"), "posted": s.get("posted"),
            "signal": s.get("signal"), "icao": s.get("icao"),
            "ts": s.get("ts"),
        }
    return {"count": len(best), "reads": list(best.values())}


@app.get("/v1/record")
def record(limit: int = Query(200, ge=1, le=20000)):
    """Every scored signal: what we said, what happened. Misses included."""
    res = _resolutions()
    res.sort(key=lambda r: r.get("published_ts") or "", reverse=True)
    # Seit 2.31.2: interne Kalibrierwerte bleiben auf dem Server. Oeffentlich ist, was
    # wir gesagt haben und was passiert ist, nicht wie die Engine gewichtet.
    intern = ("trust_k", "haircut_pp", "said_coverage_raw", "said_coverage_engine",
              "said_asks", "first_legs")
    return {"count": len(res),
            "signals": [{k: v for k, v in r.items() if k not in intern} for r in res[:limit]]}


@app.get("/v1/readcheck")
def readcheck(request: Request, limit: int = Query(500, ge=1, le=5000)):
    """Which read was actually closest to the station: ensemble, +WU, or +bias?

    Every backtest we have is ensemble-only -- field_backtest says so in its own
    note -- because WU publishes no history. But the live engine blends WU at 70%
    and then applies the city bias, so the backtested system is NOT the running
    system. That gap can only be closed forward, from our own snapshots.

    So: no distributions are rebuilt here. For each resolved market we take the
    stored reads and measure the absolute error of each variant against the
    temperature that actually settled. Lower is better, and the winner tells you
    whether the WU weight and the bias correction are earning their place.
    """
    _intern(request)
    # Auch die Aufloesungen gezielt holen. _rows() nimmt die neuesten 5000
    # Zeilen, und bei 46 Staedten mehrmals taeglich reicht das nur wenige
    # Tage zurueck: aeltere Aufloesungen fehlten dadurch ganz.
    res = []
    pg0 = storage._init_pg()
    if pg0:
        try:
            cur0 = pg0.execute(
                "SELECT payload FROM snapshots WHERE payload->>'kind'"
                " = 'resolution' ORDER BY ts DESC LIMIT %s", (limit * 3,))
            for row in cur0.fetchall():
                r = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                if r.get("outcome_known"):
                    res.append(r)
        except Exception:
            res = []
    if not res:
        res = [r for r in _rows(24 * 365) if r.get("kind") == "resolution"
               and r.get("outcome_known")]
    if not res:
        return {"count": 0, "note": "no resolved signals yet"}

    # WARNUNG 03.08.: hier stand ein zweiter Aufruf von _rows(24*365). Der
    # holt "ORDER BY ts DESC LIMIT 5000", also die NEUESTEN 5000 Zeilen. Die
    # Engine scannt 46 Staedte mehrmals taeglich, damit reicht das nur wenige
    # Tage zurueck, und aeltere Aufloesungen fanden ihren Schnappschuss nicht
    # mehr. Ergebnis: count 29 bei 154 aufgeloesten Signalen, die Auswertung
    # stand auf einem Fuenftel der Daten.
    #
    # `slug` ist eine eigene Spalte MIT Index. Also gezielt danach fragen,
    # statt tausende Zeilen zu holen und wegzuwerfen.
    gesucht = [r.get("slug") for r in res[:limit] if r.get("slug")]
    snaps = {}
    pg = storage._init_pg()
    if pg and gesucht:
        try:
            cur = pg.execute(
                "SELECT payload FROM snapshots WHERE slug = ANY(%s)"
                " ORDER BY ts ASC", (list(set(gesucht)),))
            for row in cur.fetchall():
                s = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                if s.get("kind") == "resolution" or s.get("ens_mean") is None:
                    continue
                # Den FRUEHESTEN Schnappschuss je Markt behalten: das ist der
                # Stand bei Veroeffentlichung, bevor der Markt zur Antwort hin
                # gelaufen ist. ORDER BY ts ASC, also gewinnt der erste.
                snaps.setdefault(s["slug"], s)
        except Exception:
            snaps = {}

    if not snaps:
        # Rueckfall ohne Postgres: die Datei durchgehen.
        for s in _rows(24 * 365):
            if s.get("kind") == "resolution" or not s.get("slug"):
                continue
            if s.get("ens_mean") is None:
                continue
            cur2 = snaps.get(s["slug"])
            if not cur2 or (s.get("ts") or "") < (cur2.get("ts") or ""):
                snaps[s["slug"]] = s

    def to_c(label):
        """Die aufgeloeste Temperatur in CELSIUS, egal wie das Label lautet.

        WARNUNG 03.08.: hier stand nur die erste Zahl aus dem Label. Bei
        US-Maerkten steht dort aber Fahrenheit:

            chicago   Label "76-77 F"  ->  actual_c 76.0
                      Engine sagte 24,26 C, also 75,7 F, also RICHTIG.
                      Gemessener Fehler trotzdem: 51,74 Grad.

        Ueber alle Zeilen ergab das einen mittleren Fehler von 10,7 Grad, wo
        er ohne die US-Staedte bei 0,93 liegt. Damit war auch das `verdict`
        wertlos: es entschied zwischen ens, wu und bias auf Daten, die fast
        nur aus einem Umrechnungsfehler bestanden.

        Bewusst OHNE engine.parse_bucket: api.py importiert engine nicht, und
        ein Import nur hierfuer wuerde den Webdienst an den Worker binden.
        Dieselbe Rechnung wie dort, aber lokal.
        """
        if not label:
            return None
        s = str(label)
        # WARNUNG: das Minuszeichen NICHT ins Muster nehmen. In "76-77F" ist
        # der Bindestrich ein Trenner, kein Vorzeichen, und ein gieriges
        # Muster liest daraus 76 und MINUS 77, also eine Mitte von -18.
        # Erst das Band lesen, dann ein fuehrendes Minus getrennt behandeln.
        m = re.match(r"\s*(-?\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?))?", s)
        if not m:
            return None
        lo = float(m.group(1))
        hi = float(m.group(2)) if m.group(2) else lo
        v = (lo + hi) / 2.0
        if re.search(r"(?<![A-Za-z])F(?![A-Za-z])", s.upper()):
            v = (v - 32.0) * 5.0 / 9.0
        return v

    rows, agg = [], {"ens": [], "wu": [], "bias": []}
    ohne_snap = 0
    for r in res[:limit]:
        s = snaps.get(r.get("slug"))
        if not s:
            ohne_snap += 1
            continue
        actual = to_c(r.get("outcome_label"))
        ens = s.get("ens_mean")
        if actual is None or ens is None:
            continue
        # distribution() uses mm = m - bias, so subtracting the stored bias
        # reproduces the centre each variant actually used.
        wub = s.get("wu_bias_c") or 0.0
        cityb = (s.get("city_bias_c") or 0.0) - wub  # city part only
        r_ens = ens
        r_wu = ens - wub
        r_bias = ens - wub - cityb
        e = {"ens": abs(r_ens - actual), "wu": abs(r_wu - actual),
             "bias": abs(r_bias - actual)}
        for k in agg:
            agg[k].append(e[k])
        rows.append({
            "city": r.get("city"), "date": r.get("date"),
            "actual_label": r.get("outcome_label"),
            "actual_c": round(actual, 2),
            "read_ensemble": round(r_ens, 2), "err_ensemble": round(e["ens"], 2),
            "read_wu": round(r_wu, 2), "err_wu": round(e["wu"], 2),
            "read_wu_bias": round(r_bias, 2), "err_wu_bias": round(e["bias"], 2),
            "best": min(e, key=e.get),
            "hit": r.get("outcome_hit"),
        })

    if not rows:
        return {"count": 0, "note": "resolved signals exist but no matching "
                                    "snapshots carry a read yet"}

    def mean(v):
        return round(sum(v) / len(v), 3) if v else None
    wins = {k: sum(1 for x in rows if x["best"] == k) for k in agg}
    return {
        "count": len(rows),
        "mean_abs_error_c": {k: mean(v) for k, v in agg.items()},
        "times_closest": wins,
        "verdict": min(agg, key=lambda k: mean(agg[k]) if agg[k] else 9e9),
        # Sichtbar machen, wieviele Aufloesungen KEINEN Schnappschuss haben.
        # Steht hier eine grosse Zahl, beruht das Urteil wieder nur auf einem
        # Ausschnitt, und genau das war am 03.08. der Fall.
        "resolved_total": len(res[:limit]),
        "ohne_snapshot": ohne_snap,
        "note": "ensemble = raw model. wu = ensemble blended toward Weather "
                "Underground. bias = that, plus the measured per-city offset. "
                "Lower mean error is better. `ohne_snapshot` counts resolved "
                "markets whose published snapshot could not be found.",
        "rows": rows,
    }


@app.get("/v1/viertesband")
def viertesband(request: Request,
                limit: int = Query(1000, ge=1, le=3000),
                maxcost: float = Query(0.15, ge=0.0, le=1.0),
                nurgesendet: bool = Query(False)):
    """Was haette ein VIERTES Band gebracht?

    Die Idee (03.08.): zu den drei gesetzten Feldern ein viertes dazunehmen,
    aber nur wenn es billig ist. Nicht irgendeines, sondern der guenstigere
    der beiden Nachbarn.

    Das ist rueckrechenbar, weil `snap["market_prices"]` ALLE Felder mit
    ihrem Briefkurs enthaelt, nicht nur die drei gewaehlten. Frueher wurde
    angenommen, die Nachbarpreise seien nicht gespeichert.

    `maxcost` ist die Obergrenze fuer das vierte Bein. Bei 0.15 wird es nur
    dazugenommen, wenn es hoechstens 15 Cent kostet.

    Gerechnet wird wie ueberall: ein Dollar je Buendel, gleichmaessig auf die
    Beine verteilt, ein Treffer zahlt 1/Preis auf sein Bein. Ohne die
    Dollar-Mindestorder, die das in Wirklichkeit verzerrt.
    """
    res = _resolutions()[:limit]
    gesucht = [r.get("slug") for r in res if r.get("slug")]
    snaps = {}
    pg = storage._init_pg()
    if pg and gesucht:
        try:
            cur = pg.execute(
                "SELECT payload FROM snapshots WHERE slug = ANY(%s)"
                " ORDER BY ts ASC", (list(set(gesucht)),))
            for row in cur.fetchall():
                s = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                if s.get("kind") == "resolution":
                    continue
                if not s.get("market_prices") or not s.get("legs"):
                    continue
                # ⚠ 03.08.: ohne diesen Filter stecken auch Buendel drin, die
                # die Engine gescannt und BEWUSST NICHT veroeffentlicht hat --
                # also genau die, die sie als unrentabel aussortiert. Sie
                # mitzuzaehlen bestraft die Engine fuer ihre eigene
                # Vorsichtsregel.
                if nurgesendet and not s.get("posted"):
                    continue
                snaps.setdefault(s["slug"], s)
        except Exception as e:
            return {"error": "snapshots not readable", "detail": str(e)[:200]}

    drei = {"n": 0, "hit": 0, "stake": 0.0, "back": 0.0}
    vier = {"n": 0, "hit": 0, "stake": 0.0, "back": 0.0, "dazu": 0}
    rows, ohne = [], 0

    for r in res:
        s = snaps.get(r.get("slug"))
        if not s:
            ohne += 1
            continue
        preise = s.get("market_prices") or {}
        legs = list(s.get("legs") or [])
        asks = list(s.get("asks") or [])
        if len(legs) < 2 or len(asks) != len(legs):
            continue
        ergebnis = r.get("outcome_label")
        if not ergebnis:
            continue

        # Die Reihenfolge der Felder im Markt, damit "Nachbar" definiert ist.
        reihe = [o for o in preise.keys()]
        try:
            i_lo = reihe.index(legs[0])
            i_hi = reihe.index(legs[-1])
        except ValueError:
            continue

        # Beide Nachbarn ansehen, den GUENSTIGEREN nehmen.
        kandidaten = []
        if i_lo - 1 >= 0:
            lab = reihe[i_lo - 1]
            p = preise.get(lab)
            if p:
                kandidaten.append((p, lab))
        if i_hi + 1 < len(reihe):
            lab = reihe[i_hi + 1]
            p = preise.get(lab)
            if p:
                kandidaten.append((p, lab))
        kandidaten.sort()

        treffer3 = ergebnis in legs
        block3 = sum(asks)

        drei["n"] += 1
        drei["stake"] += 1.0
        if treffer3:
            drei["hit"] += 1
            # ⚠ 03.08.: hier stand (1/Anzahl Beine)/Preis, also ein Dollar
            # GLEICHMAESSIG verteilt. Das ist NICHT, was die Engine macht.
            #
            # Sie verteilt PROPORTIONAL zum Preis, damit jedes Bein dieselbe
            # Auszahlung bringt: Einsatz_i = Preis_i / Block, also
            # Anteile_i = 1/Block, unabhaengig davon welches Bein trifft.
            # Genau darum geht es beim Dutching.
            #
            # Gleichmaessig gerechnet faellt die Bilanz systematisch zu
            # schlecht aus, weil das TEURE Bein am haeufigsten trifft und
            # dabei am wenigsten zahlt. Der erste Lauf zeigte dadurch
            # -12,4 %, wo einkauf.py +1,0 % misst.
            drei["back"] += 1.0 / block3

        # Und jetzt mit dem vierten Bein.
        nimm = None
        if kandidaten and kandidaten[0][0] <= maxcost:
            nimm = kandidaten[0]

        legs4 = list(legs)
        asks4 = list(asks)
        if nimm:
            legs4.append(nimm[1])
            asks4.append(nimm[0])
            vier["dazu"] += 1

        treffer4 = ergebnis in legs4
        block4 = sum(asks4)
        vier["n"] += 1
        vier["stake"] += 1.0
        if treffer4:
            vier["hit"] += 1
            vier["back"] += 1.0 / block4

        if nimm and treffer4 and not treffer3:
            rows.append({"city": r.get("city"), "date": r.get("date"),
                         "outcome": ergebnis, "viertes_bein": nimm[1],
                         "preis": round(nimm[0], 3),
                         "block3": round(block3, 3)})

    def fasse(d):
        n = d["n"] or 1
        return {"buendel": d["n"], "treffer": d["hit"],
                "quote": round(d["hit"] / n, 3),
                "eingesetzt": round(d["stake"], 2),
                "zurueck": round(d["back"], 2),
                "roi": round(d["back"] / (d["stake"] or 1) - 1, 4)}

    return {
        "maxcost_viertes_bein": maxcost,
        "nur_gesendet": nurgesendet,
        "ohne_snapshot": ohne,
        "drei_baender": fasse(drei),
        "vier_baender": fasse(vier),
        "viertes_bein_gekauft": vier["dazu"],
        "gerettete_buendel": rows[:40],
        "note": "Ein Dollar je Buendel, PROPORTIONAL zum Preis verteilt, so "
                "wie die Engine es macht. Ein Treffer zahlt dann 1/Blockpreis, "
                "unabhaengig davon welches Bein trifft. Das vierte Bein ist "
                "der GUENSTIGERE der beiden Nachbarn und wird nur genommen, "
                "wenn er hoechstens maxcost kostet. Ohne die "
                "Dollar-Mindestorder.",
    }


@app.get("/v1/regeln")
def regeln(request: Request, limit: int = Query(2000, ge=1, le=5000)):
    """Blockpreis UND Vorsprung gemeinsam, statt jeweils einzeln.

    Ueber 464 aufgeloeste Buendel (25.08.) sagen die Einzeltabellen:

        Blockpreis 70-80c    116 Buendel   78 %   +5,0 %   p = 0,0012
        edge unter 10pp      297 Buendel   71 %   -1,5 %

    Beide messen etwas Verschiedenes: der Preis, wie der Markt das Buendel
    einschaetzt, der Vorsprung, wie stark Engine und Markt sich widersprechen.
    Ob die Schnittmenge besser ist als jedes Kriterium allein, laesst sich aus
    den Einzeltabellen NICHT ablesen. Genau das rechnet dieser Endpunkt.

    ⚠ ZUR VORSICHT BEIM LESEN
    Hier werden viele Kombinationen auf einmal geprueft. Bei genuegend Zellen
    sieht immer eine gut aus, auch wenn nichts dahintersteckt. Deshalb steht
    bei jeder Zelle die Anzahl dabei, und Zellen unter 25 Buendeln sind mit
    Vorsicht zu lesen. Ein Befund zaehlt erst, wenn er auch bei einer anderen
    Aufteilung stehen bleibt.

    Gerechnet wird wie ueberall: ein Dollar je Buendel, ein Treffer zahlt
    1/Blockpreis. Ohne Gebuehren, ohne die Dollar-Mindestorder.
    """
    res = [r for r in _resolutions()[:limit] if r.get("outcome_known")]
    daten = []
    for r in res:
        c, e = r.get("cost"), r.get("edge_pp")
        if not isinstance(c, (int, float)) or not isinstance(e, (int, float)):
            continue
        if c <= 0:
            continue
        daten.append((c, e, 1 if r.get("outcome_hit") else 0))

    if not daten:
        return {"error": "keine auswertbaren Buendel"}

    PREIS = [(0.0, 0.55, "unter 55c"), (0.55, 0.65, "55-65c"),
             (0.65, 0.70, "65-70c"), (0.70, 0.80, "70-80c"),
             (0.80, 1.01, "80c und mehr")]
    EDGE = [(-99.0, 5.0, "unter 5pp"), (5.0, 10.0, "5-10pp"),
            (10.0, 20.0, "10-20pp"), (20.0, 999.0, "ab 20pp")]

    def fasse(g):
        n = len(g)
        if not n:
            return None
        t = sum(x[2] for x in g)
        mp = sum(x[0] for x in g) / n
        return {"n": n, "treffer": t, "quote": round(t / n, 3),
                "block": round(mp, 3),
                "roi": round(t / n / mp - 1, 4) if mp else None}

    tabelle = {}
    for plo, phi, pname in PREIS:
        zeile = {}
        for elo, ehi, ename in EDGE:
            g = [x for x in daten if plo <= x[0] < phi and elo <= x[1] < ehi]
            f = fasse(g)
            if f:
                zeile[ename] = f
        if zeile:
            tabelle[pname] = zeile

    # Die naheliegenden Regeln direkt ausgerechnet, damit man nicht selbst
    # aus der Tabelle summieren muss.
    def regel(name, pruef):
        g = [x for x in daten if pruef(x)]
        f = fasse(g)
        if f:
            f["regel"] = name
            f["anteil"] = round(len(g) / len(daten), 3)
        return f

    kandidaten = [
        regel("alles", lambda x: True),
        regel("Block 70-80c", lambda x: 0.70 <= x[0] < 0.80),
        regel("edge unter 10pp", lambda x: x[1] < 10),
        regel("Block 70-80c UND edge unter 10pp",
              lambda x: 0.70 <= x[0] < 0.80 and x[1] < 10),
        regel("Block 70-80c UND edge unter 5pp",
              lambda x: 0.70 <= x[0] < 0.80 and x[1] < 5),
        regel("Block ab 70c UND edge unter 10pp",
              lambda x: x[0] >= 0.70 and x[1] < 10),
        regel("Block 65-80c UND edge unter 10pp",
              lambda x: 0.65 <= x[0] < 0.80 and x[1] < 10),
    ]
    kandidaten = [k for k in kandidaten if k]
    kandidaten.sort(key=lambda k: -(k["roi"] or -9))

    return {
        "buendel": len(daten),
        "kreuztabelle": tabelle,
        "regeln_sortiert": kandidaten,
        "note": "Ein Dollar je Buendel, ein Treffer zahlt 1/Blockpreis. Ohne "
                "Gebuehren und ohne die Dollar-Mindestorder. `anteil` ist der "
                "Anteil aller Buendel, den die Regel durchlaesst. Zellen unter "
                "25 Buendeln sind mit Vorsicht zu lesen: bei vielen "
                "gleichzeitig geprueften Kombinationen sieht immer eine gut "
                "aus.",
    }


@app.get("/v1/positions")
def positions(request: Request):
    """Real fills from the exchange, not what the engine said it would do.

    The calibration table answers "were the probabilities honest". This answers
    "what did the account actually make", which is a different question and the
    only one a P&L can be built from. Read-only: it asks Polymarket's data API
    for the proxy wallet's positions and reshapes them. Nothing is placed here.
    """
    _intern(request)
    addr = (cfg.wallet_address or "").strip()
    if not addr:
        return {"count": 0, "positions": [], "note": "WALLET_ADDRESS not set"}
    try:
        import urllib.request
        url = ("https://data-api.polymarket.com/positions?user="
               + urllib.parse.quote(addr))
        # Ohne Browser-Kennung antwortet Polymarkets Schutzschicht mit 403.
        # urllib meldet sich sonst als "Python-urllib/3.x" und wird gefiltert.
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/126.0 Safari/537.36"),
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = json.loads(r.read().decode())
    except Exception as e:
        raise HTTPException(502, f"positions upstream failed: {e}")
    if not isinstance(raw, list):
        raise HTTPException(502, f"unexpected upstream payload: {raw}")

    out, staked, value = [], 0.0, 0.0
    for p in raw:
        try:
            sz = float(p.get("size") or 0)
            avg = float(p.get("avgPrice") or 0)
            cur = float(p.get("currentValue") or 0)
        except (TypeError, ValueError):
            continue
        cost = sz * avg
        staked += cost
        value += cur
        out.append({
            "title": p.get("title"),
            "outcome": p.get("outcome"),
            "shares": round(sz, 4),
            "avg_price": round(avg, 4),
            "staked_usd": round(cost, 2),
            "value_usd": round(cur, 2),
            "pnl_usd": round(cur - cost, 2),
            "pnl_pct": round((cur / cost - 1) * 100, 1) if cost > 0 else 0.0,
        })
    out.sort(key=lambda x: -x["value_usd"])
    return {
        "count": len(out),
        "staked_usd": round(staked, 2),
        "value_usd": round(value, 2),
        "pnl_usd": round(value - staked, 2),
        "pnl_pct": round((value / staked - 1) * 100, 1) if staked > 0 else 0.0,
        "positions": out,
    }


@app.get("/v1/stations")
def stations_ep():
    return {
        "count": len(station_mod.STATIONS),
        "excluded": sorted(station_mod.EXCLUDED_CITIES),
        "note": "Resolution stations are parsed from each market's own rules "
                "(ICAO in the source URL). Hong Kong resolves at 0.1C "
                "precision while every other city rounds to whole degrees, "
                "so it is excluded until that boundary is measured.",
        "stations": [{"icao": k, "lat": v[0], "lon": v[1], "name": v[2]}
                     for k, v in sorted(station_mod.STATIONS.items())],
    }


def _letzter_snapshot_min():
    """Minuten seit dem juengsten gespeicherten Schnappschuss, None wenn unbekannt."""
    pg = storage._init_pg()
    if not pg:
        return None
    try:
        cur = pg.execute("SELECT max(ts) FROM snapshots "
                         "WHERE payload->>'kind' IS DISTINCT FROM 'resolution'")
        t = cur.fetchone()[0]
        if t is None:
            return None
        if isinstance(t, str):
            t = datetime.fromisoformat(t.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - t).total_seconds() / 60)
    except Exception:
        return None


@app.get("/v1/health")
def health():
    rows = _rows(6)
    alter = _letzter_snapshot_min()
    worker_ok = alter is not None and alter <= cfg.watchdog_hours * 60
    return {"ok": True, "worker_ok": worker_ok,
            "last_snapshot_min": alter,
            "snapshots_last_6h": len(rows),
            "postgres": storage._init_pg() is not None}


# Waechter. Laeuft im web-Dienst, nicht im Worker: ein toter Worker kann
# nicht selbst melden, dass er tot ist.
import threading as _th
import time as _time


def _waechter():
    gemeldet = False
    while True:
        _time.sleep(30 * 60)
        try:
            alter = _letzter_snapshot_min()
            chat = cfg.tg_private
            if alter is None or not chat:
                continue
            if alter > cfg.watchdog_hours * 60 and not gemeldet:
                _tg("sendMessage", chat_id=chat, text=(
                    f"⚠️ OddsEngine Worker steht seit {alter // 60} Stunden. "
                    "Keine Scans, keine Signale. Railway, Dienst worker, "
                    "Deployments ansehen und neu starten."))
                gemeldet = True
            elif alter <= cfg.watchdog_hours * 60 and gemeldet:
                _tg("sendMessage", chat_id=chat,
                    text="✅ OddsEngine Worker scannt wieder.")
                gemeldet = False
        except Exception:
            pass


def _webhook_nachziehen():
    """Seit 2.36.5: der Webhook muss auch channel_post liefern. Einmal beim Start nachziehen."""
    try:
        _time.sleep(15)
        info = (_tg("getWebhookInfo") or {}).get("result") or {}
        url = info.get("url")
        erlaubt = info.get("allowed_updates") or ["message"]
        if url and not {"channel_post", "callback_query"} <= set(erlaubt):
            r = _tg("setWebhook", url=url, allowed_updates=sorted(set(erlaubt) | {"message", "channel_post", "callback_query"}))
            print(f"telegram webhook: channel_post and buttons added {bool(r and r.get('ok'))}", flush=True)
    except Exception as e:
        print(f"telegram webhook nachziehen: {e}", flush=True)


@app.on_event("startup")
def _waechter_starten():
    _th.Thread(target=_waechter, daemon=True).start()
    _th.Thread(target=_webhook_nachziehen, daemon=True).start()
    try:
        import ai_api as _ai_api
        _ai_api.starten()
    except Exception as e:
        print(f"ai: watcher not started: {e}", flush=True)


# ─────────────────────────────────────────────────────────────────────
#  Telegram relay
# ─────────────────────────────────────────────────────────────────────
#
# The problem this solves: a channel cannot receive messages, so "Request
# access" pointing at t.me/OddsEngine goes nowhere. And answering from a
# personal account puts a real username in front of every stranger who asks.
#
# So the bot is the front desk. A visitor messages the bot; the bot forwards
# it here. The operator replies TO that forwarded message and the bot relays
# it back. The visitor only ever sees the bot.
#
# It runs on this service because it already has a domain. Telegram delivers
# straight to it, so there is nothing else to host and nothing to poll.
#
# Wire it up once:
#   https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<DOMAIN>/telegram/webhook
#
# Remove it again:
#   https://api.telegram.org/bot<TOKEN>/deleteWebhook

import re
import requests as _rq

TG = "https://api.telegram.org/bot{token}/{method}"


def _tg(method, **payload):
    if not cfg.tg_token:
        return None
    try:
        r = _rq.post(TG.format(token=cfg.tg_token, method=method),
                     json=payload, timeout=15)
        return r.json()
    except Exception:
        return None


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """
    Always answers 200. Telegram retries on any error, and a retry storm over
    a message we could not parse is worse than dropping it.

    WHY NOT forwardMessage
    ----------------------
    Forwarding looked obvious and broke on the first real test. Anyone with
    forwarding privacy on has their sender id stripped by Telegram, so a
    forwarded message carries no way to route a reply. The operator answers
    into the void.

    So we do not forward. We compose a fresh message that carries the id in
    its own text, and read it back off the reply. Works for everyone, privacy
    settings or not.
    """
    try:
        upd = await request.json()
    except Exception:
        return {"ok": True}

    # ── Seit 2.36.5: Posts im offiziellen Kanal gehen an alle Your Engines ──
    cp = upd.get("channel_post")
    if cp:
        try:
            quellen = {str(x).strip() for x in (os.getenv("ENGINE_NEWS_CHANNELS", "") or str(cfg.tg_public or "")).split(",") if str(x).strip()}
            ch = cp.get("chat") or {}
            if str(ch.get("id")) in quellen or ("@" + str(ch.get("username") or "")) in quellen:
                import motor
                motor.ankuendigen(cp.get("text") or cp.get("caption") or "", "channel", ch.get("id"), cp.get("message_id"))
        except Exception as e:
            print(f"engine news: {e}", flush=True)
        return {"ok": True}

    # ── Seit 2.39: Knoepfe unter Your-Engine-Nachrichten (Watch it, Refresh, ...) ──
    cq = upd.get("callback_query")
    if cq:
        try:
            _tg("answerCallbackQuery", callback_query_id=cq.get("id"))
            chat_cb = ((cq.get("message") or {}).get("chat") or {}).get("id") or (cq.get("from") or {}).get("id")
            import agent as _ag
            import motor as _mo
            u_ = _ag.laden(chat_cb) or {}
            if u_.get("wallet"):
                _th.Thread(target=_mo.knopf, args=(u_["wallet"], chat_cb, str(cq.get("data") or "")), daemon=True).start()
        except Exception as e:
            print(f"engine knopf: {e}", flush=True)
        return {"ok": True}

    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return {"ok": True}

    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = msg.get("text") or ""
    operator = cfg.tg_private
    if not chat_id or not operator:
        return {"ok": True}

    # Groups and channels: the front desk stays silent there. Otherwise every
    # holder message in the holder group would get "a human will read this"
    # and land in the operator's inbox. Only /id answers, so the operator can
    # read the chat id without any third party bot.
    if chat.get("type") != "private":
        try:
            import gruppe
            if gruppe.nachricht(msg):
                return {"ok": True}
        except Exception as e:
            print(f"gruppe: {e}", flush=True)
        if text.split("@")[0].strip() == "/id":
            _tg("sendMessage", chat_id=chat_id,
                text=f"Chat id: {chat_id}\nPut this in Railway as TELEGRAM_PAID_CHANNEL_ID.")
        return {"ok": True}

    # ── Seit 2.39.1: Fotos an Your Engine werden angeschaut (nicht mehr automatisch ein Ticket) ──
    ist_bild = bool(msg.get("photo") or str((msg.get("document") or {}).get("mime_type") or "").startswith("image/"))
    if ist_bild and str(chat_id) != str(operator) and not re.match(r"^/(support|help)\b", msg.get("caption") or ""):
        try:
            import agent as _ag
            import motor as _mo
            u_ = _ag.laden(chat_id) or {}
            if u_.get("wallet") and _mo.offen(u_["wallet"]):
                if msg.get("photo"):
                    fid, mime = msg["photo"][-1]["file_id"], "image/jpeg"
                else:
                    fid, mime = msg["document"]["file_id"], msg["document"].get("mime_type") or "image/jpeg"
                _th.Thread(target=_mo.telegram_foto, args=(chat_id, u_["wallet"], fid, msg.get("caption") or "", mime), daemon=True).start()
                return {"ok": True}
        except Exception as e:
            print(f"engine foto: {e}", flush=True)

    # ── Support (seit 2.33): Screenshots an den Bot werden ein Ticket mit Bild ──
    if str(chat_id) != str(operator) and ist_bild:
        try:
            import support
            import agent
            von = msg.get("from") or {}
            if msg.get("photo"):
                fid, mime = msg["photo"][-1]["file_id"], "image/jpeg"
            else:
                fid, mime = msg["document"]["file_id"], msg["document"].get("mime_type") or "image/jpeg"
            bilder = []
            f = _tg("getFile", file_id=fid) or {}
            pfad = (f.get("result") or {}).get("file_path")
            if pfad:
                rr = _rq.get(f"https://api.telegram.org/file/bot{cfg.tg_token}/{pfad}", timeout=30)
                if rr.ok and len(rr.content) <= support.MAX_BILD:
                    bilder.append((mime, rr.content))
            cap = re.sub(r"^/(support|help)(@\w+)?\s*", "", msg.get("caption") or "").strip()
            u_ = agent.laden(chat_id) or {}
            tid = support.neu("telegram", cap, tg_id=chat_id, wallet=u_.get("wallet"), name=von.get("first_name"),
                              handle=von.get("username"), melden=True, bilder=bilder)
            _tg("sendMessage", chat_id=chat_id, text=f"Got it, ticket #{tid} with your screenshot. The team reads every message and answers here."
                + ("" if cap else "\nAdd a line about what went wrong if you can."))
        except Exception as e:
            print(f"support foto: {e}", flush=True)
        return {"ok": True}

    # ── Support (seit 2.33): /support <text> von jedem, wird als Ticket gespeichert ──
    if text.split(" ")[0].split("@")[0] in ("/support", "/help") and str(chat_id) != str(operator):
        rest = text.split(" ", 1)[1].strip() if " " in text else ""
        if not rest:
            _tg("sendMessage", chat_id=chat_id, text="Write /support and your message in one line, e.g.\n/support my balance did not update")
            return {"ok": True}
        try:
            import support
            import agent
            von = msg.get("from") or {}
            u_ = agent.laden(chat_id) or {}
            tid = support.neu("telegram", rest, tg_id=chat_id, wallet=u_.get("wallet"),
                              name=von.get("first_name"), handle=von.get("username"), melden=True)
            _tg("sendMessage", chat_id=chat_id, text=f"Got it, ticket #{tid}. The team reads every message and answers here.")
        except Exception as e:
            print(f"support: {e}", flush=True)
        return {"ok": True}

    # ── Your Engine (seit 2.20): Befehle im privaten Chat ──
    # Auch der Betreiber kann seinen eigenen Agenten benutzen, solange er
    # nicht gerade auf eine weitergeleitete Nachricht antwortet.
    if text.startswith("/") and not msg.get("reply_to_message"):
        try:
            import agent
            if agent.befehl(chat_id, msg.get("from") or {}, text):
                return {"ok": True}
        except Exception as e:
            print(f"agent befehl: {e}", flush=True)

    # ── Your Engine (seit 2.33): freier Text und Sprachnachrichten an den eigenen Agenten ──
    if not text.startswith("/") and not msg.get("reply_to_message") and (text or msg.get("voice")):
        try:
            import agent
            import features
            u_ = agent.laden(chat_id)
            import motor
            if u_ and u_.get("wallet") and motor.offen(u_["wallet"]):
                threading.Thread(target=motor.telegram, daemon=True,
                                 args=(chat_id, u_["wallet"], text, (msg.get("voice") or {}).get("file_id"))).start()
                return {"ok": True}
        except Exception as e:
            print(f"motor webhook: {e}", flush=True)

    # ── The operator answering ──
    if str(chat_id) == str(operator):
        reply = msg.get("reply_to_message") or {}
        target = None
        m = re.search(r"#id(\d+)", reply.get("text") or "")
        if m:
            target = m.group(1)
        if target and text:
            r = _tg("sendMessage", chat_id=target, text=text,
                    disable_web_page_preview=True)
            if r and r.get("ok"):
                try:
                    import support
                    support.beantwortet_per_telegram(target, text)
                except Exception:
                    pass
            if not (r and r.get("ok")):
                _tg("sendMessage", chat_id=operator,
                    text=f"Could not deliver: {(r or {}).get('description', 'unknown')}")
        elif reply:
            _tg("sendMessage", chat_id=operator,
                text="Reply to the message that has #id in it, that is the one "
                     "carrying the address.")
        return {"ok": True}

    # ── A visitor ──
    u = msg.get("from") or {}
    who = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or "someone"
    handle = f"@{u['username']}" if u.get("username") else "no username"

    if text.startswith("/start"):
        _tg("sendMessage", chat_id=chat_id, disable_web_page_preview=True, text=(
            "OddsEngine.\n\n"
            "The engine reads the weather for about 50 cities every day and "
            "calls the Polymarket temperature markets. Each call says if it is a BET or just a READ. "
            "Every call is posted before the station reports and scored after. "
            "Hits and misses, all of them.\n\n"
            "FREE\n"
            "One call a day and every result on @OddsEngine.\n\n"
            "HOLD $ODDS\n"
            "Every call live in the app, with the split for your stake. "
            "Verify your wallet at public.oddsengine.live\n\n"
            "How it works: docs.oddsengine.live\n\n"
            "Write here and it lands with a human. Replies come back through "
            "this bot."))

    # Seit 2.33: jede Nachricht auch als Support-Ticket gespeichert (admin.html, Support inbox).
    if not text.startswith("/start") and (text or msg.get("voice")):
        try:
            import support
            support.neu("telegram", text or "[voice note, see the Telegram chat]", tg_id=chat_id,
                        name=u.get("first_name"), handle=u.get("username"))
        except Exception as e:
            print(f"support: {e}", flush=True)

    # Composed, not forwarded. The id in the text is how a reply finds its way.
    _tg("sendMessage", chat_id=operator, disable_web_page_preview=True, text=(
        f"{who} ({handle})  #id{chat_id}\n\n{text}\n\n"
        "Reply to this message to answer them."))

    if not text.startswith("/start"):
        _tg("sendMessage", chat_id=chat_id,
            text="Got it. A human will read this and reply here.")
    return {"ok": True}


@app.get("/telegram/setup")
def telegram_setup(request: Request):
    """
    Open once in a browser to register the webhook. Reports exactly what is
    missing rather than a silent null.
    """
    _intern(request)
    # Railway terminates TLS at the edge and hands the app plain http, so
    # base_url says http:// even though the world reaches it over https.
    # Telegram refuses anything that is not https, so force it.
    base = str(request.base_url).rstrip("/").replace("http://", "https://", 1)
    hook = f"{base}/telegram/webhook"

    problems = []
    if not cfg.tg_token:
        problems.append("TELEGRAM_BOT_TOKEN is not set on this service. The "
                        "relay is the bot answering, so this service needs the "
                        "token too. Railway -> web -> Variables.")
    if not cfg.tg_private:
        problems.append("TELEGRAM_PRIVATE_CHAT_ID is not set on this service. "
                        "Without it there is nobody to forward to.")
    if problems:
        return {"ok": False, "would_set": hook, "missing": problems}

    res = _tg("setWebhook", url=hook, allowed_updates=["message", "channel_post", "callback_query"])
    info = _tg("getWebhookInfo")
    me = _tg("getMe")
    ok = bool(res and res.get("ok"))
    out = {"ok": ok, "set_to": hook, "result": res, "info": info}
    if me and me.get("ok"):
        u = me["result"].get("username")
        out["bot"] = "@" + u if u else None
        out["button_link"] = f"https://t.me/{u}" if u else None
    return out


# ── Krypto-Engine (seit 2.27, hinter dem Schalter "crypto") ─────────────
#
# Die Daten schreibt der eigene Dienst `crypto` (krypto_main.py) alle paar
# Sekunden in die kv-Tabelle. Hier wird nur gelesen, mit 3 Sekunden Cache,
# damit viele offene Terminals die Datenbank nicht belasten.

_KRYPTO_CACHE = {"ts": 0.0, "d": None}


@app.get("/v1/lows")
def lows_lesen(request: Request):
    """Tiefstwert-Calls. Oeffentlich nur mit Schalter "lows", ohne Interna.
    Der Betreiber sieht alles, auch mit Schalter aus."""
    import tief
    if _admin_ok(request):
        return {"enabled": tief.an(), **(tief.stand() or {})}
    if not _freigegeben(request, "lows"):
        return {"enabled": False}
    return {"enabled": True, **(tief.stand(oeffentlich=True) or {})}


@app.get("/v1/crypto")
def krypto_lesen(request: Request):
    if not _freigegeben(request, "crypto"):
        return {"enabled": False}
    import time as _t
    if _KRYPTO_CACHE["d"] is None or _t.time() - _KRYPTO_CACHE["ts"] > 3:
        import krypto
        try:
            _KRYPTO_CACHE["d"] = krypto.lage()
            _KRYPTO_CACHE["ts"] = _t.time()
        except Exception as e:
            raise HTTPException(status_code=503, detail=str(e)[:160])
    out = dict(_KRYPTO_CACHE["d"])
    out["enabled"] = True
    return out


@app.get("/v1/admin/margin-burn")
def margenburn_lesen(request: Request):
    """Was aus der Marge der $ODDS-Aufladungen zu verbrennen ist. Nur Betreiber."""
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="operator only")
    import margenburn
    try:
        return margenburn.lage()
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e)[:160])


@app.post("/v1/admin/margin-burn")
async def margenburn_eintragen(request: Request):
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="operator only")
    try:
        b = await request.json()
    except Exception:
        b = {}
    import margenburn
    r = margenburn.eintragen((b or {}).get("tx"))
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r.get("error"))
    return r


@app.get("/v1/admin/krypto-lab")
def admin_krypto_lab(request: Request):
    """Krypto-Lernlabor (seit 2.37): Walk-forward-Bericht und Schattentrades. Nur Betreiber."""
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="operator only")
    import lernen
    return lernen.bericht()


@app.get("/v1/admin/health")
def admin_gesundheit(request: Request):
    """Was laeuft, was fehlt: Variablen, Dienste, neue Bereiche. Nur Betreiber."""
    if not _admin_ok(request):
        raise HTTPException(status_code=403, detail="operator only")
    import time as _t
    out = {"version": VERSION, "vars": {
        "MAIL_KEY": bool(os.getenv("MAIL_KEY", "").strip()),
        "TOMTOM_API_KEY": bool(os.getenv("TOMTOM_API_KEY", "").strip()),
        "OPENROUTER_API_KEY": bool(os.getenv("OPENROUTER_API_KEY", "").strip()),
        "ENGINE_BETA": len([x for x in os.getenv("ENGINE_BETA", "").split(",") if x.strip()]),
        "X402_GAS_KEY": bool(os.getenv("X402_GAS_KEY", "").strip()),
        "AUTH_SECRET": bool(os.getenv("AUTH_SECRET", "").strip()),
    }}
    try:
        import boersen
        b = boersen.lage()
        out["exchanges"] = {"age_s": round(_t.time() - float(b.get("ts") or 0)) if b.get("ts") else None,
                            "coins": len(b.get("coins") or {}),
                            "mood": {c: v.get("mood") for c, v in (b.get("coins") or {}).items()}}
    except Exception as e:
        out["exchanges"] = {"error": str(e)[:120]}
    try:
        import optionen
        o = optionen.lage()
        out["options"] = {"age_s": round(_t.time() - float(o.get("updated") or 0)) if o.get("updated") else None,
                          "events": len(o.get("events") or []), "gaps_now": len(o.get("top") or []),
                          "open_picks": o.get("open_picks"), "record": (o.get("record") or {}).get("all"),
                          "by_venue": {k: v for k, v in (o.get("record") or {}).items() if k != "all"},
                          "calibration": o.get("calibration"), "recent": (o.get("recent") or [])[:10]}
    except Exception as e:
        out["options"] = {"error": str(e)[:120]}
    try:
        import motor
        import postfach
        import kalender
        nutzer = motor._nutzer()
        out["engine"] = {"users": len(nutzer),
                         "mail_connected": sum(1 for a in nutzer if postfach.status(a).get("connected")),
                         "calendars": sum(1 for a in nutzer if kalender.status(a).get("connected")),
                         "signal_alerts": sum(len(motor.zustand(a).get("signals") or []) for a in nutzer)}
    except Exception as e:
        out["engine"] = {"error": str(e)[:120]}
    return out


@app.get("/v1/burns/margin")
def margenburn_oeffentlich():
    """Oeffentlich: jeder Burn aus der Marge der $ODDS-Aufladungen, mit Transaktion."""
    import margenburn
    return margenburn.oeffentlich()


_SENT_CACHE = {"ts": 0.0, "d": None}
_OPT_CACHE = {"ts": 0.0, "d": None}


@app.get("/v1/sentiment")
def sentiment_lesen(request: Request):
    """Boersen (Funding, Open Interest, Long/Short, Taker-Flow, Orderbuecher, Liquidationen)
    neben Polymarket-Masse und Engine. Seit 2.35, gleicher Schalter wie das Terminal."""
    if not _freigegeben(request, "sentiment"):
        return {"enabled": False}
    import time as _t
    if _SENT_CACHE["d"] is None or _t.time() - _SENT_CACHE["ts"] > 10:
        import boersen
        try:
            _SENT_CACHE["d"] = boersen.sentiment()
            _SENT_CACHE["ts"] = _t.time()
        except Exception as e:
            raise HTTPException(status_code=503, detail=str(e)[:160])
    return {"enabled": True, **_SENT_CACHE["d"]}


@app.get("/v1/options")
def optionen_lesen(request: Request):
    """Optionsmarkt (Deribit) gegen Polymarket und Kalshi bei "BTC/ETH ueber X". Seit 2.35."""
    if not _freigegeben(request, "options"):
        return {"enabled": False}
    import time as _t
    if _OPT_CACHE["d"] is None or _t.time() - _OPT_CACHE["ts"] > 30:
        import optionen
        try:
            _OPT_CACHE["d"] = optionen.lage()
            _OPT_CACHE["ts"] = _t.time()
        except Exception as e:
            raise HTTPException(status_code=503, detail=str(e)[:160])
    return {"enabled": True, **_OPT_CACHE["d"]}


@app.get("/telegram/status")
def telegram_status(request: Request):
    """Is the relay wired up, and does Telegram agree?"""
    _intern(request)
    return {"token_set": bool(cfg.tg_token),
            "operator_set": bool(cfg.tg_private),
            "bot": _tg("getMe"),
            "webhook": _tg("getWebhookInfo")}
