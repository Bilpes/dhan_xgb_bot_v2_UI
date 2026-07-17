# ============================================================
#  config/config.py  —  Infrastructure, paths, API keys,
#                        timing, filters, Redis, Telegram.
#
#  v4.0 OVERHAUL 2026-07-17:
#   - ALLOW_SHORTS = True  (BUY + SELL both enabled)
#   - NO_NEW_TRADE_BEFORE = 09:15 (was 09:20 — missed ORB candle)
#   - MAX_PER_SECTOR = 3 for BANKING (6 bank stocks in universe)
#   - ATR_SL_MULT  = 1.2  (tighter SL = higher win rate on scalps)
#   - ATR_TP_MULT  = 2.5  (realistic for 0.6-0.8% moves)
#   - BUY_THRESHOLD_DEFAULT = 0.52  (catch more high-probability moves)
#   - SELL_THRESHOLD_DEFAULT = 0.52 (symmetric short side)
#   - MIN_RR_RATIO = 1.5  (ensure each trade justifies the risk)
#   - MAX_OPEN_POSITIONS = 3  (focus — 3 concentrated positions)
#   - DAILY_TARGET = 500  (hard profit target in INR; stop after hitting)
#   - SIDEWAYS_ATR_RATIO = 0.8  (skip flat/consolidating individual stocks)
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
CAPITAL               = 400_000      # Total trading capital in INR
TOTAL_CAPITAL         = CAPITAL      # alias
MAX_RISK_PCT          = 0.01         # 1% of capital max risk per trade = Rs4,000
RISK_PER_TRADE        = MAX_RISK_PCT
MAX_CAPITAL_PER_TRADE = 0.25         # 25% of capital max per single position
MAX_PER_SECTOR        = 3            # v4: raised to 3 (6 bank stocks in universe)
MAX_OPEN_TRADES       = 3            # Focus: max 3 concurrent open positions
MAX_OPEN_POSITIONS    = MAX_OPEN_TRADES
DAILY_LOSS_LIMIT      = 0.02         # 2% of CAPITAL = Rs8,000 daily circuit breaker
MAX_DAILY_LOSS        = DAILY_LOSS_LIMIT

# v4.0: Daily profit target — stop new entries after hitting Rs500
DAILY_TARGET          = 500.0        # Rs500 target per day


# ── Trade mode ──────────────────────────────────────────────
TRADE_MODE      = "intraday"
PAPER_TRADE     = os.getenv("BOT_MODE", "paper").lower() != "live"
ALLOW_SHORTS    = True               # v4.0: SELL (short) trades ENABLED


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

# v4.0: Start at 09:15 — catch first ORB candle
NO_NEW_TRADE_BEFORE = _t("09:15")
NO_NEW_TRADE_AFTER  = _t("15:00")
AUTO_EXIT_TIME      = _t("15:15")


# ── Entry quality filters ───────────────────────────────────
STOP_LOSS_PCT                = 0.020
MIN_STOCK_PRICE              = 50.0
MIN_VOLUME_RATIO             = 0.50          # lowered: 9:15 candles naturally lower
MIN_VOLUME_RATIO_CONFIRM     = 0.75
MIN_ATR_PCT                  = 0.0007
MIN_CANDLE_BODY_PCT          = 0.0005
MAX_DISTANCE_FROM_EMA20      = 0.06
MAX_DISTANCE_FROM_VWAP       = 0.05
MIN_SL_PCT                   = 0.004
MAX_SL_PCT                   = 0.035
ATR_SL_MULT                  = 1.2           # v4: tighter SL (was 1.5)
ATR_TP_MULT                  = 2.5           # v4: realistic TP (was 3.0)

# v4.0: Skip stock if its own ATR is below SIDEWAYS_ATR_RATIO of its 20-period mean
# Prevents entering stocks in personal consolidation even if Nifty is trending.
SIDEWAYS_ATR_RATIO           = 0.80

# Context-aware confirmation flags
REQUIRE_BREAKOUT_CONFIRMATION= True
REQUIRE_VWAP_CONFIRM         = False         # v4: soft penalty only, no hard gate
TREND_STRENGTH_ENABLED       = True

# Extension guard
MAX_EXTENSION_PCT            = 0.030         # slightly wider for high-beta names
VWAP_SOFT_PENALTY            = 0.03


# ── Signal thresholds ───────────────────────────────────────
BUY_THRESHOLD_DEFAULT        = 0.52          # v4: slightly lower for more signals
BUY_THRESHOLD_WEAK           = 0.58
SELL_THRESHOLD_DEFAULT       = 0.52          # v4: symmetric short threshold
SELL_THRESHOLD_WEAK          = 0.58
MIN_RR_RATIO                 = 1.5           # v4: raised from 1.2 — better trades only


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
TRAILING_SL_ACTIVATE_MULT    = 0.8          # v4: activate trailing sooner
TRAILING_SL_TRAIL_MULT       = 1.2          # v4: tighter trail


# ── Position rotation ───────────────────────────────────────
ROTATION_ENABLED   = True
ROTATION_MIN_PROFIT= 0.005
ROTATION_MIN_EDGE  = 0.05


# ── Watchlist ───────────────────────────────────────────────
NIFTY50_SECURITY_ID = 13

_WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "..", "watchlist.json")

def _load_watchlist():
    wf = os.path.abspath(_WATCHLIST_FILE)
    # fallback: look inside config/ too
    if not os.path.exists(wf):
        wf = os.path.join(os.path.dirname(__file__), "watchlist.json")
    if not os.path.exists(wf):
        print("\n[CONFIG WARNING] watchlist.json not found.")
        return {}, {}

    with open(wf) as f:
        data = _json.load(f)

    sec_ids    = data.get("SECURITY_IDS", {})
    sector_map = data.get("SECTOR_MAP",   {})

    tier_a  = data.get("tier_a", [])
    tier_b  = data.get("tier_b", [])
    symbols = tier_a + tier_b

    if not symbols:
        old_style = data.get("WATCHLIST", {})
        if old_style:
            return old_style, sector_map
        print("[CONFIG WARNING] watchlist.json has no tier_a/tier_b keys.")
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
        print(f"[config] \u26a0\ufe0f  {len(missing)} symbols have no SECURITY_ID — skipped: {missing}")

    print(f"[config] \u2705  Watchlist loaded: {len(watchlist)} symbols "
          f"({len(tier_a)} Tier-A + {len(tier_b)} Tier-B)")
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

# Redis TTLs (seconds)
TTL_FEATURE         = 90
TTL_PREDICTION      = 90
TTL_ATR             = 300
TTL_COOLDOWN        = 3600
TTL_CIRCUIT_BREAKER = 86400
TTL_DEDUP_ORDER     = 120
TTL_NIFTY_REGIME    = 300
