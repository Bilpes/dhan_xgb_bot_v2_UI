# bot.py — dhan_xgb_bot_v2
# =============================================================
# v4.5 PATCH 2026-07-19:
#
#   BUG-2 WIRE: NEUTRAL SL STREAK TRACKING
#     import increment_neutral_sl_streak / reset_neutral_sl_streak
#     from signal_engine (added in backtest-fix PR).
#     _manage_positions() now calls:
#       increment_neutral_sl_streak() → after any SL exit on NEUTRAL day
#       reset_neutral_sl_streak()     → after any TP / profit exit
#     run() calls reset_neutral_sl_streak() at session start alongside
#     tm.reset_daily() so the counter is always clean on day-open.
#
#   v4.4 PATCH 2026-07-18:
#
#   GAP-1: SIDEWAYS DAY DETECTION
#     New counter: _neutral_scan_count
#     If Nifty regime stays NEUTRAL and abs(nifty_r5c) < SIDEWAYS_NIFTY_THRESH
#     for SIDEWAYS_CONSECUTIVE_SCANS (3) scans in a row, bot sets
#     self._sideways_day = True and blocks ALL new entries for the rest
#     of the day. Existing positions are still managed (trailing SL, TP, EOD).
#     Resets: _sideways_day cleared at EOD reset and when regime turns
#     BULL or WEAK (those have their own entry logic).
#
#   GAP-2: TP multiplier adaptive (1.8x choppy, 2.5x bull)
#     signal_engine.get_signal() now receives nifty_regime and passes
#     tp_mult=ATR_TP_MULT_BULL (2.5) on BULL days, ATR_TP_MULT (1.8) otherwise.
#     trade_manager.compute_sl_tp() accepts tp_mult parameter.
#     On choppy days: TP target is 1.8x ATR -> more frequent hits.
#     On bull days:   TP target is 2.5x ATR -> ride the full trend move.
#
#   GAP-3: Implemented in signal_engine via tp_mult in signal dict.
#
# v4.1 changes retained (profit-lock, POST_TARGET_BULL_ONLY, WEAK hard-stop).
# v4.0 changes retained (SHORT support).
# =============================================================

import time
import logging
import schedule
from datetime import datetime, time as dtime

import config as cfg

from watchlist import get_watchlist, BLOCKED_SYMBOLS
from signal_engine import (
    SignalEngine,
    get_nifty_regime,
    increment_neutral_sl_streak,   # BUG-2 wire
    reset_neutral_sl_streak,       # BUG-2 wire
)
from trade_manager import TradeManager
from watchlist_manager import WatchlistManager

try:
    from telegram import Bot as TelegramBot
    _TG = bool(getattr(cfg, "TELEGRAM_BOT_TOKEN", ""))
except ImportError:
    _TG = False

import os
os.makedirs("logs",   exist_ok=True)
os.makedirs("models", exist_ok=True)
os.makedirs("data",   exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/bot.log"),
    ],
)
log = logging.getLogger("bot")


class DhanXGBBot:
    def __init__(self):
        self.dhan   = self._connect()
        self.engine = SignalEngine()
        self.tm     = TradeManager(self.dhan)
        self._tg    = TelegramBot(cfg.TELEGRAM_BOT_TOKEN) if _TG else None
        self.regime    = "NEUTRAL"
        self.nifty_r5c = 0.0
        self.nifty_ema50 = 0.0

        # Profit-lock
        self._peak_pnl: float = 0.0
        self._profit_locked: bool = False

        # GAP-1: Sideways day detection
        self._neutral_scan_count: int = 0   # consecutive NEUTRAL+flat scans
        self._sideways_day: bool = False    # True = skip all new entries today

        self.wm = WatchlistManager(
            dhan_client  = self.dhan,
            model        = self.engine.model,
            scaler       = self.engine.scaler,
            feature_cols = self.engine.features,
        )
        self.tm.set_watchlist_manager(self.wm)
        log.info("DhanXGBBot v4.5 — bug2 NEUTRAL SL streak wired + sideways-skip + adaptive TP")

    def _connect(self):
        if cfg.PAPER_TRADE:
            log.info("PAPER_TRADE=True — dhan client is None")
            return None
        from dhanhq import dhanhq
        return dhanhq(cfg.DHAN_CLIENT_ID, cfg.DHAN_ACCESS_TOKEN)

    def notify(self, msg: str):
        log.info(f"[MSG] {msg}")
        if self._tg:
            for cid in [
                getattr(cfg, "TELEGRAM_CHAT_ID",   ""),
                getattr(cfg, "TELEGRAM_CHAT_ID_1", ""),
            ]:
                if cid:
                    try:
                        self._tg.send_message(chat_id=cid, text=msg)
                    except Exception:
                        pass
                    break

    # ----------------------------------------------------------------
    # Fetch helpers
    # ----------------------------------------------------------------
    def fetch(self, symbol: str, n: int = 250):
        import pandas as pd
        if cfg.PAPER_TRADE:
            csv_path = f"data/{symbol}_5min.csv"
            if os.path.exists(csv_path):
                try:
                    df = pd.read_csv(csv_path, parse_dates=["datetime"], index_col="datetime")
                    df.columns = [c.lower() for c in df.columns]
                    df = df.sort_index().tail(n)
                    if len(df) >= 20:
                        return df
                except Exception as e:
                    log.debug(f"CSV read failed {symbol}: {e}")
            if not cfg.DHAN_CLIENT_ID or not cfg.DHAN_ACCESS_TOKEN:
                log.warning(f"{symbol}: no CSV and no DHAN credentials")
                return None
        try:
            dhan_client = self.dhan
            if dhan_client is None:
                from dhanhq import dhanhq
                dhan_client = dhanhq(cfg.DHAN_CLIENT_ID, cfg.DHAN_ACCESS_TOKEN)
            r = dhan_client.intraday_minute_data(
                security_id=symbol,
                exchange_segment="NSE_EQ",
                instrument_type="EQUITY",
            )
            import pandas as pd
            df = pd.DataFrame(r["data"])
            df["datetime"] = pd.to_datetime(df["start_Time"])
            df = df.set_index("datetime").sort_index()
            df = df[["open", "high", "low", "close", "volume"]]
            if cfg.PAPER_TRADE:
                df.to_csv(f"data/{symbol}_5min.csv")
            return df.tail(n)
        except Exception as e:
            log.warning(f"Fetch failed {symbol}: {e}")
            return None

    def fetch_index(self, symbol: str, n: int = 250):
        import pandas as pd
        if cfg.PAPER_TRADE and not cfg.DHAN_CLIENT_ID:
            csv_path = f"data/{symbol}_5min.csv"
            if os.path.exists(csv_path):
                try:
                    df = pd.read_csv(csv_path, parse_dates=["datetime"], index_col="datetime")
                    df.columns = [c.lower() for c in df.columns]
                    return df.sort_index().tail(n)
                except Exception:
                    pass
            return None
        try:
            dhan_client = self.dhan
            if dhan_client is None:
                from dhanhq import dhanhq
                dhan_client = dhanhq(cfg.DHAN_CLIENT_ID, cfg.DHAN_ACCESS_TOKEN)
            r = dhan_client.intraday_minute_data(
                security_id=symbol,
                exchange_segment="NSE_INDICES",
                instrument_type="INDEX",
            )
            df = pd.DataFrame(r["data"])
            df["datetime"] = pd.to_datetime(df["start_Time"])
            df = df.set_index("datetime").sort_index()
            return df[["open", "high", "low", "close", "volume"]].tail(n)
        except Exception as e:
            log.warning(f"Index fetch failed {symbol}: {e}")
            return None

    # ----------------------------------------------------------------
    # Regime + Nifty resistance level
    # ----------------------------------------------------------------
    def update_regime(self):
        """Update Nifty regime, EMA50, and sideways-day counter."""
        df = self.fetch_index("NIFTY50")
        if df is not None:
            self.regime, self.nifty_r5c = get_nifty_regime(df)
            try:
                ema50_series = df["close"].ewm(span=50, adjust=False).mean()
                self.nifty_ema50 = float(ema50_series.iloc[-1])
            except Exception:
                self.nifty_ema50 = 0.0

            # GAP-1: Track consecutive NEUTRAL+flat scans for sideways detection
            sideways_thresh = getattr(cfg, "SIDEWAYS_NIFTY_THRESH", 0.003)
            consec_needed   = getattr(cfg, "SIDEWAYS_CONSECUTIVE_SCANS", 3)

            if self.regime == "NEUTRAL" and abs(self.nifty_r5c) < sideways_thresh:
                self._neutral_scan_count += 1
                log.debug(
                    "[Sideways] NEUTRAL+flat scan #%d (need %d to declare sideways)",
                    self._neutral_scan_count, consec_needed,
                )
            else:
                # Regime changed to BULL or WEAK, or market is moving — reset counter
                if self._neutral_scan_count > 0:
                    log.debug(
                        "[Sideways] Counter reset (regime=%s ret5c=%.4f)",
                        self.regime, self.nifty_r5c,
                    )
                self._neutral_scan_count = 0
                # If market breaks out of sideways, allow entries again
                if self._sideways_day and self.regime in ("BULL", "WEAK"):
                    log.info(
                        "[Sideways] Regime turned %s — clearing sideways-day flag.",
                        self.regime,
                    )
                    self._sideways_day = False

            # Declare sideways day once threshold is hit
            if (not self._sideways_day
                    and self._neutral_scan_count >= consec_needed):
                self._sideways_day = True
                log.warning(
                    "[Sideways] SIDEWAYS DAY declared after %d consecutive "
                    "NEUTRAL+flat scans. No new entries for rest of session.",
                    self._neutral_scan_count,
                )
                self.notify(
                    f"⚠️ SIDEWAYS DAY detected | Nifty flat for {self._neutral_scan_count} "
                    f"scans | No new entries | Existing positions managed normally."
                )
        else:
            log.debug("Nifty fetch failed — keeping regime: %s", self.regime)

    def _nifty_above_resistance(self) -> bool:
        if self.nifty_ema50 <= 0:
            return False
        df = self.fetch_index("NIFTY50", n=5)
        if df is None or len(df) == 0:
            return False
        nifty_close = float(df["close"].iloc[-1])
        mult = getattr(cfg, "NIFTY_RESISTANCE_MULT", 1.002)
        return nifty_close > self.nifty_ema50 * mult

    # ----------------------------------------------------------------
    # GAP-2/3: Determine adaptive TP multiplier for current conditions
    # ----------------------------------------------------------------
    def _get_tp_mult(self) -> float:
        """Return TP multiplier based on market regime.

        BULL + above resistance -> 2.5x ATR (ride the full trend move)
        NEUTRAL / WEAK / SIDEWAYS -> 1.8x ATR (take quicker profit in chop)
        """
        if self.regime == "BULL" and self._nifty_above_resistance():
            return getattr(cfg, "ATR_TP_MULT_BULL", 2.5)
        return getattr(cfg, "ATR_TP_MULT", 1.8)

    # ----------------------------------------------------------------
    # v4.1: Profit-lock check
    # ----------------------------------------------------------------
    def _check_profit_lock(self) -> bool:
        daily_target   = getattr(cfg, "DAILY_TARGET",        500.0)
        pullback_rs    = getattr(cfg, "PROFIT_PULLBACK_RS",   45.0)
        current_pnl    = self.tm.daily_pnl

        if current_pnl >= daily_target:
            if current_pnl > self._peak_pnl:
                self._peak_pnl = current_pnl
                log.info("[ProfitLock] New peak PnL: Rs%.2f", self._peak_pnl)

        if self._peak_pnl >= daily_target:
            drawdown = self._peak_pnl - current_pnl
            if drawdown >= pullback_rs:
                if self.tm.positions:
                    log.warning(
                        "[ProfitLock] PULLBACK TRIGGERED: peak=Rs%.2f current=Rs%.2f "
                        "drawdown=Rs%.2f — force-exiting ALL positions.",
                        self._peak_pnl, current_pnl, drawdown,
                    )
                    self._force_exit_all(reason="PROFIT_LOCK")
                    self._profit_locked = True
                    self.notify(
                        f"🔒 PROFIT LOCK | Peak=Rs{self._peak_pnl:.0f} "
                        f"Current=Rs{current_pnl:.0f} — All positions exited. "
                        f"Locked PnL ≈ Rs{self.tm.daily_pnl:.0f}"
                    )
                    return True
        return False

    def _force_exit_all(self, reason: str = "FORCE"):
        for sym in list(self.tm.positions):
            df = self.fetch(sym, 3)
            ltp = (
                float(df["close"].iloc[-1])
                if df is not None and len(df) > 0
                else self.tm.positions[sym].entry_price
            )
            self.tm.force_exit(sym, ltp, reason)

    # ----------------------------------------------------------------
    # Gate: can we take new entries?
    # ----------------------------------------------------------------
    def _can_enter_new(self) -> bool:
        if self.tm.daily_loss_breached:
            return False

        # Hard stop on WEAK Nifty
        weak_stop = getattr(cfg, "NIFTY_WEAK_HARD_STOP", True)
        if weak_stop and self.regime == "WEAK":
            log.info("[Gate] WEAK regime — no new entries.")
            return False

        # GAP-1: Sideways day = no new entries
        if self._sideways_day:
            log.info("[Gate] SIDEWAYS DAY — no new entries.")
            return False

        daily_target = getattr(cfg, "DAILY_TARGET", 500.0)
        if self.tm.daily_pnl < daily_target:
            return True

        # Past target: only allow on BULL + above resistance
        post_bull_only = getattr(cfg, "POST_TARGET_BULL_ONLY", True)
        if not post_bull_only:
            return False

        if self.regime == "BULL" and self._nifty_above_resistance():
            log.info("[Gate] Target hit but BULL+above_resistance — allowing new entry.")
            return True

        log.info(
            "[Gate] Target Rs%.0f hit, regime=%s — no new entries.",
            daily_target, self.regime,
        )
        return False

    # ----------------------------------------------------------------
    # Main scan
    # ----------------------------------------------------------------
    def scan(self):
        now = datetime.now().time()

        if now >= cfg.AUTO_EXIT_TIME:
            self._force_exit_all(reason="EOD_CUTOFF")
            if not self.tm.positions:
                self.notify(f"EOD done | Daily PnL Rs{self.tm.daily_pnl:.2f}")
            return

        if now < cfg.NO_NEW_TRADE_BEFORE:
            return

        # Always manage open positions first
        self._manage_positions()

        # Check profit-lock
        if self._check_profit_lock():
            return

        # Gate check
        if not self._can_enter_new():
            log.info(
                "Scan — new entries blocked | PnL=Rs%.2f regime=%s sideways=%s",
                self.tm.daily_pnl, self.regime, self._sideways_day,
            )
            return

        # Update regime AFTER gate (avoids expensive fetch when already blocked)
        self.update_regime()

        # Re-check gate after regime update (sideways may have been declared)
        if not self._can_enter_new():
            return

        # GAP-2/3: Compute adaptive TP multiplier for this scan
        tp_mult = self._get_tp_mult()

        all_syms = get_watchlist()
        syms = [s for s in all_syms if s not in BLOCKED_SYMBOLS]

        for sym in syms:
            if sym in self.tm.positions:
                continue

            df = self.fetch(sym)
            if df is None or len(df) < 10:
                continue

            sig = self.engine.get_signal(
                sym, df, self.tm.positions,
                self.regime, self.nifty_r5c,
                daily_pnl=self.tm.daily_pnl,
                tp_mult=tp_mult,           # GAP-2/3: pass adaptive multiplier
            )

            if sig["action"] in ("BUY", "SELL"):
                pos = self.tm.enter(sym, sig)
                if pos:
                    self.notify(
                        f"{sig['action']} {sym} [{sig.get('side','LONG')}] "
                        f"Rs{sig['entry']:.0f} SL={sig['sl']:.0f} "
                        f"TP={sig['target']:.0f} p={sig['prob']:.2f} "
                        f"rr={sig.get('rr_ratio',0):.1f} "
                        f"tp_mult={tp_mult:.1f}x regime={self.regime}"
                    )

        log.info(
            "Scan done | open=%s | PnL=Rs%.2f | peak=Rs%.2f | "
            "regime=%s | sideways=%s | tp_mult=%.1fx",
            list(self.tm.positions), self.tm.daily_pnl,
            self._peak_pnl, self.regime, self._sideways_day, tp_mult,
        )

    # ----------------------------------------------------------------
    # Position management
    # ----------------------------------------------------------------
    def _manage_positions(self):
        for sym in list(self.tm.positions):
            df = self.fetch(sym, 5)
            if df is None:
                continue
            ltp = float(df["close"].iloc[-1])
            self.tm.update_trailing_sl(sym, ltp)
            reason = self.tm.check_exits(sym, df.iloc[-1].to_dict())
            if reason:
                self.notify(
                    f"EXIT {sym} [{reason}] | PnL=Rs{self.tm.daily_pnl:.2f}"
                )
                # BUG-2 WIRE: update NEUTRAL SL streak on every exit
                if self.regime == "NEUTRAL":
                    if reason == "SL":
                        increment_neutral_sl_streak()
                        log.info(
                            "[NeutralStreak] SL exit on NEUTRAL — streak incremented "
                            "(sym=%s)", sym
                        )
                    elif reason in ("TP", "TRAIL", "PROFIT_LOCK"):
                        reset_neutral_sl_streak()
                        log.info(
                            "[NeutralStreak] Profit exit on NEUTRAL — streak reset "
                            "(sym=%s reason=%s)", sym, reason
                        )

    # ----------------------------------------------------------------
    def reload(self):
        self.engine.reload_model()
        self.wm.reload_model(model=self.engine.model, scaler=self.engine.scaler)
        log.info("Model reloaded")

    def run(self):
        # Reset all daily state including NEUTRAL SL streak (BUG-2)
        self.tm.reset_daily()
        reset_neutral_sl_streak()
        log.info("[NeutralStreak] Reset at session open.")

        for h in range(9, 16):
            for m in range(0, 60, 5):
                if h == 9 and m < 15:
                    continue
                if h == 15 and m >= 30:
                    continue
                schedule.every().day.at(f"{h:02d}:{m:02d}").do(self.scan)

        self.wm.run_scheduled()

        self.notify(
            f"DhanXGBBot v4.5 started | PAPER={cfg.PAPER_TRADE} | "
            f"Capital=Rs{cfg.CAPITAL:,.0f} | Slots={cfg.MAX_OPEN_POSITIONS} | "
            f"SlotBudget=Rs{cfg.CAPITAL//cfg.MAX_OPEN_POSITIONS:,.0f} | "
            f"TP=1.8x(chop)/2.5x(bull) | Target=Rs{getattr(cfg,'DAILY_TARGET',500):.0f}"
        )
        log.info("Scheduler running")

        while True:
            schedule.run_pending()
            time.sleep(10)


if __name__ == "__main__":
    DhanXGBBot().run()
