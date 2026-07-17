# bot.py — dhan_xgb_bot_v2
# =============================================================
# v4.0 OVERHAUL 2026-07-17:
#   - SELL (SHORT) signal handling: scan() now routes SELL signals
#     to tm.enter(sym, sig) which handles SHORT internally via
#     Position.side='SHORT'. All exit/P&L logic adjusted.
#   - 9:15 start: schedule loop now starts at 09:15 (was already fixed
#     in ISSUE-12 but NO_NEW_TRADE_BEFORE is now truly 09:15 in config).
#   - daily_pnl passed to engine.get_signal() for daily target gate.
#   - Tier-B stocks now allowed from 9:15 too (not blocked until 10:00)
#     because ORB on high-beta stocks like AARTIIND happens early.
#   - Telegram notify: differentiates BUY vs SELL messages.
#   - Prior patches: ISSUE-9 through ISSUE-21, FIX-15 all retained.
# =============================================================

import time
import logging
import schedule
from datetime import datetime, time as dtime

import config as cfg

from watchlist import get_watchlist, get_tier_a, get_tier_b, BLOCKED_SYMBOLS
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

        self.wm = WatchlistManager(
            dhan_client  = self.dhan,
            model        = self.engine.model,
            scaler       = self.engine.scaler,
            feature_cols = self.engine.features,
        )
        self.tm.set_watchlist_manager(self.wm)
        log.info("DhanXGBBot v4 — BUY+SELL enabled, 9:15 start")

    def _connect(self):
        if cfg.PAPER_TRADE:
            log.info("PAPER_TRADE=True — dhan client is None")
            return None
        from dhanhq import dhanhq
        client = dhanhq(cfg.DHAN_CLIENT_ID, cfg.DHAN_ACCESS_TOKEN)
        log.info("Dhan API connected")
        return client

    def notify(self, msg: str):
        log.info(f"[MSG] {msg}")
        # try both TELEGRAM_CHAT_ID and TELEGRAM_CHAT_ID_1
        chat_ids = [
            getattr(cfg, "TELEGRAM_CHAT_ID",   ""),
            getattr(cfg, "TELEGRAM_CHAT_ID_1", ""),
        ]
        if self._tg:
            for cid in chat_ids:
                if cid:
                    try:
                        self._tg.send_message(chat_id=cid, text=msg)
                    except Exception:
                        pass
                    break  # send to first valid chat only

    def fetch(self, symbol: str, n: int = 250):
        import pandas as pd

        if cfg.PAPER_TRADE:
            csv_path = f"data/{symbol}_5min.csv"
            if os.path.exists(csv_path):
                try:
                    df = pd.read_csv(csv_path, parse_dates=["datetime"], index_col="datetime")
                    df.columns = [c.lower() for c in df.columns]
                    df = df.sort_index().tail(n)
                    if len(df) >= 20:   # v4: reduced min — allow 9:15 ORB with fewer candles
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
            df = pd.DataFrame(r["data"])
            df["datetime"] = pd.to_datetime(df["start_Time"])
            df = df.set_index("datetime").sort_index()
            df = df[["open", "high", "low", "close", "volume"]]
            if cfg.PAPER_TRADE:
                csv_path = f"data/{symbol}_5min.csv"
                df.to_csv(csv_path)
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

    def update_regime(self):
        df = self.fetch_index("NIFTY50")
        if df is not None:
            self.regime, self.nifty_r5c = get_nifty_regime(df)
        else:
            log.debug("Nifty fetch failed — keeping regime: %s", self.regime)

    def reload(self):
        self.engine.reload_model()
        self.wm.reload_model(
            model  = self.engine.model,
            scaler = self.engine.scaler,
        )
        log.info("Model reloaded")

    def scan(self):
        now = datetime.now().time()

        # EOD force-exit
        if now >= cfg.AUTO_EXIT_TIME:
            for sym in list(self.tm.positions):
                df = self.fetch(sym, 3)
                ltp = (
                    df["close"].iloc[-1]
                    if df is not None and len(df) > 0
                    else self.tm.positions[sym].entry_price
                )
                self.tm.force_exit(sym, ltp, "EOD_CUTOFF")
            if not self.tm.positions:
                self.notify(f"EOD done | Daily PnL Rs{self.tm.daily_pnl:.2f}")
            return

        if now < cfg.NO_NEW_TRADE_BEFORE:
            return

        if self.tm.daily_loss_breached:
            log.warning("Daily loss limit hit — scan paused")
            return

        # v4: daily target gate — skip scan if Rs500 already made
        daily_target = getattr(cfg, "DAILY_TARGET", 500.0)
        if self.tm.daily_pnl >= daily_target:
            log.info(f"Daily target Rs{daily_target:.0f} hit — no new entries. PnL=Rs{self.tm.daily_pnl:.2f}")
            # still manage existing positions
            self._manage_positions()
            return

        self.update_regime()

        # v4: ALL stocks tradeable from 9:15 — ORB on high-beta names happens early
        all_syms = get_watchlist()
        syms = [s for s in all_syms if s not in BLOCKED_SYMBOLS]

        for sym in syms:
            if sym in self.tm.positions:
                continue  # handled in _manage_positions below

            df = self.fetch(sym)
            if df is None or len(df) < 10:   # v4: minimum 10 candles (was 50)
                continue

            sig = self.engine.get_signal(
                sym, df, self.tm.positions,
                self.regime, self.nifty_r5c,
                daily_pnl=self.tm.daily_pnl,
            )

            if sig["action"] in ("BUY", "SELL"):
                pos = self.tm.enter(sym, sig)
                if pos:
                    direction = sig["action"]
                    side_str  = sig.get("side", "LONG")
                    self.notify(
                        f"{direction} {sym} [{side_str}] Rs{sig['entry']:.0f} "
                        f"SL={sig['sl']:.0f} TP={sig['target']:.0f} "
                        f"p={sig['prob']:.2f} rr={sig.get('rr_ratio',0):.1f} "
                        f"regime={self.regime}"
                    )

        self._manage_positions()

        log.info(
            f"Scan | open={list(self.tm.positions)} | "
            f"PnL=Rs{self.tm.daily_pnl:.2f} | regime={self.regime}"
        )

    def _manage_positions(self):
        """Update trailing SL and check exits for all open positions."""
        for sym in list(self.tm.positions):
            df = self.fetch(sym, 5)
            if df is None:
                continue
            ltp = df["close"].iloc[-1]
            self.tm.update_trailing_sl(sym, ltp)
            reason = self.tm.check_exits(sym, df.iloc[-1].to_dict())
            if reason:
                self.notify(
                    f"EXIT {sym} [{reason}] | PnL=Rs{self.tm.daily_pnl:.2f}"
                )

    def run(self):
        self.tm.reset_daily()

        # Schedule every 5 min from 09:15 to 15:25
        for h in range(9, 16):
            for m in range(0, 60, 5):
                if h == 9 and m < 15:
                    continue
                if h == 15 and m >= 30:
                    continue
                schedule.every().day.at(f"{h:02d}:{m:02d}").do(self.scan)

        self.wm.run_scheduled()

        self.notify(
            f"DhanXGBBot v4 started | PAPER={cfg.PAPER_TRADE} | "
            f"BUY+SELL enabled | Target=Rs{getattr(cfg,'DAILY_TARGET',500):.0f}/day | "
            f"Start=09:15"
        )
        log.info("Scheduler running")

        while True:
            schedule.run_pending()
            time.sleep(10)


if __name__ == "__main__":
    DhanXGBBot().run()
