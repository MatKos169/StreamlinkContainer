# Streamlink Recorder

Records Twitch streams automatically. You add channels in a small web UI; a
recorder container checks them regularly and records every channel that is
live. Each channel has its own output folder. Next to every video it writes a
CSV with the stream's category changes, for turning them into chapters later.

Fork of [liofal/streamlink](https://github.com/liofal/streamlink), reduced to Twitch-only
recording with a multi-channel web UI.

## Containers

| Service | What it does | Needs |
|---|---|---|
| `recorder` | Checks all enabled channels every `CHECK_INTERVAL` seconds (one Twitch API call for up to 100 channels) and records live ones, one thread per stream. | Twitch app credentials, `/config`, `/recordings` |
| `web` | Web UI on port 8080 to add, edit, disable and remove channels, and shows the recorder's status. | `/config` |

Both share `/config/streams.json` (the channel list, written by the web UI) and
`/config/status.json` (written by the recorder). **The recorder does not need the
web container.** You can stop `web`, and recordings keep running. The recorder
picks up changes to `streams.json` on its next check, without a restart.

## Setup

```bash
cp clientid.env.example clientid.env      # add TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET
mkdir -p config recordings && sudo chown 1027:100 config recordings   # or set PUID/PGID
docker compose up -d --build
```

Then open `http://<host>:8080`.

Create the Twitch credentials at <https://dev.twitch.tv/console/apps> (any
redirect URL, category "Application Integration").

## Configuration

Global settings are environment variables (`default.env`, `clientid.env`, or the
shell for the compose variables):

| Variable | Default | Meaning |
|---|---|---|
| `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` | – | Twitch app credentials (recorder only, required) |
| `TWITCH_OAUTH_TOKEN` | – | Optional browser `auth-token` cookie, so Turbo/subscriptions apply (no ads) |
| `CHECK_INTERVAL` | `60` | Seconds between checks (minimum 10) |
| `DEFAULT_QUALITY` | `best` | Quality when a channel has none, e.g. `1080p60,720p60,best` (first match wins) |
| `ALLOWED_NETWORKS` | private ranges | Who may open the web UI: comma-separated IPs/CIDRs, e.g. `10.0.0.0/24,192.168.1.20` |
| `TZ` | `Europe/Berlin` (in `default.env`) | Time zone for file names |
| `PUID`, `PGID` | `1027`, `100` | User/group the containers run as (owner of the volumes) |
| `CONFIG_PATH`, `RECORDINGS_PATH` | `./config`, `./recordings` | Host folders for the volumes |
| `WEB_PORT` | `8080` | Host port of the web UI |

**Note on `ALLOWED_NETWORKS`:** the web container sees the client's real IP for
requests from other machines. Requests to `localhost` on the Docker host arrive
from the Docker bridge gateway (for example `172.19.0.1`). Add that address if you
open the UI on the host itself. Behind a reverse proxy, allow the proxy's IP.

Per-channel settings (in the UI):

- **Channel**: login or URL (`https://www.twitch.tv/name`)
- **Output folder**: relative to `/recordings`, may contain subfolders (`Twitch/Name`). Empty means the channel name. `..` is rejected.
- **Quality**: overrides `DEFAULT_QUALITY`
- **Active**: unchecking it stops a running recording

## Output

```
/recordings/<output folder>/<YYYY-MM-DD>/<HH-MM-SS> - <title>.ts
/recordings/<output folder>/<YYYY-MM-DD>/<HH-MM-SS> - <title>.chapters.csv
```

- While recording, the video is written as `.part` and renamed when the stream ends.
- If a recording breaks off with an error, the data is kept as `.ts.incomplete`.
- On `docker compose down`/SIGTERM, running recordings are closed cleanly and kept as `.ts`.
- If the stream is still live after a recording ended (e.g. a dropout), a new file is started on the next check.

The chapters CSV gets one row at the start and one per category change. It is
updated while recording:

```csv
offset_seconds,offset,timestamp_utc,category_id,category,title
0.0,00:00:00,2026-11-06T18:21:54Z,26936,Music,lofi hip hop radio
1834.2,00:30:34,2026-11-06T18:52:28Z,509658,Just Chatting,new title
```

The offset counts from the recording start. Category changes are detected on the next
check, so they are up to `CHECK_INTERVAL` seconds late.

## Development

Tests run with pytest; external services (Twitch, streamlink) are mocked:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
```

| Path | Role |
|---|---|
| `app/recorder.py` | Recorder entry point, `Supervisor` loop (reload config, check Twitch, start/stop recordings, write status) |
| `app/recording.py` | `Recording` thread (streamlink → file), `ChapterWriter` |
| `app/twitch.py` | Helix batch live check (`get_live`) |
| `app/web.py`, `app/static/index.html` | Web UI and JSON API, IP allowlist |
| `app/streams.py` | `streams.json` model, validation, atomic writes |
| `app/naming.py` | Safe file/folder names |
| `app/settings.py` | Environment settings |
