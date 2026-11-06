import json

import pytest

from app.streams import StreamConfig, load_streams, parse_channel, save_streams


@pytest.mark.parametrize("value", [
    "SomeStreamer", "twitch.tv/somestreamer", "https://www.twitch.tv/SomeStreamer",
    "http://m.twitch.tv/somestreamer/videos", "https://twitch.tv/somestreamer?x=1",
])
def test_parse_channel(value):
    assert parse_channel(value) == "somestreamer"


@pytest.mark.parametrize("value", ["", "a b", "https://youtube.com/x", "x" * 26, "https://www.twitch.tv/"])
def test_parse_channel_rejects(value):
    with pytest.raises(ValueError):
        parse_channel(value)


def test_from_input_defaults_output_to_channel():
    s = StreamConfig.from_input({"channel": "https://www.twitch.tv/Foo"})
    assert (s.channel, s.output, s.quality, s.enabled) == ("foo", "foo", "", True)
    assert s.id


@pytest.mark.parametrize("data", [
    {"channel": "foo", "output": "../x"},
    {"channel": "foo", "quality": "best; rm -rf"},
    {"channel": "foo", "enabled": "yes"},
    "not a dict",
])
def test_from_input_rejects(data):
    with pytest.raises(ValueError):
        StreamConfig.from_input(data)


def test_roundtrip(tmp_path):
    path = tmp_path / "cfg" / "streams.json"
    streams = [StreamConfig.from_input({"channel": "a", "output": "x/y", "quality": "720p,best"}),
               StreamConfig.from_input({"channel": "b", "enabled": False})]
    save_streams(str(path), streams)
    assert load_streams(str(path)) == streams
    assert [p.name for p in path.parent.iterdir()] == ["streams.json"]  # no temp files left


def test_missing_file_is_empty(tmp_path):
    assert load_streams(str(tmp_path / "nope.json")) == []


def test_invalid_file_raises(tmp_path):
    path = tmp_path / "streams.json"
    path.write_text("{broken")
    with pytest.raises(ValueError):
        load_streams(str(path))
    path.write_text(json.dumps({"streams": [{"channel": "a", "output": "../x"}]}))
    with pytest.raises(ValueError):
        load_streams(str(path))
