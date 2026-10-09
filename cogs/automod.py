import re
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from sentra.cache import TTLCache
from sentra.db.engine import session_factory
from sentra.db.models import Guild, ModCase
from sentra.ui import AUTOMOD, EMOJI, style, TICK, CROSS, WARN

INVITE_RE = re.compile(
    r"(?:discord\.(?:gg|io|me)|discord(?:app)?\.com/invite)/[A-Za-z0-9_-]+", re.IGNORECASE
)
ZALGO_MARKS_RE = re.compile(r"[\u0300-\u036f]")
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
LINK_RE = re.compile(r"https?://\S+", re.IGNORECASE)

DEFAULTS = {
    "invites_enabled": True,
    "mentions_enabled": True,
    "mention_limit": 6,
    "ladder": True,
    "zalgo_enabled": False,
    "caps_enabled": False,
    "caps_min_len": 12,
    "caps_ratio": 0.70,
    "emoji_spam_enabled": False,
    "emoji_limit": 8,
    "attachments_blocked_exts": [],
    "links_enabled": False,
    "link_allow_list": [],
    "native_managed": True,
    "punishments": {},
}

LADDER_STEPS = {
    1: ("notice", None),
    2: ("timeout", 10),
    3: ("timeout", 30),
}

MANAGED_RULE_NAMES = {
    "invite": "Sentra — Invite Filter",
    "mention": "Sentra — Mention Spam",
    "spam": "Sentra — Spam Shield",
}


TOGGLE_BUTTONS = [
    ("invites_enabled", "Invites", "🔗"),
    ("mentions_enabled", "Mentions", "📣"),
    ("zalgo_enabled", "Zalgo", "🌀"),
    ("caps_enabled", "Caps", "🔠"),
    ("emoji_spam_enabled", "Emoji", "🤬"),
    ("links_enabled", "Links", "🌐"),
    ("ladder", "Ladder", "🪜"),
]

CONFIG_SELECT_ID = "sentra:automod-configure"
PUNISH_SELECT_ID = "sentra:automod-punish-select"
PUNISH_BACK_ID = "sentra:automod-punish-back"

# (config key, friendly label, emoji) — one punishment slot per filter.
PUNISHABLE_KINDS = [
    ("invite", "Invite links", "🔗"),
    ("mention_spam", "Mention spam", "📣"),
    ("zalgo", "Zalgo text", "🌀"),
    ("caps_spam", "Caps spam", "🔠"),
    ("emoji_spam", "Emoji spam", "🤬"),
    ("blocked_attachment", "Blocked files", "📎"),
    ("blocked_link", "Links", "🌐"),
]

PUNISH_ACTION_CHOICES = [
    app_commands.Choice(name="Default — delete and let the ladder escalate", value="default"),
    app_commands.Choice(name="Delete only — never punish further", value="delete"),
    app_commands.Choice(name="Timeout — delete and time the member out", value="timeout"),
    app_commands.Choice(name="Kick — delete and kick the member", value="kick"),
]


def punishment_label(entry: dict | None) -> str:
    """Human-readable label for a stored punishment entry (or None = ladder)."""
    if not isinstance(entry, dict) or "action" not in entry:
        return "ladder (default)"
    action = entry["action"]
    if action == "timeout":
        return f"timeout {entry.get('duration', 10)}m"
    return str(action)


def build_configure_select(config: dict) -> discord.ui.Select:
    """Select menu used to fine-tune numeric / list settings from the panel."""
    options = [
        discord.SelectOption(
            label="Mention limit",
            value="mentions",
            description=f"Now {config['mention_limit']} pings per message",
            emoji="📣",
        ),
        discord.SelectOption(
            label="Caps filter tuning",
            value="caps",
            description=f"Now {config['caps_min_len']}+ chars · {config['caps_ratio']:.0%} caps",
            emoji="🔠",
        ),
        discord.SelectOption(
            label="Emoji limit",
            value="emoji",
            description=f"Now {config['emoji_limit']} per message",
            emoji="🤬",
        ),
        discord.SelectOption(
            label="Blocked file types",
            value="attachments",
            description=", ".join(config["attachments_blocked_exts"]) or "none blocked",
            emoji="📎",
        ),
        discord.SelectOption(
            label="Link allow-list",
            value="links",
            description=", ".join(config["link_allow_list"]) or "all links blocked",
            emoji="🌐",
        ),
        discord.SelectOption(
            label="Punishments",
            value="punishments",
            description="Per-filter actions: delete, timeout or kick",
            emoji="⚖️",
        ),
    ]
    return discord.ui.Select(
        placeholder="Fine-tune a filter…",
        options=options,
        custom_id=CONFIG_SELECT_ID,
    )


def status_body(config: dict) -> str:
    """Markdown body for the panel / plain status, driven straight from config."""
    def mark(key: str) -> str:
        return "<:sentra_check:1546486790779572385>" if config.get(key) else "<:sentra_cross:1546486840822075512>"

    lines = [
        f"🔗 **Invite filter** — {mark('invites_enabled')}",
        f"📣 **Mention guard** — {mark('mentions_enabled')} · limit **{config['mention_limit']}**",
        f"🪜 **Escalation ladder** — {mark('ladder')} · warn → 10m → 30m → kick",
        f"🌀 **Zalgo filter** — {mark('zalgo_enabled')}",
        f"🔠 **Caps filter** — {mark('caps_enabled')} · {config['caps_min_len']}+ chars, {config['caps_ratio']:.0%} caps",
        f"🤬 **Emoji spam** — {mark('emoji_spam_enabled')} · limit **{config['emoji_limit']}**",
        f"🌐 **Link filter** — {mark('links_enabled')} · allow `{', '.join(config['link_allow_list']) or '—'}`",
        f"📎 **Blocked files** — `{', '.join(config['attachments_blocked_exts']) or 'none'}`",
        f"🛰️ **Native Discord rules** — {'<:sentra_check:1546486790779572385> managed' if config['native_managed'] else '<:sentra_cross:1546486840822075512> off'}",
    ]
    punishments = config.get("punishments") or {}
    if punishments:
        summary = " · ".join(
            f"{kind.replace('_', ' ')} → {punishment_label(entry)}"
            for kind, entry in punishments.items()
        )
        lines.append(f"⚖️ **Custom punishments** — {summary}")
    else:
        lines.append("⚖️ **Custom punishments** — none (escalation ladder)")
    return "\n".join(lines)


def count_combining_marks(text: str) -> int:
    return len(ZALGO_MARKS_RE.findall(text))


def is_caps_spam(text: str, min_len: int, ratio: float) -> bool:
    stripped = text.strip()
    if len(stripped) < min_len:
        return False
    letters = [c for c in stripped if c.isalpha()]
    if len(letters) < min_len:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return (upper / len(letters)) >= ratio


def count_emojis(text: str) -> int:
    custom = len(CUSTOM_EMOJI_RE.findall(text))
    unicode_count = 0
    for char in text:
        cp = ord(char)
        if (
            0x1F300 <= cp <= 0x1F6FF
            or 0x1F900 <= cp <= 0x1F9FF
            or 0x2600 <= cp <= 0x26FF
            or 0x2700 <= cp <= 0x27BF
            or 0x1FA70 <= cp <= 0x1FAFF
        ):
            unicode_count += 1
    return custom + unicode_count


def blocked_attachment(message: discord.Message, blocked_exts: list[str]) -> str | None:
    if not blocked_exts or not message.attachments:
        return None
    lowered = {e.lower().lstrip(".") for e in blocked_exts}
    for attachment in message.attachments:
        name = (attachment.filename or "").lower()
        ext = name.rsplit(".", 1)[-1] if "." in name else ""
        if ext in lowered:
            return f".{ext}"
    return None


class AutoModPanel(discord.ui.LayoutView):
    """Components-V2 control center for AutoMod.

    Persistent (``timeout=None``) custom-id components routed through
    ``AutoModCog.on_interaction`` — buttons keep working after restarts.
    """

    def __init__(self, cog: "AutoModCog", guild_id: int, config: dict) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id

        container = discord.ui.Container(accent_color=AUTOMOD)
        header = (
            "# 🤖 AutoMod Control Center\n"
            "-# Flip filters with the buttons · fine-tune with the menu below.\n"
            "-# Changes apply instantly and sync to Discord's native AutoMod."
        )
        bot_user = cog.bot.user
        if bot_user is not None:
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(header),
                    accessory=discord.ui.Thumbnail(bot_user.display_avatar.url),
                )
            )
        else:
            container.add_item(discord.ui.TextDisplay(header))
        container.add_item(discord.ui.Separator())
        container.add_item(discord.ui.TextDisplay(status_body(config)))
        container.add_item(discord.ui.Separator())

        def toggle_button(key: str, label: str, emoji: str) -> discord.ui.Button:
            return discord.ui.Button(
                label=label,
                emoji=emoji,
                style=(
                    discord.ButtonStyle.success if config.get(key) else discord.ButtonStyle.secondary
                ),
                custom_id=f"sentra:automod-toggle:{key}",
            )

        container.add_item(
            discord.ui.ActionRow(*(toggle_button(*t) for t in TOGGLE_BUTTONS[:5]))
        )

        second_row = [toggle_button(*t) for t in TOGGLE_BUTTONS[5:]]
        second_row.append(
            discord.ui.Button(
                label="Native sync",
                emoji="🛰️",
                style=(
                    discord.ButtonStyle.success
                    if config.get("native_managed")
                    else discord.ButtonStyle.secondary
                ),
                custom_id="sentra:automod-sync",
            )
        )
        second_row.append(
            discord.ui.Button(
                label="Reset all",
                emoji="♻️",
                style=discord.ButtonStyle.danger,
                custom_id="sentra:automod-reset",
            )
        )
        container.add_item(discord.ui.ActionRow(*second_row))
        container.add_item(discord.ui.ActionRow(build_configure_select(config)))
        self.add_item(container)


async def build_panel(cog: "AutoModCog", guild_id: int) -> AutoModPanel:
    """Fetch fresh config and rebuild the panel view."""
    config = await cog.get_config(guild_id)
    return AutoModPanel(cog, guild_id, config)


class PunishmentPanel(discord.ui.LayoutView):
    """Components-V2 sub-panel for editing per-filter punishments."""

    def __init__(self, cog: "AutoModCog", guild_id: int, config: dict) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id

        container = discord.ui.Container(accent_color=AUTOMOD)
        container.add_item(
            discord.ui.TextDisplay(
                "# ⚖️ AutoMod Punishments\n"
                "-# Pick a filter below, then choose `delete`, `timeout`, `kick` or `default`.\n"
                "-# `default` = delete the message and let the escalation ladder handle repeats."
            )
        )
        container.add_item(discord.ui.Separator())
        punishments = config.get("punishments") or {}
        container.add_item(
            discord.ui.TextDisplay(
                "\n".join(
                    f"{emoji} **{label}** — {punishment_label(punishments.get(kind))}"
                    for kind, label, emoji in PUNISHABLE_KINDS
                )
            )
        )
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.ActionRow(
                discord.ui.Select(
                    placeholder="Pick a filter to change its punishment…",
                    options=[
                        discord.SelectOption(
                            label=label,
                            value=kind,
                            description=f"Currently: {punishment_label(punishments.get(kind))}"[:100],
                            emoji=emoji,
                        )
                        for kind, label, emoji in PUNISHABLE_KINDS
                    ],
                    custom_id=PUNISH_SELECT_ID,
                )
            )
        )
        container.add_item(
            discord.ui.ActionRow(
                discord.ui.Button(
                    label="Back to panel",
                    emoji="🔙",
                    style=discord.ButtonStyle.secondary,
                    custom_id=PUNISH_BACK_ID,
                )
            )
        )
        self.add_item(container)


class AutoModCog(commands.Cog):
    automod = app_commands.Group(
        name="automod",
        description="Configure automatic moderation",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )
    native = app_commands.Group(
        name="native",
        parent=automod,
        description="Manage real Discord AutoMod rules",
        default_permissions=discord.Permissions(manage_guild=True),
    )
    punishment = app_commands.Group(
        name="punishment",
        parent=automod,
        description="Choose the punishment for each automod filter",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.config_cache = TTLCache(ttl=15)
        self.offenses = TTLCache(ttl=600)

    async def get_config(self, guild_id: int) -> dict:
        cached = self.config_cache.get(str(guild_id))
        if cached is not None:
            return cached
        async with session_factory() as session:
            guild = await session.get(Guild, guild_id)
            if guild is None:
                guild = Guild(id=guild_id)
                session.add(guild)
                await session.commit()
            stored = dict((guild.config or {}).get("automod", {}))
        config = {**DEFAULTS, **stored}
        if not isinstance(config.get("attachments_blocked_exts"), list):
            config["attachments_blocked_exts"] = []
        if not isinstance(config.get("link_allow_list"), list):
            config["link_allow_list"] = []
        if not isinstance(config.get("punishments"), dict):
            config["punishments"] = {}
        self.config_cache.set(str(guild_id), config)
        return config

    async def set_config(self, guild_id: int, key: str, value) -> None:
        async with session_factory() as session:
            guild = await session.get(Guild, guild_id)
            if guild is None:
                guild = Guild(id=guild_id)
                session.add(guild)
            config = dict(guild.config or {})
            am = dict(config.get("automod", {}))
            am[key] = value
            config["automod"] = am
            guild.config = config
            await session.commit()
        self.config_cache.pop(str(guild_id))

    async def alert_channel_id(self, guild_id: int) -> int | None:
        shield = self.bot.get_cog("ShieldCog")
        if shield:
            cfg = await shield.get_config(guild_id)
            return cfg.get("alerts_channel_id")
        return None

    @automod.command(name="invites", description="Toggle the server-invite link filter")
    async def invites(self, interaction: discord.Interaction, enabled: bool) -> None:
        assert interaction.guild is not None
        await self.set_config(interaction.guild.id, "invites_enabled", enabled)
        await interaction.response.send_message(f"Invite filter is now **{'on' if enabled else 'off'}**.", ephemeral=True)
        await self._resync_native(interaction)

    @automod.command(name="mentions", description="Toggle mass-mention protection")
    @app_commands.describe(enabled="Turn protection on or off", limit="Mentions allowed per message (3-20)")
    async def mentions(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        limit: app_commands.Range[int, 3, 20] = 6,
    ) -> None:
        assert interaction.guild is not None
        await self.set_config(interaction.guild.id, "mentions_enabled", enabled)
        await self.set_config(interaction.guild.id, "mention_limit", limit)
        state = "on" if enabled else "off"
        await interaction.response.send_message(
            f"Mention-spam guard is now **{state}** (limit: {limit} mentions).", ephemeral=True
        )
        await self._resync_native(interaction)

    @automod.command(name="ladder", description="Escalating punishments for repeat offenders")
    @app_commands.describe(enabled="Warn → timeout → longer timeout → kick on repeat violations")
    async def ladder(self, interaction: discord.Interaction, enabled: bool) -> None:
        assert interaction.guild is not None
        await self.set_config(interaction.guild.id, "ladder", enabled)
        state = (
            "**on** — 1st violation warns, 2nd times out 10m, 3rd times out 30m, 4th+ kicks."
            if enabled
            else "**off** — violations are deleted but never punished automatically."
        )
        await interaction.response.send_message(f"Escalation ladder {state}", ephemeral=True)

    @automod.command(name="zalgo", description="Block zalgo / corrupted-text spam")
    async def zalgo(self, interaction: discord.Interaction, enabled: bool) -> None:
        assert interaction.guild is not None
        await self.set_config(interaction.guild.id, "zalgo_enabled", enabled)
        await interaction.response.send_message(f"Zalgo filter is now **{'on' if enabled else 'off'}**.", ephemeral=True)

    @automod.command(name="caps", description="Block excessive caps spam")
    @app_commands.describe(enabled="Enable caps filter", min_len="Minimum message length to check", ratio="Ratio of caps 0.5-0.95")
    async def caps(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        min_len: app_commands.Range[int, 6, 100] = 12,
        ratio: float = 0.70,
    ) -> None:
        assert interaction.guild is not None
        ratio = max(0.5, min(0.95, ratio))
        await self.set_config(interaction.guild.id, "caps_enabled", enabled)
        await self.set_config(interaction.guild.id, "caps_min_len", min_len)
        await self.set_config(interaction.guild.id, "caps_ratio", ratio)
        state = "on" if enabled else "off"
        await interaction.response.send_message(f"Caps filter **{state}** (min {min_len} chars, {ratio:.0%} caps).", ephemeral=True)

    @automod.command(name="emoji-spam", description="Block mass emoji spam")
    @app_commands.describe(enabled="Enable emoji spam filter", limit="Max emojis allowed per message")
    async def emoji_spam(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        limit: app_commands.Range[int, 4, 30] = 8,
    ) -> None:
        assert interaction.guild is not None
        await self.set_config(interaction.guild.id, "emoji_spam_enabled", enabled)
        await self.set_config(interaction.guild.id, "emoji_limit", limit)
        await interaction.response.send_message(f"Emoji-spam filter **{'on' if enabled else 'off'}** (limit {limit}).", ephemeral=True)

    @automod.command(name="attachments", description="Block file types by extension")
    @app_commands.describe(extensions="Comma-separated extensions, e.g. .exe,.scr — leave empty to allow all")
    async def attachments(self, interaction: discord.Interaction, extensions: str = "") -> None:
        assert interaction.guild is not None
        if not extensions.strip():
            blocked: list[str] = []
        else:
            blocked = [e.strip().lower().lstrip(".") for e in extensions.replace(",", " ").split() if e.strip()]
            blocked = [f".{e}" for e in blocked][:20]
        await self.set_config(interaction.guild.id, "attachments_blocked_exts", blocked)
        if blocked:
            await interaction.response.send_message(f"Blocked attachments: `{' '.join(blocked)}`", ephemeral=True)
        else:
            await interaction.response.send_message("Attachment filter cleared — all file types allowed.", ephemeral=True)

    @automod.command(name="links", description="Block links except an allow-list")
    @app_commands.describe(enabled="Block links not on the allow-list", allow_list="Comma-separated domains to allow, e.g. youtube.com,github.com")
    async def links(self, interaction: discord.Interaction, enabled: bool, allow_list: str = "") -> None:
        assert interaction.guild is not None
        allowed = [d.strip().lower() for d in allow_list.replace(",", " ").split() if d.strip()][:20]
        await self.set_config(interaction.guild.id, "links_enabled", enabled)
        await self.set_config(interaction.guild.id, "link_allow_list", allowed)
        if enabled:
            note = f" Allow-list: `{' '.join(allowed)}`" if allowed else " (all links blocked)"
            await interaction.response.send_message(f"Link filter **on**.{note}", ephemeral=True)
        else:
            await interaction.response.send_message("Link filter **off**.", ephemeral=True)

    async def _set_punishment(
        self,
        interaction: discord.Interaction,
        kind: str,
        label: str,
        action: str,
        duration: int,
    ) -> None:
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        minutes = max(1, min(40320, int(duration or 10)))
        config = await self.get_config(guild_id)
        punishments = dict(config.get("punishments") or {})
        if action == "default":
            punishments.pop(kind, None)
            note = f"{label} punishment reset to the **escalation ladder**."
        else:
            entry: dict = {"action": action}
            if action == "timeout":
                entry["duration"] = minutes
                note = f"{label} punishment set to **timeout {minutes}m**."
            elif action == "kick":
                note = f"{label} punishment set to **kick**."
            else:
                note = f"{label} punishment set to **delete only** (no escalation)."
            punishments[kind] = entry
        await self.set_config(guild_id, "punishments", punishments)
        await interaction.response.send_message(f"⚖️ {note}", ephemeral=True)

    @punishment.command(name="invites", description="Punishment for posting server invite links")
    @app_commands.choices(action=PUNISH_ACTION_CHOICES)
    @app_commands.describe(action="What happens when someone posts an invite", duration="Timeout minutes (timeout action only)")
    async def punishment_invites(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        duration: app_commands.Range[int, 1, 40320] = 10,
    ) -> None:
        await self._set_punishment(interaction, "invite", "Invite links", action.value, duration)

    @punishment.command(name="mentions", description="Punishment for mass-pinging members")
    @app_commands.choices(action=PUNISH_ACTION_CHOICES)
    @app_commands.describe(action="What happens when someone mass-pings", duration="Timeout minutes (timeout action only)")
    async def punishment_mentions(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        duration: app_commands.Range[int, 1, 40320] = 10,
    ) -> None:
        await self._set_punishment(interaction, "mention_spam", "Mention spam", action.value, duration)

    @punishment.command(name="zalgo", description="Punishment for zalgo / corrupted text")
    @app_commands.choices(action=PUNISH_ACTION_CHOICES)
    @app_commands.describe(action="What happens when someone posts zalgo text", duration="Timeout minutes (timeout action only)")
    async def punishment_zalgo(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        duration: app_commands.Range[int, 1, 40320] = 10,
    ) -> None:
        await self._set_punishment(interaction, "zalgo", "Zalgo text", action.value, duration)

    @punishment.command(name="caps", description="Punishment for caps spam")
    @app_commands.choices(action=PUNISH_ACTION_CHOICES)
    @app_commands.describe(action="What happens when someone spams caps", duration="Timeout minutes (timeout action only)")
    async def punishment_caps(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        duration: app_commands.Range[int, 1, 40320] = 10,
    ) -> None:
        await self._set_punishment(interaction, "caps_spam", "Caps spam", action.value, duration)

    @punishment.command(name="emoji", description="Punishment for emoji spam")
    @app_commands.choices(action=PUNISH_ACTION_CHOICES)
    @app_commands.describe(action="What happens when someone spams emojis", duration="Timeout minutes (timeout action only)")
    async def punishment_emoji(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        duration: app_commands.Range[int, 1, 40320] = 10,
    ) -> None:
        await self._set_punishment(interaction, "emoji_spam", "Emoji spam", action.value, duration)

    @punishment.command(name="attachments", description="Punishment for posting blocked file types")
    @app_commands.choices(action=PUNISH_ACTION_CHOICES)
    @app_commands.describe(action="What happens when someone posts a blocked file", duration="Timeout minutes (timeout action only)")
    async def punishment_attachments(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        duration: app_commands.Range[int, 1, 40320] = 10,
    ) -> None:
        await self._set_punishment(interaction, "blocked_attachment", "Blocked files", action.value, duration)

    @punishment.command(name="links", description="Punishment for posting blocked links")
    @app_commands.choices(action=PUNISH_ACTION_CHOICES)
    @app_commands.describe(action="What happens when someone posts a blocked link", duration="Timeout minutes (timeout action only)")
    async def punishment_links(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        duration: app_commands.Range[int, 1, 40320] = 10,
    ) -> None:
        await self._set_punishment(interaction, "blocked_link", "Links", action.value, duration)

    @punishment.command(name="show", description="Show the punishment for every automod filter")
    async def punishment_show(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        config = await self.get_config(interaction.guild.id)
        punishments = config.get("punishments") or {}
        lines = [
            f"{emoji} **{label}** — {punishment_label(punishments.get(kind))}"
            for kind, label, emoji in PUNISHABLE_KINDS
        ]
        embed = discord.Embed(title="⚖️ AutoMod punishments", description="\n".join(lines), color=AUTOMOD)
        await interaction.response.send_message(embed=style(embed, interaction, emoji=EMOJI["automod"]), ephemeral=True)

    @automod.command(name="status", description="Open the interactive AutoMod control panel")
    async def status(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        config = await self.get_config(interaction.guild.id)
        await interaction.response.send_message(
            view=AutoModPanel(self, interaction.guild.id, config), ephemeral=True
        )

    @native.command(name="list", description="List this server's real Discord AutoMod rules")
    async def native_list(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        try:
            rules = await interaction.guild.fetch_automod_rules()
        except discord.Forbidden:
            await interaction.followup.send("I need **Manage Guild** to view AutoMod rules.")
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(f"Couldn't fetch rules: `{exc}`")
            return
        if not rules:
            await interaction.followup.send("No native AutoMod rules exist. Use `/automod native sync` to create Sentra's managed ones.")
            return
        lines = []
        for rule in rules:
            source = " · 🤖 Sentra" if rule.name in MANAGED_RULE_NAMES.values() else ""
            lines.append(
                f"`{rule.id}` **{rule.name}** — {rule.trigger.type.name} · {'enabled' if rule.enabled else 'disabled'}{source}"
            )
        embed = discord.Embed(title="Native AutoMod rules", description="\n".join(lines)[:4000], color=AUTOMOD)
        await interaction.followup.send(embed=style(embed, interaction, emoji=EMOJI["automod"]))

    async def _sync_native_rules(
        self, guild: discord.Guild
    ) -> tuple[list[str], list[str], list[str]]:
        """Create or update Sentra's managed native AutoMod rules.

        Returns (created, updated, skipped) rule-name strings. Any failure is
        best-effort — the bot-side content filter still enforces toggles on its own.
        """
        config = await self.get_config(guild.id)
        alert_id = await self.alert_channel_id(guild.id)
        actions_block = [discord.AutoModRuleAction(type=discord.AutoModRuleActionType.block_message)]
        if alert_id:
            actions_block.append(
                discord.AutoModRuleAction(
                    type=discord.AutoModRuleActionType.send_alert_message, channel_id=alert_id
                )
            )

        try:
            existing = {r.name: r for r in await guild.fetch_automod_rules()}
        except discord.Forbidden:
            return [], [], ["permissions"]
        except discord.HTTPException:
            return [], [], ["fetch_failed"]

        invite_trigger = discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType.keyword,
            keyword_filter=["discord.gg/", "discord.com/invite", "discord.me/", "discordapp.com/invite"],
        )
        mention_trigger = discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType.mention_spam,
            mention_limit=config["mention_limit"] if config["mentions_enabled"] else 7,
            mention_raid_protection=True,
        )
        spam_trigger = discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.spam)

        mention_actions = list(actions_block) + [
            discord.AutoModRuleAction(
                type=discord.AutoModRuleActionType.timeout, duration=timedelta(minutes=10)
            )
        ]

        desired = [
            (
                MANAGED_RULE_NAMES["invite"],
                discord.AutoModRuleEventType.message_send,
                invite_trigger,
                actions_block,
                bool(config["invites_enabled"] and config["native_managed"]),
            ),
            (
                MANAGED_RULE_NAMES["mention"],
                discord.AutoModRuleEventType.message_send,
                mention_trigger,
                mention_actions,
                bool(config["mentions_enabled"] and config["native_managed"]),
            ),
            (
                MANAGED_RULE_NAMES["spam"],
                discord.AutoModRuleEventType.message_send,
                spam_trigger,
                actions_block,
                bool(config["native_managed"]),
            ),
        ]

        created, updated, skipped = [], [], []
        for name, event_type, trigger, actions, enabled in desired:
            rule = existing.get(name)
            try:
                if rule:
                    await rule.edit(
                        event_type=event_type,
                        trigger=trigger,
                        actions=actions,
                        enabled=enabled,
                        reason="Sentra AutoMod sync",
                    )
                    updated.append(f"{name} → {'enabled' if enabled else 'disabled'}")
                else:
                    await guild.create_automod_rule(
                        name=name,
                        event_type=event_type,
                        trigger=trigger,
                        actions=actions,
                        enabled=enabled,
                        reason="Sentra AutoMod sync",
                    )
                    created.append(f"{name} → {'enabled' if enabled else 'disabled'}")
            except discord.HTTPException as exc:
                skipped.append(f"{name}: {getattr(exc, 'text', str(exc))[:100]}")
        return created, updated, skipped

    async def _resync_native(self, interaction: discord.Interaction) -> None:
        """Best-effort re-sync of native rules after a content toggle."""
        assert interaction.guild is not None
        try:
            await self._sync_native_rules(interaction.guild)
        except Exception:
            pass

    @native.command(name="sync", description="Create or update Sentra's native AutoMod rules (real Discord rules)")
    async def native_sync(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        await self.set_config(interaction.guild.id, "native_managed", True)
        created, updated, skipped = await self._sync_native_rules(interaction.guild)
        parts = []
        if created:
            parts.append("<:sentra_check:1546486790779572385> Created:\n" + "\n".join(created))
        if updated:
            parts.append("🔄 Updated:\n" + "\n".join(updated))
        if skipped:
            parts.append("<:sentra_warn:1546486928096890951> Skipped:\n" + "\n".join(skipped))
        await interaction.followup.send("\n\n".join(parts) or "Nothing to do — rules already in sync.")

    @native.command(name="delete", description="Delete a native AutoMod rule by ID")
    @app_commands.describe(rule_id="The rule ID from /automod native list")
    async def native_delete(self, interaction: discord.Interaction, rule_id: str) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        try:
            rule = await interaction.guild.fetch_automod_rule(int(rule_id))
            await rule.delete(reason=f"Deleted by {interaction.user}")
            await interaction.followup.send(f"🗑️ Deleted **{rule.name}**.")
        except (ValueError, discord.NotFound):
            await interaction.followup.send("Rule not found. Check the ID from `/automod native list`.")
        except discord.Forbidden:
            await interaction.followup.send("I need **Manage Guild**.")
        except discord.HTTPException as exc:
            await interaction.followup.send(f"Delete failed: `{exc}`")

    @native.command(name="toggle", description="Enable or disable a native AutoMod rule")
    @app_commands.describe(rule_id="The rule ID", enabled="On or off")
    async def native_toggle(self, interaction: discord.Interaction, rule_id: str, enabled: bool) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        try:
            rule = await interaction.guild.fetch_automod_rule(int(rule_id))
            await rule.edit(enabled=enabled, reason=f"Toggled by {interaction.user}")
            await interaction.followup.send(f"**{rule.name}** is now **{'enabled' if enabled else 'disabled'}**.")
        except (ValueError, discord.NotFound):
            await interaction.followup.send("Rule not found.")
        except discord.Forbidden:
            await interaction.followup.send("I need **Manage Guild**.")
        except discord.HTTPException as exc:
            await interaction.followup.send(f"Toggle failed: `{exc}`")

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        """Route persistent AutoMod panel components (Components V2)."""
        if interaction.type != discord.InteractionType.component:
            return
        data = interaction.data or {}
        custom_id = data.get("custom_id", "")
        if not custom_id.startswith("sentra:automod"):
            return
        if interaction.guild is None:
            return
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.guild_permissions.manage_guild:
            await interaction.response.send_message(
                "🔒 You need **Manage Server** to use the AutoMod panel.", ephemeral=True
            )
            return
        guild_id = interaction.guild.id

        if custom_id.startswith("sentra:automod-toggle:"):
            key = custom_id.removeprefix("sentra:automod-toggle:")
            if key not in DEFAULTS or not isinstance(DEFAULTS[key], bool):
                await interaction.response.defer()
                return
            config = await self.get_config(guild_id)
            await self.set_config(guild_id, key, not config[key])
            if key in ("invites_enabled", "mentions_enabled"):
                await self._resync_native(interaction)
            await interaction.response.edit_message(view=await build_panel(self, guild_id))
        elif custom_id == "sentra:automod-sync":
            await interaction.response.defer()
            await self.set_config(guild_id, "native_managed", True)
            created, updated, skipped = await self._sync_native_rules(interaction.guild)
            parts = []
            if created:
                parts.append("<:sentra_check:1546486790779572385> Created:\n" + "\n".join(created))
            if updated:
                parts.append("🔄 Updated:\n" + "\n".join(updated))
            if skipped:
                parts.append("<:sentra_warn:1546486928096890951> Skipped:\n" + "\n".join(skipped))
            await interaction.edit_original_response(view=await build_panel(self, guild_id))
            await interaction.followup.send(
                "\n\n".join(parts) or "Nothing to do — rules already in sync.", ephemeral=True
            )
        elif custom_id == "sentra:automod-reset":
            for key, value in DEFAULTS.items():
                await self.set_config(guild_id, key, value)
            await self._resync_native(interaction)
            await interaction.response.edit_message(view=await build_panel(self, guild_id))
            await interaction.followup.send("♻️ AutoMod settings restored to defaults.", ephemeral=True)
        elif custom_id == CONFIG_SELECT_ID:
            values = data.get("values") or []
            choice = values[0] if values else None
            config = await self.get_config(guild_id)
            if choice == "punishments":
                await interaction.response.send_message(
                    view=PunishmentPanel(self, guild_id, config), ephemeral=True
                )
                return
            factories = {
                "mentions": lambda: MentionModal(self, guild_id, config),
                "caps": lambda: CapsModal(self, guild_id, config),
                "emoji": lambda: EmojiModal(self, guild_id, config),
                "attachments": lambda: AttachmentsModal(self, guild_id, config),
                "links": lambda: LinksModal(self, guild_id, config),
            }
            factory = factories.get(choice)
            if factory is None:
                await interaction.response.defer()
            else:
                await interaction.response.send_modal(factory())
        elif custom_id == PUNISH_SELECT_ID:
            values = data.get("values") or []
            kind = values[0] if values else None
            valid_kinds = {k for k, _, _ in PUNISHABLE_KINDS}
            if kind not in valid_kinds:
                await interaction.response.defer()
                return
            config = await self.get_config(guild_id)
            label = next(label for k, label, _ in PUNISHABLE_KINDS if k == kind)
            await interaction.response.send_modal(PunishmentModal(self, guild_id, kind, label, config))
        elif custom_id == PUNISH_BACK_ID:
            await interaction.response.edit_message(view=await build_panel(self, guild_id))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self.check_message(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.content != after.content:
            await self.check_message(after)

    async def check_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        me = message.guild.me
        if not me.guild_permissions.manage_messages:
            return
        config = await self.get_config(message.guild.id)
        kind: str | None = None
        detail: str | None = None

        if config["invites_enabled"] and message.content and INVITE_RE.search(message.content):
            kind = "invite"
        elif config["zalgo_enabled"] and message.content and count_combining_marks(message.content) >= 12:
            kind = "zalgo"
        elif config["caps_enabled"] and message.content and is_caps_spam(
            message.content, config["caps_min_len"], config["caps_ratio"]
        ):
            kind = "caps_spam"
        elif config["emoji_spam_enabled"] and message.content and count_emojis(message.content) >= config["emoji_limit"]:
            kind = "emoji_spam"
        elif (blocked := blocked_attachment(message, config["attachments_blocked_exts"])):
            kind = "blocked_attachment"
            detail = blocked
        elif config["links_enabled"] and message.content and LINK_RE.search(message.content):
            allow = [d.lower() for d in config["link_allow_list"]]
            if allow:
                urls = LINK_RE.findall(message.content)
                allowed_hit = False
                for url in urls:
                    lower = url.lower()
                    if any(domain in lower for domain in allow):
                        allowed_hit = True
                        break
                if not allowed_hit:
                    kind = "blocked_link"
            else:
                kind = "blocked_link"
        elif (
            config["mentions_enabled"]
            and len(message.mentions) >= config["mention_limit"]
            and isinstance(message.author, discord.Member)
        ):
            kind = "mention_spam"

        if kind is None:
            return
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        await self.handle_violation(message, kind, config, detail)

    async def handle_violation(
        self, message: discord.Message, kind: str, config: dict, detail: str | None = None
    ) -> None:
        guild = message.guild
        member = message.author
        me = guild.me
        key = f"esc:{guild.id}:{member.id}"
        count = (self.offenses.get(key) or 0) + 1
        self.offenses.set(key, count)

        punishment = "deleted"
        custom = (config.get("punishments") or {}).get(kind)
        if isinstance(custom, dict) and custom.get("action") in ("delete", "timeout", "kick"):
            action = custom["action"]
            if action == "timeout":
                minutes = max(1, int(custom.get("duration") or 10))
                if me.guild_permissions.moderate_members:
                    punishment = f"timeout {minutes}m"
                    await self.apply_timeout(member, minutes, kind, count)
                else:
                    punishment = "timeout skipped (missing permission)"
            elif action == "kick":
                if me.guild_permissions.kick_members and isinstance(member, discord.Member):
                    try:
                        await member.kick(reason=f"AutoMod: {kind} violation")
                        punishment = "kicked"
                    except discord.HTTPException:
                        punishment = await self._fallback_timeout(member, kind, count) or "attempted"
                else:
                    punishment = await self._fallback_timeout(member, kind, count) or "skipped"
        elif config.get("ladder"):
            if count in LADDER_STEPS:
                step_name, duration = LADDER_STEPS[count]
                if step_name == "timeout" and me.guild_permissions.moderate_members:
                    punishment = f"timeout {duration}m"
                    await self.apply_timeout(member, duration, kind, count)
            elif count >= 4:
                if me.guild_permissions.kick_members and isinstance(member, discord.Member):
                    try:
                        await member.kick(reason=f"AutoMod ladder: repeat {kind} ({count})")
                        punishment = "kicked"
                    except discord.HTTPException:
                        punishment = await self._fallback_timeout(member, kind, count) or "attempted"
                else:
                    punishment = await self._fallback_timeout(member, kind, count) or "skipped"
        elif kind == "mention_spam" and me.guild_permissions.moderate_members:
            punishment = "timeout 10m"
            await self.apply_timeout(member, 10, kind, count)

        reason = f"x{count} · {punishment}" + (f" · {detail}" if detail else "")
        await self.log_case(guild.id, member.id, f"automod_{kind}", reason)
        detail_note = f" (strike {count}: {punishment})" if punishment != "deleted" else ""
        kind_labels = {
            "invite": "invite links",
            "zalgo": "zalgo text",
            "caps_spam": "caps spam",
            "emoji_spam": "emoji spam",
            "blocked_attachment": f"blocked file type `{detail or ''}`",
            "blocked_link": "links",
            "mention_spam": "mass-pinging",
        }
        label = kind_labels.get(kind, kind)
        await self.notice(
            message.channel,
            f"{member.mention}, {label} are not allowed here.{detail_note}",
        )

    async def _fallback_timeout(self, member, kind, count) -> str | None:
        if member.guild.me.guild_permissions.moderate_members:
            await self.apply_timeout(member, 60, kind, count)
            return "timeout 60m"
        return None

    async def apply_timeout(self, member: discord.Member, minutes: int, kind: str, count: int) -> None:
        until = discord.utils.utcnow() + timedelta(minutes=minutes)
        try:
            await member.timeout(until, reason=f"AutoMod ladder: {kind} strike {count}")
        except discord.HTTPException:
            pass

    async def log_case(self, guild_id: int, user_id: int, action: str, reason: str) -> None:
        bot_id = self.bot.user.id if self.bot.user else 0
        async with session_factory() as session:
            session.add(
                ModCase(
                    guild_id=guild_id,
                    user_id=user_id,
                    moderator_id=bot_id,
                    action=action,
                    reason=reason[:1000],
                )
            )
            await session.commit()

    async def notice(self, channel: object, text: str) -> None:
        try:
            await channel.send(text, delete_after=8, allowed_mentions=discord.AllowedMentions(users=False))
        except discord.HTTPException:
            pass


class _AutoModConfigModal(discord.ui.Modal):
    """Base for panel fine-tuning modals — saves, rebuilds the panel, confirms."""

    def __init__(self, cog: "AutoModCog", guild_id: int, title: str) -> None:
        super().__init__(title=title, timeout=300)
        self.cog = cog
        self.guild_id = guild_id

    async def save_and_refresh(self, interaction: discord.Interaction, message: str) -> None:
        await interaction.response.edit_message(view=await build_panel(self.cog, self.guild_id))
        await interaction.followup.send(message, ephemeral=True)

    async def reject(self, interaction: discord.Interaction, message: str) -> None:
        await interaction.response.send_message(f"<:sentra_cross:1546486840822075512> {message}", ephemeral=True)


class MentionModal(_AutoModConfigModal):
    limit = discord.ui.TextInput(
        label="Mentions allowed per message (3-20)",
        placeholder="6",
        min_length=1,
        max_length=2,
    )

    def __init__(self, cog: "AutoModCog", guild_id: int, config: dict) -> None:
        super().__init__(cog, guild_id, "📣 Mention guard")
        self.limit.default = str(config["mention_limit"])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            value = int(str(self.limit.value))
        except ValueError:
            await self.reject(interaction, "Enter a whole number, e.g. `6`.")
            return
        value = max(3, min(20, value))
        await self.cog.set_config(self.guild_id, "mention_limit", value)
        await self.save_and_refresh(interaction, f"<:sentra_check:1546486790779572385> Mention limit set to **{value}**.")


class CapsModal(_AutoModConfigModal):
    min_len = discord.ui.TextInput(
        label="Minimum message length to check (6-100)",
        placeholder="12",
        min_length=1,
        max_length=3,
    )
    ratio = discord.ui.TextInput(
        label="Caps ratio as percent (50-95)",
        placeholder="70",
        min_length=2,
        max_length=2,
    )

    def __init__(self, cog: "AutoModCog", guild_id: int, config: dict) -> None:
        super().__init__(cog, guild_id, "🔠 Caps filter tuning")
        self.min_len.default = str(config["caps_min_len"])
        self.ratio.default = str(round(config["caps_ratio"] * 100))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            min_len = int(str(self.min_len.value))
            ratio = int(str(self.ratio.value))
        except ValueError:
            await self.reject(interaction, "Enter whole numbers, e.g. `12` and `70`.")
            return
        min_len = max(6, min(100, min_len))
        ratio = max(0.5, min(0.95, ratio / 100))
        await self.cog.set_config(self.guild_id, "caps_min_len", min_len)
        await self.cog.set_config(self.guild_id, "caps_ratio", ratio)
        await self.save_and_refresh(
            interaction, f"<:sentra_check:1546486790779572385> Caps filter: **{min_len}+** chars at **{ratio:.0%}** caps."
        )


class EmojiModal(_AutoModConfigModal):
    limit = discord.ui.TextInput(
        label="Max emojis allowed per message (4-30)",
        placeholder="8",
        min_length=1,
        max_length=2,
    )

    def __init__(self, cog: "AutoModCog", guild_id: int, config: dict) -> None:
        super().__init__(cog, guild_id, "🤬 Emoji spam limit")
        self.limit.default = str(config["emoji_limit"])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            value = int(str(self.limit.value))
        except ValueError:
            await self.reject(interaction, "Enter a whole number, e.g. `8`.")
            return
        value = max(4, min(30, value))
        await self.cog.set_config(self.guild_id, "emoji_limit", value)
        await self.save_and_refresh(interaction, f"<:sentra_check:1546486790779572385> Emoji limit set to **{value}**.")


class AttachmentsModal(_AutoModConfigModal):
    exts = discord.ui.TextInput(
        label="Blocked extensions (comma-separated)",
        placeholder=".exe, .scr, .bat — leave empty to allow all",
        required=False,
        max_length=200,
        style=discord.TextStyle.short,
    )

    def __init__(self, cog: "AutoModCog", guild_id: int, config: dict) -> None:
        super().__init__(cog, guild_id, "📎 Blocked file types")
        self.exts.default = ", ".join(config["attachments_blocked_exts"])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.exts.value).strip()
        if not raw:
            blocked: list[str] = []
            note = "All file types are now allowed."
        else:
            blocked = [
                f".{e.strip().lower().lstrip('.')}"
                for e in raw.replace(",", " ").split()
                if e.strip().lstrip(".")
            ][:20]
            note = f"Blocked attachments: `{', '.join(blocked)}`"
        await self.cog.set_config(self.guild_id, "attachments_blocked_exts", blocked)
        await self.save_and_refresh(interaction, f"<:sentra_check:1546486790779572385> {note}")


class LinksModal(_AutoModConfigModal):
    allow = discord.ui.TextInput(
        label="Allowed domains (comma-separated)",
        placeholder="youtube.com, github.com — empty blocks all links",
        required=False,
        max_length=300,
        style=discord.TextStyle.short,
    )

    def __init__(self, cog: "AutoModCog", guild_id: int, config: dict) -> None:
        super().__init__(cog, guild_id, "🌐 Link allow-list")
        self.allow.default = ", ".join(config["link_allow_list"])

    async def on_submit(self, interaction: discord.Interaction) -> None:
        allowed = [d.strip().lower() for d in str(self.allow.value).replace(",", " ").split() if d.strip()][:20]
        await self.cog.set_config(self.guild_id, "link_allow_list", allowed)
        if allowed:
            note = f"Allow-list: `{', '.join(allowed)}`"
        else:
            note = "Allow-list cleared — the link filter now blocks **all** links."
        await self.save_and_refresh(interaction, f"<:sentra_check:1546486790779572385> {note}")


class PunishmentModal(_AutoModConfigModal):
    """Per-filter punishment editor opened from the punishments sub-panel."""

    action = discord.ui.TextInput(
        label="Action: delete / timeout / kick / default",
        placeholder="timeout",
        min_length=4,
        max_length=7,
    )
    duration = discord.ui.TextInput(
        label="Timeout duration in minutes (timeout only)",
        placeholder="30",
        required=False,
        max_length=5,
    )

    def __init__(
        self,
        cog: "AutoModCog",
        guild_id: int,
        kind: str,
        label: str,
        config: dict,
    ) -> None:
        super().__init__(cog, guild_id, f"⚖️ Punishment — {label}")
        self.kind = kind
        self.label = label
        entry = (config.get("punishments") or {}).get(kind)
        if isinstance(entry, dict) and "action" in entry:
            self.action.default = str(entry["action"])
            self.duration.default = (
                str(entry.get("duration", 30)) if entry.get("action") == "timeout" else "30"
            )

    async def save_and_refresh(self, interaction: discord.Interaction, message: str) -> None:
        config = await self.cog.get_config(self.guild_id)
        await interaction.response.edit_message(
            view=PunishmentPanel(self.cog, self.guild_id, config)
        )
        await interaction.followup.send(message, ephemeral=True)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        action = str(self.action.value).strip().lower()
        if action not in ("delete", "timeout", "kick", "default"):
            await self.reject(interaction, "Action must be one of: `delete`, `timeout`, `kick`, `default`.")
            return
        minutes = 10
        if action == "timeout":
            try:
                minutes = max(1, min(40320, int(str(self.duration.value).strip() or 10)))
            except ValueError:
                await self.reject(interaction, "Duration must be a whole number of minutes, e.g. `30`.")
                return
        config = await self.cog.get_config(self.guild_id)
        punishments = dict(config.get("punishments") or {})
        if action == "default":
            punishments.pop(self.kind, None)
            note = f"{self.label} punishment reset to the **escalation ladder**."
        else:
            entry: dict = {"action": action}
            if action == "timeout":
                entry["duration"] = minutes
            punishments[self.kind] = entry
            note = f"{self.label} punishment set to **{punishment_label(entry)}**."
        await self.cog.set_config(self.guild_id, "punishments", punishments)
        await self.save_and_refresh(interaction, f"<:sentra_check:1546486790779572385> {note}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AutoModCog(bot))
