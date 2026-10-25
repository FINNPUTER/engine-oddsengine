# START HERE

OddsEngine 2.0 — everything, in order. 30 minutes to live.

---

## WHAT THIS IS

Polymarket temperature markets → real probability distribution → the best
contiguous 3–5 outcome block → a Telegram signal with one-tap links.

Read-only. No wallet, no private key. The engine cannot lose money. It
publishes numbers; people decide for themselves.

---

## THE NUMBERS. ALL MEASURED, NONE GUESSED.

Backtested on **300 resolved markets**, Dec 2025 – Mar 2026, using the
archived model runs as they were issued — not today's models looking
backwards.

| | |
|---|---|
| Engine said | **84.9%** |
| Actually happened | **84.7%** |
| Calibration error | **0.2pp** |
| Market's price for the same blocks | **77.2%** |
| Edge | **+7.5pp** |
| Signal rate | 39% of markets |

**The edge does not move.** Tripling the dressing width σ swings the
calibration error from −10pp to +14pp and the hit rate from 77% to 96%.
The edge stays between +5.8 and +7.9pp the whole way. An overfit wanders
with its parameter. This doesn't.

**Control:** same markets, no engine, just buy the cheapest four-block →
**−38.1%**. The tails are expensive and the middle is cheap. That gap is
the entire business.

**What we found in ourselves:** across all markets the engine is calibrated
to 0.2pp. On the ones it *selects*, it claimed 81.3% and hit 72.4% — picking
the best of ~25 blocks selects for upward noise. That 8.8pp is now subtracted
before anything is published (`COVERAGE_HAIRCUT_PP`).

That correction is not modesty, it is arithmetic. Kelly sizing on the inflated
number would have staked 53% of bankroll instead of 31%, and turned a 46x into
a 3x. Honesty is the biggest single lever in this system.

---

## WHY IT WORKS

Not because the forecast beats the market — on per-bucket Brier the market
wins by 6%.

Because of **favourite–longshot bias**. People buy lottery tickets. The edges
get bid up, the middle is left alone. The engine buys the boring middle.

Two independent datasets say the same thing:
- On-chain trading record: under 1¢ → **−100%**, 5–20¢ → **+91%**
- Backtest control: buy the cheapest block → **−38.1%**

Sell that. Not "we see the weather better." **"The market has a structural
lean and we harvest it with discipline."** Harder to copy, easier to prove.

---

## DO THIS

### 1. Repo (5 min)

github.com → New repository → **`oddsengine-platform`** → **Private** → Create
→ "uploading an existing file" → drag everything from `oddsengine-platform/`
(including the `tools/` folder) → Commit

### 2. Railway (10 min)

railway.app → New Project → **Deploy from GitHub repo** → `oddsengine-platform`
→ Settings → rename the project to **`oddsengine`**

**Do not reuse the old Railway project.** It is wired to the crypto bot. Leave
it alone, don't delete it.

### 3. Postgres (1 min)

Same project → **+ New → Database → PostgreSQL**

Railway injects `DATABASE_URL` into every service. `storage.py` creates the
table on boot. Without this, every redeploy wipes your track record — which
is exactly how the droplet died.

### 4. Variables (2 min)

```
SIGNAL_MODE=observe
RISK_MODE=balanced
TELEGRAM_BOT_TOKEN=<@BotFather>
TELEGRAM_PRIVATE_CHAT_ID=<@userinfobot>
```

### 5. Deploy — the log must say

```
version 2.1.1  (2026-07-17  sigma=1.4 + 8.8pp optimizer-curse haircut)
signal_mode=observe  risk_mode=balanced
postgres snapshot store ready
discovered N active temperature events
scan done: N markets | X signals | Y no-signal
```

No `postgres snapshot store ready` → the DB isn't attached → redeploy.

**Expect 2–5 signals from ~50 markets.** More than 40% and the engine warns
you itself — that would be a bug, not an edge.

### 6. Landing page (5 min)

github.com → New repository → **`oddsengine-landing`** → **Public**
→ drag everything from `oddsengine-landing/` → Settings → Pages →
`main` / root → Save

Before you publish: the Telegram link is a placeholder in three places
(search `t.me/oddsengine`), and `onboarding.html` is untouched from the old
site — check it for crypto leftovers.

### 7. Tomorrow

Signals look sane in the private chat → change one word:

```
SIGNAL_MODE=calibration
TELEGRAM_PUBLIC_CHANNEL_ID=@OddsEngine
```

Redeploy. The channel is live. See `TELEGRAM.md`.

---

## THE THREE RULES

**No invented numbers.** Backtest is labelled backtest. The live record starts
when the worker starts. Nothing on the site has not been measured.

**Free until it holds live.** A backtest is not a track record. Charging for
numbers only ever scored against the past makes us the thing we're replacing.

**Publish the misses.** Every signal goes to an append-only log before the
station reports, and gets scored either way. Any channel can post winners.

---

## LATER, NOT NOW

**Auto-execution.** Technically ready — `py-clob-client`, RiskManager with
kill switch and loss limits all exist in the old code. But the engine has
never run live. Wiring a wallet to something that has never produced a real
signal is exactly the mistake `nohup.out` documents: protections built,
never connected, `edge_evals=0`, bankroll −69%.

**$ODDS token.** The only honest utility is API credits, burned on use —
consumption, not revenue share. And not before the live record exists;
otherwise it is a memecoin with a weather theme. Free stays free: channel,
chart, dashboard, landing.

**Same-day nowcast.** At 3pm local, if the station already reads 33°C, the
day's max is *at least* 33. No model knows that and the market is often slow
to price it. That was the real edge in the old manual workflow (`wu_check.py`)
and it is not in the engine yet.

**Hong Kong** is excluded — it resolves at 0.1°C precision while the other 50
cities round to whole degrees, and the rules never say where its bucket edge
sits. Measure before including.

---

## FILES

```
oddsengine-platform/
  main.py          worker: discover → evaluate → post → log
  engine.py        discovery, ensemble, distribution, optimizer, render
  stations.py      50 resolution stations, ICAO from the rules
  config.py        every knob, environment only
  storage.py       append-only snapshots (JSONL + Postgres)
  telegram.py      poster, observe / calibration / live
  api.py           read API — second Railway service, same repo
  tools/
    check_resolution_rules.py
    calibration.py            the report that matters
  backtest_roi.py  score the engine against resolved markets
  sim2.py          bankroll replay
  rules_audit.py   rebuild the station table

oddsengine-landing/
  index.html       the site
  logo.png og-image.png icon-*.png CNAME onboarding.html
```

## THE KNOBS THAT WERE MEASURED

```
DRESS_SIGMA_C=1.4          fitted on 269 markets. 0.9 was 10pp overconfident.
COVERAGE_HAIRCUT_PP=8.8    the optimizer's curse, measured on 116 signals.
BUCKET_ROUNDING=0.5        verified in the rule text: "whole degrees" — 50/51 cities.
GAMMA_TAG_ID=104596        100/100 hit rate. Never paginate /events unfiltered.
SCAN_INTERVAL_MIN=180      models rerun every 6–12h. Hourly burns the quota.
```

Re-measure `DRESS_SIGMA_C` and `COVERAGE_HAIRCUT_PP` with `sim2.py` once live
data accumulates. They are the two numbers that decide whether the channel is
honest.
