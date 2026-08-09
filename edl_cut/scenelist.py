"""Building and serialising character scene lists.

Scene lists are the durable artifact of this project — the thing worth
versioning, hand-correcting, and publishing. They are always expressed in
**dataset time**, never in the timing of any particular library.

That is the single most important property of this module. A scene list carrying
one user's calibration baked in is wrong for every other user, and silently so.
Calibration is applied at emit time, on the machine that owns the media.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .dataset import Episode, format_timestamp

# Segments closer together than this are joined. The dataset starts a new record
# at every location change, so a cross-cut battle sequence arrives as a stutter
# of very short fragments; without merging, a filtered cut is technically
# correct and unwatchable.
DEFAULT_MERGE_GAP = 30.0

# Breathing room around each segment. A cut that lands exactly on the first
# syllable of a line reads as broken even when the timestamp is right.
DEFAULT_PAD_PRE = 1.5
DEFAULT_PAD_POST = 1.5


@dataclass
class Segment:
    episode: str
    start: float
    end: float
    label: str
    tags: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start


def _tags_for(scenes: list) -> list[str]:
    """Derive tags from the dataset rather than inventing a vocabulary.

    Location and sublocation are near-universal in the data; flashback,
    greensight and warg are rare (41, 16 and 7 scenes across the whole series)
    but are exactly the ones a viewer wants to filter on, so they are worth
    carrying even though they will rarely fire.
    """
    tags: list[str] = []
    for scene in scenes:
        for value in (scene.location, scene.sub_location):
            if value:
                slug = value.lower().replace(" ", "-").replace("'", "")
                if slug not in tags:
                    tags.append(slug)
        for flag, name in ((scene.flashback, "flashback"),
                           (scene.greensight, "greensight"),
                           (scene.warg, "warg")):
            if flag and name not in tags:
                tags.append(name)
    return tags


def _label_for(scenes: list, character: str) -> str:
    """A short human-readable description, from location and co-present cast.

    Deliberately descriptive rather than quoted: labels are our own original
    text about the scene, never transcribed dialogue.
    """
    primary = scenes[0]
    where = primary.sub_location or primary.location or "unknown"
    others = []
    for scene in scenes:
        for name in scene.characters:
            if name != character and name not in others:
                others.append(name)
    if others:
        listed = ", ".join(others[:3])
        if len(others) > 3:
            listed += f" +{len(others) - 3}"
        return f"{where} — with {listed}"
    return f"{where} — alone"


def build(
    episodes: list[Episode],
    character: str,
    merge_gap: float = DEFAULT_MERGE_GAP,
    pad_pre: float = DEFAULT_PAD_PRE,
    pad_post: float = DEFAULT_PAD_POST,
) -> list[Segment]:
    """Every scene `character` is present for, merged and padded, in dataset time."""
    segments: list[Segment] = []

    for episode in episodes:
        present = [s for s in episode.scenes if character in s.characters]
        if not present:
            continue
        present.sort(key=lambda s: s.start)

        # Group into runs separated by more than merge_gap. Grouping the source
        # scenes rather than just their times keeps the labels and tags
        # meaningful for the merged whole.
        runs: list[list] = [[present[0]]]
        for scene in present[1:]:
            if scene.start - runs[-1][-1].end <= merge_gap:
                runs[-1].append(scene)
            else:
                runs.append([scene])

        for run in runs:
            start = max(0.0, run[0].start - pad_pre)
            end = run[-1].end + pad_post
            segments.append(
                Segment(
                    episode=episode.code,
                    start=start,
                    end=end,
                    label=_label_for(run, character),
                    tags=_tags_for(run),
                )
            )
    return segments


def filter_by_tags(segments: list[Segment], include: list[str] | None,
                   exclude: list[str] | None) -> list[Segment]:
    out = segments
    if include:
        wanted = {t.lower() for t in include}
        out = [s for s in out if wanted & set(s.tags)]
    if exclude:
        unwanted = {t.lower() for t in exclude}
        out = [s for s in out if not (unwanted & set(s.tags))]
    return out


def to_yaml(segments: list[Segment], series: str, character: str,
            version: str = "1.0") -> str:
    """Serialise by hand rather than via PyYAML.

    This file is meant to be read and edited by people and diffed in review, so
    the layout is chosen for that: one scene per stanza, quoted timestamps,
    inline tag lists. A dumper would reformat it on every round trip and make
    the diffs unreadable.
    """
    total = sum(s.duration for s in segments)
    lines = [
        f'version: "{version}"',
        f'series: "{series}"',
        f'character: "{character}"',
        "# Timestamps are in DATASET time, not calibrated to any library.",
        "# edl-cut applies per-episode offsets when it emits a playlist.",
        f"# {len(segments)} segments, {total / 3600:.2f} hours.",
        "scenes:",
    ]
    for segment in segments:
        tags = ", ".join(segment.tags)
        label = segment.label.replace('"', "'")
        lines.append(f"  - episode: {segment.episode}")
        lines.append(f'    start: "{format_timestamp(segment.start)}"')
        lines.append(f'    end: "{format_timestamp(segment.end)}"')
        lines.append(f'    label: "{label}"')
        lines.append(f"    tags: [{tags}]")
    return "\n".join(lines) + "\n"
