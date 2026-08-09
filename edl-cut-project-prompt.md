# Project Kickoff: Character-Focused Rewatch EDL Toolkit

## What we're building

A tool that generates **playback instructions** for character-focused supercuts of TV
series — starting with a Daenerys Targaryen cut of *Game of Thrones*.

The tool ships **timestamps and code only**. It never distributes video. Users point it
at media files they already own, and it emits a playlist/EDL/script that plays or
assembles only the scenes containing a chosen character.

The first proof of concept: every Daenerys scene across all 8 seasons, roughly 10–12
hours of runtime.

## Hard constraint: the distribution boundary

This is the single most important rule in the project, and it must not be relaxed for
convenience.

- The repo contains **no video, no audio, no frames, no thumbnails** from the source
  material — not as test fixtures, not as example output, not as documentation images.
- Any generated `.mkv` / `.mp4` / segment files go in `.gitignore` before the exporter
  is written, not after.
- Test fixtures must be synthetic (generate color-bar clips with ffmpeg's `testsrc` and
  `sine` filters) or use public-domain footage.
- The scene lists are original creative work (scene boundary decisions, inclusion
  judgments, labels). The code is original. Neither is derivative of HBO's footage.
- README should state this boundary plainly so it's obvious to anyone who finds the repo.

If a design choice would require shipping any part of the source video, the answer is no
— find a different design.

## Data source

**Jeffrey Lancaster's Game of Thrones dataset**
https://github.com/jeffreylancaster/game-of-thrones

Key file: `data/episodes.json`. Structure:

```
episodes[] -> {
  seasonNum, episodeNum, episodeTitle, episodeAirDate, ...
  scenes[] -> {
    sceneStart, sceneEnd,        // timestamps
    location, subLocation, altLocation,
    flashback, greensight, warg, // booleans
    characters[] -> { name, title, alive, ... }
  }
}
```

This gives us scene boundaries AND per-scene character presence for all 8 seasons — the
entire scene list, already assembled.

Also useful:
- `data/keyValues.json` — includes a per-episode `length` field. **Critical for
  calibration** (see below).
- `data/characters.json` — name normalization, aliases, house affiliation.
- `data/locations.json` — location/sublocation hierarchy, useful for tag generation.

**Licensing:** The author explicitly permits reuse and asks only for citation of the
repo and a note about what was built with it. Vendor the data files into `data/` with a
clear attribution notice, and credit prominently in the README. Do not strip attribution.

The dataset is described as "provided as is... probably not perfectly accurate," so
treat it as an excellent first pass that gets hand-corrected, not as ground truth.

## The central technical problem: timestamp calibration

The dataset's timestamps are relative to whatever cut the author was watching. Any given
user's rips will differ — recaps included or stripped, different intro handling, framerate
conversion, chapter padding.

**Solve this first, before building any output format.** It determines whether the whole
project is viable and it's the piece that makes someone else's timestamps work against an
arbitrary local file. Everything else is JSON filtering and string formatting.

### Calibration approach, in order of preference

1. **Duration comparison (the cheap diagnostic).** For each episode, compare the
   dataset's `length` against the local file:
   ```bash
   ffprobe -v error -show_entries format=duration -of csv=p=0 <file>
   ```
   Print a table of deltas across the whole library. Interpretation:
   - Deltas near zero → timing conventions match, use timestamps as-is
   - Constant delta across all episodes → fixed offset, add it
   - Varying delta per episode → recap length differences, need per-episode offsets
   - Consistent ~4% scaling → PAL speedup, multiply rather than add

2. **Title sequence detection (the robust method).** The opening credits are musically
   and visually near-identical every episode. Detect where the sequence starts in the
   local file and derive the offset from it. Audio fingerprinting or scene-change
   signature both work. This handles the varying-recap case automatically.

3. **Subtitle cross-check.** Subtitles extracted from the user's own rips are ground
   truth for that file. Matching known dialogue lines against the dataset's scene
   boundaries validates a computed offset.

4. **Manual override.** Always allow a per-episode offset override in a config file.
   Automation will fail on someone's weird encode; give them an escape hatch.

Store computed offsets in a cache file so calibration runs once per library, not once
per invocation.

## Output formats

Same scene list, three emitters, user's choice via `--format`.

### 1. mpv EDL (`--format edl`) — the good one, make this the default

```
# mpv EDL v0
S01E01.mkv,872,273
S01E01.mkv,2470,141
S01E02.mkv,415,208
```

Format is `path,start,LENGTH` — length, not end time. Easy to get wrong.

Why it's the default: no processing, no re-encoding, no disk cost, instant regeneration
when a boundary changes. mpv presents the whole thing as a single virtual file with one
continuous seekbar and chapter marks at segment boundaries. Seeks are keyframe-accurate
by default (can be off by up to a GOP length); exact seeking is available at the cost of
a short delay per cut. For scene-level cuts, keyframe accuracy is fine.

### 2. VLC M3U (`--format m3u`) — the accessible one

```
#EXTM3U
#EXTINF:-1,S01E01 - Dany at the wedding
#EXTVLCOPT:start-time=872
#EXTVLCOPT:stop-time=1145
file:///path/to/S01E01.mkv
```

Times in seconds. Technically the worst option — VLC stops and reopens the file at every
segment, so transitions visibly hitch. Keep it anyway: VLC is what most people already
have installed, so it's the zero-friction way to sample the cut before installing
anything.

### 3. ffmpeg export (`--format mkv`) — the portable one

Produces an actual video file, for tablets / Plex / TVs / anything that isn't mpv.

- **Stream copy path:** `-ss <start> -to <end> -c copy -avoid_negative_ts make_zero`
  per segment, then concat demuxer with `-c copy`. This is a remux, not a re-encode —
  no quality loss, runs at disk speed. But segments must begin on keyframes, so cut
  points drift by up to a GOP (typically 2–5s on Blu-ray rips).
- **Concat homogeneity:** the concat demuxer requires matching codec, resolution, pixel
  format, and audio stream layout across all segments. This holds within a season's rip
  but frequently breaks across all 8 seasons. Probe everything with `ffprobe` up front;
  either normalize outliers or group output per season.
- **Hybrid (the version worth publishing):** re-encode only the fragment from the
  desired start to the next keyframe, stream-copy the remainder, then concat.
  Frame-accurate boundaries while re-encoding ~1% of footage. Get keyframe timestamps
  with:
  ```bash
  ffprobe -skip_frame nokey -show_entries frame=pkt_pts_time -select_streams v -of csv <file>
  ```
  This is the thing neither VLC nor mpv can do — it's the differentiator.
- **Full re-encode:** available but discouraged. Long, lossy, rarely necessary.

**Disk planning:** output will be roughly 18–35 GB at typical 1080p rip bitrates, plus
intermediate segments before concat. Preflight must estimate this and check free space
before writing a byte.

## Scene list format

The dataset is the input, but the durable artifact is a human-editable scene list that
can be hand-corrected and versioned:

```yaml
version: "1.0"
series: "Game of Thrones"
character: "Daenerys Targaryen"
scenes:
  - episode: S01E01
    start: "00:14:32"
    end: "00:19:05"
    label: "Viserys presents Dany to Drogo"
    tags: [essos, viserys]
```

Tags matter more than they look — they let one file generate several cuts: everything,
Essos-only, drop the Meereen political scenes, Dany-and-Tyrion-only. The dataset's
`location` / `subLocation` / `flashback` / `warg` fields give us tags for free; generate
them, don't invent a vocabulary.

Version the scene list with a changelog. It lets someone say "I'm using
dany-scenes v1.2" and have that mean something, and it invites contributions —
someone else adds `tyrion-scenes.yaml` against the same tool and it becomes a format
rather than a one-off.

## Preflight (required, not optional)

Before generating anything, the tool must:

1. Scan the media directory and match files to episodes
2. Report coverage explicitly: `Found 71 of 73 episodes — missing S05E07, S06E02`
3. Verify calibration offsets exist (or compute them)
4. For `--format mkv`: probe codec/resolution/audio consistency and estimate output size

An EDL that silently skips segments is the worst failure mode, because the user
concludes the timestamps are wrong. Fail loudly and specifically instead.

**Filename matching** is the fiddliest real-world problem. Rips are named
`Game.of.Thrones.S03E04.1080p.WhateverGroup.mkv` and a hundred variants. Write a regex
that pulls season/episode from common patterns, and provide a manual override map as the
escape hatch. Don't try to be clever — try to be diagnosable.

## Environment

- Dual-boot machine: Windows 11 Home / Ubuntu 24.04. Build for both; Python 3 with
  stdlib only where possible (plus PyYAML) so it runs identically on each side.
- Media lives on NTFS data drives that **do not auto-mount at boot** on the Linux side.
  If the media directory looks empty, check whether it's mounted before concluding
  anything else. Preflight should distinguish "directory not found" from "directory
  empty" from "not mounted."
- Both NTFS data drives are ~97% full. The Ubuntu root partition has the headroom, so
  scratch space and export output should default there, and the size estimator matters.
- ffmpeg/ffprobe are hard dependencies for calibration and export; mpv is needed to test
  EDL output. Check for them in preflight and give a clear install hint if missing.

## Build order

1. **Calibration diagnostic.** Fetch/vendor the dataset, read episode lengths, probe
   local files, print a delta table. Nothing else. This is the go/no-go milestone and
   should be satisfyingly small.
2. **Scene list generation.** Filter `episodes.json` by character, emit tagged YAML with
   calibrated timestamps.
3. **mpv EDL emitter.** Verify by actually launching mpv and watching a few cuts.
4. **VLC M3U emitter.** Trivial once the EDL works.
5. **Preflight hardening.** Coverage reporting, missing files, dependency checks.
6. **ffmpeg exporter.** Stream-copy first, then the hybrid keyframe-accurate version.

## Target CLI

```
edl-cut --character "Daenerys Targaryen" --media ~/media/got --format edl
edl-cut --character "Daenerys Targaryen" --media ~/media/got --format m3u
edl-cut --character "Daenerys Targaryen" --media ~/media/got --format mkv --out dany.mkv
edl-cut --calibrate --media ~/media/got
edl-cut --character "Daenerys Targaryen" --tags essos --exclude-tags flashback
```

## Framing

Publish as "an EDL toolkit for character-focused rewatches, demonstrated with
Daenerys" — the tool is the artifact, the scene list is the example. That's both more
accurate and a better pitch than a single-purpose Daenerys script, and it makes the
generalization to other characters and other shows obvious to anyone who lands on the
repo.

## Start here

Begin with milestone 1 only. Fetch the dataset, get the episode lengths out of it, probe
my local files, and show me the delta table. Don't build output formats yet — I want to
see whether the timestamps align before we build on top of them.
