import asyncio
import platform
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import sentra
from sentra.config import settings

COLOR_PRIMARY = 0x5865F2
COLOR_SUCCESS = 0x57F287
COLOR_INFO = 0xFEE75C
COLOR_DANGER = 0xED4245

PERMISSION_HIGHLIGHTS = [
    ("administrator", "Administrator"),
    ("manage_guild", "Manage Server"),
    ("manage_roles", "Manage Roles"),
    ("manage_channels", "Manage Channels"),
    ("ban_members", "Ban Members"),
    ("kick_members", "Kick Members"),
    ("moderate_members", "Timeout Members"),
    ("manage_messages", "Manage Messages"),
    ("mention_everyone", "Mention Everyone"),
]

USER_BADGES = [
    ("staff", "Discord Staff", "🛡️"),
    ("partner", "Partnered Owner", "🤝"),
    ("hypesquad", "HypeSquad Events", "🎪"),
    ("hypesquad_bravery", "HypeSquad Bravery", "🪓"),
    ("hypesquad_brilliance", "HypeSquad Brilliance", "💎"),
    ("hypesquad_balance", "HypeSquad Balance", "⚖️"),
    ("early_supporter", "Early Supporter", "🏆"),
    ("bug_hunter", "Bug Hunter", "🐛"),
    ("bug_hunter_level_2", "Bug Hunter Gold", "🥇"),
    ("active_developer", "Active Developer", "⌨️"),
]

IMAGE_FORMATS = [("PNG", "png"), ("JPG", "jpg"), ("WEBP", "webp")]


def standard_footer(embed: discord.Embed, interaction: discord.Interaction) -> discord.Embed:
    embed.set_footer(
        text=f"Requested by {interaction.user.display_name}",
        icon_url=interaction.user.display_avatar.url,
    )
    embed.timestamp = discord.utils.utcnow()
    embed.color = embed.color or discord.Color.blurple()
    return embed


async def db_ping_seconds() -> float | None:
    from sqlalchemy import text as sql_text

    from sentra.db.engine import session_factory

    try:
        start = time.perf_counter()
        async with session_factory() as session:
            await session.execute(sql_text("SELECT 1"))
        return time.perf_counter() - start
    except Exception:
        return None


def asset_format_buttons(asset: discord.Asset) -> discord.ui.View:
    view = discord.ui.View(timeout=300)
    formats = list(IMAGE_FORMATS)
    if asset.is_animated():
        formats.append(("GIF", "gif"))
    for label, fmt in formats:
        try:
            url = asset.replace(format=fmt, size=1024).url
        except Exception:
            continue
        view.add_item(discord.ui.Button(label=f"{label} · 1024px", url=url))
    return view


class GeneralCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="Latency and connection health")
    @app_commands.checks.cooldown(2, 10.0)
    async def ping(self, interaction: discord.Interaction) -> None:
        ws_ms = self.bot.latency * 1000 if self.bot.latency == self.bot.latency else None
        embed = discord.Embed(title="📡 Connection check", color=COLOR_INFO)
        api_note = "*measuring…*"
        embed.add_field(name="Gateway WebSocket", value=f"`{ws_ms:.0f} ms`" if ws_ms else "`—`")
        embed.add_field(name="API round-trip", value=api_note)

        await interaction.response.send_message(embed=standard_footer(embed, interaction))
        sent_at = time.perf_counter()
        db_ms = await db_ping_seconds()
        rtt_ms = max((discord.utils.utcnow() - interaction.created_at).total_seconds() * 1000, 0)
        embed.set_field_at(1, name="API round-trip", value=f"`{rtt_ms:.0f} ms`", inline=True)
        db_value = f"`{db_ms * 1000:.0f} ms`" if db_ms is not None else "`unreachable` 🔴"
        embed.add_field(name="Database ping", value=db_value)
        overall = COLOR_SUCCESS if (db_ms is not None and (ws_ms or 999) < 400) else COLOR_DANGER
        embed.colour = overall
        embed.description = "All systems operational." if overall == COLOR_SUCCESS else "Degraded — see fields above."
        await interaction.edit_original_response(embed=standard_footer(embed, interaction))

    @app_commands.command(name="about", description="About Sentra")
    async def about(self, interaction: discord.Interaction) -> None:
        uptime = discord.utils.utcnow() - self.bot.start_time
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes = remainder // 60
        member_count = sum(g.member_count or 0 for g in self.bot.guilds)
        command_count = len(self.bot.tree.get_commands())

        embed = discord.Embed(
            title="Sentra",
            description=(
                "**Guard first. Entertain always.**\n"
                "One bot for security, moderation, leveling, music, voice rooms and community games — "
                "built to replace half a dozen single-purpose bots."
            ),
            color=COLOR_PRIMARY,
        )
        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.add_field(name="Version", value=f"v{getattr(sentra, '__version__', '1.1.0')}")
        embed.add_field(name="Library", value=f"discord.py `{discord.__version__}`")
        embed.add_field(name="Runtime", value=f"Python `{platform.python_version()}`")
        embed.add_field(name="Uptime", value=f"{hours}h {minutes}m")
        embed.add_field(name="Servers", value=str(len(self.bot.guilds)))
        embed.add_field(name="Members reached", value=f"~{member_count:,}")
        embed.add_field(name="Slash command groups", value=str(command_count))
        embed.add_field(name="Modules loaded", value=str(len(self.bot.cogs)))
        embed.add_field(name="Team", value=settings.developer or "Sentra Team", inline=False)
        standard_footer(embed, interaction)

        invite_url = None
        if self.bot.application_id:
            perms = discord.Permissions(
                manage_guild=True,
                manage_roles=True,
                manage_channels=True,
                kick_members=True,
                ban_members=True,
                moderate_members=True,
                manage_messages=True,
                view_audit_log=True,
            )
            invite_url = discord.utils.oauth_url(
                self.bot.application_id, permissions=perms, scopes=("bot", "applications.commands")
            )
        view = discord.ui.View(timeout=300)
        if invite_url:
            view.add_item(discord.ui.Button(label="Invite Sentra", url=invite_url, emoji="➕"))
        if settings.support_url:
            view.add_item(discord.ui.Button(label="Support server", url=settings.support_url, emoji="🛟"))
        if settings.repo_url:
            view.add_item(discord.ui.Button(label="Source", url=settings.repo_url, emoji="📂"))
        if view.children:
            await interaction.response.send_message(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed)

    @app_commands.command(name="serverinfo", description="Server overview at a glance")
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        fully_cached = len(guild.members) == guild.member_count

        channel_counts = {"Text": 0, "Voice": 0, "Stage": 0, "Forum": 0, "Categories": 0}
        for channel in guild.channels:
            if isinstance(channel, discord.TextChannel):
                channel_counts["Text"] += 1
            elif isinstance(channel, discord.VoiceChannel):
                channel_counts["Voice"] += 1
            elif isinstance(channel, discord.StageChannel):
                channel_counts["Stage"] += 1
            elif isinstance(channel, discord.ForumChannel):
                channel_counts["Forum"] += 1
            elif isinstance(channel, discord.CategoryChannel):
                channel_counts["Categories"] += 1
        threads = len(guild.threads)

        top_roles = sorted(
            (r for r in guild.roles if not r.managed and r != guild.default_role),
            key=lambda r: r.position,
            reverse=True,
        )[:5]
        role_lines = []
        for role in top_roles:
            count = f" · {len(role.members)}" if fully_cached else ""
            role_lines.append(f"{role.mention}{count}")
        humans = sum(1 for m in guild.members if not m.bot) if fully_cached else None
        bots = len(guild.members) - humans if humans is not None else None

        embed = discord.Embed(title=guild.name, description=(guild.description or None), color=COLOR_PRIMARY)
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        embed.add_field(name="Owner", value=f"<@{guild.owner_id}>")
        embed.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, "R"))
        embed.add_field(
            name="Boosts",
            value=f"Lv {guild.premium_tier} · {guild.premium_subscription_count or 0} 🚀",
        )
        m_count = guild.member_count or len(guild.members)
        embed.add_field(
            name=f"Members ({m_count:,})",
            value=f"{humans:,} humans · {bots:,} bots" if humans is not None else f"{m_count:,}",
        )
        embed.add_field(
            name=f"Channels ({len(guild.channels)})",
            value=" · ".join(f"{v} {k.lower()}" for k, v in channel_counts.items() if v) + f" · {threads} threads",
        )
        embed.add_field(name=f"Roles ({len(guild.roles)})", value="\n".join(role_lines) or "*none*", inline=False)
        embed.add_field(
            name="Security",
            value=f"Verification: {guild.verification_level.name.title()} · "
            f"2FA: {'required' if getattr(guild, 'mfa_level', 0) == 1 else 'not required'}",
        )
        embed.add_field(name="Emojis / Stickers", value=f"{len(guild.emojis)} · {len(guild.stickers)}")
        embed.add_field(name="Server ID", value=f"`{guild.id}`")
        if guild.banner:
            embed.set_image(url=guild.banner.url)
        await interaction.response.send_message(embed=standard_footer(embed, interaction))

    @app_commands.command(description="Detailed profile for any member")
    @app_commands.guild_only()
    async def userinfo(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        member = user or interaction.user
        assert isinstance(member, discord.Member)
        badges = [
            f"{emoji} **{label}**"
            for attr, label, emoji in USER_BADGES
            if getattr(member.public_flags, attr, False)
        ]
        if member.bot:
            badges.insert(0, "🤖 **Bot**")
        if member.premium_since:
            badges.append("🚀 **Booster**")

        notable = [
            label
            for attr, label in PERMISSION_HIGHLIGHTS
            if getattr(member.guild_permissions, attr, False)
        ]
        perm_text = ", ".join(notable[:8]) + (" …" if len(notable) > 8 else "") if notable else "No elevated permissions"

        roles = [r for r in reversed(member.roles) if r != interaction.guild.default_role]
        shown_roles = " ".join(r.mention for r in roles[:8])
        more = f" (+{len(roles) - 8})" if len(roles) > 8 else ""

        accent = member.color if member.color.value else COLOR_PRIMARY
        embed = discord.Embed(color=accent)
        author_line = f"{member.display_name}"
        if member.nick and member.nick != member.display_name:
            author_line += f" (aka {member.name})"
        embed.set_author(name=author_line, icon_url=member.display_avatar.url)
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.description = "\n".join(badges) if badges else None
        embed.add_field(name="Username", value=f"{member.name}\n`{member.id}`")
        embed.add_field(
            name="Account created",
            value=f"{discord.utils.format_dt(member.created_at, 'D')}\n({discord.utils.format_dt(member.created_at, 'R')})",
        )
        joined_value = (
            f"{discord.utils.format_dt(member.joined_at, 'D')}\n({discord.utils.format_dt(member.joined_at, 'R')})"
            if member.joined_at
            else "unknown"
        )
        embed.add_field(name="Joined server", value=joined_value)
        if member.premium_since:
            embed.add_field(name="Boosting since", value=discord.utils.format_dt(member.premium_since, "R"))
        if member.timed_out_until and member.timed_out_until > discord.utils.utcnow():
            embed.add_field(name="Timed out until", value=discord.utils.format_dt(member.timed_out_until, "R"))
        if roles:
            embed.add_field(
                name=f"Roles ({len(roles)})",
                value=(shown_roles or "*none*") + more,
                inline=False,
            )
        embed.add_field(name="Key permissions", value=perm_text[:1024], inline=False)
        standard_footer(embed, interaction)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="avatar", description="Full-size avatar with download links")
    async def avatar(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        target = user or interaction.user
        asset = target.display_avatar
        embed = discord.Embed(
            title=f"{target.display_name}'s avatar",
            url=asset.url,
            color=COLOR_PRIMARY,
        )
        embed.set_image(url=asset.url)
        animated = "animated " if asset.is_animated() else ""
        standard_footer(embed, interaction).set_footer(
            text=f"{animated}avatar · pick a format to download",
            icon_url=interaction.user.display_avatar.url,
        )
        await interaction.response.send_message(embed=embed, view=asset_format_buttons(asset))

    @app_commands.command(name="banner", description="Profile banner — works for users and the bot itself")
    @app_commands.describe(user="Leave empty and tick bot_banner to see Sentra's own banner", of_bot="Show the bot's banner instead")
    async def banner(self, interaction: discord.Interaction, user: discord.User | None = None, of_bot: bool = False) -> None:
        await interaction.response.defer()
        if of_bot and self.bot.application_id:
            target_id = self.bot.application_id
        elif user is not None:
            target_id = user.id
        else:
            target_id = interaction.user.id
        try:
            fetched = await self.bot.fetch_user(target_id)
        except discord.HTTPException:
            await interaction.followup.send("Couldn't fetch that profile right now.")
            return
        display_name = fetched.display_name
        banner_asset = fetched.banner
        accent = fetched.accent_color or COLOR_PRIMARY

        if banner_asset is None:
            embed = discord.Embed(
                title=f"{display_name} has no custom banner",
                description="Showing the profile accent colour instead — here's their avatar meanwhile.",
                color=accent,
            )
            embed.set_image(url=fetched.display_avatar.url)
            await interaction.followup.send(embed=embed, view=asset_format_buttons(fetched.display_avatar))
            return

        embed = discord.Embed(title=f"{display_name}'s banner", url=banner_asset.url, color=accent)
        embed.set_image(url=banner_asset.url)
        animated = "animated " if banner_asset.is_animated() else ""
        footer_embed = standard_footer(embed, interaction)
        footer_embed.set_footer(text=f"{animated}banner · pick a format to download")
        await interaction.followup.send(embed=embed, view=asset_format_buttons(banner_asset))

    @app_commands.command(name="status", description="Bot health, versions and resource usage")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        started = time.perf_counter()
        db_ok = await db_ping_seconds()
        db_ms = db_ok * 1000 if db_ok is not None else None

        def loop_state(cog_name: str, attr: str) -> str:
            cog = self.bot.get_cog(cog_name)
            loop = getattr(cog, attr, None)
            if loop is None:
                return "—"
            return "🟢" if loop.is_running() else "🔴"

        start_dt = getattr(self.bot, "start_time", discord.utils.utcnow())
        uptime = discord.utils.utcnow() - start_dt
        hours, remainder = divmod(int(uptime.total_seconds()), 3600)
        minutes = remainder // 60

        memory_mb = cpu_percent = threads = None
        try:
            import psutil

            process = psutil.Process()
            memory_mb = process.memory_info().rss / (1024 * 1024)
            threads = process.num_threads()
            cpu_percent = process.cpu_percent(interval=None)
        except Exception:
            pass

        premium_active = len([g for g in self.bot.guilds if g.id in settings.premium_guild_ids])

        embed = discord.Embed(
            title="🩺 Sentra status",
            description=("All systems operational." if db_ok else "Database unreachable — features degraded."),
            color=COLOR_SUCCESS if db_ok else COLOR_DANGER,
        )
        embed.add_field(name="Version", value=f"v{getattr(sentra, '__version__', '1.1.0')}")
        embed.add_field(name="Uptime", value=f"{hours}h {minutes}m")
        raw_latency = self.bot.latency * 1000
        latency_str = f"`{round(raw_latency)}ms`" if raw_latency == raw_latency else "`—`"
        embed.add_field(name="Gateway", value=latency_str)
        embed.add_field(name="Database", value=f"`{db_ms:.0f}ms` ({time.perf_counter() - started:.2f}s probe)" if db_ms is not None else "🔴 unreachable")
        if memory_mb is not None:
            embed.add_field(name="Memory", value=f"{memory_mb:.0f} MB")
        if cpu_percent is not None:
            embed.add_field(name="CPU", value=f"{cpu_percent:.1f}%")
        if threads is not None:
            embed.add_field(name="Threads", value=str(threads))
        embed.add_field(name="Servers", value=str(len(self.bot.guilds)))
        embed.add_field(name="Commands", value=str(len(self.bot.tree.get_commands())))
        embed.add_field(name="Premium servers", value=str(premium_active))
        embed.add_field(
            name="Background loops",
            value=(
                f"Sentinel {loop_state('ShieldCog', 'sentinel')} · "
                f"Reminders {loop_state('RemindersCog', 'dispatch_loop')} · "
                f"Unbans {loop_state('ModerationCog', 'unban_loop')} · "
                f"Voice sweep {loop_state('VoiceCog', '_sweeper')} · "
                f"Music sweep {loop_state('MusicCog', '_sweeper')}"
            ),
            inline=False,
        )
        embed.add_field(name="Stack", value=f"discord.py `{discord.__version__}` · Python `{platform.python_version()}`")
        standard_footer(embed, interaction)
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(GeneralCog(bot))
