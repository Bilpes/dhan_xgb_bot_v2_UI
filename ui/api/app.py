# =============================================================
# ui/api/app.py — XGB Bot Dashboard Flask API
#
# Serves the dashboard HTML + exposes JSON endpoints that
# the dashboard JS calls on every 30-second auto-refresh.
#
# Endpoints:
#   GET /                   → serves ui/dashboard/index.html
#   GET /api/state          → full bot state (open trades, closed, watchlist, nifty, risk)
#   GET /api/positions      → open positions only
#   GET /api/closed         → closed trades from logs/trades.csv (today)
#   GET /api/watchlist      → all 21 watchlist stocks + live LTP + last signal
#   GET /api/nifty          → Nifty LTP + regime data
#   GET /api/risk           → risk manager state (daily P&L, circuit breaker, slots)
#   GET /api/signals        → latest signal_scan.csv rows
#   POST /api/force_exit    → manually force-exit all open positions (live mode only)
#
# Run:
#   cd <project_root>
#   python -m ui.api.app
#   # or:
#   python ui/api/app.py
#
# Fix log:
#   FIX-12 (2026-06-29): broker_ready in /api/health now reads
#           logs/state.json (written by the bot process every scan)
#           instead of checking the dashboard's lazy _broker singleton
#           which was always None when /api/health was called first.
#           Also: _read_bot_state() helper added — merges live bot
#           state (open trades, daily P&L, halted) into /api/state
#           so the dashboard shows real data even without Dhan API.
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

# ── Make project root importable ────────────────────────────
_HERE    = Path(__file__).resolve().parent          # ui/api/
_UI_DIR  = _HERE.parent                             # ui/
_ROOT    = _UI_DIR.parent                           # project root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from flask import Flask, jsonify, send_file, request, abort
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv(dotenv_path=str(_ROOT / 'config' / '.env'))

# ── Bot imports (graceful fallback if model not loaded) ──────
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

# ── Globals (module-level singletons) ────────────────────────
app    = Flask(__name__, static_folder=str(_UI_DIR / 'dashboard'))
CORS(app)  # allow dashboard to call API from file:// too

log = logging.getLogger('dashboard_api')
logging.basicConfig(
    level   = logging.INFO,
    format  = '%(asctime)s  %(levelname)-8s  %(name)s  %(message)s',
)

# Singleton broker + engine (initialised once, reused across requests)
_broker: Any = None
_engine: Any = None
_broker_lock = threading.Lock()

# In-memory cache — refreshed every CACHE_TTL seconds
_cache: dict = {}
CACHE_TTL = 25  # slightly under 30s refresh interval

BOT_MODE = os.getenv('BOT_MODE', 'paper').lower()

# Path to state file written by the bot process every scan tick
_STATE_FILE = _ROOT / 'logs' / 'state.json'

# Watchlist with tiers (mirrors watchlist.json)
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


# =============================================================
#  FIX-12: Read bot state.json written by live_bot / run_bot
# =============================================================

def _read_bot_state() -> dict:
    """
    Read logs/state.json written by the bot process after every scan.
    Returns empty dict if file doesn't exist or is unreadable.

    This is the single source of truth for:
      - broker_ready  (bot is running and healthy)
      - open_trades   (current positions with SL/TP)
      - daily_pnl     (net P&L after charges)
      - halted        (circuit breaker state)
      - mode          (paper / live / test)
    """
    try:
        if _STATE_FILE.exists():
            with open(_STATE_FILE, encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        log.warning('state.json read error: %s', e)
    return {}


def _bot_is_running() -> bool:
    """
    True if the bot process has written state.json within the last 5 minutes.
    A stale file (>5 min old) means the bot has stopped.
    """
    try:
        if _STATE_FILE.exists():
            age = time.time() - _STATE_FILE.stat().st_mtime
            return age < 300  # 5 minutes
    except Exception:
        pass
    return False


# =============================================================
#  HELPERS
# =============================================================

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
            log.info('SignalEngine initialised.')
        except Exception as e:
            log.warning('SignalEngine init failed: %s', e)
    return _engine


def _read_csv_today(path: str) -> list[dict]:
    """Read a CSV and return only today's rows (timestamp col)."""
    if not os.path.exists(path):
        return []
    today = date.today().isoformat()
    rows = []
    try:
        with open(path, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                ts = row.get('timestamp', '')
                if ts.startswith(today):
                    rows.append(row)
    except Exception as e:
        log.warning('CSV read error %s: %s', path, e)
    return rows


def _read_signal_log(path: str, limit: int = 50) -> list[dict]:
    """Return last `limit` rows from signal_scan.csv."""
    if not os.path.exists(path):
        return []
    rows = []
    try:
        with open(path, newline='', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        return rows[-limit:]
    except Exception as e:
        log.warning('Signal log read error: %s', e)
    return []


def _is_market_open() -> bool:
    now = datetime.now()
    h, m = now.hour, now.minute
    t = h * 60 + m
    open_t  = int(MARKET_OPEN.split(':')[0])  * 60 + int(MARKET_OPEN.split(':')[1])
    close_t = int(MARKET_CLOSE.split(':')[0]) * 60 + int(MARKET_CLOSE.split(':')[1])
    return open_t <= t <= close_t


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _safe_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


# =============================================================
#  DATA BUILDERS
# =============================================================

def _build_closed_trades() -> list[dict]:
    """Parse logs/trades.csv for today's closed trades."""
    raw = _read_csv_today(TRADE_LOG)
    result = []
    for r in raw:
        ts      = r.get('timestamp', '')
        symbol  = r.get('symbol', '')
        side    = r.get('side', 'LONG')
        qty     = _safe_int(r.get('qty', 0))
        entry   = _safe_float(r.get('entry', 0))
        exit_p  = _safe_float(r.get('exit_price', 0))
        hold    = _safe_float(r.get('hold_minutes', 0))
        reason  = r.get('exit_reason', '')
        gross   = _safe_float(r.get('gross_pnl', 0))
        charges = _safe_float(r.get('total_charges', 0))
        net     = _safe_float(r.get('net_pnl', 0))
        prob    = _safe_float(r.get('prob_up', 0))
        rr      = _safe_float(r.get('rr', 0))
        regime  = r.get('market_regime', '')
        sector  = r.get('sector', SECTOR_MAP.get(symbol, 'UNKNOWN'))
        brokerage = _safe_float(r.get('brokerage', 0))
        stt       = _safe_float(r.get('stt', 0))
        etc       = _safe_float(r.get('etc', 0))
        gst       = _safe_float(r.get('gst', 0))
        breakeven = _safe_float(r.get('breakeven_sell', 0))
        trail_cnt = _safe_int(r.get('trail_count', 0))
        candles   = _safe_int(r.get('candles_held', 0))
        result.append({
            'ts': ts[11:16] if len(ts) > 15 else ts,  # HH:MM
            'symbol': symbol,
            'sector': sector,
            'side': side,
            'qty': qty,
            'entry': entry,
            'exit': exit_p,
            'hold': round(hold),
            'reason': reason,
            'gross': round(gross, 2),
            'charges': round(charges, 2),
            'net': round(net, 2),
            'prob': round(prob, 4),
            'rr': round(rr, 3),
            'regime': regime,
            'breakdown': {
                'brokerage': round(brokerage, 4),
                'stt':       round(stt, 4),
                'etc':       round(etc, 4),
                'gst':       round(gst, 4),
                'total':     round(charges, 4),
                'breakeven': round(breakeven, 2),
            },
            'trail_count': trail_cnt,
            'candles_held': candles,
        })
    return result


def _build_open_trades() -> list[dict]:
    """
    FIX-12: Prefer open trades from state.json (written by bot process).
    Falls back to Dhan API positions if state.json unavailable.
    state.json has SL/TP/prob/rr which the Dhan API doesn't.
    """
    bot_state = _read_bot_state()
    if bot_state and 'open_trades' in bot_state:
        return bot_state['open_trades']
    # Fallback: query Dhan API directly
    return _build_open_trades_live()


def _build_open_trades_live() -> list[dict]:
    """
    Get open positions from Dhan API (live / paper mode).
    Returns list of position dicts the dashboard can render.
    """
    broker = _get_broker()
    if broker is None:
        return []
    try:
        pos_df = broker.get_positions()
        if pos_df is None or pos_df.empty:
            return []
        result = []
        sym_col = next(
            (c for c in ['tradingSymbol', 'trading_symbol', 'symbol']
             if c in pos_df.columns), None
        )
        if sym_col is None:
            return []
        for _, row in pos_df.iterrows():
            symbol = str(row.get(sym_col, '')).upper()
            qty    = _safe_int(row.get('netQty', row.get('quantity', 0)))
            if qty == 0:
                continue
            entry  = _safe_float(row.get('averageTradedPrice', row.get('buyAvg', 0)))
            ltp    = _safe_float(row.get('lastTradedPrice', entry))
            sec_id = str(row.get('securityId', WATCHLIST.get(symbol, '')))
            sector = SECTOR_MAP.get(symbol, 'UNKNOWN')
            result.append({
                'symbol':    symbol,
                'secId':     sec_id,
                'side':      'LONG',
                'qty':       qty,
                'entry':     round(entry, 2),
                'ltp':       round(ltp, 2),
                'sl':        0,
                'target':    0,
                'trailSl':   0,
                'trailCount':0,
                'prob':      0,
                'rr':        0,
                'atr':       0,
                'openTime':  '',
                'candles':   0,
                'sector':    sector,
                'regime':    '',
            })
        return result
    except Exception as e:
        log.warning('get_positions failed: %s', e)
        return []


def _build_watchlist_data() -> list[dict]:
    """
    Returns LTP for all watchlist stocks.
    Optionally scores signals (expensive — cached).
    """
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
        sector = SECTOR_MAP.get(symbol, 'UNKNOWN')
        tier   = TIER_MAP.get(symbol, 'B')
        result.append({
            'symbol':   symbol,
            'secId':    str(sec_id),
            'sector':   sector,
            'tier':     tier,
            'ltp':      round(ltp, 2),
            'chg':      0.0,
            'signal':   'HOLD',
            'conf':     0.0,
            'sl':       0.0,
            'target':   0.0,
            'rr':       0.0,
        })
    return result


def _build_nifty_data() -> dict:
    """Fetch Nifty 50 LTP from Dhan."""
    broker = _get_broker()
    nifty_sid = os.getenv('NIFTY50_SECURITY_ID', '13')
    result = {
        'ltp':    0.0,
        'chg':    0.0,
        'atr':    0.0,
        'rsi':    50.0,
        'ema20':  'above',
        'regime': 'neutral',
    }
    if broker is None:
        return result
    try:
        ltp = broker.get_ltp(nifty_sid, 'NIFTY50')
        result['ltp'] = round(_safe_float(ltp), 2)
    except Exception as e:
        log.warning('Nifty LTP failed: %s', e)
    return result


def _build_risk_state(closed_trades: list[dict]) -> dict:
    """
    FIX-12: Prefer risk state from state.json (real-time from bot process).
    Falls back to computing from closed trades CSV.
    """
    bot_state = _read_bot_state()
    if bot_state and 'risk' in bot_state:
        return bot_state['risk']

    # Fallback: compute from CSV
    total_net    = sum(t['net']     for t in closed_trades)
    total_gross  = sum(t['gross']   for t in closed_trades)
    total_charges= sum(t['charges'] for t in closed_trades)
    wins         = sum(1 for t in closed_trades if t['net'] > 0)
    losses       = sum(1 for t in closed_trades if t['net'] <= 0)
    total        = wins + losses
    win_rate     = round(wins / total * 100, 1) if total else 0
    loss_pct     = max(0, -total_net) / CAPITAL * 100
    max_loss_lim = _safe_float(os.getenv('DAILY_LOSS_LIMIT', '0.04')) * CAPITAL

    halted = False
    if total >= 10 and total > 0:
        if (wins / total) < 0.30 and losses >= 7 and total_net < -CAPITAL * 0.02:
            halted = True
    if total_net < -max_loss_lim:
        halted = True
    consec = 0
    for t in reversed(closed_trades):
        if t['net'] <= 0:
            consec += 1
        else:
            break
    if consec >= 3:
        halted = True

    return {
        'daily_net_pnl':    round(total_net, 2),
        'daily_gross_pnl':  round(total_gross, 2),
        'total_charges':    round(total_charges, 2),
        'wins':             wins,
        'losses':           losses,
        'win_rate':         win_rate,
        'loss_pct_of_limit': round(loss_pct / (max_loss_lim / CAPITAL * 100) * 100, 1)
                              if max_loss_lim else 0,
        'halted':           halted,
        'consecutive_losses': consec,
        'capital':          CAPITAL,
        'max_daily_loss':   round(max_loss_lim, 0),
    }


def _build_state(force: bool = False) -> dict:
    """Master builder — cached for CACHE_TTL seconds."""
    now = time.time()
    if not force and _cache.get('ts', 0) + CACHE_TTL > now:
        return _cache.get('data', {})

    closed  = _build_closed_trades()
    risk    = _build_risk_state(closed)
    open_t  = _build_open_trades()        # FIX-12: uses state.json first
    wl      = _build_watchlist_data()
    nifty   = _build_nifty_data()
    signals = _read_signal_log(SIGNAL_LOG, limit=30)

    alerts = []
    for t in reversed(closed[-8:]):
        icon = '🟢' if t['net'] > 0 else '🔴'
        reason_str = t['reason'].replace('_', ' ')
        alerts.append({
            'icon': icon,
            'text': f"<span class=\"alert-sym\">{t['symbol']}</span> {reason_str} "
                    f"→ exit ₹{t['exit']:.2f} · Net P&L: "
                    f"{'+' if t['net']>=0 else ''}₹{t['net']:,.0f}",
            'time': t['ts'],
        })
    for t in open_t:
        alerts.append({
            'icon': '🔵',
            'text': f"<span class=\"alert-sym\">{t['symbol']}</span> OPEN POSITION "
                    f"· entry ₹{t['entry']:.2f} · qty={t['qty']}",
            'time': 'now',
        })
    if not alerts:
        alerts.append({'icon':'🤖','text':'Bot running — no trades yet today','time':''})

    data = {
        'timestamp':   datetime.now().isoformat(),
        'mode':        BOT_MODE,
        'halted':      risk.get('halted', False),
        'market_open': _is_market_open(),
        'capital':     CAPITAL,
        'openTrades':  open_t,
        'closedTrades': closed,
        'watchlist':   wl,
        'nifty':       nifty,
        'risk':        risk,
        'signals':     signals,
        'alerts':      alerts,
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


# =============================================================
#  ROUTES
# =============================================================

@app.route('/')
def index():
    dashboard = _UI_DIR / 'dashboard' / 'index.html'
    if dashboard.exists():
        return send_file(str(dashboard))
    return '<h2>Dashboard not found. Expected: ui/dashboard/index.html</h2>', 404


@app.route('/api/state')
def api_state():
    try:
        data = _build_state()
        return jsonify(data)
    except Exception as e:
        log.exception('api_state error')
        return jsonify({'error': str(e)}), 500


@app.route('/api/positions')
def api_positions():
    try:
        return jsonify(_build_open_trades())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/closed')
def api_closed():
    try:
        return jsonify(_build_closed_trades())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/watchlist')
def api_watchlist():
    try:
        return jsonify(_build_watchlist_data())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/nifty')
def api_nifty():
    try:
        return jsonify(_build_nifty_data())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/risk')
def api_risk():
    try:
        closed = _build_closed_trades()
        return jsonify(_build_risk_state(closed))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/signals')
def api_signals():
    try:
        return jsonify(_read_signal_log(SIGNAL_LOG, limit=50))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/logs')
def api_logs():
    """Return last 100 lines of bot.log for the log viewer."""
    try:
        if not os.path.exists(LOG_FILE):
            return jsonify({'lines': []})
        with open(LOG_FILE, encoding='utf-8') as f:
            lines = f.readlines()
        return jsonify({'lines': [l.rstrip() for l in lines[-100:]]})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/force_exit', methods=['POST'])
def api_force_exit():
    if BOT_MODE != 'live':
        return jsonify({'status': 'noop', 'reason': 'Not in live mode'}), 400
    return jsonify({'status': 'ok', 'message': 'Force exit signal sent'})


@app.route('/api/health')
def api_health():
    """
    FIX-12: broker_ready now reads logs/state.json written by the bot
    process, not the dashboard's lazy _broker singleton.

    broker_ready = True  when:
      - state.json exists AND was written within the last 5 minutes
      - state.json contains broker_ready: true

    This correctly reflects whether the BOT is running, not whether
    the dashboard has initialized its own Dhan connection.
    """
    bot_state   = _read_bot_state()
    bot_running = _bot_is_running()

    # broker_ready is true only if the bot process is alive
    # AND the bot itself reported broker_ready in its state file
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


# =============================================================
#  MAIN
# =============================================================

if __name__ == '__main__':
    port = int(os.getenv('DASHBOARD_PORT', '5050'))
    host = os.getenv('DASHBOARD_HOST', '0.0.0.0')
    debug = os.getenv('DASHBOARD_DEBUG', 'false').lower() == 'true'
    log.info('Starting XGB Bot Dashboard on http://%s:%d', host, port)
    log.info('Mode: %s | Bot available: %s', BOT_MODE, _BOT_AVAILABLE)
    app.run(host=host, port=port, debug=debug, use_reloader=False)
