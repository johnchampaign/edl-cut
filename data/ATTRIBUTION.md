# Data attribution

Every JSON file in this directory is vendored, unmodified, from:

**Jeffrey Lancaster — *Game of Thrones* dataset**
<https://github.com/jeffreylancaster/game-of-thrones>

The author permits reuse and asks in return for citation of the repository and a
note describing what was built with it. This project honours both: the citation
is here and in the top-level README, and the note is below.

**What was built with it:** `edl-cut`, a toolkit that generates playback
instructions (mpv EDL, VLC M3U, ffmpeg cut lists) for character-focused
rewatches. It reads this dataset's per-scene character rosters to determine
which scenes a given character is present for, calibrates those timestamps
against a user's own local media files, and emits a playlist. It distributes no
video.

Do not strip this notice. If you fork or vendor these files onward, carry it
with them.

**These files are not covered by this repository's MIT licence.** That licence
applies to our own code, documentation, and scene lists. The JSON files here
belong to their author and are redistributed under the terms above; we have no
authority to relicense them and have not attempted to. See `../NOTICE.md` for
the full file-by-file breakdown.

## Files

| File | Used for |
|---|---|
| `episodes.json` | Scene boundaries and per-scene character presence. The core input. |
| `keyValues.json` | Per-episode runtime in seconds. Calibration reference. |
| `characters.json` | Name normalisation and aliases. |
| `locations.json` | Location hierarchy, used to generate scene tags. |

## Accuracy

The author describes the dataset as "provided as is... probably not perfectly
accurate." This project treats it accordingly: as an excellent first pass that
gets hand-corrected downstream, not as ground truth. Corrections live in our
own scene lists, never by editing these files — keeping them pristine is what
makes re-vendoring from upstream possible.

## A note on `shift`

`keyValues.json` carries a `shift` field at both season and episode level. It is
a cumulative running offset used to lay every episode out on a single
whole-series timeline for the author's visualisations. It is **not** a
calibration or correction field, and must not be used as one.
