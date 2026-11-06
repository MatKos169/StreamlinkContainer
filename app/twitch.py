"""Twitch Helix: which of the configured channels are live, with category and title."""
import asyncio
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

TWITCH_CALL_TIMEOUT = 30
MAX_LOGINS_PER_CALL = 100
_AUTH_STATUS_RE = re.compile(r"Authentication failed with code (\d{3})\b")


def error_summary(exc):
    """Describe a Twitch error without its message, which can embed the client secret."""
    summary = type(exc).__name__
    try:
        from twitchAPI.type import TwitchAuthorizationException
        if isinstance(exc, TwitchAuthorizationException):
            match = _AUTH_STATUS_RE.match(str(exc))
            if match:
                summary += f" (HTTP {match.group(1)})"
    except ImportError:
        pass
    return summary


@dataclass(frozen=True)
class LiveInfo:
    login: str
    title: str
    category_id: str
    category: str


class TwitchClient:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self._twitch = None
        self._loop = asyncio.new_event_loop()

    async def _fetch(self, logins):
        from twitchAPI.twitch import Twitch
        if self._twitch is None:
            self._twitch = await Twitch(self.client_id, self.client_secret)
        live = {}
        for i in range(0, len(logins), MAX_LOGINS_PER_CALL):
            chunk = logins[i:i + MAX_LOGINS_PER_CALL]
            async for stream in self._twitch.get_streams(user_login=chunk, first=MAX_LOGINS_PER_CALL):
                if getattr(stream, "type", "live") != "live":
                    continue
                login = stream.user_login.lower()
                live[login] = LiveInfo(login, stream.title or "", stream.game_id or "", stream.game_name or "")
        return live

    async def _fetch_video(self, video_id):
        from twitchAPI.twitch import Twitch
        if self._twitch is None:
            self._twitch = await Twitch(self.client_id, self.client_secret)
        # Helix rejects "first" together with ids; twitchAPI defaults it to 20.
        async for video in self._twitch.get_videos(ids=[video_id], first=None):
            return video
        return None

    def get_video(self, video_id):
        """Helix metadata of a VOD: {"title", "login", "created_at" (aware datetime), "duration" (s)}."""
        from .vod import parse_twitch_duration
        try:
            video = self._loop.run_until_complete(asyncio.wait_for(self._fetch_video(video_id), TWITCH_CALL_TIMEOUT))
        except BaseException:
            self._twitch = None
            raise
        if video is None:
            return {}
        return {"title": video.title or "", "login": (video.user_login or "").lower(),
                "created_at": video.created_at, "duration": parse_twitch_duration(video.duration)}

    def get_live(self, logins):
        """Return {login: LiveInfo} for the live channels among logins. Raises on API errors."""
        if not logins:
            return {}
        try:
            return self._loop.run_until_complete(asyncio.wait_for(self._fetch(list(logins)), TWITCH_CALL_TIMEOUT))
        except BaseException:
            # twitchAPI can leave its token-refresh lock set after a failure; start fresh next time.
            self._twitch = None
            raise
