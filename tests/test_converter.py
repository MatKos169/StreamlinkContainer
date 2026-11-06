import json
import os
import shutil
import subprocess
import threading

import pytest

from app import jobs
from app.converter import Converter, build_metadata, place_file, read_chapters

from fakes import make_settings

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def write_csv(path, rows):
    with open(path, "w") as f:
        f.write("offset_seconds,offset,timestamp_utc,category_id,category,title\n")
        for offset, category in rows:
            f.write(f"{offset},x,x,1,{category},t\n")


def test_read_chapters(tmp_path):
    path = tmp_path / "chapters.csv"
    write_csv(path, [(1.5, "Just Chatting"), (60.0, "Elden Ring"), (90.0, "Elden Ring"), (500.0, "Too late")])
    assert read_chapters(str(path), 120.0) == [(0, 60000, "Just Chatting"), (60000, 120000, "Elden Ring")]


def test_read_chapters_missing_or_empty(tmp_path):
    assert read_chapters(str(tmp_path / "nope.csv"), 10.0) == [(0, 10000, "Ohne Kategorie")]
    path = tmp_path / "c.csv"
    write_csv(path, [(0, "")])
    assert read_chapters(str(path), 10.0) == [(0, 10000, "Ohne Kategorie")]


def test_build_metadata_escapes():
    text = build_metadata({"title": "a=b;c#d\\e\nf", "channel": "chan", "started_at": "2026-11-06T20:15:00+01:00"},
                          [(0, 1000, "A=1"), (1000, 2000, "B")])
    assert text.startswith(";FFMETADATA1\n")
    assert r"title=a\=b\;c\#d\\e f" + "\n" in text
    assert "date=2026-11-06\n" in text
    assert "comment=Twitch: chan | A\\=1, B\n" in text
    assert text.count("[CHAPTER]") == 2 and "START=1000\nEND=2000\ntitle=B\n" in text


def test_place_file_numbers_collisions(tmp_path):
    root = tmp_path / "rec"
    for i in range(3):
        src = tmp_path / f"src{i}"
        src.write_text(str(i))
        place_file(str(src), str(root), "Chan-2026/11/x.mp4")
    assert sorted(p.name for p in (root / "Chan-2026" / "11").iterdir()) == ["x.mp4", "x_1.mp4", "x_2.mp4"]


def test_place_file_rejects_traversal(tmp_path):
    src = tmp_path / "s"
    src.write_text("x")
    with pytest.raises(ValueError):
        place_file(str(src), str(tmp_path / "rec"), "../evil.mp4")


def test_ffmpeg_command_normalize_switch(tmp_path):
    conv = Converter(make_settings(tmp_path), "c")
    with_norm = conv.ffmpeg_command("in.ts", "m", "out.mp4", normalize=True)
    assert "loudnorm=I=-14:TP=-1.5:LRA=11" in with_norm and "192k" in with_norm
    assert with_norm[with_norm.index("-c:v") + 1] == "copy"
    copy = conv.ffmpeg_command("in.ts", "m", "out.mp4", normalize=False)
    assert "loudnorm=I=-14:TP=-1.5:LRA=11" not in copy and copy[copy.index("-c:a") + 1] == "copy"


def queue_job(settings, job_id, meta, video_bytes=None, video_src=None, chapters=None):
    path = os.path.join(settings.work_dir, "queue", job_id)
    os.makedirs(path)
    if video_src:
        shutil.copy(video_src, os.path.join(path, "video.ts"))
    else:
        with open(os.path.join(path, "video.ts"), "wb") as f:
            f.write(video_bytes or b"not a video")
    if chapters:
        write_csv(os.path.join(path, "chapters.csv"), chapters)
    jobs.write_job(path, {"id": job_id, "channel": "chan", "title": "T", **meta})
    return path


def test_broken_video_goes_to_failed(tmp_path):
    settings = make_settings(tmp_path)
    queue_job(settings, "j1", {"destination": "chan/x.mp4"})
    conv = Converter(settings, "c", ffprobe="false")
    job_id, path = jobs.claim_next(settings.work_dir, "c")
    conv.process(job_id, path, threading.Event())
    failed = os.path.join(settings.work_dir, "failed", "j1")
    meta = jobs.read_job(failed)
    assert meta["attempts"] == 1 and "ffprobe" in meta["error"] and meta["failed_by"] == "c"
    assert not os.path.exists(os.path.join(failed, jobs.CLAIM_FILE))


@pytest.fixture(scope="module")
def sample_ts(tmp_path_factory):
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not installed")
    path = tmp_path_factory.mktemp("media") / "sample.ts"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "12",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-f", "mpegts", str(path)], check=True)
    return str(path)


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-show_format", "-show_streams", "-of", "json", path],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.mark.parametrize("normalize, keep", [(True, False), (False, True)])
def test_real_conversion(tmp_path, sample_ts, normalize, keep):
    settings = make_settings(tmp_path)
    queue_job(settings, "j1", {"destination": "chan-2026/11/2026-11-06 20-15-00 - T.mp4",
                               "audio_normalize": normalize, "keep_source": keep, "started_at": "2026-11-06T20:15:00+01:00"},
              video_src=sample_ts, chapters=[(0, "Just Chatting"), (5.0, "Elden Ring")])
    conv = Converter(settings, "c")
    job_id, path = jobs.claim_next(settings.work_dir, "c")
    conv.process(job_id, path, threading.Event())

    out = os.path.join(settings.recordings_dir, "chan-2026/11/2026-11-06 20-15-00 - T.mp4")
    assert os.path.exists(out), os.listdir(settings.work_dir + "/failed")
    info = probe(out)
    assert [c["tags"]["title"] for c in info["chapters"]] == ["Just Chatting", "Elden Ring"]
    assert float(info["chapters"][1]["start_time"]) == pytest.approx(5.0, abs=0.01)
    assert info["format"]["tags"]["title"] == "T" and info["format"]["tags"]["artist"] == "chan"
    # MP4 chapters come with a text track that ffprobe lists as "data".
    assert {s["codec_type"] for s in info["streams"]} <= {"video", "audio", "data"}
    assert {"video", "audio"} <= {s["codec_type"] for s in info["streams"]}
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert audio["codec_name"] == "aac"
    if normalize:
        assert 150_000 <= int(audio["bit_rate"]) <= 230_000
    assert os.listdir(os.path.join(settings.work_dir, "converting")) == []
    done_dir = os.path.join(settings.work_dir, "done")
    done = os.listdir(done_dir) if os.path.isdir(done_dir) else []
    if keep:
        assert done == ["j1"]
        assert jobs.read_job(os.path.join(settings.work_dir, "done", "j1"))["converted_to"].endswith("T.mp4")
    else:
        assert done == []


def test_stop_releases_job(tmp_path, sample_ts):
    settings = make_settings(tmp_path)
    queue_job(settings, "j1", {"destination": "chan/x.mp4"}, video_src=sample_ts)
    conv = Converter(settings, "c")
    job_id, path = jobs.claim_next(settings.work_dir, "c")
    stop = threading.Event()
    stop.set()
    conv.process(job_id, path, stop)
    assert os.listdir(os.path.join(settings.work_dir, "queue")) == ["j1"]
    assert sorted(os.listdir(os.path.join(settings.work_dir, "queue", "j1"))) == ["job.json", "video.ts"]
    assert not os.path.exists(os.path.join(settings.recordings_dir, "chan", "x.mp4"))
