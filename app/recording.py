"""One running recording: streamlink -> .part file, plus a chapters CSV of category changes."""
import csv
import datetime
import logging
import os
import threading
import time

from . import jobs
from .naming import destination_path

logger = logging.getLogger(__name__)

READ_SIZE = 64 * 1024
CHAPTER_FIELDS = ["offset_seconds", "offset", "timestamp_utc", "category_id", "category", "title"]


class NoStreamsAvailable(Exception):
    """Twitch reports the channel live but streamlink found no playable stream (yet)."""


def select_quality(preferences, streams):
    """First entry of a comma-separated preference list present in streams, else "best"."""
    for entry in (preferences or "").split(","):
        entry = entry.strip()
        if entry and entry in streams:
            return entry
    return "best"


def stream_extension(stream):
    url = getattr(stream, "url", "") or ""
    return "ts" if url.split("?")[0].endswith(".m3u8") or type(stream).__name__.endswith("HLSStream") else "mp4"


def format_offset(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def describe_error(error):
    """Error text that is safe to show: type name, plus errno text for OSErrors (never str(error))."""
    detail = type(error).__name__
    if isinstance(error, OSError) and error.errno is not None:
        detail += f" (errno {error.errno}: {os.strerror(error.errno)})"
    return detail


class ChapterWriter:
    """Appends one CSV row per category change; offsets are seconds since recording start."""

    def __init__(self, path, clock=time.monotonic, utcnow=None):
        self.path = path
        self._clock = clock
        self._utcnow = utcnow or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self._start = clock()
        self._last_category = None
        self._file = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        self._writer.writerow(CHAPTER_FIELDS)
        self._file.flush()
        self.rows = 0

    def add(self, info):
        """Write a row if the category differs from the last one. Returns True if written."""
        if self._file is None or info is None or info.category_id == self._last_category:
            return False
        offset = max(self._clock() - self._start, 0)
        self._writer.writerow([
            f"{offset:.1f}", format_offset(offset),
            self._utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            info.category_id, info.category, info.title,
        ])
        self._file.flush()
        self._last_category = info.category_id
        self.rows += 1
        return True

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None


def default_session_factory():
    import streamlink
    return streamlink.Streamlink()


class Recording(threading.Thread):
    """Records one channel into a job folder in /work until the stream ends or stop() is called.

    When done, the folder is moved to /work/queue for a converter.
    """

    def __init__(self, stream, info, settings, options=None, session_factory=default_session_factory, now=None):
        super().__init__(daemon=True, name=f"rec-{stream.channel}")
        self.stream = stream
        self.settings = settings
        self.options = options or settings.defaults
        self._session_factory = session_factory
        self._now = now or datetime.datetime.now
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._info = info
        self._chapters = None
        self.state = "starting"
        self.error = None
        self.file = None      # destination below /recordings, set once recording started
        self.job_id = None

    @property
    def info(self):
        with self._lock:
            return self._info

    def stop(self):
        self._stop_event.set()

    def update(self, info):
        """Called by the supervisor on every poll while live; adds a chapter on category change."""
        with self._lock:
            self._info = info
            if self._chapters is not None:
                self._chapters.add(info)

    def run(self):
        try:
            self._record()
            self.state = "finished"
        except NoStreamsAvailable:
            self.state = "waiting"
            self.error = "no playable stream yet"
            logger.debug("%s: no playable stream yet", self.stream.channel)
        except Exception as error:
            self.state = "error"
            self.error = describe_error(error)
            logger.error("%s: recording failed: %s", self.stream.channel, self.error)

    def _plugin_options(self):
        token = self.settings.oauth_token
        if not token:
            return None
        from streamlink.options import Options
        # api-header is only sent to Twitch's API, not to the video CDN.
        return Options({"api-header": [("Authorization", f"OAuth {token}")]})

    def _record(self):
        session = self._session_factory()
        streams = session.streams(f"https://www.twitch.tv/{self.stream.channel}", options=self._plugin_options())
        if not streams:
            raise NoStreamsAvailable
        quality = select_quality(self.stream.quality, streams)
        selected = streams[quality]

        started = self._now()
        info = self.info
        work = self.settings.work_dir
        self.job_id = jobs.new_job_id(self.stream.channel, started)
        path = jobs.job_dir(work, "recording", self.job_id)
        os.makedirs(path)
        destination = destination_path(self.stream.output, self.options.folder_layout, started, info.title)
        jobs.write_job(path, {
            "id": self.job_id, "channel": self.stream.channel, "title": info.title,
            "category": info.category, "quality": quality, "destination": destination,
            "keep_source": self.options.keep_source,
            "audio_normalize": self.options.audio_normalize,
            "started_at": started.astimezone().isoformat(timespec="seconds"),
        })
        part = os.path.join(path, f"video.{stream_extension(selected)}.part")

        fd = selected.open()
        failure = None
        try:
            with open(part, "wb") as out:
                with self._lock:
                    self._chapters = ChapterWriter(os.path.join(path, jobs.CHAPTERS_FILE))
                    self._chapters.add(self._info)
                self.state = "recording"
                self.file = destination
                logger.info("%s: recording (%s), job %s -> %s", self.stream.channel, quality, self.job_id, destination)
                while not self._stop_event.is_set():
                    data = fd.read(READ_SIZE)
                    if not data:
                        break
                    out.write(data)
        except Exception as error:
            failure = error
        finally:
            with self._lock:
                if self._chapters is not None:
                    self._chapters.close()
            _close_quietly(fd)
            queued = jobs.finish_recording(work, self.job_id, {
                "ended_at": self._now().astimezone().isoformat(timespec="seconds"),
                "complete": failure is None,
                "error": describe_error(failure) if failure else None,
            })
        if failure is not None:
            raise failure
        logger.info("%s: recording finished, %s", self.stream.channel,
                    f"queued for conversion ({self.job_id})" if queued else "nothing recorded")


def _close_quietly(fd):
    try:
        fd.close()
    except Exception as error:
        logger.warning("Closing stream failed: %s", type(error).__name__)
