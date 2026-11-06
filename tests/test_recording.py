import csv
import datetime
import os
import threading

import pytest

from app.recording import ChapterWriter, Recording, format_offset, select_quality

from fakes import FakeFd, FakeSession, FakeStream, info, make_settings, make_stream

NOW = datetime.datetime(2026, 11, 6, 20, 15, 0)


def run(tmp_path, fd, streams_extra=None, stream=None, live=None, **settings_kw):
    settings = make_settings(tmp_path, **settings_kw)
    streams = {"best": FakeStream(fd), **(streams_extra or {})}
    session = FakeSession(streams)
    rec = Recording(stream or make_stream(output="Twitch/Streamer"), live or info(title="My Title"),
                    settings, session_factory=lambda: session, now=lambda: NOW)
    return rec, session


def files(tmp_path):
    root = tmp_path / "rec"
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_select_quality():
    streams = {"720p": 1, "best": 2}
    assert select_quality("1080p60, 720p", streams) == "720p"
    assert select_quality("", streams) == "best"
    assert select_quality("480p", streams) == "best"


def test_format_offset():
    assert format_offset(3725.9) == "01:02:05"


def test_records_to_output_folder_with_chapters(tmp_path):
    rec, session = run(tmp_path, FakeFd([b"abc", b"def"]))
    rec.run()
    assert rec.state == "finished", rec.error
    assert files(tmp_path) == ["Twitch/Streamer/2026-11-06/20-15-00 - My Title.chapters.csv",
                               "Twitch/Streamer/2026-11-06/20-15-00 - My Title.ts"]
    assert (tmp_path / "rec/Twitch/Streamer/2026-11-06/20-15-00 - My Title.ts").read_bytes() == b"abcdef"
    rows = list(csv.DictReader(open(tmp_path / "rec/Twitch/Streamer/2026-11-06/20-15-00 - My Title.chapters.csv")))
    assert [(r["offset"], r["category"], r["category_id"], r["title"]) for r in rows] == [
        ("00:00:00", "Just Chatting", "509658", "My Title")]
    assert rec.file == "Twitch/Streamer/2026-11-06/20-15-00 - My Title.ts"
    assert session.calls[0][0] == "https://www.twitch.tv/streamer"
    assert session.calls[0][1] is None


def test_oauth_token_goes_to_api_header_only(tmp_path):
    rec, session = run(tmp_path, FakeFd([b"a"]), oauth_token="secret")
    rec.run()
    options = session.calls[0][1]
    assert options.get("api-header") == [("Authorization", "OAuth secret")]


def test_quality_preference(tmp_path):
    hq = FakeFd([b"hq"])
    rec, _ = run(tmp_path, FakeFd([b"best"]), streams_extra={"1080p60": FakeStream(hq)},
                 stream=make_stream(output="s", quality="1080p60,best"))
    rec.run()
    assert hq.closed


def test_no_streams_is_waiting(tmp_path):
    settings = make_settings(tmp_path)
    rec = Recording(make_stream(), info(), settings, session_factory=lambda: FakeSession({}), now=lambda: NOW)
    rec.run()
    assert rec.state == "waiting"
    assert not (tmp_path / "rec").exists()


def test_failure_keeps_data_as_incomplete(tmp_path):
    fd = FakeFd([b"abc"], fail=OSError(5, "boom http://secret"))
    rec, _ = run(tmp_path, fd)
    rec.run()
    assert rec.state == "error"
    assert "secret" not in rec.error and "errno 5" in rec.error
    assert any(f.endswith(".ts.incomplete") for f in files(tmp_path))
    assert fd.closed


def test_empty_recording_leaves_no_files(tmp_path):
    rec, _ = run(tmp_path, FakeFd([]))
    rec.run()
    assert files(tmp_path) == []


def test_stop_and_category_change(tmp_path):
    gate = threading.Event()
    rec, _ = run(tmp_path, FakeFd([b"a"], block=gate))
    rec.start()
    for _ in range(100):
        if rec.state == "recording":
            break
        threading.Event().wait(0.02)
    rec.update(info(category="Just Chatting", title="same category"))   # no new chapter
    rec.update(info(category="Elden Ring", category_id="512953", title="Now gaming"))
    rec.stop()
    gate.set()
    rec.join(5)
    assert rec.state == "finished"
    csv_file = next((tmp_path / "rec").rglob("*.chapters.csv"))
    rows = list(csv.DictReader(open(csv_file)))
    assert [r["category"] for r in rows] == ["Just Chatting", "Elden Ring"]
    assert any(f.endswith(".ts") for f in files(tmp_path))


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
