"""
Run this FIRST. Before trusting a single probability.

Prints the raw resolution rules of live temperature markets so you can
verify by eye:
  - which station resolves the market
  - the rounding convention  ("33C" = [32.5,33.5) or the rounded reading?)
  - whether it is the max over the whole local day, incl. night

BUCKET_ROUNDING in config.py rests on this. Half a bucket of error and
every probability the engine emits is shifted.

    python tools/check_resolution_rules.py
    python tools/check_resolution_rules.py --n 8
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import engine

ap = argparse.ArgumentParser()
ap.add_argument("--n", type=int, default=5)
a = ap.parse_args()

events = engine.discover()
print(f"{len(events)} active temperature events\n")

for ev in events[:a.n]:
    print("=" * 70)
    print(f"{ev['city'].upper()}  —  {ev['date']}")
    print(f"slug    : {ev['slug']}")
    print(f"station : {ev['station'] or '!! NOT FOUND IN RULES !!'}")
    print(f"buckets : {', '.join(o['label'] for o in ev['outcomes'])}")
    print(f"unit    : {ev['outcomes'][0]['unit']}")
    print("-" * 70)
    print(ev["description"] or "(no description)")
    print()

print("=" * 70)
print("CHECKLIST — answer these before going live:")
print("  1. Is the station named, and is it an airport/METAR site?")
print("  2. Does it say 'highest temperature' over the whole day incl. night?")
print("  3. Is '33C' an interval or a rounded reading?")
print("  4. Which timezone defines the day?")
print("  5. Which source: WU, NWS, METAR, other?")
print("=" * 70)
