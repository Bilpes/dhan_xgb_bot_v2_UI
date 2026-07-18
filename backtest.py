"""
backtest.py  --  Real-data backtest for dhan_xgb_bot_v2_UI
================================================================
Auto-loads credentials from  config/.env  (DHAN_CLIENT_ID +
DHAN_ACCESS_TOKEN).  Falls back to environment variables if the
file is absent.

Usage
-----
# Default: Dhan API, last 60 days, 5-min candles
    python backtest.py

# Explicit args:
    python backtest.py --source dhan    --days 60
    python backtest.py --source dhan    --start 2026-05-01 --end 2026-07-17
    python backtest.py --source yfinance --days 30

Output files (written to repo root)
------------------------------------
  backtest_trades.csv   -- every trade: symbol, entry/exit time & px,
                           qty, SL, TP, P&L, exit reason
  backtest_summary.csv  -- day-by-day P&L, regime, win%, target hit
  backtest_report.txt   -- full text report (also printed to console)
================================================================
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings
import json
from collections import defaultdict
from datetime import date, time as dtime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# -------------------------------------------------------------------
# Load credentials from config/.env
# -------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent
_ENV_FILE  = _REPO_ROOT / "config" / ".env"

try:
    from dotenv import load_dotenv
    if _ENV_FILE.exists():
        load_dotenv(_ENV_FILE)
        print(f"[backtest] OK  Loaded credentials from {_ENV_FILE}")
    else:
        print(f"[backtest] WARN  config/.env not found -- reading shell env vars")
except ImportError:
    print("[backtest] WARN  python-dotenv not installed; reading env vars directly")


# -------------------------------------------------------------------
# Import existing bot config
# -------------------------------------------------------------------

try:
    from config.config import (
        CAPITAL, DAILY_TARGET, MAX_DAILY_LOSS,
        MAX_OPEN_POSITIONS, ATR_SL_MULT, ATR_TP_MULT, ATR_TP_MULT_BULL,
        PROFIT_PULLBACK_RS, MAX_PER_SECTOR,
        NIFTY_WEAK_HARD_STOP, POST_TARGET_BULL_ONLY,
        BUY_THRESHOLD_DEFAULT, BUY_THRESHOLD_WEAK,
        SIDEWAYS_NIFTY_THRESH, SIDEWAYS_CONSECUTIVE_SCANS,
        MIN_RR_RATIO, MAX_SL_PCT, MIN_SL_PCT, RISK_PER_TRADE,
        MAX_TRADES_PER_STOCK_PER_DAY,
    )
except ImportError:
    from config import (
        CAPITAL, DAILY_TARGET, MAX_DAILY_LOSS,
        MAX_OPEN_POSITIONS, ATR_SL_MULT, ATR_TP_MULT, ATR_TP_MULT_BULL,
        PROFIT_PULLBACK_RS, MAX_PER_SECTOR,
        NIFTY_WEAK_HARD_STOP, POST_TARGET_BULL_ONLY,
        BUY_THRESHOLD_DEFAULT, BUY_THRESHOLD_WEAK,
        SIDEWAYS_NIFTY_THRESH, SIDEWAYS_CONSECUTIVE_SCANS,
        MIN_RR_RATIO, MAX_SL_PCT, MIN_SL_PCT, RISK_PER_TRADE,
        MAX_TRADES_PER_STOCK_PER_DAY,
    )


# -------------------------------------------------------------------
# Load watchlist + Dhan security IDs from config/watchlist.json
# Structure: { "tier_a": [...], "tier_b": [...], "SECURITY_IDS": {sym: id} }
# -------------------------------------------------------------------

def _load_watchlist() -> tuple[list[str], dict[str, int]]:
    """
    Returns (symbols, dhan_id_map) by reading config/watchlist.json.

    Supports three layouts:
      A) { "tier_a": [...], "tier_b": [...], "SECURITY_IDS": {sym: "id"} }  <- current repo
      B) { "stocks": [{"symbol": ..., "security_id": ...}] }               <- legacy
      C) flat list of strings or dicts                                       <- fallback
    """
    candidates = [
        _REPO_ROOT / "config" / "watchlist.json",
        _REPO_ROOT / "watchlist.json",
    ]

    wl_path = None
    for p in candidates:
        if p.exists():
            wl_path = p
            break

    if wl_path is None:
        raise FileNotFoundError(
            "watchlist.json not found. Looked in:\n"
            + "\n".join(f"  {p}" for p in candidates)
        )

    with open(wl_path, encoding="utf-8") as f:
        wl = json.load(f)

    symbols: list[str]       = []
    dhan_ids: dict[str, int] = {}

    if "tier_a" in wl or "tier_b" in wl:
        symbols = list(wl.get("tier_a", [])) + list(wl.get("tier_b", []))
        for sym, sid in wl.get("SECURITY_IDS", {}).items():
            try:
                dhan_ids[sym] = int(sid)
            except (ValueError, TypeError):
                pass

    elif "stocks" in wl:
        for entry in wl["stocks"]:
            sym = entry.get("symbol", "")
            if sym:
                symbols.append(sym)
                sid = entry.get("security_id") or entry.get("dhan_id")
                if sid:
                    dhan_ids[sym] = int(sid)

    elif isinstance(wl, list):
        for entry in wl:
            if isinstance(entry, str):
                symbols.append(entry)
            elif isinstance(entry, dict):
                sym = entry.get("symbol", "")
                if sym:
                    symbols.append(sym)
                    sid = entry.get("security_id") or entry.get("dhan_id")
                    if sid:
                        dhan_ids[sym] = int(sid)

    symbols = [s for s in symbols if s]
    print(f"[backtest] OK  Watchlist loaded: {len(symbols)} symbols from {wl_path.name}")
    return symbols, dhan_ids


SYMBOLS, _WL_DHAN_IDS = _load_watchlist()

# Nifty 50 index constants
NIFTY_YF   = "^NSEI"
NIFTY_DHAN = "__NIFTY__"
NIFTY_SEC_ID = int(os.environ.get("NIFTY50_SECURITY_ID", "13"))

DHAN_ID: dict[str, int] = {
    "HDFCBANK":   1333,  "ICICIBANK":  4963,  "SBIN":       3045,
    "AXISBANK":   5900,  "KOTAKBANK":  1922,  "BAJFINANCE": 317,
    "RELIANCE":   2885,  "EICHERMOT":  910,   "SUNPHARMA":  3351,
    "DRREDDY":    881,   "CIPLA":      694,   "LT":         11483,
    "HAL":        2303,  "BEL":        383,   "TITAN":      3506,
    "TRENT":      1964,  "ETERNAL":    5097,  "ADANIPORTS": 15083,
    "CHOLAFIN":   685,   "CGPOWER":    760,   "HAVELLS":    9819,
}
DHAN_ID.update(_WL_DHAN_IDS)
DHAN_ID[NIFTY_DHAN] = NIFTY_SEC_ID

_DHAN_MAX_CHUNK_DAYS = 85

# Dhan SDK interval map: CLI minutes string -> SDK interval constant
_DHAN_INTERVAL_MAP = {
    "1":  "1",
    "5":  "5",
    "15": "15",
    "25": "25",
    "60": "60",
}


# -------------------------------------------------------------------
# DATA LAYER -- Dhan historical API  (dhanhq v2 SDK)
#
# The v2 SDK method is:  dhan.get_intraday_candle_data(...)
# NOT historical_minute_charts() which was v1.
#
# v2 signature:
#   get_intraday_candle_data(
#       security_id   : str,
#       exchange_segment : str,   # "NSE_EQ" or "IDX_I"
#       instrument_type  : str,   # "EQUITY" or "INDEX"
#       interval         : str,   # "1", "5", "15", "25", "60"
#       from_date        : str,   # "YYYY-MM-DD"
#       to_date          : str,
#   )
# Response: { "data": { "open": [...], "high": [...], "low": [...],
#                       "close": [...], "volume": [...],
#                       "timestamp": [...] } }
# -------------------------------------------------------------------

def _dhan_fetch_one(
    dhan,
    sym: str,
    sec_id: int,
    from_date: date,
    to_date: date,
    interval: str,
) -> pd.DataFrame:
    is_index = sym == NIFTY_DHAN
    frames: list[pd.DataFrame] = []

    chunk_start = from_date
    while chunk_start <= to_date:
        chunk_end = min(chunk_start + timedelta(days=_DHAN_MAX_CHUNK_DAYS - 1), to_date)
        try:
            resp = dhan.get_intraday_candle_data(
                security_id      = str(sec_id),
                exchange_segment = "IDX_I"  if is_index else "NSE_EQ",
                instrument_type  = "INDEX"  if is_index else "EQUITY",
                interval         = _DHAN_INTERVAL_MAP.get(interval, "5"),
                from_date        = str(chunk_start),
                to_date          = str(chunk_end),
            )

            # handle both dict-response and direct-list-response
            if isinstance(resp, dict):
                data = resp.get("data", resp)
            else:
                data = resp

            if data:
                # normalise key names (SDK uses 'timestamp' or 'start_Time')
                ts_key = "timestamp" if "timestamp" in data else "start_Time"
                df = pd.DataFrame({
                    "open":      data.get("open",    []),
                    "high":      data.get("high",    []),
                    "low":       data.get("low",     []),
                    "close":     data.get("close",   []),
                    "volume":    data.get("volume",  []),
                    "timestamp": data.get(ts_key,    []),
                })
                if not df.empty:
                    df.index = (
                        pd.to_datetime(df["timestamp"])
                          .dt.tz_localize("Asia/Kolkata")
                    )
                    df = df[["open", "high", "low", "close", "volume"]].dropna()
                    frames.append(df)
        except Exception as exc:
            print(f"    WARN  chunk {chunk_start} to {chunk_end}: {exc}")

        chunk_start = chunk_end + timedelta(days=1)

    if not frames:
        return pd.DataFrame()
    result = pd.concat(frames).sort_index()
    return result[~result.index.duplicated(keep="first")]


def fetch_dhan(
    symbols: list[str],
    start: date,
    end: date,
    interval: str = "5",
) -> dict[str, pd.DataFrame]:
    """
    Fetch OHLCV for all symbols + Nifty from Dhan historical API.
    Uses dhanhq v2 SDK: get_intraday_candle_data()
    interval: "1" | "5" | "15" | "25" | "60" minutes
    """
    from dhanhq import dhanhq

    client_id    = os.environ.get("DHAN_CLIENT_ID", "").strip()
    access_token = os.environ.get("DHAN_ACCESS_TOKEN", "").strip()

    if not client_id or not access_token:
        print("\n[ERROR] DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not set.")
        print(f"        Expected at: {_ENV_FILE}")
        sys.exit(1)

    dhan     = dhanhq(client_id, access_token)
    result: dict[str, pd.DataFrame] = {}
    all_syms = symbols + [NIFTY_DHAN]
    total    = len(all_syms)

    print(f"\n[Dhan API] Fetching {total} symbols  {start} to {end}  interval={interval}m")
    print(f"           Range: {(end - start).days} days  (<={_DHAN_MAX_CHUNK_DAYS}-day chunks)\n")

    for i, sym in enumerate(all_syms, 1):
        sec_id = DHAN_ID.get(sym)
        if not sec_id:
            print(f"  [{i:>3}/{total}]  SKIP  {sym}: not in DHAN_ID map")
            continue
        df = _dhan_fetch_one(dhan, sym, sec_id, start, end, interval)
        if df.empty:
            print(f"  [{i:>3}/{total}]  FAIL  {sym}: no data returned")
        else:
            result[sym] = df
            print(f"  [{i:>3}/{total}]  OK    {sym}: {len(df):>5} candles")

    return result


# -------------------------------------------------------------------
# DATA LAYER -- yfinance (fallback, no credentials needed)
# -------------------------------------------------------------------

def fetch_yfinance(
    symbols: list[str],
    start: date,
    end: date,
    interval: str = "5m",
) -> dict[str, pd.DataFrame]:
    """
    Fetch OHLCV via yfinance (free, no credentials).
    NOTE: yfinance caps intraday data at ~60 days.
    """
    import yfinance as yf

    all_syms   = symbols + [NIFTY_YF]
    tickers_ns = {sym: (sym if sym.startswith("^") else f"{sym}.NS") for sym in all_syms}
    result: dict[str, pd.DataFrame] = {}

    print(f"\n[yfinance] Fetching {len(all_syms)} tickers  {start} to {end}  interval={interval}")

    raw = yf.download(
        tickers     = list(tickers_ns.values()),
        start       = str(start),
        end         = str(end + timedelta(days=1)),
        interval    = interval,
        progress    = False,
        auto_adjust = True,
        group_by    = "ticker",
    )

    for sym, ticker in tickers_ns.items():
        try:
            if len(tickers_ns) > 1:
                if ticker not in raw.columns.get_level_values(0):
                    continue
                df = raw[ticker].copy()
            else:
                df = raw.copy()

            if df is None or df.empty:
                continue

            # flatten MultiIndex columns if present
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df.columns = [c.lower() for c in df.columns]

            # ensure tz-aware IST index
            df.index = pd.to_datetime(df.index)
            if df.index.tz is None:
                df.index = df.index.tz_localize("Asia/Kolkata")
            else:
                df.index = df.index.tz_convert("Asia/Kolkata")

            df = df[["open", "high", "low", "close", "volume"]].dropna()

            # keep only market hours 09:15 - 15:30 IST
            df = df.between_time("09:15", "15:30")

            if df.empty:
                continue

            result[sym] = df
            print(f"  OK    {sym}: {len(df)} candles")
        except Exception as exc:
            print(f"  FAIL  {sym}: {exc}")

    return result


# -------------------------------------------------------------------
# HELPERS
# -------------------------------------------------------------------

def _day_slice(df: pd.DataFrame, d: date) -> pd.DataFrame:
    """
    Extract candles for a single calendar date from a tz-aware DataFrame.
    Uses .date() comparison instead of .normalize() to avoid tz issues.
    """
    if df.empty:
        return df
    # df.index.date returns an array of datetime.date objects -- safe for tz-aware
    mask = np.array(df.index.date) == d
    sliced = df.loc[mask]
    if sliced.empty:
        return sliced
    return sliced.between_time("09:15", "15:30")


def _get_regime(nifty_day: pd.DataFrame) -> tuple[str, float]:
    if nifty_day.empty:
        return "NEUTRAL", 0.0
    ret = (nifty_day.iloc[-1]["close"] - nifty_day.iloc[0]["open"]) / nifty_day.iloc[0]["open"]
    if   ret >  SIDEWAYS_NIFTY_THRESH: return "BULL",    round(float(ret), 5)
    elif ret < -SIDEWAYS_NIFTY_THRESH: return "WEAK",    round(float(ret), 5)
    else:                               return "NEUTRAL", round(float(ret), 5)


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    h, l, c = df["high"], df["low"], df["close"]
    pc  = c.shift(1)
    tr  = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if pd.notna(val) else float(tr.mean())


def _simple_rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g  = gains[-period:].mean()
    avg_l  = losses[-period:].mean()
    return 100.0 if avg_l == 0 else 100.0 - (100.0 / (1.0 + avg_g / avg_l))


def _compute_sl_tp(
    entry: float, atr: float, side: str, tp_mult: float
) -> tuple[float, float, float]:
    if side == "LONG":
        sl  = max(entry - ATR_SL_MULT * atr, entry * (1 - MAX_SL_PCT))
        sl  = min(sl, entry * (1 - MIN_SL_PCT))
        pts = entry - sl
        tp  = entry + pts * (tp_mult / ATR_SL_MULT)
    else:
        sl  = min(entry + ATR_SL_MULT * atr, entry * (1 + MAX_SL_PCT))
        sl  = max(sl, entry * (1 + MIN_SL_PCT))
        pts = sl - entry
        tp  = entry - pts * (tp_mult / ATR_SL_MULT)
    rr = abs(tp - entry) / max(abs(sl - entry), 1e-9)
    return round(sl, 2), round(tp, 2), round(rr, 4)


def _compute_qty(entry: float, sl: float) -> int:
    pts      = abs(entry - sl)
    if pts <= 0:
        return 0
    risk_qty = int((RISK_PER_TRADE * CAPITAL) / pts)
    slot_qty = int((CAPITAL / MAX_OPEN_POSITIONS) / entry)
    return max(min(risk_qty, slot_qty), 0)


def _trade_rec(
    trade_date: date, sym: str, pos: dict,
    exit_px: float, exit_ts, pnl: float,
    reason: str, regime: str,
) -> dict:
    return {
        "date":       str(trade_date),
        "symbol":     sym,
        "side":       pos["side"],
        "regime":     regime,
        "entry_time": str(pos["entry_ts"]),
        "entry_px":   round(pos["entry"], 4),
        "exit_time":  str(exit_ts),
        "exit_px":    round(exit_px, 4),
        "qty":        pos["qty"],
        "sl":         round(pos["sl"], 4),
        "tp":         round(pos["tp"], 4),
        "pnl":        round(pnl, 2),
        "reason":     reason,
    }


# -------------------------------------------------------------------
# CORE -- bar-by-bar replay for ONE trading day
# -------------------------------------------------------------------

def replay_day(
    trade_date: date,
    all_data: dict[str, pd.DataFrame],
    nifty_key: str,
) -> tuple[list[dict], dict]:
    nifty_day = _day_slice(all_data.get(nifty_key, pd.DataFrame()), trade_date)
    regime, _ = _get_regime(nifty_day)

    is_bull = regime == "BULL"
    tp_mult = ATR_TP_MULT_BULL if is_bull else ATR_TP_MULT

    daily_pnl      = 0.0
    peak_pnl       = 0.0
    open_pos: dict = {}
    stock_trades   = defaultdict(int)
    neutral_streak = 0
    sideways_day   = False
    post_target    = False
    profit_locked  = False
    cb_triggered   = False
    day_trades: list[dict] = []

    all_ts: set = set()
    for sym in SYMBOLS:
        df = _day_slice(all_data.get(sym, pd.DataFrame()), trade_date)
        all_ts.update(df.index.tolist())
    if not nifty_day.empty:
        all_ts.update(nifty_day.index.tolist())
    sorted_ts = sorted(all_ts)

    for ts in sorted_ts:
        t = ts.time()

        # EOD force-close at 15:14
        if t >= dtime(15, 14):
            for sym, pos in list(open_pos.items()):
                df  = _day_slice(all_data.get(sym, pd.DataFrame()), trade_date)
                sub = df[df.index >= ts]
                ep  = float(sub.iloc[0]["close"]) if not sub.empty else pos["entry"]
                pnl = (ep - pos["entry"]) * pos["qty"] if pos["side"] == "LONG" \
                      else (pos["entry"] - ep) * pos["qty"]
                daily_pnl += pnl
                peak_pnl   = max(peak_pnl, daily_pnl)
                day_trades.append(_trade_rec(trade_date, sym, pos, ep, ts, pnl, "EOD", regime))
            open_pos.clear()
            break

        if t >= dtime(15, 0):
            continue
        if dtime(12, 30) <= t < dtime(13, 0):
            continue

        # SL / TP check on open positions
        for sym in list(open_pos.keys()):
            pos = open_pos[sym]
            df  = _day_slice(all_data.get(sym, pd.DataFrame()), trade_date)
            row = df[df.index == ts]
            if row.empty:
                continue
            c    = row.iloc[0]
            side = pos["side"]
            hit_sl = (c["low"]  <= pos["sl"]) if side == "LONG" else (c["high"] >= pos["sl"])
            hit_tp = (c["high"] >= pos["tp"]) if side == "LONG" else (c["low"]  <= pos["tp"])
            if hit_tp or hit_sl:
                ep     = pos["tp"] if hit_tp else pos["sl"]
                reason = "TP" if hit_tp else "SL"
                pnl    = (ep - pos["entry"]) * pos["qty"] if side == "LONG" \
                         else (pos["entry"] - ep) * pos["qty"]
                daily_pnl += pnl
                peak_pnl   = max(peak_pnl, daily_pnl)
                stock_trades[sym] += 1
                day_trades.append(_trade_rec(trade_date, sym, pos, ep, ts, pnl, reason, regime))
                del open_pos[sym]

        # Profit-lock check
        if daily_pnl >= DAILY_TARGET:
            post_target = True
        if post_target and daily_pnl < peak_pnl - PROFIT_PULLBACK_RS:
            profit_locked = True

        # Circuit breaker
        if daily_pnl <= -(MAX_DAILY_LOSS * CAPITAL):
            cb_triggered = True

        if profit_locked or cb_triggered:
            for sym, pos in list(open_pos.items()):
                df  = _day_slice(all_data.get(sym, pd.DataFrame()), trade_date)
                sub = df[df.index >= ts]
                ep  = float(sub.iloc[0]["close"]) if not sub.empty else pos["entry"]
                pnl = (ep - pos["entry"]) * pos["qty"] if pos["side"] == "LONG" \
                      else (pos["entry"] - ep) * pos["qty"]
                daily_pnl += pnl
                tag = "PROFIT_LOCK" if profit_locked else "CB"
                day_trades.append(_trade_rec(trade_date, sym, pos, ep, ts, pnl, tag, regime))
            open_pos.clear()
            break

        # Sideways detection
        nr = nifty_day[nifty_day.index == ts]
        if not nr.empty:
            bar_ret = (nr.iloc[0]["close"] - nr.iloc[0]["open"]) / nr.iloc[0]["open"]
            neutral_streak = (neutral_streak + 1) if abs(bar_ret) < SIDEWAYS_NIFTY_THRESH else 0
            if neutral_streak >= SIDEWAYS_CONSECUTIVE_SCANS:
                sideways_day = True

        # Entry guards
        if sideways_day:                                            continue
        if NIFTY_WEAK_HARD_STOP and regime == "WEAK":              continue
        if post_target and POST_TARGET_BULL_ONLY and not is_bull:   continue
        if len(open_pos) >= MAX_OPEN_POSITIONS:                     continue

        # Entry scan
        for sym in SYMBOLS:
            if sym in open_pos:                                         continue
            if stock_trades[sym] >= MAX_TRADES_PER_STOCK_PER_DAY:      continue
            if len(open_pos) >= MAX_OPEN_POSITIONS:                     break

            df  = _day_slice(all_data.get(sym, pd.DataFrame()), trade_date)
            row = df[df.index == ts]
            if row.empty:
                continue
            c = row.iloc[0]

            hist = df[df.index <= ts].tail(20)
            if len(hist) < 5:
                continue
            atr = _atr(hist)
            if np.isnan(atr) or atr <= 0:
                continue

            closes   = hist["close"].values
            short_ma = closes[-3:].mean()
            long_ma  = closes[-10:].mean() if len(closes) >= 10 else closes.mean()
            rsi      = _simple_rsi(closes, 14)

            if regime == "BULL":
                ok = (short_ma > long_ma) and (rsi > 45) and (c["close"] > c["open"])
            elif regime == "NEUTRAL":
                ok = (short_ma > long_ma * 1.001) and (40 < rsi < 65)
            else:
                ok = False

            if not ok:
                continue

            entry = float(c["close"])
            side  = "LONG"
            sl, tp, rr = _compute_sl_tp(entry, atr, side, tp_mult)
            if rr < MIN_RR_RATIO:
                continue
            qty = _compute_qty(entry, sl)
            if qty <= 0:
                continue

            open_pos[sym] = {
                "entry":    entry,
                "entry_ts": ts,
                "side":     side,
                "sl":       sl,
                "tp":       tp,
                "qty":      qty,
            }

    wins   = [t for t in day_trades if t["pnl"] > 0]
    losses = [t for t in day_trades if t["pnl"] <= 0]
    return day_trades, {
        "date":          str(trade_date),
        "regime":        regime,
        "trades":        len(day_trades),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_pct":       round(len(wins) / len(day_trades) * 100, 1) if day_trades else 0,
        "day_pnl":       round(daily_pnl, 2),
        "target_hit":    daily_pnl >= DAILY_TARGET,
        "profit_locked": profit_locked,
        "cb_triggered":  cb_triggered,
        "avg_win":       round(float(np.mean([t["pnl"] for t in wins])),   2) if wins   else 0,
        "avg_loss":      round(float(np.mean([t["pnl"] for t in losses])), 2) if losses else 0,
    }


# -------------------------------------------------------------------
# MAIN ORCHESTRATOR
# -------------------------------------------------------------------

def run_backtest(
    source:   str        = "dhan",
    days:     int        = 60,
    start:    date | None = None,
    end:      date | None = None,
    interval: str        = "5",
) -> None:
    if end is None:
        end = date.today() - timedelta(days=1)
    if start is None:
        start = end - timedelta(days=days)

    print(f"\n{'='*64}")
    print(f"  BACKTEST  |  source={source.upper()}  |  {start} to {end}")
    print(f"  Symbols   : {len(SYMBOLS)}  |  Capital: Rs.{CAPITAL:,.0f}")
    print(f"  Target/day: Rs.{DAILY_TARGET:,.0f}  |  Max daily loss: {MAX_DAILY_LOSS*100:.1f}%")
    print(f"  Profit-lock pullback: Rs.{PROFIT_PULLBACK_RS}  |  Interval: {interval}m")
    print(f"{'='*64}\n")

    if source == "dhan":
        all_data  = fetch_dhan(SYMBOLS, start, end, interval=interval)
        nifty_key = NIFTY_DHAN
    else:
        all_data  = fetch_yfinance(SYMBOLS, start, end, interval=f"{interval}m")
        nifty_key = NIFTY_YF

    if not all_data:
        print("\n[ERROR] No data returned. Check credentials and network.")
        sys.exit(1)

    all_dates: set[date] = set()
    for df in all_data.values():
        for ts in df.index:
            d = ts.date()
            if start <= d <= end:
                all_dates.add(d)
    trading_dates = sorted(all_dates)
    print(f"\nReplaying {len(trading_dates)} trading days...\n")

    all_trades: list[dict]    = []
    all_summaries: list[dict] = []
    cum_pnl = 0.0

    for d in trading_dates:
        trades, summary = replay_day(d, all_data, nifty_key)
        cum_pnl              += summary["day_pnl"]
        summary["cum_pnl"]    = round(cum_pnl, 2)
        all_trades.extend(trades)
        all_summaries.append(summary)

        icon  = "[+]" if summary["day_pnl"] >= 0 else "[-]"
        flags = (
            " [TARGET]" if summary["target_hit"]    else ""
        ) + (
            " [LOCKED]" if summary["profit_locked"] else ""
        ) + (
            " [CB]"     if summary["cb_triggered"]  else ""
        )
        print(
            f"  {d}  {summary['regime']:<8}"
            f"  {summary['trades']:>3} trades"
            f"  {summary['win_pct']:>5.1f}% wins"
            f"  Day P&L: {summary['day_pnl']:>+9,.2f}"
            f"  Cum: {cum_pnl:>+11,.2f}  {icon}{flags}"
        )

    total_pnl    = sum(s["day_pnl"]  for s in all_summaries)
    total_trades = sum(s["trades"]   for s in all_summaries)
    total_wins   = sum(s["wins"]     for s in all_summaries)
    total_losses = sum(s["losses"]   for s in all_summaries)
    profit_days  = sum(1 for s in all_summaries if s["day_pnl"] >= 0)
    target_days  = sum(1 for s in all_summaries if s["target_hit"])
    win_pnls     = [t["pnl"] for t in all_trades if t["pnl"] >  0]
    loss_pnls    = [t["pnl"] for t in all_trades if t["pnl"] <= 0]
    avg_win      = float(np.mean(win_pnls))  if win_pnls  else 0.0
    avg_loss     = float(np.mean(loss_pnls)) if loss_pnls else 0.0
    gross_profit = sum(win_pnls)
    gross_loss   = abs(sum(loss_pnls))
    pf           = gross_profit / gross_loss if gross_loss else float("inf")
    win_rate     = total_wins / total_trades * 100 if total_trades else 0.0

    sym_pnl: dict[str, float] = defaultdict(float)
    for t in all_trades:
        sym_pnl[t["symbol"]] += t["pnl"]
    top5    = sorted(sym_pnl.items(), key=lambda x: -x[1])[:5]
    bottom5 = sorted(sym_pnl.items(), key=lambda x:  x[1])[:5]

    sep    = "-" * 64
    report = (
        f"\n{'='*64}\n"
        f"BACKTEST REPORT  |  {start} to {end}  ({len(trading_dates)} trading days)\n"
        f"{'='*64}\n"
        f"Data Source        : {source.upper()}\n"
        f"Candle Interval    : {interval}m\n"
        f"Capital            : Rs.{CAPITAL:>14,.0f}\n"
        f"Daily Target       : Rs.{DAILY_TARGET:>14,.0f}\n"
        f"Profit-lock        : Rs.{PROFIT_PULLBACK_RS} pullback after target\n"
        f"\n{sep}\nP & L SUMMARY\n{sep}\n"
        f"Total Net P&L      : Rs.{total_pnl:>+14,.2f}\n"
        f"Profitable Days    : {profit_days:>4} / {len(trading_dates)}\n"
        f"Target-hit Days    : {target_days:>4} / {len(trading_dates)}\n"
        f"Best Day           : Rs.{max(s['day_pnl'] for s in all_summaries):>+14,.2f}\n"
        f"Worst Day          : Rs.{min(s['day_pnl'] for s in all_summaries):>+14,.2f}\n"
        f"\n{sep}\nTRADE STATISTICS\n{sep}\n"
        f"Total Trades       : {total_trades:>6}\n"
        f"Win Rate           : {win_rate:>5.1f}%  ({total_wins}W / {total_losses}L)\n"
        f"Avg Winning Trade  : Rs.{avg_win:>+12,.2f}\n"
        f"Avg Losing Trade   : Rs.{avg_loss:>+12,.2f}\n"
        f"Gross Profit       : Rs.{gross_profit:>12,.2f}\n"
        f"Gross Loss         : Rs.{gross_loss:>12,.2f}\n"
        f"Profit Factor      : {pf:>10.2f}\n"
        f"\n{sep}\nTOP 5 STOCKS\n{sep}\n"
        f"  {'Symbol':<14}  {'Net P&L':>12}\n"
    )
    for sym, pnl in top5:
        report += f"  {sym:<14}  Rs.{pnl:>+10,.2f}\n"
    report += f"\n{sep}\nBOTTOM 5 STOCKS\n{sep}\n  {'Symbol':<14}  {'Net P&L':>12}\n"
    for sym, pnl in bottom5:
        report += f"  {sym:<14}  Rs.{pnl:>+10,.2f}\n"
    report += f"{'='*64}\n"

    print(report)

    out = _REPO_ROOT
    trades_df = pd.DataFrame(all_trades)
    if not trades_df.empty:
        p = out / "backtest_trades.csv"
        trades_df.to_csv(p, index=False)
        print(f"  OK {p}")

    summary_df = pd.DataFrame(all_summaries)
    p = out / "backtest_summary.csv"
    summary_df.to_csv(p, index=False)
    print(f"  OK {p}")

    # FIX: always write as UTF-8 to avoid cp1252 crash on Windows
    p = out / "backtest_report.txt"
    p.write_text(report, encoding="utf-8")
    print(f"  OK {p}\n")


# -------------------------------------------------------------------
# CLI
# -------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Real-data backtest -- dhan_xgb_bot_v2_UI"
    )
    ap.add_argument("--source",   choices=["dhan", "yfinance"], default="dhan")
    ap.add_argument("--days",     type=int, default=60)
    ap.add_argument("--start",    type=str, default=None,
                    help="Start date YYYY-MM-DD  (overrides --days)")
    ap.add_argument("--end",      type=str, default=None,
                    help="End date YYYY-MM-DD  (default: yesterday)")
    ap.add_argument("--interval", type=str, default="5",
                    help="Candle interval in minutes: 1 5 15 25 60  (default: 5)")
    args = ap.parse_args()

    run_backtest(
        source   = args.source,
        days     = args.days,
        start    = date.fromisoformat(args.start) if args.start else None,
        end      = date.fromisoformat(args.end)   if args.end   else None,
        interval = args.interval,
    )
