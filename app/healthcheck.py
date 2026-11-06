"""Container healthcheck: `python -m app.healthcheck` (exit 0 = healthy).

recorder: /config/status.json must have been rewritten within MAX_AGE seconds
(the recorder does this at least every 30 s).
"""
import os
import sys
import time

MAX_AGE = 120


def main(path=None, now=None):
    path = path or os.path.join(os.environ.get("CONFIG_DIR", "/config"), "status.json")
    try:
        age = (now or time.time()) - os.path.getmtime(path)
    except OSError:
        print(f"unhealthy: {path} missing")
        return 1
    if age > MAX_AGE:
        print(f"unhealthy: status.json is {age:.0f}s old (recorder loop stuck?)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
