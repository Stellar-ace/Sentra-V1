import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from sentra.config import settings
from sentra.db.engine import session_factory
from sentra.db.models import Guild, Member
from sentra.ui import EMOJI, PERSONA, style, TICK

from sentra.cogs.leveling import level_for, xp_for_level


class PersonaCog(commands.Cog):
    profile_group = app_commands.Group(name="profile", description="Member profiles", guild_only=True)

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def is_premium(self, guild_id: int) -> bool:
        return guild_id in settings.premium_guild_ids

    async def member_record(self, guild_id: int, user_id: int) -> Member | None:
        async with session_factory() as session:
            return (
                await session.execute(
                    select(Member).where(Member.guild_id == guild_id, Member.user_id == user_id)
                )
            ).scalar_one_or_none()

    async def customizations(self, guild_id: int) -> dict:
        async with session_factory() as session:
            guild = await session.get(Guild, guild_id)
            stored = dict((guild.config or {}).get("profiles", {})) if guild else {}
        return stored

    @profile_group.command(name="view", description="Show a member's profile card")
    async def view(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        assert interaction.guild is not None
        target = user or interaction.user
        record = await self.member_record(interaction.guild.id, target.id)
        customs = (await self.customizations(interaction.guild.id)).get(str(target.id), {})
        level = level_for(record.xp) if record else 0
        xp = record.xp if record else 0
        balance = record.balance if record else 0
        badges = []
        if target.premium_since:
            badges.append("💎 Booster")
        if level >= 10:
            badges.append("🔥 Level 10+")
        if xp >= 10000:
            badges.append("⭐ Veteran")
        color = customs.get("color")
        accent = (
            discord.Color.from_str(color) if isinstance(color, str) and color.startswith("#") else (target.color if target.color.value else PERSONA)
        )
        embed = discord.Embed(title=f"{target.display_name}", color=accent)
        embed.set_thumbnail(url=target.display_avatar.url)
        bio = customs.get("bio")
        if bio:
            embed.description = f"*{bio}*"
        next_xp = xp_for_level(level + 1)
        floor_xp = xp_for_level(level)
        span = max(next_xp - floor_xp, 1)
        into = max(xp - floor_xp, 0)
        bar = "█" * round(into / span * 12) + "░" * (12 - round(into / span * 12))
        embed.add_field(name="Level", value=str(level))
        embed.add_field(name="XP", value=f"`{bar}` {into}/{span}")
        embed.add_field(name="Coins", value=f"🪙 {balance}")
        embed.add_field(name="Joined server", value=discord.utils.format_dt(target.joined_at, "R") if target.joined_at else "?")
        embed.add_field(name="Account age", value=discord.utils.format_dt(target.created_at, "R"))
        if badges:
            embed.set_footer(text="  ·  ".join(badges))
        style(embed, interaction, emoji=EMOJI["profile"])
        await interaction.response.send_message(embed=embed)

    @profile_group.command(name="set", description="Customize your card — premium servers only")
    @app_commands.describe(bio="Short tagline shown on your card", color_hex="Accent hex like #FF7A00")
    async def set_custom(self, interaction: discord.Interaction, bio: str | None = None, color_hex: str | None = None) -> None:
        assert interaction.guild is not None
        if not self.is_premium(interaction.guild_id):
            await interaction.response.send_message(
                "Profile customization is a **Plus** feature — this server isn't premium yet.", ephemeral=True
            )
            return
        if color_hex and not color_hex.startswith("#"):
            await interaction.response.send_message("Color must be a hex value like `#FF7A00`.", ephemeral=True)
            return
        async with session_factory() as session:
            guild = await session.get(Guild, interaction.guild_id)
            full = dict(guild.config or {}) if guild else {}
            profiles = dict(full.get("profiles", {}))
            mine = dict(profiles.get(str(interaction.user.id), {}))
            if bio is not None:
                mine["bio"] = bio[:200]
            if color_hex is not None:
                try:
                    discord.Color.from_str(color_hex)
                except ValueError:
                    await interaction.response.send_message("Invalid hex color.", ephemeral=True)
                    return
                mine["color"] = color_hex
            profiles[str(interaction.user.id)] = mine
            if guild is None:
                guild = Guild(id=interaction.guild_id, config={"profiles": profiles})
                session.add(guild)
            else:
                full["profiles"] = profiles
                guild.config = full
            await session.commit()
        await interaction.response.send_message("<:sentra_check:1546486790779572385> Profile updated — check `/profile view`.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PersonaCog(bot))
