"""Subtitle cross-check — the decisive calibration signal.

Comparing file durations can tell you that a local file and the dataset disagree,
but it cannot tell you *where* the disagreement lives. A file that is 197s longer
than the reference might have a 197s recap at the front (a real offset that
shifts every timestamp) or 197s of credits on the back (no offset at all). Those
demand opposite responses and the duration column cannot distinguish them.

Subtitles can, because they are timed against the user's own file. Dialogue near
the start and end of an episode gives two anchors; if both report the same
offset, the correction is a pure additive shift and we can trust it. If they
disagree, something structural is going on — appended bonus content, a stretched
timeline — and a human needs to look before any cut is generated.

Subtitles here are only ever *read*, as timing evidence about the user's own
media. Nothing extracted is written into the repo, and .gitignore excludes every
subtitle format to keep it that way.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

_CUE = re.compile(
    r"(\d+):(\d\d):(\d\d)[,.](\d\d\d)\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d\d\d)"
)

# A cue this far past the dataset's last scene is not the episode any more.
TRAILING_CONTENT_THRESHOLD = 180.0


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_cues(text: str) -> list[tuple[float, float]]:
    return [
        (
            _to_seconds(*match.group(1, 2, 3, 4)),
            _to_seconds(*match.group(5, 6, 7, 8)),
        )
        for match in _CUE.finditer(text)
    ]


def load_cues(video: Path) -> tuple[list[tuple[float, float]], str]:
    """Return (cues, source). Prefers a sidecar, falls back to an embedded track."""
    sidecar = video.with_suffix(".srt")
    if sidecar.exists():
        text = sidecar.read_text(encoding="utf-8", errors="replace")
        cues = parse_cues(text)
        if cues:
            return cues, "sidecar"

    # Embedded track. Convert to srt in a temp file; never inside the repo.
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "track.srt"
        try:
            result = subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", str(video),
                 "-map", "0:s:0", str(out)],
                capture_output=True, text=True, timeout=300,
            )
        except (subprocess.TimeoutExpired, OSError):
            return [], "none"
        if result.returncode != 0 or not out.exists():
            return [], "none"
        return parse_cues(out.read_text(encoding="utf-8", errors="replace")), "embedded"


@dataclass
class Alignment:
    code: str
    source: str
    head_offset: float | None   # local dialogue start - dataset first scene start
    tail_offset: float | None   # local dialogue end   - dataset last scene end
    trailing: float | None      # content after the last usable cue
    note: str = ""

    @property
    def agrees(self) -> bool:
        if self.head_offset is None or self.tail_offset is None:
            return False
        return abs(self.head_offset - self.tail_offset) <= 15.0

    @property
    def offset(self) -> float | None:
        """Best single additive offset, or None if the anchors disagree."""
        if not self.agrees:
            return None
        return (self.head_offset + self.tail_offset) / 2


def align(code: str, video: Path, first_scene: int, last_scene_end: int,
          duration: float | None) -> Alignment:
    cues, source = load_cues(video)
    if not cues:
        return Alignment(code, "none", None, None, None, "no subtitles available")

    first_cue = cues[0][0]

    # Drop cues that sit far beyond the dataset's last scene: on this library the
    # Season 8 files have an 'Inside the Episode' featurette appended, and its
    # commentary is subtitled, so the naive last cue lands ~10 minutes into
    # bonus material and reports a wildly wrong offset.
    in_episode = [c for c in cues if c[1] <= last_scene_end + TRAILING_CONTENT_THRESHOLD]
    note = ""
    if len(in_episode) < len(cues):
        dropped = len(cues) - len(in_episode)
        note = f"dropped {dropped} cues past episode end (appended bonus content?)"
    if not in_episode:
        return Alignment(code, source, first_cue - first_scene, None, None,
                         "all cues fall beyond the dataset's last scene")

    last_cue = in_episode[-1][1]
    trailing = (duration - last_cue) if duration is not None else None

    return Alignment(
        code=code,
        source=source,
        head_offset=first_cue - first_scene,
        tail_offset=last_cue - last_scene_end,
        trailing=trailing,
        note=note,
    )


def render_table(rows: list[Alignment]) -> str:
    header = (
        f"{'episode':<8} {'subs':>9} {'headOff':>9} {'tailOff':>9} "
        f"{'agree':>6} {'offset':>8}  note"
    )
    lines = [header, "-" * (len(header) + 10)]
    for row in rows:
        head = f"{row.head_offset:+9.1f}" if row.head_offset is not None else f"{'-':>9}"
        tail = f"{row.tail_offset:+9.1f}" if row.tail_offset is not None else f"{'-':>9}"
        offset = f"{row.offset:+8.1f}" if row.offset is not None else f"{'--':>8}"
        agree = "yes" if row.agrees else "NO"
        lines.append(
            f"{row.code:<8} {row.source:>9} {head} {tail} {agree:>6} {offset}  {row.note}"
        )
    return "\n".join(lines)
