# TELEGRAM , rebuild the channel

Copy-paste ready. The channel is the product; the site is the proof.

---

## 1. GET THE IDS

**Bot token:** @BotFather → `/newbot` → copy the token.

**Your private chat id:** message @userinfobot → copy the number.

**Channel id:** add the bot to the channel **as admin** (needs "Post Messages"),
post anything once, then open:

```
https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
```

Look for `"chat":{"id":-1001234567890}`. Channels are negative. That's the one.

---

## 2. CHANNEL DESCRIPTION

Settings -> Edit -> Description. 255 characters, this fits.

```
Probability distributions for weather prediction markets.
Every number published before the station reports, and scored after.
Hits and misses, all of them.
Free while the record is short.
oddsengine.live
```

German version, if you run a second channel:

```
Wahrscheinlichkeiten fuer Wetter-Prognosemaerkte.
Jede Zahl wird veroeffentlicht, bevor die Station meldet, und danach gescort.
Treffer wie Nieten, alle.
Gratis, solange der Record kurz ist.
oddsengine.live
```

---

## 3. PINNED POST

Post it, then pin it. First thing anyone reads.

```
ODDSENGINE, PRECISION SIGNALS. ZERO NOISE.

What this is
────────────
Every day, Polymarket runs temperature markets on 50 cities.
"Highest temperature in Seoul on July 18?", eleven brackets,
one wins.

We put a probability on every bracket, compare it to what the
market charges, and post only where the gap is worth taking.

What a signal contains
──────────────────────
· the temperatures we're on
· our probability for each one
· combined coverage
· the price of every leg, and of the whole block
· the edge, coverage minus cost
· a suggested split, and the payout if it lands
· one-tap links straight to each market

You decide what to buy. We never hold your keys and we never
touch your wallet.

Why most days are quiet
───────────────────────
Roughly two in five markets clear the gates. The rest are fairly
priced, too thin, too wide, or the models disagree. A channel
posting twenty signals a day is posting fifteen bad ones.

If nothing comes through, nothing gets posted. That is the point,
not a fault.

The record
──────────
261 markets, already settled, already scored:

· we said it would land 85 times out of 100
· it landed 85 times out of 100
· the market priced the same blocks at 77 out of 100

That was measured in hindsight. The same thing now runs here, in
public, and every result joins it. Hits and misses both.

Free while the record is short. When it is long enough to be
worth paying for, it will cost 99 euro a month.

oddsengine.live
```

---

## 4. FIRST POST AFTER THE SWITCH
## 4. FIRST POST AFTER THE SWITCH

Post this when you flip `SIGNAL_MODE=calibration`.

```
The engine is running.

From here, every signal is published in this channel before the
station reports, and scored afterwards. Hit or miss, no exceptions,
nothing quietly dropped.

Free while the record is short.

Expect quiet days. Three in five markets get no signal at all.
That is the point.
```

---

## 5. THE RULES THIS CHANNEL LIVES BY

**Post the misses.** Every signal is written to an append-only log the
moment it goes out , full distribution, ensemble spread, executable asks,
timestamp. Nothing is edited afterwards. Anyone can post winners; the
record is what people pay for later.

**No performance claims until they're live.** The backtest may be quoted
only as a backtest. No ROI on the site, no "8.7x", no bankroll numbers.
Those came from a simulation with no order book depth and a σ fitted on
the same markets it was tested against.

**Never gate the channel behind a token.** The whole thesis is credibility.
If a token ever happens, it buys API credits , not signals.

**Silence is a feature.** If the engine has nothing, the channel says
nothing. Do not lower the gates to fill the feed.

---

## 6. THE SWITCH

```
SIGNAL_MODE=observe        private chat. Nobody sees it. Day 1.
SIGNAL_MODE=calibration    the public channel, with an honest footer. Free.
SIGNAL_MODE=live           no footer. Only after tools/calibration.py
                           reports under ~3pp on LIVE data.
```

In `calibration` every signal carries:

```
,,,
CALIBRATION
The engine is being scored in public. This signal was published before
the station reported , we post the result either way, hit or miss.
Free until the reliability chart fills in. No performance claims until then.
oddsengine.live
```

That footer is not fine print. It's the pitch. Every other signal channel
posts its winners. None of them post the number they promised beforehand
and the number they delivered afterwards.

`REPOST_COOLDOWN_H=24` , one post per market per day. Without it the same
city lands in the channel three times before lunch.

---

## 7. WHAT A SIGNAL LOOKS LIKE

```
ODDSENGINE , SEOUL

Market:
Highest temperature in Seoul
Resolution station: Incheon Intl (RKSI)
2026-07-18

Mode:
Balanced

Selected range:
27°C · 28°C · 29°C · 30°C · 31°C

Engine probabilities:
27°C , 11%
28°C , 20%
29°C , 25%
30°C , 21%
31°C , 12%

Combined coverage:
80%

Current executable prices:
27°C , 9.0¢
28°C , 16.0¢
29°C , 20.0¢
30°C , 14.0¢
31°C , 8.0¢

Market price of block:
67.0¢

Edge:
+12.7pp

Budget:
$10.00

Suggested allocation:
27°C , $1.34
28°C , $2.39
29°C , $2.99
30°C , $2.09
31°C , $1.19

Quote if covered:
1.49x

Estimated payout if covered:
$14.93

Outside-range risk:
20%

Lead time:
13h

Published:
17:06 UTC
```

Buttons underneath:

```
[ 27°C · $1.34 ]  [ 28°C · $2.39 ]  [ 29°C · $2.99 ]
[ 30°C · $2.09 ]  [ 31°C · $1.19 ]  [ Open full market ]
```

Tap → Polymarket opens on that exact bracket → buy with your own wallet.

The two lines nobody else prints are **Market price of block** and **Edge**.
Without them a coverage number is a boast. With them the reader can check
the whole thing before spending a cent.

---

## 8. WIPING THE OLD CHANNEL

The old channel is full of crypto calls and a positioning that no longer
exists. Two ways, and the choice matters.

### Option A , keep the channel, clear the history (recommended)

You keep the subscribers, the @handle and the age of the channel. Age is
credibility you cannot buy.

Telegram has no "delete all". Realistic path:

1. **Desktop app** (mobile can't do this properly)
2. Open the channel → select the oldest message
3. Scroll to the newest with **Shift held** → selects the whole range
4. Right-click → **Delete** → tick **Delete for everyone**
5. Telegram caps bulk deletes around 100 at a time , repeat. It's tedious. It's 20 minutes.
6. Then: new description, new pinned post (sections 2 and 3 above)

**Post an explanation before you start.** Subscribers who watch a channel
empty itself overnight and never hear why, leave.

```
This channel is being rebuilt.

OddsEngine is now one thing only: probability distributions for weather
prediction markets. No crypto, no sports, no calls.

The old posts are coming down because they describe a product that no
longer exists. What replaces them is narrower and, for the first time,
measurable: we publish a probability before the station reports, and we
publish the result afterwards , hit or miss.

Backtested on 300 resolved markets: we said 84.9%, it happened 84.7%.
That is a backtest, not a track record. The live one starts this week,
here, free.

oddsengine.live
```

### Option B , new channel

Clean, instant, and you throw away every subscriber and the channel's age.
Only worth it if the old audience is the wrong audience , crypto degens who
will never care about a 7.5 point edge on Seoul's temperature.

**Be honest with yourself about which one it is.** A weather-market channel
with 400 crypto subscribers who never click is worth less than 40 who do.

### Either way

- Wipe the description before the messages. It's what a visitor reads first.
- Keep the @handle if you can. `t.me/OddsEngine` is on the landing page and in
  every signal footer.
- Update the three `t.me/OddsEngine` placeholders in `index.html`.

---

## 9. WHAT YOU CAN DISTRIBUTE TODAY

See `NUMBERS.md` , the full fact sheet, plain language, honestly labelled.
Send it as-is. Everything in it is measured.

**The one line:**

> Backtest, 300 resolved markets: the engine said 84.9%. It happened 84.7%.
> The market charged 77.2% for the same ranges.

**The three that carry it:**

- 300 markets tested, Dec 2025 - Mar 2026
- Same markets with no engine: −38.1%
- Triple the uncertainty setting: hit rate swings 77%→96%, edge stays +5.8 to +7.9

**What you must never drop:** the words *"this is a backtest, not a track
record."* Not because a lawyer says so , because it's the sentence that makes
every other number believable. Anyone can post returns. Almost nobody posts
the caveat that undercuts them.

**What you cannot distribute yet:** anything about live signals. There are
none. In two weeks there will be ~100, and that number replaces the backtest
on the site and in every conversation.
