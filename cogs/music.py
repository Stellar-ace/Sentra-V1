import asyncio
import math
import pathlib
import random

import discord
from discord import app_commands
from discord.ext import commands, tasks

from sentra.db.engine import session_factory
from sentra.db.models import Guild
from sentra.music.player import GuildPlayer, PlayerManager
from sentra.music.tracks import Track, format_duration, resolve_query, search
from sentra.ui import EMOJI, MUSIC, style


def _load_opus() -> bool:
    if discord.opus.is_loaded():
        return True
    candidates = []
    pkg_bin = pathlib.Path(discord.__file__).parent / "bin"
    if pkg_bin.is_dir():
        candidates.extend(sorted(pkg_bin.glob("libopus*.dll"), reverse=True))
    for candidate in candidates:
        try:
            discord.opus.load_opus(str(candidate))
            return True
        except (OSError, discord.opus.OpusError):
            continue
    return False


OPUS_READY = _load_opus()

MUSIC_DEFAULTS = {"dj_role_id": None, "dj_only": False}


class NowPlayingView(discord.ui.View):
    """Persistent player controls attached to now-playing messages.

    ``timeout=None`` with fixed custom ids (registered via ``bot.add_view``),
    so buttons keep working after bot restarts — same pattern as AutoModPanel.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _cog(self, interaction: discord.Interaction) -> "MusicCog | None":
        cog = interaction.client.get_cog("MusicCog")
        if cog is None:
            await interaction.response.send_message("Music is unavailable right now.", ephemeral=True)
            return None
        return cog

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, custom_id="sentra:music-pause", row=0)
    async def pause_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cog = await self._cog(interaction)
        if cog:
            await cog.handle_pause_toggle(interaction, ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, custom_id="sentra:music-skip", row=0)
    async def skip_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cog = await self._cog(interaction)
        if cog:
            await cog.handle_skip(interaction, ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, custom_id="sentra:music-stop", row=0)
    async def stop_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cog = await self._cog(interaction)
        if cog:
            await cog.handle_stop(interaction, ephemeral=True)

    @discord.ui.button(label="Loop", style=discord.ButtonStyle.secondary, custom_id="sentra:music-loop", row=1)
    async def loop_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cog = await self._cog(interaction)
        if cog:
            await cog.handle_loop_cycle(interaction, ephemeral=True)

    @discord.ui.button(label="Shuffle", style=discord.ButtonStyle.secondary, custom_id="sentra:music-shuffle", row=1)
    async def shuffle_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cog = await self._cog(interaction)
        if cog:
            await cog.handle_shuffle(interaction, ephemeral=True)

    @discord.ui.button(label="Autoplay", style=discord.ButtonStyle.success, custom_id="sentra:music-autoplay", row=1)
    async def autoplay_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        cog = await self._cog(interaction)
        if cog:
            await cog.handle_autoplay_toggle(interaction, ephemeral=True)

class QueuePagerView(discord.ui.View):
    """Page controls for ``/queue`` — dies with the message after 3 minutes."""

    def __init__(self, cog: "MusicCog", guild_id: int, page: int) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.page = max(0, page)
        self.message: discord.Message | None = None

    def _total_pages(self, player: GuildPlayer) -> int:
        return max(1, math.ceil(len(player.queue) / 10))

    @discord.ui.button(emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def prev_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        player = self.cog.players.get(self.guild_id)
        if player is None:
            await interaction.response.edit_message(content="The player has stopped.", embed=None, view=None)
            return
        self.page = (self.page - 1) % self._total_pages(player)
        await interaction.response.edit_message(embed=self.cog.build_queue_embed(player, self.page), view=self)

    @discord.ui.button(emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        player = self.cog.players.get(self.guild_id)
        if player is None:
            await interaction.response.edit_message(content="The player has stopped.", embed=None, view=None)
            return
        self.page = (self.page + 1) % self._total_pages(player)
        await interaction.response.edit_message(embed=self.cog.build_queue_embed(player, self.page), view=self)

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass


class SearchSelectView(discord.ui.View):
    """Select-menu result picker for ``/search`` (expires in 2 minutes)."""

    def __init__(self, cog: "MusicCog", results: list[Track]) -> None:
        super().__init__(timeout=120)
        self.cog = cog
        self.results = results[:5]
        self.message: discord.Message | None = None
        options = [
            discord.SelectOption(
                label=(t.title[:100] or f"Result {i + 1}"),
                description=format_duration(t.duration) if t.duration else "Live stream",
                value=str(i),
            )
            for i, t in enumerate(self.results)
        ]
        select = discord.ui.Select(placeholder="Pick a track to queue…", options=options)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or []
        track = self.results[int(values[0])]
        player = await self.cog.ensure_connected(interaction)
        if player is None:
            return
        track.requester_id = interaction.user.id
        player.enqueue([track])
        await interaction.response.edit_message(content=f"➕ Queued **{track.title}**", embed=None, view=None)

    async def on_timeout(self) -> None:
        if self.message is None:
            return
        for child in self.children:
            if isinstance(child, discord.ui.Select):
                child.disabled = True
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass

class MusicCog(commands.Cog):
    """Sentra music — top-level slash commands with a premium playback flow."""

    dj = app_commands.Group(
        name="dj",
        description="DJ role settings",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.players = PlayerManager(bot)
        self.players.now_playing_view_factory = NowPlayingView
        bot.add_view(NowPlayingView())
        self._sweeper.start()

    async def cog_unload(self) -> None:
        self._sweeper.cancel()
        for guild_id in list(self.players.players):
            await self.players.destroy(guild_id, "cog unloaded")

    # ------------------------------------------------------------------
    # Configuration & permissions
    # ------------------------------------------------------------------

    async def music_config(self, guild_id: int) -> dict:
        async with session_factory() as session:
            guild = await session.get(Guild, guild_id)
            stored = dict((guild.config or {}).get("music", {})) if guild else {}
        return {**MUSIC_DEFAULTS, **stored}

    async def set_music_config(self, guild_id: int, key: str, value) -> None:
        async with session_factory() as session:
            guild = await session.get(Guild, guild_id)
            if guild is None:
                guild = Guild(id=guild_id)
                session.add(guild)
            full = dict(guild.config or {})
            cfg = dict(full.get("music", {}))
            cfg[key] = value
            full["music"] = cfg
            guild.config = full
            await session.commit()

    def is_dj(self, interaction: discord.Interaction, config: dict) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        if not config["dj_only"]:
            return True
        if member.guild_permissions.manage_guild:
            return True
        role_id = config["dj_role_id"]
        return bool(role_id and any(r.id == role_id for r in member.roles))

    async def _dj_gate(self, interaction: discord.Interaction) -> dict | None:
        """Gate DJ-sensitive commands when DJ-only mode is enabled."""
        config = await self.music_config(interaction.guild_id)
        if not self.is_dj(interaction, config):
            await interaction.response.send_message(
                "DJ-only mode is on — you need the DJ role or **Manage Server** to do that.",
                ephemeral=True,
            )
            return None
        return config

    async def _live_player(self, interaction: discord.Interaction, ephemeral: bool) -> GuildPlayer | None:
        player = self.players.get(interaction.guild_id)
        if player is None or player.voice_client() is None:
            await interaction.response.send_message("I'm not playing anything right now.", ephemeral=True)
            return None
        return player

    async def require_player(self, interaction: discord.Interaction) -> GuildPlayer | None:
        player = self.players.get(interaction.guild_id)
        if player is None or player.voice_client() is None:
            await interaction.response.send_message("I'm not playing anything right now.", ephemeral=True)
            return None
        return player

    async def ensure_connected(self, interaction: discord.Interaction) -> GuildPlayer | None:
        user = interaction.user
        assert isinstance(user, discord.Member)
        vc_channel = user.voice.channel if user.voice else None
        if vc_channel is None:
            await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
            return None
        existing = interaction.guild.voice_client
        if existing is None:
            try:
                await vc_channel.connect(self_deaf=True)
            except discord.HTTPException:
                await interaction.response.send_message("Could not join that channel — check my permissions.", ephemeral=True)
                return None
        elif existing.channel.id != vc_channel.id:
            await interaction.response.send_message(
                f"I'm already in **{existing.channel.name}** — join me there.",
                ephemeral=True,
            )
            return None
        player = self.players.get(interaction.guild_id)
        if player is None:
            player = self.players.create(interaction.guild_id)
        player.voice_channel_id = vc_channel.id
        if isinstance(interaction.channel, discord.TextChannel):
            player.text_channel_id = player.text_channel_id or interaction.channel.id
        return player
    # ------------------------------------------------------------------
    # Shared handlers (slash commands + persistent buttons)
    # ------------------------------------------------------------------

    @staticmethod
    async def _safe_send(interaction: discord.Interaction, message: str, *, ephemeral: bool = False) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(message, ephemeral=ephemeral)
        except (discord.NotFound, discord.HTTPException):
            pass

    async def handle_pause_toggle(self, interaction: discord.Interaction, *, ephemeral: bool = False) -> None:
        if await self._dj_gate(interaction) is None:
            return
        player = await self._live_player(interaction, ephemeral)
        if player is None:
            return
        vc = player.voice_client()
        if vc and vc.is_paused():
            vc.resume()
            await self._safe_send(interaction, "▶️ Resumed.", ephemeral=ephemeral)
        elif vc and vc.is_playing():
            vc.pause()
            await self._safe_send(interaction, "⏸️ Paused.", ephemeral=ephemeral)
        else:
            await self._safe_send(interaction, "Nothing is playing.", ephemeral=True)

    async def handle_skip(self, interaction: discord.Interaction, *, ephemeral: bool = False) -> None:
        player = await self._live_player(interaction, ephemeral)
        if player is None:
            return
        config = await self.music_config(interaction.guild_id)
        track = player.current
        can_force = (track and track.requester_id == interaction.user.id) or self.is_dj(interaction, config)
        if can_force or player.current is None:
            player.skip()
            await self._safe_send(interaction, "⏭️ Skipped.", ephemeral=ephemeral)
            return
        player.voters.add(interaction.user.id)
        needed = max(1, player.human_listeners() // 2)
        if len(player.voters) >= needed:
            player.skip()
            await self._safe_send(interaction, "🗳️ Vote passed — skipped.", ephemeral=ephemeral)
        else:
            await self._safe_send(
                interaction,
                f"🗳️ Vote registered ({len(player.voters)}/{needed} needed).",
                ephemeral=ephemeral,
            )

    async def handle_stop(self, interaction: discord.Interaction, *, ephemeral: bool = False) -> None:
        player = await self._live_player(interaction, ephemeral)
        if player is None:
            return
        config = await self.music_config(interaction.guild_id)
        track = player.current
        can_stop = (track and track.requester_id == interaction.user.id) or self.is_dj(interaction, config)
        if not can_stop:
            await self._safe_send(
                interaction,
                "Only the requester or a DJ can stop playback — use **Skip** to vote instead.",
                ephemeral=True,
            )
            return
        await self.players.destroy(interaction.guild_id, "stop command")
        await self._safe_send(interaction, "⏹️ Stopped — queue cleared and disconnected.", ephemeral=ephemeral)

    async def handle_loop_cycle(self, interaction: discord.Interaction, *, ephemeral: bool = False) -> None:
        if await self._dj_gate(interaction) is None:
            return
        player = await self._live_player(interaction, ephemeral)
        if player is None:
            return
        if not player.loop_track and not player.loop_queue:
            player.loop_track = True
            mode, icon = "Track", "🔁"
        elif player.loop_track:
            player.loop_track = False
            player.loop_queue = True
            mode, icon = "Queue", "🔂"
        else:
            player.loop_track = False
            player.loop_queue = False
            mode, icon = "Off", "➡️"
        await interaction.response.send_message(f"{icon} Loop mode: **{mode}**.", ephemeral=ephemeral)

    async def handle_shuffle(self, interaction: discord.Interaction, *, ephemeral: bool = False) -> None:
        if await self._dj_gate(interaction) is None:
            return
        player = await self._live_player(interaction, ephemeral)
        if player is None:
            return
        if not player.queue:
            await interaction.response.send_message("Nothing to shuffle.", ephemeral=True)
            return
        items = list(player.queue)
        random.shuffle(items)
        player.queue.clear()
        player.queue.extend(items)
        await interaction.response.send_message(f"🔀 Shuffled **{len(items)}** track(s).", ephemeral=ephemeral)

    async def handle_autoplay_toggle(self, interaction: discord.Interaction, *, ephemeral: bool = False) -> None:
        if await self._dj_gate(interaction) is None:
            return
        player = await self._live_player(interaction, ephemeral)
        if player is None:
            return
        player.autoplay = not player.autoplay
        state = "**on** — I'll keep the music going with related tracks." if player.autoplay else "**off**."
        await interaction.response.send_message(f"🧠 Autoplay: {state}", ephemeral=ephemeral)
    # ------------------------------------------------------------------
    # Lifecycle: empty-channel handling & cleanup
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        if member.bot:
            return
        player = self.players.get(member.guild.id)
        if player is None or player.voice_channel_id is None:
            return
        vc = player.voice_client()
        if vc is None or vc.channel is None:
            return
        if before.channel and before.channel.id == player.voice_channel_id and after.channel != before.channel:
            if not any(not m.bot for m in vc.channel.members) and not player.twentyfourseven:
                await asyncio.sleep(30)
                fresh = self.players.get(member.guild.id)
                if fresh is player and not player.twentyfourseven:
                    current_vc = player.voice_client()
                    if current_vc and current_vc.channel and not any(not m.bot for m in current_vc.channel.members):
                        await player._announce("⏹️ Everyone left the voice channel — stopping playback.")
                        await self.players.destroy(member.guild.id, "channel empty")

    @tasks.loop(seconds=60)
    async def _sweeper(self) -> None:
        """Clean up finished players — and make sure their voice clients die too."""
        for guild_id, player in list(self.players.players.items()):
            if player._task.done():
                await self.players.destroy(guild_id, "player finished")
    # ------------------------------------------------------------------
    # Embed builders
    # ------------------------------------------------------------------

    def build_np_embed(self, player: GuildPlayer) -> discord.Embed:
        track = player.current
        assert track is not None
        elapsed = player.elapsed()
        if track.duration:
            filled = int((min(elapsed, track.duration) / track.duration) * 14)
            bar = "▰" * filled + "▱" * (14 - filled)
            progress = f"`{format_duration(elapsed)} {bar} {format_duration(track.duration)}`"
        else:
            progress = f"`{format_duration(elapsed)} · 🔴 live`"
        embed = discord.Embed(title="🎵 Now playing", description=f"**{track.title}**", color=MUSIC)
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        embed.add_field(name="Requested by", value=f"<@{track.requester_id}>")
        if track.duration:
            embed.add_field(name="Duration", value=format_duration(track.duration))
        up_next = player.queue[0] if player.queue else None
        embed.add_field(name="Up next", value=up_next.title[:80] if up_next else "*Queue is empty*", inline=False)
        embed.description += f"\n\n{progress}"
        flags = []
        if player.loop_track:
            flags.append("Loop: Track")
        elif player.loop_queue:
            flags.append("Loop: Queue")
        if player.autoplay:
            flags.append("Autoplay")
        if player.twentyfourseven:
            flags.append("24/7")
        footer = f"Vol {int(player.volume)}% · Filters: {player.filters.describe()} · Queue: {len(player.queue)}"
        if flags:
            footer += " · " + " · ".join(flags)
        embed.set_footer(text=footer)
        return embed

    def build_queue_embed(self, player: GuildPlayer, page: int = 0) -> discord.Embed:
        per_page = 10
        total = len(player.queue)
        total_pages = max(1, math.ceil(total / per_page))
        page = min(max(0, page), total_pages - 1)
        lines = []
        if player.current:
            dur = format_duration(player.current.duration) if player.current.duration else "live"
            lines.append(f"**Now:** {player.current.title[:80]} `{format_duration(player.elapsed())} / {dur}`")
        if total == 0:
            lines.append("\n*Queue is empty — add something with `/play`.*")
        else:
            chunk = list(player.queue)[page * per_page:(page + 1) * per_page]
            lines.append(f"\n**Up next — {total} track(s)** · page {page + 1}/{total_pages}")
            for idx, t in enumerate(chunk, start=page * per_page + 1):
                tdur = format_duration(t.duration) if t.duration else "live"
                lines.append(f"`{idx}.` **{t.title[:70]}** `{tdur}` <@{t.requester_id}>")
            remaining = sum(t.duration or 0 for t in player.queue)
            lines.append(f"\n*Total queue time: {format_duration(remaining)}*")
        flags = []
        if player.loop_track:
            flags.append("Loop: Track")
        elif player.loop_queue:
            flags.append("Loop: Queue")
        if player.autoplay:
            flags.append("Autoplay")
        if player.twentyfourseven:
            flags.append("24/7")
        embed = discord.Embed(title="🎵 Queue", description="\n".join(lines)[:4000], color=MUSIC)
        if flags:
            embed.set_footer(text=" · ".join(flags))
        return embed
    # ------------------------------------------------------------------
    # Playback commands (top-level)
    # ------------------------------------------------------------------

    @app_commands.command(name="play", description="Play a song, playlist, or search query")
    @app_commands.describe(query="A URL, playlist link, or search text")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        if await self._dj_gate(interaction) is None:
            return
        await interaction.response.defer(thinking=True)
        player = await self.ensure_connected(interaction)
        if player is None:
            return
        was_idle = player.current is None and not player.queue
        try:
            tracks = await resolve_query(query, interaction.user.id)
        except Exception as exc:
            await interaction.followup.send(f"Could not resolve that: `{str(exc)[:200]}`")
            return
        if not tracks:
            await interaction.followup.send("No results found for that query.")
            return
        added = player.enqueue(tracks)
        if added == 0:
            await interaction.followup.send("Queue is full (500 tracks max).", ephemeral=True)
            return
        if len(tracks) > 1:
            total = sum(t.duration or 0 for t in tracks[:added])
            embed = discord.Embed(
                title="📃 Playlist queued",
                description=f"**{added}** track(s) from **{tracks[0].title}**",
                color=MUSIC,
            )
            if tracks[0].thumbnail:
                embed.set_thumbnail(url=tracks[0].thumbnail)
            embed.add_field(name="Queue length", value=str(len(player.queue)))
            if total:
                embed.add_field(name="Playlist length", value=format_duration(total))
            if was_idle:
                embed.add_field(name="Status", value="▶️ Starting now", inline=False)
            style(embed, interaction, emoji=EMOJI["music"])
            await interaction.followup.send(embed=embed)
            return
        track = tracks[0]
        embed = discord.Embed(title="➕ Added to queue", description=f"**{track.title}**", color=MUSIC)
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        if track.duration:
            embed.add_field(name="Duration", value=format_duration(track.duration))
        if was_idle:
            embed.add_field(name="Status", value="▶️ Playing now")
        else:
            embed.add_field(name="Position", value=f"#{len(player.queue)}")
        style(embed, interaction, emoji=EMOJI["music"])
        await interaction.followup.send(embed=embed)
    @app_commands.command(name="search", description="Search and pick a result from a menu")
    @app_commands.describe(query="What to search for")
    @app_commands.guild_only()
    async def search_cmd(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            results = await search(query)
        except Exception as exc:
            await interaction.followup.send(f"Search failed: `{str(exc)[:200]}`")
            return
        if not results:
            await interaction.followup.send("No results found.")
            return
        embed = discord.Embed(title="🔎 Search results", description="Pick a track from the menu below.", color=MUSIC)
        view = SearchSelectView(self, results)
        view.message = await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="pause", description="Pause the current track")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        if await self._dj_gate(interaction) is None:
            return
        player = await self._live_player(interaction, ephemeral=False)
        if player is None:
            return
        vc = player.voice_client()
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ Paused — use `/resume` or the pause button to continue.")
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume a paused track")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        if await self._dj_gate(interaction) is None:
            return
        player = await self._live_player(interaction, ephemeral=False)
        if player is None:
            return
        vc = player.voice_client()
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Resumed.")
        else:
            await interaction.response.send_message("Nothing is paused.", ephemeral=True)

    @app_commands.command(name="skip", description="Skip the current song (vote if you're not the requester)")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        await self.handle_skip(interaction)

    @app_commands.command(name="stop", description="Stop playback, clear the queue, and leave")
    @app_commands.guild_only()
    async def stop_cmd(self, interaction: discord.Interaction) -> None:
        await self.handle_stop(interaction)

    @app_commands.command(name="join", description="Bring the bot to your voice channel")
    @app_commands.guild_only()
    async def join(self, interaction: discord.Interaction) -> None:
        player = await self.ensure_connected(interaction)
        if player is None:
            return
        channel = interaction.user.voice.channel
        await interaction.response.send_message(f"👋 Connected to **{channel.name}**.")

    @app_commands.command(name="leave", description="Disconnect the bot and clear the queue")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction) -> None:
        player = self.players.get(interaction.guild_id)
        if player is None and interaction.guild.voice_client is None:
            await interaction.response.send_message("I'm not connected to a voice channel.", ephemeral=True)
            return
        await self.players.destroy(interaction.guild_id, "leave command")
        await interaction.response.send_message("👋 Disconnected — queue cleared.")
    @app_commands.command(name="nowplaying", description="Show the current track with live controls")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        player = await self._live_player(interaction, ephemeral=False)
        if player is None:
            return
        if player.current is None:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        embed = self.build_np_embed(player)
        style(embed, interaction, emoji=EMOJI["music"])
        await interaction.response.send_message(embed=embed, view=NowPlayingView())

    @app_commands.command(name="queue", description="Show the queue with page controls")
    @app_commands.guild_only()
    async def queue_cmd(self, interaction: discord.Interaction) -> None:
        player = await self._live_player(interaction, ephemeral=False)
        if player is None:
            return
        embed = self.build_queue_embed(player, 0)
        view = QueuePagerView(self, interaction.guild_id, 0)
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="loop", description="Set loop mode — omit the option to cycle Off → Track → Queue")
    @app_commands.describe(mode="Loop mode (omit to cycle)")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Off", value="off"),
        app_commands.Choice(name="Track", value="track"),
        app_commands.Choice(name="Queue", value="queue"),
    ])
    @app_commands.guild_only()
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str] | None = None) -> None:
        if await self._dj_gate(interaction) is None:
            return
        player = await self._live_player(interaction, ephemeral=False)
        if player is None:
            return
        if mode is None:
            await self.handle_loop_cycle(interaction)
            return
        player.loop_track = mode.value == "track"
        player.loop_queue = mode.value == "queue"
        icon = {"off": "➡️", "track": "🔁", "queue": "🔂"}[mode.value]
        await interaction.response.send_message(f"{icon} Loop mode: **{mode.name}**.")

    @app_commands.command(name="shuffle", description="Shuffle the queue")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction) -> None:
        if await self._dj_gate(interaction) is None:
            return
        await self.handle_shuffle(interaction)

    @app_commands.command(name="autoplay", description="Toggle autoplay — related tracks when the queue runs dry")
    @app_commands.describe(enabled="Leave empty to toggle")
    @app_commands.guild_only()
    async def autoplay(self, interaction: discord.Interaction, enabled: bool | None = None) -> None:
        if await self._dj_gate(interaction) is None:
            return
        player = await self._live_player(interaction, ephemeral=False)
        if player is None:
            return
        player.autoplay = (not player.autoplay) if enabled is None else enabled
        state = "**on** — I'll keep the music going with related tracks." if player.autoplay else "**off**."
        await interaction.response.send_message(f"🧠 Autoplay: {state}")
    @app_commands.command(name="volume", description="Set playback volume (0-150%)")
    @app_commands.describe(level="Volume percentage")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 150]) -> None:
        if await self._dj_gate(interaction) is None:
            return
        player = await self._live_player(interaction, ephemeral=False)
        if player is None:
            return
        player.volume = float(level)
        vc = player.voice_client()
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = level / 100.0
        await interaction.response.send_message(f"🔊 Volume set to **{level}%**.")

    @app_commands.command(name="seek", description="Jump to a position in the current track")
    @app_commands.describe(seconds="Position in seconds to jump to")
    @app_commands.guild_only()
    async def seek(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 36000]) -> None:
        if await self._dj_gate(interaction) is None:
            return
        player = await self._live_player(interaction, ephemeral=False)
        if player is None:
            return
        duration = player.current.duration if player.current else 0
        if duration and seconds > duration:
            seconds = max(0, duration - 5)
        ok = await player.seek(seconds)
        if not ok:
            await interaction.response.send_message("Could not seek — try again in a moment.", ephemeral=True)
            return
        await interaction.response.send_message(f"⏩ Jumped to **{format_duration(seconds)}**.")
    @app_commands.command(name="filters", description="Toggle audio filters live")
    @app_commands.describe(bassboost="Deep low-end boost", nightcore="Sped-up pitch shift", karaoke="Vocal reduction")
    @app_commands.guild_only()
    async def filters(
        self,
        interaction: discord.Interaction,
        bassboost: bool | None = None,
        nightcore: bool | None = None,
        karaoke: bool | None = None,
    ) -> None:
        if await self._dj_gate(interaction) is None:
            return
        player = await self._live_player(interaction, ephemeral=False)
        if player is None:
            return
        changed = False
        if bassboost is not None and bassboost != player.filters.bassboost:
            player.filters.bassboost = bassboost
            changed = True
        if nightcore is not None and nightcore != player.filters.nightcore:
            player.filters.nightcore = nightcore
            changed = True
        if karaoke is not None and karaoke != player.filters.karaoke:
            player.filters.karaoke = karaoke
            changed = True
        if changed:
            applied = await player.refilter()
            note = " (restarted current track)" if applied and player.current else ""
        else:
            note = ""
        await interaction.response.send_message(f"🎚️ Filters: **{player.filters.describe()}**{note}")

    @app_commands.command(name="247", description="Toggle 24/7 mode — stay connected even when idle")
    @app_commands.describe(enabled="Leave empty to toggle")
    @app_commands.guild_only()
    async def twentyfourseven(self, interaction: discord.Interaction, enabled: bool | None = None) -> None:
        if await self._dj_gate(interaction) is None:
            return
        player = await self._live_player(interaction, ephemeral=False)
        if player is None:
            return
        player.twentyfourseven = (not player.twentyfourseven) if enabled is None else enabled
        state = (
            "**on** — I'll stay connected and keep playing even when the room empties."
            if player.twentyfourseven
            else "**off** — I'll leave when the queue is idle or everyone leaves."
        )
        await interaction.response.send_message(f"🕰️ 24/7 mode is now {state}")

    @app_commands.command(name="remove", description="Remove a queued track by position")
    @app_commands.describe(position="Position in the queue (see /queue)")
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, 500]) -> None:
        if await self._dj_gate(interaction) is None:
            return
        player = await self._live_player(interaction, ephemeral=False)
        if player is None:
            return
        if position > len(player.queue):
            await interaction.response.send_message("That position doesn't exist.", ephemeral=True)
            return
        track = list(player.queue)[position - 1]
        del player.queue[position - 1]
        await interaction.response.send_message(f"🗑️ Removed **{track.title}**.")

    @app_commands.command(name="clear", description="Clear the entire queue")
    @app_commands.guild_only()
    async def clear(self, interaction: discord.Interaction) -> None:
        if await self._dj_gate(interaction) is None:
            return
        player = await self._live_player(interaction, ephemeral=False)
        if player is None:
            return
        count = len(player.queue)
        player.queue.clear()
        await interaction.response.send_message(f"🗑️ Cleared **{count}** track(s).")
    # ------------------------------------------------------------------
    # DJ configuration
    # ------------------------------------------------------------------

    @dj.command(name="role", description="Set or clear the DJ role")
    @app_commands.describe(role="Leave empty to clear")
    async def dj_role(self, interaction: discord.Interaction, role: discord.Role | None = None) -> None:
        await self.set_music_config(interaction.guild_id, "dj_role_id", role.id if role else None)
        await interaction.response.send_message(f"DJ role: {role.mention if role else '*cleared*'}", ephemeral=True)

    @dj.command(name="only", description="Restrict music controls to the DJ role")
    @app_commands.describe(enabled="Require the DJ role for playback commands")
    async def dj_only(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self.set_music_config(interaction.guild_id, "dj_only", enabled)
        state = "on" if enabled else "off"
        await interaction.response.send_message(f"DJ-only mode: **{state}**.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MusicCog(bot))