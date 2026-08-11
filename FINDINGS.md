# Calibration findings

Run against a complete 73-episode 1080p x265 library (a single mixed-source
release: seasons 1–7 from Blu-ray with sidecar `.srt`, season 8 from WEB with
embedded subtitles).

This is written as a narrative, in the order things were discovered, because
three of the four approaches below failed in instructive ways and the failures
are the useful part. Later sections supersede earlier ones.

## The headline

**The duration table, read the obvious way, gives the wrong answer.**

Every episode's local file is about **+197 seconds longer** than the dataset's
stated runtime, with a spread of only 33s across 64 episodes. The naive reading
is "apply a +197s offset." That would break every cut in the library.

It would also be easy to misread as a *speedup*: the ratio `local/stated` sits
around 1.06, close enough to PAL's 1.042 to tempt you. It isn't a speedup. The
ratio drifts from 1.0429 to 1.0726 while the delta stays pinned near +197 —
the signature of a fixed number of seconds being a bigger fraction of a short
episode than a long one. A genuine speedup holds the *ratio* constant, not the
delta.

What the +197s actually is: the dataset's `length` field is not file duration.
It excludes the recap and end credits that the rips include. Extra material at
the **end** of a file changes its duration while leaving every scene timestamp
untouched.

Duration alone cannot tell front-padding from back-padding. Subtitles can,
because they are timed against the user's own files.

## What the subtitle cross-check found

| Season | Offset | Action |
|---|---|---|
| S1 | ≈ 0 s, two episodes near −31 | mostly use as-is |
| S2 | ≈ 0 s | use as-is |
| S3 | ≈ 0 s | use as-is |
| S4 | ≈ 0 s, three episodes near −58 | mostly use as-is |
| S5 | −86 to −211 s | per-episode offset |
| S6 | −169 to −310 s | per-episode offset |
| S7 | −120 to −226 s | per-episode offset |
| S8 | −51 to −148 s | per-episode; see below |

Note how wide the per-episode spread is within seasons 5–7. That matters: it is
exactly what defeated the season-consensus approach described further down.

Seasons 1–4 need **no correction at all**. Seasons 5–7 need a real, growing
negative offset — the dataset's timeline runs progressively ahead of these
rips. Within an episode the two anchors often agree to within a second
(S05E09: −144.7 head, −144.5 tail), which confirms the correction is purely
additive with no scaling component.

## Season 8 is a different animal

The S8 files come from a different source than the rest of the release — `WEB`
rather than `BluRay`, embedded ASS subtitles rather than sidecars — and each
one has the ***Inside the Episode*** featurette welded onto the end. In S08E01
the subtitles stop being episode dialogue at 55:49 and become cast-and-crew
commentary, running on for another nine minutes.

That inflates the duration delta to +700–880s and makes the naive last-cue
anchor meaningless. The diagnostic now discards cues falling more than 180s
past the dataset's final scene and reports how many it dropped.

`S08E04` has no subtitle track at all, so it cannot be calibrated this way and
will need a manual offset.

## From two anchors to a real estimator

The first estimator compared the first and last subtitle cue against the first
and last scene boundary, and resolved only **30 of 73** episodes. Both extremes
are bad places to measure: "previously on" recaps are subtitled and land before
the dataset's first scene, dragging the head anchor early, while credit-roll
song lyrics disturb the tail.

The replacement uses a signal that appears dozens of times per episode and is a
property of television rather than of any one file:

> dialogue almost never spans a scene cut

Shift the dataset's boundaries by the correct offset and they fall into the
gaps between subtitle cues. Shift them wrongly and they land on dialogue at
roughly the rate dialogue occupies the episode. Sweeping the offset and scoring
*what fraction of boundaries land in silence* produces a sharp peak at the true
offset, and the peak's height above that episode's own background is the
confidence measure. It needs no knowledge of what is being said, so it
generalises to any series and any language.

Three things were needed to make it work, each found by it failing:

1. **Score only inside the subtitled span.** The first version preferred
   implausible ~+400 s offsets, because shoving every boundary past the final
   cue lands them all in a region that reads as silence for the trivial reason
   that nothing was ever timed there. Restricting the evidence window to
   `[first cue, last cue]` removed the artefact — resolution went from 0/72 to
   most episodes clustering correctly.
2. **Judge on margin, not score.** A talkative episode leaves fewer silent gaps,
   so a correct offset there may score 0.50 where a quiet episode scores 0.85.
   Only the height above that episode's own background carries information.
3. **Let the season vote.** Episodes with genuinely flat scoring curves can peak
   anywhere. Since a library is internally consistent within a rip source, the
   season elects a consensus by plain median (robust to a minority of bad
   peaks), and outliers are re-searched within ±90 s of it. This rescued
   S03E03, S03E08, S04E04, S04E06, S05E04 and S07E04, whose unconstrained peaks
   were hundreds of seconds wrong.

That reached 66 of 73 — and then the diagnostic for the remaining seven showed
the season-consensus step was itself the main problem.

## The consensus window was corrupting correct answers

Fitting each half of an episode separately is a clean test of whether a single
additive offset is even the right model. On episodes that calibrated fine, the
two halves agree to within 5 seconds. On S06E01 they agreed with each other at
−308 and −309.5 — while the cache held **−211**, because the ±90 s consensus
window had rejected the correct peak as implausible and substituted a worse one.

S6's true offsets span about 160 s, far wider than the window assumed. So the
step meant to rescue ambiguous episodes was overriding unambiguous ones. That is
the worst failure mode available: not a refusal to answer, but a confident wrong
answer.

## The second signal, which turned out to be the better one

An episode file is its content plus end credits, so

    local_duration  =  (last_scene_end + offset) + credits

Learn `credits` from the library itself and the offset falls out of **ffprobe
alone** — no subtitles, one probe per file. Measured against the subtitle
estimator across 66 episodes: **median error 2.5 s**, within 30 s for 62 of them.
Learning `credits` per season rather than library-wide matters — season 1's runs
about 12 s shorter than the rest, enough to bias its predictions.

The two signals are now combined rather than ranked: duration sets a ±45 s prior,
subtitle scoring picks the exact value inside it, and **agreement between two
independent measurements** is the confidence test. Where they disagree the
episode is flagged rather than guessed.

Both disputes on this library were then settled by frame inspection, and
**duration won both times**:

| Episode | Duration said | Subtitles said | Truth, by frame |
|---|---|---|---|
| S06E05 | −198.7 | −230.2 | **−198.7** — the Waif, Braavos |
| S07E04 | −122.2 | −164.4 | **−122.2** — Highgarden, then Winterfell |

## Season 8, and a third signal

S8 files defeat the duration predictor because the *Inside the Episode*
featurette inflates their length. Detecting the **end-credits black run** solves
it: the long black stretch before the featurette starts exactly where episode
content ends, so `offset = credits_start − last_scene_end`. Validated against the
three S8 episodes with known offsets, it reads a consistent 4–13 s low (median
10.5 s), which is a correctable bias.

That closed the last two, including **S08E04, which has no subtitle track at
all** and was previously uncalibratable by any method here.

One guard was needed: with no subtitles, nothing can detect appended material
either, so the raw predictor confidently returned **+474 s** for S08E04 — a
plausible-looking number that would have generated a cut of pure featurette. An
uncorroborated duration-only prediction is now checked against its season's
resolved episodes and refused if it is wildly out.

## Where it landed

**All 73 episodes calibrated**, 69 automatically and 4 set by hand. The Daenerys
cut is complete: **175 of 175 segments, 9.14 hours**, nothing skipped.

Two of the manual entries carry honest uncertainty. S08E03 and S08E04 rest on
credits-detection alone at roughly ±15 s — *The Long Night* is too dark to
identify frames reliably, and S08E04 has no subtitles to corroborate with. Both
are marked `"manual": true` with that caveat recorded in the cache.

## Verified against playback

The Daenerys EDL was opened in mpv, which reported a single 27,699-second
virtual file — matching the generated length exactly. Frames were then sampled
from the midpoint of one segment per season and inspected:

| Season | Offset applied | What was on screen |
|---|---|---|
| S1 | −31.5 s | Drogo's arrival at Pentos — the presentation scene |
| S4 | −1.0 s | Daenerys addressing the Unsullied |
| S5 | −104 s | Grey Worm and Missandei, Meereen |
| S6 | −196 s | Daenerys, Temple of the Dosh Khaleen |
| S6E01 | −309.7 s | the Dothraki who captured her |
| S7E01 | −226.0 s | Daenerys arriving at Dragonstone |
| S8E03 | −95.6 s | Daenerys in the dark, the Long Night |
| S7 | −120 s | Daenerys, Dragonstone throne room |
| S8 | −51 s | Winterfell great hall — Daenerys with Jon, Sansa, Tyrion |

Every sample landed in the intended scene, including the seasons carrying the
largest corrections. The offsets are real, not an artefact of the scoring.

## Consequences for the design

1. **Offsets are per-episode, not global.** No single constant serves this
   library. The config needs a per-episode override, and the cache must be
   keyed per episode.
2. **The offset model is additive.** `local = dataset + b`. No evidence of any
   multiplicative term anywhere in the library, so the affine `a` parameter can
   default to 1 and only be fitted if some other library demands it.
3. **A library can be internally inconsistent.** This one is a single download
   that silently mixes two rip sources. Calibration must be reported and
   diagnosed per season, never averaged library-wide.
4. **Never ship calibrated scene lists.** These offsets are a property of *this
   library*. Scene lists are published in dataset time and calibrated at
   generation time on the user's machine, or every file published is silently
   wrong for everyone else.


## The exporter, and why it cuts to MPEG-TS

`precise` mode re-encodes only the fragment between a requested cut point and
the next keyframe, stream-copies the remainder, and joins the pieces. Getting
the cut points right took five attempts against the reference library, and every
failure had the same shape: **the export completes, the file plays, and the
footage is wrong.**

| Attempt | Symptom | Cause |
|---|---|---|
| 1 | 7.000 s request wrote 12.020 s | input-only seek bounds the piece from the keyframe ffmpeg landed on |
| 2 | two pieces ~30 s long | a flat 30 s pre-roll survived into the output on HEVC |
| 3 | 2.92 s request wrote 6.20 s | two-stage seek on a *re-encode* leaves timestamps offset |
| 4 | 0.40 s request wrote 11.12 s | the piece kept the source's timestamps — 10 frames correct, start 9.488 s |
| 5 | sub-second fragments unreliable even with `setpts` | encoder behaviour at that scale |

The common thread only became clear at attempt four. The picture data was
*never* wrong. A 2.78 s request produced exactly 67 frames. What was wrong was
the labelling: a piece cut from 30 minutes into an episode kept saying it
started at 9.5 seconds, and Matroska stores whatever timestamps it is given, so
the join believed them and every later piece slid. That is how a 1183 s cut came
out 1246 s.

Worse, whether ffmpeg rebased the timestamps depended on the codec and on how
far back the preceding keyframe sat — fine at 3.2 s, broken at 9.6 s. So each
fix was correct for the cases visible at the time and wrong for the next one.

**MPEG-TS removes the class rather than the instance.** Pieces are cut to TS and
joined from there. TS is the broadcast format, built for streams being spliced
with mismatched clocks, so the join recomputes timing instead of trusting each
piece's own labels. A TS piece legitimately starts at a non-zero PCR and it
simply does not matter.

Measured on the case that defeated every flag — a 2.78 s head fragment whose
preceding keyframe is 9.61 s back, followed by a 20 s copy:

    joined: 22.917 s against 22.78 s expected, 424 frames out for 66 + 358 in

And end to end on a real four-segment slice spanning both the majority format
and the 1888-wide outlier:

    1183.53 s against 1183.00 s planned, +0.53 s across seven pieces

Frames sampled from the result — including two seconds past a splice, and
inside the rescaled outlier — are clean.

## What actually caught these

A check that probes every piece after writing it and compares against the plan.
It was added after the second failure and caught the third, fourth and fifth.

Without it this project would have shipped an exporter that produced a
plausible-looking 15 GB file showing the wrong scenes, and the only symptom
would have been a viewer thinking the timestamps were bad. Because the tolerance
is now measured against TS output, which lands within 0.1 s of plan, it sits at
1.0 s — far below the failures worth catching, which overshot by 5 s and 30 s.


## Season 8, and why the automated refinement missed by ten seconds

Cut-based refinement corrected 52 of 73 episodes but refused all of season 8.
Two of those, hand-verified by frame, turned out to be the worst offsets in the
library:

| Episode | Was | Is | Out by |
|---|---|---|---|
| S08E03 | −95.60 | **−105.90** | 10.3 s |
| S08E06 | −148.40 | **−136.00** | 12.4 s |

Both were derived the same way: take several scene boundaries, find the real
visual cut nearest each, and check that the implied offsets agree. S08E06 gave
−136.30, −135.71 and −135.96 from three boundaries; S08E03 matched 4 of 5
boundaries within 0.35 s. Frames confirmed both — Tyrion entering the dragonpit,
Jon in his cell, the Stark farewell at the harbour; Varys with a lantern in the
crypt, the Night King walking through fire, Theon in the godswood.

**The refinement could not have found these.** Its search window was ±8 s, sized
from seasons 1–7 where every correction was smaller than that. With an 8 s
search the detection window spans only ±9 s, so a cut 12 s away is never even
detected. The search was not converging on a wrong answer — it was choosing
among candidates that did not include the right one, which is why its thresholds
disagreed and it correctly refused.

That is a useful shape to recognise. The refusal was right, the diagnosis
("these episodes have no alignable boundaries") was wrong, and the difference
only became visible by measuring outside the range the tool was looking in.

Two fixes followed:

* **Retry wider on refusal.** When thresholds disagree, search ±20 s once more
  before giving up. The cost falls only on episodes that need it. The trigger is
  refusal rather than any property of the winning value, because the failure is
  invisible in a single result.
* **No cuts anywhere is not consensus.** If detection finds nothing at any
  threshold, every threshold trivially returns a delta of zero and they appear
  to agree unanimously. That is consensus on nothing, and it now reports no
  support rather than a confident non-answer.
