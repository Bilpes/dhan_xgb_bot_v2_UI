# ============================================================
# bot/symbol_penalty.py
# FIX-15  —  Per-symbol rolling loss tracker
#
# Problem (from log analysis 2026-06-29 – 2026-07-03):
#   The bot repeatedly re-entered the same symbols (YESBANK,
#   ETERNAL, KOTAKBANK, HDFCBANK, CGPOWER) after consecutive
#   SL hits, amplifying daily losses instead of cooling off.
#
# Solution:
#   - Track last PENALTY_LOOKBACK trade results per symbol.
#   - If all N are losses AND cumulative loss > threshold,
#     mark symbol as penalised for the rest of the session.
#   - penalty_factor() returns a 0.0–1.0 size multiplier so
#     the bot can trade smaller rather than being hard-blocked.
#   - Penalty resets at BOD (reset_all called by bot.run).
# ============================================================

from __future__ import annotations
import logging
from collections import deque, defaultdict
from dataclasses import dataclass, field
from typing import Dict

import config as cfg

log = logging.getLogger("symbol_penalty")

LOOKBACK = getattr(cfg, "PENALTY_LOOKBACK",  3)
MIN_LOSS  = getattr(cfg, "PENALTY_MIN_LOSS",  1200.0)   # INR


@dataclass
class _SymbolRecord:
    results: deque = field(default_factory=lambda: deque(maxlen=LOOKBACK))

    def add(self, pnl: float) -> None:
        self.results.append(pnl)

    @property
    def all_losses(self) -> bool:
        return len(self.results) == LOOKBACK and all(p < 0 for p in self.results)

    @property
    def cum_loss(self) -> float:
        return sum(p for p in self.results if p < 0)

    def is_penalised(self) -> bool:
        return self.all_losses and abs(self.cum_loss) >= MIN_LOSS

    def factor(self) -> float:
        """Returns 0.0–1.0 position-size multiplier.

        1.0 = no penalty
        0.5 = 2 consecutive losses but cumulative loss < threshold
        0.0 = fully penalised (all N losses + threshold breached)
        """
        if self.is_penalised():
            return 0.0
        losses = sum(1 for p in self.results if p < 0)
        if losses == 0:
            return 1.0
        # Linearly reduce: 1 loss → 0.75, 2 → 0.5
        return max(0.25, 1.0 - losses * 0.25)


class SymbolPenalty:
    """Session-level symbol penalty tracker.

    Usage
    -----
    penalty = SymbolPenalty()

    # On every exit:
    penalty.set_result('YESBANK', -609.98)
    penalty.set_result('BEL', +1107.0)

    # Before entry:
    if penalty.is_penalised('YESBANK'):
        return reject('SYMBOL_PENALISED')
    qty = int(base_qty * penalty.penalty_factor('YESBANK'))
    """

    def __init__(self) -> None:
        self._records: Dict[str, _SymbolRecord] = defaultdict(_SymbolRecord)

    def set_result(self, symbol: str, pnl: float) -> None:
        """Record the P&L of a completed trade for `symbol`."""
        self._records[symbol].add(pnl)
        rec = self._records[symbol]
        if rec.is_penalised():
            log.warning(
                "[SymbolPenalty] %s PENALISED — last %d trades all losses, "
                "cumulative loss ₹%.2f",
                symbol, LOOKBACK, abs(rec.cum_loss),
            )

    def is_penalised(self, symbol: str) -> bool:
        """True if symbol should be skipped entirely this session."""
        return self._records[symbol].is_penalised()

    def penalty_factor(self, symbol: str) -> float:
        """0.0–1.0 size multiplier for `symbol`."""
        return self._records[symbol].factor()

    def reset_all(self) -> None:
        """Clear all penalty state. Call once per trading day."""
        self._records.clear()
        log.info("[SymbolPenalty] All symbol penalties reset.")

    def status(self) -> dict:
        """Return a snapshot dict {symbol: factor} for dashboard."""
        return {
            sym: round(rec.factor(), 2)
            for sym, rec in self._records.items()
            if rec.results
        }
