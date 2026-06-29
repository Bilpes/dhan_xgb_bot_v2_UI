# =============================================================
# bot/state_writer.py
#
# Called from LiveBot after every scan + every monitor tick.
# Writes logs/state.json so the Flask dashboard can read
# live open trades, risk state, and bot status WITHOUT needing
# its own broker connection.
#
# Fix log:
#   FIX-13 (2026-06-29): LTP is now fetched from broker for
#           each open position before writing state.json.
#           Previously ltp was hardcoded 0.0 with a comment
#           "dashboard fetches separately" — but the dashboard
#           never did. unrealised_pnl is now also computed here
#           so the dashboard can display it without any extra
#           API calls.
# =============================================================

from __future__ import annotations
import json
import logging
import os
from datetime import datetime
from pathlib import Path

log = logging.getLogger("state_writer")
STATE_FILE = Path("logs/state.json")


def write_state(bot) -> None:
    """
    Serialise the LiveBot's current state to logs/state.json.
    Called after every scan and every monitor tick.
    Silently ignores errors so it never crashes the bot.
    """
    try:
        open_trades = []
        for symbol, t in bot.trades.items():
            from config.config import SECTOR_MAP
            sector = SECTOR_MAP.get(symbol, "UNKNOWN")

            # FIX-13: Fetch live LTP from broker for each open position.
            # Use entry as fallback so unrealised_pnl shows 0 rather than
            # a garbage number if the LTP call fails.
            ltp = t.entry
            try:
                fetched = bot.broker.get_ltp(str(t.security_id), symbol)
                if fetched and fetched > 0:
                    ltp = round(float(fetched), 2)
            except Exception as e:
                log.debug("state_writer: LTP fetch failed for %s: %s", symbol, e)

            # Unrealised P&L: gross, before charges
            if t.side == "LONG":
                unrealised_pnl = round((ltp - t.entry) * t.qty, 2)
            else:
                unrealised_pnl = round((t.entry - ltp) * t.qty, 2)

            # Progress toward target as percentage (0–100)
            price_range = t.target - t.stop_loss
            if price_range > 0 and t.side == "LONG":
                progress_pct = round(
                    min(100, max(0, (ltp - t.stop_loss) / price_range * 100)), 1
                )
            else:
                progress_pct = 0.0

            open_trades.append({
                "symbol":        symbol,
                "secId":         str(t.security_id),
                "side":          t.side,
                "qty":           t.qty,
                "entry":         round(t.entry, 2),
                "ltp":           ltp,
                "sl":            round(t.stop_loss, 2),
                "target":        round(t.target, 2),
                "trailSl":       round(t.stop_loss, 2),
                "trailCount":    t.trail_count,
                "prob":          round(t.entry_prob, 4),
                "rr":            round(t.rr, 3),
                "atr":           round(t.atr, 4),
                "openTime":      t.open_time.strftime("%H:%M"),
                "candles":       t.candles_held,
                "sector":        sector,
                "regime":        "paper" if t.mode == "paper" else "live",
                "unrealisedPnl": unrealised_pnl,
                "progressPct":   progress_pct,
                "holdMinutes":   round(
                    (datetime.now() - t.open_time).total_seconds() / 60, 1
                ),
            })

        risk = bot.risk
        from config.config import CAPITAL, DAILY_LOSS_LIMIT
        max_daily_loss = CAPITAL * DAILY_LOSS_LIMIT

        # Total unrealised P&L across all open positions
        total_unrealised = round(sum(t["unrealisedPnl"] for t in open_trades), 2)

        state = {
            "timestamp":      datetime.now().isoformat(),
            "mode":           os.getenv("BOT_MODE", "paper"),
            "halted":         risk.circuit_open,
            "broker_ready":   True,   # if this file exists, broker IS running
            "paper_pnl":      round(bot.paper_pnl, 2),
            "daily_pnl":      round(risk.daily_pnl, 2),
            "total_unrealised": total_unrealised,
            "open_trades":    open_trades,
            "risk": {
                "daily_pnl":          round(risk.daily_pnl, 2),
                "wins":               risk.win_count,
                "losses":             risk.loss_count,
                "win_rate":           round(
                    risk.win_count / (risk.win_count + risk.loss_count) * 100, 1
                ) if (risk.win_count + risk.loss_count) > 0 else 0.0,
                "halted":             risk.circuit_open,
                "consecutive_losses": risk._consecutive_loss,
                "capital":            CAPITAL,
                "max_daily_loss":     round(max_daily_loss, 0),
                "total_unrealised":   total_unrealised,
            },
            "rejection_stats": dict(bot.rejection_stats),
        }

        os.makedirs("logs", exist_ok=True)
        tmp = str(STATE_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, str(STATE_FILE))  # atomic write

    except Exception as e:
        log.warning("state_writer: failed to write state.json: %s", e)
