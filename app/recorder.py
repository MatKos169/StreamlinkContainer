"""Recorder container: watches streams.json, polls Twitch and runs one Recording per live channel.

Runs independently of the web container: it only reads streams.json (re-read
when it changes) and writes status.json for the UI.
"""
import datetime
import logging
import os
import signal
import sys
import threading

from .recording import Recording
from .settings import SettingsError, load_settings
from .streams import load_streams, write_json_atomic
from .twitch import TwitchClient, error_summary

logger = logging.getLogger(__name__)

SHUTDOWN_TIMEOUT = 30


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Supervisor:
    def __init__(self, settings, twitch, recording_factory=Recording):
        self.settings = settings
        self.twitch = twitch
        self.recording_factory = recording_factory
        self.streams = []
        self.recordings = {}      # stream id -> running Recording
        self.last_result = {}     # stream id -> {"state", "error", "file", "at"} of the last ended recording
        self.config_error = None
        self.twitch_error = None
        self._streams_mtime = None

    def reload_streams(self):
        path = self.settings.streams_file
        try:
            mtime = os.stat(path).st_mtime_ns
        except FileNotFoundError:
            mtime = None
        if mtime == self._streams_mtime and self.config_error is None:
            return
        try:
            self.streams = load_streams(path)
            self.config_error = None
            self._streams_mtime = mtime
            logger.info("Loaded %d channel(s) from %s", len(self.streams), path)
        except ValueError as error:
            # Keep recording with the last good list.
            self.config_error = str(error)
            self._streams_mtime = mtime
            logger.error("Ignoring invalid %s: %s", path, error)

    def tick(self):
        self.reload_streams()
        wanted = {s.id: s for s in self.streams if s.enabled}

        for sid, rec in list(self.recordings.items()):
            target = wanted.get(sid)
            if target is None or target.channel != rec.stream.channel:
                rec.stop()
            if not rec.is_alive():
                self.last_result[sid] = {"state": rec.state, "error": rec.error, "file": rec.file, "at": _now_iso()}
                del self.recordings[sid]

        live = None
        logins = sorted({s.channel for s in wanted.values()})
        if logins:
            try:
                live = self.twitch.get_live(logins)
                self.twitch_error = None
            except Exception as error:
                self.twitch_error = error_summary(error)
                logger.error("Twitch check failed: %s", self.twitch_error)

        if live is not None:
            for sid, stream in wanted.items():
                info = live.get(stream.channel)
                rec = self.recordings.get(sid)
                if rec is not None:
                    if info is not None and rec.stream.channel == stream.channel:
                        rec.update(info)
                elif info is not None:
                    rec = self.recording_factory(stream, info, self.settings)
                    self.recordings[sid] = rec
                    rec.start()
                    logger.info("%s is live (%s), starting recording", stream.channel, info.category or "no category")

        self.write_status(live)

    def stream_status(self, stream, live):
        rec = self.recordings.get(stream.id)
        info = (live or {}).get(stream.channel)
        status = {"live": None if live is None else info is not None,
                  "category": info.category if info else None,
                  "title": info.title if info else None,
                  "last": self.last_result.get(stream.id)}
        if not stream.enabled:
            status["state"] = "disabled"
        elif rec is not None:
            status.update(state=rec.state, file=rec.file, error=rec.error)
        elif live is None:
            status["state"] = "unknown"
        else:
            status["state"] = "live" if info else "offline"
        return status

    def write_status(self, live):
        data = {
            "updated_at": _now_iso(),
            "check_interval": self.settings.check_interval,
            "twitch_error": self.twitch_error,
            "config_error": self.config_error,
            "streams": {s.id: self.stream_status(s, live) for s in self.streams},
        }
        try:
            write_json_atomic(self.settings.status_file, data)
        except OSError as error:
            logger.warning("Could not write status file: %s", type(error).__name__)

    def run(self, stop_event):
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("Unexpected error in recorder loop")
            stop_event.wait(self.settings.check_interval)
        self.shutdown()

    def shutdown(self):
        for rec in self.recordings.values():
            rec.stop()
        for rec in self.recordings.values():
            rec.join(SHUTDOWN_TIMEOUT)
        self.recordings.clear()


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

    logger.info("Recorder started: checking every %ss, channels from %s", settings.check_interval, settings.streams_file)
    Supervisor(settings, TwitchClient(settings.client_id, settings.client_secret)).run(stop_event)
    logger.info("Recorder stopped")


if __name__ == "__main__":
    main()
