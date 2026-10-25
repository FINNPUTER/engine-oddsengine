"""
features.py: der Schaltpult. Seit 2.21.

Jedes Update ist fertig im Code und sammelt im Hintergrund schon Daten.
Sichtbar wird es erst, wenn der Betreiber den Schalter umlegt: auf der
Admin-Seite der App, ohne Deploy. Zurueckschalten geht genauso.

Gespeichert im kv-Speicher (Postgres) unter "features". Ohne Datenbank gelten
die Standardwerte aus den Variablen FEATURE_<NAME>=true|false.
"""
import json
import os
import time

import storage

# name -> (Anzeigename, was es tut, Standard)
LISTE = {
    "torch":   ("Torch launch",
                "Landing and app turn green, '$ODDS is live on Torch' appears, the "
                "contract address shows and every Buy button goes to Torch. Needs "
                "ODDS_MINT on the web service. Before this is on, the address stays "
                "hidden even if ODDS_MINT is set.", False),
    "agent":   ("Your Engine",
                "Personal agent in Telegram: calls that fit each holder's rules, "
                "sized to their stake, own record, weekly report. Linking and the "
                "holder group work either way.", False),
    "station": ("Station Live",
                "Live readings from the resolution stations: high so far, last "
                "reading, which brackets are already out.", False),
    "crowd":   ("Engine vs Crowd",
                "The market's own favourite block next to the engine's pick, "
                "both scored in public.", False),
    "beat":    ("Beat the Engine",
                "Daily game for holders: pick the bracket in three cities before "
                "the engine locks its own pick. 1 point for a hit, 3 when the "
                "engine missed, 2 bonus for all three. Weekly and all time board. "
                "No stakes, no prizes.", False),
    "crypto":  ("Crypto Terminal",
                "terminal.oddsengine.live: Up/Down markets for BTC, ETH, SOL, XRP, "
                "DOGE, BNB and HYPE in 5 minute, 15 minute and 4 hour windows. Engine "
                "odds against the order book, five modules with a public "
                "record. Needs the crypto service running.", False),
    "engine":  ("Engine page: Your Engine tab",
                "engine.oddsengine.live: Your Engine opens (chat with chain, market and web tools, "
                "Telegram text and voice, morning briefing, reminders, price alerts, watched pages, "
                "memory, email and calendar, money alerts for crowd vs engine and options edges; pay per answer "
                "from the balance). Off: teaser. ENGINE_BETA wallets get it anyway.", False),
    "ai":      ("Engine page: AI tab",
                "engine.oddsengine.live: OddsEngine AI tab with playground, API keys, deposits "
                "and model list, paid in $ODDS. Also opens the public API "
                "(/v1/chat/completions). Needs OPENROUTER_API_KEY on the web service. "
                "Off: the tab shows a teaser, keys stop working.", False),
    "agents":  ("AI for agents",
                "API keys with a daily limit and allowed models, keys created by a wallet "
                "signature without a browser (POST /v1/agents/key) and the MCP server at "
                "/mcp for Claude, Cursor and other agents. Needs the ai switch too.", False),
    "x402":    ("x402 payments",
                "Agents pay per request in $ODDS without an account: /v1/chat/completions "
                "without a key answers 402 with the price, the agent signs a permit, the "
                "server settles it on chain. Needs X402_GAS_KEY (gas only wallet) on the web "
                "service and the ai switch.", False),
    "x402usdc": ("x402 in USDC (Base)",
                "Agents pay with standard x402 in USDC on Base, no custom code needed. Our margin "
                "buys $ODDS on Robinhood Chain, half is burned, half goes to revenue, all public on "
                "/v1/x402/buybacks. Needs the x402 and ai switches, X402_GAS_KEY and a little ETH on "
                "Base in the gas wallet.", False),
    "lows":    ("Lowest temperature",
                "Calls for the lowest temperature of the day, same cities and stations as "
                "the highs. Off: the engine still makes and scores these calls in the "
                "background, only you see them (on this page, below the switches). On: calls "
                "and results go to the holder channel and /v1/lows shows them.", False),
    "sentiment": ("Sentiment (exchanges)",
                "Terminal section 'What the exchanges say': funding, open interest, long/short, aggressive "
                "buyers, order book pressure and liquidations from Hyperliquid, OKX, Coinbase, Kraken and "
                "Binance next to the Polymarket crowd, plus /v1/sentiment. Off: recorded in the background, "
                "only you see it. Needs the crypto service.", False),
    "trade":   ("Your Engine: trading by chat",
                "Users say 'buy $50 of <CA>' or 'sell half of $XYZ' and confirm in their own wallet on "
                "engine.oddsengine.live. Non-custodial, routed through Relay on Robinhood Chain, capped by "
                "TRADE_MAX_USD (1000). Together with 'scan': every chain, Solana through Jupiter with Phantom or "
                "Solflare. Off: only ENGINE_BETA wallets can trade.", False),
    "scan":    ("Your Engine: CA report on every chain",
                "Paste any contract, mint or chart link from Solana, Base, Ethereum, BNB Chain, Arbitrum or Robinhood Chain: "
                "verdict, safety (honeypot, tax, mint/freeze, LP lock), supply (top 10, bundles, fresh wallets, clusters, "
                "snipers), dev wallet and earlier launches, smart wallets among the first buyers. Card with buttons in "
                "Telegram (Full report, Chart, Watch it, Refresh), the live desktop page on engine.oddsengine.live and "
                "live watches with alarms on dev sells, LP pulls and top holder dumps. With 'trade' on, trading works on "
                "all these chains including Solana (Phantom, Solflare). Own deep scan (bundles, snipers, fresh wallets, "
                "same-funder clusters) and own wallet scores, no outside service; Solana is best with HELIUS_API_KEY, "
                "Robinhood with BLOCKSCOUT_API_KEY. Off: only Robinhood Chain like before. ENGINE_BETA wallets get it anyway.", False),
    "smart":   ("Smart money: read in the background",
                "Watches the wallets our own scoring rates highest (iw_wallets, score SMART_MIN_SCORE 25 or more, best "
                "SMART_WATCHLIST 150) on Solana and Robinhood Chain. When SMART_MIN_WALLETS (3) of them with different funders "
                "buy the same coin inside SMART_WINDOW_SEC (1800), it becomes a call with the full token report and goes into "
                "the track record. On its own nothing is sent: only you see the calls in the admin. Works even before Your "
                "Engine is live, so the record is there on launch day. Needs HELIUS_API_KEY (Solana) and BLOCKSCOUT_API_KEY "
                "(Robinhood); SMART_DAILY_RPC (60000) caps the reads a day. Run the wallet import first.", False),
    "smart_calls": ("Smart money: send calls",
                "Together with 'smart': users who turn calls on (/smart on in Telegram, the switch on the page, or by asking) get "
                "every call in Telegram with the report and buttons, plus follow ups at 2x, 3x, 5x, 10x and up. Optional "
                "SMART_CHAT_ID posts them to a Telegram channel too. Red verdicts are recorded, never sent. The public track "
                "record is at /v1/engine/smart/record. ENGINE_BETA wallets get it anyway.", False),
    "guard":   ("Your Engine: Wallet Guard",
                "Users who turn it on (chat, the switch on the page) get a Telegram alert within seconds when their wallet approves "
                "a spender we do not know (ERC-20 approve, NFT approve-all, Permit2) or gets a Solana delegate or owner change, "
                "when someone else moves tokens out with an old approval, and when tokens leave without a swap. Every alert has "
                "a Revoke button their own wallet signs. Plus a wallet check of all open approvals (all five EVM chains and Solana). "
                "Live alerts: Robinhood and Arbitrum out of the box, Base, Ethereum and BNB once RPC_BASE, RPC_ETHEREUM or "
                "RPC_BSC point to your own node (public nodes refuse the query). Solana over the Helius webhook.", False),
    "group_scan": ("Your Engine: scans in Telegram groups",
                "Add the bot to any Telegram group or community. When someone posts a contract, a chart link or /scan <CA>, the "
                "bot answers in the same topic with the full report card, free, and fills in the deep scan in the same message "
                "when it is ready. Buttons: Full report, Chart, Ask Your Engine (opens the bot privately). Set BotFather "
                "/setprivacy to Disable (or make the bot admin) so it can see the messages. GROUP_SCAN_PER_HOUR (60) per group.", False),
    "voice":   ("Your Engine: voice replies",
                "A voice note to the bot gets a voice note back (text below it), /voice on|off, and a play button on every "
                "answer in the web chat with the head's ring moving to the voice. Needs ELEVENLABS_API_KEY or OPENAI_API_KEY "
                "on the web service. Costs VOICE_USD_PER_1K (0.12) per 1000 characters from the balance. ENGINE_BETA wallets "
                "get it anyway.", False),
    "lang":    ("Your Engine: language setting",
                "Your Engine saves the user's language (asked at the start, /language, or a setting on the page) and sends "
                "everything in it: answers, briefing, alerts, check-ins, watches and OddsEngine news, translated "
                "automatically. Off: it answers in the language the user writes in, proactive messages stay English.", False),
    "options": ("Above X (options)",
                "Terminal section 'Options market vs Polymarket and Kalshi': Deribit options turned into a "
                "chance for every 'BTC/ETH above X' strike, paper picks with a public record, plus /v1/options. "
                "Off: picks are still made and scored, only you see them.", False),
    "kalshi":  ("Kalshi",
                "Kalshi Edition: the engine on every Kalshi weather market (highs and lows, "
                "20+ US cities, at Kalshi's own stations), calls with the Kalshi fee included, "
                "public in the Kalshi tab of the weather app and in Your Engine. Off: calls are "
                "still made and scored, visible only in admin.", False),
}

_stand = {"ts": 0.0, "werte": None}


def _standard(name):
    roh = os.getenv(f"FEATURE_{name.upper()}")
    if roh is None:
        return LISTE[name][2]
    return roh.strip().lower() in ("1", "true", "yes", "on")


def alle(frisch=False):
    """{name: bool} fuer alle bekannten Schalter. 30 Sekunden Cache."""
    if not frisch and _stand["werte"] is not None and time.time() - _stand["ts"] < 30:
        return dict(_stand["werte"])
    gespeichert = {}
    try:
        roh = storage.kv_get("features")
        gespeichert = json.loads(roh) if roh else {}
    except Exception:
        gespeichert = {}
    werte = {n: bool(gespeichert[n]) if n in gespeichert else _standard(n) for n in LISTE}
    _stand["werte"], _stand["ts"] = werte, time.time()
    return dict(werte)


def an(name):
    return bool(alle().get(name, False))


def setzen(name, wert):
    if name not in LISTE:
        raise KeyError(name)
    jetzt = alle(frisch=True)
    jetzt[name] = bool(wert)
    ok = storage.kv_set("features", json.dumps(jetzt))
    _stand["werte"], _stand["ts"] = jetzt, time.time()
    return ok, jetzt


def beschreibung():
    w = alle()
    return [{"name": n, "title": t, "about": a, "on": w.get(n, False)}
            for n, (t, a, _) in LISTE.items()]
