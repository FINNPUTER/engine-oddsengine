# ODDSENGINE — THE NUMBERS

Everything below is measured. Nothing is estimated, projected or rounded in
our favour. If a number isn't here, we don't have it yet.

**Status: backtest complete. Live record starts now.**

---

## PART 1 — HOW WE FOUND IT (real money, on-chain)

Before any of this was a model, it was **901 bets with real money** across
23 cities in 18 days, Mar–Apr 2026. Crude method: read the station, bet the
number, hedge a degree either side. No distribution, no optimizer.

Net result: **+16.9%**. But the total isn't the point. Sort every bet by what
it cost:

| entry price | bets | in | out | **return** |
|---|---|---|---|---|
| under 1¢ | 35 | $46.30 | $0.00 | **−100%** |
| 1–3¢ | 23 | $38.85 | $55.56 | +43% |
| 3–5¢ | 17 | $33.00 | $33.29 | +1% |
| **5–10¢** | 33 | $68.19 | $130.03 | **+91%** |
| **10–20¢** | 46 | $116.92 | $176.37 | **+51%** |
| 20–35¢ | 47 | $147.01 | $135.14 | −8% |
| 35–60¢ | 43 | $201.63 | $208.06 | +3% |

**The lottery tickets went to zero.** 35 bets under a cent, not one came in.

**The favourites paid nothing.** 90 bets at 20–60¢, for −8% and +3%. That's
where the market is efficient and everyone is looking.

**The middle paid everything.** 79 bets at 5–20¢: +91% and +51%.

That shape is **favourite–longshot bias**. Found by accident, with real money,
before anything was built to look for it. Every trade is settled on Polygon
and independently verifiable.

**The honest caveat:** roughly half of all payouts came from ten lucky hits —
one $1 bet returned $55. Strip those and the total is near zero. 18 days
proves nothing on its own.

**And this is not OddsEngine's track record.** It was a different, cruder
system — a person reading a station and hedging by hand. It's here because
it's where the pattern surfaced, independently of the model that came after.

That's why we then tested 300 markets with a real model.

---

## PART 2 — THE ONE LINE

> **Backtest, 300 resolved markets, Dec 2025 – Mar 2026:
> the engine said 84.9%. It happened 84.7%.**

Off by 0.2 percentage points across 300 markets.

---

## IN PLAIN LANGUAGE

Polymarket runs temperature markets on 50 cities. *"Highest temperature in
Seoul on July 18?"* — eleven brackets, one wins.

We forecast the whole distribution and pick a range of three to five degrees.

- That range costs about **77 cents** on the market.
- It lands inside about **85 times out of 100**.
- Paying 77 for something worth 85 is the business. Repeated daily, across 50 cities.

**Why is the gap there?** People buy lottery tickets. *"35°C for 2 cents —
that's a 50x!"* It almost never comes in. The extremes get bid up, the boring
middle gets left alone. We buy the boring middle.

---

## THE NUMBERS

| | | |
|---|---|---|
| **300** | markets tested | every temperature market that resolved, Dec–Mar |
| **84.9%** | what the engine said | averaged across all 300 |
| **84.7%** | what actually happened | off by 0.2 points |
| **77.2%** | what the market charged | 7.5 points less than it was worth |
| **+7.5pp** | the edge | the gap, per signal |
| **39%** | of markets get a signal | the other 61% are fairly priced, too thin, or the models disagree |
| **−38.1%** | the control | same markets, no engine, buy the cheapest block |

---

## HOW WE KNOW IT ISN'T CURVE-FITTED

σ is the one knob that matters — how much uncertainty we add around each
forecast. Sweeping it across the full plausible range, over 269 markets:

| σ | we said | happened | error | market | **edge** |
|---|---|---|---|---|---|
| 0.8 | 86.6% | 76.6% | −10.1 | 68.7% | **+7.9** |
| 1.0 | 86.5% | 79.9% | −6.7 | 72.2% | **+7.7** |
| 1.2 | 85.3% | 81.5% | −3.7 | 74.0% | **+7.5** |
| **1.4** | **84.9%** | **84.7%** | **−0.2** | **77.2%** | **+7.5** |
| 1.6 | 83.4% | 86.1% | +2.7 | 78.9% | **+7.2** |
| 2.0 | 81.1% | 89.5% | +8.5 | 83.3% | **+6.2** |
| 2.8 | 82.1% | 95.7% | +13.6 | 89.8% | **+5.8** |

**The error column swings 24 points. The edge column moves 2.**

That is the difference between a fitted parameter and a real effect. An
overfitted result wanders with its knob until it disappears. This one doesn't.

We ship **σ = 1.4** — the row where the engine says what it means.

---

## WHAT WE FOUND IN OURSELVES

Across all 300 markets, the engine is accurate to 0.2 points.

On the ones it **selects**, it claimed 81.3% and hit 72.4%.

Picking the best of ~25 candidate ranges selects for your own optimism — the
options that look best are the ones where your estimate happened to err high.
Averaged over everything it cancels out. Over the winners it does not. And the
winner is what goes in the signal.

**So 8.8 points are subtracted before anything is published.** Fewer signals.
Honest ones.

This isn't modesty, it's arithmetic. Sizing off the inflated number would have
staked 53% of a bankroll where 31% was correct — and turned a 46x into a 3x.
Four points further and a real, working edge would have lost money.

---

## WHAT THIS IS NOT

**This is a backtest, not a track record.** Specifically:

- **No order book depth.** It buys at the quoted ask no matter how thin the book. Live fills are worse.
- **σ was fitted on the same markets it was tested on.** Out-of-sample is always worse.
- **23 trading days.** A winter. Summer is more volatile.
- **The 8.8pp haircut came after.** It will cut the signal count roughly in half.
- **No live record exists yet.** Zero signals have been published.

**Realistic live expectation: +3% to +10% per signal, on materially fewer
signals than the backtest produced.** Anyone quoting the backtest as a live
return — including us — should be ignored.

---

## METHOD

**Forecasts.** ~11 archived model runs per market (ECMWF IFS + AIFS, GFS,
ICON, JMA, GEM, Météo-France, UKMO), taken as they were *issued* — not from
today's models looking backwards. Verified: on 2026-07-03 at Incheon the
archive returned GFS at 22.2°C while the truth was 26.0°C. A model that had
seen the answer does not miss by 3.8 degrees.

**Stations.** All 50 resolution stations are parsed from each market's own
rules, via the ICAO code in the source URL. NYC resolves on **LaGuardia**, not
Central Park. London on **London City**, not Heathrow. Paris on **Le Bourget**,
not CDG. Forecasting the city centre instead of the station is a one-to-two
bracket error, every single day.

**Daily maximum.** For each model we take the hourly series and compute the
maximum over the market's local calendar day, including the night. Models
publish their own daily-max field; it misses warm-front nights. We don't use it.

**Truth.** The resolved market itself. Polymarket reads the resolution source
and settles; we read the settlement. No scraping, no second-guessing.

**Brackets.** The rules state the source measures "to whole degrees" — verified
in the text for 50 of 51 cities. So "33°C" is exactly the readings that round
to 33. Hong Kong resolves at 0.1°C precision and never states where its bracket
edge sits, so it's excluded.

---

## WHERE THE EDGE COMES FROM

Not from forecasting better than the market. On a per-bracket basis the market
is slightly sharper than we are.

**Favourite–longshot bias** — the most robustly documented effect in betting
markets. Three independent datasets say the same thing here:

| Source | Finding |
|---|---|
| On-chain, real money, 244 markets, Mar–Apr 2026 | under 1¢ → **−100%** · 5–20¢ → **+91%** |
| Backtest, 300 markets, Dec 2025–Mar 2026 | engine 84.7% vs market 77.2% → **+7.5pp** |
| Backtest control, same 300 markets | buy the cheapest block → **−38.1%** |

Different systems, different periods, different methods, one built before the
others existed. Same conclusion every time: the extremes are overpriced, the
middle is underpriced.

That is the difference between a finding and a coincidence.

**We don't sell "we see the weather better." We sell "the market has a
structural lean, and we harvest it with discipline."** Harder to copy, easier
to prove.

---

## WHAT HAPPENS NEXT

Every signal is written to an append-only log the moment it publishes — full
distribution, ensemble spread, executable asks, timestamp. Nothing is edited
afterwards. When the station reports, it's scored.

After ~100 live signals we'll know whether 84.7% holds. That number replaces
everything on this page.

**Free until then.** Charging for numbers only ever scored against the past
would make us the thing we're trying to replace.

**oddsengine.live**
