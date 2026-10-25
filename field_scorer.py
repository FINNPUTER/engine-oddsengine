"""
Shadow-mode field scorer.

The forward test of the user's strategy: take our ensemble read, average it with
Wunderground's forecast, and play a 3-bracket field centred on that blend. Does
it actually hit, and does it make money, measured against the resolved market?

This reads snapshots we already stored (ens_mean, wu_high_c, market_prices) and
pulls the winning bucket from Gamma (resolution.winning_bucket). No new data
collection, no lookahead: every number was written before the market settled.
WU cannot be backtested (no archived WU forecasts), so this only scores markets
we snapshotted live -- which is exactly the point.
"""
import re
import engine
import storage
import resolution
from config import cfg


def _bucket_c(k):
    """
    Repraesentative Mitte eines Bucket-Labels in Grad Celsius.

    Muss dieselben Buckets kennen wie die Engine. Die alte Fassung hat die
    Randbuckets ("39C or higher", "29C or below") herausgefiltert — die Engine
    kauft sie aber, und sie gewinnen regelmaessig. Jeder solche Gewinner zaehlte
    dadurch als Fehlschlag, egal wie gut der Read lag. Zusaetzlich klemmte das
    Fenster dann nach unten weg, was heisse Tage doppelt bestraft hat.
    """
    if not k:
        return None
    is_f = "F" in k.upper()
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", k)]
    if not nums:
        return None
    if "higher" in k.lower():
        v = nums[0] + (1.0 if is_f else 0.5)      # lo_c + 1.0, wie engine
    elif "below" in k.lower():
        v = nums[0] - (1.0 if is_f else 0.5)
    elif len(nums) >= 2:
        v = (nums[0] + nums[1]) / 2.0             # "100-101F" -> 100.5
    else:
        v = nums[0]
    return (v - 32.0) * 5.0 / 9.0 if is_f else v


def _blend_field(snap, corrected=True):
    """
    Die 3 zusammenhaengenden Buckets, zentriert auf den Read.

    corrected=True bildet nach, was die Engine wirklich tut:
      * WU wird mit WU_WEIGHT gewichtet, nicht stur halbe-halbe
      * der gemessene Stadt-Bias wird abgezogen (read - bias, wie distribution())
    corrected=False ist die alte, unkorrigierte Grundlinie zum Vergleich.
    """
    mp = snap.get("market_prices") or {}
    ens = snap.get("ens_mean")
    wu = snap.get("wu_high_c")
    if ens is None or not mp:
        return None

    if corrected:
        w = cfg.wu_weight if cfg.wu_enabled else 0.0
        center = ens + w * (wu - ens) if wu is not None else ens
        _, bias = engine.city_params(snap.get("city"))
        center = center - bias
    else:
        center = (ens + wu) / 2.0 if wu is not None else ens

    # Alles in Celsius, Randbuckets eingeschlossen.
    cand = [(k, _bucket_c(k)) for k in mp]
    cand = sorted([(k, c) for k, c in cand if c is not None], key=lambda t: t[1])
    if len(cand) < 3:
        return None

    ci = min(range(len(cand)), key=lambda i: abs(cand[i][1] - center))
    lo = max(0, min(ci - 1, len(cand) - 3))
    win = [k for k, _ in cand[lo:lo + 3]]
    cost = sum(mp.get(k, 0) or 0 for k in win)
    return {"field": win, "cost": round(cost, 3), "center": round(center, 1),
            "used_wu": wu is not None, "corrected": corrected}


def run(max_markets=50, maxcost=0.80, corrected=True):
    """Score the blend-field over every resolved market we snapshotted."""
    # city_params.json wird nur von main.py geladen, dieser Endpunkt laeuft
    # aber im API-Dienst. Ohne das hier waere der Stadt-Bias still null.
    if not engine._CITY_PARAMS:
        engine._load_city_params()

    rows = storage.load_all()
    # latest snapshot per market that actually has a distribution + prices
    latest = {}
    for r in rows:
        if not (r.get("full_dist") and r.get("market_prices")):
            continue
        slug = r.get("slug")
        if not slug:
            continue
        if slug not in latest or r.get("ts", "") > latest[slug].get("ts", ""):
            latest[slug] = r

    out = {"scored": 0, "hit": 0, "staked": 0.0, "returned": 0.0,
           "skipped_too_expensive": 0, "unresolved": 0, "no_wu": 0,
           "maxcost": maxcost, "corrected": corrected,
           "wu_weight": cfg.wu_weight if cfg.wu_enabled else 0.0,
           "by_city": {}, "detail": []}
    checked = 0
    for slug, snap in latest.items():
        if checked >= max_markets:
            break
        fld = _blend_field(snap, corrected=corrected)
        if not fld:
            continue
        label, resolved = resolution.winning_bucket(
            snap.get("event_slug") or slug)
        checked += 1
        if not resolved:
            out["unresolved"] += 1
            continue
        if not fld["used_wu"]:
            out["no_wu"] += 1
        if fld["cost"] > maxcost:
            out["skipped_too_expensive"] += 1
            continue
        hit = label in fld["field"]
        out["scored"] += 1
        out["staked"] += fld["cost"]
        out["returned"] += 1.0 if hit else 0.0
        out["hit"] += 1 if hit else 0
        city = snap.get("city", "?")
        bc = out["by_city"].setdefault(city, {"n": 0, "hit": 0})
        bc["n"] += 1
        bc["hit"] += 1 if hit else 0
        out["detail"].append({
            "city": city, "date": snap.get("market_date"),
            "our_read": snap.get("ens_mean"), "wu": snap.get("wu_high_c"),
            "blend_center": fld["center"], "field": fld["field"],
            "cost_c": round(fld["cost"] * 100), "winner": label, "hit": hit})

    if out["scored"]:
        out["hit_rate"] = round(out["hit"] / out["scored"], 3)
        out["breakeven_needed"] = (round(out["staked"] / out["scored"], 3)
                                   if out["scored"] else None)
        out["roi"] = (round((out["returned"] - out["staked"]) / out["staked"], 3)
                      if out["staked"] else None)
        out["staked"] = round(out["staked"], 2)
        out["returned"] = round(out["returned"], 2)
        out["verdict"] = ("field makes money" if out.get("roi", 0) and out["roi"] > 0
                          else "field loses or breaks even")
    else:
        out["note"] = ("No resolved markets scored yet. Snapshots accumulate "
                       "live; check back after markets settle.")
    return out
