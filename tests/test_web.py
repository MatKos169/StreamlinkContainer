import json

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
