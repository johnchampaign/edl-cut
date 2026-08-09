"""Milestone 1: the calibration diagnostic.

The dataset's timestamps are relative to whatever cut its author watched. Any
given user's rips differ — recaps kept or stripped, different intro handling,
framerate conversion, chapter padding. This module answers one question:

    do the dataset's timestamps line up with THIS library, and if not, how?

It only diagnoses. It deliberately emits no playlist and writes no offset cache,
because deciding what to do about a mismatch needs a human looking at the table
first.

Two independent references are compared against each local file:

  stated  — keyValues.json's per-episode runtime, in whole seconds
  scenes  — where the dataset's final scene ends

They come from different parts of the dataset. When they agree, the reference is
trustworthy and any delta is real. When they disagree, the dataset is the
problem, not the rip — and that distinction is the whole point of showing both.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from pathlib import Path

from .dataset import Episode


@dataclass
class Row:
    code: str
    local: float | None
    stated: int | None
    scenes_end: int | None

    @property
    def delta_stated(self) -> float | None:
        if self.local is None or self.stated is None:
            return None
        return self.local - self.stated

    @property
    def delta_scenes(self) -> float | None:
        if self.local is None or self.scenes_end is None:
            return None
        return self.local - self.scenes_end

    @property
    def ratio(self) -> float | None:
        """local / stated. ~1.0 means same speed; ~1.042 means PAL speedup."""
        if self.local is None or not self.stated:
            return None
        return self.local / self.stated


def build_rows(episodes: list[Episode], durations: dict) -> list[Row]:
    rows = []
    for episode in episodes:
        rows.append(
            Row(
                code=episode.code,
                local=durations.get((episode.season, episode.number)),
                stated=episode.stated_length,
                scenes_end=episode.last_scene_end,
            )
        )
    return rows


def _spread(values: list[float]) -> tuple[float, float, float]:
    return min(values), statistics.median(values), max(values)


def _outliers(rows: list[Row]) -> tuple[list[Row], list[Row]]:
    """Split rows into (typical, outlying) by median absolute deviation.

    A handful of structurally different files — a different rip source, bonus
    content welded onto the end — would otherwise dominate the spread and drag
    the verdict for the whole library toward 'inconsistent'. Judging the bulk and
    naming the exceptions is far more actionable than one averaged answer.
    """
    usable = [r for r in rows if r.delta_stated is not None]
    if len(usable) < 4:
        return usable, []
    deltas = [r.delta_stated for r in usable]
    median = statistics.median(deltas)
    mad = statistics.median([abs(d - median) for d in deltas]) or 1.0
    typical, odd = [], []
    for row in usable:
        (odd if abs(row.delta_stated - median) > max(6 * mad, 30) else typical).append(row)
    return typical, odd


def interpret(rows: list[Row]) -> list[str]:
    """Turn the delta columns into a recommendation.

    The offset model we ultimately want is affine — local = a*dataset + b — so
    this looks for evidence of each parameter separately. The crucial subtlety:
    a *constant delta* with a *varying ratio* is additive, while a *constant
    ratio* with a varying delta is multiplicative. Reading the ratio alone will
    mistake a fixed 197s of credits on a 3100s episode for a 6% speedup.
    """
    notes: list[str] = []
    typical, odd = _outliers(rows)
    if not typical:
        return ["No episode could be compared — nothing to interpret."]

    deltas = [r.delta_stated for r in typical]
    ratios = [r.ratio for r in typical if r.ratio is not None]
    lo, mid, hi = _spread(deltas)
    spread = hi - lo
    r_lo, r_mid, r_hi = _spread(ratios)

    notes.append(
        f"delta vs stated runtime: median {mid:+.1f}s, range {lo:+.1f}s to {hi:+.1f}s "
        f"(spread {spread:.1f}s)   [over {len(typical)} typical episodes]"
    )
    notes.append(f"ratio local/stated:      median {r_mid:.4f}, range {r_lo:.4f} to {r_hi:.4f}")

    delta_is_stable = spread < 60
    ratio_is_stable = (r_hi - r_lo) < 0.006

    if ratio_is_stable and not delta_is_stable:
        notes.append(
            f"VERDICT: the ratio is stable ({r_mid:.4f}) while the delta is not — this is "
            "multiplicative. Consistent with a framerate conversion such as PAL speedup."
        )
    elif delta_is_stable:
        notes.append(
            f"VERDICT: the delta is stable ({mid:+.0f}s) while the ratio drifts "
            f"({r_lo:.4f}–{r_hi:.4f}) — this is ADDITIVE, not a speedup. The ratio only "
            "looks like a ~6% stretch because a fixed number of seconds is a larger "
            "fraction of a short episode than a long one."
        )
        notes.append(
            "  IMPORTANT: a stable duration delta does NOT by itself imply a timestamp "
            "offset. Extra material at the END of a file (credits, bonus features) "
            "changes duration while leaving every scene timestamp correct. Only the "
            "subtitle cross-check below can tell the two apart."
        )
    else:
        notes.append(
            f"VERDICT: neither delta ({spread:.0f}s spread) nor ratio is stable. Treat the "
            "reference as unreliable and calibrate from subtitles instead."
        )

    if odd:
        codes = ", ".join(r.code for r in odd)
        notes.append(
            f"OUTLIERS ({len(odd)}), excluded from the verdict above: {codes}"
        )
        notes.append(
            "  These differ structurally from the rest of the library — a different rip "
            "source or extra content in the file. Inspect them individually; do not let "
            "one global offset speak for them."
        )

    # Cross-check the two references against each other. If they disagree, the
    # dataset is internally inconsistent and neither delta means much.
    both = [
        (r.stated - r.scenes_end)
        for r in rows
        if r.stated is not None and r.scenes_end is not None
    ]
    if both:
        b_lo, b_mid, b_hi = _spread([float(x) for x in both])
        notes.append(
            f"reference cross-check (stated - last scene end): median {b_mid:+.0f}s, "
            f"range {b_lo:+.0f}s to {b_hi:+.0f}s"
        )
        if abs(b_mid) > 30 or (b_hi - b_lo) > 300:
            notes.append(
                "  NOTE: the dataset's two runtime references disagree substantially. "
                "Scenes may not tile the full episode (credits, cold opens). Prefer "
                "the 'stated' column, and treat subtitle cross-check as the tiebreak."
            )
    return notes


def render_table(rows: list[Row]) -> str:
    header = (
        f"{'episode':<8} {'local':>9} {'stated':>8} {'delta':>9} "
        f"{'ratio':>7} {'scenesEnd':>10} {'delta':>9}"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        local = f"{row.local:9.1f}" if row.local is not None else f"{'MISSING':>9}"
        stated = f"{row.stated:8d}" if row.stated is not None else f"{'-':>8}"
        d_stated = (
            f"{row.delta_stated:+9.1f}" if row.delta_stated is not None else f"{'-':>9}"
        )
        ratio = f"{row.ratio:7.4f}" if row.ratio is not None else f"{'-':>7}"
        scenes = f"{row.scenes_end:10d}" if row.scenes_end is not None else f"{'-':>10}"
        d_scenes = (
            f"{row.delta_scenes:+9.1f}" if row.delta_scenes is not None else f"{'-':>9}"
        )
        lines.append(
            f"{row.code:<8} {local} {stated} {d_stated} {ratio} {scenes} {d_scenes}"
        )
    return "\n".join(lines)
