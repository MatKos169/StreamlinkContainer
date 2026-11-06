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
| `web` | Web UI (port 8080): channels, settings, recorder status, processing overview (jobs open/running/completed/failed, converters online, progress with remaining time, kept originals), retry/delete failed jobs, delete kept originals. | `/config`, `/work` |

**The recorder does not need the web container or a converter.** If no converter
runs, recordings wait in `/work/queue/` and are converted later.

### Work folder

```
/work/recording/<job>/    being recorded: video.ts.part, chapters.csv, job.json
/work/queue/<job>/        waiting for a converter
/work/converting/<job>/   claimed by a converter (claim.json: name, heartbeat, progress)
/work/failed/<job>/       conversion failed; "Erneut versuchen"/"Löschen" in the UI
/work/done/<job>/         original .ts kept after conversion ("Original behalten")
/work/workers/<name>.json converter heartbeats ("Worker online": seen within max(60 s, 3 x poll interval))
/work/history/<job>.json  one record per finished conversion ("Jobs abgeschlossen", last 1000 kept)
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

## VOD downloads

In the web UI ("VOD-Downloads"), enter a Twitch VOD URL (`https://www.twitch.tv/videos/<id>`),
optionally with an output folder, quality, mode and pace. The recorder downloads it into
`/work`, then it is converted like a recording.

| Mode | What happens |
|---|---|
| **Echtzeit** (realtime, default) | streamlink resolves the VOD (access token, playlist). The recorder then fetches the segments **one by one at playback speed × pace** (keeping ~10 s of playback time as lead), so bandwidth is spread over the VOD's length and Twitch is not hit with a burst. A 3 h VOD at pace 1 takes 3 h, at pace 4 about 45 min. |
| **Download** | streamlink's normal behaviour: as fast as possible, with *VOD-Download-Threads* parallel segment downloads. |

Why not just let streamlink pace itself: when writing to a file, streamlink fetches as fast
as the network allows. It only appears real-time when a player reads at playback speed, and even then
it queues up to 20 segments ahead plus a 16 MB buffer. Measured with a 2-min test VOD: download
mode got all 48 MB in 0.2 s. Pacing only the reading still got all segments within 0.4 s. The
own segment fetching delivered one 4 s segment per second at pace ×4.

- **Metadata:** title, channel and stream start date come from Helix (with `TWITCH_CLIENT_ID`/`SECRET`), with streamlink's metadata as fallback. The file is named after the stream's start, like a recording.
- **Output folder:** the one entered, else the folder of that channel if it is in the channel list, else the channel name.
- **Chapters:** one chapter with the VOD's category. Helix has no endpoint for category changes within a VOD.
- **Subscriber-only VODs** need `TWITCH_OAUTH_TOKEN`.
- **Interrupting:** cancelling or stopping the recorder discards the partial download. A pending download starts again from the beginning after a restart. Failed downloads can be retried or removed in the UI.
- Encrypted playlists (not used by Twitch VODs) fall back to streamlink's reader with paced reading.

Requests live in `/config/downloads/` (`<id>.json` pending, `<id>.cancel` cancel marker,
`<id>.failed.json` failed), so the recorder works through them without the web container.

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
| Delete originals after ("Originale löschen nach") | `DONE_RETENTION_DAYS=0` | The recorder deletes kept originals in `/work/done/` older than this many days, hourly (0 = never). They can also be deleted by hand in the UI |
| Normalize audio ("Lautstärke normalisieren") | `AUDIO_NORMALIZE=true` | `loudnorm=I=-14:TP=-1.5:LRA=11`, audio re-encoded as AAC 192k. Off: audio copied unchanged |
| Max. parallel Twitch streams ("Max. parallele Twitch-Streams") | `MAX_PARALLEL_STREAMS=0` | Live recordings + VOD downloads together. When reached, no new VOD download starts (it waits, shown as "Limit erreicht"); live recordings always run, and running downloads continue. After a stream ends, its channel keeps its slot for *slot cooldown* seconds, so it can come back. 0 = no limit |
| Slot cooldown ("Platz reservieren nach Stream-Ende") | `SLOT_COOLDOWN=120` | Seconds a channel keeps its slot after its stream ended (0 = free at once). "Platz freigeben" in the channel list releases it early (the quick re-check goes on; if the stream comes back it is recorded anyway) |
| VOD mode / pace / threads / parallel | `VOD_MODE=realtime`, `VOD_SPEED=1`, `VOD_SEGMENT_THREADS=3`, `VOD_PARALLEL=1` | Defaults for VOD downloads (see above); mode and pace can be set per download |
| Video codec | `VIDEO_CODEC=copy` | `copy` keeps the video as is (fast, lossless). Any ffmpeg encoder (`libx264`, `libx265`, `libsvtav1`, …) re-encodes |
| Own ffmpeg parameters ("Eigene ffmpeg-Parameter") | `FFMPEG_ARGS=` | Extra output options, e.g. `-crf 23 -preset medium` or `-vf scale=-2:720`. They come right before the output file, so they override the options above. `-i`, `-progress`, `-y`, `-n` are rejected |

Conversion settings (codec, parameters, normalization, keep original) are stored in
each job when its recording starts; "Erneut versuchen" on a failed job applies the
current settings. Values saved in the UI are stored in `streams.json` and override the env values. "Reset"
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
| `CHECK_INTERVAL`, `DEFAULT_QUALITY`, `RESCAN_INTERVAL`, `RESCAN_DURATION`, `FOLDER_LAYOUT`, `KEEP_SOURCE`, `AUDIO_NORMALIZE`, `VIDEO_CODEC`, `FFMPEG_ARGS`, `DONE_RETENTION_DAYS`, `VOD_*` | see above | Defaults for the UI settings |
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
| `app/vod.py` | VOD download requests, `VodDownload` (paced segment fetching, `TsClock`) |
| `app/jobs.py` | Work-folder queue: states, claim/heartbeat/requeue, listing |
| `app/converter.py` | Converter entry point: chapters/metadata, ffmpeg, placing the MP4 |
| `app/twitch.py` | Helix batch live check (`get_live`) |
| `app/web.py`, `app/static/index.html` | Web UI and JSON API, IP allowlist |
| `app/streams.py` | `streams.json` model, validation, atomic writes |
| `app/naming.py` | Safe file/folder names |
| `app/settings.py` | Environment settings |
