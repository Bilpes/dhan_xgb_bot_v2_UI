# ============================================================
# bot/live_bot.py — PhD-grade NSE intraday trading loop
#
# Research basis:
#   - Chordia et al. (2000): intraday liquidity patterns on NSE
#   - Anand & Chakravarty (2007): informed trading & order flow
#   - Berkman et al. (2012): opening/closing auction effects
#   - Kumar & Lee (2006): retail sentiment and reversal risk
#   - NSE circular: CNC margin framework, SEBI LODR compliance
#
# Modes:
#   BOT_MODE=test   → one scan cycle, print signals, exit
#   BOT_MODE=paper  → full loop, simulated fills, P&L tracked
#   BOT_MODE=live   → real bracket orders via Dhan API
#
# Fix log (2026-05-14):
#   FIX-2: market_regime now uses entry_prob (immutable), not last_prob
#   FIX-3: momentum exit min candles = 14 (70 min) for CNC mode
#   FIX-4: circuit breaker Telegram alert fires only once per halt
#   FIX-5: _should_rotate() uses fresh engine.score() for last_prob
#   FIX-6: _force_exit_all() fallback uses entry price, not running_high
#   FIX-7: _is_candle_boundary() de-duplicated per candle via key guard
#          KeyboardInterrupt now calls _force_exit_all() in live mode
#
# Fix log (2026-05-25):
#   BUG-A: df.empty guard moved BEFORE EMA calculation in _scan_and_enter
#   BUG-B: eod_reset used self.circuitalertsent instead of self._circuit_alert_sent
#   BUG-C: eod_reset used self.last_boundary_key instead of self._last_boundary_key
#   BUG-D: momentum failure exit used hardcoded 14 instead of
#          self._MOMENTUM_EXIT_MIN_CANDLES
#
# Fix log (2026-06-28):
#   FIX-8: All P&L paths now use calculate_charges() from bot.brokerage.
#   FIX-9: Signal-log header race fixed.
#
# Fix log (2026-06-29):
#   FIX-10: write_state(self) added at end of _scan_and_enter() and
#           _monitor_positions().
#
# PROD-READY (2026-07-10):
#   PROD-1: EV gate wired — ev_gate.compute_net_ev() + ev_passes() called
#           after qty sizing in _scan_and_enter(). Trades with expected
#           net P&L ≤ ₹50 (after charges) are rejected before entry.
#   PROD-2: Live interlock wired — live_guard.assert_live_allowed() called
#           inside _enter_live() before every bracket order placement.
#           Raises RuntimeError if all 3 factors not satisfied.
#   PROD-3: Watchlist guard wired — validate_watchlist() called in
#           __init__. Raises WatchlistValidationError on bad symbols.
#   PROD-4: Startup reconciliation wired — reconcile_on_startup() called
#           at the start of run() (live mode only). Detects ghost positions
#           and removes stale bot.trades entries.
#   PROD-5: Trade audit wired — TradeAudit logs every ENTER, EXIT,
#           SL_MOVE, CIRCUIT_BREAKER, REJECT, SESSION event to
#           logs/trade_audit.ndjson.
# ============================================================

from __future__ import annotations

import csv
import logging
import os
import time
from datetime import datetime, time as dtime
from typing import Optional
from pathlib import Path
from dotenv import load_dotenv
from collections import defaultdict
from bot.trade_policy import MOMENTUM_EXIT_CANDLES
load_dotenv(dotenv_path=os.path.join("config", ".env"))

# ── Mode validation (fail-fast before any imports) ───────────
BOT_MODE = os.getenv("BOT_MODE", "paper").lower().strip()
if BOT_MODE not in ("test", "paper", "live"):
    raise SystemExit(
        f"[ERROR] BOT_MODE='{BOT_MODE}' invalid. Use: test | paper | live"
    )

# ── Project imports ──────────────────────────────────────────
from bot.brokerage import calculate_charges, ChargeBreakdown
from bot.dhan_api import DhanBroker
from bot.signal_engine import SignalEngine
from bot.risk_manager import RiskManager
from bot.state_writer import write_state          # FIX-10: dashboard live data
from bot.telegram_alert import (
    alert_bot_started, alert_entry, alert_exit,
    alert_trail_update, alert_daily_summary,
    alert_circuit_breaker, _send,
)
from bot.trade_policy import BLOCKED_SYMBOLS
# ── PROD-READY imports ───────────────────────────────────────
from bot.ev_gate import compute_net_ev, ev_passes           # PROD-1
from bot.live_guard import assert_live_allowed              # PROD-2
from bot.watchlist_guard import validate_watchlist          # PROD-3
from bot.startup_reconcile import reconcile_on_startup      # PROD-4
from bot.trade_audit import TradeAudit                      # PROD-5
from config.config import (
    WATCHLIST, SECTOR_MAP, TRADE_MODE, MAX_OPEN_TRADES,
    MARKET_OPEN, MARKET_CLOSE, INTRADAY_CUTOFF,
    NO_NEW_TRADE_AFTER, NO_NEW_TRADE_BEFORE,
    TRADE_LOG, LOG_FILE, CAPITAL,
)

# ── Runtime parameters (env-configurable, no restart needed) ─
MAX_PER_SECTOR          = int(os.getenv("MAX_PER_SECTOR",           "2"))
NEW_TRADE_LOSS_PAUSE    = float(os.getenv("NEW_TRADE_LOSS_PAUSE",   "-0.03"))
NIFTY50_SECURITY_ID     = os.getenv("NIFTY50_SECURITY_ID",          "13")
MONITOR_INTERVAL        = int(os.getenv("MONITOR_INTERVAL",         "60"))
SCAN_INTERVAL           = int(os.getenv("SCAN_INTERVAL",            "30"))
AUTO_EXIT_TIME          = os.getenv("AUTO_EXIT_TIME",               "15:15")
AUTO_EXIT_THRESHOLD     = float(os.getenv("AUTO_EXIT_THRESHOLD",    "-0.01"))
EOD_RESET_TIME          = os.getenv("EOD_RESET_TIME",               "15:30")

# Execution quality gates
EXEC_MAX_SPREAD_PCT     = float(os.getenv("EXEC_MAX_SPREAD_PCT",    "0.0005"))
EXEC_MAX_DRIFT_PCT      = float(os.getenv("EXEC_MAX_DRIFT_PCT",     "0.0010"))
LIQUIDITY_MIN_VALUE     = float(os.getenv("LIQUIDITY_MIN_VALUE",    "5000000"))
LIQUIDITY_LOOKBACK_BARS = int(os.getenv("LIQUIDITY_LOOKBACK_BARS",  "3"))
ENTRY_REPRICE_SECS      = int(os.getenv("ENTRY_REPRICE_SECS",       "10"))

# Rotation parameters
ROTATION_MIN_PROFIT     = float(os.getenv("ROTATION_MIN_PROFIT",    "0.005"))
ROTATION_MIN_EDGE       = float(os.getenv("ROTATION_MIN_EDGE",      "0.05"))

# ── NSE session schedule ─────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level    = logging.INFO,
    format   = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers = [
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("live_bot")
TRADE_ANALYSIS_LOG = Path("logs/trade_analysis.csv")
MODE_LABEL = {
    "test":  "TEST   — one scan cycle, no orders, exits after",
    "paper": "PAPER  — full loop, no real orders, simulated P&L",
    "live":  "LIVE   — real bracket orders on Dhan, real money",
}

# ── Candle period (minutes) — must match features.py / retrain ─
CANDLE_MINUTES = 5

# ── CSV columns for TRADE_LOG ─────────────────────────────────
_TRADE_LOG_FIELDS = [
    "timestamp", "symbol", "side", "qty",
    "entry", "exit_price", "stop_loss", "target",
    "rr", "atr", "hold_minutes", "candles_held",
    "exit_reason", "mode", "sector",
    "prob_up", "market_regime", "trail_count",
    "gross_pnl", "brokerage", "etc", "stt", "gst",
    "total_charges", "breakeven_sell", "net_pnl",
]

# ── CSV columns for TRADE_ANALYSIS_LOG ───────────────────────
_ANALYSIS_LOG_FIELDS = [
    "timestamp", "symbol", "entry", "exit_price",
    "qty", "hold_minutes", "exit_reason",
    "prob_up", "rr", "atr",
    "gross_pnl", "total_charges", "net_pnl",
    "market_regime",
]


def _expected_pnl(
    prob_up: float,
    entry:   float,
    sl:      float,
    target:  float,
    qty:     int,
) -> float:
    """
    Heuristic expected PnL for one trade in rupees.
    E = prob_up × reward - (1 - prob_up) × risk
    NOTE: Uses gross values. For after-charge EV use ev_gate.compute_net_ev().
    """
    reward = (target - entry) * qty
    risk   = (entry - sl)    * qty
    return prob_up * reward - (1.0 - prob_up) * risk


# ─────────────────────────────────────────────────────────────
#  Trade dataclass
# ─────────────────────────────────────────────────────────────
class Trade:
    """
    Immutable identity fields set at entry.
    Mutable fields updated during position monitoring.

    entry_prob: Immutable. Model probability at entry time.
    last_prob:  Mutable. Updated each candle boundary for rotation.
    """
    __slots__ = (
        "symbol", "security_id", "side", "qty",
        "entry", "stop_loss", "target", "order_id", "mode",
        "running_high", "open_time",
        "entry_prob",
        "last_prob", "rr", "atr",
        "candles_held", "trail_count",
    )

    def __init__(
        self,
        symbol:      str,
        security_id,
        side:        str,
        qty:         int,
        entry:       float,
        stop_loss:   float,
        target:      float,
        order_id:    str,
        mode:        str,
        last_prob:   float = 0.5,
        rr:          float = 0.0,
        atr:         float = 0.0,
    ):
        self.symbol       = symbol
        self.security_id  = security_id
        self.side         = side
        self.qty          = qty
        self.entry        = entry
        self.stop_loss    = stop_loss
        self.target       = target
        self.order_id     = order_id
        self.mode         = mode
        self.running_high = entry
        self.open_time    = datetime.now()
        self.entry_prob   = last_prob
        self.last_prob    = last_prob
        self.rr           = rr
        self.atr          = atr
        self.candles_held = 0
        self.trail_count  = 0

    def unrealised_pnl(self, ltp: float) -> float:
        if self.side == "LONG":
            return (ltp - self.entry) * self.qty
        return (self.entry - ltp) * self.qty

    def hold_minutes(self) -> float:
        return (datetime.now() - self.open_time).total_seconds() / 60.0

    def __repr__(self) -> str:
        return (
            f"Trade({self.symbol} {self.side} qty={self.qty} "
            f"entry={self.entry:.2f} SL={self.stop_loss:.2f} "
            f"TP={self.target:.2f} held={self.hold_minutes():.0f}m)"
        )


# ─────────────────────────────────────────────────────────────
#  Utility functions
# ─────────────────────────────────────────────────────────────
def _log_trade_csv(row: dict):
    """Append one trade row to TRADE_LOG CSV. Header written if file absent."""
    exists = os.path.exists(TRADE_LOG)
    with open(TRADE_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_TRADE_LOG_FIELDS, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)


def _log_trade_analysis(row: dict):
    """Append one row to trade_analysis.csv (calibration feed)."""
    exists = TRADE_ANALYSIS_LOG.exists()
    with open(TRADE_ANALYSIS_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_ANALYSIS_LOG_FIELDS, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)


def _now_time() -> dtime:
    return datetime.now().time()


def _parse_time(s) -> dtime:
    """Accept either a string 'HH:MM' or an already-parsed datetime.time."""
    if isinstance(s, dtime):
        return s
    h, m = map(int, s.split(":"))
    return dtime(h, m)


def _is_market_open() -> bool:
    t = _now_time()
    return _parse_time(MARKET_OPEN) <= t <= _parse_time(MARKET_CLOSE)


def _is_cutoff_passed() -> bool:
    return _now_time() >= _parse_time(INTRADAY_CUTOFF)


def _no_new_trades() -> bool:
    return _now_time() >= _parse_time(NO_NEW_TRADE_AFTER)


def _is_auto_exit_time() -> bool:
    return _now_time() >= _parse_time(AUTO_EXIT_TIME)


# ─────────────────────────────────────────────────────────────
#  LiveBot
# ─────────────────────────────────────────────────────────────
class LiveBot:
    """
    Main trading loop. Responsibilities:
      - Nifty regime context refresh
      - Stock scan + confidence ranking
      - Execution quality gates (spread, drift, liquidity)
      - EV gate: cost-aware expected-value filter (PROD-1)
      - Entry: bracket order (live) or paper simulation
      - Live trading interlock (PROD-2)
      - Monitor: trailing SL, target, signal-flip, candle-low SL
      - Exit: market order (live) or paper simulation
      - Daily P&L, circuit breaker, EOD reset
      - Portfolio rotation: exit weakest, enter strongest signal
      - Trade audit: every lifecycle event logged to NDJSON (PROD-5)
    """

    _ROTATION_ROUNDTRIP_COST_PCT = 0.0020
    _ROTATION_MIN_CANDLES = 6
    _MOMENTUM_EXIT_MIN_CANDLES = MOMENTUM_EXIT_CANDLES

    def __init__(self):
        self.broker  = DhanBroker()
        self.engine  = SignalEngine()
        self.risk    = RiskManager()
        self.trades: dict[str, Trade] = {}
        self.closed_trades: list[dict] = []
        self.paper_pnl        = 0.0
        self.sl_blacklist:    set[str] = set()
        self._last_scan_ts    = 0.0
        self._entry_quotes:   dict[str, dict] = {}
        self._nifty_refreshed = False
        self.cooldown_bars:   dict[str, int] = {}
        self._circuit_alert_sent = False
        self.rejection_stats:   dict[str, int]        = defaultdict(int)
        self.rejection_symbols: dict[str, list[str]]  = defaultdict(list)
        self._last_boundary_key: Optional[tuple] = None

        # PROD-5: Trade audit — one instance for the whole session
        self.audit = TradeAudit()

        # PROD-3: Validate watchlist on startup — raises if any symbol
        # is in BLOCKED_SYMBOLS, missing a security_id, or a duplicate.
        try:
            validate_watchlist(WATCHLIST, SECTOR_MAP)
        except Exception as wl_err:
            log.critical("[WatchlistGuard] Startup blocked: %s", wl_err)
            raise

    # ──────────────────────────────────────────────────────────
    #  FIX-7: Candle boundary check
    # ──────────────────────────────────────────────────────────
    def _is_candle_boundary(self) -> bool:
        now = datetime.now()
        if now.minute % CANDLE_MINUTES != 0:
            return False
        key = (now.date(), now.hour, now.minute)
        if key == self._last_boundary_key:
            return False
        self._last_boundary_key = key
        return True

    # ──────────────────────────────────────────────────────────
    #  Nifty regime context
    # ──────────────────────────────────────────────────────────
    def _refresh_nifty(self):
        try:
            nifty_df = self.broker.get_candles(
                security_id = NIFTY50_SECURITY_ID,
                symbol      = "NIFTY50",
                days_back   = 5,
            )
            if not nifty_df.empty:
                self.engine.update_nifty(nifty_df)
                self._nifty_refreshed = True
                log.info("Nifty refreshed: %d candles, last=%s",
                         len(nifty_df), nifty_df.index[-1])
            else:
                log.warning("Nifty fetch empty — using previous values.")
        except Exception as e:
            log.warning("Nifty refresh failed: %s — engine uses neutral values.", e)

    # ──────────────────────────────────────────────────────────
    #  Execution quality gate
    # ──────────────────────────────────────────────────────────
    def _recent_liquidity_value(self, df) -> float:
        if df is None or df.empty:
            return 0.0
        tail = df.tail(LIQUIDITY_LOOKBACK_BARS)
        return float((tail["close"] * tail["volume"]).sum()) if not tail.empty else 0.0

    def _passes_entry_filters(
        self,
        symbol:        str,
        sec_id,
        entry_price:   float,
        reason_prefix: str = "",
    ) -> tuple[bool, dict]:
        is_entry_stage = "[ENTRY]" in reason_prefix or "[EXEC]" in reason_prefix
        try:
            ltp = self.broker.get_ltp(str(sec_id), symbol)
            if ltp <= 0:
                log.info("%s%s: LTP unavailable — skip.", reason_prefix, symbol)
                return False, {}

            bid = ask = spread_pct = spread_abs = None
            quote: dict = {}

            if hasattr(self.broker, "get_quote"):
                try:
                    raw = self.broker.get_quote(str(sec_id), symbol) or {}
                    meta = raw.get("meta", {})
                    bid, ask   = raw.get("bid"), raw.get("ask")
                    spread_abs = raw.get("spread_abs")
                    spread_pct = raw.get("spread_pct")

                    if meta:
                        log.info(
                            "%s%s: quote dhan_ok=%s partial=%s fallback=%s attempts=%s",
                            reason_prefix, symbol,
                            meta.get("dhan_success"),
                            meta.get("partial_success"),
                            meta.get("fallback_used"),
                            meta.get("attempts"),
                        )

                    dhan_ok = meta.get("dhan_success", True) if meta else True
                    if not dhan_ok:
                        if is_entry_stage:
                            log.info("%s%s: quote unavailable — skip entry.", reason_prefix, symbol)
                            return False, raw
                        raw = {}

                    if bid is not None and ask is not None and spread_pct is None:
                        spread_abs = float(ask) - float(bid)
                        spread_pct = spread_abs / ltp if ltp > 0 else 1.0

                    if is_entry_stage and spread_pct is not None:
                        if spread_pct > EXEC_MAX_SPREAD_PCT:
                            log.info(
                                "%s%s: spread %.5f%% > max %.5f%% — skip.",
                                reason_prefix, symbol,
                                spread_pct * 100, EXEC_MAX_SPREAD_PCT * 100,
                            )
                            return False, raw

                    quote = raw
                except Exception as e:
                    log.warning("%s%s: quote error: %s", reason_prefix, symbol, e)
                    if is_entry_stage:
                        return False, {}

            df = self.broker.get_candles(sec_id, symbol, days_back=1)
            if df.empty:
                log.info("%s%s: candles unavailable — skip.", reason_prefix, symbol)
                return False, quote

            liquidity = self._recent_liquidity_value(df)
            if liquidity < LIQUIDITY_MIN_VALUE:
                log.info(
                    "%s%s: liquidity ₹%.0f < min ₹%.0f — skip.",
                    reason_prefix, symbol, liquidity, LIQUIDITY_MIN_VALUE,
                )
                return False, quote

            drift = abs(ltp - entry_price) / entry_price if entry_price > 0 else 1.0
            if drift > EXEC_MAX_DRIFT_PCT:
                log.info(
                    "%s%s: drift %.4f%% > max %.4f%% (signal=%.2f ltp=%.2f) — skip.",
                    reason_prefix, symbol,
                    drift * 100, EXEC_MAX_DRIFT_PCT * 100,
                    entry_price, ltp,
                )
                return False, quote

            return True, {
                "ltp":        ltp,
                "bid":        bid,
                "ask":        ask,
                "spread_abs": spread_abs,
                "spread_pct": spread_pct,
                "meta":       quote.get("meta", {}),
            }

        except Exception as e:
            log.warning("%s%s: filter error: %s", reason_prefix, symbol, e)
            return False, {}

    # ──────────────────────────────────────────────────────────
    #  TEST MODE
    # ──────────────────────────────────────────────────────────
    def run_test(self):
        log.info("=" * 55)
        log.info("TEST MODE — scanning %d stocks, no orders", len(WATCHLIST))
        log.info("=" * 55)

        _send(
            f"🤖 <b>Bot TEST MODE</b>\n"
            f"Scanning {len(WATCHLIST)} stocks...\n"
            f"No orders placed.\n"
            f"Capital: ₹{CAPITAL:,} | Mode: {TRADE_MODE.upper()}"
        )

        self._refresh_nifty()
        results: list[str] = []

        for symbol, sec_id in WATCHLIST.items():
            try:
                if symbol.upper() in BLOCKED_SYMBOLS:
                    results.append(f"  {symbol:<14} BLOCKED by policy")
                    continue

                df = self.broker.get_candles(sec_id, symbol, days_back=10)
                if df.empty:
                    results.append(f"  {symbol:<14} no data")
                    continue

                r      = self.engine.score(df, symbol=symbol)
                signal = r["signal"]
                prob   = r["prob_up"]
                entry  = r["entry"]
                sl     = r["sl"]
                target = r["target"]
                rr     = r["rr"]

                if entry <= 0 or sl <= 0 or target <= 0 or rr <= 0:
                    results.append(f"  {symbol:<14} invalid signal")
                    continue

                qty      = self.risk.position_size(entry, sl)
                sector   = SECTOR_MAP.get(symbol, "?")
                risk_amt = (entry - sl) * qty

                if qty > 0:
                    be     = calculate_charges(entry, entry, qty).breakeven_sell
                    net_ev = compute_net_ev(prob, entry, sl, target, qty)
                    be_gap = f"  BE=₹{be:.2f}  EV=₹{net_ev:.0f}"
                else:
                    be_gap = ""

                results.append(
                    f"  {symbol:<14} {signal:<5} ₹{entry:.1f}"
                    f"  conf={prob:.1%}  SL=₹{sl:.1f}"
                    f"  R:R={rr:.2f}x  qty={qty}"
                    f"  risk=₹{risk_amt:.0f}  [{sector}]{be_gap}"
                )
                log.info("  %s: %s prob=%.3f entry=%.2f R:R=%.2fx [%s]",
                         symbol, signal, prob, entry, rr, sector)

            except Exception as e:
                results.append(f"  {symbol:<14} error: {e}")
                log.error("  %s: error: %s", symbol, e)

        chunk_size = 15
        chunks = [results[i:i+chunk_size] for i in range(0, len(results), chunk_size)]
        for idx, chunk in enumerate(chunks):
            header = (
                f"📊 <b>TEST RESULTS ({idx+1}/{len(chunks)})</b> "
                f"— {datetime.now().strftime('%d %b %Y')}\n"
                f"{'─' * 30}\n"
            )
            footer = "\n\n✅ Bot OK. Set BOT_MODE=paper to start paper trading." \
                     if idx == len(chunks) - 1 else ""
            _send(header + "\n".join(chunk) + footer)

        log.info("Test scan complete.")

    # ──────────────────────────────────────────────────────────
    #  FIX-5: Portfolio rotation
    # ──────────────────────────────────────────────────────────
    def _should_rotate(self, new_prob: float) -> tuple[bool, Optional[str]]:
        if not self.trades:
            return False, None

        id_map = {str(t.security_id): sym for sym, t in self.trades.items()}
        prices = self.broker.get_ltp_batch(id_map)

        weakest_symbol: Optional[str] = None
        weakest_prob   = new_prob

        for symbol, trade in self.trades.items():
            ltp = prices.get(str(trade.security_id), 0.0)
            if ltp <= 0:
                continue

            profit_pct = (ltp - trade.entry) / trade.entry
            if profit_pct < ROTATION_MIN_PROFIT:
                log.info("%s: rotation skip — profit %.2f%% < min %.2f%%",
                         symbol, profit_pct * 100, ROTATION_MIN_PROFIT * 100)
                continue

            try:
                df_live = self.broker.get_candles(trade.security_id, symbol, days_back=5)
                if not df_live.empty:
                    scored = self.engine.score(df_live, symbol=symbol)
                    trade.last_prob = scored["prob_up"]
            except Exception as e:
                log.warning("%s: rotation probe score failed: %s — using cached prob", symbol, e)

            if new_prob >= trade.last_prob + ROTATION_MIN_EDGE:
                if weakest_symbol is None or trade.last_prob < weakest_prob:
                    weakest_symbol = symbol
                    weakest_prob   = trade.last_prob
                    log.info(
                        "Rotation candidate: exit %s (prob=%.3f) → new signal (prob=%.3f edge=%.3f)",
                        symbol, trade.last_prob, new_prob, new_prob - trade.last_prob,
                    )

        return (weakest_symbol is not None), weakest_symbol

    # ──────────────────────────────────────────────────────────
    #  Dhan position sync (live mode only)
    # ──────────────────────────────────────────────────────────
    def _sync_with_dhan(self):
        if BOT_MODE != "live" or not self.trades:
            return
        try:
            dhan_pos = self.broker.get_positions()
            if dhan_pos.empty:
                log.warning("sync_dhan: Dhan returned empty positions — API glitch? skipping.")
                return

            sym_col = next(
                (c for c in ["tradingSymbol", "trading_symbol", "symbol"]
                 if c in dhan_pos.columns), None
            )
            if sym_col is None:
                log.warning("sync_dhan: cannot find symbol column in Dhan positions.")
                return

            open_on_dhan = set(dhan_pos[sym_col].str.upper())
            for symbol, trade in list(self.trades.items()):
                if symbol.upper() not in open_on_dhan:
                    log.warning("%s: missing from Dhan — manual close? syncing.", symbol)
                    ltp = self.broker.get_ltp(str(trade.security_id), symbol)
                    exit_price = ltp if ltp > 0 else trade.stop_loss
                    self._exit_trade(trade, exit_price, "CLOSED_ON_DHAN")
                    self.sl_blacklist.add(symbol)
        except Exception as e:
            log.error("sync_dhan: failed: %s", e)

    # ──────────────────────────────────────────────────────────
    #  EXIT TRADE — FIX-8 + PROD-5
    # ──────────────────────────────────────────────────────────
    def _exit_trade(
        self,
        trade:      Trade,
        exit_price: float,
        reason:     str,
    ):
        symbol = trade.symbol

        if trade.side != "LONG":
            log.error(
                "%s: _exit_trade called for side=%s — only LONG supported. Skipping.",
                symbol, trade.side,
            )
            return

        try:
            charges: ChargeBreakdown = calculate_charges(
                buy_price  = trade.entry,
                sell_price = exit_price,
                quantity   = trade.qty,
            )
        except Exception as e:
            log.error("%s: calculate_charges failed (%s) — falling back to gross P&L.", symbol, e)
            gross = round((exit_price - trade.entry) * trade.qty, 2)
            from bot.brokerage import ChargeBreakdown as CB
            charges = CB(
                buy_price=trade.entry, sell_price=exit_price, quantity=trade.qty,
                brokerage=0.0, etc=0.0, stt=0.0, gst=0.0,
                sebi=0.0, stamp=0.0, ipft=0.0,
                total_charges=0.0, gross_pnl=gross,
                net_pnl=gross, breakeven_sell=trade.entry,
            )

        net_pnl = charges.net_pnl

        if trade.entry_prob >= 0.75:
            regime = "high_conf"
        elif trade.entry_prob >= 0.65:
            regime = "medium_conf"
        else:
            regime = "low_conf"

        hold_min = round(trade.hold_minutes(), 1)
        sector   = SECTOR_MAP.get(symbol, "unknown")
        ts       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        log.info(
            "[TRADE_CLOSED] %s  exit=%.2f  reason=%s  held=%.0fm  %s",
            symbol, exit_price, reason, hold_min, charges.to_log_string(),
        )

        if BOT_MODE == "live":
            try:
                self.broker.place_market_order(
                    security_id = str(trade.security_id),
                    symbol      = symbol,
                    qty         = trade.qty,
                    side        = "SELL",
                    order_type  = "MARKET",
                )
                log.info("%s: market exit order placed (reason=%s).", symbol, reason)
            except Exception as e:
                log.error("%s: exit order failed: %s — position may still be open.", symbol, e)

        self.paper_pnl += net_pnl
        self.risk.update_pnl(net_pnl)

        trade_row = {
            "timestamp":      ts,
            "symbol":         symbol,
            "side":           trade.side,
            "qty":            trade.qty,
            "entry":          round(trade.entry, 2),
            "exit_price":     round(exit_price, 2),
            "stop_loss":      round(trade.stop_loss, 2),
            "target":         round(trade.target, 2),
            "rr":             round(trade.rr, 3),
            "atr":            round(trade.atr, 4),
            "hold_minutes":   hold_min,
            "candles_held":   trade.candles_held,
            "exit_reason":    reason,
            "mode":           BOT_MODE,
            "sector":         sector,
            "prob_up":        round(trade.entry_prob, 4),
            "market_regime":  regime,
            "trail_count":    trade.trail_count,
            "gross_pnl":      charges.gross_pnl,
            "brokerage":      charges.brokerage,
            "etc":            round(charges.etc, 4),
            "stt":            round(charges.stt, 4),
            "gst":            round(charges.gst, 4),
            "total_charges":  charges.total_charges,
            "breakeven_sell": charges.breakeven_sell,
            "net_pnl":        net_pnl,
        }
        _log_trade_csv(trade_row)

        analysis_row = {
            "timestamp":     ts,
            "symbol":        symbol,
            "entry":         round(trade.entry, 2),
            "exit_price":    round(exit_price, 2),
            "qty":           trade.qty,
            "hold_minutes":  hold_min,
            "exit_reason":   reason,
            "prob_up":       round(trade.entry_prob, 4),
            "rr":            round(trade.rr, 3),
            "atr":           round(trade.atr, 4),
            "gross_pnl":     charges.gross_pnl,
            "total_charges": charges.total_charges,
            "net_pnl":       net_pnl,
            "market_regime": regime,
        }
        _log_trade_analysis(analysis_row)

        # PROD-5: audit trail for every exit
        try:
            self.audit.exit(
                symbol        = symbol,
                exit_price    = exit_price,
                qty           = trade.qty,
                gross_pnl     = charges.gross_pnl,
                net_pnl       = net_pnl,
                total_charges = charges.total_charges,
                reason        = reason,
                hold_minutes  = hold_min,
            )
        except Exception as ae:
            log.warning("audit.exit failed: %s", ae)

        try:
            alert_exit(
                symbol         = symbol,
                exit_price     = exit_price,
                pnl            = net_pnl,
                reason         = reason,
                hold_minutes   = hold_min,
                charges_detail = charges.to_telegram_lines(),
            )
        except TypeError:
            alert_exit(
                symbol       = symbol,
                exit_price   = exit_price,
                pnl          = net_pnl,
                reason       = reason,
                hold_minutes = hold_min,
            )
            log.info("[CHARGES] %s  %s", symbol, charges.to_log_string())

        self.closed_trades.append({
            **trade_row,
            "open_time": trade.open_time.strftime("%Y-%m-%d %H:%M:%S"),
        })

        self.trades.pop(symbol, None)
        self.engine.reset_symbol(symbol)

        log.info(
            "%s: closed. daily_pnl=₹%.2f  paper_pnl=₹%.2f  "
            "open_trades=%d  net_pnl=₹%.2f  total_charges=₹%.2f",
            symbol, self.risk.daily_pnl, self.paper_pnl,
            len(self.trades), net_pnl, charges.total_charges,
        )

    # ──────────────────────────────────────────────────────────
    #  Scan and enter — PROD-1: EV gate added
    # ──────────────────────────────────────────────────────────
    def _scan_and_enter(self):
        if _now_time() < _parse_time(NO_NEW_TRADE_BEFORE):
            log.info("Pre-trade window: waiting until %s", NO_NEW_TRADE_BEFORE)
            return
        if _no_new_trades():
            return

        if self.risk.is_halted():
            log.critical("RiskManager halted trading — scan aborted.")
            return

        daily_loss_pct = self.risk.daily_pnl / CAPITAL
        if daily_loss_pct <= NEW_TRADE_LOSS_PAUSE:
            log.warning(
                "Daily loss %.2f%% ≤ pause threshold %.2f%% — no new entries.",
                daily_loss_pct * 100, NEW_TRADE_LOSS_PAUSE * 100,
            )
            return

        max_slots_full = len(self.trades) >= MAX_OPEN_TRADES
        log.info("── Scan: ranking %d eligible stocks ──", len(WATCHLIST))

        candidates: list[tuple[str, str, dict, dict]] = []

        for symbol, sec_id in WATCHLIST.items():
            if symbol.upper() in BLOCKED_SYMBOLS:
                continue
            if symbol in self.trades:
                continue
            if symbol in self.sl_blacklist:
                log.debug("%s: SL-blacklisted today — skip.", symbol)
                continue

            cooldown = self.cooldown_bars.get(symbol, 0)
            if cooldown > 0:
                self.cooldown_bars[symbol] = cooldown - 1
                log.debug("%s: cooldown active (%d bars left)", symbol, cooldown)
                continue

            sector = SECTOR_MAP.get(symbol, "unknown")
            sector_count = sum(
                1 for s in self.trades
                if SECTOR_MAP.get(s, "unknown") == sector
            )
            if sector_count >= MAX_PER_SECTOR:
                continue

            df = self.broker.get_candles(sec_id, symbol, days_back=10)
            if df.empty:
                continue

            result = self.engine.score(df, symbol=symbol)
            if result["signal"] != "BUY":
                reason_str = result.get("reason", "")
                if reason_str and reason_str not in (
                    "null", "blocked_symbol", "insufficient_data",
                    "feature_error", "empty_features",
                    "missing_features", "nan_in_features",
                    "predict_error", "invalid_atr_or_price"
                ):
                    for r in reason_str.split(","):
                        r = r.strip()
                        if r:
                            self.rejection_stats[r] += 1
                            if symbol not in self.rejection_symbols[r]:
                                self.rejection_symbols[r].append(symbol)
                continue

            entry  = result["entry"]
            sl     = result["sl"]
            target = result["target"]
            rr     = result["rr"]

            if any(v <= 0 for v in [entry, sl, target, rr]):
                log.warning("%s: invalid engine output entry=%.2f sl=%.2f — skip", symbol, entry, sl)
                continue

            ok, quote = self._passes_entry_filters(symbol, sec_id, entry, reason_prefix="[SCAN] ")
            if not ok:
                continue

            candidates.append((symbol, sec_id, result, quote))
            log.info(
                "  Candidate %-14s prob=%.3f entry=%.2f SL=%.2f TP=%.2f "
                "R:R=%.2fx ev=%.3f spread=%.4f%% [%s]",
                symbol, result["prob_up"], entry, sl, target, rr,
                result["prob_up"] * rr,
                (quote.get("spread_pct") or 0.0) * 100,
                sector,
            )

        if not candidates:
            log.info("No BUY signals this scan.")
            if self.rejection_stats:
                top = sorted(self.rejection_stats.items(), key=lambda x: -x[1])[:6]
                summary = " | ".join(f"{k}={v}" for k, v in top)
                log.info("  Rejection stats (today): %s", summary)
            write_state(self)
            return

        def calibrated_score(prob, rr):
            calibrated = 0.50 + ((prob - 0.50) * 0.35)
            return calibrated * rr

        candidates.sort(
            key=lambda x: calibrated_score(x[2]["prob_up"], x[2].get("rr", 1.0)),
            reverse=True,
        )

        for rank, (sym, _, res, _) in enumerate(candidates, start=1):
            log.info(
                "  Rank #%d %-14s prob=%.3f rr=%.2fx ev=%.3f",
                rank, sym, res["prob_up"], res.get("rr", 0.0),
                res["prob_up"] * res.get("rr", 1.0),
            )

        best_sym, best_sec_id, best_result, best_quote = candidates[0]
        log.info(
            "Best signal: %-14s prob=%.3f rr=%.2fx ev_score=%.3f scan_rank=1/%d",
            best_sym, best_result["prob_up"], best_result.get("rr", 0.0),
            best_result["prob_up"] * best_result.get("rr", 1.0), len(candidates),
        )

        if max_slots_full:
            should_rotate, exit_sym = self._should_rotate(best_result["prob_up"])

            if should_rotate and exit_sym:
                trade_to_exit = self.trades[exit_sym]

                if trade_to_exit.candles_held < self._ROTATION_MIN_CANDLES:
                    log.info(
                        "ROTATION BLOCKED (time hysteresis): %s only %d candles old (min=%d).",
                        exit_sym, trade_to_exit.candles_held, self._ROTATION_MIN_CANDLES,
                    )
                    write_state(self)
                    return

                ev_new = _expected_pnl(
                    prob_up=best_result["prob_up"],
                    entry=best_result["entry"],
                    sl=best_result["sl"],
                    target=best_result["target"],
                    qty=self.risk.position_size(best_result["entry"], best_result["sl"]),
                )
                ev_old = _expected_pnl(
                    prob_up=trade_to_exit.last_prob,
                    entry=trade_to_exit.entry,
                    sl=trade_to_exit.stop_loss,
                    target=trade_to_exit.target,
                    qty=trade_to_exit.qty,
                )
                roundtrip_cost = (
                    trade_to_exit.entry * trade_to_exit.qty * self._ROTATION_ROUNDTRIP_COST_PCT
                )
                ev_gain = ev_new - ev_old

                if ev_gain <= roundtrip_cost:
                    log.info(
                        "ROTATION BLOCKED (ev gate): %s -> %s "
                        "ev_new=₹%.2f ev_old=₹%.2f ev_gain=₹%.2f <= cost=₹%.2f",
                        exit_sym, best_sym, ev_new, ev_old, ev_gain, roundtrip_cost,
                    )
                    write_state(self)
                    return

                id_map = {str(trade_to_exit.security_id): exit_sym}
                prices = self.broker.get_ltp_batch(id_map)
                ltp = prices.get(str(trade_to_exit.security_id), trade_to_exit.entry)
                log.info(
                    "ROTATION APPROVED: exiting %s (held=%d candles, ev_gain=₹%.2f) -> entering %s",
                    exit_sym, trade_to_exit.candles_held, ev_gain, best_sym,
                )
                self._exit_trade(trade_to_exit, ltp, "ROTATION_BETTER_SIGNAL")
            else:
                log.info("Max open trades (%d). No rotation opportunity.", MAX_OPEN_TRADES)
                write_state(self)
                return

        available_slots = max(0, MAX_OPEN_TRADES - len(self.trades))
        if available_slots <= 0:
            write_state(self)
            return

        selected_candidates = candidates[:available_slots]

        for scan_rank, (sym, sec_id, result, quote) in enumerate(selected_candidates, start=1):
            entry  = result["entry"]
            sl     = result["sl"]
            target = result["target"]
            rr     = result["rr"]

            if any(v <= 0 for v in [entry, sl, target, rr]):
                log.warning("%s: final proposal invalid — skip.", sym)
                continue

            sector = SECTOR_MAP.get(sym, "unknown")
            sector_count = sum(
                1 for s in self.trades if SECTOR_MAP.get(s, "unknown") == sector
            )
            if sector_count >= MAX_PER_SECTOR:
                log.info("%s: skipped at entry stage due to sector cap.", sym)
                continue

            qty = self.risk.position_size(entry, sl)
            if qty <= 0:
                log.warning("%s: position size = 0 — skip.", sym)
                continue

            # ── PROD-1: Cost-aware EV gate ────────────────────
            # Compute expected net P&L after Dhan intraday charges.
            # Block the trade if net EV ≤ MIN_NET_EV_INR (₹50 default).
            net_ev = compute_net_ev(result["prob_up"], entry, sl, target, qty)
            if not ev_passes(net_ev, sym):
                self.audit.reject(sym, "ev_gate", prob=result["prob_up"])
                self.rejection_stats["ev_gate"] += 1
                continue

            log.info(
                "SIGNAL BUY %-14s entry=%.2f SL=%.2f TP=%.2f qty=%d prob=%.3f "
                "R:R=%.2fx net_ev=₹%.0f scan_rank=%d/%d sector=%s spread=%.4f%%",
                sym, entry, sl, target, qty,
                result["prob_up"], rr, net_ev,
                scan_rank, len(candidates), sector,
                (quote.get("spread_pct") or 0.0) * 100,
            )

            if BOT_MODE == "live":
                ok, entry_quote = self._passes_entry_filters(
                    sym, sec_id, entry, reason_prefix="[ENTRY] ",
                )
                if not ok:
                    log.warning("%s: entry execution filters failed.", sym)
                    continue

                self._entry_quotes[sym] = {"entry": entry, "ts": time.time()}
                self._enter_live(sym, sec_id, qty, entry, sl, target, result)
            else:
                self._enter_paper(sym, sec_id, qty, entry, sl, target, result)

        write_state(self)

    # ──────────────────────────────────────────────────────────
    #  Paper entry — PROD-5: audit.enter()
    # ──────────────────────────────────────────────────────────
    def _enter_paper(
        self,
        symbol:  str,
        sec_id,
        qty:     int,
        entry:   float,
        sl:      float,
        target:  float,
        result:  dict,
    ):
        trade = Trade(
            symbol      = symbol,
            security_id = sec_id,
            side        = "LONG",
            qty         = qty,
            entry       = entry,
            stop_loss   = sl,
            target      = target,
            order_id    = f"PAPER_{symbol}_{int(time.time())}",
            mode        = "paper",
            last_prob   = result["prob_up"],
            rr          = result.get("rr", 0.0),
            atr         = result.get("atr", 0.0),
        )
        self.trades[symbol] = trade

        be_sell = calculate_charges(entry, entry, qty).breakeven_sell
        net_ev  = compute_net_ev(result["prob_up"], entry, sl, target, qty)

        log.info(
            "PAPER ENTRY %-14s qty=%d entry=%.2f SL=%.2f TP=%.2f "
            "prob=%.3f R:R=%.2fx BE=₹%.2f net_ev=₹%.0f",
            symbol, qty, entry, sl, target,
            result["prob_up"], result.get("rr", 0.0), be_sell, net_ev,
        )

        # PROD-5: audit ENTER event
        try:
            self.audit.enter(
                symbol   = symbol,
                entry    = entry,
                sl       = sl,
                target   = target,
                qty      = qty,
                prob     = result["prob_up"],
                rr       = result.get("rr", 0.0),
                atr      = result.get("atr", 0.0),
                net_ev   = net_ev,
                mode     = "paper",
                order_id = trade.order_id,
                sector   = SECTOR_MAP.get(symbol, ""),
            )
        except Exception as ae:
            log.warning("audit.enter(paper) failed: %s", ae)

        try:
            alert_entry(
                symbol  = symbol, entry = entry, sl = sl, target = target,
                qty     = qty, prob = result["prob_up"],
                rr      = result.get("rr", 0.0), be_sell = be_sell,
            )
        except TypeError:
            alert_entry(
                symbol = symbol, entry = entry, sl = sl, target = target,
                qty    = qty, prob = result["prob_up"], rr = result.get("rr", 0.0),
            )

    # ──────────────────────────────────────────────────────────
    #  Live entry — PROD-2: assert_live_allowed() + PROD-5: audit
    # ──────────────────────────────────────────────────────────
    def _enter_live(
        self,
        symbol:  str,
        sec_id,
        qty:     int,
        entry:   float,
        sl:      float,
        target:  float,
        result:  dict,
    ):
        # PROD-2: 3-factor live interlock — raises RuntimeError if not satisfied
        try:
            assert_live_allowed()
        except RuntimeError as guard_err:
            log.critical(
                "[LiveGuard] BLOCKED live entry for %s: %s", symbol, guard_err
            )
            _send(f"🛑 <b>LiveGuard blocked entry</b>\n{symbol}\n{guard_err}")
            return

        try:
            order_id = self.broker.place_bracket_order(
                security_id = str(sec_id),
                symbol      = symbol,
                qty         = qty,
                entry       = entry,
                sl          = sl,
                target      = target,
            )
            trade = Trade(
                symbol      = symbol,
                security_id = sec_id,
                side        = "LONG",
                qty         = qty,
                entry       = entry,
                stop_loss   = sl,
                target      = target,
                order_id    = order_id,
                mode        = "live",
                last_prob   = result["prob_up"],
                rr          = result.get("rr", 0.0),
                atr         = result.get("atr", 0.0),
            )
            self.trades[symbol] = trade

            be_sell = calculate_charges(entry, entry, qty).breakeven_sell
            net_ev  = compute_net_ev(result["prob_up"], entry, sl, target, qty)

            log.info(
                "LIVE ENTRY %-14s order_id=%s qty=%d entry=%.2f SL=%.2f TP=%.2f "
                "BE=₹%.2f net_ev=₹%.0f",
                symbol, order_id, qty, entry, sl, target, be_sell, net_ev,
            )

            # PROD-5: audit ENTER event
            try:
                self.audit.enter(
                    symbol   = symbol,
                    entry    = entry,
                    sl       = sl,
                    target   = target,
                    qty      = qty,
                    prob     = result["prob_up"],
                    rr       = result.get("rr", 0.0),
                    atr      = result.get("atr", 0.0),
                    net_ev   = net_ev,
                    mode     = "live",
                    order_id = order_id,
                    sector   = SECTOR_MAP.get(symbol, ""),
                )
            except Exception as ae:
                log.warning("audit.enter(live) failed: %s", ae)

            try:
                alert_entry(
                    symbol  = symbol, entry = entry, sl = sl, target = target,
                    qty     = qty, prob = result["prob_up"],
                    rr      = result.get("rr", 0.0), be_sell = be_sell,
                )
            except TypeError:
                alert_entry(
                    symbol = symbol, entry = entry, sl = sl, target = target,
                    qty    = qty, prob = result["prob_up"], rr = result.get("rr", 0.0),
                )

        except Exception as e:
            log.error("LIVE ENTRY FAILED %s: %s", symbol, e)

    # ──────────────────────────────────────────────────────────
    #  Monitor positions — PROD-5: audit.sl_move()
    # ──────────────────────────────────────────────────────────
    def _monitor_positions(self):
        if not self.trades:
            return

        id_map  = {str(t.security_id): sym for sym, t in self.trades.items()}
        prices  = self.broker.get_ltp_batch(id_map)
        is_boundary = self._is_candle_boundary()

        for symbol, trade in list(self.trades.items()):
            ltp = prices.get(str(trade.security_id), 0.0)
            if ltp <= 0:
                log.warning("%s: LTP unavailable — skipping monitor tick.", symbol)
                continue

            if is_boundary:
                trade.candles_held += 1
                if ltp > trade.running_high:
                    trade.running_high = ltp

            # 1. SL hit
            if ltp <= trade.stop_loss:
                log.info("%s: SL hit at ₹%.2f (SL=₹%.2f). Exiting.",
                         symbol, ltp, trade.stop_loss)
                self._exit_trade(trade, ltp, "SL_HIT")
                self.sl_blacklist.add(symbol)
                continue

            # 2. Target hit
            if ltp >= trade.target:
                log.info("%s: TARGET hit at ₹%.2f (TP=₹%.2f). Exiting.",
                         symbol, ltp, trade.target)
                self._exit_trade(trade, ltp, "TARGET_HIT")
                continue

            # 3. Auto-exit 15:15
            if _is_auto_exit_time():
                unrealised_pct = trade.unrealised_pnl(ltp) / (trade.entry * trade.qty)
                if unrealised_pct <= AUTO_EXIT_THRESHOLD:
                    log.info(
                        "%s: auto-exit (15:15 threshold): unrealised=%.2f%% ≤ %.2f%%",
                        symbol, unrealised_pct * 100, AUTO_EXIT_THRESHOLD * 100,
                    )
                    self._exit_trade(trade, ltp, "AUTO_EXIT_EOD")
                    continue

            # 4. Trailing SL
            should_trail, new_sl = self.risk.should_trail(
                trade.entry, ltp, trade.running_high
            )
            if should_trail and new_sl > trade.stop_loss:
                old_sl = trade.stop_loss
                trade.stop_loss = new_sl
                trade.trail_count += 1
                log.info(
                    "%s: trail SL ₹%.2f → ₹%.2f (ltp=₹%.2f trail#%d)",
                    symbol, old_sl, new_sl, ltp, trade.trail_count,
                )
                # PROD-5: audit SL move
                try:
                    self.audit.sl_move(symbol, old_sl, new_sl, ltp)
                except Exception as ae:
                    log.warning("audit.sl_move failed: %s", ae)
                try:
                    alert_trail_update(
                        symbol=symbol, old_sl=old_sl, new_sl=new_sl, ltp=ltp
                    )
                except Exception:
                    pass
                continue

            # 5. Candle boundary checks
            if not is_boundary:
                continue

            # 6. Momentum failure exit
            if trade.candles_held >= self._MOMENTUM_EXIT_MIN_CANDLES:
                try:
                    df_mon = self.broker.get_candles(
                        trade.security_id, symbol, days_back=5
                    )
                    if not df_mon.empty:
                        scored = self.engine.score(df_mon, symbol=symbol)
                        trade.last_prob = scored["prob_up"]

                        from bot.trade_policy import EXIT_LONG_THRESHOLD, WEAK_THRESHOLD, WEAK_CANDLES_MAX
                        if scored["prob_up"] < EXIT_LONG_THRESHOLD:
                            log.info(
                                "%s: momentum failure (prob=%.3f < %.3f). Exiting.",
                                symbol, scored["prob_up"], EXIT_LONG_THRESHOLD,
                            )
                            self._exit_trade(trade, ltp, "MOMENTUM_FAILURE")
                            self.cooldown_bars[symbol] = 3
                            continue
                except Exception as e:
                    log.warning("%s: momentum check failed: %s", symbol, e)

        write_state(self)

    # ──────────────────────────────────────────────────────────
    #  Force exit all (KeyboardInterrupt / EOD)
    # ──────────────────────────────────────────────────────────
    def _force_exit_all(self, reason: str = "FORCE_EXIT"):
        if not self.trades:
            return
        log.warning("Force-exiting all %d open positions: %s", len(self.trades), reason)
        id_map = {str(t.security_id): sym for sym, t in self.trades.items()}
        prices = self.broker.get_ltp_batch(id_map)
        for symbol, trade in list(self.trades.items()):
            ltp = prices.get(str(trade.security_id), 0.0)
            exit_price = ltp if ltp > 0 else trade.entry  # FIX-6: use entry, not running_high
            self._exit_trade(trade, exit_price, reason)

    # ──────────────────────────────────────────────────────────
    #  EOD reset — PROD-5: audit.session_end()
    # ──────────────────────────────────────────────────────────
    def _eod_reset(self):
        log.info("EOD reset triggered.")

        summary = self.risk.daily_summary()
        try:
            alert_daily_summary(
                pnl        = summary["pnl"],
                trades     = self.closed_trades,
                capital    = summary["capital"],
            )
        except Exception as e:
            log.warning("EOD alert failed: %s", e)

        # PROD-5: session end audit record + rejection stats
        try:
            self.audit.session_end(
                net_pnl = self.paper_pnl,
                trades  = len(self.closed_trades),
            )
            if self.rejection_stats:
                top = sorted(self.rejection_stats.items(), key=lambda x: -x[1])[:10]
                log.info("Session rejection summary: %s",
                         " | ".join(f"{k}={v}" for k, v in top))
        except Exception as ae:
            log.warning("audit.session_end failed: %s", ae)

        self.risk.reset_daily()
        self.closed_trades.clear()
        self.paper_pnl = 0.0
        self.sl_blacklist.clear()
        self.cooldown_bars.clear()
        self.rejection_stats.clear()
        self.rejection_symbols.clear()
        self._circuit_alert_sent = False
        self._last_boundary_key  = None  # BUG-C fix
        log.info("EOD reset complete.")

    # ──────────────────────────────────────────────────────────
    #  Main run loop — PROD-4: startup reconciliation
    # ──────────────────────────────────────────────────────────
    def run(self):
        log.info("=" * 60)
        log.info("LiveBot starting | MODE=%s | Capital=₹%s", BOT_MODE, f"{CAPITAL:,}")
        log.info(MODE_LABEL.get(BOT_MODE, BOT_MODE))
        log.info("=" * 60)

        alert_bot_started(mode=BOT_MODE, capital=CAPITAL, watchlist_size=len(WATCHLIST))

        # PROD-5: session start audit
        try:
            self.audit.session_start(
                mode          = BOT_MODE,
                capital       = CAPITAL,
                watchlist_size= len(WATCHLIST),
            )
        except Exception as ae:
            log.warning("audit.session_start failed: %s", ae)

        # PROD-4: Startup broker reconciliation (live mode only)
        # Detects ghost positions from previous sessions and cleans state.
        reconcile_on_startup(self.broker, self.trades, self.audit, BOT_MODE)

        self._refresh_nifty()

        if BOT_MODE == "test":
            self.run_test()
            return

        try:
            while True:
                now = datetime.now()

                if not _is_market_open():
                    log.info("Market closed — sleeping 60s.")
                    time.sleep(60)
                    continue

                # Circuit breaker alert (once per halt)
                if self.risk.is_halted() and not self._circuit_alert_sent:
                    self._circuit_alert_sent = True
                    try:
                        alert_circuit_breaker(
                            reason   = "Daily loss or consecutive SL limit breached",
                            daily_pnl= self.risk.daily_pnl,
                        )
                        # PROD-5: audit circuit
                        self.audit.circuit(
                            reason    = "circuit_breaker_halt",
                            daily_pnl = self.risk.daily_pnl,
                        )
                    except Exception:
                        pass

                # EOD reset
                if _now_time() >= _parse_time(EOD_RESET_TIME):
                    if self.trades:
                        self._force_exit_all("EOD_FORCE_EXIT")
                    self._eod_reset()
                    time.sleep(300)
                    continue

                # Scan for entries
                if time.time() - self._last_scan_ts >= SCAN_INTERVAL:
                    self._last_scan_ts = time.time()
                    self._scan_and_enter()

                # Sync with Dhan (live mode)
                self._sync_with_dhan()

                # Monitor open positions
                self._monitor_positions()

                time.sleep(MONITOR_INTERVAL)

        except KeyboardInterrupt:
            log.warning("KeyboardInterrupt — shutting down.")
            if BOT_MODE == "live" and self.trades:
                log.warning("Force-exiting all live positions before shutdown.")
                self._force_exit_all("KEYBOARD_INTERRUPT")
            self._eod_reset()

        except Exception as e:
            log.critical("Unhandled exception in run loop: %s", e, exc_info=True)
            _send(f"🚨 <b>Bot crashed</b>\n{e}")
            if BOT_MODE == "live" and self.trades:
                self._force_exit_all("CRASH_EXIT")
            raise


# ─────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot = LiveBot()
    bot.run()
