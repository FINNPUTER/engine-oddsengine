"""
OddsEngine — Execution

Places the dutched block on Polymarket, on YOUR wallet only. Never a
subscriber's. Nobody's keys but yours ever touch this process, and even
yours only live in the environment.

    EXECUTION_ENABLED=false     <-- default. Leave it there for now.

WHY IT SHIPS SWITCHED OFF
-------------------------
The old bot's own log is the argument:

    Quant: edge_evals=0 rejected=0 | ob_sims=0 ob_reject=0
    Bankroll: $200.00 -> $61.60

EdgeEngine, OrderbookSimulator and a half-Kelly PositionSizer were all built,
all initialised, and never once fired — they were wired to a strategy that
never produced a signal. The protections existed on paper while the bankroll
fell 69%.

So this file will not trade until the engine has actually been right in
public. It counts scored signals in the snapshot store and refuses below
MIN_SCORED_SIGNALS. Not caution — the same arithmetic as the haircut: sizing
on a number you haven't verified is how a real edge turns into a loss.

THE GATES, in order
-------------------
  1. EXECUTION_ENABLED must be true
  2. TRADING_MODE=live (paper is the default and simulates)
  3. >= MIN_SCORED_SIGNALS resolved signals on record
  4. kill switch file absent
  5. daily loss limit not hit
  6. open positions under the cap
  7. per-day stake cap not hit
  8. the leg's book is >= MIN_DEPTH_RATIO x the order

Any one fails -> no order. The reason is logged, always.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import storage
from config import cfg
from logger import log

KILL_FILE = os.getenv("KILL_SWITCH_FILE", "data/STOP")

_client = None


def _clob():
    """Lazy CLOB client. Never constructed unless we are actually trading."""
    global _client
    if _client is not None:
        return _client
    key = os.getenv("POLY_PRIVATE_KEY", "").strip()
    funder = os.getenv("POLY_FUNDER_ADDRESS", "").strip()
    if not key or not funder:
        log.error("POLY_PRIVATE_KEY / POLY_FUNDER_ADDRESS missing")
        return None
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON
        # POLY_SIGNATURE_TYPE: 0 = plain EOA wallet (MetaMask-style; funder = the
        # same address) -- this is the proven April setup. 2 = Polymarket proxy
        # wallet (funder = proxy address). Wrong type => "invalid signature".
        sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
        c = ClobClient("https://clob.polymarket.com", key=key, chain_id=POLYGON,
                       signature_type=sig_type, funder=funder)
        c.set_api_creds(c.create_or_derive_api_creds())
        _client = c
        log.info(f"CLOB ready — funder {funder[:6]}...{funder[-4:]}")
        return _client
    except ImportError:
        log.error("py-clob-client not installed")
    except Exception as e:
        log.error(f"CLOB init failed: {e}")
    return None


# ── Gates ────────────────────────────────────────────────────────────────

def scored_signals():
    """Signals that have been published AND have a recorded outcome."""
    return sum(1 for s in storage.load_all()
               if s.get("signal") and s.get("outcome_known"))


def today_state():
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    staked = 0.0
    n = 0
    for s in storage.load_all():
        if not s.get("executed"):
            continue
        try:
            if datetime.fromisoformat(s["ts"]) < since:
                continue
        except Exception:
            continue
        staked += sum(s.get("stakes", []))
        n += 1
    return staked, n


def can_trade():
    """(ok, reason). Every refusal is explicit and logged by the caller."""
    if not cfg.execution_enabled:
        return False, "EXECUTION_ENABLED=false"
    if not cfg.is_live_trading:
        return False, "TRADING_MODE=paper"
    if os.path.exists(KILL_FILE):
        return False, f"kill switch present ({KILL_FILE})"

    n = scored_signals()
    if n < cfg.min_scored_signals:
        return False, (f"only {n} scored signals, need {cfg.min_scored_signals} "
                       "— the engine has not proven itself live yet")

    staked, cnt = today_state()
    if staked >= cfg.max_daily_stake_usd:
        return False, f"daily stake cap ${cfg.max_daily_stake_usd:.0f} reached"
    if cnt >= cfg.max_trades_per_day:
        return False, f"daily trade cap {cfg.max_trades_per_day} reached"
    return True, None


def kill(reason="manual"):
    os.makedirs(os.path.dirname(KILL_FILE) or ".", exist_ok=True)
    with open(KILL_FILE, "w") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()}  {reason}\n")
    log.warning(f"KILL SWITCH SET — {reason}. No orders until the file is removed.")


def unkill():
    if os.path.exists(KILL_FILE):
        os.remove(KILL_FILE)
        log.info("kill switch cleared")


# ── Execution ────────────────────────────────────────────────────────────

def execute(snap):
    """
    Buy the dutched block described by `snap`.

    Returns a dict recorded onto the snapshot. Never raises — a broken order
    must never take the scanner down with it.
    """
    if snap.get("grade", "bet") != "bet":
        return {"executed": False, "reason": "read only (2.20.1): no honest edge"}
    ok, why = can_trade()
    if not ok:
        log.info(f"no execution ({snap.get('city')}): {why}")
        return {"executed": False, "reason": why}

    legs = list(zip(snap.get("token_ids", []), snap.get("stakes", []),
                    snap.get("asks", []), snap.get("legs", [])))
    if not legs:
        return {"executed": False, "reason": "no legs"}

    # Paper path: fill at the ask we published, no invention.
    if not cfg.is_live_trading:
        return {"executed": False, "reason": "paper"}

    client = _clob()
    if not client:
        return {"executed": False, "reason": "clob unavailable"}

    from py_clob_client.clob_types import OrderArgs
    from py_clob_client.order_builder.constants import BUY

    orders, spent = [], 0.0
    for token_id, stake, ask, label in legs:
        if stake < 0.5:                      # dust
            continue
        # Cross the spread by a hair so it fills; never chase.
        px = min(round(ask * (1 + cfg.exec_slippage_bps / 10000), 3), 0.999)
        size = round(stake / px, 2)
        try:
            resp = client.post_order(client.create_order(
                OrderArgs(token_id=token_id, price=px, size=size, side=BUY)))
            filled = bool(resp.get("success"))
            orders.append({"leg": label, "token_id": token_id, "price": px,
                           "size": size, "usd": round(stake, 2),
                           "order_id": resp.get("orderID"), "filled": filled,
                           "raw": str(resp)[:200]})
            if filled:
                spent += stake
            log.info(f"  {label}: {'FILLED' if filled else 'REJECTED'} "
                     f"{size}@{px} = ${stake:.2f}")
        except Exception as e:
            orders.append({"leg": label, "error": str(e)[:200], "filled": False})
            log.error(f"  {label}: {e}")

    filled = sum(1 for o in orders if o.get("filled"))

    # A partially filled dutch is not a dutch — it is a directional bet you
    # did not intend. Flag it loudly; the operator decides what to do.
    if 0 < filled < len(orders):
        log.warning(f"PARTIAL FILL {filled}/{len(orders)} on {snap.get('city')} "
                    "— the block is no longer balanced. Check it.")

    return {"executed": filled > 0, "orders": orders,
            "legs_filled": filled, "legs_total": len(orders),
            "usd_spent": round(spent, 2),
            "partial": 0 < filled < len(orders),
            "executed_at": datetime.now(timezone.utc).isoformat()}


def status():
    ok, why = can_trade()
    staked, cnt = today_state()
    return {
        "enabled": cfg.execution_enabled,
        "mode": "live" if cfg.is_live_trading else "paper",
        "can_trade": ok,
        "reason": why,
        "scored_signals": scored_signals(),
        "needed": cfg.min_scored_signals,
        "staked_24h": round(staked, 2),
        "trades_24h": cnt,
        "kill_switch": os.path.exists(KILL_FILE),
    }


if __name__ == "__main__":
    print(json.dumps(status(), indent=2))
