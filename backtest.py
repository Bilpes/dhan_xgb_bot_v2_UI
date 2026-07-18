"""
backtest.py  —  Real-data backtest for dhan_xgb_bot_v2_UI
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
    python backtest.py --source yfinance --days 30   # fallback, no creds needed

Output files (written to repo root)
------------------------------------
  backtest_trades.csv   — every trade: symbol, entry/exit time & px,
                          qty, SL, TP, P&L, exit reason
  backtest_summary.csv  — day-by-day P&L, regime, win%, target hit
  backtest_report.txt   — full text report (also printed to console)

Requirements (all already in requirements.txt)
-----------------------------------------------
  dhanhq>=2.0.0   yfinance>=0.2.30   pandas   numpy
  python-dotenv>=1.0.0
================================================================
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings
import json
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ═════════════════════════════════════════════════════════════════════
# Load credentials from config/.env
# ═════════════════════════════════════════════════════════════════════

_REPO_ROOT = Path(__file__).resolve().parent
_ENV_FILE  = _REPO_ROOT / "config" / ".env"

try:
    from dotenv import load_dotenv
    if _ENV_FILE.exists():
        load_dotenv(_ENV_FILE)
        print(f"[.env] Loaded credentials from {_ENV_FILE}")
    else:
        print(f"[.env] config/.env not found — falling back to shell env vars")
except ImportError:
    print("[.env] python-dotenv not installed; reading env vars directly")


# ═════════════════════════════════════════════════════════════════════
# Import existing bot config
# ═════════════════════════════════════════════════════════════════════

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

# ── Load watchlist ─────────────────────────────────────────────────────

def _load_symbols() -> list[str]:
    """Try both watchlist.json locations used by the repo."""
    candidates = [
        _REPO_ROOT / "watchlist.json",
        _REPO_ROOT / "config" / "watchlist.json",
    ]
    for p in candidates:
        if p.exists():
            with open(p) as f:
                wl = json.load(f)
            if isinstance(wl, list):
                return [s["symbol"] if isinstance(s, dict) else s for s in wl]
            if "stocks" in wl:
                return [s["symbol"] for s in wl["stocks"]]
    raise FileNotFoundError("watchlist.json not found in root or config/")

SYMBOLS: list[str] = _load_symbols()

NIFTY_YF   = "^NSEI"
NIFTY_DHAN = "__NIFTY__"

# Dhan security ID map  (from api-scrip-master.csv)
# https://images.dhan.co/api-data/api-scrip-master.csv
DHAN_ID: dict[str, int] = {
    "RELIANCE":   2885,  "HDFCBANK":   1333,  "INFY":       1594,
    "TCS":        11536, "ICICIBANK":  4963,  "AXISBANK":   5900,
    "SBIN":       3045,  "TATAMOTORS": 3456,  "LT":         11483,
    "NTPC":       11630, "ONGC":       11975, "BEL":        383,
    "BAJFINANCE": 317,   "SUNPHARMA":  3351,  "COALINDIA":  20374,
    "HCLTECH":    1348,  "MARUTI":     10999, "WIPRO":      3787,
    "ADANIENT":   25,    "ADANIPORTS": 15083, "APOLLOHOSP": 157,
    "ASIANPAINT": 236,   "BAJAJFINSV": 16675, "BHARTIARTL": 10604,
    "BPCL":       526,   "BRITANNIA":  547,   "CIPLA":      694,
    "DIVISLAB":   10720, "DRREDDY":    881,   "EICHERMOT":  910,
    "GRASIM":     1232,  "HEROMOTOCO": 1348,  "HINDALCO":   1363,
    "HINDUNILVR": 1394,  "INDUSINDBK": 5258,  "ITC":        1660,
    "JSWSTEEL":   11723, "KOTAKBANK":  1922,  "M&M":        2031,
    "NESTLEIND":  17963, "POWERGRID":  14977, "SHREECEM":   3103,
    "TATACONSUM": 3432,  "TATASTEEL":  3499,  "TECHM":      13538,
    "TITAN":      3506,  "ULTRACEMCO": 11532, "UPL":        11287,
    NIFTY_DHAN:   13,
}

# Dhan API hard limit: 90 calendar days per request for intraday data
_DHAN_MAX_CHUNK_DAYS = 85


# ═════════════════════════════════════════════════════════════════════
# DATA LAYER — Dhan historical API
# ═════════════════════════════════════════════════════════════════════

def _dhan_fetch_one(
    dhan,
    sym: str,
    sec_id: int,
    from_date: date,
    to_date: date,
    interval: str,
) -> pd.DataFrame:
    """
    Fetch one symbol from Dhan, auto-chunking if the range exceeds
    _DHAN_MAX_CHUNK_DAYS (Dhan rejects intraday requests > 90 days).
    Returns a single concatenated OHLCV DataFrame indexed by IST timestamp.
    """
    is_index = sym == NIFTY_DHAN
    frames   = []

    chunk_start = from_date
    while chunk_start <= to_date:
        chunk_end = min(chunk_start + timedelta(days=_DHAN_MAX_CHUNK_DAYS - 1), to_date)
        try:
            resp = dhan.historical_minute_charts(
                symbol           = "NIFTY" if is_index else sym,
                exchange_segment = "IDX_I" if is_index else "NSE_EQ",
                instrument_type  = "INDEX" if is_index else "EQUITY",
                expiry_code      = 0,
                from_date        = str(chunk_start),
                to_date          = str(chunk_end),
            )
            data = resp.get("data", {})
            if data:
                df = pd.DataFrame({
                    "open":      data.get("open",       []),
                    "high":      data.get("high",       []),
                    "low":       data.get("low",        []),
                    "close":     data.get("close",      []),
                    "volume":    data.get("volume",     []),
                    "timestamp": data.get("start_Time", []),
                })
                if not df.empty:
                    df.index = (
                        pd.to_datetime(df["timestamp"])
                          .dt.tz_localize("Asia/Kolkata")
                    )
                    df = df[["open", "high", "low", "close", "volume"]].dropna()
                    frames.append(df)
        except Exception as exc:
            print(f"    chunk {chunk_start}→{chunk_end}: {exc}")

        chunk_start = chunk_end + timedelta(days=1)

    if not frames:
        return pd.DataFrame()
    result = pd.concat(frames).sort_index()
    result = result[~result.index.duplicated(keep="first")]
    return result


def fetch_dhan(
    symbols: list[str],
    start: date,
    end: date,
    interval: str = "5",
) -> dict[str, pd.DataFrame]:
    """
    Fetch OHLCV for all symbols + Nifty from Dhan historical API.

    Credentials are read from env vars (auto-loaded from config/.env):
        DHAN_CLIENT_ID
        DHAN_ACCESS_TOKEN

    interval: "1" | "5" | "15" | "25" | "60" minutes
    """
    from dhanhq import dhanhq

    client_id    = os.environ.get("DHAN_CLIENT_ID", "").strip()
    access_token = os.environ.get("DHAN_ACCESS_TOKEN", "").strip()

    if not client_id or not access_token:
        print("\n[ERROR] DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not set.")
        print(f"        Create  config/.env  using  config/.env.example  as template.")
        print(f"        File expected at: {_ENV_FILE}")
        sys.exit(1)

    dhan     = dhanhq(client_id, access_token)
    result: dict[str, pd.DataFrame] = {}
    all_syms = symbols + [NIFTY_DHAN]
    total    = len(all_syms)

    print(f"\n[Dhan API] Fetching {total} symbols  {start} → {end}  interval={interval}m")
    print(f"           Range = {(end - start).days} days  "
          f"(chunked in ≤{_DHAN_MAX_CHUNK_DAYS}-day blocks)\n")

    for i, sym in enumerate(all_syms, 1):
        sec_id = DHAN_ID.get(sym)
        if not sec_id:
            print(f"  [{i:>3}/{total}]  ⚠  {sym}: not in DHAN_ID map — skipping")
            continue

        df = _dhan_fetch_one(dhan, sym, sec_id, start, end, interval)
        if df.empty:
            print(f"  [{i:>3}/{total}]  ✗  {sym}: no data")
        else:
            result[sym] = df
            print(f"  [{i:>3}/{total}]  ✓  {sym}: {len(df):>5} candles")

    return result


# ═════════════════════════════════════════════════════════════════════
# DATA LAYER — yfinance (fallback / no-creds option)
# ═════════════════════════════════════════════════════════════════════

def fetch_yfinance(
    symbols: list[str],
    start: date,
    end: date,
    interval: str = "5m",
) -> dict[str, pd.DataFrame]:
    """
    Fetch OHLCV via yfinance (free, no credentials).
    NOTE: yfinance caps intraday data at last 60 days.
          Use --interval 1d for longer history.
    """
    import yfinance as yf

    all_syms   = symbols + [NIFTY_YF]
    tickers_ns = {sym: (sym if sym.startswith("^") else f"{sym}.NS") for sym in all_syms}
    result: dict[str, pd.DataFrame] = {}

    print(f"\n[yfinance] Fetching {len(all_syms)} tickers  {start} → {end}  interval={interval}")

    raw = yf.download(
        tickers   = list(tickers_ns.values()),
        start     = str(start),
        end       = str(end + timedelta(days=1)),
        interval  = interval,
        progress  = False,
        auto_adjust = True,
        group_by  = "ticker",
    )

    for sym, ticker in tickers_ns.items():
        try:
            df = raw[ticker].copy() if len(tickers_ns) > 1 else raw.copy()
            if df is None or df.empty:
                continue
            df.columns = [c.lower() for c in df.columns]
            df.index   = pd.to_datetime(df.index)
            if df.index.tz is None:
                df.index = df.index.tz_localize("Asia/Kolkata")
            else:
                df.index = df.index.tz_convert("Asia/Kolkata")
            df = df[["open", "high", "low", "close", "volume"]].dropna()
            result[sym] = df
            print(f"  ✓  {sym}: {len(df)} candles")
        except Exception as exc:
            print(f"  ✗  {sym}: {exc}")

    return result


# ═════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════

def _day_slice(df: pd.DataFrame, d: date) -> pd.DataFrame:
    if df.empty:
        return df
    mask = df.index.normalize() == pd.Timestamp(d, tz="Asia/Kolkata")
    return df.loc[mask].between_time("09:15", "15:30")


def _get_regime(nifty_day: pd.DataFrame) -> tuple[str, float]:
    if nifty_day.empty:
        return "NEUTRAL", 0.0
    ret = (nifty_day.iloc[-1]["close"] - nifty_day.iloc[0]["open"]) / nifty_day.iloc[0]["open"]
    if   ret >  SIDEWAYS_NIFTY_THRESH: return "BULL",    round(ret, 5)
    elif ret < -SIDEWAYS_NIFTY_THRESH: return "WEAK",    round(ret, 5)
    else:                               return "NEUTRAL", round(ret, 5)


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
    if avg_l == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_g / avg_l))


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


# ═════════════════════════════════════════════════════════════════════
# CORE — bar-by-bar replay for ONE trading day
# ═════════════════════════════════════════════════════════════════════

def replay_day(
    trade_date: date,
    all_data: dict[str, pd.DataFrame],
    nifty_key: str,
) -> tuple[list[dict], dict]:
    """
    Bar-by-bar replay of one NSE trading session using real OHLCV data.
    All guard rails from config.py (profit-lock, CB, sideways, WEAK stop)
    are applied identically to the live bot.
    """
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

    # Union of all bar timestamps for this day across all symbols
    all_ts: set = set()
    for sym in SYMBOLS:
        df = _day_slice(all_data.get(sym, pd.DataFrame()), trade_date)
        all_ts.update(df.index.tolist())
    if not nifty_day.empty:
        all_ts.update(nifty_day.index.tolist())
    sorted_ts = sorted(all_ts)

    for ts in sorted_ts:
        t = ts.time()

        # ── EOD force-close ──────────────────────────────────────────
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

        # Skip lunch
        if dtime(12, 30) <= t < dtime(13, 0):
            continue

        # ── SL / TP check on open positions ──────────────────────────
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

        # ── Profit-lock ──────────────────────────────────────────────
        if daily_pnl >= DAILY_TARGET:
            post_target = True
        if post_target and daily_pnl < peak_pnl - PROFIT_PULLBACK_RS:
            profit_locked = True

        # ── Circuit breaker ──────────────────────────────────────────
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

        # ── Sideways detection (real Nifty bar) ───────────────────────
        nr = nifty_day[nifty_day.index == ts]
        if not nr.empty:
            bar_ret = (nr.iloc[0]["close"] - nr.iloc[0]["open"]) / nr.iloc[0]["open"]
            neutral_streak = (neutral_streak + 1) if abs(bar_ret) < SIDEWAYS_NIFTY_THRESH else 0
            if neutral_streak >= SIDEWAYS_CONSECUTIVE_SCANS:
                sideways_day = True

        # ── Entry guards ──────────────────────────────────────────────
        if sideways_day:                                              continue
        if NIFTY_WEAK_HARD_STOP and regime == "WEAK":                continue
        if post_target and POST_TARGET_BULL_ONLY and not is_bull:     continue
        if len(open_pos) >= MAX_OPEN_POSITIONS:                       continue

        # ── Entry scan ──────────────────────────────────────────────
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

    # ── Day summary ───────────────────────────────────────────────────
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
        "avg_win":       round(np.mean([t["pnl"] for t in wins]),   2) if wins   else 0,
        "avg_loss":      round(np.mean([t["pnl"] for t in losses]), 2) if losses else 0,
    }


# ═════════════════════════════════════════════════════════════════════
# MAIN ORCHESTRATOR
# ═════════════════════════════════════════════════════════════════════

def run_backtest(
    source:   str        = "dhan",
    days:     int        = 60,
    start:    date|None  = None,
    end:      date|None  = None,
    interval: str        = "5",
) -> None:
    # Date range
    if end is None:
        end = date.today() - timedelta(days=1)
    if start is None:
        start = end - timedelta(days=days)

    print(f"\n{'='*64}")
    print(f"  BACKTEST  |  source={source.upper()}  |  {start} → {end}")
    print(f"  Symbols   : {len(SYMBOLS)}  |  Capital: ₹{CAPITAL:,.0f}")
    print(f"  Target/day: ₹{DAILY_TARGET:,.0f}  |  Max daily loss: {MAX_DAILY_LOSS*100:.1f}%")
    print(f"  Profit-lock pullback: ₹{PROFIT_PULLBACK_RS}  |  Interval: {interval}m")
    print(f"{'='*64}\n")

    # Fetch
    if source == "dhan":
        all_data  = fetch_dhan(SYMBOLS, start, end, interval=interval)
        nifty_key = NIFTY_DHAN
    else:
        all_data  = fetch_yfinance(SYMBOLS, start, end, interval=f"{interval}m")
        nifty_key = NIFTY_YF

    if not all_data:
        print("\n[ERROR] No data returned. Check credentials and network.")
        sys.exit(1)

    # Unique trading dates in the fetched data
    all_dates: set[date] = set()
    for df in all_data.values():
        for ts in df.index:
            d = ts.date()
            if start <= d <= end:
                all_dates.add(d)
    trading_dates = sorted(all_dates)
    print(f"\nReplaying {len(trading_dates)} trading days...\n")

    # Bar-by-bar replay
    all_trades: list[dict]   = []
    all_summaries: list[dict] = []
    cum_pnl = 0.0

    for d in trading_dates:
        trades, summary = replay_day(d, all_data, nifty_key)
        cum_pnl               += summary["day_pnl"]
        summary["cum_pnl"]     = round(cum_pnl, 2)
        all_trades.extend(trades)
        all_summaries.append(summary)

        icon  = "🟢" if summary["day_pnl"] >= 0 else "🔴"
        flags = (
            " [TARGET]✓" if summary["target_hit"]    else ""
        ) + (
            " [LOCKED]"  if summary["profit_locked"] else ""
        ) + (
            " [CB]⚠"    if summary["cb_triggered"]  else ""
        )
        print(
            f"  {d}  {summary['regime']:<8}"
            f"  {summary['trades']:>3} trades"
            f"  {summary['win_pct']:>5.1f}% wins"
            f"  Day P&L: {summary['day_pnl']:>+9,.2f}"
            f"  Cum: {cum_pnl:>+11,.2f}"
            f"  {icon}{flags}"
        )

    # Aggregate metrics
    total_pnl     = sum(s["day_pnl"]  for s in all_summaries)
    total_trades  = sum(s["trades"]   for s in all_summaries)
    total_wins    = sum(s["wins"]     for s in all_summaries)
    total_losses  = sum(s["losses"]   for s in all_summaries)
    profit_days   = sum(1 for s in all_summaries if s["day_pnl"] >= 0)
    target_days   = sum(1 for s in all_summaries if s["target_hit"])
    win_pnls      = [t["pnl"] for t in all_trades if t["pnl"] >  0]
    loss_pnls     = [t["pnl"] for t in all_trades if t["pnl"] <= 0]
    avg_win       = float(np.mean(win_pnls))  if win_pnls  else 0.0
    avg_loss      = float(np.mean(loss_pnls)) if loss_pnls else 0.0
    gross_profit  = sum(win_pnls)
    gross_loss    = abs(sum(loss_pnls))
    pf            = gross_profit / gross_loss if gross_loss else float("inf")
    win_rate      = total_wins / total_trades * 100 if total_trades else 0.0

    sym_pnl: dict[str, float] = defaultdict(float)
    for t in all_trades:
        sym_pnl[t["symbol"]] += t["pnl"]
    top5    = sorted(sym_pnl.items(), key=lambda x: -x[1])[:5]
    bottom5 = sorted(sym_pnl.items(), key=lambda x:  x[1])[:5]

    sep = "─" * 64
    report = f"""
{'='*64}
BACKTEST REPORT  |  {start} → {end}  ({len(trading_dates)} trading days)
{'='*64}
Data Source        : {source.upper()}
Candle Interval    : {interval}m
Capital            : ₹{CAPITAL:>14,.0f}
Daily Target       : ₹{DAILY_TARGET:>14,.0f}
Profit-lock        : ₹{PROFIT_PULLBACK_RS} pullback after target

{sep}
P & L SUMMARY
{sep}
Total Net P&L      : ₹{total_pnl:>+14,.2f}
Profitable Days    : {profit_days:>4} / {len(trading_dates)}
Target-hit Days    : {target_days:>4} / {len(trading_dates)}
Best Day           : ₹{max(s['day_pnl'] for s in all_summaries):>+14,.2f}
Worst Day          : ₹{min(s['day_pnl'] for s in all_summaries):>+14,.2f}

{sep}
TRADE STATISTICS
{sep}
Total Trades       : {total_trades:>6}
Win Rate           : {win_rate:>5.1f}%  ({total_wins}W / {total_losses}L)
Avg Winning Trade  : ₹{avg_win:>+12,.2f}
Avg Losing Trade   : ₹{avg_loss:>+12,.2f}
Gross Profit       : ₹{gross_profit:>12,.2f}
Gross Loss         : ₹{gross_loss:>12,.2f}
Profit Factor      : {pf:>10.2f}

{sep}
TOP 5 STOCKS
{sep}
  {'Symbol':<14}  {'Net P&L':>12}"""
    for sym, pnl in top5:
        report += f"\n  {sym:<14}  ₹{pnl:>+10,.2f}"
    report += f"\n\n{sep}\nBOTTOM 5 STOCKS\n{sep}\n  {'Symbol':<14}  {'Net P&L':>12}"
    for sym, pnl in bottom5:
        report += f"\n  {sym:<14}  ₹{pnl:>+10,.2f}"
    report += f"\n{'='*64}\n"

    print(report)

    # Save outputs
    out_dir = _REPO_ROOT
    trades_df = pd.DataFrame(all_trades)
    if not trades_df.empty:
        p = out_dir / "backtest_trades.csv"
        trades_df.to_csv(p, index=False)
        print(f"  ✓ Saved  {p}")

    summary_df = pd.DataFrame(all_summaries)
    p = out_dir / "backtest_summary.csv"
    summary_df.to_csv(p, index=False)
    print(f"  ✓ Saved  {p}")

    p = out_dir / "backtest_report.txt"
    p.write_text(report)
    print(f"  ✓ Saved  {p}\n")


# ═════════════════════════════════════════════════════════════════════
# CLI
# ═════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Real-data 60-day backtest — dhan_xgb_bot_v2_UI"
    )
    ap.add_argument(
        "--source", choices=["dhan", "yfinance"], default="dhan",
        help="Data source (default: dhan)"
    )
    ap.add_argument(
        "--days", type=int, default=60,
        help="Calendar days to look back (default: 60)"
    )
    ap.add_argument(
        "--start", type=str, default=None,
        help="Start date YYYY-MM-DD  (overrides --days)"
    )
    ap.add_argument(
        "--end", type=str, default=None,
        help="End date YYYY-MM-DD  (default: yesterday)"
    )
    ap.add_argument(
        "--interval", type=str, default="5",
        help="Candle interval in minutes: 1, 5, 15, 25, 60  (default: 5)"
    )
    args = ap.parse_args()

    run_backtest(
        source   = args.source,
        days     = args.days,
        start    = date.fromisoformat(args.start) if args.start else None,
        end      = date.fromisoformat(args.end)   if args.end   else None,
        interval = args.interval,
    )
