import asyncio
from dataclasses import dataclass

YTDL_BASE = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
    "format": "bestaudio[acodec!=none]/best",
}


@dataclass
class Track:
    title: str
    webpage_url: str
    requester_id: int
    duration: int | None = None
    thumbnail: str | None = None
    stream_url: str | None = None


def _thumb_of(info: dict) -> str | None:
    thumbs = info.get("thumbnails") or []
    for candidate in reversed(thumbs):
        url = candidate.get("url")
        if url:
            return url
    return None


def _track_from_info(info: dict, requester_id: int) -> Track:
    return Track(
        title=info.get("title") or "Unknown title",
        webpage_url=info.get("webpage_url") or info.get("original_url") or "",
        requester_id=requester_id,
        duration=info.get("duration"),
        thumbnail=_thumb_of(info),
        stream_url=info.get("url"),
    )


import re as _re
from urllib.parse import urlparse as _urlparse

_ALLOWED_DOMAINS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be",
    "soundcloud.com", "www.soundcloud.com", "m.soundcloud.com",
    "open.spotify.com",
    "bandcamp.com",
}


def _is_safe_query(query: str) -> bool:
    """Reject raw URLs that point to non-whitelisted domains (SSRF mitigation)."""
    if not _re.match(r"https?://", query, _re.IGNORECASE):
        return True  # plain search text — safe
    try:
        parsed = _urlparse(query)
    except Exception:
        return False
    host = (parsed.hostname or "").lower().lstrip(".")
    # allow any subdomain of allowed domains (e.g. artist.bandcamp.com)
    return any(host == d or host.endswith(f".{d}") for d in _ALLOWED_DOMAINS)


def _sync_extract(query: str, playlist: bool, flat: bool) -> dict:
    import yt_dlp

    if not _is_safe_query(query):
        raise ValueError(f"URL domain not allowed: only YouTube, SoundCloud, Spotify, and Bandcamp links are supported.")
    opts = dict(YTDL_BASE)
    opts["noplaylist"] = not playlist
    if playlist and flat:
        opts["extract_flat"] = "in_playlist"
        opts["playlistend"] = 200
    with yt_dlp.YoutubeDL(opts) as ydl:
        data = ydl.extract_info(query, download=False)
        return ydl.sanitize_info(data)


async def extract(query: str, playlist: bool = False, flat: bool = False) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_extract, query, playlist, flat)


async def resolve_query(query: str, requester_id: int) -> list[Track]:
    info = await extract(query)
    if not info:
        return []
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        tracks = []
        for entry in entries:
            url = entry.get("webpage_url") or entry.get("url") or ""
            if not url.startswith("http"):
                continue
            tracks.append(
                Track(
                    title=entry.get("title") or "Unknown title",
                    webpage_url=url,
                    requester_id=requester_id,
                    duration=entry.get("duration"),
                    thumbnail=_thumb_of(entry),
                )
            )
        return tracks
    return [_track_from_info(info, requester_id)]


async def search(query: str, count: int = 5) -> list[Track]:
    loop = asyncio.get_running_loop()

    def run() -> dict:
        import yt_dlp

        opts = {
            "quiet": True,
            "no_warnings": True,
            "default_search": f"ytsearch{count}",
            "extract_flat": True,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(query, download=False)
            return ydl.sanitize_info(data)

    info = await loop.run_in_executor(None, run)
    results = []
    for entry in (info or {}).get("entries") or []:
        if not entry:
            continue
        results.append(
            Track(
                title=entry.get("title") or "Unknown title",
                webpage_url=entry.get("webpage_url") or entry.get("url") or "",
                requester_id=0,
                duration=entry.get("duration"),
            )
        )
    return results


async def ensure_stream(track: Track) -> None:
    if track.stream_url:
        return
    info = await extract(track.webpage_url)
    if info and not info.get("_type") == "playlist":
        track.stream_url = info.get("url")
        track.duration = track.duration or info.get("duration")


def format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "live"
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
