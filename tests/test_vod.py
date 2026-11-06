import datetime
import json
import os
import shutil
import subprocess
import threading

import pytest

from app import jobs, vod
from app.streams import Options
from app.vod import Pacer, TsClock, VodDownload, parse_twitch_duration, parse_vod_url

from fakes import FakeFd, FakeStream, make_settings


# -- helpers -----------------------------------------------------------------

def ts_packet(pid, pts=None, adaptation=0):
    """One 188-byte TS packet; with pts it starts a PES with that PTS."""
    header = bytearray([0x47, (0x40 if pts is not None else 0) | (pid >> 8 & 0x1F), pid & 0xFF,
                        0x30 if adaptation else 0x10])
    body = bytearray()
    if adaptation:
        body += bytes([adaptation - 1]) + b"\xff" * (adaptation - 1)
    if pts is not None:
        p = [((pts >> 29) & 0x0E) | 0x21, (pts >> 22) & 0xFF, ((pts >> 14) & 0xFE) | 1, (pts >> 7) & 0xFF, ((pts << 1) & 0xFE) | 1]
        body += b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + bytes(p)
    return bytes(header + body + b"\xff" * (184 - len(body)))


class FakePlugin:
    def __init__(self, streams, title="VOD title", author="Streamer", category="Elden Ring"):
        self._streams, self.title, self.author, self.category = streams, title, author, category

    def streams(self):
        return self._streams

    def get_title(self):
        return self.title

    def get_author(self):
        return self.author

    def get_category(self):
        return self.category


class FakeHttp:
    def __init__(self, files=None):
        self.files, self.calls = files or {}, []

    def get(self, url, timeout=None):
        self.calls.append(url)
        if url not in self.files:
            raise OSError(404, "not found")
        return type("Response", (), {"content": self.files[url]})()


class FakeVodSession:
    def __init__(self, plugin, http=None):
        self.plugin, self.options, self.resolved = plugin, {}, []
        self.http = http or FakeHttp()

    def set_option(self, key, value):
        self.options[key] = value

    def resolve_url(self, url):
        self.resolved.append(url)
        return "twitch", lambda session, u, options: self.plugin, url


def request(**kw):
    return {"id": "20261106-200000-vod123-abcdef", "video_id": "123", "url": "https://www.twitch.tv/videos/123",
            "output": "", "quality": "", "mode": None, "speed": None, "created": 1.0, **kw}


def run_download(tmp_path, chunks, req=None, options=None, helix=None, channel_outputs=None, plugin=None, http=None,
                 **settings_kw):
    settings = make_settings(tmp_path, **settings_kw)
    plugin = plugin or FakePlugin({"best": FakeStream(FakeFd(chunks))})
    session = FakeVodSession(plugin, http)
    download = VodDownload(req or request(mode="download"), settings, options or Options(keep_source=False),
                           channel_outputs=channel_outputs, session_factory=lambda: session,
                           metadata_fetcher=(lambda vid: helix) if helix is not None else None,
                           now=lambda: datetime.datetime(2026, 11, 6, 21, 0, 0))
    download.run()
    return download, session, settings


# -- parsing -----------------------------------------------------------------

@pytest.mark.parametrize("value", ["https://www.twitch.tv/videos/2345678901", "twitch.tv/videos/2345678901?t=1h2m",
                                   "2345678901", "https://m.twitch.tv/videos/2345678901/", "https://www.twitch.tv/somebody/v/2345678901"])
def test_parse_vod_url(value):
    assert parse_vod_url(value) == "2345678901"


@pytest.mark.parametrize("value", ["", "https://www.twitch.tv/somebody", "https://youtube.com/videos/1", "abc"])
def test_parse_vod_url_rejects(value):
    with pytest.raises(ValueError):
        parse_vod_url(value)


def test_parse_twitch_duration():
    assert parse_twitch_duration("3h2m1s") == 10921
    assert parse_twitch_duration("45m") == 2700
    assert parse_twitch_duration("12s") == 12
    assert parse_twitch_duration("") is None and parse_twitch_duration("x") is None


# -- TS clock and pacing -----------------------------------------------------------

def test_ts_clock_counts_media_time_and_handles_quirks():
    clock = TsClock()
    hz = 90000
    assert clock.feed(ts_packet(256, pts=10 * hz)) == 0.0
    assert clock.feed(ts_packet(256, pts=12 * hz) + ts_packet(257, pts=99 * hz)) == pytest.approx(2.0)  # other PID ignored
    assert clock.feed(ts_packet(256, pts=int(11.9 * hz))) == pytest.approx(2.0)     # small step back (B-frames)
    assert clock.feed(ts_packet(256, pts=13 * hz, adaptation=8)) == pytest.approx(3.0)
    assert clock.feed(ts_packet(256, pts=500 * hz)) == pytest.approx(3.0)          # discontinuity: no time counted
    assert clock.feed(ts_packet(256, pts=501 * hz)) == pytest.approx(4.0)
    assert clock.feed(ts_packet(256)) == pytest.approx(4.0)                          # no PTS in chunk


def test_ts_clock_pts_wraparound_and_split_packets():
    clock = TsClock()
    wrap = 1 << 33
    data = ts_packet(256, pts=wrap - 90000) + ts_packet(256, pts=90000)
    clock.feed(data[:100])          # packet split across chunks
    clock.feed(data[100:250])
    assert clock.feed(data[250:]) == pytest.approx(2.0)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_ts_clock_on_real_ts(tmp_path):
    path = tmp_path / "s.ts"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30", "-f", "lavfi",
                    "-i", "sine", "-t", "20", "-c:v", "libx264", "-preset", "ultrafast", "-bf", "2", "-c:a", "aac",
                    "-f", "mpegts", str(path)], check=True)
    data, clock = path.read_bytes(), TsClock()
    for i in range(0, len(data), 64 * 1024):
        seconds = clock.feed(data[i:i + 64 * 1024])
    assert seconds == pytest.approx(20, abs=1.0)


def test_ts_clock_resyncs_after_garbage():
    clock = TsClock()
    clock.feed(ts_packet(256, pts=90000))
    clock.feed(b"\x00garbage" + ts_packet(256, pts=3 * 90000) + ts_packet(256))
    assert clock.seconds == pytest.approx(2.0)


def test_pacer():
    now = [100.0]
    pacer = Pacer(speed=2.0, lead=10, clock=lambda: now[0])
    assert pacer.delay(15) == 0.0          # 15 media s at 2x = 7.5 s, inside the 10 s lead
    assert pacer.delay(60) == pytest.approx(20.0)
    now[0] += 20
    assert pacer.delay(60) == 0.0


# -- requests ----------------------------------------------------------------

def test_request_lifecycle(tmp_path):
    config = str(tmp_path)
    r = vod.create_request(config, "https://www.twitch.tv/videos/42", output="Twitch/X", mode="download", speed="2")
    assert (r["video_id"], r["output"], r["mode"], r["speed"]) == ("42", "Twitch/X", "download", 2.0)
    pending, failed = vod.list_requests(config)
    assert [p["id"] for p in pending] == [r["id"]] and not pending[0]["cancel"] and failed == []
    assert vod.cancel_request(config, r["id"])
    assert vod.list_requests(config)[0][0]["cancel"]
    vod.finish_request(config, r, error="boom")
    pending, failed = vod.list_requests(config)
    assert pending == [] and failed[0]["error"] == "boom" and "cancel" not in failed[0]
    assert vod.retry_failed(config, r["id"])
    pending, failed = vod.list_requests(config)
    assert [p["id"] for p in pending] == [r["id"]] and "error" not in pending[0] and failed == []
    vod.finish_request(config, r, error="again")
    assert vod.delete_failed(config, r["id"])
    assert vod.list_requests(config) == ([], [])


@pytest.mark.parametrize("kw", [{"url": "nope"}, {"url": "1", "mode": "fast"}, {"url": "1", "speed": 0.1},
                                {"url": "1", "output": "../x"}, {"url": "1", "quality": "a;b"}])
def test_create_request_rejects(tmp_path, kw):
    with pytest.raises(ValueError):
        vod.create_request(str(tmp_path), **kw)


def test_request_ids_are_validated(tmp_path):
    with pytest.raises(ValueError):
        vod.cancel_request(str(tmp_path), "../streams")


# -- VodDownload ------------------------------------------------------------------

def queued(settings):
    folder = os.path.join(settings.work_dir, "queue")
    ids = os.listdir(folder)
    assert len(ids) == 1
    return os.path.join(folder, ids[0])


def test_download_queues_job_with_helix_metadata(tmp_path):
    helix = {"title": "Helix title", "login": "streamer",
             "created_at": datetime.datetime(2026, 10, 1, 18, 30, tzinfo=datetime.timezone.utc), "duration": 3600}
    d, session, settings = run_download(tmp_path, [b"ab", b"cd"], helix=helix,
                                        channel_outputs={"streamer": "Twitch/Streamer"})
    assert d.state == "done", d.error
    assert session.options == {"stream-segment-threads": 3} and session.resolved == ["https://www.twitch.tv/videos/123"]
    path = queued(settings)
    meta = jobs.read_job(path)
    local = datetime.datetime(2026, 10, 1, 18, 30, tzinfo=datetime.timezone.utc).astimezone()
    assert meta["destination"] == f"Twitch/Streamer-2026/10/{local:%Y-%m-%d %H-%M-%S} - Helix title.mp4"
    assert (meta["kind"], meta["video_id"], meta["channel"], meta["download_mode"]) == ("vod", "123", "streamer", "download")
    assert open(os.path.join(path, "video.ts"), "rb").read() == b"abcd"
    rows = open(os.path.join(path, "chapters.csv")).read().splitlines()
    assert len(rows) == 2 and "Elden Ring" in rows[1]


def test_download_without_helix_uses_streamlink_metadata(tmp_path):
    d, _, settings = run_download(tmp_path, [b"x"], req=request(mode="download", output="Eigene/VODs"))
    meta = jobs.read_job(queued(settings))
    assert meta["destination"] == "Eigene/VODs-2026/11/2026-11-06 21-00-00 - VOD title.mp4"
    assert meta["channel"] == "streamer" and d.progress is None


PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-PLAYLIST-TYPE:VOD
#EXTINF:10.000,
0.ts
#EXT-X-DISCONTINUITY
#EXTINF:10.000,
1-muted.ts
#EXTINF:4.5,
https://other.example/2.ts
#EXT-X-ENDLIST
"""
BASE = "https://cdn.example/vod/chunked/index-dvr.m3u8?sig=1"


def test_parse_media_playlist():
    plan = vod.parse_media_playlist(PLAYLIST, BASE)
    assert plan.segments == [(10.0, "https://cdn.example/vod/chunked/0.ts"), (10.0, "https://cdn.example/vod/chunked/1-muted.ts"),
                             (4.5, "https://other.example/2.ts")]
    assert (plan.extension, plan.duration, plan.init) == ("ts", 24, None)
    fmp4 = vod.parse_media_playlist('#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:2,\na.m4s\n', BASE)
    assert (fmp4.extension, fmp4.init) == ("mp4", "https://cdn.example/vod/chunked/init.mp4")


@pytest.mark.parametrize("text", ["nope", "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nv.m3u8",
                                  '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXTINF:2,\na.ts', "#EXTM3U\na.ts"])
def test_parse_media_playlist_unsupported(text):
    with pytest.raises(vod.UnsupportedPlaylist):
        vod.parse_media_playlist(text, BASE)


def segment_http():
    return FakeHttp({BASE: PLAYLIST.encode(), "https://cdn.example/vod/chunked/0.ts": b"A" * 10,
                     "https://cdn.example/vod/chunked/1-muted.ts": b"B" * 10, "https://other.example/2.ts": b"C" * 5})


class PlaylistStream(FakeStream):
    url = BASE

    def open(self):
        raise AssertionError("realtime mode must not use streamlink's reader")


def test_realtime_fetches_segments_itself(tmp_path):
    http = segment_http()
    plugin = FakePlugin({"best": PlaylistStream(None)})
    d, session, settings = run_download(tmp_path, [], req=request(mode=None), plugin=plugin, http=http,
                                        options=Options(vod_mode="realtime", vod_speed=20))
    assert d.state == "done", d.error
    assert session.options == {}                                    # no segment threads in realtime
    assert http.calls == [BASE, "https://cdn.example/vod/chunked/0.ts", "https://cdn.example/vod/chunked/1-muted.ts",
                          "https://other.example/2.ts"]
    assert open(os.path.join(queued(settings), "video.ts"), "rb").read() == b"A" * 10 + b"B" * 10 + b"C" * 5
    assert (d.duration, d.media_seconds, d.progress) == (24, 24.5, 1.0)


def test_realtime_waits_between_segments(tmp_path):
    """Segment n is fetched only when playback (x speed) reaches it, minus the lead."""
    settings = make_settings(tmp_path)
    segments = "".join(f"#EXTINF:20,\n{i}.ts\n" for i in range(4))
    files = {BASE: ("#EXTM3U\n" + segments).encode()}
    files.update({f"https://cdn.example/vod/chunked/{i}.ts": b"x" for i in range(4)})
    http = FakeHttp(files)
    now = [0.0]
    fetched_at = []
    original_get = http.get
    def get(url, timeout=None):
        if url.endswith(".ts"):
            fetched_at.append((url.rsplit("/", 1)[1], now[0]))
        return original_get(url, timeout)
    http.get = get
    session = FakeVodSession(FakePlugin({"best": PlaylistStream(None)}), http)
    d = VodDownload(request(mode="realtime", speed=2.0), settings, Options(), session_factory=lambda: session,
                    clock=lambda: now[0])
    def fake_wait(seconds):
        now[0] += seconds
        return False
    d._stop_event.wait = fake_wait
    d.run()
    assert d.state == "done", d.error
    # lead 10s, speed 2: segment n (starts at 20n media s) may start at 10n - 10 s
    assert fetched_at == [("0.ts", 0.0), ("1.ts", 0.0), ("2.ts", 10.0), ("3.ts", 20.0)]


def test_realtime_falls_back_for_encrypted_playlist(tmp_path):
    http = FakeHttp({BASE: b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXTINF:2,\na.ts\n'})
    class Stream(FakeStream):
        url = BASE
    plugin = FakePlugin({"best": Stream(FakeFd([b"via-streamlink"]))})
    d, _, settings = run_download(tmp_path, [], req=request(mode="realtime"), plugin=plugin, http=http)
    assert d.state == "done", d.error
    assert open(os.path.join(queued(settings), "video.ts"), "rb").read() == b"via-streamlink"


def test_realtime_segment_failure_fails_download(tmp_path, monkeypatch):
    monkeypatch.setattr(vod, "SEGMENT_RETRIES", 2)
    http = segment_http()
    del http.files["https://cdn.example/vod/chunked/1-muted.ts"]
    plugin = FakePlugin({"best": PlaylistStream(None)})
    d, _, settings = run_download(tmp_path, [], req=request(mode="realtime"), plugin=plugin, http=http)
    assert d.state == "error" and "errno 404" in d.error
    assert os.listdir(os.path.join(settings.work_dir, "recording")) == []


def test_not_available(tmp_path):
    d, _, settings = run_download(tmp_path, [], plugin=FakePlugin({}))
    assert d.state == "error" and "not available" in d.error
    assert not os.path.exists(os.path.join(settings.work_dir, "queue"))


def test_failure_drops_partial_download(tmp_path):
    settings = make_settings(tmp_path)
    plugin = FakePlugin({"best": FakeStream(FakeFd([b"abc"], fail=OSError(104, "reset http://secret")))})
    d = VodDownload(request(mode="download"), settings, Options(), session_factory=lambda: FakeVodSession(plugin))
    d.run()
    assert d.state == "error" and "secret" not in d.error and "errno 104" in d.error
    assert os.listdir(os.path.join(settings.work_dir, "recording")) == []


def test_cancel_drops_partial_download(tmp_path):
    settings = make_settings(tmp_path)
    gate = threading.Event()
    plugin = FakePlugin({"best": FakeStream(FakeFd([b"abc"], block=gate))})
    d = VodDownload(request(mode="download"), settings, Options(), session_factory=lambda: FakeVodSession(plugin))
    d.start()
    for _ in range(100):
        if d.state == "downloading":
            break
        threading.Event().wait(0.02)
    d.stop(cancel=True)
    gate.set()
    d.join(5)
    assert d.state == "cancelled"
    assert os.listdir(os.path.join(settings.work_dir, "recording")) == []
    assert not os.path.exists(os.path.join(settings.work_dir, "queue"))


def test_recover_drops_unfinished_vod(tmp_path):
    work = str(tmp_path)
    path = os.path.join(work, "recording", "v1")
    os.makedirs(path)
    open(os.path.join(path, "video.ts.part"), "wb").write(b"data")
    jobs.write_job(path, {"kind": "vod"})
    assert jobs.recover_recordings(work) == []
    assert os.listdir(os.path.join(work, "recording")) == []


def test_download_mode_reads_duration_from_playlist(tmp_path):
    class Stream(FakeStream):
        url = BASE
    plugin = FakePlugin({"best": Stream(FakeFd([b"data"]))})
    d, _, _ = run_download(tmp_path, [], req=request(mode="download"), plugin=plugin, http=segment_http())
    assert d.state == "done" and d.duration == 24
