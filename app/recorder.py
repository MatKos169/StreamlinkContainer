"""Recorder container: watches streams.json, polls Twitch and runs one Recording per live channel.

Runs independently of the web container: it only reads streams.json (re-read
when it changes) and writes status.json for the UI.

Schedule:
- every check_interval: one Helix call for all enabled channels; live ones
  without a recording get one, running recordings get category updates.
- after a recording ends: for rescan_duration seconds the channel is probed
  every rescan_interval directly with streamlink (no Helix delay), so a stream
  that comes back after a dropout is picked up quickly.
"""
import dataclasses
import datetime
import logging
import signal
import sys
import threading
import os
import time

from . import jobs, vod
from .recording import Recording
from .settings import SettingsError, load_settings
from .streams import load_config, write_json_atomic
from .twitch import LiveInfo, TwitchClient, error_summary

logger = logging.getLogger(__name__)

SHUTDOWN_TIMEOUT = 30
STEP_SECONDS = 1
STATUS_HEARTBEAT_SECONDS = 30
PRUNE_SECONDS = 3600


def _iso(ts=None):
    moment = datetime.datetime.fromtimestamp(ts if ts is not None else time.time(), datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class Supervisor:
    def __init__(self, settings, twitch, recording_factory=Recording, clock=time.monotonic, wallclock=time.time,
                 download_factory=None):
        self.settings = settings
        self.twitch = twitch
        self.recording_factory = recording_factory
        self.clock = clock
        self.wallclock = wallclock
        self.options = settings.defaults
        self.streams = []
        self.recordings = {}      # stream id -> running Recording
        self.last_result = {}     # stream id -> last recording that produced a file
        self.last_info = {}       # stream id -> last LiveInfo from Helix (title/category for rescans)
        self.rescan = {}          # stream id -> {"until": t, "next": t} (monotonic)
        self.cooldown = {}        # stream id -> t (monotonic): slot reserved after a stream end
        self.live = None          # result of the last Helix check, None if it failed / never ran
        self.config_error = None
        self.twitch_error = None
        self.next_check = 0.0
        self._streams_mtime = None
        self._dirty = True
        self._last_status = None
        self._rec_signature = ()
        self._next_prune = 0.0
        self.download_factory = download_factory or self._default_download
        self.downloads = {}       # request id -> running VodDownload
        self.download_errors = 0
        self.downloads_blocked = False   # a pending download waits for max_parallel_streams

    # -- config ---------------------------------------------------------

    def reload_config(self):
        path = self.settings.streams_file
        try:
            st = os.stat(path)
            # Every save replaces the file (new inode), so this also catches two
            # saves within the file system's timestamp granularity.
            mtime = (st.st_ino, st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            mtime = None
        if mtime == self._streams_mtime:
            return
        self._streams_mtime = mtime
        self._dirty = True
        try:
            config = load_config(path)
            options = config.options(self.settings.defaults)
        except ValueError as error:
            # Keep recording with the last good config.
            self.config_error = str(error)
            logger.error("Ignoring invalid %s: %s", path, error)
            return
        self.config_error = None
        self.streams = config.streams
        if options.done_retention_days != self.options.done_retention_days:
            self._next_prune = 0.0  # apply a changed retention right away
        if options.check_interval != self.options.check_interval:
            self.next_check = min(self.next_check, self.clock() + options.check_interval)
        self.options = options
        logger.info("Loaded %d channel(s); check every %ss, re-check every %ss for %ss after a stream ends",
                    len(self.streams), options.check_interval, options.rescan_interval, options.rescan_duration)

    # -- recordings -----------------------------------------------------

    def _start(self, stream, info, reason, quiet=False):
        resolved = dataclasses.replace(stream, quality=stream.quality or self.options.default_quality)
        rec = self.recording_factory(resolved, info, self.settings, self.options)
        rec.probe = quiet          # quick re-check after a stream end (counts only once it records)
        self.recordings[stream.id] = rec
        rec.start()
        self._dirty = True
        logger.log(logging.DEBUG if quiet else logging.INFO, "%s: %s, trying to record", stream.channel, reason)

    def _reap(self, wanted, now):
        for sid, rec in list(self.recordings.items()):
            target = wanted.get(sid)
            if target is None or target.channel != rec.stream.channel:
                rec.stop()
            if rec.is_alive():
                continue
            del self.recordings[sid]
            self._dirty = True
            if rec.file:  # it actually recorded: remember it and watch for a quick restart
                self.last_result[sid] = {"state": rec.state, "error": rec.error, "file": rec.file, "at": _iso(self.wallclock())}
                if target is not None and self.options.slot_cooldown > 0:
                    self.cooldown[sid] = now + self.options.slot_cooldown
                if target is not None and self.options.rescan_duration > 0:
                    self.rescan[sid] = {"until": now + self.options.rescan_duration,
                                        "next": now + self.options.rescan_interval}
                    logger.info("%s: recording ended, re-checking every %ss for %ss", target.channel,
                                self.options.rescan_interval, self.options.rescan_duration)

    def _check_twitch(self, wanted, now):
        self.next_check = now + self.options.check_interval
        self._dirty = True
        logins = sorted({s.channel for s in wanted.values()})
        try:
            live = self.twitch.get_live(logins) if logins else {}
            self.twitch_error = None
        except Exception as error:
            self.twitch_error = error_summary(error)
            self.live = None
            logger.error("Twitch check failed: %s", self.twitch_error)
            return
        self.live = live
        for sid, stream in wanted.items():
            info = live.get(stream.channel)
            if info is None:
                continue
            self.last_info[sid] = info
            rec = self.recordings.get(sid)
            if rec is not None:
                if rec.stream.channel == stream.channel:
                    rec.update(info)
            else:
                self.rescan.pop(sid, None)
                self._start(stream, info, f"live ({info.category or 'no category'})")

    def _run_rescans(self, wanted, now):
        for sid, window in list(self.rescan.items()):
            stream = wanted.get(sid)
            if stream is None or now >= window["until"]:
                del self.rescan[sid]
                self._dirty = True
                if stream is not None:
                    logger.info("%s: did not come back, back to normal checks", stream.channel)
                continue
            if sid in self.recordings or now < window["next"]:
                continue
            window["next"] = now + self.options.rescan_interval
            info = self.last_info.get(sid) or LiveInfo(stream.channel, "", "", "")
            self._start(stream, info, "re-check after stream end", quiet=True)

    def step(self):
        now = self.clock()
        self.reload_config()
        wanted = {s.id: s for s in self.streams if s.enabled}
        self._reap(wanted, now)
        if now >= self.next_check:
            self._check_twitch(wanted, now)
        self._run_rescans(wanted, now)
        self._update_cooldowns(wanted, now)
        self._prune_done(now)
        self._manage_downloads()
        signature = tuple((sid, rec.state, rec.file) for sid, rec in sorted(self.recordings.items()))
        signature += tuple((rid, d.state, int((d.progress or 0) * 100)) for rid, d in sorted(self.downloads.items()))
        signature += (self.active_streams(), self.cooldown_streams())
        if signature != self._rec_signature:
            self._rec_signature = signature
            self._dirty = True
        if self._dirty or self._last_status is None or now - self._last_status >= STATUS_HEARTBEAT_SECONDS:
            self.write_status(now)

    # -- VOD downloads --------------------------------------------------

    def _default_download(self, request, channel_outputs):
        fetcher = None
        if self.settings.client_id and self.settings.client_secret:
            # Own client per download: TwitchClient is not thread-safe.
            fetcher = lambda video_id: TwitchClient(self.settings.client_id, self.settings.client_secret).get_video(video_id)
        return vod.VodDownload(request, self.settings, self.options, channel_outputs=channel_outputs,
                               metadata_fetcher=fetcher)

    def _recording_ids(self):
        return {sid for sid, rec in self.recordings.items() if not getattr(rec, "probe", False) or rec.file}

    def active_streams(self):
        """Twitch stream slots in use: live recordings, channels in their slot cooldown after a
        stream end (the slot stays reserved in case the stream comes back) and VOD downloads."""
        return len(self._recording_ids() | set(self.cooldown)) + len(self.downloads)

    def cooldown_streams(self):
        """Channels holding a slot only because of the cooldown (no recording running)."""
        return len(set(self.cooldown) - self._recording_ids())

    def _update_cooldowns(self, wanted, now):
        """Expire cooldowns, drop them for removed/disabled channels or when recording again,
        and apply "Platz freigeben" requests from the web UI (/config/commands/release-<id>)."""
        folder = os.path.join(self.settings.config_dir, "commands")
        try:
            commands = os.listdir(folder)
        except FileNotFoundError:
            commands = []
        for name in commands:
            if name.startswith("release-"):
                sid = name[len("release-"):]
                if self.cooldown.pop(sid, None) is not None:
                    logger.info("Stream slot of %s released from the web UI", sid)
                try:
                    os.remove(os.path.join(folder, name))
                except FileNotFoundError:
                    pass
                self._dirty = True
        recording = self._recording_ids()
        for sid, until in list(self.cooldown.items()):
            if now >= until or sid not in wanted or sid in recording:
                del self.cooldown[sid]
                self._dirty = True

    def _manage_downloads(self):
        config_dir = self.settings.config_dir
        try:
            pending, _ = vod.list_requests(config_dir)
        except OSError:
            return
        by_id = {r["id"]: r for r in pending}
        ended = set()
        for rid, download in list(self.downloads.items()):
            request = by_id.get(rid)
            if request is None or request.get("cancel"):
                download.stop(cancel=True)
            if download.is_alive():
                continue
            del self.downloads[rid]
            ended.add(rid)   # its request is in `pending` (read above); don't start it again
            self._dirty = True
            if download.state == "done" or download.state == "cancelled" or request is None:
                vod.finish_request(config_dir, download.request)
            elif download.state == "error":
                vod.finish_request(config_dir, download.request, error=download.error)
                self.download_errors += 1
        blocked = False
        for request in pending:
            rid = request["id"]
            if rid in self.downloads or rid in ended:
                continue
            if request.get("cancel"):
                vod.finish_request(config_dir, request)
                self._dirty = True
                continue
            if len(self.downloads) >= self.options.vod_parallel:
                break
            limit = self.options.max_parallel_streams
            if limit and self.active_streams() >= limit:
                # Live recordings are never held back; VOD downloads wait until a slot is free
                # (a channel in its slot cooldown after a stream end still holds its slot).
                blocked = True
                break
            # Without an own output folder, a VOD goes to its channel's folder if that channel is configured.
            download = self.download_factory(request, {s.channel: s.output for s in self.streams})
            self.downloads[rid] = download
            download.start()
            self._dirty = True
        if blocked != self.downloads_blocked:
            self.downloads_blocked = blocked
            self._dirty = True
            if blocked:
                logger.info("VOD downloads wait: %d of %d Twitch streams in use",
                            self.active_streams(), self.options.max_parallel_streams)

    def download_status(self):
        return {rid: {"state": d.state, "title": d.title, "channel": d.channel, "mode": d.mode, "speed": d.speed,
                      "progress": d.progress, "media_seconds": int(d.media_seconds), "duration": d.duration,
                      "bytes": d.bytes, "file": d.file, "error": d.error,
                      "elapsed_seconds": int(time.monotonic() - d.started)}
                for rid, d in self.downloads.items()}

    def _prune_done(self, now):
        """Delete kept originals older than done_retention_days (hourly; works without the web UI)."""
        if now < self._next_prune:
            return
        self._next_prune = now + PRUNE_SECONDS
        if not self.options.done_retention_days:
            return
        try:
            pruned = jobs.prune_done(self.settings.work_dir, self.options.done_retention_days)
        except OSError as error:
            logger.warning("Cleaning up %s/done failed: %s", self.settings.work_dir, type(error).__name__)
            return
        if pruned:
            logger.info("Deleted %d original(s) older than %d days from done/", len(pruned), self.options.done_retention_days)

    # -- status ---------------------------------------------------------

    def stream_status(self, stream, now):
        rec = self.recordings.get(stream.id)
        info = (self.live or {}).get(stream.channel)
        window = self.rescan.get(stream.id)
        cooldown = self.cooldown.get(stream.id)
        status = {"live": None if self.live is None else info is not None,
                  "category": info.category if info else None,
                  "title": info.title if info else None,
                  "rescan_until": _iso(self.wallclock() + window["until"] - now) if window else None,
                  "cooldown_until": _iso(self.wallclock() + cooldown - now) if cooldown else None,
                  "last": self.last_result.get(stream.id)}
        if not stream.enabled:
            status["state"] = "disabled"
        elif rec is not None and (rec.file or not window):
            status.update(state=rec.state, file=rec.file, error=rec.error)
        elif window:
            status["state"] = "rescan"
        elif self.live is None:
            status["state"] = "unknown"
        else:
            status["state"] = "live" if info else "offline"
        return status

    def write_status(self, now):
        self._dirty = False
        self._last_status = now
        data = {
            "updated_at": _iso(self.wallclock()),
            "options": dataclasses.asdict(self.options),
            "check_interval": self.options.check_interval,
            "next_check_in": max(int(self.next_check - now), 0),
            "twitch_error": self.twitch_error,
            "config_error": self.config_error,
            "streams": {s.id: self.stream_status(s, now) for s in self.streams},
            "downloads": self.download_status(),
            "streams_active": self.active_streams(),
            "streams_cooldown": self.cooldown_streams(),
            "streams_limit": self.options.max_parallel_streams,
            "downloads_blocked": self.downloads_blocked,
        }
        try:
            write_json_atomic(self.settings.status_file, data)
        except OSError as error:
            logger.warning("Could not write status file: %s", type(error).__name__)

    # -- lifecycle ------------------------------------------------------

    def run(self, stop_event):
        while not stop_event.is_set():
            try:
                self.step()
            except Exception:
                logger.exception("Unexpected error in recorder loop")
            stop_event.wait(STEP_SECONDS)
        self.shutdown()

    def shutdown(self):
        workers = list(self.recordings.values()) + list(self.downloads.values())
        for worker in workers:
            worker.stop()
        for worker in workers:
            worker.join(SHUTDOWN_TIMEOUT)
        self.recordings.clear()
        self.downloads.clear()


def main():
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings(require_twitch=True)
    except SettingsError as error:
        logger.error("Configuration error: %s", error)
        sys.exit(2)

    stop_event = threading.Event()

    def handle_signal(signum, frame):
        logger.info("Stop requested, finishing recordings")
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    jobs.ensure_dirs(settings.work_dir)
    for job_id in jobs.recover_recordings(settings.work_dir):
        logger.warning("Queued unfinished recording %s from before the restart", job_id)
    logger.info("Recorder started, channels from %s, recording into %s", settings.streams_file, settings.work_dir)
    Supervisor(settings, TwitchClient(settings.client_id, settings.client_secret)).run(stop_event)
    logger.info("Recorder stopped")


if __name__ == "__main__":
    main()
