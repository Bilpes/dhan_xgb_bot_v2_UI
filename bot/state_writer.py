# =============================================================
# bot/state_writer.py
#
# Called from LiveBot after every scan + every monitor tick.
# Writes logs/state.json so the Flask dashboard can read
# live open trades, risk state, and bot status WITHOUT needing
# its own broker connection.
#
# Import and call in live_bot.py:
#   from bot.state_writer import write_state
#   write_state(bot)   # pass the LiveBot instance
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
            ltp = 0.0  # LTP not cached here; dashboard fetches separately
            open_trades.append({
                "symbol":     symbol,
                "secId":      str(t.security_id),
                "side":       t.side,
                "qty":        t.qty,
                "entry":      round(t.entry, 2),
                "sl":         round(t.stop_loss, 2),
                "target":     round(t.target, 2),
                "trailSl":    round(t.stop_loss, 2),
                "trailCount": t.trail_count,
                "prob":       round(t.entry_prob, 4),
                "rr":         round(t.rr, 3),
                "atr":        round(t.atr, 4),
                "openTime":   t.open_time.strftime("%H:%M"),
                "candles":    t.candles_held,
                "sector":     sector,
                "regime":     "paper" if t.mode == "paper" else "live",
                "ltp":        ltp,
            })

        risk = bot.risk
        from config.config import CAPITAL, DAILY_LOSS_LIMIT
        max_daily_loss = CAPITAL * DAILY_LOSS_LIMIT

        state = {
            "timestamp":   datetime.now().isoformat(),
            "mode":        os.getenv("BOT_MODE", "paper"),
            "halted":      risk.circuit_open,
            "broker_ready": True,   # if this file exists, broker IS running
            "paper_pnl":   round(bot.paper_pnl, 2),
            "daily_pnl":   round(risk.daily_pnl, 2),
            "open_trades": open_trades,
            "risk": {
                "daily_pnl":     round(risk.daily_pnl, 2),
                "wins":          risk.win_count,
                "losses":        risk.loss_count,
                "halted":        risk.circuit_open,
                "consecutive_losses": risk._consecutive_loss,
                "capital":       CAPITAL,
                "max_daily_loss": round(max_daily_loss, 0),
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
