"""One running recording: streamlink -> .part file, plus a chapters CSV of category changes."""
import csv
import datetime
import logging
import os
import shutil
import threading
import time

from .naming import recording_base

logger = logging.getLogger(__name__)

READ_SIZE = 64 * 1024
INCOMPLETE_SUFFIX = ".incomplete"
CHAPTERS_SUFFIX = ".chapters.csv"
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
    """Records one channel until the stream ends or stop() is called."""

    def __init__(self, stream, info, settings, session_factory=default_session_factory, now=None):
        super().__init__(daemon=True, name=f"rec-{stream.channel}")
        self.stream = stream
        self.settings = settings
        self._session_factory = session_factory
        self._now = now or datetime.datetime.now
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._info = info
        self._chapters = None
        self.state = "starting"
        self.error = None
        self.file = None

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
            logger.info("%s: no playable stream yet", self.stream.channel)
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
        quality = select_quality(self.stream.quality or self.settings.default_quality, streams)
        selected = streams[quality]

        base = recording_base(self.settings.recordings_dir, self.stream.output, self.info.title, self._now())
        os.makedirs(os.path.dirname(base), exist_ok=True)
        part = f"{base}.part"
        final = f"{base}.{stream_extension(selected)}"
        chapters_path = f"{base}{CHAPTERS_SUFFIX}"

        fd = selected.open()
        complete = False
        try:
            with open(part, "wb") as out:
                with self._lock:
                    self._chapters = ChapterWriter(chapters_path)
                    self._chapters.add(self._info)
                self.state = "recording"
                self.file = os.path.relpath(final, self.settings.recordings_dir)
                logger.info("%s: recording (%s) to %s", self.stream.channel, quality, self.file)
                while not self._stop_event.is_set():
                    data = fd.read(READ_SIZE)
                    if not data:
                        break
                    out.write(data)
            complete = True
        finally:
            with self._lock:
                if self._chapters is not None:
                    self._chapters.close()
            _close_quietly(fd)
            _finalize(part, final, chapters_path, complete)
        logger.info("%s: recording finished: %s", self.stream.channel, self.file)


def _close_quietly(fd):
    try:
        fd.close()
    except Exception as error:
        logger.warning("Closing stream failed: %s", type(error).__name__)


def _finalize(part, final, chapters_path, complete):
    """Rename .part to the final name; keep partial data as .incomplete; drop empty files."""
    if not os.path.exists(part):
        return
    if os.path.getsize(part) == 0:
        os.remove(part)
        if os.path.exists(chapters_path):
            os.remove(chapters_path)
    elif complete:
        shutil.move(part, final)
    else:
        shutil.move(part, f"{final}{INCOMPLETE_SUFFIX}")
