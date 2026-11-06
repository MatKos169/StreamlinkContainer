"""Global settings from environment variables.

Per-channel settings (channel, output path, quality) live in streams.json and
are edited in the web UI. Everything here is container-wide.
"""
import ipaddress
import os
from dataclasses import dataclass, field

from .streams import Options

DEFAULT_ALLOWED_NETWORKS = "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"


class SettingsError(ValueError):
    pass


def env(*names, default=None):
    """First non-empty value among the given env variable names."""
    for name in names:
        value = os.environ.get(name)
        if value not in (None, ""):
            return value
    return default


def parse_int(value, name, minimum):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise SettingsError(f"{name} must be a whole number") from None
    if number < minimum:
        raise SettingsError(f"{name} must be {minimum} or more")
    return number


def parse_networks(value):
    """Parse a comma-separated list of IPs/CIDRs ("10.0.0.0/24,192.168.1.5")."""
    networks = []
    for entry in (value or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            raise SettingsError(f"ALLOWED_NETWORKS: invalid entry {entry!r}") from None
    if not networks:
        raise SettingsError("ALLOWED_NETWORKS must contain at least one IP or network")
    return networks


@dataclass
class Settings:
    config_dir: str = "/config"
    recordings_dir: str = "/recordings"
    work_dir: str = "/work"
    # Defaults for the options that can be changed in the web UI.
    defaults: Options = field(default_factory=Options)
    client_id: str = None
    client_secret: str = None
    oauth_token: str = None
    allowed_networks: list = field(default_factory=lambda: parse_networks(DEFAULT_ALLOWED_NETWORKS))
    web_port: int = 8080

    @property
    def streams_file(self):
        return os.path.join(self.config_dir, "streams.json")

    @property
    def status_file(self):
        return os.path.join(self.config_dir, "status.json")


def load_defaults():
    """Option defaults from env (CHECK_INTERVAL, DEFAULT_QUALITY, RESCAN_INTERVAL, RESCAN_DURATION)."""
    try:
        return Options().merged({
            "check_interval": env("CHECK_INTERVAL", "timer"),
            "default_quality": env("DEFAULT_QUALITY", "quality"),
            "rescan_interval": env("RESCAN_INTERVAL"),
            "rescan_duration": env("RESCAN_DURATION"),
            "folder_layout": env("FOLDER_LAYOUT"),
            "keep_source": env("KEEP_SOURCE"),
            "done_retention_days": env("DONE_RETENTION_DAYS"),
            "audio_normalize": env("AUDIO_NORMALIZE"),
            "video_codec": env("VIDEO_CODEC"),
            "ffmpeg_args": env("FFMPEG_ARGS"),
            "vod_mode": env("VOD_MODE"),
            "vod_speed": env("VOD_SPEED"),
            "vod_segment_threads": env("VOD_SEGMENT_THREADS"),
            "vod_parallel": env("VOD_PARALLEL"),
            "max_parallel_streams": env("MAX_PARALLEL_STREAMS"),
            "slot_cooldown": env("SLOT_COOLDOWN"),
        })
    except ValueError as error:
        raise SettingsError(str(error)) from None


def load_settings(require_twitch=False):
    settings = Settings(
        config_dir=env("CONFIG_DIR", default="/config"),
        recordings_dir=env("RECORDINGS_DIR", default="/recordings"),
        work_dir=env("WORK_DIR", default="/work"),
        defaults=load_defaults(),
        client_id=env("TWITCH_CLIENT_ID", "clientid"),
        client_secret=env("TWITCH_CLIENT_SECRET", "clientsecret"),
        oauth_token=env("TWITCH_OAUTH_TOKEN", "TWITCH_AUTH_TOKEN", "oauthtoken"),
        allowed_networks=parse_networks(env("ALLOWED_NETWORKS", default=DEFAULT_ALLOWED_NETWORKS)),
        web_port=parse_int(env("WEB_PORT", default=8080), "WEB_PORT", 1),
    )
    if require_twitch and not (settings.client_id and settings.client_secret):
        raise SettingsError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are required")
    return settings
