"""Shared embed styling helpers for Sentra."""
from __future__ import annotations

import discord

BRAND = discord.Color.from_str("#5865F2")  # Sentra blurple

# Semantic palette keyed by module / action.
SEC = discord.Color.from_str("#C0392B")        # anti-nuke / shield
AUTOMOD = discord.Color.from_str("#E67E22")    # content filtering
WARN = discord.Color.from_str("#F1C40F")
TIMEOUT = discord.Color.from_str("#E67E22")
KICK = discord.Color.from_str("#ED4245")
DANGER = discord.Color.from_str("#ED4245")
BAN = discord.Color.from_str("#720101")
LEVEL = discord.Color.from_str("#2ECC71")
MUSIC = discord.Color.from_str("#9B59B6")
VOICE = discord.Color.from_str("#1ABC9C")
REMINDER = BRAND
PERSONA = discord.Color.from_str("#9B59B6")
FUN = discord.Color.from_str("#9B59B6")
SETUP = discord.Color.from_str("#5865F2")
UTILITY = discord.Color.from_str("#9B59B6")
WARNING = discord.Color.from_str("#F1C40F")

EMOJI = {
    "automod": "🤖",
    "security": "🛡️",
    "mod": "🔨",
    "levels": "🏆",
    "music": "🎵",
    "voice": "🔈",
    "remind": "⏰",
    "profile": "✨",
    "fun": "🎉",
    "utility": "🧰",
    "setup": "🧩",
    "honeypot": "🍯",
}

# Custom sentra emoji (tick / cross / warn) — use these everywhere instead of
# the unicode defaults ✅ ❌ ⚠️.
TICK = "<:sentra_check:1546486790779572385>"
CROSS = "<:sentra_cross:1546486840822075512>"
WARN = "<:sentra_warn:1546486928096890951>"


def style(
    embed: discord.Embed,
    interaction: discord.Interaction | None = None,
    *,
    emoji: str | None = None,
    color: discord.Color | None = None,
) -> discord.Embed:
    """Ensure a default color, then append a requestor footer + live timestamp.

    Preserves any footer text already set (e.g. badge lines) by appending to it.
    Safe to call with ``interaction=None`` for non-command alert embeds.
    """
    if color is not None:
        embed.color = color
    elif embed.color is None or embed.color.value == 0:
        embed.color = BRAND

    if interaction is not None and interaction.user is not None:
        user_part = f"Requested by {interaction.user.display_name}"
        if emoji:
            user_part = f"{emoji} {user_part}"
        existing = embed.footer.text
        if existing:
            embed.set_footer(
                text=f"{existing}  ·  {user_part}",
                icon_url=interaction.user.display_avatar.url,
            )
        else:
            embed.set_footer(text=user_part, icon_url=interaction.user.display_avatar.url)

    embed.timestamp = discord.utils.utcnow()
    return embed
