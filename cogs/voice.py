import asyncio
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import delete, select

from sentra.db.engine import session_factory
from sentra.db.models import Guild, TempVoice
from sentra.ui import CROSS, TICK, VOICE, WARN, style

VOICE_DEFAULTS = {
    "hubs": [],
    "text_in_voice": True,
    "voice_roles": {},
    "j2c_enabled": False,
    "hub_channel_id": None,
    "interface_channel_id": None,
    "category_id": None,
    "panel_message_id": None,
}

ACTIVITIES = {
    "watch-together": 880218394199220622,
    "poker-night": 755827207812677713,
    "betrayal-io": 773336526917861400,
    "fishing-frenzy": 814288819477020702,
    "chess-in-the-park": 832012774040141892,
    "sketch-heads": 878067389634314250,
    "letter-league": 879863686565621819,
    "word-snacks": 879863976006127613,
}

NAME_SANITIZE_RE = re.compile(r"[^\w\s'!?.-]", re.UNICODE)

LIMIT_CHOICES = [("1", 1), ("2", 2), ("3", 3), ("4", 4), ("5", 5), ("6", 6), ("7", 7), ("8", 8), ("9", 9), ("10", 10), ("15", 15), ("20", 20), ("25", 25), ("No limit", 0)]

PANEL_PREFIX = "vc:panel:"
MODAL_RENAME_ID = "vc:panel:rename-modal"


def sanitize_name(text: str) -> str:
    cleaned = NAME_SANITIZE_RE.sub("", text).strip()
    return (cleaned or "Unnamed")[:90]


class VoiceCog(commands.Cog):
    voice = app_commands.Group(name="voice", description="Voice channel management", guild_only=True)
    vrole = app_commands.Group(
        name="vrole",
        parent=voice,
        description="Roles granted while members sit in a voice channel",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._warned_no_perm: set[int] = set()
        self._sweeper.start()

    def cog_unload(self) -> None:
        self._sweeper.cancel()

    async def get_config(self, guild_id: int) -> dict:
        async with session_factory() as session:
            guild = await session.get(Guild, guild_id)
            stored = dict((guild.config or {}).get("voice", {})) if guild else {}
        return {**VOICE_DEFAULTS, **stored}

    async def update_config(self, guild_id: int, mutate) -> None:
        async with session_factory() as session:
            guild = await session.get(Guild, guild_id)
            if guild is None:
                guild = Guild(id=guild_id)
                session.add(guild)
            full = dict(guild.config or {})
            voice_cfg = {**VOICE_DEFAULTS, **dict(full.get("voice", {}))}
            mutate(voice_cfg)
            full["voice"] = voice_cfg
            guild.config = full
            await session.commit()

    async def get_row(self, guild_id: int, channel_id: int) -> TempVoice | None:
        async with session_factory() as session:
            return (
                await session.execute(
                    select(TempVoice).where(
                        TempVoice.guild_id == guild_id, TempVoice.channel_id == channel_id
                    )
                )
            ).scalar_one_or_none()

    # ------------------------------------------------------------ j2c provisioning

    async def _provision(self, guild: discord.Guild) -> dict | None:
        """Create (or heal) the J2C category, hub VC, interface channel + panel.

        Returns the final config dict, or None when the bot lacks permissions.
        """
        me = guild.me
        if me is None or not me.guild_permissions.manage_channels:
            return None
        config = await self.get_config(guild.id)

        category = guild.get_channel(config.get("category_id") or 0)
        if not isinstance(category, discord.CategoryChannel):
            try:
                category = await guild.create_category(
                    "Join to Create", reason="Join-to-create auto setup"
                )
            except discord.HTTPException:
                return None
            await self.update_config(guild.id, lambda c: c.__setitem__("category_id", category.id))

        hub = guild.get_channel(config.get("hub_channel_id") or 0)
        if not isinstance(hub, discord.VoiceChannel):
            try:
                hub = await guild.create_voice_channel(
                    "🔊 Join to Create",
                    category=category,
                    reason="Join-to-create hub",
                )
            except discord.HTTPException:
                return None
            await self.update_config(guild.id, lambda c: c.__setitem__("hub_channel_id", hub.id))

        interface = guild.get_channel(config.get("interface_channel_id") or 0)
        if not isinstance(interface, discord.TextChannel):
            try:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(
                        view_channel=True, send_messages=False, read_message_history=True
                    ),
                    me: discord.PermissionOverwrite(
                        view_channel=True, send_messages=True, manage_messages=True
                    ),
                }
                interface = await guild.create_text_channel(
                    "vc-controls",
                    category=category,
                    overwrites=overwrites,
                    topic="🎛️ Join-to-create control panel — manage your temp voice channel with the buttons below.",
                    reason="Join-to-create interface",
                )
            except discord.HTTPException:
                return None
            await self.update_config(
                guild.id, lambda c: c.__setitem__("interface_channel_id", interface.id)
            )

        await self.update_config(guild.id, lambda c: c.__setitem__("j2c_enabled", True))

        # (re)post the panel if it's missing
        panel_id = (await self.get_config(guild.id)).get("panel_message_id")
        if panel_id:
            try:
                await interface.fetch_message(panel_id)
                return await self.get_config(guild.id)
            except discord.HTTPException:
                pass
        msg = await self._publish_panel(interface)
        await self.update_config(guild.id, lambda c: c.__setitem__("panel_message_id", msg.id))
        return await self.get_config(guild.id)

    async def _publish_panel(self, channel: discord.TextChannel) -> discord.Message:
        return await channel.send(embed=self._panel_embed(), view=self._panel_view())

    def _panel_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🎛️ Voice Channel Controls",
            description=(
                "Join **Join to Create** to get your own voice channel, then manage it with the buttons below.\n\n"
                "🔒 Lock / 🔓 Unlock — control who can *join*\n"
                "🙈 Hide / 👁 Unhide — control who can *see*\n"
                "✏️ Rename — change the name\n"
                "👥 Limit — cap member count\n"
                "🚫 Ban — block someone from joining\n"
                "✅ Permit — let someone into a locked/hidden channel\n"
                "🔗 Invite — grab an invite link\n"
                "🔁 Transfer — hand hosting to someone else\n"
                "🧵 Thread — start a discussion thread for your channel\n"
                "🗑 Delete — delete your channel\n"
                "👑 Claim — take over when the host left"
            ),
            color=VOICE,
        )
        embed.set_footer(text="Buttons only work for the channel host · Join to Create")
        style(embed)
        return embed

    def _panel_view(self) -> discord.ui.View:
        def btn(label: str, cid: str, style: discord.ButtonStyle = discord.ButtonStyle.secondary, emoji: str | None = None, row: int | None = None) -> discord.ui.Button:
            return discord.ui.Button(label=label, style=style, custom_id=PANEL_PREFIX + cid, emoji=emoji, row=row)

        view = discord.ui.View(timeout=None)
        # row 1 — access
        view.add_item(btn("Lock", "lock", emoji="🔒", row=0))
        view.add_item(btn("Unlock", "unlock", emoji="🔓", row=0))
        view.add_item(btn("Hide", "hide", emoji="🙈", row=0))
        view.add_item(btn("Unhide", "unhide", emoji="👁", row=0))
        view.add_item(btn("Rename", "rename", emoji="✏️", row=0))
        # row 2 — members
        view.add_item(btn("Limit", "limit", emoji="👥", row=1))
        view.add_item(btn("Ban", "ban", emoji="🚫", row=1))
        view.add_item(btn("Permit", "permit", emoji="✅", row=1))
        view.add_item(btn("Invite", "invite", emoji="🔗", row=1))
        view.add_item(btn("Thread", "thread", emoji="🧵", row=1))
        # row 3 — ownership
        view.add_item(btn("Transfer", "transfer", emoji="🔁", row=2))
        view.add_item(btn("Claim", "claim", emoji="👑", row=2))
        view.add_item(btn("Delete", "delete", style=discord.ButtonStyle.danger, emoji="🗑", row=2))
        return view

    # ------------------------------------------------------------- j2c listeners

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        try:
            await self._provision(guild)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        guild = channel.guild
        config = await self.get_config(guild.id)
        if channel.id not in {config.get("hub_channel_id"), config.get("interface_channel_id")}:
            if not isinstance(channel, discord.CategoryChannel):
                return
            if channel.id != config.get("category_id"):
                return
        if channel.id == config.get("category_id"):
            # category gone — hub/interface may die with it; leave them to sweeper/heal
            await self.update_config(guild.id, lambda c: c.__setitem__("category_id", None))
            return
        if channel.id == config.get("hub_channel_id"):
            await self.update_config(guild.id, lambda c: (
                c.__setitem__("hub_channel_id", None),
                c.__setitem__("j2c_enabled", False),
            ))
        elif channel.id == config.get("interface_channel_id"):
            await self.update_config(guild.id, lambda c: (
                c.__setitem__("interface_channel_id", None),
                c.__setitem__("panel_message_id", None),
                c.__setitem__("j2c_enabled", False),
            ))

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> None:
        """Heal the interface channel lock-down if someone edits its overwrites."""
        if not isinstance(after, discord.TextChannel):
            return
        config = await self.get_config(after.guild.id)
        if after.id != config.get("interface_channel_id"):
            return
        me = after.guild.me
        everyone_ow = after.overwrites_for(after.guild.default_role)
        if everyone_ow.send_messages is False and me is not None:
            return  # already locked
        try:
            ow = after.overwrites_for(after.guild.default_role)
            ow.send_messages = False
            await after.set_permissions(
                after.guild.default_role, overwrite=ow, reason="Re-lock vc-controls"
            )
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------- temp channels

    async def resolve_host_context(self, interaction: discord.Interaction) -> TempVoice | None:
        assert interaction.guild is not None
        member = interaction.user
        assert isinstance(member, discord.Member)
        row = None
        if isinstance(interaction.channel, discord.TextChannel):
            row = await self.get_row(interaction.guild.id, interaction.channel.id)
            if row and row.owner_id != member.id and not member.guild_permissions.manage_channels:
                await interaction.response.send_message("Only the channel host can use this here.", ephemeral=True)
                return None
        if row is None and member.voice and member.voice.channel:
            row = await self.get_row(interaction.guild.id, member.voice.channel.id)
        if row is None:
            await interaction.response.send_message("Connect to your temp channel (or use its text chat) first.", ephemeral=True)
            return None
        if row.owner_id != member.id and not member.guild_permissions.manage_channels:
            await interaction.response.send_message("You are not the host of that channel.", ephemeral=True)
            return None
        vc = interaction.guild.get_channel(row.channel_id)
        if not isinstance(vc, discord.VoiceChannel):
            await interaction.response.send_message("That temp channel no longer exists.", ephemeral=True)
            return None
        row._vc = vc
        return row

    async def _resolve_panel_ctx(
        self, interaction: discord.Interaction
    ) -> tuple[discord.Member, discord.VoiceChannel, TempVoice] | None:
        """Resolve (member, their temp VC, db row) for a panel button press."""
        assert interaction.guild is not None
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
            await interaction.response.send_message(
                f"{WARN} Join your temp voice channel first.", ephemeral=True
            )
            return None
        vc = member.voice.channel
        row = await self.get_row(interaction.guild.id, vc.id)
        if row is None:
            await interaction.response.send_message(
                f"{CROSS} This isn't a Sentra temp channel.", ephemeral=True
            )
            return None
        return member, vc, row

    async def _panel_host_only(
        self, interaction: discord.Interaction
    ) -> tuple[discord.Member, discord.VoiceChannel, TempVoice] | None:
        ctx = await self._resolve_panel_ctx(interaction)
        if ctx is None:
            return None
        member, vc, row = ctx
        if row.owner_id != member.id and not member.guild_permissions.manage_channels:
            await interaction.response.send_message(
                f"{CROSS} Only the host can do that — use 👑 Claim if the host left.", ephemeral=True
            )
            return None
        return ctx

    async def create_temp(self, member: discord.Member, hub: discord.VoiceChannel) -> None:
        guild = member.guild
        me = guild.me
        if me is None or not me.guild_permissions.manage_channels:
            if guild.id not in self._warned_no_perm:
                self._warned_no_perm.add(guild.id)
                log_channel = guild.system_channel
                if log_channel and log_channel.permissions_for(me).send_messages:
                    try:
                        await log_channel.send(
                            f"{WARN} Voice needs **Manage Channels** to run join-to-create."
                        )
                    except discord.HTTPException:
                        pass
            return
        config = await self.get_config(guild.id)
        name = sanitize_name(f"{member.display_name}'s Channel")
        try:
            vc = await guild.create_voice_channel(name, category=hub.category, reason=f"Temp VC for {member}")
            await member.move_to(vc, reason="Joined join-to-create hub")
        except discord.HTTPException:
            return
        text_channel: discord.TextChannel | None = None
        if config["text_in_voice"]:
            try:
                overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
                overwrites[member] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, manage_messages=True
                )
                text_channel = await guild.create_text_channel(
                    name=sanitize_name(f"{name} chat"),
                    category=hub.category,
                    overwrites=overwrites,
                    topic=f"Text chat for voice channel {vc.id}",
                    reason=f"Text-in-voice for {member}",
                )
            except discord.HTTPException:
                text_channel = None
        async with session_factory() as session:
            session.add(
                TempVoice(
                    guild_id=guild.id,
                    channel_id=vc.id,
                    text_channel_id=text_channel.id if text_channel else None,
                    owner_id=member.id,
                )
            )
            await session.commit()
        if text_channel:
            embed = discord.Embed(
                title=f"👋 Welcome, {member.display_name}",
                description=(
                    f"This is the private chat for **{vc.name}**.\n"
                    "Manage your channel with the buttons in the **vc-controls** channel, "
                    "or the `/voice` commands."
                ),
                color=VOICE,
            )
            embed.set_footer(text="You are the host — invite friends by joining your channel.")
            style(embed)
            try:
                await text_channel.send(embed=embed)
            except discord.HTTPException:
                pass

    async def delete_temp(self, guild: discord.Guild, vc: discord.VoiceChannel, row: TempVoice) -> None:
        async with session_factory() as session:
            await session.execute(delete(TempVoice).where(TempVoice.id == row.id))
            await session.commit()
        if row.text_channel_id:
            text_channel = guild.get_channel(row.text_channel_id)
            if isinstance(text_channel, discord.TextChannel):
                try:
                    await text_channel.delete(reason="Temp voice emptied")
                except discord.HTTPException:
                    pass
        try:
            await vc.delete(reason="Temp voice emptied")
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        guild = member.guild
        if guild.me is None:
            return
        config = await self.get_config(guild.id)

        hub_ids = set(config["hubs"])
        if config.get("hub_channel_id"):
            hub_ids.add(int(config["hub_channel_id"]))
        if after.channel and after.channel.id in hub_ids and (before.channel != after.channel):
            await self.create_temp(member, after.channel)

        await self.apply_voice_roles(member, before.channel, after.channel, config)

        left = before.channel
        if left is None or (after.channel and after.channel.id == left.id):
            return
        row = await self.get_row(guild.id, left.id)
        if row is None:
            return

        if row.owner_id == member.id:
            remaining = [m for m in left.members if not m.bot]
            if remaining:
                new_owner = remaining[0]
                async with session_factory() as session:
                    stored = await session.get(TempVoice, row.id)
                    if stored:
                        stored.owner_id = new_owner.id
                        await session.commit()
                channel = guild.get_channel(row.text_channel_id) if row.text_channel_id else None
                if isinstance(channel, discord.TextChannel):
                    try:
                        await channel.send(f"👑 <@{new_owner.id}> is the new channel host.")
                    except discord.HTTPException:
                        pass
                return
        await asyncio.sleep(1.5)
        fresh_vc = guild.get_channel(left.id)
        if fresh_vc and isinstance(fresh_vc, discord.VoiceChannel):
            if not any(not m.bot for m in fresh_vc.members):
                await self.delete_temp(guild, fresh_vc, row)

    async def apply_voice_roles(
        self,
        member: discord.Member,
        before_ch: discord.abc.Snowflake | None,
        after_ch: discord.abc.Snowflake | None,
        config: dict,
    ) -> None:
        mapping = config["voice_roles"]
        if not mapping:
            return
        lost = set(mapping.get(str(before_ch.id), [])) if before_ch else set()
        gained = set(mapping.get(str(after_ch.id), [])) if after_ch else set()
        to_remove = [r for r in (lost - gained)]
        to_add = [r for r in (gained - lost)]
        if not to_remove and not to_add:
            return
        me = member.guild.me
        current_ids = {r.id for r in member.roles}
        remove_objs = [
            role for rid in to_remove if (role := member.guild.get_role(rid)) and role.id in current_ids
        ]
        add_objs = [
            role
            for rid in to_add
            if (role := member.guild.get_role(rid)) and role.id not in current_ids and role < me.top_role
        ]
        try:
            if remove_objs:
                await member.remove_roles(*remove_objs, reason="Voice role binding left")
            if add_objs:
                await member.add_roles(*add_objs, reason="Voice role binding joined")
        except discord.HTTPException:
            pass

    @tasks.loop(minutes=10)
    async def _sweeper(self) -> None:
        await self.bot.wait_until_ready()
        async with session_factory() as session:
            rows = (await session.execute(select(TempVoice))).scalars().all()
        for row in rows:
            guild = self.bot.get_guild(row.guild_id)
            if guild is None:
                continue
            vc = guild.get_channel(row.channel_id)
            if vc is None:
                async with session_factory() as session:
                    await session.execute(delete(TempVoice).where(TempVoice.id == row.id))
                    if row.text_channel_id:
                        stale_text = guild.get_channel(row.text_channel_id)
                        if isinstance(stale_text, discord.TextChannel):
                            try:
                                await stale_text.delete(reason="Orphaned temp chat")
                            except discord.HTTPException:
                                pass
                    await session.commit()
            elif row.text_channel_id and guild.get_channel(row.text_channel_id) is None:
                async with session_factory() as session:
                    stored = await session.get(TempVoice, row.id)
                    if stored:
                        stored.text_channel_id = None
                        await session.commit()

    @_sweeper.before_loop
    async def before_sweeper(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------- panel router

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if custom_id.startswith("vc:focus:"):
            await self._handle_focus(interaction)
            return
        if not custom_id.startswith(PANEL_PREFIX):
            return
        action = custom_id[len(PANEL_PREFIX):]
        if action == "lock":
            await self._panel_lock(interaction, True)
        elif action == "unlock":
            await self._panel_lock(interaction, False)
        elif action == "hide":
            await self._panel_hide(interaction, True)
        elif action == "unhide":
            await self._panel_hide(interaction, False)
        elif action == "limit":
            await self._panel_limit(interaction)
        elif action == "ban":
            await self._panel_ban(interaction)
        elif action == "permit":
            await self._panel_permit(interaction)
        elif action == "invite":
            await self._panel_invite(interaction)
        elif action == "thread":
            await self._panel_thread(interaction)
        elif action == "transfer":
            await self._panel_transfer(interaction)
        elif action == "claim":
            await self._panel_claim(interaction)
        elif action == "delete":
            await self._panel_delete(interaction)
        elif action == "delete-confirm":
            await self._panel_delete_confirm(interaction)
        elif action == "rename":
            await self._panel_rename(interaction)
        elif action == "limit-pick":
            await self._panel_limit_pick(interaction)
        elif action == "ban-pick":
            await self._panel_ban_pick(interaction)
        elif action == "permit-pick":
            await self._panel_permit_pick(interaction)
        elif action == "transfer-pick":
            await self._panel_transfer_pick(interaction)

    async def _handle_focus(self, interaction: discord.Interaction) -> None:
        custom_id = (interaction.data or {}).get("custom_id", "")
        if interaction.guild is None:
            return
        _, _, action, _ = custom_id.split(":", 3)
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
            await interaction.response.send_message("Join the voice channel first.", ephemeral=True)
            return
        me = interaction.guild.me
        if not me.guild_permissions.mute_members:
            await interaction.response.send_message("I need the **Mute Members** permission.", ephemeral=True)
            return
        mute = action == "mute"
        try:
            await member.edit(mute=mute, reason="Focus mode consent")
        except discord.HTTPException:
            await interaction.response.send_message("Could not update your mute state.", ephemeral=True)
            return
        await interaction.response.send_message("🔇 You're muted." if mute else "🔊 You're unmuted.", ephemeral=True)

    # ------------------------------------------------------------- panel actions

    async def _panel_lock(self, interaction: discord.Interaction, lock: bool) -> None:
        ctx = await self._panel_host_only(interaction)
        if ctx is None:
            return
        _, vc, _ = ctx
        overwrites = dict(vc.overwrites)
        everyone = overwrites.get(interaction.guild.default_role, discord.PermissionOverwrite())
        everyone.connect = False if lock else None
        overwrites[interaction.guild.default_role] = everyone
        try:
            await vc.edit(overwrites=overwrites, reason=f"{'Locked' if lock else 'Unlocked'} via panel by {interaction.user}")
        except discord.HTTPException:
            await interaction.response.send_message(f"{CROSS} I couldn't do that — check my permissions.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🔒 Channel locked." if lock else "🔓 Channel unlocked.", ephemeral=True
        )

    async def _panel_hide(self, interaction: discord.Interaction, hide: bool) -> None:
        ctx = await self._panel_host_only(interaction)
        if ctx is None:
            return
        _, vc, _ = ctx
        overwrites = dict(vc.overwrites)
        everyone = overwrites.get(interaction.guild.default_role, discord.PermissionOverwrite())
        everyone.view_channel = False if hide else None
        overwrites[interaction.guild.default_role] = everyone
        try:
            await vc.edit(overwrites=overwrites, reason=f"{'Hidden' if hide else 'Unhidden'} via panel by {interaction.user}")
        except discord.HTTPException:
            await interaction.response.send_message(f"{CROSS} I couldn't do that — check my permissions.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🙈 Channel hidden from @everyone." if hide else "👁 Channel visible to @everyone.", ephemeral=True
        )

    async def _panel_limit(self, interaction: discord.Interaction) -> None:
        ctx = await self._panel_host_only(interaction)
        if ctx is None:
            return
        options = [
            discord.SelectOption(label=label if label != "No limit" else "No limit ∞", value=str(value))
            for label, value in LIMIT_CHOICES
        ]
        select = discord.ui.Select(
            placeholder="How many members can join?",
            options=options,
            custom_id=PANEL_PREFIX + "limit-pick",
        )
        view = discord.ui.View(timeout=180)
        view.add_item(select)
        await interaction.response.send_message(view=view, ephemeral=True)

    async def _panel_limit_pick(self, interaction: discord.Interaction) -> None:
        ctx = await self._panel_host_only(interaction)
        if ctx is None:
            return
        _, vc, _ = ctx
        raw = (interaction.data or {}).get("values", ["0"])[0]
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        try:
            await vc.edit(user_limit=value or None, reason=f"Limit set via panel by {interaction.user}")
        except discord.HTTPException:
            await interaction.response.send_message(f"{CROSS} Couldn't apply the limit.", ephemeral=True)
            return
        label = "unlimited" if value == 0 else str(value)
        await interaction.response.edit_message(
            content=f"{TICK} Limit: **{label}**.", view=None
        )

    async def _panel_ban(self, interaction: discord.Interaction) -> None:
        ctx = await self._panel_host_only(interaction)
        if ctx is None:
            return
        options = [
            discord.SelectOption(label=m.display_name[:100], value=str(m.id), emoji="🚫")
            for m in ctx[1].members
            if not m.bot
        ]
        if not options:
            await interaction.response.send_message(
                "Nobody to ban — use `/voice reject @user` for members outside your channel.", ephemeral=True
            )
            return
        select = discord.ui.Select(
            placeholder="Who should be banned from your channel?",
            options=options[:25],
            custom_id=PANEL_PREFIX + "ban-pick",
        )
        view = discord.ui.View(timeout=180)
        view.add_item(select)
        await interaction.response.send_message(view=view, ephemeral=True)

    async def _panel_ban_pick(self, interaction: discord.Interaction) -> None:
        ctx = await self._panel_host_only(interaction)
        if ctx is None:
            return
        _, vc, _ = ctx
        raw = (interaction.data or {}).get("values", [])
        if not raw:
            await interaction.response.edit_message(content=f"{CROSS} Nobody selected.", view=None)
            return
        user_id = int(raw[0])
        user = interaction.guild.get_member(user_id)
        if user is None:
            await interaction.response.edit_message(content=f"{CROSS} Member not found.", view=None)
            return
        overwrites = dict(vc.overwrites)
        overwrites[user] = discord.PermissionOverwrite(connect=False, view_channel=False)
        try:
            await vc.edit(overwrites=overwrites, reason=f"Banned via panel by {interaction.user}")
            if user.voice and user.voice.channel and user.voice.channel.id == vc.id:
                await user.move_to(None, reason=f"Banned from temp VC by {interaction.user}")
        except discord.HTTPException:
            await interaction.response.edit_message(content=f"{CROSS} Couldn't ban them.", view=None)
            return
        await interaction.response.edit_message(
            content=f"{TICK} 🚫 {user.mention} banned from your channel.", view=None
        )

    async def _panel_permit(self, interaction: discord.Interaction) -> None:
        ctx = await self._panel_host_only(interaction)
        if ctx is None:
            return
        # populate with members currently in the channel (that's who you'd permit
        # back in after a ban/lock) — for others, use /voice permit
        options = [
            discord.SelectOption(label=m.display_name[:100], value=str(m.id), emoji="✅")
            for m in ctx[1].members
            if not m.bot
        ][:25]
        if not options:
            await interaction.response.send_message(
                "Nobody to permit — use `/voice permit @user` for members outside your channel.", ephemeral=True
            )
            return
        select = discord.ui.Select(
            placeholder="Who may join your channel?",
            options=options,
            custom_id=PANEL_PREFIX + "permit-pick",
        )
        view = discord.ui.View(timeout=180)
        view.add_item(select)
        await interaction.response.send_message(view=view, ephemeral=True)

    async def _panel_permit_pick(self, interaction: discord.Interaction) -> None:
        ctx = await self._panel_host_only(interaction)
        if ctx is None:
            return
        _, vc, _ = ctx
        raw = (interaction.data or {}).get("values", [])
        if not raw:
            await interaction.response.edit_message(content=f"{CROSS} Nobody selected.", view=None)
            return
        user_id = int(raw[0])
        user = interaction.guild.get_member(user_id)
        if user is None:
            await interaction.response.edit_message(content=f"{CROSS} Member not found.", view=None)
            return
        overwrites = dict(vc.overwrites)
        overwrites[user] = discord.PermissionOverwrite(connect=True, view_channel=True, speak=True)
        try:
            await vc.edit(overwrites=overwrites, reason=f"Permitted via panel by {interaction.user}")
        except discord.HTTPException:
            await interaction.response.edit_message(content=f"{CROSS} Couldn't permit them.", view=None)
            return
        await interaction.response.edit_message(
            content=f"{TICK} {user.mention} may now join.", view=None
        )

    async def _panel_invite(self, interaction: discord.Interaction) -> None:
        ctx = await self._panel_host_only(interaction)
        if ctx is None:
            return
        _, vc, _ = ctx
        try:
            invite = await vc.create_invite(max_age=3600, max_uses=0, reason=f"Invite via panel by {interaction.user}")
        except discord.HTTPException:
            await interaction.response.send_message(f"{CROSS} Couldn't create an invite.", ephemeral=True)
            return
        await interaction.response.send_message(f"🔗 Your invite (1h): {invite.url}", ephemeral=True)

    async def _panel_thread(self, interaction: discord.Interaction) -> None:
        ctx = await self._panel_host_only(interaction)
        if ctx is None:
            return
        _, vc, _ = ctx
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(f"{CROSS} Use this in the vc-controls channel.", ephemeral=True)
            return
        me = interaction.guild.me
        if me is None or not me.guild_permissions.create_public_threads:
            await interaction.response.send_message(
                f"{CROSS} I need the **Create Public Threads** permission.", ephemeral=True
            )
            return
        try:
            thread = await interaction.channel.create_thread(
                name=sanitize_name(vc.name)[:100], auto_archive_duration=1440, reason=f"Thread via panel by {interaction.user}"
            )
        except discord.HTTPException:
            await interaction.response.send_message(f"{CROSS} Couldn't create the thread.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"{TICK} 🧵 Thread created: {thread.mention} — talk about **{vc.name}** there.", ephemeral=True
        )

    async def _panel_transfer(self, interaction: discord.Interaction) -> None:
        ctx = await self._panel_host_only(interaction)
        if ctx is None:
            return
        _, vc, _ = ctx
        options = [
            discord.SelectOption(label=m.display_name[:100], value=str(m.id))
            for m in vc.members
            if not m.bot and m.id != interaction.user.id
        ]
        if not options:
            await interaction.response.send_message("Nobody else is in your channel to transfer to.", ephemeral=True)
            return
        select = discord.ui.Select(
            placeholder="Transfer hosting to…",
            options=options[:25],
            custom_id=PANEL_PREFIX + "transfer-pick",
        )
        view = discord.ui.View(timeout=180)
        view.add_item(select)
        await interaction.response.send_message(view=view, ephemeral=True)

    async def _panel_transfer_pick(self, interaction: discord.Interaction) -> None:
        ctx = await self._panel_host_only(interaction)
        if ctx is None:
            return
        raw = (interaction.data or {}).get("values", [])
        if not raw:
            await interaction.response.edit_message(content=f"{CROSS} Nobody selected.", view=None)
            return
        member = interaction.guild.get_member(int(raw[0]))
        if member is None:
            await interaction.response.edit_message(content=f"{CROSS} Member not found.", view=None)
            return
        async with session_factory() as session:
            stored = await session.get(TempVoice, ctx[2].id)
            if stored:
                stored.owner_id = member.id
                await session.commit()
        await interaction.response.edit_message(
            content=f"{TICK} 👑 {member.mention} is now the host.", view=None
        )

    async def _panel_claim(self, interaction: discord.Interaction) -> None:
        ctx = await self._resolve_panel_ctx(interaction)
        if ctx is None:
            return
        member, vc, row = ctx
        host_present = any(m.id == row.owner_id for m in vc.members)
        if host_present and row.owner_id != member.id:
            await interaction.response.send_message(f"{CROSS} The host is still here.", ephemeral=True)
            return
        async with session_factory() as session:
            stored = await session.get(TempVoice, row.id)
            if stored:
                stored.owner_id = member.id
                await session.commit()
        await interaction.response.send_message(
            f"{TICK} 👑 You claimed **{vc.name}**.", ephemeral=True
        )

    async def _panel_delete(self, interaction: discord.Interaction) -> None:
        ctx = await self._panel_host_only(interaction)
        if ctx is None:
            return
        view = discord.ui.View(timeout=60)
        view.add_item(
            discord.ui.Button(
                label="Confirm delete", style=discord.ButtonStyle.danger,
                custom_id=PANEL_PREFIX + "delete-confirm",
            )
        )
        await interaction.response.send_message(
            f"⚠️ Delete **{ctx[1].name}**? This can't be undone.", view=view, ephemeral=True
        )

    async def _panel_delete_confirm(self, interaction: discord.Interaction) -> None:
        ctx = await self._panel_host_only(interaction)
        if ctx is None:
            return
        _, vc, row = ctx
        await interaction.response.edit_message(content="🗑 Deleting…", view=None)
        await self.delete_temp(interaction.guild, vc, row)
        await interaction.edit_original_response(content=f"{TICK} Channel deleted.")

    async def _panel_rename(self, interaction: discord.Interaction) -> None:
        ctx = await self._panel_host_only(interaction)
        if ctx is None:
            return
        modal = _RenameModal(self, interaction.user.id)
        await interaction.response.send_modal(modal)

    # ------------------------------------------------------------- slash commands

    @voice.command(name="setup", description="Create (or heal) the join-to-create category, hub and control panel")
    @app_commands.default_permissions(manage_guild=True)
    async def setup_cmd(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        me = interaction.guild.me
        if me is None or not me.guild_permissions.manage_channels:
            await interaction.response.send_message(
                f"{CROSS} I need **Manage Channels**.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        config = await self._provision(interaction.guild)
        if config is None:
            await interaction.followup.send(f"{CROSS} Setup failed — check my permissions and retry.", ephemeral=True)
            return
        hub = f"<#{config['hub_channel_id']}>" if config.get("hub_channel_id") else "—"
        interface = f"<#{config['interface_channel_id']}>" if config.get("interface_channel_id") else "—"
        embed = discord.Embed(title="🎙️ Join-to-Create ready", color=VOICE)
        embed.add_field(name="Hub", value=hub)
        embed.add_field(name="Controls", value=interface)
        style(embed, interaction, emoji="🎙️")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @voice.command(name="disable", description="Disable join-to-create (temp channels keep working until empty)")
    @app_commands.default_permissions(manage_guild=True)
    async def disable_cmd(self, interaction: discord.Interaction) -> None:
        await self.update_config(interaction.guild_id, lambda c: c.__setitem__("j2c_enabled", False))
        await interaction.response.send_message(f"{TICK} Join-to-create **disabled**.", ephemeral=True)

    @vrole.command(name="add", description="Grant a role while members sit in a voice channel")
    async def vrole_add(
        self, interaction: discord.Interaction, channel: discord.VoiceChannel, role: discord.Role
    ) -> None:
        if role >= interaction.guild.me.top_role or role.managed:
            await interaction.response.send_message("I can't grant that role.", ephemeral=True)
            return

        def mutate(cfg: dict) -> None:
            roles = dict(cfg["voice_roles"])
            entry = list(set(roles.get(str(channel.id), [])) | {role.id})
            roles[str(channel.id)] = entry
            cfg["voice_roles"] = roles

        await self.update_config(interaction.guild_id, mutate)
        await interaction.response.send_message(
            f"{TICK} Members in {channel.mention} now get {role.mention}.", ephemeral=True
        )

    @vrole.command(name="remove", description="Stop granting a role in a voice channel")
    async def vrole_remove(
        self, interaction: discord.Interaction, channel: discord.VoiceChannel, role: discord.Role
    ) -> None:
        def mutate(cfg: dict) -> None:
            roles = dict(cfg["voice_roles"])
            roles[str(channel.id)] = [r for r in roles.get(str(channel.id), []) if r != role.id]
            if not roles[str(channel.id)]:
                del roles[str(channel.id)]
            cfg["voice_roles"] = roles

        await self.update_config(interaction.guild_id, mutate)
        await interaction.response.send_message(f"{role.mention} is no longer bound to {channel.mention}.", ephemeral=True)

    @vrole.command(name="list", description="List voice-role bindings")
    async def vrole_list(self, interaction: discord.Interaction) -> None:
        config = await self.get_config(interaction.guild_id)
        if not config["voice_roles"]:
            await interaction.response.send_message("No bindings yet.", ephemeral=True)
            return
        lines = [
            f"<#{ch}> → {' '.join(f'<@&{rid}>' for rid in roles)}"
            for ch, roles in config["voice_roles"].items()
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @voice.command(name="textinvc", description="Toggle auto text chats for temp channels")
    async def textinvc(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self.update_config(interaction.guild_id, lambda cfg: cfg.__setitem__("text_in_voice", enabled))
        await interaction.response.send_message(f"Text-in-voice is **{'on' if enabled else 'off'}** for new temp channels.", ephemeral=True)

    @voice.command(name="lock", description="Lock your temp channel")
    async def lock(self, interaction: discord.Interaction) -> None:
        ctx = await self.resolve_host_context(interaction)
        if ctx is None:
            return
        vc: discord.VoiceChannel = ctx._vc
        overwrites = dict(vc.overwrites)
        everyone = overwrites.get(interaction.guild.default_role, discord.PermissionOverwrite())
        everyone.connect = False
        overwrites[interaction.guild.default_role] = everyone
        try:
            await vc.edit(overwrites=overwrites, reason=f"Locked by {interaction.user}")
        except discord.HTTPException:
            await interaction.response.send_message("I couldn't lock it — missing permissions.", ephemeral=True)
            return
        async with session_factory() as session:
            stored = await session.get(TempVoice, ctx.id)
            if stored:
                stored.locked = True
                await session.commit()
        await interaction.response.send_message("🔒 Channel locked.")

    @voice.command(name="unlock", description="Unlock your temp channel")
    async def unlock(self, interaction: discord.Interaction) -> None:
        ctx = await self.resolve_host_context(interaction)
        if ctx is None:
            return
        vc: discord.VoiceChannel = ctx._vc
        overwrites = dict(vc.overwrites)
        everyone = overwrites.get(interaction.guild.default_role, discord.PermissionOverwrite())
        everyone.connect = None
        overwrites[interaction.guild.default_role] = everyone
        try:
            await vc.edit(overwrites=overwrites, reason=f"Unlocked by {interaction.user}")
        except discord.HTTPException:
            await interaction.response.send_message("I couldn't unlock it.", ephemeral=True)
            return
        async with session_factory() as session:
            stored = await session.get(TempVoice, ctx.id)
            if stored:
                stored.locked = False
                await session.commit()
        await interaction.response.send_message("🔓 Channel unlocked.")

    @voice.command(name="limit", description="Set a max number of users (0 = unlimited)")
    async def limit(self, interaction: discord.Interaction, users: app_commands.Range[int, 0, 99]) -> None:
        ctx = await self.resolve_host_context(interaction)
        if ctx is None:
            return
        vc: discord.VoiceChannel = ctx._vc
        try:
            await vc.edit(user_limit=users or None, reason=f"Limit set by {interaction.user}")
        except discord.HTTPException:
            await interaction.response.send_message("Couldn't apply the limit.", ephemeral=True)
            return
        await interaction.response.send_message(f"👥 Limit: **{'unlimited' if users == 0 else users}**.")

    @voice.command(name="permit", description="Allow a user or role into your locked channel")
    async def permit(self, interaction: discord.Interaction, target: discord.Member | discord.Role) -> None:
        ctx = await self.resolve_host_context(interaction)
        if ctx is None:
            return
        vc: discord.VoiceChannel = ctx._vc
        overwrites = dict(vc.overwrites)
        allow = discord.PermissionOverwrite(connect=True, view_channel=True, speak=True)
        if row_text := getattr(ctx, "text_channel_id", None):
            text_channel = interaction.guild.get_channel(row_text)
            if isinstance(text_channel, discord.TextChannel):
                t_overwrites = dict(text_channel.overwrites)
                t_overwrites[target] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
                try:
                    await text_channel.edit(overwrites=t_overwrites)
                except discord.HTTPException:
                    pass
        overwrites[target] = allow
        try:
            await vc.edit(overwrites=overwrites, reason=f"Permitted by {interaction.user}")
        except discord.HTTPException:
            await interaction.response.send_message("Couldn't update permissions.", ephemeral=True)
            return
        await interaction.response.send_message(f"{TICK} {target.mention} may now join.")

    @voice.command(name="reject", description="Block someone from your channel and disconnect them")
    async def reject(self, interaction: discord.Interaction, user: discord.Member) -> None:
        ctx = await self.resolve_host_context(interaction)
        if ctx is None:
            return
        vc: discord.VoiceChannel = ctx._vc
        overwrites = dict(vc.overwrites)
        deny = discord.PermissionOverwrite(connect=False, view_channel=False)
        overwrites[user] = deny
        try:
            await vc.edit(overwrites=overwrites, reason=f"Rejected by {interaction.user}")
            if user.voice and user.voice.channel and user.voice.channel.id == vc.id:
                await user.move_to(None, reason=f"Rejected by {interaction.user}")
        except discord.HTTPException:
            await interaction.response.send_message("Couldn't do that — check my permissions.", ephemeral=True)
            return
        await interaction.response.send_message(f"🚫 {user.mention} was rejected and removed.")

    @voice.command(name="kick", description="Disconnect someone from your channel (they can rejoin)")
    async def kick(self, interaction: discord.Interaction, user: discord.Member) -> None:
        ctx = await self.resolve_host_context(interaction)
        if ctx is None:
            return
        vc: discord.VoiceChannel = ctx._vc
        if not (user.voice and user.voice.channel and user.voice.channel.id == vc.id):
            await interaction.response.send_message("They're not in your channel.", ephemeral=True)
            return
        try:
            await user.move_to(None, reason=f"Kicked from temp VC by {interaction.user}")
        except discord.HTTPException:
            await interaction.response.send_message("Couldn't move them.", ephemeral=True)
            return
        await interaction.response.send_message(f"👢 Removed {user.mention} from the channel.")

    @voice.command(name="rename", description="Rename your temp channel")
    async def rename(self, interaction: discord.Interaction, name: str) -> None:
        ctx = await self.resolve_host_context(interaction)
        if ctx is None:
            return
        vc: discord.VoiceChannel = ctx._vc
        clean = sanitize_name(name)
        try:
            await vc.edit(name=clean, reason=f"Renamed by {interaction.user}")
        except discord.HTTPException:
            await interaction.response.send_message("Rename failed (rate limits cool down renames).", ephemeral=True)
            return
        await interaction.response.send_message(f"✏️ Renamed to **{clean}**.")

    @voice.command(name="transfer", description="Give hosting of your channel to another member")
    async def transfer(self, interaction: discord.Interaction, member: discord.Member) -> None:
        ctx = await self.resolve_host_context(interaction)
        if ctx is None:
            return
        if member.bot:
            await interaction.response.send_message("Bots make poor hosts.", ephemeral=True)
            return
        async with session_factory() as session:
            stored = await session.get(TempVoice, ctx.id)
            if stored:
                stored.owner_id = member.id
                await session.commit()
        await interaction.response.send_message(f"👑 {member.mention} is now the host.")

    @voice.command(name="claim", description="Claim a temp channel whose host has left")
    async def claim(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        member = interaction.user
        assert isinstance(member, discord.Member)
        if not (member.voice and member.voice.channel):
            await interaction.response.send_message("Connect to the channel first.", ephemeral=True)
            return
        row = await self.get_row(interaction.guild.id, member.voice.channel.id)
        if row is None:
            await interaction.response.send_message("This isn't a temp channel.", ephemeral=True)
            return
        host_present = any(m.id == row.owner_id for m in member.voice.channel.members)
        if host_present:
            await interaction.response.send_message("The host is still here.", ephemeral=True)
            return
        async with session_factory() as session:
            stored = await session.get(TempVoice, row.id)
            if stored:
                stored.owner_id = member.id
                await session.commit()
        await interaction.response.send_message(f"👑 You claimed **{member.voice.channel.name}**.")

    @voice.command(name="muteall", description="Server-mute everyone else in your channel")
    async def muteall(self, interaction: discord.Interaction) -> None:
        ctx = await self.resolve_host_context(interaction)
        if ctx is None:
            return
        vc: discord.VoiceChannel = ctx._vc
        me = interaction.guild.me
        if not me.guild_permissions.mute_members:
            await interaction.response.send_message("I need **Mute Members** permission.", ephemeral=True)
            return
        count = 0
        for target in vc.members:
            if target.bot or target.id == interaction.user.id or target.voice.mute:
                continue
            try:
                await target.edit(mute=True, reason=f"Muteall by {interaction.user}")
                count += 1
            except discord.HTTPException:
                continue
        await interaction.response.send_message(f"🔇 Muted **{count}** member(s).")

    @voice.command(name="unmuteall", description="Remove server mute from everyone in your channel")
    async def unmuteall(self, interaction: discord.Interaction) -> None:
        ctx = await self.resolve_host_context(interaction)
        if ctx is None:
            return
        vc: discord.VoiceChannel = ctx._vc
        me = interaction.guild.me
        if not me.guild_permissions.mute_members:
            await interaction.response.send_message("I need **Mute Members** permission.", ephemeral=True)
            return
        count = 0
        for target in vc.members:
            if target.bot or not target.voice.mute:
                continue
            try:
                await target.edit(mute=False, reason=f"Unmuteall by {interaction.user}")
                count += 1
            except discord.HTTPException:
                continue
        await interaction.response.send_message(f"🔊 Unmuted **{count}** member(s).")

    @voice.command(name="moveall", description="Move everyone in your channel to another voice channel")
    async def moveall(self, interaction: discord.Interaction, destination: discord.VoiceChannel) -> None:
        ctx = await self.resolve_host_context(interaction)
        if ctx is None:
            return
        vc: discord.VoiceChannel = ctx._vc
        me = interaction.guild.me
        perms = destination.permissions_for(me)
        if not (me.guild_permissions.move_members and perms.connect and perms.view_channel):
            await interaction.response.send_message("I can't move people there.", ephemeral=True)
            return
        moved = 0
        for target in list(vc.members):
            if target.bot:
                continue
            try:
                await target.move_to(destination, reason=f"Moveall by {interaction.user}")
                moved += 1
            except discord.HTTPException:
                continue
        await interaction.response.send_message(f"📦 Moved **{moved}** member(s) to {destination.mention}.")

    @voice.command(name="activity", description="Launch a Discord Activity in your channel")
    @app_commands.choices(game=[app_commands.Choice(name=name.replace("-", " ").title(), value=name) for name in ACTIVITIES])
    async def activity(self, interaction: discord.Interaction, game: app_commands.Choice[str]) -> None:
        ctx = await self.resolve_host_context(interaction)
        if ctx is None:
            return
        vc: discord.VoiceChannel = ctx._vc
        app_id = ACTIVITIES[game.value]
        try:
            invite = await vc.create_invite(
                max_age=300,
                unique=True,
                target_type=discord.InviteTarget.embedded_application,
                target_application_id=app_id,
                reason=f"Activity launch by {interaction.user}",
            )
        except discord.HTTPException:
            await interaction.response.send_message("Couldn't create the activity invite.", ephemeral=True)
            return
        await interaction.response.send_message(f"🎮 Join **{game.name}**: {invite.url}")

    @voice.command(name="focus", description="Start focus mode — listeners consent-mute themselves")
    async def focus(self, interaction: discord.Interaction) -> None:
        ctx = await self.resolve_host_context(interaction)
        if ctx is None:
            return
        vc: discord.VoiceChannel = ctx._vc
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Mute me 🔇", style=discord.ButtonStyle.primary, custom_id=f"vc:focus:mute:{vc.id}"))
        view.add_item(discord.ui.Button(label="Unmute me 🔊", style=discord.ButtonStyle.secondary, custom_id=f"vc:focus:unmute:{vc.id}"))
        embed = discord.Embed(
            title="🎯 Focus mode",
            description=(
                f"{interaction.user.mention} is starting a focused session in **{vc.name}**.\n"
                "Tap below to mute or unmute yourself — only you can change your own state."
            ),
            color=discord.Color.gold(),
        )
        if isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(embed=embed, view=view)
        else:
            await interaction.response.defer()
            text_channel = interaction.guild.get_channel(ctx.text_channel_id) if ctx.text_channel_id else None
            if isinstance(text_channel, discord.TextChannel):
                await text_channel.send(embed=embed, view=view)


class _RenameModal(discord.ui.Modal, title="Rename your channel"):
    name = discord.ui.TextInput(
        label="New channel name",
        placeholder="e.g. chill zone",
        required=True,
        max_length=90,
    )

    def __init__(self, cog: VoiceCog, host_id: int) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.host_id = host_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
            await interaction.response.send_message(f"{CROSS} Join your temp channel first.", ephemeral=True)
            return
        row = await self.cog.get_row(interaction.guild.id, member.voice.channel.id)
        if row is None or (row.owner_id != member.id and not member.guild_permissions.manage_channels):
            await interaction.response.send_message(f"{CROSS} Only the host can rename the channel.", ephemeral=True)
            return
        clean = sanitize_name(str(self.name.value))
        try:
            await member.voice.channel.edit(name=clean, reason=f"Renamed via panel by {member}")
        except discord.HTTPException:
            await interaction.response.send_message("Rename failed — try again in a minute.", ephemeral=True)
            return
        await interaction.response.send_message(f"{TICK} ✏️ Renamed to **{clean}**.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceCog(bot))
