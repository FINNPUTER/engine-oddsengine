# Deploy

Two repos, two places. Fifteen minutes, no terminal needed after the first step.

---

## 1. `oddsengine-platform` → Railway (private repo)

### Do you need a new Railway project?

**Yes.** Do not reuse the old one. The old project is wired to the crypto bot
(`polydegen-bot` / possibly the one named `serene-friendship`) — same variables,
same volume, same state. Reusing it means crypto config leaking into a weather
worker, and it is the one thing you said you don't want.

Leave the old project alone. Do not delete it.

### Steps

1. **GitHub** → new **private** repo `oddsengine-platform`
2. Drag every file from `oddsengine-platform/` into it (keep the `tools/` folder)
3. **Railway** → New Project → **Deploy from GitHub repo** → pick it
4. Railway reads `railway.toml` and runs it as a **worker**. No port, no domain.
5. **Variables** tab → paste from `.env.example`, filled in. Minimum:

   ```
   SIGNAL_MODE=observe
   RISK_MODE=balanced
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_PRIVATE_CHAT_ID=...
   ```

6. **Storage — pick one, this is not optional:**
   - **Postgres** (recommended): `+ New` → Database → Add PostgreSQL. Railway
     injects `DATABASE_URL` automatically. `storage.py` creates the table on boot.
   - **Volume**: Settings → Volumes → mount at `/app/data`, then set
     `DATA_DIR=/app/data`.

   Skip this and every redeploy wipes your track record.

7. **Deploy.** Logs should show:
   ```
   OddsEngine 2.0
   signal_mode=observe  risk_mode=balanced
   OBSERVE MODE — measuring, not publishing.
   discovered N active temperature events
   scan done: N markets | 2 signals | 28 no-signal
   ```

### Telegram setup

- Bot token: **@BotFather** → `/newbot`
- Private chat id: message **@userinfobot**
- Public channel id: add the bot to the channel as admin, post once, then open
  `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `chat.id`
  (channels are negative, like `-1001234567890`)

---

## 2. `oddsengine-landing` → GitHub Pages

1. **GitHub** → new **public** repo `oddsengine-landing`
2. Drag `index.html` in
3. Settings → Pages → Source: `main` / root → Save
4. Custom domain: `oddsengine.live` → add a `CNAME` file containing
   `oddsengine.live`, and point the DNS at GitHub Pages
5. Fill in the Telegram invite link — search `href="#"` in `index.html`,
   there are three of them

**Before you publish it:** drop your real logo in and check the colours against
your brand. The palette here is a night synoptic chart — slate ground, and the
temperature ramp used *only* where it means temperature. If your brand green
belongs somewhere, put it in `--accent` and nowhere else.

---

## Order of operations

1. `python tools/check_resolution_rules.py` → **verify the rounding convention.**
   Everything below is worthless if this is wrong.
2. `python main.py --dry` → sanity-check the signal count. More than ~40% of
   markets signalling means a bug.
3. Deploy the worker in `observe`. Let it run.
4. Landing page live, channel open, free, honest.
5. After ~300 resolved city-days: `python tools/calibration.py`
6. Error under 3pp → flip `SIGNAL_MODE=live`, fill in the reliability chart,
   turn on pricing.
7. Error over 8pp → raise `DRESS_SIGMA_C`, throw away the log, start again.

Step 6 might not happen. That is the honest version of this business.
