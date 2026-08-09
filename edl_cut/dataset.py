"""Reading Jeffrey Lancaster's Game of Thrones dataset.

Everything here works in *dataset time* — seconds from the start of whatever cut
the dataset's author was watching. Nothing in this module knows about the user's
local files. That separation is deliberate: scene lists are a property of
(series, character), calibration is a property of (library, episode), and mixing
them produces scene lists that only work on one person's rip.

See data/ATTRIBUTION.md for the source and its licence terms.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def parse_timestamp(text: str) -> int:
    """'0:14:32' -> 872 seconds. The dataset uses H:MM:SS without zero padding."""
    parts = [int(p) for p in text.strip().split(":")]
    if len(parts) == 2:  # tolerate MM:SS
        parts = [0] + parts
    if len(parts) != 3:
        raise ValueError(f"unparseable timestamp: {text!r}")
    hours, minutes, seconds = parts
    return hours * 3600 + minutes * 60 + seconds


def format_timestamp(total: float) -> str:
    """872 -> '00:14:32'. Zero padded, for scene lists meant to be hand-edited."""
    total = int(round(total))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


@dataclass(frozen=True)
class Scene:
    start: int
    end: int
    location: str
    sub_location: str
    characters: tuple[str, ...]
    flashback: bool
    greensight: bool
    warg: bool

    @property
    def duration(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Episode:
    season: int
    number: int
    title: str
    scenes: tuple[Scene, ...]
    # Runtime in seconds from keyValues.json. None if that file wasn't loaded.
    stated_length: int | None = None

    @property
    def code(self) -> str:
        return f"S{self.season:02d}E{self.number:02d}"

    @property
    def last_scene_end(self) -> int | None:
        """Independent runtime estimate: where the final scene stops.

        Scenes tile gaplessly in most episodes, so this approximates the episode
        duration without relying on keyValues.json. Having two independent
        references is what lets calibration distinguish a genuine offset from a
        bad reference value.
        """
        return self.scenes[-1].end if self.scenes else None


def _load(name: str, data_dir: Path) -> dict:
    with open(data_dir / name, encoding="utf-8") as handle:
        return json.load(handle)


def _stated_lengths(data_dir: Path) -> dict[tuple[int, int], int]:
    """(season, episode) -> runtime in seconds, from keyValues.json.

    Note the nesting: keyValues['episodes'] is a list of *seasons*, each with its
    own 'episodes' list. The 'shift' fields alongside are a cumulative
    whole-series timeline offset for the author's visualisations, not a
    correction — we ignore them.
    """
    try:
        raw = _load("keyValues.json", data_dir)
    except FileNotFoundError:
        return {}
    lengths: dict[tuple[int, int], int] = {}
    for season in raw.get("episodes", []):
        for episode in season.get("episodes", []):
            if "length" in episode:
                key = (season["seasonNum"], episode["episodeNum"])
                lengths[key] = int(episode["length"])
    return lengths


def load_episodes(data_dir: Path = DATA_DIR) -> list[Episode]:
    raw = _load("episodes.json", data_dir)
    lengths = _stated_lengths(data_dir)

    episodes = []
    for entry in raw["episodes"]:
        scenes = []
        for scene in entry.get("scenes", []):
            start = parse_timestamp(scene["sceneStart"])
            end = parse_timestamp(scene["sceneEnd"])
            if end <= start:
                # A handful of records are zero-length or inverted. They carry no
                # footage, so dropping them here keeps every downstream consumer
                # from having to defend against it.
                continue
            scenes.append(
                Scene(
                    start=start,
                    end=end,
                    location=scene.get("location", ""),
                    sub_location=scene.get("subLocation", ""),
                    characters=tuple(
                        c["name"] for c in scene.get("characters", []) if "name" in c
                    ),
                    flashback=bool(scene.get("flashback", False)),
                    greensight=bool(scene.get("greensight", False)),
                    warg=bool(scene.get("warg", False)),
                )
            )
        season, number = entry["seasonNum"], entry["episodeNum"]
        episodes.append(
            Episode(
                season=season,
                number=number,
                title=entry.get("episodeTitle", ""),
                scenes=tuple(scenes),
                stated_length=lengths.get((season, number)),
            )
        )
    episodes.sort(key=lambda e: (e.season, e.number))
    return episodes


def character_names(episodes: list[Episode]) -> list[str]:
    """Every distinct character name that actually appears in a scene."""
    return sorted({name for e in episodes for s in e.scenes for name in s.characters})


def resolve_character(query: str, episodes: list[Episode],
                      data_dir: Path = DATA_DIR) -> tuple[str | None, list[str]]:
    """Turn what the user typed into an exact dataset name.

    Returns (resolved, suggestions). Resolution is attempted in descending order
    of confidence and stops at the first tier that yields exactly one candidate;
    a tier matching several names is ambiguous, so those become suggestions
    rather than a guess. Picking one arbitrarily would silently generate the
    wrong person's cut, which is far worse than asking.
    """
    import difflib

    names = character_names(episodes)
    if not names:
        return None, []
    query = query.strip()
    lowered = {n.lower(): n for n in names}

    # How many scenes each name appears in. Used only to order suggestions: for
    # an ambiguous query like 'Jon', the character with 632 scenes is far more
    # likely to be meant than a namesake with three, and burying them under an
    # alphabetical list is unhelpful.
    prominence: dict[str, int] = {}
    for episode in episodes:
        for scene in episode.scenes:
            for name in scene.characters:
                prominence[name] = prominence.get(name, 0) + 1

    # 1. Exact, then case-insensitive exact.
    if query in names:
        return query, []
    if query.lower() in lowered:
        return lowered[query.lower()], []

    # 2. Our own nickname table. The upstream dataset has no alias field, so
    #    'dany' and 'the hound' are unresolvable without it.
    try:
        with open(data_dir / "aliases.json", encoding="utf-8") as handle:
            aliases = {k.lower(): v for k, v in json.load(handle).items()
                       if not k.startswith("_")}
    except (FileNotFoundError, ValueError):
        aliases = {}
    target = aliases.get(query.lower())
    if target and target in names:
        return target, []

    # 3. Whole-word match, e.g. 'arya' -> 'Arya Stark'.
    words = [n for n in names if query.lower() in n.lower().split()]
    if len(words) == 1:
        return words[0], []

    # 4. Actor name, so 'Emilia Clarke' works.
    try:
        with open(data_dir / "characters.json", encoding="utf-8") as handle:
            records = json.load(handle)["characters"]
        by_actor = [r["characterName"] for r in records
                    if r.get("actorName", "").lower() == query.lower()
                    and r.get("characterName") in names]
        if len(by_actor) == 1:
            return by_actor[0], []
    except (FileNotFoundError, ValueError, KeyError):
        by_actor = []

    # Nothing certain. Rank suggestions: whole-word hits first, then substring,
    # then fuzzy — the order in which a person would recognise their own intent.
    suggestions: list[str] = list(words)
    for name in names:
        if name not in suggestions and query.lower() in name.lower():
            suggestions.append(name)
    for name in names:
        if name in suggestions:
            continue
        parts = [name.lower()] + name.lower().split()
        if difflib.get_close_matches(query.lower(), parts, n=1, cutoff=0.72):
            suggestions.append(name)
    suggestions.sort(key=lambda n: -prominence.get(n, 0))
    return None, suggestions[:6]
