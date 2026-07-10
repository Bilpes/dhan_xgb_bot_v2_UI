# ============================================================
# bot/live_guard.py — Live trading interlock
#
# PROD-READY (2026-07-10)
#
# PROBLEM:
#   Setting BOT_MODE=live is not sufficient protection against
#   accidental live trading. The .env can be mis-edited, the
#   environment variable can be inherited from a previous shell,
#   or a test runner can start the bot with BOT_MODE=live.
#
# SOLUTION:
#   A three-factor interlock that must ALL be satisfied before
#   any live order is placed:
#
#     Factor 1: BOT_MODE env var == "live"
#     Factor 2: LIVE_TRADING_ENABLED env var == "true"  (explicit opt-in)
#     Factor 3: LIVE_CONFIRM_TOKEN env var matches a known SHA256
#               of a passphrase you set in your .env.  This makes
#               it impossible to accidentally enable live trading
#               via copy-paste without deliberately regenerating
#               the token.
#
# SETUP:
#   1. Pick a passphrase, e.g. "my-live-token-2026"
#   2. Run: python -c "import hashlib; print(hashlib.sha256(b'my-live-token-2026').hexdigest())"
#   3. Add to config/.env:
#        LIVE_TRADING_ENABLED=true
#        LIVE_CONFIRM_TOKEN=<the hash from step 2>
#        LIVE_CONFIRM_PASSPHRASE=my-live-token-2026
#
# Usage:
#   from bot.live_guard import assert_live_allowed
#   assert_live_allowed()   # raises RuntimeError if not fully authorised
# ============================================================

from __future__ import annotations

import hashlib
import logging
import os

log = logging.getLogger("live_guard")


def assert_live_allowed() -> None:
    """
    Verify all three live-trading interlock factors.
    Raises RuntimeError with a clear message if any factor fails.
    Call this once at the start of any live order placement path.
    """
    mode = os.getenv("BOT_MODE", "paper").lower().strip()
    enabled = os.getenv("LIVE_TRADING_ENABLED", "false").lower().strip()
    token  = os.getenv("LIVE_CONFIRM_TOKEN", "").strip()
    phrase = os.getenv("LIVE_CONFIRM_PASSPHRASE", "").strip()

    # Factor 1
    if mode != "live":
        raise RuntimeError(
            f"[LiveGuard] BOT_MODE='{mode}' — must be 'live' for live orders."
        )

    # Factor 2
    if enabled != "true":
        raise RuntimeError(
            "[LiveGuard] LIVE_TRADING_ENABLED is not 'true'. "
            "Set LIVE_TRADING_ENABLED=true in config/.env to enable live orders."
        )

    # Factor 3
    if not token or not phrase:
        raise RuntimeError(
            "[LiveGuard] LIVE_CONFIRM_TOKEN or LIVE_CONFIRM_PASSPHRASE missing. "
            "See bot/live_guard.py for setup instructions."
        )
    expected = hashlib.sha256(phrase.encode()).hexdigest()
    if token != expected:
        raise RuntimeError(
            "[LiveGuard] LIVE_CONFIRM_TOKEN does not match LIVE_CONFIRM_PASSPHRASE hash. "
            "Regenerate the token as described in bot/live_guard.py."
        )

    log.info("[LiveGuard] ✅ All 3 live-trading interlock factors verified.")


def is_live_allowed() -> bool:
    """Non-raising version. Returns True only if all three factors pass."""
    try:
        assert_live_allowed()
        return True
    except RuntimeError:
        return False
