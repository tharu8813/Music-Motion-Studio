from __future__ import annotations

import csv
import json
from dataclasses import replace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from app.models.playlist import PlaylistTrack
from app.timeline.models import TransitionType
from app.timeline.render_plan import (
    AudioRenderClip, AudioRenderPlan, AudioRenderTransition, CompiledRenderPlan, TempoRamp, TransitionDsp,
    build_presentation_and_metadata,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from automix_listening_report import RATING_COLUMNS, write_blind_comparison, write_rating_sheet  # noqa: E402


class RatingSheetTests(unittest.TestCase):
    def test_rating_sheet_includes_cuts_and_skips_explicit_gaps(self) -> None:
        rows = [
            {"index": 1, "from": "첫 곡", "to": "B", "timeline_start": 185.4, "duration": 16.0,
             "dsp": "bass_swap", "type": "beat_match"},
            {"index": 2, "from": "B", "to": "C", "timeline_start": 400.0, "duration": 0.0,
             "dsp": "", "type": "sequential"},
            {"index": 3, "from": "C", "to": "D", "timeline_start": 610.0, "duration": 3.0,
             "dsp": "", "type": "crossfade"},
            {"index": 4, "from": "D", "to": "E", "timeline_start": 850.0, "duration": 0.0,
             "dsp": "", "type": "gap"},
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "ratings.csv"
            write_rating_sheet(path, rows)
            with path.open(encoding="utf-8-sig", newline="") as file:
                sheet = list(csv.DictReader(file))

        self.assertEqual([row["index"] for row in sheet], ["1", "2", "3"])
        self.assertEqual(sheet[0]["from"], "첫 곡")
        self.assertEqual(sheet[0]["start"], "3:05")
        self.assertEqual(sheet[0]["style"], "bass_swap")
        self.assertEqual(sheet[1]["style"], "sequential")
        self.assertEqual(sheet[2]["style"], "crossfade")
        self.assertTrue(all(row[column] == "" for row in sheet for column in RATING_COLUMNS))

    def test_blind_pair_keeps_tempo_gain_and_filters_and_excludes_previous_transition(self) -> None:
        first = AudioRenderClip("intro", "intro", 0.0, 0.0, 12.0)
        outgoing = AudioRenderClip("a", "a", 10.0, 0.0, 7.0, gain=0.8, tempo_ramp=TempoRamp(1.0, 2.0, 1.1))
        cue = outgoing.timeline_end - 2.0
        incoming = AudioRenderClip("b", "b", cue, 0.0, 14.0)
        clips = first, outgoing, incoming
        previous = AudioRenderTransition("intro", "a", 10.0, 2.0, TransitionType.EQUAL_POWER, TransitionDsp.SHORT_FADE)
        chosen = AudioRenderTransition("a", "b", cue, 2.0, TransitionType.BEAT_MATCH, TransitionDsp.VOCAL_SAFE_EQ,
                                       vocal_handoff=0.6)
        presentation, metadata, duration = build_presentation_and_metadata(clips)
        plan = CompiledRenderPlan(AudioRenderPlan(clips, (previous, chosen)), presentation, metadata, duration)
        tracks = [PlaylistTrack("intro.wav", "Intro", id="intro"), PlaylistTrack("a.wav", "첫 곡", id="a"),
                  PlaylistTrack("b.wav", "B", id="b", volume_db=-3.0, bass_db=6.0)]
        pipeline = Mock()
        pipeline.render.return_value = SimpleNamespace(path=Path("mix.flac"))

        def fake_excerpt(ffmpeg, source, output, start, duration):
            output.write_bytes(b"audio")

        with TemporaryDirectory() as directory, patch("automix_listening_report.write_excerpt", side_effect=fake_excerpt) as excerpt:
            out_dir = Path(directory)
            self.assertEqual(write_blind_comparison(Path("ffmpeg"), pipeline, plan, tracks, out_dir), 1)
            with ZipFile(out_dir / "blind.zip") as bundle:
                self.assertEqual(set(bundle.namelist()), {"02_A.flac", "02_B.flac", "README.txt", "ratings.csv"})
                [rating] = list(csv.DictReader(bundle.read("ratings.csv").decode("utf-8-sig").splitlines()))
            key = json.loads((out_dir / "blind-key.json").read_text(encoding="utf-8"))

        self.assertEqual(rating["from"], "첫 곡")
        self.assertAlmostEqual(float(rating["transition_at_s"]), cue - first.timeline_end, places=4)
        self.assertEqual({item["version"] for item in key}, {"automix", "matched_fade"})
        self.assertEqual({item["file"] for item in key}, {"02_A.flac", "02_B.flac"})
        renders = {call.args[0].transitions[0].dsp: call for call in pipeline.render.call_args_list}
        actual = renders[TransitionDsp.VOCAL_SAFE_EQ].args[0]
        control = renders[TransitionDsp.SHORT_FADE].args[0]
        self.assertEqual(control, replace(actual, transitions=(replace(actual.transitions[0],
                                                      dsp=TransitionDsp.SHORT_FADE, vocal_handoff=None),)))
        self.assertEqual(actual.clips[0].tempo_ramp, outgoing.tempo_ramp)
        self.assertEqual(actual.clips[0].gain, 0.8)
        for call in renders.values():
            self.assertEqual(call.kwargs["track_filters"]["b"], tracks[2].audio_filter)
            self.assertEqual(call.kwargs["resting_dsp"], {"a": (True, False), "b": (True, False)})
        for call in excerpt.call_args_list:
            self.assertAlmostEqual(call.args[3], first.timeline_end - outgoing.timeline_start)


if __name__ == "__main__":
    unittest.main()
