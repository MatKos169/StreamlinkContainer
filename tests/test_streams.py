import json

import pytest

from app.streams import Config, Options, StreamConfig, load_config, parse_channel, parse_ffmpeg_args, save_config


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
    save_config(str(path), Config(settings={"rescan_interval": 5}, streams=streams))
    loaded = load_config(str(path))
    assert loaded.streams == streams
    assert loaded.options(Options()).rescan_interval == 5
    assert loaded.options(Options()).check_interval == 60
    assert [p.name for p in path.parent.iterdir()] == ["streams.json"]  # no temp files left


def test_missing_file_is_empty(tmp_path):
    assert load_config(str(tmp_path / "nope.json")) == Config()


def test_invalid_file_raises(tmp_path):
    path = tmp_path / "streams.json"
    path.write_text("{broken")
    with pytest.raises(ValueError):
        load_config(str(path))
    path.write_text(json.dumps({"streams": [{"channel": "a", "output": "../x"}]}))
    with pytest.raises(ValueError):
        load_config(str(path))
    path.write_text(json.dumps({"settings": {"rescan_interval": 1}, "streams": []}))
    with pytest.raises(ValueError):
        load_config(str(path))


def test_options_merged_validates_and_converts():
    base = Options()
    merged = base.merged({"check_interval": "30", "rescan_duration": 0, "default_quality": " 720p,best ", "rescan_interval": None})
    assert (merged.check_interval, merged.rescan_duration, merged.default_quality, merged.rescan_interval) == (30, 0, "720p,best", 10)
    assert base.check_interval == 60  # unchanged


@pytest.mark.parametrize("bad", [
    {"check_interval": 5}, {"rescan_interval": 4}, {"rescan_duration": 3601},
    {"rescan_interval": "x"}, {"rescan_interval": True}, {"default_quality": "a;b"}, {"nope": 1},
])
def test_options_merged_rejects(bad):
    with pytest.raises(ValueError):
        Options().merged(bad)


def test_video_codec_and_ffmpeg_args():
    merged = Options().merged({"video_codec": " libx265 ", "ffmpeg_args": " -crf 23  -vf 'scale=-2:720' "})
    assert merged.video_codec == "libx265"
    assert parse_ffmpeg_args(merged.ffmpeg_args) == ["-crf", "23", "-vf", "scale=-2:720"]
    assert Options().merged({"video_codec": ""}).video_codec == "copy"
    assert Options().video_codec == "copy" and Options().ffmpeg_args == ""


@pytest.mark.parametrize("bad", [
    {"video_codec": "libx264; rm"}, {"video_codec": "a b"},
    {"ffmpeg_args": "-i /etc/passwd"}, {"ffmpeg_args": "-progress x"}, {"ffmpeg_args": "-y"},
    {"ffmpeg_args": "'unclosed"}, {"ffmpeg_args": "x" * 501},
])
def test_video_codec_and_ffmpeg_args_rejected(bad):
    with pytest.raises(ValueError):
        Options().merged(bad)
