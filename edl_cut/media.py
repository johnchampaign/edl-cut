"""Finding the user's local media files and asking ffprobe about them.

Matching rips to episodes is the fiddliest real-world problem in the project.
The goal here is not cleverness — it is diagnosability. Every file we scan lands
in exactly one of three buckets (matched / unmatched / duplicate) and the caller
can print all three.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

VIDEO_SUFFIXES = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".webm"}

# Ordered most-specific first. The first pattern that matches wins.
_PATTERNS = [
    re.compile(r"[Ss](\d{1,2})[\s._-]*[Ee](\d{1,3})"),          # S01E01, s1.e1
    re.compile(r"(?<!\d)(\d{1,2})[Xx](\d{1,3})(?!\d)"),          # 1x01
    re.compile(r"[Ss]eason[\s._-]*(\d{1,2}).*?[Ee]pisode[\s._-]*(\d{1,3})"),
]


class DependencyMissing(RuntimeError):
    pass


def require_tool(name: str, hint: str) -> str:
    path = shutil.which(name)
    if not path:
        raise DependencyMissing(f"{name} not found on PATH. Install it with: {hint}")
    return path


def parse_episode_code(name: str) -> tuple[int, int] | None:
    for pattern in _PATTERNS:
        match = pattern.search(name)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


@dataclass
class MediaScan:
    """What we found on disk, bucketed so every file is accounted for."""

    matched: dict[tuple[int, int], Path]
    unmatched: list[Path]
    duplicates: dict[tuple[int, int], list[Path]]
    root_state: str  # "ok" | "missing" | "empty" | "not-mounted"


def _classify_root(root: Path) -> str:
    """Distinguish 'not there' from 'empty' from 'the NTFS drive isn't mounted'.

    On this machine the media lives on drives that do not auto-mount at boot, so
    an empty-looking directory is far more often an unmounted volume than a
    genuinely empty one. Guessing wrong sends the user debugging the wrong thing.
    """
    if not root.exists():
        parent = root.parent
        if parent.exists() and os.path.ismount(parent):
            return "missing"
        return "not-mounted" if not parent.exists() else "missing"
    if not any(root.iterdir()):
        return "not-mounted" if not os.path.ismount(root) else "empty"
    return "ok"


def scan(root: Path, overrides: dict[str, str] | None = None) -> MediaScan:
    """Walk `root` and bucket every video file by the episode it belongs to.

    `overrides` maps a filename (or relative path) to an 'S01E01' code, and is
    the escape hatch for rips our patterns cannot read.
    """
    state = _classify_root(root)
    if state != "ok":
        return MediaScan({}, [], {}, state)

    override_codes = {}
    for key, code in (overrides or {}).items():
        parsed = parse_episode_code(code)
        if parsed:
            override_codes[key] = parsed

    matched: dict[tuple[int, int], Path] = {}
    duplicates: dict[tuple[int, int], list[Path]] = {}
    unmatched: list[Path] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        key = (
            override_codes.get(path.name)
            or override_codes.get(str(path.relative_to(root)))
            or parse_episode_code(path.name)
            # Fall back to the parent directory, which catches layouts that put
            # the episode number only in a folder name.
            or parse_episode_code(path.parent.name)
        )
        if key is None:
            unmatched.append(path)
        elif key in matched:
            duplicates.setdefault(key, [matched[key]]).append(path)
        else:
            matched[key] = path

    return MediaScan(matched, unmatched, duplicates, "ok")


def probe_duration(path: Path) -> float | None:
    """Container duration in seconds, or None if ffprobe can't say."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    value = result.stdout.strip()
    try:
        return float(value)
    except ValueError:
        return None


def probe_durations(paths: dict[tuple[int, int], Path], workers: int = 8) -> dict:
    """Probe many files at once. ffprobe reads container metadata, so this is
    IO-bound and threads help even on a spinning disk."""
    keys = list(paths)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        durations = list(pool.map(lambda k: probe_duration(paths[k]), keys))
    return dict(zip(keys, durations))
