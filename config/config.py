# ============================================================
#  config/config.py  — Infrastructure, paths, API keys,
#                        timing, filters, Redis, Telegram.
#
#  v4.4 PATCH 2026-07-18:
#
#  SIZING INTENT (user):
#    Paper mode: 4L total capital, 4 open trades = Rs1L per trade slot.
#    'Don't cap anything' — each trade gets its full slot budget.
#    On Rs1000 stock: qty = floor(1,00,000 / 1000) = 100 shares.
#    TP at 1.8x ATR (e.g. Rs9/share) * 100 = Rs900 per winner.
#    SL at 1.2x ATR (e.g. Rs6/share) * 100 = -Rs600 per loser.
#
#  CAPITAL = 4,00,000 (paper mode; 4 slots x Rs1L each)
#  MAX_CAPITAL_PER_TRADE = 1.00 (NO cap — full slot budget per trade)
#  Per-slot budget computed as: CAPITAL / MAX_OPEN_POSITIONS
#
#  GAP-1: SIDEWAYS_NIFTY_THRESH = 0.003
#         When abs(nifty_5c_return) < 0.003 AND regime=NEUTRAL for
#         3 consecutive scans -> treat day as SIDEWAYS, block new entries.
#
#  GAP-2: ATR_TP_MULT = 1.8 (was 2.5)
#         Choppy/range-bound market — price rarely travels 2.5x ATR.
#         1.8x TP hits more frequently. RR = 1.8/1.2 = 1.5 (valid).
#
#  GAP-3: ATR_TP_MULT_BULL = 2.5
#         On confirmed BULL days, signal_engine dynamically uses 2.5x.
#         On NEUTRAL/WEAK, uses ATR_TP_MULT (1.8x).
#
#  v4.3 changes (2026-07-18) retained:
#    CAPITAL corrected, MAX_RISK_PCT=0.015, thresholds 0.55/0.60,
#    NO_REENTRY_MINUTES=30, AVOID_LUNCH_HOURS=True
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
# Paper mode: 4L total capital, 4 open trade slots = Rs1L per slot.
# Each slot budget = CAPITAL / MAX_OPEN_POSITIONS = Rs1,00,000.
# No per-trade cap (MAX_CAPITAL_PER_TRADE = 1.0).
#
# Qty sizing on a Rs1000 stock:
#   slot_budget  = 4,00,000 / 4 = Rs1,00,000
#   qty          = floor(1,00,000 / 1000) = 100 shares
#   SL distance  = 1.2 * ATR (e.g. Rs6)  -> risk = 100 * Rs6 = Rs600
#   TP distance  = 1.8 * ATR (e.g. Rs9)  -> gain = 100 * Rs9 = Rs900
#   Daily target Rs500 needs ~1 winning trade (Rs900) or 2 moderate ones.
CAPITAL               = 4_00_000     # Paper: 4L total (4 slots × Rs1L)
TOTAL_CAPITAL         = CAPITAL

MAX_RISK_PCT          = 0.015        # 1.5% risk — Rs6000 on Rs4L
RISK_PER_TRADE        = MAX_RISK_PCT

# NO cap: each trade slot uses its full budget (CAPITAL / MAX_OPEN_POSITIONS)
# compute_qty uses slot_budget = CAPITAL / MAX_OPEN_POSITIONS
MAX_CAPITAL_PER_TRADE = 1.00        # 100% — no restriction; slot budgeting handles this

MAX_PER_SECTOR        = 2           # max 2 positions in same sector (was 3)
MAX_OPEN_TRADES       = 4
MAX_OPEN_POSITIONS    = MAX_OPEN_TRADES

# Daily loss limit: 2% of Rs4L = Rs8,000 max daily loss (paper)
DAILY_LOSS_LIMIT      = 0.02
MAX_DAILY_LOSS        = DAILY_LOSS_LIMIT


# ── Daily P&L management ─────────────────────────────────────────────
# Rs500 target per day = 0.125% of Rs4L paper capital.
DAILY_TARGET          = 500.0
PROFIT_LOCK_FLOOR     = 500.0
PROFIT_PULLBACK_RS    = 45.0
POST_TARGET_BULL_ONLY    = True
NIFTY_RESISTANCE_MULT    = 1.002
NIFTY_WEAK_HARD_STOP     = True


# ── GAP-1: Sideways day detection ────────────────────────────────────
# When Nifty 5-candle return < this threshold AND regime stays NEUTRAL
# for SIDEWAYS_CONSECUTIVE_SCANS scans, declare it a SIDEWAYS day.
# No new entries on sideways days — only manage existing positions.
SIDEWAYS_NIFTY_THRESH        = 0.003   # 0.3% threshold
SIDEWAYS_CONSECUTIVE_SCANS   = 3       # 3 consecutive NEUTRAL scans = sideways day


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

# GAP-2: TP multiplier — 1.8x for choppy/range market (was 2.5x)
# RR = 1.8 / 1.2 = 1.5 — exactly meets MIN_RR_RATIO. Valid.
# More TP hits in sideways-to-mild-trending conditions.
ATR_TP_MULT                  = 1.8

# GAP-3: BULL day TP multiplier — 2.5x on confirmed trend days
# signal_engine uses this when regime == "BULL" + nifty_above_resistance
ATR_TP_MULT_BULL             = 2.5

SIDEWAYS_ATR_RATIO           = 0.80

REQUIRE_BREAKOUT_CONFIRMATION= True
REQUIRE_VWAP_CONFIRM         = False
TREND_STRENGTH_ENABLED       = True
MAX_EXTENSION_PCT            = 0.030
VWAP_SOFT_PENALTY            = 0.03


# ── Signal thresholds ─────────────────────────────────────────────────
BUY_THRESHOLD_DEFAULT        = 0.55
BUY_THRESHOLD_WEAK           = 0.58
SELL_THRESHOLD_DEFAULT       = 0.60
SELL_THRESHOLD_WEAK          = 0.58
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
