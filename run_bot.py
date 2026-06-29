# ============================================================
# run_bot.py  —  Safe entrypoint for LiveBot
#
# Use this instead of `python -m bot.live_bot` because
# live_bot.py's __main__ block was lost in a truncated push.
# This file monkey-patches the missing methods back onto
# LiveBot at runtime, then starts the bot.
#
# Usage:
#   python run_bot.py
#
# Fix log (2026-06-29):
#   FIX-11: live_bot.py was truncated by GitHub API content
#           limit during the FIX-10 push. The run(), eod_reset(),
#           and _force_exit_all() methods + __main__ block were
#           cut off. This file restores them without requiring
#           a full re-push of the 1800-line live_bot.py.
# ============================================================

from __future__ import annotations
import logging
import time
import os

# ── Bootstrap .env before anything else ─────────────────────
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join("config", ".env"))

from bot.live_bot import (
    LiveBot, BOT_MODE, MODE_LABEL,
    WATCHLIST, CAPITAL,
    _is_market_open, _parse_time,
    SCAN_INTERVAL, MONITOR_INTERVAL, EOD_RESET_TIME,
)
from bot.telegram_alert import alert_bot_started

log = logging.getLogger("run_bot")


# ── Patch: run() ─────────────────────────────────────────────
def _run(self):
    """Main paper/live trading loop."""
    log.info("=" * 55)
    log.info("LiveBot starting  MODE=%s", BOT_MODE)
    log.info(MODE_LABEL.get(BOT_MODE, BOT_MODE))
    log.info("Capital: ₹%s | Watchlist: %d symbols", f"{CAPITAL:,}", len(WATCHLIST))
    log.info("=" * 55)

    try:
        alert_bot_started(BOT_MODE, len(WATCHLIST), CAPITAL)
    except Exception as e:
        log.warning("Telegram startup alert failed: %s", e)

    self._refresh_nifty()

    last_monitor = 0.0

    while True:
        try:
            now_ts = time.time()

            if not _is_market_open():
                from bot.live_bot import _now_time
                t = _now_time()
                eod = _parse_time(EOD_RESET_TIME)
                if t >= eod:
                    self.eod_reset()
                time.sleep(30)
                continue

            # ── Scan for new entries ──────────────────────────
            if now_ts - self._last_scan_ts >= SCAN_INTERVAL:
                self._scan_and_enter()
                self._last_scan_ts = now_ts

            # ── Monitor open positions ────────────────────────
            if self.trades and now_ts - last_monitor >= MONITOR_INTERVAL:
                self._sync_with_dhan()
                self._monitor_positions()
                last_monitor = now_ts

            time.sleep(10)

        except KeyboardInterrupt:
            log.info("KeyboardInterrupt — shutting down.")
            if BOT_MODE == "live" and self.trades:
                log.info("Forcing exit of %d open positions...", len(self.trades))
                self._force_exit_all("KEYBOARD_INTERRUPT")
            break
        except Exception as e:
            log.error("Main loop error: %s", e, exc_info=True)
            time.sleep(15)


# ── Patch: eod_reset() ───────────────────────────────────────
def _eod_reset(self):
    """Reset daily counters at market close (15:30)."""
    log.info("EOD reset: clearing daily P&L, blacklist, circuit breaker.")
    self.risk.reset_daily()
    self.sl_blacklist.clear()
    self._circuit_alert_sent = False
    self._last_boundary_key  = None
    self.rejection_stats.clear()
    self.rejection_symbols.clear()
    log.info("EOD reset complete. Ready for next session.")


# ── Patch: _force_exit_all() ─────────────────────────────────
def _force_exit_all(self, reason: str = "FORCE_EXIT"):
    """Emergency market-exit all open positions."""
    log.warning("Force-exiting all %d positions. Reason: %s", len(self.trades), reason)
    for symbol, trade in list(self.trades.items()):
        try:
            ltp = self.broker.get_ltp(str(trade.security_id), symbol)
            exit_price = ltp if ltp > 0 else trade.entry  # FIX-6: use entry not running_high
            self._exit_trade(trade, exit_price, reason)
        except Exception as e:
            log.error("Force exit failed for %s: %s", symbol, e)


# ── Apply patches ─────────────────────────────────────────────
LiveBot.run            = _run
LiveBot.eod_reset      = _eod_reset
LiveBot._force_exit_all = _force_exit_all


# ── Entry point ───────────────────────────────────────────────
if __name__ == "__main__":
    bot = LiveBot()
    if BOT_MODE == "test":
        bot.run_test()
    else:
        bot.run()
