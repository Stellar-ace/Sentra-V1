from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from sentra.db.engine import session_factory
from sentra.db.models import Guild, ModCase, utcnow
from sentra.ui import BRAND, EMOJI, style

HONEYPOT_DEFAULTS: dict = {
    "channel_id": None,
    "panel_message_id": None,
    "log_channel_id": None,
    "hits": 0,
    "hits_by_channel": {},
    "enabled": False,
    "action": "softban",
    "softban_delete_days": 1,
    "timeout_minutes": 60,
}

ACTION_VERB = {
    "softban": "**softbanned** (banned, then unbanned so they can rejoin)",
    "ban": "**banned**",
    "kick": "**kicked**",
    "timeout": "**timed out**",
}

DEBOUNCE_SECONDS = 5
MAX_BREAKDOWN_LINES = 10
MAX_TIMEOUT_MINUTES = 28 * 24 * 60  # Discord timeout cap: 28 days


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


class HoneypotCog(commands.Cog):
    honeypot = app_commands.Group(
        name="honeypot",
        description="Spam trap — punishes anyone who posts in the trap channel",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._lock = asyncio.Lock()
        self._debounce: dict[int, int] = {}  # channel_id -> last panel edit (monotonic)

    # ------------------------------------------------------------------ config

    async def get_config(self, guild_id: int) -> dict:
        async with session_factory() as session:
            guild = await session.get(Guild, guild_id)
            stored = dict((guild.config or {}).get("honeypot", {})) if guild else {}
        cfg = dict(HONEYPOT_DEFAULTS)
        for k, v in stored.items():
            if isinstance(v, dict):
                cfg[k] = dict(v)
            elif isinstance(v, list):
                cfg[k] = list(v)
            else:
                cfg[k] = v
        for k, v in HONEYPOT_DEFAULTS.items():
            cfg.setdefault(k, dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
        cfg["enabled"] = bool(cfg.get("enabled", False))
        cfg["hits"] = int(cfg.get("hits", 0) or 0)
        if cfg.get("action") not in ACTION_VERB:
            cfg["action"] = "softban"
        cfg["softban_delete_days"] = _clamp(cfg.get("softban_delete_days", 1), 0, 7)
        cfg["timeout_minutes"] = _clamp(cfg.get("timeout_minutes", 60), 1, MAX_TIMEOUT_MINUTES)
        return cfg

    async def update_config(self, guild_id: int, mutate) -> None:
        async with session_factory() as session:
            guild = await session.get(Guild, guild_id)
            if guild is None:
                guild = Guild(id=guild_id)
                session.add(guild)
            full = dict(guild.config or {})
            cfg = dict(HONEYPOT_DEFAULTS)
            for k, v in dict(full.get("honeypot", {})).items():
                cfg[k] = dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v
            mutate(cfg)
            full["honeypot"] = cfg
            guild.config = full
            await session.commit()

    # ------------------------------------------------------------------ helpers

    async def log_channel(self, guild: discord.Guild, config: dict) -> discord.TextChannel | None:
        cid = config.get("log_channel_id")
        if cid:
            ch = guild.get_channel(int(cid))
            if isinstance(ch, discord.TextChannel):
                return ch
        shield = self.bot.get_cog("ShieldCog")
        if shield:
            sc = await shield.get_config(guild.id)
            sid = sc.get("alerts_channel_id")
            if sid:
                ch = guild.get_channel(int(sid))
                if isinstance(ch, discord.TextChannel):
                    return ch
        if isinstance(guild.system_channel, discord.TextChannel):
            return guild.system_channel
        for ch in guild.text_channels:
            try:
                if ch.permissions_for(guild.me).send_messages:
                    return ch
            except Exception:
                continue
        return None

    def _panel_embed(self, hits: int, action: str) -> discord.Embed:
        verb = ACTION_VERB.get(action, ACTION_VERB["softban"])
        embed = discord.Embed(
            title="DO NOT SEND MESSAGES IN THIS CHANNEL",
            description=(
                "This channel is a trap used to catch spam and captcha bots.\n"
                f"Anyone who sends a message here will be {verb}."
            ),
            color=BRAND,
        )
        embed.set_thumbnail(url="https://cdn.discordapp.com/attachments/1266061524968490546/1328946562909343774/474137395059028077.png")
        embed.set_footer(text=f"{EMOJI['honeypot']} Total kicks: {hits}")
        return embed

    def _counter_view(self, hits: int) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label=f"{EMOJI['honeypot']} Kicks: {hits}",
                style=discord.ButtonStyle.secondary,
                custom_id="honeypot:stats",
            )
        )
        return view

    async def _publish_panel(self, channel: discord.TextChannel, hits: int, action: str) -> int:
        msg = await channel.send(embed=self._panel_embed(hits, action), view=self._counter_view(hits))
        try:
            await msg.pin()
        except discord.HTTPException:
            pass
        return msg.id

    async def _update_panel(self, channel: discord.TextChannel | None, hits: int, action: str) -> None:
        if not isinstance(channel, discord.TextChannel):
            return
        now = asyncio.get_event_loop().time()
        last = self._debounce.get(channel.id, 0)
        if now - last < DEBOUNCE_SECONDS:
            return
        self._debounce[channel.id] = now
        msg_id = (await self.get_config(channel.guild.id)).get("panel_message_id")
        if not msg_id:
            return
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=self._panel_embed(hits, action), view=self._counter_view(hits))
        except discord.HTTPException:
            # panel was deleted — repost it
            try:
                new_id = await self._publish_panel(channel, hits, action)
                await self.update_config(channel.guild.id, lambda c: c.__setitem__("panel_message_id", new_id))
            except discord.HTTPException:
                pass

    async def _punish(self, guild: discord.Guild, member: discord.Member, config: dict) -> str:
        action = config.get("action", "softban")
        me = guild.me
        if me is None:
            return "flagged (bot missing)"
        delete_seconds = int(config.get("softban_delete_days", 1)) * 86400
        try:
            if action == "ban":
                if me.guild_permissions.ban_members:
                    await guild.ban(member, reason="Honeypot trap", delete_message_seconds=delete_seconds)
                    return "banned"
            elif action == "kick":
                if me.guild_permissions.kick_members:
                    await member.kick(reason="Honeypot trap")
                    return "kicked"
            elif action == "timeout":
                if me.guild_permissions.moderate_members:
                    minutes = int(config.get("timeout_minutes", 60))
                    until = discord.utils.utcnow() + timedelta(minutes=minutes)
                    await member.timeout(until, reason="Honeypot trap")
                    return f"timed out ({minutes}m)"
            else:  # softban
                if me.guild_permissions.ban_members:
                    await guild.ban(member, reason="Honeypot trap", delete_message_seconds=delete_seconds)
                    await asyncio.sleep(3)
                    await guild.unban(
                        discord.Object(id=member.id),
                        reason="Honeypot softban — rejoin allowed",
                    )
                    return "softbanned"
        except discord.HTTPException:
            pass
        # chosen action failed / no perms — fall back to a timeout if possible
        if me.guild_permissions.moderate_members:
            try:
                until = discord.utils.utcnow() + timedelta(hours=1)
                await member.timeout(until, reason="Honeypot trap (fallback)")
                return "timed out (fallback, 1h)"
            except discord.HTTPException:
                pass
        return "flagged (no permissions)"

    async def handle_hit(self, guild: discord.Guild, member: discord.Member, source: str) -> None:
        config = await self.get_config(guild.id)
        if not config.get("enabled") or not config.get("channel_id"):
            return
        channel_id = int(config["channel_id"])

        async with self._lock:
            def mutate(cfg: dict) -> None:
                cfg["hits"] = int(cfg.get("hits", 0)) + 1
                byc = dict(cfg.get("hits_by_channel") or {})
                key = str(channel_id)
                byc[key] = int(byc.get(key, 0)) + 1
                cfg["hits_by_channel"] = byc

            await self.update_config(guild.id, mutate)
            cfg_now = await self.get_config(guild.id)
            hits = cfg_now["hits"]
            await self.log_case(guild.id, member.id, f"honeypot via {source}")
            result = await self._punish(guild, member, cfg_now)

            # update panel (debounced)
            await self._update_panel(guild.get_channel(channel_id), hits, cfg_now["action"])

            # log embed to log channel
            log_ch = await self.log_channel(guild, cfg_now)
            if log_ch is None:
                return
            embed = discord.Embed(
                title=f"{EMOJI['honeypot']} Honeypot — {result}",
                description=f"{member.mention} (`{member.id}`) hit the trap via **{source}**.",
                color=BRAND,
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="Channel", value=f"<#{channel_id}>")
            embed.add_field(name="Result", value=result)
            embed.add_field(name="Total kicks", value=str(hits))
            style(embed)
            try:
                await log_ch.send(embed=embed)
            except discord.HTTPException:
                pass

    async def log_case(self, guild_id: int, user_id: int, reason: str) -> None:
        bot_id = self.bot.user.id if self.bot.user else 0
        async with session_factory() as session:
            session.add(ModCase(guild_id=guild_id, user_id=user_id, moderator_id=bot_id, action="honeypot", reason=reason[:1000]))
            await session.commit()

    # ------------------------------------------------------------------ stats

    async def _stats_embed(self, guild: discord.Guild, config: dict) -> discord.Embed:
        byc_raw = config.get("hits_by_channel") or {}
        breakdown: dict[int, int] = {}
        for k, v in byc_raw.items():
            try:
                breakdown[int(k)] = int(v)
            except (TypeError, ValueError):
                continue
        ranked = sorted(breakdown.items(), key=lambda kv: kv[1], reverse=True)
        if ranked:
            lines = [f"<#{cid}> — {n}" for cid, n in ranked[:MAX_BREAKDOWN_LINES]]
            extra = len(ranked) - MAX_BREAKDOWN_LINES
            if extra > 0:
                lines.append(f"*…and {extra} more*")
            per_channel = "\n".join(lines)
        else:
            per_channel = "*none yet*"

        week_ago = utcnow() - timedelta(days=7)
        async with session_factory() as session:
            week_count = await session.scalar(
                select(func.count()).select_from(ModCase).where(
                    ModCase.guild_id == guild.id,
                    ModCase.action == "honeypot",
                    ModCase.created_at >= week_ago,
                )
            )
            global_count = await session.scalar(
                select(func.count()).select_from(ModCase).where(ModCase.action == "honeypot")
            )
        enabled_guilds = 0
        for g in self.bot.guilds:
            gcfg = await self.get_config(g.id)
            if gcfg.get("enabled"):
                enabled_guilds += 1

        embed = discord.Embed(title=f"{EMOJI['honeypot']} Honeypot Statistics", color=BRAND)
        embed.add_field(name="Server total", value=str(config.get("hits", 0)))
        embed.add_field(name="Last 7 days", value=str(week_count or 0))
        embed.add_field(name="Action", value=config.get("action", "softban"))
        embed.add_field(name="Per channel", value=per_channel, inline=False)
        embed.add_field(name="Servers protected", value=str(enabled_guilds))
        embed.add_field(name="Traps sprung (global)", value=str(global_count or 0))
        style(embed)
        return embed

    # ------------------------------------------------------------------ setup

    @honeypot.command(name="setup", description="Create or claim the trap channel and post the panel")
    @app_commands.describe(channel="Existing channel to use (leave empty to create one)")
    @app_commands.describe(category="Category for a new channel")
    async def setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        category: discord.CategoryChannel | None = None,
    ) -> None:
        assert interaction.guild is not None
        me = interaction.guild.me
        if me is None or not me.guild_permissions.manage_channels or not me.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "I need **Manage Channels** and **Manage Roles**.", ephemeral=True
            )
            return

        healed = ""
        if channel is None:
            overwrites = {
                interaction.guild.default_role: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True
                ),
                me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                    manage_messages=True, manage_channels=True,
                ),
            }
            try:
                channel = await interaction.guild.create_text_channel(
                    name="honeypot",
                    overwrites=overwrites,
                    category=category,
                    topic="🍯 Honeypot — do not send messages here.",
                    reason=f"Honeypot created by {interaction.user}",
                )
            except discord.HTTPException as exc:
                await interaction.response.send_message(f"Couldn't create channel: `{exc}`", ephemeral=True)
                return
        else:
            # self-heal: force the channel visible and postable for @everyone
            try:
                ow = channel.overwrites_for(interaction.guild.default_role)
                if ow.view_channel is not True or ow.send_messages is not True:
                    ow.view_channel = True
                    ow.send_messages = True
                    await channel.set_permissions(
                        interaction.guild.default_role, overwrite=ow,
                        reason=f"Honeypot setup by {interaction.user}",
                    )
                    healed = " (permissions fixed to visible + postable)"
            except discord.HTTPException as exc:
                await interaction.response.send_message(
                    f"Couldn't fix permissions on {channel.mention}: `{exc}` — "
                    "make sure it's visible and postable for @everyone.", ephemeral=True
                )
                return

        # sit at the top of the channel list
        try:
            await channel.move(position=0, reason="Honeypot panel to top")
        except (discord.HTTPException, discord.InvalidArgument):
            pass

        config = await self.get_config(interaction.guild.id)
        await self.update_config(interaction.guild.id, lambda cfg: (
            cfg.__setitem__("channel_id", channel.id),
            cfg.__setitem__("enabled", True),
        ))
        msg_id = await self._publish_panel(channel, config.get("hits", 0), config.get("action", "softban"))
        await self.update_config(interaction.guild.id, lambda cfg: cfg.__setitem__("panel_message_id", msg_id))
        await interaction.response.send_message(
            f"🍯 Honeypot live: {channel.mention} — visible to everyone, panel posted (pinned) at the top{healed}. "
            f"Anyone posting gets **{config.get('action', 'softban')}**.",
            ephemeral=True,
        )

    @honeypot.command(name="config", description="Configure the honeypot punishment and logging")
    @app_commands.describe(
        action="What happens to someone who posts in the trap channel",
        softban_delete_days="Days of their messages to delete on ban/softban (0-7)",
        timeout_minutes="Timeout length in minutes when action is timeout (max 40320)",
        log_channel="Channel for hit logs (leave empty to keep current)",
    )
    async def config_cmd(
        self,
        interaction: discord.Interaction,
        action: Literal["softban", "ban", "kick", "timeout"] | None = None,
        softban_delete_days: int | None = None,
        timeout_minutes: int | None = None,
        log_channel: discord.TextChannel | None = None,
    ) -> None:
        assert interaction.guild is not None

        def mutate(cfg: dict) -> None:
            if action is not None:
                cfg["action"] = action
            if softban_delete_days is not None:
                cfg["softban_delete_days"] = _clamp(softban_delete_days, 0, 7)
            if timeout_minutes is not None:
                cfg["timeout_minutes"] = _clamp(timeout_minutes, 1, MAX_TIMEOUT_MINUTES)
            if log_channel is not None:
                cfg["log_channel_id"] = log_channel.id

        await self.update_config(interaction.guild.id, mutate)
        config = await self.get_config(interaction.guild.id)

        # refresh the panel so the warning reflects the new action
        ch = interaction.guild.get_channel(config["channel_id"]) if config.get("channel_id") else None
        if isinstance(ch, discord.TextChannel):
            try:
                msg = await ch.fetch_message(config.get("panel_message_id"))
                await msg.edit(embed=self._panel_embed(config["hits"], config["action"]), view=self._counter_view(config["hits"]))
            except discord.HTTPException:
                pass

        log_ch = f"<#{config['log_channel_id']}>" if config.get("log_channel_id") else "*Shield alerts / system channel*"
        embed = discord.Embed(title=f"{EMOJI['honeypot']} Honeypot configured", color=BRAND)
        embed.add_field(name="Action", value=config["action"])
        embed.add_field(name="Delete days", value=str(config["softban_delete_days"]))
        embed.add_field(name="Timeout minutes", value=str(config["timeout_minutes"]))
        embed.add_field(name="Log channel", value=log_ch)
        style(embed, interaction, emoji=EMOJI["honeypot"])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @honeypot.command(name="disable", description="Disable the honeypot (panel stays)")
    async def disable(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await self.update_config(interaction.guild.id, lambda cfg: cfg.__setitem__("enabled", False))
        await interaction.response.send_message("🍯 Honeypot **disabled**.", ephemeral=True)

    @honeypot.command(name="reset", description="Reset hit counter to 0")
    async def reset(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await self.update_config(interaction.guild.id, lambda cfg: (
            cfg.__setitem__("hits", 0),
            cfg.__setitem__("hits_by_channel", {}),
        ))
        config = await self.get_config(interaction.guild.id)
        channel = interaction.guild.get_channel(config["channel_id"])
        if isinstance(channel, discord.TextChannel):
            try:
                msg = await channel.fetch_message(config.get("panel_message_id"))
                await msg.edit(embed=self._panel_embed(0, config["action"]), view=self._counter_view(0))
            except discord.HTTPException:
                pass
        await interaction.response.send_message("🍯 Honeypot counter reset to **0**.", ephemeral=True)

    @honeypot.command(name="status", description="Show honeypot state")
    async def status(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        config = await self.get_config(interaction.guild.id)
        ch = f"<#{config['channel_id']}>" if config["channel_id"] else "*not set*"
        log_ch = f"<#{config['log_channel_id']}>" if config.get("log_channel_id") else "*Shield alerts / system channel*"
        panel = f"<#{config['channel_id']}>/message {config.get('panel_message_id')}" if config.get("panel_message_id") else "*pending*"
        embed = discord.Embed(title=f"{EMOJI['honeypot']} Honeypot status", color=BRAND)
        embed.add_field(name="Channel", value=ch)
        embed.add_field(name="Enabled", value="yes" if config["enabled"] else "no")
        embed.add_field(name="Total hits", value=str(config["hits"]))
        embed.add_field(name="Action", value=config["action"])
        embed.add_field(name="Delete days", value=str(config["softban_delete_days"]))
        embed.add_field(name="Timeout minutes", value=str(config["timeout_minutes"]))
        embed.add_field(name="Panel", value=panel)
        embed.add_field(name="Log channel", value=log_ch)
        style(embed, interaction, emoji=EMOJI["honeypot"])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ listeners

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        custom_id = (interaction.data or {}).get("custom_id", "")
        if custom_id != "honeypot:stats":
            return
        guild = interaction.guild
        if guild is None:
            return
        config = await self.get_config(guild.id)
        embed = await self._stats_embed(guild, config)
        try:
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if message.channel.id is None:
            return
        cfg = await self.get_config(message.guild.id)
        if not cfg.get("enabled") or message.channel.id != cfg.get("channel_id"):
            return
        if not isinstance(message.author, discord.Member):
            return
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        await self.handle_hit(message.guild, message.author, "message in honeypot")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.GuildChannel) -> None:
        guild = getattr(channel, "guild", None)
        if guild is None:
            return
        cfg = await self.get_config(guild.id)
        if cfg.get("channel_id") != channel.id:
            return
        await self.update_config(guild.id, lambda c: (
            c.__setitem__("channel_id", None),
            c.__setitem__("panel_message_id", None),
            c.__setitem__("enabled", False),
        ))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HoneypotCog(bot))
