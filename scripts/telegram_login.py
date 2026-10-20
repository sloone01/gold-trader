"""One-time Telegram login for the signal listener. Run on the VPS, in a normal console:

    py -m scripts.telegram_login

Reads TG_API_ID / TG_API_HASH / TG_SESSION from .env (or the environment), asks for your phone
number and the code Telegram sends you, then saves the session file. Restart the server afterwards.
"""
import asyncio
import os
import sys
from pathlib import Path


def load_env() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


async def run() -> None:
    from telethon import TelegramClient

    session = os.environ.get("TG_SESSION", "telegram.session")
    client = TelegramClient(session, int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"])
    try:
        print("Connecting to Telegram…", flush=True)
        try:
            await asyncio.wait_for(client.connect(), timeout=30)
        except asyncio.TimeoutError:
            raise SystemExit("Could not reach Telegram within 30 s. Check the VPS internet connection or firewall "
                             "(outbound TCP 443/80 to Telegram) and try again.") from None
        if not await client.is_user_authorized():
            phone = input("Phone number with country code (e.g. +9689xxxxxxx): ").strip()
            print("Asking Telegram to send a login code…", flush=True)
            await client.send_code_request(phone)
            code = input("Code Telegram sent to your app: ").strip()
            try:
                await client.sign_in(phone, code)
            except Exception as ex:  # two-step verification password
                if "password" in str(ex).lower() or type(ex).__name__ == "SessionPasswordNeededError":
                    pw = input("Two-step verification password: ")
                    await client.sign_in(password=pw)
                else:
                    raise
        me = await client.get_me()
        print(f"\nLogged in as {me.first_name} (@{me.username}). Session saved to {session}.")
        print("Channels and groups you are in (listing your chats, this can take a moment):", flush=True)
        seen = 0
        async for d in client.iter_dialogs():
            seen += 1
            if d.is_channel or d.is_group:
                print(f"  {d.id:>16}  {d.name}", flush=True)
            elif seen % 50 == 0:
                print(f"  … {seen} chats checked", flush=True)
        print("\nDone. Now restart the server:  powershell -ExecutionPolicy Bypass -File scripts\\restart_server.ps1")
    finally:
        await client.disconnect()


def main() -> None:
    load_env()
    if not os.environ.get("TG_API_ID") or not os.environ.get("TG_API_HASH"):
        raise SystemExit("Set TG_API_ID and TG_API_HASH in .env first (from https://my.telegram.org).")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nCancelled. If you were already logged in, the session is saved; otherwise run the command again.")
        return
    # Telethon's background tasks complain noisily when the loop closes on Python 3.14; nothing is wrong.
    os._exit(0)


if __name__ == "__main__":
    main()
