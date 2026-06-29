# UI Design Concepts

Two design concepts for the Dhan XGBoost Bot dashboard. Open each `index.html` locally in a browser to preview.

## Concept A — Clean Operator Desk
`ui/concept-a/index.html`

- Dark warm palette with a sidebar layout
- KPI row: open positions, unrealized P&L, realized, daily net, monthly net
- Open positions table: symbol, qty, buy price, live LTP, unrealized P&L, SL, target, SL→TP progress bar, status badge
- Positions include trailing SL state (shows current trail level vs initial SL)
- Intraday equity sparkline
- Daily P&L breakdown (realized + unrealized + charges + net)
- Monthly P&L blocks (weekly breakdown)
- Paper/live mode button in topbar

## Concept B — Pro Trading Terminal
`ui/concept-b/index.html`

- Deep blue-black with vivid green/teal accents — feels like a live trading screen
- Animated pulse indicator showing bot running status
- IST clock in topbar
- Top KPI strip with color-coded accent bars (green/violet/red)
- Positions table: symbol, live LTP with directional arrow + change, unrealized P&L, init SL + trail SL, target, SL→TP progress ladder
- Right column: daily P&L panel, risk snapshot (mode, gate, win rate, threshold), monthly P&L
- Bottom row: intraday equity curve SVG + weekly P&L bar chart

## Next step
Approve Concept A, B, or a hybrid — then the full multi-page app will be built and wired to your `trade_manager.py` + `signal_engine.py` data.
