# Gold Trader — phase 1: trading engine

The engine opens XAU/USD trades on MT5 and manages P1–P3 steps (partial close and stop moves), trailing after P3, retries, restart recovery and risk limits. The phone app and API come in later phases.

```
engine/
  units.py        pips <-> price (10 pips = $1), volume rounding
  models.py       settings, presets, trade and step state
  validation.py   the spec's validation rules
  engine.py       the trading logic
  store.py        state file (JSON now, SQLite in phase 2)
  broker.py       broker interface
  sim_broker.py   simulated broker for testing anywhere
  mt5_broker.py   real MT5 adapter (Windows only)
scripts/
  simulate.py     replay a price path with the simulator
  run_demo.py     run against your MT5 terminal
tests/
```

## On the Mac (no MT5 needed)

```bash
python3.13 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/pytest -q
.venv/bin/python -m scripts.simulate
```

## On the Windows VPS (demo account first)

1. Install Python 3.11+ and the MT5 terminal, log in to the **demo** account, and turn on **Algo Trading** (toolbar button).
2. Check that the account is **hedging** (the bot refuses netting accounts) and find the gold symbol name (`XAUUSD`, `GOLD`, `XAUUSD.m`…).
3. Copy this folder over, then:
   ```
   pip install -r requirements.txt
   set MT5_LOGIN=12345678
   set MT5_PASSWORD=your-demo-password
   set MT5_SERVER=YourBroker-Demo
   set MT5_SYMBOL=XAUUSD
   python -m scripts.run_demo buy
   ```
   The bot shows a preview and asks before sending. Stop it with Ctrl+C. Run `python -m scripts.run_demo watch` to resume managing open trades.

## Defaults (change in `engine/engine.py`)

| Setting | Default |
|---|---|
| Breakeven buffer | +1 pip |
| Backup SL in Far mode with no SL | 300 pips |
| Retries per step | 3, with a 2 s / 4 s backoff |
| Max spread for new trades | 10 pips |
| Max lot / open trades / daily loss | 0.04 in run_demo / 3 / $50 |
