import asyncio
import logging
import shutil
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import discord

from sentra.music.tracks import Track, ensure_stream, format_duration, search
from sentra.ui import MUSIC, WARN

log = logging.getLogger("sentra.music")

IDLE_TIMEOUT_S = 300


class FFmpegUnavailable(RuntimeError):
    pass


def ffmpeg_executable() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise FFmpegUnavailable(
            "FFmpeg is required for playback but was not found."
        ) from exc


@dataclass
class Filters:
    bassboost: bool = False
    nightcore: bool = False
    karaoke: bool = False

    def any_active(self) -> bool:
        return self.bassboost or self.nightcore or self.karaoke

    def describe(self) -> str:
        parts = []
        if self.bassboost:
            parts.append("Bass Boost")
        if self.nightcore:
            parts.append("Nightcore")
        if self.karaoke:
            parts.append("Karaoke")
        return ", ".join(parts) if parts else "None"


def build_source(stream_url: str, filters: Filters, volume: float, seek: int = 0):
    before = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
    if seek > 0:
        before += f" -ss {seek}"
    chains = []
    if filters.bassboost:
        chains.append("bass=g=6")
    if filters.nightcore:
        chains.append("asetrate=44100*1.25,aresample=44100,atempo=1.07")
    if filters.karaoke:
        chains.append("karaoke=f=-0.12")
    options = "-vn"
    if chains:
        options += f" -af {','.join(chains)}"
    pcm = discord.FFmpegPCMAudio(
        stream_url,
        executable=ffmpeg_executable(),
        before_options=before,
        options=options,
    )
    return discord.PCMVolumeTransformer(pcm, volume=max(0.0, min(volume / 100.0, 2.0)))


class GuildPlayer:
    def __init__(self, bot: discord.Client, guild_id: int) -> None:
        self.bot = bot
        self.guild_id = guild_id
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.filters = Filters()
        self.volume = 80.0
        self.loop_track = False
        self.loop_queue = False
        self.twentyfourseven = False
        self.autoplay = False
        self._history: deque[str] = deque(maxlen=50)
        self.announce_view_factory: Callable[[], discord.ui.View] | None = None
        self.text_channel_id: int | None = None
        self.voice_channel_id: int | None = None
        self.started_at: float | None = None
        self.voters: set[int] = set()
        self._done = asyncio.Event()
        self._wake = asyncio.Event()
        self._generation = 0
        self._task = asyncio.create_task(self.run())

    def voice_client(self) -> discord.VoiceClient | None:
        guild = self.bot.get_guild(self.guild_id)
        vc = guild.voice_client if guild else None
        return vc if isinstance(vc, discord.VoiceClient) else None

    def human_listeners(self) -> int:
        vc = self.voice_client()
        if vc is None or vc.channel is None:
            return 0
        return sum(1 for m in vc.channel.members if not m.bot)

    def elapsed(self) -> int:
        if self.started_at is None:
            return 0
        factor = 1.3 if self.filters.nightcore else 1.0
        return int((time.monotonic() - self.started_at) * factor)

    async def run(self) -> None:
        await self.bot.wait_until_ready()
        try:
            while True:
                track = await self._next_track()
                if track is None:
                    if await self._wait_idle():
                        await self._farewell()
                        break
                    continue
                started = await self._start(track)
                if not started:
                    self.current = None
                    continue
                self._done.clear()
                await self._done.wait()
        except asyncio.CancelledError:
            pass

    async def _farewell(self) -> None:
        """The player idled out (or the room emptied) — announce and leave."""
        vc = self.voice_client()
        if vc is None or not vc.is_connected():
            return
        await self._announce(
            "👋 Nothing left to play and nobody's listening — disconnecting. "
            "Use `/play` to start the party again."
        )
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass

    async def _next_track(self) -> Track | None:
        if self.current is not None and (self.loop_track or self.loop_queue):
            replay = self.current
            self.current = None
            self.voters.clear()
            if self.loop_queue:
                self.queue.appendleft(replay)
            else:
                return replay
            self.current = self.queue.popleft() if self.queue else None
            return self.current
        self.current = self.queue.popleft() if self.queue else None
        self.voters.clear()
        if self.current is None and self.autoplay:
            await self._fill_autoplay()
            self.current = self.queue.popleft() if self.queue else None
        return self.current

    async def _fill_autoplay(self) -> bool:
        """Queue a related track when autoplay is on and the queue ran dry."""
        seed_title = self.current.title if self.current else (self._history[-1] if self._history else None)
        if not seed_title:
            return False
        try:
            candidates = await search(f"{seed_title} similar song", count=8)
        except Exception as exc:
            log.warning("autoplay resolve failed: %s", exc)
            return False
        seen = set(self._history)
        if self.current is not None:
            seen.add(self.current.webpage_url)
        seen.update(t.webpage_url for t in self.queue)
        requester_id = self.bot.user.id if self.bot.user else 0
        for candidate in candidates:
            if not candidate.webpage_url or candidate.webpage_url in seen:
                continue
            candidate.requester_id = requester_id
            self.queue.append(candidate)
            self._wake.set()
            await self._announce(f"🧠 Autoplay queued **{candidate.title}**")
            return True
        return False

    async def _wait_idle(self) -> bool:
        waited = 0.0
        while True:
            if self.queue:
                return False
            vc = self.voice_client()
            if vc is None or not vc.is_connected():
                return True
            if not self.twentyfourseven:
                channel = vc.channel
                if channel is None or not any(not m.bot for m in channel.members):
                    return True
            if waited >= IDLE_TIMEOUT_S:
                # Idle too long with an empty queue — leave unless 24/7 is on.
                return not self.twentyfourseven
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5.0)
                self._wake.clear()
                return False
            except asyncio.TimeoutError:
                waited += 5.0

    async def _start(self, track: Track, seek: int | None = None) -> bool:
        vc = self.voice_client()
        if vc is None or not vc.is_connected():
            return False
        try:
            await ensure_stream(track)
        except Exception as exc:
            log.warning("stream resolve failed: %s", exc)
            await self._announce(f"<:sentra_warn:1546486928096890951> Could not load **{track.title}** — skipping.")
            return False
        if not track.stream_url:
            return False
        offset = self.elapsed() if seek is None else max(0, seek)
        self._generation += 1
        generation = self._generation
        if vc.is_playing() or vc.is_paused():
            # Bump the generation first so the old source's callback can't
            # mark playback as finished, then stop it (filter changes / seeks).
            vc.stop()
        self.current = track
        if seek is None:
            self._history.append(track.webpage_url)
        self.started_at = time.monotonic() - (offset / (1.3 if self.filters.nightcore else 1.0))

        def after_playback(error: Exception | None) -> None:
            if error:
                log.warning("playback error: %s", error)

            def finish() -> None:
                if generation == self._generation:
                    self._done.set()

            self.bot.loop.call_soon_threadsafe(finish)

        source = build_source(track.stream_url, self.filters, self.volume, seek=offset)
        try:
            vc.play(source, after=after_playback)
        except Exception as exc:
            log.exception("failed to start playback")
            await self._announce(f"<:sentra_warn:1546486928096890951> Failed to play **{track.title}**: `{exc}`")
            return False
        if seek is None:
            embed = discord.Embed(
                title="▶️ Now playing",
                description=f"**{track.title}**\n{track.webpage_url}",
                color=MUSIC,
            )
            if track.thumbnail:
                embed.set_thumbnail(url=track.thumbnail)
            up_next = self.queue[0] if self.queue else None
            embed.add_field(
                name="Duration",
                value=format_duration(track.duration) if track.duration else "Live",
            )
            embed.add_field(name="Requested by", value=f"<@{track.requester_id}>")
            if up_next:
                embed.add_field(name="Up next", value=up_next.title[:80], inline=False)
            flags = []
            if self.loop_track:
                flags.append("Loop: Track")
            elif self.loop_queue:
                flags.append("Loop: Queue")
            if self.autoplay:
                flags.append("Autoplay")
            if self.twentyfourseven:
                flags.append("24/7")
            filters_note = f" · Filters: {self.filters.describe()}" if self.filters.any_active() else ""
            footer = f"Volume {int(self.volume)}% · Queue: {len(self.queue)}{filters_note}"
            if flags:
                footer += " · " + " · ".join(flags)
            embed.set_footer(text=footer)
            view = self.announce_view_factory() if self.announce_view_factory else None
            await self._announce_embed(embed, view=view)
        return True

    async def _announce_embed(self, embed: discord.Embed, view: discord.ui.View | None = None) -> None:
        if not self.text_channel_id:
            return
        guild = self.bot.get_guild(self.guild_id)
        channel = guild.get_channel(self.text_channel_id) if guild else None
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(embed=embed, view=view)
            except discord.HTTPException:
                pass

    async def _announce(self, text: str) -> None:
        if not self.text_channel_id:
            return
        guild = self.bot.get_guild(self.guild_id)
        channel = guild.get_channel(self.text_channel_id) if guild else None
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(text[:2000])
            except discord.HTTPException:
                pass

    def enqueue(self, tracks: list[Track]) -> int:
        added = 0
        for track in tracks:
            if len(self.queue) >= 500:
                break
            self.queue.append(track)
            added += 1
        if added:
            self._wake.set()
        return added

    def skip(self) -> None:
        vc = self.voice_client()
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        else:
            self._generation += 1
            self._done.set()

    async def refilter(self) -> bool:
        vc = self.voice_client()
        if vc is None or not (vc.is_playing() or vc.is_paused()) or self.current is None:
            return False
        seek = self.elapsed()
        if self.filters.nightcore and seek:
            seek = int(seek / 1.07)
        paused = vc.is_paused()
        ok = await self._start(self.current, seek=seek)
        if ok and paused:
            vc.pause()
        return ok

    async def seek(self, seconds: int) -> bool:
        """Restart the current track at the given offset (used by /seek)."""
        if self.current is None:
            return False
        return await self._start(self.current, seek=max(0, seconds))

    async def destroy(self, reason: str = "stopped") -> None:
        self.twentyfourseven = False
        self.queue.clear()
        self.current = None
        self._generation += 1
        self._done.set()
        if self._task is not asyncio.current_task():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        vc = self.voice_client()
        if vc:
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass


class PlayerManager:
    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}
        self.now_playing_view_factory: Callable[[], discord.ui.View] | None = None

    def get(self, guild_id: int) -> GuildPlayer | None:
        player = self.players.get(guild_id)
        if player and player._task.done():
            self.players.pop(guild_id, None)
            return None
        return player

    def create(self, guild_id: int) -> GuildPlayer:
        old = self.players.pop(guild_id, None)
        if old:
            old._task.cancel()
        player = GuildPlayer(self.bot, guild_id)
        player.announce_view_factory = self.now_playing_view_factory
        self.players[guild_id] = player
        return player

    async def destroy(self, guild_id: int, reason: str = "stopped") -> None:
        player = self.players.pop(guild_id, None)
        if player:
            await player.destroy(reason)
