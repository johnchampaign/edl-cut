"""Command line entry point. Milestone 1 exposes --calibrate only."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import align, cache, calibrate as calibration, cuts, emit, export, scenelist, subs
from . import calibration as twosignal
from .dataset import DATA_DIR, load_episodes, resolve_character
from .media import DependencyMissing, probe_durations, require_tool, scan

ROOT_HINTS = {
    "missing": "The media directory does not exist. Check the path.",
    "empty": "The media directory exists but contains nothing.",
    "not-mounted": (
        "The media directory looks empty and is not a mount point. If your media "
        "lives on a separate drive, check whether it is actually mounted — "
        "external and secondary drives often do not mount automatically at boot, "
        "and an unmounted volume looks exactly like an empty directory."
    ),
}


def _report_coverage(scan_result, episodes) -> int:
    expected = {(e.season, e.number) for e in episodes}
    found = set(scan_result.matched)
    missing = sorted(expected - found)
    extra = sorted(found - expected)

    print(f"Found {len(found & expected)} of {len(expected)} episodes.")
    if missing:
        codes = ", ".join(f"S{s:02d}E{n:02d}" for s, n in missing)
        print(f"  MISSING ({len(missing)}): {codes}")
    if extra:
        codes = ", ".join(f"S{s:02d}E{n:02d}" for s, n in extra)
        print(f"  NOT IN DATASET ({len(extra)}): {codes}")
    if scan_result.unmatched:
        print(f"  UNREADABLE FILENAMES ({len(scan_result.unmatched)}):")
        for path in scan_result.unmatched[:10]:
            print(f"      {path.name}")
        if len(scan_result.unmatched) > 10:
            print(f"      ... and {len(scan_result.unmatched) - 10} more")
        print("      Use --overrides to map these manually.")
    if scan_result.duplicates:
        print(f"  DUPLICATES ({len(scan_result.duplicates)}):")
        for (season, number), paths in sorted(scan_result.duplicates.items()):
            print(f"      S{season:02d}E{number:02d}: {len(paths)} files, using {paths[0].name}")
    return len(missing)


def _interpret_alignments(alignments: list) -> list[str]:
    """Summarise the subtitle evidence, grouped by season.

    Grouping matters: a library assembled from more than one rip source can be
    internally consistent within a season and inconsistent across seasons, and a
    single library-wide number hides exactly that.
    """
    import statistics as stats

    notes = []
    agreed = [a for a in alignments if a.agrees]
    print_order = sorted({a.code[:3] for a in alignments})

    notes.append(
        f"{len(agreed)} of {len(alignments)} episodes have head and tail anchors that "
        "agree — those offsets are trustworthy."
    )
    for season in print_order:
        rows = [a for a in alignments if a.code.startswith(season)]
        ok = [a for a in rows if a.agrees]
        if ok:
            offsets = [a.offset for a in ok]
            median = stats.median(offsets)
            spread = max(offsets) - min(offsets)
            verdict = (
                "USE AS-IS, no offset needed" if abs(median) < 10 and spread < 20
                else f"apply a per-episode offset (median {median:+.0f}s, spread {spread:.0f}s)"
            )
            notes.append(f"  {season}: {len(ok)}/{len(rows)} agree, median {median:+7.1f}s  ->  {verdict}")
        else:
            notes.append(f"  {season}: 0/{len(rows)} agree  ->  NEEDS MANUAL INSPECTION")

    unresolved = [a for a in alignments if not a.agrees]
    if unresolved:
        notes.append(
            f"{len(unresolved)} episodes could not be resolved automatically. The usual "
            "cause is dialogue in a recap before the first scene, which drags the head "
            "anchor early. These need a manual offset."
        )
    return notes


def _estimate_offsets(episodes, matched, durations, verbose=True):
    """Read every subtitle track and resolve offsets from both signals.

    Detecting appended material matters here: if trimming the cues to the
    episode dropped any, the file holds something after the episode and its
    duration no longer measures the episode, so the duration predictor must be
    withheld for it.
    """
    inputs = []
    for episode in episodes:
        path = matched.get((episode.season, episode.number))
        if path is None or len(episode.scenes) < 9:
            continue
        cues, _ = subs.load_cues(path)
        trimmed = align.trim_to_episode(cues, episode.last_scene_end) if cues else []
        inputs.append(twosignal.Input(
            code=episode.code,
            boundaries=[s.start for s in episode.scenes] + [episode.scenes[-1].end],
            cues=trimmed,
            duration=durations.get((episode.season, episode.number)),
            last_scene_end=episode.last_scene_end,
            appended=bool(cues) and len(trimmed) < len(cues),
        ))
        if verbose and len(inputs) % 15 == 0:
            print(f"  read {len(inputs)} subtitle tracks...", flush=True)
    return twosignal.calibrate(inputs)


def _suggest(query: str, episodes) -> list[str]:
    """Offer near-misses when a character name does not match.

    Substring matching alone is not enough: the dataset spells her
    "Daenerys Targaryen", so a user typing the far more natural "Dany" gets
    nothing back. Fuzzy matching on the whole name and on each word of it
    catches nicknames, surnames alone, and ordinary typos.
    """
    import difflib

    names = sorted({c for e in episodes for s in e.scenes for c in s.characters})
    query = query.strip().lower()

    hits = [n for n in names if query in n.lower()]
    for name in names:
        if name in hits:
            continue
        parts = [name.lower()] + name.lower().split()
        if difflib.get_close_matches(query, parts, n=1, cutoff=0.7):
            hits.append(name)
    return hits[:5]


def _export(resolved, args) -> int:
    """The ffmpeg export path: preflight, plan, then write."""
    out = Path(args.out) if args.out else Path("cut.mkv")

    print()
    print("Preflight:")
    check = export.preflight(resolved, out, args.mode)
    for message in check.messages:
        print(f"  {message}")
    if not check.ok:
        print("\nAborted before writing anything.", file=sys.stderr)
        return 2

    print()
    print(f"Planning cut points ({args.mode})...", flush=True)
    plan = export.build_plan(resolved, args.mode, progress=print,
                             normalise=check.outliers, target=check.target)
    print(f"  {len(plan.pieces)} pieces, "
          f"{plan.reencoded_fraction * 100:.1f}% of footage re-encoded")
    if plan.normalised:
        print(f"  conforming {len(plan.normalised)} outlier file(s) to the "
              f"majority format: {', '.join(sorted(plan.normalised))}")
    if plan.drift:
        worst = max(abs(d) for _, d in plan.drift)
        print(f"  {len(plan.drift)} cut points snap back to a keyframe, "
              f"worst {worst:.1f}s early.")
        print("  Use --mode precise for frame-accurate boundaries.")

    if args.dry_run:
        print("\nDry run: nothing written.")
        return 0

    workdir = Path(args.workdir) if args.workdir else out.parent / ".edl-cut-work"
    print()
    print(f"Writing (work area: {workdir})...", flush=True)
    try:
        export.run(plan, out, workdir, progress=print)
    except RuntimeError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 2
    size = out.stat().st_size
    print(f"\nWrote {out} ({size / 1e9:.2f} GB)")
    print(f"Intermediate pieces are still in {workdir} — delete when satisfied.")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    episodes = load_episodes(Path(args.data))
    root = Path(args.media).expanduser()
    result = scan(root)
    if result.root_state != "ok":
        print(f"error: {ROOT_HINTS[result.root_state]}", file=sys.stderr)
        return 2

    character, suggestions = resolve_character(args.character, episodes, Path(args.data))
    if character is None:
        print(f"error: no character matching {args.character!r}", file=sys.stderr)
        for name in suggestions:
            print(f"       did you mean: {name}", file=sys.stderr)
        if not suggestions:
            print("       try --list-characters", file=sys.stderr)
        return 2
    if character != args.character:
        print(f"Resolved {args.character!r} -> {character}")

    segments = scenelist.build(
        episodes, character,
        merge_gap=args.merge_gap,
        pad_pre=args.pad if args.pad is not None else args.pad_pre,
        pad_post=args.pad if args.pad is not None else args.pad_post,
    )
    segments = scenelist.filter_by_tags(segments, args.tags, args.exclude_tags)
    if args.preview:
        segments = scenelist.preview(segments, args.preview,
                                     args.preview_seconds, args.preview_at)
        if args.preview_seconds:
            print(f"Spot-check: {len(segments)} openings of "
                  f"{args.preview_seconds:.0f}s, spread across the series. "
                  "This checks timestamps; it is not meant to watch well.")
        else:
            print(f"Sample: {len(segments)} complete consecutive scenes, "
                  "starting partway into the cut.")
    total = sum(s.duration for s in segments)
    print(f"{character}: {len(segments)} segments, {total / 3600:.2f} hours "
          f"across {len({s.episode for s in segments})} episodes.")

    if args.scene_list:
        text = scenelist.to_yaml(segments, "Game of Thrones", character)
        Path(args.scene_list).write_text(text, encoding="utf-8")
        print(f"Wrote scene list (dataset time, publishable): {args.scene_list}")

    offsets = cache.offsets(root)
    if not offsets:
        print("error: no calibration cache for this library. Run --calibrate first.",
              file=sys.stderr)
        return 2

    resolved, skipped = emit.resolve(segments, result.matched, offsets)

    if resolved and args.snap_to_cut:
        print("Snapping starts to real visual cuts (decodes a little video; "
              "cached per library)...", flush=True)
        snap_cache = cuts.SnapCache(cache.CACHE_DIR / "cuts.json")
        resolved, moved, trimmed = cuts.snap_all(resolved, snap_cache, progress=print)
        print(f"  moved {moved} of {len(resolved)} starts forward, "
              f"trimming {trimmed:.0f}s of preceding footage.")

    if resolved and not args.no_snap_dialogue:
        cache_cues: dict = {}

        def cues_for(path):
            if path not in cache_cues:
                found, _ = subs.load_cues(path)
                cache_cues[path] = sorted(found)
            return cache_cues[path]

        resolved, extended, added = emit.snap_ends_to_dialogue(resolved, cues_for)
        if extended:
            print(f"Extended {extended} segment ends by {added:.0f}s total so a "
                  "line of dialogue is not cut in half.")
    if skipped:
        print(f"SKIPPED {len(skipped)} segments:")
        for reason in sorted(set(skipped))[:12]:
            print(f"    {reason}")
    if not resolved:
        print("error: nothing resolvable.", file=sys.stderr)
        return 2

    if args.format == "mkv":
        return _export(resolved, args)

    render, suffix = emit.EMITTERS[args.format]
    out = Path(args.out) if args.out else Path(
        character.lower().replace(" ", "-") + suffix)
    out.write_text(render(resolved), encoding="utf-8")
    kept = sum(e - s for _, _, s, e in resolved)
    print(f"Wrote {out}  ({len(resolved)} segments, {kept / 3600:.2f} hours)")
    if args.format == "edl":
        # mpv seeks to keyframes by default, and keyframes in these rips sit
        # 1-8s apart, so a segment can begin several seconds before its cut
        # point. Exact seeking costs a short delay per segment and removes it.
        print(f"Play with exact cuts:  mpv --hr-seek=yes {out}")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    try:
        require_tool("ffprobe", "sudo apt install ffmpeg  (or: winget install ffmpeg)")
    except DependencyMissing as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    episodes = load_episodes(Path(args.data))
    print(f"Dataset: {len(episodes)} episodes, "
          f"{sum(len(e.scenes) for e in episodes)} scenes.\n")

    overrides = {}
    if args.overrides:
        with open(args.overrides, encoding="utf-8") as handle:
            overrides = json.load(handle)

    root = Path(args.media).expanduser()
    result = scan(root, overrides)
    if result.root_state != "ok":
        print(f"error: {ROOT_HINTS[result.root_state]}", file=sys.stderr)
        print(f"       path: {root}", file=sys.stderr)
        return 2

    missing_count = _report_coverage(result, episodes)
    print()

    print(f"Probing {len(result.matched)} files with ffprobe...", flush=True)
    durations = probe_durations(result.matched)
    failed = [k for k, v in durations.items() if v is None]
    if failed:
        codes = ", ".join(f"S{s:02d}E{n:02d}" for s, n in sorted(failed))
        print(f"  ffprobe could not read {len(failed)}: {codes}")
    print()

    rows = calibration.build_rows(episodes, durations)
    print(calibration.render_table(rows))
    print()
    for note in calibration.interpret(rows):
        print(note)

    if not args.no_subs:
        print()
        print("Subtitle cross-check (ground truth for THIS library)...", flush=True)
        alignments = []
        for episode in episodes:
            key = (episode.season, episode.number)
            path = result.matched.get(key)
            if path is None or not episode.scenes:
                continue
            alignments.append(
                subs.align(
                    episode.code, path,
                    episode.scenes[0].start, episode.last_scene_end,
                    durations.get(key),
                )
            )
        print()
        print(subs.render_table(alignments))
        print()
        for note in _interpret_alignments(alignments):
            print(note)

    print()
    print("Estimating per-episode offsets (duration prior + subtitle scoring)...",
          flush=True)
    estimates, credits = _estimate_offsets(episodes, result.matched, durations)
    print(f"  learned trailing/credits length for this library: {credits:.1f}s")
    print()
    print(twosignal.render_table(estimates))
    print()

    existing = cache.load(root)
    entries = {}
    kept_manual = 0
    for code, est in estimates.items():
        if existing.get(code, {}).get("manual"):
            entries[code] = existing[code]
            kept_manual += 1
            continue
        entries[code] = {
            "offset": est.offset if est.offset is not None else 0.0,
            "source": est.source,
            "margin": round(est.margin, 3),
            "confident": est.confident,
        }

    target = cache.save(root, entries)
    good = sum(1 for e in entries.values() if e.get("confident") or e.get("manual"))
    print(f"  {good}/{len(entries)} episodes calibrated with confidence.")
    if kept_manual:
        print(f"  {kept_manual} manual overrides preserved.")
    unresolved = [c for c, e in entries.items()
                  if not (e.get("confident") or e.get("manual"))]
    if unresolved:
        print(f"  NEEDS A MANUAL OFFSET: {', '.join(sorted(unresolved))}")
        print(f"  Edit them in {target} and set \"manual\": true.")
    print(f"Wrote {target}")
    return 1 if missing_count else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="edl-cut",
        description="EDL toolkit for character-focused rewatches. "
                    "Ships timestamps and code; never video.",
    )
    parser.add_argument("--calibrate", action="store_true",
                        help="compare dataset runtimes against local files")
    parser.add_argument("--media", help="root directory of your media library")
    parser.add_argument("--data", default=str(DATA_DIR),
                        help="directory holding the vendored dataset")
    parser.add_argument("--no-subs", action="store_true",
                        help="skip the subtitle cross-check (faster, less reliable)")
    parser.add_argument("--overrides",
                        help="JSON file mapping filenames to episode codes")
    parser.add_argument("--character", help="character whose scenes to collect")
    parser.add_argument("--format", choices=sorted(emit.EMITTERS) + ["mkv"],
                        default="edl",
                        help="output format (default: edl, for mpv)")
    parser.add_argument("--mode", choices=("precise", "copy", "reencode"),
                        default="precise",
                        help="mkv export strategy (default: precise — "
                             "frame-accurate, re-encodes ~1%% of footage)")
    parser.add_argument("--workdir", help="scratch area for intermediate pieces")
    parser.add_argument("--dry-run", action="store_true",
                        help="preflight and plan only; write nothing")
    parser.add_argument("--out", help="output file")
    parser.add_argument("--scene-list", help="also write the scene list YAML here")
    parser.add_argument("--merge-gap", type=float, default=scenelist.DEFAULT_MERGE_GAP,
                        help="join segments separated by less than this many seconds")
    parser.add_argument("--snap-to-cut", action="store_true",
                        help="move each start forward to the next real visual "
                             "cut, removing the tail of the previous scene. "
                             "Decodes a little video per segment; cached.")
    parser.add_argument("--no-snap-dialogue", action="store_true",
                        help="do not extend segment ends to finish a line of "
                             "dialogue that runs across the cut")
    parser.add_argument("--pad", type=float, metavar="S",
                        help="shorthand: set both --pad-pre and --pad-post")
    parser.add_argument("--pad-pre", type=float, default=scenelist.DEFAULT_PAD_PRE,
                        metavar="S",
                        help="seconds before each scene (default 0 — anything "
                             "more shows the end of the previous scene)")
    parser.add_argument("--pad-post", type=float, default=scenelist.DEFAULT_PAD_POST,
                        metavar="S",
                        help="seconds after each scene (default 0.5, lets a "
                             "line finish)")
    parser.add_argument("--preview", type=int, metavar="N",
                        help="sample N complete consecutive scenes — a short "
                             "version of the real cut, for judging whether it "
                             "works as a story")
    parser.add_argument("--preview-seconds", type=float, metavar="S",
                        help="instead, take only the first S seconds of N "
                             "segments spread across the series: a calibration "
                             "spot-check, not something to watch for pleasure")
    parser.add_argument("--preview-at", type=float, default=0.35, metavar="F",
                        help="how far into the cut the sample starts, 0-1 "
                             "(default 0.35)")
    parser.add_argument("--tags", nargs="*", help="keep only segments with these tags")
    parser.add_argument("--exclude-tags", nargs="*", help="drop segments with these tags")
    args = parser.parse_args(argv)

    if args.calibrate:
        if not args.media:
            parser.error("--calibrate requires --media")
        return cmd_calibrate(args)
    if args.character:
        if not args.media:
            parser.error("--character requires --media")
        return cmd_generate(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
