"""Channel list (streams.json): shared between the web UI (writer) and the recorder (reader)."""
import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass

from .naming import clean_output_path

_LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{1,25}$")
_URL_RE = re.compile(r"^(?:https?://)?(?:www\.|m\.)?twitch\.tv/([^/?#]+)", re.IGNORECASE)
_QUALITY_RE = re.compile(r"^[A-Za-z0-9_,\s]{0,100}$")


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


def load_streams(path):
    """Read streams.json. A missing file means no channels; a broken one raises ValueError."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as error:
        raise ValueError(f"{path} is not valid JSON: {error}") from None
    entries = data.get("streams", []) if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{path}: 'streams' must be a list")
    return [StreamConfig.from_input(entry) for entry in entries]


def save_streams(path, streams):
    """Write streams.json atomically so the recorder never reads a half-written file."""
    write_json_atomic(path, {"streams": [asdict(s) for s in streams]})


def write_json_atomic(path, data):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
