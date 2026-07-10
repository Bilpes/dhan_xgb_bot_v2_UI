# ============================================================
#  bot/trade_policy.py — Single source of truth for all
#  trade execution parameters (ATR, SL, TP, thresholds)
#
#  auto_retrain.py, signal_engine.py, live_bot.py ALL import
#  from here so train-time and live-time logic are IDENTICAL.
#
#  config/config.py owns ONLY: infra, paths, API keys, Redis,
#  Telegram, timing, filters (VWAP/volume/ATR floor).
#  ALL numeric trading parameters live here — never in config.
#
#  PROD-READY changes (2026-07-10):
#    P1  — MIN_NET_EV_INR gate: every trade must have positive
#           expected net P&L (after charges) before entry.
#    P2  — IT_SECTOR_BLOCKED: hard block on all IT names.
#    P3  — MAX_CONSECUTIVE_LOSSES raised to 4 (log evidence).
#    P4  — REQUIRE_POSITIVE_NET_EV flag (default True).
#    P5  — Consolidated BLOCKED_SYMBOLS matches watchlist.json.
# ============================================================


# ─────────────────────────────────────────────────────────────
# Label design (train-time targets)  — used by auto_retrain.py
# ─────────────────────────────────────────────────────────────
HORIZON           = 12    # 12 x 5 min = 60 min forward window
LABEL_ENTRY_SHIFT = 1     # entry at next-candle open — prevents look-ahead bias
ATR_LABEL_TP_MULT = 2.0   # TP multiplier used during label generation
ATR_LABEL_SL_MULT = 1.2   # SL multiplier used during label generation


# ─────────────────────────────────────────────────────────────
# LIVE trade execution settings
# ─────────────────────────────────────────────────────────────
ATR_SL_MULT = 1.5
ATR_TP_MULT = 3.0
ATR_PERIOD  = 14


# ─────────────────────────────────────────────────────────────
# Entry filters
# ─────────────────────────────────────────────────────────────
BUY_THRESHOLD_DEFAULT = 0.55
BUY_THRESHOLD_WEAK    = 0.60
MIN_RR_RATIO          = 1.2

# P1: Cost-aware expected-value gate.
# Before placing any order the bot computes:
#   EV = prob_up * reward_net - (1-prob_up) * risk_net
# where reward_net and risk_net INCLUDE Dhan intraday charges.
# Trade is blocked if EV <= MIN_NET_EV_INR.
# Set to 0.0 to disable (trades with EV=0 still pass).
MIN_NET_EV_INR        = 50.0   # ₹50 minimum expected net value per trade

# P4: Master switch. Set False in backtesting to ignore EV gate.
REQUIRE_POSITIVE_NET_EV = True


# ─────────────────────────────────────────────────────────────
# Exit / deterioration logic
# ─────────────────────────────────────────────────────────────
EXIT_LONG_THRESHOLD  = 0.48
EXIT_SHORT_THRESHOLD = 0.60
WEAK_THRESHOLD    = 0.52
WEAK_CANDLES_MAX  = 5
MOMENTUM_EXIT_CANDLES = 14


# ─────────────────────────────────────────────────────────────
# Risk controls
# ─────────────────────────────────────────────────────────────
MAX_OPEN_POSITIONS     = 5
MAX_DAILY_LOSS_PCT     = 0.02     # halt after -2% on the day
MAX_CONSECUTIVE_LOSSES = 4        # P3: was 5, tightened after log evidence


# ─────────────────────────────────────────────────────────────
# Volatility-adjusted position sizing
# ─────────────────────────────────────────────────────────────
MAX_ATR_RISK_MULTIPLIER = 0.015
MIN_POSITION_SCALE      = 0.35


# ─────────────────────────────────────────────────────────────
# P2: IT sector block
# These symbols are always skipped regardless of watchlist config.
# Kept separate from watchlist BLOCKED_SYMBOLS so a watchlist
# change cannot accidentally re-enable IT names.
# ─────────────────────────────────────────────────────────────
IT_BLOCKED_SYMBOLS: frozenset = frozenset({
    "TCS", "INFY", "HCLTECH", "WIPRO", "LTIM",
    "PERSISTENT", "MPHASIS", "COFORGE", "TECHM", "OFSS",
    "HEXAWARE", "KPITTECH", "LTTS", "BIRLASOFT",
})


# ─────────────────────────────────────────────────────────────
# P5: Combined blocked list (policy + legacy)
# Merged with watchlist.json BLOCKED_SYMBOLS at runtime.
# ─────────────────────────────────────────────────────────────
BLOCKED_SYMBOLS: frozenset = frozenset({
    # Adani — policy
    "ADANIENT", "ADANIPOWER", "ADANIENERGY",
    # PSU junk / illiquid
    "YESBANK", "IDEA", "RBLBANK", "PAYTM", "BANKBARODA",
    "SAIL", "BHEL",
    # Metals — high gap risk
    "TATASTEEL", "JSWSTEEL", "HINDALCO",
    # Utilities
    "NTPC", "POWERGRID",
}) | IT_BLOCKED_SYMBOLS
