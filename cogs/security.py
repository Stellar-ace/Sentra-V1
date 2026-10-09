import asyncio
import time
from collections import defaultdict, deque
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from sentra.cache import TTLCache
from sentra.db.engine import session_factory
from sentra.db.models import Guild, ModCase
from sentra.ui import DANGER, EMOJI, SEC, style, TICK, WARN

SHIELD_DEFAULTS = {
    "alerts_channel_id": None,
    "min_account_age_days": 0,
    "gate_action": "quarantine",
    "raidmode": False,
    "raid_joins": 10,
    "raid_window_s": 30,
    "raid_pulse_minutes": 5,
    "quarantine_role_id": None,
    "saved_roles": {},
    "lockdown_snapshot": {},
}

AUDIT_WINDOW_S = 60

NUKE_LIMITS = {
    discord.AuditLogAction.channel_delete: ("channel deletions", 4),
    discord.AuditLogAction.channel_create: ("channel creations", 8),
    discord.AuditLogAction.role_delete: ("role deletions", 3),
    discord.AuditLogAction.ban: ("bans", 8),
    discord.AuditLogAction.kick: ("kicks", 8),
    discord.AuditLogAction.webhook_create: ("webhook creations", 5),
    discord.AuditLogAction.bot_add: ("bots added", 1),
}


def shield_config(stored: dict | None) -> dict:
    return {**SHIELD_DEFAULTS, **(stored or {})}


class ShieldCog(commands.Cog):
    security = app_commands.Group(
        name="security",
        description="Server protection controls",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config_cache = TTLCache(ttl=10)
        self.seen_audit = TTLCache(ttl=300, maxsize=50_000)
        self.punished = TTLCache(ttl=900, maxsize=10_000)
        self.join_times: dict[int, deque] = defaultdict(lambda: deque(maxlen=200))
        self.raid_pulse_until: dict[int, float] = {}

    async def get_config(self, guild_id: int) -> dict:
        cached = self.config_cache.get(str(guild_id))
        if cached is not None:
            return cached
        async with session_factory() as session:
            guild = await session.get(Guild, guild_id)
            if guild is None:
                guild = Guild(id=guild_id, config={})
                session.add(guild)
                await session.commit()
            stored = dict(guild.config or {}).get("shield", {})
        config = shield_config(stored)
        self.config_cache.set(str(guild_id), config)
        return config

    async def update_config(self, guild_id: int, mutate) -> dict:
        async with session_factory() as session:
            guild = await session.get(Guild, guild_id)
            if guild is None:
                guild = Guild(id=guild_id)
                session.add(guild)
            full = dict(guild.config or {})
            shield = shield_config(full.get("shield"))
            mutate(shield)
            full["shield"] = shield
            guild.config = full
            await session.commit()
            result = dict(shield)
        self.config_cache.pop(str(guild_id))
        return result

    async def log_case(self, guild_id: int, user_id: int, action: str, reason: str | None) -> None:
        bot_id = self.bot.user.id if self.bot.user else 0
        async with session_factory() as session:
            session.add(
                ModCase(
                    guild_id=guild_id,
                    user_id=user_id,
                    moderator_id=bot_id,
                    action=action,
                    reason=reason[:1000] if reason else None,
                )
            )
            await session.commit()

    async def alert_channel(self, guild: discord.Guild):
        config = await self.get_config(guild.id)
        channel_id = config.get("alerts_channel_id")
        if channel_id:
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                return channel
        channel = guild.system_channel
        if channel and channel.permissions_for(guild.me).send_messages:
            return channel
        for candidate in guild.text_channels:
            if candidate.permissions_for(guild.me).send_messages:
                return candidate
        return None

    async def send_alert(self, guild: discord.Guild, embed: discord.Embed, view: discord.ui.View | None = None):
        channel = await self.alert_channel(guild)
        if channel is None:
            try:
                owner = guild.owner
                if owner:
                    await owner.send(embed=embed)
            except discord.HTTPException:
                pass
            return
        try:
            await channel.send(embed=embed, view=view)
        except discord.HTTPException:
            pass

    async def ensure_quarantine_role(self, guild: discord.Guild) -> discord.Role | None:
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            return None
        config = await self.get_config(guild.id)
        role_id = config.get("quarantine_role_id")
        role = guild.get_role(role_id) if role_id else None
        if role is None:
            role = discord.utils.get(guild.roles, name="Quarantined")
        if role is None or role >= me.top_role:
            try:
                role = await guild.create_role(
                    name="Quarantined",
                    colour=discord.Colour.dark_grey(),
                    hoist=False,
                    mentionable=False,
                    reason="Sentra Shield quarantine role",
                )
            except discord.HTTPException:
                return None
        if role_id != role.id:
            await self.update_config(guild.id, lambda s: s.__setitem__("quarantine_role_id", role.id))
        for channel in guild.channels:
            current = dict(channel.overwrites)
            merged = current.get(role, discord.PermissionOverwrite())
            merged.send_messages = False
            merged.send_messages_in_threads = False
            merged.create_public_threads = False
            merged.create_private_threads = False
            merged.send_voice_messages = False
            merged.add_reactions = False
            merged.connect = False
            merged.speak = False
            # Note: discord.py has no `create_forum_posts` permission flag.
            # In forum channels, creating a post is gated by `send_messages`,
            # which is already denied above.
            current[role] = merged
            try:
                await channel.edit(overwrites=current, reason="Sentra Shield quarantine setup")
            except discord.HTTPException:
                continue
            await asyncio.sleep(0.4)
        return role

    async def quarantine_member(
        self, guild: discord.Guild, member: discord.Member, reason: str, actor_label: str
    ) -> bool:
        role = await self.ensure_quarantine_role(guild)
        if role is None:
            return False
        removable = [
            r
            for r in member.roles
            if r != guild.default_role and r != role and not r.managed and r < guild.me.top_role
        ]
        try:
            await member.remove_roles(*removable, reason=f"Shield: {reason}")
            await member.add_roles(role, reason=f"Shield: {reason}")
        except discord.HTTPException:
            return False

        def save_roles(shield: dict) -> None:
            saved = dict(shield.get("saved_roles", {}))
            saved[str(member.id)] = [r.id for r in removable]
            shield["saved_roles"] = saved

        await self.update_config(guild.id, save_roles)
        await self.log_case(guild.id, member.id, "quarantine", f"{actor_label}: {reason}")

        embed = discord.Embed(
            title="🛡️ Member quarantined",
            description=f"<@{member.id}> (`{member.id}`)\n**Reason:** {reason}",
            color=SEC,
        )
        embed.set_footer(text=f"Actor: {actor_label}")
        style(embed)
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(label="Release", style=discord.ButtonStyle.success, custom_id=f"shield:release:{member.id}")
        )
        await self.send_alert(guild, embed, view)
        return True

    async def release_member(self, guild: discord.Guild, user_id: int, actor_id: int) -> tuple[bool, str]:
        config = await self.get_config(guild.id)
        saved_ids = (config.get("saved_roles") or {}).get(str(user_id), [])
        member = guild.get_member(user_id)

        def clear_saved(shield: dict) -> None:
            saved = dict(shield.get("saved_roles", {}))
            saved.pop(str(user_id), None)
            shield["saved_roles"] = saved

        restored = 0
        if member:
            quarantine_role = guild.get_role(config.get("quarantine_role_id") or 0)
            roles_to_add = [guild.get_role(rid) for rid in saved_ids]
            valid = [r for r in roles_to_add if r and r < guild.me.top_role and not r.managed]
            try:
                if quarantine_role and quarantine_role in member.roles:
                    await member.remove_roles(quarantine_role, reason=f"Released by <@{actor_id}>")
                if valid:
                    await member.add_roles(*valid, reason=f"Released by <@{actor_id}>")
                restored = len(valid)
            except discord.HTTPException as exc:
                return False, f"Release failed: {exc}"
        await self.update_config(guild.id, clear_saved)
        await self.log_case(guild.id, user_id, "release", f"by {actor_id}, {restored} role(s) restored")
        detail = f"{restored} role(s) restored." if member else "Member has left the server; record cleared."
        return True, detail

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component or interaction.guild is None:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith("shield:release:"):
            return
        user = interaction.user
        if not isinstance(user, discord.Member) or not user.guild_permissions.manage_guild:
            await interaction.response.send_message("Only staff with Manage Server can release.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        target_id = int(custom_id.rsplit(":", 1)[-1])
        ok, detail = await self.release_member(interaction.guild, target_id, user.id)
        await interaction.followup.send(f"{'<:sentra_check:1546486790779572385>' if ok else '<:sentra_warn:1546486928096890951>'} {detail}", ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            return
        now = time.monotonic()
        self.join_times[guild.id].append(now)
        config = await self.get_config(guild.id)
        window = config["raid_window_s"]
        recent = sum(1 for t in self.join_times[guild.id] if now - t <= window)
        burst = recent >= config["raid_joins"]
        pulse_active = self.raid_pulse_until.get(guild.id, 0) > now
        if burst and not pulse_active and config["raid_pulse_minutes"] > 0:
            self.raid_pulse_until[guild.id] = now + config["raid_pulse_minutes"] * 60
            pulse_active = True
            embed = discord.Embed(
                title="🛡️ Raid suspected",
                description=(
                    f"{recent} joins in {window}s — new members will be quarantined "
                    f"for the next {config['raid_pulse_minutes']} minute(s)."
                ),
                color=SEC,
            )
            style(embed)
            await self.send_alert(guild, embed)

        reasons = []
        min_days = config["min_account_age_days"]
        if min_days > 0:
            age_days = (discord.utils.utcnow() - member.created_at).days
            if age_days < min_days:
                reasons.append(f"account younger than {min_days} day(s)")
        if config["raidmode"] or pulse_active:
            reasons.append("raid protection active")
        if not reasons:
            return

        if config["gate_action"] == "kick":
            try:
                await member.send(
                    f"You were removed from **{guild.name}**: {'; '.join(reasons)}. "
                    "You may rejoin once your account meets the server's requirements."
                )
            except discord.HTTPException:
                pass
            try:
                await member.kick(reason=f"Shield gate: {'; '.join(reasons)}")
            except discord.HTTPException:
                return
            await self.log_case(guild.id, member.id, "gate_kick", "; ".join(reasons))
            return

        quarantined = await self.quarantine_member(guild, member, "; ".join(reasons), "Shield gate")
        if not quarantined:
            embed = discord.Embed(
                title="<:sentra_warn:1546486928096890951> Quarantine failed",
                description=f"Could not quarantine <@{member.id}> — check my role position and Manage Roles permission.",
                color=DANGER,
            )
            style(embed)
            await self.send_alert(guild, embed)

    @tasks.loop(seconds=12)
    async def sentinel(self) -> None:
        await self.bot.wait_until_ready()
        for guild in list(self.bot.guilds):
            me = guild.me
            if me is None or not me.guild_permissions.view_audit_log:
                continue
            try:
                entries = [entry async for entry in guild.audit_logs(limit=30)]
            except discord.HTTPException:
                continue
            now = discord.utils.utcnow()
            counts: dict[tuple[int, str], int] = defaultdict(int)
            for entry in entries:
                mapped = NUKE_LIMITS.get(entry.action)
                if mapped is None:
                    continue
                if entry.created_at < now - timedelta(seconds=AUDIT_WINDOW_S):
                    break
                dedupe_key = f"{guild.id}:{entry.id}"
                if self.seen_audit.get(dedupe_key):
                    continue
                self.seen_audit.set(dedupe_key, True)
                actor = entry.user
                if (
                    actor is None
                    or actor.id == guild.owner_id
                    or (self.bot.user and actor.id == self.bot.user.id)
                ):
                    continue
                counts[(actor.id, mapped[0])] += 1
            for (actor_id, action_name), total in counts.items():
                limit = next(l for name, l in NUKE_LIMITS.values() if name == action_name)
                if total < limit:
                    continue
                punish_key = f"{guild.id}:{actor_id}:{action_name}"
                if self.punished.get(punish_key):
                    continue
                self.punished.set(punish_key, True)
                description = (
                    f"<@{actor_id}> performed **{total} {action_name}** within {AUDIT_WINDOW_S}s "
                    f"(limit {limit})."
                )
                member = guild.get_member(actor_id)
                quarantined = False
                if member and not member.guild_permissions.administrator:
                    quarantined = await self.quarantine_member(
                        guild, member, f"Anti-nuke: {total} {action_name} in {AUDIT_WINDOW_S}s", "Anti-Nuke Sentinel"
                    )
                embed = discord.Embed(
                    title="🚨 Anti-nuke trigger" if quarantined else "🚨 Anti-nuke warning",
                    description=description + (
                        "\nMember quarantined pending review." if quarantined else "\nCould not auto-quarantine — manual review required."
                    ),
                    color=SEC,
                )
                style(embed)
                view = None
                if quarantined and member:
                    view = discord.ui.View(timeout=None)
                    view.add_item(
                        discord.ui.Button(
                            label="Release", style=discord.ButtonStyle.success, custom_id=f"shield:release:{member.id}"
                        )
                    )
                await self.send_alert(guild, embed, view)
            await asyncio.sleep(1.0)

    @sentinel.before_loop
    async def before_sentinel(self) -> None:
        await self.bot.wait_until_ready()

    def cog_unload(self) -> None:
        self.sentinel.cancel()

    @security.command(name="setup", description="Create the quarantine role and apply its channel blocks")
    async def setup_cmd(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        role = await self.ensure_quarantine_role(interaction.guild)
        if role is None:
            await interaction.followup.send("I need **Manage Roles** to set up quarantine.")
            return
        await interaction.followup.send(f"Shield ready. Quarantine role: {role.mention}.")

    @security.command(name="alerts", description="Set the channel for security alerts")
    @app_commands.describe(channel="Where alerts should be posted")
    async def alerts(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        assert interaction.guild is not None
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.embed_links):
            await interaction.response.send_message("I can't post embeds there.", ephemeral=True)
            return
        await self.update_config(interaction.guild.id, lambda s: s.__setitem__("alerts_channel_id", channel.id))
        await interaction.response.send_message(f"Security alerts will go to {channel.mention}.", ephemeral=True)

    @security.command(name="gate", description="Require a minimum account age to join")
    @app_commands.describe(enabled="Turn the gate on or off", days="Minimum account age in days (1-30)", action="What happens to young accounts")
    @app_commands.choices(action=[app_commands.Choice(name="Quarantine", value="quarantine"), app_commands.Choice(name="Kick", value="kick")])
    async def gate(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        days: app_commands.Range[int, 1, 30] = 7,
        action: app_commands.Choice[str] | None = None,
    ) -> None:
        assert interaction.guild is not None

        def mutate(shield: dict) -> None:
            shield["min_account_age_days"] = days if enabled else 0
            if action is not None:
                shield["gate_action"] = action.value

        config = await self.update_config(interaction.guild.id, mutate)
        state = f"on — minimum {days}d ({config['gate_action']})" if enabled else "off"
        await interaction.response.send_message(f"Account gate: **{state}**.", ephemeral=True)

    @security.command(name="raidmode", description="Manually toggle raid mode (quarantines all new joins)")
    @app_commands.describe(enabled="Force on/off, or omit to flip automatically by join velocity only")
    async def raidmode(self, interaction: discord.Interaction, enabled: bool | None = None) -> None:
        assert interaction.guild is not None

        def mutate(shield: dict) -> None:
            if enabled is not None:
                shield["raidmode"] = enabled

        config = await self.update_config(interaction.guild.id, mutate)
        state = "ON — all new joins quarantined" if config["raidmode"] else "off (auto-burst detection still active)"
        await interaction.response.send_message(f"Raid mode: **{state}**.", ephemeral=True)

    @security.command(name="quarantine", description="Manually quarantine a member")
    @app_commands.describe(member="Who to isolate", reason="Why")
    async def quarantine_cmd(
        self, interaction: discord.Interaction, member: discord.Member, reason: str = "Manual review"
    ) -> None:
        assert interaction.guild is not None
        if member.top_role >= interaction.guild.me.top_role or member.id == interaction.guild.owner_id:
            await interaction.response.send_message("I cannot quarantine that member.", ephemeral=True)
            return
        await interaction.response.defer()
        ok = await self.quarantine_member(interaction.guild, member, reason, str(interaction.user))
        if not ok:
            await interaction.followup.send("Quarantine failed — check my role position and permissions.")
        else:
            await interaction.followup.send(f"Quarantined {member.mention}. Use the button on the alert to release.")

    @security.command(name="release", description="Release a member from quarantine")
    @app_commands.describe(user="Who to release")
    async def release_cmd(self, interaction: discord.Interaction, user: discord.User) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        ok, detail = await self.release_member(interaction.guild, user.id, interaction.user.id)
        await interaction.followup.send(f"{'<:sentra_check:1546486790779572385>' if ok else '<:sentra_warn:1546486928096890951>'} {detail}")

    @security.command(name="lockdown", description="Lock all public text channels (reversible)")
    @app_commands.describe(confirm="Must be true to execute")
    async def lockdown(self, interaction: discord.Interaction, confirm: bool) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        if not confirm:
            await interaction.response.send_message("Pass `confirm: True` — this locks every public channel.", ephemeral=True)
            return
        if not guild.me.guild_permissions.manage_channels:
            await interaction.response.send_message("I need **Manage Channels**.", ephemeral=True)
            return
        await interaction.response.defer()
        snapshot: dict[str, bool | None] = {}
        locked = 0
        for channel in guild.text_channels:
            overwrites = channel.overwrites_for(guild.default_role)
            if overwrites.send_messages is False:
                continue
            snapshot[str(channel.id)] = overwrites.send_messages
            overwrites.send_messages = False
            overwrites.send_messages_in_threads = False
            try:
                current = dict(channel.overwrites)
                current[guild.default_role] = overwrites
                await channel.edit(overwrites=current, reason=f"Lockdown by {interaction.user}")
                locked += 1
            except discord.HTTPException:
                snapshot.pop(str(channel.id), None)
            await asyncio.sleep(0.4)

        def save_snapshot(shield: dict) -> None:
            shield["lockdown_snapshot"] = snapshot

        await self.update_config(guild.id, save_snapshot)
        await interaction.followup.send(f"🔒 Locked **{locked}** channel(s). Use `/security unlock` to restore exact prior access.")

    @security.command(name="unlock", description="Restore channels to their pre-lockdown permissions")
    async def unlock(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        guild = interaction.guild
        config = await self.get_config(guild.id)
        snapshot = config.get("lockdown_snapshot") or {}
        if not snapshot:
            await interaction.response.send_message("No lockdown snapshot found.", ephemeral=True)
            return
        await interaction.response.defer()
        restored = 0
        for channel_id, previous in snapshot.items():
            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, discord.TextChannel):
                continue
            restore = True if previous else None
            overwrites = channel.overwrites_for(guild.default_role)
            overwrites.send_messages = restore
            overwrites.send_messages_in_threads = restore
            current = dict(channel.overwrites)
            if overwrites.is_empty():
                current.pop(guild.default_role, None)
            else:
                current[guild.default_role] = overwrites
            try:
                await channel.edit(overwrites=current, reason=f"Unlock by {interaction.user}")
                restored += 1
            except discord.HTTPException:
                continue
            await asyncio.sleep(0.4)

        def clear_snapshot(shield: dict) -> None:
            shield["lockdown_snapshot"] = {}

        await self.update_config(guild.id, clear_snapshot)
        await interaction.followup.send(f"🔓 Unlocked **{restored}** channel(s).")

    @security.command(name="status", description="Show current shield configuration")
    async def status(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        config = await self.get_config(interaction.guild.id)
        alerts_channel = (
            f"<#{config['alerts_channel_id']}>" if config["alerts_channel_id"] else "*not set*"
        )
        quarantine_count = len(config.get("saved_roles") or {})
        embed = discord.Embed(title="🛡️ Shield status", color=SEC)
        embed.add_field(name="Alerts channel", value=alerts_channel)
        embed.add_field(name="Account gate", value=f"{config['min_account_age_days']}d → {config['gate_action']}" if config["min_account_age_days"] else "off")
        embed.add_field(name="Raid mode", value="ON" if config["raidmode"] else "auto only")
        embed.add_field(name="Burst trigger", value=f"{config['raid_joins']} joins / {config['raid_window_s']}s")
        embed.add_field(name="Currently quarantined", value=str(quarantine_count))
        embed.add_field(name="Lockdown", value="ACTIVE" if config.get("lockdown_snapshot") else "clear")
        embed.set_footer(text=f"Audit sentinel running: {self.sentinel.is_running()}")
        style(embed, interaction, emoji=EMOJI["security"])
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    cog = ShieldCog(bot)
    await bot.add_cog(cog)
    cog.sentinel.start()
