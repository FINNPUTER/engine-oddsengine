# OddsEngine 2.0 — Weather Probability Engine

Polymarket temperature markets → real probability distribution → best contiguous
3–5 outcome block by **edge** → Telegram signal → append-only snapshot.

Read-only. No wallet, no private key, no trading. Nothing here can lose money.

```
main.py            worker: discover → evaluate → post → log, on an interval
engine.py          discovery, ensemble forecast, distribution, optimizer, render
config.py          every knob, from environment only
storage.py         append-only snapshots (JSONL + optional Postgres)
telegram.py        poster (observe → private, live → public)
logger.py          stdout logging
tools/
  check_resolution_rules.py   RUN THIS FIRST
  calibration.py              the only report that matters
```

## Run it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python tools/check_resolution_rules.py    # verify assumptions FIRST
python main.py --dry                      # scan, print signals, post nothing
python main.py --once                     # one scan, post if configured
python main.py                            # loop (this is what Railway runs)
```

## The two numbers that can invalidate everything

**`BUCKET_ROUNDING = 0.5`** — the assumption that "33°C" means the interval
[32.5, 33.5). If Polymarket resolves on the rounded station reading instead,
every probability is shifted by half a bucket and the whole engine is wrong.
`tools/check_resolution_rules.py` prints the raw rules so you can verify by eye.
Do this before anything else.

**`DRESS_SIGMA_C = 0.9`** — how wide each ensemble member's kernel is. Raw
ensembles are underdispersed: they are more confident than they deserve to be.
This number is currently a guess. Too small and the engine invents signals out
of noise — which is exactly how v1 ended up with 35 sub-1¢ bets and no hits.
`tools/calibration.py` tells you which direction to move it.

## Why it stays quiet

Most city-days are fairly priced. If the engine finds edge in more than ~40% of
markets, it has a bug, not an edge — `main.py` prints a warning when that happens.
Expect 2–5 signals from 30 markets. A channel that posts twenty a day is posting
fifteen bad ones.

Gates, all in `.env`: minimum edge (by mode), minimum coverage, minimum ask
depth, maximum spread, lead-time window, minimum ensemble members.

## observe vs live

`SIGNAL_MODE=observe` (default) posts to a private chat and prefixes every
message with a notice. `SIGNAL_MODE=live` posts to the public channel.

**Stay on observe until `tools/calibration.py` reports a mean absolute
calibration error under ~3pp.** Selling uncalibrated numbers to paying
subscribers is how a signal channel dies — and it is the one thing the spec
(§13: no unprovable performance claims) explicitly forbids.

## Storage

Railway wipes the filesystem on every redeploy. Either attach a **Volume** and
set `DATA_DIR=/app/data`, or add **Postgres** — Railway injects `DATABASE_URL`
and `storage.py` picks it up automatically.

Without one of these, your track record dies with the next deploy. That is
literally how the old droplet was lost.

Snapshots are append-only. No UPDATE path exists. Spec §12.

## What it does not do yet

- Geocoding hits the city centre, not the resolution station. Milano Malpensa
  is 40 km from the Duomo. The station name is parsed from the rules but not
  yet used for coordinates.
- No per-city bias correction. That needs resolved history — the thing
  `calibration.py` is accumulating.
- No dashboard, no public API. Those come after the reliability chart fills in.
