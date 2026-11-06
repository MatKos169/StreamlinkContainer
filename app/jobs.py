"""Work queue on a shared folder (/work), used by recorder, converter(s) and web UI.

    /work/recording/<id>/   being recorded (video.<ext>.part, chapters.csv, job.json)
    /work/queue/<id>/       finished recording, waiting for a converter
    /work/converting/<id>/  claimed by a converter (claim.json: who, heartbeat, progress)
    /work/failed/<id>/      conversion failed (error in job.json); retry/delete in the UI
    /work/done/<id>/        source kept after conversion (only with keep_source)

Converters claim a job by renaming queue/<id> to converting/<id>. A rename is
atomic, also on NFS, so several converters on different hosts can share one
/work folder. A claim whose heartbeat is older than STALE_SECONDS goes back to
the queue (converter crashed or host gone).
"""
import json
import os
import shutil
import time
import uuid

from .streams import write_json_atomic

STATES = ("recording", "queue", "converting", "failed", "done")
JOB_FILE = "job.json"
CLAIM_FILE = "claim.json"
CHAPTERS_FILE = "chapters.csv"
STALE_SECONDS = 600


def new_job_id(channel, now):
    return f"{now.strftime('%Y%m%d-%H%M%S')}-{channel}-{uuid.uuid4().hex[:6]}"


def job_dir(work_dir, state, job_id):
    return os.path.join(work_dir, state, job_id)


def ensure_dirs(work_dir):
    for state in STATES:
        os.makedirs(os.path.join(work_dir, state), exist_ok=True)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def read_job(path):
    return read_json(os.path.join(path, JOB_FILE)) or {}


def write_job(path, data):
    write_json_atomic(os.path.join(path, JOB_FILE), data)


def video_file(path):
    """The finished video in a job folder (video.ts / video.mp4), or None."""
    try:
        names = sorted(os.listdir(path))
    except FileNotFoundError:
        return None
    for name in names:
        if name.startswith("video.") and not name.endswith(".part"):
            return os.path.join(path, name)
    return None


def move(work_dir, job_id, src_state, dst_state):
    """Atomically move a job folder between states. Returns the new path, None if it was gone."""
    os.makedirs(os.path.join(work_dir, dst_state), exist_ok=True)
    dst = job_dir(work_dir, dst_state, job_id)
    try:
        os.rename(job_dir(work_dir, src_state, job_id), dst)
    except FileNotFoundError:
        return None
    return dst


def remove(path):
    shutil.rmtree(path, ignore_errors=True)


def finish_recording(work_dir, job_id, updates):
    """Settle a recording folder: rename .part files, update job.json and queue it.

    Drops the job when nothing was recorded. Returns the queue path or None.
    """
    path = job_dir(work_dir, "recording", job_id)
    if not os.path.isdir(path):
        return None
    has_data = False
    for name in os.listdir(path):
        full = os.path.join(path, name)
        if name.startswith("video."):
            if os.path.getsize(full) == 0:
                os.remove(full)
                continue
            has_data = True
            if name.endswith(".part"):
                os.replace(full, full[:-len(".part")])
    if not has_data:
        remove(path)
        return None
    job = read_job(path)
    job.update(updates)
    write_job(path, job)
    return move(work_dir, job_id, "recording", "queue")


def recover_recordings(work_dir):
    """After a recorder restart, queue what is left in recording/ (marked incomplete)."""
    folder = os.path.join(work_dir, "recording")
    if not os.path.isdir(folder):
        return []
    recovered = []
    for job_id in sorted(os.listdir(folder)):
        if finish_recording(work_dir, job_id, {"complete": False, "error": "recorder restarted during recording"}):
            recovered.append(job_id)
    return recovered


def queued_ids(work_dir):
    try:
        return sorted(os.listdir(os.path.join(work_dir, "queue")))
    except FileNotFoundError:
        return []


def claim_next(work_dir, converter):
    """Claim the oldest queued job. Returns (job_id, path) or (None, None)."""
    for job_id in queued_ids(work_dir):
        path = move(work_dir, job_id, "queue", "converting")
        if path is None:
            continue  # another converter was faster
        heartbeat(path, converter, progress=0.0)
        return job_id, path
    return None, None


def heartbeat(path, converter, progress=None):
    claim = read_json(os.path.join(path, CLAIM_FILE)) or {"converter": converter, "started": time.time()}
    claim.update(converter=converter, heartbeat=time.time())
    if progress is not None:
        claim["progress"] = round(progress, 3)
    # create_dirs=False: never recreate a job folder that was just moved away.
    write_json_atomic(os.path.join(path, CLAIM_FILE), claim, create_dirs=False)


def still_owner(path, converter):
    claim = read_json(os.path.join(path, CLAIM_FILE))
    return claim is not None and claim.get("converter") == converter


def requeue_stale(work_dir, now=None, stale_seconds=STALE_SECONDS):
    """Return claims without a recent heartbeat to the queue. Returns the job ids."""
    now = time.time() if now is None else now
    folder = os.path.join(work_dir, "converting")
    if not os.path.isdir(folder):
        return []
    requeued = []
    for job_id in sorted(os.listdir(folder)):
        path = os.path.join(folder, job_id)
        claim = read_json(os.path.join(path, CLAIM_FILE)) or {}
        last = claim.get("heartbeat") or _mtime(path)
        if now - last > stale_seconds:
            os.makedirs(os.path.join(work_dir, "queue"), exist_ok=True)
            try:
                os.remove(os.path.join(path, CLAIM_FILE))
            except FileNotFoundError:
                pass
            if move(work_dir, job_id, "converting", "queue"):
                requeued.append(job_id)
    return requeued


def release(work_dir, job_id, converter):
    """Give a claimed job back to the queue (converter shutting down)."""
    path = job_dir(work_dir, "converting", job_id)
    if still_owner(path, converter):
        for name in os.listdir(path):
            if name != JOB_FILE and name != CHAPTERS_FILE and not name.startswith("video."):
                _remove_file(os.path.join(path, name))
        move(work_dir, job_id, "converting", "queue")


def _remove_file(path):
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except FileNotFoundError:
        return time.time()


def _size(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def list_jobs(work_dir, states=("recording", "queue", "converting", "failed")):
    """Summaries for the web UI."""
    result = {}
    for state in states:
        folder = os.path.join(work_dir, state)
        entries = []
        try:
            ids = sorted(os.listdir(folder))
        except FileNotFoundError:
            ids = []
        for job_id in ids:
            path = os.path.join(folder, job_id)
            job = read_job(path)
            entry = {k: job.get(k) for k in ("channel", "title", "destination", "started_at", "ended_at",
                                              "complete", "error", "attempts")}
            entry.update(id=job_id, size=_size(path))
            if state == "converting":
                entry["claim"] = read_json(os.path.join(path, CLAIM_FILE))
            entries.append(entry)
        result[state] = entries
    return result


def valid_job_id(job_id):
    return bool(job_id) and "/" not in job_id and "\\" not in job_id and job_id not in (".", "..")
