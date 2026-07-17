# bot.py — dhan_xgb_bot_v2
# =============================================================
# v4.1 PATCH 2026-07-17:
#   1. PROFIT-LOCK EXIT: Once daily_pnl ever touches DAILY_TARGET (Rs500),
#      bot tracks a peak_pnl high-water mark. If pnl then DROPS by
#      PROFIT_PULLBACK_RS (Rs45) from that peak, ALL positions are
#      force-exited immediately to lock in at least Rs455+.
#      e.g. pnl peaks at Rs550, drops to Rs505 -> EXIT ALL.
#
#   2. POST-TARGET NEW ENTRIES (conditional):
#      After target is hit, new entries are ALLOWED only when:
#        a) regime == "BULL"  (Nifty in uptrend)
#        b) Nifty close > Nifty EMA50 * NIFTY_RESISTANCE_MULT (0.2% above resistance)
#      This lets the bot ride a genuine bull day while staying protected.
#
#   3. NIFTY WEAK = HARD STOP:
#      If regime == "WEAK" (Nifty below EMA20 and EMA50, sloping down),
#      no new entries at all. Existing positions are still managed normally
#      (trailing SL, TP checks, EOD exit).
#
# All v4.0 changes retained.
# =============================================================

import time
import logging
import schedule
from datetime import datetime, time as dtime

import config as cfg

from watchlist import get_watchlist, BLOCKED_SYMBOLS
from signal_engine import SignalEngine, get_nifty_regime
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
        self.nifty_ema50 = 0.0        # v4.1: tracked for resistance check

        # v4.1: high-water mark for profit-lock logic
        self._peak_pnl: float = 0.0
        self._profit_locked: bool = False   # True once we've force-exited to protect target

        self.wm = WatchlistManager(
            dhan_client  = self.dhan,
            model        = self.engine.model,
            scaler       = self.engine.scaler,
            feature_cols = self.engine.features,
        )
        self.tm.set_watchlist_manager(self.wm)
        log.info("DhanXGBBot v4.1 — profit-lock + Nifty-conditional trades")

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
        """Update Nifty regime AND compute EMA50 for resistance check."""
        df = self.fetch_index("NIFTY50")
        if df is not None:
            self.regime, self.nifty_r5c = get_nifty_regime(df)
            # v4.1: compute Nifty EMA50 for resistance level check
            try:
                ema50_series = df["close"].ewm(span=50, adjust=False).mean()
                self.nifty_ema50 = float(ema50_series.iloc[-1])
            except Exception:
                self.nifty_ema50 = 0.0
        else:
            log.debug("Nifty fetch failed — keeping regime: %s", self.regime)

    def _nifty_above_resistance(self) -> bool:
        """
        Returns True if Nifty is trading ABOVE its EMA50 by at least
        NIFTY_RESISTANCE_MULT (default 0.2%). This is the 'above resistance'
        condition for allowing post-target new trades on a bull day.
        """
        if self.nifty_ema50 <= 0:
            return False
        df = self.fetch_index("NIFTY50", n=5)
        if df is None or len(df) == 0:
            return False
        nifty_close = float(df["close"].iloc[-1])
        mult = getattr(cfg, "NIFTY_RESISTANCE_MULT", 1.002)
        return nifty_close > self.nifty_ema50 * mult

    # ----------------------------------------------------------------
    # v4.1: Profit-lock check — called every scan
    # ----------------------------------------------------------------
    def _check_profit_lock(self) -> bool:
        """
        Manages the profit high-water mark.

        Rules:
          1. Once daily_pnl first hits DAILY_TARGET (Rs500), start tracking peak_pnl.
          2. If peak_pnl – current_pnl >= PROFIT_PULLBACK_RS (Rs45), force-exit ALL.
          3. After force-exit, set _profit_locked=True so we don't re-enter
             unless Nifty is strongly bullish (POST_TARGET_BULL_ONLY check).

        Returns True if a profit-lock exit was triggered this scan.
        """
        daily_target   = getattr(cfg, "DAILY_TARGET",        500.0)
        pullback_rs    = getattr(cfg, "PROFIT_PULLBACK_RS",   45.0)
        current_pnl    = self.tm.daily_pnl

        # Update peak only once target is crossed
        if current_pnl >= daily_target:
            if current_pnl > self._peak_pnl:
                self._peak_pnl = current_pnl
                log.info("[ProfitLock] New peak PnL: Rs%.2f", self._peak_pnl)

        # Check pullback from peak
        if self._peak_pnl >= daily_target:
            drawdown = self._peak_pnl - current_pnl
            if drawdown >= pullback_rs:
                if self.tm.positions:
                    log.warning(
                        "[ProfitLock] PULLBACK TRIGGERED: peak=Rs%.2f current=Rs%.2f "
                        "drawdown=Rs%.2f ≥ Rs%.2f — force-exiting ALL positions.",
                        self._peak_pnl, current_pnl, drawdown, pullback_rs,
                    )
                    self._force_exit_all(reason="PROFIT_LOCK")
                    self._profit_locked = True
                    self.notify(
                        f"\U0001f512 PROFIT LOCK | Peak=Rs{self._peak_pnl:.0f} "
                        f"Current=Rs{current_pnl:.0f} — All positions exited. "
                        f"Locked PnL ≈ Rs{self.tm.daily_pnl:.0f}"
                    )
                    return True
        return False

    def _force_exit_all(self, reason: str = "FORCE"):
        """Fetch latest price for each open position and exit."""
        for sym in list(self.tm.positions):
            df = self.fetch(sym, 3)
            ltp = (
                float(df["close"].iloc[-1])
                if df is not None and len(df) > 0
                else self.tm.positions[sym].entry_price
            )
            self.tm.force_exit(sym, ltp, reason)

    # ----------------------------------------------------------------
    # v4.1: Should we allow NEW entries right now?
    # ----------------------------------------------------------------
    def _can_enter_new(self) -> bool:
        """
        Single gate for all new-entry decisions:

        BLOCK new entries if ANY of:
          - daily_loss_breached
          - NIFTY_WEAK_HARD_STOP=True AND regime == "WEAK"
          - _profit_locked AND NOT (BULL + above_resistance)

        ALLOW new entries if:
          - daily_pnl < DAILY_TARGET (normal operation), OR
          - daily_pnl >= DAILY_TARGET BUT regime==BULL AND nifty above resistance
            (only when POST_TARGET_BULL_ONLY=True)
        """
        if self.tm.daily_loss_breached:
            return False

        # Hard stop on WEAK Nifty
        weak_stop = getattr(cfg, "NIFTY_WEAK_HARD_STOP", True)
        if weak_stop and self.regime == "WEAK":
            log.info("[Gate] WEAK regime — no new entries.")
            return False

        daily_target = getattr(cfg, "DAILY_TARGET", 500.0)
        if self.tm.daily_pnl < daily_target:
            # Normal operating mode — allow entries
            return True

        # Past target: only allow if POST_TARGET_BULL_ONLY allows it
        post_bull_only = getattr(cfg, "POST_TARGET_BULL_ONLY", True)
        if not post_bull_only:
            return False   # conservative: stop all new entries after target

        # Bull-only extension: regime must be BULL + Nifty above resistance
        if self.regime == "BULL" and self._nifty_above_resistance():
            log.info(
                "[Gate] Target hit but BULL+above_resistance — allowing new entry."
            )
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

        # EOD force-exit window
        if now >= cfg.AUTO_EXIT_TIME:
            self._force_exit_all(reason="EOD_CUTOFF")
            if not self.tm.positions:
                self.notify(f"EOD done | Daily PnL Rs{self.tm.daily_pnl:.2f}")
            return

        if now < cfg.NO_NEW_TRADE_BEFORE:
            return

        # Always manage open positions first (trailing SL, TP, profit-lock)
        self._manage_positions()

        # v4.1: Check profit-lock pullback AFTER managing positions
        if self._check_profit_lock():
            return   # positions just exited — skip new entry scan

        # Gate: can we take new entries?
        if not self._can_enter_new():
            log.info(
                "Scan — new entries blocked | PnL=Rs%.2f regime=%s",
                self.tm.daily_pnl, self.regime,
            )
            return

        self.update_regime()

        # Seek new entries
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
            )

            if sig["action"] in ("BUY", "SELL"):
                pos = self.tm.enter(sym, sig)
                if pos:
                    self.notify(
                        f"{sig['action']} {sym} [{sig.get('side','LONG')}] "
                        f"Rs{sig['entry']:.0f} SL={sig['sl']:.0f} "
                        f"TP={sig['target']:.0f} p={sig['prob']:.2f} "
                        f"rr={sig.get('rr_ratio',0):.1f} regime={self.regime}"
                    )

        log.info(
            "Scan done | open=%s | PnL=Rs%.2f | peak=Rs%.2f | regime=%s",
            list(self.tm.positions), self.tm.daily_pnl,
            self._peak_pnl, self.regime,
        )

    # ----------------------------------------------------------------
    # Position management (trailing SL + exit checks)
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

    # ----------------------------------------------------------------
    def reload(self):
        self.engine.reload_model()
        self.wm.reload_model(model=self.engine.model, scaler=self.engine.scaler)
        log.info("Model reloaded")

    def run(self):
        self.tm.reset_daily()

        for h in range(9, 16):
            for m in range(0, 60, 5):
                if h == 9 and m < 15:
                    continue
                if h == 15 and m >= 30:
                    continue
                schedule.every().day.at(f"{h:02d}:{m:02d}").do(self.scan)

        self.wm.run_scheduled()

        self.notify(
            f"DhanXGBBot v4.1 started | PAPER={cfg.PAPER_TRADE} | "
            f"BUY+SELL | Target=Rs{getattr(cfg,'DAILY_TARGET',500):.0f} | "
            f"ProfitLock=Rs{getattr(cfg,'PROFIT_PULLBACK_RS',45):.0f} pullback | "
            f"Start=09:15"
        )
        log.info("Scheduler running")

        while True:
            schedule.run_pending()
            time.sleep(10)


if __name__ == "__main__":
    DhanXGBBot().run()
