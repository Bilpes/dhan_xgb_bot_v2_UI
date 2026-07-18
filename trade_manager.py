"""trade_manager.py — full execution layer for dhan_xgb_bot_v2/v3/v4

v4.5 Changes 2026-07-18 (backtest fixes):

FIX-1 [GAP-1]: SIDEWAYS_CONSECUTIVE_SCANS 3→2 (in config.py)
  TradeManager itself is unchanged for this fix — the sideways-day
  block lives in signal_engine / bot loop. Config change is enough.

FIX-2: Per-stock per-day trade cap (MAX_TRADES_PER_STOCK_PER_DAY)
  _stock_trade_count dict tracks closed trades per symbol per session.
  enter_trade() checks count before entering.
  reset_daily() clears the counter.
  Prevents BAJFINANCE/BEL style over-trading (26-31% win-rate stocks
  re-entering after losses and compounding drawdown).

FIX-3: MIN_SIGNAL_SCORE_WEAK = 10 (in config.py)
  TradeManager does not gate on signal score directly — that filter
  lives in signal_engine. Config change + threshold change is enough.

v4.4 Changes 2026-07-18 (retained):
* SIZING REWORK — 'No cap, Rs1L per trade slot':
  compute_qty now uses SLOT BUDGET instead of a capital-percentage cap.
  slot_budget = CAPITAL / MAX_OPEN_POSITIONS
             = Rs4,00,000 / 4 = Rs1,00,000 per slot
  qty = floor(slot_budget / entry_price)
  e.g. Rs1000 stock -> qty = floor(1,00,000 / 1000) = 100 shares
  No arbitrary 20%/30% cap. Full slot budget deployed.
  Risk is still controlled via SL (qty * sl_pts capped at RISK_PER_TRADE * capital).
  We take the SMALLER of: risk-based qty OR slot-budget qty.
  This ensures:
    - Never exceed Rs1L per slot (capital protection)
    - Never risk more than Rs6000 per trade (loss protection)
    - On Rs1000 stock: qty=100, SL Rs6 away -> risk=Rs600 (well within)

v4.3 Changes 2026-07-18 (retained):
  FIX-3 explicit qty=0 guard, can_trade() DAILY_TARGET gate removed.

v4.0 Changes 2026-07-17 (retained):
  SHORT position support, side=LONG/SHORT throughout.
"""

from __future__ import annotations

import csv
import logging
import math
import os
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, Optional

import config as cfg

log = logging.getLogger(__name__)


@dataclass
class Position:
    symbol:     str
    sector:     str
    entry:      float
    qty:        int
    sl:         float
    tp:         float
    atr:        float
    side:       str   = "LONG"
    peak:       float = 0.0
    trough:     float = 0.0
    trailing_active: bool = False
    current_sl: float = 0.0
    opened_at:  datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        if self.current_sl == 0.0:
            self.current_sl = self.sl
        if self.peak == 0.0:
            self.peak = self.entry
        if self.trough == 0.0:
            self.trough = self.entry

    @property
    def entry_price(self) -> float:
        return self.entry

    @property
    def unrealised_pnl(self) -> float:
        lp = getattr(self, '_last_price', self.entry)
        if self.side == "SHORT":
            return (self.entry - lp) * self.qty
        return (lp - self.entry) * self.qty

    def set_last_price(self, price: float) -> None:
        self._last_price = price
        if self.side == "LONG" and price > self.peak:
            self.peak = price
        elif self.side == "SHORT" and price < self.trough:
            self.trough = price


class TradeManager:

    def __init__(self, dhan_client=None, watchlist_manager=None):
        self._dhan      = dhan_client
        self._wm        = watchlist_manager
        self._lock      = threading.Lock()

        self.capital: float = cfg.TOTAL_CAPITAL
        self.positions: Dict[str, Position] = {}

        self._today: date         = date.today()
        self._realised_pnl: float = 0.0
        self._daily_cb_tripped    = False

        # FIX-2: per-stock per-day closed-trade counter
        # Keyed by symbol, value = number of closed trades today.
        # Cleared by reset_daily().
        self._stock_trade_count: Dict[str, int] = defaultdict(int)

        os.makedirs(os.path.dirname(cfg.TRADE_LOG_PATH), exist_ok=True)
        self._log_path = cfg.TRADE_LOG_PATH
        self._log_lock = threading.Lock()
        self._ensure_header()

    def set_watchlist_manager(self, wm) -> None:
        self._wm = wm
        log.info("[TradeManager] WatchlistManager linked.")

    def can_trade(self) -> bool:
        self._reset_daily_if_needed()
        if self._daily_cb_tripped:
            log.warning("[TradeManager] Daily-loss CB ACTIVE.")
            return False
        with self._lock:
            if len(self.positions) >= cfg.MAX_OPEN_POSITIONS:
                return False
        return True

    def can_trade_symbol(self, symbol: str) -> bool:
        """FIX-2: Return False once a symbol has hit MAX_TRADES_PER_STOCK_PER_DAY
        closed trades in the current session.
        """
        limit = getattr(cfg, "MAX_TRADES_PER_STOCK_PER_DAY", 3)
        count = self._stock_trade_count.get(symbol, 0)
        if count >= limit:
            log.info(
                "[TradeManager] FIX-2: %s reached %d/%d trades today — blocked.",
                symbol, count, limit,
            )
            return False
        return True

    def can_enter_sector(self, sector: str) -> bool:
        with self._lock:
            count = sum(1 for p in self.positions.values() if p.sector == sector)
        return count < cfg.MAX_PER_SECTOR

    def compute_sl_tp(self, entry: float, atr: float, side: str = "LONG",
                      tp_mult: float = None) -> tuple:
        """Compute SL and TP.
        GAP-3: tp_mult can be passed dynamically (1.8 choppy, 2.5 bull).
        Defaults to cfg.ATR_TP_MULT if not provided.
        """
        if tp_mult is None:
            tp_mult = cfg.ATR_TP_MULT

        if side == "SHORT":
            raw_sl = entry + cfg.ATR_SL_MULT * atr
            sl = min(raw_sl, entry * (1 + cfg.MAX_SL_PCT))
            sl = max(sl,     entry * (1 + cfg.MIN_SL_PCT))
            sl_pts = sl - entry
            tp     = entry - sl_pts * (tp_mult / cfg.ATR_SL_MULT)
            rr     = (entry - tp) / max(sl - entry, 1e-6)
        else:
            raw_sl = entry - cfg.ATR_SL_MULT * atr
            sl = max(raw_sl, entry * (1 - cfg.MAX_SL_PCT))
            sl = min(sl,     entry * (1 - cfg.MIN_SL_PCT))
            sl_pts = entry - sl
            tp     = entry + sl_pts * (tp_mult / cfg.ATR_SL_MULT)
            rr     = (tp - entry) / max(entry - sl, 1e-6)
        return round(sl, 2), round(tp, 2), round(rr, 4)

    def compute_qty(self, entry: float, sl: float) -> int:
        """Compute share quantity — SLOT BUDGET approach.

        v4.4: No percentage cap. Each trade slot gets:
            slot_budget = CAPITAL / MAX_OPEN_POSITIONS

        qty is the MINIMUM of:
          (a) risk-based qty  = floor(risk_amount / sl_pts)
              caps downside risk at RISK_PER_TRADE * CAPITAL
          (b) slot-based qty  = floor(slot_budget / entry)
              caps capital deployment to 1 slot (Rs1L on Rs4L / 4 trades)

        Example — Rs1000 stock, ATR=Rs5, SL=Rs6:
          risk_amount  = 0.015 * 4,00,000 = Rs6,000
          risk_qty     = floor(6000 / 6)  = 1000  shares (risk-only)
          slot_budget  = 4,00,000 / 4     = Rs1,00,000
          slot_qty     = floor(1,00,000 / 1000) = 100  shares  <- BINDING
          Final qty    = min(1000, 100)   = 100 shares
          Capital used = 100 * 1000       = Rs1,00,000 ✅
          Actual risk  = 100 * 6          = Rs600 (well within Rs6000 limit)
        """
        sl_pts = abs(entry - sl)
        if sl_pts <= 0:
            log.warning("[compute_qty] sl_pts=0 for entry=%.2f — skipping.", entry)
            return 0

        # (a) Risk-based qty
        risk_amount = cfg.RISK_PER_TRADE * self.capital
        risk_qty    = math.floor(risk_amount / sl_pts)

        # (b) Slot-based qty — slot_budget = CAPITAL / MAX_OPEN_POSITIONS
        slot_budget = self.capital / max(cfg.MAX_OPEN_POSITIONS, 1)
        slot_qty    = math.floor(slot_budget / entry)

        qty = min(risk_qty, slot_qty)

        if qty <= 0:
            log.warning(
                "[compute_qty] entry=%.2f sl=%.2f slot_budget=%.0f slot_qty=%d "
                "risk_qty=%d -> qty=0. Skipped.",
                entry, sl, slot_budget, slot_qty, risk_qty,
            )
            return 0

        log.info(
            "[compute_qty] %s: entry=%.2f sl_pts=%.2f "
            "risk_qty=%d slot_qty=%d -> qty=%d (capital_used=Rs%.0f)",
            entry, entry, sl_pts, risk_qty, slot_qty, qty, qty * entry,
        )
        return qty

    def enter(self, symbol: str, sig: dict) -> bool:
        side    = sig.get("side", "LONG")
        tp_mult = sig.get("tp_mult", cfg.ATR_TP_MULT)
        return self.enter_trade(
            symbol  = symbol,
            entry   = sig.get("entry",    0.0),
            atr     = sig.get("atr",      0.0),
            prob    = sig.get("prob",     0.0),
            rr      = sig.get("rr_ratio", None),
            side    = side,
            tp_mult = tp_mult,
        )

    def enter_trade(self, symbol: str, entry: float, atr: float,
                    prob: float, rr: Optional[float] = None,
                    side: str = "LONG", tp_mult: float = None) -> bool:

        if not self.can_trade():
            return False

        # FIX-2: block symbol if daily trade cap reached
        if not self.can_trade_symbol(symbol):
            return False

        sector = self._get_sector(symbol)
        if not self.can_enter_sector(sector):
            return False

        with self._lock:
            if symbol in self.positions:
                return False

        sl, tp, computed_rr = self.compute_sl_tp(entry, atr, side, tp_mult)
        actual_rr = rr if rr is not None else computed_rr

        if actual_rr < cfg.MIN_RR_RATIO:
            log.debug("[TradeManager] %s RR=%.2f < MIN=%.2f — skipped.", symbol, actual_rr, cfg.MIN_RR_RATIO)
            return False

        qty = self.compute_qty(entry, sl)
        if qty <= 0:
            log.warning(
                "[TradeManager] %s qty=0 (entry=%.2f sl=%.2f capital=%.0f) — SKIPPED.",
                symbol, entry, sl, self.capital,
            )
            return False

        pos = Position(
            symbol=symbol, sector=sector, entry=entry, qty=qty,
            sl=sl, tp=tp, atr=atr, side=side,
        )

        if cfg.PAPER_TRADE:
            direction = "SELL(SHORT)" if side == "SHORT" else "BUY(LONG)"
            log.info(
                "[PAPER] %s %s qty=%d entry=%.2f sl=%.2f tp=%.2f "
                "rr=%.2f prob=%.3f capital_used=Rs%.0f",
                direction, symbol, qty, entry, sl, tp, actual_rr, prob,
                qty * entry,
            )
        else:
            order_side = "SELL" if side == "SHORT" else "BUY"
            success = self._place_order(symbol, qty, entry, side=order_side)
            if not success:
                return False

        with self._lock:
            self.positions[symbol] = pos

        action = "ENTER_SHORT" if side == "SHORT" else "ENTER_LONG"
        self._log_trade(
            action=action, symbol=symbol, qty=qty, price=entry,
            sl=sl, tp=tp, rr=actual_rr, prob=prob, pnl=0.0,
        )
        return True

    def exit_trade(self, symbol: str, price: float, reason: str = "SIGNAL") -> float:
        with self._lock:
            pos = self.positions.pop(symbol, None)
        if pos is None:
            log.warning("[TradeManager] exit_trade for unknown symbol %s.", symbol)
            return 0.0

        if pos.side == "SHORT":
            pnl = (pos.entry - price) * pos.qty
        else:
            pnl = (price - pos.entry) * pos.qty

        if cfg.PAPER_TRADE:
            direction = "COVER(SHORT)" if pos.side == "SHORT" else "SELL(LONG)"
            log.info(
                "[PAPER] %s %s qty=%d price=%.2f pnl=%.2f reason=%s",
                direction, symbol, pos.qty, price, pnl, reason,
            )
        else:
            exit_side = "BUY" if pos.side == "SHORT" else "SELL"
            self._place_order(symbol, pos.qty, price, side=exit_side)

        self._realised_pnl += pnl

        # FIX-2: increment per-stock closed-trade counter on every exit
        self._stock_trade_count[symbol] += 1
        log.debug(
            "[TradeManager] FIX-2: %s trade_count today = %d/%d",
            symbol,
            self._stock_trade_count[symbol],
            getattr(cfg, "MAX_TRADES_PER_STOCK_PER_DAY", 3),
        )

        self._check_daily_cb()

        try:
            from signal_engine import _get_penalty
            p = _get_penalty()
            if p:
                p.set_result(symbol, pnl)
        except Exception as _pen_exc:
            log.debug("[TradeManager] SymbolPenalty.set_result skipped: %s", _pen_exc)

        if self._wm is not None:
            try:
                self._wm.record_trade_result(symbol, pnl)
            except Exception as e:
                log.debug("[TradeManager] wm.record_trade_result error: %s", e)

        self._log_trade(
            action=f"EXIT:{reason}", symbol=symbol, qty=pos.qty,
            price=price, sl=pos.current_sl, tp=pos.tp,
            rr=0.0, prob=0.0, pnl=pnl,
        )
        return pnl

    def force_exit(self, symbol: str, price: float, reason: str = "FORCE") -> float:
        return self.exit_trade(symbol, price, reason)

    def update_trailing_sl(self, symbol: str, last_price: float) -> Optional[float]:
        with self._lock:
            pos = self.positions.get(symbol)
            if pos is None:
                return None

            pos.set_last_price(last_price)

            if pos.side == "LONG":
                gain_atr = (last_price - pos.entry) / pos.atr if pos.atr > 0 else 0.0
                if not pos.trailing_active:
                    if gain_atr >= cfg.TRAILING_SL_ACTIVATE_MULT:
                        pos.trailing_active = True
                if pos.trailing_active:
                    new_sl = pos.peak - cfg.TRAILING_SL_TRAIL_MULT * pos.atr
                    new_sl = round(new_sl, 2)
                    if new_sl > pos.current_sl:
                        pos.current_sl = new_sl
                        return new_sl
            else:
                gain_atr = (pos.entry - last_price) / pos.atr if pos.atr > 0 else 0.0
                if not pos.trailing_active:
                    if gain_atr >= cfg.TRAILING_SL_ACTIVATE_MULT:
                        pos.trailing_active = True
                if pos.trailing_active:
                    new_sl = pos.trough + cfg.TRAILING_SL_TRAIL_MULT * pos.atr
                    new_sl = round(new_sl, 2)
                    if new_sl < pos.current_sl:
                        pos.current_sl = new_sl
                        return new_sl
        return None

    def check_exits(self, symbol: str, candle) -> Optional[str]:
        with self._lock:
            pos = self.positions.get(symbol)
            if pos is None:
                return None

        if isinstance(candle, dict):
            low  = float(candle.get("low",   candle.get("close", 0)))
            high = float(candle.get("high",  candle.get("close", 0)))
        else:
            low = high = float(candle)

        if pos.side == "LONG":
            if low <= pos.current_sl:
                self.exit_trade(symbol, pos.current_sl, reason="SL")
                return "SL"
            if high >= pos.tp:
                self.exit_trade(symbol, pos.tp, reason="TP")
                return "TP"
        else:
            if high >= pos.current_sl:
                self.exit_trade(symbol, pos.current_sl, reason="SL")
                return "SL"
            if low <= pos.tp:
                self.exit_trade(symbol, pos.tp, reason="TP")
                return "TP"

        return None

    def check_sl_tp(self, symbol: str, last_price: float) -> Optional[str]:
        return self.check_exits(symbol, last_price)

    def exit_all(self, prices: Dict[str, float], reason: str = "EOD") -> float:
        total = 0.0
        for symbol in list(self.positions.keys()):
            price = prices.get(symbol, 0.0)
            if price <= 0:
                continue
            total += self.exit_trade(symbol, price, reason=reason)
        return total

    @property
    def open_symbols(self) -> list:
        with self._lock:
            return list(self.positions.keys())

    @property
    def daily_pnl(self) -> float:
        return self._realised_pnl

    @property
    def daily_loss_breached(self) -> bool:
        self._reset_daily_if_needed()
        return self._daily_cb_tripped

    def reset_daily(self) -> None:
        self._today            = date.today()
        self._realised_pnl     = 0.0
        self._daily_cb_tripped = False
        # FIX-2: clear per-stock trade counts for the new session
        self._stock_trade_count.clear()
        log.info("[TradeManager] Daily state reset for %s.", self._today)

    def _get_sector(self, symbol: str) -> str:
        if self._wm is not None:
            try:
                return self._wm.get_sector(symbol)
            except Exception:
                pass
        try:
            from watchlist import SECTOR_MAP
            return SECTOR_MAP.get(symbol, "UNKNOWN")
        except Exception:
            return "UNKNOWN"

    def _reset_daily_if_needed(self) -> None:
        if date.today() != self._today:
            self.reset_daily()

    def _check_daily_cb(self) -> None:
        threshold = -(cfg.MAX_DAILY_LOSS * self.capital)
        if self._realised_pnl <= threshold:
            self._daily_cb_tripped = True
            log.warning(
                "[TradeManager] Daily-loss CB TRIPPED: P&L=%.2f threshold=%.2f.",
                self._realised_pnl, threshold,
            )

    def _place_order(self, symbol: str, qty: int, price: float, side: str = "BUY") -> bool:
        if self._dhan is None:
            log.error("[TradeManager] Dhan client not initialised.")
            return False
        try:
            resp = self._dhan.place_order(
                security_id=symbol,
                exchange_segment="NSE_EQ",
                transaction_type=side,
                quantity=qty,
                order_type="MARKET",
                product_type="INTRADAY",
                price=0,
            )
            log.info("[TradeManager] Order response: %s", resp)
            return True
        except Exception as exc:
            log.error("[TradeManager] Order failed for %s: %s", symbol, exc)
            return False

    _COLS = ["ts", "action", "symbol", "qty", "price", "sl", "tp", "rr", "prob", "pnl"]

    def _ensure_header(self) -> None:
        with self._log_lock:
            if not os.path.exists(self._log_path):
                with open(self._log_path, "w", newline="") as f:
                    csv.DictWriter(f, fieldnames=self._COLS).writeheader()

    def _log_trade(self, **kwargs) -> None:
        row = {"ts": datetime.now().isoformat(timespec="seconds")}
        row.update(kwargs)
        with self._log_lock:
            with open(self._log_path, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=self._COLS, extrasaction="ignore")
                w.writerow(row)
