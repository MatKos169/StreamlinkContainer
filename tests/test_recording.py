import csv
import datetime
import json
import threading

from app.recording import ChapterWriter, Recording, format_offset, select_quality
from app.streams import Options

from fakes import FakeFd, FakeSession, FakeStream, info, make_settings, make_stream

NOW = datetime.datetime(2026, 11, 6, 20, 15, 0)


def run(tmp_path, fd, streams_extra=None, stream=None, live=None, options=None, **settings_kw):
    settings = make_settings(tmp_path, **settings_kw)
    streams = {"best": FakeStream(fd), **(streams_extra or {})}
    session = FakeSession(streams)
    rec = Recording(stream or make_stream(output="Twitch/Streamer"), live or info(title="My Title"),
                    settings, options or Options(folder_layout="year_month", keep_source=False),
                    session_factory=lambda: session, now=lambda: NOW)
    return rec, session


def work_files(tmp_path):
    root = tmp_path / "work"
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def queued(tmp_path):
    jobs = list((tmp_path / "work" / "queue").iterdir())
    assert len(jobs) == 1
    return jobs[0]


def test_select_quality():
    streams = {"720p": 1, "best": 2}
    assert select_quality("1080p60, 720p", streams) == "720p"
    assert select_quality("", streams) == "best"
    assert select_quality("480p", streams) == "best"


def test_format_offset():
    assert format_offset(3725.9) == "01:02:05"


def test_records_into_job_folder_and_queues_it(tmp_path):
    rec, session = run(tmp_path, FakeFd([b"abc", b"def"]))
    rec.run()
    assert rec.state == "finished", rec.error
    job = queued(tmp_path)
    assert job.name.startswith("20261106-201500-streamer-")
    assert sorted(p.name for p in job.iterdir()) == ["chapters.csv", "job.json", "video.ts"]
    assert (job / "video.ts").read_bytes() == b"abcdef"
    meta = json.loads((job / "job.json").read_text())
    assert meta["destination"] == "Twitch/Streamer/2026/11/2026-11-06 20-15-00 - My Title.mp4"
    assert (meta["channel"], meta["title"], meta["category"], meta["complete"], meta["error"]) == (
        "streamer", "My Title", "Just Chatting", True, None)
    assert meta["keep_source"] is False and meta["audio_normalize"] is True
    assert meta["started_at"].startswith("2026-11-06T20:15:00")
    rows = list(csv.DictReader(open(job / "chapters.csv")))
    assert [(r["offset"], r["category"], r["category_id"]) for r in rows] == [("00:00:00", "Just Chatting", "509658")]
    assert rec.file == meta["destination"]
    assert session.calls[0] == ("https://www.twitch.tv/streamer", None)


def test_oauth_token_goes_to_api_header_only(tmp_path):
    rec, session = run(tmp_path, FakeFd([b"a"]), oauth_token="secret")
    rec.run()
    assert session.calls[0][1].get("api-header") == [("Authorization", "OAuth secret")]


def test_quality_preference(tmp_path):
    hq = FakeFd([b"hq"])
    rec, _ = run(tmp_path, FakeFd([b"best"]), streams_extra={"1080p60": FakeStream(hq)},
                 stream=make_stream(output="s", quality="1080p60,best"))
    rec.run()
    assert hq.closed
    assert json.loads((queued(tmp_path) / "job.json").read_text())["quality"] == "1080p60"


def test_no_streams_is_waiting(tmp_path):
    settings = make_settings(tmp_path)
    rec = Recording(make_stream(), info(), settings, session_factory=lambda: FakeSession({}), now=lambda: NOW)
    rec.run()
    assert rec.state == "waiting"
    assert not (tmp_path / "work").exists()


def test_failure_queues_incomplete_job(tmp_path):
    fd = FakeFd([b"abc"], fail=OSError(5, "boom http://secret"))
    rec, _ = run(tmp_path, fd)
    rec.run()
    assert rec.state == "error"
    assert "secret" not in rec.error and "errno 5" in rec.error
    meta = json.loads((queued(tmp_path) / "job.json").read_text())
    assert meta["complete"] is False and "errno 5" in meta["error"]
    assert fd.closed


def test_empty_recording_leaves_nothing(tmp_path):
    rec, _ = run(tmp_path, FakeFd([]))
    rec.run()
    assert work_files(tmp_path) == []


def test_stop_and_category_change(tmp_path):
    gate = threading.Event()
    rec, _ = run(tmp_path, FakeFd([b"a"], block=gate))
    rec.start()
    for _ in range(100):
        if rec.state == "recording":
            break
        threading.Event().wait(0.02)
    assert len(list((tmp_path / "work" / "recording").iterdir())) == 1   # recorded in work/recording
    rec.update(info(category="Just Chatting", title="same category"))   # no new chapter
    rec.update(info(category="Elden Ring", category_id="512953", title="Now gaming"))
    rec.stop()
    gate.set()
    rec.join(5)
    assert rec.state == "finished"
    rows = list(csv.DictReader(open(queued(tmp_path) / "chapters.csv")))
    assert [r["category"] for r in rows] == ["Just Chatting", "Elden Ring"]


def test_chapter_writer_offsets(tmp_path):
    clock = iter([100.0, 100.0, 3700.4]).__next__
    writer = ChapterWriter(str(tmp_path / "c.csv"), clock=clock,
                           utcnow=lambda: datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
    assert writer.add(info(category="A", category_id="1"))
    assert writer.add(info(category="B", category_id="2"))
    writer.close()
    rows = list(csv.reader(open(tmp_path / "c.csv")))
    assert rows[1][:2] == ["0.0", "00:00:00"]
    assert rows[2][:2] == ["3600.4", "01:00:00"]
    assert rows[2][2] == "2026-01-01T00:00:00Z"
