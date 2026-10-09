import logging
import re
from datetime import timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import delete, select

from sentra.db.engine import session_factory
from sentra.db.models import ModCase, TimedAction
from sentra.ui import EMOJI, style, TICK, WARN

log = logging.getLogger("sentra.mod")

ACTION_COLORS = {
    "warn": discord.Color.yellow(),
    "timeout": discord.Color.orange(),
    "untimeout": discord.Color.green(),
    "kick": discord.Color.red(),
    "ban": discord.Color.dark_red(),
}


def has_perm(member: object, perm: str) -> bool:
    return (
        isinstance(member, discord.Member)
        and getattr(member.guild_permissions, perm, False)
    )


def case_embed(
    title: str,
    color: discord.Color,
    user_id: int,
    moderator_id: int,
    reason: str | None,
    extra: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="User", value=f"<@{user_id}> (`{user_id}`)")
    embed.add_field(name="Moderator", value=f"<@{moderator_id}>")
    if extra:
        embed.add_field(name="Details", value=extra)
    if reason:
        embed.add_field(name="Reason", value=reason[:1000], inline=False)
    embed.set_footer(text="Sentra · moderation")
    embed.timestamp = discord.utils.utcnow()
    return embed


class ModerationCog(commands.Cog):
    mod = app_commands.Group(
        name="mod",
        description="Moderation tools",
        guild_only=True,
        default_permissions=discord.Permissions(moderate_members=True),
    )
    modcase = app_commands.Group(
        name="modcase",
        description="View moderation history",
        guild_only=True,
        default_permissions=discord.Permissions(moderate_members=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.unban_loop.start()

    def cog_unload(self) -> None:
        self.unban_loop.cancel()

    @tasks.loop(seconds=30)
    async def unban_loop(self) -> None:
        await self.bot.wait_until_ready()
        from sentra.db.models import utcnow as db_utcnow

        now = db_utcnow()
        async with session_factory() as session:
            due = (
                await session.execute(
                    select(TimedAction).where(TimedAction.action == "unban", TimedAction.run_at <= now)
                    .order_by(TimedAction.run_at).limit(20)
                )
            ).scalars().all()
        for action in due:
            guild = self.bot.get_guild(action.guild_id)
            if guild is None:
                continue
            succeeded = False
            try:
                await guild.unban(discord.Object(id=action.user_id), reason="Temporary ban expired")
                succeeded = True
            except discord.NotFound:
                pass  # Already unbanned; the timer is complete.
            except discord.HTTPException as exc:
                log.warning("auto-unban failed in %s: %s", action.guild_id, exc)
                continue  # Keep the timer for a later retry.
            async with session_factory() as session:
                if succeeded:
                    session.add(ModCase(
                        guild_id=guild.id, user_id=action.user_id,
                        moderator_id=self.bot.user.id if self.bot.user else 0,
                        action="auto_unban", reason=f"Ban #{action.created_by or 'timer'} expired",
                    ))
                await session.execute(delete(TimedAction).where(TimedAction.id == action.id))
                await session.commit()

    @unban_loop.before_loop
    async def before_unban_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def modlog_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        shield = self.bot.get_cog("ShieldCog")
        channel_id = None
        if shield:
            config = await shield.get_config(guild.id)
            channel_id = config.get("alerts_channel_id")
        channel = guild.get_channel(channel_id) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def echo_to_modlog(self, guild: discord.Guild | None, embed: discord.Embed) -> None:
        if guild is None:
            return
        channel = await self.modlog_channel(guild)
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    async def log_case(
        self,
        guild_id: int,
        user_id: int,
        moderator_id: int,
        action: str,
        reason: str | None,
    ) -> None:
        async with session_factory() as session:
            session.add(
                ModCase(
                    guild_id=guild_id,
                    user_id=user_id,
                    moderator_id=moderator_id,
                    action=action,
                    reason=reason,
                )
            )
            await session.commit()

    async def notify_user(self, member: discord.Member, embed: discord.Embed) -> None:
        try:
            await member.send(embed=embed)
        except discord.HTTPException:
            pass

    @mod.command(name="warn", description="Warn a member")
    @app_commands.describe(user="The member to warn", reason="Why they are being warned")
    async def warn(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str | None = None,
    ) -> None:
        assert interaction.guild is not None
        if not has_perm(interaction.user, "moderate_members"):
            await interaction.response.send_message("Missing permission.", ephemeral=True)
            return
        await self.log_case(interaction.guild.id, user.id, interaction.user.id, "warn", reason)
        embed = case_embed("Warn issued", ACTION_COLORS["warn"], user.id, interaction.user.id, reason)
        await self.notify_user(user, embed)
        await self.echo_to_modlog(interaction.guild, embed)
        await interaction.response.send_message(embed=embed)

    @mod.command(name="timeout", description="Timeout a member")
    @app_commands.describe(user="The member to timeout", minutes="Duration in minutes (max 28 days)", reason="Why")
    async def timeout_cmd(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        minutes: app_commands.Range[int, 1, 40320],
        reason: str | None = None,
    ) -> None:
        assert interaction.guild is not None
        me = interaction.guild.me
        if not has_perm(interaction.user, "moderate_members"):
            await interaction.response.send_message("Missing permission.", ephemeral=True)
            return
        if user.top_role >= me.top_role and interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message("I cannot moderate that member.", ephemeral=True)
            return
        until = discord.utils.utcnow() + timedelta(minutes=minutes)
        try:
            await user.timeout(until, reason=reason or f"By {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message("I lack permission to timeout that member.", ephemeral=True)
            return
        await self.log_case(interaction.guild.id, user.id, interaction.user.id, "timeout", reason)
        extra = f"{minutes} minute(s), until <t:{int(until.timestamp())}:R>"
        embed = case_embed("Timeout applied", ACTION_COLORS["timeout"], user.id, interaction.user.id, reason, extra)
        await self.notify_user(user, embed)
        await self.echo_to_modlog(interaction.guild, embed)
        await interaction.response.send_message(embed=embed)

    @mod.command(name="untimeout", description="Remove a member's timeout")
    async def untimeout(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        assert interaction.guild is not None
        if not has_perm(interaction.user, "moderate_members"):
            await interaction.response.send_message("Missing permission.", ephemeral=True)
            return
        await user.timeout(None, reason=f"Untimeout by {interaction.user}")
        await self.log_case(interaction.guild.id, user.id, interaction.user.id, "untimeout", None)
        await interaction.response.send_message(
            embed=case_embed("Timeout removed", ACTION_COLORS["untimeout"], user.id, interaction.user.id, None)
        )

    @mod.command(name="kick", description="Kick a member")
    async def kick(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str | None = None,
    ) -> None:
        assert interaction.guild is not None
        if not has_perm(interaction.user, "kick_members"):
            await interaction.response.send_message("Missing permission.", ephemeral=True)
            return
        if user.top_role >= interaction.guild.me.top_role:
            await interaction.response.send_message("I cannot moderate that member.", ephemeral=True)
            return
        embed = case_embed("You were kicked", ACTION_COLORS["kick"], user.id, interaction.user.id, reason)
        await self.notify_user(user, embed)
        await interaction.guild.kick(user, reason=reason or f"By {interaction.user}")
        await self.log_case(interaction.guild.id, user.id, interaction.user.id, "kick", reason)
        embed = case_embed("Kicked", ACTION_COLORS["kick"], user.id, interaction.user.id, reason)
        await self.echo_to_modlog(interaction.guild, embed)
        await interaction.response.send_message(embed=embed)

    @mod.command(name="ban", description="Ban a user (works even if not in the server)")
    @app_commands.describe(
        user="The user to ban",
        delete_days="Days of messages to delete",
        duration_minutes="Leave 0 for permanent; otherwise auto-unban after this many minutes",
        reason="Why",
    )
    async def ban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        delete_days: app_commands.Range[int, 0, 7] = 1,
        duration_minutes: app_commands.Range[int, 0, 525600] = 0,
        reason: str | None = None,
    ) -> None:
        assert interaction.guild is not None
        if not has_perm(interaction.user, "ban_members"):
            await interaction.response.send_message("Missing permission.", ephemeral=True)
            return
        try:
            await interaction.guild.ban(
                user,
                delete_message_seconds=delete_days * 86400,
                reason=reason or f"By {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.response.send_message("I lack permission to ban that user.", ephemeral=True)
            return
        await self.log_case(interaction.guild.id, user.id, interaction.user.id, "ban", reason)
        extra = None
        title = "Banned"
        if duration_minutes > 0:
            until = discord.utils.utcnow() + timedelta(minutes=duration_minutes)
            async with session_factory() as session:
                session.add(
                    TimedAction(
                        guild_id=interaction.guild.id,
                        user_id=user.id,
                        action="unban",
                        run_at=until.replace(tzinfo=None),
                        created_by=interaction.user.id,
                    )
                )
                await session.commit()
            extra = f"Temporary — auto-unban <t:{int(until.timestamp())}:R>"
            title = "Temp-banned"
        embed = case_embed(title, ACTION_COLORS["ban"], user.id, interaction.user.id, reason, extra)
        await self.echo_to_modlog(interaction.guild, embed)
        await interaction.response.send_message(embed=embed)

    @mod.command(name="massban", description="Ban up to 25 user IDs at once (raid cleanup)")
    @app_commands.describe(ids="Space or comma separated user IDs", reason="Applied to every ban")
    async def massban(self, interaction: discord.Interaction, ids: str, reason: str) -> None:
        assert interaction.guild is not None
        if not has_perm(interaction.user, "ban_members"):
            await interaction.response.send_message("Missing permission.", ephemeral=True)
            return
        raw = re.split(r"[\s,]+", ids.strip())
        targets: list[int] = []
        for chunk in raw:
            if chunk.isdigit() and 17 <= len(chunk) <= 20:
                parsed = int(chunk)
                if parsed not in targets:
                    targets.append(parsed)
        targets = targets[:25]
        if not targets:
            await interaction.response.send_message("No valid user IDs found.", ephemeral=True)
            return
        await interaction.response.defer()
        banned, failed = [], []
        for target_id in targets:
            try:
                await interaction.guild.ban(
                    discord.Object(id=target_id),
                    delete_message_seconds=86400,
                    reason=f"Massban by {interaction.user}: {reason}",
                )
                banned.append(target_id)
                await self.log_case(interaction.guild.id, target_id, interaction.user.id, "massban", reason)
            except (discord.HTTPException, ValueError):
                failed.append(target_id)
        lines = [f"<:sentra_check:1546486790779572385> Banned **{len(banned)}** · <:sentra_warn:1546486928096890951> Failed **{len(failed)}**", f"Reason: {reason}"]
        if failed:
            lines.append("Failed IDs: " + ", ".join(map(str, failed[:10])))
        embed = discord.Embed(title="Massban complete", description="\n".join(lines), color=ACTION_COLORS["ban"])
        embed.set_footer(text=f"By {interaction.user}")
        await self.echo_to_modlog(interaction.guild, embed)
        await interaction.followup.send(embed=embed)

    @mod.command(name="purge", description="Bulk delete recent messages in this channel")
    @app_commands.describe(
        count="How many messages (1-100)",
        user="Only delete this user's messages",
        bots_only="Only bot messages",
        links_only="Only messages containing links",
        attachments_only="Only messages with files/images",
    )
    async def purge(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 100],
        user: discord.User | None = None,
        bots_only: bool = False,
        links_only: bool = False,
        attachments_only: bool = False,
    ) -> None:
        assert interaction.channel is not None
        if not has_perm(interaction.user, "manage_messages"):
            await interaction.response.send_message("Missing permission.", ephemeral=True)
            return

        def check(message: discord.Message) -> bool:
            if user is not None and message.author.id != user.id:
                return False
            if bots_only and not message.author.bot:
                return False
            if links_only and not re.search(r"https?://", message.content):
                return False
            if attachments_only and not message.attachments:
                return False
            return True

        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=count, check=check)
        await interaction.followup.send(f"Deleted {len(deleted)} message(s).")

    @modcase.command(name="view", description="View a user's moderation history")
    async def view_cases(
        self, interaction: discord.Interaction, user: discord.User
    ) -> None:
        assert interaction.guild is not None
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(ModCase)
                    .where(ModCase.guild_id == interaction.guild.id, ModCase.user_id == user.id)
                    .order_by(ModCase.created_at.desc())
                    .limit(10)
                )
            ).scalars().all()
        if not rows:
            await interaction.response.send_message(f"{user.mention} has a clean record here.", ephemeral=True)
            return
        lines = [
            f"`#{r.id}` **{r.action}** — <t:{int(r.created_at.replace(tzinfo=timezone.utc).timestamp())}:R> by <@{r.moderator_id}>"
            + (f"\n   {r.reason[:120]}" if r.reason else "")
            for r in rows
        ]
        embed = discord.Embed(
            title=f"Mod history — {user}",
            description="\n".join(lines)[:4000],
            color=discord.Color.orange(),
        )
        style(embed, interaction, emoji=EMOJI["mod"])
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    cog = ModerationCog(bot)
    await bot.add_cog(cog)
