"""Video file utilities."""

import re

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
