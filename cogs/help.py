import discord
from discord import app_commands
from discord.ext import commands

MODULES = {
    "general": ("<:sentra_general:1546486624119029810>", "Ping, stats & user utilities", [
        ("/ping", "WebSocket + API + database timings"),
        ("/about", "Version, stats, team & invite links"),
        ("/serverinfo", "Channels, roles, security & boosts"),
        ("/userinfo @user", "Badges, join dates, roles & perms"),
        ("/avatar [@user]", "Full-size avatar · JPG/PNG/WEBP/GIF buttons"),
        ("/banner [@user] [--of-bot]", "Profile banners — user or bot, GIF-aware"),
        ("/status", "Uptime, memory, loops & AI status (staff)"),
    ]),
    "setup": ("<:sentra_setup:1546504760876736693>", "Channels, roles & onboarding bindings", [
        ("/setup logs #channel", "Route security alerts to your mod-log"),
        ("/setup welcome #channel", "Welcome embed + template placeholders"),
        ("/setup farewell #channel", "Goodbye messages"),
        ("/setup autorole @role", "Role for new members"),
        ("/setup guide", "First-run checklist"),
        ("/setup status", "Current bindings"),
    ]),
    "security": ("<:sentra_anti:1546486994706636882>", "Quarantine, raid mode & lockdown", [
        ("/security setup", "Create quarantine role + channel blocks"),
        ("/security alerts #channel", "Where alerts are posted"),
        ("/security gate True 7", "Minimum account age to join"),
        ("/security raidmode True", "Quarantine all joins during raids"),
        ("/security quarantine/release", "Isolate or restore a member"),
        ("/security lockdown/unlock", "Freeze all public channels safely"),
        ("/security status", "Shield configuration overview"),
    ]),
    "automod": ("<:sentra_automd:1546504630719217674>", "Filters, punishments & escalation ladder", [
        ("/automod status", "Interactive control panel — toggle & tune everything"),
        ("/automod punishment <filter> <action>", "Per-filter punishment — delete, timeout, kick"),
        ("/automod invites True", "Delete server invite links"),
        ("/automod mentions True limit:6", "Mass-mention guard"),
        ("/automod ladder True", "Escalate repeat offenders → timeout → kick"),
    ]),
    "moderation": ("<:sentra_mod:1546500810408460308>", "Warns, mutes, bans, purges & cases", [
        ("/mod warn @user [reason]", "Warn and log a case"),
        ("/mod timeout @user 30m", "Timed mute (up to 28 days)"),
        ("/mod untimeout @user", "Clear a timeout"),
        ("/mod kick/ban @user", "Ban even if left · temp-bans auto-lift"),
        ("/mod massban <ids> reason", "Raid cleanup — up to 25 IDs"),
        ("/mod purge 50 [filters]", "Bots/links/attachments filters"),
        ("/modcase view @user", "Full moderation history"),
    ]),
    "leveling": ("<:sentra_leveling:1546504938509565984>", "XP, ranks, leaderboards & rewards", [
        ("/rank [@user]", "Level card + progress bar"),
        ("/leaderboard", "Top 10 most active members"),
        ("Passive XP", "Chat earns XP · level-ups pay coins · boosters get 1.5×"),
        ("/levels reward-add 10 @role", "Roles unlocked at levels"),
        ("Voice XP", "Time in voice channels counts too"),
    ]),
    "reminders": ("<:sentra_reminder:1546504481494011924>", "Natural-language reminders", [
        ("/remind me 2h30m stretch", "Natural-language durations"),
        ("/remind me 1w standup repeat:1w", "Repeating reminders"),
        ("/remind list / remove id", "Manage yours"),
    ]),
    "utility": ("<:sentra_utils:1546500902352068729>", "Starboard, rolemenus, schedules & embeds", [
        ("/starboard set #ch 5", "⭐ highlights board"),
        ("/rolemenu new #roles Title @r1…", "Self-service role picker"),
        ("/schedule post #ann 1d text", "Recurring announcements"),
        ("/embed …", "Custom embed publisher"),
        ("/insights", "Server activity snapshot"),
    ]),
    "music": ("<:sentra_music:1546500966751150080>", "Full playback, queue & audio filters", [
        ("/play query-or-url", "Songs, playlists & search — auto-joins you"),
        ("/queue · nowplaying", "Paged queue + progress bar with live buttons"),
        ("/skip · /stop · /pause · /resume", "Core playback controls"),
        ("/volume · /seek · /loop · /shuffle", "Fine-tune the session"),
        ("/autoplay · /filters · /search", "Related-track radio · bass boost/nightcore/karaoke"),
        ("/247 · /leave · /dj only True", "Stay connected · disconnect · DJ-only mode"),
    ]),
    "voice": ("<:sentra_j2c:1546501166756667423>", "Join-to-create temp voice channels", [
        ("Auto setup", "Join 🔊 Join to Create → get your own channel"),
        ("/voice setup", "Create/heal the hub + control panel"),
        ("vc-controls panel", "Lock · hide · rename · limit · ban · permit · invite · thread · transfer · claim · delete"),
        ("/voice muteall/moveall/activity", "Run the room"),
        ("/voice focus", "Consent-based listening mode"),
        ("/voice vrole add <VC> @role", "Roles granted while in VC"),
    ]),
    "fun": ("<:sentra_fun:1546504676655243356>", "Coins, duels & classics", [
        ("/fun daily", "Daily trivia · first correct wins coins"),
        ("/fun duel @user 25", "RPS wager duels"),
        ("/fun wyr · 8ball · coinflip · dice", "Classics"),
        ("/fun confess", "Anonymous, staff-reviewed confessions"),
    ]),
    "persona": ("<:sentra_persona:1546501050486366319>", "Profiles", [
        ("/profile view [@user]", "Level, coins, badges, bio"),
        ("/profile set bio/color", "Card customization (Plus)"),
    ]),
    "honeypot": ("<:sentra_honeypot:1546501114516742194>", "Hidden trap channels for bad actors", [
        ("/honeypot create #category", "Create a hidden trap channel"),
        ("/honeypot channel #channel", "Designate an existing channel as trap"),
        ("/honeypot config action:ban log:#mod-log", "What happens on trigger + where to alert"),
        ("/honeypot disable", "Remove the honeypot"),
        ("/honeypot status", "Show trap state & hit count"),
    ]),
}

HOME_COLOR = discord.Color.blurple()


def build_select(active: str | None = None) -> discord.ui.Select:
    return discord.ui.Select(
        placeholder="Browse modules…" if active is None else f"Browsing: {active.title()}",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(
                label=name.title(),
                value=name,
                emoji=emoji,
                description=desc[:100],
                default=(name == active),
            )
            for name, (emoji, desc, _) in MODULES.items()
        ],
        custom_id="sentra:help",
    )


class HelpLayoutView(discord.ui.LayoutView):
    def __init__(self, bot: commands.Bot, active: str | None = None) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        container = discord.ui.Container(accent_color=HOME_COLOR)

        if active is None:
            raw_latency = bot.latency * 1000
            latency = f"`{round(raw_latency)}ms`" if raw_latency == raw_latency else "`—`"
            header = (
                f"# Sentra — Command Center\n"
                f"**Guard first. Entertain always.**\n"
                f"-# <:sentra_arrow:1546505216847913000> Serving **{len(bot.guilds)}** server(s) · {latency}"
            )
            avatar = bot.user.display_avatar.url if bot.user else None
            if avatar:
                container.add_item(
                    discord.ui.Section(
                        discord.ui.TextDisplay(header),
                        accessory=discord.ui.Thumbnail(avatar),
                    )
                )
            else:
                container.add_item(discord.ui.TextDisplay(header))
            container.add_item(discord.ui.Separator())

            directory = "\n".join(
                f"{emoji} **{name.title()}** — *{desc}*"
                for name, (emoji, desc, _) in MODULES.items()
            )
            container.add_item(discord.ui.TextDisplay(directory))
            container.add_item(discord.ui.Separator())

            footer = "-# Safety suite is free forever — premium buys polish, never protection."
            container.add_item(discord.ui.TextDisplay(footer))
        else:
            emoji, desc, entries = MODULES[active]
            header_section = discord.ui.Section(
                discord.ui.TextDisplay(f"## {emoji} {active.title()}\n-# {desc}"),
                accessory=discord.ui.Button(
                    label="Home",
                    emoji="<:sentra_home:1546505364697251871>",
                    style=discord.ButtonStyle.secondary,
                    custom_id="sentra:help-home",
                ),
            )
            container.add_item(header_section)
            container.add_item(discord.ui.Separator())
            body = "\n".join(f"`{cmd}` — {text}" for cmd, text in entries)
            container.add_item(discord.ui.TextDisplay(body[:3800]))

        container.add_item(discord.ui.ActionRow(build_select(active)))
        self.add_item(container)


class HelpCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="Interactive command guide")
    @app_commands.describe(module="Jump straight to a module")
    @app_commands.choices(module=[app_commands.Choice(name=name.title(), value=name) for name in MODULES])
    async def help_command(self, interaction: discord.Interaction, module: app_commands.Choice[str] | None = None) -> None:
        await interaction.response.send_message(
            view=HelpLayoutView(self.bot, module.value if module else None)
        )

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if custom_id == "sentra:help":
            values = (interaction.data or {}).get("values") or []
            key = values[0] if values else None
            if key not in MODULES:
                await interaction.response.defer()
                return
            await interaction.response.edit_message(view=HelpLayoutView(self.bot, key))
        elif custom_id == "sentra:help-home":
            await interaction.response.edit_message(view=HelpLayoutView(self.bot))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(HelpCog(bot))
