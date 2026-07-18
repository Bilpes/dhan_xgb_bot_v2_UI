# ============================================================
#  config/config.py  — Infrastructure, paths, API keys,
#                        timing, filters, Redis, Telegram.
#
#  v4.6 PATCH 2026-07-19  (4 backtest bug fixes from real data):
#
#  BUG-1: MAX_TRADES_PER_DAY = 8
#    Session-level hard stop. Jul-9 had 17 trades at 11.8% win
#    rate = -₹4,462 because bot re-entered after every SL hit
#    with no ceiling. 8 trades = 2 full rotations of 4-slot book.
#
#  BUG-2: NEUTRAL regime consecutive SL block
#    18/21 backtest days NEUTRAL, win rate 29.3% — far below
#    break-even. Block all new entries after 2 consecutive SL
#    exits on NEUTRAL days. Reset streak on any winning trade.
#    Also tightened BUY_THRESHOLD_NEUTRAL 0.60 → 0.62.
#
#  BUG-3: MAX_DAILY_LOSS 2% → 0.375% (₹8000 → ₹1500)
#    Jul 6/7/9/15 each bled ₹1500–₹4500 with no circuit breaker
#    firing. ₹1500 = ~3 max-loss trades = meaningful protection
#    without triggering on normal intraday swings.
#
#  BUG-4: Banking stocks min threshold on NEUTRAL days
#    SBIN+LT+RELIANCE+AXISBANK = -₹11,800 combined. These 4
#    high-beta names need score ≥10 (prob ≥ 0.64) on NEUTRAL days
#    to avoid chasing momentum against the broader market trend.
#
#  v4.5 PATCH 2026-07-18  (retained — 3 synthetic backtest fixes):
#  FIX-1 [GAP-1]: SIDEWAYS_CONSECUTIVE_SCANS 3→2
#  FIX-2: MAX_TRADES_PER_STOCK_PER_DAY = 3
#  FIX-3: MIN_SIGNAL_SCORE_WEAK = 10
#
#  v4.4 PATCH 2026-07-18 (retained):
#  SIZING REWORK — slot budget Rs1L per trade, no cap.
# ============================================================

import os
import json as _json
from pathlib import Path
from dotenv import load_dotenv


def _find_and_load_env() -> bool:
    _this_dir = Path(__file__).resolve().parent
    _project  = _this_dir.parent
    _cwd      = Path.cwd()
    candidates = [
        _this_dir  / ".env",
        _project   / ".env",
        _cwd       / "config" / ".env",
        _cwd       / ".env",
    ]
    for path in candidates:
        if path.exists():
            load_dotenv(dotenv_path=str(path), override=True)
            print(f"[config] ✅  Loaded .env from: {path}")
            return True
    print(
        "\n[config] ⚠️  WARNING: No .env file found.\n"
        "  Expected location: config/.env\n"
        "  Copy config/.env.example → config/.env and fill in your credentials.\n"
    )
    return False


_ENV_LOADED = _find_and_load_env()


DHAN_CLIENT_ID    = os.getenv("DHAN_CLIENT_ID",    "")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN",  "")
TELEGRAM_BOT_TOKEN= os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID_1= os.getenv("TELEGRAM_CHAT_ID_1", "")
TELEGRAM_CHAT_ID_2= os.getenv("TELEGRAM_CHAT_ID_2", "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID",   "")

LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"


# ── Capital & position sizing ────────────────────────────────────────
CAPITAL               = 4_00_000     # Paper: 4L total (4 slots × Rs1L)
TOTAL_CAPITAL         = CAPITAL

MAX_RISK_PCT          = 0.015        # 1.5% risk — Rs6000 on Rs4L
RISK_PER_TRADE        = MAX_RISK_PCT

MAX_CAPITAL_PER_TRADE = 1.00        # No cap — slot budgeting handles this

MAX_PER_SECTOR        = 2
MAX_OPEN_TRADES       = 4
MAX_OPEN_POSITIONS    = MAX_OPEN_TRADES

# ── BUG-3 FIX: Daily loss limit tightened 2% → 0.375% ───────────────
# Old: 0.02 * 4L = Rs8,000  — too wide, bled Rs4,462 on Jul-9 alone
# New: 0.00375 * 4L = Rs1,500 — ~3 full-size losing trades
# Rationale: Rs500 daily target means Rs1,500 loss = -3x target.
#   At that point the day is statistically unrecoverable.
DAILY_LOSS_LIMIT      = 0.00375     # BUG-3: was 0.02
MAX_DAILY_LOSS        = DAILY_LOSS_LIMIT


# ── Daily P&L management ─────────────────────────────────────────────
DAILY_TARGET          = 500.0
PROFIT_LOCK_FLOOR     = 500.0
PROFIT_PULLBACK_RS    = 45.0
POST_TARGET_BULL_ONLY    = True
NIFTY_RESISTANCE_MULT    = 1.002
NIFTY_WEAK_HARD_STOP     = True


# ── BUG-1 FIX: Session-level trade cap ──────────────────────────────
# Hard ceiling on total trades fired per session (all symbols combined).
# Jul-9: 17 trades at 11.8% win rate = -₹4,462. No session ceiling meant
# the bot kept re-entering after every SL hit on choppy NEUTRAL days.
# 8 = 2 full rotations of the 4-slot book. Beyond 8, day is noise.
MAX_TRADES_PER_DAY           = 8    # BUG-1: NEW — session hard stop


# ── FIX-1 (v4.5): Sideways day detection ─────────────────────────────
SIDEWAYS_NIFTY_THRESH        = 0.003
SIDEWAYS_CONSECUTIVE_SCANS   = 2    # was 3


# ── FIX-2 (v4.5): Per-stock per-day trade cap ────────────────────────
MAX_TRADES_PER_STOCK_PER_DAY = 3


# ── FIX-3 (v4.5): Signal score floor on weak/neutral days ────────────
MIN_SIGNAL_SCORE_WEAK        = 10


# ── BUG-2 FIX: NEUTRAL regime consecutive SL block ──────────────────
# After this many consecutive SL exits on a NEUTRAL day, block all new
# entries for the rest of the session. Reset on any winning trade.
# 18/21 backtest days were NEUTRAL at only 29.3% win rate.
NEUTRAL_CONSEC_SL_BLOCK      = 2    # BUG-2: NEW


# ── BUG-4 FIX: Banking stocks higher threshold on NEUTRAL days ────────
# SBIN + LT + RELIANCE + AXISBANK = -₹11,800 combined in 30-day backtest.
# These high-beta names require stronger conviction on NEUTRAL days:
#   BUY_THRESHOLD_BANKING_NEUTRAL = 0.64 ≈ signal score ≥ 10
# Add/remove tickers here without touching signal_engine logic.
BANKING_STOCKS = [
    "SBIN", "AXISBANK", "ICICIBANK", "HDFCBANK",
    "KOTAKBANK", "INDUSINDBK", "BANDHANBNK",
    "LT", "RELIANCE",          # over-traded high-beta non-banks included
]
BUY_THRESHOLD_BANKING_NEUTRAL = 0.64   # BUG-4: NEW — prob ≥ 0.64 on NEUTRAL


# ── Trade mode ────────────────────────────────────────────────────────
TRADE_MODE      = "intraday"
PAPER_TRADE     = os.getenv("BOT_MODE", "paper").lower() != "live"
ALLOW_SHORTS    = True


# ── Timing ───────────────────────────────────────────────────────────
CANDLE_INTERVAL      = "5"
LOOKBACK_CANDLES     = 60
INTRADAY_CUTOFF      = "15:15"
MARKET_OPEN          = "09:15"
MARKET_CLOSE         = "15:30"

from datetime import time as _time
def _t(s: str) -> _time:
    h, m = map(int, s.split(":"))
    return _time(h, m)

NO_NEW_TRADE_BEFORE = _t("09:15")
NO_NEW_TRADE_AFTER  = _t("15:00")
AUTO_EXIT_TIME      = _t("15:15")


# ── Entry quality filters ─────────────────────────────────────────────
STOP_LOSS_PCT                = 0.020
MIN_STOCK_PRICE              = 50.0
MIN_VOLUME_RATIO             = 0.50
MIN_VOLUME_RATIO_CONFIRM     = 0.75
MIN_ATR_PCT                  = 0.0007
MIN_CANDLE_BODY_PCT          = 0.0005
MAX_DISTANCE_FROM_EMA20      = 0.06
MAX_DISTANCE_FROM_VWAP       = 0.05
MIN_SL_PCT                   = 0.004
MAX_SL_PCT                   = 0.035
ATR_SL_MULT                  = 1.2

ATR_TP_MULT                  = 1.8   # choppy/range market
ATR_TP_MULT_BULL             = 2.5   # confirmed BULL days
SIDEWAYS_ATR_RATIO           = 0.80

REQUIRE_BREAKOUT_CONFIRMATION= True
REQUIRE_VWAP_CONFIRM         = False
TREND_STRENGTH_ENABLED       = True
MAX_EXTENSION_PCT            = 0.030
VWAP_SOFT_PENALTY            = 0.03


# ── Signal thresholds ─────────────────────────────────────────────────
BUY_THRESHOLD_DEFAULT        = 0.55
BUY_THRESHOLD_WEAK           = 0.60
BUY_THRESHOLD_NEUTRAL        = 0.62   # BUG-2: NEW — NEUTRAL needs higher conviction
SELL_THRESHOLD_DEFAULT       = 0.60
SELL_THRESHOLD_WEAK          = 0.60
MIN_RR_RATIO                 = 1.5


# ── Symbol penalty ────────────────────────────────────────────────────
PENALTY_LOOKBACK             = 3
PENALTY_MIN_LOSS             = 1200


# ── Re-entry protection ───────────────────────────────────────────────
NO_REENTRY_MINUTES = 30


# ── Lunch hours ───────────────────────────────────────────────────────
AVOID_LUNCH_HOURS  = True
LUNCH_START        = "12:30"
LUNCH_END          = "13:00"


# ── Trailing stop ─────────────────────────────────────────────────────
TRAIL_AFTER_PCT              = 0.012
TRAIL_DISTANCE               = 0.010
TRAILING_SL_ACTIVATE_MULT    = 0.8
TRAILING_SL_TRAIL_MULT       = 1.2


# ── Position rotation ─────────────────────────────────────────────────
ROTATION_ENABLED   = True
ROTATION_MIN_PROFIT= 0.005
ROTATION_MIN_EDGE  = 0.05


# ── Watchlist ─────────────────────────────────────────────────────────
NIFTY50_SECURITY_ID = 13

_WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "..", "watchlist.json")

def _load_watchlist():
    wf = os.path.abspath(_WATCHLIST_FILE)
    if not os.path.exists(wf):
        wf = os.path.join(os.path.dirname(__file__), "watchlist.json")
    if not os.path.exists(wf):
        print("\n[CONFIG WARNING] watchlist.json not found.")
        return {}, {}

    with open(wf) as f:
        data = _json.load(f)

    sec_ids    = data.get("SECURITY_IDS", {})
    sector_map = data.get("SECTOR_MAP",   {})
    tier_a     = data.get("tier_a", [])
    tier_b     = data.get("tier_b", [])
    symbols    = tier_a + tier_b

    if not symbols:
        old_style = data.get("WATCHLIST", {})
        if old_style:
            return old_style, sector_map
        return {}, sector_map

    watchlist = {}
    missing   = []
    for sym in symbols:
        sid = sec_ids.get(sym)
        if sid:
            watchlist[sym] = str(sid)
        else:
            missing.append(sym)

    if missing:
        print(f"[config] ⚠️  {len(missing)} symbols missing SECURITY_ID: {missing}")
    print(f"[config] ✅  Watchlist: {len(watchlist)} symbols ({len(tier_a)} A + {len(tier_b)} B)")
    return watchlist, sector_map


WATCHLIST, SECTOR_MAP = _load_watchlist()


# ── Model paths ───────────────────────────────────────────────────────
MODEL_PATH         = "models/xgb_model.pkl"
SCALER_PATH        = "models/scaler.pkl"
FEATURE_PATH       = "models/feature_cols.pkl"
BACKUP_MODEL_PATH  = "models/xgb_model_backup.pkl"
BACKUP_SCALER_PATH = "models/scaler_backup.pkl"


# ── Logging paths ─────────────────────────────────────────────────────
LOG_FILE          = "logs/bot.log"
TRADE_LOG         = "logs/trades.csv"
TRADE_LOG_PATH    = TRADE_LOG
RETRAIN_LOG       = "logs/retrain.log"
SIGNAL_LOG        = "logs/signal_scan.csv"
SIGNAL_LOG_PATH   = SIGNAL_LOG


# ── Retraining schedule ───────────────────────────────────────────────
RETRAIN_EVERY_DAYS  = 7
EMBARGO_DAYS        = 14
MIN_TRAIN_SAMPLES   = 3000
WALK_FORWARD_FOLDS  = 5
MIN_ACCURACY        = 0.52
MIN_AUC             = 0.56
MIN_PRECISION       = 0.52


# ── Redis ─────────────────────────────────────────────────────────────
REDIS_ENABLED            = os.getenv("REDIS_ENABLED", "false").lower() == "true"
REDIS_HOST               = os.getenv("REDIS_HOST",     "localhost")
REDIS_PORT               = int(os.getenv("REDIS_PORT",  "6379"))
REDIS_DB                 = int(os.getenv("REDIS_DB",    "0"))
REDIS_PASSWORD           = os.getenv("REDIS_PASSWORD",  None)
REDIS_MAX_CONNECTIONS    = 10
REDIS_SOCKET_TIMEOUT     = 2
REDIS_RETRY_ON_TIMEOUT   = True

TTL_FEATURE         = 90
TTL_PREDICTION      = 90
TTL_ATR             = 300
TTL_COOLDOWN        = 3600
TTL_CIRCUIT_BREAKER = 86400
TTL_DEDUP_ORDER     = 120
TTL_NIFTY_REGIME    = 300
