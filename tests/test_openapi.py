from pathlib import Path

import yaml
from openapi_spec_validator import validate

from app.web import create_app

from fakes import make_settings

SPEC = Path(__file__).resolve().parent.parent / "app" / "static" / "openapi.yaml"


def load():
    return yaml.safe_load(SPEC.read_text(encoding="utf-8"))


def test_spec_is_valid_openapi():
    validate(load())


def test_spec_matches_routes(tmp_path):
    app = create_app(make_settings(tmp_path))
    routes = set()
    for route in app.router.routes():
        if route.method in ("HEAD", "OPTIONS"):
            continue
        routes.add((route.method.lower(), route.resource.canonical))
    documented = {(method, path) for path, ops in load()["paths"].items()
                  for method in ops if method in ("get", "post", "put", "delete", "patch")}
    assert routes - documented == set(), "routes missing in openapi.yaml"
    assert documented - routes == set(), "openapi.yaml documents routes that do not exist"


async def test_openapi_is_served(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from app.settings import parse_networks
    client = TestClient(TestServer(create_app(make_settings(tmp_path, allowed_networks=parse_networks("127.0.0.1")))))
    await client.start_server()
    try:
        r = await client.get("/api/openapi.yaml")
        assert r.status == 200 and r.headers["Content-Type"].startswith("application/yaml")
        assert yaml.safe_load(await r.text())["openapi"].startswith("3.1")
    finally:
        await client.close()
