import discord
from discord import app_commands
from discord.ext import commands

from sentra.db.engine import session_factory
from sentra.db.models import Guild
from sentra.ui import EMOJI, SETUP, style, TICK

WELCOME_DEFAULTS = {
    "enabled": True,
    "channel_id": None,
    "message": "Welcome {user} to **{server}**! You are member #{count}.",
    "farewell_channel_id": None,
    "farewell_message": "**{username}** has left the server.",
    "autorole_id": None,
}


def render(template: str, member: discord.Member, count: int | None = None) -> str:
    mapping = {
        "{user}": member.mention,
        "{username}": member.display_name,
        "{server}": member.guild.name,
        "{count}": str(count) if count else "?",
    }
    # Single-pass replacement to prevent double-substitution when
    # a member's display name or server name contains placeholder text.
    import re
    pattern = re.compile("|".join(re.escape(k) for k in mapping))
    return pattern.sub(lambda m: mapping[m.group(0)], template)


class SetupCog(commands.Cog):
    setup = app_commands.Group(
        name="setup",
        description="Server onboarding and channel wiring",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def get_config(self, guild_id: int) -> dict:
        async with session_factory() as session:
            guild = await session.get(Guild, guild_id)
            stored = dict((guild.config or {}).get("welcome", {})) if guild else {}
        return {**WELCOME_DEFAULTS, **stored}

    async def update_config(self, guild_id: int, mutate) -> None:
        async with session_factory() as session:
            guild = await session.get(Guild, guild_id)
            if guild is None:
                guild = Guild(id=guild_id)
                session.add(guild)
            full = dict(guild.config or {})
            cfg = {**WELCOME_DEFAULTS, **dict(full.get("welcome", {}))}
            mutate(cfg)
            full["welcome"] = cfg
            guild.config = full
            await session.commit()

    @setup.command(name="welcome", description="Set the welcome channel and message")
    @app_commands.describe(
        channel="Where greetings are posted",
        message="Template — supports {user} {username} {server} {count}",
    )
    async def welcome(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: str | None = None,
    ) -> None:
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.send_messages and perms.embed_links):
            await interaction.response.send_message("I can't post embeds there.", ephemeral=True)
            return

        def mutate(cfg: dict) -> None:
            cfg["enabled"] = True
            cfg["channel_id"] = channel.id
            if message:
                cfg["message"] = message[:1000]

        await self.update_config(interaction.guild_id, mutate)
        preview = render((message or (await self.get_config(interaction.guild_id))["message"]), interaction.user, interaction.guild.member_count)
        embed = discord.Embed(description=preview, color=SETUP)
        embed.set_author(name="Welcome message preview", icon_url=interaction.user.display_avatar.url)
        style(embed, interaction, emoji=EMOJI["setup"])
        await interaction.response.send_message(f"<:sentra_check:1546486790779572385> Welcomes will go to {channel.mention}:", embed=embed, ephemeral=True)

    @setup.command(name="farewell", description="Set the farewell channel and message")
    async def farewell(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message: str | None = None,
    ) -> None:
        def mutate(cfg: dict) -> None:
            cfg["farewell_channel_id"] = channel.id
            if message:
                cfg["farewell_message"] = message[:1000]

        await self.update_config(interaction.guild_id, mutate)
        preview = render(message or "{username} has left the server.", interaction.user)
        await interaction.response.send_message(f"<:sentra_check:1546486790779572385> Farewells will go to {channel.mention}. Preview: {preview}", ephemeral=True)

    @setup.command(name="welcome-off", description="Disable welcome and farewell messages")
    async def welcome_off(self, interaction: discord.Interaction) -> None:
        await self.update_config(interaction.guild_id, lambda cfg: cfg.__setitem__("enabled", False))
        await interaction.response.send_message("Greetings disabled.", ephemeral=True)

    @setup.command(name="autorole", description="Role automatically given to new members")
    @app_commands.describe(role="Leave empty to clear")
    async def autorole(self, interaction: discord.Interaction, role: discord.Role | None = None) -> None:
        if role and (role.managed or role >= interaction.guild.me.top_role):
            await interaction.response.send_message("I can't assign that role.", ephemeral=True)
            return
        await self.update_config(interaction.guild_id, lambda cfg: cfg.__setitem__("autorole_id", role.id if role else None))
        await interaction.response.send_message(f"Autorole: {role.mention if role else '*cleared*'}", ephemeral=True)

    @setup.command(name="logs", description="Wire the mod-log channel (security alerts + staff actions)")
    async def logs(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        perms = channel.permissions_for(interaction.guild.me)
        if not perms.send_messages:
            await interaction.response.send_message("I can't send messages there.", ephemeral=True)
            return
        shield = self.bot.get_cog("ShieldCog")
        if shield:
            await shield.update_config(
                interaction.guild.id, lambda cfg: cfg.__setitem__("alerts_channel_id", channel.id)
            )
        await interaction.response.send_message(f"🛡️ Security alerts and Shield events now route to {channel.mention}.", ephemeral=True)

    @setup.command(name="guide", description="Recommended first-run steps")
    async def guide(self, interaction: discord.Interaction) -> None:
        steps = [
            "1. `/setup logs #mod-log` — routes Shield alerts and security events",
            "2. `/security setup` — creates the quarantine role and blocks",
            "3. `/setup welcome #general` + `/setup autorole @Member` — onboarding",
            "4. `/voice hub add <Join VC>` — join-to-create voice channels",
            "5. `/automod invites True` + `/automod mentions True` — baseline AutoMod",
            "6. `/starboard set #highlights 5` — community highlights",
            "7. `/rolemenu create #roles Title @role1 @role2` — self-roles",
        ]
        embed = discord.Embed(title="🧭 Sentra setup guide", description="\n".join(steps), color=SETUP)
        style(embed, interaction, emoji=EMOJI["setup"])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @setup.command(name="status", description="Show current onboarding configuration")
    async def status(self, interaction: discord.Interaction) -> None:
        cfg = await self.get_config(interaction.guild_id)
        welcome_ch = f"<#{cfg['channel_id']}>" if cfg["channel_id"] else "*not set*"
        farewell_ch = f"<#{cfg['farewell_channel_id']}>" if cfg["farewell_channel_id"] else "*not set*"
        autorole = f"<@&{cfg['autorole_id']}>" if cfg["autorole_id"] else "*not set*"
        embed = discord.Embed(title="🧩 Onboarding status", color=SETUP)
        embed.add_field(name="Welcomes", value=f"{('on' if cfg['enabled'] else 'off')} → {welcome_ch}")
        embed.add_field(name="Farewells", value=farewell_ch)
        embed.add_field(name="Autorole", value=autorole)
        style(embed, interaction, emoji=EMOJI["setup"])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        cfg = await self.get_config(member.guild.id)
        me = member.guild.me
        autorole_id = cfg.get("autorole_id")
        if autorole_id:
            role = member.guild.get_role(autorole_id)
            if role and role < me.top_role and not role.managed:
                try:
                    await member.add_roles(role, reason="Autorole")
                except discord.HTTPException:
                    pass
        if not cfg["enabled"] or not cfg["channel_id"]:
            return
        channel = member.guild.get_channel(cfg["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return
        embed = discord.Embed(
            description=render(cfg["message"], member, member.guild.member_count),
            color=member.color if member.color.value else discord.Color.green(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        style(embed)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        cfg = await self.get_config(member.guild.id)
        if not cfg["enabled"] or not cfg["farewell_channel_id"]:
            return
        channel = member.guild.get_channel(cfg["farewell_channel_id"])
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            await channel.send(render(cfg["farewell_message"], member))
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SetupCog(bot))
