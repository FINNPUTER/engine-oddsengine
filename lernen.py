"""
lernen.py: Krypto-Lernlabor. Seit 2.37. Nur intern: handelt nichts, zeigt nichts oeffentlich.

Ziel: herausfinden, ob die Krypto-Engine mit den Boersendaten (Orderbuecher, Funding, Open
Interest, aggressive Kaeufe, Liquidationen) und dem Optionsmarkt einen echten Vorsprung vor
dem Polymarket-Preis bekommt. Nicht "oefter treffen", sondern besser kalibriert sein als der
Preis und nur kaufen, wenn der Vorsprung nach Gebuehren reicht.

    punkt()        In jedem Up/Down-Fenster an 10, 20, ... 90 % der Laufzeit eine Zeile mit allen
                   Merkmalen, Marktpreis, Asks und Engine-Zahl in krypto_lern (Postgres).
    aufloesen()    Nach dem Ausgang: y (1 = Up) eintragen, Schattentrades abrechnen.
    trainieren()   Alle 6 Stunden: logistische Regression je Zeitfenster (5m, 15m, 4h).
                   Walk-forward: jeder Testtag wird nur mit den Tagen davor trainiert.
                   Bericht (Brier Modell gegen Markt gegen Engine, Kalibrierung, Papier-Ergebnis
                   je Schwelle) in kv krypto_lab. Das Modell auf allen Daten in kv krypto_ml_model.
    schatten       Sobald ein Modell da ist: einmal je Fenster ein Schattentrade, wenn
                   Modell - Ask - Gebuehr >= Schwelle. Echte Out-of-sample-Bilanz in kv krypto_ml_live.

Nichts davon fliesst in die oeffentlichen Module oder ins Terminal. Live geht es erst, wenn der
Betreiber es nach Wochen mit stabil positiven Zahlen entscheidet.
"""
import json
import math
import os
import threading
import time
from datetime import datetime, timezone

import storage
from logger import log

LERN_CHECKS = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
HALTEN_TAGE = int(os.getenv("LAB_KEEP_DAYS", "40"))
MIN_TRAIN = int(os.getenv("LAB_MIN_TRAIN", "1500"))
TEST_TAGE = int(os.getenv("LAB_TEST_DAYS", "14"))
SCHWELLEN = (0.02, 0.03, 0.05, 0.08, 0.12)
L2 = float(os.getenv("LAB_L2", "3.0"))
FEE_RATE = float(os.getenv("CRYPTO_FEE_RATE", "0.07"))
TAKT_H = float(os.getenv("LAB_TRAIN_HOURS", "6"))

MERKMALE = ("lm", "lr", "z", "f", "sig", "r60", "r300", "spread", "overround",
            "b10", "b50", "tk5", "tk15", "fund", "oi15", "ls", "liq", "prem", "opt", "has_ex", "has_opt")

_SCHEMA = [False]
_LOCK = threading.Lock()
_MODELL = {"ts": 0.0, "m": None}


def _gebuehr(p):
    p = max(0.0, min(1.0, float(p)))
    return FEE_RATE * p * (1 - p)


def _logit(p):
    p = min(0.995, max(0.005, float(p)))
    return math.log(p / (1 - p))


def _pg():
    pg = storage._init_pg()
    if not pg:
        return None
    if not _SCHEMA[0]:
        pg.execute("""CREATE TABLE IF NOT EXISTS krypto_lern (
            slug TEXT NOT NULL, chk TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL DEFAULT now(),
            a TEXT, tf TEXT, x JSONB, mid DOUBLE PRECISION, ask_u DOUBLE PRECISION,
            ask_d DOUBLE PRECISION, p_eng DOUBLE PRECISION, p_ml DOUBLE PRECISION, y SMALLINT,
            PRIMARY KEY (slug, chk))""")
        pg.execute("CREATE INDEX IF NOT EXISTS ix_krypto_lern_ts ON krypto_lern(ts)")
        _SCHEMA[0] = True
    return pg


# ── Merkmale ─────────────────────────────────────────────────────────────

_BOERSE = {"ts": 0.0, "d": {}}


def _boerse():
    if time.time() - _BOERSE["ts"] > 20:
        try:
            _BOERSE["d"] = json.loads(storage.kv_get("boersen_live") or "{}")
        except Exception:
            _BOERSE["d"] = {}
        _BOERSE["ts"] = time.time()
    return _BOERSE["d"]


def _zahl(v, d=0.0, lo=-50.0, hi=50.0):
    try:
        v = float(v)
        if math.isnan(v) or math.isinf(v):
            return d
        return max(lo, min(hi, v))
    except Exception:
        return d


def merkmale(a, tf, m, pr, mid_u, ask_u, ask_d, bid_u, w, jetzt):
    import krypto
    sig = krypto.sigma(a)
    r60, r300 = krypto.rendite(a, 60, jetzt), krypto.rendite(a, 300, jetzt)
    x = {"lm": _logit(mid_u), "lr": _logit(pr), "z": _zahl((m["mu"] - w["s0"]) / m["sd"] if m["sd"] else 0, 0, -8, 8),
         "f": m["f"], "sig": _zahl(sig * 1e4, 0, 0, 50),
         "r60": _zahl(r60 / (sig * math.sqrt(60)) if r60 is not None and sig else 0, 0, -10, 10),
         "r300": _zahl(r300 / (sig * math.sqrt(300)) if r300 is not None and sig else 0, 0, -10, 10),
         "spread": _zahl((ask_u - bid_u) if (ask_u is not None and bid_u is not None) else 0.02, 0.02, 0, 1),
         "overround": _zahl(((ask_u or 0) + (ask_d or 0)) - 1, 0, -1, 1)}
    b = _boerse()
    c = (b.get("coins") or {}).get(a) or {}
    frisch = c and time.time() - float(b.get("ts") or 0) < 240
    if frisch:
        liq = c.get("liquidations") or {}
        lo, sh = float(liq.get("long_15m") or 0), float(liq.get("short_15m") or 0)
        ls = (c.get("long_short") or {}).get("now")
        x.update({
            "b10": _zahl((c.get("book") or {}).get("pressure_10bp"), 0, -1, 1),
            "b50": _zahl((c.get("book") or {}).get("pressure_50bp"), 0, -1, 1),
            "tk5": _zahl(((c.get("taker_buy") or {}).get("5m") or 0.5) - 0.5, 0, -0.5, 0.5),
            "tk15": _zahl(((c.get("taker_buy") or {}).get("15m") or 0.5) - 0.5, 0, -0.5, 0.5),
            "fund": _zahl((c.get("funding") or {}).get("annual"), 0, -3, 3),
            "oi15": _zahl((c.get("oi") or {}).get("chg_15m"), 0, -0.2, 0.2),
            "ls": _zahl(math.log(float(ls)) if ls else 0, 0, -3, 3),
            "liq": _zahl((sh - lo) / (sh + lo + 1e5), 0, -1, 1),
            "prem": _zahl(((c.get("basis") or {}).get("perp_premium") or 0) * 1e3, 0, -20, 20),
            "has_ex": 1.0})
    else:
        x.update({k: 0.0 for k in ("b10", "b50", "tk5", "tk15", "fund", "oi15", "ls", "liq", "prem")})
        x["has_ex"] = 0.0
    x["opt"], x["has_opt"] = 0.0, 0.0
    try:
        import optionen
        sym = (optionen.COINS.get(a) or (None,))[0]
        fl = optionen._FL.get(sym) if sym else None
        if fl and time.time() - fl.get("_ts", 0) < 900 and tf == "4h":
            # Optionsmarkt: P(Kurs am Fensterende > Startpreis). Fuer 4 h tragfaehig, kuerzer zu ungenau.
            o = optionen.wahrscheinlichkeit(fl, w["s0"], w["start"] + w["dauer"])
            if o and o.get("p") is not None:
                x["opt"] = _zahl(_logit(o["p"]) - x["lm"], 0, -6, 6)
                x["has_opt"] = 1.0
    except Exception:
        pass
    return x


# ── Datensammlung ────────────────────────────────────────────────────────

def punkt(w, a, tf, m, pr, mid_u, ask_u, ask_d, bid_u, jetzt):
    """Aus krypto.tick fuer jedes Fenster mit Modell und Buch. Billig, wenn kein Checkpunkt dran ist."""
    if not (m and w.get("full") and mid_u is not None and ask_u and ask_d and pr is not None):
        return
    done = w.setdefault("lern", [])
    checks = LERN_CHECKS if tf != "5m" else (0.2, 0.5, 0.8)
    faellig = [c for c in checks if str(c) not in done and m["f"] >= c and m["f"] < c + 0.08]
    for c in checks:
        if str(c) not in done and m["f"] >= c + 0.08:
            done.append(str(c))   # verpasst (z. B. Neustart): auslassen statt spaet nachtragen
    if not faellig:
        return
    c = faellig[-1]
    done.append(str(c))
    x = merkmale(a, tf, m, pr, mid_u, ask_u, ask_d, bid_u, w, jetzt)
    p_ml = vorhersage(tf, x)
    try:
        pg = _pg()
        if pg:
            pg.execute("""INSERT INTO krypto_lern (slug, chk, a, tf, x, mid, ask_u, ask_d, p_eng, p_ml)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                       (w["slug"], str(c), a, tf, json.dumps(x), mid_u, ask_u, ask_d, m["p_up"], p_ml))
    except Exception as e:
        log.debug(f"lernen punkt: {e}")
    _schatten(w, tf, p_ml, ask_u, ask_d, m["f"])


def _schatten(w, tf, p, ask_u, ask_d, f):
    if p is None or w.get("ml") or not (0.2 <= f <= 0.95):
        return
    mod = (_MODELL.get("m") or {}).get(tf) or {}
    schwelle = mod.get("edge") or float(os.getenv("LAB_EDGE", "0.05"))
    eu = p - ask_u - _gebuehr(ask_u)
    ed = (1 - p) - ask_d - _gebuehr(ask_d)
    seite, ask, e = ("up", ask_u, eu) if eu >= ed else ("down", ask_d, ed)
    if e >= schwelle and 0.05 <= ask <= 0.95:
        w["ml"] = {"side": seite, "price": round(ask, 4), "p": round(p if seite == "up" else 1 - p, 4),
                   "edge": round(e, 4), "f": round(f, 3)}


def aufloesen(w, sieger):
    y = 1 if sieger == "up" else 0
    try:
        pg = _pg()
        if pg and w.get("lern"):
            pg.execute("UPDATE krypto_lern SET y=%s WHERE slug=%s", (y, w["slug"]))
    except Exception as e:
        log.debug(f"lernen aufloesen: {e}")
    t = w.get("ml")
    if not t:
        return
    with _LOCK:
        try:
            st = json.loads(storage.kv_get("krypto_ml_live") or "{}")
        except Exception:
            st = {}
        kosten = t["price"] * 10 + _gebuehr(t["price"]) * 10
        pnl = (10.0 if t["side"] == sieger else 0.0) - kosten
        for k in (w["tf"], "*"):
            s = st.setdefault(k, {"n": 0, "win": 0, "cost": 0.0, "pnl": 0.0})
            s["n"] += 1
            s["win"] += 1 if t["side"] == sieger else 0
            s["cost"] = round(s["cost"] + kosten, 4)
            s["pnl"] = round(s["pnl"] + pnl, 4)
        letzte = st.setdefault("last", [])
        letzte.insert(0, {"a": w["a"], "tf": w["tf"], "side": t["side"], "price": t["price"], "p": t["p"],
                          "won": t["side"] == sieger, "pnl": round(pnl, 3), "end": int(w["start"] + w["dauer"])})
        del letzte[40:]
        st.setdefault("since", int(time.time()))
        storage.kv_set("krypto_ml_live", json.dumps(st))


# ── Modell ───────────────────────────────────────────────────────────────

def _lade_modell():
    if time.time() - _MODELL["ts"] > 300:
        try:
            _MODELL["m"] = json.loads(storage.kv_get("krypto_ml_model") or "null")
        except Exception:
            _MODELL["m"] = None
        _MODELL["ts"] = time.time()
    return _MODELL["m"]


def _p(mod, x):
    s = mod["b"]
    for k, wv, mu, sd in zip(MERKMALE, mod["w"], mod["mu"], mod["sd"]):
        s += wv * ((float(x.get(k, 0.0)) - mu) / sd)
    s = max(-30, min(30, s))
    return 1 / (1 + math.exp(-s))


def vorhersage(tf, x):
    m = _lade_modell()
    mod = (m or {}).get(tf)
    if not mod:
        return None
    try:
        return round(min(0.997, max(0.003, _p(mod, x))), 4)
    except Exception:
        return None


def _fit(X, y, l2=L2, iters=30):
    """L2-logistische Regression mit Newton (IRLS). X ohne Achsenabschnitt, schon standardisiert."""
    import numpy as np
    n, d = X.shape
    Xb = np.hstack([np.ones((n, 1)), X])
    w = np.zeros(d + 1)
    w[0] = math.log(max(y.mean(), 1e-3) / max(1 - y.mean(), 1e-3))
    reg = np.eye(d + 1) * l2
    reg[0, 0] = 0
    for _ in range(iters):
        z = np.clip(Xb @ w, -30, 30)
        p = 1 / (1 + np.exp(-z))
        g = Xb.T @ (p - y) + reg @ w
        H = (Xb * (p * (1 - p))[:, None]).T @ Xb + reg
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            break
        w -= step
        if np.max(np.abs(step)) < 1e-6:
            break
    return w


def _trainiere(X, y):
    import numpy as np
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd[sd < 1e-9] = 1.0
    w = _fit((X - mu) / sd, y)
    return {"b": float(w[0]), "w": [float(v) for v in w[1:]], "mu": [float(v) for v in mu], "sd": [float(v) for v in sd]}


def _p_vec(mod, X):
    import numpy as np
    z = mod["b"] + ((X - np.array(mod["mu"])) / np.array(mod["sd"])) @ np.array(mod["w"])
    return 1 / (1 + np.exp(-np.clip(z, -30, 30)))


def _brier(ps, ys):
    import numpy as np
    return float(np.mean((ps - ys) ** 2)) if len(ys) else None


def _gebuehr_vec(p):
    return FEE_RATE * p * (1 - p)


def _sim(D, ps, schwelle):
    """Einmal je Fenster kaufen, beim ersten Punkt (ab 20 %) mit genug Vorsprung. 10 Anteile, echter Ask, Gebuehr."""
    import numpy as np
    au, ad, y, chk, fen = D["ask_u"], D["ask_d"], D["y"], D["chk"], D["win"]
    eu = ps - au - _gebuehr_vec(au)
    ed = (1 - ps) - ad - _gebuehr_vec(ad)
    up = eu >= ed
    ask = np.where(up, au, ad)
    e = np.where(up, eu, ed)
    ok = (e >= schwelle) & (ask >= 0.05) & (ask <= 0.95) & (chk >= 0.2)
    idx = np.where(ok)[0]
    if not len(idx):
        return {"n": 0, "win": 0, "hit": None, "cost": 0.0, "pnl": 0.0, "roi": None}
    reihen = idx[np.lexsort((chk[idx], fen[idx]))]
    _, erste = np.unique(fen[reihen], return_index=True)
    sel = reihen[erste]
    kosten = ask[sel] * 10 + _gebuehr_vec(ask[sel]) * 10
    gewonnen = (up[sel] & (y[sel] == 1)) | (~up[sel] & (y[sel] == 0))
    pnl = np.where(gewonnen, 10 - kosten, -kosten)
    n, k = len(sel), float(kosten.sum())
    return {"n": int(n), "win": int(gewonnen.sum()), "hit": round(float(gewonnen.mean()), 4),
            "cost": round(k, 2), "pnl": round(float(pnl.sum()), 2), "roi": round(float(pnl.sum()) / k, 4) if k else None}


def _kalibrierung(ps, ys):
    import numpy as np
    out = []
    b = np.minimum(9, (ps * 10).astype(int))
    for i in range(10):
        m = b == i
        if m.any():
            out.append({"from": i / 10, "to": (i + 1) / 10, "n": int(m.sum()), "real": round(float(ys[m].mean()), 3)})
    return out


def _laden(pg, tf, limit):
    import numpy as np
    spalten = ", ".join(f"COALESCE((x->>'{k}')::float8, 0)" for k in MERKMALE)
    rows = pg.execute(f"""SELECT slug, chk, to_char(ts AT TIME ZONE 'UTC','YYYY-MM-DD'), mid, ask_u, ask_d, p_eng, y, {spalten}
                          FROM krypto_lern WHERE tf=%s AND y IS NOT NULL AND mid IS NOT NULL
                          AND ask_u IS NOT NULL AND ask_d IS NOT NULL AND p_eng IS NOT NULL
                          ORDER BY ts DESC LIMIT %s""", (tf, limit)).fetchall()
    if not rows:
        return None
    rows.reverse()
    slugs = {}
    D = {"win": np.array([slugs.setdefault(r[0], len(slugs)) for r in rows]),
         "chk": np.array([float(r[1]) for r in rows]),
         "day": np.array([r[2] for r in rows]),
         "mid": np.array([float(r[3]) for r in rows]), "ask_u": np.array([float(r[4]) for r in rows]),
         "ask_d": np.array([float(r[5]) for r in rows]), "p_eng": np.array([float(r[6]) for r in rows]),
         "y": np.array([int(r[7]) for r in rows]),
         "X": np.array([[float(v) for v in r[8:]] for r in rows], dtype=float)}
    return D


def trainieren():
    import numpy as np
    t0 = time.time()
    pg = _pg()
    if not pg:
        return None
    try:
        pg.execute("DELETE FROM krypto_lern WHERE ts < now() - make_interval(days => %s)", (HALTEN_TAGE,))
    except Exception:
        pass
    offen = pg.execute("SELECT count(*) FROM krypto_lern WHERE y IS NULL").fetchone()[0]
    bericht = {"ts": int(time.time()), "unresolved": int(offen), "tf": {}, "features": list(MERKMALE),
               "note": "Internal. Walk-forward: each test day is predicted by a model trained only on earlier days."}
    modelle, gesamt = {}, 0
    for tf in ("5m", "15m", "4h"):
        D = _laden(pg, tf, int(os.getenv("LAB_MAX_ROWS", "150000")))
        if D is None:
            bericht["tf"][tf] = {"rows": 0, "verdict": f"collecting data: needs {MIN_TRAIN} settled rows"}
            continue
        n = len(D["y"])
        gesamt += n
        tage = sorted(set(D["day"].tolist()))
        info = {"rows": n, "windows": int(len(set(D["win"].tolist()))), "days": len(tage), "from": tage[0], "to": tage[-1]}
        tp, tm = [], []
        for d in tage[-TEST_TAGE:]:
            tr = D["day"] < d
            te = D["day"] == d
            if tr.sum() < MIN_TRAIN or not te.any():
                continue
            mod = _trainiere(D["X"][tr], D["y"][tr])
            tp.append(_p_vec(mod, D["X"][te]))
            tm.append(te)
        if tp:
            maske = np.zeros(n, dtype=bool)
            ps_voll = np.zeros(n)
            for p_, m_ in zip(tp, tm):
                ps_voll[m_] = p_
                maske |= m_
            T = {k: (v[maske] if hasattr(v, "__len__") and len(v) == n else v) for k, v in D.items()}
            ps, ys = ps_voll[maske], T["y"]
            wf = {"rows": int(maske.sum()), "windows": int(len(set(T["win"].tolist()))), "test_days": len(tp),
                  "brier_model": round(_brier(ps, ys), 5), "brier_market": round(_brier(T["mid"], ys), 5),
                  "brier_engine": round(_brier(T["p_eng"], ys), 5), "calibration": _kalibrierung(ps, ys),
                  "paper": {str(s): _sim(T, ps, s) for s in SCHWELLEN},
                  "engine_paper": {str(s): _sim(T, T["p_eng"], s) for s in SCHWELLEN}}
            info["walk_forward"] = wf
            besser = wf["brier_model"] < wf["brier_market"]
            beste = max(((s, v) for s, v in wf["paper"].items() if v["n"] >= 50), key=lambda t: t[1]["pnl"], default=None)
            info["best_edge"] = float(beste[0]) if beste and beste[1]["pnl"] > 0 else None
            info["verdict"] = ("promising: beats the market price out of sample and the paper result is positive"
                               if besser and info["best_edge"] else
                               "beats the market price, but no threshold makes money yet" if besser else
                               "not better than the market price yet")
        else:
            info["verdict"] = f"collecting data: needs {MIN_TRAIN} settled rows before the first test day"
        if n >= MIN_TRAIN:
            mod = _trainiere(D["X"], D["y"])
            mod["edge"] = info.get("best_edge") or float(os.getenv("LAB_EDGE", "0.05"))
            mod["n"] = n
            modelle[tf] = mod
            info["weights"] = sorted(([k, round(wv, 4)] for k, wv in zip(MERKMALE, mod["w"])), key=lambda t: -abs(t[1]))[:10]
        bericht["tf"][tf] = info
    bericht["rows"] = gesamt
    bericht["seconds"] = round(time.time() - t0, 1)
    storage.kv_set("krypto_lab", json.dumps(bericht, default=str))
    if modelle:
        storage.kv_set("krypto_ml_model", json.dumps(modelle))
        _MODELL["ts"] = 0.0
    log.info(f"lernen: {gesamt} rows, {len(modelle)} models, {bericht['seconds']} s")
    return bericht


def schleife():
    time.sleep(600)
    while True:
        try:
            trainieren()
        except Exception as e:
            log.warning(f"lernen trainieren: {e}")
        time.sleep(TAKT_H * 3600)


def bericht():
    try:
        b = json.loads(storage.kv_get("krypto_lab") or "{}")
    except Exception:
        b = {}
    try:
        live = json.loads(storage.kv_get("krypto_ml_live") or "{}")
    except Exception:
        live = {}
    zeilen = None
    try:
        pg = _pg()
        if pg:
            r = pg.execute("SELECT count(*), count(y), min(ts) FROM krypto_lern").fetchone()
            zeilen = {"total": r[0], "settled": r[1], "since": r[2].isoformat() if r[2] else None}
    except Exception:
        pass
    return {"report": b, "shadow": live, "rows": zeilen}
