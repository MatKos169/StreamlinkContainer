# AGENTS.md

Guidance for coding agents working in this repository. Read it before changing anything.

## What this is

A Twitch recorder in three containers (see `README.md`):
- `recorder` (`python -m app.recorder`) polls Helix for all enabled channels and records live ones with the `streamlink` Python API, one thread per stream, into job folders in `/work` (`app/jobs.py`) with a `chapters.csv` of category changes.
- VOD downloads: the web UI writes requests to `/config/downloads/`; the recorder runs them (`app/vod.py`) into the same `/work` pipeline. Realtime mode fetches segments itself at playback pace; do not replace it with paced reading of streamlink's stream (streamlink prefetches regardless, measured).
- `converter` (`python -m app.converter`, image target `converter` with ffmpeg) claims queued jobs, writes an MP4 with chapters and moves it to `/recordings`. Several converters on several hosts may share one `/work`.
- `web` (`python -m app.web`, aiohttp) edits `/config/streams.json`, shows `/config/status.json` and the `/work` queue.

The recorder must keep working when the web container or all converters are down. Its only input is `streams.json`, which it re-reads when the file changes.

## Validation gate

- Run tests and Docker builds on the remote test host, not locally (ask the user for access).
- `pip install -r requirements-dev.txt && python -m pytest -q`; the full suite must pass.
- A bug fix comes with a test that fails without the fix.
- Mock `streamlink` and `twitchAPI` at their boundaries; tests never hit the network.

## Invariants

- **Never log, store or show exception messages from Twitch, streamlink or HTTP calls.** Use `twitch.error_summary` / `recording.describe_error`. Library messages can embed `client_secret`.
- **Never read, print or commit secrets** (`clientid.env`, any `*.env` except examples and `default.env`).
- Never use Twitch's private GQL API or its web client id; use Helix (official) or what streamlink provides.
- Output paths come from users: always go through `naming.clean_output_path` (no `..`, relative to `/recordings`).
- `streams.json`, `status.json`, `job.json` and `claim.json` are written atomically (`streams.write_json_atomic`).
- Job folders change state only by `os.rename` (`jobs.move`); that is what makes claims exclusive across converters. Never copy-then-delete between states.
- Output paths in `job.json` go through `clean_output_path` again in the converter (`place_file`); never overwrite an existing file in `/recordings`.
- The web UI only answers clients in `ALLOWED_NETWORKS`; mutating API calls require `Content-Type: application/json`.
- Containers run non-root with a read-only root filesystem; only `/config`, `/recordings` and `/tmp` are writable.

## Commits

Conventional Commits. Branches `feat|fix|chore|docs/<slug>`. Never push to `master` directly, never force-push.
