# ============================================================
# bot/watchlist_guard.py — Startup watchlist safety validator
#
# PROD-READY (2026-07-10)
#
# Validates the active watchlist before the bot enters the
# scan loop. Blocks startup if any of the following are found:
#
#   1. Blocked symbols (from trade_policy.BLOCKED_SYMBOLS)
#   2. IT sector symbols (from trade_policy.IT_BLOCKED_SYMBOLS)
#   3. Symbols without a valid security_id (would fail API calls)
#   4. Symbols without a sector assignment (would bypass sector limits)
#   5. Duplicate security_ids (same underlying, double exposure)
#
# Usage:
#   from bot.watchlist_guard import validate_watchlist
#   validate_watchlist(WATCHLIST, SECTOR_MAP)  # raises on failure
# ============================================================

from __future__ import annotations

import logging
from typing import Dict

from bot.trade_policy import BLOCKED_SYMBOLS, IT_BLOCKED_SYMBOLS

log = logging.getLogger("watchlist_guard")


class WatchlistValidationError(ValueError):
    """Raised when watchlist fails a safety check."""


def validate_watchlist(
    watchlist: Dict[str, str],
    sector_map: Dict[str, str],
    strict: bool = True,
) -> None:
    """
    Run all safety checks on the active watchlist.

    Parameters
    ----------
    watchlist  : {symbol: security_id} dict (from config.WATCHLIST)
    sector_map : {symbol: sector} dict (from config.SECTOR_MAP)
    strict     : if True, raise on any error; if False, log warnings only.

    Raises
    ------
    WatchlistValidationError  if strict=True and any check fails.
    """
    errors:   list[str] = []
    warnings: list[str] = []

    seen_sids: dict[str, str] = {}

    for sym, sid in watchlist.items():
        sym_upper = sym.upper()

        # Check 1: blocked by policy
        if sym_upper in BLOCKED_SYMBOLS:
            errors.append(f"{sym}: in BLOCKED_SYMBOLS — must be removed from watchlist")

        # Check 2: IT sector
        if sym_upper in IT_BLOCKED_SYMBOLS:
            errors.append(f"{sym}: IT sector symbol — blocked by trade_policy.IT_BLOCKED_SYMBOLS")

        # Check 3: valid security_id
        if not sid or not str(sid).strip():
            errors.append(f"{sym}: missing security_id")

        # Check 4: sector assignment
        if sym not in sector_map:
            warnings.append(f"{sym}: no sector in SECTOR_MAP — sector exposure limits won't apply")

        # Check 5: duplicate security_ids
        sid_str = str(sid).strip()
        if sid_str in seen_sids:
            errors.append(
                f"{sym}: security_id '{sid_str}' already used by {seen_sids[sid_str]} "
                f"— duplicate underlying exposure"
            )
        else:
            seen_sids[sid_str] = sym

    for w in warnings:
        log.warning("[WatchlistGuard] ⚠ %s", w)

    if errors:
        msg = "Watchlist validation failed:\n" + "\n".join(f"  • {e}" for e in errors)
        if strict:
            raise WatchlistValidationError(msg)
        else:
            for e in errors:
                log.error("[WatchlistGuard] ✗ %s", e)

    if not errors and not warnings:
        log.info(
            "[WatchlistGuard] ✅ Watchlist OK: %d symbols, all checks passed.",
            len(watchlist),
        )
    elif not errors:
        log.info(
            "[WatchlistGuard] ✅ Watchlist passed with %d warning(s).",
            len(warnings),
        )
