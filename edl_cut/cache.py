"""Per-library calibration cache.

Calibration is expensive (it reads every subtitle track) and it is a property of
one machine's media, so it is computed once, stored locally, and never
committed. `.gitignore` excludes `cache/` for that reason.

The file is plain JSON and hand-editable on purpose: automatic estimation will
fail on somebody's unusual encode, and the escape hatch has to be obvious.
"""

from __future__ import annotations

import json
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"


def path_for(media_root: Path) -> Path:
    """One cache file per library, keyed by a readable slug of its path."""
    slug = "".join(c if c.isalnum() else "-" for c in str(media_root.resolve()))
    slug = "-".join(filter(None, slug.split("-")))[-90:]
    return CACHE_DIR / f"offsets-{slug}.json"


def save(media_root: Path, entries: dict[str, dict]) -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    target = path_for(media_root)
    payload = {
        "media_root": str(media_root.resolve()),
        "note": (
            "Offsets are in seconds and additive: local = dataset + offset. "
            "They describe THIS library only — never publish them with a scene "
            "list. Edit 'offset' by hand to override; set 'manual': true so a "
            "recalibration does not overwrite your value."
        ),
        "episodes": entries,
    }
    target.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return target


def load(media_root: Path) -> dict[str, dict]:
    target = path_for(media_root)
    if not target.exists():
        return {}
    return json.loads(target.read_text(encoding="utf-8")).get("episodes", {})


def offsets(media_root: Path, include_unconfident: bool = False) -> dict[str, float]:
    """Episode code -> offset, for the emitters."""
    out = {}
    for code, entry in load(media_root).items():
        if entry.get("manual") or entry.get("confident") or include_unconfident:
            out[code] = float(entry["offset"])
    return out
