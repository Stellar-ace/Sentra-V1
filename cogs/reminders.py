import re
from datetime import timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import delete, select

from sentra.db.engine import session_factory
from sentra.db.models import Reminder
from sentra.db.models import utcnow

UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
MAX_SECONDS = 365 * 86400


def parse_duration(text: str) -> int | None:
    matches = DURATION_RE.findall(text)
    if not matches or DURATION_RE.sub("", text).strip():
        return None
    total = sum(int(n) * UNIT_SECONDS[u.lower()] for n, u in matches)
    return total if 0 < total <= MAX_SECONDS else None


def to_discord_ts(naive_dt) -> str:
    return f"<t:{int(naive_dt.replace(tzinfo=timezone.utc).timestamp())}:R>"


class RemindersCog(commands.Cog):
    remind = app_commands.Group(name="remind", description="Set and manage reminders", guild_only=True)

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.dispatch_loop.start()

    def cog_unload(self) -> None:
        self.dispatch_loop.cancel()

    @tasks.loop(seconds=30)
    async def dispatch_loop(self) -> None:
        now = utcnow()
        async with session_factory() as session:
            due = (
                await session.execute(select(Reminder).where(Reminder.due_at <= now).limit(25))
            ).scalars().all()
            for reminder in due:
                channel = self.bot.get_channel(reminder.channel_id)
                if isinstance(channel, (discord.TextChannel, discord.Thread)):
                    if reminder.user_id == 0:
                        embed = discord.Embed(
                            title="📢 Announcement",
                            description=reminder.content[:2000],
                            color=discord.Color.gold(),
                        )
                        content = None
                    else:
                        embed = discord.Embed(
                            title="⏰ Reminder",
                            description=reminder.content[:2000],
                            color=discord.Color.blurple(),
                        )
                        embed.set_footer(text=f"Set {to_discord_ts(reminder.created_at)}")
                        content = f"<@{reminder.user_id}>"
                    try:
                        await channel.send(
                            content=content, embed=embed,
                            allowed_mentions=discord.AllowedMentions(
                                everyone=False, roles=False, replied_user=False,
                                users=[discord.Object(id=reminder.user_id)] if reminder.user_id else False,
                            ),
                        )
                    except discord.HTTPException:
                        continue  # Retain the reminder for a later delivery attempt.
                if reminder.repeat_seconds:
                    reminder.due_at = now + timedelta(seconds=reminder.repeat_seconds)
                else:
                    await session.execute(delete(Reminder).where(Reminder.id == reminder.id))
            await session.commit()

    @dispatch_loop.before_loop
    async def before_dispatch(self) -> None:
        await self.bot.wait_until_ready()

    @remind.command(name="me", description="Set a reminder")
    @app_commands.checks.cooldown(2, 10.0)
    @app_commands.describe(
        when='When to remind you, e.g. "2h30m" or "1d"',
        message="What to remind you about",
        repeat="Optional repeat interval, e.g. \"1w\" (empty = one-off)",
    )
    async def me(
        self,
        interaction: discord.Interaction,
        when: str,
        message: str,
        repeat: str | None = None,
    ) -> None:
        seconds = parse_duration(when)
        if seconds is None:
            await interaction.response.send_message(
                'Could not read that duration. Try "45m", "2h30m", or "1d".', ephemeral=True
            )
            return
        repeat_seconds = parse_duration(repeat) if repeat else None
        if repeat and repeat_seconds is None:
            await interaction.response.send_message("Invalid repeat interval. Try `1h` or `1w`.", ephemeral=True)
            return
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("Use this command in a server text channel or thread.", ephemeral=True)
            return
        due = utcnow() + timedelta(seconds=seconds)
        assert interaction.guild is not None
        # Enforce per-user reminder limit to prevent DB bloat
        from sqlalchemy import func
        async with session_factory() as session:
            count = (
                await session.execute(
                    select(func.count()).select_from(Reminder).where(
                        Reminder.guild_id == interaction.guild.id,
                        Reminder.user_id == interaction.user.id,
                    )
                )
            ).scalar_one()
            if count >= 25:
                await interaction.response.send_message(
                    "You've hit the limit of **25** reminders in this server. Remove some with `/remind remove` first.",
                    ephemeral=True,
                )
                return
            session.add(
                Reminder(
                    guild_id=interaction.guild.id,
                    channel_id=interaction.channel.id,
                    user_id=interaction.user.id,
                    content=message,
                    due_at=due,
                    repeat_seconds=repeat_seconds,
                )
            )
            await session.commit()
        suffix = f" repeating every {repeat}" if repeat_seconds else ""
        await interaction.response.send_message(f"Got it — I'll remind you {to_discord_ts(due)}{suffix}.")

    @remind.command(name="list", description="List your reminders in this server")
    async def list_reminders(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(Reminder)
                    .where(Reminder.guild_id == interaction.guild.id, Reminder.user_id == interaction.user.id)
                    .order_by(Reminder.due_at)
                    .limit(10)
                )
            ).scalars().all()
        if not rows:
            await interaction.response.send_message("You have no reminders here.", ephemeral=True)
            return
        lines = [f"`#{r.id}` {to_discord_ts(r.due_at)}{' 🔁' if r.repeat_seconds else ''} — {r.content[:80]}" for r in rows]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @remind.command(name="remove", description="Remove one of your reminders")
    async def remove(self, interaction: discord.Interaction, id: int) -> None:
        assert interaction.guild is not None
        async with session_factory() as session:
            result = await session.execute(
                delete(Reminder).where(
                    Reminder.id == id,
                    Reminder.user_id == interaction.user.id,
                    Reminder.guild_id == interaction.guild.id,
                )
            )
            await session.commit()
        if result.rowcount:
            await interaction.response.send_message(f"Removed reminder `#{id}`.", ephemeral=True)
        else:
            await interaction.response.send_message("No matching reminder found.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RemindersCog(bot))
