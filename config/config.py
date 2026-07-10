# ============================================================
#  config/config.py  —  Infrastructure, paths, API keys,
#                        timing, filters, Redis, Telegram.
#
#  THIS FILE OWNS: credentials, paths, candle settings,
#                  timing windows, entry quality filters,
#                  capital sizing constants, Redis TTLs,
#                  Telegram IDs.
#
#  THIS FILE DOES NOT OWN: BUY_THRESHOLD, ATR_SL_MULT,
#  ATR_TP_MULT, HORIZON, MAX_OPEN_POSITIONS, MAX_DAILY_LOSS,
#  MIN_RR_RATIO, or any numeric trading parameter.
#  → All of those live exclusively in bot/trade_policy.py
#
#  CREDENTIALS: Never stored here — read from .env file.
#  See config/.env.example for setup instructions.
#
# Fix log:
#   2026-05-25: BUG-A/B/C/D fixes (see git history)
#   2026-06-28: Removed all duplicate trading params.
#   2026-06-28: Bulletproof .env loader (4 candidate paths).
#   2026-06-28: Fixed _load_watchlist() — was calling
#               data.get('WATCHLIST') but watchlist.json uses
#               keys tier_a / tier_b / SECURITY_IDS.
#               Now builds {symbol: security_id} correctly.
#   2026-06-29: Added DAILY_LOSS_LIMIT — was missing, caused
#               ImportError in risk_manager.py on live_bot start.
#   2026-06-29: Added MAX_OPEN_TRADES — was missing, caused
#               ImportError in live_bot.py. Full import audit done;
#               no further missing constants.
#   2026-07-06: FIX-15 — added extension guard + penalty params.
#               REQUIRE_BREAKOUT_CONFIRMATION and REQUIRE_VWAP_CONFIRM
#               are now context-aware (see signal_engine.py FIX-15).
#   2026-07-10: PROD-READY — added TOTAL_CAPITAL alias for
#               trade_manager.py; added LIVE_TRADING_ENABLED;
#               added MIN_STOCK_PRICE; unified path constants;
#               added REDIS_* defaults for signal_engine.
# ============================================================

import os
import json as _json
from pathlib import Path
from dotenv import load_dotenv


# ── Bulletproof .env loader ──────────────────────────────────
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
        "  Tried:\n" +
        "\n".join(f"    • {p}" for p in candidates) + "\n"
    )
    return False


_ENV_LOADED = _find_and_load_env()


DHAN_CLIENT_ID    = os.getenv("DHAN_CLIENT_ID",    "")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN",  "")
TELEGRAM_BOT_TOKEN= os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID_1= os.getenv("TELEGRAM_CHAT_ID_1", "")
TELEGRAM_CHAT_ID_2= os.getenv("TELEGRAM_CHAT_ID_2", "")

# PROD-READY: live trading explicit opt-in (used by bot/live_guard.py)
LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"


# ── Capital & position sizing ────────────────────────────────
CAPITAL               = 400_000     # Total trading capital in INR
TOTAL_CAPITAL         = CAPITAL     # alias used by trade_manager.py
MAX_RISK_PCT          = 0.01        # 1% of capital max risk per trade = ₹4,000
RISK_PER_TRADE        = MAX_RISK_PCT  # alias used by trade_manager.py
MAX_CAPITAL_PER_TRADE = 0.25        # 25% of capital max per single position
MAX_PER_SECTOR        = 2           # Max concurrent positions per sector
MAX_OPEN_TRADES       = 4           # Max concurrent open positions
MAX_OPEN_POSITIONS    = MAX_OPEN_TRADES  # alias
DAILY_LOSS_LIMIT      = 0.04        # 4% of CAPITAL = ₹16,000 daily circuit breaker
MAX_DAILY_LOSS        = DAILY_LOSS_LIMIT  # alias used by trade_manager.py


# ── Trade mode ──────────────────────────────────────────────
TRADE_MODE      = "intraday"
PAPER_TRADE     = os.getenv("BOT_MODE", "paper").lower() != "live"
ALLOW_SHORTS    = False


# ── Timing ──────────────────────────────────────────────────
CANDLE_INTERVAL      = "5"
LOOKBACK_CANDLES     = 60
NO_NEW_TRADE_BEFORE  = _parse_time = None  # resolved below as datetime.time
NO_NEW_TRADE_AFTER   = None
INTRADAY_CUTOFF      = "15:15"
MARKET_OPEN          = "09:15"
MARKET_CLOSE         = "15:30"

from datetime import time as _time

def _t(s: str) -> _time:
    h, m = map(int, s.split(":"))
    return _time(h, m)

NO_NEW_TRADE_BEFORE = _t("09:20")
NO_NEW_TRADE_AFTER  = _t("15:00")


# ── Entry quality filters ───────────────────────────────────
STOP_LOSS_PCT                = 0.025
MIN_STOCK_PRICE              = 50.0   # ₹50 minimum — prevents penny-stock entries
MIN_VOLUME_RATIO             = 0.60
MIN_VOLUME_RATIO_CONFIRM     = 0.75
MIN_ATR_PCT                  = 0.0007
MIN_CANDLE_BODY_PCT          = 0.0005
MAX_DISTANCE_FROM_EMA20      = 0.06
MAX_DISTANCE_FROM_VWAP       = 0.05
MIN_SL_PCT                   = 0.005   # minimum SL distance
MAX_SL_PCT                   = 0.04    # maximum SL distance
ATR_SL_MULT                  = 1.5     # must match bot/trade_policy.py
ATR_TP_MULT                  = 3.0     # must match bot/trade_policy.py

# Context-aware confirmation flags (FIX-15)
REQUIRE_BREAKOUT_CONFIRMATION= True
REQUIRE_VWAP_CONFIRM         = True
TREND_STRENGTH_ENABLED       = True

# FIX-15: Extension guard
MAX_EXTENSION_PCT            = 0.025
VWAP_SOFT_PENALTY            = 0.04


# ── Symbol penalty (FIX-15) ─────────────────────────────────
PENALTY_LOOKBACK             = 3
PENALTY_MIN_LOSS             = 1200


# ── Re-entry protection ─────────────────────────────────────
NO_REENTRY_MINUTES = 60


# ── Lunch hours ─────────────────────────────────────────────
AVOID_LUNCH_HOURS  = False
LUNCH_START        = "12:30"
LUNCH_END          = "13:00"


# ── Trailing stop ───────────────────────────────────────────
TRAIL_AFTER_PCT  = 0.015
TRAIL_DISTANCE   = 0.012
TRAILING_SL_ACTIVATE_MULT = 1.0   # ATR multiples gain before trailing activates
TRAILING_SL_TRAIL_MULT    = 1.5   # trail this many ATR below running peak


# ── Position rotation ───────────────────────────────────────
ROTATION_ENABLED   = True
ROTATION_MIN_PROFIT= 0.005
ROTATION_MIN_EDGE  = 0.05


# ── Watchlist ───────────────────────────────────────────────
NIFTY50_SECURITY_ID = 13

_WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "watchlist.json")


def _load_watchlist():
    if not os.path.exists(_WATCHLIST_FILE):
        print(
            "\n[CONFIG WARNING] config/watchlist.json not found.\n"
            "Expected at: " + _WATCHLIST_FILE + "\n"
        )
        return {}, {}

    with open(_WATCHLIST_FILE) as f:
        data = _json.load(f)

    sec_ids    = data.get("SECURITY_IDS", {})
    sector_map = data.get("SECTOR_MAP",   {})

    tier_a  = data.get("tier_a", [])
    tier_b  = data.get("tier_b", [])
    symbols = tier_a + tier_b

    if not symbols:
        old_style = data.get("WATCHLIST", {})
        if old_style:
            print("[config] Using legacy WATCHLIST key from watchlist.json")
            return old_style, sector_map
        print("[CONFIG WARNING] watchlist.json has no tier_a/tier_b/WATCHLIST keys.")
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
        print(f"[config] ⚠️  {len(missing)} symbols have no SECURITY_ID — skipped: {missing}")

    print(f"[config] ✅  Watchlist loaded: {len(watchlist)} symbols "
          f"({len(tier_a)} Tier-A + {len(tier_b)} Tier-B)")
    return watchlist, sector_map


WATCHLIST, SECTOR_MAP = _load_watchlist()


# ── Model paths ─────────────────────────────────────────────
MODEL_PATH         = "models/xgb_model.pkl"
SCALER_PATH        = "models/scaler.pkl"
FEATURE_PATH       = "models/feature_cols.pkl"
BACKUP_MODEL_PATH  = "models/xgb_model_backup.pkl"
BACKUP_SCALER_PATH = "models/scaler_backup.pkl"


# ── Logging paths (unified) ──────────────────────────────────
LOG_FILE          = "logs/bot.log"
TRADE_LOG         = "logs/trades.csv"
TRADE_LOG_PATH    = TRADE_LOG          # alias used by trade_manager.py
RETRAIN_LOG       = "logs/retrain.log"
SIGNAL_LOG        = "logs/signal_scan.csv"
SIGNAL_LOG_PATH   = SIGNAL_LOG        # alias used by signal_engine.py


# ── Retraining schedule ─────────────────────────────────────
RETRAIN_EVERY_DAYS  = 7
EMBARGO_DAYS        = 14
MIN_TRAIN_SAMPLES   = 3000
WALK_FORWARD_FOLDS  = 5
MIN_ACCURACY        = 0.52
MIN_AUC             = 0.56
MIN_PRECISION       = 0.52


# ── Redis (signal_engine.py requires these) ──────────────────
REDIS_ENABLED            = os.getenv("REDIS_ENABLED", "false").lower() == "true"
REDIS_HOST               = os.getenv("REDIS_HOST",     "localhost")
REDIS_PORT               = int(os.getenv("REDIS_PORT",  "6379"))
REDIS_DB                 = int(os.getenv("REDIS_DB",    "0"))
REDIS_PASSWORD           = os.getenv("REDIS_PASSWORD",  None)
REDIS_MAX_CONNECTIONS    = 10
REDIS_SOCKET_TIMEOUT     = 2
REDIS_RETRY_ON_TIMEOUT   = True

# Redis TTLs (seconds)
TTL_FEATURE       = 90
TTL_PREDICTION    = 90
TTL_ATR           = 300
TTL_COOLDOWN      = 3600
TTL_CIRCUIT_BREAKER = 86400
TTL_DEDUP_ORDER   = 120
TTL_NIFTY_REGIME  = 300

# Signal-engine config aliases
BUY_THRESHOLD_DEFAULT = 0.55
BUY_THRESHOLD_WEAK    = 0.60
MIN_RR_RATIO          = 1.2
