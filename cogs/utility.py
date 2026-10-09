import datetime
import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from sentra.db.engine import session_factory
from sentra.db.models import Guild, Reminder
from sentra.cogs.reminders import parse_duration, utcnow
from sentra.ui import EMOJI, UTILITY, style, TICK

STAR_EMOJI = "⭐"

UTILITY_DEFAULTS = {
    "starboard": {"channel_id": None, "threshold": 5, "done": []},
    "role_menus": {},
}


class UtilityCog(commands.Cog):
    starboard = app_commands.Group(
        name="starboard",
        description="Highlight beloved messages",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )
    rolemenu = app_commands.Group(
        name="rolemenu",
        description="Self-service role pickers",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )
    schedule = app_commands.Group(
        name="schedule",
        description="Recurring announcements",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def get_config(self, guild_id: int) -> dict:
        async with session_factory() as session:
            guild = await session.get(Guild, guild_id)
            stored = dict((guild.config or {}).get("utility", {})) if guild else {}
        merged = {**UTILITY_DEFAULTS}
        for key, value in stored.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        return merged

    async def update_config(self, guild_id: int, mutate) -> None:
        async with session_factory() as session:
            guild = await session.get(Guild, guild_id)
            if guild is None:
                guild = Guild(id=guild_id)
                session.add(guild)
            full = dict(guild.config or {})
            cfg = {**UTILITY_DEFAULTS, **dict(full.get("utility", {}))}
            mutate(cfg)
            full["utility"] = cfg
            guild.config = full
            await session.commit()

    @starboard.command(name="set", description="Set the starboard channel and threshold")
    async def starboard_set(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        threshold: app_commands.Range[int, 1, 25] = 5,
    ) -> None:
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.embed_links):
            await interaction.response.send_message("I can't post there.", ephemeral=True)
            return

        def mutate(cfg: dict) -> None:
            cfg["starboard"] = {**cfg["starboard"], "channel_id": channel.id, "threshold": threshold}

        await self.update_config(interaction.guild_id, mutate)
        await interaction.response.send_message(
            f"⭐ Messages with {threshold}+ ⭐ will appear in {channel.mention}.", ephemeral=True
        )

    @starboard.command(name="off", description="Disable the starboard")
    async def starboard_off(self, interaction: discord.Interaction) -> None:
        await self.update_config(interaction.guild_id, lambda cfg: cfg["starboard"].__setitem__("channel_id", None))
        await interaction.response.send_message("Starboard disabled.", ephemeral=True)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or str(payload.emoji.name) != STAR_EMOJI:
            return
        config = await self.get_config(payload.guild_id)
        sb = config["starboard"]
        channel_id = sb.get("channel_id")
        if not channel_id:
            return
        if payload.channel_id == channel_id:
            return
        done = sb.get("done", [])
        if payload.message_id in done:
            return
        guild = self.bot.get_guild(payload.guild_id)
        origin = guild.get_channel(payload.channel_id) if guild else None
        board = guild.get_channel(channel_id) if guild else None
        if not isinstance(origin, discord.TextChannel) or not isinstance(board, discord.TextChannel):
            return
        try:
            message = await origin.fetch_message(payload.message_id)
            reaction = next((r for r in message.reactions if str(r.emoji) == STAR_EMOJI), None)
        except discord.HTTPException:
            return
        # Exclude self-stars from the count
        star_count = reaction.count if reaction else 0
        if reaction and message.author.id == payload.user_id:
            star_count = max(0, star_count - 1)
        if star_count < sb.get("threshold", 5):
            return
        embed = discord.Embed(
            description=message.content[:4000] or "*[attachment]*",
            color=discord.Color.gold(),
        )
        embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
        embed.add_field(name="Source", value=f"[Jump!]({message.jump_url})")
        if message.attachments:
            first = message.attachments[0]
            if first.content_type and first.content_type.startswith("image/"):
                embed.set_image(url=first.url)
        embed.set_footer(text=f"⭐ {reaction.count} · #{origin.name}")
        style(embed)
        try:
            await board.send(embed=embed)
        except discord.HTTPException:
            return

        def mark_done(cfg: dict) -> None:
            done_list = list(cfg["starboard"].get("done", []))
            done_list.append(payload.message_id)
            cfg["starboard"]["done"] = done_list[-500:]

        await self.update_config(payload.guild_id, mark_done)

    @rolemenu.command(name="new", description="Post a role picker (up to 10 roles)")
    @app_commands.describe(channel="Where to post", title="Heading above the menu")
    async def rolemenu_new(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: str,
        role1: discord.Role,
        role2: discord.Role | None = None,
        role3: discord.Role | None = None,
        role4: discord.Role | None = None,
        role5: discord.Role | None = None,
        role6: discord.Role | None = None,
        role7: discord.Role | None = None,
        role8: discord.Role | None = None,
        role9: discord.Role | None = None,
        role10: discord.Role | None = None,
    ) -> None:
        me = interaction.guild.me
        chosen = [r for r in [role1, role2, role3, role4, role5, role6, role7, role8, role9, role10] if r]
        bad = [r for r in chosen if r.managed or r >= me.top_role]
        if bad:
            await interaction.response.send_message(f"Cannot assign: {', '.join(r.mention for r in bad)}", ephemeral=True)
            return
        options = [
            discord.SelectOption(label=r.name[:100], value=str(r.id))
            for r in chosen[:20]
        ]
        # Send embed first without view to get message ID
        try:
            sent = await channel.send(embed=discord.Embed(description=f"**{title}**\nPick your roles below.", color=discord.Color.blurple()))
        except discord.HTTPException:
            await interaction.response.send_message("Couldn't post in that channel.", ephemeral=True)
            return
        # Now create select with the actual message ID as custom_id
        select_menu = discord.ui.Select(
            placeholder=title[:100],
            min_values=0,
            max_values=len(options),
            options=options,
            custom_id=f"rm:{sent.id}",
        )
        view = discord.ui.View(timeout=None)
        view.add_item(select_menu)
        try:
            await sent.edit(view=view)
        except discord.HTTPException:
            pass

        def mutate(cfg: dict) -> None:
            menus = dict(cfg["role_menus"])
            menus[str(sent.id)] = {
                "message_id": sent.id,
                "title": title[:100],
                "role_ids": [r.id for r in chosen],
            }
            cfg["role_menus"] = menus

        await self.update_config(interaction.guild_id, mutate)
        await interaction.response.send_message(f"<:sentra_check:1546486790779572385> Role menu posted in {channel.mention}.", ephemeral=True)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component or interaction.guild is None:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith("rm:"):
            return
        member = interaction.user
        if not isinstance(member, discord.Member):
            return
        values = (interaction.data or {}).get("values", [])
        config = await self.get_config(interaction.guild.id)
        menu_key = custom_id.split(":", 1)[1]
        menus_by_msg = {str(v.get("message_id")): v for v in config["role_menus"].values()}
        menu = menus_by_msg.get(menu_key)
        if not menu:
            await interaction.response.send_message("This role menu is no longer active.", ephemeral=True)
            return
        allowed = set(menu["role_ids"])
        current = {r.id for r in member.roles} & allowed
        desired = {int(v) for v in values if int(v) in allowed}
        to_add = [interaction.guild.get_role(rid) for rid in desired - current]
        to_remove = [interaction.guild.get_role(rid) for rid in current - desired]
        valid_adds = [r for r in to_add if r and r < interaction.guild.me.top_role and not r.managed]
        valid_removes = [r for r in to_remove if r and r < interaction.guild.me.top_role and not r.managed]
        try:
            if valid_adds:
                await member.add_roles(*valid_adds, reason="Role menu")
            if valid_removes:
                await member.remove_roles(*valid_removes, reason="Role menu")
        except discord.HTTPException:
            await interaction.response.send_message("Couldn't update roles — missing permissions.", ephemeral=True)
            return
        names = [f"<@&{rid}>" for rid in sorted(desired)] or ["*none*"]
        await interaction.response.send_message(f"<:sentra_check:1546486790779572385> Your roles: {' '.join(names)}", ephemeral=True)

    @schedule.command(name="post", description="Schedule a recurring announcement")
    @app_commands.describe(
        channel="Where it posts",
        every='Interval, e.g. "1d" or "6h"',
        text="The announcement body",
    )
    async def schedule_post(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        every: str,
        text: str,
    ) -> None:
        seconds = parse_duration(every)
        if seconds is None or seconds < 300:
            await interaction.response.send_message('Interval must be at least 5 minutes, e.g. "30m", "6h", "1d".', ephemeral=True)
            return
        async with session_factory() as session:
            session.add(
                Reminder(
                    guild_id=interaction.guild_id,
                    channel_id=channel.id,
                    user_id=0,
                    content=text[:2000],
                    due_at=utcnow(),
                    repeat_seconds=seconds,
                )
            )
            await session.commit()
        await interaction.response.send_message(f"📢 Announcement scheduled — first post is live now, repeating every {every}.")

    @schedule.command(name="list", description="List scheduled announcements")
    async def schedule_list(self, interaction: discord.Interaction) -> None:
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(Reminder).where(Reminder.guild_id == interaction.guild_id, Reminder.user_id == 0)
                )
            ).scalars().all()
        if not rows:
            await interaction.response.send_message("No scheduled announcements.", ephemeral=True)
            return
        lines = [f"`#{r.id}` → <#{r.channel_id}> every {max(r.repeat_seconds // 60, 1)}m — {r.content[:60]}" for r in rows]
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    @schedule.command(name="cancel", description="Cancel a scheduled announcement by ID")
    async def schedule_cancel(self, interaction: discord.Interaction, id: int) -> None:
        from sqlalchemy import delete

        async with session_factory() as session:
            result = await session.execute(
                delete(Reminder).where(Reminder.id == id, Reminder.guild_id == interaction.guild_id, Reminder.user_id == 0)
            )
            await session.commit()
        if result.rowcount:
            await interaction.response.send_message(f"Cancelled announcement `#{id}`.", ephemeral=True)
        else:
            await interaction.response.send_message("Not found.", ephemeral=True)

    @app_commands.command(name="embed", description="Send a custom embed")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(title="Embed heading", description="Body text", color_hex="Hex like #5865F2", image_url="Optional image", channel="Target channel (default here)")
    async def embed_cmd(
        self,
        interaction: discord.Interaction,
        title: str,
        description: str | None = None,
        color_hex: str | None = None,
        image_url: str | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        color = discord.Color.blurple()
        if color_hex:
            try:
                color = discord.Color.from_str(color_hex)
            except ValueError:
                await interaction.response.send_message("Invalid hex color, e.g. `#5865F2`.", ephemeral=True)
                return
        target = channel or interaction.channel
        perms = target.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.embed_links):
            await interaction.response.send_message("I can't post there.", ephemeral=True)
            return
        embed = discord.Embed(title=title[:256], description=(description or "")[:4000], color=color)
        if image_url:
            embed.set_image(url=image_url)
        embed.set_footer(text=f"Posted by {interaction.user.display_name}")
        await interaction.response.defer(ephemeral=True)
        try:
            await target.send(embed=embed)
        except discord.HTTPException:
            await interaction.followup.send("Failed to send.")
            return
        await interaction.followup.send(f"Sent to {target.mention}.")

    @app_commands.command(name="insights", description="Server activity snapshot")
    @app_commands.guild_only()
    async def insights(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        fully_cached = len(guild.members) == guild.member_count
        week_ago = discord.utils.utcnow() - datetime.timedelta(days=7)
        joins_week = sum(1 for m in guild.members if m.joined_at and m.joined_at > week_ago) if fully_cached else None
        top_roles = sorted(
            (r for r in guild.roles if not r.managed and r != guild.default_role),
            key=lambda r: len(r.members),
            reverse=True,
        )[:5]
        humans = sum(1 for m in guild.members if not m.bot) if fully_cached else None
        bots = (len(guild.members) - humans) if fully_cached else None
        embed = discord.Embed(title=f"📊 Insights — {guild.name}", color=UTILITY)
        embed.add_field(name="Members", value=str(guild.member_count))
        if joins_week is not None:
            embed.add_field(name="Joins (7d)", value=str(joins_week))
        if bots is not None:
            embed.add_field(name="Bots", value=str(bots))
        embed.add_field(name="Boosts", value=f"Lv {guild.premium_tier} ({guild.premium_subscription_count or 0})")
        embed.add_field(
            name="Top roles",
            value="\n".join(f"{r.name}: {len(r.members)}" for r in top_roles) or "*none*",
        )
        embed.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, "D"))
        embed.set_footer(text="Full analytics ship with premium insights." if joins_week is None else "")
        style(embed, interaction, emoji=EMOJI["utility"])
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(UtilityCog(bot))
