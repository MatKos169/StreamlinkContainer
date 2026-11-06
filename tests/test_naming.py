import datetime
import unicodedata

import pytest

from app.naming import clean_output_path, recording_base, sanitize_component

NOW = datetime.datetime(2026, 11, 4, 12, 30, 5)


def base(title, output="streamer"):
    return recording_base("/recordings", output, title, NOW)


def test_layout():
    assert base("Hello   World") == "/recordings/streamer/2026-11-04/12-30-05 - Hello World"


def test_empty_title():
    for title in ("", "  ", None, "..."):
        assert base(title) == "/recordings/streamer/2026-11-04/12-30-05"


def test_reserved_and_control_chars():
    assert sanitize_component("a/b:c\n\x00d!") == "a_b_c d"
    name = base("a\nb\r\tc\x00d")
    assert not any(unicodedata.category(c).startswith("C") for c in name)


def test_long_titles_fit_filesystem_limit():
    for title in ("x" * 1000, "漢" * 300, "é漢" * 150):
        name = base(title).rsplit("/", 1)[1]
        assert len((name + ".chapters.csv").encode()) <= 255
        assert "�" not in name


def test_clean_output_path():
    assert clean_output_path("Twitch/Streamer A") == "Twitch/Streamer A"
    assert clean_output_path("/abs//path/./x/") == "abs/path/x"
    assert clean_output_path("a\\b") == "a/b"
    assert clean_output_path("") == ""


@pytest.mark.parametrize("bad", ["../x", "a/../../b", "a/!!!"])
def test_clean_output_path_rejects(bad):
    with pytest.raises(ValueError):
        clean_output_path(bad)


def test_output_cannot_escape_recordings_dir():
    with pytest.raises(ValueError):
        base("t", output="../../etc")
    assert base("t", output="/etc").startswith("/recordings/etc/")
