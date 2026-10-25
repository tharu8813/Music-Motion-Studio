"""Subtitle exports follow audio placements and keep both lyrics during overlaps."""

from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import subprocess
import unittest
from unittest.mock import PropertyMock, patch

from PySide6.QtWidgets import QDialog, QMessageBox

from app.dialogs.export_settings_dialog import ExportSettingsDialog
from app.models.playlist import PlaylistTrack
from app.renderer.ffmpeg_renderer import FFmpegRenderer, FFmpegNotFoundError, RenderCancelledError, RenderSettings
from app.services.lyrics_service import LyricsService, LyricsError
from app.services.subtitle_export_service import SubtitleCue, SubtitleExportService
from app.timeline.render_plan import AudioRenderClip, AudioRenderPlan, CompiledRenderPlan, TempoRamp, build_presentation_and_metadata
from tests.main_window_base import MainWindowTestCase
from app.utils.subprocess_utils import hidden_process_kwargs


def plan_for(clips):
    presentation, metadata, duration = build_presentation_and_metadata(clips)
    return CompiledRenderPlan(AudioRenderPlan(tuple(clips)), presentation, metadata, duration)


def overlapping_tracks():
    tracks = [PlaylistTrack("a.wav", "A", duration_seconds=10, id="a",
                            lyrics=[{"start": 0, "end": 10, "text": "a 가사"}]),
              PlaylistTrack("b.wav", "B", duration_seconds=10, id="b",
                            lyrics=[{"start": 0, "end": 10, "text": "b 가사"}])]
    plan = plan_for([AudioRenderClip("a", "a", 0, 0, 10), AudioRenderClip("b", "b", 8, 0, 10)])
    return tracks, plan


class SubtitleTimingTests(unittest.TestCase):
    def test_superseded_lyrics_do_not_reappear_after_a_shorter_cue_ends(self):
        track = PlaylistTrack("song.wav", "Song", duration_seconds=10, lyrics=[
            {"start": 0, "end": 10, "text": "First"},
            {"start": 5, "end": 6, "text": "Later"},
        ])
        from app.timeline.compiler import compile_playlist

        cues = SubtitleExportService.cues([track], compile_playlist([track]))
        self.assertEqual(cues, [SubtitleCue(0, 5, "First"), SubtitleCue(5, 6, "Later")])
        for cue in cues:
            self.assertEqual(LyricsService.current_text(track.lyrics, (cue.start + cue.end) / 2), cue.text)
        self.assertFalse(any(cue.start <= 7 < cue.end for cue in cues))

    def test_overlap_is_outgoing_above_incoming_then_returns_to_single_line(self):
        tracks, plan = overlapping_tracks()
        self.assertEqual(SubtitleExportService.cues(tracks, plan), [
            SubtitleCue(0, 8, "a 가사"), SubtitleCue(8, 10, "- a 가사\n- b 가사"), SubtitleCue(10, 18, "b 가사"),
        ])
        reversed_plan = replace(plan, audio=AudioRenderPlan(tuple(reversed(plan.audio.clips))))
        self.assertEqual(SubtitleExportService.cues(tracks, reversed_plan), SubtitleExportService.cues(tracks, plan))

    def test_lyrics_changing_during_overlap_split_both_rows_at_the_right_time(self):
        tracks, plan = overlapping_tracks()
        tracks[1].lyrics = [{"start": 0, "end": 1, "text": "b 첫 줄"}, {"start": 1, "end": 10, "text": "b 다음 줄"}]
        cues = SubtitleExportService.cues(tracks, plan)
        self.assertEqual(cues[1:3], [SubtitleCue(8, 9, "- a 가사\n- b 첫 줄"), SubtitleCue(9, 10, "- a 가사\n- b 다음 줄")])

    def test_trim_speed_and_positive_lyric_offset_use_source_time(self):
        track = PlaylistTrack("a.wav", "A", duration_seconds=15, id="a", lyrics_timing_offset_seconds=1,
                              lyrics=[{"start": 7, "end": 11, "text": "timed"}])
        plan = plan_for([AudioRenderClip("a", "a", 10, 5, 15, playback_rate=2)])
        self.assertEqual(SubtitleExportService.cues([track], plan), [SubtitleCue(10.5, 12.5, "timed")])

    def test_tempo_ramp_changes_subtitle_times_using_the_audio_rate_segments(self):
        track = PlaylistTrack("a.wav", "A", duration_seconds=10, id="a", lyrics=[{"start": 5, "end": 9, "text": "ramp"}])
        clip = AudioRenderClip("a", "a", 0, 0, 10, tempo_ramp=TempoRamp(4, 8, 2, steps=2))
        cues = SubtitleExportService.cues([track], plan_for([clip]))
        self.assertEqual(cues[0].start, 4.8)
        self.assertAlmostEqual(cues[0].end, 7.243, places=3)

    def test_disabled_and_trimmed_out_lyrics_are_excluded(self):
        tracks, plan = overlapping_tracks()
        tracks[0].enabled = False
        tracks[1].lyrics = [{"start": 30, "end": 40, "text": "outside"}]
        self.assertEqual(SubtitleExportService.cues(tracks, plan), [])

    def test_srt_and_lrc_store_real_line_breaks_and_clear_gaps(self):
        tracks, plan = overlapping_tracks()
        with TemporaryDirectory() as directory:
            service = SubtitleExportService()
            for extension in ("srt", "lrc"):
                target = service.export(tracks, Path(directory) / f"mix.{extension}", plan)
                text = target.read_text(encoding="utf-8")
                self.assertIn("- a 가사\n- b 가사", text)
                self.assertNotIn("\\n", text)
                parsed = LyricsService.load(target)
                self.assertEqual(LyricsService.current_text(parsed, 9), "- a 가사\n- b 가사")
                self.assertEqual(LyricsService.current_text(parsed, 10), "b 가사")
                self.assertEqual(LyricsService.current_text(parsed, 18), "")
            lrc = service.format_lrc([SubtitleCue(0, 1, "first"), SubtitleCue(3, 4, "later")])
            parsed = LyricsService._parse_lrc(lrc)
            self.assertEqual(LyricsService.current_text(parsed, 2), "")
            leading_gap = service.format_lrc([SubtitleCue(3, 4, "later")])
            parsed = LyricsService._parse_lrc(leading_gap)
            self.assertEqual(parsed[LyricsService.display_cue_index(parsed, 0)]["text"], "")

    def test_save_failure_preserves_existing_subtitle(self):
        tracks, plan = overlapping_tracks()
        with TemporaryDirectory() as directory:
            target = Path(directory) / "mix.srt"
            target.write_text("original", encoding="utf-8")
            with patch.object(Path, "replace", side_effect=OSError("locked")):
                with self.assertRaises(LyricsError):
                    SubtitleExportService().export(tracks, target, plan)
            self.assertEqual(target.read_text(), "original")
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_multiline_source_does_not_create_invalid_srt_block_separators(self):
        tracks, plan = overlapping_tracks()
        tracks[0].lyrics[0]["text"] = "a 첫 줄\n\n다음 줄"
        cues = SubtitleExportService.cues(tracks, plan)
        self.assertEqual(cues[0].text, "a 첫 줄\n다음 줄")
        content = SubtitleExportService.format_srt([SubtitleCue(3601, 3602, cues[0].text)])
        self.assertIn("01:00:01,000 --> 01:00:02,000", content)
        self.assertEqual(len(LyricsService._parse_srt(content)), 1)


@unittest.skipUnless(os.environ.get("PLAYLIST_CANVAS_TEST_FFMPEG"), "Set PLAYLIST_CANVAS_TEST_FFMPEG for real mixing")
class MixedSubtitleIntegrationTests(unittest.TestCase):
    def test_subtitle_overlap_matches_the_plan_of_a_real_crossfade_render(self):
        renderer = FFmpegRenderer(os.environ["PLAYLIST_CANVAS_TEST_FFMPEG"])
        with TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            root = Path(directory)
            source = root / "source.wav"
            subprocess.run([str(renderer.executable), "-v", "error", "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=2", "-y", str(source)], check=True,
                           **hidden_process_kwargs())
            tracks = [PlaylistTrack(str(source), title, duration_seconds=2,
                                    lyrics=[{"start": 0, "end": 2, "text": title}]) for title in ("a 가사", "b 가사")]
            plans = []
            renderer.prepare_playlist_audio(tracks, root, RenderSettings(), transition_mode="crossfade",
                                            crossfade_seconds=0.5, audio_codec="flac", plan_callback=plans.append)
            self.assertAlmostEqual(plans[0].duration_seconds, 3.5)
            for extension in ("srt", "lrc"):
                path = SubtitleExportService().export(tracks, root / f"mix.{extension}", plans[0])
                cues = LyricsService.load(path)
                self.assertEqual(LyricsService.current_text(cues, 1.4), "a 가사")
                self.assertEqual(LyricsService.current_text(cues, 1.6), "- a 가사\n- b 가사")
                self.assertEqual(LyricsService.current_text(cues, 2.1), "b 가사")
                self.assertEqual(LyricsService.current_text(cues, 3.5), "")


class SubtitleExportUiTests(MainWindowTestCase):
    def test_subtitle_formats_and_timeline_export_name_are_visible(self):
        dialog = ExportSettingsDialog(self.window.settings_service.current, 2, 20, self.window.translator, Path("mix.mp4"))
        try:
            dialog.export_type_combo.setCurrentIndex(2)
            self.assertTrue(dialog.subtitle_only)
            self.assertEqual([dialog.format_combo.itemData(i) for i in range(dialog.format_combo.count())], ["lrc", "srt"])
            self.assertTrue(dialog.render_group.isHidden())
            self.assertTrue(dialog.advanced_group.isHidden())
            self.assertEqual(self.window.playlist_files_action.text(), "타임라인 내보내기")
        finally:
            dialog.close()

    def test_unmixed_subtitles_export_without_ffmpeg(self):
        tracks, _plan = overlapping_tracks()
        self.window.playlist_service.replace(tracks)
        self.window.project_settings.transition_mode = "none"
        with TemporaryDirectory() as directory:
            output = Path(directory) / "mix.srt"
            with patch.object(ExportSettingsDialog, "exec", return_value=QDialog.DialogCode.Accepted), \
                 patch.object(ExportSettingsDialog, "output_path", new_callable=PropertyMock, return_value=output), \
                 patch("app.controllers.export_controller.FFmpegRenderer", side_effect=FFmpegNotFoundError()) as renderer, \
                 patch.object(self.window.export_orchestrator, "show_complete_dialog") as complete:
                self.window._export_video()
            renderer.assert_not_called()
            complete.assert_called_once()
            self.assertIn("a 가사", output.read_text(encoding="utf-8"))

    def test_mixed_subtitle_export_uses_the_prepared_plan_and_unlocks_the_editor(self):
        tracks, plan = overlapping_tracks()
        self.window.playlist_service.replace(tracks)
        self.window.project_settings.transition_mode = "crossfade"
        with TemporaryDirectory() as directory:
            output = Path(directory) / "mix.srt"
            with patch.object(ExportSettingsDialog, "exec", return_value=QDialog.DialogCode.Accepted), \
                 patch.object(ExportSettingsDialog, "output_path", new_callable=PropertyMock, return_value=output), \
                 patch("app.controllers.export_controller.FFmpegRenderer"), \
                 patch("app.controllers.export_controller.prepare_audio_for_ui", return_value=(Path("audio.flac"), plan)) as prepare, \
                 patch.object(self.window.export_orchestrator, "show_complete_dialog"):
                self.window._export_video()
            prepare.assert_called_once()
            self.assertIn("00:00:08,000 --> 00:00:10,000\n- a 가사\n- b 가사", output.read_text(encoding="utf-8"))
            self.assertIsNone(self.window._export_preparation_cancel)
            self.assertTrue(self.window.centralWidget().isEnabled())

    def test_cancel_mix_timing_does_not_replace_existing_subtitle(self):
        tracks, _plan = overlapping_tracks()
        self.window.project_settings.transition_mode = "crossfade"
        with TemporaryDirectory() as directory:
            output = Path(directory) / "mix.lrc"
            output.write_text("original", encoding="utf-8")
            with patch("app.controllers.export_controller.prepare_audio_for_ui", side_effect=RenderCancelledError()), \
                 patch.object(self.window.export_orchestrator, "show_complete_dialog") as complete:
                self.window.export_orchestrator.export_subtitles(tracks, str(output), object())
            self.assertEqual(output.read_text(), "original")
            complete.assert_not_called()
            self.assertIsNone(self.window._export_preparation_cancel)
            self.assertTrue(self.window.centralWidget().isEnabled())


if __name__ == "__main__":
    unittest.main()
