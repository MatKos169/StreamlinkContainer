import json
import os

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.settings import parse_networks
from app.web import client_allowed, create_app

from fakes import make_settings


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, allowed_networks=parse_networks("127.0.0.1/32"))


async def client_for(settings):
    client = TestClient(TestServer(create_app(settings)))
    await client.start_server()
    return client


def test_client_allowed():
    nets = parse_networks("10.0.0.0/24,192.168.1.5")
    assert client_allowed("10.0.0.196", nets)
    assert client_allowed("::ffff:10.0.0.7", nets)
    assert client_allowed("192.168.1.5", nets)
    assert not client_allowed("192.168.1.6", nets)
    assert not client_allowed("8.8.8.8", nets)
    assert not client_allowed(None, nets)


async def test_forbidden_outside_allowlist(tmp_path):
    settings = make_settings(tmp_path, allowed_networks=parse_networks("10.9.9.0/24"))
    client = await client_for(settings)
    try:
        assert (await client.get("/")).status == 403
        assert (await client.get("/api/streams")).status == 403
    finally:
        await client.close()


async def test_crud(settings):
    client = await client_for(settings)
    try:
        assert (await client.get("/")).status == 200
        r = await client.post("/api/streams", json={"channel": "https://www.twitch.tv/Alpha", "output": "Twitch/A"})
        assert r.status == 201
        created = await r.json()
        assert (created["channel"], created["output"]) == ("alpha", "Twitch/A")

        assert (await client.post("/api/streams", json={"channel": "alpha"})).status == 409
        assert (await client.post("/api/streams", json={"channel": "beta", "output": "../x"})).status == 400
        assert (await client.post("/api/streams", data="channel=x",
                                   headers={"Content-Type": "application/x-www-form-urlencoded"})).status == 415

        r = await client.put(f"/api/streams/{created['id']}", json={"enabled": False, "quality": "720p,best"})
        assert r.status == 200
        listed = (await (await client.get("/api/streams")).json())["streams"]
        assert listed == [{**created, "enabled": False, "quality": "720p,best"}]
        on_disk = json.load(open(settings.streams_file))
        assert on_disk["streams"][0]["output"] == "Twitch/A"

        assert (await client.put("/api/streams/nope", json={})).status == 404
        assert (await client.delete(f"/api/streams/{created['id']}")).status == 204
        assert (await (await client.get("/api/streams")).json())["streams"] == []
    finally:
        await client.close()


async def test_status(settings):
    client = await client_for(settings)
    try:
        assert (await (await client.get("/api/status")).json()) == {"status": None}
        import os
        os.makedirs(settings.config_dir, exist_ok=True)
        with open(settings.status_file, "w") as f:
            json.dump({"updated_at": "x"}, f)
        assert (await (await client.get("/api/status")).json())["status"] == {"updated_at": "x"}
    finally:
        await client.close()


async def test_settings_api(settings):
    client = await client_for(settings)
    try:
        data = await (await client.get("/api/settings")).json()
        assert data["settings"] == data["defaults"]
        assert data["settings"]["rescan_interval"] == 10

        r = await client.put("/api/settings", json={"rescan_interval": "15", "rescan_duration": 300})
        assert r.status == 200
        data = await r.json()
        assert (data["settings"]["rescan_interval"], data["settings"]["rescan_duration"]) == (15, 300)
        assert data["overrides"] == {"rescan_interval": 15, "rescan_duration": 300}

        # channel edits keep the settings
        await client.post("/api/streams", json={"channel": "alpha"})
        on_disk = json.load(open(settings.streams_file))
        assert on_disk["settings"] == {"rescan_interval": 15, "rescan_duration": 300}
        assert len(on_disk["streams"]) == 1

        assert (await client.put("/api/settings", json={"rescan_interval": 1})).status == 400
        assert (await client.put("/api/settings", json={"unknown": 1})).status == 400

        data = await (await client.put("/api/settings", json={"rescan_interval": None, "rescan_duration": None})).json()
        assert data["overrides"] == {}
        assert data["settings"] == data["defaults"]
    finally:
        await client.close()


async def test_jobs_api(settings):
    from app import jobs
    os.makedirs(os.path.join(settings.work_dir, "failed", "j1"))
    jobs.write_job(os.path.join(settings.work_dir, "failed", "j1"), {"channel": "a", "error": "ffmpeg exit code 1"})
    os.makedirs(os.path.join(settings.work_dir, "failed", "j2"))
    client = await client_for(settings)
    try:
        data = await (await client.get("/api/jobs")).json()
        assert [j["id"] for j in data["failed"]] == ["j1", "j2"]
        assert (await client.post("/api/jobs/j1/retry", data="",
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})).status == 415
        await client.put("/api/settings", json={"video_codec": "libx265", "ffmpeg_args": "-crf 28"})
        assert (await client.post("/api/jobs/j1/retry", json={})).status == 200
        retried = jobs.read_job(os.path.join(settings.work_dir, "queue", "j1"))
        assert (retried["video_codec"], retried["ffmpeg_args"], retried["error"]) == ("libx265", "-crf 28", None)
        assert (await client.post("/api/jobs/j1/retry", json={})).status == 404   # only failed jobs
        assert (await client.delete("/api/jobs/j2")).status == 204
        assert not os.path.exists(os.path.join(settings.work_dir, "failed", "j2"))
        assert (await client.delete("/api/jobs/..")).status in (400, 404)
    finally:
        await client.close()


async def test_settings_output_options(settings):
    client = await client_for(settings)
    try:
        data = await (await client.put("/api/settings", json={"folder_layout": "year", "keep_source": False,
                                                                 "audio_normalize": False})).json()
        assert (data["settings"]["folder_layout"], data["settings"]["keep_source"], data["settings"]["audio_normalize"]) == (
            "year", False, False)
        assert "output_year_month" in data["folder_layouts"]
        assert (await client.put("/api/settings", json={"folder_layout": "weird"})).status == 400
    finally:
        await client.close()



async def test_overview_api(settings):
    from app import jobs
    jobs.worker_heartbeat(settings.work_dir, "conv1", "idle")
    jobs.record_completed(settings.work_dir, "j1", {"file": "a.mp4"})
    client = await client_for(settings)
    try:
        data = await (await client.get("/api/overview")).json()
        assert data["counts"]["workers_online"] == 1 and data["counts"]["completed"] == 1
        assert data["counts"]["open"] == 0 and data["jobs"]["queue"] == []
    finally:
        await client.close()



async def test_done_api(settings):
    from app import jobs
    for job_id in ("d1", "d2", "d3"):
        path = os.path.join(settings.work_dir, "done", job_id)
        os.makedirs(path)
        jobs.write_job(path, {"channel": "a", "done_ts": 1.0})
    client = await client_for(settings)
    try:
        data = await (await client.get("/api/overview")).json()
        assert data["counts"]["done"] == 3 and [d["id"] for d in data["done"]] == ["d1", "d2", "d3"]
        assert (await (await client.delete("/api/done/d1")).json()) == {"deleted": 1}
        assert (await client.delete("/api/done/d1")).status == 404
        assert (await client.delete("/api/done/..")).status in (400, 404)
        assert (await (await client.delete("/api/done")).json()) == {"deleted": 2}
        assert os.listdir(os.path.join(settings.work_dir, "done")) == []
        r = await client.put("/api/settings", json={"done_retention_days": 3651})
        assert r.status == 400 and "days" in (await r.json())["error"]
    finally:
        await client.close()
