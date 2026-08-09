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
    stream-copy the rest of the segment, then concat. Frame-accurate boundaries
    while re-encoding on the order of 1% of the footage. This is the thing an
    EDL player cannot do.

`reencode`
    Re-encode everything. Slow and lossy; offered only because a sufficiently
    inconsistent library leaves nothing else.

The concat demuxer requires matching codec, resolution, pixel format and audio
layout across every piece. That holds within one season's rip and frequently
fails across a whole series, so preflight probes everything up front rather than
discovering it midway through writing 15GB.
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

# How far before the target the fast input-side seek lands, leaving a short
# window for the accurate output-side seek to cross. Large enough to clear any
# GOP, small enough that demuxing it costs nothing.
PRE_ROLL = 30.0

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
        anchor = preceding if preceding is not None else (following or start)
        return [Piece(path, anchor, end, False)], start - anchor

    # precise: re-encode the head fragment up to the next keyframe, copy the rest.
    if following is None or following >= end:
        # No keyframe inside the segment at all — the whole thing must be encoded.
        return [Piece(path, start, end, True)], 0.0
    if following - start <= KEYFRAME_EPSILON:
        return [Piece(path, following, end, False)], 0.0
    return (
        [Piece(path, start, following, True), Piece(path, following, end, False)],
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
            plan.pieces.append(Piece(path, start, end, True,
                                     scale=(target.width, target.height),
                                     pix_fmt=target.pix_fmt))
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


def run(plan: Plan, out: Path, workdir: Path, progress=None) -> Path:
    """Cut every piece, then concat. Returns the finished file."""
    workdir.mkdir(parents=True, exist_ok=True)
    info_cache: dict[Path, StreamInfo | None] = {}
    parts: list[Path] = []

    for index, piece in enumerate(plan.pieces):
        part = workdir / f"part{index:05d}.mkv"
        if piece.reencode:
            if piece.source not in info_cache:
                info_cache[piece.source] = probe_streams(piece.source)
            args = _encode_args(info_cache[piece.source])
            if piece.scale:
                width, height = piece.scale
                args += ["-vf", f"scale={width}:{height}:flags=lanczos"]
            if piece.pix_fmt:
                args += ["-pix_fmt", piece.pix_fmt]
        else:
            args = ["-c", "copy"]
        # Two-stage seek. A fast input-side seek to `pre_roll` seconds before the
        # target, then an accurate output-side seek across that short window.
        #
        # Output-side `-ss` is the part that matters for correctness. Seeking
        # only on the input side and bounding with `-t` (or `-to`) produced
        # pieces exactly one GOP too long — measured, not theorised: for a
        # 7-second request ffmpeg wrote 12.02 seconds, because the length was
        # applied from the keyframe it landed on rather than from the requested
        # position. Output-side seek gave 7.04 seconds and timestamps based at
        # zero, which is also what the concat demuxer needs.
        #
        # The input-side seek is purely an optimisation: without it every piece
        # would demux from the start of the file, which for a segment an hour in
        # is very slow.
        pre_roll = min(piece.start, PRE_ROLL)
        command = [
            "ffmpeg", "-v", "error", "-y",
            "-ss", f"{piece.start - pre_roll:.3f}",
            "-i", str(piece.source),
            "-ss", f"{pre_roll:.3f}",
            "-t", f"{piece.duration:.3f}",
            *args,
            "-avoid_negative_ts", "make_zero",
            str(part),
        ]
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
        ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c", "copy", str(out)],
        capture_output=True, text=True, timeout=7200,
    )
    if result.returncode != 0:
        raise RuntimeError(f"concat failed:\n{result.stderr.strip()[:800]}")
    return out
