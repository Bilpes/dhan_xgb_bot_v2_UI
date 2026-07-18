"""
backtest.py  —  Real-data backtest for dhan_xgb_bot_v2_UI
================================================================
Plugs yfinance (free, no creds) or Dhan historical API (real NSE
tick data) into the existing signal + trade engine for a true
bar-by-bar replay.

Usage
-----
# Quick run with yfinance (last 30 days, 5-min candles):
    python backtest.py

# Specify source / date range:
    python backtest.py --source yfinance --days 30
    python backtest.py --source yfinance --days 30 --interval 5m
    python backtest.py --source dhan     --days 30

# Single specific day:
    python backtest.py --source yfinance --start 2026-06-09 --end 2026-06-09

Output
------
  backtest_trades.csv   — every trade: symbol, entry time, entry px,
                          exit time, exit px, qty, P&L, reason
  backtest_summary.csv  — day-by-day summary
  backtest_report.txt   — human-readable report printed + saved

Requirements
------------
  pip install yfinance pandas numpy openpyxl
  (dhanhq already in requirements.txt for Dhan source)

Environment variables (Dhan source only)
-----------------------------------------
  DHAN_CLIENT_ID
  DHAN_ACCESS_TOKEN
================================================================
"""

from __future__ import annotations
import argparse, os, sys, warnings, json
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────────
# Import existing bot config & modules
# ─────────────────────────────────────────────────────────────────────
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
    # Fallback: read config/config.py directly if it lives in root
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

# ── Load watchlist symbols ──────────────────────────────────────────
_WL_PATH = os.path.join(os.path.dirname(__file__), "watchlist.json")
with open(_WL_PATH) as _f:
    _wl = json.load(_f)

# watchlist.json stores a list of dicts with "symbol" key
if isinstance(_wl, list):
    SYMBOLS: list[str] = [s["symbol"] for s in _wl]
elif "stocks" in _wl:
    SYMBOLS = [s["symbol"] for s in _wl["stocks"]]
else:
    # Fallback: flat list of strings or dicts
    SYMBOLS = [s if isinstance(s, str) else s.get("symbol", "") for s in _wl.values()]
SYMBOLS = [s for s in SYMBOLS if s]

# yfinance Nifty ticker / Dhan security key
NIFTY_YF   = "^NSEI"
NIFTY_DHAN = "__NIFTY__"

# ─────────────────────────────────────────────────────────────────────
# Dhan security ID map  (extend from api-scrip-master.csv as needed)
# Download master: https://images.dhan.co/api-data/api-scrip-master.csv
# ─────────────────────────────────────────────────────────────────────
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


# ═════════════════════════════════════════════════════════════════════
# DATA LAYER — yfinance
# ═════════════════════════════════════════════════════════════════════

def fetch_yfinance(
    symbols: list[str],
    start: date,
    end: date,
    interval: str = "5m",
) -> dict[str, pd.DataFrame]:
    """
    Fetch OHLCV from yfinance for NSE stocks + Nifty index.

    Notes:
      • 5m / 15m data: available only for the last 60 days (yfinance limit).
      • 1d data: available for years; use for longer backtests.
      • Returns dict keyed by plain symbol (no .NS suffix).
    """
    import yfinance as yf

    all_syms = symbols + [NIFTY_YF]
    result: dict[str, pd.DataFrame] = {}

    print(f"\n[yfinance] Fetching {len(all_syms)} tickers  "
          f"{start} → {end}  interval={interval}")

    tickers_ns = {
        sym: (sym if sym.startswith("^") else f"{sym}.NS")
        for sym in all_syms
    }

    # Batch download is faster
    raw = yf.download(
        tickers=list(tickers_ns.values()),
        start=str(start),
        end=str(end + timedelta(days=1)),
        interval=interval,
        progress=False,
        auto_adjust=True,
        group_by="ticker",
    )

    for sym, ticker in tickers_ns.items():
        try:
            if len(tickers_ns) == 1:
                df = raw.copy()
            else:
                df = raw[ticker].copy() if ticker in raw.columns.get_level_values(0) else pd.DataFrame()

            if df is None or df.empty:
                print(f"  ⚠  {sym}: no data returned")
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
# DATA LAYER — Dhan historical API
# ═════════════════════════════════════════════════════════════════════

def fetch_dhan(
    symbols: list[str],
    start: date,
    end: date,
    interval: str = "5",
) -> dict[str, pd.DataFrame]:
    """
    Fetch OHLCV from Dhan historical minute-chart API.

    Env vars required:
        DHAN_CLIENT_ID
        DHAN_ACCESS_TOKEN

    interval: "1" | "5" | "15" | "25" | "60" minutes
    """
    from dhanhq import dhanhq

    client_id    = os.environ.get("DHAN_CLIENT_ID", "")
    access_token = os.environ.get("DHAN_ACCESS_TOKEN", "")
    if not client_id or not access_token:
        raise EnvironmentError(
            "Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN environment variables."
        )

    dhan   = dhanhq(client_id, access_token)
    result: dict[str, pd.DataFrame] = {}
    all_syms = symbols + [NIFTY_DHAN]

    print(f"\n[Dhan API] Fetching {len(all_syms)} tickers  "
          f"{start} → {end}  interval={interval}m")

    for sym in all_syms:
        sec_id = DHAN_ID.get(sym)
        if not sec_id:
            print(f"  ⚠  {sym}: no Dhan security_id in DHAN_ID map — skipping")
            continue

        is_index = sym == NIFTY_DHAN
        try:
            resp = dhan.historical_minute_charts(
                symbol           = "NIFTY" if is_index else sym,
                exchange_segment = "IDX_I" if is_index else "NSE_EQ",
                instrument_type  = "INDEX" if is_index else "EQUITY",
                expiry_code      = 0,
                from_date        = str(start),
                to_date          = str(end),
            )
            data = resp.get("data", {})
            df   = pd.DataFrame({
                "open":      data.get("open",   []),
                "high":      data.get("high",   []),
                "low":       data.get("low",    []),
                "close":     data.get("close",  []),
                "volume":    data.get("volume", []),
                "timestamp": data.get("start_Time", []),
            })
            if df.empty:
                print(f"  ⚠  {sym}: empty response")
                continue

            df.index = (
                pd.to_datetime(df["timestamp"])
                  .dt.tz_localize("Asia/Kolkata")
            )
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
    """Return candles for a single calendar date between 09:15 and 15:30 IST."""
    if df.empty:
        return df
    mask = df.index.normalize() == pd.Timestamp(d, tz="Asia/Kolkata")
    return df.loc[mask].between_time("09:15", "15:30")


def _get_regime(nifty_day: pd.DataFrame) -> tuple[str, float]:
    """Derive intraday Nifty regime from day's candles."""
    if nifty_day.empty:
        return "NEUTRAL", 0.0
    ret = (nifty_day.iloc[-1]["close"] - nifty_day.iloc[0]["open"]) / nifty_day.iloc[0]["open"]
    if   ret >  SIDEWAYS_NIFTY_THRESH: return "BULL",    ret
    elif ret < -SIDEWAYS_NIFTY_THRESH: return "WEAK",    ret
    else:                               return "NEUTRAL", ret


def _atr(df: pd.DataFrame, period: int = 14) -> float:
    """True-range ATR from OHLCV DataFrame."""
    h, l, c = df["high"], df["low"], df["close"]
    pc  = c.shift(1)
    tr  = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    val = tr.rolling(period).mean().iloc[-1]
    return float(val) if not np.isnan(val) else float(tr.mean())


def _simple_rsi(closes: np.ndarray, period: int = 14) -> float:
    """Wilder RSI from a 1-D array of closes."""
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g  = gains[-period:].mean()
    avg_l  = losses[-period:].mean()
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def _compute_sl_tp(
    entry: float,
    atr: float,
    side: str,
    tp_mult: float,
) -> tuple[float, float, float]:
    """Return (sl, tp, rr) matching live engine logic."""
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
    pts       = abs(entry - sl)
    if pts <= 0:
        return 0
    risk_qty  = int((RISK_PER_TRADE * CAPITAL) / pts)
    slot_qty  = int((CAPITAL / MAX_OPEN_POSITIONS) / entry)
    return max(min(risk_qty, slot_qty), 0)


def _trade_rec(
    trade_date: date,
    sym: str,
    pos: dict,
    exit_px: float,
    exit_ts,
    pnl: float,
    reason: str,
    regime: str,
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
    Replay a single trading day bar-by-bar using real OHLCV candles.

    Returns
    -------
    trades  : list of trade dicts
    summary : day-level stats dict
    """
    nifty_day = _day_slice(all_data.get(nifty_key, pd.DataFrame()), trade_date)
    regime, _ = _get_regime(nifty_day)

    is_bull  = regime == "BULL"
    tp_mult  = ATR_TP_MULT_BULL if is_bull else ATR_TP_MULT

    # ── Day state ─────────────────────────────────────────────────────
    daily_pnl         = 0.0
    peak_pnl          = 0.0
    open_pos: dict    = {}
    stock_trades      = defaultdict(int)
    neutral_streak    = 0
    sideways_day      = False
    post_target       = False
    profit_locked     = False
    cb_triggered      = False
    day_trades: list  = []

    # Collect all unique bar timestamps across all symbols for this day
    all_ts: set = set()
    for sym in SYMBOLS:
        df = _day_slice(all_data.get(sym, pd.DataFrame()), trade_date)
        all_ts.update(df.index.tolist())
    if nifty_day is not None:
        all_ts.update(nifty_day.index.tolist())
    sorted_ts = sorted(all_ts)

    for ts in sorted_ts:
        t = ts.time()

        # ── EOD close-out at 15:14 ──────────────────────────────────
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

        # Skip after 15:00 (no new entries)
        if t >= dtime(15, 0):
            continue

        # Skip lunch 12:30–13:00
        if dtime(12, 30) <= t < dtime(13, 0):
            continue

        # ── Check SL / TP on open positions ─────────────────────────
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
        if post_target and (daily_pnl < peak_pnl - PROFIT_PULLBACK_RS):
            profit_locked = True

        # ── Circuit breaker ──────────────────────────────────────────
        if daily_pnl <= -(MAX_DAILY_LOSS * CAPITAL):
            cb_triggered = True

        if profit_locked or cb_triggered:
            # Force-close remaining positions
            for sym, pos in list(open_pos.items()):
                df  = _day_slice(all_data.get(sym, pd.DataFrame()), trade_date)
                sub = df[df.index >= ts]
                ep  = float(sub.iloc[0]["close"]) if not sub.empty else pos["entry"]
                pnl = (ep - pos["entry"]) * pos["qty"] if pos["side"] == "LONG" \
                      else (pos["entry"] - ep) * pos["qty"]
                daily_pnl += pnl
                day_trades.append(
                    _trade_rec(trade_date, sym, pos, ep, ts, pnl,
                               "PROFIT_LOCK" if profit_locked else "CB", regime)
                )
            open_pos.clear()
            break

        # ── Guards ───────────────────────────────────────────────────
        # Sideways detection via Nifty bar return
        nr = nifty_day[nifty_day.index == ts]
        if not nr.empty:
            bar_ret = (nr.iloc[0]["close"] - nr.iloc[0]["open"]) / nr.iloc[0]["open"]
            neutral_streak = neutral_streak + 1 if abs(bar_ret) < SIDEWAYS_NIFTY_THRESH else 0
            if neutral_streak >= SIDEWAYS_CONSECUTIVE_SCANS:
                sideways_day = True

        if sideways_day:
            continue
        if NIFTY_WEAK_HARD_STOP and regime == "WEAK":
            continue
        if post_target and POST_TARGET_BULL_ONLY and not is_bull:
            continue
        if len(open_pos) >= MAX_OPEN_POSITIONS:
            continue

        # ── Entry scan ───────────────────────────────────────────────
        for sym in SYMBOLS:
            if sym in open_pos:
                continue
            if stock_trades[sym] >= MAX_TRADES_PER_STOCK_PER_DAY:
                continue
            if len(open_pos) >= MAX_OPEN_POSITIONS:
                break

            df  = _day_slice(all_data.get(sym, pd.DataFrame()), trade_date)
            row = df[df.index == ts]
            if row.empty:
                continue
            c = row.iloc[0]

            # ATR from last 20 candles
            hist = df[df.index <= ts].tail(20)
            if len(hist) < 5:
                continue
            atr = _atr(hist)
            if np.isnan(atr) or atr <= 0:
                continue

            # ── Signal logic (mirrors live signal_engine heuristics) ──
            closes = hist["close"].values
            if len(closes) < 5:
                continue
            short_ma = closes[-3:].mean()
            long_ma  = closes[-10:].mean() if len(closes) >= 10 else closes.mean()
            rsi      = _simple_rsi(closes, 14)

            if regime == "BULL":
                ok = (short_ma > long_ma) and (rsi > 45) and (c["close"] > c["open"])
            elif regime == "NEUTRAL":
                ok = (short_ma > long_ma * 1.001) and (40 < rsi < 65)
            else:
                ok = False   # WEAK → NIFTY_WEAK_HARD_STOP already blocked above

            if not ok:
                continue

            entry = float(c["close"])
            side  = "LONG"   # extend to SHORT when short-selling enabled
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
    summary = {
        "date":           str(trade_date),
        "regime":         regime,
        "trades":         len(day_trades),
        "wins":           len(wins),
        "losses":         len(losses),
        "win_pct":        round(len(wins) / len(day_trades) * 100, 1) if day_trades else 0,
        "day_pnl":        round(daily_pnl, 2),
        "target_hit":     daily_pnl >= DAILY_TARGET,
        "profit_locked":  profit_locked,
        "cb_triggered":   cb_triggered,
        "avg_win":        round(np.mean([t["pnl"] for t in wins]),   2) if wins   else 0,
        "avg_loss":       round(np.mean([t["pnl"] for t in losses]), 2) if losses else 0,
    }
    return day_trades, summary


# ═════════════════════════════════════════════════════════════════════
# MAIN — orchestrate fetch + replay + report
# ═════════════════════════════════════════════════════════════════════

def run_backtest(
    source:   str  = "yfinance",
    days:     int  = 30,
    start:    date | None = None,
    end:      date | None = None,
    interval: str  = "5m",
) -> None:
    # ── Date range ────────────────────────────────────────────────────
    if end is None:
        end = date.today() - timedelta(days=1)
    if start is None:
        start = end - timedelta(days=days)

    print(f"\n{'='*60}")
    print(f"  BACKTEST  |  source={source}  |  {start} → {end}")
    print(f"  Symbols   : {len(SYMBOLS)}  |  Capital: ₹{CAPITAL:,.0f}")
    print(f"  Target/day: ₹{DAILY_TARGET}  |  Max Loss: {MAX_DAILY_LOSS*100:.1f}%")
    print(f"{'='*60}\n")

    # ── Fetch data ────────────────────────────────────────────────────
    if source == "dhan":
        all_data = fetch_dhan(SYMBOLS, start, end, interval=interval.replace("m", ""))
        nifty_key = NIFTY_DHAN
    else:
        all_data = fetch_yfinance(SYMBOLS, start, end, interval=interval)
        nifty_key = NIFTY_YF

    if not all_data:
        print("\n[ERROR] No data fetched. Check credentials / internet / symbol list.")
        sys.exit(1)

    # ── Get list of unique trading dates ─────────────────────────────
    all_dates: set[date] = set()
    for df in all_data.values():
        for ts in df.index:
            d = ts.date()
            if start <= d <= end:
                all_dates.add(d)
    trading_dates = sorted(all_dates)
    print(f"\nFound {len(trading_dates)} trading days to replay.\n")

    # ── Replay each day ───────────────────────────────────────────────
    all_trades:   list[dict] = []
    all_summaries: list[dict] = []
    cumulative_pnl = 0.0

    for d in trading_dates:
        trades, summary = replay_day(d, all_data, nifty_key)
        cumulative_pnl += summary["day_pnl"]
        summary["cum_pnl"] = round(cumulative_pnl, 2)
        all_trades.extend(trades)
        all_summaries.append(summary)

        icon = "🟢" if summary["day_pnl"] >= 0 else "🔴"
        lock = " [LOCKED]" if summary["profit_locked"] else ""
        cb   = " [CB]"     if summary["cb_triggered"]  else ""
        print(
            f"  {d}  {summary['regime']:<8}  "
            f"{summary['trades']:>3} trades  "
            f"{summary['win_pct']:>5.1f}% wins  "
            f"Day P&L: {summary['day_pnl']:>+9,.2f}  "
            f"Cum: {cumulative_pnl:>+10,.2f}  "
            f"{icon}{lock}{cb}"
        )

    # ── Aggregate stats ───────────────────────────────────────────────
    total_pnl    = sum(s["day_pnl"]  for s in all_summaries)
    total_trades = sum(s["trades"]   for s in all_summaries)
    total_wins   = sum(s["wins"]     for s in all_summaries)
    total_losses = sum(s["losses"]   for s in all_summaries)
    profit_days  = sum(1 for s in all_summaries if s["day_pnl"] >= 0)
    target_days  = sum(1 for s in all_summaries if s["target_hit"])
    win_pnls     = [t["pnl"] for t in all_trades if t["pnl"] > 0]
    loss_pnls    = [t["pnl"] for t in all_trades if t["pnl"] <= 0]
    avg_win      = np.mean(win_pnls)  if win_pnls  else 0.0
    avg_loss     = np.mean(loss_pnls) if loss_pnls else 0.0
    profit_factor= abs(sum(win_pnls) / sum(loss_pnls)) if sum(loss_pnls) != 0 else float("inf")
    win_rate     = total_wins / total_trades * 100 if total_trades else 0.0

    # Per-symbol P&L
    sym_pnl: dict[str, float] = defaultdict(float)
    for t in all_trades:
        sym_pnl[t["symbol"]] += t["pnl"]
    top5    = sorted(sym_pnl.items(), key=lambda x: x[1], reverse=True)[:5]
    bottom5 = sorted(sym_pnl.items(), key=lambda x: x[1])[:5]

    report = f"""
{'='*60}
BACKTEST REPORT  |  {start} → {end}  ({len(trading_dates)} days)
{'='*60}
Capital            : ₹{CAPITAL:>12,.0f}
Daily Target       : ₹{DAILY_TARGET:>12,.0f}
Data Source        : {source}
Interval           : {interval}

── P&L ─────────────────────────────────────────────────────
Total Net P&L      : ₹{total_pnl:>+12,.2f}
Profitable Days    : {profit_days:>3} / {len(trading_dates)}
Target Hit Days    : {target_days:>3} / {len(trading_dates)}
Best Day           : ₹{max(s['day_pnl'] for s in all_summaries):>+12,.2f}
Worst Day          : ₹{min(s['day_pnl'] for s in all_summaries):>+12,.2f}

── Trades ──────────────────────────────────────────────────
Total Trades       : {total_trades:>6}
Win Rate           : {win_rate:>5.1f}%  ({total_wins}W / {total_losses}L)
Avg Winning Trade  : ₹{avg_win:>+10,.2f}
Avg Losing Trade   : ₹{avg_loss:>+10,.2f}
Profit Factor      : {profit_factor:>8.2f}

── Top 5 Stocks (by P&L) ───────────────────────────────────
{'  Symbol':<16} {'P&L':>12}
"""
    for sym, pnl in top5:
        report += f"  {sym:<14} ₹{pnl:>+10,.2f}\n"
    report += f"\n── Bottom 5 Stocks ─────────────────────────────────────\n"
    report += f"{'  Symbol':<16} {'P&L':>12}\n"
    for sym, pnl in bottom5:
        report += f"  {sym:<14} ₹{pnl:>+10,.2f}\n"
    report += f"\n{'='*60}\n"

    print(report)

    # ── Save CSV outputs ──────────────────────────────────────────────
    trades_df = pd.DataFrame(all_trades)
    if not trades_df.empty:
        trades_df.to_csv("backtest_trades.csv", index=False)
        print("  Saved → backtest_trades.csv")

    summary_df = pd.DataFrame(all_summaries)
    summary_df.to_csv("backtest_summary.csv", index=False)
    print("  Saved → backtest_summary.csv")

    with open("backtest_report.txt", "w") as f:
        f.write(report)
    print("  Saved → backtest_report.txt\n")


# ═════════════════════════════════════════════════════════════════════
# CLI entry point
# ═════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Real-data backtest for dhan_xgb_bot_v2_UI"
    )
    parser.add_argument(
        "--source", choices=["yfinance", "dhan"], default="yfinance",
        help="Data source: 'yfinance' (free, no creds) or 'dhan' (real NSE data)"
    )
    parser.add_argument(
        "--days", type=int, default=30,
        help="Number of calendar days to look back (default: 30)"
    )
    parser.add_argument(
        "--start", type=str, default=None,
        help="Start date YYYY-MM-DD (overrides --days)"
    )
    parser.add_argument(
        "--end", type=str, default=None,
        help="End date YYYY-MM-DD (default: yesterday)"
    )
    parser.add_argument(
        "--interval", type=str, default="5m",
        help="Candle interval: 5m, 15m, 1h, 1d (yfinance) or 5, 15, 60 (dhan). Default: 5m"
    )
    args = parser.parse_args()

    start_d = date.fromisoformat(args.start) if args.start else None
    end_d   = date.fromisoformat(args.end)   if args.end   else None

    run_backtest(
        source   = args.source,
        days     = args.days,
        start    = start_d,
        end      = end_d,
        interval = args.interval,
    )
