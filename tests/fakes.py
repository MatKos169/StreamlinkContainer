import threading

from app.settings import Settings
from app.streams import StreamConfig
from app.twitch import LiveInfo


def make_settings(tmp_path, **kw):
    return Settings(config_dir=str(tmp_path / "config"), recordings_dir=str(tmp_path / "rec"), check_interval=10, **kw)


def make_stream(channel="streamer", output=None, **kw):
    return StreamConfig.from_input({"channel": channel, "output": output, **kw})


def info(channel="streamer", category="Just Chatting", category_id="509658", title="Hello"):
    return LiveInfo(channel, title, category_id, category)


class FakeFd:
    def __init__(self, chunks, block=None, fail=None):
        self.chunks = list(chunks)
        self.block = block          # threading.Event: read() waits on it once chunks are used up
        self.fail = fail            # exception raised once chunks are used up
        self.closed = False

    def read(self, size):
        if self.chunks:
            return self.chunks.pop(0)
        if self.fail:
            raise self.fail
        if self.block is not None:
            self.block.wait(5)
            return b"x"
        return b""

    def close(self):
        self.closed = True


class FakeStream:
    url = "https://cdn.example/index.m3u8?sig=1"

    def __init__(self, fd):
        self.fd = fd

    def open(self):
        return self.fd


class FakeSession:
    def __init__(self, streams):
        self._streams = streams
        self.calls = []

    def streams(self, url, options=None):
        self.calls.append((url, options))
        return self._streams
