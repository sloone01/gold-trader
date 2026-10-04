"""Replay a price path through the engine with the simulated broker. Runs anywhere.

    python -m scripts.simulate            # the spec's hybrid example
    python -m scripts.simulate --sell     # same, mirrored
"""
import argparse

from engine import Engine, MemoryStore, Side, TradeSettings
from engine.sim_broker import SimBroker

PATH_PIPS = [0, 10, 20, 35, 50, 70, 100, 130, 150, 175, 200, 160, 130, 100]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sell", action="store_true")
    args = ap.parse_args()
    side = Side.SELL if args.sell else Side.BUY

    broker = SimBroker(bid=2650.00)
    eng = Engine(broker, MemoryStore())
    trade, events = eng.open_trade(side, TradeSettings())
    for e in events:
        print(f"[{e.kind}] {e.message}")

    for pips in PATH_PIPS:
        # Move the closing price (bid for buys, ask for sells) to entry +/- pips.
        target = trade.entry + side.sign * pips * 0.10
        broker.set_price(target if side is Side.BUY else target - broker.spread)
        for e in eng.tick():
            print(f"  {pips:+4d} pips  [{e.kind}] {e.message}")
        if not eng.store.trades[trade.ticket].open:
            break


if __name__ == "__main__":
    main()
