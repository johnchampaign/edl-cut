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
LOOK_AHEAD = 2.0

# A cut this recently behind means we are already just past the scene's opening
# and must not move.
#
# This guard matters more than it looks. Measured across the library, the error
# runs both ways: the median start is 0.62s before the next cut, but a third of
# starts sit mid-shot with no cut within 4s ahead, meaning they are *late*
# rather than early. Snapping those forward would jump to the next shot change
# and lose more of the scene. Only starts with no recent cut behind them are
# candidates for moving.
LOOK_BEHIND = 1.5

# Scene-change score above which a frame counts as a cut.
#
# Set to catch real cuts in dim footage without chasing camera movement.
#
# The window is what keeps this safe rather than the score. Once the dataset's
# systematic ~1.2s lead is trimmed in scenelist.py, a start sits within roughly a
# second of the true cut, so looking only 2s forward finds that cut and rarely
# reaches an intra-scene shot change. A high threshold was tried first and
# missed real cuts: the opening of S01E01's Pentos scene scores under 0.35.
THRESHOLD = 0.20

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
    """Move `start` forward to the scene's opening cut, when that is safe.

    Three outcomes, and the refusals are as important as the moves:

    * a cut just behind us — we are already at or past the opening, leave it;
    * a cut close ahead — that is the opening, snap to it exactly;
    * nothing either side — we are mid-shot, and guessing would only lose more.
    """
    found = detect(path, start)
    if any(start - LOOK_BEHIND <= cut <= start + 0.02 for cut in found):
        return start, False
    ahead = [cut for cut in found if start + 0.02 < cut <= start + LOOK_AHEAD]
    if ahead:
        return ahead[0], True
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


# --- Offset refinement against real shot cuts --------------------------------
#
# Subtitle calibration aims at gaps between dialogue, which are 1-3s wide, so it
# cannot resolve finer than that. Every scene change is a *shot cut*, and shot
# cuts are frame-exact — a far sharper target. Matching a sample of the dataset's
# scene boundaries against detected cuts pins the offset to a fraction of a
# second.
#
# Validated against offsets derived by hand from frame inspection:
#   S01E06  refinement +0.65  vs  +0.41 measured
#   S01E01  refinement -29.5  vs  -28.9 measured
#   S04E08  (already correct) moved only +0.20

# How far from the current offset to search.
REFINE_WINDOW = 8.0

# A boundary counts as landing on a cut within this distance.
REFINE_TOLERANCE = 0.30

# Boundaries sampled per episode. More is sharper but costs decoding.
REFINE_SAMPLES = 14

# Fraction of sampled boundaries that must land on a cut for the result to be
# trusted. Episodes whose boundaries simply do not correspond to shot cuts —
# and there are some — must be left alone rather than nudged by noise.
REFINE_MIN_MATCH = 0.5


def _cuts_window(path: Path, begin: float, span: float, threshold: float = 0.15):
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{begin:.3f}",
             "-i", str(path), "-t", f"{span:.3f}",
             "-vf", f"select='gt(scene,{threshold})',showinfo",
             "-an", "-sn", "-f", "null", "-"],
            capture_output=True, text=True, timeout=600,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    out = []
    for match in _PTS.finditer(proc.stderr):
        t = begin + float(match.group(1))
        if begin - 0.1 <= t <= begin + span + 0.1:
            out.append(t)
    return out


def refine_offset(path: Path, boundaries: list[int], current: float,
                  samples: int = REFINE_SAMPLES):
    """Sharpen `current` by matching scene boundaries to real cuts.

    Returns (suggested_offset, matched, sampled). `matched` below
    REFINE_MIN_MATCH of `sampled` means the episode's boundaries do not
    correspond to shot cuts well enough to trust the result.
    """
    interior = sorted(set(boundaries))[2:-2]
    if len(interior) < 6:
        return current, 0, 0
    step = max(1, len(interior) // samples)
    chosen = interior[::step][:samples]

    detected = []
    for edge in chosen:
        centre = edge + current
        found = _cuts_window(path, max(0.0, centre - REFINE_WINDOW - 1),
                             REFINE_WINDOW * 2 + 2)
        detected.append((edge, found))

    best_delta, best_score = 0.0, -1
    delta = -REFINE_WINDOW
    while delta <= REFINE_WINDOW + 1e-9:
        score = sum(
            1 for edge, found in detected
            if any(abs(c - (edge + current + delta)) <= REFINE_TOLERANCE
                   for c in found)
        )
        # Ties go to the smaller correction: the existing offset is evidence too.
        if score > best_score or (score == best_score and abs(delta) < abs(best_delta)):
            best_score, best_delta = score, delta
        delta += 0.05

    return current + best_delta, best_score, len(chosen)


# --- Consensus refinement ----------------------------------------------------
#
# Match fraction turned out to be the wrong confidence signal. Detection finds
# more cuts as the threshold drops, so a fixed fraction against a growing pool
# says little, and episodes that align perfectly can still score low.
#
# What separates a real alignment from a coincidence is **stability**: run the
# search at several detection thresholds and see whether the answer moves. On a
# control episode the best delta held at +0.15 across thresholds while matches
# climbed 5->7->8. On S08E03 — the near-lightless Long Night — the same sweep
# gave -1.00, +7.25 and -1.20 from 15, 58 and 136 detected cuts: plenty of
# candidates, no agreement, nothing really there to align to.
CONSENSUS_THRESHOLDS = (0.15, 0.08, 0.04)

# How closely two thresholds must agree to count as corroborating.
CONSENSUS_TOLERANCE = 0.5


def refine_offset_consensus(path: Path, boundaries: list[int], current: float,
                            samples: int = REFINE_SAMPLES):
    """Refine an offset, trusting it only when thresholds agree.

    Returns (suggested, agreeing, tried, per_threshold). `agreeing` counts how
    many thresholds landed within CONSENSUS_TOLERANCE of the chosen answer; two
    or more is corroboration, one is a coincidence.
    """
    interior = sorted(set(boundaries))[2:-2]
    if len(interior) < 6:
        return current, 0, 0, {}
    step = max(1, len(interior) // samples)
    chosen = interior[::step][:samples]

    answers: dict[float, float] = {}
    for threshold in CONSENSUS_THRESHOLDS:
        detected = [
            (edge, _cuts_window(path, max(0.0, edge + current - REFINE_WINDOW - 1),
                                REFINE_WINDOW * 2 + 2, threshold=threshold))
            for edge in chosen
        ]
        best_delta, best_score = 0.0, -1
        delta = -REFINE_WINDOW
        while delta <= REFINE_WINDOW + 1e-9:
            score = sum(
                1 for edge, found in detected
                if any(abs(c - (edge + current + delta)) <= REFINE_TOLERANCE
                       for c in found)
            )
            if score > best_score or (score == best_score
                                      and abs(delta) < abs(best_delta)):
                best_score, best_delta = score, delta
            delta += 0.05
        answers[threshold] = best_delta

    # The winning delta is the one the most thresholds cluster around.
    best_delta, best_support = 0.0, 0
    for candidate in answers.values():
        support = sum(1 for v in answers.values()
                      if abs(v - candidate) <= CONSENSUS_TOLERANCE)
        if support > best_support or (support == best_support
                                      and abs(candidate) < abs(best_delta)):
            best_support, best_delta = support, candidate

    if best_support >= 2:
        agreed = [v for v in answers.values()
                  if abs(v - best_delta) <= CONSENSUS_TOLERANCE]
        best_delta = sum(agreed) / len(agreed)

    return current + best_delta, best_support, len(CONSENSUS_THRESHOLDS), answers
