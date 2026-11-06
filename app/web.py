"""Web container: edit the channel list (streams.json) and show the recorder's status.

Only clients from ALLOWED_NETWORKS get access. The recorder does not depend on
this process; it picks up changes to streams.json on its next check.
"""
import ipaddress
import json
import logging
import os
import sys
import threading
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

from . import jobs
from .naming import FOLDER_LAYOUTS
from .settings import SettingsError, load_settings
from .streams import OPTION_LIMITS, StreamConfig, load_config, save_config

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")
SETTINGS_KEY = web.AppKey("settings", object)
LOCK_KEY = web.AppKey("lock", object)


def client_allowed(remote, networks):
    try:
        address = ipaddress.ip_address(remote)
    except (TypeError, ValueError):
        return False
    if getattr(address, "ipv4_mapped", None):
        address = address.ipv4_mapped
    return any(address in network for network in networks)


@web.middleware
async def ip_allowlist(request, handler):
    if not client_allowed(request.remote, request.app[SETTINGS_KEY].allowed_networks):
        logger.warning("Rejected request from %s", request.remote)
        raise web.HTTPForbidden(text="Forbidden")
    return await handler(request)


def _error(status, message):
    return web.json_response({"error": message}, status=status)


async def _read_json(request):
    # Requiring application/json also blocks simple cross-site form posts.
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="Content-Type must be application/json")
    try:
        return await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="invalid JSON") from None


def _path(app):
    return app[SETTINGS_KEY].streams_file


def _duplicate(streams, candidate):
    return any(s.channel == candidate.channel and s.id != candidate.id for s in streams)


async def index(request):
    return web.FileResponse(STATIC_DIR / "index.html")


async def list_streams(request):
    try:
        config = load_config(_path(request.app))
    except ValueError as error:
        return _error(500, str(error))
    return web.json_response({"streams": [asdict(s) for s in config.streams]})


async def create_stream(request):
    data = await _read_json(request)
    with request.app[LOCK_KEY]:
        config = load_config(_path(request.app))
        try:
            stream = StreamConfig.from_input({**data, "id": None})
        except ValueError as error:
            return _error(400, str(error))
        if _duplicate(config.streams, stream):
            return _error(409, f"channel {stream.channel} is already configured")
        config.streams.append(stream)
        save_config(_path(request.app), config)
    return web.json_response(asdict(stream), status=201)


async def update_stream(request):
    sid = request.match_info["id"]
    data = await _read_json(request)
    with request.app[LOCK_KEY]:
        config = load_config(_path(request.app))
        index_ = next((i for i, s in enumerate(config.streams) if s.id == sid), None)
        if index_ is None:
            return _error(404, "unknown stream")
        try:
            stream = StreamConfig.from_input({**asdict(config.streams[index_]), **data}, stream_id=sid)
        except ValueError as error:
            return _error(400, str(error))
        if _duplicate(config.streams, stream):
            return _error(409, f"channel {stream.channel} is already configured")
        config.streams[index_] = stream
        save_config(_path(request.app), config)
    return web.json_response(asdict(stream))


async def delete_stream(request):
    sid = request.match_info["id"]
    with request.app[LOCK_KEY]:
        config = load_config(_path(request.app))
        remaining = [s for s in config.streams if s.id != sid]
        if len(remaining) == len(config.streams):
            return _error(404, "unknown stream")
        config.streams = remaining
        save_config(_path(request.app), config)
    return web.Response(status=204)


def _settings_response(app, config):
    defaults = app[SETTINGS_KEY].defaults
    return web.json_response({
        "settings": asdict(config.options(defaults)),
        "defaults": asdict(defaults),
        "overrides": config.settings,
        "limits": OPTION_LIMITS,
        "folder_layouts": list(FOLDER_LAYOUTS),
    })


async def get_settings(request):
    try:
        config = load_config(_path(request.app))
    except ValueError as error:
        return _error(500, str(error))
    return _settings_response(request.app, config)


async def update_settings(request):
    """Body: {name: value} to override, {name: null} to go back to the env default."""
    data = await _read_json(request)
    if not isinstance(data, dict):
        return _error(400, "settings must be an object")
    with request.app[LOCK_KEY]:
        config = load_config(_path(request.app))
        overrides = {**config.settings, **data}
        overrides = {k: v for k, v in overrides.items() if v is not None}
        try:
            # Normalized values (e.g. "30" -> 30) are what gets stored.
            merged = request.app[SETTINGS_KEY].defaults.merged(overrides)
        except ValueError as error:
            return _error(400, str(error))
        config.settings = {k: getattr(merged, k) for k in overrides}
        save_config(_path(request.app), config)
    return _settings_response(request.app, config)


async def list_jobs(request):
    return web.json_response(jobs.list_jobs(request.app[SETTINGS_KEY].work_dir))


def _failed_job(request):
    job_id = request.match_info["id"]
    if not jobs.valid_job_id(job_id):
        raise web.HTTPBadRequest(text="invalid job id")
    work = request.app[SETTINGS_KEY].work_dir
    if not os.path.isdir(jobs.job_dir(work, "failed", job_id)):
        raise web.HTTPNotFound(text="no failed job with this id")
    return work, job_id


async def retry_job(request):
    await _read_json(request)  # JSON only: blocks cross-site form posts
    work, job_id = _failed_job(request)
    if jobs.move(work, job_id, "failed", "queue") is None:
        return _error(404, "no failed job with this id")
    return web.json_response({"id": job_id, "state": "queue"})


async def delete_job(request):
    work, job_id = _failed_job(request)
    jobs.remove(jobs.job_dir(work, "failed", job_id))
    return web.Response(status=204)


async def get_status(request):
    try:
        with open(request.app[SETTINGS_KEY].status_file, encoding="utf-8") as f:
            status = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        status = None
    return web.json_response({"status": status})


def create_app(settings):
    app = web.Application(middlewares=[ip_allowlist], client_max_size=64 * 1024)
    app[SETTINGS_KEY] = settings
    app[LOCK_KEY] = threading.Lock()
    app.add_routes([
        web.get("/", index),
        web.get("/api/streams", list_streams),
        web.post("/api/streams", create_stream),
        web.put("/api/streams/{id}", update_stream),
        web.delete("/api/streams/{id}", delete_stream),
        web.get("/api/status", get_status),
        web.get("/api/settings", get_settings),
        web.put("/api/settings", update_settings),
        web.get("/api/jobs", list_jobs),
        web.post("/api/jobs/{id}/retry", retry_job),
        web.delete("/api/jobs/{id}", delete_job),
    ])
    return app


def main():
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings()
    except SettingsError as error:
        logger.error("Configuration error: %s", error)
        sys.exit(2)
    os.makedirs(settings.config_dir, exist_ok=True)
    logger.info("Allowed networks: %s", ", ".join(str(n) for n in settings.allowed_networks))
    web.run_app(create_app(settings), port=settings.web_port, access_log=None)


if __name__ == "__main__":
    main()
