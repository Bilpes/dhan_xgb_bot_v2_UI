# live_bot.py — state_writer integration

Add these 3 lines to `bot/live_bot.py` to enable live dashboard data.

## Step 1 — Add import (top of file, after other bot imports)

```python
from bot.state_writer import write_state
```

## Step 2 — Call write_state at end of `_scan_and_enter()`

Find the end of `_scan_and_enter()` method (around the last `for` loop that calls `_enter_paper` / `_enter_live`), add:

```python
        # Write state for dashboard
        write_state(self)
```

## Step 3 — Call write_state at end of `_monitor_positions()`

At the very end of `_monitor_positions()` method, after the `for symbol, trade in list(self.trades.items()):` loop closes, add:

```python
        # Write state for dashboard  
        write_state(self)
```

That's it. The dashboard Flask API will auto-detect `logs/state.json`
and show `broker_ready: true` with live open trades.
