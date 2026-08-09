"""Test suite. Run with: python3 -m unittest discover -s tests -v

Tests are weighted toward the failure modes that are *silent* — the ones that
produce a playlist which opens fine and plays the wrong footage. A crash gets
noticed; a segment that is 40 seconds early does not, until someone is watching.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from edl_cut import align, calibration, emit, scenelist        # noqa: E402
from edl_cut.dataset import (format_timestamp, parse_timestamp,  # noqa: E402
                             resolve_character)
from edl_cut.media import parse_episode_code, scan              # noqa: E402
from tests import fixtures                                      # noqa: E402


class TestTimestamps(unittest.TestCase):
    def test_parses_dataset_format(self):
        self.assertEqual(parse_timestamp("0:14:32"), 872)
        self.assertEqual(parse_timestamp("1:00:57"), 3657)
        self.assertEqual(parse_timestamp("00:00:40"), 40)

    def test_tolerates_mm_ss(self):
        self.assertEqual(parse_timestamp("14:32"), 872)

    def test_roundtrip(self):
        for seconds in (0, 59, 872, 3657, 35999):
            self.assertEqual(parse_timestamp(format_timestamp(seconds)), seconds)

    def test_format_is_zero_padded(self):
        self.assertEqual(format_timestamp(872), "00:14:32")

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            parse_timestamp("banana")


class TestFilenameMatching(unittest.TestCase):
    def test_common_release_patterns(self):
        cases = {
            "Game.of.Thrones.S01E01.1080p.BluRay.x265.mkv": (1, 1),
            "Show s2.e10 720p.mkv": (2, 10),
            "Show 3x07 HDTV.mkv": (3, 7),
            "Show - Season 4 Episode 2.mkv": (4, 2),
            "SHOW.S08E06.PROPER.mkv": (8, 6),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(parse_episode_code(name), expected)

    def test_returns_none_when_unreadable(self):
        self.assertIsNone(parse_episode_code("some.movie.2019.1080p.mkv"))

    def test_does_not_mistake_resolution_for_episode(self):
        # '1080p' must not be read as season 10, episode 80 or similar.
        self.assertEqual(
            parse_episode_code("Show.S05E03.1080p.x264.mkv"), (5, 3))


class TestSceneList(unittest.TestCase):
    def _episode(self, scenes):
        return fixtures.make_episode(1, 1, scenes)

    def test_selects_only_scenes_with_the_character(self):
        ep = self._episode([(0, 60, ["A"]), (60, 120, ["B"]), (120, 180, ["A", "B"])])
        segs = scenelist.build([ep], "A", merge_gap=0, pad_pre=0, pad_post=0)
        self.assertEqual([(s.start, s.end) for s in segs], [(0, 60), (120, 180)])

    def test_merge_gap_coalesces_cross_cuts(self):
        # A present, 10s away from camera, present again: one segment, not two.
        ep = self._episode([(0, 60, ["A"]), (60, 70, ["B"]), (70, 130, ["A"])])
        segs = scenelist.build([ep], "A", merge_gap=30, pad_pre=0, pad_post=0)
        self.assertEqual(len(segs), 1)
        self.assertEqual((segs[0].start, segs[0].end), (0, 130))

    def test_merge_gap_respects_the_threshold(self):
        ep = self._episode([(0, 60, ["A"]), (60, 200, ["B"]), (200, 260, ["A"])])
        segs = scenelist.build([ep], "A", merge_gap=30, pad_pre=0, pad_post=0)
        self.assertEqual(len(segs), 2)

    def test_padding_applied_but_never_negative(self):
        ep = self._episode([(2, 60, ["A"])])
        segs = scenelist.build([ep], "A", merge_gap=0, pad_pre=10, pad_post=5)
        self.assertEqual(segs[0].start, 0.0)   # clamped, not -8
        self.assertEqual(segs[0].end, 65.0)

    def test_tags_come_from_the_dataset(self):
        ep = self._episode([(0, 60, ["A"])])
        segs = scenelist.build([ep], "A")
        self.assertIn("loc", segs[0].tags)
        self.assertIn("sub", segs[0].tags)

    def test_tag_filtering(self):
        ep = self._episode([(0, 60, ["A"])])
        segs = scenelist.build([ep], "A")
        self.assertEqual(len(scenelist.filter_by_tags(segs, ["sub"], None)), 1)
        self.assertEqual(len(scenelist.filter_by_tags(segs, ["nope"], None)), 0)
        self.assertEqual(len(scenelist.filter_by_tags(segs, None, ["sub"])), 0)

    def test_yaml_declares_dataset_time(self):
        ep = self._episode([(0, 60, ["A"])])
        text = scenelist.to_yaml(scenelist.build([ep], "A"), "Show", "A")
        self.assertIn("DATASET time", text)
        self.assertIn('character: "A"', text)

    def test_labels_are_descriptive_not_dialogue(self):
        ep = self._episode([(0, 60, ["A", "B"])])
        label = scenelist.build([ep], "A")[0].label
        self.assertIn("Sub", label)
        self.assertIn("B", label)


class TestEmitters(unittest.TestCase):
    def _resolved(self, start=100.0, end=160.0, name="S01E01.mkv"):
        seg = scenelist.Segment("S01E01", start, end, "label", ["t"])
        return [(seg, Path("/media") / name, start, end)]

    def test_edl_writes_length_not_end_time(self):
        # The documented footgun: field 3 is a LENGTH.
        text = emit.to_mpv_edl(self._resolved(100.0, 160.0))
        line = [l for l in text.splitlines() if not l.startswith("#")][0]
        self.assertTrue(line.endswith(",100.000,60.000"), line)

    def test_edl_has_the_required_header(self):
        self.assertTrue(emit.to_mpv_edl(self._resolved()).startswith("# mpv EDL v0"))

    def test_edl_quotes_paths_containing_commas(self):
        text = emit.to_mpv_edl(self._resolved(name="Show, The.mkv"))
        line = [l for l in text.splitlines() if not l.startswith("#")][0]
        self.assertTrue(line.startswith("%"), line)
        # The byte length prefix must match the path's real byte length.
        declared = int(line[1:line.index("%", 1)])
        path_text = str((Path("/media") / "Show, The.mkv").resolve())
        self.assertEqual(declared, len(path_text.encode("utf-8")))

    def test_edl_leaves_plain_paths_unquoted(self):
        line = [l for l in emit.to_mpv_edl(self._resolved()).splitlines()
                if not l.startswith("#")][0]
        self.assertFalse(line.startswith("%"))

    def test_m3u_uses_start_and_stop_times(self):
        text = emit.to_m3u(self._resolved(100.0, 160.0))
        self.assertIn("#EXTVLCOPT:start-time=100.000", text)
        self.assertIn("#EXTVLCOPT:stop-time=160.000", text)
        self.assertIn("file://", text)

    def test_concat_uses_inpoint_outpoint(self):
        text = emit.to_ffmpeg_concat(self._resolved(100.0, 160.0))
        self.assertIn("inpoint 100.000", text)
        self.assertIn("outpoint 160.000", text)

    def test_concat_escapes_single_quotes_in_paths(self):
        text = emit.to_ffmpeg_concat(self._resolved(name="Bob's Show.mkv"))
        self.assertIn(r"'\''", text)


class TestResolve(unittest.TestCase):
    def _seg(self, code="S01E01"):
        return scenelist.Segment(code, 10.0, 70.0, "l", [])

    def test_applies_the_offset(self):
        media = {(1, 1): Path("/m/a.mkv")}
        resolved, skipped = emit.resolve([self._seg()], media, {"S01E01": -5.0})
        self.assertEqual(skipped, [])
        self.assertEqual((resolved[0][2], resolved[0][3]), (5.0, 65.0))

    def test_reports_missing_media_rather_than_dropping_it(self):
        resolved, skipped = emit.resolve([self._seg()], {}, {"S01E01": 0.0})
        self.assertEqual(resolved, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("no media file", skipped[0])

    def test_reports_missing_calibration_rather_than_assuming_zero(self):
        media = {(1, 1): Path("/m/a.mkv")}
        resolved, skipped = emit.resolve([self._seg()], media, {})
        self.assertEqual(resolved, [])
        self.assertIn("no calibration offset", skipped[0])

    def test_clamps_a_start_that_would_go_negative(self):
        media = {(1, 1): Path("/m/a.mkv")}
        resolved, _ = emit.resolve([self._seg()], media, {"S01E01": -30.0})
        self.assertEqual(resolved[0][2], 0.0)     # clamped from -20
        self.assertEqual(resolved[0][3], 40.0)

    def test_drops_a_segment_that_falls_entirely_before_zero(self):
        media = {(1, 1): Path("/m/a.mkv")}
        resolved, skipped = emit.resolve([self._seg()], media, {"S01E01": -100.0})
        self.assertEqual(resolved, [])
        self.assertIn("inverted", skipped[0])


class TestAlign(unittest.TestCase):
    """The offset estimator, against synthetic dialogue with a known answer."""

    @staticmethod
    def _boundaries(count=30, start=400):
        """Irregularly spaced scene edges, as a real episode has.

        A perfectly periodic fixture is not merely unrealistic, it is degenerate:
        every offset that is a multiple of the period lands boundaries in exactly
        the same silences, so the estimator has no basis to prefer one. Irregular
        spacing is what makes the peak unique.
        """
        import random
        rng = random.Random(20260809)
        edges, t = [start], start
        for _ in range(count):
            t += rng.choice((37, 44, 58, 71, 83, 96, 112))
            edges.append(t)
        return edges

    def _cues(self, boundaries, offset, speak=6.0, gap=4.0):
        """Dialogue that fills each scene but stops before every boundary."""
        cues = []
        for a, b in zip(boundaries, boundaries[1:]):
            t = a + offset + 1.0
            while t + speak < b + offset - gap:
                if t >= 0:
                    cues.append((t, t + speak))
                t += speak + 1.0
        return cues

    def test_recovers_a_known_offset(self):
        boundaries = self._boundaries()
        est = align.estimate(boundaries, self._cues(boundaries, -120.0))
        self.assertIsNotNone(est.offset)
        self.assertAlmostEqual(est.offset, -120.0, delta=2.0)
        self.assertGreater(est.margin, 0.2)

    def test_recovers_zero_offset(self):
        boundaries = self._boundaries()
        est = align.estimate(boundaries, self._cues(boundaries, 0.0))
        self.assertAlmostEqual(est.offset, 0.0, delta=2.0)

    def test_refuses_when_there_are_too_few_boundaries(self):
        est = align.estimate([0, 60, 120], [(1.0, 5.0)])
        self.assertIsNone(est.offset)

    def test_refuses_with_no_cues(self):
        self.assertIsNone(align.estimate(self._boundaries(), []).offset)

    def test_recovers_a_large_negative_offset(self):
        # Seasons 5-7 of the reference library need corrections near -300s.
        boundaries = self._boundaries(start=900)
        est = align.estimate(boundaries, self._cues(boundaries, -300.0))
        self.assertAlmostEqual(est.offset, -300.0, delta=2.0)

    def test_does_not_prefer_offsets_past_the_subtitled_span(self):
        """Regression: an early version scored huge offsets highly because they
        pushed every boundary beyond the last cue, into a region that reads as
        silence only because nothing was ever timed there."""
        boundaries = self._boundaries()
        est = align.estimate(boundaries, self._cues(boundaries, -60.0))
        self.assertAlmostEqual(est.offset, -60.0, delta=2.0)

    def test_trim_drops_appended_material(self):
        cues = [(10.0, 12.0), (100.0, 102.0), (5000.0, 5002.0)]
        kept = align.trim_to_episode(cues, last_scene_end=200)
        self.assertEqual(len(kept), 2)

    def test_trim_never_returns_empty(self):
        cues = [(9000.0, 9002.0)]
        self.assertEqual(align.trim_to_episode(cues, last_scene_end=10), cues)


class TestCalibration(unittest.TestCase):
    @staticmethod
    def _edges(last_scene_end):
        import random
        rng = random.Random(11)
        edges, t = [120], 120
        while t < last_scene_end:
            t += rng.choice((41, 63, 77, 94, 108))
            edges.append(min(t, last_scene_end))
        return edges

    def _input(self, code, offset, last_scene_end=1700, credits=83.0, cues=True):
        boundaries = self._edges(last_scene_end)
        cue_list = []
        if cues:
            for a, b in zip(boundaries, boundaries[1:]):
                t = a + offset + 1.0
                while t + 6.0 < b + offset - 4.0:
                    if t >= 0:
                        cue_list.append((t, t + 6.0))
                    t += 7.0
        return calibration.Input(
            code=code, boundaries=boundaries, cues=cue_list,
            duration=last_scene_end + offset + credits,
            last_scene_end=last_scene_end,
        )

    def test_two_signals_agree_and_produce_confidence(self):
        items = [self._input(f"S01E{i:02d}", -60.0) for i in range(1, 6)]
        results, credits = calibration.calibrate(items)
        self.assertAlmostEqual(credits, 83.0, delta=6.0)
        for r in results.values():
            self.assertTrue(r.confident, r.note)
            self.assertAlmostEqual(r.offset, -60.0, delta=3.0)
            self.assertEqual(r.source, "duration+subtitles")

    def test_predictor_is_withheld_for_appended_content(self):
        item = self._input("S08E01", -60.0)
        item.appended = True
        self.assertIsNone(calibration.predict(item, 83.0))

    def test_uncorroborated_duration_outlier_is_refused(self):
        """Regression: a file with bonus content and no subtitles produced a
        confident +474s offset, which would have generated a cut of pure
        featurette. It must be refused instead."""
        items = [self._input(f"S08E{i:02d}", -60.0) for i in range(1, 5)]
        rogue = self._input("S08E05", -60.0, cues=False)
        rogue.duration += 600.0          # bonus feature welded on
        items.append(rogue)
        results, _ = calibration.calibrate(items)
        self.assertFalse(results["S08E05"].confident)
        self.assertEqual(results["S08E05"].source, "unresolved")
        self.assertIsNone(results["S08E05"].offset)

    def test_duration_only_accepted_when_it_matches_its_peers(self):
        items = [self._input(f"S01E{i:02d}", -60.0) for i in range(1, 5)]
        quiet = self._input("S01E05", -60.0, cues=False)
        items.append(quiet)
        results, _ = calibration.calibrate(items)
        self.assertTrue(results["S01E05"].confident)
        self.assertAlmostEqual(results["S01E05"].offset, -60.0, delta=8.0)


class TestCharacterResolution(unittest.TestCase):
    def setUp(self):
        self.episodes = [fixtures.make_episode(1, 1, [
            (0, 60, ["Daenerys Targaryen", "Jorah Mormont"]),
            (60, 120, ["Jon Snow", "Jon Arryn"]),
        ])]
        self.dir = tempfile.TemporaryDirectory()
        Path(self.dir.name, "aliases.json").write_text(
            json.dumps({"_note": "x", "dany": "Daenerys Targaryen"}), encoding="utf-8")

    def tearDown(self):
        self.dir.cleanup()

    def _resolve(self, q):
        return resolve_character(q, self.episodes, Path(self.dir.name))

    def test_exact_match(self):
        self.assertEqual(self._resolve("Daenerys Targaryen")[0], "Daenerys Targaryen")

    def test_case_insensitive(self):
        self.assertEqual(self._resolve("daenerys targaryen")[0], "Daenerys Targaryen")

    def test_nickname_via_alias_table(self):
        self.assertEqual(self._resolve("dany")[0], "Daenerys Targaryen")

    def test_unique_whole_word(self):
        self.assertEqual(self._resolve("Jorah")[0], "Jorah Mormont")

    def test_ambiguous_refuses_and_suggests(self):
        resolved, suggestions = self._resolve("Jon")
        self.assertIsNone(resolved)          # must not silently pick one
        self.assertIn("Jon Snow", suggestions)
        self.assertIn("Jon Arryn", suggestions)

    def test_unknown_returns_nothing_resolved(self):
        self.assertIsNone(self._resolve("Gandalf")[0])


@unittest.skipUnless(fixtures.have_ffmpeg(), "ffmpeg/ffprobe not on PATH")
class TestAgainstSyntheticMedia(unittest.TestCase):
    """End-to-end against generated colour bars. No real footage involved."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.paths = fixtures.make_library(cls.root, episodes=2, seconds=30)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_scan_finds_and_matches_every_file(self):
        result = scan(self.root)
        self.assertEqual(result.root_state, "ok")
        self.assertEqual(set(result.matched), {(1, 1), (1, 2)})
        self.assertEqual(result.unmatched, [])

    def test_probe_reports_a_plausible_duration(self):
        from edl_cut.media import probe_duration
        self.assertAlmostEqual(probe_duration(self.paths[(1, 1)]), 30.0, delta=1.5)

    def test_missing_directory_is_distinguished_from_empty(self):
        self.assertEqual(scan(self.root / "nope").root_state, "missing")
        empty = self.root / "empty"
        empty.mkdir()
        self.assertIn(scan(empty).root_state, {"empty", "not-mounted"})

    def test_generated_edl_opens_in_mpv(self):
        import shutil as _sh
        import subprocess
        if not _sh.which("mpv"):
            self.skipTest("mpv not installed")
        seg = scenelist.Segment("S01E01", 5.0, 15.0, "l", [])
        resolved, _ = emit.resolve([seg], self.paths, {"S01E01": 0.0})
        edl = self.root / "t.edl"
        edl.write_text(emit.to_mpv_edl(resolved), encoding="utf-8")
        proc = subprocess.run(
            ["mpv", "--no-config", "--vo=null", "--ao=null", "--frames=1", str(edl)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()


class TestVendoredDataset(unittest.TestCase):
    """Guards the vendored data itself.

    The dataset is committed, so it can be checked in CI. These assertions are
    deliberately about *shape and scale*, not exact numbers, so that re-vendoring
    a corrected upstream release does not fail the build — but silently shipping
    a truncated or restructured file does.
    """

    @classmethod
    def setUpClass(cls):
        from edl_cut.dataset import load_episodes
        cls.episodes = load_episodes()

    def test_all_episodes_present(self):
        self.assertEqual(len(self.episodes), 73)

    def test_episodes_are_sorted_and_uniquely_coded(self):
        codes = [e.code for e in self.episodes]
        self.assertEqual(codes, sorted(codes))
        self.assertEqual(len(codes), len(set(codes)))

    def test_every_episode_has_scenes(self):
        for episode in self.episodes:
            with self.subTest(episode=episode.code):
                self.assertGreater(len(episode.scenes), 5)

    def test_scenes_are_well_formed(self):
        for episode in self.episodes:
            for scene in episode.scenes:
                self.assertGreater(scene.end, scene.start, episode.code)

    def test_stated_lengths_are_precise_seconds_not_rounded_minutes(self):
        """If upstream ever rounds this field, the duration predictor breaks."""
        lengths = [e.stated_length for e in self.episodes if e.stated_length]
        self.assertGreater(len(lengths), 70)
        self.assertGreater(sum(1 for x in lengths if x % 60), 60)

    def test_character_presence_is_populated(self):
        from edl_cut.dataset import character_names
        names = character_names(self.episodes)
        self.assertGreater(len(names), 300)
        self.assertIn("Daenerys Targaryen", names)

    def test_aliases_resolve_against_the_real_dataset(self):
        for query, expected in (("dany", "Daenerys Targaryen"),
                                ("the hound", "Sandor Clegane"),
                                ("Emilia Clarke", "Daenerys Targaryen")):
            with self.subTest(query=query):
                self.assertEqual(resolve_character(query, self.episodes)[0], expected)

    def test_every_alias_points_at_a_real_character(self):
        """A typo in aliases.json would otherwise fail only when someone used it."""
        from edl_cut.dataset import DATA_DIR, character_names
        names = set(character_names(self.episodes))
        aliases = json.loads((DATA_DIR / "aliases.json").read_text(encoding="utf-8"))
        for key, target in aliases.items():
            if key.startswith("_"):
                continue
            with self.subTest(alias=key):
                self.assertIn(target, names)

    def test_a_character_cut_is_the_expected_scale(self):
        segments = scenelist.build(self.episodes, "Daenerys Targaryen")
        hours = sum(s.duration for s in segments) / 3600
        self.assertGreater(len(segments), 100)
        self.assertTrue(7.0 < hours < 12.0, f"{hours:.2f}h")


class TestExportPlanning(unittest.TestCase):
    """Cut-point planning, which is where the exporter's correctness lives."""

    def setUp(self):
        from edl_cut import export
        self.export = export
        self.path = Path("/m/a.mkv")
        self.keyframes = [float(t) for t in range(0, 600, 10)]   # GOP = 10s

    def _seg(self):
        return scenelist.Segment("S01E01", 0, 0, "l", [])

    def test_copy_snaps_backward_to_the_preceding_keyframe(self):
        pieces, drift = self.export.plan_segment(
            self._seg(), self.path, 47.0, 100.0, self.keyframes, "copy")
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0].start, 40.0)      # not 47
        self.assertFalse(pieces[0].reencode)
        self.assertAlmostEqual(drift, 7.0)

    def test_copy_never_seeks_forward_and_loses_footage(self):
        # Snapping to 50 would silently drop 3s the user asked for.
        pieces, _ = self.export.plan_segment(
            self._seg(), self.path, 47.0, 100.0, self.keyframes, "copy")
        self.assertLessEqual(pieces[0].start, 47.0)

    def test_precise_splits_into_encoded_head_and_copied_tail(self):
        pieces, drift = self.export.plan_segment(
            self._seg(), self.path, 47.0, 100.0, self.keyframes, "precise")
        self.assertEqual(len(pieces), 2)
        self.assertEqual(drift, 0.0)
        head, tail = pieces
        self.assertTrue(head.reencode)
        self.assertEqual((head.start, head.end), (47.0, 50.0))
        self.assertFalse(tail.reencode)
        self.assertEqual((tail.start, tail.end), (50.0, 100.0))

    def test_precise_is_frame_accurate_and_contiguous(self):
        pieces, _ = self.export.plan_segment(
            self._seg(), self.path, 47.0, 100.0, self.keyframes, "precise")
        self.assertEqual(pieces[0].start, 47.0)
        self.assertEqual(pieces[-1].end, 100.0)
        self.assertEqual(pieces[0].end, pieces[1].start)   # no gap, no overlap

    def test_precise_pure_copies_when_start_is_already_a_keyframe(self):
        pieces, _ = self.export.plan_segment(
            self._seg(), self.path, 50.0, 100.0, self.keyframes, "precise")
        self.assertEqual(len(pieces), 1)
        self.assertFalse(pieces[0].reencode)

    def test_precise_encodes_wholly_when_no_keyframe_falls_inside(self):
        pieces, _ = self.export.plan_segment(
            self._seg(), self.path, 41.0, 49.0, self.keyframes, "precise")
        self.assertEqual(len(pieces), 1)
        self.assertTrue(pieces[0].reencode)
        self.assertEqual((pieces[0].start, pieces[0].end), (41.0, 49.0))

    def test_reencode_mode_never_stream_copies(self):
        pieces, drift = self.export.plan_segment(
            self._seg(), self.path, 47.0, 100.0, self.keyframes, "reencode")
        self.assertEqual(len(pieces), 1)
        self.assertTrue(pieces[0].reencode)
        self.assertEqual(drift, 0.0)

    def test_precise_reencodes_only_a_small_fraction(self):
        resolved = [
            (self._seg(), self.path, float(s) + 3.0, float(s) + 120.0)
            for s in range(0, 400, 130)
        ]
        self.export.keyframe_times = lambda p: self.keyframes   # avoid probing
        plan = self.export.build_plan(resolved, "precise")
        self.assertGreater(plan.total_seconds, 0)
        self.assertLess(plan.reencoded_fraction, 0.10)


@unittest.skipUnless(fixtures.have_ffmpeg(), "ffmpeg/ffprobe not on PATH")
class TestExportAgainstSyntheticMedia(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from edl_cut import export
        cls.export = export
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        # GOP forced to 5s so keyframe positions are known exactly.
        cls.clip = fixtures.make_clip(cls.root / "clip.mkv", seconds=40, gop=5, rate=10)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_keyframes_are_found_at_the_forced_interval(self):
        times = self.export.keyframe_times(self.clip)
        self.assertGreaterEqual(len(times), 5)
        self.assertAlmostEqual(times[0], 0.0, delta=0.2)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertAlmostEqual(sum(gaps) / len(gaps), 5.0, delta=1.0)

    def test_probe_streams_reports_usable_parameters(self):
        info = self.export.probe_streams(self.clip)
        self.assertIsNotNone(info)
        self.assertEqual((info.width, info.height), (320, 180))
        self.assertEqual(info.codec, "h264")

    def test_precise_export_has_the_requested_duration(self):
        """The whole point: a cut that starts where it was asked to."""
        from edl_cut.media import probe_duration
        seg = scenelist.Segment("S01E01", 0, 0, "l", [])
        resolved = [(seg, self.clip, 7.0, 17.0), (seg, self.clip, 22.0, 29.0)]
        plan = self.export.build_plan(resolved, "precise")
        out = self.root / "out.mkv"
        self.export.run(plan, out, self.root / "work")
        self.assertTrue(out.exists())
        self.assertAlmostEqual(probe_duration(out), 17.0, delta=1.0)  # 10 + 7

    def test_copy_mode_overshoots_because_it_snaps_to_keyframes(self):
        """Documents the tradeoff rather than pretending it does not exist."""
        from edl_cut.media import probe_duration
        seg = scenelist.Segment("S01E01", 0, 0, "l", [])
        resolved = [(seg, self.clip, 7.0, 17.0)]
        plan = self.export.build_plan(resolved, "copy")
        self.assertTrue(plan.drift)
        out = self.root / "copy.mkv"
        self.export.run(plan, out, self.root / "work2")
        self.assertGreater(probe_duration(out), 10.5)

    def test_preflight_reports_size_and_space(self):
        seg = scenelist.Segment("S01E01", 0, 0, "l", [])
        result = self.export.preflight(
            [(seg, self.clip, 0.0, 10.0)], self.root / "x.mkv", "precise")
        self.assertTrue(result.ok, result.messages)
        self.assertGreater(result.estimated_bytes, 0)
        self.assertEqual(len(result.groups), 1)


@unittest.skipUnless(fixtures.have_ffmpeg(), "ffmpeg/ffprobe not on PATH")
class TestOutlierNormalisation(unittest.TestCase):
    """A single differently-sized file must not abort the whole export.

    This is drawn from the reference library, where 72 of 73 files are
    1920x1080 and one is 1888x1080 — enough to break the concat demuxer.
    """

    @classmethod
    def setUpClass(cls):
        from edl_cut import export
        cls.export = export
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.normal = [
            fixtures.make_clip(cls.root / f"n{i}.mkv", seconds=20, gop=5, rate=10)
            for i in range(3)
        ]
        # Same codec and audio, deliberately different width.
        cls.odd = cls.root / "odd.mkv"
        import subprocess
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y",
             "-f", "lavfi", "-i", "testsrc=size=300x180:rate=10:duration=20",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
             "-c:a", "aac", "-shortest", str(cls.odd)],
            check=True, capture_output=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _resolved(self):
        seg = scenelist.Segment("S01E01", 0, 0, "l", [])
        return ([(seg, p, 3.0, 11.0) for p in self.normal]
                + [(seg, self.odd, 3.0, 11.0)])

    def test_preflight_identifies_the_minority_as_the_outlier(self):
        check = self.export.preflight(self._resolved(), self.root / "o.mkv", "precise")
        self.assertTrue(check.ok, check.messages)
        self.assertEqual(check.outliers, {self.odd})
        self.assertEqual((check.target.width, check.target.height), (320, 180))

    def test_copy_mode_cannot_conform_and_says_so(self):
        check = self.export.preflight(self._resolved(), self.root / "o.mkv", "copy")
        self.assertFalse(check.ok)
        self.assertTrue(any("cannot conform" in m for m in check.messages))

    def test_plan_marks_the_outlier_for_rescaling(self):
        check = self.export.preflight(self._resolved(), self.root / "o.mkv", "precise")
        plan = self.export.build_plan(self._resolved(), "precise",
                                      normalise=check.outliers, target=check.target)
        scaled = [p for p in plan.pieces if p.scale]
        self.assertTrue(scaled)
        self.assertTrue(all(p.source == self.odd for p in scaled))
        self.assertEqual(scaled[0].scale, (320, 180))
        self.assertEqual(plan.normalised, {self.odd.name})

    def test_export_with_a_conformed_outlier_produces_one_file(self):
        from edl_cut.media import probe_duration
        check = self.export.preflight(self._resolved(), self.root / "o.mkv", "precise")
        plan = self.export.build_plan(self._resolved(), "precise",
                                      normalise=check.outliers, target=check.target)
        out = self.root / "combined.mkv"
        self.export.run(plan, out, self.root / "w")
        self.assertTrue(out.exists())
        # Four 8-second segments, concatenated.
        self.assertAlmostEqual(probe_duration(out), 32.0, delta=2.0)
        info = self.export.probe_streams(out)
        self.assertEqual((info.width, info.height), (320, 180))


class TestSeekStrategy(unittest.TestCase):
    """Pins the distinction between how re-encoded and copied pieces are cut.

    Collapsing these back into one strategy reintroduces wrong-length output
    that plays fine and shows the wrong footage, so the shape of each command
    is asserted directly.
    """

    def setUp(self):
        from edl_cut import export
        self.export = export
        self.part = Path("/tmp/part.mkv")

    def _cmd(self, piece):
        return self.export.seek_command(piece, ["-c", "copy"], self.part)

    def _ss_values(self, cmd):
        return [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "-ss"]

    def test_reencode_uses_a_single_input_side_seek(self):
        from edl_cut.export import Piece
        cmd = self._cmd(Piece(Path("/m/a.mkv"), 100.0, 103.0, True, anchor=95.0))
        self.assertEqual(self._ss_values(cmd), ["100.000"])
        # The one -ss must precede -i, i.e. be an input option.
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))

    def test_reencode_ignores_the_anchor(self):
        """An output-side seek here leaves timestamps unrebased."""
        from edl_cut.export import Piece
        cmd = self._cmd(Piece(Path("/m/a.mkv"), 100.0, 103.0, True, anchor=95.0))
        self.assertNotIn("95.000", cmd)
        self.assertNotIn("5.000", cmd)

    def test_copy_uses_two_seeks_straddling_the_input(self):
        from edl_cut.export import Piece
        cmd = self._cmd(Piece(Path("/m/a.mkv"), 100.0, 160.0, False, anchor=95.0))
        self.assertEqual(self._ss_values(cmd), ["95.000", "5.000"])
        first, second = [i for i, t in enumerate(cmd) if t == "-ss"]
        index = cmd.index("-i")
        self.assertLess(first, index)      # input-side: lands on a keyframe
        self.assertGreater(second, index)  # output-side: accurate remainder

    def test_copy_without_a_usable_anchor_still_emits_one_seek(self):
        from edl_cut.export import Piece
        cmd = self._cmd(Piece(Path("/m/a.mkv"), 100.0, 160.0, False, anchor=None))
        self.assertEqual(self._ss_values(cmd), ["100.000"])

    def test_length_is_always_bounded_after_the_input(self):
        from edl_cut.export import Piece
        for reencode in (True, False):
            with self.subTest(reencode=reencode):
                cmd = self._cmd(
                    Piece(Path("/m/a.mkv"), 100.0, 160.0, reencode, anchor=95.0))
                self.assertIn("-t", cmd)
                self.assertEqual(cmd[cmd.index("-t") + 1], "60.000")
                self.assertGreater(cmd.index("-t"), cmd.index("-i"))
