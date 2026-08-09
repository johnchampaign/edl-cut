"""Two-signal calibration.

Two facts about a local file constrain the offset, and they come from completely
different evidence:

**Duration.** An episode file is its content plus end credits. So

    local_duration  =  (last_scene_end + offset) + credits

and once `credits` is known, the offset falls out of ffprobe alone. Across this
library that predictor has a median error of 2.5 seconds. It needs no subtitles,
costs one probe per file, and — crucially — is available for episodes whose
dialogue is too sparse for the subtitle method to find a peak.

**Subtitle timing.** Boundary-in-silence scoring (see `align.py`), which knows
nothing about duration.

Neither is trusted alone. The duration predictor sets a narrow prior; subtitle
scoring picks the exact value inside it. Agreement between two independent
signals is the confidence measure, which is far stronger than either one's
internal margin.

The earlier season-consensus approach is kept only as a fallback, because it
turned out to be actively harmful where it was applied without a prior: seasons
whose true offsets span 160 seconds had correct answers rejected by a ±90 second
window and replaced with wrong ones.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from . import align

# How far subtitle scoring may wander from the duration prior. Wide enough to
# absorb the predictor's error (p90 is under 17s), tight enough to exclude the
# spurious distant peaks that defeated the unconstrained search.
PRIOR_WINDOW = 45.0

# Agreement between the two signals, beyond which we stop believing them.
AGREEMENT_TOLERANCE = 25.0

# Fallback credits length if a library gives us too little to learn from.
DEFAULT_CREDITS = 83.0

# How far an uncorroborated duration-only prediction may sit from its season's
# resolved episodes before we refuse to report it as calibrated.
PEER_TOLERANCE = 120.0


@dataclass
class Input:
    code: str
    boundaries: list[int]
    cues: list[tuple[float, float]]
    duration: float | None
    last_scene_end: int
    # True when material was detected after the episode proper (a featurette
    # bundled into the same file). Duration is then not a measure of the
    # episode, so the predictor must not be used.
    appended: bool = False


@dataclass
class Result:
    code: str
    offset: float | None
    source: str            # how it was decided
    predicted: float | None
    subtitle: float | None
    agreement: float | None
    margin: float
    confident: bool
    note: str = ""


def _season(code: str) -> str:
    return code[:3]


def learn_credits(inputs: list[Input],
                  rough: dict[str, align.Estimate]) -> tuple[float, dict[str, float]]:
    """Infer trailing (credits) length from the library's own files.

    Learned rather than assumed, because it is a property of how the library was
    encoded — a rip that strips credits and one that keeps them differ by well
    over a minute, and a wrong constant would bias every prediction equally.

    Learned per season as well as overall, because seasons are where encoding
    conventions change: in the reference library season 1's trailing length runs
    about 12s shorter than the rest, which is enough to bias its predictions.
    """
    samples: list[float] = []
    by_season: dict[str, list[float]] = {}
    for item in inputs:
        estimate = rough.get(item.code)
        if (item.appended or item.duration is None
                or estimate is None or estimate.offset is None
                or not estimate.confident):
            continue
        value = item.duration - item.last_scene_end - estimate.offset
        samples.append(value)
        by_season.setdefault(_season(item.code), []).append(value)

    overall = statistics.median(samples) if len(samples) >= 5 else DEFAULT_CREDITS
    per_season = {s: statistics.median(v) for s, v in by_season.items() if len(v) >= 4}
    return overall, per_season


def predict(item: Input, credits: float) -> float | None:
    if item.appended or item.duration is None:
        return None
    return item.duration - item.last_scene_end - credits


def calibrate(inputs: list[Input]) -> tuple[dict[str, Result], float]:
    """Resolve every episode, preferring corroboration over any single signal."""
    # Pass 1: unconstrained subtitle search, only to bootstrap the credits
    # length. Its individual answers are not trusted here.
    rough = {
        item.code: align.estimate(item.boundaries, item.cues)
        for item in inputs
    }
    overall_credits, season_credits = learn_credits(inputs, rough)

    results: dict[str, Result] = {}
    fallback: list[Input] = []
    duration_only: list[Input] = []

    for item in inputs:
        credits = season_credits.get(_season(item.code), overall_credits)
        prior = predict(item, credits)
        if prior is None:
            fallback.append(item)
            continue

        if not item.cues:
            # No subtitles, so nothing corroborates the prediction and — worse —
            # nothing could have detected appended material in this file either.
            # Held back for a plausibility check against its season's peers.
            duration_only.append(item)
            continue

        constrained = align.estimate(
            item.boundaries, item.cues,
            search_lo=prior - PRIOR_WINDOW,
            search_hi=prior + PRIOR_WINDOW,
            step=0.25,
        )
        subtitle = constrained.offset
        if subtitle is None:
            results[item.code] = Result(
                item.code, round(prior, 1), "duration-only", prior, None, None,
                0.0, True, "subtitle scoring found nothing in the prior window",
            )
            continue

        agreement = subtitle - prior
        agrees = abs(agreement) <= AGREEMENT_TOLERANCE
        results[item.code] = Result(
            code=item.code,
            offset=round(subtitle, 1),
            source="duration+subtitles" if agrees else "disputed",
            predicted=round(prior, 1),
            subtitle=round(subtitle, 1),
            agreement=round(agreement, 1),
            margin=constrained.margin,
            confident=agrees,
            note="" if agrees else (
                f"duration says {prior:+.0f}s, subtitles say {subtitle:+.0f}s — "
                "spot-check before trusting"
            ),
        )

    # Episodes where duration is unusable (appended bonus material) fall back to
    # the subtitle-only path with season consensus.
    if fallback:
        group = [(i.code, i.boundaries, i.cues) for i in fallback if i.cues]
        consensus = align.calibrate_group(group) if len(group) >= 3 else {
            code: align.estimate(b, c) for code, b, c in group
        }
        for item in fallback:
            estimate = consensus.get(item.code)
            if estimate is None or estimate.offset is None:
                results[item.code] = Result(
                    item.code, None, "unresolved", None, None, None, 0.0, False,
                    "file contains appended material and has no usable subtitles",
                )
                continue
            results[item.code] = Result(
                code=item.code,
                offset=round(estimate.offset, 1),
                source="subtitles-only",
                predicted=None,
                subtitle=round(estimate.offset, 1),
                agreement=None,
                margin=estimate.margin,
                confident=estimate.confident,
                note="duration unusable (appended bonus content); subtitles only",
            )

    # Duration-only episodes last, so their season's other answers are available
    # to sanity-check them. Without subtitles we cannot detect appended material
    # in the file, so a prediction that disagrees wildly with its neighbours is
    # far more likely to be measuring a bundled featurette than a real offset.
    # Reporting such a value as confident is the worst outcome available: it
    # generates a plausible-looking cut of entirely the wrong footage.
    for item in duration_only:
        credits = season_credits.get(_season(item.code), overall_credits)
        prior = predict(item, credits)
        peers = [r.offset for c, r in results.items()
                 if _season(c) == _season(item.code) and r.offset is not None
                 and r.confident]
        if peers and abs(prior - statistics.median(peers)) > PEER_TOLERANCE:
            results[item.code] = Result(
                item.code, None, "unresolved", round(prior, 1), None, None, 0.0,
                False,
                f"no subtitles, and duration implies {prior:+.0f}s against a season "
                f"median of {statistics.median(peers):+.0f}s — the file probably "
                "contains extra material. Set this one by hand.",
            )
            continue
        results[item.code] = Result(
            item.code, round(prior, 1), "duration-only", round(prior, 1), None,
            None, 0.0, bool(peers),
            "no subtitle track; offset from duration alone, please spot-check",
        )
    return results, overall_credits


def render_table(results: dict[str, Result]) -> str:
    header = (f"{'episode':<8} {'offset':>9} {'predicted':>10} {'subtitle':>9} "
              f"{'agree':>7} {'source':<20} note")
    lines = [header, "-" * (len(header) + 12)]
    for code in sorted(results):
        r = results[code]
        def fmt(v, w):
            return f"{v:{w}.1f}" if v is not None else f"{'-':>{w}}"
        flag = "" if r.confident else "  <-- CHECK"
        lines.append(
            f"{r.code:<8} {fmt(r.offset, 9)} {fmt(r.predicted, 10)} "
            f"{fmt(r.subtitle, 9)} {fmt(r.agreement, 7)} {r.source:<20} "
            f"{r.note}{flag}"
        )
    return "\n".join(lines)
