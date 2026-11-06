"""VOD downloads: requests in /config/downloads, run by the recorder, then converted like recordings.

    /config/downloads/<id>.json         pending or running request (written by the web UI)
    /config/downloads/<id>.cancel       cancel marker (web UI); the recorder removes both
    /config/downloads/<id>.failed.json  request plus error (written by the recorder)

Modes:
- realtime (default): streamlink resolves the VOD (access token, variant
  playlist); the segments are then fetched one by one at playback speed
  (times vod_speed), using the playlist's segment durations. This spreads the
  bandwidth over the VOD's length. (Pacing only the reading would not help:
  streamlink fetches up to 20 segments ahead regardless of the reader.)
- download: streamlink's normal behaviour, as fast as possible, with
  vod_segment_threads parallel segment downloads.
"""
import datetime
import json
import logging
import os
import re
import threading
import time
import uuid

from . import jobs
from .naming import clean_output_path, destination_path
from .recording import (READ_SIZE, ChapterWriter, NoStreamsAvailable, conversion_settings, default_session_factory,
                        describe_error, select_quality, stream_extension)
from .streams import VOD_MODES as MODES
from .streams import write_json_atomic
from .twitch import LiveInfo

logger = logging.getLogger(__name__)

_VOD_URL_RE = re.compile(r"^(?:https?://)?(?:www\.|m\.)?twitch\.tv/(?:videos/|[A-Za-z0-9_]{1,25}/v(?:ideo)?/)(\d{1,15})(?:[/?#].*)?$",
                         re.IGNORECASE)
_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")

# Real-time pacing: how far reading may run ahead of playback time (keeps a buffer).
PACE_LEAD_SECONDS = 10
PTS_HZ = 90000
PTS_WRAP = 1 << 33
PTS_MAX_STEP = 10 * PTS_HZ   # bigger jumps are discontinuities, not playback time
SEGMENT_RETRIES = 4
SEGMENT_TIMEOUT = 30


def parse_vod_url(value):
    """Return the video id of a Twitch VOD URL ("https://www.twitch.tv/videos/123") or a bare id."""
    value = (value or "").strip()
    if value.isdigit() and len(value) <= 15:
        return value
    match = _VOD_URL_RE.match(value)
    if not match:
        raise ValueError("expected a Twitch VOD URL like https://www.twitch.tv/videos/123456789")
    return match.group(1)


def parse_twitch_duration(text):
    """Helix video duration ("3h2m1s") in seconds, or None."""
    match = _DURATION_RE.match(text or "")
    if not match or not any(match.groups()):
        return None
    h, m, s = (int(g or 0) for g in match.groups())
    return h * 3600 + m * 60 + s


# -- MPEG-TS clock ------------------------------------------------------------

class TsClock:
    """Media seconds seen so far in an MPEG-TS byte stream, from PES timestamps (PTS).

    Only the first PTS per chunk is looked at, which is cheap enough even at full
    download speed. Uses one PID (the first with a PTS). Packets split across
    chunks are carried over; after garbage it re-synchronizes on 0x47.
    """

    def __init__(self):
        self.seconds = 0.0
        self._pid = None
        self._last = None
        self._carry = b""

    def feed(self, data):
        buf = self._carry + data
        start = self._sync(buf)
        if start is None:
            self._carry = buf[-187:]
            return self.seconds
        usable = start + (len(buf) - start) // 188 * 188
        self._carry = buf[usable:]
        pts = self._first_pts(buf, start, usable)
        if pts is None:
            return self.seconds
        if self._last is None:
            self._last = pts
            return self.seconds
        step = (pts - self._last) % PTS_WRAP
        if step <= PTS_MAX_STEP:
            self.seconds += step / PTS_HZ
            self._last = pts
        elif step < PTS_WRAP - PTS_HZ:
            self._last = pts         # discontinuity: re-anchor without counting time
        # else: small step back (B-frame reordering): keep the max
        return self.seconds

    @staticmethod
    def _sync(buf):
        """Offset of the first packet start (0x47 followed by 0x47 one packet later, if visible)."""
        i = buf.find(b"\x47")
        while i != -1:
            if i + 188 >= len(buf) or buf[i + 188] == 0x47:
                return i
            i = buf.find(b"\x47", i + 1)
        return None

    def _first_pts(self, data, start, end):
        for i in range(start, end, 188):
            if data[i] != 0x47:
                return None          # lost sync; the next chunk re-synchronizes
            b1, b3 = data[i + 1], data[i + 3]
            if not b1 & 0x40:        # payload_unit_start_indicator
                continue
            pid = ((b1 & 0x1F) << 8) | data[i + 2]
            if self._pid is not None and pid != self._pid:
                continue
            payload = i + 4
            if b3 & 0x20:            # adaptation field
                payload += 1 + data[i + 4]
            if not b3 & 0x10 or payload + 14 > i + 188:
                continue
            if data[payload:payload + 3] != b"\x00\x00\x01" or not data[payload + 7] & 0x80:
                continue
            p = data[payload + 9:payload + 14]
            pts = ((p[0] >> 1) & 0x07) << 30 | p[1] << 22 | (p[2] >> 1) << 15 | p[3] << 7 | p[4] >> 1
            if self._pid is None:
                self._pid = pid
            return pts
        return None


class Pacer:
    """How long to wait so that reading does not run ahead of playback (times speed)."""

    def __init__(self, speed=1.0, lead=PACE_LEAD_SECONDS, clock=time.monotonic):
        self.speed = speed
        self.lead = lead
        self._clock = clock
        self._start = clock()

    def delay(self, media_seconds):
        allowed = media_seconds / self.speed - self.lead
        return max(allowed - (self._clock() - self._start), 0.0)


# -- requests in /config/downloads --------------------------------------------

def downloads_dir(config_dir):
    return os.path.join(config_dir, "downloads")


def _request_path(config_dir, request_id, suffix=".json"):
    if not jobs.valid_job_id(request_id):
        raise ValueError("invalid download id")
    return os.path.join(downloads_dir(config_dir), f"{request_id}{suffix}")


def create_request(config_dir, url, output="", quality="", mode=None, speed=None):
    video_id = parse_vod_url(url)
    if mode not in (None, "", *MODES):
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if speed not in (None, ""):
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            raise ValueError("speed must be a number") from None
        if not 0.25 <= speed <= 20:
            raise ValueError("speed must be between 0.25 and 20")
    else:
        speed = None
    output = clean_output_path(output) if output else ""
    quality = str(quality or "").strip()
    if not re.match(r"^[A-Za-z0-9_,\s]{0,100}$", quality):
        raise ValueError("quality may only list stream names like '1080p60,720p,best'")
    request = {
        "id": f"{time.strftime('%Y%m%d-%H%M%S')}-vod{video_id}-{uuid.uuid4().hex[:6]}",
        "video_id": video_id, "url": f"https://www.twitch.tv/videos/{video_id}",
        "output": output, "quality": quality, "mode": mode or None, "speed": speed,
        "created": time.time(),
    }
    write_json_atomic(_request_path(config_dir, request["id"]), request)
    return request


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def list_requests(config_dir):
    """(pending, failed) requests, oldest first; pending ones carry "cancel": True if marked."""
    folder = downloads_dir(config_dir)
    try:
        names = sorted(os.listdir(folder))
    except FileNotFoundError:
        return [], []
    pending, failed = [], []
    for name in names:
        if name.endswith(".failed.json"):
            data = _read(os.path.join(folder, name))
            if data:
                failed.append(data)
        elif name.endswith(".json") and not name.startswith("."):
            data = _read(os.path.join(folder, name))
            if data:
                data["cancel"] = os.path.exists(os.path.join(folder, f"{data['id']}.cancel"))
                pending.append(data)
    key = lambda r: (r.get("created") or 0, r.get("id"))
    return sorted(pending, key=key), sorted(failed, key=key)


def cancel_request(config_dir, request_id):
    """Mark a pending/running download as cancelled; returns False if unknown."""
    if not os.path.exists(_request_path(config_dir, request_id)):
        return False
    with open(_request_path(config_dir, request_id, ".cancel"), "w"):
        pass
    return True


def retry_failed(config_dir, request_id):
    path = _request_path(config_dir, request_id, ".failed.json")
    data = _read(path)
    if data is None:
        return False
    for key in ("error", "failed_at"):
        data.pop(key, None)
    write_json_atomic(_request_path(config_dir, request_id), data)
    os.remove(path)
    return True


def delete_failed(config_dir, request_id):
    try:
        os.remove(_request_path(config_dir, request_id, ".failed.json"))
        return True
    except FileNotFoundError:
        return False


def finish_request(config_dir, request, error=None):
    """Called by the recorder when a download ended: drop it, or keep it as failed."""
    for suffix in (".json", ".cancel"):
        try:
            os.remove(_request_path(config_dir, request["id"], suffix))
        except FileNotFoundError:
            pass
    if error:
        data = {k: v for k, v in request.items() if k != "cancel"}
        data.update(error=error, failed_at=time.time())
        write_json_atomic(_request_path(config_dir, request["id"], ".failed.json"), data)


# -- the download ------------------------------------------------------------

class VodDownload(threading.Thread):
    """Downloads one VOD into a job folder in /work and queues it for conversion."""

    def __init__(self, request, settings, options, channel_outputs=None, session_factory=default_session_factory,
                 metadata_fetcher=None, now=None, clock=time.monotonic):
        super().__init__(daemon=True, name=f"vod-{request.get('video_id')}")
        self.request = request
        self.settings = settings
        self.options = options
        self.channel_outputs = channel_outputs or {}
        self.mode = request.get("mode") or options.vod_mode
        self.speed = request.get("speed") or options.vod_speed
        self._session_factory = session_factory
        self._metadata_fetcher = metadata_fetcher
        self._now = now or datetime.datetime.now
        self._clock = clock
        self._stop_event = threading.Event()
        self._session = None
        self.cancelled = False
        self.state = "starting"
        self.error = None
        self.job_id = None
        self.file = None
        self.title = None
        self.channel = None
        self.duration = None
        self.media_seconds = 0.0
        self.bytes = 0
        self.started = clock()

    @property
    def progress(self):
        if self.duration:
            return min(self.media_seconds / self.duration, 1.0)
        return None

    def stop(self, cancel=False):
        self.cancelled = self.cancelled or cancel
        self._stop_event.set()

    def run(self):
        try:
            self._download()
        except Stopped:
            self.state = "cancelled" if self.cancelled else "stopped"
        except NoStreamsAvailable:
            self.state, self.error = "error", "VOD not found or not available (subscriber-only? set TWITCH_OAUTH_TOKEN)"
        except Exception as error:
            self.state, self.error = "error", describe_error(error)
        if self.error:
            logger.error("VOD %s failed: %s", self.request.get("video_id"), self.error)

    def _plugin_options(self):
        token = self.settings.oauth_token
        if not token:
            return None
        from streamlink.options import Options
        return Options({"api-header": [("Authorization", f"OAuth {token}")]})

    def _open(self):
        """Streams and streamlink's metadata (title, author, category) for the VOD."""
        session = self._session = self._session_factory()
        if self.mode == "download":
            session.set_option("stream-segment-threads", self.options.vod_segment_threads)
        _, plugin_class, url = session.resolve_url(self.request["url"])
        plugin = plugin_class(session, url, self._plugin_options())
        streams = plugin.streams()
        meta = {}
        for key, getter in (("title", "get_title"), ("channel", "get_author"), ("category", "get_category")):
            try:
                meta[key] = getattr(plugin, getter)()
            except Exception:
                meta[key] = None
        return streams, meta

    def _helix_metadata(self):
        if self._metadata_fetcher is None:
            return {}
        try:
            return self._metadata_fetcher(self.request["video_id"]) or {}
        except Exception as error:
            logger.warning("VOD %s: Helix lookup failed (%s), using streamlink's metadata",
                           self.request["video_id"], type(error).__name__)
            return {}

    def _download(self):
        helix = self._helix_metadata()
        streams, meta = self._open()
        if not streams:
            raise NoStreamsAvailable
        self.title = helix.get("title") or meta.get("title") or f"VOD {self.request['video_id']}"
        self.channel = (helix.get("login") or meta.get("channel") or "twitch").lower()
        self.duration = helix.get("duration")
        started = helix.get("created_at") or self._now()
        quality = select_quality(self.request.get("quality") or self.options.default_quality, streams)
        selected = streams[quality]
        # Playlist first: nothing is written to /work before this can fail or be cancelled.
        plan = self._realtime_plan(selected) if self.mode == "realtime" else None
        if plan is not None:
            self.duration = self.duration or plan.duration
        elif not self.duration:
            self.duration = self._playlist_duration(selected)   # for the progress bar
        extension = plan.extension if plan else stream_extension(selected)

        work = self.settings.work_dir
        self.job_id = jobs.new_job_id(f"vod{self.request['video_id']}", started)
        path = jobs.job_dir(work, "recording", self.job_id)
        os.makedirs(path)
        try:
            self._prepare_job(path, started, quality, meta)
        except BaseException:
            jobs.remove(path)
            raise
        part = os.path.join(path, f"video.{extension}.part")
        self._run_download(path, part, plan, selected, quality)

    def _prepare_job(self, path, started, quality, meta):
        output = self.request.get("output") or self.channel_outputs.get(self.channel) or self.channel
        destination = destination_path(output, self.options.folder_layout, started, self.title)
        category = meta.get("category") or ""
        jobs.write_job(path, {
            "id": self.job_id, "kind": "vod", "video_id": self.request["video_id"], "url": self.request["url"],
            "channel": self.channel, "title": self.title, "category": category, "quality": quality,
            "destination": destination, "keep_source": self.options.keep_source, **conversion_settings(self.options),
            "started_at": started.astimezone().isoformat(timespec="seconds"),
            "download_mode": self.mode, "download_speed": self.speed if self.mode == "realtime" else None,
        })
        # Helix has no per-VOD category changes, so a VOD gets one chapter (its category).
        chapters = ChapterWriter(os.path.join(path, jobs.CHAPTERS_FILE))
        chapters.add(LiveInfo(self.channel, self.title, category, category))
        chapters.close()
        self.file = destination

    def _run_download(self, path, part, plan, selected, quality):
        work = self.settings.work_dir
        destination = self.file
        self.state = "downloading"
        logger.info("VOD %s (%s, %s): %s -> %s", self.request["video_id"], quality,
                    f"realtime x{self.speed:g}, own segment fetching" if plan else
                    ("realtime x%g, paced reading" % self.speed if self.mode == "realtime" else
                     f"download, {self.options.vod_segment_threads} threads"), self.title, destination)
        failure = None
        try:
            with open(part, "wb") as out:
                if plan:
                    self._fetch_paced(plan, out)
                else:
                    self._read_stream(selected, out)
        except Exception as error:
            failure = error
        if self._stop_event.is_set() or failure is not None:
            # Cancelled, recorder stopping or failed: drop the partial file. A
            # pending request is downloaded again from the start later.
            jobs.remove(path)
            if failure is not None:
                raise failure
            self.state = "cancelled" if self.cancelled else "stopped"
            return
        jobs.finish_recording(work, self.job_id, {
            "ended_at": self._now().astimezone().isoformat(timespec="seconds"), "complete": True, "error": None})
        self.state = "done"
        logger.info("VOD %s downloaded (%.0f MB), queued for conversion", self.request["video_id"], self.bytes / 1e6)

    # -- download mode (and fallback): streamlink's own reader ------------------

    def _read_stream(self, selected, out):
        """streamlink fetches ahead on its own; in realtime fallback only the reading is paced,
        which does not limit bandwidth (used only for playlists we cannot fetch ourselves)."""
        ts_clock = TsClock()
        pacer = Pacer(self.speed, clock=self._clock) if self.mode == "realtime" else None
        fd = selected.open()
        try:
            while not self._stop_event.is_set():
                data = fd.read(READ_SIZE)
                if not data:
                    break
                out.write(data)
                self.bytes += len(data)
                self.media_seconds = ts_clock.feed(data)
                if pacer is not None:
                    wait = pacer.delay(self.media_seconds)
                    if wait > 0:
                        self._stop_event.wait(min(wait, 5))
        finally:
            try:
                fd.close()
            except Exception:
                pass

    # -- realtime: fetch segments one by one at playback pace ------------------

    def _http_get(self, url, retries=None):
        last = None
        for attempt in range(retries or SEGMENT_RETRIES):
            if self._stop_event.is_set():
                raise Stopped
            try:
                response = self._session.http.get(url, timeout=SEGMENT_TIMEOUT)
                return response.content
            except Exception as error:
                last = error
                if attempt + 1 < (retries or SEGMENT_RETRIES):
                    self._stop_event.wait(2 ** attempt)
        raise last

    def _realtime_plan(self, selected):
        """Segment list of the selected variant, or None to fall back to streamlink's reader."""
        url = getattr(selected, "url", None)
        if not url or ".m3u8" not in url.split("?")[0]:
            logger.warning("VOD %s: not an HLS playlist, realtime pacing limits reading only", self.request["video_id"])
            return None
        try:
            plan = parse_media_playlist(self._http_get(url).decode("utf-8", "replace"), url)
        except UnsupportedPlaylist as error:
            logger.warning("VOD %s: %s, realtime pacing limits reading only", self.request["video_id"], error)
            return None
        if not plan.segments:
            raise NoStreamsAvailable
        return plan

    def _playlist_duration(self, selected):
        url = getattr(selected, "url", None)
        if not url or ".m3u8" not in url.split("?")[0]:
            return None
        try:
            # One try only: the duration is just for the progress bar.
            return parse_media_playlist(self._http_get(url, retries=1).decode("utf-8", "replace"), url).duration or None
        except Stopped:
            raise
        except Exception:
            return None

    def _fetch_paced(self, plan, out):
        pacer = Pacer(self.speed, clock=self._clock)
        if plan.init:
            self._write(out, self._http_get(plan.init))
        for duration, url in plan.segments:
            while True:
                wait = pacer.delay(self.media_seconds)
                if wait <= 0 or self._stop_event.is_set():
                    break
                self._stop_event.wait(min(wait, 5))
            if self._stop_event.is_set():
                return
            try:
                self._write(out, self._http_get(url))
            except Stopped:
                return
            self.media_seconds += duration

    def _write(self, out, data):
        out.write(data)
        self.bytes += len(data)


class Stopped(Exception):
    pass


class UnsupportedPlaylist(Exception):
    pass


class PlaylistPlan:
    def __init__(self, segments, init=None):
        self.segments = segments          # [(seconds, absolute url)]
        self.init = init                  # fMP4 init segment (EXT-X-MAP) or None
        self.extension = "mp4" if init else "ts"
        self.duration = round(sum(d for d, _ in segments))


def parse_media_playlist(text, base_url):
    """Parse an HLS media playlist into a PlaylistPlan (TS or fMP4, unencrypted)."""
    from urllib.parse import urljoin
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise UnsupportedPlaylist("not an HLS playlist")
    segments, init, duration = [], None, None
    for line in lines[1:]:
        if line.startswith("#EXT-X-STREAM-INF"):
            raise UnsupportedPlaylist("multivariant playlist")
        if line.startswith("#EXT-X-KEY") and "METHOD=NONE" not in line:
            raise UnsupportedPlaylist("encrypted playlist")
        if line.startswith("#EXT-X-MAP"):
            match = re.search(r'URI="([^"]+)"', line)
            if not match or init is not None:
                raise UnsupportedPlaylist("unsupported EXT-X-MAP")
            init = urljoin(base_url, match.group(1))
        elif line.startswith("#EXTINF:"):
            try:
                duration = float(line[8:].split(",")[0])
            except ValueError:
                raise UnsupportedPlaylist("bad EXTINF") from None
        elif not line.startswith("#"):
            if duration is None:
                raise UnsupportedPlaylist("segment without EXTINF")
            segments.append((duration, urljoin(base_url, line)))
            duration = None
    return PlaylistPlan(segments, init)
