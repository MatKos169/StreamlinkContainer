import json
import os

from app.recorder import Supervisor
from app.streams import save_streams

from fakes import info, make_settings, make_stream


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

    def __init__(self, stream, info, settings):
        self.stream, self.info, self.settings = stream, info, settings
        self.updates, self.stopped, self.alive = [], False, False
        self.state, self.error, self.file = "recording", None, "x.ts"
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


def setup(tmp_path, streams):
    FakeRecording.created = []
    settings = make_settings(tmp_path)
    save_streams(settings.streams_file, streams)
    twitch = FakeTwitch()
    return Supervisor(settings, twitch, recording_factory=FakeRecording), twitch, settings


def status(settings):
    return json.load(open(settings.status_file))


def test_starts_recording_when_live_and_updates_category(tmp_path):
    a, b = make_stream("alpha"), make_stream("beta", output="other/beta")
    sup, twitch, settings = setup(tmp_path, [a, b])
    sup.tick()
    assert FakeRecording.created == []
    assert twitch.calls == [["alpha", "beta"]]
    assert status(settings)["streams"][a.id]["state"] == "offline"

    twitch.live = {"beta": info("beta")}
    sup.tick()
    assert [r.stream.channel for r in FakeRecording.created] == ["beta"]
    assert FakeRecording.created[0].stream.output == "other/beta"
    assert status(settings)["streams"][b.id]["state"] == "recording"

    twitch.live = {"beta": info("beta", category="Elden Ring", category_id="1")}
    sup.tick()
    assert len(FakeRecording.created) == 1
    assert FakeRecording.created[0].updates[-1].category == "Elden Ring"


def test_finished_recording_restarts_if_still_live(tmp_path):
    a = make_stream("alpha")
    sup, twitch, settings = setup(tmp_path, [a])
    twitch.live = {"alpha": info("alpha")}
    sup.tick()
    FakeRecording.created[0].alive = False
    FakeRecording.created[0].state = "waiting"
    sup.tick()
    assert len(FakeRecording.created) == 2
    assert status(settings)["streams"][a.id]["last"]["state"] == "waiting"


def test_removed_or_disabled_stream_stops_recording(tmp_path):
    a, b = make_stream("alpha"), make_stream("beta")
    sup, twitch, settings = setup(tmp_path, [a, b])
    twitch.live = {"alpha": info("alpha"), "beta": info("beta")}
    sup.tick()
    rec_a, rec_b = FakeRecording.created
    b.enabled = False
    save_streams(settings.streams_file, [b])
    os.utime(settings.streams_file, ns=(1, 1))  # make sure the mtime changes
    sup.tick()
    assert rec_a.stopped and rec_b.stopped
    assert len(twitch.calls) == 1  # no enabled channel left, so no API call
    assert status(settings)["streams"][b.id]["state"] == "disabled"


def test_twitch_error_keeps_running_recordings(tmp_path):
    a = make_stream("alpha")
    sup, twitch, settings = setup(tmp_path, [a])
    twitch.live = {"alpha": info("alpha")}
    sup.tick()
    twitch.fail = RuntimeError("https://id.twitch.tv/?client_secret=SECRET")
    sup.tick()
    assert not FakeRecording.created[0].stopped
    st = status(settings)
    assert st["twitch_error"] == "RuntimeError"
    assert "SECRET" not in json.dumps(st)


def test_invalid_config_keeps_last_good_list(tmp_path):
    a = make_stream("alpha")
    sup, twitch, settings = setup(tmp_path, [a])
    sup.tick()
    with open(settings.streams_file, "w") as f:
        f.write("{broken")
    sup.tick()
    assert [s.channel for s in sup.streams] == ["alpha"]
    assert status(settings)["config_error"]
    save_streams(settings.streams_file, [a])
    sup.tick()
    assert status(settings)["config_error"] is None


def test_shutdown_stops_all(tmp_path):
    sup, twitch, settings = setup(tmp_path, [make_stream("alpha")])
    twitch.live = {"alpha": info("alpha")}
    sup.tick()
    sup.shutdown()
    assert FakeRecording.created[0].stopped
