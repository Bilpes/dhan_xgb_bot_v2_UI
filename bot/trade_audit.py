# ============================================================
# bot/trade_audit.py — JSON trade lifecycle auditor
#
# PROD-READY (2026-07-10)
#
# Writes one JSON record per trade lifecycle event:
#   ENTER, EXIT, SL_MOVE, CIRCUIT_BREAKER, RECONCILE, REJECT
#
# Each record is a standalone JSON object (NDJSON format) so
# the file is streamable and never requires full-file parsing.
#
# Usage:
#   from bot.trade_audit import TradeAudit
#   audit = TradeAudit()          # one per bot session
#   audit.enter(symbol, ...)
#   audit.exit(symbol, ...)
#   audit.sl_move(symbol, ...)
#   audit.circuit(reason)
#   audit.reject(symbol, reason)
#
# File: logs/trade_audit.ndjson
# ============================================================

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("trade_audit")

_AUDIT_PATH = Path("logs/trade_audit.ndjson")


class TradeAudit:
    """Thread-safe JSON trade lifecycle auditor."""

    def __init__(self, path: Path = _AUDIT_PATH) -> None:
        self._path = path
        self._lock = threading.Lock()
        os.makedirs(self._path.parent, exist_ok=True)
        log.info("[TradeAudit] logging to %s", self._path)

    # ── Core write ────────────────────────────────────────────
    def _write(self, record: dict) -> None:
        record["ts"] = datetime.now().isoformat(timespec="milliseconds")
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    # ── Event constructors ────────────────────────────────────
    def enter(
        self,
        symbol:    str,
        entry:     float,
        sl:        float,
        target:    float,
        qty:       int,
        prob:      float,
        rr:        float,
        atr:       float,
        net_ev:    float = 0.0,
        mode:      str   = "paper",
        order_id:  str   = "",
        sector:    str   = "",
    ) -> None:
        self._write({
            "event":    "ENTER",
            "symbol":   symbol,
            "entry":    round(entry, 2),
            "sl":       round(sl,    2),
            "target":   round(target,2),
            "qty":      qty,
            "prob":     round(prob,  4),
            "rr":       round(rr,    3),
            "atr":      round(atr,   4),
            "net_ev":   round(net_ev,2),
            "mode":     mode,
            "order_id": order_id,
            "sector":   sector,
        })

    def exit(
        self,
        symbol:         str,
        exit_price:     float,
        qty:            int,
        gross_pnl:      float,
        net_pnl:        float,
        total_charges:  float,
        reason:         str,
        hold_minutes:   float,
    ) -> None:
        self._write({
            "event":          "EXIT",
            "symbol":         symbol,
            "exit_price":     round(exit_price,    2),
            "qty":            qty,
            "gross_pnl":      round(gross_pnl,     2),
            "net_pnl":        round(net_pnl,       2),
            "total_charges":  round(total_charges, 4),
            "reason":         reason,
            "hold_minutes":   round(hold_minutes,  1),
        })

    def sl_move(
        self,
        symbol:  str,
        old_sl:  float,
        new_sl:  float,
        ltp:     float,
    ) -> None:
        self._write({
            "event":   "SL_MOVE",
            "symbol":  symbol,
            "old_sl":  round(old_sl, 2),
            "new_sl":  round(new_sl, 2),
            "ltp":     round(ltp,    2),
        })

    def circuit(self, reason: str, daily_pnl: float = 0.0) -> None:
        self._write({
            "event":      "CIRCUIT_BREAKER",
            "reason":     reason,
            "daily_pnl":  round(daily_pnl, 2),
        })

    def reject(
        self,
        symbol: str,
        reason: str,
        prob:   float = 0.0,
    ) -> None:
        self._write({
            "event":  "REJECT",
            "symbol": symbol,
            "reason": reason,
            "prob":   round(prob, 4),
        })

    def reconcile(
        self,
        symbol:    str,
        action:    str,
        detail:    str = "",
    ) -> None:
        """Record a broker reconciliation event (ghost position, sync, etc.)."""
        self._write({
            "event":  "RECONCILE",
            "symbol": symbol,
            "action": action,
            "detail": detail,
        })

    def session_start(self, mode: str, capital: float, watchlist_size: int) -> None:
        self._write({
            "event":          "SESSION_START",
            "mode":           mode,
            "capital":        round(capital, 2),
            "watchlist_size": watchlist_size,
        })

    def session_end(self, net_pnl: float, trades: int) -> None:
        self._write({
            "event":    "SESSION_END",
            "net_pnl":  round(net_pnl, 2),
            "trades":   trades,
        })
