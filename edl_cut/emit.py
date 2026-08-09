"""Turning a scene list plus a calibration into something a player can open.

This is where dataset time becomes local time. Every emitter takes segments in
dataset time and an offset table, and applies `local = dataset + offset` at the
last possible moment. Nothing upstream of here knows about the user's library,
and nothing written here is publishable as a scene list.
"""

from __future__ import annotations

from pathlib import Path

from .scenelist import Segment


class Unresolvable(RuntimeError):
    pass


def resolve(segments: list[Segment], media: dict[tuple[int, int], Path],
            offsets: dict[str, float]) -> tuple[list[tuple[Segment, Path, float, float]],
                                                list[str]]:
    """Map segments onto real files and local timestamps.

    Returns (resolved, skipped). Skipped segments are reported rather than
    silently dropped: an EDL missing a quarter of its scenes still plays, and
    the user concludes the timestamps are wrong instead of that four episodes
    were absent.
    """
    resolved = []
    skipped = []
    for segment in segments:
        season = int(segment.episode[1:3])
        number = int(segment.episode[4:6])
        path = media.get((season, number))
        if path is None:
            skipped.append(f"{segment.episode}: no media file")
            continue
        if segment.episode not in offsets:
            skipped.append(f"{segment.episode}: no calibration offset")
            continue
        offset = offsets[segment.episode]
        start = max(0.0, segment.start + offset)
        end = segment.end + offset
        if end <= start:
            skipped.append(f"{segment.episode}: segment inverted after offset")
            continue
        resolved.append((segment, path, start, end))
    return resolved, skipped


def _edl_path(path: Path) -> str:
    """mpv's EDL path field, quoted when the path needs it.

    The bare `path,start,length` form breaks on any path containing a comma,
    which is common in release directory names. mpv's length-prefixed form
    `%<bytes>%<path>` sidesteps quoting entirely, so it is used whenever the
    path is not plainly safe.
    """
    text = str(path)
    if "," in text or "%" in text or "\n" in text:
        return f"%{len(text.encode('utf-8'))}%{text}"
    return text


def to_mpv_edl(resolved: list[tuple[Segment, Path, float, float]]) -> str:
    """mpv EDL v0.

    The field order is `path,start,LENGTH` — a length, not an end time. Getting
    that wrong produces a playlist that looks plausible and plays the wrong
    footage, so it is worth being explicit about.

    Paths are absolute: mpv resolves relative paths against the EDL file's own
    directory, which surprises people who move the file.
    """
    lines = ["# mpv EDL v0"]
    for segment, path, start, end in resolved:
        lines.append(f"{_edl_path(path.resolve())},{start:.3f},{end - start:.3f}")
    return "\n".join(lines) + "\n"


def to_m3u(resolved: list[tuple[Segment, Path, float, float]]) -> str:
    """VLC-flavoured M3U.

    Technically the weakest output: VLC closes and reopens the file at every
    segment, so transitions visibly hitch, and its handling of per-item
    start/stop options has historically been unreliable beyond the first entry.
    It earns its place by being the format people can already open.
    """
    lines = ["#EXTM3U"]
    for segment, path, start, end in resolved:
        lines.append(f"#EXTINF:{int(end - start)},{segment.episode} - {segment.label}")
        lines.append(f"#EXTVLCOPT:start-time={start:.3f}")
        lines.append(f"#EXTVLCOPT:stop-time={end:.3f}")
        lines.append(path.resolve().as_uri())
    return "\n".join(lines) + "\n"


def to_ffmpeg_concat(resolved: list[tuple[Segment, Path, float, float]]) -> str:
    """ffmpeg concat demuxer script, for the export path.

    Stream-copying from this will snap cut points to the nearest preceding
    keyframe — up to a GOP early, typically 2–5s on these rips. Frame-accurate
    export needs the hybrid re-encode of the leading fragment, which is a later
    milestone; this is the honest stream-copy version.
    """
    lines = ["# ffmpeg concat demuxer script", "# ffmpeg -f concat -safe 0 -i this.txt -c copy out.mkv"]
    for segment, path, start, end in resolved:
        escaped = str(path.resolve()).replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
        lines.append(f"inpoint {start:.3f}")
        lines.append(f"outpoint {end:.3f}")
    return "\n".join(lines) + "\n"


EMITTERS = {
    "edl": (to_mpv_edl, ".edl"),
    "m3u": (to_m3u, ".m3u"),
    "concat": (to_ffmpeg_concat, ".txt"),
}
