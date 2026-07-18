"""trade_manager.py — full execution layer for dhan_xgb_bot_v2/v3/v4

v4.2 Changes 2026-07-18:
* FIX-3: compute_qty cap raised from 0.20 → 0.30 of capital.
  With Rs1L capital and 0.20 cap, high-price stocks (Rs1000+) get
  capped at only 20 shares — too little to make Rs500/day target.
  0.30 cap = Rs30,000 max per trade, enough to carry meaningful qty.
* FIX-3: Added explicit qty=0 guard with log warning instead of silent skip.
  Previously qty<=0 was silently returning False with no log trace.
* can_trade() third gate removed (DAILY_TARGET check duplicated here AND
  in bot._can_enter_new). Removed from here — bot._can_enter_new() is the
  single authoritative gate.

v4.0 Changes 2026-07-17:
* SHORT (SELL) position support — Position now has a side field (LONG/SHORT)
* P&L for SHORT = (entry - exit_price) * qty
* enter() now reads sig[side] and sig[action] to determine direction
* _place_order() uses SELL for SHORT entry and BUY for SHORT exit
* check_exits() applies SL/TP correctly for both LONG and SHORT

All prior fixes (ISSUE-13 through ISSUE-21, FIX-15) retained.
"""

from __future__ import annotations

import csv
import logging
import math
import os
import threading
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
    side:       str   = "LONG"   # v4: 'LONG' or 'SHORT'
    peak:       float = 0.0
    trough:     float = 0.0      # v4: for SHORT trailing SL
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

        os.makedirs(os.path.dirname(cfg.TRADE_LOG_PATH), exist_ok=True)
        self._log_path = cfg.TRADE_LOG_PATH
        self._log_lock = threading.Lock()
        self._ensure_header()

    def set_watchlist_manager(self, wm) -> None:
        self._wm = wm
        log.info("[TradeManager] WatchlistManager linked.")

    def can_trade(self) -> bool:
        """Check if a new trade is allowed.

        FIX-3 v4.2: Removed the DAILY_TARGET duplicate check from here.
        The authoritative gate is bot._can_enter_new() which checks
        DAILY_TARGET + regime + profit-lock together.
        Keeping it here caused false rejections when daily_pnl was
        correctly handled upstream.
        """
        self._reset_daily_if_needed()
        if self._daily_cb_tripped:
            log.warning("[TradeManager] Daily-loss CB ACTIVE.")
            return False
        with self._lock:
            if len(self.positions) >= cfg.MAX_OPEN_POSITIONS:
                return False
        return True

    def can_enter_sector(self, sector: str) -> bool:
        with self._lock:
            count = sum(1 for p in self.positions.values() if p.sector == sector)
        return count < cfg.MAX_PER_SECTOR

    def compute_sl_tp(self, entry: float, atr: float, side: str = "LONG") -> tuple:
        if side == "SHORT":
            raw_sl = entry + cfg.ATR_SL_MULT * atr
            raw_tp = entry - cfg.ATR_TP_MULT * atr
            sl = min(raw_sl, entry * (1 + cfg.MAX_SL_PCT))
            sl = max(sl,     entry * (1 + cfg.MIN_SL_PCT))
            sl_pts = sl - entry
            tp     = entry - sl_pts * (cfg.ATR_TP_MULT / cfg.ATR_SL_MULT)
            rr     = (entry - tp) / max(sl - entry, 1e-6)
        else:
            raw_sl = entry - cfg.ATR_SL_MULT * atr
            raw_tp = entry + cfg.ATR_TP_MULT * atr
            sl = max(raw_sl, entry * (1 - cfg.MAX_SL_PCT))
            sl = min(sl,     entry * (1 - cfg.MIN_SL_PCT))
            sl_pts = entry - sl
            tp     = entry + sl_pts * (cfg.ATR_TP_MULT / cfg.ATR_SL_MULT)
            rr     = (tp - entry) / max(entry - sl, 1e-6)
        return round(sl, 2), round(tp, 2), round(rr, 4)

    def compute_qty(self, entry: float, sl: float) -> int:
        """Compute share quantity based on risk per trade.

        FIX-3 v4.2:
        - Cap raised from 0.20 → 0.30 of capital.
          With Rs1L capital: max Rs30,000 per trade.
          Previously Rs20,000 cap was too low for high-price stocks
          (e.g. Rs800 stock: old cap = 25 shares, new = 37 shares).
        - Added explicit qty=0 guard with log warning so we never
          silently enter with 0 shares.
        """
        sl_pts = abs(entry - sl)
        if sl_pts <= 0:
            log.warning("[compute_qty] %s: sl_pts=0, cannot size — skipping.", entry)
            return 0
        risk_amount = cfg.RISK_PER_TRADE * self.capital
        qty = math.floor(risk_amount / sl_pts)
        # FIX-3: raised cap from 0.20 → 0.30
        max_qty = math.floor(self.capital * 0.30 / entry)
        qty = min(qty, max_qty)
        if qty <= 0:
            log.warning(
                "[compute_qty] entry=%.2f sl=%.2f risk=%.0f → qty=%d (capped). "
                "Trade skipped — check ATR or capital.",
                entry, sl, risk_amount, qty,
            )
            return 0
        return qty

    def enter(self, symbol: str, sig: dict) -> bool:
        """Wrapper for bot.py. sig has keys: action, side, entry, sl, target, prob, atr."""
        side = sig.get("side", "LONG")
        return self.enter_trade(
            symbol = symbol,
            entry  = sig.get("entry",    0.0),
            atr    = sig.get("atr",      0.0),
            prob   = sig.get("prob",     0.0),
            rr     = sig.get("rr_ratio", None),
            side   = side,
        )

    def enter_trade(self, symbol: str, entry: float, atr: float,
                    prob: float, rr: Optional[float] = None,
                    side: str = "LONG") -> bool:

        if not self.can_trade():
            return False

        sector = self._get_sector(symbol)
        if not self.can_enter_sector(sector):
            return False

        with self._lock:
            if symbol in self.positions:
                return False

        sl, tp, computed_rr = self.compute_sl_tp(entry, atr, side)
        actual_rr = rr if rr is not None else computed_rr

        if actual_rr < cfg.MIN_RR_RATIO:
            log.debug("[TradeManager] %s RR=%.2f < MIN=%.2f — skipped.", symbol, actual_rr, cfg.MIN_RR_RATIO)
            return False

        qty = self.compute_qty(entry, sl)
        if qty <= 0:
            # FIX-3: explicit log instead of silent skip
            log.warning(
                "[TradeManager] %s qty=0 after sizing (entry=%.2f sl=%.2f capital=%.0f) — SKIPPED.",
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
                "[PAPER] %s %s qty=%d entry=%.2f sl=%.2f tp=%.2f rr=%.2f prob=%.3f",
                direction, symbol, qty, entry, sl, tp, actual_rr, prob,
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
            else:  # SHORT
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
        else:  # SHORT
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
