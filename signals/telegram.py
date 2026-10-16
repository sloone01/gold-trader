"""Listens to Telegram channels with the user's own account (Telethon / MTProto).

Log in once with `py -m scripts.telegram_login`; the session file is reused afterwards.
Runs its own asyncio loop in a background thread and hands messages to a callback.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("gold-trader.telegram")

DIALOGS_TTL = 600       # seconds a cached channel list stays fresh
CALL_TIMEOUT = 15       # seconds an API thread waits on Telegram before giving up


class TelegramListener:
    def __init__(self, api_id: int, api_hash: str, session: str | Path, on_message, channel_ids: list[int]) -> None:
        self.api_id, self.api_hash, self.session = api_id, api_hash, str(session)
        self.on_message = on_message
        self.channel_ids = set(channel_ids)
        self.loop = asyncio.new_event_loop()
        self.client = None
        self.connected = False
        self.me = None
        self.error: str | None = None
        self._thread: threading.Thread | None = None
        # Listing dialogs walks every chat on the account and can take many seconds (or hit a flood
        # wait), so keep the last result and refresh it in the background instead of on each page load.
        self._dialogs_cache: list[dict] | None = None
        self._dialogs_at = 0.0
        self._dialogs_fut: concurrent.futures.Future | None = None
        self._topics_cache: dict[int, list[dict]] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="telegram", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self.client and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.client.disconnect(), self.loop)

    def set_channels(self, channel_ids: list[int]) -> None:
        self.channel_ids = set(int(c) for c in channel_ids)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except Exception as ex:
            self.error = str(ex)
            log.exception("telegram listener stopped")
        finally:
            self.connected = False

    async def _main(self) -> None:
        from telethon import TelegramClient, events

        self.client = TelegramClient(self.session, self.api_id, self.api_hash)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            self.error = "Not logged in: run  py -m scripts.telegram_login  on the VPS"
            log.warning(self.error)
            return
        self.me = await self.client.get_me()
        self.connected = True
        log.info("Telegram connected as %s", self.me.username or self.me.first_name)
        with self._lock:   # prefetch the chat list now so the Channels page opens instantly
            self._dialogs_fut = asyncio.run_coroutine_threadsafe(self._load_dialogs(), self.loop)

        @self.client.on(events.NewMessage())
        async def handler(event):
            chat_id = _norm_id(event.chat_id)
            if chat_id not in self.channel_ids:
                return
            text = event.raw_text or ""
            image, image_type = None, "image/jpeg"
            msg = event.message
            doc_mime = getattr(getattr(msg, "document", None), "mime_type", "") or ""
            if msg.photo or doc_mime.startswith("image/"):
                try:
                    image = await event.download_media(bytes)
                    if doc_mime.startswith("image/"):
                        image_type = doc_mime
                    if image and len(image) > 4_000_000:
                        image = None    # too big for the API; fall back to the caption alone
                except Exception:
                    log.exception("could not download media")
            if not text.strip() and not image:
                return
            chat = await event.get_chat()
            title = getattr(chat, "title", None) or getattr(chat, "username", "") or str(chat_id)
            reply_to, topic_id = _reply_and_topic(event.message, getattr(chat, "forum", False))
            try:
                await asyncio.to_thread(self.on_message, chat_id, title, event.id, text,
                                        event.date.timestamp() if event.date else None, reply_to, topic_id,
                                        image, image_type)
            except Exception:
                log.exception("signal handler failed")

        await self.client.run_until_disconnected()

    # -- called from API threads

    def list_dialogs(self, refresh: bool = False, timeout: float = CALL_TIMEOUT) -> tuple[list[dict], str | None]:
        """(channels and groups, error). Answers from the cache when it has one and refreshes a stale
        cache in the background. Waits (at most `timeout` seconds) only with no cache yet or on `refresh`."""
        if not self.connected:
            return [], None
        with self._lock:
            cached = self._dialogs_cache
            stale = time.time() - self._dialogs_at > DIALOGS_TTL
            if self._dialogs_fut is None and (cached is None or stale or refresh):
                self._dialogs_fut = asyncio.run_coroutine_threadsafe(self._load_dialogs(), self.loop)
            fut = self._dialogs_fut
        if cached is not None and not refresh:
            return cached, None
        if fut is None:
            return cached or [], None
        try:
            return fut.result(timeout=timeout), None
        except concurrent.futures.TimeoutError:
            return cached or [], "Telegram is slow to list your chats; still loading in the background, try again shortly"
        except Exception as ex:
            return cached or [], f"Could not load your chats: {ex}"

    async def _load_dialogs(self, limit: int = 200) -> list[dict]:
        try:
            out = await self._dialogs(limit)
            with self._lock:
                self._dialogs_cache, self._dialogs_at = out, time.time()
            return out
        except Exception:
            log.exception("could not list dialogs")
            raise
        finally:
            with self._lock:
                self._dialogs_fut = None

    async def _dialogs(self, limit: int) -> list[dict]:
        out = []
        async for d in self.client.iter_dialogs(limit=limit):
            if d.is_channel or d.is_group:
                out.append({"id": _norm_id(d.id), "title": d.name, "kind": "channel" if d.is_channel and not d.is_group else "group",
                            "username": getattr(d.entity, "username", None), "forum": bool(getattr(d.entity, "forum", False))})
        return out

    def list_topics(self, chat_id: int, refresh: bool = False) -> list[dict]:
        """Topics (sub-groups) of a forum-style group. Empty for ordinary channels."""
        if not self.connected:
            return []
        if chat_id in self._topics_cache and not refresh:
            return self._topics_cache[chat_id]
        fut = asyncio.run_coroutine_threadsafe(self._topics(chat_id), self.loop)
        try:
            topics = fut.result(timeout=CALL_TIMEOUT)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            raise TimeoutError("Telegram took too long to list the topics; try again") from None
        self._topics_cache[chat_id] = topics
        return topics

    async def _topics(self, chat_id: int) -> list[dict]:
        try:  # Telethon >= 1.45 keeps it under messages, older releases under channels
            from telethon.tl.functions.messages import GetForumTopicsRequest
            kw = {"peer": None}
        except ImportError:
            from telethon.tl.functions.channels import GetForumTopicsRequest
            kw = {"channel": None}

        entity = await self.client.get_entity(chat_id)
        if not getattr(entity, "forum", False):
            return []
        key = next(iter(kw))
        res = await self.client(GetForumTopicsRequest(**{key: entity}, offset_date=None, offset_id=0, offset_topic=0, limit=100))
        return [{"id": t.id, "title": t.title} for t in res.topics if hasattr(t, "title")]


def _reply_and_topic(msg, is_forum: bool) -> tuple[int | None, int | None]:
    """(message replied to, topic id). In forum groups every message belongs to a topic; the
    'General' topic has id 1 and its messages carry no reply header."""
    r = getattr(msg, "reply_to", None)
    if r is None:
        return None, (1 if is_forum else None)
    if getattr(r, "forum_topic", False):
        top = getattr(r, "reply_to_top_id", None)
        if top:                      # a reply inside a topic: top = topic, msg = the replied message
            return getattr(r, "reply_to_msg_id", None), top
        return None, getattr(r, "reply_to_msg_id", None)   # a plain post in a topic
    return getattr(r, "reply_to_msg_id", None), (1 if is_forum else None)


def _norm_id(chat_id: int) -> int:
    """Telethon reports channels as -100xxxxxxxxxx; keep that form so ids are stable across calls."""
    return int(chat_id)
