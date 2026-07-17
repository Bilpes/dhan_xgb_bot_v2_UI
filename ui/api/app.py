# =============================================================
# ui/api/app.py — XGB Bot Dashboard Flask API  v4.2
#
# FIX-15 changelog:
#   1. /api/live_pnl  — new endpoint: polls Dhan LTP for all open
#      positions every call, returns per-position unrealised P&L
#      + cumulative daily_pnl (closed_net + unrealised)
#   2. /api/monthly   — new endpoint: reads trades.csv for entire
#      current month, groups by date, returns daily & cumulative
#      P&L for the Monthly P&L tab
#   3. _build_open_trades() now always fetches fresh LTP from Dhan
#      (or state.json if Dhan is unavailable) so the table never
#      shows stale prices
#   4. _build_state() includes live_pnl block so the JS header
#      Daily Net P&L is always closed_net + live unrealised
# =============================================================

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, date
from pathlib import Path
from typing import Any

_HERE   = Path(__file__).resolve().parent
_UI_DIR = _HERE.parent
_ROOT   = _UI_DIR.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from flask import Flask, jsonify, send_file, request, abort
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv(dotenv_path=str(_ROOT / 'config' / '.env'))

try:
    from config.config import (
        WATCHLIST, SECTOR_MAP, CAPITAL,
        MARKET_OPEN, MARKET_CLOSE,
        TRADE_LOG, SIGNAL_LOG, LOG_FILE,
    )
    from bot.dhan_api import DhanBroker
    from bot.signal_engine import SignalEngine
    from bot.brokerage import calculate_charges
    _BOT_AVAILABLE = True
except Exception as e:
    logging.warning("Bot modules not fully available: %s", e)
    _BOT_AVAILABLE = False
    WATCHLIST  = {}
    SECTOR_MAP = {}
    CAPITAL    = 400_000
    TRADE_LOG  = str(_ROOT / 'logs' / 'trades.csv')
    SIGNAL_LOG = str(_ROOT / 'logs' / 'signal_scan.csv')
    LOG_FILE   = str(_ROOT / 'logs' / 'bot.log')

app = Flask(__name__, static_folder=str(_UI_DIR / 'dashboard'))
CORS(app)

log = logging.getLogger('dashboard_api')
logging.basicConfig(
    level  = logging.INFO,
    format = '%(asctime)s  %(levelname)-8s  %(name)s  %(message)s',
)

_broker: Any      = None
_engine: Any      = None
_broker_lock      = threading.Lock()
_cache: dict      = {}
CACHE_TTL         = 25
BOT_MODE          = os.getenv('BOT_MODE', 'paper').lower()
_STATE_FILE       = _ROOT / 'logs' / 'state.json'

TIER_MAP = {
    'ICICIBANK':'A','SBIN':'A','AXISBANK':'A','KOTAKBANK':'A','BAJFINANCE':'A',
    'RELIANCE':'A','SUNPHARMA':'A','LT':'A','HAL':'A','BEL':'A','TITAN':'A','CHOLAFIN':'A',
    'HDFCBANK':'B','DRREDDY':'B','CIPLA':'B','EICHERMOT':'B','TRENT':'B',
    'ETERNAL':'B','ADANIPORTS':'B','CGPOWER':'B','HAVELLS':'B',
}

SECTOR_COLORS = {
    'BANKING':'#00c4ff','FINANCE':'#7b61ff','ENERGY':'#ff9100',
    'AUTO':'#00e676','PHARMA':'#ff6b9d','INFRA':'#ffd740',
    'DEFENCE':'#f44336','CONSUMER':'#e91e8c','PORTS':'#26c6da',
}


# ── state.json helpers ────────────────────────────────────────

def _read_bot_state() -> dict:
    try:
        if _STATE_FILE.exists():
            with open(_STATE_FILE, encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        log.warning('state.json read error: %s', e)
    return {}


def _bot_is_running() -> bool:
    try:
        if _STATE_FILE.exists():
            return time.time() - _STATE_FILE.stat().st_mtime < 300
    except Exception:
        pass
    return False


# ── broker / engine singletons ───────────────────────────────

def _get_broker():
    global _broker
    if _broker is not None:
        return _broker
    with _broker_lock:
        if _broker is None and _BOT_AVAILABLE:
            try:
                _broker = DhanBroker()
                log.info('DhanBroker initialised.')
            except Exception as e:
                log.warning('DhanBroker init failed: %s', e)
                _broker = None
    return _broker


def _get_engine():
    global _engine
    if _engine is None and _BOT_AVAILABLE:
        try:
            from bot.signal_engine import SignalEngine
            _engine = SignalEngine()
        except Exception as e:
            log.warning('SignalEngine init failed: %s', e)
    return _engine


# ── CSV helpers ───────────────────────────────────────────────

def _read_csv_today(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    today = date.today().isoformat()
    rows = []
    try:
        with open(path, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                if row.get('timestamp', '').startswith(today):
                    rows.append(row)
    except Exception as e:
        log.warning('CSV read error %s: %s', path, e)
    return rows


def _read_csv_month(path: str) -> list[dict]:
    """Return all rows from trades.csv for the current calendar month."""
    if not os.path.exists(path):
        return []
    prefix = date.today().strftime('%Y-%m')   # e.g. '2026-07'
    rows = []
    try:
        with open(path, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                if row.get('timestamp', '').startswith(prefix):
                    rows.append(row)
    except Exception as e:
        log.warning('CSV month read error: %s', e)
    return rows


def _read_signal_log(path: str, limit: int = 50) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline='', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        return rows[-limit:]
    except Exception as e:
        log.warning('Signal log read error: %s', e)
    return []


def _is_market_open() -> bool:
    now = datetime.now()
    t = now.hour * 60 + now.minute
    open_t  = int(MARKET_OPEN.split(':')[0])  * 60 + int(MARKET_OPEN.split(':')[1])
    close_t = int(MARKET_CLOSE.split(':')[0]) * 60 + int(MARKET_CLOSE.split(':')[1])
    return open_t <= t <= close_t


def _safe_float(v, default=0.0):
    try: return float(v)
    except: return default

def _safe_int(v, default=0):
    try: return int(float(v))
    except: return default


# ── data builders ─────────────────────────────────────────────

def _build_closed_trades() -> list[dict]:
    raw = _read_csv_today(TRADE_LOG)
    result = []
    for r in raw:
        ts      = r.get('timestamp', '')
        symbol  = r.get('symbol', '')
        gross   = _safe_float(r.get('gross_pnl', 0))
        charges = _safe_float(r.get('total_charges', 0))
        net     = _safe_float(r.get('net_pnl', 0))
        result.append({
            'ts':      ts[11:16] if len(ts) > 15 else ts,
            'symbol':  symbol,
            'sector':  r.get('sector', SECTOR_MAP.get(symbol, 'UNKNOWN')),
            'side':    r.get('side', 'LONG'),
            'qty':     _safe_int(r.get('qty', 0)),
            'entry':   _safe_float(r.get('entry', 0)),
            'exit':    _safe_float(r.get('exit_price', 0)),
            'hold':    round(_safe_float(r.get('hold_minutes', 0))),
            'reason':  r.get('exit_reason', ''),
            'gross':   round(gross, 2),
            'charges': round(charges, 2),
            'net':     round(net, 2),
            'prob':    round(_safe_float(r.get('prob_up', 0)), 4),
            'rr':      round(_safe_float(r.get('rr', 0)), 3),
            'regime':  r.get('market_regime', ''),
            'breakdown': {
                'brokerage': round(_safe_float(r.get('brokerage', 0)), 4),
                'stt':       round(_safe_float(r.get('stt', 0)), 4),
                'etc':       round(_safe_float(r.get('etc', 0)), 4),
                'gst':       round(_safe_float(r.get('gst', 0)), 4),
                'total':     round(charges, 4),
                'breakeven': round(_safe_float(r.get('breakeven_sell', 0)), 2),
            },
            'trail_count':  _safe_int(r.get('trail_count', 0)),
            'candles_held': _safe_int(r.get('candles_held', 0)),
        })
    return result


def _fetch_live_ltps(open_trades: list[dict]) -> dict:
    """
    FIX-15: Fetch fresh LTP from Dhan for all open position symbols.
    Returns {symbol: ltp} dict. Falls back to entry price if unavailable.
    """
    if not open_trades:
        return {}
    broker = _get_broker()
    if broker is None:
        return {sym_d['symbol']: sym_d.get('ltp', sym_d.get('entry', 0))
                for sym_d in open_trades}
    # Build secId -> symbol map
    id_map = {str(t.get('secId', WATCHLIST.get(t['symbol'], ''))): t['symbol']
              for t in open_trades}
    try:
        prices = broker.get_ltp_batch(id_map)
        return {sym: _safe_float(prices.get(str(WATCHLIST.get(sym, '')), 0))
                for sym in [t['symbol'] for t in open_trades]}
    except Exception as e:
        log.warning('Live LTP fetch failed: %s', e)
        return {t['symbol']: t.get('ltp', t.get('entry', 0)) for t in open_trades}


def _build_open_trades() -> list[dict]:
    """
    FIX-15: Read positions from state.json, then refresh LTP from Dhan live.
    If state.json unavailable, fall back to Dhan positions API.
    """
    bot_state = _read_bot_state()
    if bot_state and 'open_trades' in bot_state:
        trades = bot_state['open_trades']
    else:
        trades = _build_open_trades_live()

    # Refresh LTPs from Dhan API
    live_ltps = _fetch_live_ltps(trades)
    for t in trades:
        sym = t['symbol']
        fresh_ltp = live_ltps.get(sym, t.get('ltp', t.get('entry', 0)))
        if fresh_ltp and fresh_ltp > 0:
            t['ltp'] = round(fresh_ltp, 2)
        entry = t.get('entry', 0)
        qty   = t.get('qty', 0)
        ltp   = t.get('ltp', entry)
        t['unrealisedPnl'] = round((ltp - entry) * qty, 2)
        t['unrealisedPct'] = round((ltp - entry) / entry * 100, 3) if entry else 0
    return trades


def _build_open_trades_live() -> list[dict]:
    broker = _get_broker()
    if broker is None:
        return []
    try:
        pos_df = broker.get_positions()
        if pos_df is None or pos_df.empty:
            return []
        result = []
        sym_col = next(
            (c for c in ['tradingSymbol', 'trading_symbol', 'symbol'] if c in pos_df.columns), None
        )
        if sym_col is None:
            return []
        for _, row in pos_df.iterrows():
            symbol = str(row.get(sym_col, '')).upper()
            qty    = _safe_int(row.get('netQty', row.get('quantity', 0)))
            if qty == 0:
                continue
            entry = _safe_float(row.get('averageTradedPrice', row.get('buyAvg', 0)))
            ltp   = _safe_float(row.get('lastTradedPrice', entry))
            result.append({
                'symbol':     symbol,
                'secId':      str(row.get('securityId', WATCHLIST.get(symbol, ''))),
                'side':       'LONG',
                'qty':        qty,
                'entry':      round(entry, 2),
                'ltp':        round(ltp, 2),
                'sl':         0, 'target': 0, 'trailSl': 0, 'trailCount': 0,
                'prob':       0, 'rr': 0, 'atr': 0,
                'openTime':   '', 'candles': 0,
                'sector':     SECTOR_MAP.get(symbol, 'UNKNOWN'),
                'regime':     '',
                'unrealisedPnl': round((ltp - entry) * qty, 2),
                'unrealisedPct': round((ltp - entry) / entry * 100, 3) if entry else 0,
            })
        return result
    except Exception as e:
        log.warning('get_positions failed: %s', e)
        return []


def _build_watchlist_data() -> list[dict]:
    broker = _get_broker()
    if broker is None or not WATCHLIST:
        return []
    try:
        id_map = {str(v): k for k, v in WATCHLIST.items()}
        prices = broker.get_ltp_batch(id_map)
    except Exception as e:
        log.warning('LTP batch failed: %s', e)
        prices = {}
    result = []
    for symbol, sec_id in WATCHLIST.items():
        ltp = _safe_float(prices.get(str(sec_id), 0))
        result.append({
            'symbol': symbol, 'secId': str(sec_id),
            'sector': SECTOR_MAP.get(symbol, 'UNKNOWN'),
            'tier':   TIER_MAP.get(symbol, 'B'),
            'ltp':    round(ltp, 2), 'chg': 0.0,
            'signal': 'HOLD', 'conf': 0.0,
            'sl': 0.0, 'target': 0.0, 'rr': 0.0,
        })
    return result


def _build_nifty_data() -> dict:
    broker  = _get_broker()
    nifty_sid = os.getenv('NIFTY50_SECURITY_ID', '13')
    result  = {'ltp': 0.0, 'chg': 0.0, 'atr': 0.0, 'rsi': 50.0, 'ema20': 'above', 'regime': 'neutral'}
    if broker is None:
        return result
    try:
        ltp = broker.get_ltp(nifty_sid, 'NIFTY50')
        result['ltp'] = round(_safe_float(ltp), 2)
    except Exception as e:
        log.warning('Nifty LTP failed: %s', e)
    return result


def _build_risk_state(closed_trades: list[dict]) -> dict:
    bot_state = _read_bot_state()
    if bot_state and 'risk' in bot_state:
        return bot_state['risk']
    total_net     = sum(t['net']     for t in closed_trades)
    total_gross   = sum(t['gross']   for t in closed_trades)
    total_charges = sum(t['charges'] for t in closed_trades)
    wins     = sum(1 for t in closed_trades if t['net'] > 0)
    losses   = sum(1 for t in closed_trades if t['net'] <= 0)
    total    = wins + losses
    win_rate = round(wins / total * 100, 1) if total else 0
    max_loss_lim = _safe_float(os.getenv('DAILY_LOSS_LIMIT', '0.04')) * CAPITAL
    consec = 0
    for t in reversed(closed_trades):
        if t['net'] <= 0: consec += 1
        else: break
    halted = (
        total_net < -max_loss_lim
        or (total >= 10 and wins / total < 0.30 and total_net < -CAPITAL * 0.02)
        or consec >= 3
    )
    return {
        'daily_net_pnl':   round(total_net, 2),
        'daily_gross_pnl': round(total_gross, 2),
        'total_charges':   round(total_charges, 2),
        'wins': wins, 'losses': losses, 'win_rate': win_rate,
        'loss_pct_of_limit': round(max(0, -total_net) / max_loss_lim * 100, 1) if max_loss_lim else 0,
        'halted': halted,
        'consecutive_losses': consec,
        'capital': CAPITAL,
        'max_daily_loss': round(max_loss_lim, 0),
    }


def _build_monthly_pnl() -> dict:
    """
    FIX-15: Aggregate trades.csv for current month.
    Returns:
      daily_rows   — [{date, gross, charges, net, trades, wins, losses}]
      monthly_totals — {gross, charges, net, trades, wins, losses, win_rate}
      cumulative   — running cumulative net P&L per day
    """
    raw = _read_csv_month(TRADE_LOG)
    by_date: dict = {}
    for r in raw:
        ts  = r.get('timestamp', '')[:10]
        net = _safe_float(r.get('net_pnl', 0))
        grp = by_date.setdefault(ts, {'gross': 0, 'charges': 0, 'net': 0,
                                       'trades': 0, 'wins': 0, 'losses': 0})
        grp['gross']   += _safe_float(r.get('gross_pnl', 0))
        grp['charges'] += _safe_float(r.get('total_charges', 0))
        grp['net']     += net
        grp['trades']  += 1
        if net > 0: grp['wins']   += 1
        else:       grp['losses'] += 1

    daily_rows = []
    cumulative = 0.0
    for d in sorted(by_date):
        g = by_date[d]
        cumulative += g['net']
        daily_rows.append({
            'date':       d,
            'gross':      round(g['gross'], 2),
            'charges':    round(g['charges'], 2),
            'net':        round(g['net'], 2),
            'trades':     g['trades'],
            'wins':       g['wins'],
            'losses':     g['losses'],
            'cumulative': round(cumulative, 2),
        })

    total_net     = sum(r['net']     for r in daily_rows)
    total_gross   = sum(r['gross']   for r in daily_rows)
    total_charges = sum(r['charges'] for r in daily_rows)
    total_trades  = sum(r['trades']  for r in daily_rows)
    total_wins    = sum(r['wins']    for r in daily_rows)
    total_losses  = sum(r['losses']  for r in daily_rows)

    return {
        'daily_rows':  daily_rows,
        'monthly_totals': {
            'gross':    round(total_gross, 2),
            'charges':  round(total_charges, 2),
            'net':      round(total_net, 2),
            'trades':   total_trades,
            'wins':     total_wins,
            'losses':   total_losses,
            'win_rate': round(total_wins / total_trades * 100, 1) if total_trades else 0,
        },
        'month': date.today().strftime('%B %Y'),
    }


def _build_live_pnl(open_trades: list[dict], closed_trades: list[dict]) -> dict:
    """
    FIX-15: Real-time P&L snapshot.
    closed_net + live unrealised = true daily P&L.
    """
    closed_net        = sum(t['net']           for t in closed_trades)
    closed_gross      = sum(t['gross']         for t in closed_trades)
    closed_charges    = sum(t['charges']       for t in closed_trades)
    unrealised        = sum(t.get('unrealisedPnl', 0) for t in open_trades)
    total_daily_pnl   = round(closed_net + unrealised, 2)
    return {
        'closed_net':      round(closed_net, 2),
        'closed_gross':    round(closed_gross, 2),
        'closed_charges':  round(closed_charges, 2),
        'unrealised':      round(unrealised, 2),
        'total_daily_pnl': total_daily_pnl,          # THIS is what the header shows
        'open_count':      len(open_trades),
        'closed_count':    len(closed_trades),
    }


def _build_state(force: bool = False) -> dict:
    now = time.time()
    if not force and _cache.get('ts', 0) + CACHE_TTL > now:
        return _cache.get('data', {})

    closed  = _build_closed_trades()
    risk    = _build_risk_state(closed)
    open_t  = _build_open_trades()   # FIX-15: fresh LTPs from Dhan
    wl      = _build_watchlist_data()
    nifty   = _build_nifty_data()
    signals = _read_signal_log(SIGNAL_LOG, limit=30)
    live_pnl = _build_live_pnl(open_t, closed)   # FIX-15

    alerts = []
    for t in reversed(closed[-8:]):
        icon = '🟢' if t['net'] > 0 else '🔴'
        alerts.append({
            'icon': icon,
            'text': f"<span class=\"alert-sym\">{t['symbol']}</span> {t['reason'].replace('_',' ')} "
                    f"→ exit ₹{t['exit']:.2f} · Net: "
                    f"{'+' if t['net']>=0 else ''}₹{t['net']:,.0f}",
            'time': t['ts'],
        })
    for t in open_t:
        unr = t.get('unrealisedPnl', 0)
        sign = '+' if unr >= 0 else ''
        alerts.append({
            'icon': '🔵',
            'text': f"<span class=\"alert-sym\">{t['symbol']}</span> OPEN "
                    f"· entry ₹{t['entry']:.2f} · qty={t['qty']} "
                    f"· Unrealised: {sign}₹{unr:,.0f}",
            'time': 'now',
        })
    if not alerts:
        alerts.append({'icon':'🤖','text':'Bot running — no trades yet today','time':''})

    data = {
        'timestamp':    datetime.now().isoformat(),
        'mode':         BOT_MODE,
        'halted':       risk.get('halted', False),
        'market_open':  _is_market_open(),
        'capital':      CAPITAL,
        'openTrades':   open_t,
        'closedTrades': closed,
        'watchlist':    wl,
        'nifty':        nifty,
        'risk':         risk,
        'live_pnl':     live_pnl,   # FIX-15: added
        'signals':      signals,
        'alerts':       alerts,
        'chargesFormula': [
            {'lbl': 'Brokerage',  'val': '0.03% per leg, capped ₹20'},
            {'lbl': 'STT (sell)', 'val': '0.1% of turnover (delivery)'},
            {'lbl': 'ETC (NSE)',  'val': '0.00297% of turnover'},
            {'lbl': 'SEBI fee',   'val': '₹1 per ₹1 Cr turnover'},
            {'lbl': 'GST',        'val': '18% on (Brokerage + ETC)'},
            {'lbl': 'Stamp Duty', 'val': '0.015% (buyer only)'},
            {'lbl': 'IPFT',       'val': '₹1 per ₹1 Cr (NSE)'},
        ],
    }
    _cache['ts']   = now
    _cache['data'] = data
    return data


# ── routes ────────────────────────────────────────────────────

@app.route('/')
def index():
    dashboard = _UI_DIR / 'dashboard' / 'index.html'
    if dashboard.exists():
        return send_file(str(dashboard))
    return '<h2>Dashboard not found.</h2>', 404

@app.route('/api/state')
def api_state():
    try:
        return jsonify(_build_state())
    except Exception as e:
        log.exception('api_state error')
        return jsonify({'error': str(e)}), 500

@app.route('/api/positions')
def api_positions():
    try: return jsonify(_build_open_trades())
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/closed')
def api_closed():
    try: return jsonify(_build_closed_trades())
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/watchlist')
def api_watchlist():
    try: return jsonify(_build_watchlist_data())
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/nifty')
def api_nifty():
    try: return jsonify(_build_nifty_data())
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/risk')
def api_risk():
    try:
        return jsonify(_build_risk_state(_build_closed_trades()))
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/signals')
def api_signals():
    try: return jsonify(_read_signal_log(SIGNAL_LOG, limit=50))
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/api/logs')
def api_logs():
    try:
        if not os.path.exists(LOG_FILE):
            return jsonify({'lines': []})
        with open(LOG_FILE, encoding='utf-8') as f:
            lines = f.readlines()
        return jsonify({'lines': [l.rstrip() for l in lines[-100:]]})
    except Exception as e: return jsonify({'error': str(e)}), 500


@app.route('/api/live_pnl')
def api_live_pnl():
    """
    FIX-15: Lightweight endpoint polled every 10s by the dashboard.
    Returns cumulative daily P&L = closed_net + live unrealised.
    Also returns per-position unrealised so the table stays fresh.
    """
    try:
        open_t  = _build_open_trades()      # refreshes LTPs from Dhan
        closed  = _build_closed_trades()
        live    = _build_live_pnl(open_t, closed)
        # Per-position snapshot for the open positions table
        positions = [{
            'symbol':        t['symbol'],
            'ltp':           t.get('ltp', 0),
            'unrealisedPnl': t.get('unrealisedPnl', 0),
            'unrealisedPct': t.get('unrealisedPct', 0),
        } for t in open_t]
        return jsonify({
            **live,
            'positions': positions,
            'timestamp': datetime.now().isoformat(),
        })
    except Exception as e:
        log.exception('api_live_pnl error')
        return jsonify({'error': str(e)}), 500


@app.route('/api/monthly')
def api_monthly():
    """
    FIX-15: Monthly P&L aggregation for the Monthly tab.
    """
    try:
        return jsonify(_build_monthly_pnl())
    except Exception as e:
        log.exception('api_monthly error')
        return jsonify({'error': str(e)}), 500


@app.route('/api/force_exit', methods=['POST'])
def api_force_exit():
    if BOT_MODE != 'live':
        return jsonify({'status': 'noop', 'reason': 'Not in live mode'}), 400
    return jsonify({'status': 'ok', 'message': 'Force exit signal sent'})


@app.route('/api/health')
def api_health():
    bot_state   = _read_bot_state()
    bot_running = _bot_is_running()
    broker_ready = bot_running and bool(bot_state.get('broker_ready', False))
    return jsonify({
        'status':        'ok',
        'bot_available': _BOT_AVAILABLE,
        'broker_ready':  broker_ready,
        'bot_running':   bot_running,
        'market_open':   _is_market_open(),
        'mode':          bot_state.get('mode', BOT_MODE),
        'halted':        bot_state.get('halted', False),
        'open_trades':   len(bot_state.get('open_trades', [])),
        'daily_pnl':     bot_state.get('daily_pnl', 0.0),
        'timestamp':     datetime.now().isoformat(),
    })


if __name__ == '__main__':
    port  = int(os.getenv('DASHBOARD_PORT', '5050'))
    host  = os.getenv('DASHBOARD_HOST', '0.0.0.0')
    debug = os.getenv('DASHBOARD_DEBUG', 'false').lower() == 'true'
    log.info('Starting XGB Bot Dashboard on http://%s:%d', host, port)
    app.run(host=host, port=port, debug=debug, use_reloader=False)
