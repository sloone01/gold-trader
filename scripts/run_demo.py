"""Run the engine against a real MT5 terminal (Windows VPS, DEMO account first).

    set MT5_LOGIN=12345678
    set MT5_PASSWORD=...
    set MT5_SERVER=YourBroker-Demo
    python -m scripts.run_demo buy            # open a buy with the default preset, then manage it
    python -m scripts.run_demo sell --mode tight
    python -m scripts.run_demo watch          # only manage trades already in state.json

Ctrl+C stops the bot. Open trades keep their SL/TP on the broker; run "watch" to resume.
"""
import argparse
import os
import time

from engine import Engine, JsonStore, RiskLimits, Side, StopMode, TradeError, TradeSettings
from engine.mt5_broker import MT5Broker


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["buy", "sell", "watch"])
    ap.add_argument("--mode", choices=[m.value for m in StopMode], default="medium")
    ap.add_argument("--lot", type=float, default=0.04)
    ap.add_argument("--sl", type=float, help="SL pips (Far mode)")
    ap.add_argument("--tp", type=float, help="TP pips")
    ap.add_argument("--symbol", default=os.environ.get("MT5_SYMBOL", "XAUUSD"))
    args = ap.parse_args()

    broker = MT5Broker(
        symbol=args.symbol,
        login=int(os.environ["MT5_LOGIN"]) if os.environ.get("MT5_LOGIN") else None,
        password=os.environ.get("MT5_PASSWORD"),
        server=os.environ.get("MT5_SERVER"),
        terminal_path=os.environ.get("MT5_PATH"),
    )
    eng = Engine(broker, JsonStore("state.json"), limits=RiskLimits(max_lot=0.04))
    info = eng.info
    print(f"Connected. {args.symbol}: tick {info.tick_size}, min lot {info.vol_min}, "
          f"stops level {info.stops_level / 0.10:.0f} pips")

    if args.action != "watch":
        s = TradeSettings(lot=args.lot, stop_mode=StopMode(args.mode), sl_pips=args.sl, tp_pips=args.tp)
        side = Side(args.action)
        print("Preview:", eng.preview(side, s))
        if input("Send this trade? [y/N] ").strip().lower() != "y":
            return
        try:
            _, events = eng.open_trade(side, s)
        except TradeError as e:
            print("Not opened:", e)
            return
        for e in events:
            print(f"[{e.kind}] {e.message}")

    print("Managing open trades. Ctrl+C to stop.")
    while True:
        try:
            for e in eng.tick():
                print(time.strftime("%H:%M:%S"), f"[{e.kind}] {e.message}")
        except Exception as ex:  # keep the loop alive; the broker-side SL still protects the trade
            print("Loop error:", ex)
            time.sleep(2)
        time.sleep(0.25)


if __name__ == "__main__":
    main()
