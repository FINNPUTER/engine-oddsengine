"""Config from environment. No secrets in code — ever."""
import os


def _f(k, d): return float(os.getenv(k, d))
def _i(k, d): return int(os.getenv(k, d))
def _b(k, d): return os.getenv(k, str(d)).lower() in ("1", "true", "yes")
def _cities(k, d): return {c.strip().lower() for c in os.getenv(k, d).split(",") if c.strip()}


class Config:
    def __init__(self):
        # ── Signal mode ──
        # observe     = private chat only. Nobody sees it. Shake-down runs.
        # calibration = the PUBLIC channel, labelled as calibration, free.
        #               Publishing is not the problem — charging is. This is
        #               the mode the landing page describes.
        # live        = public channel, no calibration header. Only after
        #               tools/calibration.py reports error under ~3pp.
        self.signal_mode = os.getenv("SIGNAL_MODE", "observe").lower()
        self.mode = os.getenv("RISK_MODE", "balanced").lower()

        # ── Telegram ──
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.tg_private = os.getenv("TELEGRAM_PRIVATE_CHAT_ID", "")
        self.tg_public = os.getenv("TELEGRAM_PUBLIC_CHANNEL_ID", "")     # @OddsEngine, free
        self.tg_paid = os.getenv("TELEGRAM_PAID_CHANNEL_ID", "")         # private, subscribers

        # ── Paywall ──
        # The free channel stays genuinely useful — it is where trust and reach
        # come from. The paid channel gets what actually costs something to
        # give away: TIME. Free sees the signal after the paid channel has had
        # it, by which point the good price is usually gone.
        #
        # Off until the live record backs the backtest. Charging for numbers
        # only ever scored against the past is the thing we're replacing.
        self.paywall_enabled = _b("PAYWALL_ENABLED", True)
        self.free_delay_min = _i("FREE_DELAY_MIN", 90)
        # Seit 2.14: $ODDS-Holder sehen jedes Signal zuerst in der App, der
        # oeffentliche Kanal und X bekommen es PUBLIC_DELAY_MIN spaeter.
        # 0 = alles sofort, wie vor 2.14. Muss auf worker UND web gleich sein.
        self.public_delay_min = _i("PUBLIC_DELAY_MIN", 120)
        # Seit 2.15: free = Holder sehen alles in der App, Kanal und X
        # bekommen nur FREE_PER_DAY Gratis-Signale (und alle Ergebnisse).
        # delay = altes Modell mit PUBLIC_DELAY_MIN, instant = alles sofort.
        self.public_mode = os.getenv("PUBLIC_MODE", "free").strip().lower()
        self.free_per_day = _i("FREE_PER_DAY", 1)
        self.odds_mint = os.getenv("ODDS_MINT", "").strip()
        # Free channel: only the strongest N per day, and only balanced mode.
        self.free_max_per_day = _i("FREE_MAX_PER_DAY", 2)
        # The public record post fires only at each Nth settled signal (10, 20,
        # 30...), never on a thin count. A record of two looks like nothing.
        # Seit 2.31.6 aus: die Bilanz-Posts gingen nach dem Railway-Ausfall bei jedem
        # Scan in den oeffentlichen Chat. Nur noch mit RECORD_POST_EVERY > 0.
        self.record_post_every = _i("RECORD_POST_EVERY", 0)

        # ── The calibration knobs. These are GUESSES until measured. ──
        # MEASURED, not guessed. Fitted on 269 resolved markets (backtest,
        # 2026-07-17): at 1.4 the engine says 84.9% and hits 84.7% — a 0.2pp
        # calibration error. 0.9 was overconfident by ~10pp.
        #
        # Fitted against BLOCK hit rate, which is what we publish, not the
        # per-bucket Brier. Those are different objectives and optimising the
        # wrong one is how 0.9 happened.
        self.dress_sigma_c = _f("DRESS_SIGMA_C", 1.4)

        # THE OPTIMIZER'S CURSE, measured.
        #
        # Across all 300 backtested markets the engine is calibrated to
        # 0.2pp. On the ~39% it actually selects, it claimed 81.3% and hit
        # 72.4% — overconfident by 8.8pp.
        #
        # Not a bug in the distribution. It is what happens when you evaluate
        # ~25 blocks and keep the one with the highest coverage-minus-cost:
        # you systematically keep the ones whose estimate errs high. Averaged
        # over all blocks it cancels. Over the winners it does not — and the
        # winner is what goes in the signal.
        #
        # So we subtract the measured bias before publishing and before
        # gating. The subscriber sees a number that has been right.
        # Re-measure with sim2.py as live data accumulates.
        self.coverage_haircut_pp = _f("COVERAGE_HAIRCUT_PP", 8.8)
        self.sigma_per_day_c = _f("SIGMA_PER_DAY_C", 0.35)
        # Confident tight bets: near resolution the day's max is largely
        # measured, so the dressing shrinks from dress_sigma_c toward sigma_min.
        # That lets a sure 1-2 bucket call clear coverage instead of being
        # forced to spread across 3-5. Only fires when observations were folded
        # in (obs_fold). TIGHT_NEAR_RESOLUTION=false disables it.
        self.sigma_min = _f("SIGMA_MIN_C", 0.6)
        self.sigma_full_lead_h = _f("SIGMA_FULL_LEAD_H", 12.0)
        self.tight_near_resolution = _b("TIGHT_NEAR_RESOLUTION", True)
        # Lead-aware coverage: far from resolution the edge is being AHEAD of the
        # market, not being certain -- so the required coverage may ease from the
        # mode's value (short lead) down to cov_floor (long lead). Never below the
        # floor, or cheap 17%-tail blocks win again (the $46/35-bets lottery).
        # Off by default -- opt in once the closest-calls log shows edges being
        # clipped on coverage at medium/long lead.
        self.lead_aware_coverage = _b("LEAD_AWARE_COVERAGE", False)
        self.cov_full_lead_h = _f("COV_FULL_LEAD_H", 6.0)
        self.cov_floor = _f("COV_FLOOR", 0.55)
        # Front signal: flag precip/cloud at the expected peak (suppressed-max =
        # cheap low bucket). Diagnostic today (logged + stored), foundation for
        # weighting the low buckets later.
        self.front_signal_enabled = _b("FRONT_SIGNAL_ENABLED", True)
        self.front_precip_pct = _f("FRONT_PRECIP_PCT", 40.0)
        # Independent front source: airport TAF (aviationweather.gov), the
        # by-hand tool, not the Open-Meteo ensemble. Diagnostic/logged for now.
        self.taf_enabled = _b("TAF_ENABLED", True)
        # Wunderground's OWN forecast for the resolution station. WU is the
        # settlement source; its forecast predicts its own resolution better
        # than an Open-Meteo proxy. Opt-in. Nudges the ensemble centre toward
        # WU by WU_WEIGHT (0=ignore ... 1=fully trust WU), keeping the spread.
        self.wu_enabled = _b("WU_ENABLED", False)
        self.wu_weight = _f("WU_WEIGHT", 0.5)
        self.wu_cache_min = _i("WU_CACHE_MIN", 180)
        # Safety net: WU's scraped daily "High" can be the wrong number (the
        # feels-like/heat index, or a different day's high from the multi-day
        # strip). If WU disagrees with the 3-model ensemble by more than this,
        # DISCARD it rather than fold a bad value. Qingdao 20.07: WU parsed 32C
        # vs ensemble 28C vs WU's own hourly 29C -> rejected.
        self.wu_max_dev_c = _f("WU_MAX_DEV_C", 4.0)
        # "33C" == [32.5, 33.5). UNVERIFIED against real resolutions.
        self.bucket_rounding = _f("BUCKET_ROUNDING", 0.5)

        # ── No-signal gates ──
        # Depth is checked RELATIVE to the order, not as a flat wall.
        # Lowered for hand-placed $5 stakes: at that size a thin book barely
        # costs slippage, so a shallow book is still fine to fill. Raise these
        # back up before any sizeable or automated trading.
        self.min_depth_ratio = _f("MIN_DEPTH_RATIO", 1.2)
        # Floor, so a book with almost nothing on it never counts as tradeable.
        self.min_ask_depth_usd = _f("MIN_ASK_DEPTH_USD", 0.5)
        self.max_spread_bps = _f("MAX_SPREAD_BPS", 2500)
        # Zentrierter Modus: die drei Buckets um den korrigierten Read kaufen,
        # statt unter ~25 Bloecken den mit dem groessten Vorsprung zu suchen.
        # Ohne Suche gibt es keine Auswahlverzerrung, deshalb entfaellt hier der
        # Haircut, und die Abdeckung wird nur noch berichtet, nicht geprueft.
        self.centered_field = _b("CENTERED_FIELD", False)
        self.centered_width = _i("CENTERED_WIDTH", 3)
        # Meldet die Flughafenvorhersage eine Front zur Spitzenstunde, wird der
        # Block um ein Band nach UNTEN erweitert. Kein geratener Abschlag,
        # sondern Abdeckung in die Richtung, in die das Risiko zeigt. Bewusst
        # an der TAF festgemacht: der Modellregen steckt schon in der Temperatur.
        self.front_widen = _b("FRONT_WIDEN", False)
        # Veroeffentlichungsboden fuer den zentrierten Modus. Der Modus kennt
        # bewusst keine Abdeckungs-Untergrenze mehr fuer die Blockwahl — ohne
        # jeden Boden gehen aber auch 40%-Bloecke raus, bei denen 6 von 10
        # danebengehen. Das hier prueft NICHT die Auswahl, nur ob es gepostet
        # wird.
        self.centered_min_coverage = _f("CENTERED_MIN_COVERAGE", 0.50)
        # Vorsprung ohne das billigste Bein. Default aus (-999 = kein Filter),
        # die Zahl steht aber in jedem Signal und in der Diagnose.
        self.min_robust_edge_pp = _f("MIN_ROBUST_EDGE_PP", -999.0)
        # Cost cap on the 3-field block. The backtest is emphatic: fields under
        # 70c returned +34% (winter) / +12% (summer); 70-100c only +5-6%. So the
        # engine never buys the expensive band. Env-tunable.
        # Seit 2.16 (01.10.2026, Record 597 Buendel): 80-90c verlor in beiden
        # Phasen (fest 80-85c: -16 %, vorher 80-90c: -7 %). Obergrenze 80c.
        self.max_field_cost = _f("MAX_FIELD_COST", 0.80)
        # Seit 2.16: zu grosser Vorsprung heisst meist, die Engine liegt falsch,
        # nicht der Markt. Ab 7pp: fest 13 Buendel 62 % Treffer -28 %,
        # vorher 270 Buendel 46-56 % Treffer -14 bis -20 %. Gemessen, nicht
        # veroeffentlicht. 0 schaltet den Filter ab.
        self.max_edge_pp = _f("MAX_EDGE_PP", 7.0)
        # Seit 2.20.1: ein Call ist nur dann eine Wette, wenn die ehrliche
        # Wahrscheinlichkeit (Preis + k * (Modell - Preis)) den Preis um so
        # viele Punkte schlaegt. Alles darunter geht als "read" raus, ohne
        # Einsatzvorschlag, oder gar nicht (PUBLISH_READS=false).
        self.bet_min_edge_pp = _f("BET_MIN_EDGE_PP", 3.0)
        self.publish_reads = _b("PUBLISH_READS", True)
        self.trust_start = _f("KALIB_TRUST_START", 0.25)
        # Untergrenze fuer den Blockpreis (25.09.2026, 550 Buendel).
        # Unter 70c widerspricht der Markt der Engine, und der Markt hat recht:
        # unter 55c sagte die Engine 68 %, getroffen wurden 31 % (ROI -31 %).
        # 70c und mehr: Engine 79 %, Treffer 78 %, ROI +3 %, in beiden
        # Haelften positiv. Billigere Bloecke werden weiter gemessen und
        # gespeichert, aber nicht veroeffentlicht. 0 schaltet den Filter ab.
        self.min_field_cost = _f("MIN_FIELD_COST", 0.70)
        # Gesperrte Staedte (25.09.2026): in beiden Haelften des Records klar
        # im Minus (seoul 36 Buendel -35 %, helsinki 28 -31 %, manila 27 -26 %).
        # Werden gemessen, nicht veroeffentlicht. Leer = keine Sperre.
        self.blocked_cities = _cities("BLOCKED_CITIES", "seoul,helsinki,manila")
        # Waechter: meldet im privaten Telegram-Chat, wenn der Worker so
        # lange nichts gespeichert hat.
        self.watchdog_hours = _f("WATCHDOG_HOURS", 6.0)
        # X-Entwuerfe: zu jedem Signal und jedem Ergebnis kommt zusaetzlich
        # eine kurze, fertige Fassung fuer X in den privaten Chat. Lange
        # druecken, kopieren, auf X posten. Keine X-API noetig.
        self.x_drafts = _b("X_DRAFTS", True)
        # Ab hier gelten die festen Regeln. Die laufende Bilanz unter jedem
        # Ergebnis zaehlt nur Signale ab diesem Tag.
        self.rules_since = os.getenv("RULES_SINCE", "2026-09-25")
        # Direkt auf X posten. Aus, bis die vier Schluessel gesetzt sind UND
        # X_POST_ENABLED=true. Solange es aus ist oder ein Post scheitert,
        # kommt der Entwurf wie bisher in den privaten Telegram-Chat.
        self.x_post_enabled = _b("X_POST_ENABLED", False)
        self.x_api_key = os.getenv("X_API_KEY", "").strip()
        self.x_api_secret = os.getenv("X_API_SECRET", "").strip()
        self.x_access_token = os.getenv("X_ACCESS_TOKEN", "").strip()
        self.x_access_secret = os.getenv("X_ACCESS_SECRET", "").strip()
        # Kostenbremse: hoechstens so viele Posts pro Tag (je 0,015 $).
        self.x_max_per_day = _i("X_MAX_PER_DAY", 30)
        # Signal whitelist: only these cities produce a tradeable/published
        # signal. Others are still fully evaluated and stored (for measurement),
        # but never fire. Default = the backtest-proven green cities. Set the env
        # var to a comma list to change it, or to empty to disable (all cities).
        self.signal_whitelist = _cities("SIGNAL_CITIES_WHITELIST",
            "jeddah,toronto,beijing,chicago,nyc,austin,paris,madrid,shenzhen,"
            "warsaw,moscow,busan,amsterdam,seattle,sao paulo,dallas,"
            # bias-corrected reds: proven +ROI in the summer biascorrect backtest
            # (seoul +11, shanghai +25, taipei +15, munich +15, guangzhou +17,
            # chongqing +22). Fire in PAPER only until live corrected signals hit.
            "seoul,shanghai,taipei,munich,guangzhou,chongqing")
        # Polymarket rejects orders below $1. A dutched split is proportional to
        # the leg prices, so the cheapest leg is the binding constraint: the
        # smallest total stake where EVERY leg clears the minimum is
        # cost / min(ask) * min_order_usd. Below that the published split is not
        # executable as shown, so the signal has to say so.
        self.min_order_usd = _f("MIN_ORDER_USD", 1.0)
        # Polymarket proxy wallet, read-only. Used to fetch real fills
        # for the P&L view. Never used to sign or place anything.
        self.wallet_address = os.getenv("WALLET_ADDRESS", "")
        self.min_lead_h = _f("MIN_LEAD_H", 1)
        self.max_lead_h = _f("MAX_LEAD_H", 60)
        self.min_members = _i("MIN_MEMBERS", 10)

        # (min_outcomes, max_outcomes, min_edge_pp, min_coverage)
        self.modes = {
            "safe":       (4, 5, _f("SAFE_MIN_EDGE_PP", 4.0), 0.85),
            "balanced":   (1, 5, _f("BAL_MIN_EDGE_PP", 3.0), 0.70),
            "aggressive": (3, 3, _f("AGG_MIN_EDGE_PP", 2.0), 0.50),
        }

        # The split is published as PERCENTAGES so it scales to whoever is
        # reading — $50 or $50,000, same shares. The dollar figures next to
        # them are an illustration at EXAMPLE_BANKROLL x STAKE_PCT, not a
        # recommendation. We do not know anyone's bankroll and will not pretend.
        self.budget = _f("BUDGET_USD", 10.0)          # internal maths unit
        self.example_bankroll = _f("EXAMPLE_BANKROLL", 100.0)
        self.stake_pct = _f("STAKE_PCT", 5.0)
        self.ens_models = os.getenv("ENS_MODELS", "icon_seamless,gfs_seamless,ecmwf_ifs025")
        # Fold the station's already-measured daily max into the ensemble
        # members near resolution. Defensive: if the obs call fails the raw
        # ensemble is kept. OBS_FOLD_ENABLED=false disables it instantly.
        self.obs_fold_enabled = _b("OBS_FOLD_ENABLED", True)

        # ── Runtime ──
        # Weather models rerun every 6-12h. Hourly scanning re-reads identical
        # numbers and burns the API quota. 180 min is already generous — the
        # only thing that genuinely moves between runs is the order book.
        self.scan_interval_min = _i("SCAN_INTERVAL_MIN", 45)
        self.ens_cache_min = _i("ENS_CACHE_MIN", 180)
        self.throttle_s = _f("THROTTLE_S", 0.12)
        # Gamma tag "Highest temperature" — 100/100 hit rate, measured.
        # Never discover by paging /events unfiltered: Gamma orders by volume
        # and caps offset at ~2100, so temperature markets are unreachable.
        self.tag_id = _i("GAMMA_TAG_ID", 104596)
        self.max_discovery_offset = _i("MAX_DISCOVERY_OFFSET", 1000)
        # Per DAY, not per scan. At 8 scans a day the old per-scan cap of 8
        # allowed 64 posts — a channel nobody would stay subscribed to.
        self.max_signals_per_day = _i("MAX_SIGNALS_PER_DAY", 6)
        self.repost_cooldown_h = _f("REPOST_COOLDOWN_H", 24)
        # Ein Markt darf INNERHALB des Cooldowns erneut raus, wenn sich etwas
        # Wesentliches geaendert hat. Sonst steht im Kanal bis zur Aufloesung
        # eine Zahl, die wir selbst nicht mehr glauben — genau das ist bei
        # Dallas passiert, als WU seine Prognose um sechs Grad revidierte.
        self.repost_min_gap_h = _f("REPOST_MIN_GAP_H", 2.0)
        # neuer Favorit: die gekauften Baender haben sich verschoben
        self.repost_on_shift = _b("REPOST_ON_SHIFT", True)
        # oder der Block ist deutlich billiger geworden und jetzt gut bezahlbar
        self.repost_price_drop_c = _f("REPOST_PRICE_DROP_C", 5.0)
        self.repost_max_cost = _f("REPOST_MAX_COST", 0.68)
        # Die Station hat schon gemessen. Tote Signale gar nicht erst posten,
        # und nachladen, wenn der gemessene Wert bereits im Block liegt.
        self.metar_gate = _b("METAR_GATE", True)
        self.metar_load_max_cost = _f("METAR_LOAD_MAX_COST", 0.85)

        # ── Execution (own wallet only) ──
        # Ships off. The old bot's log is the reason: EdgeEngine,
        # OrderbookSimulator and half-Kelly sizing were all built, all
        # initialised, and fired exactly zero times while the bankroll fell
        # 69%. Protections that are never wired are decoration.
        self.execution_enabled = _b("EXECUTION_ENABLED", False)
        self.trading_mode = os.getenv("TRADING_MODE", "paper").lower()
        # Will not trade until the engine has been publicly right this often.
        self.min_scored_signals = _i("MIN_SCORED_SIGNALS", 100)
        self.max_daily_stake_usd = _f("MAX_DAILY_STAKE_USD", 50.0)
        self.max_trades_per_day = _i("MAX_TRADES_PER_DAY", 5)
        self.exec_slippage_bps = _f("EXEC_SLIPPAGE_BPS", 100)

        # ── Storage ──
        self.data_dir = os.getenv("DATA_DIR", "data")
        self.database_url = os.getenv("DATABASE_URL", "")

    @property
    def is_live_trading(self):
        return self.trading_mode == "live"

    @property
    def is_public(self):
        return self.signal_mode in ("calibration", "live")

    @property
    def is_live(self):
        return self.signal_mode == "live"

    def chat_id(self):
        return self.tg_public if self.is_public else self.tg_private

    def paid_chat_id(self):
        return self.tg_paid or None

    def validate(self):
        errs = []
        if self.mode not in self.modes:
            errs.append(f"RISK_MODE must be one of {list(self.modes)}")
        if self.signal_mode not in ("observe", "calibration", "live"):
            errs.append("SIGNAL_MODE must be observe | calibration | live")
        if self.tg_token and not self.chat_id():
            errs.append(f"SIGNAL_MODE={self.signal_mode} but no matching chat id set")
        if self.dress_sigma_c < 0.3:
            errs.append("DRESS_SIGMA_C < 0.3 is not physically plausible")
        if self.execution_enabled and self.is_live_trading:
            if not os.getenv("POLY_PRIVATE_KEY"):
                errs.append("EXECUTION_ENABLED + TRADING_MODE=live needs POLY_PRIVATE_KEY")
            if not os.getenv("POLY_FUNDER_ADDRESS"):
                errs.append("EXECUTION_ENABLED + TRADING_MODE=live needs POLY_FUNDER_ADDRESS")
        return errs


cfg = Config()
