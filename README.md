# Streamlink Recorder

Records Twitch streams automatically. You add channels in a small web UI; a
recorder container records every live channel into a work folder. A converter
container then turns each recording into an MP4 with chapters (one per Twitch
category), normalized audio and metadata, and moves it into the channel's
output folder, organized by year/month for Jellyfin.

Fork of [liofal/streamlink](https://github.com/liofal/streamlink); the converter follows
[MatKos169/ffmpeg](https://github.com/MatKos169/ffmpeg).

## Containers

| Service | What it does | Volumes |
|---|---|---|
| `recorder` | Checks all enabled channels every *check interval* (one Twitch API call for up to 100 channels) and records live ones, one thread per stream, into `/work/recording/`. Finished recordings go to `/work/queue/`. | `/config`, `/work` |
| `converter` | Takes jobs from `/work/queue/`, writes an MP4 with chapters and moves it to `/recordings/...`. Any number can run, also on other hosts. | `/work`, `/recordings` |
| `web` | Web UI (port 8080): channels, settings, recorder status, processing queue (retry/delete failed jobs). | `/config`, `/work` |

**The recorder does not need the web container or a converter.** If no converter
runs, recordings wait in `/work/queue/` and are converted later.

### Work folder

```
/work/recording/<job>/    being recorded: video.ts.part, chapters.csv, job.json
/work/queue/<job>/        waiting for a converter
/work/converting/<job>/   claimed by a converter (claim.json: name, heartbeat, progress)
/work/failed/<job>/       conversion failed; "Erneut versuchen"/"Löschen" in the UI
/work/done/<job>/         original .ts kept after conversion ("Original behalten")
```

### Several converters / other hosts

A converter claims a job by renaming its folder from `queue/` to `converting/`.
The rename is atomic, also on NFS, so converters never take the same job. A
converter refreshes its claim every 10 s. If a claim is older than 10 minutes
(converter crashed or host gone), another converter puts the job back in the queue.
On a normal stop, a converter returns its current job to the queue.

- Same host: `docker compose up -d --scale converter=2`
- Other host: mount the same `work` and `recordings` shares there, then
  ```bash
  WORK_PATH=/mnt/nas/streamlink-work RECORDINGS_PATH=/mnt/nas/twitch CONVERTER_NAME=host2 \
    docker compose -f docker-compose.converter.yml up -d --build
  ```
  The user in `PUID`/`PGID` must be able to write both shares.

## Setup

```bash
cp clientid.env.example clientid.env      # add TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET
mkdir -p config work recordings && sudo chown 1027:100 config work recordings   # or set PUID/PGID
docker compose up -d --build
```

Then open `http://<host>:8080`.

Create the Twitch credentials at <https://dev.twitch.tv/console/apps> (any
redirect URL, category "Application Integration").

## Configuration

### In the web UI ("Einstellungen")

| Setting | Default (env) | Meaning |
|---|---|---|
| Check interval | `CHECK_INTERVAL=60` | Seconds between Twitch checks (10–3600) |
| Default quality | `DEFAULT_QUALITY=best` | Quality when a channel has none, e.g. `1080p60,720p60,best` (first match wins) |
| Re-check every | `RESCAN_INTERVAL=10` | After a stream ends, probe the channel this often (5–600 s) … |
| Re-check for | `RESCAN_DURATION=120` | … for this long (0–3600 s, 0 = off) |
| Subfolders ("Unterordner") | `FOLDER_LAYOUT=output_year_month` | `output_year_month`: `<folder>-2026/11/` (like the old ffmpeg container), `year_month`: `<folder>/2026/11/`, `year`, `date` (`<folder>/2026-11-06/`), `none` |
| Keep original ("Original behalten") | `KEEP_SOURCE=true` | Keep the recorded `.ts` in `/work/done/` after converting, otherwise delete it |
| Normalize audio ("Lautstärke normalisieren") | `AUDIO_NORMALIZE=true` | `loudnorm=I=-14:TP=-1.5:LRA=11`, audio re-encoded as AAC 192k. Off: audio copied unchanged. Video is always copied |

Values saved in the UI are stored in `streams.json` and override the env values. "Reset"
goes back to the env values. The recorder applies changes within a second.

**Quick re-check after a stream ends:** when a recording ends (stream over or
dropped), the recorder probes the channel every *re-check interval* seconds for
*re-check duration* seconds. It probes directly with streamlink, not through
the Twitch API, because the API often reports a restarted stream 30–90 s late.
If the stream is back, a new file is started (with the last known category in
its chapters CSV). After the window, the channel is checked at the normal interval again.

### Environment

Credentials, access control and paths are only set by environment variables
(`default.env`, `clientid.env`, or the shell for the compose variables):

| Variable | Default | Meaning |
|---|---|---|
| `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` | – | Twitch app credentials (recorder only, required) |
| `TWITCH_OAUTH_TOKEN` | – | Optional browser `auth-token` cookie, so Turbo/subscriptions apply (no ads) |
| `CHECK_INTERVAL`, `DEFAULT_QUALITY`, `RESCAN_INTERVAL`, `RESCAN_DURATION`, `FOLDER_LAYOUT`, `KEEP_SOURCE`, `AUDIO_NORMALIZE` | see above | Defaults for the UI settings |
| `CONVERTER_NAME` | container hostname | Converter name shown in the UI |
| `CONVERTER_POLL_INTERVAL` | `10` | Seconds a converter waits when the queue is empty |
| `ALLOWED_NETWORKS` | private ranges | Who may open the web UI: comma-separated IPs/CIDRs, e.g. `10.0.0.0/24,192.168.1.20` |
| `TZ` | `Europe/Berlin` (in `default.env`) | Time zone for file names |
| `PUID`, `PGID` | `1027`, `100` | User/group the containers run as (owner of the volumes) |
| `CONFIG_PATH`, `WORK_PATH`, `RECORDINGS_PATH` | `./config`, `./work`, `./recordings` | Host folders for the volumes |
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
/recordings/<output folder>-2026/11/2026-11-06 20-15-00 - <title>.mp4     (layout output_year_month)
/recordings/<output folder>/2026/11/2026-11-06 20-15-00 - <title>.mp4     (layout year_month)
```

- The date and time in the file name are when the recording started, so files sort correctly in any layout.
- If the file exists already, `_1`, `_2`, … is appended.
- Each MP4 has one chapter per Twitch category, starting at 0, plus `title` (stream title),
  `artist` (channel), `date` and `comment` (channel and categories) metadata.
- Across file systems, the MP4 is copied under a hidden temp name first, so Jellyfin never sees a half-copied file.
- If a recording breaks off with an error, it is still converted and marked "unvollständig" in the UI.
- On `docker compose down`/SIGTERM, running recordings are closed and queued. After a recorder crash,
  leftovers in `/work/recording/` are queued on the next start.
- After a dropout, the quick re-check starts a new recording as soon as the stream is back.

`chapters.csv` (in the job folder) gets one row at the start and one per category change:

```csv
offset_seconds,offset,timestamp_utc,category_id,category,title
0.0,00:00:00,2026-11-06T18:21:54Z,26936,Music,lofi hip hop radio
1834.2,00:30:34,2026-11-06T18:52:28Z,509658,Just Chatting,new title
```

Category changes are detected on the next check, so chapters start up to one
check interval late.

## Development

Tests run with pytest; Twitch and streamlink are mocked. The converter tests use a real ffmpeg if installed (skipped otherwise):

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
```

| Path | Role |
|---|---|
| `app/recorder.py` | Recorder entry point, `Supervisor` loop (reload config, check Twitch, start/stop recordings, write status) |
| `app/recording.py` | `Recording` thread (streamlink → job folder), `ChapterWriter` |
| `app/jobs.py` | Work-folder queue: states, claim/heartbeat/requeue, listing |
| `app/converter.py` | Converter entry point: chapters/metadata, ffmpeg, placing the MP4 |
| `app/twitch.py` | Helix batch live check (`get_live`) |
| `app/web.py`, `app/static/index.html` | Web UI and JSON API, IP allowlist |
| `app/streams.py` | `streams.json` model, validation, atomic writes |
| `app/naming.py` | Safe file/folder names |
| `app/settings.py` | Environment settings |
