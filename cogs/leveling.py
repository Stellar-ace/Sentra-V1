from __future__ import annotations

import asyncio
import logging
import math
import random
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from sentra.config import settings
from sentra.db.engine import session_factory
from sentra.db.models import Guild, Member
from sentra.ui import EMOJI, LEVEL, style, TICK, CROSS

log = logging.getLogger(__name__)

# Backwards-compat constants — also serve as defaults when guild config is absent.
COOLDOWN_SECONDS = 60
VOICE_XP_PER_MINUTE = 8
VOICE_XP_MAX_MINUTES = 120  # cap per award when the bot missed a long window

LEVELS_DEFAULTS: dict = {
    "role_rewards": {},  # str(level) -> role_id
    "enabled": True,
    "announce_channel_id": None,
    "xp_min": 15,
    "xp_max": 25,
    "voice_xp_per_min": 8,
    "ignored_channel_ids": [],
    "ignored_role_ids": [],
    "cooldown_seconds": 60,
}


def level_for(xp: int) -> int:
    return math.isqrt(max(xp, 0) // 100)


def xp_for_level(level: int) -> int:
    return 100 * level * level


def progress_bar(current: int, needed: int, width: int = 12) -> str:
    ratio = min(max(current / needed, 0), 1) if needed else 0
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


class LevelingCog(commands.Cog):
    levels = app_commands.Group(
        name="levels",
        description="Level system configuration",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # (guild_id, user_id) -> expiry monotonic
        self._cooldowns: dict[tuple[int, int], float] = {}
        # (guild_id, user_id) -> (channel_id, last_awarded_monotonic)
        self.voice_joins: dict[tuple[int, int], tuple[int, float]] = {}
        self._voice_lock = asyncio.Lock()

    # ------------------------------------------------------------------ config

    def _copy_levels_defaults(self) -> dict:
        return {
            k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
            for k, v in LEVELS_DEFAULTS.items()
        }

    async def get_config(self, guild_id: int) -> dict:
        async with session_factory() as session:
            guild = await session.get(Guild, guild_id)
            stored = dict((guild.config or {}).get("levels", {})) if guild else {}
        cfg = self._copy_levels_defaults()
        # merge stored (deep copy stored collections)
        for k, v in stored.items():
            if isinstance(v, dict):
                cfg[k] = dict(v)
            elif isinstance(v, list):
                cfg[k] = list(v)
            else:
                cfg[k] = v
        # allow unknown keys to persist (forward-compat) but ensure defaults exist
        for k, v in LEVELS_DEFAULTS.items():
            cfg.setdefault(k, v if not isinstance(v, (dict, list)) else (dict(v) if isinstance(v, dict) else list(v)))
        # normalise types
        rr = cfg.get("role_rewards", {})
        if not isinstance(rr, dict):
            rr = {}
        norm_rr: dict[str, int] = {}
        for k, v in rr.items():
            ks = str(k)
            if ks.isdigit():
                try:
                    norm_rr[ks] = int(v)
                except Exception:
                    continue
        cfg["role_rewards"] = norm_rr
        # lists
        for lk in ("ignored_channel_ids", "ignored_role_ids"):
            lst = cfg.get(lk, [])
            if not isinstance(lst, list):
                lst = []
            cfg[lk] = [int(x) for x in lst if str(x).isdigit()]
        # scalars
        cfg["enabled"] = bool(cfg.get("enabled", True))
        cfg["announce_channel_id"] = int(cfg["announce_channel_id"]) if str(cfg.get("announce_channel_id") or "").isdigit() else None
        try:
            cfg["xp_min"] = int(cfg.get("xp_min", 15))
            cfg["xp_max"] = int(cfg.get("xp_max", 25))
            cfg["voice_xp_per_min"] = int(cfg.get("voice_xp_per_min", 8))
            cfg["cooldown_seconds"] = int(cfg.get("cooldown_seconds", 60))
        except Exception:
            pass
        # clamp
        cfg["xp_min"] = max(1, min(cfg["xp_min"], 500))
        cfg["xp_max"] = max(cfg["xp_min"], min(cfg["xp_max"], 500))
        cfg["voice_xp_per_min"] = max(1, min(cfg["voice_xp_per_min"], 50))
        cfg["cooldown_seconds"] = max(5, min(cfg["cooldown_seconds"], 600))
        return cfg

    async def update_config(self, guild_id: int, mutate) -> None:
        async with session_factory() as session:
            guild = await session.get(Guild, guild_id)
            if guild is None:
                guild = Guild(id=guild_id)
                session.add(guild)
                full = {}
                cfg = self._copy_levels_defaults()
            else:
                full = dict(guild.config or {})
                stored = dict(full.get("levels", {}))
                cfg = self._copy_levels_defaults()
                for k, v in stored.items():
                    if isinstance(v, dict):
                        cfg[k] = dict(v)
                    elif isinstance(v, list):
                        cfg[k] = list(v)
                    else:
                        cfg[k] = v
            mutate(cfg)  # type: ignore[operator]
            full["levels"] = cfg
            guild.config = full
            await session.commit()

    async def xp_multiplier(self, member: discord.Member) -> float:
        multiplier = 1.0
        if getattr(member, "premium_since", None):
            multiplier *= 1.5
        try:
            if member.guild.id in settings.premium_guild_ids:
                multiplier *= 2.0
        except Exception:
            pass
        return multiplier

    # ------------------------------------------------------------------ DB helpers (atomic)

    async def fetch_member(self, guild_id: int, user_id: int) -> Member | None:
        async with session_factory() as session:
            return (
                await session.execute(
                    select(Member).where(Member.guild_id == guild_id, Member.user_id == user_id)
                )
            ).scalar_one_or_none()

    async def _atomic_add_xp(self, guild_id: int, user_id: int, amount: int) -> int:
        """Atomically increment xp by amount (>0) and return new total.

        Uses UPDATE … SET xp = xp + amount; if no row, INSERTs. Retries on
        IntegrityError race. Works on both SQLite and Postgres.
        """
        if amount == 0:
            rec = await self.fetch_member(guild_id, user_id)
            return int(rec.xp) if rec else 0
        if amount < 0:
            # negative path — fetch/compute/set to avoid negative SQL
            rec = await self.fetch_member(guild_id, user_id)
            old = int(rec.xp) if rec else 0
            new = max(0, old + amount)
            return await self._atomic_set_xp(guild_id, user_id, new)

        for _ in range(3):
            async with session_factory() as session:
                try:
                    result = await session.execute(
                        update(Member)
                        .where(Member.guild_id == guild_id, Member.user_id == user_id)
                        .values(xp=Member.xp + amount)
                    )
                    if result.rowcount:  # type: ignore[attr-defined]
                        row = await session.execute(
                            select(Member.xp).where(Member.guild_id == guild_id, Member.user_id == user_id)
                        )
                        total = int(row.scalar_one())
                        await session.commit()
                        return total
                    # no row — try insert
                    session.add(Member(guild_id=guild_id, user_id=user_id, xp=amount, balance=0))
                    await session.commit()
                    return amount
                except IntegrityError:
                    await session.rollback()
                    continue
                except Exception:
                    await session.rollback()
                    raise
        # fallback
        rec = await self.fetch_member(guild_id, user_id)
        return int(rec.xp) if rec else 0

    async def _atomic_set_xp(self, guild_id: int, user_id: int, xp: int) -> int:
        xp = max(0, int(xp))
        for _ in range(3):
            async with session_factory() as session:
                try:
                    result = await session.execute(
                        update(Member).where(Member.guild_id == guild_id, Member.user_id == user_id).values(xp=xp)
                    )
                    if result.rowcount:  # type: ignore[attr-defined]
                        await session.commit()
                        return xp
                    # insert
                    # check if balance row exists — preserve balance if so
                    existing = (
                        await session.execute(
                            select(Member).where(Member.guild_id == guild_id, Member.user_id == user_id)
                        )
                    ).scalar_one_or_none()
                    if existing is not None:
                        # race: row appeared, retry as update
                        await session.rollback()
                        continue
                    session.add(Member(guild_id=guild_id, user_id=user_id, xp=xp, balance=0))
                    await session.commit()
                    return xp
                except IntegrityError:
                    await session.rollback()
                    continue
        return xp

    async def _atomic_add_balance(self, guild_id: int, user_id: int, delta: int) -> int:
        if delta == 0:
            rec = await self.fetch_member(guild_id, user_id)
            return int(rec.balance) if rec else 0
        for _ in range(3):
            async with session_factory() as session:
                try:
                    # try update existing
                    result = await session.execute(
                        update(Member)
                        .where(Member.guild_id == guild_id, Member.user_id == user_id)
                        .values(balance=Member.balance + delta)
                    )
                    if result.rowcount:  # type: ignore[attr-defined]
                        # clamp negatives in Python (cannot do GREATEST portably)
                        row = await session.execute(
                            select(Member.balance).where(Member.guild_id == guild_id, Member.user_id == user_id)
                        )
                        bal = int(row.scalar_one())
                        if bal < 0:
                            await session.execute(
                                update(Member)
                                .where(Member.guild_id == guild_id, Member.user_id == user_id)
                                .values(balance=0)
                            )
                            bal = 0
                        await session.commit()
                        return bal
                    # no row → insert only if delta positive (otherwise 0)
                    init = max(0, int(delta))
                    session.add(Member(guild_id=guild_id, user_id=user_id, xp=0, balance=init))
                    await session.commit()
                    return init
                except IntegrityError:
                    await session.rollback()
                    continue
                except Exception:
                    await session.rollback()
                    raise
        rec = await self.fetch_member(guild_id, user_id)
        return int(rec.balance) if rec else max(0, int(delta))

    async def credit(self, guild_id: int, user_id: int, amount: int) -> None:
        await self._atomic_add_balance(guild_id, user_id, max(0, int(amount)))

    async def add_xp(self, guild_id: int, user_id: int, amount: int) -> int:
        return await self._atomic_add_xp(guild_id, user_id, int(amount))

    # ------------------------------------------------------------------ rewards & announce

    async def grant_rewards(
        self, guild: discord.Guild, member: discord.Member, new_level: int
    ) -> None:
        config = await self.get_config(guild.id)
        me = guild.me
        if me is None or not me.guild_permissions.manage_roles:
            return
        unlocked_ids: set[int] = set()
        for lvl_str, rid in config["role_rewards"].items():
            if not lvl_str.isdigit():
                continue
            try:
                if int(lvl_str) <= new_level:
                    unlocked_ids.add(int(rid))
            except Exception:
                continue
        if not unlocked_ids:
            return
        to_add: list[discord.Role] = []
        for rid in unlocked_ids:
            role = guild.get_role(rid)
            if role is None:
                continue
            if role.managed or role >= me.top_role or role in member.roles:
                continue
            to_add.append(role)
        if to_add:
            try:
                await member.add_roles(*to_add, reason=f"Level {new_level} reward")
            except discord.HTTPException:
                pass

    def _resolve_announce_channel(
        self, guild: discord.Guild, config: dict, fallback: discord.abc.Messageable | None
    ) -> discord.TextChannel | None:
        ch_id = config.get("announce_channel_id")
        if ch_id:
            ch = guild.get_channel(int(ch_id))
            if isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).send_messages:  # type: ignore[union-attr]
                return ch
        if isinstance(fallback, discord.TextChannel) and fallback.permissions_for(guild.me).send_messages:  # type: ignore[union-attr]
            return fallback
        # guild system channel
        sys_ch = guild.system_channel
        if isinstance(sys_ch, discord.TextChannel) and sys_ch.permissions_for(guild.me).send_messages:  # type: ignore[union-attr]
            return sys_ch
        # first writable text channel
        me = guild.me
        if me is None:
            return None
        for ch in guild.text_channels:
            try:
                if ch.permissions_for(me).send_messages:
                    return ch
            except Exception:
                continue
        return None

    async def _handle_level_up(
        self,
        guild: discord.Guild,
        member: discord.Member,
        old_xp: int,
        new_xp: int,
        fallback_channel: discord.abc.Messageable | None,
        *,
        multiplier: float = 1.0,
    ) -> None:
        old_level = level_for(old_xp)
        new_level = level_for(new_xp)
        if new_level <= old_level:
            return
        levels_crossed = new_level - old_level
        coins = 25 * levels_crossed
        # credit coins (per level)
        try:
            await self._atomic_add_balance(guild.id, member.id, coins)
        except Exception:
            log.exception("credit failed for %s/%s", guild.id, member.id)
        # role rewards
        try:
            await self.grant_rewards(guild, member, new_level)
        except Exception:
            log.exception("grant_rewards failed for %s/%s", guild.id, member.id)

        # announce
        try:
            config = await self.get_config(guild.id)
            channel = self._resolve_announce_channel(guild, config, fallback_channel)
            if channel is None:
                return
            suffix = f" (+{coins} 🪙)"
            if multiplier > 1.0:
                suffix += f" · {multiplier:g}× XP"
            # if multi-level jump, note it
            jump_note = f" ({old_level} → {new_level})" if levels_crossed > 1 else ""
            embed = discord.Embed(
                description=f"GG {member.mention}, you reached level **{new_level}**{jump_note}!{suffix}",
                color=LEVEL,
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            style(embed)
            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=False),
            )
        except discord.HTTPException:
            pass
        except Exception:
            log.exception("level-up announce failed")

    # ------------------------------------------------------------------ listeners

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        guild = message.guild
        # allow fetch if author not yet a Member (ensures roles + premium_since)
        author = message.author
        if not isinstance(author, discord.Member):
            try:
                author = await guild.fetch_member(author.id)
            except discord.HTTPException:
                return
        try:
            config = await self.get_config(guild.id)
        except Exception:
            log.exception("get_config failed for guild %s", guild.id)
            return
        if not config.get("enabled", True):
            return

        # ignored channel / category / role checks
        try:
            ignored_ch = set(config.get("ignored_channel_ids", []))
            if message.channel.id in ignored_ch:
                return
            # category
            cat_id = getattr(message.channel, "category_id", None)
            if cat_id and int(cat_id) in ignored_ch:
                return
            ignored_roles = set(config.get("ignored_role_ids", []))
            if ignored_roles and any(r.id in ignored_roles for r in getattr(author, "roles", [])):
                return
        except Exception:
            pass

        # ignore very short messages? Keep permissive — only ignore empty.
        if not message.content and not message.attachments:
            # still counts? but many bots require content; we allow empty only if attachment present
            if not message.attachments:
                return

        # cooldown (per guild+user)
        cooldown = int(config.get("cooldown_seconds", COOLDOWN_SECONDS))
        key = (guild.id, author.id)
        now = time.monotonic()
        exp = self._cooldowns.get(key, 0)
        if now < exp:
            return
        self._cooldowns[key] = now + cooldown
        # occasional prune
        if len(self._cooldowns) > 8000:
            self._cooldowns = {k: v for k, v in self._cooldowns.items() if v > now}

        xp_min = int(config.get("xp_min", 15))
        xp_max = int(config.get("xp_max", 25))
        base = random.randint(min(xp_min, xp_max), max(xp_min, xp_max))

        try:
            multiplier = await self.xp_multiplier(author)  # type: ignore[arg-type]
        except Exception:
            multiplier = 1.0
        gain = max(1, int(base * multiplier))

        # fetch old xp before increment for level-up detection
        try:
            existing = await self.fetch_member(guild.id, author.id)
            old_xp = int(existing.xp) if existing else 0
        except Exception:
            old_xp = 0

        try:
            total = await self._atomic_add_xp(guild.id, author.id, gain)
        except Exception:
            log.exception("add_xp failed for %s/%s", guild.id, author.id)
            return

        # handle level-up (also grants coins + roles + announces)
        try:
            await self._handle_level_up(guild, author, old_xp, total, message.channel, multiplier=multiplier)  # type: ignore[arg-type]
        except Exception:
            log.exception("handle_level_up failed")

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        guild = member.guild
        if member.bot:
            return
        try:
            config = await self.get_config(guild.id)
        except Exception:
            config = LEVELS_DEFAULTS
        if not config.get("enabled", True):
            return

        # ignored role check for voice too
        try:
            ignored_roles = set(config.get("ignored_role_ids", []))
            if ignored_roles and any(r.id in ignored_roles for r in getattr(member, "roles", [])):
                return
        except Exception:
            pass

        afk_id = guild.afk_channel.id if guild.afk_channel else None
        ignored_ch = set(config.get("ignored_channel_ids", []))
        key = (guild.id, member.id)
        voice_xp_rate = int(config.get("voice_xp_per_min", VOICE_XP_PER_MINUTE))

        async with self._voice_lock:
            # LEAVE (process before JOIN so a move credits the old channel correctly)
            if before.channel is not None and before.channel != after.channel:
                left = before.channel
                # skip AFK and ignored channels — and only if we were tracking
                if left.id != afk_id and left.id not in ignored_ch:
                    popped = self.voice_joins.pop(key, None)
                    if popped is not None:
                        _ch_id, started = popped
                        # guard: popped channel should match left channel; if not, use popped anyway
                        minutes = int((time.monotonic() - started) // 60)
                        if minutes >= 1:
                            minutes = min(minutes, VOICE_XP_MAX_MINUTES)
                            try:
                                multiplier = await self.xp_multiplier(member)
                            except Exception:
                                multiplier = 1.0
                            gain = max(1, int(minutes * voice_xp_rate * multiplier))
                            try:
                                existing = await self.fetch_member(guild.id, member.id)
                                old_xp = int(existing.xp) if existing else 0
                                total = await self._atomic_add_xp(guild.id, member.id, gain)
                                await self._handle_level_up(guild, member, old_xp, total, None, multiplier=multiplier)
                            except Exception:
                                log.exception("voice leave xp failed for %s/%s", guild.id, member.id)

            # JOIN
            if after.channel is not None and before.channel != after.channel:
                joined = after.channel
                if joined.id == afk_id or joined.id in ignored_ch:
                    return
                # only track if not already tracked (shouldn't happen after leave pop)
                if key not in self.voice_joins:
                    # respect deaf: don't track while server-deafened or self-deafened
                    vs = after
                    if getattr(vs, "deaf", False) or getattr(vs, "self_deaf", False):
                        return
                    self.voice_joins[key] = (joined.id, time.monotonic())
                else:
                    # move case where we didn't pop? shouldn't happen — ensure we update to new channel
                    self.voice_joins[key] = (joined.id, time.monotonic())

            # MUTE/DEAF toggles without channel change — pause or resume
            if before.channel == after.channel and before.channel is not None:
                # if user became deafened, pause: pop and discard elapsed? or keep paused
                became_deaf = (not before.deaf and after.deaf) or (not before.self_deaf and after.self_deaf)
                became_undeaf = (before.deaf and not after.deaf) or (before.self_deaf and not after.self_deaf)
                if became_deaf and key in self.voice_joins:
                    # stop tracking — credit partial up to now
                    _ch_id, started = self.voice_joins.pop(key)
                    minutes = int((time.monotonic() - started) // 60)
                    if minutes >= 1:
                        minutes = min(minutes, VOICE_XP_MAX_MINUTES)
                        try:
                            multiplier = await self.xp_multiplier(member)
                            gain = max(1, int(minutes * voice_xp_rate * multiplier))
                            existing = await self.fetch_member(guild.id, member.id)
                            old_xp = int(existing.xp) if existing else 0
                            total = await self._atomic_add_xp(guild.id, member.id, gain)
                            await self._handle_level_up(guild, member, old_xp, total, None, multiplier=multiplier)
                        except Exception:
                            log.exception("voice deaf pause xp failed")
                elif became_undeaf and key not in self.voice_joins:
                    if after.channel.id != afk_id and after.channel.id not in ignored_ch:
                        self.voice_joins[key] = (after.channel.id, time.monotonic())

    # ------------------------------------------------------------------ voice periodic tick

    @tasks.loop(minutes=1)
    async def voice_tick(self) -> None:
        now = time.monotonic()
        # copy snapshot to avoid holding lock while DB IO
        async with self._voice_lock:
            snapshot = list(self.voice_joins.items())
        for (guild_id, user_id), (channel_id, last) in snapshot:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                async with self._voice_lock:
                    self.voice_joins.pop((guild_id, user_id), None)
                continue
            member = guild.get_member(user_id)
            if member is None or member.bot:
                async with self._voice_lock:
                    self.voice_joins.pop((guild_id, user_id), None)
                continue
            vs = getattr(member, "voice", None)
            if vs is None or vs.channel is None or vs.channel.id != channel_id:
                # desync — remove
                async with self._voice_lock:
                    # keep if moved? on_voice_state_update will have handled; just skip
                    if (guild_id, user_id) in self.voice_joins and self.voice_joins[(guild_id, user_id)][0] != (vs.channel.id if vs and vs.channel else None):
                        self.voice_joins.pop((guild_id, user_id), None)
                continue
            # AFK / deaf checks
            afk_id = guild.afk_channel.id if guild.afk_channel else None
            if channel_id == afk_id:
                async with self._voice_lock:
                    self.voice_joins[(guild_id, user_id)] = (channel_id, now)
                continue
            if getattr(vs, "deaf", False) or getattr(vs, "self_deaf", False):
                async with self._voice_lock:
                    self.voice_joins[(guild_id, user_id)] = (channel_id, now)
                continue
            try:
                config = await self.get_config(guild_id)
            except Exception:
                config = LEVELS_DEFAULTS
            if not config.get("enabled", True):
                continue
            ignored_ch = set(config.get("ignored_channel_ids", []))
            if channel_id in ignored_ch:
                continue
            ignored_roles = set(config.get("ignored_role_ids", []))
            if ignored_roles and any(r.id in ignored_roles for r in getattr(member, "roles", [])):
                continue

            minutes = int((now - last) // 60)
            if minutes < 1:
                continue
            minutes = min(minutes, VOICE_XP_MAX_MINUTES)
            voice_rate = int(config.get("voice_xp_per_min", VOICE_XP_PER_MINUTE))
            try:
                multiplier = await self.xp_multiplier(member)
            except Exception:
                multiplier = 1.0
            gain = max(1, int(minutes * voice_rate * multiplier))
            try:
                existing = await self.fetch_member(guild_id, user_id)
                old_xp = int(existing.xp) if existing else 0
                total = await self._atomic_add_xp(guild_id, user_id, gain)
                await self._handle_level_up(guild, member, old_xp, total, None, multiplier=multiplier)
            except Exception:
                log.exception("voice_tick xp failed for %s/%s", guild_id, user_id)
                continue
            # advance watermark
            async with self._voice_lock:
                cur = self.voice_joins.get((guild_id, user_id))
                if cur and cur[0] == channel_id:
                    # move watermark forward by credited minutes (preserves residual seconds)
                    self.voice_joins[(guild_id, user_id)] = (channel_id, last + minutes * 60)

    @voice_tick.before_loop
    async def before_voice_tick(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            # load_check context — client not logged in; cancel loop to avoid noise
            if self.voice_tick.is_running():
                self.voice_tick.cancel()

    async def cog_load(self) -> None:
        try:
            if not self.voice_tick.is_running():
                self.voice_tick.start()
        except RuntimeError:
            pass  # load_check: client not initialised

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # seed currently connected voice members so long sessions aren't missed after restart
        for guild in self.bot.guilds:
            afk_id = guild.afk_channel.id if guild.afk_channel else None
            for vs_channel in guild.voice_channels:
                if vs_channel.id == afk_id:
                    continue
                for member in list(vs_channel.members):
                    if member.bot:
                        continue
                    if getattr(member.voice, "deaf", False) or getattr(member.voice, "self_deaf", False):
                        continue
                    key = (guild.id, member.id)
                    if key in self.voice_joins:
                        continue
                    try:
                        cfg = await self.get_config(guild.id)
                        ignored_ch = set(cfg.get("ignored_channel_ids", []))
                        if vs_channel.id in ignored_ch:
                            continue
                        ignored_roles = set(cfg.get("ignored_role_ids", []))
                        if ignored_roles and any(r.id in ignored_roles for r in getattr(member, "roles", [])):
                            continue
                    except Exception:
                        pass
                    async with self._voice_lock:
                        if key not in self.voice_joins:
                            self.voice_joins[key] = (vs_channel.id, time.monotonic())

    def cog_unload(self) -> None:
        if self.voice_tick.is_running():
            self.voice_tick.cancel()

    # ------------------------------------------------------------------ slash commands

    @levels.command(name="reward-add", description="Grant a role when members reach a level")
    @app_commands.describe(level="The level that unlocks this role", role="Role to grant")
    async def reward_add(
        self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 999], role: discord.Role
    ) -> None:
        assert interaction.guild is not None
        me = interaction.guild.me
        if me is None or role.managed or role >= me.top_role:
            await interaction.response.send_message("I can't grant that role — it's managed or above me.", ephemeral=True)
            return

        def mutate(cfg: dict) -> None:
            rewards = dict(cfg.get("role_rewards", {}))
            rewards[str(level)] = int(role.id)
            cfg["role_rewards"] = rewards

        await self.update_config(interaction.guild.id, mutate)
        embed = discord.Embed(description=f"<:sentra_check:1546486790779572385> Level **{level}** now unlocks {role.mention}.", color=LEVEL)
        style(embed, interaction, emoji=EMOJI["levels"])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @levels.command(name="reward-remove", description="Remove a level reward")
    @app_commands.describe(level="The level to clear")
    async def reward_remove(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 999]) -> None:
        assert interaction.guild is not None

        def mutate(cfg: dict) -> None:
            rewards = dict(cfg.get("role_rewards", {}))
            rewards.pop(str(level), None)
            cfg["role_rewards"] = rewards

        await self.update_config(interaction.guild.id, mutate)
        embed = discord.Embed(description=f"Reward for level **{level}** removed.", color=LEVEL)
        style(embed, interaction, emoji=EMOJI["levels"])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @levels.command(name="rewards", description="List configured level rewards")
    async def reward_list(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        config = await self.get_config(interaction.guild.id)
        if not config["role_rewards"]:
            await interaction.response.send_message("No rewards configured. Add one with `/levels reward-add`.", ephemeral=True)
            return
        lines = [
            f"Level **{lvl}** → <@&{rid}>"
            for lvl, rid in sorted(config["role_rewards"].items(), key=lambda kv: int(kv[0]))
        ]
        embed = discord.Embed(title="🎖️ Level rewards", description="\n".join(lines), color=LEVEL)
        style(embed, interaction, emoji=EMOJI["levels"])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @levels.command(name="toggle", description="Enable or disable the leveling system")
    @app_commands.describe(enabled="Whether leveling should be active")
    async def levels_toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        assert interaction.guild is not None
        await self.update_config(interaction.guild.id, lambda cfg: cfg.__setitem__("enabled", bool(enabled)))
        state = "enabled <:sentra_check:1546486790779572385>" if enabled else "disabled <:sentra_cross:1546486840822075512>"
        embed = discord.Embed(description=f"Leveling is now **{state}**.", color=LEVEL)
        style(embed, interaction, emoji=EMOJI["levels"])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @levels.command(name="announce", description="Set the level-up announcement channel")
    @app_commands.describe(channel="Where level-up messages go (leave empty to use the earning channel)")
    async def levels_announce(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        assert interaction.guild is not None
        if channel is not None:
            perms = channel.permissions_for(interaction.guild.me)  # type: ignore[union-attr]
            if not perms.send_messages:
                await interaction.response.send_message("I can't send messages there.", ephemeral=True)
                return

        def mutate(cfg: dict) -> None:
            cfg["announce_channel_id"] = channel.id if channel else None

        await self.update_config(interaction.guild.id, mutate)
        msg = f"Announcements → {channel.mention}" if channel else "Announcements → *earning channel / system channel*"
        embed = discord.Embed(description=f"<:sentra_check:1546486790779572385> {msg}", color=LEVEL)
        style(embed, interaction, emoji=EMOJI["levels"])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @levels.command(name="xprange", description="Set XP gained per message")
    @app_commands.describe(xp_min="Minimum XP per message (1-500)", xp_max="Maximum XP per message (1-500)")
    async def levels_xprange(
        self,
        interaction: discord.Interaction,
        xp_min: app_commands.Range[int, 1, 500],
        xp_max: app_commands.Range[int, 1, 500],
    ) -> None:
        assert interaction.guild is not None
        if xp_min > xp_max:
            await interaction.response.send_message("`xp_min` must be ≤ `xp_max`.", ephemeral=True)
            return

        def mutate(cfg: dict) -> None:
            cfg["xp_min"] = int(xp_min)
            cfg["xp_max"] = int(xp_max)

        await self.update_config(interaction.guild.id, mutate)
        embed = discord.Embed(description=f"<:sentra_check:1546486790779572385> XP per message: **{xp_min}–{xp_max}**", color=LEVEL)
        style(embed, interaction, emoji=EMOJI["levels"])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @levels.command(name="cooldown", description="Set the per-user message XP cooldown")
    @app_commands.describe(seconds="Cooldown in seconds (5-600)")
    async def levels_cooldown(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 5, 600]) -> None:
        assert interaction.guild is not None
        await self.update_config(interaction.guild.id, lambda cfg: cfg.__setitem__("cooldown_seconds", int(seconds)))
        embed = discord.Embed(description=f"<:sentra_check:1546486790779572385> Cooldown → **{seconds}s**", color=LEVEL)
        style(embed, interaction, emoji=EMOJI["levels"])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @levels.command(name="voice-rate", description="Set XP per minute for voice")
    @app_commands.describe(rate="XP per minute (1-50)")
    async def levels_voice_rate(self, interaction: discord.Interaction, rate: app_commands.Range[int, 1, 50]) -> None:
        assert interaction.guild is not None
        await self.update_config(interaction.guild.id, lambda cfg: cfg.__setitem__("voice_xp_per_min", int(rate)))
        embed = discord.Embed(description=f"<:sentra_check:1546486790779572385> Voice XP → **{rate}/min**", color=LEVEL)
        style(embed, interaction, emoji=EMOJI["levels"])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @levels.command(name="ignore-channel", description="Ignore a channel/category for XP")
    @app_commands.describe(channel="Channel to ignore (its category is also considered if you pick a text channel)")
    async def ignore_channel(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel) -> None:
        assert interaction.guild is not None

        def mutate(cfg: dict) -> None:
            lst = list(cfg.get("ignored_channel_ids", []))
            if channel.id not in lst:
                lst.append(int(channel.id))
            cfg["ignored_channel_ids"] = lst

        await self.update_config(interaction.guild.id, mutate)
        await interaction.response.send_message(f"<:sentra_check:1546486790779572385> Ignoring {channel.mention} for XP.", ephemeral=True)

    @levels.command(name="unignore-channel", description="Stop ignoring a channel for XP")
    @app_commands.describe(channel="Channel to stop ignoring")
    async def unignore_channel(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel) -> None:
        assert interaction.guild is not None

        def mutate(cfg: dict) -> None:
            cfg["ignored_channel_ids"] = [int(x) for x in cfg.get("ignored_channel_ids", []) if int(x) != channel.id]

        await self.update_config(interaction.guild.id, mutate)
        await interaction.response.send_message(f"<:sentra_check:1546486790779572385> No longer ignoring {channel.mention}.", ephemeral=True)

    @levels.command(name="config", description="Show the current leveling configuration")
    async def levels_config(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        cfg = await self.get_config(interaction.guild.id)
        announce = f"<#{cfg['announce_channel_id']}>" if cfg["announce_channel_id"] else "*earning / system channel*"
        ignored_ch = ", ".join(f"<#{i}>" for i in cfg["ignored_channel_ids"][:10]) or "*none*"
        # rewards preview
        rewards = cfg["role_rewards"]
        rewards_lines = (
            ", ".join(f"Lv{lv}→<@&{rid}>" for lv, rid in sorted(rewards.items(), key=lambda kv: int(kv[0]))[:5])
            or "*none*"
        )
        embed = discord.Embed(title="⚙️ Leveling config", color=LEVEL)
        embed.add_field(name="Enabled", value="yes" if cfg["enabled"] else "no")
        embed.add_field(name="XP / message", value=f"{cfg['xp_min']}–{cfg['xp_max']}")
        embed.add_field(name="Cooldown", value=f"{cfg['cooldown_seconds']}s")
        embed.add_field(name="Voice", value=f"{cfg['voice_xp_per_min']}/min")
        embed.add_field(name="Announce", value=announce)
        embed.add_field(name="Ignored channels", value=ignored_ch)
        embed.add_field(name="Ignored roles", value=str(len(cfg["ignored_role_ids"])) + " role(s)" if cfg["ignored_role_ids"] else "*none*")
        embed.add_field(name="Rewards", value=rewards_lines)
        style(embed, interaction, emoji=EMOJI["levels"])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @levels.command(name="set-level", description="Set a member's level directly")
    @app_commands.describe(member="Target member", level="Level to set (0-999)")
    async def set_level(
        self, interaction: discord.Interaction, member: discord.Member, level: app_commands.Range[int, 0, 999]
    ) -> None:
        assert interaction.guild is not None
        if member.bot:
            await interaction.response.send_message("Bots can't have levels.", ephemeral=True)
            return
        new_xp = xp_for_level(int(level))
        old_rec = await self.fetch_member(interaction.guild.id, member.id)
        old_xp = int(old_rec.xp) if old_rec else 0
        await self._atomic_set_xp(interaction.guild.id, member.id, new_xp)
        # grant rewards if leveled up
        if level_for(new_xp) > level_for(old_xp):
            await self.grant_rewards(interaction.guild, member, int(level))
        embed = discord.Embed(description=f"<:sentra_check:1546486790779572385> {member.mention} is now level **{level}** ({new_xp} XP).", color=LEVEL)
        style(embed, interaction, emoji=EMOJI["levels"])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @levels.command(name="give-xp", description="Give XP to a member")
    @app_commands.describe(member="Target member", amount="XP to give (1-100000)")
    async def give_xp(
        self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 100000]
    ) -> None:
        assert interaction.guild is not None
        if member.bot:
            await interaction.response.send_message("Bots can't have levels.", ephemeral=True)
            return
        old_rec = await self.fetch_member(interaction.guild.id, member.id)
        old_xp = int(old_rec.xp) if old_rec else 0
        total = await self._atomic_add_xp(interaction.guild.id, member.id, int(amount))
        # also handle level-up side effects via central helper (coins already handled there)
        # For admin give, we still want rewards + coins — use same path
        await self._handle_level_up(interaction.guild, member, old_xp, total, interaction.channel)  # type: ignore[arg-type]
        embed = discord.Embed(description=f"<:sentra_check:1546486790779572385> Gave **{amount} XP** to {member.mention} → {total} XP (Lv {level_for(total)}).", color=LEVEL)
        style(embed, interaction, emoji=EMOJI["levels"])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @levels.command(name="remove-xp", description="Remove XP from a member")
    @app_commands.describe(member="Target member", amount="XP to remove (1-100000)")
    async def remove_xp(
        self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 100000]
    ) -> None:
        assert interaction.guild is not None
        old_rec = await self.fetch_member(interaction.guild.id, member.id)
        old_xp = int(old_rec.xp) if old_rec else 0
        new_xp = max(0, old_xp - int(amount))
        await self._atomic_set_xp(interaction.guild.id, member.id, new_xp)
        embed = discord.Embed(description=f"<:sentra_check:1546486790779572385> Removed **{amount} XP** from {member.mention} → {new_xp} XP (Lv {level_for(new_xp)}).", color=LEVEL)
        style(embed, interaction, emoji=EMOJI["levels"])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @levels.command(name="reset", description="Reset a member's XP and level to 0")
    @app_commands.describe(member="Member to reset")
    async def reset_member(self, interaction: discord.Interaction, member: discord.Member) -> None:
        assert interaction.guild is not None
        await self._atomic_set_xp(interaction.guild.id, member.id, 0)
        # optionally remove reward roles that were granted? Keep them — admin can manually remove. We note it.
        embed = discord.Embed(description=f"<:sentra_check:1546486790779572385> {member.mention} has been reset to **Lv 0** (0 XP).", color=LEVEL)
        style(embed, interaction, emoji=EMOJI["levels"])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ rank / leaderboard (user-facing)

    @app_commands.command(name="rank", description="Show your level card")
    @app_commands.guild_only()
    async def rank(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        assert interaction.guild is not None
        target = user or interaction.user  # type: ignore[assignment]
        # ensure we have a Member for roles/color
        if isinstance(target, discord.User):
            try:
                target = await interaction.guild.fetch_member(target.id)
            except discord.HTTPException:
                pass
        if not isinstance(target, discord.Member):
            await interaction.response.send_message("That user isn't in this server.", ephemeral=True)
            return
        record = await self.fetch_member(interaction.guild.id, target.id)
        if record is None:
            await interaction.response.send_message("No XP recorded yet — start chatting!", ephemeral=True)
            return
        level = level_for(record.xp)
        current_floor = xp_for_level(level)
        next_xp = xp_for_level(level + 1)
        span = next_xp - current_floor
        into = record.xp - current_floor

        # rank position
        try:
            async with session_factory() as session:
                higher = (
                    await session.execute(
                        select(func.count()).select_from(Member).where(Member.guild_id == interaction.guild.id, Member.xp > record.xp)
                    )
                ).scalar_one()
                total_members = (
                    await session.execute(
                        select(func.count()).select_from(Member).where(Member.guild_id == interaction.guild.id)
                    )
                ).scalar_one()
            rank_pos = int(higher) + 1
        except Exception:
            rank_pos = None
            total_members = None

        # next reward
        try:
            cfg = await self.get_config(interaction.guild.id)
            upcoming = [(int(lv), int(rid)) for lv, rid in cfg["role_rewards"].items() if int(lv) > level]
            upcoming.sort()
            next_reward = f"<@&{upcoming[0][1]}> at Lv {upcoming[0][0]}" if upcoming else None
        except Exception:
            next_reward = None

        color = target.color if isinstance(target, discord.Member) and target.color.value else LEVEL
        embed = discord.Embed(title=f"Level card — {target.display_name}", color=color)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Level", value=str(level))
        embed.add_field(name="Total XP", value=str(record.xp))
        embed.add_field(name="Coins", value=f"🪙 {record.balance}")
        if rank_pos is not None:
            embed.add_field(name="Rank", value=f"#{rank_pos}" + (f" / {total_members}" if total_members else ""))
        embed.add_field(name="Progress", value=f"{into}/{span} XP")
        if next_reward:
            embed.add_field(name="Next reward", value=next_reward)
        embed.description = f"{progress_bar(into, span)} `{into}/{span}`"
        style(embed, interaction, emoji=EMOJI["levels"])
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="leaderboard", description="Top 10 most active members")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        async with session_factory() as session:
            rows = (
                await session.execute(
                    select(Member)
                    .where(Member.guild_id == interaction.guild.id)
                    .order_by(Member.xp.desc())
                    .limit(10)
                )
            ).scalars().all()
        if not rows:
            await interaction.response.send_message("No one has earned XP yet.", ephemeral=True)
            return
        medals = ["🥇", "🥈", "🥉"]
        lines: list[str] = []
        for idx, row in enumerate(rows):
            member = interaction.guild.get_member(row.user_id)
            name = member.display_name if member else f"<@{row.user_id}>"
            mention = member.mention if member else f"<@{row.user_id}>"
            prefix = medals[idx] if idx < 3 else f"`#{idx + 1}`"
            # try to resolve name gracefully without pinging everyone
            lines.append(f"{prefix} {mention} — Lv {level_for(row.xp)} · {row.xp:,} XP · 🪙 {row.balance}")
        embed = discord.Embed(
            title=f"🏆 Leaderboard — {interaction.guild.name}",
            description="\n".join(lines),
            color=LEVEL,
        )
        # add requesting user's rank if not in top 10
        try:
            requester_id = interaction.user.id
            if requester_id not in {r.user_id for r in rows}:
                async with session_factory() as session:
                    requester = await session.execute(
                        select(Member).where(Member.guild_id == interaction.guild.id, Member.user_id == requester_id)
                    )
                    rec = requester.scalar_one_or_none()
                    if rec:
                        higher = (
                            await session.execute(
                                select(func.count()).select_from(Member).where(Member.guild_id == interaction.guild.id, Member.xp > rec.xp)
                            )
                        ).scalar_one()
                        embed.set_footer(text=f"Your rank: #{int(higher)+1} · Lv {level_for(rec.xp)} · {rec.xp:,} XP")
        except Exception:
            pass
        style(embed, interaction, emoji=EMOJI["levels"])
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LevelingCog(bot))
