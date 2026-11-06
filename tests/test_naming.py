import datetime
import unicodedata

import pytest

from app.naming import FOLDER_LAYOUTS, clean_output_path, destination_path, sanitize_component

NOW = datetime.datetime(2026, 11, 4, 12, 30, 5)


def base(title, output="streamer", layout="year_month"):
    return destination_path(output, layout, NOW, title)


@pytest.mark.parametrize("layout, expected", [
    ("output_year_month", "Twitch/streamer-2026/11/2026-11-04 12-30-05 - Hello World.mp4"),
    ("year_month", "Twitch/streamer/2026/11/2026-11-04 12-30-05 - Hello World.mp4"),
    ("year", "Twitch/streamer/2026/2026-11-04 12-30-05 - Hello World.mp4"),
    ("date", "Twitch/streamer/2026-11-04/2026-11-04 12-30-05 - Hello World.mp4"),
    ("none", "Twitch/streamer/2026-11-04 12-30-05 - Hello World.mp4"),
])
def test_layouts(layout, expected):
    assert destination_path("Twitch/streamer", layout, NOW, "Hello   World") == expected


def test_all_layouts_covered():
    assert set(FOLDER_LAYOUTS) == {"output_year_month", "year_month", "year", "date", "none"}


def test_empty_title():
    for title in ("", "  ", None, "..."):
        assert base(title) == "streamer/2026/11/2026-11-04 12-30-05.mp4"


def test_reserved_and_control_chars():
    assert sanitize_component("a/b:c\n\x00d!") == "a_b_c d"
    name = base("a\nb\r\tc\x00d")
    assert not any(unicodedata.category(c).startswith("C") for c in name)


def test_long_titles_fit_filesystem_limit():
    for title in ("x" * 1000, "漢" * 300, "é漢" * 150):
        name = base(title).rsplit("/", 1)[1]
        assert len((name + ".part").encode()) <= 255
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
    assert base("t", output="/etc").startswith("etc/")
