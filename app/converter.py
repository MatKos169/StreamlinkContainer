"""Converter container: turns queued recordings into MP4s with chapters and moves them into place.

Takes jobs from /work/queue (see jobs.py), so any number of converters can run,
also on other hosts, as long as they mount the same /work and /recordings.
Streams are copied, not re-encoded (-c copy): fast and lossless.
"""
import csv
import datetime
import errno
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

from . import jobs
from .naming import clean_output_path
from .settings import SettingsError, env, load_settings, parse_int
from .streams import parse_ffmpeg_args

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 10
PROGRESS_SECONDS = 2
OUTPUT_NAME = "output.mp4"
METADATA_NAME = "metadata.ffmeta"
NO_CATEGORY = "Ohne Kategorie"
LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"
AUDIO_BITRATE = "192k"


class ConversionError(Exception):
    pass


class Stopped(Exception):
    pass


# -- chapters ---------------------------------------------------------------

def read_chapters(csv_path, duration):
    """[(start_ms, end_ms, title)] from chapters.csv; the last chapter ends at duration."""
    starts = []
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    start = float(row.get("offset_seconds") or 0)
                except ValueError:
                    continue
                title = (row.get("category") or "").strip() or NO_CATEGORY
                if starts and title == starts[-1][1]:
                    continue
                starts.append((max(start, 0.0), title))
    except FileNotFoundError:
        pass
    if not starts:
        starts = [(0.0, NO_CATEGORY)]
    starts[0] = (0.0, starts[0][1])  # the first chapter always starts at 0
    starts = [s for s in starts if s[0] < duration] or starts[:1]
    chapters = []
    for i, (start, title) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else duration
        chapters.append((int(start * 1000), int(max(end, start) * 1000), title))
    return chapters


def _escape(value):
    """Escape a value for ffmpeg's FFMETADATA format."""
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    for char in ("\\", "=", ";", "#"):
        text = text.replace(char, "\\" + char)
    return text


def build_metadata(job, chapters):
    started = job.get("started_at") or ""
    categories = []
    for _, _, title in chapters:
        if title not in categories:
            categories.append(title)
    lines = [
        ";FFMETADATA1",
        f"title={_escape(job.get('title') or job.get('channel'))}",
        f"artist={_escape(job.get('channel'))}",
        f"album_artist={_escape(job.get('channel'))}",
        f"date={_escape(started[:10])}",
        f"comment={_escape('Twitch: ' + str(job.get('channel')) + ' | ' + ', '.join(categories))}",
        f"description={_escape(job.get('title'))}",
    ]
    for start, end, title in chapters:
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}", f"title={_escape(title)}"]
    return "\n".join(lines) + "\n"


# -- files ------------------------------------------------------------------

def place_file(src, root, relative):
    """Move src to root/relative without overwriting (adds "_1", "_2" … like the old converter).

    Across file systems the file is copied to a hidden temp name first, so a
    media server never sees a half-copied file. Returns the final path.
    """
    relative = clean_output_path(relative)
    if not relative:
        raise ConversionError("job has no destination")
    dest = os.path.join(root, relative)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    base, ext = os.path.splitext(dest)
    n = 1
    while os.path.exists(dest):
        dest = f"{base}_{n}{ext}"
        n += 1
    try:
        os.rename(src, dest)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        tmp = os.path.join(os.path.dirname(dest), f".{os.path.basename(dest)}.part")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)
        os.remove(src)
    return dest


# -- converter --------------------------------------------------------------

class Converter:
    def __init__(self, settings, name, ffmpeg="ffmpeg", ffprobe="ffprobe", poll_seconds=10):
        self.settings = settings
        self.name = name
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.poll_seconds = poll_seconds
        self.progress = 0.0
        self.current_job = None
        self.started = time.time()

    @property
    def work(self):
        return self.settings.work_dir

    def beat_worker(self):
        try:
            jobs.worker_heartbeat(self.work, self.name, "busy" if self.current_job else "idle", self.current_job,
                                  self.progress if self.current_job else None, self.poll_seconds, self.started)
        except OSError as error:
            logger.warning("Could not write worker heartbeat: %s", type(error).__name__)

    def _worker_heartbeats(self, stop_event):
        while not stop_event.wait(HEARTBEAT_SECONDS):
            self.beat_worker()

    def run(self, stop_event):
        jobs.ensure_dirs(self.work)
        self.beat_worker()
        beater = threading.Thread(target=self._worker_heartbeats, args=(stop_event,), daemon=True)
        beater.start()
        try:
            self._loop(stop_event)
        finally:
            beater.join(HEARTBEAT_SECONDS + 1)
            jobs.remove_worker(self.work, self.name)

    def _loop(self, stop_event):
        while not stop_event.is_set():
            try:
                for job_id in jobs.requeue_stale(self.work):
                    logger.warning("Job %s had no heartbeat, back in the queue", job_id)
                job_id, path = jobs.claim_next(self.work, self.name)
                if job_id is None:
                    stop_event.wait(self.poll_seconds)
                    continue
                self.process(job_id, path, stop_event)
            except Exception:
                logger.exception("Unexpected error in converter loop")
                stop_event.wait(self.poll_seconds)

    def probe_duration(self, video):
        result = subprocess.run(
            [self.ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", video],
            capture_output=True, text=True, timeout=300)
        try:
            return float(result.stdout.strip())
        except ValueError:
            raise ConversionError(f"ffprobe could not read the video: {result.stderr.strip()[-300:]}") from None

    def ffmpeg_command(self, video, metadata, output, normalize=True, video_codec="copy", extra_args=()):
        """Build the ffmpeg call.

        Video: copied by default, or encoded with video_codec. Audio: normalized like
        the MatKos169/ffmpeg container (loudnorm -14 LUFS, AAC 192k) or copied.
        extra_args (from the UI) come right before the output file, so they win
        over the options above (ffmpeg uses the last value given).
        """
        if normalize:
            audio = ["-af", LOUDNORM, "-c:a", "aac", "-b:a", AUDIO_BITRATE]
        else:
            audio = ["-c:a", "copy", "-bsf:a", "aac_adtstoasc"]
        return [
            self.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
            "-i", video, "-f", "ffmetadata", "-i", metadata,
            "-map", "0:v?", "-map", "0:a?", "-map_metadata", "1", "-map_chapters", "1",
            "-c:v", video_codec or "copy", *audio, "-movflags", "+faststart",
            *extra_args,
            output,
        ]

    def run_ffmpeg(self, command, path, duration, stop_event):
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stderr_lines = []
        reader = threading.Thread(target=lambda: stderr_lines.extend(process.stderr), daemon=True)
        reader.start()
        last_progress = 0.0
        for line in process.stdout:
            if stop_event.is_set():
                process.terminate()
                process.wait(30)
                raise Stopped
            key, _, value = line.strip().partition("=")
            if key in ("out_time_us", "out_time_ms") and value.isdigit() and duration > 0:
                self.progress = min(int(value) / 1_000_000 / duration, 1.0)
                if time.monotonic() - last_progress >= PROGRESS_SECONDS:
                    last_progress = time.monotonic()
                    try:
                        jobs.heartbeat(path, self.name, self.progress)
                    except OSError:
                        pass
        code = process.wait()
        reader.join(5)
        if stop_event.is_set():
            raise Stopped
        if code != 0:
            raise ConversionError(f"ffmpeg exit code {code}: {''.join(stderr_lines).strip()[-500:]}")

    def _heartbeats(self, path, done):
        """Keep the claim fresh for the whole job (ffmpeg and the final copy)."""
        while not done.wait(HEARTBEAT_SECONDS):
            try:
                jobs.heartbeat(path, self.name, self.progress)
            except OSError:
                pass

    def process(self, job_id, path, stop_event):
        self.progress = 0.0
        self.current_job = job_id
        self.beat_worker()
        done = threading.Event()
        beater = threading.Thread(target=self._heartbeats, args=(path, done), daemon=True)
        beater.start()
        try:
            self._process(job_id, path, stop_event)
        finally:
            done.set()
            beater.join(5)
            self.current_job = None
            self.progress = 0.0
            self.beat_worker()

    def _process(self, job_id, path, stop_event):
        job = jobs.read_job(path)
        output = os.path.join(path, OUTPUT_NAME)
        logger.info("Converting %s (%s)", job_id, job.get("destination"))
        try:
            video = jobs.video_file(path)
            if video is None:
                raise ConversionError("no video file in job")
            duration = self.probe_duration(video)
            chapters = read_chapters(os.path.join(path, jobs.CHAPTERS_FILE), duration)
            metadata = os.path.join(path, METADATA_NAME)
            with open(metadata, "w", encoding="utf-8") as f:
                f.write(build_metadata(job, chapters))
            try:
                extra_args = parse_ffmpeg_args(job.get("ffmpeg_args"))
            except ValueError as error:
                raise ConversionError(str(error)) from None
            command = self.ffmpeg_command(video, metadata, output, normalize=job.get("audio_normalize", True),
                                          video_codec=job.get("video_codec") or "copy", extra_args=extra_args)
            logger.info("%s: video %s, audio %s%s", job_id, job.get("video_codec") or "copy",
                        "normalized" if job.get("audio_normalize", True) else "copy",
                        f", extra: {' '.join(extra_args)}" if extra_args else "")
            self.run_ffmpeg(command, path, duration, stop_event)
            if not jobs.still_owner(path, self.name):
                logger.warning("Lost the claim on %s (heartbeat too old?), discarding result", job_id)
                return
            final = place_file(output, self.settings.recordings_dir, job.get("destination"))
        except Stopped:
            logger.info("Stopping: %s goes back to the queue", job_id)
            jobs.release(self.work, job_id, self.name)
            return
        except Exception as error:
            message = str(error) if isinstance(error, ConversionError) else type(error).__name__
            logger.error("Converting %s failed: %s", job_id, message)
            job.update(error=message, attempts=(job.get("attempts") or 0) + 1, failed_by=self.name,
                       failed_at=_now_iso())
            _quiet_remove(output)
            jobs.write_job(path, job)
            _quiet_remove(os.path.join(path, jobs.CLAIM_FILE))
            jobs.move(self.work, job_id, "converting", "failed")
            return

        relative = os.path.relpath(final, self.settings.recordings_dir)
        logger.info("Done: %s -> %s (%d chapters)", job_id, relative, len(chapters))
        try:
            jobs.record_completed(self.work, job_id, {
                "channel": job.get("channel"), "title": job.get("title"), "file": relative,
                "converter": self.name, "chapters": len(chapters), "duration": round(duration, 1),
                "size": os.path.getsize(final),
            })
        except OSError as error:
            logger.warning("Could not write history entry: %s", type(error).__name__)
        if job.get("keep_source"):
            job.update(converted_to=relative, converted_by=self.name, converted_at=_now_iso(), done_ts=time.time())
            jobs.write_job(path, job)
            _quiet_remove(os.path.join(path, jobs.CLAIM_FILE))
            _quiet_remove(os.path.join(path, METADATA_NAME))
            jobs.move(self.work, job_id, "converting", "done")
        else:
            jobs.remove(path)


def _now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _quiet_remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def main():
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
        poll = parse_int(env("CONVERTER_POLL_INTERVAL", default=10), "CONVERTER_POLL_INTERVAL", 1)
    except SettingsError as error:
        logger.error("Configuration error: %s", error)
        sys.exit(2)
    name = env("CONVERTER_NAME", default=socket.gethostname())

    stop_event = threading.Event()

    def handle_signal(signum, frame):
        logger.info("Stop requested")
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    logger.info("Converter %s started: %s -> %s", name, settings.work_dir, settings.recordings_dir)
    Converter(settings, name, poll_seconds=poll).run(stop_event)
    logger.info("Converter stopped")


if __name__ == "__main__":
    main()
