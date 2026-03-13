"""
CLI tool — Send commands to Degen Scanner bot via Telegram.

Uses a separate Telethon session (cli_session) to avoid
conflicting with the running bot's user_client session.

First run: requires phone auth (one-time only).
After that: just run `python cli.py <command>`.

Usage:
    python cli.py scan              # /scan
    python cli.py scanbig           # /scanbig
    python cli.py status            # /status
    python cli.py patterns          # /patterns
    python cli.py exchanges         # /exchanges
    python cli.py help              # /help
    python cli.py send <text>       # send any text to bot
    python cli.py logs [N]          # show last N lines of bot_stderr.log
"""

import sys
import asyncio
import argparse

from telethon import TelegramClient
from config import API_ID, API_HASH, BOT_TOKEN

# Separate session to avoid SQLite lock with bot.py
CLI_SESSION = "cli_session"

# Extract bot username from token (numeric ID before ':')
BOT_ID = int(BOT_TOKEN.split(":")[0])


async def send_command(command: str):
    """Connect as user, send command to bot, print bot's reply."""
    client = TelegramClient(CLI_SESSION, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("❌ Session expired. Re-copy: cp degen_scanner.session cli_session.session")
        return

    try:
        # Send command to bot
        await client.send_message(BOT_ID, command)
        print(f"📤 Sent: {command}")

        # Wait for bot reply (up to 30s)
        await asyncio.sleep(2)

        # Read last messages from bot conversation
        messages = []
        async for msg in client.iter_messages(BOT_ID, limit=5):
            if msg.out:
                break
            messages.append(msg)

        if messages:
            messages.reverse()
            for msg in messages:
                print(f"📥 Bot reply:\n{msg.text}\n")
        else:
            print("⏳ No reply yet (bot may still be processing)")

    finally:
        await client.disconnect()


async def send_and_wait_long(command: str, timeout: int = 60):
    """Send command and wait longer for reply (for /scan, /scanbig)."""
    client = TelegramClient(CLI_SESSION, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("❌ Session expired. Re-copy: cp degen_scanner.session cli_session.session")
        return

    try:
        await client.send_message(BOT_ID, command)
        print(f"📤 Sent: {command}")
        print(f"⏳ Waiting up to {timeout}s for reply...")

        # Poll for reply
        start = asyncio.get_event_loop().time()
        seen_ids = set()

        while (asyncio.get_event_loop().time() - start) < timeout:
            await asyncio.sleep(2)
            async for msg in client.iter_messages(BOT_ID, limit=5):
                if msg.out:
                    break
                if msg.id not in seen_ids:
                    seen_ids.add(msg.id)
                    print(f"📥 Bot reply:\n{msg.text}\n")

            # If we got a "complete" or "done" message, stop
            if seen_ids:
                latest = None
                async for msg in client.iter_messages(BOT_ID, limit=1):
                    if not msg.out:
                        latest = msg
                if latest and any(
                    kw in (latest.text or "").lower()
                    for kw in ["complete", "done", "found"]
                ):
                    break

    finally:
        await client.disconnect()


def show_logs(n: int = 30):
    """Show last N lines of bot_stderr.log."""
    import os
    log_path = os.path.join(os.path.dirname(__file__), "bot_stderr.log")
    if not os.path.exists(log_path):
        print("❌ bot_stderr.log not found")
        return

    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    for line in lines[-n:]:
        print(line.rstrip())


def main():
    parser = argparse.ArgumentParser(description="Degen Scanner CLI")
    parser.add_argument(
        "command",
        help="Command: scan, scanbig, status, patterns, exchanges, help, send, logs",
    )
    parser.add_argument(
        "args",
        nargs="*",
        help="Extra args (for 'send': message text, for 'logs': line count)",
    )

    args = parser.parse_args()
    cmd = args.command.lower()

    # Commands that map to bot /commands
    CMD_MAP = {
        "scan": "/scan",
        "scanbig": "/scanbig",
        "status": "/status",
        "patterns": "/patterns",
        "exchanges": "/exchanges",
        "help": "/help",
        "start": "/start",
    }

    if cmd == "logs":
        n = int(args.args[0]) if args.args else 30
        show_logs(n)
        return

    if cmd == "send":
        if not args.args:
            print("Usage: python cli.py send <text>")
            return
        text = " ".join(args.args)
        asyncio.run(send_command(text))
        return

    if cmd in CMD_MAP:
        bot_cmd = CMD_MAP[cmd]
        # scan/scanbig need longer wait
        if cmd in ("scan", "scanbig"):
            asyncio.run(send_and_wait_long(bot_cmd, timeout=60))
        else:
            asyncio.run(send_command(bot_cmd))
        return

    # Unknown — try as raw command
    print(f"Unknown command: {cmd}")
    print("Available: scan, scanbig, status, patterns, exchanges, help, send, logs")


if __name__ == "__main__":
    main()
