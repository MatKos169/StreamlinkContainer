"""Channel list (streams.json): shared between the web UI (writer) and the recorder (reader)."""
import json
import os
import re
import shlex
import tempfile
import uuid
from dataclasses import asdict, dataclass, field

from .naming import FOLDER_LAYOUTS, clean_output_path

_LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")
_URL_RE = re.compile(r"^(?:https?://)?(?:www\.|m\.)?twitch\.tv/([^/?#]+)", re.IGNORECASE)
_QUALITY_RE = re.compile(r"^[A-Za-z0-9_,\s]{0,100}$")
_CODEC_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
FFMPEG_ARGS_MAX = 500
# Options that would break the converter: extra inputs, progress reporting, interactive prompts.
FFMPEG_ARGS_BLOCKED = {"-i", "-progress", "-stdin", "-nostdin", "-y", "-n"}


def parse_ffmpeg_args(text):
    """Split user-entered extra ffmpeg output options like a shell would (no shell is used)."""
    text = (text or "").strip()
    if len(text) > FFMPEG_ARGS_MAX:
        raise ValueError(f"ffmpeg_args may be at most {FFMPEG_ARGS_MAX} characters")
    try:
        args = shlex.split(text)
    except ValueError as error:
        raise ValueError(f"ffmpeg_args: {error}") from None
    blocked = sorted(set(args) & FFMPEG_ARGS_BLOCKED)
    if blocked:
        raise ValueError(f"ffmpeg_args must not contain {', '.join(blocked)}")
    return args


def parse_channel(value):
    """Accept "name", "twitch.tv/name" or "https://www.twitch.tv/name" and return the login."""
    value = (value or "").strip()
    match = _URL_RE.match(value)
    login = match.group(1) if match else value
    if not _LOGIN_RE.match(login):
        raise ValueError("channel must be a Twitch login or a twitch.tv URL")
    return login.lower()


@dataclass
class StreamConfig:
    id: str
    channel: str
    output: str
    quality: str = ""
    enabled: bool = True

    @classmethod
    def from_input(cls, data, stream_id=None):
        """Validate user input (web API or streams.json) into a StreamConfig."""
        if not isinstance(data, dict):
            raise ValueError("stream must be an object")
        channel = parse_channel(data.get("channel"))
        output = clean_output_path(data.get("output") or channel)
        if not output:
            raise ValueError("output path is empty")
        quality = str(data.get("quality") or "").strip()
        if not _QUALITY_RE.match(quality):
            raise ValueError("quality may only list stream names like '1080p60,720p,best'")
        enabled = data.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be true or false")
        return cls(id=stream_id or data.get("id") or uuid.uuid4().hex[:12],
                   channel=channel, output=output, quality=quality, enabled=enabled)


# Recorder options editable in the web UI: (min, max) for each number.
OPTION_LIMITS = {
    "check_interval": (10, 3600),
    "rescan_interval": (5, 600),
    "rescan_duration": (0, 3600),   # 0 disables the quick re-check after a stream ends
    "done_retention_days": (0, 3650),   # 0 keeps originals in /work/done forever
    "vod_segment_threads": (1, 10),
    "vod_parallel": (1, 5),
    "max_parallel_streams": (0, 100),   # live recordings + VOD downloads; 0 = no limit
    "slot_cooldown": (0, 3600),         # seconds a channel keeps its slot after a stream end
}
VOD_MODES = ("realtime", "download")
VOD_SPEED_LIMITS = (0.25, 20.0)


@dataclass
class Options:
    """Recorder options. Env values are the defaults; the web UI stores overrides in streams.json."""
    check_interval: int = 60
    default_quality: str = "best"
    rescan_interval: int = 10
    rescan_duration: int = 120
    folder_layout: str = "output_year_month"   # see naming.FOLDER_LAYOUTS
    keep_source: bool = True            # keep the .ts in /work/done after converting
    done_retention_days: int = 0        # recorder deletes originals in /work/done older than this (0 = never)
    audio_normalize: bool = True        # loudnorm to -14 LUFS, audio re-encoded as AAC 192k
    video_codec: str = "copy"           # ffmpeg -c:v value; "copy" = no re-encoding
    ffmpeg_args: str = ""               # extra ffmpeg output options, appended last (they win)
    vod_mode: str = "realtime"          # realtime: paced to playback speed; download: as fast as possible
    vod_speed: float = 1.0              # realtime pace (1.0 = playback speed)
    vod_segment_threads: int = 3        # parallel segment downloads in download mode
    vod_parallel: int = 1               # VOD downloads at the same time
    max_parallel_streams: int = 0       # Twitch streams at once (recordings + VODs); VODs only start below it
    slot_cooldown: int = 120            # after a stream end its slot stays reserved this long (0 = not at all)

    def merged(self, overrides):
        """Return a copy with validated overrides applied (None values are ignored)."""
        if not isinstance(overrides, dict):
            raise ValueError("settings must be an object")
        values = asdict(self)
        for key, value in overrides.items():
            if key not in values:
                raise ValueError(f"unknown setting {key!r}")
            if value is None:
                continue
            if key == "default_quality":
                value = str(value).strip() or "best"
                if not _QUALITY_RE.match(value):
                    raise ValueError("default_quality may only list stream names like '1080p60,720p,best'")
            elif key == "folder_layout":
                if value not in FOLDER_LAYOUTS:
                    raise ValueError(f"folder_layout must be one of {', '.join(FOLDER_LAYOUTS)}")
            elif key in ("keep_source", "audio_normalize"):
                value = _parse_bool(value, key)
            elif key == "vod_mode":
                if value not in VOD_MODES:
                    raise ValueError(f"vod_mode must be one of {', '.join(VOD_MODES)}")
            elif key == "vod_speed":
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    raise ValueError("vod_speed must be a number") from None
                if isinstance(value, bool) or not VOD_SPEED_LIMITS[0] <= value <= VOD_SPEED_LIMITS[1]:
                    raise ValueError(f"vod_speed must be between {VOD_SPEED_LIMITS[0]:g} and {VOD_SPEED_LIMITS[1]:g}")
            elif key == "video_codec":
                value = str(value).strip() or "copy"
                if not _CODEC_RE.match(value):
                    raise ValueError("video_codec must be an ffmpeg codec name like copy, libx264 or libx265")
            elif key == "ffmpeg_args":
                value = str(value).strip()
                parse_ffmpeg_args(value)
            else:
                if isinstance(value, bool):
                    raise ValueError(f"{key} must be a whole number")
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    raise ValueError(f"{key} must be a whole number") from None
                low, high = OPTION_LIMITS[key]
                if not low <= value <= high:
                    unit = "days" if key.endswith("_days") else "seconds"
                    raise ValueError(f"{key} must be between {low} and {high} {unit}")
            values[key] = value
        return Options(**values)


def _parse_bool(value, key):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{key} must be true or false")


@dataclass
class Config:
    """Content of streams.json: option overrides from the UI plus the channel list."""
    settings: dict = field(default_factory=dict)
    streams: list = field(default_factory=list)

    def options(self, defaults):
        return defaults.merged(self.settings)


def load_config(path):
    """Read streams.json. A missing file means no channels; a broken one raises ValueError."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return Config()
    except json.JSONDecodeError as error:
        raise ValueError(f"{path} is not valid JSON: {error}") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain an object")
    entries = data.get("streams", [])
    if not isinstance(entries, list):
        raise ValueError(f"{path}: 'streams' must be a list")
    settings = data.get("settings") or {}
    Options().merged(settings)  # validate
    return Config(settings={k: v for k, v in settings.items() if v is not None},
                  streams=[StreamConfig.from_input(entry) for entry in entries])


def save_config(path, config):
    """Write streams.json atomically so the recorder never reads a half-written file."""
    write_json_atomic(path, {"settings": config.settings, "streams": [asdict(s) for s in config.streams]})


def write_json_atomic(path, data, create_dirs=True):
    directory = os.path.dirname(path) or "."
    if create_dirs:
        os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        # mkstemp creates 0600; use normal file permissions (umask applies) so other
        # containers/hosts with a different uid can read the file.
        umask = os.umask(0)
        os.umask(umask)
        os.chmod(tmp, 0o666 & ~umask)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
