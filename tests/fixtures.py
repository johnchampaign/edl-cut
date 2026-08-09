"""Synthetic media fixtures.

Every test clip here is generated on the fly by ffmpeg's `testsrc` and `sine`
filters. No frame of real footage is used as a fixture, and none is committed —
that is the project's distribution boundary applied to its own test suite.

Colour bars are actually a better fixture than real video for this project: the
pattern carries a visible frame counter, so a mis-timed cut is obvious, and we
control the keyframe interval exactly, which matters for testing the exporter's
keyframe handling.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from edl_cut.dataset import Episode, Scene


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def make_clip(path: Path, seconds: int = 30, gop: int = 10,
              rate: int = 10, tone: int = 440) -> Path:
    """Generate a colour-bar clip with a known, fixed keyframe interval.

    `gop` is forced so tests can assert exactly where cut points will snap to
    under stream copy. Left to its own devices an encoder picks scene-adaptive
    keyframes, which would make those assertions flaky.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=320x180:rate={rate}:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency={tone}:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-g", str(gop * rate), "-keyint_min", str(gop * rate),
            "-sc_threshold", "0",
            "-c:a", "aac", "-shortest",
            str(path),
        ],
        check=True, capture_output=True,
    )
    return path


def make_library(root: Path, episodes: int = 3, seconds: int = 30) -> dict:
    """A tiny synthetic 'series' with conventional release-style filenames."""
    paths = {}
    for number in range(1, episodes + 1):
        name = f"Test.Show.S01E{number:02d}.1080p.BluRay.x264-SYNTH.mkv"
        paths[(1, number)] = make_clip(root / "Season 1" / name, seconds=seconds)
    return paths


def make_episode(season: int, number: int, scenes: list[tuple[int, int, list[str]]],
                 stated_length: int | None = None) -> Episode:
    """Build an Episode without touching the real dataset."""
    return Episode(
        season=season,
        number=number,
        title=f"Test {season}x{number}",
        scenes=tuple(
            Scene(start=a, end=b, location="Loc", sub_location="Sub",
                  characters=tuple(c), flashback=False, greensight=False, warg=False)
            for a, b, c in scenes
        ),
        stated_length=stated_length,
    )
