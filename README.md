# edl-cut

An EDL toolkit for character-focused rewatches — watch one character's story
through a series, in order, skipping everything they aren't in.

Demonstrated with *Game of Thrones*. The tool is the artifact; the scene lists
are the examples.

## This project ships timestamps and code. Never video.

That is a hard boundary, not a preference.

- No video, audio, frames, or thumbnails from any source material live in this
  repo — not as test fixtures, not as example output, not as documentation
  images. `.gitignore` enforces it and was written before the exporter existed.
- You point the tool at media **you already own**. It reads durations and
  subtitle timings from your files to calibrate, and emits a playlist. It never
  copies, uploads, or redistributes any part of them.
- Test fixtures are synthetic — colour bars generated with ffmpeg's `testsrc`
  and `sine` filters — or public domain.
- The scene lists are original creative work: scene-boundary decisions,
  inclusion judgments, and labels. The code is original. Neither is derivative
  of the footage.

If a design choice would require shipping any part of the source video, the
answer is no.

## Credit where it is due

Scene boundaries and per-scene character presence come from **Jeffrey
Lancaster's *Game of Thrones* dataset**:

<https://github.com/jeffreylancaster/game-of-thrones>

That dataset is the reason this project is a weekend's work rather than a year
of manual logging. The author permits reuse and asks for citation and a note
about what was built with it; see [data/ATTRIBUTION.md](data/ATTRIBUTION.md).
Please keep that notice intact if you fork this.

## How it works

The dataset records, for every scene in all 73 episodes, which characters are
**present** — not who speaks. Filtering it by character gives you that
character's story as scenes, not as a dialogue reel.

For Daenerys Targaryen that is 509 scenes across 62 episodes, about 8.8 hours,
13% of the series. Every major character yields a comparable cut: Jon Snow
11.4 h, Tyrion Lannister 11.6 h, Cersei Lannister 7.2 h, Arya Stark 6.8 h.

Three things stand between the dataset and a watchable cut:

1. **Calibration.** The dataset's timestamps are relative to whatever cut its
   author watched. Yours will differ. See below — this is the hard part.
2. **Merging.** The dataset splits on every location change, so a cross-cut
   battle becomes a stutter of 3-second fragments. Adjacent segments are
   coalesced within a configurable gap.
3. **Emitting.** mpv EDL (default), VLC M3U, or an ffmpeg cut list.

## Calibration

The single technical problem worth taking seriously. Two signals are compared:

**Duration** — the dataset's stated runtime against `ffprobe`'s. Cheap, and a
good smoke test, but it cannot distinguish a recap at the *front* of a file
(which shifts every timestamp) from credits at the *back* (which shift
nothing). Both change the duration identically.

**Subtitles** — timed against your own files. Scene boundaries land in the gaps
between subtitle cues when the offset is right, because dialogue rarely spans a
cut. Dozens of anchors per episode, and no knowledge of what is being said, so
it works in any language.

**End credits** — the long black run before any bonus content starts exactly
where the episode's own content ends. This is the fallback when a file carries
appended material that makes its duration meaningless.

The duration predictor sets a narrow prior; subtitle scoring picks the exact
value inside it. Agreement between two independent measurements is the
confidence test, and disagreements are reported rather than guessed at.

Run the diagnostic before anything else:

```bash
python3 -m edl_cut.cli --calibrate --media /path/to/your/media
```

It prints its evidence before writing anything, and names every episode it
could not resolve rather than quietly assuming zero. Offsets land in `cache/`,
keyed to that library and never committed. Any entry can be corrected by hand;
set `"manual": true` on it and recalibration will leave it alone.

## Usage

Calibrate once per library, then generate as many cuts as you like:

```bash
python3 -m edl_cut.cli --calibrate --media /path/to/your/media
python3 -m edl_cut.cli --character "Daenerys Targaryen" --media /path/to/your/media \
        --format edl --out dany.edl --scene-list dany-scenes.yaml
mpv dany.edl
```

Useful flags: `--merge-gap` (join segments closer than N seconds, default 30),
`--pad` (breathing room each side, default 1.5), `--tags` / `--exclude-tags`,
and `--format m3u|concat`.

### Exporting a real video file

For anything that cannot open an EDL — tablets, Plex, televisions:

```bash
python3 -m edl_cut.cli --character "Daenerys Targaryen" --media /path/to/media \
        --format mkv --out dany.mkv --dry-run
```

`--dry-run` runs preflight and planning and writes nothing; drop it to export.
Three strategies:

| `--mode` | Boundaries | Cost |
|---|---|---|
| `precise` (default) | frame-accurate | re-encodes only the fragment from each cut to the next keyframe |
| `copy` | snap back to the preceding keyframe, up to a GOP early | pure remux, disk speed, no quality loss |
| `reencode` | frame-accurate | slow and lossy; a last resort |

`precise` is the reason this exists: it is the one thing neither mpv nor VLC can
do. Preflight checks free space, estimates output size, and — because the concat
demuxer needs matching codec, resolution, pixel format and audio layout — probes
every file first. Where a minority of files differ, they are re-encoded to match
the majority rather than aborting or re-encoding everything.

Any character in the dataset works:

```bash
python3 -m edl_cut.cli --character "Arya Stark" --media /path/to/your/media
```

## Status

On the reference library — 73 episodes, mixed Blu-ray and WEB sources — all 73
episodes calibrate, 69 automatically and 4 by hand, producing a complete
9.14-hour Daenerys cut with nothing skipped.

Milestones 1–4 are done: calibration diagnostic, per-episode offset estimation,
scene-list generation, and the mpv EDL / VLC M3U / ffmpeg-concat emitters. The
keyframe-accurate MKV exporter is next.

See [FINDINGS.md](FINDINGS.md) for what a real 73-episode library turned up —
including why the obvious reading of the duration table is wrong, and the three
failures it took to get the offset estimator working.

## Requirements

Python 3.10+, PyYAML, and `ffmpeg`/`ffprobe` on PATH. `mpv` if you want to play
the default output format. Runs on Linux and Windows.

## Tests

```bash
python3 -m unittest discover -s tests -t . -v
```

Fixtures are synthetic — colour bars and sine tones generated by ffmpeg's
`testsrc` and `sine` filters — so the suite exercises real media handling
without any footage existing in the repo. Tests needing `ffmpeg` or `mpv` skip
cleanly if those are absent locally; CI installs both so they always run.

The suite leans toward the *silent* failure modes: a playlist that opens fine
and plays the wrong footage is far more dangerous than one that crashes. CI also
fails the build if any test writes media into the working tree.

## Contributing

The interesting contribution is **another character's scene list**, or another
series entirely. The tool is deliberately not Daenerys-specific; if you produce
`tyrion-scenes.yaml` against the same format, it becomes a format rather than a
one-off.

Scene lists must stay in **dataset time**. Never commit one with your own
library's calibration applied — it will be silently wrong for everyone else.
Bug reports about timestamps should include the output of `--calibrate`, which
prints its evidence.

## Licence

MIT — see [LICENSE](LICENSE).

That covers the original work here: the code, the documentation, the nickname
table, and the scene lists. It does **not** cover the dataset files vendored
into `data/`, which are Jeffrey Lancaster's and stay under their upstream terms.
[NOTICE.md](NOTICE.md) sets out exactly which files fall where, and
[data/ATTRIBUTION.md](data/ATTRIBUTION.md) carries the upstream terms.

No audiovisual material from any television series is contained in or conveyed
by this repository.
