import json
import os

from app import jobs


def make_job(work, state="recording", job_id="20261106-201500-a-abc123", video=b"data", name="video.ts.part", meta=None):
    path = os.path.join(work, state, job_id)
    os.makedirs(path)
    if video is not None:
        with open(os.path.join(path, name), "wb") as f:
            f.write(video)
    jobs.write_job(path, meta or {"id": job_id, "channel": "a"})
    return path


def test_finish_recording_renames_part_and_queues(tmp_path):
    work = str(tmp_path)
    make_job(work)
    path = jobs.finish_recording(work, "20261106-201500-a-abc123", {"complete": True})
    assert path == os.path.join(work, "queue", "20261106-201500-a-abc123")
    assert sorted(os.listdir(path)) == ["job.json", "video.ts"]
    assert jobs.read_job(path) == {"id": "20261106-201500-a-abc123", "channel": "a", "complete": True}
    assert jobs.video_file(path).endswith("video.ts")


def test_finish_recording_drops_empty_jobs(tmp_path):
    work = str(tmp_path)
    make_job(work, video=b"")
    assert jobs.finish_recording(work, "20261106-201500-a-abc123", {}) is None
    assert os.listdir(os.path.join(work, "recording")) == []


def test_recover_recordings(tmp_path):
    work = str(tmp_path)
    make_job(work, job_id="j1")
    make_job(work, job_id="j2", video=b"")
    assert jobs.recover_recordings(work) == ["j1"]
    assert jobs.read_job(os.path.join(work, "queue", "j1"))["complete"] is False


def test_claim_is_exclusive_and_oldest_first(tmp_path):
    work = str(tmp_path)
    make_job(work, "queue", "b", name="video.ts")
    make_job(work, "queue", "a", name="video.ts")
    assert jobs.claim_next(work, "conv1")[0] == "a"
    assert jobs.claim_next(work, "conv2")[0] == "b"
    assert jobs.claim_next(work, "conv3") == (None, None)
    claim = jobs.read_json(os.path.join(work, "converting", "a", jobs.CLAIM_FILE))
    assert claim["converter"] == "conv1" and claim["progress"] == 0.0
    assert jobs.still_owner(os.path.join(work, "converting", "a"), "conv1")
    assert not jobs.still_owner(os.path.join(work, "converting", "a"), "conv2")


def test_requeue_stale(tmp_path):
    work = str(tmp_path)
    make_job(work, "queue", "a", name="video.ts")
    make_job(work, "queue", "b", name="video.ts")
    jobs.claim_next(work, "dead")
    _, path_b = jobs.claim_next(work, "alive")
    claim_a = os.path.join(work, "converting", "a", jobs.CLAIM_FILE)
    data = json.load(open(claim_a))
    data["heartbeat"] -= jobs.STALE_SECONDS + 1
    json.dump(data, open(claim_a, "w"))
    assert jobs.requeue_stale(work) == ["a"]
    assert os.listdir(os.path.join(work, "queue")) == ["a"]
    assert not os.path.exists(os.path.join(work, "queue", "a", jobs.CLAIM_FILE))
    assert os.path.isdir(path_b)


def test_heartbeat_does_not_recreate_moved_job(tmp_path):
    work = str(tmp_path)
    make_job(work, "queue", "a", name="video.ts")
    _, path = jobs.claim_next(work, "c")
    jobs.move(work, "a", "converting", "failed")
    try:
        jobs.heartbeat(path, "c", 0.5)
    except OSError:
        pass
    assert not os.path.exists(path)


def test_release_returns_job_without_temp_files(tmp_path):
    work = str(tmp_path)
    make_job(work, "queue", "a", name="video.ts")
    _, path = jobs.claim_next(work, "c")
    open(os.path.join(path, "output.mp4"), "w").close()
    jobs.release(work, "a", "c")
    assert sorted(os.listdir(os.path.join(work, "queue", "a"))) == ["job.json", "video.ts"]


def test_list_jobs(tmp_path):
    work = str(tmp_path)
    make_job(work, "recording", "r", meta={"channel": "x", "title": "T", "destination": "x/a.mp4"})
    make_job(work, "queue", "q", name="video.ts")
    jobs.claim_next(work, "conv")
    result = jobs.list_jobs(work)
    assert [j["id"] for j in result["recording"]] == ["r"]
    assert result["recording"][0]["destination"] == "x/a.mp4"
    assert result["recording"][0]["size"] > 0
    assert result["converting"][0]["claim"]["converter"] == "conv"
    assert result["queue"] == [] and result["failed"] == []


def test_valid_job_id():
    assert jobs.valid_job_id("20261106-201500-a-abc123")
    for bad in ("", ".", "..", "a/b", "a\\b"):
        assert not jobs.valid_job_id(bad)


def test_workers_online_offline(tmp_path):
    work = str(tmp_path)
    jobs.worker_heartbeat(work, "a", "idle", poll_seconds=10)
    jobs.worker_heartbeat(work, "b/../x", "busy", job_id="j", progress=0.5, poll_seconds=30)
    later = jobs.read_json(os.path.join(work, "workers", "a.json"))["heartbeat"] + 70
    workers = {w["name"]: w for w in jobs.list_workers(work, now=later)}
    assert workers["a"]["online"] is False            # 70s > max(60, 3*10)
    assert workers["b/../x"]["online"] is True        # 70s <= 3*30
    assert sorted(os.listdir(os.path.join(work, "workers"))) == ["a.json", "b_.._x.json"]
    jobs.remove_worker(work, "a")
    assert [w["name"] for w in jobs.list_workers(work)] == ["b/../x"]


def test_history_and_overview(tmp_path):
    work = str(tmp_path)
    for i in range(7):
        jobs.record_completed(work, f"j{i}", {"file": f"f{i}.mp4"})
    make_job(work, "queue", "q1", name="video.ts")
    make_job(work, "queue", "q2", name="video.ts")
    make_job(work, "recording", "r1")
    make_job(work, "failed", "f1", name="video.ts")
    jobs.claim_next(work, "conv")                     # q1 -> converting
    claim_path = os.path.join(work, "converting", "q1", jobs.CLAIM_FILE)
    claim = json.load(open(claim_path))
    claim.update(progress=0.25, started=claim["heartbeat"] - 100)
    json.dump(claim, open(claim_path, "w"))
    jobs.worker_heartbeat(work, "conv", "busy", job_id="q1", progress=0.25)
    ov = jobs.overview(work, now=claim["heartbeat"])
    assert ov["counts"] == {"open": 1, "running": 1, "recording": 1, "failed": 1, "completed": 7,
                            "workers_online": 1, "workers_total": 1, "done": 0, "done_size": 0}
    assert ov["running"][0]["converter"] == "conv"
    assert ov["running"][0]["elapsed_seconds"] == 100 and ov["running"][0]["eta_seconds"] == 300
    assert len(ov["recent"]) == 5 and ov["recent"][0]["file"] == "f6.mp4"


def test_history_is_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "HISTORY_KEEP", 3)
    for i in range(5):
        jobs.record_completed(str(tmp_path), f"j{i}", {})
    assert sorted(os.listdir(tmp_path / "history")) == ["j2.json", "j3.json", "j4.json"]


def test_done_list_delete_prune(tmp_path):
    work = str(tmp_path)
    make_job(work, "done", "old", name="video.ts", meta={"channel": "a", "done_ts": 1000.0, "converted_to": "a/x.mp4"})
    make_job(work, "done", "new", name="video.ts", meta={"channel": "b", "done_ts": 1000.0 + 9 * 86400})
    make_job(work, "done", "third", name="video.ts", meta={"channel": "c", "done_ts": 1000.0 + 9 * 86400})
    entries = {e["id"]: e for e in jobs.list_done(work)}
    assert entries["old"]["converted_to"] == "a/x.mp4" and entries["old"]["size"] > 0
    now = 1000.0 + 10 * 86400
    assert jobs.prune_done(work, 0, now=now) == []            # 0 = keep forever
    assert jobs.prune_done(work, 7, now=now) == ["old"]
    assert jobs.delete_done(work, "../queue") == 0
    assert jobs.delete_done(work, "new") == 1
    assert [e["id"] for e in jobs.list_done(work)] == ["third"]
    assert jobs.delete_done(work) == 1
    assert jobs.list_done(work) == []
