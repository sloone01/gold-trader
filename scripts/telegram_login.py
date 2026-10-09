"""One-time Telegram login for the signal listener. Run on the VPS, in a normal console:

    py -m scripts.telegram_login

Reads TG_API_ID / TG_API_HASH / TG_SESSION from .env (or the environment), asks for your phone
number and the code Telegram sends you, then saves the session file. Restart the server afterwards.
"""
import os
from pathlib import Path


def load_env() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def main() -> None:
    load_env()
    api_id, api_hash = os.environ.get("TG_API_ID"), os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("Set TG_API_ID and TG_API_HASH in .env first (from https://my.telegram.org).")
    from telethon import TelegramClient

    session = os.environ.get("TG_SESSION", "telegram.session")
    with TelegramClient(session, int(api_id), api_hash) as client:
        me = client.get_me()
        print(f"Logged in as {me.first_name} (@{me.username}). Session saved to {session}.")
        print("Channels and groups you are in:")
        for d in client.iter_dialogs():
            if d.is_channel or d.is_group:
                print(f"  {d.id:>16}  {d.name}")
        print("Pick the ones to follow in the dashboard (Signals -> Channels). Then restart the server.")


if __name__ == "__main__":
    main()
