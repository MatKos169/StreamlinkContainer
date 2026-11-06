"""Safe file and folder names for recordings (sanitizers taken over from upstream)."""
import re
import unicodedata

NAME_MAX_BYTES = 255
# ".part", ".ts", ".chapters.csv", ".incomplete" are appended to the base name.
SUFFIX_RESERVE = 32
_RESERVED_CHARS = re.compile(r'[<>:"/\\|?*]')


def truncate_utf8(text, max_bytes):
    """Cut text to at most max_bytes UTF-8 bytes without splitting a character."""
    if max_bytes <= 0:
        return ""
    return text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


def sanitize_component(text):
    """Make text safe as a single file or folder name (no separators, no control chars)."""
    text = unicodedata.normalize("NFC", text or "")
    text = _RESERVED_CHARS.sub("_", text)
    text = re.sub(r"[^\w\s._-]", "", text)
    text = "".join(c for c in text if c.isspace() or not unicodedata.category(c).startswith("C"))
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .")


def clean_output_path(path):
    """Normalize a user-entered output folder to a safe relative path ("a/b").

    Leading slashes are ignored (the path is always relative to the recordings
    volume); "..", empty and unsafe components are rejected.
    """
    parts = []
    for raw in re.split(r"[/\\]+", (path or "").strip()):
        if raw in ("", "."):
            continue
        if raw == "..":
            raise ValueError("output path must not contain '..'")
        part = sanitize_component(raw)
        if not part:
            raise ValueError(f"invalid folder name {raw!r}")
        parts.append(truncate_utf8(part, 100).strip(" ."))
    return "/".join(parts)


# Folder layouts below/around the channel's output folder (choosable in the web UI).
FOLDER_LAYOUTS = {
    "output_year_month": "<output>-%Y/%m",   # like the MatKos169/ffmpeg container: name-2026/11/
    "year_month": "<output>/%Y/%m",
    "year": "<output>/%Y",
    "date": "<output>/%Y-%m-%d",
    "none": "<output>",
}


def destination_path(output, layout, started, title, ext="mp4"):
    """Final path relative to /recordings, e.g. "<output>-2026/11/<YYYY-mm-dd HH-MM-SS> - <title>.mp4".

    The date is always in the file name, so files sort well in Jellyfin
    whatever the folder layout.
    """
    prefix = started.strftime("%Y-%m-%d %H-%M-%S")
    budget = NAME_MAX_BYTES - SUFFIX_RESERVE - len(prefix) - len(" - ")
    safe_title = truncate_utf8(sanitize_component(title), budget).strip(" .")
    filename = f"{prefix} - {safe_title}" if safe_title else prefix
    folder = started.strftime(FOLDER_LAYOUTS[layout]).replace("<output>", clean_output_path(output))
    return f"{clean_output_path(folder)}/{filename}.{ext}"
