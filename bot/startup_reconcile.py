# ============================================================
# bot/startup_reconcile.py — Startup broker reconciliation
#
# PROD-READY (2026-07-10)
#
# Problem addressed:
#   If the bot is restarted mid-session (crash, manual stop),
#   it has no memory of positions placed in the previous run.
#   Without reconciliation, it would enter the same symbols
#   again, doubling position size, or miss pending SL orders.
#
# What this does:
#   1. Fetches open positions from Dhan on startup.
#   2. For each Dhan position NOT in bot.trades:
#      → Logs a RECONCILE:GHOST_FOUND audit event.
#      → Optionally forces exit (FORCE_EXIT_ON_RESTART=True).
#   3. For each bot.trades entry NOT on Dhan:
#      → Logs RECONCILE:BOT_GHOST and removes the stale entry.
#   4. Returns a dict {symbol: qty} of live positions
#      that the bot should honour as pre-existing.
#
# Usage (from live_bot.py __init__ or run()):
#   from bot.startup_reconcile import reconcile_on_startup
#   existing = reconcile_on_startup(broker, trades, audit, mode)
# ============================================================

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.dhan_api import DhanBroker
    from bot.trade_audit import TradeAudit

log = logging.getLogger("startup_reconcile")

# If True, any Dhan position not in bot.trades is force-exited at market.
# Recommended True for production; False for debugging.
FORCE_EXIT_ON_RESTART = os.getenv("FORCE_EXIT_ON_RESTART", "true").lower() == "true"


def reconcile_on_startup(
    broker,
    bot_trades: dict,
    audit,
    mode: str = "paper",
) -> dict:
    """
    Cross-check broker positions vs bot in-memory state on startup.

    Parameters
    ----------
    broker     : DhanBroker instance
    bot_trades : dict mapping symbol -> Trade (bot's in-memory state)
    audit      : TradeAudit instance (may be None)
    mode       : "live" | "paper" | "test"

    Returns
    -------
    dict {symbol: net_qty} of positions confirmed open on broker.
    Empty if broker query fails or mode != "live".
    """
    if mode != "live":
        log.info("[Reconcile] mode=%s — skipping broker sync.", mode)
        return {}

    log.info("[Reconcile] Startup broker sync...")

    try:
        dhan_pos = broker.get_positions()
    except Exception as e:
        log.error("[Reconcile] get_positions() failed: %s — cannot reconcile.", e)
        return {}

    if dhan_pos is None or (hasattr(dhan_pos, "empty") and dhan_pos.empty):
        log.info("[Reconcile] No open positions on Dhan.")
        return {}

    # Build {symbol: net_qty} from Dhan response
    confirmed: dict = {}
    try:
        sym_col = next(
            (c for c in ["tradingSymbol", "trading_symbol", "symbol"]
             if c in dhan_pos.columns), None,
        )
        qty_col = next(
            (c for c in ["netQty", "net_qty", "quantity"]
             if c in dhan_pos.columns), None,
        )
        if sym_col and qty_col:
            for _, row in dhan_pos.iterrows():
                sym = str(row[sym_col]).upper().strip()
                qty = int(row[qty_col])
                if qty != 0:
                    confirmed[sym] = qty
    except Exception as e:
        log.error("[Reconcile] Failed to parse positions: %s", e)
        return {}

    log.info("[Reconcile] Dhan live positions: %s", list(confirmed.keys()))

    # Case 1: Dhan has a position the bot doesn't know about
    for sym, qty in confirmed.items():
        if sym not in bot_trades:
            log.warning(
                "[Reconcile] GHOST found on Dhan: %s qty=%d — not in bot state.",
                sym, qty,
            )
            if audit:
                audit.reconcile(
                    sym, "GHOST_FOUND",
                    detail=f"qty={qty} on Dhan, not in bot.trades",
                )
            if FORCE_EXIT_ON_RESTART and mode == "live":
                try:
                    ltp = broker.get_ltp(sym, sym)
                    broker.place_market_order(
                        security_id=sym, symbol=sym,
                        qty=qty, side="SELL", order_type="MARKET",
                    )
                    log.warning(
                        "[Reconcile] Force-exited ghost %s at ₹%.2f.", sym, ltp
                    )
                    if audit:
                        audit.reconcile(sym, "GHOST_EXITED", detail=f"ltp={ltp}")
                except Exception as ex:
                    log.error("[Reconcile] Force-exit of ghost %s failed: %s", sym, ex)

    # Case 2: Bot thinks it has a position but Dhan doesn't
    for sym in list(bot_trades.keys()):
        if sym not in confirmed:
            log.warning(
                "[Reconcile] BOT GHOST: %s in bot.trades but not on Dhan — removing.",
                sym,
            )
            if audit:
                audit.reconcile(
                    sym, "BOT_GHOST_REMOVED",
                    detail="in bot.trades but not on Dhan",
                )
            bot_trades.pop(sym, None)

    log.info("[Reconcile] Startup reconciliation complete. Live: %s", list(confirmed.keys()))
    return confirmed
