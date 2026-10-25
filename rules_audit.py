#!/usr/bin/env python3
"""
Resolution rules audit — the groundwork nothing else can skip.

Two jobs:

  1. STATION TABLE. Hong Kong resolves on the Hong Kong Observatory's
     "Absolute Daily Max" — not an airport, not METAR. Every city has its
     own source. This dumps them all so we can build a real mapping
     instead of geocoding the city centre and hoping.

  2. BUCKET BOUNDARIES. The rules say the market resolves to "the range
     that contains" the max, measured to one decimal place. They do NOT
     say where the boundary sits: is 33C [32.5-33.4] or [33.0-33.9]?
     This groups the exact wording across cities so we can see whether
     any of them spell it out.

    python3 rules_audit.py
    python3 rules_audit.py --full     # print every description in full
"""
import argparse
import json
import re
import time
from collections import defaultdict

import requests

G = "https://gamma-api.polymarket.com"
TAG_HIGHEST_TEMP = 104596          # 100/100 hit rate. This is the one.
s = requests.Session()
s.headers["User-Agent"] = "oddsengine-audit/1.0"

SLUG_RE = re.compile(r"^highest-temperature-in-(?P<city>[a-z0-9-]+?)-on-")


def call(path, **params):
    time.sleep(0.15)
    r = s.get(f"{G}{path}", params=params, timeout=25)
    r.raise_for_status()
    return r.json()


def discover():
    """tag_id filter, paginated. No deep offset, no volume ordering problem."""
    out, offset = [], 0
    while offset < 1000:
        try:
            page = call("/events", tag_id=TAG_HIGHEST_TEMP, closed="false",
                        limit=100, offset=offset)
        except Exception as e:
            print(f"  ! offset={offset}: {e}")
            break
        if not page:
            break
        out.extend(page)
        if len(page) < 100:
            break
        offset += 100
    return out


def source_of(desc):
    """Pull the named resolution authority out of the rules text."""
    if not desc:
        return None
    pats = [
        r"recorded by the ([A-Z][A-Za-z\s\.\'()-]{3,58}?)(?: in degrees| on | as | for |,|\.)",
        r"information from the ([A-Z][A-Za-z\s\.\'()-]{3,58}?)(?:,|\.| specifically)",
        r"reported by ([A-Z][A-Za-z\s\.\'()-]{3,58}?)(?:,|\.| for )",
        r"according to ([A-Z][A-Za-z\s\.\'()-]{3,58}?)(?:,|\.| for )",
    ]
    for p in pats:
        m = re.search(p, desc)
        if m:
            return " ".join(m.group(1).split()).strip(" .,")
    return None


def field_of(desc):
    """The exact field name they read, e.g. 'Absolute Daily Max (deg. C)'."""
    m = re.search(r'"([^"]{4,60})"', desc or "")
    return m.group(1) if m else None


def url_of(desc):
    m = re.search(r"(https?://[^\s\)]+)", desc or "")
    return m.group(1) if m else None


def precision_of(desc):
    d = (desc or "").lower()
    if "one decimal place" in d:
        return "0.1"
    if "two decimal" in d:
        return "0.01"
    if "whole degree" in d or "nearest degree" in d or "rounded to the nearest" in d:
        return "integer"
    return "?"


def boundary_hint(desc):
    """Does ANY city spell out where the bucket edge sits?"""
    d = desc or ""
    hits = []
    for p in [r"[\d.]+\s*°?[CF]?\s*(?:to|-|–)\s*[\d.]+\s*°?[CF]",
              r"greater than or equal to", r"less than", r"at least",
              r"inclusive", r"exclusive", r"rounded", r"\bround\b",
              r"midpoint", r"\.5\b"]:
        for m in re.finditer(p, d, re.I):
            hits.append(d[max(0, m.start()-55):m.end()+55].replace("\n", " "))
    return hits


ap = argparse.ArgumentParser()
ap.add_argument("--full", action="store_true")
a = ap.parse_args()

print("Discovering via tag_id=104596 ...")
events = discover()
print(f"  {len(events)} live temperature events\n")
if not events:
    raise SystemExit("Nothing found — paste the output.")

by_city = {}
for ev in events:
    m = SLUG_RE.match(ev.get("slug", "") or "")
    if not m:
        continue
    city = m.group("city").replace("-", " ")
    desc = ev.get("description") or ""
    if not desc:
        mk = ev.get("markets") or []
        desc = (mk[0].get("description") if mk else "") or ""
    if city not in by_city:            # one event per city is enough
        by_city[city] = (ev, desc)

print("=" * 78)
print(f"  STATION TABLE  —  {len(by_city)} cities")
print("=" * 78)
print(f"  {'city':<15}{'unit':<6}{'prec':<7}{'buckets':<9}source")
print("  " + "-" * 74)

units = defaultdict(list)
precs = defaultdict(list)
for city, (ev, desc) in sorted(by_city.items()):
    mk = ev.get("markets") or []
    labels = [x.get("groupItemTitle", "") for x in mk]
    unit = "F" if any("F" in l for l in labels) else ("C" if labels else "?")
    src = source_of(desc) or "!! NOT PARSED !!"
    prec = precision_of(desc)
    units[unit].append(city)
    precs[prec].append(city)
    print(f"  {city:<15}{unit:<6}{prec:<7}{len(mk):<9}{src[:44]}")

print("\n  Units :", {k: len(v) for k, v in units.items()})
print("  Precision:", {k: len(v) for k, v in precs.items()})

print("\n" + "=" * 78)
print("  RESOLUTION FIELD + URL PER CITY")
print("=" * 78)
for city, (ev, desc) in sorted(by_city.items()):
    f, u = field_of(desc), url_of(desc)
    print(f"  {city:<15} {str(f or '-')[:36]:<38} {str(u or '-')[:34]}")

print("\n" + "=" * 78)
print("  BUCKET BOUNDARY — does anyone spell it out?")
print("=" * 78)
found_any = False
for city, (ev, desc) in sorted(by_city.items()):
    hints = boundary_hint(desc)
    if hints:
        found_any = True
        print(f"\n  --- {city} ---")
        for h in dict.fromkeys(hints):
            print(f"    ...{h.strip()}...")
if not found_any:
    print("\n  Nobody spells it out. The boundary must be measured against")
    print("  resolved markets, not read out of the rules. That is the next script.")

print("\n" + "=" * 78)
print("  DISTINCT RULE TEMPLATES")
print("=" * 78)
tmpl = defaultdict(list)
for city, (ev, desc) in by_city.items():
    key = re.sub(r"\d+", "#", re.sub(r"\s+", " ", desc))[:190]
    tmpl[key].append(city)
print(f"  {len(tmpl)} distinct templates across {len(by_city)} cities\n")
for i, (k, cities) in enumerate(sorted(tmpl.items(), key=lambda x: -len(x[1])), 1):
    print(f"  [{i}] {len(cities)} cities: {', '.join(sorted(cities))}")
    print(f"      {k[:170]}...\n")

if a.full:
    print("=" * 78)
    print("  FULL DESCRIPTIONS")
    print("=" * 78)
    for city, (ev, desc) in sorted(by_city.items()):
        print(f"\n{'='*78}\n{city.upper()}  —  {ev.get('slug')}")
        print(f"buckets: {', '.join(x.get('groupItemTitle','?') for x in ev.get('markets',[]))}")
        print(f"endDate: {ev.get('endDate')}\n")
        print(desc)

out = {c: {"slug": e.get("slug"), "endDate": e.get("endDate"),
           "source": source_of(d), "field": field_of(d), "url": url_of(d),
           "precision": precision_of(d),
           "buckets": [x.get("groupItemTitle") for x in (e.get("markets") or [])],
           "description": d}
       for c, (e, d) in by_city.items()}
with open("resolution_rules.json", "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print(f"\nWrote resolution_rules.json ({len(out)} cities)")
