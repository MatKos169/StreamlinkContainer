import json
import os

from app.recorder import Supervisor
from app.streams import Options

from fakes import info, make_settings, make_stream, save_streams


class FakeTwitch:
    def __init__(self):
        self.live = {}
        self.fail = None
        self.calls = []

    def get_live(self, logins):
        self.calls.append(list(logins))
        if self.fail:
            raise self.fail
        return {k: v for k, v in self.live.items() if k in logins}


class FakeRecording:
    created = []

    def __init__(self, stream, info, settings, options):
        self.stream, self.info, self.settings, self.options = stream, info, settings, options
        self.updates, self.stopped, self.alive = [], False, False
        self.state, self.error, self.file = "starting", None, None
        FakeRecording.created.append(self)

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def stop(self):
        self.stopped = True

    def update(self, info):
        self.updates.append(info)

    def join(self, timeout=None):
        pass

    # test helpers
    def record(self):
        self.state, self.file = "recording", f"{self.stream.output}/x.ts"

    def end(self, state="finished"):
        self.state, self.alive = state, False


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def setup(tmp_path, streams, overrides=None, **settings_kw):
    FakeRecording.created = []
    settings = make_settings(tmp_path, **settings_kw)
    save_streams(settings.streams_file, streams, overrides)
    twitch, clock = FakeTwitch(), Clock()
    sup = Supervisor(settings, twitch, recording_factory=FakeRecording, clock=clock, wallclock=lambda: 1_800_000_000 + clock.t)
    return sup, twitch, settings, clock


def status(settings):
    return json.load(open(settings.status_file))


def advance(sup, clock, seconds):
    """Run one step per second, like the real loop."""
    for _ in range(seconds):
        clock.t += 1
        sup.step()


def touch(path):
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def test_starts_recording_when_live_and_updates_category(tmp_path):
    a, b = make_stream("alpha"), make_stream("beta", output="other/beta")
    sup, twitch, settings, clock = setup(tmp_path, [a, b])
    sup.step()
    assert FakeRecording.created == []
    assert twitch.calls == [["alpha", "beta"]]
    assert status(settings)["streams"][a.id]["state"] == "offline"

    twitch.live = {"beta": info("beta")}
    advance(sup, clock, 59)
    assert len(twitch.calls) == 1 and FakeRecording.created == []  # checks only every check_interval
    advance(sup, clock, 1)
    assert len(twitch.calls) == 2
    rec = FakeRecording.created[0]
    assert (rec.stream.channel, rec.stream.output, rec.stream.quality) == ("beta", "other/beta", "best")
    rec.record()
    sup.step()
    assert status(settings)["streams"][b.id]["state"] == "recording"

    twitch.live = {"beta": info("beta", category="Elden Ring", category_id="1")}
    advance(sup, clock, 60)
    assert len(FakeRecording.created) == 1
    assert rec.updates[-1].category == "Elden Ring"


def test_quick_rescan_after_stream_end_finds_restart(tmp_path):
    a = make_stream("alpha")
    sup, twitch, settings, clock = setup(tmp_path, [a])
    twitch.live = {"alpha": info("alpha", category="Music")}
    sup.step()
    first = FakeRecording.created[0]
    first.record()
    advance(sup, clock, 5)

    twitch.live = {}            # stream dropped; Helix says offline from now on
    first.end()
    advance(sup, clock, 1)
    st = status(settings)["streams"][a.id]
    assert st["state"] == "rescan" and st["rescan_until"]
    assert st["last"]["file"] == "alpha/x.ts"

    advance(sup, clock, 9)
    assert len(FakeRecording.created) == 1
    advance(sup, clock, 1)      # 10s after the end: first direct probe
    probe = FakeRecording.created[1]
    assert probe.info.category == "Music"     # last known category for the chapters CSV
    probe.end("waiting")        # still offline
    advance(sup, clock, 10)
    assert len(FakeRecording.created) == 3
    FakeRecording.created[2].record()        # back online
    advance(sup, clock, 1)
    assert status(settings)["streams"][a.id]["state"] == "recording"
    assert twitch.calls == [["alpha"]]       # restart was found without waiting for a Helix check


def test_rescan_window_expires(tmp_path):
    a = make_stream("alpha")
    sup, twitch, settings, clock = setup(tmp_path, [a], overrides={"rescan_interval": 20, "rescan_duration": 60})
    twitch.live = {"alpha": info("alpha")}
    sup.step()
    FakeRecording.created[0].record()
    twitch.live = {}
    FakeRecording.created[0].end()
    for _ in range(80):
        advance(sup, clock, 1)
        for rec in FakeRecording.created[1:]:
            if rec.alive:
                rec.end("waiting")   # every probe finds nothing
    assert len(FakeRecording.created) == 1 + 2   # probes at +20 and +40; the window closes at +60
    assert a.id not in sup.rescan
    assert status(settings)["streams"][a.id]["state"] == "offline"
    assert status(settings)["streams"][a.id]["last"]["file"] == "alpha/x.ts"


def test_failed_probe_does_not_extend_window_or_trigger_rescan(tmp_path):
    a = make_stream("alpha")
    sup, twitch, settings, clock = setup(tmp_path, [a])
    twitch.live = {"alpha": info("alpha")}   # Helix says live but streamlink finds nothing
    sup.step()
    FakeRecording.created[0].end("waiting")
    advance(sup, clock, 30)
    assert sup.rescan == {}
    assert len(FakeRecording.created) == 1   # next try only on the next regular check


def test_rescan_disabled(tmp_path):
    a = make_stream("alpha")
    sup, twitch, settings, clock = setup(tmp_path, [a], overrides={"rescan_duration": 0})
    twitch.live = {"alpha": info("alpha")}
    sup.step()
    FakeRecording.created[0].record()
    twitch.live = {}
    FakeRecording.created[0].end()
    advance(sup, clock, 30)
    assert sup.rescan == {} and len(FakeRecording.created) == 1


def test_settings_from_ui_are_picked_up(tmp_path):
    a = make_stream("alpha")
    sup, twitch, settings, clock = setup(tmp_path, [a])
    sup.step()
    save_streams(settings.streams_file, [a], {"check_interval": 15, "default_quality": "720p,best"})
    touch(settings.streams_file)
    advance(sup, clock, 16)
    assert len(twitch.calls) == 2
    assert status(settings)["options"]["check_interval"] == 15
    twitch.live = {"alpha": info("alpha")}
    advance(sup, clock, 15)
    assert FakeRecording.created[0].stream.quality == "720p,best"
    assert FakeRecording.created[0].options.check_interval == 15


def test_removed_or_disabled_stream_stops_recording(tmp_path):
    a, b = make_stream("alpha"), make_stream("beta")
    sup, twitch, settings, clock = setup(tmp_path, [a, b])
    twitch.live = {"alpha": info("alpha"), "beta": info("beta")}
    sup.step()
    rec_a, rec_b = FakeRecording.created
    rec_a.record(), rec_b.record()
    b.enabled = False
    save_streams(settings.streams_file, [b])
    touch(settings.streams_file)
    sup.step()
    assert rec_a.stopped and rec_b.stopped
    rec_a.end(), rec_b.end()
    advance(sup, clock, 30)
    assert sup.rescan == {}                  # no quick re-check for removed/disabled channels
    assert status(settings)["streams"][b.id]["state"] == "disabled"


def test_twitch_error_keeps_running_recordings(tmp_path):
    a = make_stream("alpha")
    sup, twitch, settings, clock = setup(tmp_path, [a])
    twitch.live = {"alpha": info("alpha")}
    sup.step()
    twitch.fail = RuntimeError("https://id.twitch.tv/?client_secret=SECRET")
    advance(sup, clock, 60)
    assert not FakeRecording.created[0].stopped
    st = status(settings)
    assert st["twitch_error"] == "RuntimeError"
    assert "SECRET" not in json.dumps(st)


def test_invalid_config_keeps_last_good_list(tmp_path):
    a = make_stream("alpha")
    sup, twitch, settings, clock = setup(tmp_path, [a])
    sup.step()
    with open(settings.streams_file, "w") as f:
        f.write("{broken")
    touch(settings.streams_file)
    sup.step()
    assert [s.channel for s in sup.streams] == ["alpha"]
    assert status(settings)["config_error"]
    save_streams(settings.streams_file, [a])
    touch(settings.streams_file)
    sup.step()
    assert status(settings)["config_error"] is None


def test_status_heartbeat(tmp_path):
    sup, twitch, settings, clock = setup(tmp_path, [make_stream("alpha")])
    sup.step()
    first = status(settings)["updated_at"]
    advance(sup, clock, 29)
    assert status(settings)["updated_at"] == first
    advance(sup, clock, 1)
    assert status(settings)["updated_at"] != first


def test_shutdown_stops_all(tmp_path):
    sup, twitch, settings, clock = setup(tmp_path, [make_stream("alpha")])
    twitch.live = {"alpha": info("alpha")}
    sup.step()
    sup.shutdown()
    assert FakeRecording.created[0].stopped


def test_recorder_prunes_done_with_retention(tmp_path):
    import time as _time
    from app import jobs
    a = make_stream("alpha")
    sup, twitch, settings, clock = setup(tmp_path, [a])
    for job_id, age_days in (("old", 40), ("young", 2)):
        path = os.path.join(settings.work_dir, "done", job_id)
        os.makedirs(path)
        jobs.write_job(path, {"done_ts": _time.time() - age_days * 86400})
    sup.step()                                   # retention 0: nothing deleted
    assert sorted(os.listdir(os.path.join(settings.work_dir, "done"))) == ["old", "young"]
    save_streams(settings.streams_file, [a], {"done_retention_days": 30})
    touch(settings.streams_file)
    advance(sup, clock, 1)                       # changed retention applies right away
    assert os.listdir(os.path.join(settings.work_dir, "done")) == ["young"]



class FakeDownload:
    created = []

    def __init__(self, request, channel_outputs):
        self.request, self.channel_outputs = request, channel_outputs
        self.state, self.alive, self.stopped, self.cancelled = "downloading", False, False, False
        self.title = self.channel = self.file = self.error = self.duration = None
        self.mode, self.speed, self.progress, self.media_seconds, self.bytes = "realtime", 1.0, 0.5, 10, 100
        import time as _t
        self.started = _t.monotonic()
        FakeDownload.created.append(self)

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def stop(self, cancel=False):
        self.stopped, self.cancelled = True, cancel

    def join(self, timeout=None):
        pass


def test_supervisor_runs_vod_downloads(tmp_path):
    from app import vod
    FakeDownload.created = []
    a = make_stream("alpha", output="Twitch/Alpha")
    sup, twitch, settings, clock = setup(tmp_path, [a], overrides={"vod_parallel": 1})
    sup.download_factory = FakeDownload
    r1 = vod.create_request(settings.config_dir, "1")
    r2 = vod.create_request(settings.config_dir, "2")
    sup.step()
    assert [d.request["id"] for d in FakeDownload.created] == [r1["id"]]        # one at a time
    assert FakeDownload.created[0].channel_outputs == {"alpha": "Twitch/Alpha"}
    assert status(settings)["downloads"][r1["id"]]["progress"] == 0.5

    FakeDownload.created[0].state, FakeDownload.created[0].alive = "done", False
    advance(sup, clock, 1)
    assert [d.request["id"] for d in FakeDownload.created] == [r1["id"], r2["id"]]
    pending, failed = vod.list_requests(settings.config_dir)
    assert [p["id"] for p in pending] == [r2["id"]] and failed == []

    vod.cancel_request(settings.config_dir, r2["id"])
    advance(sup, clock, 1)
    second = FakeDownload.created[1]
    assert second.stopped and second.cancelled
    second.state, second.alive = "cancelled", False
    advance(sup, clock, 1)
    assert vod.list_requests(settings.config_dir) == ([], [])


def test_supervisor_marks_failed_downloads(tmp_path):
    from app import vod
    FakeDownload.created = []
    sup, twitch, settings, clock = setup(tmp_path, [])
    sup.download_factory = FakeDownload
    r = vod.create_request(settings.config_dir, "7")
    sup.step()
    FakeDownload.created[0].state, FakeDownload.created[0].error, FakeDownload.created[0].alive = "error", "VOD not found", False
    advance(sup, clock, 1)
    pending, failed = vod.list_requests(settings.config_dir)
    assert pending == [] and failed[0]["id"] == r["id"] and failed[0]["error"] == "VOD not found"
    advance(sup, clock, 5)
    assert len(FakeDownload.created) == 1                                        # not restarted on its own


def test_cancel_before_start(tmp_path):
    from app import vod
    FakeDownload.created = []
    sup, twitch, settings, clock = setup(tmp_path, [])
    sup.download_factory = FakeDownload
    r = vod.create_request(settings.config_dir, "8")
    vod.cancel_request(settings.config_dir, r["id"])
    sup.step()
    assert FakeDownload.created == [] and vod.list_requests(settings.config_dir) == ([], [])



def test_stream_limit_holds_back_vod_downloads(tmp_path):
    from app import vod
    FakeDownload.created = []
    streams = [make_stream(c) for c in ("a", "b", "c", "d")]
    sup, twitch, settings, clock = setup(tmp_path, streams, overrides={"max_parallel_streams": 3, "vod_parallel": 5})
    sup.download_factory = FakeDownload
    twitch.live = {"a": info("a"), "b": info("b"), "c": info("c")}
    vod.create_request(settings.config_dir, "1")
    vod.create_request(settings.config_dir, "2")
    sup.step()
    assert len(FakeRecording.created) == 3 and FakeDownload.created == []      # 3 of 3 in use
    st = status(settings)
    assert (st["streams_active"], st["streams_limit"], st["downloads_blocked"]) == (3, 3, True)

    FakeRecording.created[2].end()                                            # c ends: one slot free
    twitch.live = {"a": info("a"), "b": info("b")}
    advance(sup, clock, 1)
    assert len(FakeDownload.created) == 1                                     # only one fits
    assert status(settings)["streams_active"] == 3

    twitch.live = {"a": info("a"), "b": info("b"), "d": info("d")}            # d goes live although full
    advance(sup, clock, 60)
    assert "d" in [r.stream.channel for r in FakeRecording.created]           # live is never held back
    assert sup.active_streams() == 4
    assert not FakeDownload.created[0].stopped                                # running download keeps going
    assert len(FakeDownload.created) == 1


def test_rescan_probes_do_not_take_a_slot(tmp_path):
    from app import vod
    FakeDownload.created = []
    a = make_stream("a")
    sup, twitch, settings, clock = setup(tmp_path, [a], overrides={"max_parallel_streams": 1})
    sup.download_factory = FakeDownload
    twitch.live = {"a": info("a")}
    sup.step()
    FakeRecording.created[0].record()
    twitch.live = {}
    FakeRecording.created[0].end()
    advance(sup, clock, 11)                                                   # 10s after the end: probe starts
    probe = FakeRecording.created[-1]
    assert probe.probe and probe.alive and not probe.file
    vod.create_request(settings.config_dir, "1")
    advance(sup, clock, 1)
    assert len(FakeDownload.created) == 1                                     # a probe does not take the slot
    probe.record()                                                            # ...but a probe that records does
    assert sup.active_streams() == 2


def test_no_limit_by_default(tmp_path):
    from app import vod
    FakeDownload.created = []
    streams = [make_stream(c) for c in ("a", "b", "c")]
    sup, twitch, settings, clock = setup(tmp_path, streams)
    sup.download_factory = FakeDownload
    twitch.live = {c: info(c) for c in ("a", "b", "c")}
    vod.create_request(settings.config_dir, "1")
    sup.step()
    assert len(FakeDownload.created) == 1 and status(settings)["streams_limit"] == 0
