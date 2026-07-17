# ============================================================
#  config/config.py  —  Infrastructure, paths, API keys,
#                        timing, filters, Redis, Telegram.
#
#  v4.1 PATCH 2026-07-17:
#   - PROFIT_LOCK_FLOOR     = 500   (never fall below Rs500 once hit)
#   - PROFIT_PULLBACK_RS    = 45    (if pnl peaks at 550 then drops to 505, exit)
#   - POST_TARGET_BULL_ONLY = True  (new entries after target ONLY on BULL+above_res)
#   - NIFTY_WEAK_HARD_STOP  = True  (WEAK regime = no new entries at all)
#   - NIFTY_RESISTANCE_MULT = 1.002 (nifty close must be > ema50 * this to count as "above resistance")
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
            print(f"[config] \u2705  Loaded .env from: {path}")
            return True
    print(
        "\n[config] \u26a0\ufe0f  WARNING: No .env file found.\n"
        "  Expected location: config/.env\n"
        "  Copy config/.env.example \u2192 config/.env and fill in your credentials.\n"
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


# ── Capital & position sizing ────────────────────────────────
CAPITAL               = 400_000
TOTAL_CAPITAL         = CAPITAL
MAX_RISK_PCT          = 0.01
RISK_PER_TRADE        = MAX_RISK_PCT
MAX_CAPITAL_PER_TRADE = 0.25
MAX_PER_SECTOR        = 3
MAX_OPEN_TRADES       = 3
MAX_OPEN_POSITIONS    = MAX_OPEN_TRADES
DAILY_LOSS_LIMIT      = 0.02
MAX_DAILY_LOSS        = DAILY_LOSS_LIMIT


# ── Daily P&L management (v4.1) ────────────────────────────
# Rs500 target per day.
DAILY_TARGET          = 500.0

# Once daily_pnl >= DAILY_TARGET, we never let it fall below PROFIT_LOCK_FLOOR.
# If pnl was 550 and falls back below (550 - PROFIT_PULLBACK_RS = 505), force-exit
# all positions immediately to lock in profit.
PROFIT_LOCK_FLOOR     = 500.0    # absolute floor in Rs — never fall below this
PROFIT_PULLBACK_RS    = 45.0     # if peak_pnl - current_pnl >= this, exit all

# After target hit, allow NEW entries only on confirmed BULL day
# (regime=BULL AND Nifty above EMA50 * NIFTY_RESISTANCE_MULT).
POST_TARGET_BULL_ONLY    = True
NIFTY_RESISTANCE_MULT    = 1.002   # nifty must be 0.2% above its EMA50 to qualify

# WEAK regime (nifty falling): hard-stop all new entries.
NIFTY_WEAK_HARD_STOP     = True


# ── Trade mode ──────────────────────────────────────────────
TRADE_MODE      = "intraday"
PAPER_TRADE     = os.getenv("BOT_MODE", "paper").lower() != "live"
ALLOW_SHORTS    = True


# ── Timing ──────────────────────────────────────────────────
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


# ── Entry quality filters ───────────────────────────────────
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
ATR_TP_MULT                  = 2.5
SIDEWAYS_ATR_RATIO           = 0.80

REQUIRE_BREAKOUT_CONFIRMATION= True
REQUIRE_VWAP_CONFIRM         = False
TREND_STRENGTH_ENABLED       = True
MAX_EXTENSION_PCT            = 0.030
VWAP_SOFT_PENALTY            = 0.03


# ── Signal thresholds ───────────────────────────────────────
BUY_THRESHOLD_DEFAULT        = 0.52
BUY_THRESHOLD_WEAK           = 0.58
SELL_THRESHOLD_DEFAULT       = 0.52
SELL_THRESHOLD_WEAK          = 0.58
MIN_RR_RATIO                 = 1.5


# ── Symbol penalty ──────────────────────────────────────────
PENALTY_LOOKBACK             = 3
PENALTY_MIN_LOSS             = 1200


# ── Re-entry protection ─────────────────────────────────────
NO_REENTRY_MINUTES = 60


# ── Lunch hours ─────────────────────────────────────────────
AVOID_LUNCH_HOURS  = False
LUNCH_START        = "12:30"
LUNCH_END          = "13:00"


# ── Trailing stop ───────────────────────────────────────────
TRAIL_AFTER_PCT              = 0.012
TRAIL_DISTANCE               = 0.010
TRAILING_SL_ACTIVATE_MULT    = 0.8
TRAILING_SL_TRAIL_MULT       = 1.2


# ── Position rotation ───────────────────────────────────────
ROTATION_ENABLED   = True
ROTATION_MIN_PROFIT= 0.005
ROTATION_MIN_EDGE  = 0.05


# ── Watchlist ───────────────────────────────────────────────
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
        print(f"[config] \u26a0\ufe0f  {len(missing)} symbols missing SECURITY_ID: {missing}")
    print(f"[config] \u2705  Watchlist: {len(watchlist)} symbols ({len(tier_a)} A + {len(tier_b)} B)")
    return watchlist, sector_map


WATCHLIST, SECTOR_MAP = _load_watchlist()


# ── Model paths ─────────────────────────────────────────────
MODEL_PATH         = "models/xgb_model.pkl"
SCALER_PATH        = "models/scaler.pkl"
FEATURE_PATH       = "models/feature_cols.pkl"
BACKUP_MODEL_PATH  = "models/xgb_model_backup.pkl"
BACKUP_SCALER_PATH = "models/scaler_backup.pkl"


# ── Logging paths ────────────────────────────────────────────
LOG_FILE          = "logs/bot.log"
TRADE_LOG         = "logs/trades.csv"
TRADE_LOG_PATH    = TRADE_LOG
RETRAIN_LOG       = "logs/retrain.log"
SIGNAL_LOG        = "logs/signal_scan.csv"
SIGNAL_LOG_PATH   = SIGNAL_LOG


# ── Retraining schedule ─────────────────────────────────────
RETRAIN_EVERY_DAYS  = 7
EMBARGO_DAYS        = 14
MIN_TRAIN_SAMPLES   = 3000
WALK_FORWARD_FOLDS  = 5
MIN_ACCURACY        = 0.52
MIN_AUC             = 0.56
MIN_PRECISION       = 0.52


# ── Redis ────────────────────────────────────────────────────
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
