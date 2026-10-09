"""HTTP API + the phone dashboard (served as a PWA from /).

    set GT_TOKEN=some-long-secret          # required to reach /api from the app
    set GT_BROKER=mt5                       # or "sim" to run without MT5
    python -m server                        # http://0.0.0.0:8080

Every /api route needs `Authorization: Bearer <GT_TOKEN>` (the event stream
accepts `?token=` because EventSource can't send headers).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import secrets
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine import Preset, RiskLimits, Side, StopMode, TradeError, TradeSettings
from engine.models import StepConfig
from engine.store import SqliteStore

from .service import TradingService

log = logging.getLogger("gold-trader")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


# ------------------------------------------------------------------ schemas

class StepIn(BaseModel):
    pips: float
    close_pct: float = 0.0
    move_sl_pips: float | None = None


class SettingsIn(BaseModel):
    lot: float = 0.04
    stop_mode: StopMode = StopMode.MEDIUM
    sl_pips: float | None = None
    tp_pips: float | None = None
    steps: list[StepIn] = Field(default_factory=lambda: [StepIn(pips=20, close_pct=50, move_sl_pips=0),
                                                         StepIn(pips=50, close_pct=25, move_sl_pips=20),
                                                         StepIn(pips=100, close_pct=0, move_sl_pips=50)])
    trailing: bool = True
    trail_pips: float = 50.0

    def to_settings(self) -> TradeSettings:
        return TradeSettings(lot=self.lot, stop_mode=self.stop_mode, sl_pips=self.sl_pips, tp_pips=self.tp_pips,
                             steps=[StepConfig(s.pips, s.close_pct, s.move_sl_pips) for s in self.steps],
                             trailing=self.trailing, trail_pips=self.trail_pips)


class OpenIn(BaseModel):
    side: Side
    settings: SettingsIn


class CloseIn(BaseModel):
    volume: float | None = None   # None = close everything


class StepPatch(BaseModel):
    pips: float | None = None
    close_pct: float | None = None
    move_sl_pips: float | None = None
    clear_move_sl: bool = False


class ModifyIn(BaseModel):
    sl_pips: float | None = None          # from entry; negative = loss side, positive = locked profit
    tp_pips: float | None = None
    clear_tp: bool = False
    steps: dict[int, StepPatch] | None = None


class PresetIn(BaseModel):
    name: str
    settings: SettingsIn


class LimitsIn(BaseModel):
    max_lot: float
    daily_loss_usd: float
    max_spread_pips: float


# ------------------------------------------------------------------- wiring

def build_service() -> TradingService:
    kind = os.environ.get("GT_BROKER", "mt5").lower()
    store = SqliteStore(os.environ.get("GT_DB", "gold-trader.db"))
    if kind == "sim":
        from engine.sim_broker import SimBroker
        broker = SimBroker(bid=float(os.environ.get("GT_SIM_PRICE", "2650")))
        return TradingService(broker, store, sim_walk=os.environ.get("GT_SIM_WALK", "1") != "0")
    from engine.mt5_broker import MT5Broker
    broker = MT5Broker(
        symbol=os.environ.get("MT5_SYMBOL", "XAUUSD"),
        login=int(os.environ["MT5_LOGIN"]) if os.environ.get("MT5_LOGIN") else None,
        password=os.environ.get("MT5_PASSWORD"),
        server=os.environ.get("MT5_SERVER"),
        terminal_path=os.environ.get("MT5_PATH"),
    )
    return TradingService(broker, store, limits=RiskLimits(max_lot=float(os.environ.get("GT_MAX_LOT", "0.04"))))


def build_signals(trading: TradingService):
    """Signal service + Telegram listener, when configured. Returns None when ANTHROPIC_API_KEY is missing."""
    from signals import SignalService
    from signals.parser import ClaudeParser
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY not set: Telegram signals disabled")
        return None
    saved = trading.store.get_setting("signals") or {}
    sigs = SignalService(trading, ClaudeParser(model=saved.get("model", "claude-opus-5")))
    api_id, api_hash = os.environ.get("TG_API_ID"), os.environ.get("TG_API_HASH")
    if api_id and api_hash:
        from signals.telegram import TelegramListener
        sigs.telegram = TelegramListener(int(api_id), api_hash, os.environ.get("TG_SESSION", "telegram.session"),
                                         sigs.handle_message, [c.id for c in sigs.channels() if c.mode != "off"])
        sigs.telegram.start()
    else:
        log.warning("TG_API_ID / TG_API_HASH not set: Telegram listener disabled (parsing test still works)")
    return sigs


class TopicIn(BaseModel):
    id: int
    title: str = ""


class ChannelIn(BaseModel):
    title: str
    weight: int = 3
    mode: str = "confirm"
    preset_id: int | None = None
    topics: list[TopicIn] = Field(default_factory=list)   # [] = whole group


class SignalSettingsIn(BaseModel):
    lot_per_weight: float = 0.01
    use_signal_tps: bool
    final_tp: bool
    max_entry_drift_pips: float
    default_sl_pips: float = 50.0
    model: str = "claude-opus-5"


class TestParseIn(BaseModel):
    text: str
    channel_id: int | None = None


class PlaceIn(BaseModel):
    lot: float | None = None


def create_app(service: TradingService | None = None, token: str | None = None, run_loop: bool = True,
               signals=None) -> FastAPI:
    token = os.environ.get("GT_TOKEN", "").strip() if token is None else token
    if not token:
        log.warning("GT_TOKEN is empty: the API and dashboard are OPEN to anyone who can reach this server")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.service = service or build_service()
        app.state.signals = signals if signals is not None else (build_signals(app.state.service) if service is None else None)
        if run_loop:
            app.state.service.start()
        yield
        app.state.service.stop()
        if app.state.signals and app.state.signals.telegram:
            app.state.signals.telegram.stop()

    app = FastAPI(title="Gold Trader", lifespan=lifespan)

    @app.middleware("http")
    async def no_cache(request: Request, call_next):
        # The app is reached through a CDN tunnel that would otherwise cache app.js/styles.css for hours.
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response

    def svc(request: Request) -> TradingService:
        return request.app.state.service

    def sigs(request: Request):
        s = request.app.state.signals
        if s is None:
            raise HTTPException(503, "Signals are disabled: set ANTHROPIC_API_KEY (and TG_API_ID/TG_API_HASH) in .env")
        return s

    def auth(request: Request) -> None:
        if not token:
            return
        header = request.headers.get("authorization", "")
        got = header[7:] if header.lower().startswith("bearer ") else request.query_params.get("token", "")
        if not secrets.compare_digest(got, token):
            raise HTTPException(401, "Invalid or missing token")

    api = Depends(auth)

    @app.exception_handler(TradeError)
    async def trade_error(_, exc: TradeError):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": str(exc)}, status_code=400)

    # -- auth probe

    @app.get("/api/me", dependencies=[api])
    def me():
        return {"ok": True}

    # -- status

    @app.get("/api/status", dependencies=[api])
    def status(s: TradingService = Depends(svc)):
        return s.status()

    # -- trades

    @app.get("/api/trades", dependencies=[api])
    def trades(s: TradingService = Depends(svc)):
        return s.status()["open_trades"]

    @app.get("/api/history", dependencies=[api])
    def history(limit: int = 100, s: TradingService = Depends(svc)):
        return s.history(limit)

    @app.get("/api/trades/{ticket}", dependencies=[api])
    def trade(ticket: int, s: TradingService = Depends(svc)):
        return s.get_trade(ticket)

    @app.post("/api/trades/preview", dependencies=[api])
    def preview(body: OpenIn, s: TradingService = Depends(svc)):
        return s.preview(body.side, body.settings.to_settings())

    @app.post("/api/trades", dependencies=[api], status_code=201)
    def open_trade(body: OpenIn, s: TradingService = Depends(svc)):
        return s.open_trade(body.side, body.settings.to_settings())

    @app.post("/api/trades/{ticket}/close", dependencies=[api])
    def close_trade(ticket: int, body: CloseIn | None = None, s: TradingService = Depends(svc)):
        return {"events": s.close_trade(ticket, body.volume if body else None)}

    @app.patch("/api/trades/{ticket}", dependencies=[api])
    def modify_trade(ticket: int, body: ModifyIn, s: TradingService = Depends(svc)):
        steps = None
        if body.steps:
            steps = {}
            for idx, patch in body.steps.items():
                changes = {k: v for k, v in patch.model_dump().items() if v is not None and k != "clear_move_sl"}
                if patch.clear_move_sl:
                    changes["move_sl_pips"] = None
                if changes:
                    steps[int(idx)] = changes
        return s.modify_trade(ticket, sl_pips=body.sl_pips, tp_pips=body.tp_pips, steps=steps or None,
                              clear_tp=body.clear_tp)

    @app.post("/api/trades/{ticket}/retry/{idx}", dependencies=[api])
    def retry_step(ticket: int, idx: int, s: TradingService = Depends(svc)):
        return s.retry_step(ticket, idx)

    @app.get("/api/unmanaged", dependencies=[api])
    def unmanaged(s: TradingService = Depends(svc)):
        return s.unmanaged()

    @app.post("/api/trades/{ticket}/adopt", dependencies=[api], status_code=201)
    def adopt(ticket: int, body: SettingsIn, s: TradingService = Depends(svc)):
        return s.adopt(ticket, body.to_settings())

    @app.post("/api/trades/{ticket}/reenter", dependencies=[api], status_code=201)
    def reenter(ticket: int, s: TradingService = Depends(svc)):
        return s.reenter(ticket)

    # -- presets

    @app.get("/api/presets", dependencies=[api])
    def presets(s: TradingService = Depends(svc)):
        return [s.preset_view(p) for p in s.store.list_presets()]

    @app.post("/api/presets", dependencies=[api], status_code=201)
    def create_preset(body: PresetIn, s: TradingService = Depends(svc)):
        return s.preset_view(s.store.save_preset(Preset(body.name, body.settings.to_settings())))

    @app.put("/api/presets/{preset_id}", dependencies=[api])
    def update_preset(preset_id: int, body: PresetIn, s: TradingService = Depends(svc)):
        if preset_id not in s.store.presets:
            raise HTTPException(404, "Preset not found")
        return s.preset_view(s.store.save_preset(Preset(body.name, body.settings.to_settings(), preset_id)))

    @app.delete("/api/presets/{preset_id}", dependencies=[api])
    def delete_preset(preset_id: int, s: TradingService = Depends(svc)):
        if not s.store.delete_preset(preset_id):
            raise HTTPException(404, "Preset not found")
        return {"ok": True}

    # -- limits

    @app.get("/api/limits", dependencies=[api])
    def limits(s: TradingService = Depends(svc)):
        return asdict(s.engine.limits)

    @app.put("/api/limits", dependencies=[api])
    def set_limits(body: LimitsIn, s: TradingService = Depends(svc)):
        return asdict(s.set_limits(RiskLimits(**body.model_dump())))

    # -- events

    @app.get("/api/events", dependencies=[api])
    def events(limit: int = 50, s: TradingService = Depends(svc)):
        return s.store.recent_events(limit)

    @app.get("/api/events/stream", dependencies=[api])
    async def stream(request: Request, s: TradingService = Depends(svc)):
        q = s.subscribe()

        async def gen():
            try:
                yield "retry: 3000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.to_thread(q.get, True, 15)
                        yield f"event: {ev.get('kind', 'info')}\ndata: {json.dumps(ev)}\n\n"
                    except queue.Empty:
                        yield ": ping\n\n"
            finally:
                s.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # -- telegram signals

    @app.get("/api/signals/status", dependencies=[api])
    def signals_status(request: Request):
        s = request.app.state.signals
        if s is None:
            return {"enabled": False, "telegram": None, "settings": None}
        tg = s.telegram
        return {"enabled": True, "settings": s.cfg.to_dict(),
                "telegram": None if tg is None else {"connected": tg.connected, "error": tg.error,
                                                     "user": (tg.me.username or tg.me.first_name) if tg.me else None}}

    @app.get("/api/signals", dependencies=[api])
    def list_signals(limit: int = 100, s=Depends(sigs)):
        return s.signals(limit)

    @app.post("/api/signals/test", dependencies=[api])
    def test_signal(body: TestParseIn, s=Depends(sigs)):
        return s.test_parse(body.text, body.channel_id)

    @app.post("/api/signals/{signal_id}/place", dependencies=[api])
    def place_signal(signal_id: int, body: PlaceIn | None = None, s=Depends(sigs)):
        return s.place(signal_id, body.lot if body else None).to_dict()

    @app.post("/api/signals/{signal_id}/dismiss", dependencies=[api])
    def dismiss_signal(signal_id: int, s=Depends(sigs)):
        return s.dismiss(signal_id).to_dict()

    @app.get("/api/signals/settings", dependencies=[api])
    def signal_settings(s=Depends(sigs)):
        return s.cfg.to_dict()

    @app.put("/api/signals/settings", dependencies=[api])
    def set_signal_settings(body: SignalSettingsIn, s=Depends(sigs)):
        from signals import SignalSettings
        return s.set_settings(SignalSettings(**body.model_dump())).to_dict()

    @app.get("/api/channels", dependencies=[api])
    def channels(s=Depends(sigs)):
        return [{**c.to_dict(), "review": s.channel_review(c.id)} for c in s.channels()]

    @app.get("/api/channels/available", dependencies=[api])
    def available_channels(s=Depends(sigs)):
        if s.telegram is None or not s.telegram.connected:
            return {"connected": False, "dialogs": []}
        return {"connected": True, "dialogs": s.telegram.list_dialogs()}

    @app.get("/api/channels/{channel_id}/topics", dependencies=[api])
    def channel_topics(channel_id: int, s=Depends(sigs)):
        if s.telegram is None or not s.telegram.connected:
            return []
        return s.telegram.list_topics(channel_id)

    @app.put("/api/channels/{channel_id}", dependencies=[api])
    def save_channel(channel_id: int, body: ChannelIn, s=Depends(sigs)):
        from signals import Channel
        try:
            return s.save_channel(Channel(channel_id, **body.model_dump())).to_dict()  # topics as dicts
        except ValueError as ex:
            raise HTTPException(400, str(ex))

    @app.delete("/api/channels/{channel_id}", dependencies=[api])
    def delete_channel(channel_id: int, s=Depends(sigs)):
        if not s.delete_channel(channel_id):
            raise HTTPException(404, "Channel not found")
        return {"ok": True}

    @app.get("/api/channels/{channel_id}/review", dependencies=[api])
    def channel_review(channel_id: int, s=Depends(sigs)):
        return s.channel_review(channel_id)

    # -- the app itself

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
    return app
