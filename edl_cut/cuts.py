"""Snapping segment starts to real visual cuts.

The dataset's scene starts run early. Measured against ffmpeg's scene-change
detection over 30 segments of the reference library, the nearest real cut lands
*after* our start 22 times and before it 7, with a median of +1.19s. That second
is heard and seen as the tail of the previous scene before the one you asked for.

Calibration is not the culprit — it is right to within a second, confirmed
independently. The dataset's boundaries are simply logged a beat early, which is
entirely reasonable for hand-annotated data and useless to argue with. So rather
than trusting the timestamp, use it to find the neighbourhood and let the video
say where the cut actually is.

**Only ever forward.** ffmpeg detects *shot* changes, not scene changes, and a
dialogue scene contains a cut at every reverse angle. Snapping to the nearest
cut would as often as not jump backwards into the previous scene — exactly the
problem being fixed. Moving forward only means the worst case is clipping a
moment of the wanted scene, never adding more of the unwanted one.

This is emit-time work. It depends on the user's own files, so it must never be
baked into a published scene list.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

# How far forward to look for a cut. Beyond this, a detected change is more
# likely an ordinary shot change inside the scene than the scene's own opening.
LOOK_AHEAD = 1.5

# Cuts marginally before the start still count: the dataset is early far more
# often than late, and a cut a few frames back is the same cut.
LOOK_BEHIND = 0.35

# Scene-change score above which a frame counts as a cut.
#
# Deliberately strict, and paired with a short look-ahead, because per-segment
# snapping is only safe when it is nearly certain. Measured cut scores near
# segment starts run 0.13-0.51 whether the cut opens a scene or is a reverse
# angle inside one, so a loose threshold snaps to the wrong thing and clips
# footage. The default correction is the population-level trim in scenelist.py;
# this is for going further where a strong cut sits close by.
THRESHOLD = 0.35

_PTS = re.compile(r"pts_time:([0-9.]+)")


def detect(path: Path, centre: float,
           behind: float = LOOK_BEHIND, ahead: float = LOOK_AHEAD) -> list[float]:
    """Absolute times of visual cuts in a window around `centre`."""
    begin = max(0.0, centre - behind)
    span = behind + ahead
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats",
             "-ss", f"{begin:.3f}", "-i", str(path), "-t", f"{span:.3f}",
             "-vf", f"select='gt(scene,{THRESHOLD})',showinfo",
             "-an", "-sn", "-f", "null", "-"],
            capture_output=True, text=True, timeout=300,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    found = []
    for match in _PTS.finditer(proc.stderr):
        absolute = begin + float(match.group(1))
        # ffmpeg's duration limiting is not always exact on these files; discard
        # anything outside the window we asked for rather than trusting it.
        if begin - 0.1 <= absolute <= begin + span + 0.1:
            found.append(absolute)
    return sorted(found)


def snap_start(path: Path, start: float) -> tuple[float, bool]:
    """Move `start` forward to the first real cut, if one is close enough."""
    for cut in detect(path, start):
        if cut >= start - LOOK_BEHIND:
            moved = max(start, cut)
            return moved, moved > start + 0.02
    return start, False


class SnapCache:
    """Detected starts, kept per library.

    Detection decodes a few seconds of video per segment, which turns EDL
    generation from instant into a minute or two. Caching makes that a one-off
    rather than a cost on every regeneration — the same bargain as calibration.
    """

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, float] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                self.data = {}

    @staticmethod
    def key(media: Path, start: float) -> str:
        return f"{media.name}@{start:.3f}"

    def get(self, media: Path, start: float) -> float | None:
        return self.data.get(self.key(media, start))

    def put(self, media: Path, start: float, snapped: float) -> None:
        self.data[self.key(media, start)] = round(snapped, 3)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1), encoding="utf-8")


def snap_all(resolved, cache: SnapCache | None = None, progress=None):
    """Snap every segment start forward to a real cut.

    Returns (adjusted, moved_count, seconds_trimmed).
    """
    out = []
    moved = 0
    trimmed = 0.0
    for index, (segment, path, start, end) in enumerate(resolved):
        cached = cache.get(path, start) if cache else None
        if cached is not None:
            snapped = cached
        else:
            snapped, _ = snap_start(path, start)
            if cache:
                cache.put(path, start, snapped)
            if progress and index % 20 == 0:
                progress(f"  detecting cuts: {index}/{len(resolved)}")
        if snapped > start + 0.02 and snapped < end:
            moved += 1
            trimmed += snapped - start
            out.append((segment, path, snapped, end))
        else:
            out.append((segment, path, start, end))
    if cache:
        cache.save()
    return out, moved, trimmed
