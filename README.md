# dhan_xgb_bot_v2_UI

> **Status: Paper-trading / supervised live.** Do not run unattended in live mode without completing the shadow period described below.

An intraday NSE momentum bot powered by XGBoost. Trades a curated 21-stock universe (no IT, no PSU junk) on Dhan broker. Features a Flask dashboard, Telegram alerts, and a full production safety stack.

---

## Quick start

```bash
# 1. Create venv and install
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt

# 2. Configure credentials
copy config\.env.example config\.env
# → fill in DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, TELEGRAM_*

# 3. Refresh token & download data
python -m bot.token_refresh
python -m data.download_data

# 4. Train model
python train.py

# 5. One-scan test (no orders placed)
set BOT_MODE=test
python -m bot.live_bot

# 6. Paper trading (full loop, simulated P&L)
set BOT_MODE=paper
python -m bot.live_bot

# 7. Dashboard (separate terminal)
python ui/api/app.py
# Open http://localhost:5050
```

---

## Directory structure

```
dhan_xgb_bot_v2_UI/
├── bot/
│   ├── live_bot.py          # Main trading loop
│   ├── signal_engine.py     # XGBoost inference + filters
│   ├── trade_policy.py      # ALL numeric trading params
│   ├── risk_manager.py      # Position sizing + circuit breakers
│   ├── trade_manager.py     # Execution layer
│   ├── symbol_penalty.py    # Per-symbol rolling loss tracker (FIX-15)
│   ├── ev_gate.py           # Cost-aware EV gate (PROD-P1)
│   ├── live_guard.py        # 3-factor live trading interlock (PROD)
│   ├── watchlist_guard.py   # Startup watchlist validator (PROD)
│   ├── startup_reconcile.py # Broker position sync on restart (PROD)
│   ├── trade_audit.py       # JSON trade lifecycle log (PROD)
│   ├── brokerage.py         # Exact Dhan charge calculator
│   ├── dhan_api.py          # Dhan v2 API wrapper
│   ├── risk_manager.py      # Daily P&L + trailing SL
│   ├── telegram_alert.py    # Telegram notifications
│   ├── token_refresh.py     # Dhan token auto-refresh
│   ├── auto_retrain.py      # Weekly model retraining
│   ├── backtest.py          # Walk-forward backtest
│   ├── health_check.py      # System health probe
│   ├── nse_holidays.py      # NSE holiday calendar
│   └── state_writer.py      # Dashboard state writer
├── config/
│   ├── config.py            # Infrastructure + paths
│   ├── watchlist.json       # 21-symbol curated universe
│   └── .env.example         # Credential template
├── data/
│   ├── download_data.py
│   └── load_instruments.py
├── models/                  # Trained model artifacts (gitignored)
├── logs/                    # Runtime logs (gitignored)
├── ui/                      # Flask dashboard
│   ├── api/app.py
│   └── dashboard/index.html
├── features.py
├── train.py
├── signal_engine.py
├── trade_manager.py
├── run_bot.py
├── scheduler.bat
└── requirements.txt
```

---

## Production safety stack

| Layer | File | What it does |
|---|---|---|
| Live interlock | `bot/live_guard.py` | 3-factor check (MODE + ENABLED + TOKEN) before any live order |
| EV gate | `bot/ev_gate.py` | Blocks trades where expected net P&L after charges ≤ ₹50 |
| IT block | `bot/trade_policy.py` | Hard-blocks TCS, INFY, HCLTECH, WIPRO and 10 other IT names |
| Watchlist guard | `bot/watchlist_guard.py` | Validates watchlist on startup — no blocked symbols, no missing IDs |
| Startup reconcile | `bot/startup_reconcile.py` | Syncs bot state with Dhan on restart |
| Symbol penalty | `bot/symbol_penalty.py` | Skips symbols with 3 consecutive losses > ₹1200 cumulative |
| Trade audit | `bot/trade_audit.py` | NDJSON log of every trade lifecycle event |
| Circuit breakers | `bot/risk_manager.py` | Daily loss, consecutive SL, regime failure |
| Charge accounting | `bot/brokerage.py` | Every P&L number is net of Dhan charges |

---

## Going live — pre-flight checklist

- [ ] ≥ 10 paper-trade sessions with consistent positive net expectancy after charges
- [ ] Model AUC ≥ 0.58 on out-of-sample data
- [ ] `python -m bot.health_check` passes all checks
- [ ] `LIVE_TRADING_ENABLED=true` deliberately set in `config/.env`
- [ ] `LIVE_CONFIRM_TOKEN` and `LIVE_CONFIRM_PASSPHRASE` set (see `bot/live_guard.py`)
- [ ] Telegram alerts confirmed working (`python test_telegram.py`)
- [ ] Token refreshed fresh today (`python -m bot.token_refresh`)
- [ ] Data downloaded for today (`python -m data.download_data`)
- [ ] Start with `BOT_MODE=live` and ≤ 2 max positions for first week

---

## Watchlist

21 curated NSE stocks across 9 sectors. No IT, no PSU utilities, no Adani group.
See `config/watchlist.json`. Criteria: daily turnover > ₹300Cr, MCap > ₹15,000Cr.

---

## Trade performance note

Paper-trade logs from June 29 – July 3, 2026 showed 73% gross win rate but
negative net expectancy after charges. Three improvements address this:
1. **EV gate** (`bot/ev_gate.py`) — only enter when expected net P&L > ₹50
2. **Extension guard** — skip entries where price is >2.5% above EMA20
3. **Symbol penalty** — skip repeat losers for the session

Monitor `logs/trade_audit.ndjson` and `logs/trades.csv` after each session.
