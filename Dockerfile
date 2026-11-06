# One image for both services: the recorder (default) and the web UI (python -m app.web).
FROM python:3.14.1-alpine3.21@sha256:fd8407cacb1028c803566276ae25eb7c2a00fbaa210a0a5670994f6866e29278 AS base
WORKDIR /app

FROM base AS builder
RUN apk add --no-cache gcc musl-dev
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM base AS runner
# tzdata: file names use local time (set TZ, e.g. Europe/Berlin).
RUN apk add --no-cache tzdata
COPY --from=builder /install /usr/local
COPY app ./app

RUN python -m pip uninstall -y pip setuptools \
    && rm -rf /usr/local/lib/python3.*/ensurepip \
    && mkdir -p /config /recordings

# Works with a read-only root filesystem: no bytecode writes, streamlink cache under /tmp.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    XDG_CACHE_HOME=/tmp/.cache \
    CONFIG_DIR=/config \
    RECORDINGS_DIR=/recordings

# Override with `user:` in compose to match the owner of your volumes.
USER 1027:100

EXPOSE 8080
CMD ["python", "-m", "app.recorder"]
