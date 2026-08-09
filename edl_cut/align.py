"""Robust per-episode offset estimation.

The two-anchor approach (first cue, last cue) was enough to reveal the shape of
the problem but resolved only 30 of 73 episodes, because both extremes are
polluted: subtitled "previously on" recaps drag the head anchor early, and
credit-roll song lyrics disturb the tail. Neither is a defect in the data — they
are just bad places to measure.

This estimator uses a signal that appears dozens of times per episode instead of
twice, and exploits a property of television rather than of any one file:

    dialogue almost never spans a scene cut

So if you shift the dataset's scene boundaries by the correct offset, they land
in the gaps between subtitle cues. Shift them wrongly and they land on top of
dialogue at roughly the rate dialogue occupies the episode (~50%). Sweeping the
offset and scoring "what fraction of boundaries land in silence" gives a sharp
peak at the true offset, and the peak's height above the background tells you
how much to trust it.

This needs no knowledge of what is being said, so it works for any series, any
language, and any subtitle track.
"""

from __future__ import annotations

from dataclasses import dataclass

# Resolution of the occupancy grid. Finer than this buys nothing: subtitle cue
# times are authored by hand and scene boundaries in the dataset are whole
# seconds.
GRID = 0.25

# A boundary within this distance of a cue edge is treated as ambiguous rather
# than as evidence either way — cues routinely start a beat before a cut and
# linger a beat after.
EDGE_TOLERANCE = 0.75


@dataclass
class Estimate:
    offset: float | None
    score: float          # fraction of boundaries landing in silence, at best offset
    baseline: float       # median score across all candidate offsets
    margin: float         # score - baseline; the peak's height above noise
    boundaries: int       # how many scene boundaries were usable
    note: str = ""
    # Distance from the group's elected consensus. Agreement with siblings is
    # independent corroboration, so a modest peak that lands where the rest of
    # the season landed is worth as much as a sharp peak standing alone.
    consensus_delta: float | None = None

    @property
    def confident(self) -> bool:
        """A peak worth acting on.

        Judged on `margin`, not `score`. The absolute score depends on how
        talkative the episode is — a dialogue-heavy hour leaves fewer silent
        gaps for boundaries to land in, so a correct offset there may score
        0.50 while a quiet episode scores 0.85. The height of the peak above
        that episode's own background is the part that means anything.
        """
        if self.offset is None or self.boundaries < 8:
            return False
        if self.margin >= 0.20:
            return True
        return self.consensus_delta is not None and abs(self.consensus_delta) <= 20.0


def _occupancy(cues: list[tuple[float, float]], span: float) -> bytearray:
    """1 where dialogue is on screen, 0 where it is silent."""
    size = int(span / GRID) + 2
    grid = bytearray(size)
    for start, end in cues:
        lo = max(0, int((start - EDGE_TOLERANCE) / GRID))
        hi = min(size - 1, int((end + EDGE_TOLERANCE) / GRID))
        for i in range(lo, hi + 1):
            grid[i] = 1
    return grid


def estimate(
    boundaries: list[int],
    cues: list[tuple[float, float]],
    search_lo: float = -420.0,
    search_hi: float = 420.0,
    step: float = 0.5,
) -> Estimate:
    """Find the additive offset that best drops scene boundaries into silence.

    `boundaries` are dataset-time scene edges; `cues` are local-time subtitle
    intervals. Returns local = dataset + offset.
    """
    if not cues or len(boundaries) < 8:
        return Estimate(None, 0.0, 0.0, 0.0, len(boundaries), "insufficient data")

    span = max(cues[-1][1], max(boundaries)) + abs(search_lo) + abs(search_hi) + 10
    grid = _occupancy(cues, span)
    size = len(grid)

    # Interior boundaries only. The first and last edges of the episode sit next
    # to recap and credits, which is exactly the pollution we are avoiding.
    interior = sorted(set(boundaries))[1:-1]
    if len(interior) < 8:
        return Estimate(None, 0.0, 0.0, 0.0, len(interior), "too few interior boundaries")

    # Only the stretch where subtitles actually exist counts as evidence.
    # Outside it the grid reads as silence for the trivial reason that nothing
    # was ever timed there, so a large offset that shoves every boundary past
    # the final cue would otherwise score near-perfectly. That artefact made the
    # first version of this estimator prefer implausible ~+400s offsets.
    window_lo, window_hi = cues[0][0], cues[-1][1]
    min_in_window = max(8, int(0.6 * len(interior)))

    scores: list[tuple[float, float]] = []
    offset = search_lo
    while offset <= search_hi:
        hits = considered = 0
        for b in interior:
            t = b + offset
            if not (window_lo <= t <= window_hi):
                continue
            considered += 1
            idx = int(t / GRID)
            if 0 <= idx < size and not grid[idx]:
                hits += 1
        if considered >= min_in_window:
            scores.append((offset, hits / considered))
        offset += step

    if not scores:
        return Estimate(None, 0.0, 0.0, 0.0, len(interior),
                        "no offset keeps enough boundaries inside the subtitled span")

    best_offset, best_score = max(scores, key=lambda p: p[1])
    ordered = sorted(s for _, s in scores)
    baseline = ordered[len(ordered) // 2]

    return Estimate(
        offset=best_offset,
        score=best_score,
        baseline=baseline,
        margin=best_score - baseline,
        boundaries=len(interior),
    )


def refine(estimate_: Estimate, boundaries: list[int],
           cues: list[tuple[float, float]]) -> Estimate:
    """Second pass at finer resolution around an accepted peak."""
    if estimate_.offset is None:
        return estimate_
    fine = estimate(
        boundaries, cues,
        search_lo=estimate_.offset - 2.0,
        search_hi=estimate_.offset + 2.0,
        step=0.1,
    )
    if fine.offset is None:
        return estimate_
    # Keep the wide search's score/baseline/margin. Recomputed over a 4-second
    # window the baseline is essentially the peak itself, which would report
    # every refined estimate as margin ~0.
    fine.score = estimate_.score
    fine.baseline = estimate_.baseline
    fine.margin = estimate_.margin
    fine.note = estimate_.note
    return fine


# Margin above which a lone episode's peak is trusted without corroboration.
TRUSTED_MARGIN = 0.30

# How far an episode may sit from its season's consensus before we stop
# believing it. Recap lengths vary within a season by well under a minute.
CONSENSUS_WINDOW = 90.0


def trim_to_episode(cues: list[tuple[float, float]], last_scene_end: int,
                    slack: float = 240.0) -> list[tuple[float, float]]:
    """Drop cues belonging to material appended after the episode.

    Season 8 of the reference library carries an 'Inside the Episode'
    featurette inside the same file, and its commentary is subtitled. Left in,
    it stretches the evidence window by ten minutes and flattens the peak.
    """
    return [c for c in cues if c[1] <= last_scene_end + slack] or cues


def calibrate_group(items: list[tuple[str, list[int], list[tuple[float, float]]]]
                    ) -> dict[str, Estimate]:
    """Estimate offsets for a group of episodes that share a rip source.

    Two stages. First every episode is searched independently over the full
    range. Then the episodes whose peaks were sharp elect a consensus, and the
    rest are re-searched within a window around it.

    The second stage is what rescues the ambiguous episodes: a quiet episode
    with sparse dialogue has a genuinely flat scoring curve, and its global
    maximum can land anywhere. Constrained to the neighbourhood its siblings
    agree on, the same curve usually has a clear local peak in the right place.
    """
    import statistics

    stage1 = {code: estimate(bounds, cues) for code, bounds, cues in items}

    found = [e.offset for e in stage1.values() if e.offset is not None]
    if len(found) < 3:
        for est in stage1.values():
            est.note = est.note or "group too small to elect a consensus"
        return stage1

    # A plain median, not a margin-filtered one. The median already survives a
    # minority of wrong peaks, whereas requiring a high margin threw away whole
    # seasons whose peaks were real but modest.
    consensus = statistics.median(found)

    final: dict[str, Estimate] = {}
    for code, bounds, cues in items:
        first = stage1[code]
        if first.offset is not None and abs(first.offset - consensus) <= CONSENSUS_WINDOW:
            best = first
        else:
            best = estimate(
                bounds, cues,
                search_lo=consensus - CONSENSUS_WINDOW,
                search_hi=consensus + CONSENSUS_WINDOW,
                step=0.5,
            )
            best.note = (
                f"unconstrained peak {first.offset:+.0f}s rejected as implausible; "
                f"re-searched near group consensus {consensus:+.0f}s"
            )
        result = refine(best, bounds, cues) if best.offset is not None else best
        if result.offset is not None:
            result.consensus_delta = result.offset - consensus
        final[code] = result
    return final
