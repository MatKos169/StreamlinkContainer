# AGENTS.md

Guidance for coding agents working in this repository. Read it before changing anything.

## What this is

A Twitch recorder in two containers built from one image (see `README.md`):
- `recorder` (`python -m app.recorder`) polls Helix for all enabled channels and records live ones with the `streamlink` Python API, one thread per stream. It writes a `.chapters.csv` with category changes next to each video.
- `web` (`python -m app.web`, aiohttp) edits `/config/streams.json` and shows `/config/status.json`.

The recorder must keep working when the web container is down. Its only input is `streams.json`, which it re-reads when the file changes.

## Validation gate

- Run tests and Docker builds on the remote test host, not locally (ask the user for access).
- `pip install -r requirements-dev.txt && python -m pytest -q`; the full suite must pass.
- A bug fix comes with a test that fails without the fix.
- Mock `streamlink` and `twitchAPI` at their boundaries; tests never hit the network.

## Invariants

- **Never log, store or show exception messages from Twitch, streamlink or HTTP calls.** Use `twitch.error_summary` / `recording.describe_error`. Library messages can embed `client_secret`.
- **Never read, print or commit secrets** (`clientid.env`, any `*.env` except examples and `default.env`).
- Output paths come from users: always go through `naming.clean_output_path` (no `..`, relative to `/recordings`).
- `streams.json` and `status.json` are written atomically (`streams.write_json_atomic`).
- The web UI only answers clients in `ALLOWED_NETWORKS`; mutating API calls require `Content-Type: application/json`.
- Containers run non-root with a read-only root filesystem; only `/config`, `/recordings` and `/tmp` are writable.

## Commits

Conventional Commits. Branches `feat|fix|chore|docs/<slug>`. Never push to `master` directly, never force-push.
