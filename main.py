#!/usr/bin/env python3
"""
Sentra Bot Main Entry Point

Start the bot:
    python main.py

Pterodactyl Egg Startup:
    Main File: main.py
    Startup Command: python main.py (or python -u main.py)
"""
import logging
import os
import sys
from pathlib import Path

# Ensure sentra package resolution works immediately without 'pip install -e .'
import _bootstrap  # noqa: F401


def ensure_env_file(root_dir: Path) -> bool:
    """Creates a template .env file if none exists yet."""
    env_path = root_dir / ".env"
    example_path = root_dir / ".env.example"

    if env_path.exists():
        return True

    if example_path.exists():
        template = example_path.read_text(encoding="utf-8")
    else:
        template = (
            "# Sentra Bot Environment Variables\n"
            "DISCORD_TOKEN=your_bot_token_here\n"
            "DATABASE_URL=sqlite+aiosqlite:///sentra.db\n"
            "SYNC_COMMANDS=true\n"
            "ABOUT_DEVELOPER=Sentra Team\n"
        )

    try:
        env_path.write_text(template, encoding="utf-8")
        print("\n" + "=" * 65)
        print("[Sentra] Notice: Created a new '.env' file for you.")
        print("[Sentra] Please open '.env', add your DISCORD_TOKEN, and restart the bot.")
        print("=" * 65 + "\n")
        return False
    except OSError as err:
        print(f"[Sentra] Warning: Could not create .env file: {err}")
        return False


def main() -> None:
    root_dir = Path(__file__).resolve().parent

    # Check for .env file
    if not ensure_env_file(root_dir):
        sys.exit(1)

    # Import settings after environment file check
    from sentra.config import settings

    # Clean check on bot token
    token = settings.token
    if not token or token.lower() in {
        "your_bot_token_here",
        "token_here",
        "replace_me",
        "your_token_here",
    }:
        print("\n" + "=" * 65)
        print("[ERROR] DISCORD_TOKEN is missing or not configured!")
        print("Please edit the '.env' file and set DISCORD_TOKEN:")
        print("    DISCORD_TOKEN=your_token_here")
        print()
        print("Get your bot token at: https://discord.com/developers/applications")
        print("=" * 65 + "\n")
        sys.exit(1)

    # Start the bot
    from sentra.bot import SentraBot

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        settings.validate()
    except ValueError as err:
        print(f"\n[ERROR] Configuration error: {err}\n")
        sys.exit(1)

    bot = SentraBot()
    try:
        bot.run(settings.token, log_handler=None)
    except KeyboardInterrupt:
        print("\n[Sentra] Bot process terminated by user (Ctrl+C).")
    except Exception as err:
        logging.exception("Fatal error while running Sentra bot: %s", err)
        sys.exit(1)


if __name__ == "__main__":
    main()
