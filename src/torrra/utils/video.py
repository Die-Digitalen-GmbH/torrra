"""Video file utilities."""

import re
import string

# Common video file extensions
VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".m4v",
    ".webm",
    ".flv",
    ".mov",
    ".wmv",
    ".mpg",
    ".mpeg",
    ".ts",
    ".m2ts",
}

# Where a release name stops being a title. Bare "web" is left out because it
# is also a word: Charlotte's Web.
_RELEASE_TAG = re.compile(
    r"\b(\d{3,4}[pi]|4k|uhd|hdtv|hdrip|b[dr]-?rip|blu-?ray|bdremux|dvdrip"
    r"|web-?dl(rip)?|web-?rip|xvid|divx|[xh]\.? ?26[45]|hevc|av1)\b",
    re.IGNORECASE,
)
_RELEASE_GROUP_PREFIX = re.compile(r"^\s*\[[^\]]*\]\s*")
_EPISODE = re.compile(r"\bs\d{1,2}e\d{1,3}(?:-?e\d{1,3})*\b", re.IGNORECASE)
# Not the start of a date, which is how a daily show tells its episodes apart.
_YEAR = re.compile(r"\b(?:19|20)\d\d\b(?![ -]\d\d[ -]\d\d)")


def detect_video_extension(title: str) -> str | None:
    """Detect video file extension from a torrent title.

    Looks for common video extensions in the title string.
    Returns the extension (with dot) if found, None otherwise.
    """
    # Look for extension patterns in the title
    # Common patterns: "Movie.Name.2024.1080p.BluRay.x264.mkv"
    # or "Movie Name (2024) [1080p].mkv"
    title_lower = title.lower()

    for ext in VIDEO_EXTENSIONS:
        # Check if title ends with extension
        if title_lower.endswith(ext):
            return ext
        # Check for extension followed by common suffixes or brackets
        pattern = rf"{re.escape(ext)}[\s\]\)]"
        if re.search(pattern, title_lower):
            return ext

    return None


def humanize_title(stem: str) -> str:
    """Turn a release name into a title: "Big Mouth S01E01 (2022)", "Aftersun (2022)".

    The title is everything before the first sign of release info, which keeps
    every word of it, even ones that look like tags ("The Darjeeling Limited").
    A name that holds no title comes back unchanged.
    """
    name = stem.replace("_", " ")
    if " " not in name:
        name = name.replace(".", " ")
    name = _RELEASE_GROUP_PREFIX.sub("", name)
    if tag := _RELEASE_TAG.search(name):
        name = name[: tag.start()]

    episode = _EPISODE.search(name)
    title = name[: episode.start()] if episode else name

    # The last year is the release: 2001 A Space Odyssey 1968. A leading one is
    # the title itself: 1917.
    years = [year for year in _YEAR.finditer(title) if year.start() > 0]
    year = years[-1] if years else None
    if year:
        title = title[: year.start()]
    elif episode:
        year = _YEAR.match(name[episode.end() :].lstrip(" (-"))

    title = " ".join(title.split()).strip(" -([,")
    if title.islower():
        title = string.capwords(title)

    parts = [title, episode and episode[0].upper(), year and f"({year[0]})"]
    return " ".join(part for part in parts if part) or stem


def is_transcodable_extension(extension: str) -> bool:
    """Check if the extension is in the transcoding rules."""
    from torrra.core.config import get_config

    if not get_config().get("transcoding.enabled", False):
        return False

    rules = get_config().get("transcoding.rules", [])
    if not isinstance(rules, list):
        return False

    ext_lower = extension.lower()
    if not ext_lower.startswith("."):
        ext_lower = f".{ext_lower}"

    for rule in rules:
        rule_ext = rule.get("input_extension", "").lower()
        if not rule_ext.startswith("."):
            rule_ext = f".{rule_ext}"
        if ext_lower == rule_ext:
            return True

    return False
