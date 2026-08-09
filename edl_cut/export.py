"""Exporting a cut to a real video file.

The EDL is the good output — no processing, no disk cost, regenerate in a
second. This module exists for everything that cannot open an EDL: tablets,
Plex, televisions.

Three modes, in increasing cost:

`copy`
    Stream copy every segment, then concat. A remux, not a re-encode: no quality
    loss, runs at disk speed. The catch is that a segment can only *begin* on a
    keyframe, so every cut point snaps backward to the preceding one — up to a
    full GOP early, typically 2–5s on Blu-ray rips. You get a few seconds of the
    preceding scene before each of your cuts.

`precise` (the one worth having)
    Re-encode only the fragment from the wanted start to the next keyframe, then
    stream-copy the rest of the segment, then concat. Frame-accurate *starts*
    while re-encoding on the order of 1% of the footage. This is the thing an
    EDL player cannot do.

    Segment ends are packet-accurate rather than frame-accurate: the copied tail
    can only stop on a packet boundary, which on HEVC tends to run a second or
    two long. Starts are what matter for a character cut — a scene beginning
    mid-sentence is jarring in a way that a second of overrun at the end is not.

`reencode`
    Re-encode everything. Slow and lossy; offered only because a sufficiently
    inconsistent library leaves nothing else.

The concat demuxer requires matching codec, resolution, pixel format and audio
layout across every piece. That holds within one season's rip and frequently
fails across a whole series, so preflight probes everything up front rather than
discovering it midway through writing 15GB.

Pieces are cut to **MPEG-TS**, not to the output container, and joined from
there. This is not an arbitrary choice. Written to Matroska, a piece keeps the
source's timestamps instead of being rebased to zero — a 2.78s fragment taken
from 30 minutes in reported 12.31s, with the right 67 frames but a start time of
9.5s. Joining then trusts those labels and every later piece slides, which is
how a 1183s cut came out 63s long.

Nothing about the picture data was wrong, only its labelling, and no combination
of seek flags fixed it reliably: it depended on the codec and on how far back
the preceding keyframe sat. MPEG-TS is the broadcast format, built for streams
being spliced with mismatched clocks, and joining TS pieces produces correct
timestamps regardless of what each piece thought its own were. Verified on the
case that defeated every flag: 424 frames out for 66 + 358 in.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .scenelist import Segment

# A wanted start this close to a keyframe is treated as landing on it. Below
# roughly a frame there is nothing to re-encode anyway.
KEYFRAME_EPSILON = 0.04

# Shortest head fragment worth re-encoding on its own. Sub-second encodes come
# out with unrebased timestamps on some encodes — a 0.40s fragment was written
# with the right 10 frames but a start time of 9.488s, which would have shifted
# everything after it in the concatenated result. Where the head would be
# shorter than this, the split moves to the *following* keyframe so the
# fragment is a comfortable size. That costs about one extra GOP of encoding
# and keeps the start frame-accurate.
MIN_ENCODE_FRAGMENT = 1.0

# How far a written piece may differ from its planned length before the export
# is abandoned.
#
# Cutting to MPEG-TS lands well inside a second — measured 2.816s for a 2.78s
# encode and 20.101s for a 20s copy — so the bar can sit far below the failures
# worth catching, which overshot by a GOP (+5s) and by a whole pre-roll (+30s).
ENCODE_TOLERANCE = 1.0
COPY_TOLERANCE = 1.0

# Fraction of the estimated output size kept free as headroom for the
# intermediate segments, which exist alongside the final file until concat ends.
HEADROOM = 1.35


@dataclass(frozen=True)
class StreamInfo:
    codec: str
    width: int
    height: int
    pix_fmt: str
    audio_codec: str
    sample_rate: str
    channels: int
    bitrate: float | None

    @property
    def concat_key(self) -> tuple:
        """What must match across segments for the concat demuxer to work."""
        return (self.codec, self.width, self.height, self.pix_fmt,
                self.audio_codec, self.sample_rate, self.channels)

    def describe(self) -> str:
        return (f"{self.codec} {self.width}x{self.height} {self.pix_fmt} / "
                f"{self.audio_codec} {self.sample_rate}Hz {self.channels}ch")


def probe_streams(path: Path) -> StreamInfo | None:
    """Video and audio parameters, as JSON.

    Deliberately JSON rather than ffprobe's flat key=value output: that output
    emits fields in the container's own order, not the order they were
    requested, so `codec_name` arrives *before* the `codec_type` that says which
    stream it belongs to. Parsing it line-by-line files the video codec under
    audio and produces an export that tries to encode video with an audio
    encoder.
    """
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        return None
    try:
        parsed = json.loads(result.stdout)
    except ValueError:
        return None

    streams = parsed.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    if video is None:
        return None
    try:
        bitrate = float(parsed.get("format", {}).get("bit_rate"))
    except (TypeError, ValueError):
        bitrate = None
    return StreamInfo(
        codec=video.get("codec_name", "?"),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        pix_fmt=video.get("pix_fmt", "?"),
        audio_codec=audio.get("codec_name", "none"),
        sample_rate=str(audio.get("sample_rate", "0")),
        channels=int(audio.get("channels") or 0),
        bitrate=bitrate,
    )


def _duration(path: Path) -> float | None:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def keyframe_times(path: Path) -> list[float]:
    """Every video keyframe timestamp, ascending.

    This is the expensive probe — it walks the whole file — so callers should do
    it once per file and cache, never once per segment.
    """
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-skip_frame", "nokey",
         "-select_streams", "v:0", "-show_entries", "frame=pts_time",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=3600,
    )
    times = []
    for line in result.stdout.splitlines():
        value = line.strip().rstrip(",")
        if not value:
            continue
        try:
            times.append(float(value))
        except ValueError:
            continue
    times.sort()
    return times


@dataclass
class Piece:
    """One ffmpeg invocation's worth of output."""
    source: Path
    start: float
    end: float
    reencode: bool
    # Set when this piece comes from a file whose stream parameters differ from
    # the rest of the library and must be conformed for concat to accept it.
    scale: tuple[int, int] | None = None
    pix_fmt: str | None = None
    # Keyframe at or before `start`, used as the input-side seek target. Seeking
    # the input to an exact keyframe and covering the remainder with an accurate
    # output-side seek is both cheaper and more reliable than a blind pre-roll.
    anchor: float | None = None

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Plan:
    pieces: list[Piece] = field(default_factory=list)
    drift: list[tuple[str, float]] = field(default_factory=list)
    reencoded_seconds: float = 0.0
    total_seconds: float = 0.0
    normalised: set = field(default_factory=set)

    @property
    def reencoded_fraction(self) -> float:
        return self.reencoded_seconds / self.total_seconds if self.total_seconds else 0.0


def _anchor_before(keyframes: list[float], position: float) -> float:
    """The keyframe strictly before `position`, or 0.0.

    Strictly before, so that every piece has a non-zero output-side seek to
    perform. With the input seek landing exactly on the target and no output
    seek at all, ffmpeg falls back to bounding the piece from the keyframe it
    landed on and writes a full GOP too much.
    """
    earlier = [k for k in keyframes if k < position - KEYFRAME_EPSILON]
    return earlier[-1] if earlier else 0.0


def plan_segment(segment: Segment, path: Path, start: float, end: float,
                 keyframes: list[float], mode: str) -> tuple[list[Piece], float]:
    """Split one segment into pieces. Returns (pieces, drift_seconds).

    `drift` is how far the actual cut point ends up from the wanted one — zero
    for `precise` and `reencode`, up to a GOP for `copy`.
    """
    if mode == "reencode":
        return [Piece(path, start, end, True)], 0.0

    # First keyframe at or after the wanted start.
    following = next((k for k in keyframes if k >= start - KEYFRAME_EPSILON), None)
    preceding = next((k for k in reversed(keyframes) if k <= start + KEYFRAME_EPSILON),
                     None)

    if mode == "copy":
        # Stream copy can only begin on a keyframe, and seeking forward would
        # lose footage the user asked for, so it snaps backward.
        cut = preceding if preceding is not None else (following or start)
        return ([Piece(path, cut, end, False,
                       anchor=_anchor_before(keyframes, cut))],
                start - cut)

    # precise: re-encode the head fragment up to a keyframe, copy the rest.
    if following is None or following >= end:
        # No keyframe inside the segment at all — the whole thing must be encoded.
        return ([Piece(path, start, end, True,
                       anchor=_anchor_before(keyframes, start))], 0.0)
    if following - start <= KEYFRAME_EPSILON:
        return ([Piece(path, following, end, False,
                       anchor=_anchor_before(keyframes, following))], 0.0)

    # Too short a head fragment is unreliable, so push the split out one
    # keyframe when there is room for it.
    if following - start < MIN_ENCODE_FRAGMENT:
        later = next((k for k in keyframes if k > following + KEYFRAME_EPSILON), None)
        if later is not None and later < end:
            following = later
        else:
            # Nowhere to move it to: encode the whole segment rather than emit a
            # fragment we cannot trust.
            return ([Piece(path, start, end, True,
                           anchor=_anchor_before(keyframes, start))], 0.0)
    return (
        [Piece(path, start, following, True,
               anchor=_anchor_before(keyframes, start)),
         Piece(path, following, end, False,
               anchor=_anchor_before(keyframes, following))],
        0.0,
    )


def build_plan(resolved: list[tuple[Segment, Path, float, float]], mode: str,
               progress=None, normalise: set[Path] | None = None,
               target: StreamInfo | None = None) -> Plan:
    """Turn resolved segments into ffmpeg-sized pieces.

    `normalise` names files whose stream parameters differ from the rest of the
    library. Their segments are re-encoded in full and conformed to `target`,
    rather than aborting the entire export — on a real 73-episode library a
    single file being 1888 pixels wide instead of 1920 is enough to break the
    concat demuxer, and re-encoding that one episode is a far better answer than
    re-encoding all of them.
    """
    normalise = normalise or set()
    plan = Plan()
    cache: dict[Path, list[float]] = {}
    for index, (segment, path, start, end) in enumerate(resolved):
        if path in normalise and target is not None:
            if path not in cache:
                cache[path] = keyframe_times(path)
            plan.pieces.append(Piece(path, start, end, True,
                                     scale=(target.width, target.height),
                                     pix_fmt=target.pix_fmt,
                                     anchor=_anchor_before(cache[path], start)))
            plan.total_seconds += end - start
            plan.reencoded_seconds += end - start
            plan.normalised.add(path.name)
            continue
        if mode != "reencode":
            if path not in cache:
                if progress:
                    progress(f"  indexing keyframes: {path.name}")
                cache[path] = keyframe_times(path)
            keyframes = cache[path]
        else:
            keyframes = []
        pieces, drift = plan_segment(segment, path, start, end, keyframes, mode)
        plan.pieces.extend(pieces)
        plan.total_seconds += end - start
        plan.reencoded_seconds += sum(p.duration for p in pieces if p.reencode)
        if abs(drift) > KEYFRAME_EPSILON:
            plan.drift.append((segment.episode, drift))
    return plan


@dataclass
class Preflight:
    ok: bool
    messages: list[str]
    estimated_bytes: int
    free_bytes: int
    groups: dict[tuple, list[str]]
    # Files whose parameters differ from the majority, and the majority's own
    # parameters to conform them to.
    outliers: set = field(default_factory=set)
    target: StreamInfo | None = None


def preflight(resolved: list[tuple[Segment, Path, float, float]],
              out: Path, mode: str) -> Preflight:
    """Check everything that could fail before writing a single byte."""
    messages: list[str] = []
    ok = True

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            messages.append(f"{tool} not found on PATH.")
            ok = False
    if not ok:
        return Preflight(False, messages, 0, 0, {})

    # Concat homogeneity. Grouped rather than merely counted, so the report can
    # say *which* files are the odd ones out.
    groups: dict[tuple, list[str]] = {}
    by_key: dict[tuple, StreamInfo] = {}
    paths_by_key: dict[tuple, list[Path]] = {}
    bitrates = []
    for path in dict.fromkeys(p for _, p, _, _ in resolved):
        info = probe_streams(path)
        if info is None:
            messages.append(f"could not probe {path.name}")
            ok = False
            continue
        groups.setdefault(info.concat_key, []).append(path.name)
        paths_by_key.setdefault(info.concat_key, []).append(path)
        by_key[info.concat_key] = info
        if info.bitrate:
            bitrates.append(info.bitrate)

    outliers: set = set()
    target: StreamInfo | None = None
    if groups:
        dominant = max(groups, key=lambda k: len(groups[k]))
        target = by_key[dominant]
    if len(groups) > 1:
        for key, paths in paths_by_key.items():
            if key != dominant:
                outliers.update(paths)
        messages.append(
            f"{len(groups)} incompatible stream configurations. The concat demuxer "
            "needs matching codec, resolution, pixel format and audio layout."
        )
        for key, names in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            marker = "  (majority)" if key == dominant else "  <- will be conformed"
            messages.append(
                f"    {len(names):3d} files: {'/'.join(str(k) for k in key)}{marker}")
            if key != dominant and len(names) <= 5:
                messages.append(f"         {', '.join(sorted(names))}")
        if mode == "copy":
            messages.append(
                "    --mode copy cannot conform them. Use --mode precise, which "
                "re-encodes only these files, or export per season."
            )
            ok = False
        else:
            seconds_odd = sum(e - s for _, p, s, e in resolved if p in outliers)
            messages.append(
                f"    {len(outliers)} file(s) will be re-encoded in full to match "
                f"the majority ({seconds_odd / 60:.1f} minutes of footage)."
            )

    seconds = sum(end - start for _, _, start, end in resolved)
    rate = (sum(bitrates) / len(bitrates)) if bitrates else 8_000_000.0
    estimated = int(seconds * rate / 8)

    # Named distinctly from `target` above, which holds the stream format that
    # outliers get conformed to — reusing the name silently overwrote it.
    destination = out.parent if out.parent.exists() else Path.cwd()
    free = shutil.disk_usage(destination).free
    needed = int(estimated * HEADROOM)
    messages.append(
        f"{len(resolved)} segments, {seconds / 3600:.2f} hours, "
        f"~{estimated / 1e9:.1f} GB estimated output"
    )
    messages.append(
        f"{free / 1e9:.1f} GB free at {destination} "
        f"(need ~{needed / 1e9:.1f} GB including intermediates)"
    )
    if free < needed:
        messages.append("NOT ENOUGH FREE SPACE. Choose a different --out location.")
        ok = False

    return Preflight(ok, messages, estimated, free, groups, outliers, target)


def _encode_args(info: StreamInfo | None) -> list[str]:
    """Encoder settings chosen to match the source, so concat accepts the result.

    A re-encoded fragment that differs in codec, pixel format or audio layout
    will be rejected by the concat demuxer — or worse, accepted and played with
    a visible seam.
    """
    if info is None:
        return ["-c:v", "libx264", "-crf", "18", "-preset", "medium",
                "-c:a", "aac", "-b:a", "192k"]
    encoder = {"hevc": "libx265", "h264": "libx264", "vp9": "libvpx-vp9"}.get(
        info.codec, "libx264")
    args = ["-c:v", encoder, "-crf", "18", "-preset", "medium",
            "-pix_fmt", info.pix_fmt]
    if info.audio_codec and info.audio_codec != "none":
        args += ["-c:a", info.audio_codec, "-ar", info.sample_rate,
                 "-ac", str(info.channels)]
    else:
        args += ["-an"]
    return args


def seek_command(piece: Piece, args: list[str], part: Path) -> list[str]:
    """Build the ffmpeg invocation for one piece.

    The two kinds of piece need different seek strategies. Using one for both
    caused three separate wrong-length bugs, so the reasoning is recorded here
    rather than rediscovered.

    RE-ENCODE: input-side seek alone. ffmpeg decodes from the preceding keyframe
    and discards, so the cut is frame-accurate and output timestamps are rebased
    to zero. Adding an output-side seek breaks it — the same 70 frames were
    produced either way, but the two-stage version reported 6.197s for a 2.92s
    request because its timestamps kept the seek offset instead of being rebased.

    STREAM COPY: two-stage. Input-side alone bounds the piece from the keyframe
    ffmpeg landed on rather than the requested position, writing a full GOP too
    much — 12.020s for a 7.000s request. So the input side jumps to the keyframe
    strictly before the target and the output side covers the sub-GOP remainder.
    The keyframe must be *strictly* before: with no output-side seek left to
    perform, the input-only failure returns.
    """
    command = ["ffmpeg", "-v", "error", "-y"]
    if piece.reencode:
        command += ["-ss", f"{piece.start:.3f}", "-i", str(piece.source)]
    else:
        anchor = min(piece.anchor if piece.anchor is not None else piece.start,
                     piece.start)
        command += ["-ss", f"{anchor:.3f}", "-i", str(piece.source)]
        if piece.start - anchor > KEYFRAME_EPSILON:
            command += ["-ss", f"{piece.start - anchor:.3f}"]
    command += ["-t", f"{piece.duration:.3f}", *args, "-f", "mpegts", str(part)]
    return command


def run(plan: Plan, out: Path, workdir: Path, progress=None) -> Path:
    """Cut every piece, then concat. Returns the finished file."""
    workdir.mkdir(parents=True, exist_ok=True)
    info_cache: dict[Path, StreamInfo | None] = {}
    parts: list[Path] = []

    for index, piece in enumerate(plan.pieces):
        part = workdir / f"part{index:05d}.ts"
        if piece.reencode:
            if piece.source not in info_cache:
                info_cache[piece.source] = probe_streams(piece.source)
            args = _encode_args(info_cache[piece.source])
            # No setpts here. A TS piece legitimately starts at a non-zero PCR
            # and the join rebases it; forcing the filter is unnecessary and was
            # not part of the verified invocation.
            if piece.scale:
                width, height = piece.scale
                args += ["-vf", f"scale={width}:{height}:flags=lanczos"]
            if piece.pix_fmt:
                args += ["-pix_fmt", piece.pix_fmt]
        else:
            args = ["-c", "copy"]
        command = seek_command(piece, args, part)
        if progress:
            kind = "encode" if piece.reencode else "copy  "
            progress(f"  [{index + 1}/{len(plan.pieces)}] {kind} "
                     f"{piece.duration:6.1f}s from {piece.source.name}")
        result = subprocess.run(command, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0 or not part.exists():
            raise RuntimeError(
                f"ffmpeg failed on piece {index} of {piece.source.name}:\n"
                f"{result.stderr.strip()[:800]}"
            )

        # Verify what was actually written. Seek behaviour varies with container
        # and codec, and a piece that silently comes out the wrong length yields
        # a cut that plays perfectly while showing the wrong footage — the exact
        # failure this project cares most about catching.
        actual = _duration(part)
        tolerance = ENCODE_TOLERANCE if piece.reencode else COPY_TOLERANCE
        if actual is not None and abs(actual - piece.duration) > tolerance:
            raise RuntimeError(
                f"piece {index} from {piece.source.name} is {actual:.2f}s but "
                f"{piece.duration:.2f}s was requested "
                f"(start {piece.start:.2f}, anchor {piece.anchor}). "
                "Refusing to build a cut from it."
            )
        parts.append(part)

    listing = workdir / "concat.txt"
    listing.write_text(
        "\n".join(f"file '{str(p).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'"
                  for p in parts) + "\n",
        encoding="utf-8",
    )
    if progress:
        progress(f"  concatenating {len(parts)} pieces -> {out}")
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-fflags", "+genpts",
         "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c", "copy", str(out)],
        capture_output=True, text=True, timeout=7200,
    )
    if result.returncode != 0:
        raise RuntimeError(f"concat failed:\n{result.stderr.strip()[:800]}")
    return out
